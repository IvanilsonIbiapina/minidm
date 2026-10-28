"""Downloader que delega ao yt-dlp (CLI externa, GPL-3.0 — sem copiar código).

Usado quando o perfil manda (`use_ytdlp_downloader`) ou a config global
liga `use_ytdlp_downloader`, e o yt-dlp está instalado. A engine própria
(HLS/DASH/arquivo) continua sendo o fallback.

Interface igual aos outros downloaders (scheduler não muda):
`run()`, `request_stop()`, `speed_bps()`. O progresso vem do parse das
linhas `[download] 12.3% of ~10.00MiB at 1.00MiB/s ETA 00:10`.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from typing import Optional

from minidm.config import AppConfig
from minidm.deps import ffmpeg_dir, js_runtime
from minidm.meta import Task, TaskState
from minidm.ytdlp import find as find_ytdlp

# [download]  12.3% of ~ 10.00MiB at  1.00MiB/s ETA 00:10
_PROGRESS_RE = re.compile(
    r"\[download\]\s+([\d.]+)%\s+of\s+~?\s*([\d.]+)([KMG]?i?B)")
_SIZE_UNITS = {"B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3,
               "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}

# flags do yt-dlp que exigem ffmpeg (pós-processamento: converter, embed, mux)
_FFMPEG_FLAGS = {
    "-x", "--extract-audio", "--audio-format", "--audio-quality",
    "--embed-thumbnail", "--embed-metadata", "--embed-chapters",
    "--embed-subs", "--sub-format", "--recode-video", "--remux-video",
    "--merge-output-format", "--postprocessor-args", "--exec",
}
# formatos que o yt-dlp só entrega convertendo com ffmpeg
_CONVERSION_EXTS = {"mp3", "m4a", "opus", "flac", "wav", "aac", "ogg"}


def _filter_ffmpeg_args(args: list[str], ffmpeg: Optional[str]) -> list[str]:
    """Remove argumentos que dependeriam de ffmpeg quando ele não existe.

    Assim o perfil "Música" sem ffmpeg ainda baixa o melhor áudio (em
    m4a/webm nativo) em vez de abortar com "ffprobe not found". O nome do
    arquivo é ajustado para a extensão real no `_finalize`.
    """
    if ffmpeg:
        return list(args)
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in _FFMPEG_FLAGS:
            # flags com valor no próximo argumento
            skip = a in ("--audio-format", "--audio-quality", "--sub-format",
                         "--recode-video", "--remux-video",
                         "--merge-output-format", "--postprocessor-args",
                         "--exec")
            continue
        out.append(a)
    return out


def _explain_error(tail: list[str], rc: int) -> str:
    """Mensagem de erro limpa e em português a partir da saída do yt-dlp.

    Filtra os avisos (WARNING) e traduz os erros comuns do YouTube para
    algo que o usuário entende, em vez de vomitar a saída bruta.
    """
    # só as linhas de ERROR (ignora WARNING/DEBUG)
    errs = [l.strip() for l in tail
            if "ERROR" in l.upper() and "WARNING" not in l.upper()]
    if not errs:
        errs = [l.strip() for l in tail if l.strip()][-2:] or ["erro desconhecido"]
    msg = " | ".join(errs)
    low = msg.lower()
    # traduz os casos mais comuns do YouTube para algo claro
    if "unavailable" in low:
        msg += ("  → este vídeo está indisponível no YouTube "
                "(removido, privado ou bloqueado por região/idade).")
    elif "private video" in low:
        msg += "  → vídeo privado — não é possível baixar."
    elif "age" in low and ("confirm" in low or "restricted" in low):
        msg += ("  → vídeo com restrição de idade — faça login no navegador "
                "ou use --cookies.")
    elif "sign in" in low or "login" in low:
        msg += "  → o YouTube pediu login — tente com cookies do navegador."
    elif "javascript runtime" in low:
        msg += ("  → instale o Node.js (https://nodejs.org) para o yt-dlp "
                "extrair todos os formatos.")
    elif "ffmpeg" in low:
        msg += "  → instale o ffmpeg para conversão/mesclagem."
    return f"yt-dlp (código {rc}): {msg}"


def _hidden_kwargs() -> dict:
    """Evita abrir janela de console no Windows (ver `deps.hidden_kwargs`)."""
    from minidm.deps import hidden_kwargs

    return hidden_kwargs()


class YtdlpError(Exception):
    pass


class YtdlpDownloader:
    def __init__(self, task: Task, config: Optional[AppConfig] = None,
                 extra_args: Optional[list[str]] = None, **_kw):
        self.task = task
        self.config = config or AppConfig()
        self.extra_args = list(extra_args or [])
        self._proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self._received = 0
        self._total: Optional[float] = None
        self._speed = 0.0
        self._lock = threading.Lock()
        self._ytdlp_named = False   # yt-dlp escolheu o nome (título real)
        self._drop_cookie = False   # retry sem cookie (cookie zerou formatos)
        self._rc = 0                # código de saída da última execução

    # ------------------------------------------------------------- interface
    def run(self) -> Task:
        t = self.task
        t.state = TaskState.DOWNLOADING
        t.error = None
        while True:
            cmd = self._build_cmd()
            tail = self._spawn(cmd)
            if self._stop.is_set():
                t.state = TaskState.PAUSED
                t.save_meta()
                return t
            rc = self._rc
            if rc != 0 and self._retry_without_cookie(tail):
                # cookie do navegador zerou a lista de formatos (YouTube
                # devolveu "só imagens") -> refaz SEM o cookie, uma vez.
                self._drop_cookie = True
                self._received = 0
                self._total = None
                continue
            if rc != 0:
                t.state = TaskState.ERROR
                t.error = _explain_error(tail, rc)
                t.save_meta()
                raise YtdlpError(t.error)
            self._finalize()
            return t

    def _spawn(self, cmd: list[str]) -> list[str]:
        """Roda o yt-dlp uma vez; devolve as últimas linhas (tail) de log."""
        t = self.task
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, cwd=t.dest_dir or None,
                **_hidden_kwargs())
            assert self._proc.stdout is not None
            tail: list[str] = []
            for line in self._proc.stdout:
                if self._stop.is_set():
                    break
                self._parse_line(line)
                tail.append(line.rstrip())
                if len(tail) > 30:
                    tail.pop(0)
            self._rc = self._proc.wait()
            return tail
        except OSError as e:
            self._rc = 1
            t.state = TaskState.ERROR
            t.error = f"yt-dlp não rodou: {e}"
            t.save_meta()
            raise YtdlpError(t.error) from e

    def _retry_without_cookie(self, tail: list[str]) -> bool:
        """Este erro some se rodarmos sem o cookie do navegador?"""
        if self._drop_cookie:
            return False
        if not (self.task.cookie or self.config.cookie):
            return False
        text = "\n".join(tail).lower()
        return ("requested format is not available" in text
                or "only images are available" in text
                or "no video formats found" in text)

    def request_stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def speed_bps(self, window: float = 5.0) -> float:
        with self._lock:
            return self._speed

    # ----------------------------------------------------------------- cmd
    def _build_cmd(self) -> list[str]:
        cmd = find_ytdlp()
        if not cmd:
            raise YtdlpError("yt-dlp não encontrado")
        t = self.task
        args = list(cmd)
        args += ["--newline", "--no-playlist", "--no-mtime"]
        # ffmpeg: se existe, aponta o yt-dlp para ele (conversão/embed/mux);
        # se não, remove os argumentos que exigiriam pós-processamento —
        # o download continua no melhor formato nativo em vez de falhar.
        ff = ffmpeg_dir()
        if ff:
            args += ["--ffmpeg-location", ff]
        # runtime JS (node/deno/bun): o YouTube embaralha o URL com
        # JavaScript; sem runtime, o yt-dlp pode omitir formatos.
        js = js_runtime()
        if js:
            args += ["--js-runtimes", js]
            # solver de challenge (EJS) do YouTube: sem ele, o yt-dlp com
            # cookie vem com a página "só imagens" (storyboards) e o seletor
            # falha com "Requested format is not available". Testado: com a
            # flag -> 399+140; sem -> nenhum formato.
            from minidm.ytdlp import supports_remote_components

            if supports_remote_components():
                args += ["--remote-components", "ejs:github"]
        # saída: se o nome ainda é o id bruto do URL (sem título real),
        # deixa o yt-dlp nomear com o título de verdade. Se o usuário/perfil
        # definiu um nome, respeita (usa o stem como prefixo).
        stem = os.path.splitext(t.filename)[0] or "download"
        if self._generic_stem(stem):
            self._ytdlp_named = True
            args += ["-o", os.path.join(t.dest_dir or ".",
                                        "%(title)s [%(id)s].%(ext)s")]
        else:
            args += ["-o", os.path.join(t.dest_dir or ".", stem + ".%(ext)s")]
        if t.referer:
            args += ["--add-header", f"Referer:{t.referer}"]
        if t.user_agent or self.config.user_agent:
            args += ["--user-agent", t.user_agent or self.config.user_agent]
        if not self._drop_cookie and (t.cookie or self.config.cookie):
            args += ["--add-header",
                     f"Cookie:{t.cookie or self.config.cookie}"]
        if t.proxy or self.config.proxy:
            args += ["--proxy", t.proxy or self.config.proxy]
        args += self.extra_args
        args += _filter_ffmpeg_args(getattr(t, "ytdlp_args", []) or [], ff)
        args.append(t.url)
        return args

    def _generic_stem(self, stem: str) -> bool:
        """O nome é genérico (id do URL, 'download', 'watch')? Aí o título
        real do yt-dlp é melhor."""
        from minidm.profiles import _id_from_url, _is_generic

        s = (stem or "").strip().lower()
        if _is_generic(s):
            return True
        vid = _id_from_url(self.task.url)
        if not vid:
            return False
        v = vid.lower()
        if s == v:
            return True
        # template "{title} [{id}]" com title == id (sem título real):
        # "9CtAJ_nw640 [9CtAJ_nw640]" -> genérico; o yt-dlp nomeia melhor.
        base = s.rsplit(" [", 1)[0].strip()
        return base == v or _is_generic(base)

    # -------------------------------------------------------------- parsing
    def _parse_line(self, line: str) -> None:
        m = _PROGRESS_RE.search(line)
        if not m:
            return
        pct = float(m.group(1))
        total = float(m.group(2)) * _SIZE_UNITS.get(m.group(3), 1)
        with self._lock:
            self._total = int(total)
            self._received = int(total * pct / 100.0)
            self._speed = 0.0     # o yt-dlp já controla banda própria
        t = self.task
        t.total_size = self._total
        # received/progress vêm do stream dict para casar com a UI
        t.stream = {"bytes_done": self._received,
                    "segments_total": 100,
                    "segments_done": int(pct)}

    # ------------------------------------------------------------ finalizar
    def _finalize(self) -> None:
        t = self.task
        # acha o arquivo real que o yt-dlp gravou (extensão escolhida por ele)
        base = os.path.splitext(t.filename)[0]
        found = None
        try:
            if self._ytdlp_named:
                # o yt-dlp nomeou com o título real ("%(title)s [%(id)s].%(ext)s").
                # o "mais recente" é ambíguo quando várias tarefas yt-dlp
                # dividem a mesma pasta — o id do vídeo identifica com
                # segurança o arquivo DESTA tarefa; senão, cai no mais novo.
                from minidm.profiles import _id_from_url

                vid = _id_from_url(self.task.url)
                tag = f"[{vid}]" if vid else None
                best = None
                idmatch = None
                for name in os.listdir(t.dest_dir or "."):
                    if name.endswith((".part", ".ytdl", ".meta.json")) or \
                            name in ("tasks.json", "config.json",
                                     "profiles.json", "archive.txt"):
                        continue
                    full = os.path.join(t.dest_dir or ".", name)
                    if not os.path.isfile(full):
                        continue
                    if tag and tag in name:
                        idmatch = name     # arquivo com [id] deste vídeo
                        break
                    mtime = os.path.getmtime(full)
                    if best is None or mtime > best[0]:
                        best = (mtime, name)
                found = idmatch or (best[1] if best else None)
            else:
                for name in os.listdir(t.dest_dir or "."):
                    if name.startswith(base) and not name.endswith(
                            (".part", ".ytdl", ".meta")):
                        found = name
                        break
        except OSError:
            pass
        if found:
            t.filename = found
            t.total_size = os.path.getsize(
                os.path.join(t.dest_dir or ".", found))
        # sha-256 do arquivo (mesmo contrato dos outros downloaders)
        try:
            from minidm.verify import sha256_file
            path = os.path.join(t.dest_dir or ".", t.filename)
            if os.path.exists(path):
                t.sha256 = sha256_file(path)
        except Exception:
            pass
        t.state = TaskState.DONE
        t.finished_at = time.time()
        t.stream = None
        t.save_meta()
