# -*- coding: utf-8 -*-
"""Texto compartilhado de outro app → link na fila (Android ACTION_SEND).

O menu "Compartilhar" do Android dispara `ACTION_SEND` (`text/plain`)
contra o minidm — navegador, YouTube, WhatsApp… A `MainActivity`
despacha o texto para `main.share`, que extrai a URL e POSTa em
`/add`: o mesmo endpoint da webui/extensão, então classificação,
delegação ao yt-dlp e fila vêm de graça.

Fora do APK o módulo só expõe a extração (testável em qualquer SO).
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Optional

# URL http(s) crua no texto — o share do YouTube traz
# "…watch?v=ID&feature=share" ou "https://youtu.be/ID"
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# pontuação do prosa que costuma grudar no fim da URL ("…vídeo.")
_TRAIL = ".,;:!?'\u201c\u201d\""


def url_from_text(text: Optional[str]) -> Optional[str]:
    """Primeira URL http(s) do texto compartilhado, ou None.

    * ignora pontuação do prosa no fim ("veja https://x.com/a." → sem
      o ponto final);
    * mantém parêntese equilibrado (Wikipedia usa "…/Jogo_(xadrez)");
    * primeira URL vence quando há várias.
    """
    if not text:
        return None
    m = _URL_RE.search(text)
    if not m:
        return None
    url = m.group(0)
    while url and url[-1] in _TRAIL:
        url = url[:-1]
    if url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    return url or None


def submit(text: Optional[str], port: int, host: str = "127.0.0.1",
           tries: int = 50, delay: float = 0.2) -> str:
    """Adiciona o link do texto na fila via API local; frase p/ o Toast.

    `tries`×`delay` dão ~10 s p/ a API subir (o share pode chegar antes
    do daemon terminar de subir). Devolve sempre uma frase em pt-BR —
    ela vira o Toast do Android (MainActivity.handleShare).
    """
    url = url_from_text(text)
    if not url:
        return "Nenhum link no texto compartilhado"
    body = json.dumps({"url": url}).encode("utf-8")
    last = "API indisponível"
    for _ in range(max(1, tries)):
        req = urllib.request.Request(
            f"http://{host}:{port}/add", data=body,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read() or b"{}")
            if data.get("ok"):
                nome = data.get("filename") or url
                return f"Adicionado à fila: {nome}"
            last = str(data.get("error") or "recusado")
            break
        except urllib.error.HTTPError as e:
            # recusado de propósito (400): não adianta tentar de novo
            try:
                data = json.loads(e.read() or b"{}")
            except Exception:
                data = {}
            last = str(data.get("error") or e.reason or "recusado")
            break
        except OSError:
            time.sleep(delay)   # socket ainda fechado (daemon subindo)
    return f"Não adicionei: {last}"
