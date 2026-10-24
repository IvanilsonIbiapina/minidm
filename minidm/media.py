"""Detecção de mídia e análise de qualidades (HLS / DASH / arquivo direto).

Espelha o fluxo do IDM ao capturar um vídeo no navegador:

1. o monitor (extensão/webRequest) detecta a URL de mídia;
2. o gerenciador "abre" o manifest e lista as qualidades disponíveis
   (HLS: variantes do master m3u8; DASH: Representation do MPD;
   direto: o próprio arquivo com tamanho do servidor);
3. o usuário escolhe — o download usa os mesmos headers da página
   (Referer, User-Agent, Cookie) que o player usaria.

Só parsing + sondagem: nenhum download de stream acontece aqui
(ver `minidm/stream.py`).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

from minidm.remote import TIMEOUT, USER_AGENT, sanitize

MIME_HLS = ("application/vnd.apple.mpegurl", "application/x-mpegurl",
            "audio/mpegurl", "audio/x-mpegurl")
MIME_DASH = ("application/dash+xml",)

_ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)=("(?:[^"]*)"|[^,]*)')
_BYTERANGE_RE = re.compile(r"^(\d+)(?:@(\d+))?$")
_ISO_DUR_RE = re.compile(
    r"^PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?$",
    re.IGNORECASE)

# ------------------------------------------------------------ DRM (detecção)
# Só DETECTAMOS e reportamos — decodificação de DRM não é suportada (nem o
# IDM baixa conteúdo com Widevine/FairPlay; a chave vem de um CDM licenciado
# e contornar a proteção viola DMCA §1201 / Lei 9.610).
DRM_HLS_METHODS = {"SAMPLE-AES", "SAMPLE-AES-CTR", "SAMPLE-AES-CSM",
                   "ISO-23001-7"}
_UUID_TO_DRM = {
    "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed": "widevine",
    "9a04f079-9840-4286-ab92-e65be0885f95": "playready",
    "94ce8646-4741-4c55-8c84-4a55f6b3f6c4": "fairplay",
    "1077efec-c0b2-4d02-ace3-3c1e52e2fb4b": "marlin",
}
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# nomes genéricos de playlist não servem como nome de arquivo
_GENERIC_BASENAMES = {
    "master", "index", "playlist", "stream", "video", "media", "manifest",
    "chunk", "seg", "segment", "get", "play", "output", "default", "content",
    "file", "data", "hls", "dash", "player", "v", "a", "video_player",
}


def build_headers(referer: str = "", user_agent: str = "",
                  cookie: str = "") -> dict:
    """Headers que o player usaria — sem eles muitos servidores recusam."""
    h = {"User-Agent": user_agent or USER_AGENT}
    if referer:
        h["Referer"] = referer
    if cookie:
        h["Cookie"] = cookie
    return h


# --------------------------------------------------------------- HLS (m3u8)
def parse_attrs(text: str) -> dict:
    """`BANDWIDTH=1280000,RESOLUTION=720x480,CODECS="a,b"` -> dict."""
    out = {}
    for k, v in _ATTR_RE.findall(text):
        out[k.upper()] = v[1:-1] if v.startswith('"') else v
    return out


def _parse_byterange(value: str) -> Optional[tuple[int, int]]:
    """`1234@567` -> (length, offset); `1234` -> (length, None)."""
    m = _BYTERANGE_RE.match(value.strip())
    if not m:
        return None
    return int(m.group(1)), (int(m.group(2)) if m.group(2) else None)


def _key_from_attrs(attrs: dict, base_url: str) -> dict:
    key = {"method": attrs.get("METHOD", "NONE").upper(),
           "uri": urljoin(base_url, attrs["URI"]) if attrs.get("URI") else None,
           "iv": attrs.get("IV", "").lower() if attrs.get("IV") else None,
           "keyformat": attrs.get("KEYFORMAT", ""),
           "drm": None}
    key["drm"] = _drm_from_key(key)
    return key


def _drm_from_key(key: dict) -> Optional[str]:
    """Método/KEYFORMAT de uma EXT-X-KEY -> nome do DRM (ou None se descartável)."""
    method = (key.get("method") or "").upper()
    if method in ("NONE", "AES-128"):
        # AES-128 comum é stream descartável (sem DRM) — suportamos
        return None
    kf = (key.get("keyformat") or "").lower()
    if "widevine" in kf or "com.widevine" in kf:
        return "widevine"
    if "fairplay" in kf or "com.apple.streamingkeydelivery" in kf:
        return "fairplay"
    if "playready" in kf:
        return "playready"
    if method in DRM_HLS_METHODS:
        # método de DRM sem keyformat reconhecido
        return "drm ({} {})".format(method, key.get("keyformat") or "").strip()
    return None


def drm_from_content_protection(scheme_uri: str) -> Optional[str]:
    """schemeIdUri de <ContentProtection> DASH -> nome do DRM (ou None)."""
    s = (scheme_uri or "").lower()
    if not s:
        return None
    if "urn:uuid" in s:
        m = _UUID_RE.search(s)
        if m:
            return _UUID_TO_DRM.get(m.group(0).lower(), "drm")
    if "widevine" in s:
        return "widevine"
    if "playready" in s:
        return "playready"
    if "fairplay" in s:
        return "fairplay"
    if "marlin" in s:
        return "marlin"
    if "ctr" in s or "cenc" in s or "mp4protection" in s:
        return "cenc"        # pode ser só criptografia, mas sinaliza proteção
    return None


def _drm_from_content_protection_nodes(nodes) -> Optional[str]:
    """Primeiro <ContentProtection> que sinaliza um DRM conhecido."""
    for node in nodes:
        d = drm_from_content_protection(node.attrib.get("schemeIdUri", ""))
        if d:
            return d
    return None


def parse_m3u8(text: str, base_url: str) -> dict:
    """Parse de playlist M3U8 (master ou media).

    Master -> {"type": "master", "variants": [...], "alternates": [...]}
    Media  -> {"type": "media", "segments": [...], "duration": ...,
               "init": ..., "endlist": bool, "encrypted": bool}
    """
    lines = [ln.strip() for ln in text.splitlines()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ValueError("não é um playlist M3U8 (#EXTM3U ausente)")

    is_master = any(ln.startswith("#EXT-X-STREAM-INF")
                    for ln in lines)
    if is_master:
        return _parse_m3u8_master(lines, base_url)
    return _parse_m3u8_media(lines, base_url)


def _parse_m3u8_master(lines: list[str], base_url: str) -> dict:
    variants, alternates = [], []
    session_drm = []
    pending_attrs = None
    pending_type = None          # None | "media" (EXT-X-MEDIA)
    for ln in lines:
        if ln.startswith("#EXT-X-STREAM-INF:"):
            pending_attrs = parse_attrs(ln.split(":", 1)[1])
            pending_type = None
        elif ln.startswith("#EXT-X-SESSION-KEY:"):
            # sinal de DRM já no master (Widevine/FairPlay costumam avisar aqui)
            attrs = parse_attrs(ln.split(":", 1)[1])
            d = _drm_from_key(_key_from_attrs(attrs, base_url))
            if d:
                session_drm.append(d)
        elif ln.startswith("#EXT-X-MEDIA:"):
            attrs = parse_attrs(ln.split(":", 1)[1])
            if attrs.get("URI"):      # faixa legenda/áudio com URL própria
                alternates.append({
                    "url": urljoin(base_url, attrs["URI"]),
                    "type": (attrs.get("TYPE") or "AUDIO").lower(),
                    "name": attrs.get("NAME", ""),
                    "language": attrs.get("LANGUAGE", ""),
                    "default": attrs.get("DEFAULT", "").upper() == "YES",
                })
        elif ln.startswith("#"):
            continue
        elif ln and pending_attrs is not None:
            res = pending_attrs.get("RESOLUTION", "")
            variants.append({
                "url": urljoin(base_url, ln),
                "bandwidth": int(pending_attrs["BANDWIDTH"])
                if pending_attrs.get("BANDWIDTH", "").isdigit() else None,
                "average_bandwidth": int(pending_attrs["AVERAGE-BANDWIDTH"])
                if pending_attrs.get("AVERAGE-BANDWIDTH", "").isdigit() else None,
                "resolution": res,
                "width": int(res.split("x")[0]) if "x" in res else None,
                "height": int(res.split("x")[1]) if "x" in res else None,
                "frame_rate": pending_attrs.get("FRAME-RATE"),
                "codecs": pending_attrs.get("CODECS", ""),
                "name": pending_attrs.get("NAME", ""),
            })
            pending_attrs = None
        elif ln and pending_attrs is None:
            pass  # URI solta sem atributos — ignora
    # DRM do master vale para todas as variantes (a menos que a variante
    # tenha playlist própria sem DRM — isso só dá pra ver baixando ela)
    drm = session_drm[0] if session_drm else None
    for v in variants:
        v["drm"] = drm
    return {"type": "master", "variants": variants, "alternates": alternates,
            "drm": drm}


def _parse_m3u8_media(lines: list[str], base_url: str) -> dict:
    target_duration = None
    media_sequence = 0
    playlist_type = None
    endlist = False
    init = None
    segments = []
    current_key = None
    pending_dur = None
    pending_range = None
    last_range_url = None
    last_offset = 0

    for ln in lines:
        if ln.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target_duration = float(ln.split(":", 1)[1])
            except ValueError:
                pass
        elif ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(ln.split(":", 1)[1])
            except ValueError:
                pass
        elif ln.startswith("#EXT-X-PLAYLIST-TYPE:"):
            playlist_type = ln.split(":", 1)[1].strip().upper()
        elif ln.startswith("#EXT-X-ENDLIST"):
            endlist = True
        elif ln.startswith("#EXT-X-KEY:"):
            attrs = parse_attrs(ln.split(":", 1)[1])
            method = attrs.get("METHOD", "NONE").upper()
            current_key = None if method == "NONE" else _key_from_attrs(
                attrs, base_url)
        elif ln.startswith("#EXT-X-MAP:"):
            attrs = parse_attrs(ln.split(":", 1)[1])
            br = _parse_byterange(attrs.get("BYTERANGE", "")) \
                if attrs.get("BYTERANGE") else None
            init = {"url": urljoin(base_url, attrs["URI"]),
                    "byterange": (br[0], br[1] or 0) if br else None}
        elif ln.startswith("#EXTINF:"):
            body = ln.split(":", 1)[1]
            try:
                pending_dur = float(body.split(",", 1)[0])
            except ValueError:
                pending_dur = None
        elif ln.startswith("#EXT-X-BYTERANGE:"):
            pending_range = _parse_byterange(ln.split(":", 1)[1])
        elif ln.startswith("#"):
            continue
        elif ln:                                   # linha de URI do segmento
            url = urljoin(base_url, ln)
            byterange = None
            if pending_range is not None:
                length, offset = pending_range
                if offset is None:
                    if last_range_url == url:
                        offset = last_offset
                    else:
                        offset = 0
                byterange = (length, offset)
                last_offset = offset + length
                last_range_url = url
            segments.append({
                "url": url,
                "duration": pending_dur,
                "byterange": byterange,
                "key": current_key,
                "sequence": media_sequence + len(segments),
            })
            pending_dur = None
            pending_range = None

    duration = sum(s["duration"] or 0 for s in segments)
    # DRM: primeira chave que sinalizar Widevine/FairPlay/etc. vira o tipo
    drm = None
    for s in segments:
        k = s.get("key")
        if k and k.get("drm"):
            drm = k["drm"]
            break
    return {
        "type": "media",
        "target_duration": target_duration,
        "duration": duration or None,
        "playlist_type": playlist_type,
        "endlist": endlist,
        "init": init,
        "segments": segments,
        "encrypted": any(s["key"] for s in segments),
        "drm": drm,
    }


# --------------------------------------------------------------- DASH (MPD)
def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _local(el) -> str:
    return _strip_ns(el.tag)


def _child(el, name: str):
    for c in el:
        if _local(c) == name:
            return c
    return None


def _children(el, name: str):
    return [c for c in el if _local(c) == name]


def parse_duration_iso(value: str) -> Optional[float]:
    """`PT1H2M3.5S` -> 3723.5"""
    if not value:
        return None
    m = _ISO_DUR_RE.match(value.strip())
    if not m:
        return None
    h, mn, s = (float(x) if x else 0.0 for x in m.groups())
    return h * 3600 + mn * 60 + s


def _parse_template(el, base_url: str, inherited: dict | None) -> dict | None:
    node = el if _local(el) == "SegmentTemplate" else _child(el, "SegmentTemplate")
    if node is None and not inherited:
        return None
    attrs = dict(inherited or {})
    if node is not None:
        attrs.update(node.attrib)
    if not attrs.get("media") and not attrs.get("initialization"):
        return None
    timeline = None
    if node is not None:
        tl = _child(node, "SegmentTimeline")
        if tl is not None:
            timeline = []
            for s in _children(tl, "S"):
                timeline.append({k: int(v) for k, v in s.attrib.items()
                                 if v.lstrip("-").isdigit()})
    return {
        "media": attrs.get("media", ""),
        "initialization": attrs.get("initialization", ""),
        "start_number": int(attrs.get("startNumber", 1)),
        "duration": int(attrs["duration"]) if attrs.get("duration", "").isdigit() else None,
        "timescale": int(attrs.get("timescale", 1)),
        "timeline": timeline,
    }


def _parse_segment_list(node, base_url: str) -> dict | None:
    segs = []
    init = None
    for c in node:
        name = _local(c)
        if name == "SegmentURL":
            media = c.attrib.get("media", "")
            rng = c.attrib.get("range", "")
            byterange = None
            if rng and "@" in rng:
                length, offset = rng.split("@", 1)
                byterange = (int(length), int(offset))
            segs.append({"url": urljoin(base_url, media) if media else base_url,
                         "byterange": byterange})
        elif name == "Initialization":
            src = c.attrib.get("sourceURL", "")
            rng = c.attrib.get("range", "")
            byterange = None
            if rng and "@" in rng:
                length, offset = rng.split("@", 1)
                byterange = (int(length), int(offset))
            init = {"url": urljoin(base_url, src) if src else base_url,
                    "byterange": byterange}
    if not segs and init is None:
        return None
    return {"segments": segs, "init": init}


def parse_mpd(text: str, base_url: str) -> dict:
    """Parse de manifesto DASH (MPD) — retorna duração + representações."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"MPD inválido: {e}") from e
    if _local(root) != "MPD":
        raise ValueError("não é um manifesto DASH (<MPD> ausente)")

    duration = parse_duration_iso(root.attrib.get("mediaPresentationDuration", ""))
    mpd_base = urljoin(base_url, _child_text(root, "BaseURL") or "")

    representations = []
    for period in _children(root, "Period"):
        p_base = urljoin(mpd_base, _child_text(period, "BaseURL") or "")
        p_dur = parse_duration_iso(period.attrib.get("duration", "")) or duration
        for aset in _children(period, "AdaptationSet"):
            mime = (aset.attrib.get("mimeType")
                    or aset.attrib.get("contentType") or "").lower()
            a_base = urljoin(p_base, _child_text(aset, "BaseURL") or "")
            inherited_tpl = _parse_template(aset, a_base, None)
            # DRM declarado no AdaptationSet vale para as representações
            aset_drm = _drm_from_content_protection_nodes(
                _children(aset, "ContentProtection"))
            for rep in _children(aset, "Representation"):
                r_mime = (rep.attrib.get("mimeType") or mime).lower()
                r_base = urljoin(a_base, _child_text(rep, "BaseURL") or "")
                rep_drm = _drm_from_content_protection_nodes(
                    _children(rep, "ContentProtection")) or aset_drm
                entry = {
                    "id": rep.attrib.get("id", ""),
                    "bandwidth": int(rep.attrib["bandwidth"])
                    if rep.attrib.get("bandwidth", "").isdigit() else None,
                    "width": int(rep.attrib["width"])
                    if rep.attrib.get("width", "").isdigit() else None,
                    "height": int(rep.attrib["height"])
                    if rep.attrib.get("height", "").isdigit() else None,
                    "codecs": rep.attrib.get("codecs", ""),
                    "mimeType": r_mime,
                    "url": r_base,
                    "duration": p_dur,
                    "init": None,
                    "template": None,
                    "list": None,
                    "drm": rep_drm,
                }
                tpl = _parse_template(rep, r_base, inherited_tpl)
                if tpl:
                    entry["template"] = tpl
                    init_uri = tpl.get("initialization", "")
                    if init_uri:
                        entry["init"] = {"url": urljoin(r_base, init_uri),
                                         "byterange": None}
                else:
                    sl = _child(rep, "SegmentList")
                    if sl is not None:
                        parsed = _parse_segment_list(sl, r_base)
                        if parsed:
                            entry["list"] = parsed
                            entry["init"] = parsed["init"]
                representations.append(entry)

    # DRM agregado: se QUALQUER representação tem DRM, o stream tem DRM
    drm = None
    for r in representations:
        if r.get("drm"):
            drm = r["drm"]
            break
    return {"type": "dash", "duration": duration,
            "representations": representations, "drm": drm}


def _child_text(el, name: str) -> Optional[str]:
    node = _child(el, name)
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


# ------------------------------------------------------------------ análise
def _kind_from_url(url: str) -> str:
    path = urlparse(url).path.lower()
    if path.endswith(".m3u8"):
        return "hls"
    if path.endswith(".mpd"):
        return "dash"
    return ""


def _sniff_kind(prefix: bytes) -> str:
    head = prefix.lstrip()[:2048]
    if head.startswith(b"#EXTM3U"):
        return "hls"
    if head.startswith(b"<?xml") or b"<MPD" in head[:512]:
        return "dash"
    return ""


def _quality_label(resolution: str, bandwidth: Optional[int],
                   name: str = "", height: Optional[int] = None) -> str:
    if name:
        return name
    if resolution:
        if "x" in resolution:
            try:
                h = int(resolution.split("x")[1])
                return f"{h}p ({resolution})"
            except ValueError:
                pass
        return resolution
    if height:
        return f"{height}p"
    if bandwidth:
        return f"{bandwidth / 1_000_000:.1f} Mbps"
    return "original"


def _estimate_media_size(session, media: dict, headers: dict,
                         timeout: float) -> Optional[int]:
    """Estima o tamanho somando o primeiro segmento x nº de segmentos."""
    segs = media.get("segments") or []
    if not segs:
        return None
    first = segs[0]
    try:
        h = dict(headers)
        if first.get("byterange"):
            length, offset = first["byterange"]
            h["Range"] = f"bytes={offset}-{offset + length - 1}"
        r = session.get(first["url"], headers=h, stream=True, timeout=timeout)
        try:
            size = int(r.headers.get("Content-Length") or 0) or None
        finally:
            r.close()
        if not size:
            return None
        return size * len(segs)
    except requests.RequestException:
        return None


def _dash_qualities(session, dash: dict, headers: dict, base_url: str,
                    timeout: float) -> list[dict]:
    qualities = []
    reps = dash.get("representations") or []
    # prefere vídeo; se não houver, áudio
    video = [r for r in reps if r["mimeType"].startswith("video")]
    pool = video or [r for r in reps if r["mimeType"].startswith("audio")]
    for rep in pool:
        tpl, lst = rep.get("template"), rep.get("list")
        kind = "dash"
        url = rep["url"]
        size = None
        if tpl:
            # URL do manifest: o stream downloader resolve $Number$/$Time$
            url = base_url
        elif lst:
            url = base_url
            size = sum(s["byterange"][0] if s.get("byterange") else 0
                       for s in lst["segments"]) or None
        else:
            kind = "file"           # BaseURL direto -> arquivo comum
            try:
                r = session.head(url, headers=headers, allow_redirects=True,
                                 timeout=timeout)
                size = int(r.headers.get("Content-Length") or 0) or None
            except requests.RequestException:
                size = None
        label = _quality_label(
            f"{rep['width']}x{rep['height']}"
            if rep["width"] and rep["height"] else "",
            rep["bandwidth"], height=rep["height"])
        qualities.append({
            "label": label,
            "url": url,
            "kind": kind,
            "bandwidth": rep["bandwidth"],
            "resolution": (f"{rep['width']}x{rep['height']}"
                           if rep["width"] and rep["height"] else None),
            "duration": rep.get("duration") or dash.get("duration"),
            "estimated_size": size,
            "dash": rep if kind == "dash" else None,
        })
    qualities.sort(key=lambda q: q["bandwidth"] or 0, reverse=True)
    return qualities


def analyze(url: str, referer: str = "", user_agent: str = "",
            cookie: str = "", proxy: str = "",
            timeout: float = TIMEOUT, page_title: str = "",
            use_ytdlp: bool = False) -> dict:
    """Analisa uma URL de mídia e lista as qualidades disponíveis.

    Retorna {"ok", "kind", "qualities": [...], "duration", "encrypted",
    "drm", "filename"} ou {"ok": False, "error": ..., "drm": ...}.

    `page_title`: título da página (o navegador pode passar) — melhora o
    nome do arquivo quando o URL é genérico (master.m3u8, get.php…).
    `use_ytdlp`: delega a análise para o yt-dlp (se instalado) — cobre
    sites complexos; se falhar, cai no parser HLS/DASH próprio.
    """
    if use_ytdlp:
        try:
            from minidm import ytdlp
            res = ytdlp.analyze(url, referer=referer, user_agent=user_agent,
                                cookie=cookie, timeout=timeout)
            if res.get("ok") or res.get("drm"):
                # DRM ou sucesso: não tenta o parser próprio por cima
                return res
            # falha comum (site não suportado): cai para o parser próprio
        except (ImportError, RuntimeError):
            # yt-dlp ausente ou falhou por completo — parser próprio
            pass
    headers = build_headers(referer, user_agent, cookie)
    session = requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    try:
        kind = _kind_from_url(url)
        text = None
        direct_size = None
        direct_type = None
        content_disposition = None

        if not kind:
            # precisa descobrir o que é: GET parcial + content-type
            r = session.get(url, headers=headers, stream=True,
                            timeout=timeout, allow_redirects=True)
            try:
                if r.status_code >= 400:
                    return {"ok": False,
                            "error": f"servidor respondeu HTTP {r.status_code}"}
                direct_type = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                content_disposition = r.headers.get("Content-Disposition")
                prefix = b""
                for chunk in r.iter_content(2048):
                    prefix += chunk
                    if len(prefix) >= 2048:
                        break
                sniffed = _sniff_kind(prefix)
                if sniffed or direct_type in MIME_HLS or direct_type in MIME_DASH:
                    kind = sniffed or ("hls" if direct_type in MIME_HLS else "dash")
                    body = prefix
                    for chunk in r.iter_content(64 * 1024):
                        body += chunk
                        if len(body) > 4 * 1024 * 1024:
                            break
                    text = body.decode("utf-8", "replace")
                else:
                    cl = r.headers.get("Content-Length")
                    cr = r.headers.get("Content-Range")
                    if cl and cl.isdigit():
                        direct_size = int(cl)
                    elif cr and "/" in cr:
                        total = cr.split("/")[-1]
                        if total.isdigit():
                            direct_size = int(total)
            finally:
                r.close()
        else:
            resp = session.get(url, headers=headers, timeout=timeout)
            if resp.status_code >= 400:
                return {"ok": False,
                        "error": f"servidor respondeu HTTP {resp.status_code}"}
            content_disposition = resp.headers.get("Content-Disposition")
            text = resp.text

        if kind == "hls":
            media = parse_m3u8(text, url)
            if media.get("drm"):
                return {"ok": False, "drm": media["drm"],
                        "error": (f"conteúdo protegido por DRM "
                                  f"({media['drm']}) — não é possível baixar")}
            if media["type"] == "master":
                qualities = []
                for v in media["variants"]:
                    qualities.append({
                        "label": _quality_label(v["resolution"], v["bandwidth"],
                                                v["name"], v["height"]),
                        "url": v["url"],
                        "kind": "hls",
                        "bandwidth": v["bandwidth"],
                        "resolution": v["resolution"] or None,
                        "duration": None,
                        "estimated_size": None,
                    })
                # faixas de áudio separadas (música) — só quando há faixa
                # própria com URL; HLS áudio puro costuma vir assim
                for alt in media["alternates"]:
                    if alt.get("type") == "audio" and alt.get("url"):
                        qualities.append({
                            "label": f"áudio — {alt.get('name') or alt.get('language') or 'faixa'}",
                            "url": alt["url"],
                            "kind": "hls",
                            "bandwidth": None,
                            "resolution": None,
                            "duration": None,
                            "estimated_size": None,
                            "audio_only": True,
                        })
                return {"ok": True, "kind": "hls-master", "qualities": qualities,
                        "alternates": media["alternates"],
                        "duration": None, "encrypted": False,
                        "filename": _suggest_name(url, content_disposition,
                                                  page_title)}
            # playlist de mídia direto: estima tamanho pelo 1º segmento
            size = _estimate_media_size(session, media, headers, timeout)
            return {"ok": True, "kind": "hls-media",
                    "qualities": [{
                        "label": "original",
                        "url": url,
                        "kind": "hls",
                        "bandwidth": None,
                        "resolution": None,
                        "duration": media["duration"],
                        "estimated_size": size,
                    }],
                    "duration": media["duration"],
                    "encrypted": media["encrypted"],
                    "filename": _suggest_name(url, content_disposition,
                                              page_title)}

        if kind == "dash":
            dash = parse_mpd(text, url)
            if dash.get("drm"):
                return {"ok": False, "drm": dash["drm"],
                        "error": (f"conteúdo protegido por DRM "
                                  f"({dash['drm']}) — não é possível baixar")}
            qualities = _dash_qualities(session, dash, headers, url, timeout)
            # se houver representação só de áudio, marca como tal
            for q in qualities:
                rep = q.get("dash") or {}
                if rep.get("mimeType", "").startswith("audio"):
                    q["audio_only"] = True
            return {"ok": True, "kind": "dash", "qualities": qualities,
                    "duration": dash.get("duration"),
                    "encrypted": False,
                    "filename": _suggest_name(url, content_disposition,
                                              page_title)}

        # arquivo direto (mp4/webm/etc.)
        filename = _suggest_name(url, content_disposition, page_title)
        return {"ok": True, "kind": "file", "qualities": [{
            "label": "original",
            "url": url,
            "kind": "file",
            "bandwidth": None,
            "resolution": None,
            "duration": None,
            "estimated_size": direct_size,
        }], "duration": None, "encrypted": False,
            "filename": filename or "download.bin"}
    except requests.RequestException as e:
        return {"ok": False, "error": f"falha de rede: {e}"}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    finally:
        session.close()


def _suggest_name(url: str, content_disposition: str = "",
                  page_title: str = "") -> str:
    """Nome de arquivo legível, na ordem de preferência:

    1. `Content-Disposition` do servidor (mais confiável);
    2. último segmento do URL, se não for genérico;
    3. `page_title` (título da página, vindo do navegador);
    4. "video".

    Remove extensão de playlist (.m3u8/.mpd) e decodifica %XX.
    """
    from minidm.remote import filename_from

    # 1) Content-Disposition
    if content_disposition:
        name = filename_from(url, content_disposition)
        if name and name != "download.bin":
            return sanitize(_strip_playlist_ext(name))

    # 2) último segmento do URL, se não genérico
    raw = filename_from(url)
    stem = _strip_playlist_ext(raw)
    if stem and _stem_is_useful(stem):
        return sanitize(stem)

    # 3) título da página (o navegador manda; corta em tamanho razoável)
    if page_title:
        title = sanitize(page_title.strip())[:80].strip(" .")
        if title and _stem_is_useful(title):
            return sanitize(_strip_playlist_ext(title))

    # 4) fallback: usa o stem mesmo se genérico, senão "video"
    return sanitize(stem or "video")


def _strip_playlist_ext(name: str) -> str:
    for ext in (".m3u8", ".mpd"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


def _stem_is_useful(stem: str) -> bool:
    """False se o nome é genérico/curto demais para servir de nome de arquivo."""
    s = stem.strip().lower()
    if len(s) < 3:
        return False
    # "get.php", "video.mp4", "play.aspx" -> checa também sem a extensão
    candidates = {s}
    if "." in s:
        candidates.add(s.rsplit(".", 1)[0])
    if any(c in _GENERIC_BASENAMES for c in candidates):
        return False
    # só dígitos, só hex longo, ou padrão de hash/id
    if s.isdigit():
        return False
    if re.fullmatch(r"[0-9a-f]{16,}", s):
        return False
    # código de CDN: letras consoantes + dígitos sem nenhuma vogal
    # ("x36xhzz", "0a3f9b") — não é um nome legível; melhor o título
    if re.search(r"\d", s) and not re.search(r"[aeiouyáéíóúâêôãõ]", s):
        return False
    return True


def suggest_extension(kind: str, init: dict | None = None) -> str:
    """Extensão do arquivo final: HLS->.ts (ou .mp4 se fMP4), DASH->.mp4."""
    if kind == "hls":
        # segmentos fMP4 (.m4s) com EXT-X-MAP geram .mp4; TS gera .ts
        if init and init.get("url", "").lower().endswith(".mp4"):
            return ".mp4"
        return ".ts"
    if kind == "dash":
        return ".mp4"
    return ""
