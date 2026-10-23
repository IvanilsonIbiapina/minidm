"""Controle de banda: token bucket compartilhado por todos os workers.

Todos os segmentos (de todas as tarefas) chamam `bucket.acquire(n)` antes de
escrever n bytes; quando não há créditos, esperam. Assim o limite é global,
não por-download.
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, rate_bps: int, burst_seconds: float = 1.0):
        self.rate = float(rate_bps)
        self.capacity = max(1.0, self.rate * burst_seconds)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, amount: int, stop: threading.Event | None = None) -> None:
        if self.rate <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity,
                                   self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= amount or self._tokens >= self.capacity:
                    self._tokens -= amount
                    return
                need = (amount - self._tokens) / self.rate
            # espera fora do lock; aborta se pedirem para parar
            if stop is not None and stop.wait(need):
                return
            time.sleep(need)
