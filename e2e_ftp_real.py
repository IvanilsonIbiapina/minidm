"""E2E FTP/SFTP: o caminho REAL (scheduler -> fábrica -> engine remota).

Diferente dos testes unitários (que instanciam o downloader direto), aqui a
tarefa entra pela fila e o `Scheduler` escolhe o downloader — provando que
o roteamento `ftp://` -> `FtpDownloader` funciona de ponta a ponta, com
retry de 550 e credencial/known_hosts vindos da config.

    $env:PYTHONIOENCODING="utf-8"; python e2e_ftp_real.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "tests"))

from test_ftp_sftp import PAYLOAD, FtpServer          # noqa: E402
from sftp_stub import SftpServer                       # noqa: E402

from minidm.config import AppConfig                    # noqa: E402
from minidm.meta import Task, TaskState                # noqa: E402
from minidm.scheduler import Scheduler                 # noqa: E402
from minidm.store import TaskStore                     # noqa: E402

SHA_ESPERADO = hashlib.sha256(PAYLOAD).hexdigest()


def cfg(tmp: str, **kw) -> AppConfig:
    base = dict(data_dir=tmp, segments=4, retries=4, backoff_base=0.05,
                backoff_max=0.2, verify_signatures=False,
                remote_checksums=False, max_concurrent=2,
                max_connections=8)
    base.update(kw)
    return AppConfig(**base)


def baixar(url: str, tmp: str, rotulo: str, app_cfg: AppConfig) -> bool:
    """Submete uma tarefa ao scheduler e espera concluir."""
    store = TaskStore(app_cfg.data_dir)
    task = Task(url=url, dest_dir=tmp, filename=f"saida-{rotulo}.bin")
    store.add(task)

    sch = Scheduler(store, config=app_cfg)
    sch.start()
    try:
        sch.submit([task.id])
        if not sch.wait_idle(timeout=180):
            print(f"  {rotulo}: TIMEOUT (tarefa não terminou)")
            return False
    finally:
        sch.shutdown()

    t = store.get(task.id)
    if not t or t.state != TaskState.DONE:
        print(f"  {rotulo}: FALHOU state={getattr(t, 'state', '?')} "
              f"error={getattr(t, 'error', '?')!r}")
        return False

    with open(t.final_path, "rb") as f:
        ok = hashlib.sha256(f.read()).hexdigest() == SHA_ESPERADO
    print(f"  {rotulo}: DONE  sha256={'certo' if ok else 'ERRADO'}  "
          f"{t.total_size} bytes  {len(t.segments)} segmentos  "
          f"seg={sum(s.received for s in t.segments)} recv")
    return ok


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="minidm-e2e-ftp-")
    tudo_ok = True
    try:
        print("E2E FTP (scheduler -> FtpDownloader -> engine segmentada):")
        with FtpServer(PAYLOAD) as ftp:
            tudo_ok &= baixar(ftp.url(), tmp, "ftp", cfg(tmp))

        print("E2E FTP com retry (3x 550 injetado antes de liberar):")
        with FtpServer(PAYLOAD, fail_retr=3) as ftp:
            tudo_ok &= baixar(ftp.url(), tmp, "ftp-retry", cfg(tmp))

        print("E2E SFTP (scheduler -> SftpDownloader -> paramiko):")
        try:
            import paramiko  # noqa: F401
        except ImportError:
            print("  sftp: SKIP (paramiko ausente)")
        else:
            raiz = os.path.join(tmp, "raiz-sftp")
            os.makedirs(raiz, exist_ok=True)
            with open(os.path.join(raiz, "orig.bin"), "wb") as f:
                f.write(PAYLOAD)
            known = os.path.join(tmp, "known_hosts")
            open(known, "w").close()
            with SftpServer(raiz) as sftp:
                c = cfg(tmp, sftp_known_hosts=known,
                        sftp_accept_host_key=True)
                tudo_ok &= baixar(sftp.url("orig.bin"), tmp, "sftp", c)
            # o AutoAddPolicy deve ter gravado a chave no known_hosts
            with open(known) as f:
                gravada = "127.0.0.1" in f.read()
            print(f"  host key gravada em known_hosts: "
                  f"{'sim' if gravada else 'NÃO'}")
            tudo_ok &= gravada
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nE2E FTP/SFTP:", "APROVADO" if tudo_ok else "REPROVADO")
    return 0 if tudo_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
