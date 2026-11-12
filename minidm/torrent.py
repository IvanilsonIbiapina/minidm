# -*- coding: utf-8 -*-
"""Downloads torrent via aria2 externo (JSON-RPC em 127.0.0.1).

Bittorrent é um protocolo próprio (DHT, peeração, semente) — mesmo
racional do yt-dlp: chamamos o BINÁRIO (`aria2c`), nunca reimplementamos
nada do protocolo. O aria2c sobe só com RPC (porta livre + token, apenas
localhost) e o downloader acompanha `aria2.tellStatus` (bytes, total,
velocidade) — sem parse de console frágil.

* `--seed-time=0`: baixa e para de semear (previsível, estilo IDM;
  semente contínua é configuração do próprio aria2);
* `--continue=true`: pausa preserva o parcial — ao retomar, re-add da
  mesma URI continua de onde parou (arquivo `.aria2` de controle);
* `--file-allocation=none`: sem pré-alocar (portável em FAT/exFAT).

Interface igual aos outros downloaders: `run()`, `request_stop()`,
`speed_bps()`; erros viram `DownloadError` (o scheduler trata).
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import urllib.request
from typing import Any, Optional

from minidm.deps import aria2_bin, hidden_kwargs
from minidm.downloader import DownloadError
from minidm.meta import Task, TaskState

# frequência do tellStatus = frequência do save de progresso (a UI
# faz polling de 1 s — não vale gravar mais rápido que isso)
POLL_SECONDS = 1.0
# subida do RPC: 40 tentativas × 0,25 s = 10 s
BOOT_TRIES = 40
BOOT_DELAY = 0.25

_MAGNET_BTIH = re.compile(r"xt=urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})",
                          re.IGNORECASE)


def is_torrent_url(url: str) -> bool:
    """`magnet:?xt=…` ou URL http(s) apontando para um `.torrent`."""
    u = (url or "").strip().lower()
    if u.startswith("magnet:?"):
        return True
    return u.split("?")[0].endswith(".torrent")


def torrent_name(url: str) -> str:
    """Nome provisório da tarefa (o aria2 entrega o nome real no fim)."""
    m = _MAGNET_BTIH.search(url or "")
    if m:
        return f"torrent-{m.group(1)[:8].lower()}"
    base = os.path.basename((url or "").split("?")[0])
    return base or "torrent"


class TorrentDownloader:
    """Roda o `aria2c` com RPC e espelha o progresso na task."""

    def __init__(self, task: Task, config=None,
                 on_progress=None, stop: Optional[threading.Event] = None,
                 **_kw):
        self.task = task
        self.config = config
        self.on_progress = on_progress
        self.stop = stop or threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._port = 0
        self._token = secrets.token_hex(16)
        self._gid: Optional[str] = None
        self._speed = 0.0
        self._speed_ts = 0.0
        self._total = 0

    # ------------------------------------------------------------------ RPC
    def _rpc(self, method: str, params: Optional[list] = None,
             timeout: float = 5.0) -> Any:
        """Chama o aria2 (JSON-RPC sobre HTTP) com o token da sessão."""
        payload = json.dumps({
            "jsonrpc": "2.0", "id": "minidm",
            "method": method,
            "params": [f"token:{self._token}"] + list(params or []),
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}/jsonrpc", data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read() or b"{}")
        if "error" in data and data["error"] is not None:
            err = data["error"]
            msg = (err.get("message") if isinstance(err, dict) else str(err)) \
                or "erro desconhecido"
            raise DownloadError(f"aria2: {msg}")
        return data.get("result")

    def _boot(self) -> None:
        """Sobe o aria2c só com RPC local e espera o socket responder."""
        bin_ = aria2_bin()
        if not bin_:
            raise DownloadError(
                "aria2 não encontrado — instale com `winget install "
                "aria2.aria2` (Windows), `apt install aria2` (Linux), "
                "`brew install aria2` (macOS) ou defina MINIDM_ARIA2")
        t = self.task
        dest = os.path.abspath(t.dest_dir or ".")
        os.makedirs(dest, exist_ok=True)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))       # porta livre = sem colisão
            self._port = s.getsockname()[1]
        cmd = [bin_,
               "--enable-rpc",
               f"--rpc-listen-port={self._port}",
               f"--rpc-secret={self._token}",
               "--rpc-listen-all=false",   # só 127.0.0.1
               "--dir=" + dest,
               "--seed-time=0",
               "--continue=true",
               "--file-allocation=none",
               "--summary-interval=0",     # sem resumo no console
               "--console-log-level=warn",
               "--bt-max-peers=64",
               "-j", "4"]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            **hidden_kwargs())
        for _ in range(BOOT_TRIES):
            if self.stop.is_set():
                raise DownloadError("interrompido (antes de baixar)")
            try:
                # `aria2.getVersion` (e NÃO `system.getVersion`): builds
                # reais do aria2 (ex.: winget 1.37.0) só publicam o
                # prefixo `aria2.` — system.* tem só multicall/listMethods/
                # listNotifications. O nome errado dava "No such method"
                # 40x e o boot abortava com "RPC não abriu em 10 s".
                self._rpc("aria2.getVersion", timeout=1.0)
                return
            except Exception:
                if self._proc.poll() is not None:
                    raise DownloadError(
                        f"aria2 saiu na subida (código "
                        f"{self._proc.returncode})")
                time.sleep(BOOT_DELAY)
        raise DownloadError("aria2 não abriu o RPC local em 10 s")

    def _add(self) -> str:
        """Adiciona a URI/torrent à sessão do aria2; devolve o gid."""
        t = self.task
        if t.url.lower().startswith("magnet:"):
            gids = self._rpc("aria2.addUri", [[t.url]], timeout=15.0)
        else:
            # URL de .torrent: baixa os bytes e entrega via addTorrent
            # (addUri num .torrent baixaria o próprio arquivo .torrent)
            raw = self._torrent_bytes(t.url)
            gids = self._rpc("aria2.addTorrent", [None, raw, []],
                             timeout=15.0)
        gid = gids[0] if isinstance(gids, list) else gids
        return str(gid)

    @staticmethod
    def _torrent_bytes(url: str) -> str:
        """Baixa o `.torrent` e devolve em base64 (formato do addTorrent)."""
        req = urllib.request.Request(url, headers={"User-Agent": "minidm"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
        except (OSError, ValueError) as e:
            raise DownloadError(f"falha ao baixar o .torrent: {e}") from e
        if b"announce" not in data[:8192]:
            raise DownloadError("a URL não devolveu um arquivo "
                                ".torrent válido (HTML de erro?)")
        return base64.b64encode(data).decode("ascii")

    # ------------------------------------------------------------------ run
    def run(self) -> Task:
        t = self.task
        t.state = TaskState.DOWNLOADING
        t.error = None
        try:
            self._boot()
            self._gid = self._add()
            self._watch()
            self._finish()
        except DownloadError as e:
            if t.state != TaskState.PAUSED:
                t.state = TaskState.ERROR
                if not t.error:
                    t.error = str(e)
            t.save_meta()
            raise
        except Exception as e:            # nunca escapa outra exceção
            t.state = TaskState.ERROR
            t.error = f"torrent falhou: {e}"
            t.save_meta()
            raise DownloadError(t.error) from e
        finally:
            self._stop_aria2()
        return t

    def request_stop(self) -> None:
        self.stop.set()

    def speed_bps(self, window: float = 5.0) -> float:
        # o aria2 reporta a velocidade instantânea no tellStatus
        if time.monotonic() - self._speed_ts <= 10.0:
            return self._speed
        return 0.0

    # --------------------------------------------------------------- watch
    def _watch(self) -> None:
        """Polling do tellStatus: espelha bytes/total/velocidade na task."""
        t = self.task
        last_save = 0.0
        while True:
            if self.stop.is_set():
                self._remove_current()
                t.state = TaskState.PAUSED
                t.save_meta()
                raise DownloadError("interrompido (progresso salvo)")
            st = self._rpc("aria2.tellStatus", [self._gid])
            status = str(st.get("status") or "")
            done = int(st.get("completedLength") or 0)
            total = int(st.get("totalLength") or 0)
            try:
                self._speed = float(st.get("downloadSpeed") or 0)
            except (TypeError, ValueError):
                self._speed = 0.0
            self._speed_ts = time.monotonic()
            if total:
                self._total = total
                t.total_size = total
                t.stream = {"bytes_done": done,
                            "segments_total": 100,
                            "segments_done": int(done * 100 / total)}
            else:
                # magnet ainda resolve os metadados (infohash -> nome)
                st_dict = dict(t.stream) if isinstance(t.stream, dict) else {}
                st_dict["note"] = "torrent: resolvendo metadados…"
                t.stream = st_dict
            now = time.monotonic()
            if now - last_save >= POLL_SECONDS:
                t.save_meta()
                if self.on_progress:
                    self.on_progress(t)
                last_save = now
            if status == "complete":
                return
            if status in ("error", "removed"):
                msg = st.get("errorMessage") or "falha no torrent"
                raise DownloadError(f"torrent: {msg}")
            time.sleep(POLL_SECONDS)

    def _finish(self) -> None:
        """Conclui a task com o arquivo real entregue pelo aria2."""
        t = self.task
        st = self._rpc("aria2.tellStatus",
                       [self._gid, ["files", "completedLength"]])
        # torrent multi-file: o MAIOR arquivo vira o nome exibido/publicado
        # (o resto fica ao lado, na mesma pasta que o aria2 usou)
        dest = os.path.abspath(t.dest_dir or ".")
        best, best_size = None, -1
        for f in st.get("files") or []:
            p = f.get("path") or ""
            if not p or not os.path.isfile(p):
                continue
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size > best_size:
                best, best_size = p, size
        if best:
            rel = os.path.relpath(best, dest)
            t.filename = rel if not rel.startswith("..") \
                else os.path.basename(best)
        total = self._total or t.total_size or best_size or 0
        t.total_size = total
        t.stream = {"bytes_done": total}
        t.state = TaskState.DONE
        t.finished_at = time.time()
        t.save_meta()

    # ------------------------------------------------------------- teardown
    def _remove_current(self) -> None:
        """Pausa: tira o gid do aria2 (o parcial fica p/ retomar)."""
        if not self._gid:
            return
        try:
            self._rpc("aria2.forceRemove", [self._gid], timeout=3.0)
        except Exception:
            pass

    def _stop_aria2(self) -> None:
        """Desliga o aria2 DESTA task (RPC primeiro, kill como fallback)."""
        proc, self._proc = self._proc, None
        if not proc:
            return
        if self._port and proc.poll() is None:
            try:
                # mesmo caso do boot: o método real é `aria2.shutdown`
                # (system.shutdown não existe nos builds oficiais)
                self._rpc("aria2.shutdown", timeout=3.0)
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
                return
            except Exception:
                pass
        try:
            proc.kill()
        except OSError:
            pass
