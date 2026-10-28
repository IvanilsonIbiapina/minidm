# -*- coding: utf-8 -*-
"""Testes do mux com ffmpeg na engine própria (limitação #1).

O que era a limitação "baixa uma faixa por vez, sem mux": agora a análise
pareia a melhor faixa de áudio (DASH AdaptationSet separado / HLS
EXT-X-MEDIA), a engine baixa as DUAS faixas e o `ffmpeg -c copy` junta.

Cobre: pareamento de áudio na análise, dica de qualidade (rep_id) no
resolve do MPD, `attach_mux_hints` (CLI/API direta), a fábrica com/sem
ffmpeg e o `MuxDownloader` ponta a ponta com ffmpeg SIMULADO (o pipeline
inteiro roda de verdade; só o binário do ffmpeg é trocado por um fake
que concatena os dois inputs — os segmentos dos testes não são mídia
real e o ffmpeg real os rejeitaria).

Rodar com:  python -m unittest discover -s tests -p "test_mux.py"
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from minidm.config import AppConfig
from minidm.media import analyze, attach_mux_hints
from minidm.meta import Task, TaskState
from minidm.stream import (MuxDownloader, StreamDownloader, StreamError,
                           make_downloader)


# --------------------------------------------------------------- fixtures
# MPD com vídeo (2 representações) e ÁUDIO em AdaptationSet separado —
# exatamente o formato que a engine antiga baixaria mudo.
MUX_MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT8S" profiles="urn:mpeg:dash:profile:isoff-live:2011">
  <Period id="0">
    <AdaptationSet mimeType="video/mp4" segmentAlignment="true">
      <Representation id="video_720" codecs="avc1.64001f" bandwidth="1400000"
                      width="1280" height="720">
        <SegmentTemplate timescale="1000" duration="4000" startNumber="1"
                         media="vchunk_$Number$.m4s" initialization="vinit.mp4"/>
      </Representation>
      <Representation id="video_360" codecs="avc1.4d401e" bandwidth="500000"
                      width="640" height="360">
        <SegmentTemplate timescale="1000" duration="4000" startNumber="1"
                         media="v360_$Number$.m4s" initialization="vinit360.mp4"/>
      </Representation>
    </AdaptationSet>
    <AdaptationSet mimeType="audio/mp4" segmentAlignment="true">
      <Representation id="audio_128" codecs="mp4a.40.2" bandwidth="128000">
        <SegmentTemplate timescale="1000" duration="4000" startNumber="1"
                         media="achunk_$Number$.m4s" initialization="vinit_a.mp4"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

# MPD só de vídeo (nenhuma faixa de áudio separada -> sem par de mux)
MUX_VIDEO_ONLY_MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT8S">
  <Period id="0">
    <AdaptationSet mimeType="video/mp4">
      <Representation id="video_720" codecs="avc1.64001f" bandwidth="1400000"
                      width="1280" height="720">
        <SegmentTemplate timescale="1000" duration="4000" startNumber="1"
                         media="vchunk_$Number$.m4s" initialization="vinit.mp4"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

# master HLS com faixa de áudio separada (EXT-X-MEDIA)
MUX_MASTER_M3U8 = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="português",LANGUAGE="pt-BR",DEFAULT=YES,URI="aud.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=1280x720,CODECS="avc1.64001f",AUDIO="aud"
hi.m3u8
"""

MUX_HI_M3U8 = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXTINF:4.0,
hi0.ts
#EXTINF:4.0,
hi1.ts
#EXT-X-ENDLIST
"""

MUX_AUD_M3U8 = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXTINF:4.0,
aud0.ts
#EXTINF:4.0,
aud1.ts
#EXT-X-ENDLIST
"""

# conteúdos determinísticos (não é mídia real — por isso o ffmpeg é fake)
VINIT = b"VINIT" * 4                      # 20 bytes
VC1, VC2 = b"V" * 100, b"W" * 100         # segmentos de vídeo
AINIT = b"AINIT" * 2                      # 10 bytes
AC1, AC2 = b"A" * 50, b"B" * 50           # segmentos de áudio
HI0, HI1 = b"H" * 120, b"I" * 120         # variante HLS
AU0, AU1 = b"x" * 60, b"y" * 60           # áudio HLS

VIDEO_FINAL = VINIT + VC1 + VC2
AUDIO_FINAL = AINIT + AC1 + AC2


def _fake_ffmpeg(cmd, **kw):
    """`ffmpeg` simulado: concatena os dois `-i` no arquivo de saída."""
    video, audio, out = cmd[6], cmd[8], cmd[-1]
    with open(video, "rb") as fv, open(audio, "rb") as fa:
        data = fv.read() + fa.read()
    with open(out, "wb") as fo:
        fo.write(data)
    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


class MuxHandler(BaseHTTPRequestHandler):
    """Serve o manifesto mux + segmentos; conta requisições por caminho."""

    hits: dict = {}
    fail_next = 0
    _lock = threading.Lock()

    def log_message(self, *a):
        pass

    def _count(self, p):
        with MuxHandler._lock:
            MuxHandler.hits[p] = MuxHandler.hits.get(p, 0) + 1
            if not p.endswith((".m3u8", ".mpd")) and MuxHandler.fail_next > 0:
                MuxHandler.fail_next -= 1
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
        p = self.path
        if not self._count(p):
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p == "/mux/manifest.mpd":
            self._send(MUX_MPD.encode(), "application/dash+xml")
        elif p == "/mux/video-only.mpd":
            self._send(MUX_VIDEO_ONLY_MPD.encode(), "application/dash+xml")
        elif p == "/mux/master.m3u8":
            self._send(MUX_MASTER_M3U8.encode(),
                       "application/vnd.apple.mpegurl")
        elif p == "/mux/hi.m3u8":
            self._send(MUX_HI_M3U8.encode(), "application/vnd.apple.mpegurl")
        elif p == "/mux/aud.m3u8":
            self._send(MUX_AUD_M3U8.encode(), "application/vnd.apple.mpegurl")
        elif p == "/mux/vinit.mp4":
            self._send(VINIT, "video/mp4")
        elif p == "/mux/vinit360.mp4":
            self._send(VINIT, "video/mp4")
        elif p == "/mux/vinit_a.mp4":
            self._send(AINIT, "video/mp4")
        elif p in ("/mux/vchunk_1.m4s", "/mux/v360_1.m4s"):
            self._send(VC1, "video/mp4")
        elif p in ("/mux/vchunk_2.m4s", "/mux/v360_2.m4s"):
            self._send(VC2, "video/mp4")
        elif p == "/mux/achunk_1.m4s":
            self._send(AC1, "audio/mp4")
        elif p == "/mux/achunk_2.m4s":
            self._send(AC2, "audio/mp4")
        elif p == "/mux/hi0.ts":
            self._send(HI0, "video/mp2t")
        elif p == "/mux/hi1.ts":
            self._send(HI1, "video/mp2t")
        elif p == "/mux/aud0.ts":
            self._send(AU0, "video/mp2t")
        elif p == "/mux/aud1.ts":
            self._send(AU1, "video/mp2t")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


def test_config(**kw) -> AppConfig:
    base = dict(retries=1, backoff_base=0.02, backoff_max=0.05,
                verify_signatures=False, remote_checksums=False,
                max_connections=4)
    base.update(kw)
    return AppConfig(**base)


class MuxBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), MuxHandler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-mux-")
        MuxHandler.hits = {}
        MuxHandler.fail_next = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------- helpers
    @staticmethod
    def fake_ffmpeg_patches():
        """ffmpeg binário fake + subprocess.run simulado (concatena)."""
        return (patch("minidm.deps.ffmpeg_bin",
                      return_value=os.path.join(os.sep, "fake", "ffmpeg.exe")),
                patch("minidm.stream.subprocess.run",
                      side_effect=_fake_ffmpeg))

    def mux_task(self, **kw) -> Task:
        base = dict(url=f"{self.base}/mux/manifest.mpd", dest_dir=self.tmp,
                    filename="aula", kind="dash",
                    stream={"rep_id": "video_720", "media": "video"},
                    mux_audio={"url": f"{self.base}/mux/manifest.mpd",
                               "kind": "dash", "rep_id": "audio_128",
                               "media": "audio"})
        base.update(kw)
        return Task(**base)


# --------------------------------------------- análise: pareamento áudio
class TestPareamentoAudio(MuxBase):
    def test_dash_pareia_melhor_audio_em_todas_as_qualidades(self):
        r = analyze(f"{self.base}/mux/manifest.mpd")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["kind"], "dash")
        videos = [q for q in r["qualities"]
                  if not (q.get("dash") or {}).get("mimeType", "")
                  .startswith("audio")]
        self.assertEqual(len(videos), 2)          # 720 + 360
        for q in videos:
            self.assertIsInstance(q.get("audio"), dict)
            a = q["audio"]
            self.assertEqual(a["kind"], "dash")
            self.assertEqual(a["rep_id"], "audio_128")
            self.assertEqual(a["media"], "audio")
            self.assertEqual(a["url"], f"{self.base}/mux/manifest.mpd")

    def test_dash_video_only_nao_tem_par(self):
        r = analyze(f"{self.base}/mux/video-only.mpd")
        self.assertTrue(r["ok"], r)
        for q in r["qualities"]:
            self.assertIsNone(q.get("audio"),
                              "sem faixa de áudio não pode haver par")

    def test_hls_master_pareia_faixa_do_ext_x_media(self):
        r = analyze(f"{self.base}/mux/master.m3u8")
        self.assertTrue(r["ok"], r)
        videos = [q for q in r["qualities"] if not q.get("audio_only")]
        self.assertTrue(videos)
        for q in videos:
            a = q.get("audio")
            self.assertIsInstance(a, dict, "variante sem par de áudio")
            self.assertEqual(a["kind"], "hls")
            self.assertTrue(a["url"].endswith("/mux/aud.m3u8"))


# --------------------------------------- dica de qualidade + attach hints
class TestHintsDeQualidade(MuxBase):
    def test_resolve_dash_com_hint_de_audio_so_baixa_audio(self):
        task = Task(url=f"{self.base}/mux/manifest.mpd", dest_dir=self.tmp,
                    filename="so-audio", kind="dash",
                    stream={"rep_id": "audio_128", "media": "audio"})
        dl = StreamDownloader(task, test_config())
        dl._prepare()
        urls = [i["url"] for i in dl._items]
        self.assertTrue(all("achunk" in u or "vinit_a" in u for u in urls),
                        f"perna de áudio baixou vídeo: {urls}")
        self.assertFalse(any("vchunk" in u for u in urls))

    def test_resolve_dash_sem_hint_escolhe_rep_do_plano(self):
        task = Task(url=f"{self.base}/mux/manifest.mpd", dest_dir=self.tmp,
                    filename="v360", kind="dash",
                    stream={"rep_id": "video_360", "media": "video"})
        dl = StreamDownloader(task, test_config())
        dl._prepare()
        urls = [i["url"] for i in dl._items]
        # video_360 usa template PRÓPRIO (v360_*) — prova que a escolha
        # veio do rep_id, e não de um chute pela maior banda
        self.assertTrue(all("v360" in u or "vinit360" in u for u in urls),
                        f"rep errado: {urls}")

    def test_attach_mux_hints_seta_rep_e_par_com_ffmpeg(self):
        task = Task(url=f"{self.base}/mux/manifest.mpd", dest_dir=self.tmp,
                    filename="x", kind="dash")
        with patch("minidm.deps.ffmpeg_available", return_value=True):
            ok = attach_mux_hints(task, task.url)
        self.assertTrue(ok)
        self.assertEqual(task.stream.get("rep_id"), "video_720")
        self.assertEqual(task.stream.get("media"), "video")
        self.assertEqual(task.mux_audio.get("rep_id"), "audio_128")
        self.assertEqual(task.mux_audio.get("media"), "audio")
        # melhor qualidade marcada (estilo IDM mostra o rótulo)
        self.assertIn("720", task.quality_label)

    def test_attach_mux_hints_sem_ffmpeg_so_escolhe_rep(self):
        task = Task(url=f"{self.base}/mux/manifest.mpd", dest_dir=self.tmp,
                    filename="x", kind="dash")
        with patch("minidm.deps.ffmpeg_available", return_value=False):
            ok = attach_mux_hints(task, task.url)
        self.assertTrue(ok)
        self.assertEqual(task.stream.get("rep_id"), "video_720")
        self.assertIsNone(task.mux_audio, "sem ffmpeg não pode marcar mux")

    def test_attach_mux_hints_falha_e_silenciosa(self):
        task = Task(url=f"{self.base}/mux/manifest.mpd", dest_dir=self.tmp,
                    filename="x", kind="dash")
        with patch("minidm.media.analyze", side_effect=RuntimeError("rede")):
            self.assertFalse(attach_mux_hints(task, task.url))
        self.assertIsNone(task.stream)
        self.assertIsNone(task.mux_audio)

    def test_attach_mux_hints_recusa_drm(self):
        task = Task(url="http://x/drm.mpd", dest_dir=self.tmp,
                    filename="x", kind="dash")
        with patch("minidm.media.analyze",
                   return_value={"ok": False, "drm": "widevine",
                                 "error": "DRM"}):
            self.assertFalse(attach_mux_hints(task, task.url))
        self.assertIsNone(task.mux_audio)


# ------------------------------------------------------------- fábrica
class TestFabricaMux(MuxBase):
    def test_fabrica_entrega_mux_quando_ha_ffmpeg(self):
        task = self.mux_task()
        with patch("minidm.deps.ffmpeg_available", return_value=True):
            dl = make_downloader(task, test_config())
        self.assertIsInstance(dl, MuxDownloader)

    def test_fabrica_sem_ffmpeg_cai_no_downloader_de_antes(self):
        task = self.mux_task()
        with patch("minidm.deps.ffmpeg_available", return_value=False):
            dl = make_downloader(task, test_config())
        self.assertIsInstance(dl, StreamDownloader,
                              "sem ffmpeg tem que baixar 1 faixa, não quebrar")

    def test_fabrica_sem_par_de_audio_e_stream_normal(self):
        task = self.mux_task(mux_audio=None)
        with patch("minidm.deps.ffmpeg_available", return_value=True):
            dl = make_downloader(task, test_config())
        self.assertIsInstance(dl, StreamDownloader)


# ----------------------------------------------- MuxDownloader ponta a ponta
class TestMuxDownload(MuxBase):
    def test_dash_mux_baixa_duas_faixas_e_mescla(self):
        task = self.mux_task()
        p_bin, p_run = self.fake_ffmpeg_patches()
        with p_bin, p_run as run:
            dl = MuxDownloader(task, test_config())
            dl.run()

        self.assertEqual(task.state, TaskState.DONE, task.error)
        # ffmpeg chamado com -c copy e os dois inputs mapeados
        cmd = run.call_args[0][0]
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertIn("0:v:0", cmd)
        self.assertIn("1:a:0", cmd)
        # arquivo final = vídeo + áudio (o que o fake "mesclou")
        with open(task.final_path, "rb") as f:
            self.assertEqual(f.read(), VIDEO_FINAL + AUDIO_FINAL)
        self.assertEqual(task.sha256,
                         hashlib.sha256(VIDEO_FINAL + AUDIO_FINAL).hexdigest())
        # as duas faixas de verdade foram baixadas
        for p in ("/mux/vchunk_1.m4s", "/mux/vchunk_2.m4s",
                  "/mux/achunk_1.m4s", "/mux/achunk_2.m4s"):
            self.assertGreaterEqual(MuxHandler.hits.get(p, 0), 1, p)
        # temporários do áudio limpos; mux_audio consumido
        self.assertIsNone(task.mux_audio)
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "aula.audio.mp4")))
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "aula.audio.mp4.meta.json")))
        # barra completa (vídeo + áudio espelhados)
        self.assertEqual(task.stream["segments_done"],
                         task.stream["segments_total"])

    def test_hls_mux_com_faixa_do_ext_x_media(self):
        task = Task(url=f"{self.base}/mux/master.m3u8", dest_dir=self.tmp,
                    filename="serie", kind="hls",
                    mux_audio={"url": f"{self.base}/mux/aud.m3u8",
                               "kind": "hls"})
        p_bin, p_run = self.fake_ffmpeg_patches()
        with p_bin, p_run:
            MuxDownloader(task, test_config()).run()

        self.assertEqual(task.state, TaskState.DONE, task.error)
        self.assertTrue(task.filename.endswith(".ts"), task.filename)
        with open(task.final_path, "rb") as f:
            self.assertEqual(f.read(), HI0 + HI1 + AU0 + AU1)

    def test_retomada_pula_a_perna_de_video_ja_pronta(self):
        task = self.mux_task()
        # simula crash DEPOIS do vídeo: arquivo pronto + sidecar marcado
        task.filename = "aula.mp4"           # extensão que o prepare daria
        with open(task.final_path, "wb") as f:
            f.write(VIDEO_FINAL)
        task.mux_audio = dict(task.mux_audio, video_done=True)
        task.save_meta()

        # recarrega como o scheduler faria na nova execução (novo processo)
        task2 = Task.load_meta(task.meta_path)
        task2.state = TaskState.QUEUED
        p_bin, p_run = self.fake_ffmpeg_patches()
        with p_bin, p_run:
            MuxDownloader(task2, test_config()).run()

        self.assertEqual(task2.state, TaskState.DONE, task2.error)
        # o vídeo NÃO foi rebaixado
        self.assertIsNone(MuxHandler.hits.get("/mux/vchunk_1.m4s"),
                          "vídeo já pronto foi rebaixado")
        self.assertIsNone(MuxHandler.hits.get("/mux/vinit.mp4"))
        # o áudio foi
        self.assertGreater(MuxHandler.hits.get("/mux/achunk_1.m4s", 0), 0)
        with open(task2.final_path, "rb") as f:
            self.assertEqual(f.read(), VIDEO_FINAL + AUDIO_FINAL)

    def test_erro_na_perna_de_audio_vira_error_na_tarefa(self):
        task = self.mux_task()
        # vídeo já pronto: o que falha é a PERNA DE ÁUDIO (chunk 500)
        task.filename = "aula.mp4"
        with open(task.final_path, "wb") as f:
            f.write(VIDEO_FINAL)
        task.mux_audio = dict(task.mux_audio, video_done=True)
        task.save_meta()
        task2 = Task.load_meta(task.meta_path)
        task2.state = TaskState.QUEUED

        MuxHandler.fail_next = 9999          # só chunks falham; plano ok
        dl = MuxDownloader(task2, test_config(retries=1))
        with self.assertRaises((StreamError, Exception)):
            dl.run()
        # o estado tem que ser ERROR na tarefa PRINCIPAL (scheduler lê ela)
        self.assertEqual(task2.state, TaskState.ERROR, task2.state)
        self.assertTrue(task2.error)

    def test_pausa_entre_fases_preserva_progresso(self):
        task = self.mux_task()
        # vídeo pronto, falta áudio+mescla: pausa antes da fase 2
        task.filename = "aula.mp4"
        with open(task.final_path, "wb") as f:
            f.write(VIDEO_FINAL)
        task.mux_audio = dict(task.mux_audio, video_done=True)
        task.save_meta()

        task2 = Task.load_meta(task.meta_path)
        task2.state = TaskState.QUEUED
        dl = MuxDownloader(task2, test_config())
        dl.request_stop()                    # pausa pedida antes de rodar
        with self.assertRaises(StreamError):
            dl.run()
        self.assertEqual(task2.state, TaskState.PAUSED, task2.state)
        # o que já existia não sumiu
        self.assertTrue(os.path.exists(task2.final_path))
        # retomada de verdade: sem stop conclui
        p_bin, p_run = self.fake_ffmpeg_patches()
        with p_bin, p_run:
            task3 = Task.load_meta(task2.meta_path)
            task3.state = TaskState.QUEUED
            MuxDownloader(task3, test_config()).run()
        self.assertEqual(task3.state, TaskState.DONE, task3.error)

    def test_mux_sem_binario_ffmpeg_vira_erro_claro(self):
        task = self.mux_task()
        with patch("minidm.deps.ffmpeg_bin", return_value=None):
            dl = MuxDownloader(task, test_config())
            with self.assertRaises(StreamError):
                dl.run()
        self.assertEqual(task.state, TaskState.ERROR)
        self.assertIn("ffmpeg", task.error or "")


# --------------------------------------------- API /add com payload mux
class TestApiAddMux(MuxBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import json as _json
        import urllib.request
        from minidm.scheduler import Scheduler
        from minidm.server import App, make_handler
        from minidm.store import TaskStore

        cls._json = _json
        cls._urllib = urllib.request
        cls.tmp_api = tempfile.mkdtemp(prefix="minidm-mux-api-")
        cfg = test_config(data_dir=cls.tmp_api)
        cls.store = TaskStore(cls.tmp_api)
        # scheduler SEM workers: os testes usam start=False e queremos
        # inspecionar a tarefa como foi criada (sem corrida do download)
        cls.sched = Scheduler(cls.store, cfg)
        app = App(cls.store, cls.sched, cfg)
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        threading.Thread(target=cls.api.serve_forever, daemon=True).start()
        cls.api_base = f"http://127.0.0.1:{cls.api.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        cls.api.server_close()
        shutil.rmtree(cls.tmp_api, ignore_errors=True)
        super().tearDownClass()

    def _post(self, payload):
        req = self._urllib.Request(
            f"{self.api_base}/add",
            data=self._json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with self._urllib.urlopen(req, timeout=30) as r:
            return r.status, self._json.loads(r.read().decode())

    def test_add_com_rep_id_e_audio_do_cliente(self):
        # fluxo do popup: qualidade já escolhida -> rep_id + par de áudio
        with patch("minidm.deps.ffmpeg_available", return_value=True):
            status, data = self._post({
                "url": f"{self.base}/mux/manifest.mpd",
                "kind": "dash",
                "quality_url": f"{self.base}/mux/manifest.mpd",
                "rep_id": "video_360",
                "audio": {"url": f"{self.base}/mux/manifest.mpd",
                          "kind": "dash", "rep_id": "audio_128",
                          "media": "audio"},
                "filename": "api-mux", "start": False})
        self.assertEqual(status, 201, data)
        task = self.store.get(data["id"])
        self.assertEqual(task.stream.get("rep_id"), "video_360")
        self.assertEqual(task.stream.get("media"), "video")
        self.assertEqual(task.mux_audio.get("rep_id"), "audio_128")

    def test_add_sem_ffmpeg_nao_guarda_o_par(self):
        with patch("minidm.deps.ffmpeg_available", return_value=False):
            status, data = self._post({
                "url": f"{self.base}/mux/manifest.mpd",
                "kind": "dash",
                "quality_url": f"{self.base}/mux/manifest.mpd",
                "rep_id": "video_360",
                "audio": {"url": f"{self.base}/mux/manifest.mpd",
                          "kind": "dash", "rep_id": "audio_128",
                          "media": "audio"},
                "filename": "sem-ffmpeg", "start": False})
        self.assertEqual(status, 201, data)
        task = self.store.get(data["id"])
        self.assertEqual(task.stream.get("rep_id"), "video_360")
        self.assertIsNone(task.mux_audio)


if __name__ == "__main__":
    unittest.main()
