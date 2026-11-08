# -*- coding: utf-8 -*-
"""Defesas contra download vazio (0 bytes) — regressão do bug do celular.

Reproduz o comportamento do YouTube que causou o bug: HEAD com
`Content-Length: 0` e GET sem tamanho. Sem as defesas, `probe` aceitava
`size=0`, a engine planejava 1 segmento de 0 bytes e a tarefa CONCLUÍA
com sucesso — arquivo vazio que o MediaStore publicava em Download/.

Cada camada agora recusa:
  probe                    -> size None (nunca 0) / recupera do Content-Range
  Downloader.run/_prepare  -> DownloadError explicando (página exige yt-dlp)
  _finalize (3 engines)    -> erro "download concluiu vazio — 0 bytes"

Rodar:  python -m unittest discover -s tests -p "test_vazio.py" -v
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from minidm.config import AppConfig
from minidm.meta import Task, TaskState

BODY = b"conteudo de verdade " * 50          # 1000 bytes


class ZeroBase(BaseHTTPRequestHandler):
    """HEAD sempre `Content-Length: 0` (como o YouTube).

    Subclasses escolhem o comportamento do GET:
      modo "range": 206 + `Content-Range: bytes 0-0/<len>` — o tamanho
                    verdadeiro aparece SÓ no Content-Range;
      modo "sem":   200 sem Content-Length (tamanho desconhecido).
    """

    modo = "range"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.modo == "range":
            self.send_response(206)
            self.send_header("Content-Range", f"bytes 0-0/{len(BODY)}")
            self.send_header("Content-Length", "1")
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(BODY[:1])
            return
        # sem tamanho: nem Content-Length nem Content-Range (YouTube)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(BODY)


class RangeHandler(ZeroBase):
    modo = "range"


class SemTamanhoHandler(ZeroBase):
    modo = "sem"


def _servidor(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class TestProbeNaoAceitaZero(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.range_srv, cls.range_base = _servidor(RangeHandler)
        cls.sem_srv, cls.sem_base = _servidor(SemTamanhoHandler)

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.range_srv, cls.sem_srv):
            srv.shutdown()
            srv.server_close()

    def test_positive_size_so_aceita_maior_que_zero(self):
        from minidm.remote import _positive_size
        self.assertFalse(_positive_size("0"))
        self.assertFalse(_positive_size(None))
        self.assertFalse(_positive_size("abc"))
        self.assertTrue(_positive_size("10"))

    def test_head_minte_cl_zero_get_range_recupera_tamanho(self):
        # HEAD mentiroso (cl=0) -> fallback com Range descobre o tamanho
        # verdadeiro no Content-Range (nunca fica 0)
        from minidm.remote import probe
        p = probe(f"{self.range_base}/arquivo.mp4")
        self.assertEqual(p.status, 206)
        self.assertEqual(p.size, len(BODY))
        self.assertTrue(p.accept_ranges)

    def test_sem_tamanho_devolve_none_e_nunca_zero(self):
        # o caso do YouTube: sem tamanho nenhum -> None (nunca 0, que
        # viraria um "download" de 0 bytes concluído com sucesso)
        from minidm.remote import probe
        p = probe(f"{self.sem_base}/arquivo.mp4")
        self.assertEqual(p.status, 200)
        self.assertIsNone(p.size)


class TestPrepareSemTamanho(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = _servidor(SemTamanhoHandler)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_run_sem_tamanho_vira_erro_explicando_ytdlp(self):
        from minidm.downloader import DownloadError, Downloader

        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(data_dir=os.path.join(tmp, "dados"))
            t = Task(url=f"{self.base}/video.mp4",
                     dest_dir=os.path.join(tmp, "Downloads"),
                     filename="video.mp4", state=TaskState.QUEUED)
            dl = Downloader(t, cfg)
            with self.assertRaises(DownloadError) as ctx:
                dl.run()
            msg = str(ctx.exception)
            self.assertIn("não informou o tamanho", msg)
            self.assertIn("yt-dlp", msg)
            self.assertEqual(t.state, TaskState.ERROR)
            self.assertEqual(t.error, msg)      # a interface mostra isso
            self.assertFalse(os.path.exists(t.final_path))  # nada baixado


class TestFinalizeVazio(unittest.TestCase):
    """As 3 engines nunca concluem com arquivo de 0 bytes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = os.path.join(self.tmp.name, "Downloads")
        os.makedirs(self.dest, exist_ok=True)
        self.cfg = AppConfig(data_dir=os.path.join(self.tmp.name, "dados"))

    def _tarefa(self, filename):
        return Task(url="http://exemplo/x", dest_dir=self.dest,
                    filename=filename, state=TaskState.DOWNLOADING)

    def test_downloader_finalize_0_bytes_vira_erro(self):
        from minidm.downloader import DownloadError, Downloader

        t = self._tarefa("vazio.bin")
        with open(t.part_path, "wb"):            # .part vazio (nada chegou)
            pass
        dl = Downloader(t, self.cfg)
        with self.assertRaises(DownloadError) as ctx:
            dl._finalize()
        self.assertIn("0 bytes", str(ctx.exception))
        self.assertEqual(t.state, TaskState.ERROR)
        self.assertEqual(t.error, "download concluiu vazio — 0 bytes")
        # o arquivo (vazio) está marcado como erro, jamais como concluído
        self.assertNotEqual(t.state, TaskState.DONE)

    def test_stream_finalize_0_bytes_vira_erro(self):
        from minidm.stream import StreamDownloader, StreamError

        t = self._tarefa("vazio.ts")
        # sem __init__: só as duas coisas que _finalize usa
        dl = StreamDownloader.__new__(StreamDownloader)
        dl.task = t
        dl._items = []
        with self.assertRaises(StreamError) as ctx:
            dl._finalize()
        self.assertIn("0 bytes", str(ctx.exception))
        self.assertEqual(t.error, "download concluiu vazio — 0 bytes")

    def test_ytdlp_hash_and_done_0_bytes_vira_erro(self):
        from minidm.ytdlpdl import YtdlpDownloader, YtdlpError

        t = self._tarefa("vazio.mp4")
        with open(t.final_path, "wb"):            # yt-dlp "concluiu" vazio
            pass
        dl = YtdlpDownloader.__new__(YtdlpDownloader)
        dl.task = t
        with self.assertRaises(YtdlpError):
            dl._hash_and_done()
        self.assertEqual(t.state, TaskState.ERROR)
        self.assertEqual(t.error, "download concluiu vazio — 0 bytes")


if __name__ == "__main__":
    unittest.main()
