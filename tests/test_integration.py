"""Teste de integração da API: /media/analyze + /add (fluxo da extensão).

Sobe um servidor HLS local + o daemon do minidm e exercita os endpoints
exatamente como a extensão do navegador faz.

Roda com:  python -m unittest discover -s tests -p "test_integration.py"
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from minidm.config import AppConfig
from minidm.meta import Task, TaskState
from minidm.scheduler import Scheduler
from minidm.server import App, make_handler
from minidm.store import TaskStore

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=640x360
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=1280x720
high.m3u8
"""

LOW = """#EXTM3U
#EXT-X-TARGETDURATION:4
#EXTINF:4.0,
low0.ts
#EXTINF:4.0,
low1.ts
#EXT-X-ENDLIST
"""

HIGH = """#EXTM3U
#EXT-X-TARGETDURATION:4
#EXTINF:4.0,
high0.ts
#EXTINF:4.0,
high1.ts
#EXT-X-ENDLIST
"""

LOW_SEGS = [b"L" * 1000, b"L" * 1100]
HIGH_SEGS = [b"H" * 2000, b"H" * 2100]

# master com Widevine (SESSION-KEY) — o servidor tem que recusar
MASTER_DRM = """#EXTM3U
#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,URI="data:text/plain;base64,AAAA",KEYFORMAT="com.widevine.alpha"
#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=1280x720
high.m3u8
"""

# página HTML em URL sem extensão (caso youtu.be/ID): o analyze tem que
# marcar `page`, extrair o <title> e o /add tem que recusar sem yt-dlp
PAGINA_HTML = ("<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
               "<title>Vídeo Incrível</title></head>"
               "<body>conteúdo da página</body></html>")


class HlsHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        p = self.path
        if p.endswith("master.m3u8"):
            self._send(MASTER.encode(), "application/vnd.apple.mpegurl")
        elif p.endswith("master-drm.m3u8"):
            self._send(MASTER_DRM.encode(), "application/vnd.apple.mpegurl")
        elif p.endswith("low.m3u8"):
            self._send(LOW.encode(), "application/vnd.apple.mpegurl")
        elif p.endswith("high.m3u8"):
            self._send(HIGH.encode(), "application/vnd.apple.mpegurl")
        elif "low" in p and p.endswith(".ts"):
            i = int(p.rsplit("low", 1)[1].split(".")[0])
            self._send(LOW_SEGS[i], "video/mp2t")
        elif "high" in p and p.endswith(".ts"):
            i = int(p.rsplit("high", 1)[1].split(".")[0])
            self._send(HIGH_SEGS[i], "video/mp2t")
        elif p.endswith("/pagina"):
            # página HTML (o do_HEAD já responde Content-Length: 0,
            # como o YouTube)
            self._send(PAGINA_HTML.encode(), "text/html; charset=utf-8")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


def post_json(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read().decode())


def get_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, json.loads(r.read().decode())


class TestApiIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hlsd = ThreadingHTTPServer(("127.0.0.1", 0), HlsHandler)
        threading.Thread(target=cls.hlsd.serve_forever, daemon=True).start()
        cls.hls_base = f"http://127.0.0.1:{cls.hlsd.server_address[1]}"

        cls.tmp = tempfile.mkdtemp(prefix="minidm-int-")
        cfg = AppConfig(data_dir=cls.tmp, retries=2, backoff_base=0.05,
                        backoff_max=0.1, verify_signatures=False,
                        remote_checksums=False, max_connections=4,
                        download_base=os.path.join(cls.tmp, "Downloads"))
        cls.store = TaskStore(cls.tmp)
        cls.sched = Scheduler(cls.store, cfg)
        cls.sched.start()
        app = App(cls.store, cls.sched, cfg)
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        threading.Thread(target=cls.api.serve_forever, daemon=True).start()
        cls.api_base = f"http://127.0.0.1:{cls.api.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.sched.shutdown(wait=False)
        cls.api.shutdown()
        cls.api.server_close()
        cls.hlsd.shutdown()
        cls.hlsd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_01_status_responde(self):
        status, data = get_json(f"{self.api_base}/status")
        self.assertEqual(status, 200)
        self.assertIn("tasks", data)

    def test_02_media_analyze_master(self):
        status, data = post_json(
            f"{self.api_base}/media/analyze",
            {"url": f"{self.hls_base}/master.m3u8", "referer": "http://p/"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["kind"], "hls-master")
        labels = [q["label"] for q in data["qualities"]]
        self.assertIn("720p (1280x720)", labels)
        self.assertIn("360p (640x360)", labels)

    def test_03_add_stream_e_download(self):
        # adiciona a variante 720 direto, como o popup faria
        status, data = post_json(
            f"{self.api_base}/add",
            {"url": f"{self.hls_base}/high.m3u8", "kind": "hls",
             "referer": "http://p/", "filename": "filme"})
        self.assertEqual(status, 201)
        self.assertTrue(data["ok"])
        self.assertEqual(data["kind"], "hls")

        task_id = data["id"]
        self.assertTrue(self.sched.wait_idle(timeout=60))
        task = self.store.get(task_id)
        self.assertEqual(task.state, TaskState.DONE, task.error)
        # .ts acrescentado + conteúdo = init ausente + 2 segmentos altos
        self.assertTrue(task.filename.endswith(".ts"))
        path = task.final_path
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"".join(HIGH_SEGS))
        self.assertEqual(task.referer, "http://p/")

    def test_04_add_file_comum(self):
        # um arquivo .bin comum continua no fluxo de arquivo
        status, data = post_json(
            f"{self.api_base}/add",
            {"url": f"{self.hls_base}/low0.ts", "start": False})
        self.assertEqual(status, 201)
        task = self.store.get(data["id"])
        self.assertEqual(task.kind, "file")

    def test_05_add_url_invalida_400(self):
        # scheme fora do suportado (http/https/ftp/sftp) -> 400
        for ruim in ("mailto:x@y", "javascript:alert(1)", "x"):
            req = urllib.request.Request(
                f"{self.api_base}/add",
                data=json.dumps({"url": ruim}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(ctx.exception.code, 400, f"url={ruim!r}")

    def test_06_add_auto_escolhe_melhor_qualidade_e_titulo(self):
        # fluxo do popup: kind=auto + page_title -> servidor analisa,
        # escolhe 720p e nomeia pelo título da página
        status, data = post_json(
            f"{self.api_base}/add",
            {"url": f"{self.hls_base}/master.m3u8", "kind": "auto",
             "referer": "http://p/", "page_title": "Aula 03 — Cinemática",
             "start": False})
        self.assertEqual(status, 201, data)
        task = self.store.get(data["id"])
        self.assertEqual(task.kind, "hls")
        self.assertEqual(task.url, f"{self.hls_base}/high.m3u8")
        self.assertEqual(task.filename, "Aula 03 — Cinemática")
        self.assertEqual(task.referer, "http://p/")
        self.assertEqual(task.page_title, "Aula 03 — Cinemática")

    def test_07_add_auto_com_dir_e_nome_escolhidos(self):
        dest = os.path.join(self.tmp, "meus videos")
        status, data = post_json(
            f"{self.api_base}/add",
            {"url": f"{self.hls_base}/master.m3u8", "kind": "auto",
             "quality": "360p (640x360)",
             "filename": "aula-cinema", "dir": dest, "start": False})
        self.assertEqual(status, 201, data)
        task = self.store.get(data["id"])
        self.assertEqual(task.dest_dir, dest)
        self.assertEqual(task.filename, "aula-cinema")
        self.assertEqual(task.url, f"{self.hls_base}/low.m3u8")

    def test_08_add_auto_por_indice_de_qualidade(self):
        status, data = post_json(
            f"{self.api_base}/add",
            {"url": f"{self.hls_base}/master.m3u8", "kind": "auto",
             "quality": 0, "start": False})
        self.assertEqual(status, 201, data)
        task = self.store.get(data["id"])
        # índice 0 = primeira variante (360p no nosso master de teste)
        self.assertEqual(task.url, f"{self.hls_base}/low.m3u8")

    def test_09_add_auto_drm_e_recusado(self):
        req = urllib.request.Request(
            f"{self.api_base}/add",
            data=json.dumps({"url": f"{self.hls_base}/master-drm.m3u8",
                             "kind": "auto"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(ctx.exception.code, 400)
        body = json.loads(ctx.exception.read().decode())
        self.assertEqual(body["drm"], "widevine")
        self.assertIn("DRM", body["error"])

    def test_10_add_pagina_delega_ao_ytdlp(self):
        """Link de PÁGINA (não mídia direta) + yt-dlp -> usa yt-dlp sozinho.

        É o fluxo do botão "Baixar vídeo desta página" da extensão.
        """
        from minidm import ytdlp

        stub = os.path.join(self.tmp, "yt-dlp")
        with open(stub, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
        os.chmod(stub, 0o755)
        old = os.environ.get("MINIDM_YTDLP")
        os.environ["MINIDM_YTDLP"] = stub
        try:
            self.assertTrue(ytdlp.available())
            status, data = post_json(
                f"{self.api_base}/add",
                {"url": "https://www.youtube.com/watch?v=abc123",
                 "filename": "watch"})
            self.assertEqual(status, 201)
            task = self.store.get(data["id"])
            self.assertTrue(task.use_ytdlp_downloader,
                            "link de página deveria delegar ao yt-dlp")
        finally:
            if old is None:
                os.environ.pop("MINIDM_YTDLP", None)
            else:
                os.environ["MINIDM_YTDLP"] = old

    def test_11_add_direto_nao_delega(self):
        """Mídia direta continua na engine própria (não delega ao yt-dlp)."""
        status, data = post_json(
            f"{self.api_base}/add",
            {"url": f"{self.hls_base}/high.m3u8", "kind": "hls",
             "filename": "stream"})
        self.assertEqual(status, 201)
        task = self.store.get(data["id"])
        self.assertFalse(task.use_ytdlp_downloader)

    # -------------------------------------------------- categorias (IDM)
    def test_12_categories_endpoint(self):
        status, data = get_json(f"{self.api_base}/categories")
        self.assertEqual(status, 200)
        names = [c["name"] for c in data["categories"]]
        for expected in ("Vídeos", "Músicas", "Programas", "Documentos",
                         "Imagens", "Comprimidos"):
            self.assertIn(expected, names)
        # Vídeos é a pré-definida
        default = next(c for c in data["categories"] if c["default"])
        self.assertEqual(default["name"], "Vídeos")
        # pasta base = a configurada no teste
        self.assertTrue(data["base"].endswith("Downloads"))

    def test_13_add_com_categoria_vai_pra_pasta_e_perfil(self):
        base = os.path.join(self.tmp, "Downloads")
        status, data = post_json(
            f"{self.api_base}/add",
            {"url": "https://www.youtube.com/watch?v=abc123",
             "category": "Vídeos", "height": 720, "start": False})
        self.assertEqual(status, 201)
        task = self.store.get(data["id"])
        # pasta = base/Vídeos (criada)
        self.assertTrue(task.dest_dir.startswith(base))
        self.assertEqual(os.path.basename(task.dest_dir), "Vídeos")
        self.assertTrue(os.path.isdir(task.dest_dir))
        # perfil da categoria + seletor com o teto de altura
        self.assertEqual(task.profile, "Vídeo")
        args = task.ytdlp_args or []
        sel = args[args.index("-f") + 1]
        self.assertIn("[height<=720]", sel)

    def test_14_add_categoria_musica_usa_perfil_audio(self):
        base = os.path.join(self.tmp, "Downloads")
        status, data = post_json(
            f"{self.api_base}/add",
            {"url": "https://www.youtube.com/watch?v=abc123",
             "category": "Músicas", "start": False})
        self.assertEqual(status, 201)
        task = self.store.get(data["id"])
        self.assertEqual(os.path.basename(task.dest_dir), "Músicas")
        self.assertTrue(task.dest_dir.startswith(base))
        self.assertEqual(task.profile, "Música")

    def test_15_add_categoria_desconhecida_400(self):
        req = urllib.request.Request(
            f"{self.api_base}/add",
            data=json.dumps({"url": "https://example.com/a.mp4",
                             "category": "NaoExiste", "start": False}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)

    # --------------------------------------------- integrações yt-dlp A–F
    def test_16_add_playlist_expande(self):
        """(C) /add com playlist=true vira várias tarefas na fila."""
        from unittest import mock
        fake = [{"url": "https://youtu.be/p1", "title": "Um", "duration": 1},
                {"url": "https://youtu.be/p2", "title": "Dois", "duration": 2}]
        with mock.patch("minidm.server.expand_playlist", return_value=fake,
                        create=True):
            # o import é local no _add_playlist; mocka o símbolo no módulo
            with mock.patch("minidm.ytdlp.expand_playlist", return_value=fake):
                status, data = post_json(
                    f"{self.api_base}/add",
                    {"url": "https://youtube.com/playlist?list=PLx",
                     "playlist": True, "start": False})
        self.assertEqual(status, 201)
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(data["ids"]), 2)
        t1 = self.store.get(data["ids"][0])
        self.assertEqual(t1.url, "https://youtu.be/p1")

    def test_17_add_section_e_marcada(self):
        """(D) /add com section grava o campo na task e delega ao yt-dlp."""
        status, data = post_json(
            f"{self.api_base}/add",
            {"url": "https://youtu.be/abc", "section": "01:00-02:00",
             "ytdlp": True, "start": False})
        self.assertEqual(status, 201)
        task = self.store.get(data["id"])
        self.assertEqual(task.section, "01:00-02:00")
        self.assertTrue(task.use_ytdlp_downloader)

    def test_18_add_sponsorblock_vira_arg(self):
        """(F) /add com sponsorblock vira extra arg do yt-dlp."""
        status, data = post_json(
            f"{self.api_base}/add",
            {"url": "https://youtu.be/abc", "sponsorblock": "all",
             "ytdlp": True, "start": False})
        self.assertEqual(status, 201)
        task = self.store.get(data["id"])
        self.assertIn("--sponsorblock-remove", task.ytdlp_args)
        self.assertIn("all", task.ytdlp_args)

    def test_19_analyze_pagina_extrai_titulo(self):
        """URL sem extensão servindo HTML: analyze marca `page` e pega o
        `<title>` — em vez de devolver o segmento cru do URL como nome
        (o bug do "5dWeeUIZFgA" sem título no celular)."""
        status, data = post_json(
            f"{self.api_base}/media/analyze",
            {"url": f"{self.hls_base}/pagina"})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertTrue(data.get("page"))
        self.assertEqual(data.get("page_title"), "Vídeo Incrível")
        self.assertEqual(data.get("filename"), "Vídeo Incrível")

    def test_20_add_pagina_sem_ytdlp_recusa_com_erro(self):
        """Página + sem yt-dlp -> 400 explicando (nada de arquivo vazio).

        Caso real do celular: youtu.be/ID analisado como "arquivo direto"
        baixava 0 bytes e concluía com sucesso; agora o /add recusa.
        """
        with mock.patch("minidm.ytdlp.available", return_value=False):
            req = urllib.request.Request(
                f"{self.api_base}/add",
                data=json.dumps({"url": f"{self.hls_base}/pagina",
                                 "analyze": True}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=30)
        self.assertEqual(ctx.exception.code, 400)
        erro = json.loads(ctx.exception.read().decode())
        self.assertFalse(erro.get("ok"))
        self.assertIn("yt-dlp", erro.get("error", ""))
        # nenhuma tarefa foi criada
        self.assertFalse([t for t in self.store.all()
                          if "pagina" in t.url])


class TestNomeEmParalelo(unittest.TestCase):
    """(N) a tarefa entra na fila com o nome do URL e o título real
    chega em segundo plano (`server._resolve_name_bg`).

    É o fix do "entra na fila mas com nome de URL, demora a virar o
    nome real": o /add NÃO espera a extração — um thread separado
    resolve o título enquanto a tarefa espera, e o download em si não
    extraí duas vezes (se já começou, o `--print before_dl` do
    ytdlpdl resolve).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-nome-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = AppConfig(data_dir=self.tmp)
        self.store = TaskStore(self.tmp)
        # SEM start(): sem workers — a tarefa fica QUEUED de propósito
        self.sched = Scheduler(self.store, self.cfg)
        self.app = App(self.store, self.sched, self.cfg)

    def _queued(self, filename="watch"):
        t = Task(url="https://www.youtube.com/watch?v=abc123",
                 dest_dir=self.tmp, filename=filename,
                 state=TaskState.QUEUED, use_ytdlp_downloader=True)
        self.store.add(t)
        return t

    @mock.patch("minidm.ytdlp.describe")
    def test_titulo_real_substitui_o_nome_do_url(self, desc):
        from minidm.server import _resolve_name_bg

        desc.return_value = {"title": "Aula de Física", "id": "abc123"}
        t = self._queued()
        _resolve_name_bg(self.app, t.id, t.url,
                         provisional="watch", settle=0)
        self.assertEqual(t.filename, "Aula de Física [abc123]")
        # persistido: uma instância nova do store lê o nome novo
        again = TaskStore(self.tmp)
        self.assertEqual(again.get(t.id).filename,
                         "Aula de Física [abc123]")

    @mock.patch("minidm.ytdlp.describe")
    def test_nao_duplica_extra_quando_ja_esta_baixando(self, desc):
        """Se o scheduler reivindicou a tarefa (DOWNLOADING), sai SEM
        chamar o describe — o `--print before_dl` do download resolve."""
        from minidm.server import _resolve_name_bg

        t = self._queued()
        t.state = TaskState.DOWNLOADING
        _resolve_name_bg(self.app, t.id, t.url,
                         provisional="watch", settle=0)
        desc.assert_not_called()
        self.assertEqual(t.filename, "watch")

    @mock.patch("minidm.ytdlp.describe")
    def test_nao_mexe_se_o_nome_ja_mudou(self, desc):
        """Análise prévia ou before_dl já nomearam -> resultado descartado."""
        from minidm.server import _resolve_name_bg

        desc.return_value = {"title": "X", "id": "1"}
        t = self._queued()
        t.filename = "Nome Novo"
        _resolve_name_bg(self.app, t.id, t.url,
                         provisional="watch", settle=0)
        desc.assert_not_called()
        self.assertEqual(t.filename, "Nome Novo")

    @mock.patch("minidm.ytdlp.describe")
    def test_nao_aplica_se_pausou_no_meio(self, desc):
        """Pausada/removida durante a extração -> não mexe no nome."""
        from minidm.server import _resolve_name_bg

        def pausa(*_a, **_k):
            t.state = TaskState.PAUSED
            return {"title": "Ignorado", "id": "1"}

        desc.side_effect = pausa
        t = self._queued()
        _resolve_name_bg(self.app, t.id, t.url,
                         provisional="watch", settle=0)
        self.assertEqual(t.filename, "watch")

    @mock.patch("minidm.ytdlp.describe")
    def test_falha_na_extracao_e_silenciosa(self, desc):
        """Sem yt-dlp/site fora: exceção some — o before_dl/_finalize
        continua sendo o fallback do nome."""
        from minidm.server import _resolve_name_bg

        desc.side_effect = RuntimeError("yt-dlp não encontrado")
        t = self._queued()
        _resolve_name_bg(self.app, t.id, t.url,
                         provisional="watch", settle=0)
        self.assertEqual(t.filename, "watch")


class TestOpenEndpoint(unittest.TestCase):
    """POST /open (abrir pasta/arquivo no explorador) + GET /task
    (registro completo p/ o diálogo de propriedades da webui).

    `_open_path` é mockado: a API não precisa de desktop/gráficos —
    só o CAMINHO escolhido (destino publicado no Android, senão o da
    tarefa) é o que se valida aqui.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-open-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = AppConfig(data_dir=self.tmp)
        self.store = TaskStore(self.tmp)
        self.sched = Scheduler(self.store, self.cfg)   # SEM start()
        self.app = App(self.store, self.sched, self.cfg)
        self.api = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        # uma cleanup só: shutdown ANTES de server_close (addCleanup é
        # LIFO — em duas, o close rodaria primeiro e o select do
        # serve_forever cairia em socket fechado: WinError 10038)
        self.addCleanup(self._stop_api)
        self.base = f"http://127.0.0.1:{self.api.server_address[1]}"

    def _stop_api(self):
        self.api.shutdown()
        self.api.server_close()

    def _task(self, **kw):
        dest = os.path.join(self.tmp, "Downloads")
        os.makedirs(dest, exist_ok=True)
        base = dict(url="https://exemplo.com/a.mp4", dest_dir=dest,
                    filename="a.mp4", state=TaskState.DONE)
        base.update(kw)
        t = Task(**base)
        self.store.add(t)
        return t

    def _post(self, path, payload):
        req = urllib.request.Request(
            f"{self.base}{path}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    @mock.patch("minidm.server._open_path")
    def test_open_folder_abre_a_pasta_da_tarefa(self, open_path):
        t = self._task()
        status, data = self._post("/open", {"id": t.id, "what": "folder"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        open_path.assert_called_once_with(t.dest_dir)

    @mock.patch("minidm.server._open_path")
    def test_open_file_usa_o_destino_publicado(self, open_path):
        # Android: o arquivo foi movido p/ Download/ — o publicado
        # (não o dest_dir interno) é o que abre
        pub = os.path.join(self.tmp, "publico", "a.mp4")
        os.makedirs(os.path.dirname(pub), exist_ok=True)
        with open(pub, "wb") as f:
            f.write(b"x")
        t = self._task(published_path=pub)
        status, _ = self._post("/open", {"id": t.id, "what": "file"})
        self.assertEqual(status, 200)
        open_path.assert_called_once_with(pub)

    @mock.patch("minidm.server._open_path")
    def test_open_folder_publicada_abre_a_pasta_do_arquivo(self, open_path):
        pub = os.path.join(self.tmp, "publico", "a.mp4")
        os.makedirs(os.path.dirname(pub), exist_ok=True)
        t = self._task(published_path=pub)
        status, _ = self._post("/open", {"id": t.id, "what": "folder"})
        self.assertEqual(status, 200)
        open_path.assert_called_once_with(os.path.dirname(pub))

    @mock.patch("minidm.server._open_path")
    def test_open_file_sem_arquivo_404(self, open_path):
        # tarefa concluída mas o arquivo não está em disco: erro claro
        # em vez de explorador abrindo em vazio
        t = self._task()
        status, data = self._post("/open", {"id": t.id, "what": "file"})
        self.assertEqual(status, 404)
        self.assertIn("arquivo", data["error"])
        open_path.assert_not_called()

    @mock.patch("minidm.server._open_path")
    def test_open_what_invalido_400(self, open_path):
        t = self._task()
        status, _ = self._post("/open", {"id": t.id, "what": "xyz"})
        self.assertEqual(status, 400)
        open_path.assert_not_called()

    @mock.patch("minidm.server._open_path")
    def test_open_id_desconhecido_404(self, open_path):
        status, _ = self._post("/open", {"id": "nope", "what": "folder"})
        self.assertEqual(status, 404)
        open_path.assert_not_called()

    def test_task_devolve_o_registro_completo(self):
        t = self._task(referer="https://p/", section="*0:10",
                       category="Vídeos", created_at=1700000000.0)
        with urllib.request.urlopen(f"{self.base}/task?id={t.id}",
                                    timeout=5) as r:
            data = json.loads(r.read())
        # campos que o snapshot do /status NÃO expõe (motivo do /task)
        self.assertEqual(data["dest_dir"], t.dest_dir)
        self.assertEqual(data["referer"], "https://p/")
        self.assertEqual(data["section"], "*0:10")
        self.assertEqual(data["created_at"], 1700000000.0)
        with urllib.request.urlopen(f"{self.base}/status",
                                    timeout=5) as r:
            snap = json.loads(r.read())["tasks"][0]
        self.assertNotIn("dest_dir", snap)

    def test_task_id_desconhecido_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(f"{self.base}/task?id=nope", timeout=5)
        self.assertEqual(cm.exception.code, 404)


class TestConfigEndpoint(unittest.TestCase):
    """POST /config (tela de configurações da webui) + GET estendido.

    Valida ATÔMICA (requisição inteira rejeitada, nada aplicado em
    meio-termo), persistência em config.json e `apply_config` ao vivo
    (conorrência muda sem reiniciar a API).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-cfg-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = AppConfig(data_dir=self.tmp)
        self.store = TaskStore(self.tmp)
        self.sched = Scheduler(self.store, self.cfg)   # SEM start()
        self.app = App(self.store, self.sched, self.cfg)
        self.api = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.addCleanup(self._stop_api)
        self.base = f"http://127.0.0.1:{self.api.server_address[1]}"

    def _stop_api(self):
        self.api.shutdown()
        self.api.server_close()

    def _post(self, payload):
        req = urllib.request.Request(
            f"{self.base}/config", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _get(self):
        with urllib.request.urlopen(f"{self.base}/config",
                                    timeout=5) as r:
            return json.loads(r.read())

    def test_get_expoe_os_campos_editaveis(self):
        d = self._get()
        # proxy É string (a tela edita o valor real, não um bool)
        self.assertEqual(d["proxy"], "")
        self.assertIn("download_base", d)
        self.assertIn("max_concurrent", d)
        self.assertIn("cookies_from_browser", d)
        self.assertIn("sponsorblock", d)
        self.assertIn("android_public_downloads", d)
        self.assertEqual(d["data_dir"], self.tmp)   # só leitura

    def test_salva_aplica_e_persiste(self):
        status, data = self._post({"max_concurrent": 5,
                                   "throttle_bps": 2048,
                                   "proxy": "http://127.0.0.1:8080"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(self.cfg.max_concurrent, 5)
        self.assertEqual(self.cfg.throttle_bps, 2048)
        self.assertEqual(self.cfg.proxy, "http://127.0.0.1:8080")
        # persiste em config.json (sobrevive a reiniciar)
        again = AppConfig.load(data_dir=self.tmp)
        self.assertEqual(again.max_concurrent, 5)
        self.assertEqual(again.proxy, "http://127.0.0.1:8080")

    def test_rejeita_campo_nao_editavel_sem_aplicar_nada(self):
        status, data = self._post({"data_dir": "/tmp/outro",
                                   "max_concurrent": 7})
        self.assertEqual(status, 400)
        self.assertIn("data_dir", data["error"])
        # atômico: o campo válido NÃO foi aplicado em meio-termo
        self.assertEqual(self.cfg.max_concurrent, 3)

    def test_rejeita_fora_do_limite(self):
        status, data = self._post({"max_concurrent": 0})
        self.assertEqual(status, 400)
        self.assertIn("max_concurrent", data["error"])
        status, _ = self._post({"max_connections": 9999})
        self.assertEqual(status, 400)
        self.assertEqual(self.cfg.max_concurrent, 3)   # nada mudou

    def test_rejeita_tipo_errado(self):
        status, _ = self._post({"segments": "abc"})
        self.assertEqual(status, 400)
        # bool não é número (JSON true chega como bool)
        status, _ = self._post({"retries": True})
        self.assertEqual(status, 400)
        self.assertEqual(self.cfg.segments, 0)
        status, _ = self._post({"nao_existe": 1})
        self.assertEqual(status, 400)

    def test_bool_e_enum(self):
        status, _ = self._post({"download_archive": True,
                                "cookies_from_browser": "firefox",
                                "sponsorblock": "all"})
        self.assertEqual(status, 200)
        self.assertTrue(self.cfg.download_archive)
        self.assertEqual(self.cfg.cookies_from_browser, "firefox")
        self.assertEqual(self.cfg.sponsorblock, "all")
        # string booleana também vale ("true" do select do form)
        status, _ = self._post({"verify_signatures": "false"})
        self.assertEqual(status, 200)
        self.assertFalse(self.cfg.verify_signatures)
        # enum fora da lista
        status, data = self._post({"cookies_from_browser": "ie6"})
        self.assertEqual(status, 400)
        self.assertIn("cookies_from_browser", data["error"])

    def test_payload_vazio_400(self):
        status, _ = self._post({})
        self.assertEqual(status, 400)

    def test_apply_config_cresce_o_pool_ao_vivo(self):
        # pool já rodando com 1 worker: subir a concorrência nasce mais
        # workers na hora (sem reiniciar a API)
        self.cfg.max_concurrent = 1          # começa com 1 (default é 3)
        self.sched.start()
        self.assertEqual(len(self.sched._workers), 1)
        status, _ = self._post({"max_concurrent": 3})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.sched._workers), 3)
        # semáforo/bucket novos valem p/ as próximas tarefas
        self.assertIsNotNone(self.sched.sem)
        self.addCleanup(self.sched.shutdown)


if __name__ == "__main__":
    unittest.main()
