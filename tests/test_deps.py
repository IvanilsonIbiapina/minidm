# -*- coding: utf-8 -*-
"""Testes da integração "de fábrica": deps, ffmpeg, link direto × página.

Rodar:  python -m unittest discover -s tests -p "test_deps.py"
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from minidm.deps import (ffmpeg_dir, ffmpeg_available, js_runtime, status,
                         invalidate_cache)
from minidm.meta import Task
from minidm.remote import auto_delegate, looks_direct_media
from minidm.ytdlpdl import _filter_ffmpeg_args, _hidden_kwargs, _explain_error


class TestExplainError(unittest.TestCase):
    """Mensagens de erro do yt-dlp viram texto claro em português."""

    def test_unavailable_traduzido(self):
        tail = ["WARNING: [youtube] algo", "ERROR: [youtube] abc: This video is unavailable"]
        got = _explain_error(tail, 1)
        self.assertIn("indisponível", got)
        self.assertIn("código 1", got)

    def test_private_video(self):
        tail = ["ERROR: [youtube] abc: Private video"]
        self.assertIn("privado", _explain_error(tail, 1))

    def test_ffmpeg_sugerido(self):
        tail = ["ERROR: Postprocessing: ffprobe and ffmpeg not found"]
        self.assertIn("ffmpeg", _explain_error(tail, 1))

    def test_sem_error_usa_ultimas_linhas(self):
        tail = ["   50% of 10MB", "algo quebrou"]
        got = _explain_error(tail, 1)
        self.assertIn("código 1", got)

    def test_vaio_vira_desconhecido(self):
        got = _explain_error([], 1)
        self.assertIn("desconhecido", got)


class TestJsRuntime(unittest.TestCase):
    """Runtime JS (node/deno/bun) p/ extração completa do YouTube."""

    def setUp(self):
        self._old = os.environ.get("MINIDM_JS_RUNTIME")
        invalidate_cache()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MINIDM_JS_RUNTIME", None)
        else:
            os.environ["MINIDM_JS_RUNTIME"] = self._old
        invalidate_cache()

    def test_devolve_none_ou_nome(self):
        rt = js_runtime()
        self.assertTrue(rt is None or rt in ("node", "deno", "bun"))

    def test_env_invalido_vira_none(self):
        os.environ["MINIDM_JS_RUNTIME"] = "runtime-inexistente"
        invalidate_cache()
        self.assertIsNone(js_runtime())

    def test_status_inclui_js_runtime(self):
        st = status()
        self.assertIn("js_runtime", st)
        self.assertIn("available", st["js_runtime"])
        self.assertIn("extração completa do YouTube (runtime JS)",
                      st["features"])


class TestHiddenWindow(unittest.TestCase):
    """O yt-dlp roda em background, sem abrir janela de CMD no Windows."""

    def test_windows_sem_janela(self):
        if os.name != "nt":
            self.skipTest("só Windows")
        kw = _hidden_kwargs()
        self.assertIn("creationflags", kw)
        CREATE_NO_WINDOW = 0x08000000
        self.assertTrue(kw["creationflags"] & CREATE_NO_WINDOW)

    def test_nao_windows_vazio(self):
        if os.name == "nt":
            self.skipTest("fora do Windows")
        self.assertEqual(_hidden_kwargs(), {})


class TestLooksDirectMedia(unittest.TestCase):
    """`looks_direct_media` decide: engine própria (True) vs yt-dlp (False)."""

    def test_diretos_sao_true(self):
        casos = [
            "https://cdn.exemplo.com/filme.mp4",
            "https://cdn.exemplo.com/filme.webm?token=abc",
            "https://exemplo.com/live/master.m3u8",
            "https://exemplo.com/video.mpd",
            "https://exemplo.com/music.mp3",
            "https://exemplo.com/pacote.zip",
            "https://exemplo.com/app.exe",
        ]
        for u in casos:
            self.assertTrue(looks_direct_media(u), u)

    def test_paginas_sao_false(self):
        casos = [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.instagram.com/reel/ABC123/",
            "https://x.com/user/status/12345",
            "https://exemplo.com/video/123",      # sem extensão
            "https://exemplo.com/",                # raiz
            "https://exemplo.com",                 # sem path
            "https://exemplo.com/get.php?id=99",   # .php não é mídia
        ]
        for u in casos:
            self.assertFalse(looks_direct_media(u), u)

    def test_case_insensitive_e_query(self):
        self.assertTrue(looks_direct_media("https://x/A.MP4?v=1"))
        self.assertFalse(looks_direct_media("https://youtube.com/watch?x=.mp4"))


class TestFilterFfmpegArgs(unittest.TestCase):
    """Sem ffmpeg, remove flags que exigiriam pós-processamento."""

    FF = "/usr/bin/ffmpeg"   # valor truthy p/ "ffmpeg presente"

    def test_com_ffmpeg_nao_remove_nada(self):
        args = ["-f", "bestaudio", "-x", "--audio-format", "mp3",
                "--embed-thumbnail"]
        self.assertEqual(_filter_ffmpeg_args(args, self.FF), args)

    def test_sem_ffmpeg_remove_extrair_e_conversao(self):
        args = ["-f", "bestaudio", "-x", "--audio-format", "mp3",
                "--embed-thumbnail", "--embed-metadata"]
        got = _filter_ffmpeg_args(args, None)
        self.assertEqual(got, ["-f", "bestaudio"])

    def test_sem_ffmpeg_remove_flags_com_valor(self):
        # --recode-video consome o próximo argumento; os dois saem
        args = ["--recode-video", "mp4", "--merge-output-format", "mkv"]
        self.assertEqual(_filter_ffmpeg_args(args, None), [])

    def test_sem_ffmpeg_mantem_formato_nativo(self):
        # -f bestaudio (sem conversão) continua — o download segue possível
        args = ["-f", "bestaudio[ext=m4a]/bestaudio"]
        self.assertEqual(_filter_ffmpeg_args(args, None), args)

    def test_embed_subs_e_remux_removidos(self):
        args = ["--embed-subs", "--remux-video", "mp4", "url"]
        self.assertEqual(_filter_ffmpeg_args(args, None), ["url"])


class TestDeps(unittest.TestCase):
    def setUp(self):
        self._old_ff = os.environ.get("MINIDM_FFMPEG")
        self._old_yt = os.environ.get("MINIDM_YTDLP")
        invalidate_cache()

    def tearDown(self):
        for var, old in (("MINIDM_FFMPEG", self._old_ff),
                         ("MINIDM_YTDLP", self._old_yt)):
            if old is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = old
        invalidate_cache()

    def test_status_tem_chaves_esperadas(self):
        st = status()
        for chave in ("ytdlp", "ffmpeg", "features"):
            self.assertIn(chave, st)
        for info in (st["ytdlp"], st["ffmpeg"]):
            self.assertIn("available", info)
            self.assertIn("install", info)
        # features sempre tem a engine própria (nunca depende de externo)
        self.assertTrue(st["features"]["HLS/DASH e arquivos diretos "
                                       "(engine própria)"])

    def test_ffmpeg_env_aponta_pra_diretorio(self):
        tmp = tempfile.mkdtemp(prefix="minidm-ff-")
        try:
            # cria um ffmpeg falso no dir
            nome = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
            open(os.path.join(tmp, nome), "w").close()
            os.environ["MINIDM_FFMPEG"] = os.path.join(tmp, nome)
            invalidate_cache()
            self.assertEqual(ffmpeg_dir(), tmp)
            self.assertTrue(ffmpeg_available())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ffmpeg_env_inexistente_e_none(self):
        os.environ["MINIDM_FFMPEG"] = "/nao/existe/ffmpeg"
        invalidate_cache()
        self.assertIsNone(ffmpeg_dir())

    def test_ffmpeg_procura_em_base_custom(self):
        # valida o search recursivo (winget/scoop) com dir temporário
        from minidm.deps import _search_ffmpeg

        base = tempfile.mkdtemp(prefix="minidm-ffbase-")
        try:
            alvo = os.path.join(base, "pkg", "bin")
            os.makedirs(alvo)
            nome = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
            open(os.path.join(alvo, nome), "w").close()
            got = _search_ffmpeg(base)
            self.assertEqual(got, alvo)
        finally:
            shutil.rmtree(base, ignore_errors=True)


class TestAutoDelegation(unittest.TestCase):
    """`auto_delegate` decide em TODAS as interfaces (CLI/GUI/clipboard/API)."""

    def _task(self, url, kind="file"):
        return Task(url=url, dest_dir=".", filename="x", kind=kind)

    def _with_ytdlp(self, disponivel):
        from minidm import ytdlp

        old = ytdlp.available
        ytdlp.available = lambda: disponivel
        self.addCleanup(setattr, ytdlp, "available", old)

    def test_pagina_com_ytdlp_delega(self):
        self._with_ytdlp(True)
        t = self._task("https://www.youtube.com/watch?v=abc123")
        self.assertTrue(auto_delegate(t))
        self.assertTrue(t.use_ytdlp_downloader)

    def test_pagina_sem_ytdlp_nao_delega(self):
        self._with_ytdlp(False)
        t = self._task("https://www.youtube.com/watch?v=abc123")
        self.assertFalse(auto_delegate(t))
        self.assertFalse(t.use_ytdlp_downloader)

    def test_direto_nunca_delega(self):
        self._with_ytdlp(True)
        for url in ("https://cdn.ex/f.mp4", "https://cdn.ex/f.zip"):
            t = self._task(url)
            self.assertFalse(auto_delegate(t), url)
            self.assertFalse(t.use_ytdlp_downloader)

    def test_hls_dash_nunca_delega(self):
        self._with_ytdlp(True)
        for kind in ("hls", "dash"):
            t = self._task("https://cdn.ex/master.m3u8", kind=kind)
            self.assertFalse(auto_delegate(t), kind)

    def test_pedido_explicito_e_mantido(self):
        self._with_ytdlp(False)   # nem precisa existir
        t = self._task("https://cdn.ex/f.mp4")
        t.use_ytdlp_downloader = True
        self.assertTrue(auto_delegate(t))
        self.assertTrue(t.use_ytdlp_downloader)

    def test_todas_as_interfaces_importam(self):
        # CLI, GUI, daemon e servidor usam a mesma função central
        import minidm.cli as cli
        import minidm.daemon as daemon
        import minidm.gui as gui
        import minidm.server as server

        for mod in (cli, daemon, gui, server):
            self.assertIn("auto_delegate", dir(mod), mod.__name__)


if __name__ == "__main__":
    unittest.main()
