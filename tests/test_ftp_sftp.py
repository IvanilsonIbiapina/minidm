"""Testes de FTP e SFTP: engine remota do `minidm` (ftpdl.py).

FTP roda com um servidor mínimo escrito em Python puro (socket + thread),
que registra os offsets de `REST` pedidos — é isso que prova que o
download segmentado de verdade está pedindo faixas diferentes do servidor.

SFTP usa o servidor em processo de `tests/sftp_stub.py` (paramiko).

Rodar só estes testes:

    python -m unittest discover -s tests -p "test_ftp_sftp.py" -v
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import tempfile
import threading
import unittest
from unittest import mock

from minidm.config import AppConfig
from minidm.downloader import DownloadError
from minidm.ftpdl import (FtpDownloader, RemoteError, SftpDownloader,
                          make_remote_downloader, remote_stat)
from minidm.media import analyze
from minidm.meta import Task, TaskState
from minidm.stream import make_downloader

from sftp_stub import SftpServer          # noqa: E402  (tests/ no path)

PAYLOAD = os.urandom(1 * 1024 * 1024 + 77)   # ~1 MB, tamanho "embaraçoso"


def test_config(**kw) -> AppConfig:
    """Config de teste: retries/baratos e sem checagens externas."""
    base = dict(segments=4, retries=2, backoff_base=0.05, backoff_max=0.1,
                verify_signatures=False, remote_checksums=False)
    base.update(kw)
    return AppConfig(**base)


# ============================================================== servidor FTP
class FtpServer:
    """Servidor FTP mínimo (RFC 959) para testes.

    Comandos atendidos: USER/PASS/TYPE/PASV/SIZE/MDTM/REST/RETR/QUIT.

    Flags de comportamento (mudam o cenário do teste):

    * `require_auth`   — recusa login que não bate com user/pass;
    * `support_size`   — False faz o SIZE responder 550 (servidor "burro");
    * `support_rest`   — False faz o REST responder 550 (sem ranges);
    * `fail_retr`      — quantos RETRs seguidos recusar com 550 (retry).
    """

    def __init__(self, payload: bytes, *, require_auth=None,
                 support_size=True, support_rest=True, fail_retr=0):
        self.payload = payload
        self.require_auth = require_auth
        self.support_size = support_size
        self.support_rest = support_rest
        self.fail_retr = fail_retr
        self.rest_offsets = []        # offsets de REST pedidos (p/ asserção)
        self.logins = []              # (user, pass) de cada login
        self.retr_count = 0
        self._stop = threading.Event()

    # ------------------------------------------------------------ ciclo de vida
    def start(self) -> "FtpServer":
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

    def url(self, name: str = "arquivo.bin", user: str = "", pw: str = "") -> str:
        creds = f"{user}:{pw}@" if user else ""
        return f"ftp://{creds}127.0.0.1:{self.port}/{name}"

    # ---------------------------------------------------------------- loop
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return                      # socket fechado = shutdown
            threading.Thread(target=self._serve, args=(conn,),
                             daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        rest = 0
        # estado POR CONEXÃO: os workers abrem várias conexões ao mesmo
        # tempo, então PASV/RETR/USER não podem se atropelar.
        pending = None
        user = ""
        try:
            conn.sendall(b"220 minidm FTP pronto\r\n")
            f = conn.makefile("rb")
            while True:
                raw = f.readline()
                if not raw:
                    return
                line = raw.decode("latin-1").rstrip("\r\n")
                if not line:
                    continue
                cmd, _, arg = line.partition(" ")
                cmd = cmd.upper()

                if cmd == "USER":
                    user = arg
                    conn.sendall(b"331 senha required\r\n")
                elif cmd == "PASS":
                    ok = (self.require_auth is None or
                          (user, arg) == self.require_auth)
                    self.logins.append((user, arg))
                    if ok:
                        conn.sendall(b"230 login ok\r\n")
                    else:
                        conn.sendall(b"530 login incorreto\r\n")
                        return
                elif cmd == "TYPE":
                    conn.sendall(b"200 binary\r\n")
                elif cmd == "PASV":
                    data = self._open_passive()
                    h1, h2, h3, h4 = "127", "0", "0", "1"
                    p1, p2 = divmod(data.getsockname()[1], 256)
                    msg = f"227 Entering Passive Mode ({h1},{h2},{h3},{h4},{p1},{p2})\r\n"
                    conn.sendall(msg.encode("ascii"))
                    pending = data
                elif cmd == "SIZE":
                    if not self.support_size:
                        conn.sendall(b"550 SIZE nao suportado\r\n")
                    else:
                        conn.sendall(f"213 {len(self.payload)}\r\n".encode())
                elif cmd == "MDTM":
                    conn.sendall(b"213 20250101120000\r\n")
                elif cmd == "REST":
                    if not self.support_rest:
                        conn.sendall(b"550 REST nao suportado\r\n")
                    else:
                        rest = int(arg or 0)
                        self.rest_offsets.append(rest)
                        conn.sendall(b"350 offset aceito\r\n")
                elif cmd == "RETR":
                    self.retr_count += 1
                    if self.fail_retr > 0:
                        self.fail_retr -= 1
                        if pending is not None:
                            try:
                                pending.close()   # dados nunca serão usados
                            except OSError:
                                pass
                            pending = None
                        conn.sendall(b"550 falha injetada\r\n")
                        continue
                    if pending is None:
                        conn.sendall(b"425 Use PASV first\r\n")
                        continue
                    try:
                        data, _ = pending.accept()      # cliente conectou
                    except OSError:
                        conn.sendall(b"425 sem conexao de dados\r\n")
                        continue
                    finally:
                        try:
                            pending.close()
                        except OSError:
                            pass
                        pending = None
                    conn.sendall(b"150 abrindo dados\r\n")
                    try:
                        data.sendall(self.payload[rest:])
                        data.close()
                        conn.sendall(b"226 transfer complete\r\n")
                    except OSError:
                        # cliente fechou cedo (leu só a faixa pedida) —
                        # normal num RETR parcial com REST.
                        try:
                            data.close()
                        except OSError:
                            pass
                        conn.sendall(b"426 transfer abortado\r\n")
                    rest = 0
                elif cmd == "QUIT":
                    conn.sendall(b"221 tchau\r\n")
                    return
                else:
                    conn.sendall(b"500 comando desconhecido\r\n")
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _open_passive(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return s


# ================================================================= utilitários
def _task(url: str, tmp: str, name: str = "b.bin", **kw) -> Task:
    return Task(url=url, dest_dir=tmp, filename=name,
                state=TaskState.QUEUED, **kw)


class FtpBase(unittest.TestCase):
    """Sobe/derruba um servidor FTP por teste."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-ftp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.srv = FtpServer(PAYLOAD)
        self.addCleanup(self.srv.stop)
        self.srv.start()

    def _url(self, **kw) -> str:
        return self.srv.url(**kw)


# ============================================================== testes FTP
class TestFtpDownload(FtpBase):
    def test_download_multi_segmento_bate_com_o_payload(self):
        task = _task(self._url(), self.tmp)
        FtpDownloader(task, test_config(segments=4)).run()

        self.assertEqual(task.state, TaskState.DONE)
        with open(task.final_path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(),
                             hashlib.sha256(PAYLOAD).hexdigest())

    def test_segmentacao_pede_offsets_diferentes_ao_servidor(self):
        """Prova que é download segmentado: REST 0 e REST != 0 foram pedidos."""
        task = _task(self._url(), self.tmp)
        FtpDownloader(task, test_config(segments=4)).run()

        self.assertEqual(task.state, TaskState.DONE)
        # o probe (REST 0) + um RETR por segmento (o último pode começar
        # depois de offset > 0). Basta existir offset não-zero.
        self.assertIn(0, self.srv.rest_offsets)
        self.assertTrue(any(off > 0 for off in self.srv.rest_offsets),
                        f"offsets pedidos: {self.srv.rest_offsets}")

    def test_tamanho_confere_com_estimado(self):
        task = _task(self._url(), self.tmp)
        FtpDownloader(task, test_config()).run()
        self.assertEqual(task.total_size, len(PAYLOAD))

    def test_retomada_continua_do_offset_recebido(self):
        from minidm.downloader import plan_segments

        task = _task(self._url(), self.tmp, "r.bin",
                     total_size=len(PAYLOAD), accept_ranges=True)
        task.segments = plan_segments(len(PAYLOAD), 4)
        task.segments[0].received = 200_000
        task.segments[1].received = task.segments[1].total
        with open(task.part_path, "wb") as f:
            f.truncate(len(PAYLOAD))
            s0 = task.segments[0]
            f.seek(s0.start)
            f.write(PAYLOAD[s0.start:s0.start + 200_000])
            s1 = task.segments[1]
            f.seek(s1.start)
            f.write(PAYLOAD[s1.start:s1.end + 1])
        task.save_meta()

        antes = sum(s.received for s in task.segments)
        FtpDownloader(task, test_config(segments=4)).run()

        self.assertEqual(task.state, TaskState.DONE)
        with open(task.final_path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(),
                             hashlib.sha256(PAYLOAD).hexdigest())
        # o offset do segmento parcial deve ter sido retomado (não 0)
        self.assertTrue(any(off > 0 for off in self.srv.rest_offsets),
                        f"offsets: {self.srv.rest_offsets}")
        self.assertGreater(sum(s.received for s in task.segments), antes)

    def test_retry_recupera_de_550_transitorio(self):
        self.srv.fail_retr = 3             # 3 RETRs recusados, depois volta
        task = _task(self._url(), self.tmp)
        FtpDownloader(task, test_config(segments=2, retries=6)).run()

        self.assertEqual(task.state, TaskState.DONE)
        self.assertEqual(task.sha256, hashlib.sha256(PAYLOAD).hexdigest())

    def test_retry_esgota_vira_erro_legivel(self):
        self.srv.fail_retr = 10_000
        task = _task(self._url(), self.tmp)
        with self.assertRaises(Exception):
            FtpDownloader(task, test_config(segments=1, retries=1)).run()

        self.assertEqual(task.state, TaskState.ERROR)
        self.assertIn("550", task.error or "")

    def test_sem_size_erro_claro_sobre_tamanho(self):
        self.srv.support_size = False
        task = _task(self._url(), self.tmp)
        with self.assertRaises(DownloadError) as ctx:
            FtpDownloader(task, test_config()).run()

        self.assertIn("tamanho", str(ctx.exception))
        self.assertEqual(task.state, TaskState.ERROR)

    def test_sem_rest_usa_um_unico_segmento(self):
        self.srv.support_rest = False
        task = _task(self._url(), self.tmp)
        FtpDownloader(task, test_config(segments=8)).run()

        self.assertEqual(len(task.segments), 1)
        self.assertEqual(task.sha256, hashlib.sha256(PAYLOAD).hexdigest())

    def test_credenciais_do_url_tem_precedencia_sobre_config(self):
        # servidor exige user/senha; o URL traz as certas e a config erradas
        self.srv.require_auth = ("urluser", "urlsenha")
        url = self._url(user="urluser", pw="urlsenha")
        task = _task(url, self.tmp)
        cfg = test_config(ftp_user="errado", ftp_password="errado")
        FtpDownloader(task, cfg).run()

        self.assertEqual(task.state, TaskState.DONE)
        self.assertIn(("urluser", "urlsenha"), self.srv.logins)

    def test_config_de_credenciais_quando_url_nao_tem(self):
        self.srv.require_auth = ("cfguser", "cfgsenha")
        task = _task(self._url(), self.tmp)
        cfg = test_config(ftp_user="cfguser", ftp_password="cfgsenha")
        FtpDownloader(task, cfg).run()

        self.assertEqual(task.state, TaskState.DONE)
        self.assertIn(("cfguser", "cfgsenha"), self.srv.logins)

    def test_anonimo_quando_nao_há_credenciais(self):
        task = _task(self._url(), self.tmp)
        FtpDownloader(task, test_config()).run()

        self.assertEqual(task.state, TaskState.DONE)
        self.assertIn(("anonymous", "minidm@"), self.srv.logins)

    def test_mirror_morto_nao_impede_download(self):
        morta = "ftp://127.0.0.1:1/arquivo.bin"
        task = _task(morta, self.tmp, mirrors=[self._url()])
        FtpDownloader(task, test_config(segments=3)).run()

        self.assertEqual(task.state, TaskState.DONE)
        self.assertEqual(task.sha256, hashlib.sha256(PAYLOAD).hexdigest())

    def test_url_morta_sem_mirror_vira_erro(self):
        morta = "ftp://127.0.0.1:1/arquivo.bin"
        task = _task(morta, self.tmp)
        with self.assertRaises(DownloadError):
            FtpDownloader(task, test_config()).run()
        self.assertEqual(task.state, TaskState.ERROR)


class TestFtpApi(FtpBase):
    def test_fabrica_devolve_ftp_downloader(self):
        task = _task(self._url(), self.tmp)
        dl = make_downloader(task, test_config())
        self.assertIsInstance(dl, FtpDownloader)

    def test_fabrica_remota_devolve_ftp_downloader(self):
        task = _task(self._url(), self.tmp)
        self.assertIsInstance(make_remote_downloader(task, test_config()),
                              FtpDownloader)

    def test_remote_stat_devolve_tamanho_e_mtime(self):
        st = remote_stat(self._url(), test_config())
        self.assertEqual(st.size, len(PAYLOAD))
        self.assertEqual(st.mtime, "20250101120000")
        self.assertTrue(st.accept_ranges)

    def test_remote_stat_protocolo_incompativel(self):
        with self.assertRaises(RemoteError):
            remote_stat("http://exemplo.com/a.bin")

    def test_analyze_ftp_devolve_arquivo_com_tamanho(self):
        res = analyze(self._url())
        self.assertTrue(res["ok"])
        self.assertEqual(res["kind"], "file")
        self.assertEqual(res["qualities"][0]["estimated_size"], len(PAYLOAD))
        self.assertEqual(res["filename"], "arquivo.bin")

    def test_analyze_ftp_sem_size_vira_erro(self):
        self.srv.support_size = False
        res = analyze(self._url())
        self.assertFalse(res["ok"])
        self.assertIn("tamanho", res["error"])

    def test_analyze_ftp_inacessivel_vira_erro(self):
        res = analyze("ftp://127.0.0.1:1/arquivo.bin")
        self.assertFalse(res["ok"])


# ============================================================== servidor SFTP
class SftpBase(unittest.TestCase):
    """Sobe o servidor SFTP em processo por teste (paramiko)."""

    def setUp(self):
        try:
            import paramiko  # noqa: F401
        except ImportError:
            self.skipTest("paramiko não instalado")
        self.tmp = tempfile.mkdtemp(prefix="minidm-sftp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        with open(os.path.join(self.tmp, "arquivo.bin"), "wb") as f:
            f.write(PAYLOAD)
        # known_hosts em arquivo temporário: NUNCA tocar no ~/.ssh real
        self.known_hosts = os.path.join(self.tmp, "known_hosts")
        open(self.known_hosts, "w").close()      # AutoAddPolicy exige existir
        self.srv = SftpServer(self.tmp).start()
        self.addCleanup(self.srv.stop)

    def _cfg(self, **kw) -> AppConfig:
        kw.setdefault("sftp_known_hosts", self.known_hosts)
        return test_config(**kw)

    def _url(self, user: str = "user", pw: str = "senha") -> str:
        return self.srv.url("arquivo.bin", username=user, password=pw)


class TestSftpDownload(SftpBase):
    def test_stat_confere_tamanho(self):
        st = remote_stat(self._url(), self._cfg())
        self.assertEqual(st.size, len(PAYLOAD))
        self.assertTrue(st.accept_ranges)

    def test_download_multi_segmento_bate_com_o_payload(self):
        task = _task(self._url(), self.tmp)
        SftpDownloader(task, self._cfg(segments=4)).run()

        self.assertEqual(task.state, TaskState.DONE)
        with open(task.final_path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(),
                             hashlib.sha256(PAYLOAD).hexdigest())

    def test_seek_por_segmento_bate_com_faixas(self):
        task = _task(self._url(), self.tmp)
        SftpDownloader(task, self._cfg(segments=4)).run()
        # 4 segmentos => pelo menos 2 conexões diferentes de faixa
        self.assertEqual(len(task.segments), 4)
        self.assertEqual(task.sha256, hashlib.sha256(PAYLOAD).hexdigest())

    def test_retomada_continua_do_offset_recebido(self):
        from minidm.downloader import plan_segments

        task = _task(self._url(), self.tmp, "r.bin",
                     total_size=len(PAYLOAD), accept_ranges=True)
        task.segments = plan_segments(len(PAYLOAD), 4)
        task.segments[0].received = 150_000
        with open(task.part_path, "wb") as f:
            f.truncate(len(PAYLOAD))
            s0 = task.segments[0]
            f.seek(s0.start)
            f.write(PAYLOAD[s0.start:s0.start + 150_000])
        task.save_meta()

        antes = task.segments[0].received
        SftpDownloader(task, self._cfg(segments=4)).run()

        self.assertEqual(task.state, TaskState.DONE)
        with open(task.final_path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(),
                             hashlib.sha256(PAYLOAD).hexdigest())
        self.assertGreater(task.segments[0].received, antes)

    def test_fabrica_devolve_sftp_downloader(self):
        task = _task(self._url(), self.tmp)
        dl = make_downloader(task, self._cfg())
        self.assertIsInstance(dl, SftpDownloader)

    def test_credenciais_do_url_tem_precedencia_sobre_config(self):
        # o servidor só aceita user/senha; a config traz erradas
        task = _task(self._url(user="user", pw="senha"), self.tmp)
        cfg = self._cfg(sftp_user="errado", sftp_password="errado")
        SftpDownloader(task, cfg).run()
        self.assertEqual(task.state, TaskState.DONE)

    def test_credenciais_erradas_viram_remote_error(self):
        task = _task(self._url(user="user", pw="errada"), self.tmp)
        with self.assertRaises(DownloadError):
            SftpDownloader(task, self._cfg()).run()
        self.assertEqual(task.state, TaskState.ERROR)

    def test_arquivo_inexistente_vira_erro(self):
        url = self.srv.url("nao-existe.bin")
        task = _task(url, self.tmp)
        with self.assertRaises(DownloadError):
            SftpDownloader(task, self._cfg()).run()
        self.assertEqual(task.state, TaskState.ERROR)

    def test_known_hosts_estrito_rejeita_chave_desconhecida(self):
        """`sftp_accept_host_key=False` => chave nova = erro (não auto-add)."""
        vazio = os.path.join(self.tmp, "vazio.txt")
        open(vazio, "w").close()
        cfg = self._cfg(sftp_known_hosts=vazio, sftp_accept_host_key=False)
        task = _task(self._url(), self.tmp)
        with self.assertRaises(DownloadError):
            SftpDownloader(task, cfg).run()

    def test_auto_add_grava_chave_no_known_hosts(self):
        """Com política auto-add, a chave nova entra no arquivo known_hosts."""
        cfg = self._cfg(sftp_known_hosts=self.known_hosts,
                        sftp_accept_host_key=True)
        task = _task(self._url(), self.tmp)
        SftpDownloader(task, cfg).run()

        self.assertEqual(task.state, TaskState.DONE)
        with open(self.known_hosts) as f:
            conteudo = f.read()
        self.assertIn("127.0.0.1", conteudo)

    def test_analyze_sftp_devolve_arquivo_com_tamanho(self):
        res = analyze(self._url())
        self.assertTrue(res["ok"])
        self.assertEqual(res["kind"], "file")
        self.assertEqual(res["qualities"][0]["estimated_size"], len(PAYLOAD))


# ==================================================== dependências ausentes
class TestDependenciasAusentes(unittest.TestCase):
    def test_sem_paramiko_erro_com_instrucao(self):
        """paramiko ausente => DownloadError com o comando de instalação."""
        from minidm import ftpdl

        task = Task(url="sftp://127.0.0.1:1/a.bin", dest_dir=".",
                    filename="a.bin")
        with mock.patch.object(ftpdl, "paramiko", None):
            with self.assertRaises(DownloadError) as ctx:
                SftpDownloader(task, test_config())._check_deps()

        self.assertIn("pip install paramiko", str(ctx.exception))

    def test_remote_stat_sftp_sem_paramiko_erro_claro(self):
        from minidm import ftpdl

        with mock.patch.object(ftpdl, "paramiko", None):
            with self.assertRaises(DownloadError) as ctx:
                remote_stat("sftp://127.0.0.1:1/a.bin", test_config())

        self.assertIn("paramiko", str(ctx.exception))


# ============================================================== casos de borda
class TestCasosDeBorda(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-ftp-borda-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_split_url_com_porta_e_senha_percent_encoded(self):
        from minidm.ftpdl import _split

        host, port, path, user, pw = _split(
            "ftp://meu%40user:sen%20ha@host:2121/pasta%20x/arq.bin")
        self.assertEqual((host, port, path, user, pw),
                         ("host", 2121, "/pasta x/arq.bin",
                          "meu@user", "sen ha"))

    def test_split_url_sem_host_erro(self):
        from minidm.ftpdl import _split

        with self.assertRaises(RemoteError):
            _split("ftp:///sem-host")

    def test_split_protocolo_errado_erro(self):
        from minidm.ftpdl import _split

        with self.assertRaises(RemoteError):
            _split("http://host/a.bin")

    def test_ftp_url_com_caminho_com_espaco(self):
        """URL com %20 no caminho é decodificado antes de mandar ao servidor."""
        with FtpServer(PAYLOAD) as srv:
            task = _task(srv.url("meu%20arquivo.bin"), self.tmp, "espaco.bin")
            FtpDownloader(task, test_config()).run()
            self.assertEqual(task.state, TaskState.DONE)
            self.assertEqual(task.sha256, hashlib.sha256(PAYLOAD).hexdigest())

    def test_arquivo_vazio_ftp(self):
        # contrato novo (igual às engines HTTP/yt-dlp): download de 0
        # bytes NUNCA conclui como sucesso — era o bug do arquivo vazio
        # que o MediaStore publicava como "concluído" no celular
        with FtpServer(b"") as srv:
            task = _task(srv.url(), self.tmp)
            with self.assertRaises(DownloadError):
                FtpDownloader(task, test_config()).run()
            self.assertEqual(task.state, TaskState.ERROR)
            self.assertIn("0 bytes", task.error)


if __name__ == "__main__":
    unittest.main()
