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

    # ----------------------------------------------------------------- top
    def run(self) -> Task:
        t = self.task
        t.state = TaskState.DOWNLOADING
        t.error = None
        try:
            self._prepare()
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
            if not t.segments:
                t.segments = _segments_for_items(self._items)
            return
        if cached:
            # escolha parcial (rep_id/variant_index) vinda do chamador
            self._plan = dict(cached)

        plan = self._resolve(t.url)
        self._plan = plan
        self._items = plan["items"]
        t.segments = _segments_for_items(self._items)
        t.total_size = plan.get("bytes_total")
        t.accept_ranges = False
        t.stream = {
            "base_url": t.url,
            "resolved_url": plan.get("resolved_url", t.url),
            "kind": plan["kind"],
            "items": self._items,
            "bytes_done": 0,
            "bytes_total": t.total_size,
            "segments_done": 0,
            "segments_total": len(self._items),
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
        return {"kind": "hls", "items": items, "init": init,
                "resolved_url": resolved, "base_url": url,
                "durations": durations, "encrypted": playlist["encrypted"]}

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
        pool = video or [r for r in reps if r["mimeType"].startswith("audio")]
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
        """
        pending = [i for i in range(len(self._items)) if not self._item_done(i)]
        if not pending:
            return
        self._queue = list(pending)
        limit = max(1, self.config.max_connections)
        frags = getattr(self.config, "concurrent_fragments", 0) or 0
        if frags > 0:
            limit = min(limit, frags)
        n = min(len(pending), limit)
        for w in range(n):
            th = threading.Thread(target=self._worker, name=f"stream-{w}",
                                  daemon=True)
            th.start()
            self._threads.append(th)

    def _next_index(self) -> Optional[int]:
        with self._lock:
            return self._queue.pop(0) if self._queue else None

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
                        break
                    delay = min(cfg.backoff_max,
                                cfg.backoff_base * (2 ** attempt))
                    self.stop.wait(delay * (0.7 + 0.6 * ((attempt * 37) % 10) / 10))
                except StreamError as e:
                    with self._lock:
                        self.task.error = f"segmento {index}: {e}"
                    break

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

    # --------------------------------------------------------------- final
    def _finalize(self) -> None:
        t = self.task
        # concatena init + segmentos, em ordem, direto no .part
        with open(t.part_path, "wb") as out:
            for i in range(len(self._items)):
                with open(self._seg_path(i), "rb") as seg:
                    while True:
                        chunk = seg.read(CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
        _rmtree_quiet(self._segs_dir())

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


def make_downloader(task: Task, config: Optional[AppConfig] = None, **kw):
    """Fábrica de downloader.

    Ordem:
    1. yt-dlp (CLI externa) quando a tarefa ou a config mandam e ele existe;
    2. streams (hls/dash) -> StreamDownloader;
    3. o resto -> Downloader (engine própria, segmentada).
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
    if task.kind in ("hls", "dash"):
        return StreamDownloader(task, config=config, **kw)
    from minidm.downloader import Downloader

    return Downloader(task, config=config, **kw)
