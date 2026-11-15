# -*- coding: utf-8 -*-
"""Modo portable (`portable.flag` → fila/config em `<flag>/data`).

Item 3 do ROADMAP: o zip `minidm-portable-win64.zip` traz um
`portable.flag` na raiz e o minidm inteiro passa a morar em `./data`
ao lado dele (em vez de `~/.minidm`). Aqui provamos a detecção (sobe a
árvore do exe, os dois exes acham o mesmo flag, limite de níveis),
a precedência (`--data-dir` explícito vence) e a coerência CLI/GUI/
daemon (config, `config show` e TaskStore no mesmo ponto).

Sem flag o padrão `~/.minidm` continua — e o "sem flag" é
determinístico aqui: o `os.path.isfile` é blindado para nunca achar um
`portable.flag` deixado por acaso nos ancestrais do diretório temp.

Rodar:  python -m unittest discover -s tests -p "test_portable.py" -v
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from minidm import portable
from minidm.config import AppConfig, DEFAULT_DATA_DIR, default_data_dir


def _sem_flag_ancestral():
    """Nenhum `portable.flag` nos ancestrais do temp (determinístico).

    `root()` sobe até 3 níveis a partir do exe; num SO de teste os
    avós do diretório temp são desconhecidos (C:\\Users\\… ou /tmp) —
    este blindagem faz o isfile devolver False só para FLAG, delegando
    o resto ao original.
    """
    real = os.path.isfile

    def fake(p):
        if os.path.basename(p) == portable.FLAG:
            return False
        return real(p)

    return mock.patch.object(portable.os.path, "isfile", side_effect=fake)


class _Arvore(unittest.TestCase):
    """Árvore de bundle em temp: `<raiz>/portable.flag` + exes em subpastas."""

    def setUp(self):
        self.raiz = tempfile.mkdtemp(prefix="minidm-portable-")
        self.addCleanup(shutil.rmtree, self.raiz, True)
        # layout real do zip onedir: raiz (flag + LEIA-ME) / minidm-gui/ / minidm/
        self.gui_dir = os.path.join(self.raiz, "minidm-gui")
        self.cli_dir = os.path.join(self.raiz, "minidm")
        os.makedirs(self.gui_dir)
        os.makedirs(self.cli_dir)
        self.gui_exe = os.path.join(self.gui_dir, "minidm-gui.exe")
        self.cli_exe = os.path.join(self.cli_dir, "minidm.exe")
        for e in (self.gui_exe, self.cli_exe):
            with open(e, "w", encoding="utf-8") as fh:
                fh.write("exe falso p/ teste")

    def marca_flag(self):
        with open(os.path.join(self.raiz, portable.FLAG), "w",
                  encoding="utf-8") as fh:
            fh.write("minidm em modo portable\n")

    def patch_exe(self, exe: str):
        """sys.executable apontando p/ o exe da árvore (o que o bundle faz)."""
        p = mock.patch.object(sys, "executable", exe)
        p.start()
        self.addCleanup(p.stop)
        return p


class TestRoot(_Arvore):
    """`root()` — detecção do flag subindo a árvore do executável."""

    def test_dois_exes_acham_o_mesmo_flag(self):
        # os DOIS binários do zip (minidm-gui/ e minidm/) dividem a fila
        self.marca_flag()
        self.assertEqual(os.path.normcase(portable.root(self.gui_dir)),
                         os.path.normcase(self.raiz))
        self.assertEqual(os.path.normcase(portable.root(self.cli_dir)),
                         os.path.normcase(self.raiz))

    def test_flag_no_diretorio_do_exe(self):
        # flag junto do exe (uso avulso): nível 0, sem subir
        with open(os.path.join(self.cli_dir, portable.FLAG), "w",
                  encoding="utf-8") as fh:
            fh.write("")
        self.assertEqual(os.path.normcase(portable.root(self.cli_dir)),
                         os.path.normcase(self.cli_dir))

    def test_sem_flag_devolve_none(self):
        with _sem_flag_ancestral():
            self.assertIsNone(portable.root(self.gui_dir))

    def test_limite_de_tres_niveis(self):
        # exe 3 níveis abaixo da raiz: z → y → x (3 checagens)
        fundo = os.path.join(self.raiz, "x", "y", "z")
        os.makedirs(fundo)
        # flag em x (3º nível subido) → acha
        with open(os.path.join(self.raiz, "x", portable.FLAG), "w",
                  encoding="utf-8") as fh:
            fh.write("")
        self.assertEqual(os.path.normcase(portable.root(fundo)),
                         os.path.normcase(os.path.join(self.raiz, "x")))
        # flag em <raiz> (4º nível) → passa reto (NIVEIS = 3).
        # Sem blindagem de propósito: o walk-up checa só z, y e x —
        # se o limite sumisse, ele alcançaria <raiz> e falharia aqui.
        os.unlink(os.path.join(self.raiz, "x", portable.FLAG))
        with open(os.path.join(self.raiz, portable.FLAG), "w",
                  encoding="utf-8") as fh:
            fh.write("")
        self.assertIsNone(portable.root(fundo))

    def test_apagar_o_flag_volta_ao_padrao(self):
        # é um switch, não uma instalação
        self.marca_flag()
        self.assertIsNotNone(portable.root(self.raiz))
        os.unlink(os.path.join(self.raiz, portable.FLAG))
        with _sem_flag_ancestral():
            self.assertIsNone(portable.root(self.raiz))


class TestDataDir(_Arvore):
    """`data_dir()`/`enabled()` — a pasta de dados do momento."""

    def test_com_flag_e_flag_data(self):
        self.marca_flag()
        self.patch_exe(self.gui_exe)
        esperado = os.path.join(self.raiz, "data")
        self.assertEqual(os.path.normcase(portable.data_dir()),
                         os.path.normcase(esperado))
        self.assertTrue(portable.enabled())

    def test_sem_flag_devolve_none_e_desligado(self):
        self.patch_exe(self.gui_exe)
        with _sem_flag_ancestral():
            self.assertIsNone(portable.data_dir())
            self.assertFalse(portable.enabled())


class TestConfig(_Arvore):
    """`AppConfig.load()` — o ponto único de decisão da pasta."""

    def test_load_sem_arg_usa_a_pasta_portable(self):
        self.marca_flag()
        self.patch_exe(self.gui_exe)
        # a config.json do portable é LIDA de lá (não de ~/.minidm)
        os.makedirs(os.path.join(self.raiz, "data"))
        with open(os.path.join(self.raiz, "data", "config.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"max_concurrent": 9}, fh)
        cfg = AppConfig.load()
        self.assertEqual(os.path.normcase(cfg.data_dir),
                         os.path.normcase(os.path.join(self.raiz, "data")))
        self.assertEqual(cfg.max_concurrent, 9)

    def test_data_dir_explicito_vence_do_flag(self):
        self.marca_flag()
        self.patch_exe(self.gui_exe)
        outro = tempfile.mkdtemp(prefix="minidm-outro-")
        self.addCleanup(shutil.rmtree, outro, True)
        cfg = AppConfig.load(outro)
        self.assertEqual(os.path.normcase(cfg.data_dir),
                         os.path.normcase(outro))

    def test_sem_flag_o_padrao_continua(self):
        self.patch_exe(self.gui_exe)
        with _sem_flag_ancestral():
            self.assertEqual(os.path.normcase(default_data_dir()),
                             os.path.normcase(DEFAULT_DATA_DIR))


class TestStore(_Arvore):
    """`TaskStore()` sem argumento — GUI/CLI/daemon na MESMA fila."""

    def test_store_sem_arg_usa_a_pasta_portable(self):
        from minidm.store import TaskStore

        self.marca_flag()
        self.patch_exe(self.gui_exe)
        store = TaskStore()                       # sem argumento
        esperado = os.path.join(self.raiz, "data", "tasks.json")
        self.assertEqual(os.path.normcase(store.path),
                         os.path.normcase(esperado))
        self.assertTrue(os.path.isdir(store.data_dir))   # criou em ./data

    def test_store_com_data_dir_explicito_continua_valendo(self):
        from minidm.store import TaskStore

        self.marca_flag()
        self.patch_exe(self.gui_exe)
        outro = tempfile.mkdtemp(prefix="minidm-fila-")
        self.addCleanup(shutil.rmtree, outro, True)
        store = TaskStore(outro)
        self.assertEqual(os.path.normcase(store.data_dir),
                         os.path.normcase(outro))


class TestCli(_Arvore):
    """`minidm config show/set` — marca [portable] e grava em ./data."""

    @staticmethod
    def _run(argv):
        from minidm import cli
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_config_show_marca_portable(self):
        self.marca_flag()
        self.patch_exe(self.gui_exe)
        code, out, _ = self._run(["config", "show"])
        self.assertEqual(code, 0)
        primeira = out.splitlines()[0]
        self.assertIn("data_dir:", primeira)
        self.assertIn(os.path.join(self.raiz, "data"), primeira)
        self.assertIn("[portable: portable.flag]", primeira)

    def test_config_show_sem_flag_sem_marca(self):
        self.patch_exe(self.gui_exe)
        with _sem_flag_ancestral():
            code, out, _ = self._run(["config", "show"])
        self.assertEqual(code, 0)
        self.assertIn(f"data_dir: {DEFAULT_DATA_DIR}", out.splitlines()[0])
        self.assertNotIn("[portable", out)

    def test_data_dir_explicito_no_cli_nao_finge_portable(self):
        # flag presente, mas --data-dir mandou em outra pasta:
        # a marca só aparece se a config MOROU na pasta portable
        self.marca_flag()
        self.patch_exe(self.gui_exe)
        outro = tempfile.mkdtemp(prefix="minidm-cli-")
        self.addCleanup(shutil.rmtree, outro, True)
        code, out, _ = self._run(["config", "show", "--data-dir", outro])
        self.assertEqual(code, 0)
        self.assertIn(f"data_dir: {outro}", out.splitlines()[0])
        self.assertNotIn("[portable", out)

    def test_config_set_grava_na_pasta_portable(self):
        self.marca_flag()
        self.patch_exe(self.gui_exe)
        code, out, err = self._run(["config", "set", "max_concurrent", "7"])
        self.assertEqual(code, 0, err)
        cfg_json = os.path.join(self.raiz, "data", "config.json")
        self.assertTrue(os.path.isfile(cfg_json),
                        "set não gravou em <flag>/data")
        with open(cfg_json, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["max_concurrent"], 7)
        # e a próxima leitura (sem flag de novo) pega o mesmo valor
        cfg = AppConfig.load()
        self.assertEqual(cfg.max_concurrent, 7)
        self.assertEqual(os.path.normcase(cfg.data_dir),
                         os.path.normcase(os.path.join(self.raiz, "data")))


if __name__ == "__main__":
    unittest.main()
