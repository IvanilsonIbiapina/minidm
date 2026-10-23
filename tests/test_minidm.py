"""Testes do minidm com um servidor HTTP local que suporta Range.

Roda com:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from minidm.config import AppConfig
from minidm.downloader import Downloader, auto_segment_count, plan_segments
from minidm.meta import Segment, Task, TaskState
from minidm.remote import probe, still_valid
from minidm.throttle import TokenBucket
from minidm.verify import parse_checksum_text

PAYLOAD = os.urandom(3 * 1024 * 1024 + 137)  # ~3 MB, tamanho "embaraçoso"


def test_config(**kw) -> AppConfig:
    """Config de teste: retries/baratos e sem checagens externas."""
    base = dict(segments=4, retries=2, backoff_base=0.05, backoff_max=0.1,
                verify_signatures=False, remote_checksums=False)
    base.update(kw)
    return AppConfig(**base)


class RangeHandler(BaseHTTPRequestHandler):
    """Serve PAYLOAD com suporte a HEAD e Range."""

    payload = PAYLOAD
    reject_range = False

    def log_message(self, *a):
        pass

    def _headers(self, extra=None):
        self.send_header("ETag", '"teste-1"')
        self.send_header("Last-Modified", "Wed, 01 Jan 2025 00:00:00 GMT")
        if not self.reject_range:
            self.send_header("Accept-Ranges", "bytes")
        for k, v in (extra or {}).items():
            self.send_header(k, v)

    def do_HEAD(self):
        self.send_response(200)
        self._headers({"Content-Length": str(len(self.payload))})
        self.end_headers()

    def do_GET(self):
        rng = self.headers.get("Range")
        if rng and not self.reject_range:
            start_s, end_s = rng.split("=")[1].split("-")
            start = int(start_s)
            end = int(end_s) if end_s else len(self.payload) - 1
            end = min(end, len(self.payload) - 1)
            body = self.payload[start:end + 1]
            self.send_response(206)
            self._headers({
                "Content-Range": f"bytes {start}-{end}/{len(self.payload)}",
                "Content-Length": str(len(body)),
            })
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self._headers({"Content-Length": str(len(self.payload))})
            self.end_headers()
            self.wfile.write(self.payload)


class FlakyHandler(RangeHandler):
    """Falha os primeiros N GETs com 500 (simula instabilidade)."""

    fail_remaining = 0
    _lock = threading.Lock()

    def do_GET(self):
        with FlakyHandler._lock:
            if FlakyHandler.fail_remaining > 0:
                FlakyHandler.fail_remaining -= 1
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        super().do_GET()


class ConcurrencyHandler(RangeHandler):
    """Mede quantos GETs rodam simultaneamente."""

    active = 0
    max_active = 0
    _lock = threading.Lock()
    delay = 0.0

    def do_GET(self):
        with ConcurrencyHandler._lock:
            ConcurrencyHandler.active += 1
            ConcurrencyHandler.max_active = max(ConcurrencyHandler.max_active,
                                                ConcurrencyHandler.active)
        try:
            if ConcurrencyHandler.delay:
                time.sleep(ConcurrencyHandler.delay)
            super().do_GET()
        finally:
            with ConcurrencyHandler._lock:
                ConcurrencyHandler.active -= 1


def serve_handler(handler_cls):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/arquivo.bin"


class Base(unittest.TestCase):
    handler_cls = RangeHandler

    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.url = serve_handler(cls.handler_cls)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-test-")
        RangeHandler.reject_range = False
        FlakyHandler.fail_remaining = 0
        ConcurrencyHandler.active = ConcurrencyHandler.max_active = 0
        ConcurrencyHandler.delay = 0.0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ------------------------------------------------------------------ unitários
class TestPlanejamento(Base):
    def test_plan_segments_cobre_tudo_sem_sobrepor(self):
        segs = plan_segments(1000, 3)
        self.assertEqual(segs[0].start, 0)
        self.assertEqual(segs[-1].end, 999)
        for a, b in zip(segs, segs[1:]):
            self.assertEqual(a.end + 1, b.start)
        self.assertEqual(sum(s.total for s in segs), 1000)

    def test_auto_segment_count_cresce_com_tamanho(self):
        self.assertEqual(auto_segment_count(1024), 1)
        self.assertEqual(auto_segment_count(100 * 1024 * 1024), 8)
        self.assertEqual(auto_segment_count(1024 ** 3), 16)

    def test_probe_descobre_tamanho_e_ranges(self):
        p = probe(self.url)
        self.assertEqual(p.size, len(PAYLOAD))
        self.assertTrue(p.accept_ranges)
        self.assertEqual(p.etag, '"teste-1"')

    def test_parse_checksum_text_formatos(self):
        sha, md5 = parse_checksum_text(
            "9f29f957a0de8d2d906e7419bf031dadb40fd122ede8be9d1ab7e7db953cda24  a.zip")
        self.assertEqual(len(sha), 64)
        self.assertIsNone(md5)
        sha, md5 = parse_checksum_text("MD5 (a.zip) = 9e107d9d372bb6826bd81d3542a419d6")
        self.assertEqual(md5, "9e107d9d372bb6826bd81d3542a419d6")

    def test_token_bucket_limita_velocidade(self):
        bucket = TokenBucket(rate_bps=200_000)   # 200 KB/s
        t0 = time.monotonic()
        for _ in range(4):
            bucket.acquire(100_000)              # 400 KB no total
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.4)    # ~1s teórico, folga p/ CI


# -------------------------------------------------------------------- engine
class TestEngine(Base):
    handler_cls = FlakyHandler   # só falha quando o teste manda
    def test_download_completo_bate_hash(self):
        task = Task(url=self.url, dest_dir=self.tmp, filename="a.bin")
        Downloader(task, test_config(segments=6)).run()
        self.assertEqual(task.state, TaskState.DONE)
        with open(task.final_path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(),
                             hashlib.sha256(PAYLOAD).hexdigest())
        self.assertEqual(len(task.segments), 6)

    def test_retomada_continua_de_onde_parou(self):
        task = Task(url=self.url, dest_dir=self.tmp, filename="b.bin",
                    total_size=len(PAYLOAD), accept_ranges=True,
                    etag='"teste-1"', last_modified="Wed, 01 Jan 2025 00:00:00 GMT")
        task.segments = plan_segments(len(PAYLOAD), 4)
        task.segments[0].received = 700_000
        task.segments[1].received = task.segments[1].total
        with open(task.part_path, "wb") as f:
            f.truncate(len(PAYLOAD))
            f.seek(task.segments[0].start)
            f.write(PAYLOAD[task.segments[0].start:
                            task.segments[0].start + 700_000])
            s1 = task.segments[1]
            f.seek(s1.start)
            f.write(PAYLOAD[s1.start:s1.end + 1])
        task.save_meta()

        received_before = sum(s.received for s in task.segments)
        Downloader(task, test_config()).run()

        self.assertEqual(task.state, TaskState.DONE)
        with open(task.final_path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(),
                             hashlib.sha256(PAYLOAD).hexdigest())
        self.assertGreater(sum(s.received for s in task.segments),
                           received_before)

    def test_retomada_descarta_quando_arquivo_remoto_muda(self):
        task = Task(url=self.url, dest_dir=self.tmp, filename="c.bin",
                    total_size=len(PAYLOAD) + 999, accept_ranges=True,
                    etag='"antigo"')
        task.segments = [Segment(0, len(PAYLOAD) + 998, received=10)]
        with open(task.part_path, "wb") as f:
            f.truncate(len(PAYLOAD) + 999)
        task.save_meta()

        Downloader(task, test_config()).run()
        self.assertEqual(task.total_size, len(PAYLOAD))
        self.assertEqual(task.sha256, hashlib.sha256(PAYLOAD).hexdigest())

    def test_servidor_sem_range_usa_unico_segmento(self):
        RangeHandler.reject_range = True
        task = Task(url=self.url, dest_dir=self.tmp, filename="d.bin")
        Downloader(task, test_config(segments=8)).run()
        self.assertEqual(len(task.segments), 1)
        self.assertEqual(task.sha256, hashlib.sha256(PAYLOAD).hexdigest())

    def test_retry_recupera_de_falha_transitoria(self):
        FlakyHandler.fail_remaining = 3
        task = Task(url=self.url, dest_dir=self.tmp, filename="e.bin")
        Downloader(task, test_config(segments=2, retries=5)).run()
        self.assertEqual(task.state, TaskState.DONE)
        self.assertEqual(task.sha256, hashlib.sha256(PAYLOAD).hexdigest())

    def test_retry_esgota_e_vira_erro(self):
        FlakyHandler.fail_remaining = 10_000
        task = Task(url=self.url, dest_dir=self.tmp, filename="f.bin")
        with self.assertRaises(Exception):
            Downloader(task, test_config(segments=1, retries=1)).run()
        self.assertEqual(task.state, TaskState.ERROR)
        self.assertIn("segmento", task.error or "")

    def test_mirror_assume_quando_principal_morre(self):
        morta = "http://127.0.0.1:1/arquivo.bin"   # porta inválida
        task = Task(url=morta, dest_dir=self.tmp, filename="g.bin",
                    mirrors=[self.url])
        Downloader(task, test_config(segments=3)).run()
        self.assertEqual(task.state, TaskState.DONE)
        self.assertEqual(task.sha256, hashlib.sha256(PAYLOAD).hexdigest())

    def test_checksum_divergente_vira_erro(self):
        task = Task(url=self.url, dest_dir=self.tmp, filename="h.bin",
                    expected_sha256="0" * 64)
        with self.assertRaises(Exception):
            Downloader(task, test_config(segments=2)).run()
        self.assertEqual(task.state, TaskState.ERROR)
        self.assertIn("checksum", task.error or "")

    def test_checkpoint_meta_e_json_valido(self):
        task = Task(url=self.url, dest_dir=self.tmp, filename="i.bin")
        Downloader(task, test_config(segments=3)).run()
        with open(task.meta_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["sha256"], task.sha256)
        self.assertEqual(Task.from_dict(saved).id, task.id)

    def test_still_valid_compara_validadores(self):
        task = Task(url="x", dest_dir=self.tmp, filename="j",
                    total_size=10, etag='"a"')
        p = probe(self.url)
        p2 = type(p)(url=p.url, size=10, accept_ranges=True, etag='"b"',
                     last_modified=p.last_modified, filename="f", status=200)
        self.assertFalse(still_valid(task, p2))
        p3 = type(p)(url=p.url, size=11, accept_ranges=True, etag='"a"',
                     last_modified=p.last_modified, filename="f", status=200)
        self.assertFalse(still_valid(task, p3))


# ----------------------------------------------------------------- scheduler
class TestScheduler(Base):
    handler_cls = ConcurrencyHandler

    def _run(self, max_concurrent, segments, n_tasks, delay=0.15):
        from minidm.scheduler import Scheduler
        from minidm.store import TaskStore

        ConcurrencyHandler.delay = delay
        store = TaskStore(self.tmp)
        ids = []
        for i in range(n_tasks):
            t = Task(url=self.url, dest_dir=self.tmp, filename=f"s{i}.bin")
            store.add(t)
            ids.append(t.id)
        sched = Scheduler(store, test_config(max_concurrent=max_concurrent,
                                             segments=segments))
        sched.start()
        sched.submit(ids)
        self.assertTrue(sched.wait_idle(timeout=60))
        sched.shutdown()
        return store, ids

    def test_pool_serial_nao_sobrepoem_tarefas(self):
        store, ids = self._run(max_concurrent=1, segments=1, n_tasks=2)
        for i in ids:
            t = store.get(i)
            self.assertEqual(t.state, TaskState.DONE, t.error)
            self.assertEqual(t.sha256, hashlib.sha256(PAYLOAD).hexdigest())
        self.assertEqual(ConcurrencyHandler.max_active, 1)

    def test_pool_paralelo_executa_juntas(self):
        store, ids = self._run(max_concurrent=2, segments=2, n_tasks=2)
        for i in ids:
            self.assertEqual(store.get(i).state, TaskState.DONE)
        self.assertGreaterEqual(ConcurrencyHandler.max_active, 2)

    def test_pause_de_tarefa_na_fila(self):
        from minidm.scheduler import Scheduler
        from minidm.store import TaskStore

        store = TaskStore(self.tmp)
        t = Task(url=self.url, dest_dir=self.tmp, filename="p.bin")
        store.add(t)
        sched = Scheduler(store, test_config(max_concurrent=1, segments=1))
        # sem start(): nada consome a fila
        self.assertTrue(sched.pause(t.id))
        self.assertEqual(store.get(t.id).state, TaskState.PAUSED)
        self.assertEqual(sched.submit([t.id]), 1)
        self.assertEqual(store.get(t.id).state, TaskState.QUEUED)


if __name__ == "__main__":
    unittest.main()
