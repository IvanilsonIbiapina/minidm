"""Scheduler: pool global de downloads.

O modelo do v1 era "uma tarefa por vez, CLI bloca". Agora:

* `max_concurrent` workers, cada um executa **uma tarefa inteira**;
* todos os workers dividem um semáforo de `max_connections` aberturas HTTP e
  um token bucket de banda (limites **globais**, não por-tarefa);
* a fila é o próprio estado das tarefas no store (`queued` → `downloading` →
  `done`/`paused`/`error`), então vários processos/CLI podem conversar com a
  mesma fila persistente;
* `pause()` basta setar o stop cooperativo do downloader ativo — o progresso
  já gravado em `.meta.json` sobrevive.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from minidm.archive import Archive
from minidm.config import AppConfig
from minidm.downloader import DownloadError
from minidm.meta import Task, TaskState
from minidm.store import TaskStore
from minidm.stream import StreamError, make_downloader
from minidm.throttle import TokenBucket
from minidm.ytdlpdl import YtdlpError


class Scheduler:
    def __init__(self, store: TaskStore, config: AppConfig,
                 on_event: Optional[Callable[[str, Task], None]] = None):
        self.store = store
        self.config = config
        self.on_event = on_event          # callback(evento, task) p/ TUI/logs
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._active: dict[str, object] = {}
        self.sem = threading.BoundedSemaphore(max(1, config.max_connections))
        self.bucket = (TokenBucket(config.throttle_bps)
                       if config.throttle_bps > 0 else None)
        # arquivo de URLs já concluídas (como --download-archive do yt-dlp)
        self.archive = (Archive(config.data_dir)
                        if getattr(config, "download_archive", False)
                        else None)
        # recupera tarefas que ficaram "downloading" de uma execução anterior
        for t in store.all():
            if t.state == TaskState.DOWNLOADING:
                t.state = TaskState.PAUSED
        store.save()

    # ------------------------------------------------------------------- api
    def start(self) -> None:
        for i in range(max(1, self.config.max_concurrent)):
            th = threading.Thread(target=self._worker, name=f"worker-{i}",
                                  daemon=True)
            th.start()
            self._workers.append(th)

    def submit(self, task_ids: Optional[list[str]] = None) -> int:
        """Acorda os workers para as tarefas selecionadas.

        Conta tanto as que acabaram de virar `queued` quanto as que já estão
        `downloading` (worker pode ter reivindicado antes desta chamada) —
        o chamador quer saber "quantas tarefas da seleção vão rodar".
        """
        with self._cv:
            n = 0
            for t in self._select(task_ids):
                if t.state == TaskState.DOWNLOADING:
                    n += 1
                    continue
                if t.state != TaskState.QUEUED:
                    t.state = TaskState.QUEUED
                    t.error = None
                n += 1
            self.store.save()
            self._cv.notify_all()
        return n

    def pause(self, task_id: str) -> bool:
        with self._cv:
            dl = self._active.get(task_id)
            if dl:
                dl.request_stop()          # vira PAUSED no próximo checkpoint
                return True
            t = self.store.get(task_id)
            if t and t.state == TaskState.QUEUED:
                t.state = TaskState.PAUSED
                self.store.save()
                return True
        return False

    def pause_all(self) -> None:
        with self._cv:
            for dl in list(self._active.values()):
                dl.request_stop()
            for t in self.store.all():
                if t.state == TaskState.QUEUED:
                    t.state = TaskState.PAUSED
            self.store.save()

    def remove(self, task_id: str, delete_files: bool = False) -> bool:
        self.pause(task_id)
        # espera o worker soltar a tarefa (ou desiste após 10 s)
        for _ in range(100):
            with self._cv:
                if task_id not in self._active:
                    return self.store.remove(task_id, delete_files=delete_files)
            time.sleep(0.1)
        return self.store.remove(task_id, delete_files=delete_files)

    def shutdown(self, wait: bool = True) -> None:
        self._stop.set()
        with self._cv:
            for dl in self._active.values():
                dl.request_stop()
            self._cv.notify_all()
        if wait:
            for th in self._workers:
                th.join(timeout=15)

    # ------------------------------------------------------------- snapshot
    def snapshot(self) -> list[dict]:
        """Estado consolidado p/ dashboard e API."""
        # sincroniza com outros processos (CLI pode ter adicionado/removido
        # tarefas) antes de reportar — identidade preservada, barato
        self.store.refresh()
        rows = []
        for t in self.store.all():
            dl = self._active.get(t.id)
            st = t.stream if isinstance(t.stream, dict) else {}
            rows.append({
                "id": t.id,
                "url": t.url,
                "filename": t.filename,
                "kind": t.kind,
                "profile": t.profile,
                "state": t.state,
                "progress": t.progress,
                "received": t.received,
                "total": t.total_size,
                "speed": dl.speed_bps() if dl else 0.0,
                "error": t.error,
                "sha256": t.sha256,
                "checksum_ok": t.checksum_ok,
                "signature_status": t.signature_status,
                "signature_signer": t.signature_signer,
                "segments_done": sum(1 for s in t.segments if s.done),
                "segments_total": len(t.segments),
                # HLS ao vivo: total desconhecido até o ENDLIST chegar
                "live": bool(st.get("live")),
                "lost": int(st.get("lost") or 0),
            })
        return rows

    def idle(self) -> bool:
        """Nada rodando nem esperando?"""
        with self._cv:
            if self._active:
                return False
        return not any(t.state in (TaskState.QUEUED, TaskState.DOWNLOADING)
                       for t in self.store.all())

    def wait_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.idle():
            if deadline and time.monotonic() > deadline:
                return False
            time.sleep(0.2)
        return True

    # --------------------------------------------------------------- interno
    def _select(self, task_ids: Optional[list[str]]) -> list[Task]:
        if not task_ids:
            return self.store.all()
        out = []
        for i in task_ids:
            t = self.store.get(i)
            if t:
                out.append(t)
        return out

    def _claim(self) -> Optional[Task]:
        """Retira a próxima tarefa `queued` da fila (ordem de criação)."""
        with self._cv:
            for t in self.store.all():
                if t.state == TaskState.QUEUED:
                    t.state = TaskState.DOWNLOADING
                    self.store.save()
                    return t
            return None

    def _worker(self) -> None:
        while not self._stop.is_set():
            task = self._claim()
            if task is None:
                with self._cv:
                    self._cv.wait(0.5)
                continue

            # archive: pula URLs já concluídas com sucesso antes
            if self.archive and self.archive.has(task.url):
                task.state = TaskState.DONE
                task.finished_at = time.time()
                task.error = None
                self._emit("done", task)
                with self._cv:
                    self.store.save()
                    self._cv.notify_all()
                continue

            dl = make_downloader(task, config=self.config, semaphore=self.sem,
                                 bucket=self.bucket)
            with self._cv:
                self._active[task.id] = dl
            self._emit("start", task)
            try:
                dl.run()
                if self.archive:
                    self.archive.add(task.url)
                self._emit("done", task)
            except (DownloadError, StreamError, YtdlpError):
                self._emit("paused" if task.state == TaskState.PAUSED
                           else "error", task)
            finally:
                with self._cv:
                    self._active.pop(task.id, None)
                self.store.save()
                with self._cv:
                    self._cv.notify_all()

    def _emit(self, event: str, task: Task) -> None:
        if self.on_event:
            try:
                self.on_event(event, task)
            except Exception:
                pass
