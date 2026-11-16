# -*- coding: utf-8 -*-
"""Spotify: metadados públicos -> busca em fonte NÃO-DRM -> fila + recusa DRM.

Cobre as DUAS metades da feature:
* `minidm spotify <url>` — parse de URL, oEmbed/embed (`__NEXT_DATA__`),
  normalização do trackList, match por duração (ytsearch) e enfileiramento
  com perfil "Música"/categoria "Músicas";
* recusa de DRM em TODAS as entradas (`media.analyze`, `add` da CLI e
  `POST /add`) com a mensagem DMCA §1201 / Lei 9.610 que aponta o
  fluxo legítimo.

Sem rede: HTTP mockado (`minidm.spotify.requests.get`) e yt-dlp mockado
(`minidm.ytdlp.describe`). Os payloads refletem o que foi verificado ao
vivo em 2026-11-15 (caminho `props.pageProps.state.data.entity`,
duração em ms, `trackList` com uri/title/subtitle).

Rodar:  python -m unittest discover -s tests -p "test_spotify.py" -v
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from http.server import ThreadingHTTPServer
from unittest import mock

import requests

from minidm import cli, spotify
from minidm.config import AppConfig
from minidm.meta import TaskState
from minidm.scheduler import Scheduler
from minidm.server import App, make_handler
from minidm.store import TaskStore

TRACK_URL = "https://open.spotify.com/track/3n3Ppam7vgaVa1iaRUc9Lp"
PLAYLIST_URL = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
YT_URL = "https://www.youtube.com/watch?v=gGdGFtwCNBE"
# candidato ESCOLHIDO p/ 222.2 s: o de 222 s (menor diferença)
WATCH_222 = "https://www.youtube.com/watch?v=l7MaKmKJqoc"

# payloads no formato REAL da página embed (verificados ao vivo)
ENT_TRACK = {
    "type": "track", "name": "Mr. Brightside", "duration": 222200,
    "uri": "spotify:track:3n3Ppam7vgaVa1iaRUc9Lp",
    "artists": [{"name": "The Killers",
                 "uri": "spotify:artist:0C0XlULifJtAgn6ZNCW2eu"}],
    "isPlayable": True, "playabilityReason": "PLAYABLE",
    "visualIdentity": {"image": [{"url": "https://i.scdn.co/image/ab67"}]},
}


def _item(i: int, title: str, artists: str, ms: int,
          playable: bool = True, reason: str = "PLAYABLE") -> dict:
    return {"uri": "spotify:track:" + f"{i:022d}",
            "title": title, "subtitle": artists, "duration": ms,
            "isPlayable": playable, "playabilityReason": reason,
            "isExplicit": False}


ENT_PLAYLIST = {
    "type": "playlist", "name": "Today's Top Hits", "subtitle": "Spotify",
    "isPlayable": True,
    "trackList": [
        _item(1, "BbY WOW", "KAROL G, Judeline, rusowsky", 225834),
        _item(2, "Housewife", "Tate McRae", 208810),
        _item(3, "Restricted", "Artista X", 180000,
              playable=False, reason="PLAYABILITY_RESTRICTED_IN_REGION"),
    ],
}


def embed_html(entity: dict) -> str:
    """HTML mínimo com o `__NEXT_DATA__` no caminho real do embed."""
    data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return ('<html><head><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(data) + "</script></head></html>")


def http(status: int = 200, *, text: str = "", payload=None) -> mock.Mock:
    r = mock.Mock()
    r.status_code = status
    r.text = text
    r.json.return_value = payload
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(str(status))
    else:
        r.raise_for_status.return_value = None
    return r


def entry(dur, title, url, drm=None) -> dict:
    e = {"duration": dur, "title": title, "webpage_url": url,
         "id": url.rsplit("=", 1)[-1]}
    if drm is not None:
        e["_has_drm"] = drm
    return e


SEARCH_RESULT = {
    "_has_drm": False,
    "entries": [
        entry(228, "The Killers - Mr. Brightside (Official Music Video)",
              YT_URL),
        entry(222, "Mr. Brightside",
              "https://www.youtube.com/watch?v=l7MaKmKJqoc"),
        entry(501, "The Killers - Mr Brightside | Reading Festival 2023",
              "https://www.youtube.com/watch?v=4hbZbdXIf7c"),
    ],
}


# --------------------------------------------------------------------- parse
class TestParse(unittest.TestCase):
    def test_varias_formas_de_url(self):
        casos = [
            (TRACK_URL, "track", "3n3Ppam7vgaVa1iaRUc9Lp"),
            (TRACK_URL + "?si=abc123", "track", "3n3Ppam7vgaVa1iaRUc9Lp"),
            (TRACK_URL + "/", "track", "3n3Ppam7vgaVa1iaRUc9Lp"),
            ("https://open.spotify.com/intl-br/track/3n3Ppam7vgaVa1iaRUc9Lp",
             "track", "3n3Ppam7vgaVa1iaRUc9Lp"),
            ("https://open.spotify.com/embed/track/3n3Ppam7vgaVa1iaRUc9Lp",
             "track", "3n3Ppam7vgaVa1iaRUc9Lp"),
            ("spotify:track:3n3Ppam7vgaVa1iaRUc9Lp",
             "track", "3n3Ppam7vgaVa1iaRUc9Lp"),
            (PLAYLIST_URL, "playlist", "37i9dQZF1DXcBWIGoYBM5M"),
            ("https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3",
             "album", "1DFixLWuPkv3KT3TnV35m3"),
            ("https://open.spotify.com/artist/06HL4z0CvFAxyc27GXpf02",
             "artist", "06HL4z0CvFAxyc27GXpf02"),
            ("https://open.spotify.com/show/4rOjJ6/xx", "show", None),  # id curto
            ("https://open.spotify.com/episode/6rqhFgbbKwnb9MLmUQDhG6",
             "episode", "6rqhFgbbKwnb9MLmUQDhG6"),
        ]
        for url, kind, sid in casos:
            with self.subTest(url=url):
                got_kind, got_id = spotify.is_spotify_url(url)
                self.assertEqual(got_kind, kind if sid else None)
                if sid:
                    self.assertEqual(got_id, sid)

    def test_url_que_nao_e_spotify(self):
        for url in ("", "https://www.youtube.com/watch?v=abc",
                    "https://spotify.com/", "https://open.spotify.com/",
                    "https://open.spotify.com/collection/tracks",
                    "https://open.spotify.com/search/musica",
                    "magnet:?xt=urn:btih:abc", "file:///C:/musica.mp3",
                    "https://open.spotify.com/track/curto"):
            with self.subTest(url=url):
                self.assertEqual(spotify.is_spotify_url(url), (None, None))

    def test_preview_e_imagem_do_scdn(self):
        self.assertTrue(
            spotify.is_preview_url("https://p.scdn.co/mp3-preview/f6ab45"))
        self.assertFalse(
            spotify.is_preview_url("https://i.scdn.co/image/ab67616"))
        self.assertFalse(spotify.is_preview_url("https://exemplo.com/a.mp3"))


# ---------------------------------------------------------------- recusa DRM
class TestRecusaDrm(unittest.TestCase):
    def test_resposta_padrao_explica_e_aponta_o_fluxo(self):
        r = spotify.refuse()
        self.assertFalse(r["ok"])
        self.assertEqual(r["drm"], "spotify")
        for fragmento in ("DRM (spotify)", "DMCA", "9.610",
                          "minidm spotify", "NÃO-DRM"):
            self.assertIn(fragmento, r["error"])

    def test_refuse_if_spotify_cobre_url_e_preview(self):
        self.assertEqual(spotify.refuse_if_spotify(TRACK_URL)["drm"], "spotify")
        self.assertEqual(spotify.refuse_if_spotify("spotify:track:abc123456789")[
            "drm"], "spotify")
        self.assertEqual(spotify.refuse_if_spotify(
            "https://p.scdn.co/mp3-preview/abc")["drm"], "spotify")
        self.assertIsNone(spotify.refuse_if_spotify("https://x.com/a.mp4"))
        self.assertIsNone(spotify.refuse_if_spotify(""))

    def test_media_analyze_recusa_antes_de_qualquer_rede(self):
        from minidm import media

        with mock.patch("minidm.media.requests.Session") as sess, \
                mock.patch("minidm.spotify.requests.get") as get:
            r = media.analyze(TRACK_URL)
        sess.assert_not_called()      # nada de GET na página do Spotify
        get.assert_not_called()
        self.assertFalse(r["ok"])
        self.assertEqual(r["drm"], "spotify")
        self.assertIn("DMCA", r["error"])
        self.assertIn("minidm spotify", r["error"])

    def test_cli_add_recusa_e_nao_enfileira(self):
        tmp = tempfile.mkdtemp(prefix="minidm-spot-add-")
        self.addCleanup(shutil.rmtree, tmp, True)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["add", TRACK_URL, "--data-dir", tmp])
        self.assertEqual(code, 2)
        self.assertIn("minidm spotify", err.getvalue())
        self.assertIn("DMCA", err.getvalue())
        self.assertEqual(TaskStore(tmp).all(), [])

    def test_cli_add_playlist_recusa_sem_chamar_o_ytdlp(self):
        # o yt-dlp NÃO tem extractor do Spotify (KnownDRMIE): a recusa
        # tem que vir ANTES da expansão, sem sequer invocá-lo
        tmp = tempfile.mkdtemp(prefix="minidm-spot-pl-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch("minidm.ytdlp.expand_playlist") as exp, \
                redirect_stderr(io.StringIO()):
            code = cli.main(["add", PLAYLIST_URL, "--playlist",
                             "--data-dir", tmp])
        self.assertEqual(code, 2)
        exp.assert_not_called()
        self.assertEqual(TaskStore(tmp).all(), [])

    def test_cli_media_imprime_a_explicacao_completa(self):
        tmp = tempfile.mkdtemp(prefix="minidm-spot-med-")
        self.addCleanup(shutil.rmtree, tmp, True)
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli.main(["media", TRACK_URL, "--data-dir", tmp])
        self.assertEqual(code, 1)
        self.assertIn("DRM (spotify)", err.getvalue())
        self.assertIn("DMCA", err.getvalue())
        self.assertIn("minidm spotify", err.getvalue())


# --------------------------------------------------------------- metadados
class TestMetadata(unittest.TestCase):
    @mock.patch("minidm.spotify.requests.get")
    def test_track_completa(self, get):
        get.return_value = http(200, text=embed_html(ENT_TRACK))
        meta = spotify.metadata("track", "3n3Ppam7vgaVa1iaRUc9Lp", TRACK_URL)
        self.assertEqual(meta["title"], "Mr. Brightside")
        self.assertFalse(meta["truncated"])
        self.assertEqual(len(meta["tracks"]), 1)
        t = meta["tracks"][0]
        self.assertEqual(t["artists"], "The Killers")
        self.assertEqual(t["duration"], 222.2)       # ms -> s
        self.assertTrue(t["playable"])
        self.assertEqual(t["id"], "3n3Ppam7vgaVa1iaRUc9Lp")
        self.assertEqual(meta["thumbnail"],
                         "https://i.scdn.co/image/ab67")
        # a chamada foi na página embed certa (sem login, sem Web API)
        self.assertIn("/embed/track/3n3Ppam7vgaVa1iaRUc9Lp",
                      get.call_args[0][0])

    @mock.patch("minidm.spotify.requests.get")
    def test_playlist_normaliza_o_tracklist(self, get):
        get.return_value = http(200, text=embed_html(ENT_PLAYLIST))
        meta = spotify.metadata("playlist", "37i9dQZF1DXcBWIGoYBM5M",
                                PLAYLIST_URL)
        self.assertEqual(meta["title"], "Today's Top Hits")
        self.assertFalse(meta["truncated"])
        self.assertEqual(len(meta["tracks"]), 3)
        primeiro, bloqueada = meta["tracks"][0], meta["tracks"][2]
        self.assertEqual(primeiro["title"], "BbY WOW")
        self.assertEqual(primeiro["artists"],
                         "KAROL G, Judeline, rusowsky")   # subtitle
        self.assertEqual(primeiro["duration"], 225.834)    # ms -> s
        self.assertEqual(primeiro["id"], f"{1:022d}")
        self.assertFalse(bloqueada["playable"])
        self.assertEqual(bloqueada["reason"],
                         "PLAYABILITY_RESTRICTED_IN_REGION")

    @mock.patch("minidm.spotify.requests.get")
    def test_playlist_no_teto_de_100_marca_truncamento(self, get):
        ent = dict(ENT_PLAYLIST,
                   trackList=[_item(i, f"Faixa {i}", "Artista", 200000)
                              for i in range(1, 101)])
        get.return_value = http(200, text=embed_html(ent))
        meta = spotify.metadata("playlist", "37i9dQZF1DXcBWIGoYBM5M",
                                PLAYLIST_URL)
        self.assertTrue(meta["truncated"])
        self.assertEqual(len(meta["tracks"]), 100)

    @mock.patch("minidm.spotify.requests.get")
    def test_album_sem_artistas_nos_itens_usa_o_subtitle(self, get):
        ent = {"type": "album", "name": "Emotion (Deluxe)",
               "subtitle": "Carly Rae Jepsen",
               "trackList": [_item(1, "Run Away With Me", "", 251320)]}
        get.return_value = http(200, text=embed_html(ent))
        meta = spotify.metadata("album", "1DFixLWuPkv3KT3TnV35m3",
                                "https://open.spotify.com/album/x")
        self.assertEqual(meta["tracks"][0]["artists"], "Carly Rae Jepsen")

    @mock.patch("minidm.spotify.requests.get")
    def test_track_embed_fora_cai_no_oembed(self, get):
        # 1ª chamada (embed) falha; 2ª (oEmbed) devolve título/capa
        get.side_effect = [requests.ConnectionError("sem rede"),
                           http(200, payload={"title": "Mr. Brightside",
                                              "thumbnail_url": "https://i"})]
        meta = spotify.metadata("track", "3n3Ppam7vgaVa1iaRUc9Lp", TRACK_URL)
        self.assertEqual(meta["title"], "Mr. Brightside")
        # dá pra buscar pelo menos pelo título (sem duração/artista)
        self.assertEqual(meta["tracks"][0]["title"], "Mr. Brightside")
        self.assertEqual(spotify.search_query(meta["tracks"][0]),
                         "Mr. Brightside")

    @mock.patch("minidm.spotify.requests.get")
    def test_playlist_sem_tracklist_erro_claro(self, get):
        # embed responde 200 mas sem __NEXT_DATA__; oEmbed salva o título
        get.side_effect = [http(200, text="<html>sem next data</html>"),
                           http(200, payload={"title": "Today's Top Hits"})]
        with self.assertRaises(RuntimeError) as ctx:
            spotify.metadata("playlist", "37i9dQZF1DXcBWIGoYBM5M",
                             PLAYLIST_URL)
        self.assertIn("lista de faixas", str(ctx.exception))

    @mock.patch("minidm.spotify.requests.get")
    def test_tudo_fora_erro_claro_sem_traceback(self, get):
        get.side_effect = requests.ConnectionError("sem rede")
        with self.assertRaises(RuntimeError) as ctx:
            spotify.metadata("track", "3n3Ppam7vgaVa1iaRUc9Lp", TRACK_URL)
        self.assertIn("metadados do Spotify", str(ctx.exception))


# ------------------------------------------------------------ busca/resolve
class TestSearchQuery(unittest.TestCase):
    def test_artista_mais_titulo(self):
        self.assertEqual(spotify.search_query(
            {"artists": "The Killers", "title": "Mr. Brightside"}),
            "The Killers - Mr. Brightside")

    def test_titulo_ja_comeca_com_o_artista(self):
        self.assertEqual(spotify.search_query(
            {"artists": "Kun", "title": "Kun - 212KB"}), "Kun - 212KB")

    def test_so_titulo_ou_so_artista(self):
        self.assertEqual(spotify.search_query({"title": "Sem Artista"}),
                         "Sem Artista")
        self.assertEqual(spotify.search_query({"artists": "Alguém"}),
                         "Alguém")

    def test_primeiro_artista_da_lista(self):
        self.assertEqual(spotify.search_query(
            {"artists": "KAROL G, Judeline, rusowsky", "title": "BbY WOW"}),
            "KAROL G - BbY WOW")


class TestResolve(unittest.TestCase):
    TRACK = {"artists": "The Killers", "title": "Mr. Brightside",
             "duration": 222.2}

    @mock.patch("minidm.ytdlp.describe")
    def test_escolhe_menor_diferenca_de_duracao(self, desc):
        desc.return_value = SEARCH_RESULT
        r = spotify.resolve(self.TRACK)
        self.assertEqual(r["query"], "The Killers - Mr. Brightside")
        self.assertEqual(r["url"], WATCH_222)
        self.assertAlmostEqual(r["diff"], 0.2)          # 222 s vs. 222.2 s
        self.assertFalse(r["mismatch"])
        # uma chamada só, com ytsearch10 (candidatos p/ o match)
        self.assertTrue(desc.call_args[0][0].startswith("ytsearch10:"))

    @mock.patch("minidm.ytdlp.describe")
    def test_no_match_confia_no_primeiro(self, desc):
        desc.return_value = SEARCH_RESULT
        r = spotify.resolve(self.TRACK, no_match=True)
        self.assertEqual(r["url"], YT_URL)
        self.assertIsNone(r["diff"])

    @mock.patch("minidm.ytdlp.describe")
    def test_fora_da_tolerancia_sem_strict_so_avisa(self, desc):
        desc.return_value = {"entries": [entry(
            501, "ao vivo", "https://www.youtube.com/watch?v=live")]}
        r = spotify.resolve(self.TRACK, tolerance=10.0)
        self.assertTrue(r["mismatch"])
        self.assertEqual(r["diff"], 278.8)

    @mock.patch("minidm.ytdlp.describe")
    def test_strict_falha_com_mensagem_acionavel(self, desc):
        desc.return_value = {"entries": [entry(
            501, "ao vivo", "https://www.youtube.com/watch?v=live")]}
        with self.assertRaises(spotify.ResolveError) as ctx:
            spotify.resolve(self.TRACK, tolerance=10.0, strict=True)
        msg = str(ctx.exception)
        self.assertIn("--tolerance", msg)
        self.assertIn("222", msg)                # duração do Spotify
        self.assertIn("--no-match", msg)

    @mock.patch("minidm.ytdlp.describe")
    def test_sem_candidato_erro_acionavel(self, desc):
        desc.return_value = {"entries": []}
        with self.assertRaises(spotify.ResolveError) as ctx:
            spotify.resolve(self.TRACK)
        msg = str(ctx.exception)
        self.assertIn("não achei", msg)
        self.assertIn("minidm add", msg)

    @mock.patch("minidm.ytdlp.describe")
    def test_candidato_com_drm_e_descartado(self, desc):
        desc.return_value = {"entries": [
            entry(222, "rip", "https://www.youtube.com/watch?v=drm", drm=True),
            entry(222, "certo", "https://www.youtube.com/watch?v=ok")]}
        r = spotify.resolve(self.TRACK)
        self.assertEqual(r["url"], "https://www.youtube.com/watch?v=ok")

    @mock.patch("minidm.ytdlp.describe")
    def test_container_com_drm_recusado(self, desc):
        desc.return_value = {"_has_drm": True, "entries": []}
        with self.assertRaises(spotify.ResolveError) as ctx:
            spotify.resolve(self.TRACK)
        self.assertIn("DMCA", str(ctx.exception))

    @mock.patch("minidm.ytdlp.describe")
    def test_candidato_sem_url_e_ignorado(self, desc):
        desc.return_value = {"entries": [{"duration": 222, "title": "x"}]}
        with self.assertRaises(spotify.ResolveError):
            spotify.resolve(self.TRACK)


# --------------------------------------------------------------------- CLI
class TestCliSpotify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-spot-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dl = os.path.join(self.tmp, "dl")
        # pasta base das categorias DENTRO do tmp (nunca ~/Downloads)
        AppConfig(data_dir=self.tmp, download_base=self.dl).save()
        self.store = TaskStore(self.tmp)

    @staticmethod
    def _run(argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _tasks(self):
        # store NOVO: a fila é carregada na inicialização — o do setUp
        # ficaria defasado depois que a CLI grava tasks.json
        return TaskStore(self.tmp).all()

    def _patches(self, meta=None, describe=None):
        """metadata (HTTP) + describe (yt-dlp) mockados, no formato real."""
        m = mock.patch("minidm.spotify.requests.get",
                       return_value=http(200, text=embed_html(ENT_TRACK)))
        d = mock.patch("minidm.ytdlp.describe",
                       return_value=describe or SEARCH_RESULT)
        return m, d

    def test_dry_run_mostra_a_busca_e_nao_enfileira(self):
        m, d = self._patches()
        with m, d:
            code, out, err = self._run(["spotify", TRACK_URL, "--dry-run",
                                        "--data-dir", self.tmp])
        self.assertEqual(code, 0, err)
        self.assertIn("Mr. Brightside", out)
        self.assertIn("The Killers - Mr. Brightside", out)  # a query
        self.assertIn(WATCH_222, out)                       # o escolhido
        self.assertIn("Δ0s", out)                           # 222 vs 222.2
        self.assertIn("dry-run", out)
        self.assertEqual(self._tasks(), [])

    def test_enfileira_com_perfil_musica_e_origem_spotify(self):
        m, d = self._patches()
        with m, d:
            code, out, err = self._run(["spotify", TRACK_URL,
                                        "--data-dir", self.tmp])
        self.assertEqual(code, 0, err)
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual(t.url, WATCH_222)
        self.assertEqual(t.origin_url, TRACK_URL)      # metadados do Spotify
        self.assertEqual(t.page_title, "Mr. Brightside")
        self.assertEqual(t.state, TaskState.QUEUED)
        self.assertEqual(t.kind, "file")
        self.assertEqual(t.profile, "Música")
        self.assertTrue(t.use_ytdlp_downloader)        # -> YtdlpDownloader
        self.assertEqual(t.category, "Músicas")
        self.assertTrue(t.filename.startswith("The Killers - Mr. Brightside"),
                        t.filename)
        self.assertTrue(t.filename.endswith(".mp3"), t.filename)
        self.assertTrue(t.ytdlp_args, "perfil tinha que dar args do yt-dlp")
        # categoria criou a pasta em `download_base` (e não em ~/Downloads)
        self.assertEqual(os.path.normcase(t.dest_dir),
                         os.path.normcase(os.path.join(self.dl, "Músicas")))

    def test_playlist_enfileira_e_pula_a_indisponivel(self):
        meta = {"kind": "playlist", "id": "x", "title": "Today's Top Hits",
                "thumbnail": "", "truncated": False,
                "tracks": [
                    {"kind": "track", "id": "a", "title": "BbY WOW",
                     "artists": "KAROL G", "duration": 225.834,
                     "playable": True, "reason": "PLAYABLE"},
                    {"kind": "track", "id": "b", "title": "Housewife",
                     "artists": "Tate McRae", "duration": 208.81,
                     "playable": True, "reason": "PLAYABLE"},
                    {"kind": "track", "id": "c", "title": "Restricted",
                     "artists": "Artista X", "duration": 180.0,
                     "playable": False,
                     "reason": "PLAYABILITY_RESTRICTED_IN_REGION"},
                ]}
        with mock.patch("minidm.spotify.metadata", return_value=meta), \
                mock.patch("minidm.ytdlp.describe",
                           return_value=SEARCH_RESULT):
            code, out, err = self._run(["spotify", PLAYLIST_URL,
                                        "--data-dir", self.tmp])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self._tasks()), 2)
        self.assertIn("1 faixa(s) não tocável", out)
        self.assertIn("PLAYABILITY_RESTRICTED_IN_REGION", out)
        self.assertIn("[2/2]", out)

    def test_limit_corta_a_lista(self):
        meta = {"kind": "playlist", "id": "x", "title": "P", "thumbnail": "",
                "truncated": False,
                "tracks": [{"kind": "track", "id": str(i), "title": f"F{i}",
                            "artists": "A", "duration": 200.0,
                            "playable": True, "reason": "PLAYABLE"}
                           for i in range(5)]}
        with mock.patch("minidm.spotify.metadata", return_value=meta), \
                mock.patch("minidm.ytdlp.describe",
                           return_value=SEARCH_RESULT):
            code, out, err = self._run(["spotify", PLAYLIST_URL, "--limit", "2",
                                        "--data-dir", self.tmp])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self._tasks()), 2)

    def test_url_invalida_sai_com_2(self):
        code, _, err = self._run(["spotify", "https://youtube.com/watch?v=x",
                                  "--data-dir", self.tmp])
        self.assertEqual(code, 2)
        self.assertIn("URL do Spotify inválida", err)

    def test_episode_e_artist_fora_do_escopo(self):
        for url, pedaco in (
                ("https://open.spotify.com/episode/6rqhFgbbKwnb9MLmUQDhG6",
                 "podcast"),
                ("https://open.spotify.com/artist/06HL4z0CvFAxyc27GXpf02",
                 "playlist ou track")):
            with self.subTest(url=url):
                code, _, err = self._run(["spotify", url,
                                          "--data-dir", self.tmp])
                self.assertEqual(code, 2)
                self.assertIn(pedaco, err)

    def test_strict_com_diferenca_grande_sai_com_1(self):
        with mock.patch("minidm.spotify.requests.get",
                        return_value=http(200, text=embed_html(ENT_TRACK))), \
                mock.patch("minidm.ytdlp.describe",
                           return_value={"entries": [entry(
                               501, "ao vivo",
                               "https://www.youtube.com/watch?v=live")]}):
            code, out, err = self._run(["spotify", TRACK_URL, "--strict",
                                        "--data-dir", self.tmp])
        self.assertEqual(code, 1)
        self.assertIn("--tolerance", err)
        self.assertEqual(self._tasks(), [])

    def test_ytdlp_ausente_sai_com_1_e_explica(self):
        with mock.patch("minidm.spotify.requests.get",
                        return_value=http(200, text=embed_html(ENT_TRACK))), \
                mock.patch("minidm.ytdlp.describe",
                           side_effect=RuntimeError(
                               "yt-dlp não encontrado (instale: pip install "
                               "yt-dlp)")):
            code, _, err = self._run(["spotify", TRACK_URL,
                                      "--data-dir", self.tmp])
        self.assertEqual(code, 1)
        self.assertIn("yt-dlp não encontrado", err)
        self.assertEqual(self._tasks(), [])

    def test_start_delega_para_o_inline(self):
        with mock.patch("minidm.spotify.requests.get",
                        return_value=http(200, text=embed_html(ENT_TRACK))), \
                mock.patch("minidm.ytdlp.describe",
                           return_value=SEARCH_RESULT), \
                mock.patch.object(cli, "_run_inline",
                                  return_value=0) as run:
            code, _, err = self._run(["spotify", TRACK_URL, "--start",
                                      "--data-dir", self.tmp])
        self.assertEqual(code, 0, err)
        run.assert_called_once()
        self.assertEqual(run.call_args[0][2],
                         [t.id for t in self._tasks()])


# --------------------------------------------------------------- API (webui)
class TestServidorAdd(unittest.TestCase):
    """POST /add: recusa no corpo {ok:false, drm, error} (400) que a
    extensão e a webui já sabem interpretar."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-spot-api-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = AppConfig(data_dir=self.tmp)
        self.store = TaskStore(self.tmp)
        self.sched = Scheduler(self.store, self.cfg)   # SEM start()
        self.app = App(self.store, self.sched, self.cfg)
        self.api = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.addCleanup(self._stop_api)
        self.base = f"http://127.0.0.1:{self.api.server_address[1]}"

    def _stop_api(self):
        self.api.shutdown()
        self.api.server_close()

    def _post(self, payload):
        req = urllib.request.Request(
            f"{self.base}/add", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_add_de_track_spotify_recusado(self):
        with mock.patch("minidm.spotify.requests.get") as get:
            status, data = self._post({"url": TRACK_URL})
        get.assert_not_called()
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(data["drm"], "spotify")
        self.assertIn("DMCA", data["error"])
        self.assertIn("minidm spotify", data["error"])
        self.assertEqual(self.store.all(), [])

    def test_recusa_antes_do_modo_playlist_e_do_analyze(self):
        # playlist: a expansão viraria N tarefas que o yt-dlp rejeitaria
        status, data = self._post({"url": PLAYLIST_URL, "playlist": True,
                                   "limit": 50})
        self.assertEqual(status, 400)
        self.assertEqual(data["drm"], "spotify")
        self.assertEqual(self.store.all(), [])

        # kind=auto: a análise nunca chega a fazer requisição nenhuma
        with mock.patch("minidm.spotify.requests.get") as get:
            status, data = self._post({"url": TRACK_URL, "kind": "auto"})
        get.assert_not_called()
        self.assertEqual(status, 400)
        self.assertEqual(data["drm"], "spotify")
        self.assertEqual(self.store.all(), [])

    def test_add_de_url_comum_continua_normal(self):
        # a recusa não pode pegar nada que não seja do Spotify
        status, data = self._post({"url": "https://exemplo.com/arq.mp4",
                                   "filename": "arq.mp4"})
        self.assertEqual(status, 201, data)
        self.assertEqual(len(self.store.all()), 1)


if __name__ == "__main__":
    unittest.main()
