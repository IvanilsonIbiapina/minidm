"""Testes de mídia: parsing HLS/DASH + download de stream + API /media/analyze.

Servidor local serve playlists e segmentos sintéticos (inclui HLS com
AES-128 e DASH com SegmentTemplate/SegmentTimeline).

Roda com:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from minidm.config import AppConfig
from minidm.media import (analyze, build_headers,
                          parse_duration_iso, parse_m3u8, parse_mpd,
                          parse_attrs, suggest_extension)
from minidm.meta import Task, TaskState
from minidm.stream import StreamDownloader, expand_template, make_downloader, timeline_units


# ------------------------------------------------------------- playlists
def make_segments(prefix, count, size=1024, seed=0):
    """Gerador determinístico de 'segmentos' (bytes pseudoaleatórios)."""
    out = []
    for i in range(count):
        out.append(bytes((seed + i + j) % 256 for j in range(size)))
    return out


# segmentos por variante
SEG_360 = make_segments("360", 4, 1024, seed=10)
SEG_720 = make_segments("720", 4, 2048, seed=50)
SEG_ENC = make_segments("enc", 3, 512, seed=90)   # em claro
SEG_DASH = make_segments("dash", 5, 1500, seed=200)
DASH_INIT = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 40

# chave AES de teste (16 bytes) + IV fixo
AES_KEY = bytes(range(16))
AES_IV = bytes(range(16, 32))


def _pad_pkcs7(data: bytes, block=16) -> bytes:
    n = block - (len(data) % block)
    return data + bytes([n]) * n


def _encrypt_aes128(data: bytes, key= AES_KEY, iv=AES_IV) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(_pad_pkcs7(data)) + enc.finalize()


MASTER_M3U8 = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=640x360,CODECS="avc1.4d401e"
v/360.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=1280x720,CODECS="avc1.4d401f"
v/720.m3u8
"""

MEDIA_360 = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:4.0,
seg360_0.ts
#EXTINF:4.0,
seg360_1.ts
#EXTINF:4.0,
seg360_2.ts
#EXTINF:4.0,
seg360_3.ts
#EXT-X-ENDLIST
"""

MEDIA_720 = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXTINF:4.0,
seg720_0.ts
#EXTINF:4.0,
seg720_1.ts
#EXTINF:4.0,
seg720_2.ts
#EXTINF:4.0,
seg720_3.ts
#EXT-X-ENDLIST
"""

MEDIA_ENC = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x101112131415161718191a1b1c1d1e1f
#EXTINF:4.0,
enc0.ts
#EXTINF:4.0,
enc1.ts
#EXTINF:4.0,
enc2.ts
#EXT-X-ENDLIST
"""

DASH_MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT20S" profiles="urn:mpeg:dash:profile:isoff-live:2011">
  <Period id="0">
    <AdaptationSet mimeType="video/mp4" segmentAlignment="true">
      <Representation id="video_720" codecs="avc1.64001f" bandwidth="1400000"
                      width="1280" height="720">
        <SegmentTemplate timescale="1000" duration="4000" startNumber="1"
                         media="chunk_$Number$.m4s" initialization="init.mp4"/>
      </Representation>
      <Representation id="video_360" codecs="avc1.4d401e" bandwidth="500000"
                      width="640" height="360">
        <SegmentTemplate timescale="1000" duration="4000" startNumber="1"
                         media="chunk_$Number$.m4s" initialization="init.mp4"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

DASH_TIMELINE_MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT12S">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v" bandwidth="800000" width="640" height="360">
        <SegmentTemplate timescale="1000" initialization="init.mp4"
                         media="seg_$Time$.m4s">
          <SegmentTimeline>
            <S t="0" d="4000" r="2"/>
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

# master com Widevine (SESSION-KEY) e faixa de áudio separada
MASTER_DRM_M3U8 = """#EXTM3U
#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,URI="data:text/plain;base64,AAAA",KEYFORMAT="com.widevine.alpha"
#EXT-X-MEDIA:TYPE=AUDIO,NAME="português",LANGUAGE="pt-BR",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=1280x720,CODECS="avc1.64001f"
v/720.m3u8
"""

# master sem DRM mas com faixa de áudio (música)
MASTER_AUDIO_M3U8 = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,NAME="português",LANGUAGE="pt-BR",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=1280x720,CODECS="avc1.64001f"
v/720.m3u8
"""

MEDIA_AUDIO = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXTINF:4.0,
aud0.ts
#EXTINF:4.0,
aud1.ts
#EXT-X-ENDLIST
"""

# media playlist com DRM FairPlay (SAMPLE-AES + KEYFORMAT Apple)
MEDIA_DRM_M3U8 = """#EXTM3U
#EXT-X-VERSION:5
#EXT-X-TARGETDURATION:4
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://licença",KEYFORMAT="com.apple.streamingkeydelivery"
#EXTINF:4.0,
enc0.ts
#EXT-X-ENDLIST
"""

DASH_DRM_MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT8S">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>
      <Representation id="v720" bandwidth="1400000" width="1280" height="720">
        <SegmentTemplate timescale="1000" duration="4000" startNumber="1"
                         media="chunk_$Number$.m4s" initialization="init.mp4"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

DASH_AUDIO_MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT8S">
  <Period>
    <AdaptationSet mimeType="audio/mp4">
      <Representation id="a128" bandwidth="128000" codecs="mp4a.40.2">
        <SegmentTemplate timescale="1000" duration="4000" startNumber="1"
                         media="audio_$Number$.m4s" initialization="init_a.mp4"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


class MediaHandler(BaseHTTPRequestHandler):
    """Serve playlists + segmentos; registra headers recebidos."""

    last_headers = {}
    fail_next = 0
    _lock = threading.Lock()

    def log_message(self, *a):
        pass

    def _remember(self):
        # playlists e chave nunca "falham" — o retry testado é o de segmentos
        if self.path.endswith((".m3u8", ".mpd", "key.bin")):
            with MediaHandler._lock:
                MediaHandler.last_headers[self.path] = dict(self.headers)
            return True
        with MediaHandler._lock:
            MediaHandler.last_headers[self.path] = dict(self.headers)
            if MediaHandler.fail_next > 0:
                MediaHandler.fail_next -= 1
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
        return True

    def _send(self, body: bytes, ctype="application/octet-stream"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._remember():
            return
        p = self.path
        base = "http://127.0.0.1:%d" % self.server.server_address[1]

        if p == "/hls/master.m3u8":
            self._send(MASTER_M3U8.encode(), "application/vnd.apple.mpegurl")
        elif p == "/hls/master-audio.m3u8":
            self._send(MASTER_AUDIO_M3U8.encode(),
                       "application/vnd.apple.mpegurl")
        elif p == "/hls/master-drm.m3u8":
            self._send(MASTER_DRM_M3U8.encode(),
                       "application/vnd.apple.mpegurl")
        elif p == "/hls/drm.m3u8":
            self._send(MEDIA_DRM_M3U8.encode(), "application/vnd.apple.mpegurl")
        elif p == "/hls/audio.m3u8":
            self._send(MEDIA_AUDIO.encode(), "application/vnd.apple.mpegurl")
        elif p == "/hls/v/360.m3u8":
            self._send(MEDIA_360.encode(), "application/vnd.apple.mpegurl")
        elif p == "/hls/v/720.m3u8":
            self._send(MEDIA_720.encode(), "application/vnd.apple.mpegurl")
        elif p == "/hls/enc.m3u8":
            self._send(MEDIA_ENC.encode(), "application/vnd.apple.mpegurl")
        elif p == "/hls/key.bin":
            self._send(AES_KEY, "application/octet-stream")
        elif p.startswith("/dash/manifest.mpd"):
            self._send(DASH_MPD.encode(), "application/dash+xml")
        elif p.startswith("/dash/timeline.mpd"):
            self._send(DASH_TIMELINE_MPD.encode(), "application/dash+xml")
        elif p.startswith("/dash/drm.mpd"):
            self._send(DASH_DRM_MPD.encode(), "application/dash+xml")
        elif p.startswith("/dash/audio.mpd"):
            self._send(DASH_AUDIO_MPD.encode(), "application/dash+xml")
        elif p == "/dash/init.mp4" or p == "/dash/init_a.mp4":
            self._send(DASH_INIT, "video/mp4")
        elif p.startswith("/file-named.bin"):
            # servidor manda o nome real via Content-Disposition
            body = b"arquivo com nome no header" * 50
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Content-Disposition",
                'attachment; filename="Meu Video Legal.mp4"')
            self.end_headers()
            self.wfile.write(body)
        elif p.startswith("/file.bin"):
            self._send(b"arquivo direto de teste" * 100, "video/mp4")
        else:
            # segmentos por nome: seg360_N / seg720_N / enc_N / chunk_N
            name = p.rsplit("/", 1)[-1]
            body = self._segment_body(name)
            if body is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(body, "video/mp2t")

    def _segment_body(self, name):
        import re as _re
        m = _re.match(r"seg360_(\d+)\.ts$", name)
        if m:
            return SEG_360[int(m.group(1))]
        m = _re.match(r"seg720_(\d+)\.ts$", name)
        if m:
            return SEG_720[int(m.group(1))]
        m = _re.match(r"enc(\d)\.ts$", name)
        if m:
            return _encrypt_aes128(SEG_ENC[int(m.group(1))])
        m = _re.match(r"aud(\d)\.ts$", name)
        if m:
            return b"A" * 512
        m = _re.match(r"chunk_0*(\d+)\.m4s$", name)
        if m:
            return b"X" * 100
        return None


def serve_media():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MediaHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_config(**kw) -> AppConfig:
    base = dict(retries=2, backoff_base=0.05, backoff_max=0.1,
                verify_signatures=False, remote_checksums=False,
                max_connections=4)
    base.update(kw)
    return AppConfig(**base)


class MediaBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.base = serve_media()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-media-")
        MediaHandler.fail_next = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ----------------------------------------------------------------- parsing
class TestParsing(MediaBase):
    def test_parse_attrs_com_aspas_e_virgulas(self):
        a = parse_attrs('BANDWIDTH=1280000,RESOLUTION=720x480,CODECS="a,b"')
        self.assertEqual(a["BANDWIDTH"], "1280000")
        self.assertEqual(a["CODECS"], "a,b")

    def test_parse_master_lista_variantes_ordenadas_por_url(self):
        m = parse_m3u8(MASTER_M3U8, f"{self.base}/hls/master.m3u8")
        self.assertEqual(m["type"], "master")
        self.assertEqual(len(m["variants"]), 2)
        v0, v1 = m["variants"]
        self.assertEqual(v0["height"], 360)
        self.assertEqual(v1["height"], 720)
        self.assertEqual(v1["bandwidth"], 1400000)
        # URLs relativas resolvidas contra a do master
        self.assertTrue(v0["url"].endswith("/hls/v/360.m3u8"))

    def test_parse_media_conta_segmentos_e_duracao(self):
        m = parse_m3u8(MEDIA_360, f"{self.base}/hls/v/360.m3u8")
        self.assertEqual(m["type"], "media")
        self.assertEqual(len(m["segments"]), 4)
        self.assertAlmostEqual(m["duration"], 16.0)
        self.assertTrue(m["endlist"])
        self.assertFalse(m["encrypted"])
        self.assertTrue(m["segments"][0]["url"].endswith("seg360_0.ts"))

    def test_parse_media_captura_chave_aes(self):
        m = parse_m3u8(MEDIA_ENC, f"{self.base}/hls/enc.m3u8")
        self.assertTrue(m["encrypted"])
        key = m["segments"][0]["key"]
        self.assertEqual(key["method"], "AES-128")
        self.assertTrue(key["uri"].endswith("key.bin"))
        self.assertEqual(key["iv"], "0x101112131415161718191a1b1c1d1e1f")

    def test_parse_mpd_extrai_representacoes(self):
        d = parse_mpd(DASH_MPD, f"{self.base}/dash/manifest.mpd")
        self.assertEqual(d["type"], "dash")
        self.assertAlmostEqual(d["duration"], 20.0)
        reps = d["representations"]
        self.assertEqual(len(reps), 2)
        by_id = {r["id"]: r for r in reps}
        self.assertEqual(by_id["video_720"]["height"], 720)
        tpl = by_id["video_720"]["template"]
        self.assertEqual(tpl["media"], "chunk_$Number$.m4s")
        self.assertEqual(tpl["duration"], 4000)
        # init resolvido
        self.assertTrue(by_id["video_720"]["init"]["url"].endswith("init.mp4"))

    def test_expand_template_numero_com_zeros(self):
        url = expand_template("chunk_$Number%05d$.m4s", number=7)
        self.assertEqual(url, "chunk_00007.m4s")
        url2 = expand_template("seg_$Time$_$Bandwidth$.m4s", t=4000,
                               bandwidth=1400000)
        self.assertEqual(url2, "seg_4000_1400000.m4s")

    def test_timeline_units_expande_repeat(self):
        units = timeline_units([{"t": 0, "d": 4000, "r": 2}], 12.0, 1000)
        self.assertEqual(units, [0, 4000, 8000])

    def test_parse_duration_iso(self):
        self.assertAlmostEqual(parse_duration_iso("PT1H2M3.5S"), 3723.5)
        self.assertAlmostEqual(parse_duration_iso("PT20S"), 20.0)
        self.assertIsNone(parse_duration_iso("nada"))

    def test_suggest_extension(self):
        self.assertEqual(suggest_extension("hls"), ".ts")
        self.assertEqual(suggest_extension("hls", {"url": "x.mp4"}), ".mp4")
        self.assertEqual(suggest_extension("dash"), ".mp4")
        self.assertEqual(suggest_extension("file"), "")

    def test_build_headers_monta_referer_e_cookie(self):
        h = build_headers("http://site/", "UA-X", "a=1; b=2")
        self.assertEqual(h["Referer"], "http://site/")
        self.assertEqual(h["User-Agent"], "UA-X")
        self.assertEqual(h["Cookie"], "a=1; b=2")


# ---------------------------------------------------------------- analyze
class TestAnalyze(MediaBase):
    def test_analyze_master_retorna_qualidades(self):
        r = analyze(f"{self.base}/hls/master.m3u8")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["kind"], "hls-master")
        labels = [q["label"] for q in r["qualities"]]
        self.assertIn("720p (1280x720)", labels)
        self.assertIn("360p (640x360)", labels)

    def test_analyze_media_playlist_estima_tamanho(self):
        r = analyze(f"{self.base}/hls/v/360.m3u8")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["kind"], "hls-media")
        self.assertAlmostEqual(r["duration"], 16.0)
        # 4 segmentos x 1024 bytes
        self.assertEqual(r["qualities"][0]["estimated_size"], 4 * 1024)

    def test_analyze_dash_lista_representacoes(self):
        r = analyze(f"{self.base}/dash/manifest.mpd")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["kind"], "dash")
        labels = [q["label"] for q in r["qualities"]]
        self.assertIn("720p (1280x720)", labels)

    def test_analyze_arquivo_direto(self):
        r = analyze(f"{self.base}/file.bin")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["kind"], "file")
        self.assertTrue(r["qualities"][0]["estimated_size"])

    def test_analyze_erro_http(self):
        r = analyze(f"{self.base}/inexistente.xyz")
        # servidor responde 404 -> analyze reports erro
        self.assertFalse(r["ok"])
        self.assertIn("404", r["error"])


# ------------------------------------------------ DRM, nome e áudio (v4)
class TestDrmNameAudio(MediaBase):
    """DRM só é detectado (nunca contornado); nome vem do Content-Disposition
    ou do título da página; faixa de áudio vira qualidade baixável."""

    # ------------------------------------------------------------ DRM
    def test_drm_hls_session_key_no_master(self):
        r = analyze(f"{self.base}/hls/master-drm.m3u8")
        self.assertFalse(r["ok"])
        self.assertEqual(r["drm"], "widevine")
        self.assertIn("DRM", r["error"])

    def test_drm_hls_key_fairplay(self):
        r = analyze(f"{self.base}/hls/drm.m3u8")
        self.assertFalse(r["ok"])
        self.assertEqual(r["drm"], "fairplay")

    def test_drm_dash_content_protection(self):
        r = analyze(f"{self.base}/dash/drm.mpd")
        self.assertFalse(r["ok"])
        self.assertEqual(r["drm"], "widevine")

    def test_aes128_comum_nao_e_drm(self):
        # AES-128 puro (sem DRM) continua baixável
        r = analyze(f"{self.base}/hls/enc.m3u8")
        self.assertTrue(r["ok"], r)

    def test_stream_downloader_recusa_drm(self):
        from minidm.stream import StreamError

        for url, kind in ((f"{self.base}/hls/master-drm.m3u8", "hls"),
                          (f"{self.base}/dash/drm.mpd", "dash")):
            task = Task(url=url, dest_dir=self.tmp, filename="x", kind=kind)
            with self.assertRaises(StreamError):
                make_downloader(task, test_config()).run()
            self.assertIn("DRM", task.error)

    def test_parse_master_propaga_drm_para_variantes(self):
        pl = parse_m3u8(MASTER_DRM_M3U8, "http://x/hls/master-drm.m3u8")
        self.assertEqual(pl["drm"], "widevine")
        self.assertTrue(all(v["drm"] == "widevine" for v in pl["variants"]))

    def test_parse_mpd_drm_por_representacao(self):
        dash = parse_mpd(DASH_DRM_MPD, "http://x/dash/drm.mpd")
        self.assertEqual(dash["drm"], "widevine")
        self.assertEqual(dash["representations"][0]["drm"], "widevine")

    def test_uuid_playready_reconhecido(self):
        from minidm.media import drm_from_content_protection
        self.assertEqual(
            drm_from_content_protection(
                "urn:uuid:9a04f079-9840-4286-ab92-e65be0885f95"),
            "playready")

    # ------------------------------------------------------- nome de arquivo
    def test_nome_vem_do_content_disposition(self):
        r = analyze(f"{self.base}/file-named.bin")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["filename"], "Meu Video Legal.mp4")

    def test_nome_usa_titulo_da_pagina_quando_url_e_generica(self):
        r = analyze(f"{self.base}/hls/master.m3u8",
                    page_title="Aula de Física — UnB (2026)")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["filename"], "Aula de Física — UnB (2026)")

    def test_nome_url_nao_generica_vence_titulo(self):
        # o nome real do arquivo (enc) é melhor que o título da página
        r = analyze(f"{self.base}/hls/enc.m3u8", page_title="Página X")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["filename"], "enc")

    def test_suggest_name_remove_extensao_de_playlist(self):
        from minidm.media import _suggest_name
        self.assertEqual(
            _suggest_name("http://cdn/filme.m3u8?tk=1"), "filme")

    def test_suggest_name_descarta_stem_de_hash(self):
        from minidm.media import _suggest_name
        # nome tipo hash no URL -> cai no título da página
        got = _suggest_name("http://cdn/0123456789abcdef01234567.m3u8",
                            page_title="Vídeo bonito")
        self.assertEqual(got, "Vídeo bonito")

    def test_suggest_name_descarta_codigo_de_cdn(self):
        from minidm.media import _suggest_name
        # código de CDN sem vogais (x36xhzz) não é nome de arquivo
        got = _suggest_name("http://cdn/x36xhzz/x36xhzz.m3u8",
                            page_title="Big Buck Bunny")
        self.assertEqual(got, "Big Buck Bunny")

    def test_suggest_name_url_de_download_generico(self):
        from minidm.media import _suggest_name
        # get.php?id=9 (URL de download comum) não serve de nome
        got = _suggest_name("http://site/get.php?id=9", page_title="Música")
        self.assertEqual(got, "Música")

    # --------------------------------------------------------------- áudio
    def test_hls_faixa_de_audio_vira_qualidade(self):
        r = analyze(f"{self.base}/hls/master-audio.m3u8")
        self.assertTrue(r["ok"], r)
        audio = [q for q in r["qualities"] if q.get("audio_only")]
        self.assertEqual(len(audio), 1)
        self.assertIn("áudio", audio[0]["label"])
        self.assertIn("português", audio[0]["label"])

    def test_dash_audio_only_marcado(self):
        r = analyze(f"{self.base}/dash/audio.mpd")
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["qualities"][0].get("audio_only"))

    def test_download_faixa_de_audio_hls(self):
        task = Task(url=f"{self.base}/hls/audio.m3u8", dest_dir=self.tmp,
                    filename="musica.ts", kind="hls")
        d = make_downloader(task, test_config())
        d.run()
        self.assertTrue(os.path.exists(task.final_path))
        self.assertEqual(os.path.getsize(task.final_path), 1024)


# ------------------------------------------------------- download de stream
class TestStreamDownload(MediaBase):
    def _task(self, url, filename="video", **kw):
        return Task(url=url, dest_dir=self.tmp, filename=filename, **kw)

    def test_download_hls_master_baixa_maior_qualidade(self):
        # master -> variante 720 (maior bandwidth) -> 4 segs x 2048
        task = self._task(f"{self.base}/hls/master.m3u8", kind="hls")
        StreamDownloader(task, test_config()).run()
        self.assertEqual(task.state, TaskState.DONE)
        esperado = b"".join(SEG_720)
        with open(task.final_path, "rb") as f:
            self.assertEqual(f.read(), esperado)
        self.assertEqual(task.sha256, hashlib.sha256(esperado).hexdigest())
        # extensão .ts acrescentada pelo downloader
        self.assertTrue(task.filename.endswith(".ts"))
        # plano registrou 4 segmentos
        self.assertEqual(task.stream["segments_total"], 4)
        self.assertEqual(task.stream["segments_done"], 4)

    def test_download_hls_media_direto(self):
        task = self._task(f"{self.base}/hls/v/360.m3u8", kind="hls")
        StreamDownloader(task, test_config()).run()
        self.assertEqual(task.state, TaskState.DONE)
        with open(task.final_path, "rb") as f:
            self.assertEqual(f.read(), b"".join(SEG_360))

    def test_download_hls_aes128_decifra(self):
        task = self._task(f"{self.base}/hls/enc.m3u8", kind="hls")
        StreamDownloader(task, test_config()).run()
        self.assertEqual(task.state, TaskState.DONE)
        with open(task.final_path, "rb") as f:
            self.assertEqual(f.read(), b"".join(SEG_ENC))

    def test_download_dash_segment_template(self):
        task = self._task(f"{self.base}/dash/manifest.mpd", kind="dash",
                          stream={"variant_index": None})
        # força a representação 720 via plano
        task.stream = {"rep_id": "video_720"}
        StreamDownloader(task, test_config()).run()
        self.assertEqual(task.state, TaskState.DONE)
        self.assertTrue(task.filename.endswith(".mp4"))
        # init + 5 segmentos (duration 20s / 4s = 5)
        tamanho = os.path.getsize(task.final_path)
        self.assertEqual(tamanho, len(DASH_INIT) + 5 * 100)

    def test_retomada_pula_segmentos_ja_baixados(self):
        url = f"{self.base}/hls/v/360.m3u8"
        task = self._task(url, kind="hls")
        # roda uma vez pra popular o plano
        dl = StreamDownloader(task, test_config())
        dl._prepare()
        segs_dir = dl._segs_dir()
        # simula que 2 segmentos já foram baixados
        os.makedirs(segs_dir, exist_ok=True)
        for i in range(2):
            with open(dl._seg_path(i), "wb") as f:
                f.write(SEG_360[i])
            task.segments[i].received = 1
        task.stream["segments_done"] = 2
        task.stream["bytes_done"] = 2048
        task.save_meta()

        # reexecuta: deve completar só os 2 restantes
        task2 = Task.load_meta(task.meta_path)
        task2.state = TaskState.QUEUED
        StreamDownloader(task2, test_config()).run()
        self.assertEqual(task2.state, TaskState.DONE)
        with open(task2.final_path, "rb") as f:
            self.assertEqual(f.read(), b"".join(SEG_360))

    def test_retry_recupera_de_500(self):
        MediaHandler.fail_next = 2
        task = self._task(f"{self.base}/hls/v/360.m3u8", kind="hls")
        StreamDownloader(task, test_config(retries=5)).run()
        self.assertEqual(task.state, TaskState.DONE)

    def test_erro_de_rede_vira_error(self):
        MediaHandler.fail_next = 10_000
        task = self._task(f"{self.base}/hls/v/360.m3u8", kind="hls")
        with self.assertRaises(Exception):
            StreamDownloader(task, test_config(retries=1)).run()
        self.assertEqual(task.state, TaskState.ERROR)

    def test_factory_escolhe_stream_downloader(self):
        t1 = self._task("http://x/a.m3u8", kind="hls")
        t2 = self._task("http://x/a.bin", kind="file")
        self.assertIsInstance(make_downloader(t1, test_config()),
                              StreamDownloader)
        from minidm.downloader import Downloader
        self.assertIsInstance(make_downloader(t2, test_config()), Downloader)

    def test_headers_incluem_referer(self):
        task = self._task(f"{self.base}/hls/v/360.m3u8", kind="hls",
                          referer=f"{self.base}/pagina")
        dl = StreamDownloader(task, test_config())
        h = dl._headers()
        self.assertEqual(h["Referer"], f"{self.base}/pagina")


if __name__ == "__main__":
    unittest.main()
