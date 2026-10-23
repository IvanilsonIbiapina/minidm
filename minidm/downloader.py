"""Engine de download do minidm (v2).

Diferenças do v1:

* **retry por segmento** com backoff exponencial + jitter — um reset de
  conexão não derruba mais a tarefa;
* **mirrors**: tenta a URL principal e, nas retentativas, alterna para as
  URLs alternativas cadastradas;
* **throttle global**: todos os workers compartilham um token bucket;
* **orçamento de conexões**: semáforo global (N aberturas HTTP ao vivo);
* **verificação final**: SHA-256 local, checksums publicados pelo servidor
  (`.sha256`/`.md5`) e assinatura Authenticode quando aplicável.

O resto permanece igual ao v1: pré-alocação do `.part`, gravação direta na
posição do segmento, checkpoint periódico em `.meta.json` e retomada
validada por `ETag`/tamanho.
"""

from __future__ import annotations

import os
import random
import threading
import time
from typing import Callable, Optional

import requests

from minidm.config import AppConfig
from minidm.meta import Segment, Task, TaskState
from minidm.remote import (TIMEOUT, USER_AGENT, probe, still_valid,
                           _size_from_content_range)
from minidm.throttle import TokenBucket
from minidm import verify as verify_mod

CHUNK = 256 * 1024
CHECKPOINT_SECONDS = 2.0


class DownloadError(Exception):
    pass


class StopRequested(Exception):
    pass


class IncompleteSegment(Exception):
    pass


class MirrorMismatch(Exception):
    pass


def plan_segments(total: int, count: int) -> list[Segment]:
    """Divide [0, total-1] em `count` fatias quase iguais."""
    count = max(1, min(count, total))
    size = total // count
    segs = []
    start = 0
    for i in range(count):
        end = total - 1 if i == count - 1 else start + size - 1
        segs.append(Segment(start=start, end=end))
        start = end + 1
    return segs


def auto_segment_count(total: int) -> int:
    """Heurística simples: mais segmentos para arquivos maiores."""
    if total >= 512 * 1024 * 1024:
        return 16
    if total >= 64 * 1024 * 1024:
        return 8
    if total >= 8 * 1024 * 1024:
        return 4
    return 1


class Downloader:
    def __init__(
        self,
        task: Task,
        config: Optional[AppConfig] = None,
        on_progress: Optional[Callable[[Task], None]] = None,
        stop: Optional[threading.Event] = None,
        semaphore: Optional[threading.BoundedSemaphore] = None,
        bucket: Optional[TokenBucket] = None,
        checkpoint_seconds: float = CHECKPOINT_SECONDS,
    ):
        self.task = task
        self.config = config or AppConfig()
        self.on_progress = on_progress
        self.stop = stop or threading.Event()
        self.sem = semaphore
        self.bucket = bucket
        self.checkpoint_seconds = checkpoint_seconds
        self._segments_lock = threading.Lock()
        self._speed_samples: list[tuple[float, int]] = []
        self._urls = [task.url] + [u for u in task.mirrors if u != task.url]

    # ---------------------------------------------------------------- top
    def run(self) -> Task:
        t = self.task
        t.state = TaskState.DOWNLOADING
        t.error = None
        try:
            self._prepare()
            self._spawn_workers()
            self._checkpoint_loop()
            self._finalize()
        except DownloadError:
            # pause já marcou PAUSED; qualquer outro caso vira ERROR
            if self.task.state != TaskState.PAUSED:
                self.task.state = TaskState.ERROR
            self.task.save_meta()
            raise
        return t

    def request_stop(self) -> None:
        self.stop.set()

    # ----------------------------------------------------------- headers
    def _headers(self, extra: dict | None = None) -> dict:
        t = self.task
        h = {"User-Agent": t.user_agent or self.config.user_agent or USER_AGENT}
        cookie = t.cookie or self.config.cookie
        if cookie:
            h["Cookie"] = cookie
        if extra:
            h.update(extra)
        return h

    def _session(self, url: str) -> requests.Session:
        s = requests.Session()
        proxy = self.task.proxy or self.config.proxy
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        return s

    # ----------------------------------------------------------- preparação
    def _prepare(self) -> None:
        t = self.task
        os.makedirs(t.dest_dir, exist_ok=True)

        # sonda: principal + mirrors, na ordem
        last_err = None
        p = None
        for url in self._urls:
            try:
                p = probe(url, headers=self._headers())
                if p.status < 400 and p.size is not None:
                    break
                last_err = f"{url}: HTTP {p.status}"
            except requests.RequestException as e:
                last_err = f"{url}: {e}"
            p = None
        if p is None:
            raise DownloadError(f"nenhuma URL respondeu ({last_err})")

        resumed = False
        old = Task.load_meta(t.meta_path)
        if old and os.path.exists(t.part_path):
            if (old.url in self._urls and still_valid(old, p)
                    and old.segments and old.total_size == p.size):
                t.total_size = old.total_size
                t.accept_ranges = old.accept_ranges
                t.etag = old.etag
                t.last_modified = old.last_modified
                t.segments = old.segments
                resumed = True
            else:
                # arquivo remoto mudou: descarta o parcial
                old.drop_meta()

        if not resumed:
            t.total_size = p.size
            t.accept_ranges = p.accept_ranges
            t.etag = p.etag
            t.last_modified = p.last_modified
            n = self.config.segments or auto_segment_count(p.size)
            if not p.accept_ranges:
                n = 1  # sem Range, só dá pra fazer sequencial
            t.segments = plan_segments(p.size, n)
            with open(t.part_path, "wb") as f:
                f.truncate(p.size)

        t.save_meta()

    # ------------------------------------------------------------- workers
    def _spawn_workers(self) -> None:
        self._threads = []
        for i in range(len(self.task.segments)):
            if self.task.segments[i].done:
                continue
            th = threading.Thread(target=self._worker, args=(i,),
                                  name=f"seg-{i}", daemon=True)
            th.start()
            self._threads.append(th)

    def _url_for_attempt(self, attempt: int) -> str:
        """Nas retentativas, alterna entre principal e mirrors."""
        return self._urls[attempt % len(self._urls)]

    def _worker(self, index: int) -> None:
        cfg = self.config
        for attempt in range(cfg.retries + 1):
            if self.stop.is_set():
                return
            seg = self.task.segments[index]
            if seg.done:
                return
            try:
                self._stream_segment(index, self._url_for_attempt(attempt))
                return
            except StopRequested:
                return
            except (requests.RequestException, IncompleteSegment,
                    MirrorMismatch, OSError) as e:
                if attempt >= cfg.retries:
                    with self._segments_lock:
                        self.task.error = f"segmento {index}: {e}"
                    return
                delay = min(cfg.backoff_max, cfg.backoff_base * (2 ** attempt))
                self.stop.wait(delay * random.uniform(0.7, 1.3))

    def _stream_segment(self, index: int, url: str) -> None:
        t = self.task
        seg = t.segments[index]
        acquired = self.sem.acquire(timeout=60) if self.sem else True
        if not acquired:
            raise requests.Timeout("sem conexões disponíveis")
        try:
            start = seg.start + seg.received
            headers = self._headers()
            if start <= seg.end:
                headers["Range"] = f"bytes={start}-{seg.end}"
            with self._session(url) as s:
                with s.get(url, headers=headers, stream=True,
                           timeout=TIMEOUT) as r:
                    if r.status_code not in (200, 206):
                        raise requests.HTTPError(f"HTTP {r.status_code}")
                    if r.status_code == 200 and start > 0:
                        # servidor ignorou o Range: recomeça este segmento
                        with self._segments_lock:
                            seg.received = 0
                        start = seg.start
                    total = _size_from_content_range(r.headers.get("Content-Range"))
                    if total and t.total_size and total != t.total_size:
                        raise MirrorMismatch(
                            f"mirror serve tamanho {total}, esperado {t.total_size}")
                    with open(t.part_path, "r+b", buffering=0) as f:
                        f.seek(start)
                        for chunk in r.iter_content(CHUNK):
                            if self.stop.is_set():
                                raise StopRequested()
                            if not chunk:
                                continue
                            if self.bucket:
                                self.bucket.acquire(len(chunk), stop=self.stop)
                            f.write(chunk)
                            with self._segments_lock:
                                seg.received += len(chunk)
                                self._speed_samples.append(
                                    (time.monotonic(), seg.received))
        finally:
            if self.sem and acquired:
                self.sem.release()

        if not seg.done:
            raise IncompleteSegment(
                f"faltam {seg.total - seg.received} de {seg.total} bytes")

    # ------------------------------------------------------- checkpointing
    def _checkpoint_loop(self) -> None:
        """Espera as threads e grava o progresso periodicamente."""
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
            raise DownloadError("interrompido (progresso salvo)")

        if self.task.error:
            raise DownloadError(self.task.error)
        if not all(s.done for s in self.task.segments):
            self.task.error = "nem todos os segmentos completaram"
            raise DownloadError(self.task.error)

    # -------------------------------------------------------------- final
    def _finalize(self) -> None:
        t = self.task
        os.replace(t.part_path, t.final_path)

        # 1) hash local
        t.sha256 = verify_mod.sha256_file(t.final_path)

        # 2) checksums publicados ao lado do arquivo no servidor
        if self.config.remote_checksums:
            remote_sha, remote_md = verify_mod.fetch_remote_checksums(
                t.url, self._headers()["User-Agent"])
            t.remote_sha256, t.remote_md5 = remote_sha, remote_md

        # 3) comparações + Authenticode
        report = verify_mod.verify_file(
            t.final_path,
            expected_sha256=t.expected_sha256,
            remote_sha256=t.remote_sha256,
            remote_md5=t.remote_md5,
            check_signature=self.config.verify_signatures,
        )

        t.sha256 = report["sha256"]
        oks = [report["expected_sha256_ok"], report["remote_sha256_ok"],
               report["remote_md5_ok"]]
        if any(v is False for v in oks):
            t.checksum_ok = False
            t.state = TaskState.ERROR
            t.error = "checksum não bate com o publicado"
            t.save_meta()
            raise DownloadError(
                f"checksum divergente (esperado={report['expected_sha256_ok']}, "
                f"sha256 remoto={report['remote_sha256_ok']}, "
                f"md5 remoto={report['remote_md5_ok']})")
        t.checksum_ok = True if any(v is True for v in oks) else None
        t.signature_status = report["signature_status"]
        t.signature_signer = report["signature_signer"]

        t.state = TaskState.DONE
        t.finished_at = time.time()
        t.save_meta()

    # -------------------------------------------------------------- speed
    def speed_bps(self, window: float = 5.0) -> float:
        """Taxa média aproximada nos últimos `window` segundos."""
        now = time.monotonic()
        total_now = self.task.received
        old = None
        for ts, val in self._speed_samples:
            if now - ts >= window:
                old = (ts, val)
            else:
                break
        if old is None:
            if not self._speed_samples:
                return 0.0
            ts, val = self._speed_samples[0]
            return (total_now - val) / max(now - ts, 1e-6)
        ts, val = old
        return (total_now - val) / max(now - ts, 1e-6)
