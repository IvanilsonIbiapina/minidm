"""Downloader que delega ao yt-dlp (CLI; dual-licenciado Unlicense/GPLv2).

Usado quando o perfil manda (`use_ytdlp_downloader`) ou a config global
liga `use_ytdlp_downloader`, e o yt-dlp está disponível. A engine própria
(HLS/DASH/arquivo) continua sendo o fallback.

Interface igual aos outros downloaders (scheduler não muda):
`run()`, `request_stop()`, `speed_bps()`. O progresso vem do parse das
linhas `[download] 12.3% of ~10.00MiB at 1.00MiB/s ETA 00:10`.

Com a CLI externa o yt-dlp roda em subprocesso; no APK/PyInstaller o
módulo é embutido e o `_spawn` usa o caminho in-process
(`_spawn_in_process`) — mesma leitura de progresso, mesma interface.
"""

from __future__ import annotations

import io
import os
import queue
import re
import subprocess
import threading
import time
from typing import Optional

from minidm.config import AppConfig
from minidm.deps import ffmpeg_dir, js_runtime
from minidm.meta import Task, TaskState
from minidm.ytdlp import _in_process, _pipe, find as find_ytdlp

# [download]  12.3% of ~ 10.00MiB at  1.00MiB/s ETA 00:10
_PROGRESS_RE = re.compile(
    r"\[download\]\s+([\d.]+)%\s+of\s+~?\s*([\d.]+)([KMG]?i?B)")
# a velocidade impressa pelo próprio yt-dlp na mesma linha ("at 1.00MiB/s")
_SPEED_RE = re.compile(r"\bat\s+([\d.,]+)\s*([KMG]?i?B)/s")
_SIZE_UNITS = {"B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3,
               "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}
# quanto tempo a velocidade impressa vale antes de cair p/ a estimativa
_SPEED_TTL = 3.0

# flags do yt-dlp que exigem ffmpeg (pós-processamento: converter, embed, mux)
_FFMPEG_FLAGS = {
    "-x", "--extract-audio", "--audio-format", "--audio-quality",
    "--embed-thumbnail", "--embed-metadata", "--embed-chapters",
    "--embed-subs", "--sub-format", "--recode-video", "--remux-video",
    "--merge-output-format", "--postprocessor-args", "--exec",
    "--sponsorblock-remove", "--download-sections", "--force-keyframes-at-cuts",
}
# flags com valor no argumento seguinte (para o filtro pular o valor)
_FFMPEG_VALUE_FLAGS = {
    "--audio-format", "--audio-quality", "--sub-format", "--recode-video",
    "--remux-video", "--merge-output-format", "--postprocessor-args", "--exec",
    "--sponsorblock-remove", "--download-sections",
}
# formatos que o yt-dlp só entrega convertendo com ffmpeg
_CONVERSION_EXTS = {"mp3", "m4a", "opus", "flac", "wav", "aac", "ogg"}


def _filter_ffmpeg_args(args: list[str], ffmpeg: Optional[str]) -> list[str]:
    """Remove argumentos que dependeriam de ffmpeg quando ele não existe.

    Assim o perfil "Música" sem ffmpeg ainda baixa o melhor áudio (em
    m4a/webm nativo) em vez de abortar com "ffprobe not found". O nome do
    arquivo é ajustado para a extensão real no `_finalize`.
    """
    if ffmpeg:
        return list(args)
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in _FFMPEG_FLAGS:
            # flags com valor no próximo argumento
            skip = a in _FFMPEG_VALUE_FLAGS
            continue
        out.append(a)
    return out


def _explain_error(tail: list[str], rc: int) -> str:
    """Mensagem de erro limpa e em português a partir da saída do yt-dlp.

    Filtra os avisos (WARNING) e traduz os erros comuns do YouTube para
    algo que o usuário entende, em vez de vomitar a saída bruta.
    """
    # só as linhas de ERROR (ignora WARNING/DEBUG)
    errs = [l.strip() for l in tail
            if "ERROR" in l.upper() and "WARNING" not in l.upper()]
    if not errs:
        errs = [l.strip() for l in tail if l.strip()][-2:] or ["erro desconhecido"]
    msg = " | ".join(errs)
    low = msg.lower()
    # traduz os casos mais comuns do YouTube para algo claro
    if "unavailable" in low:
        msg += ("  → este vídeo está indisponível no YouTube "
                "(removido, privado ou bloqueado por região/idade).")
    elif "private video" in low:
        msg += "  → vídeo privado — não é possível baixar."
    elif "age" in low and ("confirm" in low or "restricted" in low):
        msg += ("  → vídeo com restrição de idade — faça login no navegador "
                "ou use --cookies.")
    elif "sign in" in low or "login" in low:
        msg += "  → o YouTube pediu login — tente com cookies do navegador."
    elif "javascript runtime" in low:
        msg += ("  → instale o Node.js (https://nodejs.org) para o yt-dlp "
                "extrair todos os formatos.")
    elif "ffmpeg" in low:
        msg += "  → instale o ffmpeg para conversão/mesclagem."
    return f"yt-dlp (código {rc}): {msg}"


def _hidden_kwargs() -> dict:
    """Evita abrir janela de console no Windows (ver `deps.hidden_kwargs`)."""
    from minidm.deps import hidden_kwargs

    return hidden_kwargs()


class YtdlpError(Exception):
    pass


# ---------------------------------------------------------------------
# Caminho in-process (APK/PyInstaller): o `yt_dlp.main` roda numa thread
# e escreve no `_LineSink`, que entrega linhas completas a uma fila —
# mesma leitura de progresso do caminho com subprocesso.
# ---------------------------------------------------------------------
_END = object()                 # sentinela: fim da execução (fila)


class _LineSink(io.TextIOBase):
    """File-like que transforma escritas em linhas para uma fila.

    Fechado pelo consumidor quando ele sai (`close()`): linhas tardias do
    yt-dlp caem fora em vez de crescer a fila sem fim.
    """

    def __init__(self):
        super().__init__()
        self.lines: queue.Queue = queue.Queue()
        self._buf = ""
        self._closed = False
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            s = str(s)
        with self._lock:
            if self._closed:
                return len(s)
            self._buf += s
            self._drain()
        return len(s)

    def _drain(self) -> None:
        # quebra em \n (newline) e também em \r (progresso redesenhado)
        parts = re.split(r"\r\n|\r|\n", self._buf)
        self._buf = parts.pop()          # resto = linha ainda incompleta
        for p in parts:
            if p.strip():                # linhas vazias não interessam
                self.lines.put(p)

    def flush(self) -> None:
        pass                             # tudo quebra no próprio write

    def isatty(self) -> bool:
        return False                     # sem console: yt-dlp usa --newline

    def fileno(self) -> int:
        raise io.UnsupportedOperation("não é um arquivo real")

    @property
    def encoding(self) -> str:
        return "utf-8"

    def close(self) -> None:
        """O consumidor desistiu: descarta escritas tardias do yt-dlp."""
        with self._lock:
            self._closed = True
            self._buf = ""


def _interrupt(thread: threading.Thread) -> None:
    """Levanta KeyboardInterrupt na thread do yt-dlp (mesmo efeito do
    Ctrl+C no terminal) — é como o próprio yt-dlp interrompe downloads.

    Sem ctypes (runtime exótico) o download segue em background até
    terminar; a interface já saiu do loop e a tarefa fica PAUSED.
    """
    if not thread.is_alive():
        return
    tid = thread.ident
    if not tid:                     # ident ainda não publicado (corrida)
        time.sleep(0.05)
        tid = thread.ident
    if not tid:
        return
    try:
        import ctypes

        ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(tid), ctypes.py_object(KeyboardInterrupt))
    except Exception:
        pass


class YtdlpDownloader:
    def __init__(self, task: Task, config: Optional[AppConfig] = None,
                 extra_args: Optional[list[str]] = None, **_kw):
        self.task = task
        self.config = config or AppConfig()
        self.extra_args = list(extra_args or [])
        self._proc: Optional[subprocess.Popen] = None
        self._worker: Optional[threading.Thread] = None  # yt-dlp in-process
        self._stop = threading.Event()
        self._received = 0
        self._total: Optional[float] = None
        self._speed = 0.0            # última velocidade medida/impressa
        self._speed_ts = 0.0         # monotonic da última medição
        self._samples: list[tuple[float, int]] = []  # (monotonic, bytes)
        self._lock = threading.Lock()
        self._ytdlp_named = False   # yt-dlp escolheu o nome (título real)
        self._drop_cookie = False   # retry sem cookie (cookie zerou formatos)
        self._rc = 0                # código de saída da última execução
        self._final_path: Optional[str] = None   # caminho real do arquivo (B)

    # ------------------------------------------------------------- interface
    def run(self) -> Task:
        t = self.task
        t.state = TaskState.DOWNLOADING
        t.error = None
        while True:
            cmd = self._build_cmd()
            tail = self._spawn(cmd)
            if self._stop.is_set():
                t.state = TaskState.PAUSED
                t.save_meta()
                return t
            rc = self._rc
            if rc != 0 and self._retry_without_cookie(tail):
                # cookie do navegador zerou a lista de formatos (YouTube
                # devolveu "só imagens") -> refaz SEM o cookie, uma vez.
                self._drop_cookie = True
                self._received = 0
                self._total = None
                with self._lock:
                    self._samples.clear()
                    self._speed = 0.0
                    self._speed_ts = 0.0
                continue
            if rc != 0:
                t.state = TaskState.ERROR
                t.error = _explain_error(tail, rc)
                t.save_meta()
                raise YtdlpError(t.error)
            self._finalize()
            return t

    def _spawn(self, cmd: list[str]) -> list[str]:
        """Roda o yt-dlp uma vez; devolve as últimas linhas (tail) de log.

        Com o módulo embutido (APK/PyInstaller) roda DENTRO do processo
        (`_spawn_in_process`); com CLI externa, subprocesso normal.
        """
        if _in_process(cmd):
            return self._spawn_in_process(cmd[3:])   # pula [exe, -m, yt_dlp]
        t = self.task
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, cwd=t.dest_dir or None,
                **_hidden_kwargs())
            assert self._proc.stdout is not None
            tail: list[str] = []
            for line in self._proc.stdout:
                if self._stop.is_set():
                    break
                # (B) caminho real do arquivo final (linha --print after_move)
                if line.startswith("MINIDM_PATH:"):
                    self._final_path = line[len("MINIDM_PATH:"):].strip()
                    continue
                self._parse_line(line)
                tail.append(line.rstrip())
                if len(tail) > 30:
                    tail.pop(0)
            self._rc = self._proc.wait()
            return tail
        except OSError as e:
            self._rc = 1
            t.state = TaskState.ERROR
            t.error = f"yt-dlp não rodou: {e}"
            t.save_meta()
            raise YtdlpError(t.error) from e

    def _spawn_in_process(self, args: list[str]) -> list[str]:
        """`yt_dlp.main(args)` numa thread, lendo o progresso linha a linha.

        Mesmo contrato do caminho com subprocesso: devolve o tail de log,
        preenche `_final_path`/`_rc` e honra `_stop` (interrompe a thread
        com KeyboardInterrupt, como o Ctrl+C no terminal). O redireciona-
        mento de stdout/stderr é POR THREAD (`_pipe`): uma extração
        `describe` da extensão roda junto sem se atropelar com o download.
        """
        sink = _LineSink()
        state = {"rc": 0}
        done = threading.Event()

        def worker() -> None:
            try:
                with _pipe(sink, sink):   # stdout E stderr p/ a mesma fila
                    import yt_dlp
                    yt_dlp.main(args)
            except SystemExit as e:       # a CLI termina com sys.exit
                code = e.code or 0
                if isinstance(code, str):
                    sink.write(f"ERROR: {code}\n")
                    code = 1
                state["rc"] = int(code) if isinstance(code, int) else 1
            except KeyboardInterrupt:     # pedido de parada via _stop
                state["rc"] = 130
            except BaseException as e:    # erro de extração/download
                sink.write(f"ERROR: {type(e).__name__}: {e}\n")
                state["rc"] = 1
            finally:
                done.set()
                sink.lines.put(_END)

        self._worker = threading.Thread(target=worker, name="minidm-ytdlp",
                                        daemon=True)
        self._worker.start()
        tail: list[str] = []
        try:
            while True:
                try:
                    line = sink.lines.get(timeout=0.2)
                except queue.Empty:
                    if done.is_set() or self._stop.is_set():
                        break
                    continue
                if line is _END:
                    break
                # (B) caminho real do arquivo final (linha --print after_move)
                if line.startswith("MINIDM_PATH:"):
                    self._final_path = line[len("MINIDM_PATH:"):].strip()
                    continue
                self._parse_line(line)
                tail.append(line.rstrip())
                if len(tail) > 30:
                    tail.pop(0)
                if self._stop.is_set():
                    break
        finally:
            sink.close()                  # linhas tardias caem fora
            if self._stop.is_set() and self._worker.is_alive():
                _interrupt(self._worker)
            if not done.is_set():
                self._worker.join(timeout=5.0)   # sai se o worker atrasar
        self._rc = state["rc"]
        return tail

    def _retry_without_cookie(self, tail: list[str]) -> bool:
        """Este erro some se rodarmos sem o cookie do navegador?"""
        if self._drop_cookie:
            return False
        has_cookie = bool(self.task.cookie or self.config.cookie)
        has_browser = bool(getattr(self.task, "cookies_from_browser", "")
                           or getattr(self.config, "cookies_from_browser", ""))
        if not (has_cookie or has_browser):
            return False
        text = "\n".join(tail).lower()
        return ("requested format is not available" in text
                or "only images are available" in text
                or "no video formats found" in text)

    def request_stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def speed_bps(self, window: float = 5.0) -> float:
        """Velocidade atual (fonte de verdade do ETA na UI).

        1) usa a velocidade que o próprio yt-dlp imprimiu ("at 1.00MiB/s")
           enquanto ela for fresca (≤ _SPEED_TTL);
        2) senão estima pela diferença de bytes recebidos dentro de `window`
           (serve p/ pausa de fragmento/mux, quando o yt-dlp não imprime).
        """
        now = time.monotonic()
        with self._lock:
            speed, ts = self._speed, self._speed_ts
            samples = list(self._samples)
        if speed > 0 and now - ts <= _SPEED_TTL:
            return speed
        if len(samples) < 2:
            return 0.0
        base = None
        for st, sv in samples:
            if now - st >= window:
                base = (st, sv)
            else:
                break
        if base is None:
            base = samples[0]
        last = samples[-1]
        dt = last[0] - base[0]
        if dt <= 0:
            return 0.0
        return max(0.0, (last[1] - base[1]) / dt)

    # ----------------------------------------------------------------- cmd
    def _build_cmd(self) -> list[str]:
        cmd = find_ytdlp()
        if not cmd:
            raise YtdlpError("yt-dlp não encontrado")
        t = self.task
        args = list(cmd)
        args += ["--newline", "--no-playlist", "--no-mtime"]
        # (B) caminho exato do arquivo final: o yt-dlp imprime a linha
        # "MINIDM_PATH:<caminho>" depois de gravar/mover — elimina a
        # adivinhação do `_finalize` (frágil com várias tarefas na pasta).
        args += ["--print", "after_move:MINIDM_PATH:%(filepath)s"]
        # ffmpeg: se existe, aponta o yt-dlp para ele (conversão/embed/mux);
        # se não, remove os argumentos que exigiriam pós-processamento —
        # o download continua no melhor formato nativo em vez de falhar.
        ff = ffmpeg_dir()
        if ff:
            args += ["--ffmpeg-location", ff]
        # runtime JS (node/deno/bun): o YouTube embaralha o URL com
        # JavaScript; sem runtime, o yt-dlp pode omitir formatos.
        js = js_runtime()
        if js:
            args += ["--js-runtimes", js]
            # solver de challenge (EJS) do YouTube: sem ele, o yt-dlp com
            # cookie vem com a página "só imagens" (storyboards) e o seletor
            # falha com "Requested format is not available". Testado: com a
            # flag -> 399+140; sem -> nenhum formato.
            from minidm.ytdlp import supports_remote_components

            if supports_remote_components():
                args += ["--remote-components", "ejs:github"]
        # (E) fragmentos concorrentes em HLS/DASH delegados (0 = não passa;
        # a engine própria já usa `concurrent_fragments` do config).
        cf = getattr(self.config, "concurrent_fragments", 0)
        if cf and cf > 0:
            args += ["--concurrent-fragments", str(cf)]
        # (F) SponsorBlock: remove trechos de patrocínio no pós-processamento.
        sb = (getattr(t, "sponsorblock", "") or
              getattr(self.config, "sponsorblock", ""))
        if sb and ff:       # requer ffmpeg p/ cortar
            args += ["--sponsorblock-remove", sb]
        # saída: se o nome ainda é o id bruto do URL (sem título real),
        # deixa o yt-dlp nomear com o título de verdade. Se o usuário/perfil
        # definiu um nome, respeita (usa o stem como prefixo).
        # Sem subprocesso não existe `cwd` — destino relativo teria que
        # virar absoluto, senão o yt-dlp gravaria no cwd do processo.
        dest = (os.path.abspath(t.dest_dir or ".")
                if _in_process(cmd) else (t.dest_dir or "."))
        stem = os.path.splitext(t.filename)[0] or "download"
        if self._generic_stem(stem):
            self._ytdlp_named = True
            args += ["-o", os.path.join(dest,
                                        "%(title)s [%(id)s].%(ext)s")]
        else:
            args += ["-o", os.path.join(dest, stem + ".%(ext)s")]
        if t.referer:
            args += ["--add-header", f"Referer:{t.referer}"]
        if t.user_agent or self.config.user_agent:
            args += ["--user-agent", t.user_agent or self.config.user_agent]
        if not self._drop_cookie and (t.cookie or self.config.cookie):
            args += ["--add-header",
                     f"Cookie:{t.cookie or self.config.cookie}"]
        else:
            cfb = (getattr(t, "cookies_from_browser", "") or
                   getattr(self.config, "cookies_from_browser", ""))
            if not self._drop_cookie and cfb:
                # (A) sessão do navegador do usuário (flag nativa do yt-dlp)
                args += ["--cookies-from-browser", cfb]
        if t.proxy or self.config.proxy:
            args += ["--proxy", t.proxy or self.config.proxy]
        # (D) trecho do vídeo: delega ao yt-dlp --download-sections
        sec = (getattr(t, "section", "") or "").strip()
        if sec:
            if not sec.startswith("*"):
                sec = "*" + sec      # "*" = cortar em qualquer keyframe
            args += ["--download-sections", sec, "--force-keyframes-at-cuts"]
        args += self.extra_args
        args += _filter_ffmpeg_args(getattr(t, "ytdlp_args", []) or [], ff)
        args.append(t.url)
        return args

    def _generic_stem(self, stem: str) -> bool:
        """O nome é genérico (id do URL, 'download', 'watch')? Aí o título
        real do yt-dlp é melhor."""
        from minidm.profiles import _id_from_url, _is_generic

        s = (stem or "").strip().lower()
        if _is_generic(s):
            return True
        vid = _id_from_url(self.task.url)
        if not vid:
            return False
        v = vid.lower()
        if s == v:
            return True
        # template "{title} [{id}]" com title == id (sem título real):
        # "9CtAJ_nw640 [9CtAJ_nw640]" -> genérico; o yt-dlp nomeia melhor.
        base = s.rsplit(" [", 1)[0].strip()
        return base == v or _is_generic(base)

    # -------------------------------------------------------------- parsing
    def _parse_line(self, line: str) -> None:
        m = _PROGRESS_RE.search(line)
        if not m:
            return
        pct = float(m.group(1))
        total = float(m.group(2)) * _SIZE_UNITS.get(m.group(3), 1)
        now = time.monotonic()
        with self._lock:
            prev = self._received
            self._total = int(total)
            self._received = int(total * pct / 100.0)
            # o yt-dlp imprime a velocidade na própria linha — é a fonte
            # mais direta p/ o ETA; guardamos com o instante (TTL curto).
            sp = _SPEED_RE.search(line)
            if sp:
                try:
                    num = float(sp.group(1).replace(",", "."))
                except ValueError:
                    num = 0.0
                if num > 0:
                    self._speed = num * _SIZE_UNITS.get(sp.group(2), 1)
                    self._speed_ts = now
            # bytes recuaram muito -> o yt-dlp trocou de etapa (baixou o
            # vídeo e começou o áudio num mux); zera a base da estimativa
            # por amostras (a velocidade impressa nesta linha continua boa).
            if self._received + 1024 < prev:
                self._samples.clear()
            self._samples.append((now, self._received))
            # guarda só os últimos 30 s (janela p/ a estimativa)
            self._samples = [(st, sv) for st, sv in self._samples
                             if now - st <= 30.0]
        t = self.task
        t.total_size = self._total
        # received/progress vêm do stream dict para casar com a UI
        t.stream = {"bytes_done": self._received,
                    "segments_total": 100,
                    "segments_done": int(pct)}

    # ------------------------------------------------------------ finalizar
    def _finalize(self) -> None:
        t = self.task
        # (B) o yt-dlp informou o caminho exato do arquivo? Usa direto —
        # elimina a adivinhação por mtime (frágil com várias tarefas na pasta).
        if self._final_path and os.path.exists(self._final_path):
            t.filename = os.path.basename(self._final_path)
            try:
                t.total_size = os.path.getsize(self._final_path)
            except OSError:
                pass
            self._hash_and_done()
            return
        # fallback (sem --print, versão antiga): acha o arquivo gravado
        base = os.path.splitext(t.filename)[0]
        found = None
        try:
            if self._ytdlp_named:
                # o yt-dlp nomeou com o título real ("%(title)s [%(id)s].%(ext)s").
                # o "mais recente" é ambíguo quando várias tarefas yt-dlp
                # dividem a mesma pasta — o id do vídeo identifica com
                # segurança o arquivo DESTA tarefa; senão, cai no mais novo.
                from minidm.profiles import _id_from_url

                vid = _id_from_url(self.task.url)
                tag = f"[{vid}]" if vid else None
                best = None
                idmatch = None
                for name in os.listdir(t.dest_dir or "."):
                    if name.endswith((".part", ".ytdl", ".meta.json")) or \
                            name in ("tasks.json", "config.json",
                                     "profiles.json", "archive.txt"):
                        continue
                    full = os.path.join(t.dest_dir or ".", name)
                    if not os.path.isfile(full):
                        continue
                    if tag and tag in name:
                        idmatch = name     # arquivo com [id] deste vídeo
                        break
                    mtime = os.path.getmtime(full)
                    if best is None or mtime > best[0]:
                        best = (mtime, name)
                found = idmatch or (best[1] if best else None)
            else:
                for name in os.listdir(t.dest_dir or "."):
                    if name.startswith(base) and not name.endswith(
                            (".part", ".ytdl", ".meta")):
                        found = name
                        break
        except OSError:
            pass
        if found:
            t.filename = found
            t.total_size = os.path.getsize(
                os.path.join(t.dest_dir or ".", found))
        self._hash_and_done()

    def _hash_and_done(self) -> None:
        """Confere sha-256 e marca a tarefa como concluída."""
        t = self.task
        path = os.path.join(t.dest_dir or ".", t.filename)
        # 0 bytes = o yt-dlp não gravou nada: erro, nunca "concluído"
        if os.path.exists(path) and os.path.getsize(path) == 0:
            t.state = TaskState.ERROR
            t.error = "download concluiu vazio — 0 bytes"
            t.save_meta()
            raise YtdlpError(t.error)
        # sha-256 do arquivo (mesmo contrato dos outros downloaders)
        try:
            from minidm.verify import sha256_file
            if os.path.exists(path):
                t.sha256 = sha256_file(path)
        except Exception:
            pass
        t.state = TaskState.DONE
        t.finished_at = time.time()
        t.stream = None
        t.save_meta()
