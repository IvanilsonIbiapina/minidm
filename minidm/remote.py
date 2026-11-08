"""Sondagem do servidor: o que ele suporta antes de começar a baixar.

Equivalente didático da "negociação" que um gerenciador de downloads faz no
início de cada tarefa: descobre tamanho, se aceita `Range`, e captura
`ETag`/`Last-Modified` para validar a retomada depois.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urlparse

import requests

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) minidm/1.0"
TIMEOUT = 20


@dataclass
class Probe:
    url: str
    size: Optional[int]
    accept_ranges: bool
    etag: Optional[str]
    last_modified: Optional[str]
    filename: str
    status: int


def filename_from(url: str, content_disposition: Optional[str] = None) -> str:
    """Extrai um nome de arquivo razoável do URL ou do Content-Disposition."""
    if content_disposition:
        m = re.search(r"filename\*?=(?:UTF-8''|\"?)([^\";]+)", content_disposition,
                      re.IGNORECASE)
        if m:
            return sanitize(unquote(m.group(1).strip()))
    path = unquote(urlparse(url).path)
    name = path.rstrip("/").split("/")[-1]
    return sanitize(name) if name else "download.bin"


def sanitize(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "download.bin"


# extensões que indicam arquivo de mídia direto (engine própria resolve)
_MEDIA_URL_EXTS = {
    # vídeo
    "mp4", "m4v", "webm", "mkv", "mov", "flv", "avi", "wmv", "ts", "m2ts",
    "mpg", "mpeg", "3gp", "ogv",
    # áudio
    "mp3", "m4a", "aac", "ogg", "opus", "wav", "flac", "wma", "amr",
    # streams
    "m3u8", "mpd",
    # outros que a engine própria baixa sem site-module
    "zip", "rar", "7z", "gz", "tar", "iso", "exe", "msi", "pdf", "apk",
    "jpg", "jpeg", "png", "gif", "webp", "srt", "vtt", "ass", "torrent",
}


def looks_direct_media(url: str) -> bool:
    """O URL aponta direto para um arquivo (extensão conhecida no caminho)?

    True  -> a engine própria do minidm resolve (segmentada, retomada…).
    False -> é uma *página* (youtube.com/watch, instagram.com/…): a engine
             não entende a "linguagem" do site; delegue ao yt-dlp se houver.

    Heurística só do caminho — sem rede. Páginas com caminho limpo
    (ex.: /video/123) caem em False, que é o lado seguro: aí o yt-dlp
    assume, e sem yt-dlp o usuário recebe um erro explicando.
    """
    path = unquote(urlparse(url or "").path)
    name = path.rstrip("/").split("/")[-1]
    if not name or "." not in name:
        return False
    ext = name.rsplit(".", 1)[-1].lower()
    return ext in _MEDIA_URL_EXTS


def auto_delegate(task) -> bool:
    """Decide se a tarefa deve ser baixada pelo yt-dlp, sem o usuário pedir.

    Regra (usada por TODAS as entradas — CLI, GUI, clipboard, API):
    já marcada -> mantém; mídia direta/HLS/DASH -> engine própria;
    página (não-mídia) + yt-dlp instalado -> delega ao yt-dlp.

    É o que faz "cola o link do YouTube e baixa" funcionar em qualquer
    interface. Devolve True se marcou a tarefa para delegar.
    """
    if getattr(task, "use_ytdlp_downloader", False):
        return True                       # pedido explícito já veio
    if getattr(task, "kind", "file") in ("hls", "dash"):
        return False                      # stream: engine própria
    if urlparse(task.url).scheme.lower() in ("ftp", "sftp"):
        return False                      # remoto: engine própria (ftpdl)
    if looks_direct_media(task.url):
        return False                      # arquivo direto: engine própria
    # é uma página; delega se o yt-dlp existir
    from minidm.ytdlp import available

    if available():
        task.use_ytdlp_downloader = True
        return True
    return False


def page_error(url: str) -> Optional[str]:
    """Erro claro quando o link é de PÁGINA e não há yt-dlp p/ assumir.

    Heurística conservadora: só recusa URLs http(s) cujo último segmento
    do caminho NÃO tem extensão (`youtu.be/abc123`, `/watch`,
    `/video/123`) — links com extensão (arquivo direto, `get.php`,
    `file.html`) seguem normais, sem rede nem chute. Se o yt-dlp estiver
    instalado, devolve None: a tarefa delega para ele (`auto_delegate`).

    É o "sem yt-dlp o usuário recebe um erro explicando" prometido em
    `looks_direct_media`: sem isso a tarefa fingia sucesso (baixava o
    HTML ou um arquivo de 0 bytes com o ID cru no lugar do nome).
    """
    p = urlparse(url or "")
    if p.scheme.lower() not in ("http", "https"):
        return None
    last = unquote(p.path).rstrip("/").split("/")[-1]
    if not last or "." in last:
        return None                    # é um arquivo (ou URL vazio)
    from minidm.ytdlp import available

    if available():
        return None                    # yt-dlp assume o download
    return ("este link é uma PÁGINA (YouTube, Instagram…), não um arquivo "
            "direto: baixar sites assim exige o yt-dlp, que não está "
            "instalado aqui (instale com: pip install yt-dlp; no celular, "
            "use a versão Termux do minidm).")


def _positive_size(value) -> bool:
    """O `Content-Length` serve como tamanho só se for inteiro > 0.

    YouTube responde HEAD com `Content-Length: 0` (e GET chunked, sem
    tamanho) — aceitar 0 faria a engine planejar um "download" de 0
    bytes e concluí-lo com sucesso (arquivo vazio no destino).
    """
    try:
        return value is not None and int(value) > 0
    except (TypeError, ValueError):
        return False


def probe(url: str, headers: dict | None = None) -> Probe:
    """Tenta HEAD; se o servidor não cooperar, faz GET Range mínimo."""
    base = {"User-Agent": USER_AGENT}
    if headers:
        base.update(headers)
    r = requests.head(url, headers=base, allow_redirects=True,
                      timeout=TIMEOUT)
    if r.status_code >= 400 or not _positive_size(
            r.headers.get("Content-Length")):
        # alguns servidores recusam HEAD ou "mentem" no Content-Length
        # (0) — pergunta com 1 byte de verdade
        h = dict(base)
        h["Range"] = "bytes=0-0"
        r = requests.get(url, headers=h, stream=True, timeout=TIMEOUT)
        try:
            size = _size_from_content_range(r.headers.get("Content-Range"))
            if size is None:
                size = int(r.headers.get("Content-Length") or 0) or None
            return Probe(
                url=url,
                size=size,
                accept_ranges=r.status_code == 206,
                etag=r.headers.get("ETag"),
                last_modified=r.headers.get("Last-Modified"),
                filename=filename_from(url, r.headers.get("Content-Disposition")),
                status=r.status_code,
            )
        finally:
            r.close()

    size = (int(r.headers["Content-Length"])
            if _positive_size(r.headers.get("Content-Length")) else None)
    return Probe(
        url=url,
        size=size,
        accept_ranges=str(r.headers.get("Accept-Ranges", "")).lower() == "bytes",
        etag=r.headers.get("ETag"),
        last_modified=r.headers.get("Last-Modified"),
        filename=filename_from(url, r.headers.get("Content-Disposition")),
        status=r.status_code,
    )


def _size_from_content_range(value: Optional[str]) -> Optional[int]:
    # "bytes 0-0/12345"
    if not value:
        return None
    m = re.match(r"bytes\s+\d+-\d+/(\d+|\*)", value)
    if not m or m.group(1) == "*":
        return None
    return int(m.group(1))


def still_valid(task, p: Probe) -> bool:
    """O `.part` guardado ainda corresponde ao arquivo remoto?"""
    if p.size is None or task.total_size is None:
        return p.size == task.total_size
    if p.size != task.total_size:
        return False
    # se o servidor der validadores, exige que não mudem
    if p.etag and task.etag and p.etag != task.etag:
        return False
    if p.last_modified and task.last_modified and p.last_modified != task.last_modified:
        return False
    return True
