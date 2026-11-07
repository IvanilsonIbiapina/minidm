# -*- coding: utf-8 -*-
"""CLI: duplo clique (sem argumentos) abre a GUI — comportamento de programa.

O `minidm.exe` sem subcomando morria no argparse ("the following
arguments are required: cmd"): a janela do console abria, mostrava o
erro e fechava — dava a impressão de que o programa "não abriu". Agora
`minidm` sem args cai na interface, como o IDM.

Rodar:  python -m unittest discover -s tests -p "test_cli.py" -v
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from minidm import cli


class TestSemArgs(unittest.TestCase):
    def test_vazio_abre_a_gui(self):
        with mock.patch.object(cli, "cmd_gui", return_value=0) as g:
            self.assertEqual(cli.main([]), 0)
        g.assert_called_once()

    def test_version_nao_vai_para_gui(self):
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as ctx:
            cli.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("minidm", buf.getvalue())

    def test_subcomando_invalido_continua_sendo_erro(self):
        err = io.StringIO()
        with redirect_stderr(err), mock.patch.object(cli, "cmd_gui") as g, \
                self.assertRaises(SystemExit) as ctx:
            cli.main(["comando-que-nao-existe"])
        self.assertEqual(ctx.exception.code, 2)
        g.assert_not_called()
        self.assertIn("invalid choice", err.getvalue())

    def test_subcomando_valido_nao_vai_para_gui(self):
        with mock.patch.object(cli, "cmd_gui") as g, \
                mock.patch.object(cli, "build_parser") as bp:
            bp.return_value.parse_args.side_effect = SystemExit(0)
            with self.assertRaises(SystemExit):
                cli.main(["list"])
        g.assert_not_called()


if __name__ == "__main__":
    unittest.main()
