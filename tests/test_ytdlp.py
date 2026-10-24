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
from minidm.media import analyze

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


if __name__ == "__main__":
    unittest.main()
