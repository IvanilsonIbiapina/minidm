# -*- coding: utf-8 -*-
"""Testes das integrações yt-dlp A–F (conceitos, nunca código copiado).

* (A) cookies do navegador: `--cookies-from-browser` nas flags do yt-dlp e
      conversão de jar Netscape em header p/ a engine própria;
* (B) caminho exato do arquivo: `--print after_move` -> `_finalize` usa o
      caminho real (fim da adivinhação por mtime);
* (C) playlists: `expand_playlist` (mock de subprocesso, sem rede);
* (D) trechos: campo `section` na Task + flag `--download-sections`;
* (E) fragmentos concorrentes + versidade/idade do yt-dlp;
* (F) SponsorBlock via config.

Rodar com:  python -m unittest discover -s tests -p "test_ytdlp_integ.py"
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from minidm.config import AppConfig
from minidm.cookies import (header_from_jar, jar_path, clear_cache,
                            browser_cookie_header)
from minidm.meta import Task, TaskState
from minidm.ytdlp import expand_playlist, is_outdated
from minidm.ytdlpdl import YtdlpDownloader, _filter_ffmpeg_args


class TestCookieJar(unittest.TestCase):
    """(A) jar Netscape -> header Cookie."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-ck-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, lines):
        p = os.path.join(self.tmp, "jar.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return p

    def test_converte_jar_em_header(self):
        p = self._write([
            "# Netscape HTTP Cookie File",
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc",
            ".youtube.com\tTRUE\t/\tTRUE\t0\tHSID\tdef",
        ])
        h = header_from_jar(p)
        self.assertIn("SID=abc", h)
        self.assertIn("HSID=def", h)
        self.assertIn("; ", h)

    def test_ignora_comentarios_e_linhas_ruins(self):
        p = self._write([
            "# comment",
            "",
            "linha-que-nao-e-cookie",
            ".x.com\tTRUE\t/\tTRUE\t0\tA\t1",
        ])
        h = header_from_jar(p)
        self.assertEqual(h, "A=1")

    def test_arquivo_inexistente_devolve_vazio(self):
        self.assertEqual(header_from_jar(os.path.join(self.tmp, "nope.txt")),
                         "")

    def test_jar_path_e_cache_clear(self):
        self.assertTrue(jar_path(self.tmp, "Chrome").endswith("cookies-chrome.txt"))
        # grava no caminho de jar real e confere que clear_cache remove
        p = jar_path(self.tmp, "chrome")
        with open(p, "w", encoding="utf-8") as f:
            f.write(".x.com\tTRUE\t/\tTRUE\t0\tA\t1\n")
        clear_cache(self.tmp)
        self.assertFalse(os.path.exists(p))

    def test_browser_cookie_header_sem_ytdlp_devolve_vazio(self):
        # sem yt-dlp no ambiente -> None -> header vazio (não quebra)
        with mock.patch("minidm.ytdlp.find", return_value=None):
            self.assertEqual(browser_cookie_header("chrome", self.tmp), "")


class TestBuildCmdFlags(unittest.TestCase):
    """(A/B/D/E/F) flags novas no comando do YtdlpDownloader."""

    def _cmd(self, cfg, task):
        dl = YtdlpDownloader(task, cfg)
        return " ".join(dl._build_cmd())

    def test_b_print_after_move(self):
        t = Task(url="https://youtu.be/a", dest_dir="/tmp", filename="a.mp4")
        c = self._cmd(AppConfig(), t)
        self.assertIn("--print", c)
        self.assertIn("after_move:MINIDM_PATH:%(filepath)s", c)

    def test_a_cookies_from_browser(self):
        t = Task(url="https://youtu.be/a", dest_dir="/tmp", filename="a.mp4")
        c = self._cmd(AppConfig(cookies_from_browser="chrome"), t)
        self.assertIn("--cookies-from-browser chrome", c)

    def test_a_cookie_explicito_precede_browser(self):
        t = Task(url="https://youtu.be/a", dest_dir="/tmp", filename="a.mp4",
                 cookie="k=v")
        c = self._cmd(AppConfig(cookies_from_browser="chrome"), t)
        self.assertIn("Cookie:k=v", c)
        self.assertNotIn("--cookies-from-browser", c)

    def test_d_section_com_asterisco(self):
        t = Task(url="https://youtu.be/a", dest_dir="/tmp", filename="a.mp4",
                 section="01:00-02:00")
        c = self._cmd(AppConfig(), t)
        self.assertIn("--download-sections *01:00-02:00", c)
        self.assertIn("--force-keyframes-at-cuts", c)

    def test_d_sem_section_nao_passa_flag(self):
        t = Task(url="https://youtu.be/a", dest_dir="/tmp", filename="a.mp4")
        c = self._cmd(AppConfig(), t)
        self.assertNotIn("--download-sections", c)

    def test_e_concurrent_fragments(self):
        t = Task(url="https://youtu.be/a", dest_dir="/tmp", filename="a.mp4")
        c = self._cmd(AppConfig(concurrent_fragments=4), t)
        self.assertIn("--concurrent-fragments 4", c)

    def test_f_sponsorblock(self):
        t = Task(url="https://youtu.be/a", dest_dir="/tmp", filename="a.mp4")
        c = self._cmd(AppConfig(sponsorblock="all"), t)
        self.assertIn("--sponsorblock-remove all", c)


class TestFinalizeCaminhoExato(unittest.TestCase):
    """(B) `_finalize` usa o caminho que o yt-dlp reportou."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-fin-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_usa_final_path_quando_existe(self):
        real = os.path.join(self.tmp, "Video [abc].mkv")
        with open(real, "wb") as f:
            f.write(b"x" * 500)
        t = Task(url="https://youtu.be/abc", dest_dir=self.tmp,
                 filename="[abc].mp4", use_ytdlp_downloader=True)
        dl = YtdlpDownloader(t, AppConfig())
        dl._final_path = real
        dl._finalize()
        self.assertEqual(t.filename, "Video [abc].mkv")
        self.assertEqual(t.total_size, 500)
        self.assertEqual(t.state, TaskState.DONE)

    def test_sem_final_path_cai_no_fallback(self):
        # sem caminho reportado e sem arquivo -> não quebra, marca DONE
        t = Task(url="https://youtu.be/abc", dest_dir=self.tmp,
                 filename="ausente.mp4", use_ytdlp_downloader=True)
        dl = YtdlpDownloader(t, AppConfig())
        dl._finalize()
        self.assertEqual(t.state, TaskState.DONE)


class _FakeProc:
    def __init__(self, payload, rc=0):
        self.returncode = rc
        self.stdout = json.dumps(payload).encode()
        self.stderr = b""


class TestExpandPlaylist(unittest.TestCase):
    """(C) expansão de playlist (subprocesso mockado, sem rede)."""

    PL = {"entries": [
        {"url": "https://youtu.be/v1", "title": "Um", "duration": 10},
        {"url": "https://youtu.be/v2", "title": "Dois", "duration": 20},
        {"url": "https://youtu.be/v3", "title": "Três", "duration": 30},
    ]}

    def _expand(self, payload, **kw):
        with mock.patch("minidm.ytdlp.find", return_value=["yt-dlp"]), \
                mock.patch("subprocess.run",
                           return_value=_FakeProc(payload)):
            return expand_playlist("https://youtube.com/playlist?list=X", **kw)

    def test_expande_respeitando_limit(self):
        items = self._expand(self.PL, limit=2)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["url"], "https://youtu.be/v1")
        self.assertEqual(items[1]["title"], "Dois")

    def test_url_nao_playlist_devolve_single(self):
        items = self._expand({"webpage_url": "https://youtu.be/one",
                              "title": "Unico", "duration": 5})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://youtu.be/one")

    def test_erro_ytdlp_levanta_runtime(self):
        with mock.patch("minidm.ytdlp.find", return_value=["yt-dlp"]), \
                mock.patch("subprocess.run",
                           return_value=_FakeProc({}, rc=1)):
            with self.assertRaises(RuntimeError):
                expand_playlist("https://x")


class TestOutdated(unittest.TestCase):
    """(E) detecção de yt-dlp desatualizado."""

    def test_versao_antiga_marcada(self):
        from datetime import date, timedelta
        old = (date.today() - timedelta(days=120)).isoformat().replace("-", ".")
        with mock.patch("minidm.ytdlp.version", return_value=old):
            self.assertTrue(is_outdated(90))

    def test_versao_recente_ok(self):
        from datetime import date
        fresh = date.today().isoformat().replace("-", ".")
        with mock.patch("minidm.ytdlp.version", return_value=fresh):
            self.assertFalse(is_outdated(90))

    def test_sem_versao_devolve_none(self):
        with mock.patch("minidm.ytdlp.version", return_value=None):
            self.assertIsNone(is_outdated())


class TestFiltroFfmpeg(unittest.TestCase):
    """sem ffmpeg, as flags que exigem ffmpeg são removidas (com o valor)."""

    def test_remove_sponsorblock_e_section_sem_ffmpeg(self):
        args = ["--sponsorblock-remove", "all",
                "--download-sections", "*01:00-02:00",
                "--force-keyframes-at-cuts", "-f", "best"]
        out = _filter_ffmpeg_args(args, None)
        self.assertEqual(out, ["-f", "best"])

    def test_com_ffmpeg_mantem_tudo(self):
        args = ["--sponsorblock-remove", "all", "-f", "best"]
        self.assertEqual(_filter_ffmpeg_args(args, "/ff"), args)


if __name__ == "__main__":
    unittest.main()
