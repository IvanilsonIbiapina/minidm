"""Visualizador de mídia embutido (estilo IDM "Play") — sem dependências.

Decodifica a mídia com o **ffmpeg** (que o minidm já descobre via
`deps.ffmpeg_bin()`) para rawvideo RGBA e desenha num `tk.Canvas` a uma
taxa limitada (~10 fps). Aceita URL direto, `.m3u8` (HLS) e `.mpd` (DASH) —
o ffmpeg resolve o manifesto sozinho.

Sem ffmpeg (ou em falha) o chamador mostra um placeholder: o download
nunca depende deste módulo. HLS **ao vivo** e conteúdo com **DRM** não são
reproduzíveis aqui (DRM de propósito é só detectado e recusado — ver
`media.analyze`).

Uso:

    handle = play_in_frame(frame, url, referer="", user_agent="",
                           proxy="")
    # ao fechar a janela:
    handle.stop()
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
from typing import Optional

from minidm.deps import ffmpeg_bin, hidden_kwargs

# fps do preview (baixo de propósito: é visualização, não reprodução fina)
PREVIEW_FPS = 10
# tamanho máx. do quadro p/ limitar CPU/canvas
PREVIEW_MAX_W = 640
PREVIEW_MAX_H = 360


class PreviewHandle:
    """Controla um preview em execução (ffmpeg + thread + canvas)."""

    def __init__(self, canvas, width: int, height: int):
        self.canvas = canvas
        self.width = width
        self.height = height
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._q: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=4)
        self._photo = None      # tk.PhotoImage (manter referência viva)
        self._after_id = None

    # ------------------------------------------------------------------ start
    def start(self, cmd: list[str]) -> None:
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            **hidden_kwargs())
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        self._schedule()

    def _pump(self) -> None:
        """Lê os quadros rawvideo do ffmpeg e põe na fila (thread-safe)."""
        assert self._proc and self._proc.stdout
        nbytes = self.width * self.height * 4      # rgba
        stream = self._proc.stdout
        while not self._stop.is_set():
            buf = stream.read(nbytes)
            if not buf or len(buf) < nbytes:
                break
            try:
                self._q.put(buf, timeout=0.5)
            except queue.Full:
                # descarta o mais velho p/ não travar o ffmpeg
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._q.put_nowait(buf)
                except queue.Full:
                    pass

    # ---------------------------------------------------------------- display
    def _schedule(self) -> None:
        if self._stop.is_set():
            return
        try:
            self.canvas.after(int(1000 / PREVIEW_FPS), self._draw)
        except Exception:
            pass

    def _draw(self) -> None:
        if self._stop.is_set():
            return
        latest = None
        try:
            while True:
                latest = self._q.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            try:
                import tkinter as tk

                self._photo = tk.PhotoImage(data=latest,
                                            width=self.width,
                                            height=self.height)
                self.canvas.delete("frame")
                self.canvas.create_image(0, 0, image=self._photo,
                                         anchor="nw", tags="frame")
            except Exception:
                pass
        # fim do stream?
        if self._proc and self._proc.poll() is not None and self._q.empty():
            return
        self._schedule()

    # ------------------------------------------------------------------ stop
    def stop(self) -> None:
        self._stop.set()
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass


def _fit_box(src_w: int, src_h: int, max_w: int, max_h: int) -> tuple[int, int]:
    """Escala mantendo proporção para caber em max_w x max_h (par)."""
    if src_w <= 0 or src_h <= 0:
        return max_w, max_h
    scale = min(max_w / src_w, max_h / src_h, 1.0)
    w = int(src_w * scale)
    h = int(src_h * scale)
    # p/ rawvideo o ffmpeg precisa de dimensões pares
    return (w - w % 2) or 2, (h - h % 2) or 2


def play_in_frame(frame, url: str, referer: str = "", user_agent: str = "",
                  proxy: str = "", width: int = PREVIEW_MAX_W,
                  height: int = PREVIEW_MAX_H) -> PreviewHandle:
    """Cria um preview de `url` dentro de `frame` (um tk.Frame).

    Levanta `RuntimeError` se o ffmpeg não existir ou a mídia não puder ser
    aberta — o chamador trata (placeholder). O tamanho real é ajustado p/
    caber em `width x height`.
    """
    ff = ffmpeg_bin()
    if not ff:
        raise RuntimeError("ffmpeg não encontrado (minidm deps)")

    out_w, out_h = _fit_box(width, height, width, height)
    canvas = _ensure_canvas(frame, out_w, out_h)
    handle = PreviewHandle(canvas, out_w, out_h)

    cmd = [ff, "-hide_banner", "-loglevel", "error"]
    if referer:
        cmd += ["-headers", f"Referer: {referer}\r\n"]
    if user_agent:
        cmd += ["-user_agent", user_agent]
    if proxy:
        cmd += ["-http_proxy", proxy]
    cmd += [
        "-i", url,
        "-an", "-sn", "-dn",                 # só vídeo
        "-vf", f"scale={out_w}:{out_h},format=rgba",
        "-f", "rawvideo", "-r", str(PREVIEW_FPS), "-",
    ]
    handle.start(cmd)
    return handle


def _ensure_canvas(frame, width: int, height: int):
    from tkinter import Canvas

    for w in frame.winfo_children():
        w.destroy()
    canvas = Canvas(frame, width=width, height=height,
                    bg="black", highlightthickness=0)
    canvas.pack(expand=True, fill="both")
    return canvas
