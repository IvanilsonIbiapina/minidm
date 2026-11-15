# -*- coding: utf-8 -*-
"""Atalhos no Menu Iniciar — item "programa de verdade" do ROADMAP.

O zip/tar.gz do minidm é **portátil** (sem instalador): quem extrai não
ganha atalho nenhum. Este módulo cria e remove os atalhos do usuário,
sem admin e sem dependências:

* **Windows** — ``.lnk`` em ``%APPDATA%\\Microsoft\\Windows\\Start
  Menu\\Programs\\minidm\\`` (sem admin: perfil do usuário) escrito via
  COM ``WScript.Shell`` do **PowerShell embutido** — o Python não
  escreve ``.lnk`` e pywin32 não é dependência do projeto;
* **Linux** — desktop entry ``~/.local/share/applications/minidm.desktop``
  (especificação XDG: o menu de aplicativos mostra a entrada).

macOS não é suportado — ``supported()`` responde False e o CLI dá a
instrução em vez de falhar mudo.

O alvo é o MESMO do autostart (``autostart.target()``): a GUI — no
bundle PyInstaller é o próprio ``.exe`` sem argumentos, em
desenvolvimento ``pythonw -m minidm``. No Windows um segundo atalho
abre o ``LEIA-ME.txt`` quando ele existe ao lado do bundle (instruções
do SmartScreen); o ``off`` apaga tudo que é ``.lnk`` da nossa pasta.

Uso:  minidm shortcuts on|off|status   (ou menu Arquivo da GUI)
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

from minidm import autostart

# descrições gravadas no .lnk / desktop entry
DESC_GUI = "Gerenciador de downloads — fila + API local"
DESC_LEIA = "Instruções do minidm (leia antes de usar)"


def supported() -> bool:
    """Esta plataforma tem backend de atalhos (Windows ou Linux)?"""
    return _backend() != ""


def _backend() -> str:
    """"win" (Menu Iniciar), "xdg" (aplicativos) ou "" (sem suporte)."""
    if os.name == "nt":
        return "win"
    if sys.platform.startswith("linux"):
        return "xdg"
    return ""


# --------------------------------------------------------------- alvos
def _readme() -> Optional[str]:
    """Caminho do ``LEIA-ME.txt`` ao lado do bundle, ou None.

    No zip ele fica na raiz (``<raiz>/LEIA-ME.txt``) enquanto os exes
    estão em ``<raiz>/minidm/`` e ``<raiz>/minidm-gui/`` — por isso o
    ``..``. Em desenvolvimento ele existe só em ``packaging/``.
    """
    exedir = os.path.dirname(os.path.abspath(sys.executable))
    candidatos = [
        os.path.join(exedir, "LEIA-ME.txt"),
        os.path.join(exedir, os.pardir, "LEIA-ME.txt"),
        os.path.join(os.getcwd(), "LEIA-ME.txt"),
        os.path.join(os.getcwd(), "packaging", "LEIA-ME.txt"),
    ]
    for c in candidatos:
        if os.path.isfile(c):
            return os.path.normpath(c)
    return None


def _icon() -> Optional[str]:
    """Ícone ``.png`` para o desktop entry (Linux), ou None.

    No bundle o ícone vai embutido no próprio exe (PyInstaller), então
    só se houver um .png solto ao lado — em dev, ``packaging/``.
    """
    exedir = os.path.dirname(os.path.abspath(sys.executable))
    for c in (os.path.join(exedir, "minidm.png"),
              os.path.join(os.getcwd(), "packaging", "minidm.png")):
        if os.path.isfile(c):
            return os.path.normpath(c)
    return None


def _lnk_entries() -> list[tuple[str, str, str, str]]:
    """``(arquivo, alvo, argumentos, descrição)`` dos .lnk a criar."""
    exe, args = autostart.target()
    out = [("minidm.lnk", exe, args, DESC_GUI)]
    leia = _readme()
    if leia:
        out.append(("LEIA-ME.lnk", leia, "", DESC_LEIA))
    return out


def _desktop_content() -> str:
    """Conteúdo da desktop entry (especificação XDG Desktop Entry)."""
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=minidm",
        f"Comment={DESC_GUI}",
        f"Exec={autostart.command()}",
        "Terminal=false",
        "Categories=Network;",
        "StartupNotify=false",
    ]
    icon = _icon()
    if icon:
        lines.insert(4, f"Icon={icon}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------- pastas
def win_dir() -> str:
    """Pasta de atalhos do usuário no Menu Iniciar (sem admin)."""
    appdata = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                        "Programs", "minidm")


def xdg_dir() -> str:
    """Diretório de desktop entries de aplicativos (XDG, por chamada)."""
    base = os.environ.get("XDG_DATA_HOME") or \
        os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "applications")


# ------------------------------------------------------- powershell (.lnk)
def _ps_quote(s: str) -> str:
    """Literal do PowerShell: aspas simples, ``'`` virando ``''``."""
    return "'" + s.replace("'", "''") + "'"


def _powershell() -> str:
    """Caminho do powershell.exe embutido (fallback no PATH)."""
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    full = os.path.join(root, "System32", "WindowsPowerShell", "v1.0",
                        "powershell.exe")
    return full if os.path.isfile(full) else "powershell"


def _run_ps(script: str, *, expect: bool = False) -> Optional[str]:
    """Roda um script no PowerShell embutido; devolve a saída (se pedida).

    Qualquer falha vira ``RuntimeError`` com a causa na mensagem — o
    CLI/GUI imprime, nunca um traceback.
    """
    try:
        r = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Menu Iniciar: PowerShell indisponível "
                           f"({exc})") from None
    if r.returncode != 0:
        detalhe = (r.stderr or r.stdout or "").strip() or \
            f"código {r.returncode}"
        raise RuntimeError(f"Menu Iniciar: o PowerShell falhou "
                           f"({detalhe.strip()})")
    return (r.stdout or "").strip() if expect else None


def _lnk_write(path: str, target: str, args: str, desc: str) -> None:
    """Cria o ``.lnk`` via COM ``WScript.Shell`` (PowerShell)."""
    pasta = os.path.dirname(path)
    if pasta:                   # caminho sem pasta (ou POSIX c/ '\')?
        os.makedirs(pasta, exist_ok=True)
    wd = os.path.dirname(os.path.abspath(target)) or os.getcwd()
    script = (
        "$s=New-Object -ComObject WScript.Shell;"
        f"$d=$s.CreateShortcut({_ps_quote(path)});"
        f"$d.TargetPath={_ps_quote(target)};"
        f"$d.Arguments={_ps_quote(args)};"
        f"$d.WorkingDirectory={_ps_quote(wd)};"
        f"$d.Description={_ps_quote(desc)};"
        "$d.Save()"
    )
    _run_ps(script)


def _lnk_read(path: str) -> Optional[str]:
    """``TargetPath`` gravado no .lnk, ou None (ilegível/ausente)."""
    try:
        out = _run_ps(
            "$s=New-Object -ComObject WScript.Shell;"
            f"$d=$s.CreateShortcut({_ps_quote(path)});"
            "$d.TargetPath", expect=True)
    except RuntimeError:
        return None
    return out or None


def _xdg_exec(path: str) -> Optional[str]:
    """O ``Exec=`` da desktop entry (se o arquivo existe), ou None."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("Exec="):
                    return line[5:].strip() or None
    except OSError:
        return None
    return None


def _norm(p: str) -> str:
    """Caminho comparável (aspas fora, barra/case do SO)."""
    return os.path.normcase(os.path.normpath(p.strip().strip('"')))


# ------------------------------------------------------------- API única
def enable(directory: Optional[str] = None) -> list[str]:
    """Cria os atalhos e devolve os caminhos criados.

    ``directory`` só para teste (pasta temporária); sem ele usa o Menu
    Iniciar real. Levanta ``RuntimeError`` em plataforma sem backend ou
    se o sistema recusar a escrita (o CLI traduz em mensagem de erro).
    """
    b = _backend()
    if b == "win":
        base = directory or win_dir()
        criados: list[str] = []
        for name, target, args, desc in _lnk_entries():
            path = os.path.join(base, name)
            _lnk_write(path, target, args, desc)
            criados.append(path)
        return criados
    if b == "xdg":
        base = directory or xdg_dir()
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, "minidm.desktop")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_desktop_content())
        return [path]
    raise RuntimeError("atalhos indisponíveis nesta plataforma "
                       "(só Windows e Linux)")


def disable(directory: Optional[str] = None) -> bool:
    """Remove os atalhos (True se removeu algum).

    No Windows apaga todo ``.lnk`` da NOSSA pasta (a do usuário é
    dedicada — cobre o caso do LEIA-ME que sumiu entre versões) e tenta
    remover a pasta se esvaziar.
    """
    b = _backend()
    if b == "win":
        base = directory or win_dir()
        try:
            nomes = os.listdir(base)
        except OSError:
            return False
        removidos = False
        for nome in nomes:
            if not nome.lower().endswith(".lnk"):
                continue
            try:
                os.unlink(os.path.join(base, nome))
                removidos = True
            except OSError:
                pass
        if removidos:
            try:
                os.rmdir(base)          # só se esvaziou
            except OSError:
                pass
        return removidos
    if b == "xdg":
        path = os.path.join(directory or xdg_dir(), "minidm.desktop")
        try:
            os.unlink(path)
            return True
        except OSError:
            return False
    return False


def status(directory: Optional[str] = None, *,
           readback: bool = True) -> dict:
    """Estado legível p/ CLI e GUI.

    ``enabled`` = atalho principal existe HOJE; ``missing`` = os outros
    esperados que faltam (ex.: LEIA-ME); ``stale`` = o atalho aponta
    para OUTRA instalação (zip movido) — igual ao autostart, com a
    instrução de refazer. ``readback=False`` pula a leitura do alvo
    (no Windows isso é um subprocesso do PowerShell — a GUI não precisa
    dele só para marcar o check).
    """
    b = _backend()
    atual = autostart.command()
    if b == "":
        return {"supported": False, "backend": "—", "enabled": False,
                "paths": [], "missing": [], "registered": None,
                "command": atual, "stale": False}

    if b == "win":
        base = directory or win_dir()
        alvos = [(os.path.join(base, n), t)
                 for n, t, *_ in _lnk_entries()]
        backend = "Menu Iniciar do usuário"
    else:
        base = directory or xdg_dir()
        alvos = [(os.path.join(base, "minidm.desktop"), None)]
        backend = "aplicativos XDG (~/.local/share/applications)"

    paths = [p for p, _ in alvos]
    missing = [p for p in paths if not os.path.isfile(p)]
    enabled = paths[0] not in missing        # o atalho principal existe

    registered: Optional[str] = None
    if readback and os.path.isfile(paths[0]):
        if b == "win":
            registered = _lnk_read(paths[0])
        else:
            registered = _xdg_exec(paths[0])

    stale = False
    if registered:
        if b == "win":
            # o .lnk guarda SÓ o executável (argumentos à parte)
            exe, _ = autostart.target()
            stale = _norm(registered) != _norm(exe)
        else:
            stale = registered != atual
    return {"supported": True, "backend": backend, "enabled": enabled,
            "paths": paths, "missing": missing,
            "registered": registered, "command": atual, "stale": stale}
