# -*- coding: utf-8 -*-
"""Autostart (`minidm autostart on|off|status` + menu da GUI).

"Programa de verdade" estilo IDM: abrir sozinho no login. Backends:
Windows = chave ``Run`` do HKCU (mesmo padrão de winreg do extsetup —
mockado aqui, o registro REAL nunca é tocado nos testes); Linux =
desktop entry XDG em diretório temporário (variável ``XDG_CONFIG_HOME``).

O dispatch de backend é mockável, então a lógica roda em QUALQUER SO da
CI; só a integração winreg de verdade pula fora do Windows.

Rodar:  python -m unittest discover -s tests -p "test_autostart.py" -v
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from minidm import autostart


def _tem_tk() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        return False


class TestCommand(unittest.TestCase):
    """A linha registrada SEMPRE abre a GUI."""

    def test_desenvolvimento_usa_m_minidm(self):
        # sem args, `python -m minidm` cai na GUI (cli.main → cmd_gui)
        with mock.patch.object(autostart.sys, "executable", "/x/python"), \
             mock.patch.object(autostart.sys, "frozen", False, create=True), \
             mock.patch.object(autostart, "_backend", return_value="xdg"):
            cmd = autostart.command()
        self.assertEqual(cmd, '"/x/python" -m minidm')

    def test_dev_no_windows_prefere_pythonw(self):
        # pythonw = login sem janela de console piscando
        with mock.patch.object(autostart.sys, "executable",
                               r"C:\Python\python.exe"), \
             mock.patch.object(autostart.sys, "frozen", False, create=True), \
             mock.patch.object(autostart, "_backend", return_value="win"), \
             mock.patch.object(autostart.os.path, "isfile", return_value=True):
            cmd = autostart.command()
        self.assertIn("pythonw.exe", cmd)
        self.assertIn("-m minidm", cmd)

    def test_sem_pythonw_no_path_do_dev_cai_no_python(self):
        with mock.patch.object(autostart.sys, "executable",
                               r"C:\Python\python.exe"), \
             mock.patch.object(autostart.sys, "frozen", False, create=True), \
             mock.patch.object(autostart, "_backend", return_value="win"), \
             mock.patch.object(autostart.os.path, "isfile", return_value=False):
            cmd = autostart.command()
        self.assertIn(r"C:\Python\python.exe", cmd)

    def test_frozen_e_o_exe_sem_argumentos(self):
        # PyInstaller: minidm-gui abre a janela; minidm sem args → GUI
        with mock.patch.object(autostart.sys, "executable",
                               r"C:\minidm\minidm-gui.exe"), \
             mock.patch.object(autostart.sys, "frozen", True, create=True):
            cmd = autostart.command()
        self.assertEqual(cmd, r'"C:\minidm\minidm-gui.exe"')
        self.assertNotIn("-m", cmd)          # sem subcomando, sem args


class TestBackend(unittest.TestCase):
    def test_suporte_por_plataforma(self):
        for backend, esperado in (("win", True), ("xdg", True), ("", False)):
            with mock.patch.object(autostart, "_backend",
                                   return_value=backend):
                self.assertEqual(autostart.supported(), esperado, backend)

    def test_sem_backend_on_off_status_nao_derruba(self):
        with mock.patch.object(autostart, "_backend", return_value=""):
            self.assertFalse(autostart.supported())
            with self.assertRaises(RuntimeError):
                autostart.enable()
            self.assertFalse(autostart.disable())
            st = autostart.status()
        self.assertFalse(st["supported"])
        self.assertFalse(st["enabled"])
        self.assertEqual(st["registered"], None)
        self.assertIn("—", st["backend"])

    def test_status_erro_de_leitura_vira_desligado(self):
        # backend win com leitura negada (registro bloqueado) → desligado
        with mock.patch.object(autostart, "_backend", return_value="win"), \
             mock.patch.object(autostart, "_win_read", return_value=None):
            st = autostart.status()
        self.assertFalse(st["enabled"])
        self.assertFalse(st["stale"])

    def test_status_detecta_registro_desatualizado(self):
        # registro aponta p/ outra instalação (zip movido, dev → exe)
        with mock.patch.object(autostart, "_backend", return_value="win"), \
             mock.patch.object(autostart, "_win_read",
                               return_value=r'"C:\antigo\minidm.exe"'):
            st = autostart.status()
        self.assertTrue(st["enabled"])
        self.assertTrue(st["stale"])
        self.assertNotEqual(st["registered"], st["command"])


class TestXdg(unittest.TestCase):
    """Desktop entry XDG num diretório temporário (val em qualquer SO)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-autostart-")
        self._env = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.tmp
        # backend FORÇADO em xdg: nenhum enable/disable/status desta
        # classe pode cair no registro real do Windows do host
        b = mock.patch.object(autostart, "_backend", return_value="xdg")
        b.start()
        self.addCleanup(b.stop)
        # comando determinístico (o real depende da máquina)
        patcher = mock.patch.object(autostart, "command",
                                    return_value='"/x/minidm" -m minidm')
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._env

    def test_on_off_status_de_ponta_a_ponta(self):
        with mock.patch.object(autostart, "_backend", return_value="xdg"):
            self.assertFalse(autostart.status()["enabled"])

            cmd = autostart.enable()
            self.assertEqual(cmd, '"/x/minidm" -m minidm')

            st = autostart.status()
            self.assertTrue(st["enabled"])
            self.assertFalse(st["stale"])
            self.assertEqual(st["registered"], '"/x/minidm" -m minidm')
            # arquivo no padrão XDG Desktop Entry
            path = os.path.join(self.tmp, "autostart", "minidm.desktop")
            self.assertTrue(os.path.isfile(path))
            with open(path, encoding="utf-8") as fh:
                txt = fh.read()
            self.assertIn("[Desktop Entry]", txt)
            self.assertIn('Exec="/x/minidm" -m minidm', txt)
            self.assertIn("Terminal=false", txt)

            self.assertTrue(autostart.disable())
            self.assertFalse(autostart.disable())   # 2ª vez: nada a apagar
            self.assertFalse(os.path.exists(path))
            self.assertFalse(autostart.status()["enabled"])

    def test_status_stale_com_exec_diferente(self):
        autostart.enable()
        # usuário reinstalou em outro lugar → registro antigo
        path = os.path.join(self.tmp, "autostart", "minidm.desktop")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(autostart._xdg_content('"/outro/minidm"'))
        with mock.patch.object(autostart, "_backend", return_value="xdg"):
            st = autostart.status()
        self.assertTrue(st["enabled"])
        self.assertTrue(st["stale"])
        self.assertEqual(st["registered"], '"/outro/minidm"')


@unittest.skipUnless(sys.platform == "win32",
                     "registro do Windows (mockado, mas winreg só existe lá)")
class TestWinreg(unittest.TestCase):
    """Integração winreg no MESMO formato do extsetup (sem tocar de
    verdade: CreateKey/SetValueEx/OpenKey/DeleteValue mockados)."""

    def test_on_escreve_chave_run_do_hkcu(self):
        fake = mock.MagicMock()
        with mock.patch("winreg.CreateKey", return_value=fake) as ck, \
             mock.patch("winreg.SetValueEx") as sv, \
             mock.patch("winreg.CloseKey") as close:
            with mock.patch.object(autostart, "command",
                                   return_value='"C:\\m\\minidm.exe"'):
                cmd = autostart.enable()
        ck.assert_called_once_with(
            mock.ANY, r"Software\Microsoft\Windows\CurrentVersion\Run")
        import winreg
        args = sv.call_args[0]
        self.assertEqual(args[1], "minidm")        # nome do valor
        self.assertEqual(args[2], 0)               # reserved
        self.assertEqual(args[3], winreg.REG_SZ)   # string
        self.assertEqual(args[4], '"C:\\m\\minidm.exe"')
        close.assert_called_once()
        self.assertEqual(cmd, '"C:\\m\\minidm.exe"')

    def test_status_le_queryvalueex(self):
        fake = mock.MagicMock()
        with mock.patch("winreg.OpenKey", return_value=fake), \
             mock.patch("winreg.QueryValueEx",
                        return_value=('"C:\\m\\minidm.exe"', 1)), \
             mock.patch("winreg.CloseKey"):
            with mock.patch.object(autostart, "_backend", return_value="win"):
                st = autostart.status()
        self.assertTrue(st["enabled"])
        self.assertEqual(st["registered"], '"C:\\m\\minidm.exe"')

    def test_off_chama_deletevalue_e_false_se_ausente(self):
        fake = mock.MagicMock()
        with mock.patch("winreg.OpenKey", return_value=fake), \
             mock.patch("winreg.DeleteValue") as dv, \
             mock.patch("winreg.CloseKey"):
            with mock.patch.object(autostart, "_backend", return_value="win"):
                self.assertTrue(autostart.disable())
        dv.assert_called_once_with(fake, "minidm")

        with mock.patch("winreg.OpenKey", side_effect=OSError), \
             mock.patch.object(autostart, "_backend", return_value="win"):
            self.assertFalse(autostart.disable())


class TestCliAutostart(unittest.TestCase):
    """`minidm autostart on|off|status` — mensagens claras, sem traceback."""

    @staticmethod
    def _run(argv):
        from minidm import cli
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_status_indisponivel_avisa_e_codigo_1(self):
        with mock.patch.object(autostart, "supported", return_value=False):
            code, _, err = self._run(["autostart", "status"])
        self.assertEqual(code, 1)
        self.assertIn("indisponível", err)

    def test_on_mostra_o_comando_registrado(self):
        with mock.patch.object(autostart, "supported", return_value=True), \
             mock.patch.object(autostart, "enable",
                               return_value='"C:\\m\\minidm.exe"'):
            code, out, _ = self._run(["autostart", "on"])
        self.assertEqual(code, 0)
        self.assertIn("ATIVO", out)
        self.assertIn(r"C:\m\minidm.exe", out)

    def test_on_com_erro_de_runtime_vai_pra_stderr(self):
        with mock.patch.object(autostart, "supported", return_value=True), \
             mock.patch.object(autostart, "enable",
                               side_effect=RuntimeError("sem permissão")):
            code, _, err = self._run(["autostart", "on"])
        self.assertEqual(code, 1)
        self.assertIn("sem permissão", err)

    def test_off_distingue_removido_de_já_desligado(self):
        with mock.patch.object(autostart, "supported", return_value=True), \
             mock.patch.object(autostart, "disable", return_value=True):
            code, out, _ = self._run(["autostart", "off"])
        self.assertEqual(code, 0)
        self.assertIn("desligado.", out)

        with mock.patch.object(autostart, "supported", return_value=True), \
             mock.patch.object(autostart, "disable", return_value=False):
            code, out, _ = self._run(["autostart", "off"])
        self.assertEqual(code, 0)
        self.assertIn("já estava", out)

    def test_status_ativo_mostra_aviso_de_stale(self):
        st = {"supported": True, "backend": "registro HKCU\\…\\Run",
              "enabled": True, "registered": '"C:\\antigo\\minidm.exe"',
              "command": '"C:\\novo\\minidm.exe"', "stale": True}
        with mock.patch.object(autostart, "supported", return_value=True), \
             mock.patch.object(autostart, "status", return_value=st):
            code, out, _ = self._run(["autostart", "status"])
        self.assertEqual(code, 0)
        self.assertIn("ATIVO", out)
        self.assertIn("ATENÇÃO", out)
        self.assertIn("autostart on", out)


@unittest.skipUnless(_tem_tk(), "tkinter indisponível neste ambiente")
class TestGuiToggle(unittest.TestCase):
    """Menu Arquivo → 'Iniciar com o sistema' (checkbutton da GUI)."""

    @staticmethod
    def _app(marcado: bool):
        from minidm.gui import MinidmApp
        f = MinidmApp.__new__(MinidmApp)      # sem janela (padrão do test_tray)
        f._var_autostart = mock.Mock()
        f._var_autostart.get.return_value = marcado
        f._status = mock.Mock()
        return f

    def test_ligar_chama_enable_e_atualiza_status(self):
        f = self._app(True)
        with mock.patch.object(autostart, "enable",
                               return_value='"C:\\m\\minidm.exe"') as en:
            from minidm.gui import MinidmApp
            MinidmApp._toggle_autostart(f)
        en.assert_called_once()
        f._status.assert_called_once()
        self.assertIn("Autostart ativo", f._status.call_args[0][0])
        f._var_autostart.set.assert_not_called()   # sucesso: check fica

    def test_desligar_chama_disable(self):
        f = self._app(False)
        with mock.patch.object(autostart, "disable",
                               return_value=True) as dis:
            from minidm.gui import MinidmApp
            MinidmApp._toggle_autostart(f)
        dis.assert_called_once()
        self.assertIn("Autostart desligado", f._status.call_args[0][0])

    def test_erro_de_registro_reverte_o_check(self):
        f = self._app(True)
        with mock.patch.object(autostart, "enable",
                               side_effect=RuntimeError("negado")):
            from minidm.gui import MinidmApp
            MinidmApp._toggle_autostart(f)
        # check volta ao estado real (desligado) e a barra explica
        f._var_autostart.set.assert_called_once_with(False)
        self.assertIn("Autostart: negado", f._status.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
