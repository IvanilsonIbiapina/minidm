# -*- coding: utf-8 -*-
"""Testes do share ACTION_SEND (texto compartilhado → link na fila).

Rodar:  python -m unittest discover -s tests -p "test_share.py"
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from minidm.share import submit, url_from_text


class TestUrlFromText(unittest.TestCase):
    """Extração da URL do texto que o Android manda em EXTRA_TEXT."""

    def test_url_sozinha(self):
        self.assertEqual(url_from_text("https://youtu.be/abc123"),
                         "https://youtu.be/abc123")

    def test_url_no_meio_do_texto(self):
        self.assertEqual(
            url_from_text("olha só: https://www.youtube.com/watch?v=x9 "
                          "que legal"),
            "https://www.youtube.com/watch?v=x9")

    def test_share_do_youtube_com_feature(self):
        self.assertEqual(
            url_from_text("https://www.youtube.com/watch?v=ID123"
                          "&feature=share"),
            "https://www.youtube.com/watch?v=ID123&feature=share")

    def test_sem_url(self):
        self.assertIsNone(url_from_text("sem link nenhum aqui"))
        self.assertIsNone(url_from_text(""))
        self.assertIsNone(url_from_text(None))

    def test_pontuacao_do_prosa_nao_entra(self):
        self.assertEqual(url_from_text("veja https://exemplo.com/arq."),
                         "https://exemplo.com/arq")
        self.assertEqual(url_from_text("https://exemplo.com/a, b"),
                         "https://exemplo.com/a")

    def test_parentese_equilibrado_fica(self):
        self.assertEqual(
            url_from_text("https://pt.wikipedia.org/wiki/Jogo_(xadrez)"),
            "https://pt.wikipedia.org/wiki/Jogo_(xadrez)")

    def test_parentese_sobrando_sai(self):
        self.assertEqual(url_from_text("veja (https://exemplo.com/x)"),
                         "https://exemplo.com/x")

    def test_multipla_url_pega_a_primeira(self):
        self.assertEqual(url_from_text("https://a.com/1 e depois "
                                       "https://b.com/2"),
                         "https://a.com/1")

    def test_ftp_nao_conta(self):
        # o share é de apps http (navegador/YouTube); ftp não é link
        # de página — o /add continua aceitando quando digitado na webui
        self.assertIsNone(url_from_text("ftp://servidor.com/arq.zip"))


class TestSubmit(unittest.TestCase):
    """POST em /add com a API subindo, ok, rejeitando ou fora."""

    def test_sem_url_devolve_aviso(self):
        self.assertIn("Nenhum link", submit("só texto, sem link", 9742))

    def test_sucesso_mostra_nome(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(
            {"ok": True, "filename": "video.mp4"}).encode()
        with mock.patch("urllib.request.urlopen", return_value=resp):
            msg = submit("https://x.com/a", 9742)
        self.assertIn("Adicionado à fila", msg)
        self.assertIn("video.mp4", msg)

    def test_api_fora_tenta_e_avisa(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=OSError("recusado")):
            msg = submit("https://x.com/a", 9742, tries=2, delay=0)
        self.assertIn("API indisponível", msg)

    def test_rejeicao_400_sem_retry(self):
        err = urllib.error.HTTPError(
            "http://127.0.0.1:9742/add", 400, "Bad Request", {},
            io.BytesIO(json.dumps({"error": "url inválida"}).encode()))
        with mock.patch("urllib.request.urlopen",
                        side_effect=err) as mocked:
            msg = submit("https://x.com/a", 9742, tries=5, delay=0)
        self.assertEqual(mocked.call_count, 1)   # 400 não repete
        self.assertIn("url inválida", msg)

    def test_daemon_subindo_depois_passa(self):
        # 1ª tentativa: socket fechado; 2ª: API pronta
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(
            {"ok": True, "filename": "x.zip"}).encode()
        with mock.patch("urllib.request.urlopen",
                        side_effect=[OSError("refused"), resp]):
            msg = submit("https://x.com/a", 9742, tries=5, delay=0)
        self.assertIn("Adicionado à fila", msg)


if __name__ == "__main__":
    unittest.main()
