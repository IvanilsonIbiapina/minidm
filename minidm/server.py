"""API HTTP local (v2): fila + controle.

Endpoints:

    GET  /status                  -> tarefas + config resumida
    POST /add      {url, ...}     -> adiciona (aceita mirrors/checksum/kind)
    POST /start    {id|all}       -> inicia/pausadas
    POST /pause    {id}           -> pausa uma tarefa ativa
    POST /remove   {id, delete}   -> remove da fila
    GET  /config                  -> configuração efetiva
    POST /media/analyze {url,...} -> lista qualidades de vídeo (HLS/DASH/dir.)
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from minidm.meta import Task, TaskState
from minidm.remote import filename_from
from minidm.scheduler import Scheduler
from minidm.store import TaskStore


class App:
    """Contexto compartilhado entre API e scheduler."""

    def __init__(self, store: TaskStore, scheduler: Scheduler, config):
        self.store = store
        self.scheduler = scheduler
        self.config = config


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
            elif path == "/config":
                c = app.config
                self._json({
                    "max_concurrent": c.max_concurrent,
                    "max_connections": c.max_connections,
                    "segments": c.segments,
                    "retries": c.retries,
                    "throttle_bps": c.throttle_bps,
                    "proxy": bool(c.proxy),
                    "api_port": c.api_port,
                })
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
            else:
                self._json({"error": "not found"}, 404)

        # -------------------------------------------------------------- /add
        def _add(self, payload):
            url = payload.get("url")
            if not url or urlparse(url).scheme not in ("http", "https"):
                self._json({"error": "url http(s) obrigatória"}, 400)
                return

            referer = payload.get("referer", "")
            page_title = payload.get("page_title", "")
            filename = payload.get("filename", "")
            dest_dir = payload.get("dir") or app.store.data_dir
            quality_url = payload.get("quality_url", "")
            kind = payload.get("kind", "")

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
                )
                if not res.get("ok"):
                    # DRM e erros voltam claros para o cliente mostrar
                    self._json({"ok": False, "error": res.get("error"),
                                "drm": res.get("drm")}, 400)
                    return
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

            if not quality_url:
                quality_url = url
            if not kind or kind == "auto":
                kind = _kind_from_payload(quality_url)
            if not filename:
                # usa o nome vindo do servidor/análise, senão o do URL
                filename = payload.get("filename") or filename_from(quality_url)

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
                mirrors=[m for m in payload.get("mirrors", [])
                         if isinstance(m, str)],
                proxy=payload.get("proxy", ""),
                cookie=payload.get("cookie", ""),
                user_agent=payload.get("user_agent", ""),
                expected_sha256=payload.get("sha256"),
            )
            app.store.add(task)
            if payload.get("start", True):
                app.scheduler.submit([task.id])
            self._json({"ok": True, "id": task.id, "kind": kind,
                        "filename": filename, "dir": dest_dir}, 201)

        # -------------------------------------------------- /media/analyze
        def _media_analyze(self, payload):
            url = payload.get("url")
            if not url or urlparse(url).scheme not in ("http", "https"):
                self._json({"ok": False, "error": "url http(s) obrigatória"},
                           400)
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

    path = _up(url).path.lower()
    if path.endswith(".m3u8"):
        return "hls"
    if path.endswith(".mpd"):
        return "dash"
    return "file"


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
