"""Bandeja (tray) do desktop — fechar a janela some para a bandeja.

O IDM minimiza para a bandeja do Windows; aqui o mesmo com Win32 puro
via ctypes (sem dependências):

* janela de mensagens própria numa thread separada;
* ícone com menu "Abrir minidm" / "Sair";
* as ações voltam para a GUI por uma FILA (`poll`), lida na thread do
  tkinter via `after` — nunca tocamos tkinter de outra thread;
* fora do Windows (ou se algo falhar), `show()` devolve False e a GUI
  mantém o fechar normal (degradação graciosa).

Uso (gui.py):

    tray = TrayIcon()
    if tray.show():
        ... agenda `tray.poll()` de 300 em 300 ms ...
"""

from __future__ import annotations

import ctypes
import os
import queue
import threading

# ações devolvidas à GUI pela bandeja
OPEN = "open"   # reabrir a janela
QUIT = "quit"   # sair de verdade

# ---- mensagens Win32 ------------------------------------------------
WM_APP = 0x8000
WM_TRAY = WM_APP + 1        # callback do ícone: LOWORD(lParam) = msg do mouse
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205       # abre o menu (não é ação em si)
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_NULL = 0x0000
CMD_OPEN = 1                # item "Abrir minidm" do menu
CMD_QUIT = 2                # item "Sair"

IDI_APPLICATION = 32512     # ícone padrão do sistema
NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
MF_STRING, MF_SEPARATOR = 0x0, 0x80
TPM_LEFTALIGN, TPM_BOTTOMALIGN, TPM_RIGHTBUTTON = 0x0, 0x20, 0x2

# ---- tipos ctypes (valem em qualquer SO p/ importar o módulo) --------
_LRESULT = ctypes.c_ssize_t
_PTR = ctypes.c_void_p
_WNDPROC_TYPE = ctypes.WINFUNCTYPE if os.name == "nt" else ctypes.CFUNCTYPE
_WNDPROC = _WNDPROC_TYPE(_LRESULT, _PTR, ctypes.c_uint,
                         ctypes.c_size_t, ctypes.c_ssize_t)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint32),
        ("lpfnWndProc", _PTR),          # WNDPROC (guardada em self._wndproc_ref)
        ("cbClsExtra", ctypes.c_int32),
        ("cbWndExtra", ctypes.c_int32),
        ("hInstance", _PTR),
        ("hIcon", _PTR),
        ("hCursor", _PTR),
        ("hbrBackground", _PTR),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", _PTR),
        ("message", ctypes.c_uint32),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _NOTIFYICONDATAW(ctypes.Structure):
    """NOTIFYICONDATAW (V2) — o sistema valida o cbSize."""
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("hWnd", _PTR),
        ("uID", ctypes.c_uint32),
        ("uFlags", ctypes.c_uint32),
        ("uCallbackMessage", ctypes.c_uint32),
        ("hIcon", _PTR),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_uint32),
        ("dwStateMask", ctypes.c_uint32),
        ("szInfo", ctypes.c_wchar * 256),
        ("uTimeoutOrVersion", ctypes.c_uint32),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_uint32),
        ("guidItem", ctypes.c_uint8 * 16),
        ("hBalloonIcon", _PTR),
    ]


def action_for(message: int, code: int = 0) -> str | None:
    """Traduz uma Win32 da bandeja em ação (puro, sem tocar em Win32).

    * `WM_TRAY` com clique/duplo-clique ESQUERDO do ícone → OPEN
      (clique direito abre o menu — tratado no wndproc → None);
    * `WM_COMMAND` com id do item do menu → OPEN/QUIT.
    """
    if message == WM_TRAY:
        return OPEN if code in (WM_LBUTTONDBLCLK, WM_LBUTTONUP) else None
    if message == WM_COMMAND:
        if code == CMD_OPEN:
            return OPEN
        if code == CMD_QUIT:
            return QUIT
    return None


_WIN32 = None   # cache por processo


def _win32():
    """user32/shell32/kernel32 com argtypes/restype certos p/ x64."""
    global _WIN32
    if _WIN32 is not None:
        return _WIN32
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    u, s, k = ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t
    i = ctypes.c_int
    si = ctypes.c_ssize_t

    user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    user32.RegisterClassW.restype = ctypes.c_uint16
    user32.CreateWindowExW.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p,
                                       ctypes.c_wchar_p, ctypes.c_uint32,
                                       i, i, i, i, u, u, u, u]
    user32.CreateWindowExW.restype = u
    user32.DefWindowProcW.argtypes = [u, k, k, si]
    user32.DefWindowProcW.restype = si
    user32.PostQuitMessage.argtypes = [i]
    user32.PostQuitMessage.restype = None
    user32.PostMessageW.argtypes = [u, k, k, si]
    user32.PostMessageW.restype = i
    user32.GetMessageW.argtypes = [ctypes.c_void_p, u, k, k]
    user32.GetMessageW.restype = i
    user32.TranslateMessage.argtypes = [ctypes.c_void_p]
    user32.TranslateMessage.restype = i
    user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
    user32.DispatchMessageW.restype = si
    user32.LoadIconW.argtypes = [u, u]
    user32.LoadIconW.restype = u
    user32.CreatePopupMenu.argtypes = []
    user32.CreatePopupMenu.restype = u
    user32.AppendMenuW.argtypes = [u, k, k, ctypes.c_wchar_p]
    user32.AppendMenuW.restype = i
    user32.SetForegroundWindow.argtypes = [u]
    user32.SetForegroundWindow.restype = i
    user32.GetCursorPos.argtypes = [ctypes.c_void_p]
    user32.GetCursorPos.restype = i
    user32.TrackPopupMenu.argtypes = [u, k, i, i, i, u, ctypes.c_void_p]
    user32.TrackPopupMenu.restype = i
    user32.DestroyMenu.argtypes = [u]
    user32.DestroyMenu.restype = i
    shell32.Shell_NotifyIconW.argtypes = [k, ctypes.c_void_p]
    shell32.Shell_NotifyIconW.restype = i
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetModuleHandleW.restype = u

    _WIN32 = (user32, shell32, kernel32)
    return _WIN32


class TrayIcon:
    """Ícone de bandeja; fora do Windows (ou em falha) vira no-op."""

    def __init__(self, tip: str = "minidm") -> None:
        self._tip = tip
        self._actions: "queue.Queue[str]" = queue.Queue()
        self._hwnd: int | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._visible = False
        self._wndproc_ref = None      # mantém o callback vivo (GC do ctypes)

    @property
    def visible(self) -> bool:
        return self._visible

    def show(self) -> bool:
        """Cria o ícone na bandeja. False = SO sem suporte ou falha."""
        if os.name != "nt":
            return False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="minidm-tray")
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            return False
        return self._visible

    def poll(self) -> str | None:
        """Próxima ação (OPEN/QUIT) ou None — chamar na thread do tkinter."""
        try:
            return self._actions.get_nowait()
        except queue.Empty:
            return None

    def destroy(self) -> None:
        """Remove o ícone e encerra a thread da bandeja (idempotente)."""
        hwnd, self._hwnd = self._hwnd, None
        if hwnd is not None and os.name == "nt":
            try:
                ctypes.windll.user32.PostMessageW(hwnd, WM_DESTROY, 0, 0)
            except Exception:
                pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        self._visible = False

    # ------------------------------------------------------------ internos
    def _on_wndproc(self, hwnd, msg, wparam, lparam) -> int:
        if msg == WM_TRAY:
            code = lparam & 0xFFFF
            if code == WM_RBUTTONUP:
                self._popup(hwnd)
            act = action_for(WM_TRAY, code)
            if act:
                self._actions.put(act)
            return 0
        if msg == WM_COMMAND:
            act = action_for(WM_COMMAND, wparam & 0xFFFF)
            if act:
                self._actions.put(act)
            return 0
        if msg == WM_DESTROY:
            ctypes.windll.user32.PostQuitMessage(0)
            return 0
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup(self, hwnd) -> None:
        """Menu de contexto do ícone (aberto no clique direito)."""
        user32, _, _ = _win32()
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return
        try:
            user32.AppendMenuW(hmenu, MF_STRING, CMD_OPEN, "Abrir minidm")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, CMD_QUIT, "Sair")
            pt = _POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(hwnd)
            user32.TrackPopupMenu(hmenu,
                                  TPM_LEFTALIGN | TPM_BOTTOMALIGN |
                                  TPM_RIGHTBUTTON,
                                  pt.x, pt.y, 0, hwnd, None)
            # sem WM_NULL depois, o menu não some ao clicar fora (MSDN)
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(hmenu)

    def _run(self) -> None:
        """Loop de mensagens da thread da bandeja."""
        shell32 = None
        nid = None
        try:
            user32, shell32, kernel32 = _win32()
            hinst = kernel32.GetModuleHandleW(None)
            self._wndproc_ref = _WNDPROC(self._on_wndproc)
            wc = _WNDCLASSW()
            wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p)
            wc.hInstance = hinst
            wc.lpszClassName = "minidm-tray"
            wc.lpszMenuName = ""
            user32.RegisterClassW(ctypes.byref(wc))   # 2ª vez: já existe
            hwnd = user32.CreateWindowExW(0, "minidm-tray", "minidm-tray",
                                          0, 0, 0, 0, 0, None, None, hinst,
                                          None)
            if not hwnd:
                return
            self._hwnd = hwnd
            nid = _NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = user32.LoadIconW(None, IDI_APPLICATION)
            nid.szTip = self._tip[:127]
            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                return
            self._visible = True
            self._ready.set()
            msg = _MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            pass
        finally:
            self._visible = False
            if nid is not None and shell32 is not None:
                try:
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
                except Exception:
                    pass
            self._hwnd = None
            self._ready.set()
