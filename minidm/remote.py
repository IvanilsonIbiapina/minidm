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


def probe(url: str, headers: dict | None = None) -> Probe:
    """Tenta HEAD; se o servidor não cooperar, faz GET Range mínimo."""
    base = {"User-Agent": USER_AGENT}
    if headers:
        base.update(headers)
    r = requests.head(url, headers=base, allow_redirects=True,
                      timeout=TIMEOUT)
    if r.status_code >= 400 or "content-length" not in r.headers:
        # alguns servidores recusam HEAD — pergunta com 1 byte
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

    size = int(r.headers["Content-Length"]) if "Content-Length" in r.headers else None
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
