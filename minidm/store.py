"""Fila de downloads persistente (um JSON no diretório de trabalho da ferramenta).

Vários processos do minidm podem abrir a mesma fila ao mesmo tempo (GUI +
CLI, dois `minidm start`…). Três proteções, todas sob uma **trava exclusiva
por processo** (`tasks.json.lock` — msvcrt no Windows, fcntl no POSIX):

1. **escrita atômica** com `.tmp` único por processo — o JSON nunca sai
   truncado ou com writes intercalados;
2. **merge antes de gravar** — tarefas que OUTRO processo criou entram na
   memória em vez de serem apagadas ("lost update");
3. **tombstones** (`"removed": {id: timestamp}`) — uma remoção feita por
   outro processo não é ressuscitada pelo save seguinte.

O merge preserva a *identidade* dos objetos: o scheduler/downloader
seguram a instância viva de cada tarefa, então campos são copiados para o
objeto existente — nada é substituído. Quem tem a versão mais nova de cada
tarefa é decidido por `Task.updated_at`, que só é marcado quando a
estrutura muda (estado/erro); progresso não marca, então o dono do
download nunca perde avanço por causa de um save alheio.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from typing import Iterator

from minidm.meta import Task, TaskState

DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), ".minidm")

# remoções ficam registradas 30 dias (depois o tombstone some e a antiga
# id poderia, em tese, ser "recriada" — uuid torna isso improvável)
TOMBSTONE_TTL = 30 * 86400

# chaves de progresso dentro de `task.stream` (o resto é plano/URLs =
# estrutura)
_STREAM_PROGRESS_KEYS = ("bytes_done", "segments_done", "lost")


def _structural(t: Task) -> str:
    """A ESTRUTURA da tarefa, sem campos de progresso.

    Duas tasks com a mesma estrutura mas progressos diferentes são a
    mesma "versão" p/ fins de corrida entre processos — progresso é
    monotônico e fica no dono do download (e na sidecar `.meta.json`).
    """
    d = t.to_dict()
    d.pop("updated_at", None)
    for seg in d.get("segments") or []:
        if isinstance(seg, dict):
            seg.pop("received", None)
    st = d.get("stream")
    if isinstance(st, dict):
        for k in _STREAM_PROGRESS_KEYS:
            st.pop(k, None)
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


def _copy_fields(dst: Task, src: Task) -> None:
    """Copia os campos de `src` para `dst` mantendo a IDENTIDADE de `dst`.

    O downloader pode estar segurando `dst` agora; trocar o objeto no
    dict soltaria a referência viva dele.
    """
    for name in Task.__dataclass_fields__:
        if name != "id":
            setattr(dst, name, getattr(src, name))


class TaskStore:
    def __init__(self, data_dir: str = DEFAULT_DATA_DIR,
                 lock_timeout: float = 15.0):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "tasks.json")
        self._lock = threading.Lock()          # entre threads deste processo
        self._lock_timeout = lock_timeout      # entre processos
        os.makedirs(data_dir, exist_ok=True)
        self.tasks: dict[str, Task] = {}
        self._removed: dict[str, float] = {}   # tombstones: id -> instante
        self._baseline: dict[str, str] = {}    # última estrutura gravada
        self._load()

    # ------------------------------------------------ trava entre processos
    @contextlib.contextmanager
    def _file_lock(self) -> Iterator[None]:
        """Trava exclusiva entre processos sobre a fila.

        NÃO aninhar: cada chamada abre um novo descritor e a trava do
        Windows/POSIX não é reentrante no mesmo arquivo.
        """
        fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT, 0o666)
        try:
            if os.name == "nt":
                import msvcrt

                def try_lock() -> None:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

                def unlock() -> None:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                def try_lock() -> None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

                def unlock() -> None:
                    fcntl.flock(fd, fcntl.LOCK_UN)

            deadline = time.monotonic() + self._lock_timeout
            while True:
                try:
                    try_lock()
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(
                            "outra instância do minidm segura a fila "
                            f"({os.path.basename(self.path)}.lock) há "
                            f"{self._lock_timeout:.0f}s — feche a outra "
                            "GUI/CLI e tente de novo") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                try:
                    unlock()
                except OSError:
                    pass
        finally:
            os.close(fd)

    # ------------------------------------------------------------------ io
    def _read_disk(self) -> tuple[list[dict], dict]:
        """(tarefas, tombstones) gravados AGORA por qualquer processo."""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return [], {}
        if not isinstance(raw, dict):        # formato legado: lista crua
            return (raw if isinstance(raw, list) else []), {}
        tasks = raw.get("tasks")
        removed = raw.get("removed")
        return (tasks if isinstance(tasks, list) else [],
                removed if isinstance(removed, dict) else {})

    def _adopt_removed(self, removed: dict) -> None:
        """Funde tombstones vindos do disco e esquece os expirados."""
        now = time.time()
        for tid, ts in removed.items():
            if isinstance(ts, (int, float)) and now - float(ts) <= TOMBSTONE_TTL:
                self._removed.setdefault(tid, float(ts))
        for tid in [k for k, ts in self._removed.items()
                    if now - ts > TOMBSTONE_TTL]:
            del self._removed[tid]

    def _load(self) -> None:
        disk_tasks, removed = self._read_disk()
        self._adopt_removed(removed)
        for item in disk_tasks:
            try:
                t = Task.from_dict(item)
            except TypeError:
                continue
            if t.id in self._removed and t.state != TaskState.DOWNLOADING:
                continue
            self.tasks[t.id] = t
            self._baseline[t.id] = _structural(t)

    def _merge_from_disk(self) -> None:
        """Traz para a memória o que outros processos criaram/removeram.

        Chamar com `self._lock` E a trava de arquivo em vigor.
        """
        disk_tasks, removed = self._read_disk()
        self._adopt_removed(removed)
        for item in disk_tasks:
            try:
                t = Task.from_dict(item)
            except TypeError:
                continue
            if t.id in self._removed and t.state != TaskState.DOWNLOADING:
                continue
            mem = self.tasks.get(t.id)
            if mem is None:
                self.tasks[t.id] = t                      # criada por lá
                self._baseline[t.id] = _structural(t)
            elif t.updated_at > mem.updated_at:
                _copy_fields(mem, t)                      # versão mais nova
                self._baseline[t.id] = _structural(t)
        for tid in list(self._removed):
            cur = self.tasks.get(tid)
            # uma tarefa em download não some no meio (o dono continua);
            # ela é esquecida quando terminar (state != downloading)
            if cur is not None and cur.state != TaskState.DOWNLOADING:
                del self.tasks[tid]
                self._baseline.pop(tid, None)

    def _write_locked(self) -> None:
        """Grava atômico (tmp único por processo + rename)."""
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"tasks": [t.to_dict() for t in self.tasks.values()],
                       "removed": self._removed}, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def save(self) -> None:
        with self._lock:
            with self._file_lock():
                self._merge_from_disk()
                now = time.time()
                for tid, t in list(self.tasks.items()):
                    # mudança ESTRUTURAL (estado/erro/nome…) -> marca
                    # updated_at e vence quem tinha uma versão antiga.
                    # Progresso não muda a estrutura: quem baixa mantém
                    # o avanço mesmo com saves concorrentes de outros.
                    if _structural(t) != self._baseline.get(tid):
                        t.updated_at = now
                self._write_locked()
                for tid, t in list(self.tasks.items()):
                    self._baseline[tid] = _structural(t)

    def refresh(self) -> None:
        """Sincroniza a memória com o disco, sem gravar.

        Vê o que outros processos criaram/removeu (e atualiza campos de
        tarefas que mudaram lá). Identidade preservada — seguro no loop
        da GUI/API.
        """
        with self._lock:
            with self._file_lock():
                self._merge_from_disk()

    # --------------------------------------------------------------- ops
    def add(self, task: Task) -> Task:
        with self._lock:
            self.tasks[task.id] = task
        self.save()
        return task

    def get(self, task_id: str) -> Task | None:
        if task_id in self.tasks:
            return self.tasks[task_id]
        # também aceita prefixo do id
        matches = [t for t in self.tasks.values() if t.id.startswith(task_id)]
        return matches[0] if len(matches) == 1 else None

    def remove(self, task_id: str, delete_files: bool = False) -> bool:
        with self._lock:
            t = self.get(task_id)
            if not t:
                return False
            if delete_files:
                for p in (t.part_path, t.meta_path):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                # segmentos avulsos de streams (HLS/DASH)
                segs = os.path.join(t.dest_dir, f".{t.filename}.segs")
                if os.path.isdir(segs):
                    import shutil
                    shutil.rmtree(segs, ignore_errors=True)
            with self._file_lock():
                # tombstone ANTES de apagar: o disco ainda tem a tarefa e
                # o merge do próprio save a reincidente — a marca de
                # remoção é que impede a ressurreição (em qualquer
                # processo, inclusive os que já estão com ela em memória)
                self._removed[t.id] = time.time()
                self._merge_from_disk()
                self.tasks.pop(t.id, None)
                self._baseline.pop(t.id, None)
                self._write_locked()
            return True

    def all(self) -> list[Task]:
        # snapshot: a GUI adiciona de outra thread enquanto iteramos
        return sorted(list(self.tasks.values()), key=lambda t: t.created_at)
