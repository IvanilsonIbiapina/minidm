# -*- coding: utf-8 -*-
"""Modo portable — item 3 do ROADMAP ("versões portable").

O onedir já é quase portátil (extraia e rode), mas a fila e a
configuração moram em ``~/.minidm`` — num pendrive isso deixa o rastro
na máquina. Um arquivo ``portable.flag`` **ao lado do bundle** muda
tudo: a pasta de dados vira ``<onde está o flag>/data`` e o minidm
inteiro (GUI, CLI, daemon) carrega e guarda a fila ali.

Detecção: sobe a árvore a partir do diretório do executável (o exe do
bundle fica em ``<raiz>/minidm-gui/`` ou ``<raiz>/minidm/``, e o flag
vive na **raiz**, junto do ``LEIA-ME.txt`` — os DOIS exes encontram o
mesmo flag e dividem a mesma fila). Máximo de 3 níveis (dir do exe +
2). Em desenvolvimento (``python.exe`` de instalação) quase nunca há
flag — o comportamento padrão ``~/.minidm`` continua.

Sem flag: ``data_dir()`` devolve ``None`` e todo mundo cai no padrão.
Apagar o arquivo volta ao padrão — é um switch, não uma instalação.

Uso:  coloque/renomeie um arquivo vazio para `portable.flag` na raiz
      do bundle (o zip `minidm-portable-win64.zip` já vem com ele).
"""

from __future__ import annotations

import os
import sys
from typing import Optional

FLAG = "portable.flag"
NIVEIS = 3              # dir do exe + 2 acima (raiz do zip é o pai)
SUBDIR = "data"         # pasta de dados AO LADO do flag


def root(start: Optional[str] = None) -> Optional[str]:
    """Diretório que contém o ``portable.flag``, ou None (não portable).

    ``start`` é o ponto de partida (padrão: dir do executável) — nos
    testes aponta para uma árvore temporária.
    """
    base = start or os.path.dirname(os.path.abspath(sys.executable))
    for _ in range(NIVEIS):
        if os.path.isfile(os.path.join(base, FLAG)):
            return base
        pai = os.path.dirname(base)
        if pai == base:                 # chegou na raiz do SO
            break
        base = pai
    return None


def data_dir() -> Optional[str]:
    """``<flag>/data`` quando portable; None = usar o padrão (~/.minidm)."""
    r = root()
    return os.path.join(r, SUBDIR) if r else None


def enabled() -> bool:
    """Esta execução está em modo portable?"""
    return root() is not None
