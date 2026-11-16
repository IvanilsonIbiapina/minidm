"""Template de nome de arquivo — motor próprio com placeholders `{...}`.

Inspirado no `filename_template` do YT-DLnis (preview + placeholders),
mas com sintaxe e código próprios (não copiamos o deles).

Placeholders disponíveis (faltando o dado, fica vazio):

    {title}       título do vídeo/música
    {id}          id do vídeo no site
    {ext}         extensão do arquivo (mp4, ts, mp3…)
    {uploader}    canal/artista
    {duration}    duração (segundos)
    {height}      altura (720, 1080…) ou vazio
    {quality}     rótulo da qualidade ("720p")
    {date}        data YYYY-MM-DD
    {n}           índice quando há vários (1, 2, 3…)
    {filename}    nome base sugerido pelo servidor
    {{ e }}       chaves literais

Regras:
  * caracteres inválidos no Windows são substituídos por "_";
  * o nome final é cortado em 120 caracteres (sem contar a extensão);
  * `render()` nunca devolve vazio — cai em "download".
"""

from __future__ import annotations

import re
import time
from typing import Optional

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# extensão de verdade: só letras/números, curta e não-todo-número — o
# ponto de um TÍTULO ("Mr. Brightside", "video.2024") não é extensão
_REAL_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")
MAX_STEM = 120


def render(template: str, context: dict, fallback_ext: str = "") -> str:
    """Aplica o template e devolve um nome de arquivo seguro (com extensão)."""
    ctx = {k: _fmt(v) for k, v in (context or {}).items()}

    def sub(m):
        return ctx.get(m.group(1), "")

    try:
        # protege {{ e }} antes de substituir placeholders
        protected = (template or "").replace("{{", "\x00").replace("}}", "\x01")
        name = _PLACEHOLDER_RE.sub(sub, protected)
        name = name.replace("\x00", "{").replace("\x01", "}")
    except re.error:
        name = template or ""
    name = _INVALID.sub("_", name)
    # remove grupos que ficaram vazios: "aula []" -> "aula", " - " solto
    name = re.sub(r"\s*[\[\(\{]\s*[\]\)\}]", "", name)     # [] () {} vazios
    name = re.sub(r"\s+[-–—_:]\s*$", "", name)             # separador no fim
    name = re.sub(r"\s+[-–—_:]\s*(?=\.)", "", name)        # " - .mp3" -> ".mp3"
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "download"
    # separa extensão para cortar o stem com segurança
    stem, ext = _split_ext(name)
    if ext and not _is_real_ext(ext):
        # o "ext" achado é o ponto do TÍTULO ("Mr. Brightside" ->
        # ". Brightside"): o nome inteiro é o stem e o fallback volta
        # a valer (sem isso, música com ponto nunca ganhava .mp3)
        stem, ext = name, ""
    if not ext and fallback_ext:
        ext = "." + fallback_ext.lstrip(".")
    stem = stem[:MAX_STEM].rstrip(" .")
    return stem + ext


def preview(template: str, context: dict, fallback_ext: str = "") -> str:
    """Mesmo que `render`, mas com placeholders faltando viram `{chave}`."""
    ctx = {k: _fmt(v) for k, v in (context or {}).items()}

    def sub(m):
        key = m.group(1)
        return ctx[key] if key in ctx else "{" + key + "}"

    try:
        protected = (template or "").replace("{{", "\x00").replace("}}", "\x01")
        name = _PLACEHOLDER_RE.sub(sub, protected)
        name = name.replace("\x00", "{").replace("\x01", "}")
    except re.error:
        name = template or ""
    name = _INVALID.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "download"
    stem, ext = _split_ext(name)
    if ext and not _is_real_ext(ext):
        stem, ext = name, ""
    if not ext and fallback_ext:
        ext = "." + fallback_ext.lstrip(".")
    return stem[:MAX_STEM] + ext


def context_from(info: dict, *, filename: str = "", ext: str = "",
                 quality: str = "", index: Optional[int] = None) -> dict:
    """Monta o contexto a partir do que temos (análise/yt-dlp/tarefa)."""
    ctx = {
        "title": info.get("title") or "",
        "id": info.get("id") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration") or "",
        "height": info.get("height") or "",
        "quality": quality,
        "ext": ext or info.get("ext") or "",
        "date": (time.strftime("%Y-%m-%d")
                 if not info.get("upload_date") else _ymd(info["upload_date"])),
        "filename": filename,
    }
    if index is not None:
        ctx["n"] = index
    return ctx


def _ymd(yyyymmdd: str) -> str:
    s = str(yyyymmdd)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _fmt(v) -> str:
    if v is None:
        return ""
    return str(v)


def _split_ext(name: str) -> tuple[str, str]:
    if "." in name[1:]:
        i = name.rfind(".")
        return name[:i], name[i:]
    return name, ""


def _is_real_ext(ext: str) -> bool:
    """`.mp4`/`.7z`/`.m3u8` sim; `. Brightside`/`.2024` não (título)."""
    body = ext[1:]
    return bool(_REAL_EXT_RE.match(ext)) and not body.isdigit()
