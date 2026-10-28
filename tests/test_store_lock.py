# -*- coding: utf-8 -*-
"""Testes da fila multi-processo (trava de arquivo, merge, tombstones).

O cenário real: GUI e CLI abrem o MESMO `tasks.json` ao mesmo tempo.
Simulamos com duas instâncias de `TaskStore` no mesmo diretório (as
travas de Windows/POSIX conflitam por descritor, mesmo dentro do
processo) e com processos Python DE VERDADE na hora do teste de carga.

Rodar com:  python -m unittest discover -s tests -p "test_store_lock.py"
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from minidm.meta import Task, TaskState
from minidm.store import TOMBSTONE_TTL, TaskStore


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-lock-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, **kw) -> TaskStore:
        return TaskStore(self.tmp, **kw)

    @staticmethod
    def task(name: str) -> Task:
        return Task(url=f"http://x/{name}", dest_dir="/d",
                    filename=f"{name}.bin")


class TestTravaBasica(StoreBase):
    def test_salva_sem_deixar_tmp_para_tras(self):
        s = self.make()
        s.add(self.task("a"))
        leftovers = glob.glob(os.path.join(s.path + ".*.tmp"))
        self.assertEqual(leftovers, [], f"tmp órfão: {leftovers}")
        # arquivo válido com a tarefa
        raw = read_json(s.path)
        self.assertEqual(len(raw["tasks"]), 1)

    def test_dois_processos_nao_se_apagam(self):
        """A e B escrevem alternando: as DUAS tarefas têm que sobreviver."""
        a = self.make()
        b = self.make()                  # mesma fila, instância separada
        t1, t2 = self.task("A"), self.task("B")
        a.add(t1)
        b.add(t2)                        # b carregou antes -> merge obrigatório
        ids = {t["id"] for t in read_json(a.path)["tasks"]}
        self.assertIn(t1.id, ids, "add de A sumiu após save de B")
        self.assertIn(t2.id, ids)

    def test_remocao_nao_ressuscita(self):
        """A remove; B (que ainda tem a tarefa em memória) salva depois."""
        a = self.make()
        b = self.make()
        t = self.task("victim")
        a.add(t)
        b.refresh()                      # B carrega a tarefa de A
        self.assertIsNotNone(b.get(t.id))
        self.assertTrue(a.remove(t.id))
        b.save()                         # merge tem que respeitar o tombstone
        self.assertIsNone(b.get(t.id), "remoção ressuscitada na memória")
        raw = read_json(a.path)
        self.assertEqual(raw["tasks"], [])
        self.assertIn(t.id, raw["removed"])

    def test_refresh_ve_criacao_de_outro_processo(self):
        a = self.make()
        b = self.make()
        keep = self.task("antiga")
        a.add(keep)
        b.refresh()
        novo = self.task("nova")
        a.add(novo)
        b.refresh()
        self.assertIsNotNone(b.get(novo.id), "refresh não viu a tarefa nova")
        # identidade preservada: o objeto de B não foi trocado
        obj = b.get(keep.id)
        b.refresh()
        self.assertIs(b.get(keep.id), obj)


class TestMergePorVersao(StoreBase):
    def test_estado_mais_novo_vence_com_identidade(self):
        """A marca PAUSED (estrutura nova); B tem a mesma task em memória."""
        a = self.make()
        b = self.make()
        t = self.task("x")
        a.add(t)
        b.refresh()
        obj_b = b.get(t.id)              # referência viva em B
        # A muda o estado e salva (o save marca updated_at sozinho)
        a.tasks[t.id].state = TaskState.PAUSED
        a.save()
        before = obj_b.updated_at
        b.refresh()
        self.assertEqual(b.get(t.id).state, TaskState.PAUSED,
                         "estado novo de A não chegou em B")
        self.assertIs(b.get(t.id), obj_b, "objeto vivo foi substituído")
        self.assertGreater(obj_b.updated_at, before)

    def test_progresso_nao_remarca_updated_at(self):
        """Progresso (received) não é mudança estrutural: não marca nada."""
        from minidm.meta import Segment

        s = self.make()
        t = self.task("prog")
        t.total_size = 1000
        t.segments = [Segment(0, 999, received=100)]
        s.add(t)
        stamp = s.get(t.id).updated_at
        self.assertGreater(stamp, 0.0)
        s.get(t.id).segments[0].received = 500     # avanço de download
        s.save()
        self.assertEqual(s.get(t.id).updated_at, stamp,
                         "progresso marcou updated_at")

    def test_estado_marcado_marca_updated_at(self):
        s = self.make()
        t = self.task("est")
        s.add(t)
        stamp = s.get(t.id).updated_at
        time.sleep(0.01)
        s.get(t.id).state = TaskState.ERROR
        s.save()
        self.assertGreater(s.get(t.id).updated_at, stamp)

    def test_tombstone_expirado_e_esquecido(self):
        s = self.make()
        t = self.task("velho")
        s.add(t)
        s._removed[t.id] = time.time() - TOMBSTONE_TTL - 10
        s.save()
        raw = read_json(s.path)
        self.assertNotIn(t.id, raw["removed"])
        # e a tarefa volta a ser considerada "nova" (merge a readmite)
        self.assertIn(t.id, {x["id"] for x in raw["tasks"]})

    def test_leitura_tolera_arquivo_corrompido(self):
        s = self.make()
        with open(s.path, "w", encoding="utf-8") as f:
            f.write("{trunca")
        s2 = self.make()
        self.assertEqual(s2.all(), [])
        s2.add(self.task("ok"))
        self.assertEqual(len(s2.all()), 1)


class TestLockTimeout(StoreBase):
    def test_outra_instancia_segurando_da_timeout(self):
        holder = self.make()
        waiter = self.make(lock_timeout=0.3)
        released = threading.Event()

        def hold():
            with holder._file_lock():
                released.wait(3.0)

        th = threading.Thread(target=hold, daemon=True)
        th.start()
        time.sleep(0.1)                  # deixa a travar
        try:
            with self.assertRaises(TimeoutError) as ctx:
                waiter.save()
            self.assertIn("fila", str(ctx.exception))
        finally:
            released.set()
            th.join(timeout=3.0)
        # depois de liberar, funciona de novo
        waiter.add(self.task("depois"))

    def test_salva_sem_trava_nunca_falha_sem_outra_instancia(self):
        s = self.make()
        s.add(self.task("ok"))
        s.save()
        self.assertTrue(os.path.exists(s.path + ".lock"))


class TestProcessosReais(StoreBase):
    """4 processos Python escrevendo na mesma fila ao mesmo tempo."""

    N_PROC = 4
    N_EACH = 10

    def test_carga_concorrente_reais(self):
        worker = os.path.join(self.tmp, "worker.py")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(worker, "w", encoding="utf-8") as f:
            f.write(
                "import sys, os\n"
                f"sys.path.insert(0, r'{root}')\n"
                "from minidm.store import TaskStore\n"
                "from minidm.meta import Task\n"
                "d, tag, n = sys.argv[1], sys.argv[2], int(sys.argv[3])\n"
                "s = TaskStore(d, lock_timeout=30.0)\n"
                "for i in range(n):\n"
                "    s.add(Task(url=f'http://x/{tag}{i}', dest_dir='/d',\n"
                "               filename=f'{tag}{i}.bin'))\n"
                "print('done', tag)\n")
        procs = [
            subprocess.Popen(
                [sys.executable, worker, self.tmp, f"p{k}", str(self.N_EACH)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, **_hidden())
            for k in range(self.N_PROC)
        ]
        for p in procs:
            out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, f"worker falhou: {err}")
        # arquivo válido com TODAS as tarefas (nada se apagou)
        raw = read_json(os.path.join(self.tmp, "tasks.json"))
        ids = {t["id"] for t in raw["tasks"]}
        self.assertEqual(len(ids), self.N_PROC * self.N_EACH,
                         f"perdidos: {self.N_PROC * self.N_EACH - len(ids)}")
        # nenhuma instância legada encontrou JSON quebrado no caminho
        self.assertEqual(raw["removed"], {})


def _hidden() -> dict:
    if os.name == "nt":
        return {"creationflags": 0x08000000}   # CREATE_NO_WINDOW
    return {}


if __name__ == "__main__":
    unittest.main()
