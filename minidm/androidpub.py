# -*- coding: utf-8 -*-
"""Publica arquivos concluídos na pasta pública Download/ (Android).

No desktop os downloads já caem em `~/Downloads`, visíveis no file
manager. No Android o HOME é o armazenamento **privado** do app
(`/data/data/br.com.minidm.app/files`), então `~/Downloads` fica
invisível para o app "Downloads" do sistema.

Depois que a engine conclui uma tarefa, o `Scheduler` chama
`publish_if_needed`, que:

* roda **só no APK** (a classe Java ``br.com.minidm.app.MainActivity``
  existe apenas no Android — fora daí o import falha e é no-op);
* move o arquivo para ``Download/<categoria>/`` via
  ``MainActivity.publish`` (MediaStore em API 29+, escrita direta +
  WRITE_EXTERNAL_STORAGE em API 24–28);
* registra o destino em ``task.published_path`` — o ``dest_dir``/``filename``
  originais **não mudam**, porque as engines reescrevem nele (mux, resume,
  re-execução) e escrita direta na pasta pública é bloqueada pelo scoped
  storage;
* qualquer falha (sem permissão, MediaStore indisponível, erro Java)
  deixa tudo como está: o download continua válido, só não fica visível.

Só a base **padrão** (`~/Downloads`) é publicada — um `download_base`
explícito é escolha do usuário e fica onde está.

Desligar: ``minidm config set android_public_downloads false``.
"""

from __future__ import annotations

import os

from minidm.categories import DEFAULT_BASE
from minidm.meta import Task, TaskState

# cache do resolve da classe Java: _UNRESOLVED = ainda não tentado;
# None = não é Android (import falhou); caso contrário, a classe.
_UNRESOLVED = object()
_JAVA = _UNRESOLVED


def _main_activity():
    """Classe Java do APK, ou None fora do Android (nunca levanta erro)."""
    global _JAVA
    if _JAVA is _UNRESOLVED:
        try:
            import java                    # noqa: F401
            # bridge + import hook do Chaquopy (fica no-op sem Android)
        except BaseException:
            pass
        try:
            from br.com.minidm.app import MainActivity   # noqa: F401
            _JAVA = MainActivity
        except BaseException:
            # Desktop/CI: ModuleNotFoundError. No APK uma eventual
            # NoClassDefFoundError também vira None (sem publicação).
            _JAVA = None
    return _JAVA


def publish_if_needed(task: Task, config) -> str | None:
    """Move o arquivo de `task` para Download/ do sistema (só Android).

    Devolve o caminho público do arquivo ou None (fora do Android, base
    customizada, config desligada, arquivo ausente, sem permissão ou
    falha). Seguro para o worker do scheduler: **nunca** levanta exceção.
    """
    try:
        if not getattr(config, "android_public_downloads", True):
            return None
        if task.state != TaskState.DONE:
            return None
        src = os.path.abspath(task.final_path)
        if not os.path.isfile(src):
            return None                  # já publicado (ou removido)
        # só a base PADRÃO (privada no Android); download_base explícito
        # é escolha do usuário e fica onde está
        base = os.path.abspath(DEFAULT_BASE)
        dest = os.path.abspath(task.dest_dir)
        if dest != base and not dest.startswith(base + os.sep):
            return None
        # subpasta = nome da categoria ("Videos"…); tarefa direto na
        # base vai para a raiz de Download/
        subdir = os.path.basename(dest.rstrip(os.sep))
        if subdir == os.path.basename(base):
            subdir = ""

        MainActivity = _main_activity()
        if MainActivity is None:
            return None                  # não é Android
        pub = MainActivity.publish(src, subdir, task.filename)
        pub = str(pub) if pub else ""
        if not pub or os.path.abspath(pub) == src:
            return None                  # falha ou arquivo já era público
        task.published_path = pub
        # o sidecar .meta.json interno ficou órfão (resume não existe p/ uma
        # tarefa concluída; tasks.json guarda o registro de verdade)
        try:
            os.remove(task.meta_path)
        except OSError:
            pass
        return pub
    except BaseException:
        # publicação é melhor-esforço: nunca derrubar o worker do scheduler
        return None
