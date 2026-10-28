"""Integração opcional com o yt-dlp (https://github.com/yt-dlp/yt-dlp).

O yt-dlp é GPL-3.0 — por isso só chamamos a *CLI* dele como programa
externo e lemos o JSON que ele emite (dados, não código). Nada do código
dele é copiado ou importado.

Fluxo: `yt-dlp -J` describe um vídeo (título, formatos, qualidade) e
convertemos no mesmo formato do `minidm.media.analyze`. Se o yt-dlp não
estiver instalado, o chamador cai no parser próprio (HLS/DASH direto).

Achamos o executável em: variável MINIDM_YTDLP > PATH (yt-dlp /
yt-dlp.exe / yt-dlp.cmd) > python -m yt_dlp.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from functools import lru_cache
from typing import Optional

from minidm.deps import hidden_kwargs

TIMEOUT = 60


def find() -> Optional[list[str]]:
    """Comando para rodar o yt-dlp, ou None se não estiver disponível."""
    env = os.environ.get("MINIDM_YTDLP", "")
    if env:
        # caminho explícito: só aceita se realmente existir
        if os.path.exists(env):
            return [env]
        return None
    for name in ("yt-dlp", "yt-dlp.exe", "yt-dlp.cmd", "youtube-dl"):
        path = shutil.which(name)
        if path:
            return [path]
    # último caso: o módulo instalado como pacote Python
    try:
        import yt_dlp  # noqa: F401
        return [os.sys.executable, "-m", "yt_dlp"]
    except ImportError:
        return None


def available() -> bool:
    return find() is not None


@lru_cache(maxsize=1)
def supports_remote_components() -> bool:
    """O yt-dlp instalado aceita `--remote-components`? (verifica uma vez)

    A flag existe desde 2024; versões antigas cairiam com
    "no such option". Checamos no `--help` uma vez por processo.
    """
    cmd = find()
    if not cmd:
        return False
    try:
        proc = subprocess.run(cmd + ["--help"], capture_output=True,
                              timeout=TIMEOUT, check=False,
                              **hidden_kwargs())
    except (OSError, subprocess.TimeoutExpired):
        return False
    out = (proc.stdout or b"") + (proc.stderr or b"")
    return b"--remote-components" in out


def describe(url: str, referer: str = "", user_agent: str = "",
             cookie: str = "", timeout: float = TIMEOUT) -> dict:
    """Roda `yt-dlp -J` e devolve o JSON bruto (ou levanta RuntimeError).

    Se o cookie do navegador fizer o site devolver a página sem formatos
    (comportamento observado no YouTube: "só imagens"/rc!=0), tenta de novo
    **sem o cookie** antes de desistir — o cookie é um extra, não um
    requisito na maioria dos sites.
    """
    cmd = find()
    if not cmd:
        raise RuntimeError("yt-dlp não encontrado (instale: pip install yt-dlp)")
    from minidm.deps import js_runtime

    def _args(ck: str) -> list[str]:
        a = cmd + ["-J", "--no-playlist", "--no-warnings"]
        # runtime JS: o YouTube exige para extrair todos os formatos
        js = js_runtime()
        if js:
            a += ["--js-runtimes", js]
            if supports_remote_components():
                # solver de challenge do YouTube (EJS): sem ele o yt-dlp vem
                # com a página "só imagens" (storyboards) — aí a análise não
                # acha nenhuma qualidade e o download (com cookie) erra
                # "Requested format is not available". Baixado uma vez e
                # cacheado pelo yt-dlp; se o GitHub estiver fora, é só warning.
                a += ["--remote-components", "ejs:github"]
        if referer:
            a += ["--add-header", f"Referer:{referer}"]
        if user_agent:
            a += ["--user-agent", user_agent]
        if ck:
            # o yt-dlp aceita cookies no formato "a=b; c=d" via --add-header
            a += ["--add-header", f"Cookie:{ck}"]
        a.append(url)
        return a

    def _run(a: list[str]):
        try:
            # CREATE_NO_WINDOW: esta é a chamada que a extensão dispara ao
            # detectar o vídeo — sem ela o Windows abria um CMD por segundos.
            return subprocess.run(a, capture_output=True, timeout=timeout,
                                  check=False, **hidden_kwargs())
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"yt-dlp falhou: {e}") from e

    def _decode(p) -> dict:
        if p.returncode != 0:
            err = (p.stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(
                f"yt-dlp: {err.splitlines()[-1] if err else 'erro'}")
        try:
            return json.loads(p.stdout.decode("utf-8", "replace"))
        except ValueError as e:
            raise RuntimeError(f"yt-dlp devolveu JSON inválido: {e}") from e

    proc = _run(_args(cookie))
    if cookie and not _usable_info(proc):
        # cookie quebrou a extração -> segunda chance sem ele
        retry = _run(_args(""))
        if _usable_info(retry):
            proc = retry
    return _decode(proc)


def _usable_info(proc) -> bool:
    """O `-J` trouxe formatos de verdade (com URL), ou só storyboards/erro?"""
    if proc.returncode != 0:
        return False
    try:
        info = json.loads((proc.stdout or b"").decode("utf-8", "replace"))
    except ValueError:
        return False
    return any(f.get("url") for f in (info.get("formats") or []))


def analyze(url: str, referer: str = "", user_agent: str = "",
            cookie: str = "", timeout: float = TIMEOUT) -> dict:
    """Analisa com o yt-dlp e converte para o formato do minidm.media.analyze.

    Retorna {"ok", "kind", "qualities", "duration", "drm", "filename"}.
    DRM: se o yt-dlp reportar formato não disponibilizado por DRM nós
    apenas sinalizamos — nunca tentamos contornar.
    """
    info = describe(url, referer=referer, user_agent=user_agent,
                    cookie=cookie, timeout=timeout)

    title = (info.get("title") or "").strip()
    duration = info.get("duration")
    webpage = (info.get("webpage_url_domain") or info.get("extractor_key")
               or "").lower()
    drm = None
    if any(k in webpage for k in ("netflix", "disney", "hbo", "primevideo",
                                  "paramount", "bbc_iplayer")):
        drm = webpage

    qualities = []
    for f in info.get("formats") or []:
        if f.get("ext") == "m3u8_native" or f.get("protocol") in (
                "m3u8_native", "m3u8"):
            kind = "hls"
        elif f.get("ext") == "mpd" or f.get("protocol") == "http_dash_segments":
            kind = "dash"
        else:
            kind = "file"
        height = f.get("height")
        qualities.append({
            "label": _label(f, height),
            "url": f.get("url") or f.get("fragment_base_url") or "",
            "kind": kind,
            "bandwidth": f.get("tbr") and int(f["tbr"] * 1000) or None,
            "resolution": (f"{f['width']}x{height}"
                           if f.get("width") and height else None),
            "height": height,
            "duration": duration,
            "estimated_size": None,
            "audio_only": not f.get("height"),
            "format_id": f.get("format_id", ""),
        })
    qualities = [q for q in qualities if q["url"]]
    # melhor primeiro (a UI pode reordenar; aqui já facilita)
    qualities.sort(key=lambda q: (q.get("height") or 0, q.get("bandwidth") or 0),
                   reverse=True)

    if not qualities:
        return {"ok": False, "error": "yt-dlp não listou formatos com URL"}
    if drm:
        return {"ok": False, "drm": drm,
                "error": f"conteúdo protegido por DRM ({drm}) — "
                         f"não é possível baixar"}
    return {"ok": True, "kind": "ytdlp", "qualities": qualities,
            "duration": duration, "encrypted": False, "drm": None,
            "filename": _safe_name(title)}


def _label(f: dict, height) -> str:
    bits = []
    if height:
        bits.append(f"{height}p")
    elif f.get("abr"):
        bits.append(f"{int(f['abr'])}kbps áudio")
    elif f.get("format_note"):
        bits.append(str(f["format_note"]))
    if f.get("ext"):
        bits.append(f["ext"])
    return " ".join(bits) or f.get("format_id", "formato")


def _safe_name(title: str) -> str:
    from minidm.remote import sanitize
    return sanitize(title[:80].strip(" .")) if title else ""
