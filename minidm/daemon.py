"""Modo daemon: scheduler + observador de clipboard + API numa só imagem.

É o equivalente ao "IDM rodando em background": a fila drena sozinha, links
copiados entram na fila e o navegador pode enfileirar pela API local.
"""

from __future__ import annotations

import threading

from minidm.config import AppConfig
from minidm.meta import Task, TaskState
from minidm.remote import filename_from
from minidm.scheduler import Scheduler
from minidm.server import App, serve
from minidm.store import TaskStore


def run_daemon(config: AppConfig, watch_clipboard: bool = True,
               dashboard: bool = False, dest_dir: str | None = None) -> int:
    store = TaskStore(config.data_dir)
    scheduler = Scheduler(store, config)

    def on_event(event, task):
        if event == "done":
            msg = f"[ok] {task.filename} sha256={task.sha256}"
            if task.signature_status:
                msg += f" assinatura={task.signature_status}"
            print(msg, flush=True)
        elif event == "error":
            print(f"[erro] {task.filename}: {task.error}", flush=True)

    scheduler.on_event = on_event
    scheduler.start()
    app = App(store, scheduler, config)

    # API local em thread própria
    api_thread = threading.Thread(target=serve, args=(app,), daemon=True)
    api_thread.start()

    # observador da área de transferência
    if watch_clipboard:
        from minidm import clipboard

        def on_url(url: str):
            task = Task(url=url, dest_dir=dest_dir or store.data_dir,
                        filename=filename_from(url), state=TaskState.QUEUED)
            store.add(task)
            scheduler.submit([task.id])
            print(f"[{task.id}] capturado da área de transferência: {url}",
                  flush=True)

        threading.Thread(target=clipboard.watch,
                         args=(on_url,),
                         kwargs={"interval": config.clipboard_interval},
                         daemon=True).start()
        print(f"clipboard: observando a cada {config.clipboard_interval:.0f}s",
              flush=True)

    print(f"fila: {config.max_concurrent} download(s) simultâneo(s), "
          f"{config.max_connections} conexões"
          + (f", limite {config.throttle_bps // 1024} KB/s"
             if config.throttle_bps else ""),
          flush=True)

    if dashboard:
        from minidm.dashboard import live_dashboard

        live_dashboard(scheduler, throttle_bps=config.throttle_bps)
        scheduler.shutdown()
    else:
        try:
            while True:
                import time
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nencerrando — tarefas ativas pausam com progresso salvo")
            scheduler.shutdown(wait=True)
    return 0
