"""Cookies do NAVEGADOR do usuário para a engine própria do minidm.

Conceito herdado do yt-dlp (`--cookies-from-browser`): sites que exigem
login só devolvem o arquivo se a requisição vier com a sessão do usuário.
O yt-dlp lê direto o perfil do navegador; aqui aproveitamos isso por
**chamada de CLI** (o yt-dlp é GPL — nunca copiamos código, só chamamos
o binário e lemos o arquivo de cookies que ele mesmo escreve).

Fluxo: `yt-dlp --cookies-from-browser BROWSER --cookies <jar> URL` grava o
jar em formato Netscape; convertemos em header `Cookie: a=b; c=d` e a
engine segmentada usa como qualquer outro cookie. O jar é cacheado em
`<data_dir>/cookies-<browser>.txt` por `TTL_SECONDS` p/ não reler o
perfil a cada segmento.

Só a sessão do próprio usuário, exportada localmente — nada de contorno
de DRM (ver `media.analyze`: DRM é só detectado e recusado).
"""

from __future__ import annotations

import os
import time
from typing import Optional

# navegadores que o yt-dlp sabe ler
BROWSERS = ("chrome", "edge", "firefox", "brave", "opera", "vivaldi",
            "chromium", "whale")
# quanto tempo o jar exportado vale (s) — a sessão do usuário continua a
# mesma; reler a cada download só custa tempo
TTL_SECONDS = 15 * 60
# URL neutra só p/ o yt-dlp ter onde "grudar" o jar (não baixa nada:
# --skip-download + --print saem antes)
_PROBE_URL = "https://www.youtube.com/"


def jar_path(data_dir: str, browser: str) -> str:
    return os.path.join(data_dir, f"cookies-{browser.lower()}.txt")


def export(browser: str, data_dir: str, url: str = _PROBE_URL,
           force: bool = False) -> Optional[str]:
    """Exporta o jar de `browser` para o cache; devolve o caminho (ou None).

    Chama a CLI do yt-dlp uma vez e reusa o arquivo por TTL. Qualquer
    falha (yt-dlp ausente, navegador sem perfil, permissão) devolve None
    em vez de quebrar o download — o cookie é um extra.
    """
    from minidm.ytdlp import find

    cmd = find()
    if not cmd:
        return None
    path = jar_path(data_dir, browser)
    try:
        if not force and os.path.exists(path) and \
                time.time() - os.path.getmtime(path) < TTL_SECONDS:
            return path
        os.makedirs(data_dir, exist_ok=True)
        import subprocess

        from minidm.ytdlp import _run

        # mesmo dispatcher do download (`ytdlp._run`): no APK/congelado
        # roda in-process — subprocessar sys.executable abortaria com
        # SIGABRT (app_process, ver ytdlp._in_process)
        proc = _run(
            cmd + ["--cookies-from-browser", browser,
                   "--cookies", path,
                   "--skip-download", "--no-warnings", url],
            timeout=60)
        if proc.returncode != 0 or not os.path.exists(path) or \
                os.path.getsize(path) == 0:
            return None
        return path
    except (OSError, subprocess.TimeoutExpired):
        return None


def header_from_jar(path: str) -> str:
    """Converte um jar Netscape em header `Cookie: a=b; c=d`.

    Formato do arquivo (uma linha por cookie, tab-separated):
    domain  flag  path  secure  expiration  name  value
    """
    pairs: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                name, value = parts[5], parts[6]
                if name:
                    pairs.append(f"{name}={value}")
    except OSError:
        return ""
    return "; ".join(pairs)


def browser_cookie_header(browser: str, data_dir: str,
                          url: str = _PROBE_URL) -> str:
    """Header Cookie com a sessão do navegador (cacheado), ou ""."""
    if not browser:
        return ""
    p = export(browser, data_dir, url)
    if not p:
        return ""
    return header_from_jar(p)


def clear_cache(data_dir: str, browser: str = "") -> None:
    """Remove jar(es) cacheado(s) (ex.: sessão trocada no navegador)."""
    import glob

    pattern = (jar_path(data_dir, browser) if browser
               else os.path.join(data_dir, "cookies-*.txt"))
    for p in glob.glob(pattern):
        try:
            os.remove(p)
        except OSError:
            pass
