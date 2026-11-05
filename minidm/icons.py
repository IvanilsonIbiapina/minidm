"""Ícones da toolbar desenhados em tempo de execução (PIL) — sem assets.

O projeto não carrega imagens externas: cada ícone é um PNG pequeno
gerado por `ImageDraw` na cor do tema atual e convertido em
`PhotoImage`. Sem PIL (ou sem display), devolve `None` e a GUI mostra só
o texto do botão — nunca quebra.

Desenhos genéricos e minimalistas (traço 2px): plus, play, pause, trash,
folder, eye, gear, search, moon/sun, clipboard, up, down.
"""

from __future__ import annotations

from typing import Optional

try:      # PIL é dependência do player/miniaturas; aqui é opcional
    from PIL import Image, ImageDraw, ImageTk
    _HAVE_PIL = True
except Exception:                                    # pragma: no cover
    _HAVE_PIL = False

_CACHE: dict = {}


def _draw(name: str, d: "ImageDraw.ImageDraw", s: int, c: str) -> None:
    w = max(2, s // 8)                    # espessura do traço
    m = s // 5                            # margem
    L, R, T, B = m, s - m, m, s - m
    cx, cy = s // 2, s // 2

    if name == "add":
        d.line([(cx, T), (cx, B)], fill=c, width=w)
        d.line([(L, cy), (R, cy)], fill=c, width=w)
    elif name == "start":
        d.polygon([(L + 1, T + 1), (R - 1, cy), (L + 1, B - 1)], fill=c)
    elif name == "pause":
        gap = max(2, s // 6)
        for x in (cx - gap, cx + gap - w // 2 - 1):
            d.rectangle([x, T + 1, x + w, B - 1], fill=c)
    elif name == "remove":                 # lixeira
        d.rectangle([L + w, T + w, R - w, B - 1], outline=c, width=w)
        d.line([(L, T + w // 2), (R, T + w // 2)], fill=c, width=w)
        d.line([(cx - s // 6, T), (cx + s // 6, T)], fill=c, width=w)
    elif name == "folder":
        d.rectangle([L, T + s // 5, R, B - w // 2], outline=c, width=w)
        d.line([(L, T + s // 5), (L + s // 5, T + s // 10),
                (cx - w, T + s // 10)], fill=c, width=w, joint="curve")
    elif name == "eye":
        d.arc([L, T + s // 6, R, B - s // 6], 20, 160, fill=c, width=w)
        d.arc([L, T + s // 6, R, B - s // 6], 200, 340, fill=c, width=w)
        r = max(2, s // 6)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c)
    elif name == "gear":
        r = s // 3
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=c, width=w)
        for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1)):
            d.line([(cx + dx * r, cy + dy * r),
                    (cx + dx * (r + m // 2), cy + dy * (r + m // 2))],
                   fill=c, width=w)
    elif name == "search":
        r = s // 3
        d.ellipse([L, T, L + 2 * r, T + 2 * r], outline=c, width=w)
        d.line([(L + 2 * r - w, T + 2 * r - w), (R - 1, B - 1)],
               fill=c, width=w)
    elif name == "theme":                  # lua (meia circunferencia + ponto)
        d.arc([L, T, R, B], 60, 300, fill=c, width=w)
        d.ellipse([R - s // 3 - w, T + s // 6, R - s // 6, T + s // 3],
                  fill=c)
    elif name == "clipboard":
        d.rounded_rectangle([L + w, T + w, R - w, B - w], radius=w,
                            outline=c, width=w)
        d.rounded_rectangle([cx - s // 6, T, cx + s // 6, T + s // 4],
                            radius=max(1, w // 2), fill=c)
    elif name in ("up", "down"):
        if name == "up":
            d.polygon([(cx, T + 1), (R - 1, cy + s // 8), (L + 1, cy + s // 8)],
                      fill=c)
            d.rectangle([cx - w // 2 - 1, cy + s // 8, cx + w // 2 + 1, B - 1],
                        fill=c)
        else:
            d.polygon([(cx, B - 1), (R - 1, cy - s // 8), (L + 1, cy - s // 8)],
                      fill=c)
            d.rectangle([cx - w // 2 - 1, T + 1, cx + w // 2 + 1, cy - s // 8],
                        fill=c)
    elif name == "link":
        r = s // 4
        d.arc([L, cy - r, cx + r, cy + r], 90, 270, fill=c, width=w)
        d.arc([cx - r, cy - r, R, cy + r], 270, 90, fill=c, width=w)
        d.line([(cx - r // 2, cy), (cx + r // 2, cy)], fill=c, width=w)
    else:
        d.ellipse([L, T, R, B], outline=c, width=w)   # fallback


def make_icon(name: str, *, color: str = "#1b1d21", size: int = 16,
              master=None) -> Optional["ImageTk.PhotoImage"]:
    """Ícone pronto p/ `ttk.Button(image=…)`, ou None se não der p/ gerar."""
    if not _HAVE_PIL or master is None:
        return None
    # chave por intérprete Tcl: duas janelas no mesmo processo têm caminho
    # "." igual, mas PhotoImage não cruza interpretadores
    key = (name, color, size, str(getattr(master, "tk", master)))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    try:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        _draw(name, ImageDraw.Draw(img), size, color)
        photo = ImageTk.PhotoImage(img, master=master)
    except Exception:                            # pragma: no cover
        return None
    _CACHE[key] = photo
    return photo


def toolbar_icon(name: str, master, palette) -> Optional["ImageTk.PhotoImage"]:
    """Ícone na cor do texto do tema (contraste garantido)."""
    return make_icon(name, color=palette.text, master=master)
