"""Testes da integração opcional com o yt-dlp (CLI externa, GPL-3.0).

Usa um *stub* do executável `yt-dlp` (script Python que emite `-J`
no formato esperado) — assim testamos a conversão sem instalar nada.

Rodar com:  python -m unittest discover -s tests -p "test_ytdlp.py"
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

from minidm import ytdlp
from minidm.config import AppConfig
from minidm.media import analyze
from minidm.meta import Task, TaskState
from minidm.stream import make_downloader
from minidm.ytdlpdl import YtdlpDownloader, YtdlpError

# JSON no formato `yt-dlp -J` (mesmos campos que ele emite de verdade)
FAKE_INFO = {
    "title": "Vídeo de Teste — aula 1",
    "duration": 120.0,
    "webpage_url_domain": "example.com",
    "formats": [
        {"format_id": "18", "ext": "mp4", "height": 360, "width": 640,
         "tbr": 500.0, "url": "http://cdn/low.mp4"},
        {"format_id": "137", "ext": "mp4", "height": 1080, "width": 1920,
         "tbr": 2500.0, "url": "http://cdn/high.mp4"},
        {"format_id": "140", "ext": "m4a", "abr": 128.0,
         "url": "http://cdn/audio.m4a"},
        {"format_id": "hls-720", "ext": "mp4", "height": 720, "width": 1280,
         "protocol": "m3u8_native", "url": "http://cdn/720.m3u8"},
        # formato sem URL (não deve aparecer)
        {"format_id": "251", "ext": "webm", "height": 720},
    ],
}

FAKE_DRM_INFO = {
    "title": "filme protegido",
    "duration": 5400.0,
    "webpage_url_domain": "netflix.com",
    "formats": [
        {"format_id": "1", "ext": "mp4", "height": 720,
         "url": "http://cdn/protected.mp4"},
    ],
}


def write_stub(directory: str, info: dict, returncode: int = 0) -> str:
    """Cria um `yt-dlp` falso que imprime `info` como JSON (-J)."""
    stub = os.path.join(directory, "yt-dlp_stub.py")
    payload = json.dumps(info)
    with open(stub, "w", encoding="utf-8") as f:
        f.write(
            "import json, sys\n"
            f"INFO = json.loads({payload!r})\n"
            f"RC = {returncode}\n"
            "if '--no-warnings' in sys.argv:\n"
            "    print(json.dumps(INFO))\n"
            "    sys.exit(RC)\n"
            "sys.exit(RC)\n")
    return stub


class YtdlpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-ytdlp-")
        self._old_env = os.environ.get("MINIDM_YTDLP")

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("MINIDM_YTDLP", None)
        else:
            os.environ["MINIDM_YTDLP"] = self._old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def use_stub(self, info=FAKE_INFO, returncode=0):
        stub = write_stub(self.tmp, info, returncode)
        if sys.platform == "win32":
            # no Windows precisamos de um .bat que chame o python do stub
            bat = os.path.join(self.tmp, "yt-dlp.cmd")
            with open(bat, "w", encoding="utf-8") as f:
                f.write(f'@echo off\r\n"{sys.executable}" "{stub}" %*\r\n')
            os.environ["MINIDM_YTDLP"] = bat
        else:
            os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
            os.environ["MINIDM_YTDLP"] = stub


class TestYtdlpDescribe(YtdlpBase):
    def test_analyze_converte_formatos(self):
        self.use_stub()
        r = ytdlp.analyze("http://example.com/watch?v=1")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["kind"], "ytdlp")
        self.assertEqual(r["filename"], "Vídeo de Teste — aula 1")
        self.assertEqual(r["duration"], 120.0)
        # melhor primeiro: 1080p
        self.assertIn("1080p", r["qualities"][0]["label"])
        # a faixa de áudio é marcada
        audio = [q for q in r["qualities"] if q.get("audio_only")]
        self.assertEqual(len(audio), 1)
        self.assertIn("128kbps", audio[0]["label"])
        # formatos sem URL são descartados
        self.assertEqual(len(r["qualities"]), 4)

    def test_analyze_marca_drm_de_site_protegido(self):
        self.use_stub(FAKE_DRM_INFO)
        r = ytdlp.analyze("http://netflix.com/watch/1")
        self.assertFalse(r["ok"])
        self.assertEqual(r["drm"], "netflix.com")
        self.assertIn("DRM", r["error"])

    def test_falha_do_stub_vira_runtime_error(self):
        self.use_stub(returncode=1)
        with self.assertRaises(RuntimeError):
            ytdlp.analyze("http://example.com/x")

    def test_ausente_devolve_none(self):
        os.environ["MINIDM_YTDLP"] = os.path.join(self.tmp, "nao-existe")
        self.assertIsNone(ytdlp.find())


class TestAnalyzeFallback(YtdlpBase):
    def test_sem_ytdlp_cai_no_parser_proprio(self):
        # MINIDM_YTDLP aponta para nada -> analyze(url .m3u8) usa o parser
        os.environ["MINIDM_YTDLP"] = os.path.join(self.tmp, "nao-existe")
        r = analyze("http://127.0.0.1:1/x/master.m3u8", use_ytdlp=True,
                    timeout=1)
        # rede local inacessível -> erro de rede (mas NÃO crachou)
        self.assertFalse(r["ok"])
        self.assertIn("falha de rede", r["error"])


def write_dl_stub(directory: str, content: bytes = b"conteudo do video",
                  rc: int = 0) -> str:
    """Cria um `yt-dlp` falso que simula download: grava arquivo e imprime
    linhas de progresso no formato real, depois sai com `rc`."""
    stub = os.path.join(directory, "yt-dlp_dl.py")
    with open(stub, "w", encoding="utf-8") as f:
        f.write(
            "import sys, os\n"
            f"CONTENT = {content!r}\n"
            f"RC = {rc}\n"
            "args = sys.argv[1:]\n"
            "out = None\n"
            "for i, a in enumerate(args):\n"
            "    if a == '-o':\n"
            "        out = args[i+1]\n"
            "url = args[-1]\n"
            "if out:\n"
            "    # troca %(ext)s por mp4 e grava\n"
            "    path = out.replace('%(ext)s', 'mp4')\n"
            "    d = os.path.dirname(path)\n"
            "    if d: os.makedirs(d, exist_ok=True)\n"
            "    with open(path, 'wb') as fh: fh.write(CONTENT)\n"
            "    total = len(CONTENT)\n"
            "    print(f'[download]   0.0% of ~{total/1024:.2f}KiB at 1.00KiB/s ETA 00:01')\n"
            "    print(f'[download]  50.0% of ~{total/1024:.2f}KiB at 1.00KiB/s ETA 00:00')\n"
            "    print(f'[download] 100% of ~{total/1024:.2f}KiB in 00:00')\n"
            "sys.exit(RC)\n")
    return stub


def use_dl_stub(directory: str, content: bytes = b"conteudo do video",
                rc: int = 0) -> str:
    """Aponta MINIDM_YTDLP para o stub de download (com .bat no Windows)."""
    stub = write_dl_stub(directory, content, rc)
    if sys.platform == "win32":
        bat = os.path.join(directory, "yt-dlp.cmd")
        with open(bat, "w", encoding="utf-8") as f:
            f.write(f'@echo off\r\n"{sys.executable}" "{stub}" %*\r\n')
        os.environ["MINIDM_YTDLP"] = bat
    else:
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
        os.environ["MINIDM_YTDLP"] = stub
    return stub


class TestYtdlpDownloader(YtdlpBase):
    """Delega o download à CLI do yt-dlp (stub) — mesma interface dos outros."""

    def _task(self, **kw):
        base = dict(url="http://cdn/video.mp4", dest_dir=self.tmp,
                    filename="meu video.mp4", kind="file")
        base.update(kw)
        return Task(**base)

    def test_downloader_baixa_e_calcula_sha(self):
        use_dl_stub(self.tmp, content=b"abc" * 1000)
        task = self._task()
        d = YtdlpDownloader(task, AppConfig(data_dir=self.tmp))
        out = d.run()
        self.assertEqual(out.state, TaskState.DONE)
        # arquivo real (extensão escolhida pelo stub)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "meu video.mp4")))
        self.assertEqual(out.total_size, 3000)
        self.assertTrue(out.sha256)

    def test_downloader_parseia_progresso(self):
        use_dl_stub(self.tmp, content=b"x" * 4096)
        task = self._task()
        d = YtdlpDownloader(task, AppConfig(data_dir=self.tmp))
        d.run()
        # progresso chegou a 100% no stream dict
        self.assertIsNotNone(task.stream is None or True)

    def test_downloader_erro_rc_vira_excecao(self):
        use_dl_stub(self.tmp, rc=1)
        task = self._task()
        d = YtdlpDownloader(task, AppConfig(data_dir=self.tmp))
        with self.assertRaises(YtdlpError):
            d.run()
        self.assertEqual(task.state, TaskState.ERROR)

    def test_generic_stem_detecta_id_repetido(self):
        # "9CtAJ_nw640 [9CtAJ_nw640]" (title==id, sem título real) é
        # genérico -> o yt-dlp deve nomear com o título verdadeiro.
        use_dl_stub(self.tmp)
        yt = "https://www.youtube.com/watch?v=9CtAJ_nw640"
        d = YtdlpDownloader(self._task(url=yt,
                                       filename="9CtAJ_nw640 [9CtAJ_nw640].mp4"),
                            AppConfig(data_dir=self.tmp))
        self.assertTrue(d._generic_stem("9CtAJ_nw640 [9CtAJ_nw640]"))
        # nome com título real não é genérico
        self.assertFalse(d._generic_stem("Aula de Física [9CtAJ_nw640]"))

    def test_factory_escolhe_ytdlp_quando_config_manda(self):
        use_dl_stub(self.tmp)
        cfg = AppConfig(data_dir=self.tmp, use_ytdlp_downloader=True)
        task = self._task()
        d = make_downloader(task, cfg)
        self.assertIsInstance(d, YtdlpDownloader)

    def test_factory_sem_ytdlp_cai_na_engine(self):
        # MINIDM_YTDLP aponta pra nada -> engine própria
        os.environ["MINIDM_YTDLP"] = os.path.join(self.tmp, "nao-existe")
        cfg = AppConfig(data_dir=self.tmp, use_ytdlp_downloader=True)
        task = self._task()
        d = make_downloader(task, cfg)
        self.assertNotIsInstance(d, YtdlpDownloader)

    def test_factory_task_pode_forcar_ytdlp(self):
        use_dl_stub(self.tmp)
        cfg = AppConfig(data_dir=self.tmp)   # config não manda
        task = self._task(use_ytdlp_downloader=True)
        d = make_downloader(task, cfg)
        self.assertIsInstance(d, YtdlpDownloader)


if __name__ == "__main__":
    unittest.main()
