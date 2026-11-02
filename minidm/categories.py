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

Classificação automática: quando uma tarefa não tem categoria explícita,
`classify_filename()` deduz a categoria pela **extensão do arquivo** (como
o IDM). O campo `Task.category` fica vazio nas tarefas antigas — por isso
a contagem da sidebar usa o `effective_category()`, que classifica sob
demanda sem reescrever a fila.

Nada de código do IDM é copiado — só o conceito de categorizar por pasta.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

DEFAULT_BASE = os.path.join(os.path.expanduser("~"), "Downloads")

# extensão (minúscula, sem ponto) -> nome da categoria.
# Separado de `default_categories()` p/ consultar rápido sem acentos.
EXT_TO_CATEGORY = {
    # Vídeos
    "mp4": "Vídeos", "mkv": "Vídeos", "avi": "Vídeos", "mov": "Vídeos",
    "wmv": "Vídeos", "flv": "Vídeos", "webm": "Vídeos", "m4v": "Vídeos",
    "mpg": "Vídeos", "mpeg": "Vídeos", "ts": "Vídeos", "m2ts": "Vídeos",
    "vob": "Vídeos", "3gp": "Vídeos", "ogv": "Vídeos", "m3u8": "Vídeos",
    "mpd": "Vídeos",
    # Músicas
    "mp3": "Músicas", "flac": "Músicas", "wav": "Músicas", "aac": "Músicas",
    "m4a": "Músicas", "ogg": "Músicas", "opus": "Músicas", "wma": "Músicas",
    "ape": "Músicas", "alac": "Músicas", "mid": "Músicas", "midi": "Músicas",
    # Programas
    "exe": "Programas", "msi": "Programas", "msix": "Programas",
    "appx": "Programas", "appxbundle": "Programas", "dmg": "Programas",
    "pkg": "Programas", "deb": "Programas", "rpm": "Programas",
    "apk": "Programas", "ipa": "Programas", "bin": "Programas",
    "run": "Programas", "setup": "Programas", "jar": "Programas",
    # Documentos
    "pdf": "Documentos", "doc": "Documentos", "docx": "Documentos",
    "xls": "Documentos", "xlsx": "Documentos", "ppt": "Documentos",
    "pptx": "Documentos", "txt": "Documentos", "rtf": "Documentos",
    "odt": "Documentos", "ods": "Documentos", "odp": "Documentos",
    "epub": "Documentos", "mobi": "Documentos", "csv": "Documentos",
    "md": "Documentos", "tex": "Documentos",
    # Imagens
    "jpg": "Imagens", "jpeg": "Imagens", "png": "Imagens", "gif": "Imagens",
    "bmp": "Imagens", "webp": "Imagens", "svg": "Imagens", "tif": "Imagens",
    "tiff": "Imagens", "ico": "Imagens", "heic": "Imagens",
    "raw": "Imagens", "psd": "Imagens", "cr2": "Imagens", "nef": "Imagens",
    # Comprimidos
    "zip": "Comprimidos", "rar": "Comprimidos", "7z": "Comprimidos",
    "tar": "Comprimidos", "gz": "Comprimidos", "bz2": "Comprimidos",
    "xz": "Comprimidos", "tgz": "Comprimidos", "iso": "Comprimidos",
    "cab": "Comprimidos", "z": "Comprimidos", "lz": "Comprimidos",
    "lzma": "Comprimidos",
}


def _extension(filename: str) -> str:
    """Extensão minúscula sem ponto ("filme.MP4" -> "mp4"); "" se não houver."""
    name = (filename or "").strip()
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def classify_filename(filename: str) -> str:
    """Categoria deduzida pela extensão ("", se desconhecida).

    HLS/DASH (`.m3u8`/`.mpd`) caem em Vídeos; streams sem extensão útil
    devolvem "" (o chamador decide o padrão).
    """
    return EXT_TO_CATEGORY.get(_extension(filename), "")


def classify_url(url: str) -> str:
    """Classifica pela extensão do *path* do URL (ignora query string)."""
    from urllib.parse import urlparse

    path = urlparse(url or "").path
    return classify_filename(os.path.basename(path))


def effective_category(task) -> str:
    """Categoria efetiva de uma `Task`: a explícita ou a deduzida.

    Tarefas antigas (criadas antes do campo `category`) e as vindas da
    CLI/API/extensão sem categoria ficam "" — aqui deduzimos pela extensão
    p/ a sidebar agrupar corretamente, sem reescrever a fila.
    """
    cat = (getattr(task, "category", "") or "").strip()
    if cat:
        return cat
    return classify_filename(getattr(task, "filename", ""))


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
