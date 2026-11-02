"""Servidor SFTP mínimo em processo, para testes — só o que o minidm usa.

Uma thread aceita conexões TCP, cada conexão vira um `paramiko.Transport`
de servidor com uma chave RSA gerada na hora. A interface SFTP serve
arquivos de um diretório temporário (`stat`, `open` com `read`, `list`,
`canonicalize`). Usuário/senha são configuráveis; qualquer outro valor
recusa a autenticação.

Uso típico nos testes:

    with SftpServer(root) as srv:
        url = srv.url("arquivo.bin")
        ...  # minidm baixando de url
"""
from __future__ import annotations

import os
import socket
import threading

import paramiko


# --------------------------------------------------------------- interface
class _RootSI(paramiko.SFTPServerInterface):
    """Serve `root` como se fosse a raiz do sistema."""

    def __init__(self, server, root, username=""):
        self.root = os.path.abspath(root)
        self.username = username

    # caminho lógico -> caminho real em disco (fora de `root` = negado)
    def _real(self, path):
        norm = os.path.normpath(path.replace("\\", "/"))
        real = os.path.abspath(os.path.join(self.root, norm.lstrip("/\\"))
                               if norm not in (".", "") else self.root)
        if not (real == self.root or real.startswith(self.root + os.sep)):
            raise IOError("fora do diretório raiz")
        return real

    def list_folder(self, path):
        real = self._real(path)
        if not os.path.isdir(real):
            return paramiko.SFTP_FAILURE
        out = []
        for name in sorted(os.listdir(real)):
            full = os.path.join(real, name)
            st = os.stat(full)
            attr = paramiko.SFTPAttributes.from_stat(st)
            attr.filename = name
            out.append(attr)
        return out

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError:
            return paramiko.SFTP_NO_SUCH_FILE

    lstat = stat

    def open(self, path, flags, attr):
        real = self._real(path)
        # o cliente só lê (acesso remoto); qualquer flag de escrita = negado
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT):
            return paramiko.SFTP_PERMISSION_DENIED
        if not os.path.isfile(real):
            return paramiko.SFTP_NO_SUCH_FILE
        handle = paramiko.SFTPHandle(flags)
        handle.readfile = open(real, "rb")
        return handle

    def remove(self, path):
        return paramiko.SFTP_PERMISSION_DENIED

    rename = posix_rename = remove
    mkdir = rmdir = remove
    chattr = remove

    def readlink(self, path):
        return paramiko.SFTP_FAILURE

    def symlink(self, link_path, target_path):
        return paramiko.SFTP_PERMISSION_DENIED

    def canonicalize(self, path):
        try:
            return self._real(path).replace(os.sep, "/")
        except IOError:
            return "/"


# ------------------------------------------------------------------ servidor
class _Server(threading.Thread):
    """Aceita conexões e sobe um Transport de servidor em cada uma."""

    def __init__(self, root, username, password, key, sock):
        super().__init__(daemon=True)
        self.root, self.username, self.password = root, username, password
        self.key, self.sock = key, sock
        self.transports = []
        self.channels = []

    def run(self):
        while True:
            try:
                client, _addr = self.sock.accept()
            except OSError:
                return                      # socket fechado = shutdown
            # uma thread por conexão: o downloader abre UMA conexão SSH por
            # thread de worker, então várias chegam ao mesmo tempo.
            threading.Thread(target=self._serve, args=(client,),
                             daemon=True).start()

    def _serve(self, client):
        t = paramiko.Transport(client)
        self.transports.append(t)
        t.add_server_key(self.key)
        # o handler do subsistema sftp: é registrado ANTES do cliente
        # pedir o canal, como faz um sshd de verdade.
        t.set_subsystem_handler("sftp", paramiko.SFTPServer, _RootSI,
                                self.root, self.username)
        t.start_server(server=_Handler(self))
        # o canal precisa de referência viva: sem ela, o GC fecha a
        # conexão e o cliente vê "Channel closed" no meio do handshake.
        self.channels.append(t.accept())

    def shutdown(self):
        try:
            self.sock.close()
        except OSError:
            pass
        for t in self.transports:
            try:
                t.close()
            except Exception:
                pass


class _Handler(paramiko.ServerInterface):
    def __init__(self, server):
        self.srv = server

    def check_auth_password(self, username, password):
        if username == self.srv.username and password == self.srv.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        return False                       # só SFTP, sem exec

    # check_channel_subsystem_request padrão: procura o handler no
    # subsystem_table do Transport (registrado em _Server.run).


class SftpServer:
    """Context manager: sobe o servidor em `root` e expõe `url(nome)`."""

    def __init__(self, root, username="user", password="senha"):
        self.root = os.path.abspath(root)
        self.username, self.password = username, password

    def start(self) -> "SftpServer":
        """Sobe o servidor (também usado pelo context manager)."""
        self.key = paramiko.RSAKey.generate(2048)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._srv = _Server(self.root, self.username, self.password,
                            self.key, self.sock)
        self._srv.start()
        return self

    def stop(self) -> None:
        """Derruba o servidor (thread + transports)."""
        if getattr(self, "_srv", None) is not None:
            self._srv.shutdown()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

    def url(self, name: str, username: str | None = None,
            password: str | None = None) -> str:
        u = self.username if username is None else username
        p = self.password if password is None else password
        creds = f"{u}:{p}@" if u else ""
        return f"sftp://{creds}127.0.0.1:{self.port}/{name}"
