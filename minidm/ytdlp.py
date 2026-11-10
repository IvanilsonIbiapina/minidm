"""Integração com o yt-dlp (https://github.com/yt-dlp/yt-dlp).

O yt-dlp é dual-licenciado **Unlicense / GPLv2** — sob a Unlicense o uso
como biblioteca é permitido com qualquer licença; por isso (frente 3) ele
vai EMBUTIDO no APK (pip do Chaquopy) e nos executáveis congelados, e
aqui só o invocamos como CLI (`yt_dlp.main`) e lemos o JSON que ele
emite (dados, não código). Nada do código dele é copiado.

Fluxo: `yt-dlp -J` describe um vídeo (título, formatos, qualidade) e
convertemos no mesmo formato do `minidm.media.analyze`. Se o yt-dlp não
estiver disponível, o chamador cai no parser próprio (HLS/DASH direto).

Achamos o executável em: variável MINIDM_YTDLP > PATH (yt-dlp /
yt-dlp.exe / yt-dlp.cmd) > módulo Python embutido. Neste último caso —
APK (Chaquopy não expõe `python` em runtime) e executável congelado
(PyInstaller: `sys.executable` é o nosso .exe) — a CLI roda DENTRO do
processo via `_run_in_process` (mesmo código, mesmo JSON).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from functools import lru_cache
from typing import Optional

from minidm.deps import hidden_kwargs

TIMEOUT = 60


def find() -> Optional[list[str]]:
    """Comando para rodar o yt-dlp, ou None se não estiver disponível."""
    env = os.environ.get("MINIDM_YTDLP", "")
    if env:
        # caminho explícito: só aceita se realmente existir
        if os.path.exists(env):
            return [env]
        return None
    for name in ("yt-dlp", "yt-dlp.exe", "yt-dlp.cmd", "youtube-dl"):
        path = shutil.which(name)
        if path:
            return [path]
    # último caso: o módulo instalado como pacote Python (embutido no
    # APK/congelado `executable` pode ser vazio/inexistente — aí
    # `_in_process` decide rodar dentro do processo)
    try:
        import yt_dlp  # noqa: F401
        return [getattr(sys, "executable", "") or "", "-m", "yt_dlp"]
    except ImportError:
        return None


def available() -> bool:
    return find() is not None


def _android_runtime(exe: str) -> bool:
    """True quando o "python" é o do APK (Chaquopy) — não executa.

    No Android `sys.executable` é o `app_process` (`/proc/self/exe`),
    um binário do sistema: chamado como subprocesso ele aborta com
    SIGABRT no chown do dalvik-cache ("Error changing dalvik-cache
    ownership : Permission denied" — código -6). `ANDROID_ROOT`/
    `ANDROID_DATA` só existem no sistema Android (e no Termux, onde o
    in-process também funciona).
    """
    if os.path.basename(exe or "").startswith("app_process"):
        return True
    return bool(os.environ.get("ANDROID_ROOT")
                or os.environ.get("ANDROID_DATA"))


def _in_process(argv: list[str]) -> bool:
    """True quando não dá p/ subprocessar a CLI do yt-dlp.

    * APK (Chaquopy): não existe `python` executável em runtime — o
      `sys.executable` é o `app_process`, que **aborta** (SIGABRT);
    * executável congelado (PyInstaller): `sys.executable` é o nosso
      .exe, que não entende `-m yt_dlp`.

    Em ambos, a CLI roda no MESMO processo (`_run_in_process`) — mesmo
    código de extração, mesmo JSON na saída.
    """
    if argv[1:3] != ["-m", "yt_dlp"]:
        return False         # CLI externa de verdade (PATH/MINIDM_YTDLP)
    if getattr(sys, "frozen", False):
        return True
    if _android_runtime(argv[0]):
        return True
    return not os.path.isfile(argv[0] or "")


# ---------------------------------------------------------------------
# Redirecionamento de stdout/stderr POR THREAD.
#
# `sys.stdout` é global, mas no APK/congelado o download in-process pode
# durar minutos enquanto a extensão dispara um `describe` — um redirect
# global atropelaria os dois (o JSON da extração cairia no buffer do
# download e vice-versa). O router abaixo entrega cada escrita à thread
# dona da redirecionação (thread-local); quem não redirecionou escreve
# no stream original. Instalado só enquanto há redireção ativa.
# ---------------------------------------------------------------------
_pipe_lock = threading.Lock()
_pipes = 0                      # redireções ativas (0 = router fora)
_out_router: Optional["io.TextIOBase"] = None
_err_router: Optional["io.TextIOBase"] = None


class _ThreadRouter(io.TextIOBase):
    """`sys.stdout` que roteia escritas por thread.

    Cada thread com `_pipe` ativo escreve no próprio alvo; escritas de
    qualquer outra thread caem no stream original capturado na instalação
    (GUI/TUI/logging continuam normais).
    """

    def __init__(self, fallback):
        self._fb = fallback
        self._tl = threading.local()

    # ------------------------------------------------------------ alvo
    def push(self, target) -> Optional[io.TextIOBase]:
        """Troca o alvo da thread atual; devolve o anterior (p/ restaurar)."""
        prev = getattr(self._tl, "target", None)
        self._tl.target = target
        return prev

    # -------------------------------------------------------- TextIOBase
    def write(self, s):
        t = getattr(self._tl, "target", None)
        return (self._fb if t is None else t).write(s)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        t = getattr(self._tl, "target", None)
        try:
            (self._fb if t is None else t).flush()
        except (OSError, ValueError):
            pass

    # delegados ao stream original (o yt-dlp consulta alguns deles)
    def isatty(self):
        try:
            return self._fb.isatty()
        except (OSError, ValueError, AttributeError):
            return False

    def fileno(self):
        return self._fb.fileno()

    @property
    def encoding(self):
        return getattr(self._fb, "encoding", None) or "utf-8"

    def __getattr__(self, name):
        # qualquer outro atributo (buffer, errors, name…) vem do original
        return getattr(self.__dict__["_fb"], name)


@contextlib.contextmanager
def _pipe(out, err=None):
    """Redireciona stdout/stderr SÓ da thread que entra no contexto.

    `out`/`err` são file-likes no formato texto. O router vai para
    `sys.stdout`/`sys.stderr` enquanto houver ao menos uma redireção
    ativa no processo e sai quando a última terminar (a identidade do
    stdout original é preservada para a GUI/TUI).
    """
    global _pipes, _out_router, _err_router
    with _pipe_lock:
        if _pipes == 0:
            _out_router = _ThreadRouter(sys.stdout)
            _err_router = _ThreadRouter(sys.stderr)
            sys.stdout = _out_router
            sys.stderr = _err_router
        _pipes += 1
        prev_out = _out_router.push(out)
        prev_err = _err_router.push(err if err is not None else out)
    try:
        yield
    finally:
        with _pipe_lock:
            _out_router.push(prev_out)
            _err_router.push(prev_err)
            _pipes -= 1
            if _pipes == 0:
                # só restaura se ninguém mais mexeu no stdout nesse meio
                if sys.stdout is _out_router:
                    sys.stdout = _out_router._fb
                if sys.stderr is _err_router:
                    sys.stderr = _err_router._fb
                _out_router = _err_router = None


def _run_in_process(argv: list[str]):
    """`yt-dlp <argv>` dentro do processo (padrão CompletedProcess).

    O `yt_dlp.main` encerra com `sys.exit` (capturado) e escreve em
    `sys.stdout`/`sys.stderr` — redirecionados SÓ desta thread via `_pipe`
    (extrações e downloads in-process rodam juntos sem se atropelarem).
    `timeout` não se aplica aqui (sem subprocesso p/ matar); o subprocesso
    normal continua com.
    """
    out, err = io.StringIO(), io.StringIO()
    code: object = 0
    with _pipe(out, err):
        try:
            import yt_dlp
            yt_dlp.main(argv)
        except SystemExit as e:          # a CLI termina com sys.exit
            code = e.code or 0
            if isinstance(code, str):    # sys.exit("mensagem")
                err.write(code + "\n")
                code = 1
        except Exception as e:           # erro de extração (rc!=0)
            err.write(f"{type(e).__name__}: {e}\n")
            code = 1
    return subprocess.CompletedProcess(argv, int(code),
                                       out.getvalue().encode(),
                                       err.getvalue().encode())


def _run(args: list[str], timeout: float = TIMEOUT):
    """Roda a CLI do yt-dlp: subprocesso (normal) ou in-process.

    No modo in-process o `timeout` não se aplica (não há processo a
    matar); o redirecionamento por thread de `_pipe` mantém extrações
    simultâneas isoladas.
    """
    if _in_process(args):
        return _run_in_process(args[3:])   # pula [exe, -m, yt_dlp]
    return subprocess.run(args, capture_output=True, timeout=timeout,
                          check=False, **hidden_kwargs())


@lru_cache(maxsize=1)
def supports_remote_components() -> bool:
    """O yt-dlp instalado aceita `--remote-components`? (verifica uma vez)

    A flag existe desde 2024; versões antigas cairiam com
    "no such option". Checamos no `--help` uma vez por processo.
    """
    cmd = find()
    if not cmd:
        return False
    try:
        proc = _run(cmd + ["--help"], TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return False
    out = (proc.stdout or b"") + (proc.stderr or b"")
    return b"--remote-components" in out


def describe(url: str, referer: str = "", user_agent: str = "",
             cookie: str = "", timeout: float = TIMEOUT,
             cookies_from_browser: str = "") -> dict:
    """Roda `yt-dlp -J` e devolve o JSON bruto (ou levanta RuntimeError).

    Se o cookie do navegador fizer o site devolver a página sem formatos
    (comportamento observado no YouTube: "só imagens"/rc!=0), tenta de novo
    **sem o cookie** antes de desistir — o cookie é um extra, não um
    requisito na maioria dos sites.

    `cookies_from_browser` ("chrome", "edge"…) usa a sessão do próprio
    navegador do usuário via flag nativa do yt-dlp — só quando não veio
    `cookie` explícito.
    """
    cmd = find()
    if not cmd:
        raise RuntimeError("yt-dlp não encontrado (instale: pip install yt-dlp)")
    from minidm.deps import js_runtime

    def _args(ck: str) -> list[str]:
        a = cmd + ["-J", "--no-playlist", "--no-warnings"]
        # runtime JS: o YouTube exige para extrair todos os formatos
        js = js_runtime()
        if js:
            a += ["--js-runtimes", js]
            if supports_remote_components():
                # solver de challenge do YouTube (EJS): sem ele o yt-dlp vem
                # com a página "só imagens" (storyboards) — aí a análise não
                # acha nenhuma qualidade e o download (com cookie) erra
                # "Requested format is not available". Baixado uma vez e
                # cacheado pelo yt-dlp; se o GitHub estiver fora, é só warning.
                a += ["--remote-components", "ejs:github"]
        if referer:
            a += ["--add-header", f"Referer:{referer}"]
        if user_agent:
            a += ["--user-agent", user_agent]
        if ck:
            # o yt-dlp aceita cookies no formato "a=b; c=d" via --add-header
            a += ["--add-header", f"Cookie:{ck}"]
        elif cookies_from_browser:
            # sessão do navegador do usuário (flag nativa do yt-dlp)
            a += ["--cookies-from-browser", cookies_from_browser]
        a.append(url)
        return a

    def _exec(a: list[str]):
        try:
            # CREATE_NO_WINDOW: esta é a chamada que a extensão dispara ao
            # detectar o vídeo — sem ela o Windows abria um CMD por segundos.
            # No APK/congelado vira o runner in-process (helper _run).
            return _run(a, timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"yt-dlp falhou: {e}") from e

    def _decode(p) -> dict:
        if p.returncode != 0:
            err = (p.stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(
                f"yt-dlp: {err.splitlines()[-1] if err else 'erro'}")
        try:
            return json.loads(p.stdout.decode("utf-8", "replace"))
        except ValueError as e:
            raise RuntimeError(f"yt-dlp devolveu JSON inválido: {e}") from e

    proc = _exec(_args(cookie))
    if cookie and not _usable_info(proc):
        # cookie quebrou a extração -> segunda chance sem ele
        retry = _exec(_args(""))
        if _usable_info(retry):
            proc = retry
    return _decode(proc)


def _usable_info(proc) -> bool:
    """O `-J` trouxe formatos de verdade (com URL), ou só storyboards/erro?"""
    if proc.returncode != 0:
        return False
    try:
        info = json.loads((proc.stdout or b"").decode("utf-8", "replace"))
    except ValueError:
        return False
    return any(f.get("url") for f in (info.get("formats") or []))


def analyze(url: str, referer: str = "", user_agent: str = "",
            cookie: str = "", timeout: float = TIMEOUT,
            cookies_from_browser: str = "") -> dict:
    """Analisa com o yt-dlp e converte para o formato do minidm.media.analyze.

    Retorna {"ok", "kind", "qualities", "duration", "drm", "filename"}.
    DRM: se o yt-dlp reportar formato não disponibilizado por DRM nós
    apenas sinalizamos — nunca tentamos contornar.
    """
    info = describe(url, referer=referer, user_agent=user_agent,
                    cookie=cookie, timeout=timeout,
                    cookies_from_browser=cookies_from_browser)

    title = (info.get("title") or "").strip()
    duration = info.get("duration")
    webpage = (info.get("webpage_url_domain") or info.get("extractor_key")
               or "").lower()
    drm = None
    if any(k in webpage for k in ("netflix", "disney", "hbo", "primevideo",
                                  "paramount", "bbc_iplayer")):
        drm = webpage

    qualities = []
    for f in info.get("formats") or []:
        if f.get("ext") == "m3u8_native" or f.get("protocol") in (
                "m3u8_native", "m3u8"):
            kind = "hls"
        elif f.get("ext") == "mpd" or f.get("protocol") == "http_dash_segments":
            kind = "dash"
        else:
            kind = "file"
        height = f.get("height")
        qualities.append({
            "label": _label(f, height),
            "url": f.get("url") or f.get("fragment_base_url") or "",
            "kind": kind,
            "bandwidth": f.get("tbr") and int(f["tbr"] * 1000) or None,
            "resolution": (f"{f['width']}x{height}"
                           if f.get("width") and height else None),
            "height": height,
            "duration": duration,
            "estimated_size": None,
            "audio_only": not f.get("height"),
            "format_id": f.get("format_id", ""),
        })
    qualities = [q for q in qualities if q["url"]]
    # melhor primeiro (a UI pode reordenar; aqui já facilita)
    qualities.sort(key=lambda q: (q.get("height") or 0, q.get("bandwidth") or 0),
                   reverse=True)

    if not qualities:
        return {"ok": False, "error": "yt-dlp não listou formatos com URL"}
    if drm:
        return {"ok": False, "drm": drm,
                "error": f"conteúdo protegido por DRM ({drm}) — "
                         f"não é possível baixar"}
    return {"ok": True, "kind": "ytdlp", "qualities": qualities,
            "duration": duration, "encrypted": False, "drm": None,
            "filename": _safe_name(title)}


def _label(f: dict, height) -> str:
    bits = []
    if height:
        bits.append(f"{height}p")
    elif f.get("abr"):
        bits.append(f"{int(f['abr'])}kbps áudio")
    elif f.get("format_note"):
        bits.append(str(f["format_note"]))
    if f.get("ext"):
        bits.append(f["ext"])
    return " ".join(bits) or f.get("format_id", "formato")


def _safe_name(title: str) -> str:
    from minidm.remote import sanitize
    return sanitize(title[:80].strip(" .")) if title else ""


# ------------------------------------------------------------ playlists (C)
def expand_playlist(url: str, limit: int = 50, timeout: float = TIMEOUT,
                    cookie: str = "", cookies_from_browser: str = "") -> list[dict]:
    """Expande uma URL de playlist em entradas (conceito --flat-playlist).

    Roda `yt-dlp --flat-playlist -J` (uma extração "rasa", rápida) e
    devolve [{"url", "title", "duration"}…] — cada entrada vira uma tarefa
    na fila do minidm. `limit` corta o excesso (playlists gigantes).

    Se o URL não for playlist, o próprio yt-dlp devolve a entrada única —
    aí o chamador recebe 1 item e segue normalmente.
    """
    cmd = find()
    if not cmd:
        raise RuntimeError("yt-dlp não encontrado (instale: pip install yt-dlp)")

    args = cmd + ["--flat-playlist", "-J", "--no-warnings"]
    # --flat-playlist lista as entradas SEM extrair cada vídeo (rápido) —
    # é o modo "playlist" explícito: quem chamou quer a lista inteira.
    if cookie:
        args += ["--add-header", f"Cookie:{cookie}"]
    elif cookies_from_browser:
        args += ["--cookies-from-browser", cookies_from_browser]
    args.append(url)
    try:
        proc = _run(args, timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"yt-dlp falhou: {e}") from e
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(
            f"yt-dlp: {err.splitlines()[-1] if err else 'erro'}")
    try:
        info = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError as e:
        raise RuntimeError(f"yt-dlp devolveu JSON inválido: {e}") from e

    entries = info.get("entries")
    if entries is None:
        # não é playlist: o -J devolveu o vídeo direto
        u = info.get("webpage_url") or url
        return [{"url": u, "title": info.get("title") or "",
                 "duration": info.get("duration")}] if u else []
    out: list[dict] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        u = e.get("url") or e.get("webpage_url") or ""
        if not u:
            continue
        out.append({"url": u, "title": e.get("title") or "",
                    "duration": e.get("duration")})
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------- versão/idade (E)
@lru_cache(maxsize=1)
def version() -> Optional[str]:
    """Versão do yt-dlp instalado ("2026.10.26"), ou None."""
    cmd = find()
    if not cmd:
        return None
    try:
        proc = _run(cmd + ["--version"], TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or b"").decode("utf-8", "replace").strip() or None


def is_outdated(days: int = 90) -> Optional[bool]:
    """True se a versão tem mais de `days` dias (sites mudam; vale atualizar).

    O upstream faz a mesma checagem (avisa >90 dias). Versões no formato
    `YYYY.MM.DD` são datadas; outras (nightly com sufixo) também casam no
    prefixo. Devolve None quando não dá p/ saber (sem yt-dlp, formato
    irreconhecível).
    """
    v = version()
    if not v:
        return None
    import re
    from datetime import date

    m = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", v)
    if not m:
        return None
    try:
        built = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return (date.today() - built).days > days
