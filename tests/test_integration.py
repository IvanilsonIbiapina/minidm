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
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
        req = urllib.request.Request(
            f"{self.api_base}/add",
            data=json.dumps({"url": "ftp://x"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)

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


if __name__ == "__main__":
    unittest.main()
