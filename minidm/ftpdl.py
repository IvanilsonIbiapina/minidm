"""Downloads por FTP e SFTP com a engine segmentada do minidm.

Filosofia igual ao resto do projeto: o IDM baixa de FTP/SFTP também, e o
minidm faz o MESMO que faz com HTTP — planeja segmentos, pré-aloca o
`.part`, grava por offsets, faz checkpoint, retoma após queda e aplica
throttle/retry — trocando só a camada de transporte:

    HTTP usa `Range: bytes=a-b`;  FTP usa `REST` + `RETR`;  SFTP usa
    `open` + `seek`.  O resto (segmentos, semáforo, token bucket,
    checkpoint, verify) é reaproveitado do `Downloader`.

Cada protocolo vira uma classe com dois métodos:

    _stat(url)                       -> tamanho/mtime (probe p/ retomada)
    _fetch_range(url, start, end, w) -> lê [start..end] e chama w(bytes)

Conexões são por THREAD (`threading.local`): `ftplib` e o canal SFTP não
suportam requisições concorrentes na mesma conexão, e os workers rodam em
threads — o cache por thread dá reuso entre segmentos da mesma thread.

Credenciais, na ordem de precedência: `user:pass@` no URL > config
(`ftp_user`…, `sftp_user`…) > anônimo (FTP) / agent/chaves locais (SFTP).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, NamedTuple, Optional
from urllib.parse import unquote, urlsplit

from minidm.config import AppConfig
from minidm.downloader import (CHUNK, DownloadError, Downloader,
                               IncompleteSegment, StopRequested,
                               auto_segment_count, plan_segments)
from minidm.meta import Task
from minidm.remote import TIMEOUT, filename_from

try:
    import ftplib
except ImportError:                     # pragma: no cover — stdlib sempre lá
    ftplib = None                       # type: ignore[assignment]

try:
    import paramiko
except ImportError:
    paramiko = None                     # SFTP vira erro claro, não crash


class RemoteError(OSError):
    """Falha de protocolo FTP/SFTP.

    Herda de `OSError` de propósito: o retry por segmento do `_worker` do
    `Downloader` já captura `OSError`, então uma queda de conexão remota é
    retentada com backoff exatamente como uma queda HTTP.
    """


class RemoteStat(NamedTuple):
    """Resultado do probe remoto (o análogo do `Probe` do HTTP)."""

    size: Optional[int]
    mtime: Optional[str] = None
    accept_ranges: bool = True


def _split(url: str):
    """(host, port, caminho, user, senha) de um URL ftp:// ou sftp://."""
    p = urlsplit(url)
    if p.scheme.lower() not in ("ftp", "sftp"):
        raise RemoteError(f"protocolo não suportado: {p.scheme or '(vazio)'}")
    if not p.hostname:
        raise RemoteError(f"url sem host: {url}")
    default = 21 if p.scheme.lower() == "ftp" else 22
    return (p.hostname, p.port or default, unquote(p.path or "/"),
            unquote(p.username or ""), unquote(p.password or ""))


def make_remote_downloader(task: Task, config: Optional[AppConfig] = None,
                           **kw):
    """Fábrica para URLs `ftp://` e `sftp://` (chamada pela fábrica geral)."""
    scheme = urlsplit(task.url).scheme.lower()
    cls = SftpDownloader if scheme == "sftp" else FtpDownloader
    return cls(task, config=config, **kw)


def remote_stat(url: str, config: Optional[AppConfig] = None) -> RemoteStat:
    """Sonda tamanho/mtime de um URL remoto, sem baixar (análise/GUI)."""
    scheme = urlsplit(url).scheme.lower()
    cls = {"ftp": FtpDownloader, "sftp": SftpDownloader}.get(scheme)
    if cls is None:
        raise RemoteError(f"protocolo não suportado: {scheme or '(vazio)'}")
    # só para instanciar: _stat não toca em dest_dir/nome de arquivo
    probe_task = Task(url=url, dest_dir=".", filename="probe.bin")
    return cls(probe_task, config=config or AppConfig())._stat(url)


# --------------------------------------------------------------- base remota
class RemoteRangeDownloader(Downloader):
    """Motor do `Downloader` com a camada de transporte remota.

    Sobrescreve só o que depende do protocolo:

    * `_prepare`   — probe por SIZE/stat em vez de HEAD; retomada validada
                     por tamanho + mtime (o análogo remoto de ETag);
    * `_stream_segment` — entrega os bytes ao `_fetch_range` e mantém todo
                     o resto (semáforo, bucket, lock, progresso, pausa);
    * `_finalize`  — pula checksums publicados ao lado (o `<url>.sha256`
                     é convenção HTTP; ftp:// não fala com `requests`).
    """

    _supports_remote_checksums = False   # convenção HTTP-only

    def __init__(self, task: Task, config: Optional[AppConfig] = None, **kw):
        super().__init__(task, config=config, **kw)
        # conexões por THREAD: ftplib e canal SFTP não aceitam requisições
        # concorrentes, e os workers rodam em threads — o cache por thread
        # dá reuso entre segmentos sem travar nada.
        self._local = threading.local()

    def _check_deps(self) -> None:
        """Erros claros em pt-BR antes de qualquer conexão."""

    # ------------------------------------------------- contrato das subclasses
    def _stat(self, url: str) -> RemoteStat:           # noqa: D102
        raise NotImplementedError

    def _fetch_range(self, url: str, start: int, end: int,
                     write: Callable[[bytes], None]) -> None:   # noqa: D102
        """Lê [start..end] (inclusivo) de `url` e entrega a `write`."""
        raise NotImplementedError

    def _open_conn(self, url: str):                    # noqa: D102
        raise NotImplementedError

    def _close_conn(self, conn) -> None:               # noqa: D102
        raise NotImplementedError

    # ------------------------------------------------------- conexão por thread
    def _conn(self, url: str):
        """Conexão viva para `url` nesta thread (cacheada; cria se preciso)."""
        cache = getattr(self._local, "conns", None)
        if cache is None:
            cache = self._local.conns = {}
        conn = cache.get(url)
        if conn is None:
            conn = cache[url] = self._open_conn(url)
        return conn

    def _drop(self, url: str) -> None:
        """Descarta a conexão cacheada de `url` (fica em estado incerto)."""
        cache = getattr(self._local, "conns", None) or {}
        conn = cache.pop(url, None)
        if conn is not None:
            try:
                self._close_conn(conn)
            except Exception:
                pass

    # ----------------------------------------------------------- preparação
    def _prepare(self) -> None:
        self._check_deps()
        t = self.task
        os.makedirs(t.dest_dir, exist_ok=True)

        # sonda: principal + mirrors, na ordem (igual ao HTTP)
        last_err = None
        st = None
        for url in self._urls:
            try:
                st = self._stat(url)
                break
            except RemoteError as e:
                last_err = f"{url}: {e}"
        if st is None:
            raise DownloadError(f"nenhuma URL remota respondeu ({last_err})")
        if st.size is None:
            # `0` é um arquivo VAZIO válido; só `None` é "servidor não sabe"
            raise DownloadError(
                "servidor não informou o tamanho do arquivo (SIZE/stat) — "
                "sem o tamanho não há como segmentar nem retomar")

        resumed = False
        old = Task.load_meta(t.meta_path)
        if old and os.path.exists(t.part_path):
            # retomada validada por URL + tamanho + mtime (remoto do ETag)
            same_remote = (
                old.url in self._urls and old.segments
                and old.total_size == st.size
                and (not st.mtime or not old.last_modified
                     or old.last_modified == st.mtime))
            if same_remote:
                t.total_size = old.total_size
                t.accept_ranges = True
                t.last_modified = old.last_modified or st.mtime
                t.segments = old.segments
                resumed = True
            else:
                # arquivo remoto mudou: descarta o parcial
                old.drop_meta()

        if not resumed:
            t.total_size = st.size
            t.accept_ranges = st.accept_ranges
            t.last_modified = st.mtime
            n = self.config.segments or auto_segment_count(st.size)
            if not st.accept_ranges:
                n = 1    # sem REST só dá pra fazer sequencial
            t.segments = plan_segments(st.size, n)
            with open(t.part_path, "wb") as f:
                f.truncate(st.size)

        t.save_meta()

    # ------------------------------------------------------------- transporte
    def _stream_segment(self, index: int, url: str) -> None:
        """Mesmo contrato do pai: grava o segmento `index` via `_fetch_range`."""
        t = self.task
        seg = t.segments[index]
        acquired = self.sem.acquire(timeout=60) if self.sem else True
        if not acquired:
            raise RemoteError("sem conexões disponíveis")
        try:
            start = seg.start + seg.received
            if start > seg.end:
                return                       # já completo (retomada)
            with open(t.part_path, "r+b", buffering=0) as f:
                f.seek(start)

                def write(chunk: bytes) -> None:
                    if self.stop.is_set():
                        raise StopRequested()
                    if self.bucket:
                        self.bucket.acquire(len(chunk), stop=self.stop)
                    f.write(chunk)
                    with self._segments_lock:
                        seg.received += len(chunk)
                        self._speed_samples.append(
                            (time.monotonic(), seg.received))

                self._fetch_range(url, start, seg.end, write)
        finally:
            if self.sem and acquired:
                self.sem.release()

        if not seg.done:
            raise IncompleteSegment(
                f"faltam {seg.total - seg.received} de {seg.total} bytes")


# ------------------------------------------------------------------- FTP
class FtpDownloader(RemoteRangeDownloader):
    """FTP clássico (RFC 959) via `ftplib` — sem dependência externa.

    Cada thread usa a sua conexão de controle; `PASV` (padrão do ftplib)
    atravessa NAT/firewall; `TYPE I` garante binário sem CRLF.
    """

    def __init__(self, task: Task, config: Optional[AppConfig] = None, **kw):
        if ftplib is None:                 # pragma: no cover
            raise DownloadError("ftplib indisponível nesta instalação do "
                                "Python (venv incompleta?)")
        super().__init__(task, config=config, **kw)

    # ------------------------------------------------------------ credenciais
    def _creds(self, url: str):
        host, port, path, user, pw = _split(url)
        return (host, port, path,
                user or self.config.ftp_user or "anonymous",
                pw or self.config.ftp_password or "minidm@")

    # -------------------------------------------------------------- conexão
    def _open_conn(self, url: str):
        host, port, _path, user, pw = self._creds(url)
        ftp = ftplib.FTP()
        try:
            ftp.connect(host, port, timeout=TIMEOUT)
            ftp.login(user, pw)
            ftp.voidcmd("TYPE I")           # binário: sem conversão CRLF
            ftp.set_pasv(True)              # PASV (atravessa NAT)
        except ftplib.all_errors as e:
            try:
                ftp.close()
            except OSError:
                pass
            raise RemoteError(f"ftp: {e}") from e
        return ftp

    def _close_conn(self, conn) -> None:
        try:
            conn.quit()                     # "QUIT" educado
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    # ---------------------------------------------------------------- probe
    def _stat(self, url: str) -> RemoteStat:
        ftp = self._conn(url)
        path = self._creds(url)[2]
        try:
            try:
                size = ftp.size(path)
            except ftplib.all_errors:
                size = None                 # servidor sem SIZE
            mtime = None
            try:
                resp = ftp.sendcmd("MDTM " + path)   # 213 YYYYMMDDHHMMSS
                if resp[:3] == "213":
                    # o código vem antes do timestamp: "213 20250101120000"
                    stamp = resp.split(None, 1)
                    if len(stamp) == 2 and len(stamp[1]) >= 14 \
                            and stamp[1][:14].isdigit():
                        mtime = stamp[1][:14]
            except ftplib.all_errors:
                pass                        # MDTM é opcional
            ranges = True
            try:
                ftp.sendcmd("REST 0")       # 350 = aceita offset
            except ftplib.all_errors:
                ranges = False
            return RemoteStat(size=size, mtime=mtime, accept_ranges=ranges)
        except ftplib.all_errors as e:
            self._drop(url)
            raise RemoteError(f"ftp: {e}") from e

    # ------------------------------------------------------------- download
    def _fetch_range(self, url: str, start: int, end: int,
                     write: Callable[[bytes], None]) -> None:
        ftp = self._conn(url)
        path = self._creds(url)[2]
        conn = None
        try:
            try:
                conn = ftp.transfercmd("RETR " + path, rest=start or None)
                need = end - start + 1
                while need > 0:
                    chunk = conn.recv(min(CHUNK, need))
                    if not chunk:
                        raise RemoteError(
                            "ftp: conexão de dados fechou antes do fim do "
                            f"segmento (faltam {need} bytes)")
                    write(chunk)
                    need -= len(chunk)
                conn.close()
                conn = None
                ftp.voidresp()              # consome o "226 Transfer complete"
            except ftplib.Error as e:
                # 426 "Transfer aborted" é o fim NORMAL de um RETR parcial:
                # pedimos só um trecho (REST) e o servidor ainda mandaria o
                # resto do arquivo — os bytes certos já chegaram.
                if not str(e).startswith("426"):
                    raise
        except StopRequested:
            self._drop(url)                 # conexão em estado indefinido
            raise
        except RemoteError:
            self._drop(url)
            raise
        except (ftplib.Error, OSError, EOFError) as e:
            self._drop(url)
            raise RemoteError(f"ftp: {e}") from e
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass


# ------------------------------------------------------------------ SFTP
class SftpDownloader(RemoteRangeDownloader):
    """SFTP sobre SSH (paramiko) — seek por segmento, retomada por offset.

    Host key: a primeira conexão aceita e SALVA a chave em
    `~/.ssh/known_hosts` (como `ssh` com "yes"); ligações seguintes
    conferem a chave. Use `sftp_accept_host_key = False` para política
    estrita (chave desconhecida = erro), e `sftp_known_hosts` para apontar
    para outro arquivo.
    """

    def _check_deps(self) -> None:
        if paramiko is None:
            raise DownloadError(
                "SFTP requer o pacote paramiko — instale com: "
                "python -m pip install paramiko")

    def _creds(self, url: str):
        host, port, path, user, pw = _split(url)
        key = os.path.expanduser(self.config.sftp_key) if self.config.sftp_key else ""
        return (host, port, path,
                user or self.config.sftp_user or "",
                pw or self.config.sftp_password or "", key)

    # -------------------------------------------------------------- conexão
    def _open_conn(self, url: str):
        self._check_deps()      # remote_stat chama _stat sem passar pelo
        host, port, _path, user, pw, key = self._creds(url)  # _prepare
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        known = os.path.expanduser(
            self.config.sftp_known_hosts or "~/.ssh/known_hosts")
        if os.path.exists(known):
            client.load_host_keys(known)
        if self.config.sftp_accept_host_key:
            # aceita chave nova e (quando o known_hosts existe) a salva
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                host, port=port,
                username=user or None,
                password=pw or None,
                key_filename=key or None,
                timeout=TIMEOUT,
                allow_agent=True,          # ssh-agent e chaves locais
                look_for_keys=not (pw or key),
            )
        except Exception as e:              # SSHException, socket, auth…
            try:
                client.close()
            except Exception:
                pass
            raise RemoteError(f"sftp: {e}") from e
        return client

    def _close_conn(self, conn) -> None:
        try:
            conn.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- probe
    def _stat(self, url: str) -> RemoteStat:
        client = self._conn(url)
        path = self._creds(url)[2]
        sftp = client.open_sftp()
        try:
            st = sftp.stat(path)
        except (OSError, paramiko.SSHException) as e:
            self._drop(url)
            raise RemoteError(f"sftp: {e}") from e
        finally:
            try:
                sftp.close()
            except Exception:
                pass
        mtime = str(int(st.st_mtime)) if st.st_mtime else None
        return RemoteStat(size=st.st_size, mtime=mtime, accept_ranges=True)

    # ------------------------------------------------------------- download
    def _fetch_range(self, url: str, start: int, end: int,
                     write: Callable[[bytes], None]) -> None:
        client = self._conn(url)
        path = self._creds(url)[2]
        sftp = client.open_sftp()
        try:
            f = sftp.open(path, "rb")
            try:
                f.seek(start)
                need = end - start + 1
                while need > 0:
                    chunk = f.read(min(CHUNK, need))
                    if not chunk:
                        raise RemoteError(
                            "sftp: o servidor fechou antes do fim do "
                            f"segmento (faltam {need} bytes)")
                    write(chunk)
                    need -= len(chunk)
            finally:
                try:
                    f.close()
                except Exception:
                    pass
        except StopRequested:
            self._drop(url)
            raise
        except RemoteError:
            self._drop(url)
            raise
        except (OSError, EOFError, paramiko.SSHException) as e:
            self._drop(url)
            raise RemoteError(f"sftp: {e}") from e
        finally:
            try:
                sftp.close()
            except Exception:
                pass
