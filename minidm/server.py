"""API HTTP local (v2): fila + controle.

Endpoints:

    GET  /status                  -> tarefas + config resumida
    GET  /task?id=                -> registro completo (propriedades)
    GET  /                        -> webui (UI no navegador/WebView)
    POST /add      {url, ...}     -> adiciona (aceita mirrors/checksum/kind)
    POST /start    {id|all}       -> inicia/pausadas
    POST /pause    {id}           -> pausa uma tarefa ativa
    POST /remove   {id, delete}   -> remove da fila
    POST /reorder  {ids}          -> reordena a fila (arrasto/subir/descer)
    POST /open     {id, what}     -> abre pasta ou arquivo no explorador
    GET  /config                  -> configuração efetiva (campos editáveis)
    POST /config  {campo: valor}  -> salva e aplica ao vivo (valida tudo)
    POST /media/analyze {url,...} -> lista qualidades de vídeo (HLS/DASH/dir.)
    GET  /update.xml              -> manifest da extensão (política forcelist)
    GET  /minidm.crx              -> pacote CRX3 assinado da extensão
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from minidm.extsetup import CRX_NAME, UPDATE_XML_NAME
from minidm.meta import Task, TaskState
from minidm.remote import auto_delegate, filename_from, page_error
from minidm.scheduler import Scheduler
from minidm.store import TaskStore


class App:
    """Contexto compartilhado entre API e scheduler."""

    def __init__(self, store: TaskStore, scheduler: Scheduler, config):
        self.store = store
        self.scheduler = scheduler
        self.config = config


# ----------------------------------------------------------------- webui
# UI web da API local: página do APK (WebView) e controle remoto no
# desktop pelo navegador (http://127.0.0.1:9742/).
_WEBUI_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def webui_dir() -> str:
    """Diretório do `webui/` em cada modo de execução.

    * dev: raiz do repositório (`minidm/server.py` -> `../webui`);
    * bundle PyInstaller (onedir): `sys._MEIPASS` = `_internal/` (o `webui`
      vai para lá como `datas` na spec);
    * APK Chaquopy: raiz do sistema de arquivos Python (o Gradle copia
      `minidm/` e `webui/` para o mesmo diretório).
    """
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "webui")


def _open_path(path: str) -> None:
    """Abre um caminho com o aplicativo padrão do sistema (estilo IDM).

    * Windows: `os.startfile` (explorer/padrão do sistema);
    * macOS: `open`;
    * Linux: `xdg-open`.

    Função de nível de módulo de propósito: os testes fazem
    `mock.patch("minidm.server._open_path")` e a API não precisa de
    desktop/gráficos para ser exercitada.
    """
    if os.name == "nt":
        os.startfile(path)  # noqa: S606 — intencional no Windows
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", path])  # noqa: S606


# --------------------------------------------- configuração editável
# Tela de configurações da webui (POST /config): só campos seguros p/
# editar por aqui. `data_dir` (identidade da fila), `api_port`/
# `api_enabled` (exigem reiniciar a API) e `cookie` (segredo) ficam
# de fora. chave -> (tipo, mínimo ou conjunto, máximo)
_CFG_EDIT: dict[str, tuple] = {
    "max_concurrent": ("int", 1, 64),
    "max_connections": ("int", 1, 512),
    "segments": ("int", 0, 128),
    "retries": ("int", 0, 100),
    "throttle_bps": ("int", 0, None),          # 0 = sem limite
    "concurrent_fragments": ("int", 0, 64),
    "playlist_limit": ("int", 1, 5000),
    "use_ytdlp_downloader": ("bool", None, None),
    "download_archive": ("bool", None, None),
    "android_public_downloads": ("bool", None, None),
    "verify_signatures": ("bool", None, None),
    "remote_checksums": ("bool", None, None),
    "cookies_from_browser": ("enum", ("", "chrome", "chromium", "edge",
                                      "firefox", "brave", "opera",
                                      "opera_gx", "vivaldi"), None),
    "sponsorblock": ("enum", ("", "all", "sponsor",
                              "sponsor,selfpromo",
                              "sponsor,selfpromo,interaction,intro,outro,"
                              "preview,music_offtopic"), None),
    "proxy": ("str", 0, 300),
    "download_base": ("str", 0, 400),
}


def _cfg_value(spec: tuple, raw) -> object:
    """Converte/valida um valor da tela de configurações.

    Levanta `TypeError`/`ValueError` para o campo errado — o `/config`
    rejeita a requisição INTEIRA (não aplica nada em meio-termo).
    """
    kind, lo, hi = spec
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, int) and raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str) and raw.lower() in ("true", "false", "1",
                                                    "0", "on", "off"):
            return raw.lower() in ("true", "1", "on")
        raise ValueError("esperado booleano")
    if kind in ("int", "float"):
        if isinstance(raw, bool):
            raise ValueError("booleano não é número")
        v = int(raw) if kind == "int" else float(raw)
        if lo is not None and v < lo:
            raise ValueError(f"mínimo {lo}")
        if hi is not None and v > hi:
            raise ValueError(f"máximo {hi}")
        return v
    if kind == "enum":                     # `lo` = conjunto permitido
        s = str(raw).strip().lower()
        if s not in lo:
            raise ValueError("valor fora da lista")
        return s
    if kind == "str":
        s = str(raw).strip()
        if hi is not None and len(s) > hi:
            raise ValueError(f"máximo {hi} caracteres")
        return s
    raise ValueError("tipo desconhecido")


# ------------------------------------------------------- nome em paralelo
def _resolve_name_bg(app: App, task_id: str, url: str, *,
                     referer: str = "", user_agent: str = "",
                     cookie: str = "", cookies_from_browser: str = "",
                     provisional: str = "",
                     settle: float = 0.8) -> None:
    """Resolve o NOME REAL da tarefa em paralelo (a fila não espera).

    A tarefa entra na fila NA HORA com o nome provisório do URL (share,
    "Baixar vídeo desta página", CLI sem `--filename`) e aqui o yt-dlp
    extrai o título em segundo plano; quando ele chega, o `filename` da
    tarefa é atualizado — a interface mostra na hora (o snapshot lê as
    tarefas vivas do store).

    `settle` deixa o scheduler reivindicar a tarefa: se ela já começou
    a baixar, SAI — o `--print before_dl` do ytdlpdl resolve o nome sem
    extraír duas vezes. Se o nome já mudou (análise prévia, before_dl),
    este resultado é descartado; erro/silêncio também — o `_finalize`
    continua sendo o fallback garantido.
    """
    try:
        time.sleep(settle)
        t = app.store.get(task_id)
        if (t is None or t.state != TaskState.QUEUED
                or t.filename != provisional):
            return                      # começou/mudou: não duplica extração
        from minidm.ytdlp import _safe_name, describe

        info = describe(url, referer=referer, user_agent=user_agent,
                        cookie=cookie,
                        cookies_from_browser=cookies_from_browser)
        title = (info.get("title") or "").strip()
        vid = (info.get("id") or "").strip()
        # mesmo formato do template `-o` ("%(title)s [%(id)s]"): o stem
        # bate com o arquivo e uma re-execução (resume) continua achando
        novo = _safe_name(f"{title} [{vid}]" if vid else title)
        if not novo:
            return
        t = app.store.get(task_id)
        if t is None or t.filename != provisional:
            return                      # before_dl/análise já nomeou
        if t.state not in (TaskState.QUEUED, TaskState.DOWNLOADING):
            return                      # pausada/removida/erro: não mexe
        t.filename = novo
        app.store.save()
    except Exception:
        pass                            # conforto: before_dl/_finalize resolve


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        # ------------------------------------------------------------ plumbing
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _json(self, obj, status=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

        def _file(self, path: str, ctype: str):
            """Serve um arquivo estático (CRX3/update.xml da extensão)."""
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                self._json({"error": "arquivo de extensão ausente — "
                                     "rode `minidm ext install`"}, 404)
                return
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _webui(self, rel: str):
            """Serve um arquivo do `webui/` (com trava contra traversal)."""
            root = os.path.realpath(webui_dir())
            try:
                target = os.path.realpath(os.path.join(root, rel))
                inside = os.path.normcase(
                    os.path.commonpath([root, target])) == os.path.normcase(root)
            except (OSError, ValueError):
                inside = False
            if not inside or not os.path.isfile(target):
                self._json({"error": "not found"}, 404)
                return
            try:
                with open(target, "rb") as f:
                    body = f.read()
            except OSError:
                self._json({"error": "not found"}, 404)
                return
            ctype = _WEBUI_TYPES.get(os.path.splitext(target)[1].lower(),
                                     "application/octet-stream")
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ------------------------------------------------------------- verbos
        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/status":
                self._json({
                    "tasks": app.scheduler.snapshot(),
                    "queue": {
                        "max_concurrent": app.config.max_concurrent,
                        "max_connections": app.config.max_connections,
                        "throttle_bps": app.config.throttle_bps,
                    },
                })
            elif path == "/profiles":
                # perfis do minidm (estilo YT-DLnis) p/ o popup escolher
                from minidm.profiles import ProfileStore

                ps = ProfileStore(app.store.data_dir)
                self._json({
                    "profiles": [{"name": p.name, "quality": p.quality,
                                  "format": p.format_target,
                                  "filename_template": p.filename_template}
                                 for p in ps.profiles],
                    "command_templates": [{"title": c.title,
                                           "content": c.content}
                                          for c in ps.command_templates],
                })
            elif path == "/categories":
                # categorias estilo IDM (pasta + perfil) p/ o popup escolher
                from minidm.categories import (default_categories,
                                               resolve_base)

                self._json({
                    "base": resolve_base(app.config),
                    "categories": [{"name": c.name, "folder": c.folder,
                                    "profile": c.profile,
                                    "default": c.default}
                                   for c in default_categories()],
                })
            elif path == "/config":
                c = app.config
                self._json({
                    "max_concurrent": c.max_concurrent,
                    "max_connections": c.max_connections,
                    "segments": c.segments,
                    "retries": c.retries,
                    "throttle_bps": c.throttle_bps,
                    # string ("", "http://host:8080") — a tela de
                    # configurações edita o valor real (não o bool)
                    "proxy": c.proxy,
                    "api_port": c.api_port,
                    # pasta interna = destino quando não há categoria —
                    # a webui mostra no placeholder do campo de destino
                    "data_dir": app.store.data_dir,
                    # tela de configurações (POST /config edita estes)
                    "concurrent_fragments": c.concurrent_fragments,
                    "playlist_limit": c.playlist_limit,
                    "cookies_from_browser": c.cookies_from_browser,
                    "sponsorblock": c.sponsorblock,
                    "use_ytdlp_downloader": c.use_ytdlp_downloader,
                    "download_archive": c.download_archive,
                    "download_base": c.download_base,
                    "android_public_downloads": c.android_public_downloads,
                    "verify_signatures": c.verify_signatures,
                    "remote_checksums": c.remote_checksums,
                })
            elif path == "/task":
                # registro COMPLETO da tarefa (diálogo de propriedades):
                # o snapshot do /status não tem dest_dir/referer/section/
                # created_at — o to_dict() devolve todos os campos
                tid = (parse_qs(urlparse(self.path).query).get("id")
                       or [""])[0]
                t = app.store.get(tid)
                if t is None:
                    self._json({"error": "tarefa não encontrada"}, 404)
                    return
                self._json(t.to_dict())
            elif path in ("/update.xml", f"/{UPDATE_XML_NAME}"):
                # manifest de atualização p/ a política ExtensionInstallForcelist
                self._file(os.path.join(app.store.data_dir, "ext",
                                        UPDATE_XML_NAME),
                           "application/xml")
            elif path in ("/minidm.crx", f"/{CRX_NAME}"):
                # pacote CRX3 assinado da extensão (auto-hospedado)
                self._file(os.path.join(app.store.data_dir, "ext", CRX_NAME),
                           "application/x-chrome-extension")
            elif path in ("/", "/index.html"):
                # webui: a própria UI servida pela API (WebView do APK e
                # controle remoto no navegador)
                self._webui("index.html")
            elif path.startswith("/webui/"):
                self._webui(path[len("/webui/"):])
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                payload = {}

            if path == "/add":
                self._add(payload)
            elif path == "/media/analyze":
                self._media_analyze(payload)
            elif path == "/open":
                # botão direito/⋯ da webui: abrir pasta ou arquivo
                self._open_task(payload)
            elif path == "/config":
                # tela de configurações da webui (persiste + aplica ao vivo)
                self._set_config(payload)
            elif path == "/start":
                ids = None if payload.get("all") else _ids(payload)
                n = app.scheduler.submit(ids)
                self._json({"ok": True, "queued": n})
            elif path == "/pause":
                ok = all(app.scheduler.pause(i) for i in _ids(payload)) if _ids(payload) else False
                self._json({"ok": ok})
            elif path == "/remove":
                ok = app.scheduler.remove(payload.get("id", ""),
                                          delete_files=bool(payload.get("delete")))
                self._json({"ok": ok}, 200 if ok else 404)
            elif path == "/reorder":
                # fila por arrasto: {ids: [..]} na nova ordem (1º = próximo)
                ids = payload.get("ids")
                if not (isinstance(ids, list) and
                        all(isinstance(i, str) for i in ids)):
                    self._json({"error": "ids: lista de strings obrigatória"},
                               400)
                    return
                order = app.store.reorder(ids)
                self._json({"ok": True, "order": order})
            else:
                self._json({"error": "not found"}, 404)

        # -------------------------------------------------------------- /add
        def _add(self, payload):
            url = payload.get("url")
            # torrent: validação própria (magnet: não é http/ftp) e
            # roteamento direto para a fila com o aria2 externo
            if url:
                from minidm.torrent import is_torrent_url

                if is_torrent_url(str(url)):
                    self._add_torrent(payload, str(url))
                    return
            if not url or urlparse(url).scheme not in ("http", "https",
                                                       "ftp", "sftp"):
                self._json({"error": "url http(s)/ftp/sftp obrigatória"}, 400)
                return

            # (C) modo playlist: expande em várias tarefas e sai — cada
            # entrada vira uma tarefa na fila (mesmo caminho do modo single).
            if payload.get("playlist"):
                self._add_playlist(payload, url)
                return

            referer = payload.get("referer", "")
            page_title = payload.get("page_title", "")
            filename = payload.get("filename", "")
            dest_dir = payload.get("dir") or app.store.data_dir
            quality_url = payload.get("quality_url", "")
            kind = payload.get("kind", "")
            profile = payload.get("profile", "")
            # escolha de qualidade vinda do cliente (extensão): rep do MPD
            # com SegmentTemplate + par de áudio p/ mux na engine própria
            rep_id = payload.get("rep_id") or ""
            mux_audio = (payload.get("audio")
                         if isinstance(payload.get("audio"), dict) else None)
            analyzed = False
            nome_url = False       # nome provisório vindo do URL (ver (N))

            # ---------------------------- categoria estilo IDM (pasta+perfil):
            # a categoria define a PASTA (base configurável + subpasta) e, se
            # o cliente não mandou perfil, também o perfil de download.
            category = payload.get("category", "")
            if category:
                from minidm.categories import (category_folder,
                                               ensure_folder, get_category)

                cat = get_category(category)
                if cat:
                    if not payload.get("dir"):
                        dest_dir = ensure_folder(
                            category_folder(cat, app.config))
                    if not profile:
                        profile = cat.profile
                else:
                    self._json({"error": f"categoria desconhecida: "
                                         f"{category}"}, 400)
                    return

            # ------------------------------ modo automático (estilo IDM):
            # o cliente manda a URL da página/mídia e o SERVIDOR analisa,
            # escolhe a qualidade e recusa DRM — igual ao "Baixar vídeo"
            # do IDM que abre a caixa de qualidades.
            if not quality_url and (kind == "auto" or payload.get("analyze")):
                from minidm.media import analyze

                res = analyze(
                    url,
                    referer=referer,
                    user_agent=payload.get("user_agent", "")
                    or app.config.user_agent,
                    cookie=payload.get("cookie", "") or app.config.cookie,
                    proxy=payload.get("proxy", "") or app.config.proxy,
                    page_title=page_title,
                    use_ytdlp=bool(payload.get("ytdlp")),
                    cookies_from_browser=payload.get("cookies_from_browser",
                                                     "")
                    or app.config.cookies_from_browser,
                )
                if not res.get("ok"):
                    # DRM e erros voltam claros para o cliente mostrar
                    self._json({"ok": False, "error": res.get("error"),
                                "drm": res.get("drm")}, 400)
                    return
                # é uma PÁGINA (YouTube, Instagram…) e não há yt-dlp p/
                # assumir: recusa com clareza — sem isso a tarefa fingia
                # sucesso (HTML ou arquivo de 0 bytes com o ID cru).
                if res.get("page"):
                    msg = page_error(url)
                    if msg:
                        self._json({"ok": False, "error": msg}, 400)
                        return
                # título descoberto na análise ( <title> do HTML ) vira
                # page_title da tarefa — exibido na interface
                page_title = page_title or res.get("page_title") or ""
                q = _pick_quality(res.get("qualities") or [],
                                  payload.get("quality"))
                if q is None:
                    self._json({"ok": False, "error": "nenhuma qualidade"}, 400)
                    return
                quality_url = q.get("url", "")
                kind = q.get("kind", "") or kind
                if not filename:
                    filename = res.get("filename", "")
                payload.setdefault("quality_label", q.get("label", ""))
                analyzed = True
                # qualidade escolhida neste analyze: a representação do
                # MPD (senão o downloader re-escolheria a maior banda) e o
                # par áudio p/ mux (senão o vídeo baixaria mudo)
                if not rep_id:
                    rep_id = (q.get("dash") or {}).get("id") or ""
                if mux_audio is None and isinstance(q.get("audio"), dict):
                    mux_audio = q["audio"]

            if not quality_url:
                quality_url = url
            if not kind or kind == "auto":
                kind = _kind_from_payload(quality_url)
            if not filename:
                # usa o nome vindo do servidor/análise, senão o do URL
                filename = payload.get("filename") or filename_from(quality_url)
                nome_url = not payload.get("filename")

            task = Task(
                url=quality_url,
                dest_dir=dest_dir,
                filename=filename,
                state=TaskState.QUEUED,
                kind=kind,
                referer=referer,
                page_title=page_title,
                quality_label=payload.get("quality_label", ""),
                origin_url=url if quality_url != url else "",
                profile=profile,
                category=category or _classify(quality_url, filename),
                section=payload.get("section", "") or "",
                mirrors=[m for m in payload.get("mirrors", [])
                         if isinstance(m, str)],
                proxy=payload.get("proxy", ""),
                cookie=payload.get("cookie", ""),
                user_agent=payload.get("user_agent", ""),
                cookies_from_browser=payload.get("cookies_from_browser",
                                                 "") or "",
                expected_sha256=payload.get("sha256"),
            )
            # (F) sponsorblock: vai como extra arg do yt-dlp
            sb = payload.get("sponsorblock", "") or app.config.sponsorblock
            if sb:
                task.ytdlp_args = (task.ytdlp_args or []) + [
                    "--sponsorblock-remove", sb]
            # ---------------------------- qualidade escolhida (engine própria):
            # `rep_id` senão o downloader do MPD com SegmentTemplate re-
            # escolheria a maior banda, ignorando o que o usuário pediu; e o
            # par `mux_audio` p/ baixar vídeo+áudio e mesclar com ffmpeg.
            if rep_id and kind == "dash":
                task.stream = {"rep_id": str(rep_id), "media": "video"}
            if mux_audio is not None:
                from minidm.deps import ffmpeg_available

                if ffmpeg_available():
                    task.mux_audio = dict(mux_audio)
            elif kind in ("hls", "dash") and not analyzed \
                    and not payload.get("quality_url"):
                # manifest direto sem análise (CLI/API): analisa rápido p/
                # achar a qualidade certa e o par de áudio. Silencioso —
                # falhou, a tarefa segue como estava.
                from minidm.media import attach_mux_hints

                attach_mux_hints(
                    task, url, referer=referer,
                    user_agent=payload.get("user_agent", "")
                    or app.config.user_agent,
                    cookie=payload.get("cookie", "") or app.config.cookie,
                    proxy=payload.get("proxy", "") or app.config.proxy)
            # perfil preenche destino/nome/args yt-dlp (estilo YT-DLnis)
            if profile:
                from minidm.profiles import ProfileStore, apply_profile

                pstore = ProfileStore(app.store.data_dir)
                apply_profile(task, pstore, profile,
                              filename_override=payload.get("filename", ""),
                              dir_override=payload.get("dir", ""),
                              height=payload.get("height"),
                              ext=payload.get("ext", ""))
            elif payload.get("height") or payload.get("ext"):
                # qualidade/formato escolhidos SEM perfil explícito:
                # monta o seletor de vídeo e delega ao yt-dlp.
                from minidm.profiles import build_video_selector
                from minidm.deps import ffmpeg_available

                sel = build_video_selector(
                    height=payload.get("height"),
                    ext=payload.get("ext", ""),
                    merge=ffmpeg_available())
                task.ytdlp_args = (task.ytdlp_args or []) + ["-f", sel]
                task.use_ytdlp_downloader = True
            if payload.get("ytdlp"):
                task.use_ytdlp_downloader = True
            elif not payload.get("quality_url"):
                # link de PÁGINA sem mídia escolhida (ex.: "Baixar vídeo
                # desta página" da extensão) -> delega ao yt-dlp sozinho.
                auto_delegate(task)
            app.store.add(task)
            if payload.get("start", True):
                app.scheduler.submit([task.id])
            # (N) nome em paralelo: a tarefa entra NA HORA com o nome do
            # URL e o título real é extraído em segundo plano enquanto ela
            # espera na fila (se já começou a baixar, o --print before_dl
            # do ytdlpdl resolve — aqui não extraímos duas vezes).
            if (nome_url and task.use_ytdlp_downloader
                    and payload.get("start", True)
                    and task.state == TaskState.QUEUED
                    and task.filename == filename):
                threading.Thread(
                    target=_resolve_name_bg, daemon=True,
                    name=f"minidm-nome-{task.id[:8]}",
                    args=(app, task.id, url),
                    kwargs={
                        "referer": referer,
                        "user_agent": payload.get("user_agent", "")
                        or app.config.user_agent,
                        "cookie": payload.get("cookie", "")
                        or app.config.cookie,
                        "cookies_from_browser":
                            payload.get("cookies_from_browser", "")
                            or app.config.cookies_from_browser,
                        "provisional": task.filename,
                    }).start()
            # (C) modo playlist: acumula o id e NÃO responde (uma resposta
            # só no fim, pelo _add_playlist).
            acc = payload.get("_playlist_ids")
            if isinstance(acc, list):
                acc.append(task.id)
                return
            self._json({"ok": True, "id": task.id, "kind": kind,
                        "filename": task.filename, "dir": task.dest_dir,
                        "profile": task.profile or None}, 201)

        # ---------------------------------------------------- /add torrent
        def _add_torrent(self, payload, url):
            """magnet:/URL .torrent -> fila (download via aria2 externo)."""
            from minidm.deps import aria2_available
            from minidm.torrent import torrent_name

            scheme = urlparse(url).scheme.lower()
            if scheme not in ("http", "https", "magnet"):
                self._json({"error": "torrent: só magnet: ou URL http(s) "
                                     "de .torrent"}, 400)
                return
            if not aria2_available():
                self._json({"error": "torrent precisa do aria2 — instale "
                                     "aria2c (`winget install aria2.aria2`, "
                                     "`apt install aria2`, `brew install "
                                     "aria2`) ou defina MINIDM_ARIA2"}, 400)
                return

            dest_dir = payload.get("dir") or app.store.data_dir
            category = payload.get("category", "")
            if category:
                from minidm.categories import (category_folder,
                                               ensure_folder, get_category)

                cat = get_category(category)
                if not cat:
                    self._json({"error": f"categoria desconhecida: "
                                         f"{category}"}, 400)
                    return
                if not payload.get("dir"):
                    dest_dir = ensure_folder(category_folder(cat,
                                                             app.config))
            filename = payload.get("filename") or torrent_name(url)
            task = Task(
                url=url, dest_dir=dest_dir, filename=filename,
                state=TaskState.QUEUED, kind="torrent",
                category=category or _classify(url, filename),
                page_title=payload.get("page_title", ""),
            )
            app.store.add(task)
            if payload.get("start", True):
                app.scheduler.submit([task.id])
            self._json({"ok": True, "id": task.id, "kind": "torrent",
                        "filename": task.filename, "dir": task.dest_dir,
                        "profile": None}, 201)

        # ------------------------------------------------------ /add playlist
        def _add_playlist(self, payload, url):
            """Expande uma playlist em várias tarefas (conceito yt-dlp)."""
            from minidm.ytdlp import expand_playlist

            limit = int(payload.get("limit") or app.config.playlist_limit)
            try:
                items = expand_playlist(
                    url, limit=limit,
                    cookie=payload.get("cookie", "") or app.config.cookie,
                    cookies_from_browser=payload.get("cookies_from_browser", "")
                    or app.config.cookies_from_browser)
            except RuntimeError as e:
                self._json({"error": f"playlist: {e}"}, 400)
                return
            if not items:
                self._json({"error": "playlist vazia"}, 400)
                return
            ids = []
            for it in items:
                sub = dict(payload)
                sub.pop("playlist", None)
                sub["url"] = it["url"]
                if it.get("title"):
                    sub["filename"] = it["title"]
                # reusa o fluxo single (cria a tarefa e devolve o id)
                sub["_playlist_ids"] = ids
                self._add(sub)
            self._json({"ok": True, "count": len(ids), "ids": ids}, 201)

        # -------------------------------------------------- /media/analyze
        def _media_analyze(self, payload):
            url = payload.get("url")
            if not url or urlparse(url).scheme not in ("http", "https",
                                                       "ftp", "sftp"):
                self._json({"ok": False,
                            "error": "url http(s)/ftp/sftp obrigatória"}, 400)
                return
            from minidm.media import analyze

            result = analyze(
                url,
                referer=payload.get("referer", ""),
                user_agent=payload.get("user_agent", "")
                or app.config.user_agent,
                cookie=payload.get("cookie", "") or app.config.cookie,
                proxy=payload.get("proxy", "") or app.config.proxy,
                page_title=payload.get("page_title", ""),
                use_ytdlp=bool(payload.get("ytdlp")),
            )
            self._json(result, 200 if result.get("ok") else 400)

        # ------------------------------------------------------------ /open
        def _open_task(self, payload):
            """Abre a pasta ou o arquivo da tarefa (menu/⋯ da webui).

            O caminho vem SEMPRE do registro da tarefa — pasta =
            onde o arquivo está (destino publicado no Android, senão
            `dest_dir`); arquivo = `published_path || final_path`.
            """
            t = app.store.get(str(payload.get("id", "")))
            if t is None:
                self._json({"error": "tarefa não encontrada"}, 404)
                return
            what = payload.get("what") or "folder"
            if what == "file":
                path = t.published_path or t.final_path
                if not path or not os.path.isfile(path):
                    self._json({"error": "arquivo ainda não existe"}, 404)
                    return
            elif what == "folder":
                # publicado (Android: Download/…): abre a pasta DO
                # arquivo; senão, a pasta de destino da tarefa
                path = (os.path.dirname(t.published_path)
                        if t.published_path else t.dest_dir)
                if not path or not os.path.isdir(path):
                    self._json({"error": "pasta ainda não existe"}, 404)
                    return
            else:
                self._json({"error": "what: folder ou file"}, 400)
                return
            try:
                _open_path(path)
            except OSError as e:
                self._json({"error": f"não deu para abrir: {e}"}, 500)
                return
            self._json({"ok": True, "path": path})

        # --------------------------------------------------------- /config
        def _set_config(self, payload):
            """Salva a configuração (tela de configurações da webui).

            Valida TUDO antes de aplicar (nada de meio-termo gravado),
            persiste em `config.json` e chama `scheduler.apply_config`
            — concorrência/limites valem sem reiniciar a API.
            """
            if not isinstance(payload, dict) or not payload:
                self._json({"error": "objeto com campos obrigatório"}, 400)
                return
            pend: dict[str, object] = {}
            errs: dict[str, str] = {}
            for k, raw in payload.items():
                spec = _CFG_EDIT.get(k)
                if spec is None:
                    errs[k] = "campo não editável"
                    continue
                try:
                    pend[k] = _cfg_value(spec, raw)
                except (TypeError, ValueError):
                    errs[k] = "valor inválido"
            if errs:
                detalhe = "; ".join(f"{k}: {v}" for k, v in errs.items())
                self._json({"error": f"config inválida ({detalhe})"}, 400)
                return
            for k, v in pend.items():
                setattr(app.config, k, v)
            try:
                app.config.save()
            except OSError as e:
                self._json({"error": f"não deu para gravar: {e}"}, 500)
                return
            app.scheduler.apply_config()   # workers/semáforo/archive
            self._json({"ok": True})

    return Handler


def _ids(payload) -> list[str]:
    ids = payload.get("id") or payload.get("ids") or []
    if isinstance(ids, str):
        ids = [ids]
    return ids


def _pick_quality(qualities: list, want):
    """Escolhe uma qualidade: `want` = índice (int), rótulo (str) ou None
    (= melhor: maior resolução/banda, depois o primeiro)."""
    if not qualities:
        return None
    if isinstance(want, int) and 0 <= want < len(qualities):
        return qualities[want]
    if isinstance(want, str) and want:
        for q in qualities:
            if q.get("label", "").lower() == want.lower():
                return q
        if want.isdigit():
            return _pick_quality(qualities, int(want))

    def score(q):
        h = q.get("height") or 0
        b = q.get("bandwidth") or 0
        # áudio puro (música) só se pedido explicitamente
        penalty = -1 if q.get("audio_only") else 0
        return (penalty, h, b)
    return max(qualities, key=score)


def _kind_from_payload(url: str) -> str:
    """Detecta o tipo pela extensão quando o cliente não informa `kind`."""
    from urllib.parse import urlparse as _up

    p = _up(url)
    if p.scheme.lower() in ("ftp", "sftp"):
        return "file"     # remoto: streams HLS/DASH são HTTP(S); arquivo puro
    path = p.path.lower()
    if path.endswith(".m3u8"):
        return "hls"
    if path.endswith(".mpd"):
        return "dash"
    return "file"


def _classify(url: str, filename: str) -> str:
    """Categoria automática (estilo IDM) quando o cliente não informou.

    Deduz pela extensão do arquivo; para HLS/DASH (que muitas vezes não
    têm extensão de vídeo no URL) usa a categoria Vídeos.
    """
    from minidm.categories import classify_filename, classify_url

    cat = classify_url(url) or classify_filename(filename)
    if not cat and _kind_from_payload(url) in ("hls", "dash"):
        cat = "Vídeos"
    return cat


def serve(app: App, port: int | None = None) -> None:
    port = port or app.config.api_port
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    print(f"API minidm em http://127.0.0.1:{port}  (Ctrl+C encerra)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
