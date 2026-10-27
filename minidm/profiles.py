"""Perfis de download + templates de comando (conceito do YT-DLnis).

Inspirado no `CommandTemplate`/categorias do YT-DLnis (GPL-3.0) — aqui
só pegamos a *ideia*: um perfil define como baixar (qualidade, formato,
pasta, template de nome) e pode carregar "templates de comando" com
argumentos extras do yt-dlp, ativados automaticamente por `url_regex`.

Nada de código deles é copiado; a integração com o yt-dlp é pela CLI.

Modelos:

    CommandTemplate  titulo + argumentos yt-dlp + flags de uso + url_regex
    Profile          nome + qualidade + formato + pasta + nome + templates

Persistência: `<data_dir>/profiles.json` (editável à mão também).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

PROFILES_FILE = "profiles.json"


# ------------------------------------------------------- comando template
@dataclass
class CommandTemplate:
    """Argumentos extras do yt-dlp ativáveis por perfil ou por URL.

    `content` é a string de argumentos como você escreveria na linha de
    comando (ex.: "-x --audio-format mp3 --embed-thumbnail"). O yt-dlp é
    GPL — chamamos só a CLI dele.
    """

    title: str
    content: str = ""
    # onde o template pode ser aplicado (como no YT-DLnis)
    use_as_extra_command: bool = True          # em downloads normais
    use_as_extra_command_audio: bool = True    # quando o alvo é áudio
    use_as_extra_command_video: bool = True    # quando o alvo é vídeo
    use_as_extra_command_data_fetching: bool = False   # na análise (-J)
    preferred: bool = False                    # preferido do perfil
    url_regex: list[str] = field(default_factory=list)

    def matches(self, url: str) -> bool:
        """True se algum regex do template casa com o URL."""
        for pat in self.url_regex:
            try:
                if re.search(pat, url):
                    return True
            except re.error:
                continue
        return False

    def args(self) -> list[str]:
        """Conteúdo quebrado em argumentos (aspas simples/duplas respeitadas)."""
        import shlex
        try:
            return shlex.split(self.content, posix=False)
        except ValueError:
            return self.content.split()


# ----------------------------------------------------------------- perfil
@dataclass
class Profile:
    """Um "modo de baixar" — o equivalente às categorias do YT-DLnis."""

    name: str
    # qualidade alvo: "best", "720p", "1080p", "worst", ou bitrate "128k"
    quality: str = "best"
    # formato de saída desejado quando usamos o yt-dlp: "mp4", "mp3", "m4a"…
    # vazio = deixar o yt-dlp escolher
    format_target: str = ""
    # pasta de destino (vazio = data_dir do minidm)
    dest_dir: str = ""
    # template de nome de arquivo (motor próprio: {title}, {id}, {ext}…)
    filename_template: str = "{title} [{id}].{ext}"
    # títulos dos CommandTemplates usados por este perfil
    command_templates: list[str] = field(default_factory=list)
    # true = delegar o download para o yt-dlp (se instalado)
    use_ytdlp_downloader: bool = False
    description: str = ""

    @property
    def key(self) -> str:
        return self.name.strip().lower()


# ------------------------------------------------------------ persistência
def default_profiles() -> list[Profile]:
    """Perfis embutidos (inspirados nas categorias do YT-DLnis)."""
    return [
        Profile(
            name="Padrão",
            quality="best",
            filename_template="{title} [{id}].{ext}",
            description="Vídeo na melhor qualidade disponível",
        ),
        Profile(
            name="Música",
            quality="bestaudio",
            format_target="mp3",
            filename_template="{title}.{ext}",
            command_templates=["Baixar como áudio"],
            description="Só o áudio, convertido para mp3",
        ),
        Profile(
            name="Vídeo",
            quality="best[height<=1080]",
            format_target="mp4",
            filename_template="{title} [{id}].{ext}",
            description="Vídeo em mp4 até 1080p",
        ),
    ]


def default_command_templates() -> list[CommandTemplate]:
    """Templates de comando embutidos."""
    return [
        CommandTemplate(
            title="Baixar como áudio",
            content="-x --embed-thumbnail --embed-metadata",
            use_as_extra_command_audio=True,
            use_as_extra_command_video=False,
        ),
        CommandTemplate(
            title="Melhor qualidade + metadados",
            content="--embed-metadata --embed-thumbnail --embed-chapters",
            use_as_extra_command_audio=True,
            use_as_extra_command_video=True,
        ),
        CommandTemplate(
            title="Vídeos curtos ( Shorts/Reels )",
            content="--no-playlist",
            url_regex=[r"/(shorts|reels|stories)/"],
            use_as_extra_command_data_fetching=False,
        ),
    ]


class ProfileStore:
    """Carrega/salva perfis e templates em `<data_dir>/profiles.json`."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, PROFILES_FILE)
        self.profiles: list[Profile] = []
        self.command_templates: list[CommandTemplate] = []
        self.load()

    # ---------------------------------------------------------------- io
    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = {}
        self.profiles = _load_models(Profile, raw.get("profiles"),
                                     default_profiles())
        self.command_templates = _load_models(
            CommandTemplate, raw.get("command_templates"),
            default_command_templates())

    def save(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        payload = {
            "profiles": [asdict(p) for p in self.profiles],
            "command_templates": [asdict(c) for c in self.command_templates],
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ lookup
    def get_profile(self, name: str) -> Optional[Profile]:
        if not name:
            return None
        want = name.strip().lower()
        for p in self.profiles:
            if p.key == want:
                return p
        return None

    def get_template(self, title: str) -> Optional[CommandTemplate]:
        if not title:
            return None
        want = title.strip().lower()
        for c in self.command_templates:
            if c.title.strip().lower() == want:
                return c
        return None

    def templates_for_url(self, url: str, media_type: str = "") -> list[CommandTemplate]:
        """Templates que valem para este URL/tipo ('audio'/'video'/'')."""
        out = []
        for c in self.command_templates:
            if media_type == "audio" and not c.use_as_extra_command_audio:
                continue
            if media_type == "video" and not c.use_as_extra_command_video:
                continue
            if c.url_regex and not c.matches(url):
                continue
            if not c.url_regex and not c.use_as_extra_command:
                continue
            out.append(c)
        return out

    def resolve_profile(self, name: str) -> "ResolvedProfile":
        """Perfil + templates efetivos (prontos para uso)."""
        profile = self.get_profile(name) or self.profiles[0]
        templates = [self.get_template(t) for t in profile.command_templates]
        templates = [t for t in templates if t is not None]
        return ResolvedProfile(profile=profile, templates=templates)


# containers de vídeo que o perfil pode pedir como `format_target`
_VIDEO_CONTAINERS = ("mp4", "webm", "mkv", "mov", "m4v")


def build_video_selector(height: Optional[int] = None, ext: str = "",
                          merge: bool = True) -> str:
    """Cadeia de seleção de formato de vídeo, com fallbacks encadeados.

    Por que uma *cadeia* e não um seletor único: um `-f best` **falha** em
    vídeos "DASH-only" (YouTube e vários outros — todos os formatos vêm
    separados em faixa de vídeo e faixa de áudio) com "Requested format is
    not available". A cadeia tenta, em ordem:

      1. vídeo+áudio no container pedido (`ext`), até `height`;
      2. arquivo único (progressivo) no container pedido, até `height`;
      3. vídeo+áudio em qualquer container, até `height`;
      4. arquivo único em qualquer container, até `height`;
      5. as mesmas quatro opções **sem** teto de altura — última tentativa,
         para não abortar quando o vídeo não tem formato na altura pedida.

    Regra: `height` é o eixo principal (respeitado antes de tudo) e `ext` é
    o desempate dentro de cada altura.

    `merge=False` (sem ffmpeg) descarta as opções que exigem mesclagem:
    sem ffmpeg não dá para juntar vídeo+áudio, então sobra arquivo único.
    """
    cap = f"[height<={int(height)}]" if height else ""
    ext = (ext or "").strip().lower()
    extf = f"[ext={ext}]" if ext else ""
    # áudio que casa com o container pedido (mp4 -> m4a; webm -> webm)
    aext = "m4a" if ext == "mp4" else ext

    def _tier(c: str) -> list[str]:
        out: list[str] = []
        if merge:
            if ext and aext:
                out.append(f"bv*{c}{extf}+ba[ext={aext}]")
            out.append(f"bv*{c}+ba")
        if ext:
            out.append(f"b{c}{extf}")
        out.append(f"b{c}")
        return out

    alts = _tier(cap)
    if cap:                      # relaxa o teto só depois de esgotar a tier
        alts += _tier("")
    seen: set[str] = set()
    uniq: list[str] = []
    for a in alts:               # dedupe preservando a ordem
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return "/".join(uniq)


def _height_from_quality(quality: str) -> Optional[int]:
    """Teto de altura implícito em `Profile.quality`

    1080 de "best[height<=1080]", 720 de "720p".
    """
    q = (quality or "").strip()
    m = re.search(r"height\s*<=\s*(\d+)", q, re.I)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"(\d{3,4})p?", q, re.I)
    if m:
        return int(m.group(1))
    return None


def _as_height(value) -> Optional[int]:
    """Normaliza um valor vindo de CLI/API/UI ("720", "720p", 720) em int."""
    if value is None or value == "":
        return None
    m = re.search(r"(\d{3,4})", str(value))
    return int(m.group(1)) if m else None


def _is_simple_quality(quality: str) -> bool:
    """`quality` é um valor de fábrica (best/720p/best[height<=1080])?

    Se não for, é um seletor escrito à mão pelo usuário em `profiles.json`
    — aí preservamos como está, sem reescrever por cima.
    """
    q = (quality or "").strip()
    if not q:
        return True
    return re.fullmatch(
        r"(?:best|worst|bestaudio|worstaudio|bestvideo|worstvideo)"
        r"(?:\[.*\])?|\d{3,4}p?", q, re.I) is not None


def _video_container(p: Profile) -> str:
    """Container de vídeo pedido pelo perfil ("" se não for de vídeo)."""
    t = (p.format_target or "").lower()
    return t if t in _VIDEO_CONTAINERS else ""


@dataclass
class ResolvedProfile:
    profile: Profile
    templates: list[CommandTemplate]

    def ytdlp_args(self, media_type: str = "video", height=None,
                   ext: str = "", merge: Optional[bool] = None) -> list[str]:
        """Argumentos yt-dlp deste perfil (formato + templates).

        `height`/`ext` sobrepõem a qualidade do perfil (vindos da CLI, da API
        ou do card da extensão). `merge=None` consulta o ffmpeg disponível.
        """
        args: list[str] = []
        p = self.profile
        if merge is None:
            from minidm.deps import ffmpeg_available

            merge = ffmpeg_available()
        if media_type == "audio":
            # áudio: melhor faixa de áudio, caindo para o arquivo único
            # (sites sem faixa de áudio isolada ainda baixam).
            args += ["-f", "bestaudio/b"]
            if p.format_target:
                args += ["--audio-format", p.format_target]
        else:
            h = _as_height(height) or _height_from_quality(p.quality)
            e = (ext or _video_container(p)).strip().lower()
            if height or ext or _is_simple_quality(p.quality):
                args += ["-f", build_video_selector(h, e, merge=merge)]
            else:
                args += ["-f", p.quality]   # seletor custom: preserva
        for t in self.templates:
            if media_type == "audio" and not t.use_as_extra_command_audio:
                continue
            if media_type == "video" and not t.use_as_extra_command_video:
                continue
            args += t.args()
        return args


def _is_generic(stem: str) -> bool:
    """Stem genérico de URL (index, watch, get…) não serve de título."""
    import re as _re
    s = (stem or "").strip().lower()
    return (not s or s in {"index", "watch", "get", "play", "video", "media",
                           "download", "stream", "v", "file", "content",
                           "output", "default"} or s.isdigit()
            or _re.fullmatch(r"[0-9a-f]{16,}", s) or _re.fullmatch(
                r"[a-z0-9]{10,}", s) is not None)


def _id_from_url(url: str) -> str:
    """Extrai o id de URLs comuns (watch?v=, /v/, /embed/)."""
    import re as _re
    from urllib.parse import urlparse, parse_qs
    try:
        u = urlparse(url or "")
        q = parse_qs(u.query)
        for k in ("v", "id", "video_id"):
            if q.get(k):
                return q[k][0]
        m = _re.search(r"/(?:v|embed|shorts|reels)/([A-Za-z0-9_-]{6,})", u.path)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def _title_from_url(url: str) -> str:
    """Título tentativo a partir do caminho (quando não há título real).

    Pula segmentos genéricos (watch, v, index…) — se sobrar só genérico,
    devolve vazio (o chamador cai no id ou no stem).
    """
    from urllib.parse import urlparse
    try:
        u = urlparse(url or "")
        parts = [p for p in u.path.split("/") if p]
        while parts and _is_generic(parts[-1]):
            parts.pop()
        if parts:
            last = parts[-1]
            if "." in last:
                last = last.rsplit(".", 1)[0]
            return last
    except Exception:
        pass
    return ""


def _load_models(cls, raw, defaults):
    """Carrega lista de dataclasses de JSON; se vazio, devolve os defaults."""
    if not raw:
        return list(defaults)
    out = []
    known = {f for f in cls.__dataclass_fields__}
    for item in raw:
        if not isinstance(item, dict):
            continue
        base = {k: v for k, v in item.items() if k in known}
        try:
            out.append(cls(**base))
        except TypeError:
            continue
    return out or list(defaults)


def apply_profile(task, store: "ProfileStore", profile_name: str,
                  filename_override: str = "", dir_override: str = "",
                  info: dict | None = None, height=None,
                  ext: str = "") -> None:
    """Preenche a `task` com o perfil escolhido (estilo YT-DLnis).

    * `dest_dir`/`filename` vêm do perfil (ou dos overrides);
    * o nome é renderizado pelo template de nome do perfil;
    * `ytdlp_args` recebe formato + templates de comando do perfil;
    * `height`/`ext` sobrepõem a qualidade/formato do perfil (vindos da
      CLI, da API ou do card da extensão).
    """
    import os
    from minidm.nametpl import render, context_from

    resolved = store.resolve_profile(profile_name)
    p = resolved.profile
    task.profile = p.name
    if not task.dest_dir or dir_override:
        task.dest_dir = dir_override or p.dest_dir or task.dest_dir
    if filename_override:
        # usuário definiu o nome na mão — respeita
        task.filename = filename_override
    else:
        # o perfil manda: renderiza o template de nome
        stem = os.path.splitext(task.filename)[0] or "download"
        ext_arch = _media_ext(os.path.splitext(task.filename)[1])
        ctx = context_from(info or {}, filename=stem, ext=ext_arch,
                           quality=task.quality_label)
        if not ctx.get("id"):
            ctx["id"] = _id_from_url(task.url)
        if not ctx.get("title") or _is_generic(ctx["title"]):
            # sem título real: usa caminho do URL -> id -> stem
            ctx["title"] = (_title_from_url(task.url)
                            or ctx["id"] or stem)
        # extensão: a do arquivo (se mídia), senão a que o perfil alvo
        out_ext = ext_arch or (_audio_target(p)
                               if _is_audio_profile(p) else "")
        task.filename = render(p.filename_template, ctx, fallback_ext=out_ext)
    media = "audio" if _is_audio_profile(p) else "video"
    task.ytdlp_args = resolved.ytdlp_args(media_type=media, height=height,
                                          ext=ext)
    task.use_ytdlp_downloader = p.use_ytdlp_downloader


_AUDIO_TARGETS = ("mp3", "m4a", "opus", "wav", "flac", "aac", "ogg")
_MEDIA_EXTS = ("mp4", "m4v", "webm", "mkv", "mov", "flv", "avi", "wmv", "ts",
               "m4s", "mp3", "m4a", "aac", "ogg", "opus", "wav", "flac",
               "jpg", "png", "srt", "vtt", "part")


def _media_ext(ext_dot: str) -> str:
    """Só aceita extensões de mídia (.php/.aspx não são extensão de arquivo)."""
    e = (ext_dot or "").lstrip(".").lower()
    return e if e in _MEDIA_EXTS else ""


def _is_audio_profile(p: Profile) -> bool:
    """Perfil cujo alvo é áudio (mp3/m4a/…)."""
    return (p.format_target or "").lower() in _AUDIO_TARGETS


def _audio_target(p: Profile) -> str:
    return (p.format_target or "").lower()
