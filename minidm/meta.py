"""Modelos de dados do minidm e persistência em JSON."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional


class TaskState:
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    DONE = "done"
    ERROR = "error"


@dataclass
class Segment:
    """Fatia do arquivo: [start, end] inclusivo, já recebido `received` bytes."""

    start: int
    end: int
    received: int = 0

    @property
    def total(self) -> int:
        return self.end - self.start + 1

    @property
    def done(self) -> bool:
        return self.received >= self.total


@dataclass
class Task:
    url: str
    dest_dir: str
    filename: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    state: str = TaskState.QUEUED
    total_size: Optional[int] = None
    accept_ranges: bool = False
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    segments: list[Segment] = field(default_factory=list)
    sha256: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    # ---- v2
    mirrors: list[str] = field(default_factory=list)
    proxy: str = ""
    user_agent: str = ""
    cookie: str = ""
    expected_sha256: Optional[str] = None
    remote_sha256: Optional[str] = None
    remote_md5: Optional[str] = None
    checksum_ok: Optional[bool] = None
    signature_status: Optional[str] = None
    signature_signer: Optional[str] = None
    attempts: int = 0
    # ---- v3 (captura de vídeo estilo IDM)
    kind: str = "file"               # "file" | "hls" | "dash"
    referer: str = ""                # página de origem (muitos CDNs exigem)
    stream: Optional[dict] = None    # plano de segmentos do stream (resume)
    # ---- v4 (nome/origem como o IDM)
    page_title: str = ""             # título da página (melhora o nome)
    quality_label: str = ""          # "1080p", "áudio — pt-BR"…
    origin_url: str = ""             # URL da página (quando difere da mídia)
    # ---- v5 (perfis estilo YT-DLnis)
    profile: str = ""                # nome do perfil usado ("Música"…)
    use_ytdlp_downloader: bool = False   # delegar download ao yt-dlp
    ytdlp_args: list[str] = field(default_factory=list)  # args extras yt-dlp

    # ---------------------------------------------------------------- paths
    @property
    def part_path(self) -> str:
        return os.path.join(self.dest_dir, self.filename + ".part")

    @property
    def final_path(self) -> str:
        return os.path.join(self.dest_dir, self.filename)

    @property
    def meta_path(self) -> str:
        return os.path.join(self.dest_dir, self.filename + ".meta.json")

    @property
    def received(self) -> int:
        if self.stream and isinstance(self.stream, dict):
            # stream: bytes contados no plano de segmentos
            return int(self.stream.get("bytes_done", 0))
        return sum(s.received for s in self.segments)

    @property
    def progress(self) -> float:
        if self.stream and isinstance(self.stream, dict):
            done = self.stream.get("segments_done") or 0
            total = self.stream.get("segments_total") or 0
            if total:
                return min(1.0, done / total)
        if not self.total_size:
            return 0.0
        return min(1.0, self.received / self.total_size)

    # -------------------------------------------------------- serialization
    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        segs = [Segment(**s) for s in d.get("segments", [])]
        known = {f for f in cls.__dataclass_fields__}  # tolera campos extras
        base = {k: v for k, v in d.items() if k in known and k != "segments"}
        t = cls(**base)
        t.segments = segs
        return t

    def save_meta(self) -> None:
        """Grava o estado de progresso ao lado do `.part` (atômico)."""
        os.makedirs(self.dest_dir, exist_ok=True)
        tmp = self.meta_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=1)
        os.replace(tmp, self.meta_path)

    @classmethod
    def load_meta(cls, meta_path: str) -> Optional["Task"]:
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return cls.from_dict(json.load(f))
        except (OSError, ValueError):
            return None

    def drop_meta(self) -> None:
        try:
            os.remove(self.meta_path)
        except OSError:
            pass
