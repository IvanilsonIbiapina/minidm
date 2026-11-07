# -*- coding: utf-8 -*-
"""Gera os ícones do APK Android (mipmaps) a partir do minidm.png.

O logo vem do make_icon.py (seta de download em quadrado arredondado);
aqui só dimensionamos por densidade e montamos o adaptive icon:

* legado (API 24/25): `mipmap-<densidade>/ic_launcher.png`
* adaptive (API 26+): `mipmap-anydpi-v26/ic_launcher.xml` (escrito à
  mão, ao lado) + fundo sólido `#0a7bd8` (values/colors.xml) +
  `mipmap-<densidade>/ic_launcher_foreground.png` — o logo ocupa 75%
  do canvas de 108dp, deixando a seta branca dentro da safe zone (66%).

Rodar da raiz do projeto:

    python packaging/make_android_icons.py
"""

from __future__ import annotations

import os

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "minidm.png")
RES = os.path.normpath(os.path.join(
    HERE, "..", "android", "app", "src", "main", "res"))

# densidade -> tamanho do ícone legado (dp * densidade do bucket)
LEGACY = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144,
          "xxxhdpi": 192}
# densidade -> canvas do adaptive foreground (108dp * densidade)
FG = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324,
      "xxxhdpi": 432}
FG_SCALE = 0.75                # logo dentro do canvas (safe zone = 0.66)


def main() -> None:
    logo = Image.open(SRC).convert("RGBA")

    for dens, size in LEGACY.items():
        out = os.path.join(RES, f"mipmap-{dens}", "ic_launcher.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        logo.resize((size, size), Image.LANCZOS).save(out)

    for dens, size in FG.items():
        fg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        inner = int(size * FG_SCALE)
        r = logo.resize((inner, inner), Image.LANCZOS)
        off = (size - inner) // 2
        fg.paste(r, (off, off), r)
        out = os.path.join(RES, f"mipmap-{dens}",
                           "ic_launcher_foreground.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        fg.save(out)

    print(f"ok: {len(LEGACY)} ic_launcher + {len(FG)} foreground em {RES}")


if __name__ == "__main__":
    main()
