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
from minidm.torrent import (TorrentDownloader, _magnet_dn, is_torrent_url,
                            torrent_name)


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

    def __init__(self, *args, seq=None, files=None, files_rpc=None, **kw):
        super().__init__(*args, **kw)
        self.rpc_calls = []
        self.rpc_params = []        # (método, params) p/ asserir a forma
        self.seq = list(seq if seq is not None else [
            {"status": "active", "completedLength": "50",
             "totalLength": "100", "downloadSpeed": "10"},
            {"status": "complete", "completedLength": "100",
             "totalLength": "100", "downloadSpeed": "0"},
        ])
        self.files = list(files or [])
        # aria2.getFiles (formato real: path + length) — por padrão
        # espelha os mesmos caminhos do tellStatus do _finish
        self.files_rpc = list(files_rpc if files_rpc is not None
                              else self.files)
        self.tell_n = 0

    def _rpc(self, method, params=None, timeout=5.0):
        self.rpc_calls.append(method)
        self.rpc_params.append((method, list(params or [])))
        # nomes EXATOS do aria2 real (1.37.0): o build winget não tem
        # `system.getVersion`/`system.shutdown` — só os prefixos `aria2.`
        if method == "aria2.getVersion":
            return {"version": "1.37.0"}
        if method in ("aria2.addUri", "aria2.addTorrent"):
            return ["gid-1"]
        if method == "aria2.getFiles":
            out = []
            for f in self.files_rpc:
                ent = dict(f)
                if "length" not in ent and ent.get("path"):
                    try:
                        ent["length"] = str(os.path.getsize(ent["path"]))
                    except OSError:
                        ent["length"] = "0"
                out.append(ent)
            return out
        if method == "aria2.tellStatus":
            if len(params or []) >= 2:        # chamada do _finish
                return {"files": self.files, "completedLength": "100"}
            st = self.seq[min(self.tell_n, len(self.seq) - 1)]
            self.tell_n += 1
            return st
        if method in ("aria2.forceRemove", "aria2.shutdown"):
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

    def test_dn_do_magnet_e_decodificado(self):
        """`dn=` é o nome que o link promete: sai percent+plus-decodado;
        sem `dn` (ou fora de magnet) devolve vazio."""
        h = "0123456789abcdef0123456789abcdef01234567"
        self.assertEqual(
            _magnet_dn(f"magnet:?xt=urn:btih:{h}&dn=Meu+Filme%20(2026)"),
            "Meu Filme (2026)")
        self.assertEqual(_magnet_dn(f"magnet:?xt=urn:btih:{h}"), "")
        self.assertEqual(_magnet_dn("http://ex.com/filme.torrent"), "")
        self.assertEqual(_magnet_dn("magnetismo?dn=X"), "")   # sem "magnet:"
        self.assertEqual(_magnet_dn(""), "")


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
        # a lista de arquivos (relativa à pasta) sobrevive ao _finish
        self.assertEqual(out.stream["bytes_done"], 100)
        self.assertEqual({f["path"] for f in out.stream.get("files", [])},
                         {"Meu Torrent/video.mp4", "meu arquivo.bin"})
        self.assertEqual(out.progress, 1.0)
        self.assertIn("aria2.addUri", dl.rpc_calls)
        self.assertIn("aria2.shutdown", dl.rpc_calls)   # teardown via RPC

    def test_add_uri_repassa_magnet_inteiro(self):
        """O magnet chega ao aria2 byte a byte: o conteúdo vem só do
        `xt=` — `dn`/`tr` não podem ser cortados, decodificados nem
        re-encodados no caminho (webui/CLI/addUri)."""
        magnet = ("magnet:?xt=urn:btih:" + "ab" * 20
                  + "&dn=Meu+Filme%20(2026)"
                  + "&tr=udp%3A%2F%2Ftracker.example%3A6969%2Fannounce")
        task = Task(url=magnet, dest_dir=self.tmp,
                    filename="torrent-abababab", kind="torrent")
        dl = FakeAria(task)
        self.assertEqual(dl._add(), "gid-1")
        self.assertEqual(dl.rpc_params[-1],
                         ("aria2.addUri", [[magnet]]))

    def test_add_torrent_envia_params_do_aria2(self):
        """Regressão do HTTP 400: o `_rpc` já prepende o token, então o
        `_add` manda [base64, uris] SEM None — o formato antigo
        [token, null, b64, []] era recusado pelo aria2 real 1.37.0
        (reproduzido offline) e quebrava TODO download de `.torrent`."""
        task = Task(url="http://ex.com/a.torrent", dest_dir=self.tmp,
                    filename="a.torrent", kind="torrent")
        dl = FakeAria(task)
        with mock.patch.object(FakeAria, "_torrent_bytes",
                               return_value="YWJjZA=="):
            self.assertEqual(dl._add(), "gid-1")
        self.assertEqual(dl.rpc_params[-1],
                         ("aria2.addTorrent", ["YWJjZA==", []]))

    def test_rpc_prepoe_o_token_uma_vez_so(self):
        """O payload real leva [token, …params] com o token exatamente
        uma vez — é essa composição que somava o None do bug."""
        task = Task(url="magnet:?xt=urn:btih:" + "a" * 40,
                    dest_dir=self.tmp, filename="x")
        dl = TorrentDownloader(task)
        dl._port = 12345
        corpo = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"result": ["gid-1"]}'

        def fake_urlopen(req, timeout=None):
            corpo["payload"] = req.data
            return _Resp()

        with mock.patch("minidm.torrent.urllib.request.urlopen",
                        side_effect=fake_urlopen):
            out = dl._rpc("aria2.addTorrent", ["b64", []])
        payload = json.loads(corpo["payload"])
        self.assertEqual(out, ["gid-1"])
        self.assertEqual(payload["params"],
                         [f"token:{dl._token}", "b64", []])

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

    def test_nome_real_vem_do_info_do_torrent(self):
        """Assim que o aria2 conhece os metadados, `bittorrent.info.name`
        vira o nome da tarefa (regressão do bônus do e5827ca: o provisório
        torrent-<hash> some da UI e o título real aparece)."""
        dl = _spawn(FakeAria, self.tmp, seq=[
            {"status": "active", "completedLength": "50",
             "totalLength": "100", "downloadSpeed": "10",
             "bittorrent": {"info": {"name": "Filme (2026)"}}},
            {"status": "complete", "completedLength": "100",
             "totalLength": "100", "downloadSpeed": "0"},
        ])
        dl._gid = "gid-1"
        dl._watch()
        self.assertEqual(dl.task.filename, "Filme (2026)")

    def test_watch_carrega_a_lista_de_arquivos_uma_vez(self):
        """Assim que o aria2 conhece os metadados, a lista de arquivos
        (caminho relativo + tamanho) entra no stream — é o que mostra
        ao usuário que o torrent é um PACOTE de N arquivos."""
        alvo = os.path.join(self.tmp, "Pacote", "video.mp4")
        os.makedirs(os.path.dirname(alvo), exist_ok=True)
        with open(alvo, "wb") as fh:
            fh.write(b"v" * 50)
        dl = _spawn(FakeAria, self.tmp, seq=[
            {"status": "active", "completedLength": "50",
             "totalLength": "100", "downloadSpeed": "10",
             "bittorrent": {"info": {"name": "Pacote"}}},
            {"status": "complete", "completedLength": "100",
             "totalLength": "100", "downloadSpeed": "0",
             "bittorrent": {"info": {"name": "Pacote"}}},
        ], files=[{"path": alvo}])
        dl._gid = "gid-1"
        dl._watch()
        self.assertEqual(dl.task.stream.get("files"),
                         [{"path": "Pacote/video.mp4", "bytes": 50}])
        # pediu 1× só — não repete o RPC a cada poll
        self.assertEqual(dl.rpc_params.count(
            ("aria2.getFiles", ["gid-1"])), 1)

    def test_aviso_quando_dn_difere_do_nome_real(self):
        """`dn=` é só sugestão de exibição: se o link promete um nome
        que o torrent não entrega, o stream leva o aviso (a UI mostra)."""
        magnet = ("magnet:?xt=urn:btih:" + "ab" * 20
                  + "&dn=Nome+Prometido+no+Link")
        task = Task(url=magnet, dest_dir=self.tmp,
                    filename="torrent-abababab", kind="torrent")
        dl = FakeAria(task, seq=[
            {"status": "active", "completedLength": "50",
             "totalLength": "100", "downloadSpeed": "10",
             "bittorrent": {"info": {"name": "Nome Real do Torrent"}}},
            {"status": "complete", "completedLength": "100",
             "totalLength": "100", "downloadSpeed": "0"},
        ])
        dl._gid = "gid-1"
        dl._watch()
        warn = dl.task.stream.get("warn", "")
        self.assertIn("Nome Prometido no Link", warn)
        self.assertIn("Nome Real do Torrent", warn)

    def test_sem_aviso_quando_dn_igual_ao_nome_real(self):
        magnet = ("magnet:?xt=urn:btih:" + "ab" * 20 + "&dn=Filme+Bonito")
        task = Task(url=magnet, dest_dir=self.tmp,
                    filename="torrent-abababab", kind="torrent")
        dl = FakeAria(task, seq=[
            {"status": "complete", "completedLength": "100",
             "totalLength": "100", "downloadSpeed": "0",
             "bittorrent": {"info": {"name": "Filme Bonito"}}},
        ])
        dl._gid = "gid-1"
        dl._watch()
        self.assertNotIn("warn", dl.task.stream)

    def test_conclui_preservando_lista_e_aviso(self):
        """O `_finish` recriava o stream e jogava fora files/warn —
        agora chegam inteiros ao DONE (a UI mostra os arquivos e o
        aviso mesmo depois de concluído)."""
        magnet = ("magnet:?xt=urn:btih:" + "ab" * 20
                  + "&dn=Nome+Prometido")
        alvo = os.path.join(self.tmp, "Pacote", "video.mp4")
        os.makedirs(os.path.dirname(alvo), exist_ok=True)
        with open(alvo, "wb") as fh:
            fh.write(b"v" * 100)
        task = Task(url=magnet, dest_dir=self.tmp,
                    filename="torrent-abababab", kind="torrent")
        dl = FakeAria(task, seq=[
            {"status": "active", "completedLength": "50",
             "totalLength": "100", "downloadSpeed": "10",
             "bittorrent": {"info": {"name": "Nome Real"}}},
            {"status": "complete", "completedLength": "100",
             "totalLength": "100", "downloadSpeed": "0"},
        ], files=[{"path": alvo}])
        out = dl.run()
        self.assertEqual(out.state, TaskState.DONE)
        self.assertIn("Nome Prometido", out.stream["warn"])
        self.assertIn("Nome Real", out.stream["warn"])
        self.assertEqual(out.stream["files"],
                         [{"path": "Pacote/video.mp4", "bytes": 100}])
        self.assertEqual(out.stream["bytes_done"], 100)

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

    def test_nota_conta_os_segundos_esperados(self):
        """A nota mostra o tempo decorrido — sinal de vida na UI
        ('resolvendo metadados… (60s)' em vez de colar em 0%)."""
        notas = []
        relogio = {"t": 0.0}

        def tick():
            relogio["t"] += 30.0
            return relogio["t"]

        dl = _spawn(FakeAria, self.tmp, seq=[
            {"status": "waiting", "completedLength": "0",
             "totalLength": "0", "downloadSpeed": "0"},
            {"status": "waiting", "completedLength": "0",
             "totalLength": "0", "downloadSpeed": "0"},
            {"status": "complete", "completedLength": "100",
             "totalLength": "100", "downloadSpeed": "0"},
        ], on_progress=lambda t: notas.append(
            (t.stream or {}).get("note")))
        dl._gid = "gid-1"
        with mock.patch("minidm.torrent.time.monotonic", side_effect=tick):
            dl._watch()
        self.assertTrue(any("metadados" in (n or "") for n in notas))
        # 2ª varredura: 60 s desde o 1º poll sem totalLength
        self.assertTrue(any("(60s)" in (n or "") for n in notas), notas)

    def test_metadados_presos_viram_erro_acionavel(self):
        """Watchdog: totalLength nunca chega (sem peers — DHT/UDP
        bloqueado ou torrent sem sementes) → erro com instrução em
        vez de a tarefa congelar para sempre em 'resolvendo…'."""
        relogio = {"t": 0.0}

        def tick():
            relogio["t"] += 60.0
            return relogio["t"]

        dl = _spawn(FakeAria, self.tmp, seq=[
            {"status": "active", "completedLength": "0",
             "totalLength": "0", "downloadSpeed": "0"},
        ])
        with mock.patch("minidm.torrent.time.monotonic", side_effect=tick):
            with self.assertRaises(DownloadError) as ctx:
                dl.run()
        msg = str(ctx.exception)
        self.assertIn("metadados", msg)
        self.assertIn("firewall", msg)              # ação sugerida
        self.assertEqual(dl.task.state, TaskState.ERROR)
        self.assertIn("metadados", dl.task.error)


class TestMetodosRpcReais(unittest.TestCase):
    """Regressão: os métodos RPC existem no aria2 REAL (build winget 1.37.0).

    O boot chamava `system.getVersion`; esse build só publica o prefixo
    `aria2.` (system.* = multicall/listMethods/listNotifications) — o
    "No such method" se repetia40x e o download morria com "aria2 não
    abriu o RPC local em 10 s". A lista abaixo é o `system.listMethods`
    capturado do aria2c 1.37.0 (winget): qualquer `_rpc("…")` do código
    fora dela falha aqui.
    """

    LISTA_REAL = [
        "aria2.addUri", "aria2.addTorrent", "aria2.getPeers",
        "aria2.addMetalink", "aria2.remove", "aria2.pause",
        "aria2.forcePause", "aria2.pauseAll", "aria2.forcePauseAll",
        "aria2.unpause", "aria2.unpauseAll", "aria2.forceRemove",
        "aria2.changePosition", "aria2.tellStatus", "aria2.getUris",
        "aria2.getFiles", "aria2.getServers", "aria2.tellActive",
        "aria2.tellWaiting", "aria2.tellStopped", "aria2.getOption",
        "aria2.changeUri", "aria2.changeOption", "aria2.getGlobalOption",
        "aria2.changeGlobalOption", "aria2.purgeDownloadResult",
        "aria2.removeDownloadResult", "aria2.getVersion",
        "aria2.getSessionInfo", "aria2.shutdown", "aria2.forceShutdown",
        "aria2.getGlobalStat", "aria2.saveSession",
        "system.multicall", "system.listMethods", "system.listNotifications",
    ]

    def test_todo_metodo_usado_no_codigo_existe_no_aria2(self):
        import re

        import minidm.torrent as mod

        with open(mod.__file__, encoding="utf-8") as fh:
            src = fh.read()
        usados = set(re.findall(r'_rpc\(\s*"([^"]+)"', src))
        self.assertTrue(usados, "nenhum método RPC encontrado no torrent.py")
        faltando = sorted(usados - set(self.LISTA_REAL))
        self.assertFalse(
            faltando,
            f"métodos inexistentes no aria2 real: {faltando} "
            "(o build winget 1.37.0 não os publica)")

    def test_boot_e_teardown_usam_os_metodos_certos(self):
        """Os dois pontos que quebravam: boot e desligamento."""
        import minidm.torrent as mod

        with open(mod.__file__, encoding="utf-8") as fh:
            src = fh.read()
        # a chamada (e não o comentário que cita o nome antigo)
        self.assertIn('self._rpc("aria2.getVersion"', src)
        self.assertNotIn('self._rpc("system.getVersion"', src)
        self.assertIn('self._rpc("aria2.shutdown"', src)
        self.assertNotIn('self._rpc("system.shutdown"', src)


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
