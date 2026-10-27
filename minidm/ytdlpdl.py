"""Downloader que delega ao yt-dlp (CLI externa, GPL-3.0 — sem copiar código).

Usado quando o perfil manda (`use_ytdlp_downloader`) ou a config global
liga `use_ytdlp_downloader`, e o yt-dlp está instalado. A engine própria
(HLS/DASH/arquivo) continua sendo o fallback.

Interface igual aos outros downloaders (scheduler não muda):
`run()`, `request_stop()`, `speed_bps()`. O progresso vem do parse das
linhas `[download] 12.3% of ~10.00MiB at 1.00MiB/s ETA 00:10`.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from typing import Optional

from minidm.config import AppConfig
from minidm.meta import Task, TaskState
from minidm.ytdlp import find as find_ytdlp

# [download]  12.3% of ~ 10.00MiB at  1.00MiB/s ETA 00:10
_PROGRESS_RE = re.compile(
    r"\[download\]\s+([\d.]+)%\s+of\s+~?\s*([\d.]+)([KMG]?i?B)")
_SIZE_UNITS = {"B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3,
               "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}


class YtdlpError(Exception):
    pass


class YtdlpDownloader:
    def __init__(self, task: Task, config: Optional[AppConfig] = None,
                 extra_args: Optional[list[str]] = None, **_kw):
        self.task = task
        self.config = config or AppConfig()
        self.extra_args = list(extra_args or [])
        self._proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self._received = 0
        self._total: Optional[int] = None
        self._speed = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------- interface
    def run(self) -> Task:
        t = self.task
        cmd = self._build_cmd()
        t.state = TaskState.DOWNLOADING
        t.error = None
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, cwd=t.dest_dir or None)
            assert self._proc.stdout is not None
            tail: list[str] = []
            for line in self._proc.stdout:
                if self._stop.is_set():
                    break
                self._parse_line(line)
                tail.append(line.rstrip())
                if len(tail) > 30:
                    tail.pop(0)
            rc = self._proc.wait()
            if self._stop.is_set():
                t.state = TaskState.PAUSED
                t.save_meta()
                return t
            if rc != 0:
                t.state = TaskState.ERROR
                t.error = (f"yt-dlp saiu com código {rc}: "
                           f"{' | '.join(tail[-3:])}")
                t.save_meta()
                raise YtdlpError(t.error)
            self._finalize()
            return t
        except OSError as e:
            t.state = TaskState.ERROR
            t.error = f"yt-dlp não rodou: {e}"
            t.save_meta()
            raise YtdlpError(t.error) from e

    def request_stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def speed_bps(self, window: float = 5.0) -> float:
        with self._lock:
            return self._speed

    # ----------------------------------------------------------------- cmd
    def _build_cmd(self) -> list[str]:
        cmd = find_ytdlp()
        if not cmd:
            raise YtdlpError("yt-dlp não encontrado")
        t = self.task
        args = list(cmd)
        args += ["--newline", "--no-playlist", "--no-mtime"]
        # saída: o yt-dlp escolhe a extensão; usamos nosso stem
        stem = os.path.splitext(t.filename)[0] or "download"
        args += ["-o", os.path.join(t.dest_dir or ".", stem + ".%(ext)s")]
        if t.referer:
            args += ["--add-header", f"Referer:{t.referer}"]
        if t.user_agent or self.config.user_agent:
            args += ["--user-agent", t.user_agent or self.config.user_agent]
        if t.cookie or self.config.cookie:
            args += ["--add-header",
                     f"Cookie:{t.cookie or self.config.cookie}"]
        if t.proxy or self.config.proxy:
            args += ["--proxy", t.proxy or self.config.proxy]
        args += self.extra_args
        args += list(getattr(t, "ytdlp_args", []) or [])
        args.append(t.url)
        return args

    # -------------------------------------------------------------- parsing
    def _parse_line(self, line: str) -> None:
        m = _PROGRESS_RE.search(line)
        if not m:
            return
        pct = float(m.group(1))
        total = float(m.group(2)) * _SIZE_UNITS.get(m.group(3), 1)
        with self._lock:
            self._total = int(total)
            self._received = int(total * pct / 100.0)
            self._speed = 0.0     # o yt-dlp já controla banda própria
        t = self.task
        t.total_size = self._total
        # received/progress vêm do stream dict para casar com a UI
        t.stream = {"bytes_done": self._received,
                    "segments_total": 100,
                    "segments_done": int(pct)}

    # ------------------------------------------------------------ finalizar
    def _finalize(self) -> None:
        t = self.task
        # acha o arquivo real que o yt-dlp gravou (extensão escolhida por ele)
        base = os.path.splitext(t.filename)[0]
        found = None
        try:
            for name in os.listdir(t.dest_dir or "."):
                if name.startswith(base) and not name.endswith(
                        (".part", ".ytdl", ".meta")):
                    found = name
                    break
        except OSError:
            pass
        if found:
            t.filename = found
            t.total_size = os.path.getsize(
                os.path.join(t.dest_dir or ".", found))
        # sha-256 do arquivo (mesmo contrato dos outros downloaders)
        try:
            from minidm.verify import sha256_file
            path = os.path.join(t.dest_dir or ".", t.filename)
            if os.path.exists(path):
                t.sha256 = sha256_file(path)
        except Exception:
            pass
        t.state = TaskState.DONE
        t.finished_at = time.time()
        t.stream = None
        t.save_meta()
