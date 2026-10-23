"""Fila de downloads persistente (um JSON no diretório de trabalho da ferramenta)."""

from __future__ import annotations

import json
import os
import threading

from minidm.meta import Task

DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), ".minidm")


class TaskStore:
    def __init__(self, data_dir: str = DEFAULT_DATA_DIR):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "tasks.json")
        self._lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)
        self.tasks: dict[str, Task] = {}
        self._load()

    # ------------------------------------------------------------------ io
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return
        for item in raw.get("tasks", []):
            try:
                t = Task.from_dict(item)
            except TypeError:
                continue
            self.tasks[t.id] = t

    def save(self) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"tasks": [t.to_dict() for t in self.tasks.values()]},
                          f, indent=1)
            os.replace(tmp, self.path)

    # --------------------------------------------------------------- ops
    def add(self, task: Task) -> Task:
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
        del self.tasks[t.id]
        self.save()
        return True

    def all(self) -> list[Task]:
        return sorted(self.tasks.values(), key=lambda t: t.created_at)
