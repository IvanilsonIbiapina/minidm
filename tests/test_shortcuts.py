# -*- coding: utf-8 -*-
"""Atalhos no Menu Iniciar (`minidm shortcuts on|off|status` + GUI).

"Programa de verdade" estilo IDM: o zip é portátil (sem instalador), o
comando cria os atalhos do usuário. Backends: Windows = ``.lnk`` no
Menu Iniciar escrito pelo PowerShell embutido (COM WScript.Shell);
Linux = desktop entry XDG em diretório temporário.

O dispatch de backend é mockável, então a lógica roda em QUALQUER SO da
CI; a integração com o PowerShell de verdade (e o .lnk lido de volta)
só roda no Windows.

Rodar:  python -m unittest discover -s tests -p "test_shortcuts.py" -v
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from minidm import autostart, shortcuts


def _tem_tk() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        return False


class TestTarget(unittest.TestCase):
    """O alvo dos atalhos é o MESMO do autostart (compartilhado)."""

    def test_frozen_aponta_pro_exe_sem_argumentos(self):
        with mock.patch.object(autostart.sys, "executable",
                               r"C:\minidm\minidm-gui.exe"), \
             mock.patch.object(autostart.sys, "frozen", True, create=True):
            exe, args = autostart.target()
        self.assertEqual(exe, r"C:\minidm\minidm-gui.exe")
        self.assertEqual(args, "")

    def test_dev_usa_pythonw_no_windows(self):
        with mock.patch.object(autostart.sys, "executable",
                               r"C:\Python\python.exe"), \
             mock.patch.object(autostart.sys, "frozen", False, create=True), \
             mock.patch.object(autostart, "_backend", return_value="win"), \
             mock.patch.object(autostart.os.path, "isfile", return_value=True):
            exe, args = autostart.target()
        self.assertIn("pythonw.exe", exe)
        self.assertEqual(args, "-m minidm")

    def test_command_continua_igual_ao_target(self):
        # a refatoração não mudou a linha de comando do autostart
        with mock.patch.object(autostart, "target",
                               return_value=(r"C:\m\minidm.exe", "")):
            self.assertEqual(autostart.command(), r'"C:\m\minidm.exe"')
        with mock.patch.object(autostart, "target",
                               return_value=("/x/python", "-m minidm")):
            self.assertEqual(autostart.command(),
                             '"/x/python" -m minidm')


class TestBackend(unittest.TestCase):
    def test_suporte_por_plataforma(self):
        for backend, esperado in (("win", True), ("xdg", True), ("", False)):
            with mock.patch.object(shortcuts, "_backend",
                                   return_value=backend):
                self.assertEqual(shortcuts.supported(), esperado, backend)

    def test_sem_backend_on_off_status_nao_derruba(self):
        with mock.patch.object(shortcuts, "_backend", return_value=""):
            self.assertFalse(shortcuts.supported())
            with self.assertRaises(RuntimeError):
                shortcuts.enable()
            self.assertFalse(shortcuts.disable())
            st = shortcuts.status()
        self.assertFalse(st["supported"])
        self.assertFalse(st["enabled"])
        self.assertEqual(st["registered"], None)
        self.assertEqual(st["paths"], [])
        self.assertIn("—", st["backend"])

    def test_readback_false_pula_a_leitura_do_alvo(self):
        # contrato da GUI: marcar o check não pode pagar um subprocesso
        # do PowerShell só para saber se o .lnk existe
        with mock.patch.object(shortcuts, "_backend", return_value="win"), \
             mock.patch.object(shortcuts, "_lnk_read") as rd, \
             mock.patch.object(shortcuts, "_lnk_entries",
                               return_value=[("minidm.lnk", "C:\\m\\g.exe",
                                              "", "d")]), \
             mock.patch.object(shortcuts, "win_dir",
                               return_value=tempfile.mkdtemp()):
            st = shortcuts.status(readback=False)
        rd.assert_not_called()
        self.assertTrue(st["supported"])


class TestXdg(unittest.TestCase):
    """Desktop entry XDG num diretório temporário (vale em qualquer SO)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-shortcuts-")
        self._data = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = self.tmp
        # backend FORÇADO em xdg: nenhum enable/disable/status desta
        # classe pode cair no Menu Iniciar real do Windows do host
        b = mock.patch.object(shortcuts, "_backend", return_value="xdg")
        b.start()
        self.addCleanup(b.stop)
        # comando determinístico (o real depende da máquina)
        c = mock.patch.object(autostart, "command",
                              return_value='"/x/minidm" -m minidm')
        c.start()
        self.addCleanup(c.stop)

    def tearDown(self):
        if self._data is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self._data
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def path(self):
        return os.path.join(self.tmp, "applications", "minidm.desktop")

    def test_on_off_status_de_ponta_a_ponta(self):
        self.assertFalse(shortcuts.status()["enabled"])

        paths = shortcuts.enable()
        self.assertEqual(paths, [self.path])

        st = shortcuts.status()
        self.assertTrue(st["enabled"])
        self.assertFalse(st["missing"])
        self.assertFalse(st["stale"])
        # arquivo no padrão XDG Desktop Entry
        self.assertTrue(os.path.isfile(self.path))
        with open(self.path, encoding="utf-8") as fh:
            txt = fh.read()
        self.assertIn("[Desktop Entry]", txt)
        self.assertIn('Exec="/x/minidm" -m minidm', txt)
        self.assertIn("Terminal=false", txt)
        self.assertIn("Categories=Network;", txt)

        self.assertTrue(shortcuts.disable())
        self.assertFalse(shortcuts.disable())   # 2ª vez: nada a apagar
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(shortcuts.status()["enabled"])

    def test_status_stale_com_exec_diferente(self):
        shortcuts.enable()
        # usuário reinstalou em outro lugar → atalho antigo
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("[Desktop Entry]\nExec=\"/outro/minidm\"\n")
        st = shortcuts.status()
        self.assertTrue(st["enabled"])
        self.assertTrue(st["stale"])
        self.assertEqual(st["registered"], '"/outro/minidm"')

    def test_status_lista_o_que_falta(self):
        shortcuts.enable()
        os.unlink(self.path)                  # apagou na mão
        st = shortcuts.status()
        self.assertFalse(st["enabled"])
        self.assertEqual(st["missing"], [self.path])


class TestReadme(unittest.TestCase):
    """O 2º atalho (Windows) abre o LEIA-ME.txt do bundle."""

    def test_encontra_o_leia_me_do_repositorio(self):
        # nos testes o cwd é a raiz do projeto: packaging/LEIA-ME.txt
        self.assertTrue(os.path.isfile("packaging/LEIA-ME.txt"))
        self.assertIsNotNone(shortcuts._readme())

    def test_entradas_incluem_o_leia_me_quando_existe(self):
        with mock.patch.object(shortcuts, "_readme",
                               return_value=r"C:\raiz\LEIA-ME.txt"):
            entradas = shortcuts._lnk_entries()
        self.assertEqual(entradas[0][0], "minidm.lnk")
        self.assertEqual(entradas[1],
                         ("LEIA-ME.lnk", r"C:\raiz\LEIA-ME.txt", "",
                          shortcuts.DESC_LEIA))

    def test_sem_leia_me_só_o_atalho_principal(self):
        with mock.patch.object(shortcuts, "_readme", return_value=None):
            entradas = shortcuts._lnk_entries()
        self.assertEqual([e[0] for e in entradas], ["minidm.lnk"])


class TestScriptPowerShell(unittest.TestCase):
    """O script do COM WScript.Shell (unitário, sem rodar PowerShell)."""

    def test_escaped_aspas_simples_e_flags_certas(self):
        self.assertEqual(shortcuts._ps_quote(r"C:\o'brien"),
                         "'C:\\o''brien'")
        with mock.patch.object(shortcuts, "_run_ps") as run:
            shortcuts._lnk_write(r"C:\Menu\minidm.lnk",
                                 r"C:\m\minidm-gui.exe", "", "desc")
        script = run.call_args[0][0]
        self.assertIn("WScript.Shell", script)
        self.assertIn("CreateShortcut('C:\\Menu\\minidm.lnk')", script)
        self.assertIn("TargetPath='C:\\m\\minidm-gui.exe'", script)
        self.assertIn("Arguments=''", script)
        self.assertIn("Description='desc'", script)
        self.assertTrue(script.rstrip().endswith("$d.Save()"))

    def test_argumentos_do_comando_vao_no_arguments(self):
        with mock.patch.object(shortcuts, "_run_ps") as run:
            shortcuts._lnk_write(r"C:\Menu\minidm.lnk",
                                 r"C:\Python\pythonw.exe", "-m minidm", "d")
        self.assertIn("Arguments='-m minidm'",
                      run.call_args[0][0])

    def test_falha_do_powershell_vira_runtimeerror_mensuravel(self):
        with mock.patch.object(shortcuts.subprocess, "run") as sp:
            sp.return_value = mock.Mock(returncode=1, stdout="",
                                        stderr="acesso negado")
            with self.assertRaises(RuntimeError) as ctx:
                shortcuts._run_ps("$d.Save()")
        self.assertIn("acesso negado", str(ctx.exception))

    def test_leitura_do_alvo_ilegivel_devolve_none(self):
        # CreateShortcut de arquivo inexistente devolve alvo vazio
        with mock.patch.object(shortcuts, "_run_ps",
                               return_value="") as run:
            self.assertIsNone(shortcuts._lnk_read(r"C:\x.lnk"))
        self.assertTrue(run.call_args[1].get("expect"))


@unittest.skipUnless(sys.platform == "win32",
                     "PowerShell/COM só existem no Windows")
class TestPowerShellReal(unittest.TestCase):
    """Integração de verdade: o .lnk nasce e o alvo volta pelo COM.

    Usa pasta TEMPORÁRIA — o Menu Iniciar real do host nunca é tocado.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-lnk-")
        # backend já é win no host; alvo determinístico
        p = mock.patch.object(autostart, "command",
                              return_value=r'"C:\m\minidm-gui.exe"')
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_on_off_de_ponta_a_ponta_com_o_powershell_real(self):
        with mock.patch.object(shortcuts, "_backend", return_value="win"):
            paths = shortcuts.enable(directory=self.tmp)
            lnk = os.path.join(self.tmp, "minidm.lnk")
            self.assertIn(lnk, paths)
            self.assertTrue(os.path.isfile(lnk))

            # o .lnk lê de volta o alvo da GUI (round-trip do COM)
            exe, _ = autostart.target()
            alvo = shortcuts._lnk_read(lnk)
            self.assertIsNotNone(alvo, "COM não devolveu o TargetPath")
            self.assertEqual(os.path.normcase(alvo),
                             os.path.normcase(exe))

            st = shortcuts.status(directory=self.tmp)
            self.assertTrue(st["enabled"])
            self.assertFalse(st["stale"])
            self.assertFalse(st["missing"])

            self.assertTrue(shortcuts.disable(directory=self.tmp))
            self.assertFalse(os.path.exists(lnk))
            self.assertFalse(shortcuts.status(directory=self.tmp)["enabled"])


class TestCliShortcuts(unittest.TestCase):
    """`minidm shortcuts on|off|status` — mensagens claras, sem traceback."""

    @staticmethod
    def _run(argv):
        from minidm import cli
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_status_indisponivel_avisa_e_codigo_1(self):
        with mock.patch.object(shortcuts, "supported", return_value=False):
            code, _, err = self._run(["shortcuts", "status"])
        self.assertEqual(code, 1)
        self.assertIn("indispon", err)          # indisponíveis (plural)
        self.assertIn("só Windows", err)

    def test_on_mostra_os_caminhos_criados(self):
        with mock.patch.object(shortcuts, "supported", return_value=True), \
             mock.patch.object(shortcuts, "enable",
                               return_value=[r"C:\M\minidm.lnk"]):
            code, out, _ = self._run(["shortcuts", "on"])
        self.assertEqual(code, 0)
        self.assertIn("atalhos criados", out)
        self.assertIn(r"C:\M\minidm.lnk", out)

    def test_on_com_erro_de_runtime_vai_pra_stderr(self):
        with mock.patch.object(shortcuts, "supported", return_value=True), \
             mock.patch.object(shortcuts, "enable",
                               side_effect=RuntimeError("sem permissão")):
            code, _, err = self._run(["shortcuts", "on"])
        self.assertEqual(code, 1)
        self.assertIn("sem permissão", err)

    def test_off_distingue_removido_de_já_sem_atalho(self):
        with mock.patch.object(shortcuts, "supported", return_value=True), \
             mock.patch.object(shortcuts, "disable", return_value=True):
            code, out, _ = self._run(["shortcuts", "off"])
        self.assertEqual(code, 0)
        self.assertIn("removidos.", out)

        with mock.patch.object(shortcuts, "supported", return_value=True), \
             mock.patch.object(shortcuts, "disable", return_value=False):
            code, out, _ = self._run(["shortcuts", "off"])
        self.assertEqual(code, 0)
        self.assertIn("nenhum atalho", out)

    def test_status_ativo_mostra_falta_e_stale(self):
        st = {"supported": True, "backend": "Menu Iniciar do usuário",
              "enabled": True,
              "paths": [r"C:\M\minidm.lnk", r"C:\M\LEIA-ME.lnk"],
              "missing": [r"C:\M\LEIA-ME.lnk"],
              "registered": r"C:\antigo\minidm-gui.exe",
              "command": r'"C:\novo\minidm-gui.exe"', "stale": True}
        with mock.patch.object(shortcuts, "supported", return_value=True), \
             mock.patch.object(shortcuts, "status", return_value=st):
            code, out, _ = self._run(["shortcuts", "status"])
        self.assertEqual(code, 0)
        self.assertIn("ATIVOS", out)
        self.assertIn(r"C:\M\minidm.lnk", out)
        self.assertIn("(falta", out)
        self.assertIn("ATENÇÃO", out)
        self.assertIn("shortcuts on", out)


@unittest.skipUnless(_tem_tk(), "tkinter indisponível neste ambiente")
class TestGuiToggle(unittest.TestCase):
    """Menu Arquivo → 'Atalhos no Menu Iniciar' (checkbutton da GUI)."""

    @staticmethod
    def _app(marcado: bool):
        from minidm.gui import MinidmApp
        f = MinidmApp.__new__(MinidmApp)      # sem janela (padrão do test_tray)
        f._var_shortcuts = mock.Mock()
        f._var_shortcuts.get.return_value = marcado
        f._status = mock.Mock()
        return f

    def test_ligar_chama_enable_e_atualiza_status(self):
        f = self._app(True)
        with mock.patch.object(shortcuts, "enable",
                               return_value=[r"C:\M\minidm.lnk"]) as en:
            from minidm.gui import MinidmApp
            MinidmApp._toggle_shortcuts(f)
        en.assert_called_once()
        f._status.assert_called_once()
        self.assertIn("Atalhos criados", f._status.call_args[0][0])
        f._var_shortcuts.set.assert_not_called()   # sucesso: check fica

    def test_desligar_chama_disable(self):
        f = self._app(False)
        with mock.patch.object(shortcuts, "disable",
                               return_value=True) as dis:
            from minidm.gui import MinidmApp
            MinidmApp._toggle_shortcuts(f)
        dis.assert_called_once()
        self.assertIn("removidos", f._status.call_args[0][0])

    def test_erro_de_escrita_reverte_o_check(self):
        f = self._app(True)
        with mock.patch.object(shortcuts, "enable",
                               side_effect=RuntimeError("negado")):
            from minidm.gui import MinidmApp
            MinidmApp._toggle_shortcuts(f)
        # check volta ao estado real (desligado) e a barra explica
        f._var_shortcuts.set.assert_called_once_with(False)
        self.assertIn("Atalhos: negado", f._status.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
