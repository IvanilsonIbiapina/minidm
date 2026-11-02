"""Testes da evolução da GUI (minidm/gui.py) + visualizador (player.py).

Só a lógica pura e o construtor de comando do player — sem abrir janelas
(tkinter exibe display; aqui validamos o que dá p/ validar sem GUI):

* `_best_playable` escolhe a melhor qualidade (maior resolução/banda);
* `_kind_badge` monta o rótulo de tipo/qualidade;
* `player._fit_box` escala mantendo proporção e pares;
* `player.play_in_frame` monta o comando do ffmpeg e sobe sem ffmpeg.

Rodar:

    python -m unittest discover -s tests -p "test_gui.py" -v
"""

from __future__ import annotations

import unittest
from unittest import mock

from minidm import player
from minidm.gui import _best_playable, kind_badge


def _q(**kw):
    base = {"url": "http://x/v", "label": "x", "kind": "file",
            "bandwidth": None, "resolution": None, "audio_only": False}
    base.update(kw)
    return base


class TestBestPlayable(unittest.TestCase):
    def test_maior_resolucao_vence(self):
        res = {"qualities": [
            _q(label="360p", resolution="640x360", bandwidth=500000),
            _q(label="1080p", resolution="1920x1080", bandwidth=4000000),
            _q(label="720p", resolution="1280x720", bandwidth=2000000),
        ]}
        _, q = _best_playable(res)
        self.assertEqual(q["label"], "1080p")

    def test_audio_puro_perde_pra_video(self):
        res = {"qualities": [
            _q(label="áudio", audio_only=True, bandwidth=9000000),
            _q(label="720p", resolution="1280x720", bandwidth=1000000),
        ]}
        _, q = _best_playable(res)
        self.assertEqual(q["label"], "720p")

    def test_bandwidth_desempata(self):
        res = {"qualities": [
            _q(label="a", resolution="1280x720", bandwidth=1000),
            _q(label="b", resolution="1280x720", bandwidth=9000),
        ]}
        _, q = _best_playable(res)
        self.assertEqual(q["label"], "b")

    def test_sem_qualidades(self):
        self.assertIsNone(_best_playable({"qualities": []}))


class TestKindBadge(unittest.TestCase):
    def test_hls_com_qualidade(self):
        self.assertEqual(kind_badge({"kind": "hls",
                                      "quality_label": "1080p"}),
                         "HLS 1080p")

    def test_dash_sem_qualidade(self):
        self.assertEqual(kind_badge({"kind": "dash",
                                      "quality_label": ""}), "DASH")

    def test_arquivo_direto_extensao(self):
        self.assertEqual(
            kind_badge({"kind": "file", "filename": "filme.mkv"}),
            "MKV")

    def test_arquivo_sem_extensao(self):
        self.assertEqual(kind_badge({"kind": "file",
                                      "filename": "sem_ext"}),
                         "arq")


class TestPlayerFit(unittest.TestCase):
    def test_mantem_proporcao_e_par(self):
        # 1920x1080 cabe em 640x360 -> 640x360 (ambos pares)
        self.assertEqual(player._fit_box(1920, 1080, 640, 360), (640, 360))
        # retrato: altura é o limite
        w, h = player._fit_box(360, 1080, 640, 360)
        self.assertLessEqual(h, 360)
        self.assertLessEqual(w, 640)

    def test_dimensoes_sempre_pares(self):
        for sw, sh in ((1919, 1079), (100, 333), (7, 9)):
            w, h = player._fit_box(sw, sh, 640, 360)
            self.assertEqual(w % 2, 0, f"{sw}x{sh} -> w={w}")
            self.assertEqual(h % 2, 0, f"{sw}x{sh} -> h={h}")

    def test_fonte_invalida_nao_quebra(self):
        w, h = player._fit_box(0, 0, 640, 360)
        self.assertGreater(w, 0)
        self.assertGreater(h, 0)


class TestPlayerStart(unittest.TestCase):
    def test_sem_ffmpeg_levanta(self):
        with mock.patch.object(player, "ffmpeg_bin", return_value=None):
            with self.assertRaises(RuntimeError):
                player.play_in_frame(mock.Mock(), "http://x/v.mp4")

    def test_comando_ffmpeg_monta_headers(self):
        """Confere a estrutura do comando sem rodar o ffmpeg de verdade."""
        captured = {}

        class _H:
            def __init__(self):
                self.width = 640
                self.height = 360

            def start(self, cmd):
                captured["cmd"] = cmd

        fake_canvas = mock.Mock()
        with mock.patch.object(player, "ffmpeg_bin", return_value="/ff"), \
             mock.patch.object(player, "_ensure_canvas",
                               return_value=fake_canvas), \
             mock.patch.object(player, "PreviewHandle",
                               lambda c, w, h: _H()):
            player.play_in_frame(mock.Mock(), "http://x/master.m3u8",
                                 referer="http://página",
                                 user_agent="UA", proxy="http://p:1")
        cmd = captured["cmd"]
        self.assertEqual(cmd[0], "/ff")
        self.assertIn("-i", cmd)
        self.assertIn("http://x/master.m3u8", cmd)
        self.assertIn("-headers", cmd)
        self.assertIn("Referer: http://página\r\n", cmd)
        self.assertIn("-user_agent", cmd)
        self.assertIn("-http_proxy", cmd)
        self.assertIn("rawvideo", cmd)


if __name__ == "__main__":
    unittest.main()
