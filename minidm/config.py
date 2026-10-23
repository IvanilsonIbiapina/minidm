"""Configuração do minidm: padrões + `~/.minidm/config.json` + flags da CLI."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, fields

DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), ".minidm")


@dataclass
class AppConfig:
    # concorrência
    max_concurrent: int = 3        # downloads simultâneos
    max_connections: int = 16      # segmentos/aberturas HTTP simultâneas no total
    segments: int = 0              # 0 = automático por tamanho
    # resiliência
    retries: int = 5               # tentativas por segmento
    backoff_base: float = 1.0      # segundos; dobra a cada falha
    backoff_max: float = 60.0
    # banda
    throttle_bps: int = 0          # 0 = sem limite (bytes/segundo, global)
    # rede
    user_agent: str = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "minidm/2.0")
    proxy: str = ""                # ex.: http://host:8080 ou socks5://host:1080
    cookie: str = ""               # header Cookie completo
    # integração
    api_port: int = 9742
    clipboard_interval: float = 2.0
    # verificação
    verify_signatures: bool = True     # Authenticode em .exe/.dll/.msi
    remote_checksums: bool = True      # busca <arquivo>.sha256 / .md5
    # caminhos
    data_dir: str = DEFAULT_DATA_DIR

    @property
    def config_path(self) -> str:
        return os.path.join(self.data_dir, "config.json")

    @classmethod
    def load(cls, data_dir: str = DEFAULT_DATA_DIR) -> "AppConfig":
        cfg = cls(data_dir=data_dir)
        try:
            with open(cfg.config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return cfg
        known = {f.name for f in fields(cls)}
        for k, v in raw.items():
            if k in known and k != "data_dir":
                setattr(cfg, k, v)
        return cfg

    def save(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        d = asdict(self)
        d.pop("data_dir", None)
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, self.config_path)

    def apply_flags(self, args) -> "AppConfig":
        """Sobrepõe configuração com flags explícitas da CLI (se presentes)."""
        for name in ("segments", "proxy", "cookie", "user_agent", "throttle",
                     "retries"):
            v = getattr(args, name, None)
            if v is None:
                continue
            attr = {"throttle": "throttle_bps"}.get(name, name)
            setattr(self, attr, v)
        return self
