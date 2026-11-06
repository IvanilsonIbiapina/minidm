"""Observador da área de transferência (Windows, sem dependências extras).

Usa ctypes para ler o clipboard a cada N segundos; quando aparece um link
http(s) novo, a função `callback` é chamada.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Callable

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

if os.name == "nt":
    # API Win32 do clipboard — fora do Windows (CLI/daemon/server em
    # Linux ou macOS) o módulo importa sem quebrar e o observador
    # apenas não enxerga nada (`read_clipboard_text` devolve None).
    import ctypes
    import ctypes.wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.CloseClipboard.restype = ctypes.c_bool
    user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    user32.IsClipboardFormatAvailable.restype = ctypes.c_bool
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p

    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
else:
    user32 = kernel32 = None

URL_RE = re.compile(r"https?://[^\s\"'<>()]+")


def read_clipboard_text() -> str | None:
    if user32 is None:
        return None
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def find_urls(text: str) -> list[str]:
    return URL_RE.findall(text or "")


def watch(callback: Callable[[str], None], interval: float = 2.0,
          once: bool = False,
          stop: "threading.Event | None" = None) -> None:
    """Polling do clipboard. `callback(url)` para cada link novo.

    `stop`: se fornecido, encerra quando o Event for setado (usado pela GUI
    para desligar o observador sem matar a thread).
    """
    if stop is None:
        stop = threading.Event()

    seen: set[str] = set()
    # semeia com o que já está no clipboard agora para não disparar na largada
    for u in find_urls(read_clipboard_text() or ""):
        seen.add(u)
    while not stop.is_set():
        for url in find_urls(read_clipboard_text() or ""):
            if url not in seen:
                seen.add(url)
                callback(url)
        if once:
            return
        stop.wait(interval)
