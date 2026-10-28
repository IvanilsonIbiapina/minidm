"""Categorias estilo IDM: cada categoria define a pasta de destino.

O IDM separa downloads em pastas por categoria (Vídeos, Músicas,
Programas, Documentos, Imagens, Comprimidos). Aqui cada categoria é um
par **pasta + perfil** do minidm:

  * a **pasta** vem da `download_base` configurada (vazio = `~/Downloads`)
    + uma subpasta por categoria;
  * o **perfil** diz *como* baixar (Vídeos -> perfil "Vídeo" (vídeo),
    Músicas -> perfil "Música" (áudio p/ mp3), o resto -> "Padrão").

A categoria **Vídeos** é a padrão (pré-definida), mas o usuário pode
mudar para Música/Áudio ou qualquer outra.

Nada de código do IDM é copiado — só o conceito de categorizar por pasta.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

DEFAULT_BASE = os.path.join(os.path.expanduser("~"), "Downloads")


@dataclass(frozen=True)
class Category:
    name: str        # nome exibido (como no IDM)
    folder: str      # subpasta dentro da `download_base`
    profile: str     # perfil do minidm usado ("" = perfil padrão)
    default: bool = False   # categoria pré-definida (a inicial)


def default_categories() -> list[Category]:
    """Conjunto completo estilo IDM; Vídeos é a padrão."""
    return [
        Category("Vídeos", "Vídeos", "Vídeo", default=True),
        Category("Músicas", "Músicas", "Música"),
        Category("Programas", "Programas", "Padrão"),
        Category("Documentos", "Documentos", "Padrão"),
        Category("Imagens", "Imagens", "Padrão"),
        Category("Comprimidos", "Comprimidos", "Padrão"),
    ]


def get_category(name: str) -> Optional[Category]:
    """Acha a categoria pelo nome (case- e acento-insensível); `None` se não."""
    key = _norm(name)
    if not key:
        return None
    for c in default_categories():
        if key in (_norm(c.name), _norm(c.folder)):
            return c
    return None


def _norm(s: str) -> str:
    """Minúsculas + sem acentos (\"vídeos\" == \"videos\", \"MÚSICAS\" == \"musicas\")."""
    import unicodedata

    s = (s or "").strip().lower()
    return "".join(ch for ch in unicodedata.normalize("NFD", s)
                   if unicodedata.category(ch) != "Mn")


def default_category() -> Category:
    """A categoria pré-definida (Vídeos)."""
    for c in default_categories():
        if c.default:
            return c
    return default_categories()[0]


def resolve_base(config) -> str:
    """Pasta base das categorias: `download_base` ou `~/Downloads`."""
    base = (getattr(config, "download_base", "") or "").strip()
    if not base:
        base = DEFAULT_BASE
    return os.path.expanduser(base)


def category_folder(cat: Category, config) -> str:
    """Caminho completo da pasta de uma categoria (criada sob demanda)."""
    return os.path.join(resolve_base(config), cat.folder)


def ensure_folder(path: str) -> str:
    """Cria a pasta (se não existir) e devolve o caminho."""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path
