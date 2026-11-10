# -*- coding: utf-8 -*-
"""Inicializar com o sistema (autostart) — item "programa de verdade"
do ROADMAP, espelhando o IDM ("iniciar com o Windows").

Dois backends, sem admin e sem dependências:

* **Windows** — chave ``Run`` do ``HKCU`` (mesmo padrão de registro do
  ``extsetup.write_forcelist``: ``winreg`` importado dentro da função,
  ``os.name`` como guarda — testável com winreg simulado);
* **Linux** — desktop entry ``~/.config/autostart/minidm.desktop``
  (especificação XDG/autostart: o gerenciador de login executa o
  ``Exec=`` na entrada do usuário).

macOS (LaunchAgents) não é suportado — ``supported()`` responde False e
o CLI dá a instrução em vez de falhar mudo.

O comando registrado é SEMPRE o que abre a **GUI**: no bundle
PyInstaller é o próprio ``.exe`` sem argumentos (``minidm-gui`` roda a
janela direto; ``minidm`` cai em ``cmd_gui`` pelo ``main()`` sem args);
em desenvolvimento é ``pythonw -m minidm`` (pythonw = sem janela de
console piscando no login do Windows).

Uso:  minidm autostart on|off|status   (ou o menu Arquivo da GUI)
"""

from __future__ import annotations

import os
import sys
from typing import Optional

# chave do registro (HKCU — sem admin) e nome do valor
WIN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "minidm"


def supported() -> bool:
    """Esta plataforma tem backend de autostart (Windows ou Linux)?"""
    return _backend() != ""


def _backend() -> str:
    """"win" (registro), "xdg" (desktop entry) ou "" (sem suporte)."""
    if os.name == "nt":
        return "win"
    if sys.platform.startswith("linux"):
        return "xdg"
    return ""


def command() -> str:
    """Linha de comando que abre a GUI do minidm no login.

    Sempre entre aspas (caminhos com espaços: "C:\\Program Files\\…").
    """
    exe = sys.executable
    if getattr(sys, "frozen", False):
        # PyInstaller: nosso próprio executável — sem argumentos abre a
        # GUI nos dois binários (ver packaging/minidm.spec).
        return f'"{exe}"'
    # desenvolvimento: pythonw não abre console; `-m minidm` sem args
    # já cai na GUI (cli.main → cmd_gui).
    if _backend() == "win":
        pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(pythonw):
            exe = pythonw
    return f'"{exe}" -m minidm'


# --------------------------------------------------------------- windows
def _win_read() -> Optional[str]:
    """Comando registrado na chave Run do HKCU, ou None."""
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_KEY, 0,
                             winreg.KEY_QUERY_VALUE)
    except OSError:
        return None
    try:
        val, _ = winreg.QueryValueEx(key, VALUE_NAME)
        return str(val)
    except OSError:
        return None
    finally:
        winreg.CloseKey(key)


def _win_write(cmd: str) -> None:
    import winreg

    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, WIN_KEY)
    try:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, cmd)
    finally:
        winreg.CloseKey(key)


def _win_remove() -> bool:
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_KEY, 0,
                             winreg.KEY_SET_VALUE)
    except OSError:
        return False
    try:
        winreg.DeleteValue(key, VALUE_NAME)
        return True
    except OSError:
        return False
    finally:
        winreg.CloseKey(key)


# ------------------------------------------------------------------ linux
def _xdg_dir() -> str:
    """Diretório de desktop entries de autostart (XDG, por chamada)."""
    base = os.environ.get("XDG_CONFIG_HOME") or \
        os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "autostart")


def _xdg_path() -> str:
    return os.path.join(_xdg_dir(), "minidm.desktop")


def _xdg_content(cmd: str) -> str:
    """Conteúdo da desktop entry (especificação XDG Desktop Entry)."""
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=minidm\n"
        "Comment=Gerenciador de downloads — fila + API local\n"
        f"Exec={cmd}\n"
        "Terminal=false\n"
        "NoDisplay=true\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _xdg_read() -> Optional[str]:
    """O ``Exec=`` registrado (se o arquivo existe), ou None."""
    try:
        with open(_xdg_path(), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("Exec="):
                    return line[5:].strip() or None
    except OSError:
        return None
    return None


def _xdg_write(cmd: str) -> None:
    os.makedirs(_xdg_dir(), exist_ok=True)
    with open(_xdg_path(), "w", encoding="utf-8") as fh:
        fh.write(_xdg_content(cmd))


def _xdg_remove() -> bool:
    try:
        os.unlink(_xdg_path())
        return True
    except OSError:
        return False


# ------------------------------------------------------------- API única
def enable() -> str:
    """Liga o autostart e devolve a linha de comando registrada.

    Levanta ``RuntimeError`` em plataforma sem backend (o CLI traduz
    em mensagem de erro, nunca em traceback).
    """
    cmd = command()
    b = _backend()
    if b == "win":
        _win_write(cmd)
    elif b == "xdg":
        _xdg_write(cmd)
    else:
        raise RuntimeError("autostart indisponível nesta plataforma "
                           "(só Windows e Linux)")
    return cmd


def disable() -> bool:
    """Desliga o autostart (True se removeu algo existente)."""
    b = _backend()
    if b == "win":
        return _win_remove()
    if b == "xdg":
        return _xdg_remove()
    return False


def status() -> dict:
    """Estado legível p/ CLI e GUI.

    ``registered`` = comando gravado HOJE (None se desligado) — pode
    divergir de ``command()`` se o minidm mudou de lugar (ex.: zip
    extraído outra vez); ``stale`` avisa isso.
    """
    b = _backend()
    registered: Optional[str] = None
    if b == "win":
        registered = _win_read()
    elif b == "xdg":
        registered = _xdg_read()
    atual = command()
    return {
        "supported": b != "",
        "backend": {"win": "registro HKCU\\...\\CurrentVersion\\Run",
                    "xdg": "desktop entry ~/.config/autostart",
                    "": "—"}[b],
        "enabled": registered is not None,
        "registered": registered,
        "command": atual,
        # registro aponta pra outra instalação? (zip movido, dev → exe)
        "stale": bool(registered) and registered != atual,
    }
