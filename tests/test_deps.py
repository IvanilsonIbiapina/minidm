# -*- coding: utf-8 -*-
"""Testes da integração "de fábrica": deps, ffmpeg, link direto × página.

Rodar:  python -m unittest discover -s tests -p "test_deps.py"
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from minidm.deps import (ffmpeg_bin, ffmpeg_dir, ffmpeg_available, js_runtime,
                         status, invalidate_cache)
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


class TestJsRuntimeAndroid(unittest.TestCase):
    """QuickJS embarcado no APK (libqjs.so) — YouTube completo no celular."""

    def setUp(self):
        self._old = os.environ.get("MINIDM_JS_RUNTIME")
        os.environ.pop("MINIDM_JS_RUNTIME", None)
        invalidate_cache()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MINIDM_JS_RUNTIME", None)
        else:
            os.environ["MINIDM_JS_RUNTIME"] = self._old
        invalidate_cache()

    def test_fora_do_android_devolve_none(self):
        # sem o módulo do APK (desktop/CI) o import hook falha é no-op
        from minidm.deps import _android_qjs
        self.assertIsNone(_android_qjs())

    def test_js_runtime_vira_quickjs_com_caminho(self):
        with mock.patch("minidm.deps._android_qjs",
                        return_value="/data/app/lib/arm64/libqjs.so"):
            self.assertEqual(js_runtime(),
                             "quickjs:/data/app/lib/arm64/libqjs.so")

    def test_quickjs_vence_node_no_android(self):
        # mesmo com node no PATH, no APK o qjs embarcado é o caminho
        with mock.patch("minidm.deps._android_qjs",
                        return_value="/x/libqjs.so"), \
                mock.patch("shutil.which", return_value="/usr/bin/node"):
            self.assertEqual(js_runtime(), "quickjs:/x/libqjs.so")

    def test_env_vence_quickjs(self):
        # override explícito do usuário continua mandando
        os.environ["MINIDM_JS_RUNTIME"] = "runtime-inexistente"
        invalidate_cache()
        with mock.patch("minidm.deps._android_qjs",
                        return_value="/x/libqjs.so"):
            self.assertIsNone(js_runtime())

    def test_status_mostra_instalado_no_apk(self):
        with mock.patch("minidm.deps._android_qjs",
                        return_value="/x/libqjs.so"):
            st = status()
        self.assertTrue(st["js_runtime"]["available"])
        self.assertEqual(st["js_runtime"]["path"], "quickjs:/x/libqjs.so")
        self.assertIn("APK", st["js_runtime"]["install"])


class TestFfmpegAndroid(unittest.TestCase):
    """FFmpeg estático embarcado no APK (`libffmpeg.so` na jniLibs).

    No Android `ffmpeg_dir()` devolve o PRÓPRIO arquivo (o yt-dlp aceita
    caminho de executável e deriva o `libffprobe.so` ao lado via
    `replace("ffmpeg", "ffprobe")`).
    """

    def setUp(self):
        self._old = os.environ.pop("MINIDM_FFMPEG", None)
        invalidate_cache()

    def tearDown(self):
        if self._old is not None:
            os.environ["MINIDM_FFMPEG"] = self._old
        invalidate_cache()

    def test_fora_do_android_devolve_none(self):
        # sem o módulo do APK (desktop/CI) o import hook falha é no-op
        from minidm.deps import _android_ffmpeg
        self.assertIsNone(_android_ffmpeg())

    def test_ffmpeg_dir_e_o_arquivo_no_apk(self):
        with mock.patch("minidm.deps._android_ffmpeg",
                        return_value="/x/libffmpeg.so"):
            self.assertEqual(ffmpeg_dir(), "/x/libffmpeg.so")

    def test_ffmpeg_bin_devolve_o_arquivo(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            path = fh.name
        try:
            with mock.patch("minidm.deps._android_ffmpeg",
                            return_value=path):
                self.assertEqual(ffmpeg_bin(), path)
        finally:
            os.unlink(path)

    def test_env_vence_ffmpeg_do_apk(self):
        # override explícito do usuário continua mandando
        os.environ["MINIDM_FFMPEG"] = "/nao/existe/ffmpeg"
        invalidate_cache()
        with mock.patch("minidm.deps._android_ffmpeg",
                        return_value="/x/libffmpeg.so"):
            self.assertIsNone(ffmpeg_dir())

    def test_status_mostra_embutido_no_apk(self):
        with mock.patch("minidm.deps._android_ffmpeg",
                        return_value="/x/libffmpeg.so"):
            st = status()
        self.assertTrue(st["ffmpeg"]["available"])
        self.assertEqual(st["ffmpeg"]["path"], "/x/libffmpeg.so")
        self.assertIn("APK", st["ffmpeg"]["install"])
        self.assertTrue(st["features"]["converter áudio (mp3) / recodificar"])


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
        for chave in ("ytdlp", "ffmpeg", "paramiko", "features"):
            self.assertIn(chave, st)
        for info in (st["ytdlp"], st["ffmpeg"], st["paramiko"]):
            self.assertIn("available", info)
            self.assertIn("install", info)
        # features sempre tem a engine própria (nunca depende de externo)
        self.assertTrue(st["features"]["HLS/DASH e arquivos diretos "
                                       "(engine própria)"])
        # FTP é nativo (ftplib); SFTP depende do paramiko
        self.assertTrue(st["features"]["FTP (engine própria, sem "
                                       "dependência)"])
        self.assertEqual(st["features"]["SFTP (servidores SSH)"],
                         st["paramiko"]["available"])

    def test_paramiko_available_reflete_a_instalacao(self):
        from minidm.deps import paramiko_available

        self.assertIsInstance(paramiko_available(), bool)
        # com o módulo presente, status deve bater
        self.assertEqual(status()["paramiko"]["available"],
                         paramiko_available())

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
        import minidm.server as server

        interfaces = [cli, daemon, server]
        try:
            import minidm.gui as gui
        except ImportError as e:
            if "tkinter" not in str(e):
                raise
            # GUI exige tkinter; ambiente CI sem ele — as outras
            # interfaces continuam validadas
            gui = None
        if gui is not None:
            interfaces.append(gui)

        for mod in interfaces:
            self.assertIn("auto_delegate", dir(mod), mod.__name__)


if __name__ == "__main__":
    unittest.main()
