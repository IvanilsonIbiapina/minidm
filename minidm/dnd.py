"""Drag & drop de URLs para a janela + parsing puro de drops/colagem.

O tkinter nativo não tem DnD do sistema de arquivos; usamos
`tkinterdnd2` (opcional — se não estiver instalado, a GUI cai para
**Ctrl+V** com o mesmo parsing, então soltar/colar faz a mesma coisa).

O parsing é puro e vive aqui de propósito: `urls_from_drop` recebe o
string bruto que o SO entrega e devolve URLs prontas — testável sem GUI.

Formatos aceitos no drop (Windows/macOS/Linux):
* `http://… https://…`           → direto;
* `{C:\\Users\\a\\video.url}`    → atalho do Windows, lê o `URL=` dentro;
* `/caminho/arquivo.desktop`     → launcher Linux, lê `URL=`;
* arquivo de mídia local         → ignorado (o minidm baixa de URLs).
"""

from __future__ import annotations

import os
import re

_BRACE_RE = re.compile(r"\{([^{}]*)\}")
_URL_PREFIX = ("http://", "https://")


def _tokens_from_drop(data: str) -> list[str]:
    """Quebra o payload do drop preservando caminhos com espaços."""
    raw = (data or "").strip()
    if not raw:
        return []
    # trechos entre {} são UM item (caminho com espaço); o resto é
    # separado por espaços
    braced = _BRACE_RE.findall(raw)
    rest = _BRACE_RE.sub(" ", raw)
    return [b.strip().strip('"') for b in braced if b.strip()] + \
        [t.strip().strip('"') for t in rest.split() if t.strip()]


def read_url_file(path: str) -> str:
    """URL dentro de um atalho `.url` (Windows) ou `.desktop` (Linux).

    Formato dos dois: uma linha `URL=…` (no .desktop pode vir sob
    `[Desktop Entry]`). Devolve "" se não achar.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.upper().startswith("URL="):
                    u = line[4:].strip().strip('"')
                    if u.startswith(_URL_PREFIX):
                        return u
    except OSError:
        return ""
    return ""


def urls_from_drop(data: str) -> list[str]:
    """Converte o payload de um drop em URLs http(s) válidas (na ordem)."""
    out: list[str] = []
    for tok in _tokens_from_drop(data):
        low = tok.lower()
        if low.startswith(_URL_PREFIX):
            out.append(tok)
        elif low.endswith((".url", ".desktop")) and os.path.isfile(tok):
            u = read_url_file(tok)
            if u:
                out.append(u)
    return out


def urls_from_clipboard(text: str) -> list[str]:
    """URLs http(s) num texto colado (uma por linha; colas múltiplas ok)."""
    out: list[str] = []
    for line in (text or "").splitlines():
        tok = line.strip().strip('"').strip("'")
        if tok.lower().startswith(_URL_PREFIX):
            out.append(tok)
    return out


def has_dnd(root) -> bool:
    """A raiz da janela aceita drop do sistema? (TkinterDnD.Tk)"""
    return callable(getattr(root, "drop_target_register", None))


def bind_drop(root, widget, callback) -> bool:
    """Liga `<<Drop>>` no widget; devolve False se não houver DnD.

    `callback(urls: list[str])` recebe as URLs já parseadas (vazio = nada
    aproveitável no drop, e o chamador não faz barulho).
    """
    if not has_dnd(root):
        return False
    try:
        import tkinterdnd2 as _t2

        widget.drop_target_register(_t2.DND_FILES)
        widget.dnd_bind("<<Drop>>",
                        lambda e: callback(urls_from_drop(e.data)))
        return True
    except Exception:
        return False
