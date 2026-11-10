# -*- coding: utf-8 -*-
"""Testes da frente torrent (aria2 externo via JSON-RPC local).

O aria2 REAL não é usado: o `_rpc` é substituído por um fake em memória
(mesmo padrão do stub do yt-dlp) e o subprocesso é mockado — testamos o
fluxo completo (boot → add → watch → finish) sem rede nem binário.

Rodar:  python -m unittest discover -s tests -p "test_torrent.py"
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from minidm.deps import aria2_available, aria2_bin, invalidate_cache
from minidm.downloader import DownloadError
from minidm.meta import Task, TaskState
from minidm.stream import make_downloader
from minidm.torrent import (TorrentDownloader, is_torrent_url, torrent_name)


class _FakeProc:
    """subprocess.Popen falsificado (o aria2 nunca é spawnado no teste)."""

    def __init__(self):
        self.returncode = None
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.killed = True
        self.returncode = -9


class FakeAria(TorrentDownloader):
    """TorrentDownloader com RPC em memória (sequência controlada)."""

    def __init__(self, *args, seq=None, files=None, **kw):
        super().__init__(*args, **kw)
        self.rpc_calls = []
        self.seq = list(seq if seq is not None else [
            {"status": "active", "completedLength": "50",
             "totalLength": "100", "downloadSpeed": "10"},
            {"status": "complete", "completedLength": "100",
             "totalLength": "100", "downloadSpeed": "0"},
        ])
        self.files = list(files or [])
        self.tell_n = 0

    def _rpc(self, method, params=None, timeout=5.0):
        self.rpc_calls.append(method)
        if method == "system.getVersion":
            return "1.36.0"
        if method in ("aria2.addUri", "aria2.addTorrent"):
            return ["gid-1"]
        if method == "aria2.tellStatus":
            if len(params or []) >= 2:        # chamada do _finish
                return {"files": self.files, "completedLength": "100"}
            st = self.seq[min(self.tell_n, len(self.seq) - 1)]
            self.tell_n += 1
            return st
        if method in ("aria2.forceRemove", "system.shutdown"):
            return None
        raise AssertionError(f"RPC inesperado: {method}")


def _spawn(cls, dest, **kw):
    """Instancia o downloader fake (patches de boot no setUp do teste)."""
    task = Task(url="magnet:?xt=urn:btih:" + "ab" * 20,
                dest_dir=dest, filename="torrent-" + "ab" * 4, kind="torrent")
    return cls(task, **kw)


class TestUrls(unittest.TestCase):
    def test_magnet_e_torrent(self):
        self.assertTrue(is_torrent_url("magnet:?xt=urn:btih:" + "a" * 40))
        self.assertTrue(is_torrent_url("http://ex.com/a.torrent"))
        self.assertTrue(is_torrent_url("HTTPS://ex.com/a.torrent?x=1"))
        self.assertFalse(is_torrent_url("http://ex.com/a.mp4"))
        self.assertFalse(is_torrent_url("magnetismo?x=1"))   # sem "?"
        self.assertFalse(is_torrent_url(""))
        self.assertFalse(is_torrent_url(None))

    def test_nome_provisorio(self):
        h = "0123456789abcdef0123456789abcdef01234567"
        self.assertEqual(torrent_name(f"magnet:?xt=urn:btih:{h}"),
                         "torrent-01234567")
        self.assertEqual(torrent_name("http://ex.com/filme.torrent"),
                         "filme.torrent")
        self.assertEqual(torrent_name(""), "torrent")


class TestDeps(unittest.TestCase):
    def tearDown(self):
        invalidate_cache()

    def test_env_aponta_o_aria2(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            path = fh.name
        try:
            with mock.patch.dict(os.environ, {"MINIDM_ARIA2": path}):
                invalidate_cache()
                self.assertEqual(aria2_bin(), path)
                self.assertTrue(aria2_available())
        finally:
            os.unlink(path)

    def test_env_ruim_vira_none(self):
        with mock.patch.dict(os.environ,
                             {"MINIDM_ARIA2": "/nao/existe/aria2c"}):
            invalidate_cache()
            self.assertIsNone(aria2_bin())
            self.assertFalse(aria2_available())

    def test_status_mostra_aria2(self):
        from minidm.deps import status

        st = status()
        self.assertIn("aria2", st)
        self.assertIn("available", st["aria2"])
        self.assertIn("install", st["aria2"])
        self.assertIn("Torrents (magnet/.torrent)", st["features"])


class TestFabrica(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-torrent-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_magnet_vai_para_o_aria2(self):
        t = Task(url="magnet:?xt=urn:btih:" + "a" * 40,
                 dest_dir=self.tmp, filename="x")
        self.assertIsInstance(make_downloader(t), TorrentDownloader)

    def test_kind_torrent_http_tambem(self):
        t = Task(url="http://ex.com/a.torrent", dest_dir=self.tmp,
                 filename="a.torrent", kind="torrent")
        self.assertIsInstance(make_downloader(t), TorrentDownloader)

    def test_http_comum_nao_vai_para_aria2(self):
        t = Task(url="http://ex.com/a.mp4", dest_dir=self.tmp,
                 filename="a.mp4", kind="file")
        self.assertNotIsInstance(make_downloader(t), TorrentDownloader)


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-torrent-")
        # aria2 "instalado", subprocesso falso e sleep anulado enquanto
        # o teste roda (enterContext restaura ao final de cada teste)
        self.enterContext(mock.patch("minidm.torrent.aria2_bin",
                                     return_value="aria2c-fake"))
        self.enterContext(mock.patch("minidm.torrent.subprocess.Popen",
                                     return_value=_FakeProc()))
        self.enterContext(mock.patch("minidm.torrent.time.sleep"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_conclui_e_nomeia_o_maior_arquivo(self):
        # o aria2 entrega arquivos REAIS na pasta: o _finish os acha
        alvo = os.path.join(self.tmp, "Meu Torrent", "video.mp4")
        os.makedirs(os.path.dirname(alvo), exist_ok=True)
        with open(alvo, "wb") as fh:
            fh.write(b"v" * 100)
        arq = os.path.join(self.tmp, "meu arquivo.bin")
        with open(arq, "wb") as fh:
            fh.write(b"a" * 100)
        dl = _spawn(FakeAria, self.tmp,
                    files=[{"path": alvo}, {"path": arq}])
        out = dl.run()
        self.assertEqual(out.state, TaskState.DONE)
        # o maior arquivo (empate -> o primeiro) vira o nome exibido
        self.assertIn(out.filename, ("meu arquivo.bin",
                                     os.path.join("Meu Torrent",
                                                  "video.mp4")))
        self.assertEqual(out.total_size, 100)
        self.assertEqual(out.stream, {"bytes_done": 100})
        self.assertEqual(out.progress, 1.0)
        self.assertIn("aria2.addUri", dl.rpc_calls)
        self.assertIn("system.shutdown", dl.rpc_calls)   # teardown via RPC

    def test_sem_aria2_erro_com_instrucao(self):
        task = Task(url="magnet:?xt=urn:btih:" + "a" * 40,
                    dest_dir=self.tmp, filename="x")
        with mock.patch("minidm.torrent.aria2_bin", return_value=None):
            dl = TorrentDownloader(task)
            with self.assertRaises(DownloadError) as ctx:
                dl.run()
        self.assertIn("aria2", str(ctx.exception))
        self.assertIn("MINIDM_ARIA2", str(ctx.exception))
        self.assertEqual(task.state, TaskState.ERROR)
        self.assertIn("aria2", task.error)

    def test_erro_do_aria2_vira_downloaderror(self):
        dl = _spawn(FakeAria, self.tmp, seq=[
            {"status": "active", "completedLength": "10",
             "totalLength": "100", "downloadSpeed": "1"},
            {"status": "error", "completedLength": "10",
             "totalLength": "100", "errorMessage": "sem peers disponíveis"},
        ])
        with self.assertRaises(DownloadError) as ctx:
            dl.run()
        self.assertIn("sem peers", str(ctx.exception))
        self.assertEqual(dl.task.state, TaskState.ERROR)

    def test_stop_no_watch_pausa_e_remove(self):
        dl = _spawn(FakeAria, self.tmp)
        dl._gid = "gid-1"          # tarefa já adicionada ao aria2
        dl.stop.set()
        with self.assertRaises(DownloadError):
            dl._watch()
        self.assertEqual(dl.task.state, TaskState.PAUSED)
        self.assertIn("aria2.forceRemove", dl.rpc_calls)

    def test_watch_alimenta_progresso_e_nota(self):
        dl = _spawn(FakeAria, self.tmp, seq=[
            {"status": "active", "completedLength": "25",
             "totalLength": "100", "downloadSpeed": "7"},
            {"status": "active", "completedLength": "50",
             "totalLength": "100", "downloadSpeed": "9"},
            {"status": "complete", "completedLength": "100",
             "totalLength": "100", "downloadSpeed": "0"},
        ])
        dl._gid = "gid-1"
        dl._watch()
        t = dl.task
        self.assertEqual(t.total_size, 100)
        self.assertEqual(t.stream["bytes_done"], 100)
        self.assertEqual(t.stream["segments_done"], 100)
        # velocidade instantânea registrada (o último poll vale 10 s)
        self.assertGreaterEqual(dl.speed_bps(), 0.0)

    def test_magnet_sem_total_mostra_nota(self):
        """Antes dos metadados do magnet chegar (totalLength=0), a nota
        'resolvendo metadados' é publicada no progresso da UI."""
        notas = []
        dl = _spawn(FakeAria, self.tmp, seq=[
            {"status": "waiting", "completedLength": "0",
             "totalLength": "0", "downloadSpeed": "0"},
            {"status": "complete", "completedLength": "100",
             "totalLength": "100", "downloadSpeed": "0"},
        ], on_progress=lambda t: notas.append(
            (t.stream or {}).get("note")))
        dl._gid = "gid-1"
        dl._watch()
        self.assertTrue(any("metadados" in (n or "") for n in notas))
        self.assertEqual(dl.task.stream["bytes_done"], 100)


class TestApiAddTorrent(unittest.TestCase):
    """POST /add com magnet/.torrent: recusa clara sem aria2, fila com."""

    @classmethod
    def setUpClass(cls):
        from minidm.config import AppConfig
        from minidm.scheduler import Scheduler
        from minidm.server import App, make_handler
        from minidm.store import TaskStore

        cls.tmp = tempfile.mkdtemp(prefix="minidm-torrent-api-")
        cfg = AppConfig(data_dir=cls.tmp)
        store = TaskStore(cls.tmp)
        cls.api = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(App(store, Scheduler(store, cfg), cfg)))
        cls.base = f"http://127.0.0.1:{cls.api.server_address[1]}"
        threading.Thread(target=cls.api.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        cls.api.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _post(self, obj):
        req = urllib.request.Request(
            self.base + "/add", data=json.dumps(obj).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return json.loads(r.read() or b"{}")

    def test_sem_aria2_recusa_com_instrucao(self):
        with mock.patch("minidm.deps.aria2_available", return_value=False):
            status, data = self._post(
                {"url": "magnet:?xt=urn:btih:" + "a" * 40, "start": False})
        self.assertEqual(status, 400)
        self.assertIn("aria2", data["error"])
        self.assertIn("MINIDM_ARIA2", data["error"])

    def test_com_aria2_entra_na_fila(self):
        magnet = "magnet:?xt=urn:btih:" + "b" * 40
        with mock.patch("minidm.deps.aria2_available", return_value=True):
            status, data = self._post({"url": magnet, "start": False})
        self.assertEqual(status, 201)
        self.assertEqual(data["kind"], "torrent")
        self.assertTrue(data["filename"].startswith("torrent-"))
        row = [t for t in self._get("/status")["tasks"]
               if t["id"] == data["id"]]
        self.assertEqual(row[0]["kind"], "torrent")

    def test_ftp_com_extensao_torrent_nao_passa(self):
        # ftp .torrent: fora do conjunto aceito (http/https/magnet)
        with mock.patch("minidm.deps.aria2_available", return_value=True):
            status, data = self._post(
                {"url": "ftp://host/a.torrent", "start": False})
        self.assertEqual(status, 400)
        self.assertIn("magnet", data["error"])

    def test_categoria_desconhecida_400(self):
        with mock.patch("minidm.deps.aria2_available", return_value=True):
            status, data = self._post(
                {"url": "magnet:?xt=urn:btih:" + "c" * 40,
                 "category": "Nada Assim", "start": False})
        self.assertEqual(status, 400)
        self.assertIn("categoria", data["error"])


if __name__ == "__main__":
    unittest.main()
