"""Bootstrap do APK: sobe a API local do minidm (scheduler + webui).

A `MainActivity` chama `start()` numa thread Java; a função devolve a
porta da API e a Activity carrega `http://127.0.0.1:<porta>/` no WebView
(a própria webui, servida pelo `minidm/server.py`).

Notas Android:
* `watch_clipboard=False` — o clipboard do Android não passa por
  `ctypes.wintypes` (clipboard.py é Windows-only); o import é condicional
  no daemon, então fica de fora do APK;
* saída do `print` vai para o Logcat (tags `python.stdout`/`python.stderr`);
* dados em `os.environ["HOME"]` (armazenamento interno do app):
  `~/.minidm` (fila/config) e `~/Downloads/<categoria>` (arquivos).
"""

from __future__ import annotations

import threading

_state: dict = {}


def start() -> int:
    """Sobe o daemon uma única vez; devolve a porta da API (idempotente)."""
    if _state:
        return int(_state["port"])

    from minidm.config import AppConfig
    from minidm.daemon import run_daemon

    cfg = AppConfig.load()
    port = int(cfg.api_port)
    _state["port"] = port

    def _run() -> None:
        try:
            run_daemon(cfg, watch_clipboard=False, dashboard=False)
        except OSError as exc:
            # porta ocupada (outra instância): a API existente responde
            print(f"[minidm] daemon nao subiu: {exc}", flush=True)
        except Exception as exc:   # nunca derrubar o app por isso
            print(f"[minidm] daemon parou: {exc}", flush=True)

    threading.Thread(target=_run, name="minidm-daemon", daemon=True).start()
    return port
