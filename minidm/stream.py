"""Download de streams: HLS (m3u8) e DASH (mpd) — estilo IDM.

O IDM, ao capturar um vídeo, baixa o manifest, resolve a qualidade escolhida
e baixa os segmentos (`.ts`/`.m4s`) em paralelo, concatenando tudo num único
arquivo final. Este módulo faz o mesmo, reusando a infraestrutura da engine
de arquivos: semáforo de conexões globais, token bucket de banda, retry com
backoff, checkpoint periódico e retomada.

Suporta:

* HLS master -> variante (a de maior bandwidth ou a escolhida);
* HLS media playlist, com `#EXT-X-MAP` (fMP4), `#EXT-X-BYTERANGE` e
  `#EXT-X-KEY` AES-128 (CBC, com IV do playlist ou número de sequência);
* DASH com `SegmentTemplate` (+ `SegmentTimeline`) ou `SegmentList`;
* retomada: segmentos já baixados ficam em `<dest>/.<arquivo>.segs/` e são
  pulados ao reexecutar.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import threading
import time
from typing import Callable, Optional

import requests

from minidm.config import AppConfig
from minidm.media import (build_headers, parse_m3u8, parse_mpd,
                          suggest_extension)
from minidm.meta import Task, TaskState
from minidm.remote import TIMEOUT
from minidm.throttle import TokenBucket
from minidm import verify as verify_mod

CHUNK = 256 * 1024
CHECKPOINT_SECONDS = 2.0

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    _HAS_CRYPTO = True
except ImportError:                     # HLS AES-128 fica indisponível
    _HAS_CRYPTO = False


class StreamError(Exception):
    pass


class StopRequested(Exception):
    pass


# ------------------------------------------------------------- templates DASH
_TOKEN_RE = re.compile(r"\$(RepresentationID|Bandwidth|Number|Time)(%0(\d+)d)?\$")


def expand_template(template: str, rep_id: str = "", bandwidth: int = 0,
                    number: int = 0, t: int = 0) -> str:
    """Resolve `$RepresentationID$`, `$Bandwidth$`, `$Number%05d$`, `$Time$`."""

    def sub(m):
        name, _fmt, width = m.group(1), m.group(2), m.group(3)
        value = {"RepresentationID": rep_id, "Bandwidth": bandwidth,
                 "Number": number, "Time": t}[name]
        return str(value).zfill(int(width)) if width else str(value)

    return _TOKEN_RE.sub(sub, template).replace("$$", "$")


def timeline_units(timeline: list[dict], period_duration: Optional[float],
                   timescale: int) -> list[int]:
    """`SegmentTimeline` -> instantes de início (unidades de `timescale`)."""
    out = []
    cursor = 0
    for s in timeline:
        if "t" in s:
            cursor = s["t"]
        d = s.get("d", 0)
        r = s.get("r", 0)
        repeats = r if r >= 0 else None
        count = repeats + 1 if repeats is not None else None
        i = 0
        while count is None or i < count:
            if period_duration is not None and \
                    (cursor + d) > period_duration * timescale + 1:
                break
            out.append(cursor)
            cursor += d
            i += 1
            if count is None and len(out) > 100_000:
                break
    return out


# ------------------------------------------------------------------ crypto
def _fetch_key(url: str, headers: dict, proxy: str,
               timeout: float = TIMEOUT) -> bytes:
    with requests.Session() as s:
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        r = s.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        if len(r.content) != 16:
            raise StreamError(f"chave AES inválida ({len(r.content)} bytes)")
        return r.content


def _decrypt_aes128(data: bytes, key: bytes, iv: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    # HLS usa padding PKCS7 — remove se presente
    if plain:
        pad = plain[-1]
        if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
            plain = plain[:-pad]
    return plain


def _iv_for(key_info: dict, sequence: int) -> bytes:
    raw = (key_info.get("iv") or "").lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    if raw and len(raw) == 32:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    return sequence.to_bytes(16, "big")


# ---------------------------------------------------------------- downloader
class StreamDownloader:
    """Mesma interface do `Downloader` (run/request_stop/speed_bps)."""

    def __init__(self, task: Task, config: Optional[AppConfig] = None,
                 on_progress: Optional[Callable[[Task], None]] = None,
                 stop: Optional[threading.Event] = None,
                 semaphore: Optional[threading.BoundedSemaphore] = None,
                 bucket: Optional[TokenBucket] = None,
                 checkpoint_seconds: float = CHECKPOINT_SECONDS):
        self.task = task
        self.config = config or AppConfig()
        self.on_progress = on_progress
        self.stop = stop or threading.Event()
        self.sem = semaphore
        self.bucket = bucket
        self.checkpoint_seconds = checkpoint_seconds
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._speed_samples: list[tuple[float, int]] = []
        self._items: list[dict] = []
        self._key_cache: dict[str, bytes] = {}
        self._plan: dict = {}
        self._queue: list[int] = []
        self._inflight: set[int] = set()   # índices em download agora
        self._failed: set[int] = set()     # índices que esgotaram as retries
        self._live = False                 # playlist ao vivo (sem ENDLIST)

    # ----------------------------------------------------------------- top
    def run(self) -> Task:
        t = self.task
        t.state = TaskState.DOWNLOADING
        t.error = None
        try:
            self._prepare()
            if self._live:
                self._live_loop()      # rebusca a playlist até o ENDLIST
            else:
                self._spawn_workers()
                self._checkpoint_loop()
            self._finalize()
        except (StreamError, requests.RequestException) as e:
            if t.state != TaskState.PAUSED:
                t.state = TaskState.ERROR
                if not t.error:
                    t.error = str(e)
            t.save_meta()
            raise
        return t

    def request_stop(self) -> None:
        self.stop.set()

    def _headers(self) -> dict:
        t = self.task
        return build_headers(t.referer, t.user_agent or self.config.user_agent,
                             t.cookie or self.config.cookie)

    def _session(self) -> requests.Session:
        s = requests.Session()
        proxy = self.task.proxy or self.config.proxy
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        return s

    def _get_text(self, url: str) -> str:
        with self._session() as s:
            r = s.get(url, headers=self._headers(), timeout=TIMEOUT)
            r.raise_for_status()
            return r.text

    # ------------------------------------------------------------ preparação
    def _segs_dir(self) -> str:
        return os.path.join(self.task.dest_dir,
                            f".{self.task.filename}.segs")

    def _prepare(self) -> None:
        t = self.task
        os.makedirs(t.dest_dir, exist_ok=True)

        cached = t.stream if isinstance(t.stream, dict) else None
        if cached and cached.get("base_url") == t.url and cached.get("items"):
            # retomada: reusa o plano já resolvido
            self._plan = cached
            self._items = cached["items"]
            t.total_size = cached.get("bytes_total")
            self._live = bool(cached.get("live"))
            if len(t.segments) < len(self._items):
                # gravação atômica mantém os dois juntos, mas por segurança
                # completa os segmentos que faltarem (nunca encurta)
                from minidm.meta import Segment

                while len(t.segments) < len(self._items):
                    i = len(t.segments)
                    t.segments.append(Segment(start=i, end=i, received=0))
            return
        if cached:
            # escolha parcial (rep_id/variant_index) vinda do chamador
            self._plan = dict(cached)

        plan = self._resolve(t.url)
        self._plan = plan
        self._items = plan["items"]
        t.segments = _segments_for_items(self._items)
        self._live = bool(plan.get("live"))
        # ao vivo: não existe tamanho total — a playlist cresce com o tempo
        t.total_size = None if self._live else plan.get("bytes_total")
        t.accept_ranges = False
        t.stream = {
            "base_url": t.url,
            "resolved_url": plan.get("resolved_url", t.url),
            "kind": plan["kind"],
            "items": self._items,
            "bytes_done": 0,
            "bytes_total": t.total_size,
            "segments_done": 0,
            # ao vivo o total é desconhecido (0) — a barra mostra "ao vivo"
            "segments_total": 0 if self._live else len(self._items),
            "live": self._live,
            "target_duration": plan.get("target_duration") or 0.0,
            "lost": 0,
        }

        # extensão do arquivo final (.ts/.mp4) se o nome não tiver uma
        ext = suggest_extension(plan["kind"], plan.get("init"))
        if ext and not t.filename.lower().endswith(ext):
            old_meta = t.meta_path
            t.filename += ext
            try:
                os.remove(old_meta)
            except OSError:
                pass

        os.makedirs(self._segs_dir(), exist_ok=True)
        t.save_meta()

    def _resolve(self, url: str) -> dict:
        """Master/manifest -> lista plana de segmentos da qualidade escolhida."""
        kind = self.task.kind if self.task.kind in ("hls", "dash") else "hls"
        if kind == "hls":
            return self._resolve_hls(url)
        return self._resolve_dash(url)

    def _resolve_hls(self, url: str) -> dict:
        text = self._get_text(url)
        playlist = parse_m3u8(text, url)
        if playlist.get("drm"):
            raise StreamError(
                f"conteúdo protegido por DRM ({playlist['drm']}) — "
                f"não é possível baixar")
        resolved = url
        if playlist["type"] == "master":
            variants = playlist["variants"]
            if not variants:
                raise StreamError("master m3u8 sem variantes")
            index = self._pick_variant(variants)
            resolved = variants[index]["url"]
            text = self._get_text(resolved)
            playlist = parse_m3u8(text, resolved)
            if playlist["type"] != "media":
                raise StreamError("variante não é playlist de mídia")
            if playlist.get("drm"):
                raise StreamError(
                    f"variante protegida por DRM ({playlist['drm']}) — "
                    f"não é possível baixar")
        if not playlist["segments"]:
            raise StreamError("playlist sem segmentos")

        items = []
        init = playlist.get("init")
        if init:
            items.append({"url": init["url"], "byterange": init.get("byterange"),
                          "key": None, "sequence": -1, "init": True})
        for seg in playlist["segments"]:
            items.append({
                "url": seg["url"],
                "byterange": seg.get("byterange"),
                "key": dict(seg["key"]) if seg.get("key") else None,
                "sequence": seg.get("sequence", len(items)),
                "init": False,
            })
        durations = [s.get("duration") or 0 for s in playlist["segments"]]
        # playlist sem #EXT-X-ENDLIST = sessão AO VIVO: os segmentos ainda
        # vão chegando e a playlist precisa ser rebuscada periodicamente
        live = not playlist.get("endlist")
        return {"kind": "hls", "items": items, "init": init,
                "resolved_url": resolved, "base_url": url,
                "durations": durations, "encrypted": playlist["encrypted"],
                "live": live,
                "target_duration": playlist.get("target_duration") or 0.0}

    def _pick_variant(self, variants: list[dict]) -> int:
        """Escolhe a variante: índice pedido, ou a de maior bandwidth."""
        want = self._plan.get("variant_index") if self._plan else None
        if isinstance(want, int) and 0 <= want < len(variants):
            return want
        best, best_bw = 0, -1
        for i, v in enumerate(variants):
            bw = v.get("bandwidth") or 0
            if bw > best_bw:
                best, best_bw = i, bw
        return best

    def _resolve_dash(self, url: str) -> dict:
        dash = parse_mpd(self._get_text(url), url)
        if dash.get("drm"):
            raise StreamError(
                f"conteúdo protegido por DRM ({dash['drm']}) — "
                f"não é possível baixar")
        reps = dash.get("representations") or []
        video = [r for r in reps if r["mimeType"].startswith("video")]
        audio = [r for r in reps if r["mimeType"].startswith("audio")]
        # dica vinda do mux: esta resolução é da PERNA DE ÁUDIO — só a
        # faixa de áudio, senão o "par" baixaria o vídeo de novo
        if (self._plan or {}).get("media") == "audio":
            if not audio:
                raise StreamError("MPD sem faixa de áudio separada")
            pool = audio
        else:
            pool = video or audio
        if not pool:
            raise StreamError("MPD sem representações")
        rep = self._pick_dash_rep(pool)

        items = []
        init = rep.get("init")
        if init:
            items.append({"url": init["url"], "byterange": init.get("byterange"),
                          "key": None, "sequence": -1, "init": True})
        tpl, lst = rep.get("template"), rep.get("list")
        if tpl:
            timescale = tpl.get("timescale") or 1
            numbers = []
            if tpl.get("timeline"):
                starts = timeline_units(tpl["timeline"], rep.get("duration"),
                                        timescale)
                numbers = [(n, t) for n, t in
                           enumerate(starts, start=tpl["start_number"])]
            elif tpl.get("duration") and rep.get("duration"):
                count = max(1, math.ceil(
                    rep["duration"] * timescale / tpl["duration"]))
                numbers = [(n, (n - tpl["start_number"]) * tpl["duration"])
                           for n in range(tpl["start_number"],
                                          tpl["start_number"] + count)]
            if not numbers:
                raise StreamError("SegmentTemplate sem número de segmentos")
            for number, t_units in numbers:
                media = expand_template(tpl["media"], rep.get("id", ""),
                                        rep.get("bandwidth") or 0, number,
                                        t_units)
                items.append({"url": urljoin_dash(rep["url"], media),
                              "byterange": None, "key": None,
                              "sequence": number, "init": False})
        elif lst:
            for i, seg in enumerate(lst["segments"]):
                items.append({"url": seg["url"], "byterange": seg.get("byterange"),
                              "key": None, "sequence": i, "init": False})
        else:
            raise StreamError("representação DASH sem SegmentTemplate/SegmentList")

        bytes_total = None
        if rep.get("bandwidth") and rep.get("duration"):
            bytes_total = int(rep["bandwidth"] * rep["duration"] / 8)
        return {"kind": "dash", "items": items, "init": init,
                "resolved_url": url, "base_url": url,
                "bytes_total": bytes_total}

    def _pick_dash_rep(self, pool: list[dict]) -> dict:
        want = self._plan.get("rep_id") if self._plan else None
        if want:
            for r in pool:
                if r.get("id") == want:
                    return r
        return max(pool, key=lambda r: r.get("bandwidth") or 0)

    # -------------------------------------------------------------- workers
    def _spawn_workers(self) -> None:
        """Pool de threads que puxa itens pendentes de uma fila compartilhada.

        Cada worker consome o próximo índice não-baixado até esvaziar — assim
        não se perde item quando há mais segmentos que `max_connections`.
        `concurrent_fragments` (config) limita os fragmentos simultâneos
        *deste stream* — o equivalente ao `--concurrent-fragments` do yt-dlp.

        Pode ser chamada de novo (HLS ao vivo): só enfileira o que ainda NÃO
        está na fila nem em download e só abre threads enquanto há vaga.
        """
        with self._lock:
            have = set(self._queue) | set(self._inflight) | set(self._failed)
            pending = [i for i in range(len(self._items))
                       if not self._item_done(i) and i not in have]
            if pending:
                self._queue.extend(pending)
            queued = len(self._queue)
            alive = sum(1 for th in self._threads if th.is_alive())
        if not queued:
            return
        limit = max(1, self.config.max_connections)
        frags = getattr(self.config, "concurrent_fragments", 0) or 0
        if frags > 0:
            limit = min(limit, frags)
        n = min(queued, limit - alive)
        for w in range(n):
            th = threading.Thread(target=self._worker, name=f"stream-{w}",
                                  daemon=True)
            th.start()
            self._threads.append(th)

    def _next_index(self) -> Optional[int]:
        with self._lock:
            if not self._queue:
                return None
            i = self._queue.pop(0)
            self._inflight.add(i)
            return i

    def _seg_path(self, index: int) -> str:
        return os.path.join(self._segs_dir(), f"{index:06d}.bin")

    def _item_done(self, index: int) -> bool:
        seg = self.task.segments[index]
        if not seg.done:
            return False
        return os.path.exists(self._seg_path(index))

    def _worker(self, _wid: int = 0) -> None:
        cfg = self.config
        while True:
            index = self._next_index()
            if index is None:
                return
            try:
                if self.stop.is_set():
                    return
                if self._item_done(index):
                    continue
                for attempt in range(cfg.retries + 1):
                    if self.stop.is_set():
                        return
                    if self._item_done(index):
                        break
                    try:
                        self._download_item(index)
                        break
                    except StopRequested:
                        return
                    except (requests.RequestException, OSError) as e:
                        if attempt >= cfg.retries:
                            with self._lock:
                                self.task.error = f"segmento {index}: {e}"
                                self._failed.add(index)
                            break
                        delay = min(cfg.backoff_max,
                                    cfg.backoff_base * (2 ** attempt))
                        self.stop.wait(delay * (0.7 + 0.6 * ((attempt * 37) % 10) / 10))
                    except StreamError as e:
                        with self._lock:
                            self.task.error = f"segmento {index}: {e}"
                            self._failed.add(index)
                        break
            finally:
                # tira o índice da fila "em download" — sem isso o próximo
                # `_spawn_workers` (loop ao vivo) nunca re-enfileiraria ele
                with self._lock:
                    self._inflight.discard(index)

    def _download_item(self, index: int) -> None:
        item = self._items[index]
        acquired = self.sem.acquire(timeout=60) if self.sem else True
        if not acquired:
            raise requests.Timeout("sem conexões disponíveis")
        try:
            headers = self._headers()
            data = self._fetch_bytes(item["url"], item.get("byterange"),
                                     headers)
            key_info = item.get("key")
            if key_info:
                data = self._decrypt(key_info, item.get("sequence", index), data)
            path = self._seg_path(index)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            with self._lock:
                seg = self.task.segments[index]
                seg.received = 1              # marca como concluído
                self._speed_samples.append((time.monotonic(), len(data)))
                self._update_progress_bytes(len(data))
        finally:
            if self.sem and acquired:
                self.sem.release()

    def _fetch_bytes(self, url: str, byterange: Optional[tuple],
                     headers: dict) -> bytes:
        h = dict(headers)
        if byterange:
            length, offset = byterange
            h["Range"] = f"bytes={offset}-{offset + length - 1}"
        with self._session() as s:
            r = s.get(url, headers=h, stream=True, timeout=TIMEOUT)
            if r.status_code >= 400:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            out = bytearray()
            for chunk in r.iter_content(CHUNK):
                if self.stop.is_set():
                    raise StopRequested()
                if not chunk:
                    continue
                if self.bucket:
                    self.bucket.acquire(len(chunk), stop=self.stop)
                out += chunk
            return bytes(out)

    def _decrypt(self, key_info: dict, sequence: int, data: bytes) -> bytes:
        if not _HAS_CRYPTO:
            raise StreamError("stream criptografado (AES-128) requer "
                              "`pip install cryptography`")
        uri = key_info.get("uri")
        if not uri:
            raise StreamError("EXT-X-KEY sem URI de chave")
        key = self._key_cache.get(uri)
        if key is None:
            key = _fetch_key(uri, self._headers(),
                             self.task.proxy or self.config.proxy)
            self._key_cache[uri] = key
        return _decrypt_aes128(data, key, _iv_for(key_info, sequence))

    # ------------------------------------------------------------ progresso
    def _update_progress_bytes(self, nbytes: int) -> None:
        st = self.task.stream
        if st is not None:
            st["bytes_done"] = st.get("bytes_done", 0) + nbytes
            st["segments_done"] = st.get("segments_done", 0) + 1

    def _checkpoint_loop(self) -> None:
        while True:
            if not [th for th in self._threads if th.is_alive()]:
                break
            self.task.save_meta()
            if self.on_progress:
                self.on_progress(self.task)
            time.sleep(self.checkpoint_seconds)

        self.task.save_meta()
        if self.on_progress:
            self.on_progress(self.task)

        if self.stop.is_set():
            self.task.state = TaskState.PAUSED
            self.task.save_meta()
            raise StreamError("interrompido (progresso salvo)")

        missing = [i for i in range(len(self._items)) if not self._item_done(i)]
        if missing:
            self.task.error = (f"{len(missing)} segmento(s) não baixados "
                               f"(primeiro: {missing[0]})")
            raise StreamError(self.task.error)

    # --------------------------------------------------------------- ao vivo
    def _live_interval(self) -> float:
        """Intervalo entre re-fetches da playlist ao vivo.

        Padrão: metade do `#EXT-X-TARGETDURATION`, entre 1 e 10s — rápido
        o bastante p/ não perder segmento da janela e lento o bastante p/
        não martelar o servidor. `live_poll_seconds` na config sobrepõe.
        """
        cfg_poll = float(getattr(self.config, "live_poll_seconds", 0.0) or 0.0)
        if cfg_poll > 0:
            return max(0.2, cfg_poll)
        st = self.task.stream if isinstance(self.task.stream, dict) else {}
        td = float(st.get("target_duration") or 0.0)
        if td <= 0:
            td = float((self._plan or {}).get("target_duration") or 0.0)
        if td <= 0:
            td = 4.0
        return min(10.0, max(1.0, td / 2.0))

    def _poll_playlist(self) -> tuple[int, bool]:
        """Rebusca a playlist de mídia e anexa os segmentos NOVOS.

        Devolve `(adicionados, endlist)`. Segmentos que já saíram da
        janela deslizante antes de a gente ver (ou que ficaram para trás)
        viram "lost": em transmissão ao vivo a falta é TOLERADA — é o que
        um player faz (o `#EXT-X-MEDIA-SEQUENCE` avança sozinho).
        """
        from minidm.meta import Segment

        st = self.task.stream if isinstance(self.task.stream, dict) else {}
        url = st.get("resolved_url") or self.task.url
        pl = parse_m3u8(self._get_text(url), url)
        if pl.get("type") != "media":
            raise StreamError("playlist ao vivo deixou de ser playlist de mídia")
        if pl.get("drm"):
            raise StreamError(
                f"conteúdo protegido por DRM ({pl['drm']}) — "
                f"não é possível baixar")

        known = {it.get("sequence") for it in self._items
                 if not it.get("init")}
        seqs = [s.get("sequence") for s in pl["segments"]]
        lost_gap = 0
        if seqs and known:
            # buraco entre o último que vimos e a nova janela: saiu do ar
            # antes de a gente buscar -> perdido, não é erro
            unseen = [s for s in seqs if s not in known]
            if unseen:
                lost_gap = max(0, min(unseen) - (max(known) + 1))

        added = 0
        for seg in pl["segments"]:
            seq = seg.get("sequence")
            if seq in known:
                continue
            known.add(seq)
            self._items.append({
                "url": seg["url"],
                "byterange": seg.get("byterange"),
                "key": dict(seg["key"]) if seg.get("key") else None,
                "sequence": seq,
                "init": False,
                "live": True,
            })
            i = len(self._items) - 1
            self.task.segments.append(Segment(start=i, end=i, received=0))
            added += 1

        if added or lost_gap:
            st["items"] = self._items
            st["lost"] = int(st.get("lost") or 0) + lost_gap
        return added, bool(pl.get("endlist"))

    def _live_loop(self) -> None:
        """Baixa o HLS AO VIVO: segmentos que existem + re-fetch da playlist.

        Termina quando aparece `#EXT-X-ENDLIST`, quando bate o limite de
        duração (`live_max_seconds`) ou quando o usuário PAUSA (aí o
        progresso é salvo e a retomada continua de onde parou — a janela
        pode ter andado, aí os segmentos antigos viram "lost").
        """
        cfg = self.config
        max_secs = float(getattr(cfg, "live_max_seconds", 0.0) or 0.0)
        deadline = time.monotonic() + max_secs if max_secs > 0 else None
        retries_max = max(1, int(getattr(cfg, "live_playlist_retries", 20)
                                 or 20))
        interval = self._live_interval()
        last_poll = time.monotonic()       # já temos a 1ª playlist (prepare)
        last_ckpt = time.monotonic()
        failures = 0
        ended = False

        self._spawn_workers()
        while True:
            if self.stop.is_set():
                if self.on_progress:
                    self.on_progress(self.task)
                self.task.state = TaskState.PAUSED
                self.task.save_meta()
                raise StreamError("interrompido (progresso salvo)")

            now = time.monotonic()
            if deadline is not None and now >= deadline:
                ended = True                # duração máxima: encerra por aqui

            added = 0
            if not ended and now - last_poll >= interval:
                last_poll = now
                try:
                    added, endlist = self._poll_playlist()
                    if endlist:
                        ended = True        # servidor disse "acabou"
                    failures = 0
                except (requests.RequestException, ValueError,
                        StreamError) as e:
                    failures += 1
                    if failures >= retries_max:
                        raise StreamError(
                            f"playlist ao vivo indisponível "
                            f"({failures} tentativas): {e}")

            alive = [th for th in self._threads if th.is_alive()]
            if added or not alive:
                self._spawn_workers()
                alive = [th for th in self._threads if th.is_alive()]

            if ended and not alive:
                # fila esvaziada (ou só perdeu) com a transmissão encerrada
                break

            if now - last_ckpt >= self.checkpoint_seconds:
                last_ckpt = now
                self.task.save_meta()
                if self.on_progress:
                    self.on_progress(self.task)
            time.sleep(0.2)

        self.task.save_meta()
        if self.on_progress:
            self.on_progress(self.task)

        if self.stop.is_set():
            self.task.state = TaskState.PAUSED
            self.task.save_meta()
            raise StreamError("interrompido (progresso salvo)")

    # --------------------------------------------------------------- final
    def _finalize(self) -> None:
        t = self.task
        st = t.stream if isinstance(t.stream, dict) else {}
        live = bool(st.get("live"))
        missing = [i for i in range(len(self._items))
                   if not self._item_done(i)]
        if missing and not live:
            # o `_checkpoint_loop` já abortou antes — defesa p/ não
            # concatenar um arquivo incompleto por engano
            t.error = (f"{len(missing)} segmento(s) não baixados "
                       f"(primeiro: {missing[0]})")
            raise StreamError(t.error)
        lost = len(missing) if live else 0

        # concatena init + segmentos, em ordem, direto no .part
        with open(t.part_path, "wb") as out:
            for i in range(len(self._items)):
                if not self._item_done(i):
                    continue            # ao vivo: falta tolerada (lost)
                with open(self._seg_path(i), "rb") as seg:
                    while True:
                        chunk = seg.read(CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
        _rmtree_quiet(self._segs_dir())

        if live:
            # transmissão terminou: agora o total EXISTE (o que chegou),
            # então a barra da tarefa concluída mostra o % real
            st["lost"] = int(st.get("lost") or 0) + lost
            st["segments_total"] = len(self._items)
            st["segments_done"] = len(self._items) - lost
            st["items"] = self._items
            t.error = None             # lost não é erro em ao vivo
            t.total_size = None

        # hash do .part antes de renomear (o final ainda não existe)
        t.sha256 = verify_mod.sha256_file(t.part_path)
        os.replace(t.part_path, t.final_path)

        # streams não têm checksum publicado — só hash local (+ Authenticode
        # não se aplica a mídia, então pula)
        t.checksum_ok = None
        t.state = TaskState.DONE
        t.finished_at = time.time()
        t.save_meta()

    # -------------------------------------------------------------- speed
    def speed_bps(self, window: float = 5.0) -> float:
        now = time.monotonic()
        total_now = self.task.stream.get("bytes_done", 0) if self.task.stream else 0
        old = None
        for ts, _val in self._speed_samples:
            if now - ts >= window:
                old = ts
            else:
                break
        if old is None:
            if not self._speed_samples:
                return 0.0
            return total_now / max(now - self._speed_samples[0][0], 1e-6)
        return total_now / max(now - old, 1e-6)


def _segments_for_items(items: list[dict]):
    from minidm.meta import Segment

    # start=end=index: cada item é uma unidade de progresso (done = received>=1)
    return [Segment(start=i, end=i, received=0) for i in range(len(items))]


def urljoin_dash(base: str, maybe_relative: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base, maybe_relative)


def _rmtree_quiet(path: str) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


class MuxDownloader:
    """Baixa as DUAS faixas (vídeo + áudio separados) e mescla com ffmpeg.

    É a resposta à antiga limitação "baixa uma faixa por vez (sem mux)":
    quando a análise ligou um par `task.mux_audio` (DASH com AdaptationSets
    separados, HLS com EXT-X-MEDIA de áudio), esta classe roda a engine
    normal na faixa de vídeo, depois na de áudio, e junta as duas com
    `ffmpeg -c copy` — o mesmo que o yt-dlp faz, mas na engine própria.

    Fases (a barra da tarefa principal espelha as duas juntas):
      1. vídeo — progresso natural do StreamDownloader/Downloader;
      2. áudio — segmentos do áudio somados aos do vídeo;
      3. mescla — barra em 100% enquanto o ffmpeg copia os fluxos.

    Pausa/erro em qualquer perna vira PAUSED/ERROR na tarefa principal (é
    o estado que o scheduler lê p/ emitir o evento); retomada reusa os
    dois sidecars (`.meta.json`) — vídeo pronto não é baixado de novo.
    """

    def __init__(self, task: Task, config: Optional[AppConfig] = None,
                 on_progress: Optional[Callable[[Task], None]] = None,
                 stop: Optional[threading.Event] = None,
                 semaphore: Optional[threading.BoundedSemaphore] = None,
                 bucket: Optional[TokenBucket] = None,
                 checkpoint_seconds: float = CHECKPOINT_SECONDS):
        self.task = task
        self.config = config or AppConfig()
        self.on_progress = on_progress
        self.stop = stop or threading.Event()
        self.sem = semaphore
        self.bucket = bucket
        self.checkpoint_seconds = checkpoint_seconds
        self._leg = None                    # downloader da fase corrente
        self._legs: list = []               # pernas simultâneas (ao vivo)
        self._audio_seen = 0                # bytes do áudio já espelhados

    # ------------------------------------------------------------- interface
    def request_stop(self) -> None:
        self.stop.set()
        legs = list(self._legs) or [self._leg]
        for leg in legs:
            if leg is not None:
                leg.request_stop()

    def speed_bps(self, window: float = 5.0) -> float:
        legs = [l for l in self._legs if l is not None]
        if not legs:
            legs = [self._leg] if self._leg is not None else []
        if not legs:
            return 0.0
        # ao vivo as duas pernas gravam juntas: soma as velocidades
        return sum(l.speed_bps(window) for l in legs)

    def run(self) -> Task:
        t = self.task
        t.state = TaskState.DOWNLOADING
        t.error = None
        self._recover()
        try:
            mx = dict(t.mux_audio or {})
            if (not mx.get("video_done")) and self._detect_live():
                # AO VIVO: fases 1 e 2 CONCORRENTES (as duas faixas têm de
                # gravar a MESMA janela da transmissão)
                self._run_live_pair()
            else:
                self._run_video_then_audio(mx)
            # ------------------------------ fase 3: mescla
            self._check_stop()
            self._mux()
            return t
        except Exception as exc:
            self._translate(exc)
            raise

    # ------------------------------------------------------------ fases/apoio
    def _run_video_then_audio(self, mx: dict) -> None:
        """Sequência normal (VOD): vídeo, depois áudio."""
        t = self.task
        # ------------------------------ fase 1: vídeo
        if not mx.get("video_done"):
            self._leg = self._make_leg(t, self.on_progress)
            self._leg.run()        # pausa/erro já gravados -> propaga
            st = t.stream if isinstance(t.stream, dict) else {}
            mx["video_done"] = True
            mx.setdefault("vseg",
                          st.get("segments_total") or len(t.segments))
            mx.setdefault("vbytes",
                          st.get("bytes_total") or t.total_size or 0)
            t.mux_audio = mx
            t.state = TaskState.DOWNLOADING   # ainda falta áudio+mescla
            t.finished_at = None
            t.save_meta()
        # ------------------------------ fase 2: áudio
        at = self._audio_task()
        old_audio = Task.load_meta(at.meta_path)
        audio_ok = bool(
            old_audio and old_audio.state == TaskState.DONE
            and os.path.exists(os.path.join(t.dest_dir, old_audio.filename)))
        if not audio_ok:
            self._check_stop()
            leg = self._make_leg(at, self._audio_progress)
            if isinstance(leg, StreamDownloader):
                leg._prepare()       # resolve já -> espelha os totais
            mx = dict(t.mux_audio or {})
            mx["audio_filename"] = at.filename
            t.mux_audio = mx
            t.save_meta()
            self._mirror_audio(at)
            self._leg = leg
            leg.run()
            if mx.get("audio_filename") != at.filename:
                mx["audio_filename"] = at.filename
                t.mux_audio = mx
                t.save_meta()

    def _detect_live(self) -> bool:
        """Pré-resolve a perna de vídeo p/ descobrir se é transmissão ao vivo.

        O `#EXT-X-ENDLIST` só aparece depois de ler a playlist de mídia
        (nem sempre é a URL dada), e as duas faixas de um master HLS são
        da mesma sessão — uma leitura decide o modo das DUAS.
        """
        t = self.task
        if t.kind != "hls" or not t.mux_audio:
            return False
        leg = self._make_leg(t, self.on_progress)
        try:
            leg._prepare()
        except (StreamError, requests.RequestException):
            return False            # a fase normal tenta de novo
        self._leg = leg             # a fase 1 reusa o prepare
        st = t.stream if isinstance(t.stream, dict) else {}
        return bool(st.get("live"))

    def _run_live_pair(self) -> None:
        """Fases 1+2 ao mesmo tempo (HLS ao vivo) e deixa a fase 3 pronta."""
        t = self.task
        video_leg = self._leg
        at = self._audio_task()
        old_audio = Task.load_meta(at.meta_path)
        audio_ok = bool(
            old_audio and old_audio.state == TaskState.DONE
            and os.path.exists(os.path.join(t.dest_dir, old_audio.filename)))
        audio_leg = None
        if not audio_ok:
            self._check_stop()
            audio_leg = self._make_leg(at, self._audio_progress)
            if isinstance(audio_leg, StreamDownloader):
                audio_leg._prepare()
            mx = dict(t.mux_audio or {})
            mx["audio_filename"] = at.filename
            t.mux_audio = mx
            t.save_meta()

        legs = [l for l in (video_leg, audio_leg) if l is not None]
        self._legs = legs
        errors: list[tuple] = []

        def _run(leg) -> None:
            try:
                leg.run()
            except Exception as e:          # noqa: BLE001 — traduz no caller
                errors.append((leg, e))

        ths = [threading.Thread(target=_run, args=(leg,), daemon=True,
                                name="mux-live") for leg in legs]
        for th in ths:
            th.start()
        for th in ths:
            th.join()
        self._legs = []
        if errors:
            self._leg = errors[0][0]
            raise errors[0][1]

        # bookkeeping da fase 1 (mesmo caminho do VOD)
        st = t.stream if isinstance(t.stream, dict) else {}
        mx = dict(t.mux_audio or {})
        mx["video_done"] = True
        mx.setdefault("vseg", st.get("segments_total") or len(t.segments))
        mx.setdefault("vbytes", st.get("bytes_total") or t.total_size or 0)
        # o nome do áudio pode ter ganhado extensão no prepare (.ts)
        if audio_leg is not None and mx.get("audio_filename") != at.filename:
            mx["audio_filename"] = at.filename
        t.mux_audio = mx
        t.state = TaskState.DOWNLOADING     # ainda falta mesclar
        t.finished_at = None
        t.save_meta()
        self._leg = video_leg               # erro do ffmpeg manda no estado

    def _make_leg(self, task: Task,
                  on_progress: Optional[Callable[[Task], None]]):
        if task.kind in ("hls", "dash"):
            cls = StreamDownloader
        else:
            from minidm.downloader import Downloader

            cls = Downloader
        return cls(task, config=self.config, on_progress=on_progress,
                   stop=self.stop, semaphore=self.sem, bucket=self.bucket,
                   checkpoint_seconds=self.checkpoint_seconds)

    def _recover(self) -> None:
        """Retomada: o sidecar da tarefa principal é a verdade das fases.

        Sem isso, um crash depois do vídeo (antes do áudio) rebaixaria o
        vídeo inteiro — o `.meta.json` marca `video_done`/nomes reais.
        """
        t = self.task
        old = Task.load_meta(t.meta_path)
        if old and old.mux_audio:
            t.mux_audio = old.mux_audio
            if old.mux_audio.get("video_done"):
                # o vídeo pode ter ganhado extensão no prepare (.mp4…)
                t.filename = old.filename
                t.sha256 = old.sha256
                if old.total_size:
                    t.total_size = old.total_size

    def _check_stop(self) -> None:
        """Pausa entre fases: preserva o que já baixou e sinaliza PAUSED."""
        if self.stop.is_set():
            self.task.state = TaskState.PAUSED
            self.task.save_meta()
            raise StreamError("interrompido (progresso salvo)")

    def _audio_task(self) -> Task:
        """Tarefa sintética da perna de áudio (mesma pasta, nome próprio)."""
        t = self.task
        mx = t.mux_audio or {}
        name = mx.get("audio_filename") or (
            os.path.splitext(t.filename)[0] + ".audio")
        at = Task(url=mx.get("url") or t.url, dest_dir=t.dest_dir,
                  filename=name, state=TaskState.QUEUED,
                  kind=mx.get("kind") or t.kind,
                  referer=t.referer, user_agent=t.user_agent,
                  cookie=t.cookie, proxy=t.proxy,
                  quality_label="áudio", origin_url=t.origin_url or t.url)
        if mx.get("rep_id") or mx.get("media"):
            at.stream = {k: mx[k] for k in ("rep_id", "media")
                         if mx.get(k)}
        # retomada da perna: o sidecar do áudio manda (segmentos/planos)
        old = Task.load_meta(at.meta_path)
        if old:
            at.segments = old.segments
            at.total_size = old.total_size
            at.stream = old.stream or at.stream
            at.accept_ranges = old.accept_ranges
            at.etag = old.etag
            at.last_modified = old.last_modified
            at.filename = old.filename
            at.attempts = old.attempts
        return at

    def _mirror_audio(self, atask: Task) -> None:
        """Soma a fase de áudio à barra da tarefa principal (2 e 3)."""
        t = self.task
        st = t.stream if isinstance(t.stream, dict) else None
        if st is None:
            st = t.stream = {}
        mx = t.mux_audio or {}
        vseg = int(mx.get("vseg") or 0)
        vbytes = int(mx.get("vbytes") or 0)
        a_st = atask.stream if isinstance(atask.stream, dict) else {}
        if st.get("live"):
            # AO VIVO: nenhum lado tem total ainda — não toca em
            # `segments_total` (senão a barra "ao vivo" viraria % falso).
            # O áudio entra só como incremento: o vídeo grava `bytes_done`
            # em outra thread, então somamos o DELTA (perda de um byte na
            # corrida é tolerável — é exibição, não integridade).
            now = int(a_st.get("bytes_done") or 0)
            delta = now - int(getattr(self, "_audio_seen", 0))
            if delta > 0:
                st["bytes_done"] = int(st.get("bytes_done") or 0) + delta
            self._audio_seen = now
            return
        a_total = a_st.get("segments_total") or len(atask.segments)
        a_done = (a_st.get("segments_done")
                  or sum(1 for s in atask.segments if s.done))
        if a_total:
            st["segments_total"] = vseg + a_total
            st["segments_done"] = vseg + a_done
        a_bytes = a_st.get("bytes_total") or atask.total_size
        if a_bytes:
            st["bytes_total"] = vbytes + a_bytes
            st["bytes_done"] = vbytes + (a_st.get("bytes_done")
                                         or atask.received)
            t.total_size = vbytes + a_bytes

    def _audio_progress(self, atask: Task) -> None:
        self._mirror_audio(atask)
        if self.on_progress:
            self.on_progress(self.task)

    def _translate(self, exc: Exception) -> None:
        """Erro/pausa de QUALQUER perna vira estado da tarefa principal.

        O scheduler decide "paused" × "error" lendo `task.state` — se a
        perna do áudio pausasse só nela, a tarefa principal ficaria
        "done" e a fila mentiria.
        """
        from minidm.downloader import DownloadError

        if not isinstance(exc, (StreamError, DownloadError)):
            return                        # bug interno: deixa passar cru
        t = self.task
        leg = self._leg
        # espelha a perna do áudio, exceto quando ela TERMINOU (DONE): aí a
        # exceção veio de depois (mux) e o estado gravado por `fail()` manda
        # — espelhar DONE aqui faria a fila reportar sucesso com erro.
        if (leg is not None and leg.task is not t
                and leg.task.state != TaskState.DONE):
            t.state = leg.task.state      # espelha a perna do áudio
            t.error = leg.task.error
        if t.state == TaskState.DOWNLOADING:
            t.state = TaskState.ERROR
        if t.state == TaskState.ERROR and not t.error:
            t.error = str(exc) or "falha no download"
        t.save_meta()

    # ------------------------------------------------------------------ mux
    def _mux(self) -> None:
        """`ffmpeg -c copy` do vídeo + áudio; troca o arquivo final."""
        from minidm.deps import ffmpeg_bin, hidden_kwargs

        t = self.task
        mx = t.mux_audio or {}

        def fail(msg: str) -> None:
            t.state = TaskState.ERROR
            t.error = msg
            t.save_meta()
            raise StreamError(msg)

        ff = ffmpeg_bin()
        if not ff:
            fail("ffmpeg não encontrado — a mesclagem de vídeo+áudio "
                 "precisa dele (winget install Gyan.FFmpeg)")
        # acha o arquivo de áudio (o nome final pode ter ganhado extensão)
        stem0 = os.path.splitext(t.filename)[0]
        audio_path = os.path.join(t.dest_dir, mx.get("audio_filename") or "")
        if not os.path.isfile(audio_path) and os.path.isdir(t.dest_dir):
            cands = [n for n in os.listdir(t.dest_dir)
                     if n.startswith(stem0 + ".audio")
                     and not n.endswith((".meta.json", ".part"))]
            if cands:
                audio_path = os.path.join(t.dest_dir, cands[0])
        if not os.path.isfile(audio_path):
            fail(f"arquivo de áudio não encontrado ({stem0}.audio…)")
        if not os.path.isfile(t.final_path):
            fail(f"arquivo de vídeo não encontrado ({t.filename})")

        # barra em 100% enquanto o ffmpeg copia os fluxos
        st = t.stream if isinstance(t.stream, dict) else None
        if st and st.get("segments_total"):
            st["segments_done"] = st["segments_total"]
            if st.get("bytes_total"):
                st["bytes_done"] = st["bytes_total"]
        if self.on_progress:
            self.on_progress(t)

        stem, ext = os.path.splitext(t.filename)
        out = os.path.join(t.dest_dir, stem + ".mux" + (ext or ".mp4"))
        cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
               "-i", t.final_path, "-i", audio_path,
               "-c", "copy", "-map", "0:v:0", "-map", "1:a:0", out]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=1800,
                                  **hidden_kwargs())
        except (OSError, subprocess.TimeoutExpired) as e:
            fail(f"ffmpeg não rodou: {e}")
            return
        if proc.returncode != 0 or not os.path.exists(out):
            tail = ((proc.stderr or b"").decode("utf-8", "replace")
                    .strip().splitlines())
            fail(f"ffmpeg não mesclou: {tail[-1] if tail else 'erro'}")

        # o arquivo final ainda era o VÍDEO MUDO — troca pelo mesclado
        os.replace(out, t.final_path)
        # limpa os temporários do áudio
        for p in (audio_path, audio_path + ".part",
                  audio_path + ".meta.json"):
            try:
                os.remove(p)
            except OSError:
                pass
        _rmtree_quiet(os.path.join(t.dest_dir, f".{os.path.basename(audio_path)}.segs"))
        # o conteúdo mudou -> hash de novo
        t.sha256 = verify_mod.sha256_file(t.final_path)
        t.checksum_ok = None
        t.mux_audio = None
        t.state = TaskState.DONE
        t.finished_at = time.time()
        t.error = None
        t.save_meta()


def make_downloader(task: Task, config: Optional[AppConfig] = None, **kw):
    """Fábrica de downloader.

    Ordem:
    1. yt-dlp (CLI externa) quando a tarefa ou a config mandam e ele existe;
    2. mux (vídeo+áudio separados) quando a análise ligou o par e há
       ffmpeg -> MuxDownloader;
    3. streams (hls/dash) -> StreamDownloader;
    4. o resto -> Downloader (engine própria, segmentada).
    """
    cfg = config or AppConfig()
    want_ytdlp = getattr(task, "use_ytdlp_downloader", False) or \
        getattr(cfg, "use_ytdlp_downloader", False)
    if want_ytdlp and task.kind in ("hls", "dash", "file", "ytdlp"):
        try:
            from minidm.ytdlp import available
            from minidm.ytdlpdl import YtdlpDownloader

            if available():
                return YtdlpDownloader(task, config=cfg, **kw)
        except Exception:
            pass                      # sem yt-dlp -> engine própria
    if getattr(task, "mux_audio", None) and task.kind in ("hls", "dash",
                                                          "file"):
        try:
            from minidm.deps import ffmpeg_available

            if ffmpeg_available():
                # sem ffmpeg a fábrica nem entrega o MuxDownloader: cai no
                # downloader de antes (uma faixa só) em vez de quebrar
                return MuxDownloader(task, config=config, **kw)
        except Exception:
            pass
    if task.kind in ("hls", "dash"):
        return StreamDownloader(task, config=config, **kw)
    from minidm.downloader import Downloader

    return Downloader(task, config=config, **kw)
