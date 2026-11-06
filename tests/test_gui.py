"""Testes da evolução da GUI (minidm/gui.py) + visualizador (player.py).

Só a lógica pura e o construtor de comando do player — sem abrir janelas
(tkinter exibe display; aqui validamos o que dá p/ validar sem GUI):

* `_best_playable` escolhe a melhor qualidade (maior resolução/banda);
* `_kind_badge` monta o rótulo de tipo/qualidade;
* `progress_bar`/`sort_rows` desenham e ordenam a lista (lógica pura);
* `dashboard.eta` calcula o tempo restante (casos-limite);
* `dnd.urls_from_*` convertem drop/colagem em URLs (formato Windows);
* `theme.resolve`/`next_theme` alternam claro/escuro;
* API local vem **ligada por padrão** (`api_enabled`);
* `player._fit_box` escala mantendo proporção e pares;
* `player.play_in_frame` monta o comando do ffmpeg e sobe sem ffmpeg.

Rodar:

    python -m unittest discover -s tests -p "test_gui.py" -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

try:
    import tkinter  # noqa: F401
except ImportError:
    # GUI precisa de tkinter; ambiente headless da CI sem ele pula o módulo
    raise unittest.SkipTest("tkinter indisponível neste ambiente")

from minidm import player
from minidm.config import AppConfig
from minidm.dashboard import eta, eta_seconds
from minidm.dnd import read_url_file, urls_from_clipboard, urls_from_drop
from minidm.gui import _best_playable, kind_badge, progress_bar, sort_rows
from minidm.theme import LIGHT, next_theme, resolve


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


def _row(**kw):
    """Linha no formato do `scheduler.snapshot()` p/ testar a lógica da UI."""
    base = {"id": "t1", "filename": "arq.mp4", "state": "queued",
            "kind": "file", "quality_label": "", "profile": "",
            "progress": 0.0, "received": 0, "total": None, "speed": 0.0,
            "segments_done": 0, "segments_total": 0}
    base.update(kw)
    return base


class TestProgressBar(unittest.TestCase):
    def test_metade_cheia(self):
        self.assertEqual(progress_bar(0.5, 10), "█" * 5 + "░" * 5)

    def test_valores_fora_do_intervalo_sao_cortados(self):
        self.assertEqual(progress_bar(-1, 4), "░" * 4)
        self.assertEqual(progress_bar(2, 4), "█" * 4)
        self.assertEqual(progress_bar(None, 4), "░" * 4)

    def test_nan_nao_quebra(self):
        import math

        self.assertEqual(progress_bar(math.nan, 6), "░" * 6)

    def test_largura_fixa(self):
        self.assertEqual(len(progress_bar(0.33, 20)), 20)


class TestSortRows(unittest.TestCase):
    def setUp(self):
        self.rows = [
            _row(id="a", filename="zebra.mp4", state="done", progress=1.0,
                 received=100, total=100),
            _row(id="b", filename="apple.mp4", state="downloading",
                 progress=0.5, received=50, total=100, speed=10.0),
            _row(id="c", filename="mid.mp4", state="queued"),
        ]

    def test_nome_asc(self):
        out = sort_rows(self.rows, "arquivo", True)
        self.assertEqual([r["id"] for r in out], ["b", "c", "a"])

    def test_progresso_desc(self):
        out = sort_rows(self.rows, "pct", False)
        self.assertEqual([r["id"] for r in out], ["a", "b", "c"])

    def test_eta_sem_estimativa_fica_no_fim_nos_dois_sentidos(self):
        # "c" não tem total -> sem ETA -> sempre no fim
        out = sort_rows(self.rows, "eta", True)
        self.assertEqual(out[-1]["id"], "c")
        out = sort_rows(self.rows, "eta", False)
        self.assertEqual(out[-1]["id"], "c")
        # com estimativa: menor ETA primeiro (asc) / maior primeiro (desc)
        self.assertEqual(out[0]["id"], "b")

    def test_coluna_desconhecida_devolve_a_mesma_lista(self):
        self.assertIs(sort_rows(self.rows, "nao_existe"), self.rows)


class TestEta(unittest.TestCase):
    def test_sem_total_ou_parado_e_none(self):
        self.assertIsNone(eta_seconds(0, None, 100))
        self.assertIsNone(eta_seconds(0, 100, 0))

    def test_completo_e_zero(self):
        self.assertEqual(eta_seconds(100, 100, 10), 0.0)
        self.assertEqual(eta(100, 100, 10), "00:00")

    def test_calculo_simples(self):
        self.assertEqual(eta_seconds(0, 100, 10), 10.0)
        self.assertEqual(eta(0, 3672, 1), "01:01:12")

    def test_mais_de_um_dia(self):
        self.assertEqual(eta(0, 200000, 1), ">1d")

    def test_parado_mostra_tracos(self):
        self.assertEqual(eta(50, 100, 0.0), "--:--")


class TestDndParsing(unittest.TestCase):
    """Drop/colagem de URLs (minidm.dnd) — puro, sem GUI."""

    def test_urls_diretas_no_drop(self):
        self.assertEqual(
            urls_from_drop("https://a.com/v http://b.com/x"),
            ["https://a.com/v", "http://b.com/x"])

    def test_caminho_com_espaco_entre_chaves(self):
        # formato do Windows: {C:\Users\a\meu video.url} — mas o arquivo
        # não existe aqui -> sem URL (não quebra)
        self.assertEqual(urls_from_drop("{C:\\Users\\a\\meu video.url}"), [])

    def test_ignora_lixo_e_arquivos_de_midia(self):
        self.assertEqual(urls_from_drop("ola mundo.txt"), [])
        self.assertEqual(urls_from_drop(""), [])
        self.assertEqual(urls_from_drop("   "), [])

    def test_colagem_multiplas_linhas(self):
        text = "veja\nhttps://c.com/y\nftp://no\nhttp://d.com/z"
        self.assertEqual(urls_from_clipboard(text),
                         ["https://c.com/y", "http://d.com/z"])

    def test_colagem_com_aspas(self):
        self.assertEqual(urls_from_clipboard('"https://a.com/v"'),
                         ["https://a.com/v"])

    def test_colagem_vazia(self):
        self.assertEqual(urls_from_clipboard(None), [])
        self.assertEqual(urls_from_clipboard(""), [])

    def test_atalho_url_lê_o_interno(self):
        tmp = tempfile.mkdtemp(prefix="minidm-dnd-")
        p = os.path.join(tmp, "video.url")
        with open(p, "w", encoding="utf-8") as f:
            f.write("[InternetShortcut]\nURL=https://site.com/filme\n")
        self.assertEqual(read_url_file(p), "https://site.com/filme")
        # o drop lê o atalho e devolve a URL
        self.assertEqual(urls_from_drop("{" + p + "}"),
                         ["https://site.com/filme"])

    def test_atalho_inexistente_vazio(self):
        self.assertEqual(read_url_file("/nao/existe.url"), "")


class TestTema(unittest.TestCase):
    """Paletas claro/escuro (minidm.theme)."""

    def test_resolve_por_nome(self):
        self.assertEqual(resolve("dark").name, "dark")
        self.assertEqual(resolve("light").name, "light")

    def test_auto_e_desconhecido_caem_no_claro(self):
        self.assertIs(resolve("auto"), LIGHT)
        self.assertIs(resolve(""), LIGHT)
        self.assertIs(resolve("qualquer-coisa"), LIGHT)

    def test_next_theme_alterna(self):
        self.assertEqual(next_theme("light"), "dark")
        self.assertEqual(next_theme("dark"), "light")
        # config antiga/quebrada -> primeiro clique já dá escuro
        self.assertEqual(next_theme("auto"), "dark")

    def test_paletas_têm_todas_as_cores(self):
        for pal in (resolve("light"), resolve("dark")):
            for field in ("window", "frame", "surface", "text", "accent",
                          "row_even", "row_odd", "select_bg", "entry_bg"):
                self.assertTrue(getattr(pal, field).startswith("#"),
                                f"{pal.name}.{field}")


class TestApiDefault(unittest.TestCase):
    def test_api_vem_ligada(self):
        self.assertTrue(AppConfig().api_enabled)

    def test_config_antiga_sem_o_campo_continua_ligada(self):
        # config.json gravado antes do campo -> padrão True (só ativa)
        tmp = tempfile.mkdtemp(prefix="minidm-api-")
        with open(os.path.join(tmp, "config.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"api_port": 9999, "max_concurrent": 1}, f)
        cfg = AppConfig.load(tmp)
        self.assertTrue(cfg.api_enabled)
        self.assertEqual(cfg.api_port, 9999)

    def test_escolha_desligada_e_preservada(self):
        tmp = tempfile.mkdtemp(prefix="minidm-api-")
        cfg = AppConfig(data_dir=tmp, api_enabled=False)
        cfg.save()
        self.assertFalse(AppConfig.load(tmp).api_enabled)


if __name__ == "__main__":
    unittest.main()
