"""Archive de downloads já realizados (como o `--download-archive` do yt-dlp).

Guarda um hash curto de cada URL concluída com sucesso em
`<data_dir>/archive.txt` (uma linha por item: `minidm <hash16>`). Antes de
baixar, o scheduler pergunta `has(url)`; se já existe, pula (o yt-dlp faz
o mesmo).

O hash é sha256 do URL normalizado, 16 hex — só para identificar, não é
segredo nem integridade de conteúdo.
"""

from __future__ import annotations

import hashlib
import os
import threading


def url_id(url: str) -> str:
    """Identificador curto e estável de um URL."""
    norm = (url or "").strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


class Archive:
    """Arquivo de URLs já baixadas (thread-safe, append-only)."""

    def __init__(self, data_dir: str, filename: str = "archive.txt"):
        self.path = os.path.join(data_dir, filename)
        self._lock = threading.Lock()
        self._ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        self._ids.add(parts[1])
                    elif len(parts) == 1:
                        self._ids.add(parts[0])
        except OSError:
            pass

    def has(self, url: str) -> bool:
        return url_id(url) in self._ids

    def add(self, url: str) -> bool:
        """Registra o URL; devolve True se era novo."""
        i = url_id(url)
        with self._lock:
            if i in self._ids:
                return False
            self._ids.add(i)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"minidm {i}\n")
            return True

    def __len__(self) -> int:
        return len(self._ids)
