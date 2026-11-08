"""Testes da bandeja do desktop (minidm/tray.py) + close_to_tray.

Só lógica pura — **nenhum teste cria um ícone real de bandeja** (a CI
roda em Linux/macOS/Windows e janelas Win32 não são portáveis):

* `action_for` traduz as Win32 do ícone/menu em ações OPEN/QUIT;
* `TrayIcon.poll` drena a fila de ações (contrato com a GUI);
* degradação graciosa fora do Windows (`show()` → False, `destroy()`
  idempotente);
* `AppConfig.close_to_tray` liga por padrão e o `load()` tolera o
  campo novo (configuração antiga continua abrindo).

Rodar:

    python -m unittest discover -s tests -p "test_tray.py" -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from minidm import tray
from minidm.config import AppConfig

try:
    import tkinter  # noqa: F401
except ImportError:
    tkinter = None


class TestActionFor(unittest.TestCase):
    """Tradução das Win32 em ações — puro, sem tocar em Win32."""

    def test_clique_esquerdo_abre(self):
        self.assertEqual(tray.action_for(tray.WM_TRAY, tray.WM_LBUTTONUP),
                         tray.OPEN)

    def test_duplo_clique_abre(self):
        self.assertEqual(
            tray.action_for(tray.WM_TRAY, tray.WM_LBUTTONDBLCLK),
            tray.OPEN)

    def test_clique_direito_abre_menu_e_nao_sai(self):
        # o menu em si é aberto no wndproc; a ação vem do WM_COMMAND
        self.assertIsNone(
            tray.action_for(tray.WM_TRAY, tray.WM_RBUTTONUP))

    def test_item_abrir_do_menu(self):
        self.assertEqual(
            tray.action_for(tray.WM_COMMAND, tray.CMD_OPEN), tray.OPEN)

    def test_item_sair_do_menu(self):
        self.assertEqual(
            tray.action_for(tray.WM_COMMAND, tray.CMD_QUIT), tray.QUIT)

    def test_item_desconhecido_ignorado(self):
        self.assertIsNone(tray.action_for(tray.WM_COMMAND, 99))

    def test_mensagem_qualquer_ignorada(self):
        self.assertIsNone(tray.action_for(0x000F, 0))   # WM_PAINT
        self.assertIsNone(tray.action_for(0, 0))


class TestTrayIcon(unittest.TestCase):
    """Fila de ações + degradação — sem criar janela nenhuma."""

    def test_poll_vazio_devolve_none(self):
        self.assertIsNone(tray.TrayIcon().poll())

    def test_poll_devolve_acoes_em_ordem(self):
        t = tray.TrayIcon()
        t._actions.put(tray.OPEN)
        t._actions.put(tray.QUIT)
        self.assertEqual(t.poll(), tray.OPEN)
        self.assertEqual(t.poll(), tray.QUIT)
        self.assertIsNone(t.poll())

    def test_nao_visivel_por_padrao(self):
        self.assertFalse(tray.TrayIcon().visible)

    def test_show_degrada_fora_do_windows(self):
        # Linux/macOS (e a lógica é a mesma se o Win32 falhar):
        # show() → False e a GUI mantém o fechar normal
        with mock.patch.object(tray.os, "name", "posix"):
            t = tray.TrayIcon()
            self.assertFalse(t.show())
        self.assertFalse(t.visible)
        self.assertIsNone(t._thread)

    def test_destroy_sem_bandeja_e_idempotente(self):
        t = tray.TrayIcon()
        t.destroy()          # não pode levantar exceção
        t.destroy()          # 2ª vez também
        self.assertFalse(t.visible)
        self.assertIsNone(t._hwnd)

    def test_destroy_com_thread_inexistente_e_seguro(self):
        t = tray.TrayIcon()
        t._hwnd = 1234        # "resto" de uma bandeja já morta
        t.destroy()
        self.assertIsNone(t._hwnd)
        self.assertFalse(t.visible)


class TestGuiFecharParaBandeja(unittest.TestCase):
    """Decisões do _on_close/_poll_tray — chamadas sobre um objeto fake
    (nenhuma janela tkinter é criada; só a lógica de ramificação)."""

    @classmethod
    def setUpClass(cls):
        if tkinter is None:
            raise unittest.SkipTest("GUI precisa de tkinter")

    @staticmethod
    def _fake(tray_visible, close_to_tray=True):
        from minidm.gui import MinidmApp
        f = MinidmApp.__new__(MinidmApp)   # sem __init__ (sem janela)
        f.config_app = AppConfig(close_to_tray=close_to_tray)
        f._tray = mock.Mock(visible=tray_visible)
        f.withdraw = mock.Mock()
        f._really_quit = mock.Mock()
        f._restore_from_tray = mock.Mock()
        f._schedule = mock.Mock(return_value="iid")
        return f

    def test_fechar_com_bandeja_some(self):
        from minidm.gui import MinidmApp
        f = self._fake(tray_visible=True)
        MinidmApp._on_close(f)
        f.withdraw.assert_called_once()
        f._really_quit.assert_not_called()

    def test_fechar_sem_bandeja_sai(self):
        from minidm.gui import MinidmApp
        f = self._fake(tray_visible=False)
        MinidmApp._on_close(f)
        f.withdraw.assert_not_called()
        f._really_quit.assert_called_once()

    def test_close_to_tray_desligado_sai_sempre(self):
        from minidm.gui import MinidmApp
        f = self._fake(tray_visible=True, close_to_tray=False)
        MinidmApp._on_close(f)
        f._really_quit.assert_called_once()

    def test_poll_abre_restaura_e_reagenda(self):
        from minidm.gui import MinidmApp
        f = self._fake(tray_visible=True)
        f._tray.poll.return_value = tray.OPEN
        MinidmApp._poll_tray(f)
        f._restore_from_tray.assert_called_once()
        f._schedule.assert_called_once()      # reagenda o poll (300 ms)

    def test_poll_quit_encerra_sem_reagendar(self):
        from minidm.gui import MinidmApp
        f = self._fake(tray_visible=True)
        f._tray.poll.return_value = tray.QUIT
        MinidmApp._poll_tray(f)
        f._really_quit.assert_called_once()
        f._schedule.assert_not_called()

    def test_poll_sem_acao_so_reagenda(self):
        from minidm.gui import MinidmApp
        f = self._fake(tray_visible=True)
        f._tray.poll.return_value = None
        MinidmApp._poll_tray(f)
        f._restore_from_tray.assert_not_called()
        f._really_quit.assert_not_called()
        f._schedule.assert_called_once()


class TestCloseToTray(unittest.TestCase):
    """close_to_tray: liga por padrão e é tolerado pelo load()."""

    def test_padrao_ligado(self):
        self.assertTrue(AppConfig().close_to_tray)

    def test_load_com_config_antiga_sem_o_campo(self):
        # config.json gravado pela v0.2 (sem close_to_tray) continua
        # abrindo — o campo novo assume o padrão
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"theme": "dark", "api_port": 9743}, f)
            cfg = AppConfig.load(tmp)
        self.assertTrue(cfg.close_to_tray)
        self.assertEqual(cfg.theme, "dark")

    def test_load_preserva_o_campo_quando_presente(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"close_to_tray": False}, f)
            cfg = AppConfig.load(tmp)
        self.assertFalse(cfg.close_to_tray)

    def test_save_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(data_dir=tmp, close_to_tray=False)
            cfg.save()
            again = AppConfig.load(tmp)
        self.assertFalse(again.close_to_tray)


if __name__ == "__main__":
    unittest.main()
