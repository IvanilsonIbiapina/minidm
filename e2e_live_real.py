# -*- coding: utf-8 -*-
"""E2E real do HLS AO VIVO: segmentos GERADOS pelo ffmpeg + loop de re-fetch.

Fluxo:
  1. ffmpeg gera 4 segmentos `.ts` reais de vídeo (testsrc2 -> libx264) e
     de áudio (sine -> aac), cortados com timestamps CONTÍNUOS;
  2. servidor local serve `/live-v.m3u8` e `/live-a.m3u8` como playlist
     AO VIVO — janela deslizante, SEM `#EXT-X-ENDLIST` — e anuncia o fim
     só quando a janela alcança o último segmento;
  3. `analyze` real sinaliza ** AO VIVO **;
  4. `make_downloader` entrega `MuxDownloader` -> as DUAS pernas gravam AO
     MESMO TEMPO e o ffmpeg REAL mescla (`-c copy`);
  5. ffprobe confirma as DUAS pistas no arquivo final.

Rodar:  python e2e_live_real.py
"""
from __future__ import annotations

import glob
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, r"C:\Users\ivani\projects\minidm")

from minidm.config import AppConfig
from minidm.deps import ffmpeg_bin, hidden_kwargs
from minidm.media import analyze
from minidm.meta import Task
from minidm.stream import MuxDownloader, make_downloader

SEGDIR = ""
FEEDS: dict = {}          # caminho da playlist -> Feed


class Feed:
    """Playlist ao vivo: janela deslizante que termina no último segmento."""

    def __init__(self, prefix: str, count: int, window: int = 2):
        self.prefix, self.count, self.window = prefix, count, window
        self.seq = 0
        self.polls = 0
        self.lock = threading.Lock()

    def playlist(self) -> str:
        with self.lock:
            seq = min(self.seq, max(0, self.count - self.window))
            win = list(range(seq, min(self.count, seq + self.window)))
            self.polls += 1
            self.seq += 1
            lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                     "#EXT-X-TARGETDURATION:2",
                     f"#EXT-X-MEDIA-SEQUENCE:{win[0]}"]
            for i in win:
                lines += ["#EXTINF:2.0,", f"{self.prefix}_{i}.ts"]
            if win[-1] >= self.count - 1:
                lines.append("#EXT-X-ENDLIST")   # transmissão encerrou
            return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.lstrip("/")
        if p in FEEDS:
            self._send(FEEDS[p].playlist().encode(),
                       "application/vnd.apple.mpegurl")
            return
        name = os.path.basename(p)
        path = os.path.join(SEGDIR, name)
        if os.path.isfile(path):
            with open(path, "rb") as f:
                self._send(f.read(), "video/mp2t")
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def gen_segments(ff: str, work: str) -> dict:
    """Gera segmentos .ts reais (timestamps contínuos, como HLS de verdade)."""
    specs = {
        "v": ["-f", "lavfi", "-i",
              "testsrc2=size=320x240:rate=15:duration=8",
              "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
              "-g", "15", "-keyint_min", "15", "-sc_threshold", "0"],
        "a": ["-f", "lavfi", "-i", "sine=frequency=440:duration=8",
              "-ac", "2", "-c:a", "aac"],
    }
    out = {}
    for prefix, args in specs.items():
        pattern = os.path.join(work, prefix + "_%d.ts")
        subprocess.run(
            [ff, "-y", "-hide_banner", "-loglevel", "error", *args,
             "-f", "segment", "-segment_time", "2", pattern],
            capture_output=True, **hidden_kwargs())
        files = sorted(glob.glob(os.path.join(work, prefix + "_*.ts")),
                       key=lambda p: int(p.rsplit("_", 1)[1].split(".")[0]))
        if len(files) < 3:
            print(f"FAIL: ffmpeg gerou {len(files)} segmentos {prefix}")
            sys.exit(1)
        out[prefix] = files
    return out


def probe_tracks(ff: str, path: str) -> set[str]:
    pr = subprocess.run(
        [os.path.join(os.path.dirname(ff), "ffprobe"), "-v", "error",
         "-show_entries", "stream=codec_type,codec_name", "-of", "csv=p=0",
         path],
        capture_output=True, text=True, **hidden_kwargs())
    lines = [ln.strip() for ln in pr.stdout.splitlines() if ln.strip()]
    return {f for ln in lines for f in ln.split(",") if f in ("video", "audio")}


def main() -> int:
    global SEGDIR
    work = tempfile.mkdtemp(prefix="minidm-e2e-live-")
    out_dir = os.path.join(work, "out")
    os.makedirs(out_dir, exist_ok=True)
    ff = ffmpeg_bin()
    if not ff:
        print("FAIL: ffmpeg nao encontrado")
        return 1

    print("1) gerando segmentos .ts reais (video+audio) ...")
    segs = gen_segments(ff, work)
    SEGDIR = work

    feeds = {"live-v.m3u8": Feed("v", len(segs["v"])),
             "live-a.m3u8": Feed("a", len(segs["a"]))}
    FEEDS.update(feeds)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    print(f"   servidor ao vivo em {base} "
          f"(janela de 2, {len(segs['v'])} segs de video)")

    try:
        print("2) analyze() real ...")
        r = analyze(f"{base}/live-v.m3u8")
        if not r.get("ok") or not r.get("live"):
            print(f"FAIL: analyze nao marcou ao vivo -> {r}")
            return 1
        print(f"   kind={r['kind']} live={r['live']} "
              f"qualidade={r['qualities'][0]['label']!r} "
              f"duracao={r['duration']} tamanho={r['qualities'][0]['estimated_size']}")

        print("3) tarefa + fabrica de downloader ...")
        task = Task(url=f"{base}/live-v.m3u8", dest_dir=out_dir,
                    filename="ao-vivo", kind="hls",
                    mux_audio={"url": f"{base}/live-a.m3u8", "kind": "hls"})
        cfg = AppConfig(verify_signatures=False, remote_checksums=False,
                        retries=2, backoff_base=0.1, backoff_max=0.3,
                        live_poll_seconds=0.2, live_max_seconds=60)
        dl = make_downloader(task, config=cfg)
        if not isinstance(dl, MuxDownloader):
            print(f"FAIL: esperava MuxDownloader, veio {type(dl).__name__}")
            return 1
        print(f"   downloader: {type(dl).__name__} "
              f"(pernas concorrentes por ser ao vivo)")

        print("4) gravando as DUAS pernas e mesclando com ffmpeg real ...")
        t = dl.run()
        print(f"   estado: {t.state} | erro: {t.error}")
        if t.state != "done":
            return 1

        print("5) ffprobe do resultado ...")
        tracks = sorted(probe_tracks(ff, t.final_path))
        with open(t.final_path, "rb") as f:
            blob = f.read()
        st = t.stream or {}
        print(f"   pistas: {tracks} | {len(blob)} bytes | "
              f"segs {st.get('segments_done')}/{st.get('segments_total')} | "
              f"lost={st.get('lost')} | polls v={feeds['live-v.m3u8'].polls} "
              f"a={feeds['live-a.m3u8'].polls}")
        if set(tracks) != {"video", "audio"}:
            print(f"FAIL: esperava video+audio, veio: {tracks}")
            return 1
        if st.get("lost"):
            print(f"FAIL: lost={st.get('lost')} — nada podia se perder")
            return 1
        print(f"OK E2E ao vivo: {t.final_path} ({len(blob)} bytes) "
              f"pistas {tracks} sha256="
              f"{hashlib.sha256(blob).hexdigest()[:16]}")
        print(f"   arquivos mantidos em: {work}")
        return 0
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main())
