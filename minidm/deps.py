"""Descoberta automática de dependências externas (yt-dlp, ffmpeg).

O usuário não deve precisar saber que esses programas existem: o minidm
procura sozinho. Cada função é barata e com cache — o scheduler chama
`ffmpeg_dir()` a cada tarefa.

Ordem de busca (cada uma pode ser anulada por variável de ambiente):

  ffmpeg   MINIDM_FFMPEG > PATH > winget > scoop > chocolatey > Program Files
  yt-dlp   (feito em `minidm.ytdlp.find`): MINIDM_YTDLP > PATH > módulo Python

`status()` devolve um mapa legível p/ o comando `minidm deps`, que orienta
a instalação do que faltar.
"""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from typing import Optional

# nomes de binário por plataforma
_FFMPEG_NAMES = ("ffmpeg.exe", "ffmpeg") if os.name == "nt" else ("ffmpeg",)

# runtimes JS que o yt-dlp aceita p/ extração do YouTube (node/deno/bun).
# Sem nenhum, o yt-dlp avisa que alguns formatos podem faltar (YouTube
# embaralha o URL do vídeo com JavaScript).
_JS_RUNTIMES = ("node", "deno", "bun")


def hidden_kwargs() -> dict:
    """Argumentos de `subprocess` que evitam abrir janela de console.

    yt-dlp, PowerShell etc. são apps de console; rodando a partir da GUI
    (tkinter/pythonw) ou da extensão, o Windows criaria uma janela CMD a
    cada chamada (análise de vídeo, verificação de assinatura, download).
    Com CREATE_NO_WINDOW eles rodam silencioso em background — o
    progresso continua chegando pelo pipe (stdout), que é como já lemos.
    """
    if os.name == "nt":
        return {"creationflags": 0x08000000}   # CREATE_NO_WINDOW
    return {}


def _android_qjs() -> Optional[str]:
    """Caminho do QuickJS embarcado no APK, ou None fora do Android.

    O binário `libqjs.so` (quickjs-ng estático) vem em `jniLibs/` e o
    instalador o extrai para o `nativeLibraryDir` — o único local de onde
    o Android 10+ deixa executar (regra W^X; ver `android/setup_qjs.py`).
    Bridge Python→Java pelo import hook do Chaquopy (mesmo padrão de
    `minidm/androidpub.py`): fora do APK o import falha e é no-op.
    """
    try:
        import java                        # noqa: F401 (só existe no APK)
    except BaseException:
        return None
    try:
        from br.com.minidm.app import MainActivity
        return MainActivity.quickjsPath()
    except BaseException:
        return None


@lru_cache(maxsize=1)
def js_runtime() -> Optional[str]:
    """Runtime JS p/ o yt-dlp (`--js-runtimes <valor>`), ou None.

    O yt-dlp aceita `RUNTIME[:PATH]`. No desktop procura node, deno e
    bun no PATH; no APK devolve o QuickJS embarcado
    (`quickjs:<nativeLibraryDir>/libqjs.so`). Sem runtime, a extração do
    YouTube pode vir incompleta.
    """
    env = os.environ.get("MINIDM_JS_RUNTIME", "")
    if env:
        return env if shutil.which(env) else None
    # Android: qjs estático do quickjs-ng dentro do APK (sem node/deno)
    qjs = _android_qjs()
    if qjs:
        return f"quickjs:{qjs}"
    for name in _JS_RUNTIMES:
        if shutil.which(name):
            return name
    return None


@lru_cache(maxsize=1)
def ffmpeg_dir() -> Optional[str]:
    """Diretório que contém o ffmpeg (para --ffmpeg-location), ou None.

    O yt-dlp aceita o *diretório* em `--ffmpeg-location` e acha ffmpeg e
    ffprobe lá dentro. Cacheado: descoberta é uma vez por processo.
    """
    env = os.environ.get("MINIDM_FFMPEG", "")
    if env:
        # aceita tanto o executável quanto o diretório
        p = env if os.path.isdir(env) else os.path.dirname(env)
        return p if os.path.exists(env) else None

    # 1) PATH
    hit = shutil.which("ffmpeg")
    if hit:
        return os.path.dirname(hit)

    # 2) locais comuns no Windows (winget/scoop/chocolatey/Program Files)
    for base in _windows_search_bases():
        found = _search_ffmpeg(base)
        if found:
            return found
    return None


def ffmpeg_available() -> bool:
    return ffmpeg_dir() is not None


@lru_cache(maxsize=1)
def paramiko_available() -> bool:
    """True se o `paramiko` (cliente SFTP) está instalado.

    FTP usa `ftplib` (stdlib, sempre lá); SFTP precisa do paramiko — sem
    ele o `SftpDownloader` recusa com instrução de instalação em pt-BR.
    """
    try:
        import paramiko  # noqa: F401
        return True
    except ImportError:
        return False


def ffmpeg_bin() -> Optional[str]:
    """Caminho do executável `ffmpeg`, ou None se não existir.

    `ffmpeg_dir()` devolve o diretório (o yt-dlp quer assim); para rodar o
    ffmpeg DIRETO (mux da engine própria) precisamos do binário.
    """
    d = ffmpeg_dir()
    if not d:
        return None
    for name in _FFMPEG_NAMES:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return shutil.which("ffmpeg")


@lru_cache(maxsize=1)
def status() -> dict:
    """Estado das dependências externas, legível, p/ `minidm deps`."""
    from minidm.ytdlp import find as find_ytdlp, version as ytdlp_version, \
        is_outdated as ytdlp_outdated

    ytdlp = find_ytdlp()
    yv = ytdlp_version() if ytdlp else None
    outdated = ytdlp_outdated() if yv else None
    ff = ffmpeg_dir()
    js = js_runtime()
    pk = paramiko_available()
    return {
        "ytdlp": {
            "available": ytdlp is not None,
            "path": " ".join(ytdlp) if ytdlp else None,
            "version": yv,
            "outdated": outdated,      # True = >90 dias (sites mudam)
            "install": "python -m pip install yt-dlp",
        },
        "ffmpeg": {
            "available": ff is not None,
            "path": ff,
            "install": ("winget install Gyan.FFmpeg   (Windows)  ou  "
                        "brew install ffmpeg   (macOS)  ou  "
                        "apt install ffmpeg   (Linux)"),
        },
        "paramiko": {
            "available": pk,
            "path": None if not pk else "módulo Python paramiko",
            "install": "python -m pip install paramiko",
        },
        "js_runtime": {
            "available": js is not None,
            "path": js,
            "install": (
                "embutido no APK (quickjs) — nada a instalar"
                if js and js.startswith("quickjs:")
                else "instale o Node.js (https://nodejs.org) — usado pelo "
                     "yt-dlp p/ extrair vídeos do YouTube"
            ),
        },
        # o que cada um destrava (para o usuário entender por que importa)
        "features": {
            "qualquer site (YouTube, Instagram, …)": ytdlp is not None,
            "extração completa do YouTube (runtime JS)": js is not None,
            "converter áudio (mp3) / recodificar": ff is not None,
            "embed miniatura/metadados": ff is not None,
            "mux vídeo+áudio (máx. qualidade)": ff is not None,
            "HLS/DASH e arquivos diretos (engine própria)": True,
            "FTP (engine própria, sem dependência)": True,
            "SFTP (servidores SSH)": pk,
        },
    }


def invalidate_cache() -> None:
    """Limpa o cache (útil após instalar algo em runtime/testes)."""
    ffmpeg_dir.cache_clear()
    js_runtime.cache_clear()
    paramiko_available.cache_clear()
    status.cache_clear()


# --------------------------------------------------------------- internals
def _windows_search_bases() -> list[str]:
    local = os.environ.get("LOCALAPPDATA", "")
    home = os.path.expanduser("~")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    return [
        os.path.join(local, r"Microsoft\WinGet\Packages"),
        os.path.join(home, "scoop", "apps", "ffmpeg", "current", "bin"),
        os.path.join(home, "scoop", "shims"),
        r"C:\ProgramData\chocolatey\bin",
        os.path.join(program_files, "ffmpeg", "bin"),
    ]


def _search_ffmpeg(base: str, max_depth: int = 4) -> Optional[str]:
    """Procura o ffmpeg sob `base` (limitada em profundidade p/ ser rápida)."""
    if not base or not os.path.isdir(base):
        return None
    base_depth = base.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(base):
        depth = root.rstrip(os.sep).count(os.sep) - base_depth
        if depth >= max_depth:
            dirs[:] = []
            continue
        # winget extrai em pastas por pacote; não segue links infinitos
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in _FFMPEG_NAMES:
            if name in files:
                return root
    return None
