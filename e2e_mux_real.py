# -*- coding: utf-8 -*-
"""E2E real do mux: mídia GERADA pelo ffmpeg + ffmpeg MESCLANDO de verdade.

Fluxo:
  1. ffmpeg cria video-only.mp4 (testsrc2) e audio-only.mp4 (sine);
  2. um MPD local aponta os dois como BaseURL (DASH com faixas separadas);
  3. `analyze` real pareia áudio -> Task com mux_audio;
  4. `make_downloader` entrega MuxDownloader -> engine baixa as duas
     faixas por HTTP e o ffmpeg REAL mescla (-c copy);
  5. ffprobe confirma que o arquivo final tem as DUAS pistas.

Rodar:  python e2e_mux_real.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, r"C:\Users\ivani\projects\minidm")

from minidm.config import AppConfig
from minidm.deps import ffmpeg_bin, hidden_kwargs
from minidm.media import analyze
from minidm.meta import Task
from minidm.stream import MuxDownloader, make_downloader

MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT2S">
  <Period id="0">
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v" bandwidth="300000" width="320" height="240"
                      codecs="avc1.42c015">
        <BaseURL>video-only.mp4</BaseURL>
      </Representation>
    </AdaptationSet>
    <AdaptationSet mimeType="audio/mp4">
      <Representation id="a" bandwidth="64000" codecs="mp4a.40.2">
        <BaseURL>audio-only.mp4</BaseURL>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


def main() -> int:
    work = tempfile.mkdtemp(prefix="minidm-e2e-mux-")
    out_dir = os.path.join(work, "out")
    os.makedirs(out_dir, exist_ok=True)
    ff = ffmpeg_bin()
    if not ff:
        print("FAIL: ffmpeg nao encontrado")
        return 1

    # 1) duas faixas reais (sem pistas cruzadas)
    print("1) gerando video-only.mp4 e audio-only.mp4 ...")
    r1 = subprocess.run(
        [ff, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
         "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         os.path.join(work, "video-only.mp4")],
        capture_output=True, **hidden_kwargs())
    r2 = subprocess.run(
        [ff, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-ac", "2", "-c:a", "aac",
         os.path.join(work, "audio-only.mp4")],
        capture_output=True, **hidden_kwargs())
    if r1.returncode or r2.returncode:
        print("FAIL gerando midia:", r1.stderr, r2.stderr)
        return 1
    with open(os.path.join(work, "manifest.mpd"), "w", encoding="utf-8") as f:
        f.write(MPD)

    # 2) servidor local
    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=work, **kw)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    mpd_url = f"{base}/manifest.mpd"
    try:
        # 3) análise real
        print("2) analyze() real do MPD ...")
        res = analyze(mpd_url)
        if not res.get("ok"):
            print("FAIL analyze:", res)
            return 1
        q = max(res["qualities"],
                key=lambda x: (1 if x.get("audio_only") else 0,
                               x.get("bandwidth") or 0))
        audio = q.get("audio")
        print("   qualidade:", q["label"], "| kind:", q["kind"],
              "| par de audio:", audio)

        # 4) task como o servidor /add faria + download real
        task = Task(url=q["url"], dest_dir=out_dir, filename="e2e-mux",
                    kind=q["kind"], state="queued")
        if isinstance(audio, dict):
            task.mux_audio = dict(audio)

        cfg = AppConfig(retries=2, verify_signatures=False,
                        remote_checksums=False, max_connections=4)
        dl = make_downloader(task, cfg)
        print("3) downloader:", type(dl).__name__)
        if not isinstance(dl, MuxDownloader):
            print("FAIL: fabrica nao entregou MuxDownloader")
            return 1
        dl.run()
        print("   estado:", task.state, "| erro:", task.error)

        if task.state != "done":
            print("FAIL: estado", task.state, task.error)
            return 1
        final = task.final_path
        if not os.path.exists(final):
            print("FAIL: arquivo final ausente:", final)
            return 1
        if task.mux_audio is not None:
            print("FAIL: mux_audio nao consumido")
            return 1

        # 5) ffprobe: as DUAS pistas no arquivo final?
        print("4) ffprobe do resultado ...")
        pr = subprocess.run(
            [os.path.join(os.path.dirname(ff), "ffprobe.exe"),
             "-v", "error", "-show_entries",
             "stream=codec_type,codec_name", "-of", "csv=p=0", final],
            capture_output=True, text=True, **hidden_kwargs())
        # csv vem na ordem do arquivo (ex.: "h264,video") — pega o tipo
        # entre as duas colunas, não pela posição
        streams = [ln.strip() for ln in pr.stdout.splitlines() if ln.strip()]
        print("   pistas:", streams)
        types = {f for s in streams for f in s.split(",") if f in ("video", "audio")}
        if not ({"video", "audio"} <= types):
            print("FAIL: esperava video+audio, veio:", streams)
            return 1

        size = os.path.getsize(final)
        print(f"\nOK E2E: {final} ({size} bytes) com pistas {sorted(types)}")
        print(f"   sha256={task.sha256[:16]}…  "
              f"mux_audio=consumido  estado=DONE")
        return 0
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
