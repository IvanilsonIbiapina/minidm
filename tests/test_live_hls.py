"""Testes do HLS AO VIVO (playlist sem #EXT-X-ENDLIST) — limitação #3.

O servidor local serve playlists que CRESCEM a cada re-fetch e, quando
quer, mandam o `#EXT-X-ENDLIST` — exatamente o ciclo de uma transmissão.
Cobre: detecção na análise, loop de re-fetch, segmento perdido (janela
que anda/404), pausa + retomada, limite de duração, intervalo de poll e
mux com as DUAS pernas gravando AO MESMO TEMPO.

Roda com:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from minidm.config import AppConfig
from minidm.media import analyze
from minidm.meta import Task, TaskState
from minidm.stream import MuxDownloader, StreamDownloader, StreamError


# ----------------------------------------------------------- conteúdo
SEG_SIZE = 256


def seg_bytes(n: int, prefix: str = "seg") -> bytes:
    """Bytes determinísticos por (prefixo, sequência)."""
    seed = (sum(ord(c) for c in prefix) + n * 7) % 256
    return bytes((seed + j) % 256 for j in range(SEG_SIZE))


MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=1280x720,CODECS="avc1.64001f"
live.m3u8
"""

VOD_M3U8 = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:4.0,
seg_0.ts
#EXTINF:4.0,
seg_1.ts
#EXTINF:4.0,
seg_2.ts
#EXT-X-ENDLIST
"""


# ------------------------------------------------------------- servidor
class LiveState:
    """Playlist ao vivo controlável: janela, passo, fim, perdas."""

    def __init__(self, prefix: str = "seg", size: int = 3, step: int = 1,
                 end_after: int | None = None):
        self.prefix = prefix
        self.size = size
        self.step = step
        self.end_after = end_after      # nº do poll em que vem o ENDLIST
        self.seq = 0                    # #EXT-X-MEDIA-SEQUENCE atual
        self.polls = 0
        self.windows: list[list[int]] = []
        self.lock = threading.Lock()

    def poll(self) -> tuple[str, bool]:
        with self.lock:
            window = list(range(self.seq, self.seq + self.size))
            self.polls += 1
            ended = (self.end_after is not None
                     and self.polls >= self.end_after)
            self.windows.append(window)
            self.seq += self.step
            lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                     "#EXT-X-TARGETDURATION:2",
                     f"#EXT-X-MEDIA-SEQUENCE:{window[0]}"]
            for n in window:
                lines += ["#EXTINF:2.0,", f"{self.prefix}_{n}.ts"]
            if ended:
                lines.append("#EXT-X-ENDLIST")
            return "\n".join(lines) + "\n", ended

    @property
    def seen_order(self) -> list[int]:
        """Sequências vistas, na ordem em que o downloader as anexaria."""
        out: list[int] = []
        for w in self.windows:
            for n in w:
                if n not in out:
                    out.append(n)
        return out


LIVE: dict[str, LiveState] = {}        # caminho da playlist -> estado
DROP: set[str] = set()                 # segmentos que respondem 404
HITS: dict[str, list[float]] = {}      # prefixo -> horários dos pedidos

SEG_RE = re.compile(r"^/([a-z]+)_(\d+)\.ts$")


class LiveHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _404(self) -> None:
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        p = self.path
        if p in LIVE:
            st = LIVE[p]
            text, _ = st.poll()
            self._send(text.encode(), "application/vnd.apple.mpegurl")
            return
        m = SEG_RE.match(p)
        if m:
            prefix, num = m.group(1), int(m.group(2))
            HITS.setdefault(prefix, []).append(time.monotonic())
            if p in DROP:                # segmento "perdido" (fora do ar)
                self._404()
                return
            self._send(seg_bytes(num, prefix), "video/mp2t")
            return
        if p == "/live-master.m3u8":
            self._send(MASTER.encode(), "application/vnd.apple.mpegurl")
            return
        if p == "/vod.m3u8":
            self._send(VOD_M3U8.encode(), "application/vnd.apple.mpegurl")
            return
        self._404()


def _fake_ffmpeg(cmd, **kw):
    """`ffmpeg` simulado: concatena os dois `-i` no arquivo de saída."""
    video, audio, out = cmd[6], cmd[8], cmd[-1]
    with open(video, "rb") as fv, open(audio, "rb") as fa:
        data = fv.read() + fa.read()
    with open(out, "wb") as fo:
        fo.write(data)
    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


class LiveBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), LiveHandler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-live-")
        LIVE.clear()
        DROP.clear()
        HITS.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------- helpers
    @staticmethod
    def cfg(**kw) -> AppConfig:
        base = dict(retries=1, backoff_base=0.02, backoff_max=0.05,
                    verify_signatures=False, remote_checksums=False,
                    max_connections=4, live_poll_seconds=0.1)
        base.update(kw)
        return AppConfig(**base)

    def state(self, path: str, prefix: str = "seg", **kw) -> LiveState:
        st = LiveState(prefix=prefix, **kw)
        LIVE[path] = st
        return st

    def live_task(self, path: str = "/live.m3u8", **kw) -> Task:
        base = dict(url=f"{self.base}{path}", dest_dir=self.tmp,
                    filename="transmissao", kind="hls")
        base.update(kw)
        return Task(**base)

    @staticmethod
    def expected(st: LiveState) -> bytes:
        return b"".join(seg_bytes(n) for n in st.seen_order)

    @staticmethod
    def read(path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()


# ------------------------------------------------------------- análise
class TestAnaliseAoVivo(LiveBase):
    def test_playlist_de_midia_direta_marca_live(self):
        st = self.state("/live.m3u8", end_after=None)
        r = analyze(f"{self.base}/live.m3u8")
        self.assertTrue(r["ok"])
        self.assertTrue(r["live"], "playlist sem ENDLIST tem de ser ao vivo")
        q = r["qualities"][0]
        self.assertTrue(q["live"])
        self.assertEqual(q["label"], "ao vivo")
        self.assertIsNone(r["duration"], "ao vivo não tem duração ainda")
        self.assertIsNone(q["estimated_size"], "tamanho ainda não existe")
        self.assertGreaterEqual(st.polls, 1)

    def test_vod_continua_com_duracao_e_tamanho(self):
        self.state("/live.m3u8")           # p/ os segmentos do VOD servirem
        r = analyze(f"{self.base}/vod.m3u8")
        self.assertTrue(r["ok"])
        self.assertFalse(r["live"])
        self.assertFalse(r["qualities"][0]["live"])
        self.assertEqual(r["qualities"][0]["label"], "original")
        self.assertAlmostEqual(r["duration"], 12.0)
        self.assertEqual(r["qualities"][0]["estimated_size"],
                         SEG_SIZE * 3)

    def test_master_herda_live_da_variante_sondada(self):
        self.state("/live.m3u8", end_after=None)
        r = analyze(f"{self.base}/live-master.m3u8")
        self.assertTrue(r["ok"])
        self.assertEqual(r["kind"], "hls-master")
        self.assertTrue(r["live"], "a variante sondada não tem ENDLIST")
        for q in r["qualities"]:
            self.assertTrue(q["live"], f"qualidade {q['label']} marcada")


# ------------------------------------------------- loop de re-fetch
class TestLoopAoVivo(LiveBase):
    def test_baixa_ate_o_endlist_e_concatena_tudo(self):
        st = self.state("/live.m3u8", end_after=3)
        task = self.live_task()
        out = StreamDownloader(task, config=self.cfg(),
                               checkpoint_seconds=0.1).run()

        self.assertEqual(out.state, TaskState.DONE)
        self.assertTrue(task.stream["live"])
        self.assertEqual(task.stream["lost"], 0)
        self.assertEqual(task.stream["segments_total"],
                         len(st.seen_order),
                         "total só existe depois do ENDLIST")
        self.assertTrue(task.filename.endswith(".ts"))
        self.assertEqual(self.read(task.final_path), self.expected(st))
        self.assertGreaterEqual(st.polls, 3, "tem de re-buscar a playlist")

    def test_janela_que_salta_vira_lost_e_nao_falha(self):
        st = self.state("/live.m3u8", end_after=None)
        task = self.live_task()
        dl = StreamDownloader(task, config=self.cfg(),
                              checkpoint_seconds=0.1)
        dl._prepare()                      # janela [0,1,2]
        self.assertEqual([s["sequence"] for s in task.stream["items"]],
                         [0, 1, 2])

        st.seq = 10                        # servidor pulou a janela
        added, end = dl._poll_playlist()
        self.assertEqual(added, 3)
        self.assertFalse(end)
        # 3..9 nunca apareceram -> lost, não erro
        self.assertEqual(task.stream["lost"], 7)

    def test_segmento_que_da_404_vira_lost_e_finaliza_done(self):
        st = self.state("/live.m3u8", end_after=2)
        DROP.add("/seg_2.ts")
        task = self.live_task()
        out = StreamDownloader(task, config=self.cfg(),
                               checkpoint_seconds=0.1).run()

        self.assertEqual(out.state, TaskState.DONE)
        self.assertEqual(task.stream["lost"], 1)
        self.assertIsNone(task.error, "perda em ao vivo não é erro")
        esperado = (seg_bytes(0) + seg_bytes(1) + seg_bytes(3))
        self.assertEqual(self.read(task.final_path), esperado)
        self.assertEqual(st.polls, 2)

    def test_intervalo_padrao_e_metade_do_targetduration(self):
        self.state("/live.m3u8", end_after=None)
        task = self.live_task()
        dl = StreamDownloader(task, config=self.cfg(live_poll_seconds=0.0))
        dl._prepare()
        self.assertEqual(dl._live_interval(), 1.0)   # TARGETDURATION 2 -> 1
        dl2 = StreamDownloader(task, config=self.cfg(live_poll_seconds=0.25))
        self.assertEqual(dl2._live_interval(), 0.25)  # config sobrepõe

    def test_limite_de_duracao_encerra_e_para_de_rebuscar(self):
        st = self.state("/live.m3u8", end_after=None)
        task = self.live_task()
        cfg = self.cfg(live_max_seconds=0.6)
        out = StreamDownloader(task, config=cfg,
                               checkpoint_seconds=0.1).run()
        self.assertEqual(out.state, TaskState.DONE)
        self.assertTrue(os.path.exists(task.final_path))

        polls = st.polls
        time.sleep(0.4)
        self.assertEqual(st.polls, polls,
                         "saiu do loop: ninguém pode estar re-buscando")


# ------------------------------------------------------- pausa/retomada
class TestPausaRetomada(LiveBase):
    def test_pausa_no_meio_do_ao_vivo_salva_e_retoma(self):
        st = self.state("/live.m3u8", end_after=None)
        task = self.live_task()
        stop = threading.Event()

        def first_checkpoint(_t):
            stop.set()                    # usuário deu pause no meio

        dl = StreamDownloader(task, config=self.cfg(), stop=stop,
                              on_progress=first_checkpoint,
                              checkpoint_seconds=0.1)
        with self.assertRaises(StreamError):
            dl.run()
        self.assertEqual(task.state, TaskState.PAUSED)
        self.assertTrue(task.stream["items"], "janela anexada ficou salva")
        parcial = list(st.windows)
        self.assertTrue(os.path.exists(task.meta_path))

        # transmissão "volta" e termina logo em seguida
        st.end_after = st.polls + 1
        dl2 = StreamDownloader(task, config=self.cfg(),
                               checkpoint_seconds=0.1)
        out = dl2.run()

        self.assertEqual(out.state, TaskState.DONE)
        self.assertTrue(st.windows[len(parcial):],
                        "retomada re-buscou a playlist")
        self.assertEqual(self.read(task.final_path), self.expected(st),
                         "segmentos da 1ª metade não podem sumir")


# ---------------------------------------------------------------- mux
class TestMuxAoVivo(LiveBase):
    def test_pernas_gravam_ao_mesmo_tempo_e_depois_mescla(self):
        vst = self.state("/lv.m3u8", prefix="v", end_after=3)
        ast = self.state("/la.m3u8", prefix="a", end_after=3)
        task = self.live_task(path="/lv.m3u8", filename="show",
                              mux_audio={"url": f"{self.base}/la.m3u8",
                                         "kind": "hls"})
        p_bin = patch("minidm.deps.ffmpeg_bin",
                      return_value=os.path.join(os.sep, "fake", "ffmpeg.exe"))
        p_run = patch("minidm.stream.subprocess.run",
                      side_effect=_fake_ffmpeg)
        with p_bin, p_run:
            out = MuxDownloader(task, config=self.cfg(),
                                checkpoint_seconds=0.1).run()

        self.assertEqual(out.state, TaskState.DONE)
        self.assertTrue(task.stream["live"])
        self.assertIsNone(task.mux_audio, "após o mux o par é consumido")
        # as DUAS pernas tiveram de rodar juntas: o áudio COMEÇOU antes de
        # o vídeo TERMINAR (em sequência seria o contrário, sem exceção)
        self.assertTrue(HITS.get("v") and HITS.get("a"),
                        "as duas pernas baixaram segmentos")
        self.assertLess(HITS["a"][0], HITS["v"][-1],
                        "áudio e vídeo têm de gravar a mesma janela")
        esperado = (b"".join(seg_bytes(n, "v") for n in vst.seen_order)
                    + b"".join(seg_bytes(n, "a") for n in ast.seen_order))
        self.assertEqual(self.read(task.final_path), esperado)


class TestExibicaoAoVivo(LiveBase):
    def test_snapshot_e_dashboard_mostram_ao_vivo(self):
        from rich.console import Console

        from minidm.dashboard import render
        from minidm.scheduler import Scheduler
        from minidm.store import TaskStore

        store = TaskStore(self.tmp)
        # o construtor pausa "downloading" órfãs — cria a tarefa DEPOIS
        sched = Scheduler(store, self.cfg())
        task = Task(url=f"{self.base}/live.m3u8", dest_dir=self.tmp,
                    filename="transmissao.ts", kind="hls",
                    state=TaskState.DOWNLOADING,
                    stream={"live": True, "lost": 2, "segments_total": 0,
                            "segments_done": 4, "bytes_done": 4096,
                            "base_url": "x", "items": [{"url": "y"}]})
        store.add(task)

        row = sched.snapshot()[0]
        self.assertTrue(row["live"], "o snapshot tem de dizer que é ao vivo")
        self.assertEqual(row["lost"], 2)
        self.assertEqual(row["progress"], 0.0,
                         "sem ENDLIST não existe % — não se inventa")

        buf = Console(record=True, width=220, force_terminal=False,
                      no_color=True)
        buf.print(render([row]))
        text = buf.export_text()
        self.assertIn("ao vivo", text)
        self.assertIn("--:--", text, "ETA sem total é '--:--'")


if __name__ == "__main__":
    unittest.main()
