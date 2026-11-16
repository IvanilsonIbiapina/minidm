# -*- coding: utf-8 -*-
"""Spotify: metadados públicos + download de fonte NÃO-DRM.

Regra permanente do projeto (README: "DRM: detectamos, nunca contornamos"):

* as **streams do Spotify são DRM** (OGG/AAC cifrados com a chave deles) —
  ripá-las exige contornar uma proteção tecnológica (DMCA §1201,
  Lei 9.610) e está FORA DE ESCOPO: aqui só detectamos e recusamos
  (`refuse_if_spotify`, chamado no topo de `media.analyze`, no `add`
  da CLI e no `POST /add` — o próprio yt-dlp também recusa, `KnownDRMIE`);
* o caminho legítimo é este módulo: lemos os METADADOS públicos da
  track/album/playlist (página `embed/…` + oEmbed, sem login e sem Web
  API), montamos a busca `Artista - Título` e baixamos de uma fonte
  NÃO-DRM (YouTube, via yt-dlp `ytsearch`) — o fluxo do spotdl.

Fontes verificadas ao vivo (2026-11-15):
* `https://open.spotify.com/oembed?url=…` -> 200 sem token (título/capa);
* `https://open.spotify.com/embed/<kind>/<id>` -> HTML com
  `__NEXT_DATA__` e a entidade em `props.pageProps.state.data.entity`
  (track: name/duration(ms)/artists; album|playlist: trackList com
  uri/title/subtitle/duration(ms)/isPlayable — teto ~100 itens);
* `describe("ytsearch10:…")` devolve 10 candidatos com duração.
"""
from __future__ import annotations

import json
import re
from typing import Optional
from urllib.parse import urlparse

import requests

TIMEOUT = 20.0
# tipos aceitos (o que a UI/CLI chama de "kind" do Spotify)
KINDS = ("track", "album", "playlist", "artist", "episode", "show")
# o que dá para baixar via busca (episodes/shows = podcasts: fora por ora)
DOWNLOADABLE = ("track", "album", "playlist")
# teto observado no __NEXT_DATA__ do embed (100 em playlists testadas)
_EMBED_CAP = 100
_EMBED_PATH = ("props", "pageProps", "state", "data", "entity")
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_ID_RE = re.compile(r"^[0-9A-Za-z]{10,}$")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class ResolveError(RuntimeError):
    """Não deu para resolver a faixa numa fonte não-DRM (mensagem acionável)."""


# --------------------------------------------------------------- recusa DRM
def refuse(url: str = "") -> dict:
    """Resposta padrão de recusa de DRM — mesma forma em TODAS as entradas.

    `{"ok": False, "drm": "spotify", "error": …}` é o formato que a API já
    devolve para HLS/DASH com Widevine: a extensão mostra "DRM (…)" e a
    CLI/impressão lê `error`.
    """
    return {
        "ok": False,
        "drm": "spotify",
        "error": (
            "DRM (spotify): as streams do Spotify são criptografadas — "
            "baixar exige contornar uma proteção tecnológica, o que viola a "
            "DMCA §1201 (EUA) e a Lei 9.610 (BR). O minidm só detecta e "
            "recusa. Fluxo legítimo: use `minidm spotify <url>` — lemos os "
            "metadados públicos e baixamos de uma fonte NÃO-DRM (YouTube)."
        ),
    }


def is_preview_url(url: str) -> bool:
    """Prévia de 30 s do Spotify (`p.scdn.co/mp3-preview/…`) — também recusa."""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return False
    return host == "p.scdn.co" or host.endswith(".p.scdn.co")


# -------------------------------------------------------------------- parse
def is_spotify_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """`(kind, id)` se `url` for link/URI do Spotify, senão `(None, None)`.

    Aceita: `open.spotify.com/<kind>/<id>` (com `/intl-xx/`, `/embed/`,
    query, barra final) e o URI de esquema `spotify:<kind>:<id>`.
    """
    u = (url or "").strip()
    if not u:
        return None, None
    # spotify:track:3n3Ppam7vgaVa1iaRUc9Lp (formato de esquema)
    if ":" in u and not urlparse(u).scheme.startswith("http"):
        parts = u.split(":")
        if len(parts) >= 3 and parts[0] == "spotify" and parts[1] in KINDS:
            sid = parts[2].split("?")[0]
            return (parts[1], sid) if _ID_RE.match(sid) else (None, None)
        if u.lower().startswith("spotify:"):
            return None, None
    try:
        p = urlparse(u)
    except ValueError:
        return None, None
    if (p.hostname or "").lower() not in ("open.spotify.com", "play.spotify.com"):
        return None, None
    segs = [s for s in p.path.split("/") if s]
    if segs and segs[0].startswith("intl-"):
        segs = segs[1:]            # /intl-br/track/…
    if segs and segs[0] == "embed":
        segs = segs[1:]            # /embed/track/…
    if len(segs) >= 2 and segs[0] in KINDS and _ID_RE.match(segs[1]):
        return segs[0], segs[1]
    return None, None


def refuse_if_spotify(url: str) -> Optional[dict]:
    """Se `url` for do Spotify (ou prévia), devolve a recusa; senão `None`.

    Chamado pelas três entradas compartilhadas: `media.analyze`, `add`
    da CLI e `POST /add` — nenhuma tarefa Spotify chega a ser enfileirada
    como download direto.
    """
    if not url:
        return None
    kind, _ = is_spotify_url(url)
    if kind or is_preview_url(url):
        return refuse(url)
    return None


# ----------------------------------------------------------------- metadados
def oembed(url: str, timeout: float = TIMEOUT) -> dict:
    """oEmbed público da Spotify (título + capa) — sem token, sem login."""
    r = requests.get("https://open.spotify.com/oembed",
                     params={"url": url}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def embed_entity(kind: str, sid: str, timeout: float = TIMEOUT) -> dict:
    """Entidade crua da página `embed/<kind>/<id>` (`__NEXT_DATA__`).

    Isolado aqui: o caminho é interno do Next.js da Spotify e pode mudar
    — se mudar, só este módulo e os testes precisam de ajuste.
    """
    r = requests.get(f"https://open.spotify.com/embed/{kind}/{sid}",
                     headers={"User-Agent": _UA}, timeout=timeout)
    r.raise_for_status()
    m = _NEXT_DATA_RE.search(r.text)
    if not m:
        raise RuntimeError("página embed do Spotify sem __NEXT_DATA__ "
                           "(layout mudou?)")
    data = json.loads(m.group(1))
    node = data
    for key in _EMBED_PATH:         # caminho verificado ao vivo
        if not isinstance(node, dict) or key not in node:
            node = _find_entity(data)
            break
        node = node[key]
    if not isinstance(node, dict):
        raise RuntimeError("não consegui ler a entidade do Spotify "
                           "na página embed")
    return node


def _find_entity(node, depth: int = 0):
    """Fallback se o caminho fixo mudar: achar a entidade por formato.

    Pista 1: um dict com `trackList` (album/playlist). Pista 2: um dict
    com `name`+`duration`+`artists` (track).
    """
    if depth > 14:
        return None
    if isinstance(node, dict):
        if "trackList" in node and "name" in node:
            return node
        if {"name", "duration", "artists"} <= set(node):
            return node
        for v in node.values():
            found = _find_entity(v, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_entity(v, depth + 1)
            if found is not None:
                return found
    return None


def _norm_track(item: dict) -> dict:
    """Normaliza uma faixa (entidade track ou item do trackList) p/ o resolve."""
    artists = item.get("artists")
    if isinstance(artists, list):
        names = [a.get("name", "") for a in artists if isinstance(a, dict)]
        artists = ", ".join(n for n in names if n)
    else:
        artists = item.get("subtitle") or ""
    dur = item.get("duration") or item.get("duration_ms") or 0
    uri = item.get("uri") or ""
    kind, sid = is_spotify_url(uri)
    return {
        "kind": kind or "track",
        "id": sid or item.get("id") or "",
        "title": (item.get("title") or item.get("name") or "").strip(),
        "artists": str(artists).strip(),
        "duration": (float(dur) / 1000.0) if dur else 0.0,   # ms -> s
        "playable": item.get("isPlayable", True),
        "reason": item.get("playabilityReason") or "",
    }


def metadata(kind: str, sid: str, url: str, timeout: float = TIMEOUT) -> dict:
    """Metadados normalizados SEM auth: `{"title", "tracks", "truncated"}`.

    * track  -> 1 faixa (artistas + duração) — embed; se o embed falhar,
      oEmbed ainda dá o título;
    * album/playlist -> `trackList` inteiro (teto ~100, `truncated=True`
      quando chega no teto e a lista completa era maior).
    """
    errs: list[str] = []
    ent: dict = {}
    try:
        ent = embed_entity(kind, sid, timeout)
    except Exception as exc:                    # rede/HTTP/layout
        errs.append(str(exc))
    title = str(ent.get("name") or ent.get("title") or "").strip()
    thumb = ""
    vi = ent.get("visualIdentity")
    if isinstance(vi, dict):
        imgs = ((vi.get("image") or [{}])[0].get("url")
                if isinstance(vi.get("image"), list) else None)
        thumb = imgs or ""
    if not title:
        try:
            oe = oembed(url, timeout)
            title = str(oe.get("title") or "").strip()
            thumb = thumb or str(oe.get("thumbnail_url") or "")
        except Exception as exc:
            errs.append(str(exc))
    if not title:
        raise RuntimeError("não consegui ler os metadados do Spotify ("
                           + "; ".join(errs) + ")")

    if kind in ("album", "playlist"):
        tl = [t for t in (ent.get("trackList") or []) if isinstance(t, dict)]
        tracks = [_norm_track(t) for t in tl]
        if not tracks:
            # embed falhou/truncou: sem lista não há o que buscar
            raise RuntimeError("não consegui a lista de faixas do Spotify ("
                               + ("; ".join(errs) or "sem trackList") + ")")
        truncated = len(tracks) >= _EMBED_CAP
        # álbum: se algum item ficou sem artista, usa o `subtitle` do álbum
        if kind == "album" and not any(t["artists"] for t in tracks):
            sub = str(ent.get("subtitle") or "")
            for t in tracks:
                t["artists"] = sub
    else:                                        # track
        t = _norm_track(ent)
        if not t["title"]:
            t["title"] = title      # só oEmbed deu título (embed falhou)
        tracks = [t]
        truncated = False
    return {"kind": kind, "id": sid, "title": title, "thumbnail": thumb,
            "tracks": tracks, "truncated": truncated}


# ------------------------------------------------------------------ resolve
def search_query(track: dict) -> str:
    """`"Artista - Título"` (primeiro artista) — a query usada no ytsearch."""
    title = (track.get("title") or "").strip()
    artists = (track.get("artists") or "").strip()
    first = artists.split(",")[0].strip() if artists else ""
    if not title:
        return first
    if first and not title.lower().startswith(first.lower()):
        return f"{first} - {title}"
    return title


def resolve(track: dict, tolerance: float = 10.0, strict: bool = False,
            no_match: bool = False, n: int = 10, referer: str = "",
            user_agent: str = "", cookie: str = "",
            cookies_from_browser: str = "",
            timeout: Optional[float] = None) -> dict:
    """Acha a faixa numa fonte NÃO-DRM e devolve o melhor candidato.

    * busca `ytsearch<n>:` via `ytdlp.describe` (~6 s, uma chamada);
    * escolhe o candidato com menor |Δ duração| vs. o Spotify (regra do
      spotdl); `no_match` confia no 1º resultado; `strict` falha se o
      melhor candidato estiver além da tolerância;
    * **gate de DRM**: candidato com `_has_drm` (ou o container inteiro)
      é descartado — se sobrar vazio, `ResolveError`.

    Retorna `{"query", "url", "title", "duration", "diff", "mismatch"}`.
    """
    from minidm.ytdlp import describe

    query = search_query(track)
    kw: dict = dict(referer=referer, user_agent=user_agent, cookie=cookie,
                    cookies_from_browser=cookies_from_browser)
    if timeout is not None:
        kw["timeout"] = timeout       # default é o do próprio ytdlp (maior)
    info = describe(f"ytsearch{n}:{query}", **kw)
    if info.get("_has_drm"):
        raise ResolveError(refuse()["error"])
    entries = [e for e in (info.get("entries") or [])
               if isinstance(e, dict) and e.get("webpage_url")
               and not e.get("_has_drm")]
    if not entries:
        raise ResolveError(
            f"não achei \"{query}\" numa fonte não-DRM — tente aumentar "
            f"--tolerance, `--no-match`, ou baixa o vídeo na mão com "
            f"`minidm add <url do YouTube>`")
    want = float(track.get("duration") or 0.0)

    def _diff(e: dict) -> float:
        dur = e.get("duration")
        return abs(float(dur) - want) if dur else float("inf")

    if no_match or not want:
        best, diff = entries[0], None
    else:
        best = min(entries, key=_diff)     # empate -> o primeiro (menor Δ)
        d = _diff(best)
        diff = None if d == float("inf") else d
    mismatch = bool(want and diff is not None and diff > tolerance)
    if mismatch and strict:
        raise ResolveError(
            f"candidato mais próximo difere {diff:.0f}s da duração do "
            f"Spotify ({want:.0f}s) — além de --tolerance {tolerance:g}s "
            f"com --strict; use `--tolerance {int(diff) + 1}` ou "
            f"`--no-match` para aceitar mesmo assim")
    return {"query": query, "url": best["webpage_url"],
            "title": best.get("title") or "", "duration": best.get("duration"),
            "diff": diff, "mismatch": mismatch}


def filename_for(track: dict) -> str:
    """Nome do arquivo a partir dos METADADOS do Spotify (não do YouTube)."""
    from minidm.remote import sanitize

    artists = (track.get("artists") or "").strip()
    title = (track.get("title") or "").strip()
    if artists and title and not title.lower().startswith(artists.lower()):
        return sanitize(f"{artists} - {title}")
    return sanitize(title or artists or "musica")
