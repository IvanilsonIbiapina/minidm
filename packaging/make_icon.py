# -*- coding: utf-8 -*-
"""Gera o ícone do minidm (packaging/minidm.ico + .png) com PIL.

Sem assets no repositório: o ícone é desenhado por código (mesma filosofia
de minidm/icons.py). Rodar uma vez (ou no CI antes do PyInstaller):

    python packaging/make_icon.py

Desenho: seta de download (mão livre) dentro de um quadrado arredondado.
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
ACCENT = (10, 123, 216, 255)      # azul do tema claro
DARK = (27, 29, 33, 255)
WHITE = (255, 255, 255, 255)


def draw_icon(size: int) -> Image.Image:
    """Ícone `size`x`size` com fundo arredondado e seta de download."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = max(1, size // 16)
    d.rounded_rectangle([m, m, size - m - 1, size - m - 1],
                        radius=size // 5, fill=ACCENT)
    # seta de download: haste + ponta
    w = max(2, size // 7)
    cx = size // 2
    top, tail = size // 5, int(size * 0.42)
    d.line([(cx, top), (cx, tail)], fill=WHITE, width=w)
    half = size // 4
    d.polygon([(cx, int(size * 0.78)), (cx - half, tail),
               (cx + half, tail)], fill=WHITE)
    # prato (bandeja) embaixo
    bw = size // 3
    d.line([(cx - bw, int(size * 0.82)), (cx + bw, int(size * 0.82))],
           fill=WHITE, width=w)
    return img


def main() -> None:
    png = os.path.join(OUT_DIR, "minidm.png")
    draw_icon(256).save(png)
    ico = os.path.join(OUT_DIR, "minidm.ico")
    # multi-tamanho p/ Explorer, Alt-Tab e instaladores
    draw_icon(256).save(ico, sizes=[(16, 16), (24, 24), (32, 32),
                                    (48, 48), (64, 64), (128, 128),
                                    (256, 256)])
    print(f"ok: {png} + {ico}")


if __name__ == "__main__":
    main()
