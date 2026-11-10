"""Testes do webui servido pela API (`GET /` + `GET /webui/*`).

Sobe o mesmo servidor da extensão (make_handler) e confere que a UI web
(HTML/CSS/JS do WebView do APK e do navegador) é servida corretamente,
com o tipo de MIME certo e trava contra path traversal.

Rodar com:  python -m unittest discover -s tests -p "test_webui.py"
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from minidm.config import AppConfig
from minidm.scheduler import Scheduler
from minidm.server import App, make_handler, webui_dir
from minidm.store import TaskStore


class WebuiServeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="minidm-webui-")
        cfg = AppConfig(data_dir=cls.tmp)
        store = TaskStore(cls.tmp)
        cls.api = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(App(store, Scheduler(store, cfg), cfg)))
        cls.base = f"http://127.0.0.1:{cls.api.server_address[1]}"
        threading.Thread(target=cls.api.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        cls.api.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path: str):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()

    # ------------------------------------------------------------------ UI
    def test_index_served(self):
        status, ctype, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b'id="list"', body)          # âncora da lista de tarefas
        self.assertIn(b"/webui/app.js", body)

    def test_index_alias(self):
        status, ctype, _ = self._get("/index.html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)

    def test_seletor_de_qualidade_presente(self):
        """A caixa de qualidades (pós-análise, estilo IDM) existe no
        servido: diálogo no HTML, fluxo no JS e estilo no CSS."""
        _, _, html = self._get("/")
        self.assertIn(b'id="pick"', html)
        self.assertIn(b'id="pick-list"', html)
        _, _, js = self._get("/webui/app.js")
        self.assertIn(b"function pickQuality", js)
        self.assertIn(b"/media/analyze", js)
        self.assertIn(b"quality_url", js)     # escolha vai direto no /add
        _, _, css = self._get("/webui/style.css")
        self.assertIn(b".sheet-back", css)

    def test_static_assets(self):
        for path, want in (("/webui/style.css", "text/css"),
                           ("/webui/app.js", "javascript")):
            status, ctype, body = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(want, ctype, path)
            self.assertTrue(body, path)

    def test_unknown_asset_404(self):
        status, ctype, _ = self._get("/webui/nao-existe.js")
        self.assertEqual(status, 404)
        self.assertIn("application/json", ctype)

    def test_traversal_blocked(self):
        # `..` cru e percent-encoded não pode escapar do webui/
        for path in ("/webui/../minidm/server.py",
                     "/webui/../../minidm/config.py",
                     "/webui/..%2f..%2fminidm%2fconfig.py"):
            status, _, body = self._get(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"make_handler", body)   # fonte não vaza

    def test_webui_dir_exists(self):
        import os
        self.assertTrue(os.path.isdir(webui_dir()), webui_dir())

    # ------------------------------------------------------------ regressão
    def test_status_still_json(self):
        status, ctype, body = self._get("/status")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        data = json.loads(body)
        self.assertIn("tasks", data)
        self.assertIn("queue", data)

    def test_api_routes_unaffected(self):
        # rotas da extensão continuam respondendo (o webui não as engoliu)
        status, _, _ = self._get("/config")
        self.assertEqual(status, 200)

    def test_campo_de_destino(self):
        """O campo de pasta (destino padrão no placeholder, escolha do
        usuário no payload.dir) existe e o /config expõe o data_dir p/ o
        placeholder quando não há categoria."""
        _, _, html = self._get("/")
        self.assertIn(b'id="dest"', html)
        _, _, js = self._get("/webui/app.js")
        self.assertIn(b"payload.dir", js)
        status, _, body = self._get("/config")
        self.assertEqual(status, 200)
        self.assertIn(b"data_dir", body)

    def test_busca_e_filtro_de_estado(self):
        """Busca + chips de estado (histórico estilo YTDLnis): input de
        busca, abas no HTML, lógica de filtro no JS e estilo."""
        _, _, html = self._get("/")
        self.assertIn(b'id="q"', html)
        self.assertIn(b'data-filter="done"', html)
        self.assertIn(b'data-filter="error"', html)
        _, _, js = self._get("/webui/app.js")
        self.assertIn(b"function matchesFilter", js)
        self.assertIn(b"filterState", js)
        _, _, css = self._get("/webui/style.css")
        self.assertIn(b".filters", css)
        self.assertIn(b".tab.on", css)


if __name__ == "__main__":
    unittest.main()
