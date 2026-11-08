# -*- coding: utf-8 -*-
"""Publicação Android (MediaStore): guards, movimentação e fallback.

No Android o HOME é o armazenamento privado do app, então
`androidpub.publish_if_needed` move o arquivo concluído para
`Download/<categoria>` da pasta pública (visível no sistema). Aqui
simulamos a classe Java (`MainActivity.publish`) com um fake — fora do
APK o import dela falha e a função é no-op.

Rodar:  python -m unittest discover -s tests -p "test_androidpub.py" -v
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from minidm import androidpub
from minidm.meta import Task, TaskState


class FakeMainActivity:
    """Simula a classe Java do APK: move o arquivo e devolve o destino."""

    calls: list = []
    fail: bool = False        # simula erro do MediaStore
    same: bool = False        # devolve o próprio src (arquivo já público)
    root: str = ""            # "pasta Download/" pública falsa

    @classmethod
    def reset(cls, root: str) -> None:
        cls.calls = []
        cls.fail = False
        cls.same = False
        cls.root = root

    @classmethod
    def publish(cls, src, subdir, name):
        cls.calls.append((src, subdir, name))
        if cls.fail:
            raise RuntimeError("falha simulada do MediaStore")
        if cls.same:
            return src
        d = os.path.join(cls.root, *subdir.split("/")) if subdir else cls.root
        os.makedirs(d, exist_ok=True)
        stem, ext = os.path.splitext(name)
        dst = os.path.join(d, name)
        i = 1
        while os.path.exists(dst):       # colisão -> " (1)", como o SO
            dst = os.path.join(d, f"{stem} ({i}){ext}")
            i += 1
        os.replace(src, dst)
        return dst


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.private = os.path.join(self.tmp.name, "privado", "Downloads")
        self.public = os.path.join(self.tmp.name, "publico")
        os.makedirs(self.private, exist_ok=True)
        FakeMainActivity.reset(self.public)
        for patcher in (
                mock.patch.object(androidpub, "DEFAULT_BASE", self.private),
                mock.patch.object(androidpub, "_JAVA", FakeMainActivity)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg = mock.Mock()
        self.cfg.android_public_downloads = True

    def tarefa(self, state=TaskState.DONE, subdir="Videos"):
        dest = os.path.join(self.private, subdir) if subdir else self.private
        os.makedirs(dest, exist_ok=True)
        t = Task(url="http://exemplo/a.mp4", dest_dir=dest,
                 filename="a.mp4", state=state)
        with open(t.final_path, "wb") as f:
            f.write(b"conteudo")
        return t


class TestPublish(Base):
    def test_move_para_download_publico(self):
        t = self.tarefa()
        pub = androidpub.publish_if_needed(t, self.cfg)
        self.assertEqual(pub, t.published_path)
        self.assertTrue(os.path.isfile(pub))
        self.assertEqual(os.path.dirname(pub),
                         os.path.join(self.public, "Videos"))
        self.assertEqual(os.path.basename(pub), "a.mp4")
        with open(pub, "rb") as f:
            self.assertEqual(f.read(), b"conteudo")   # nada corrompido
        self.assertFalse(os.path.exists(t.final_path))  # movido, não copiado
        self.assertEqual(FakeMainActivity.calls,
                         [(os.path.abspath(t.final_path), "Videos", "a.mp4")])

    def test_nao_publica_arquivo_vazio(self):
        # 0 bytes = download que falhou em silêncio (bug do celular):
        # o arquivo NUNCA vai para a pasta pública do sistema
        t = self.tarefa()
        with open(t.final_path, "wb"):
            pass                          # truncate para 0 bytes
        self.assertIsNone(androidpub.publish_if_needed(t, self.cfg))
        self.assertEqual(FakeMainActivity.calls, [])
        self.assertEqual(t.published_path, "")
        self.assertTrue(os.path.isfile(t.final_path))   # intacto

    def test_dest_dir_e_filename_nao_mudam(self):
        # as engines reescrevem em dest_dir (mux/resume/re-execução): o
        # registro original fica; só published_path muda
        t = self.tarefa()
        dest, nome = t.dest_dir, t.filename
        androidpub.publish_if_needed(t, self.cfg)
        self.assertEqual(t.dest_dir, dest)
        self.assertEqual(t.filename, nome)

    def test_sidecar_meta_interno_removido(self):
        t = self.tarefa()
        with open(t.meta_path, "w", encoding="utf-8") as f:
            f.write("{}")
        androidpub.publish_if_needed(t, self.cfg)
        self.assertFalse(os.path.exists(t.meta_path))

    def test_fora_do_android_e_no_op(self):
        # sem a classe Java do APK o import falha: nada acontece
        t = self.tarefa()
        with mock.patch.object(androidpub, "_JAVA",
                               androidpub._UNRESOLVED):
            self.assertIsNone(androidpub._main_activity())  # resolve p/ None
            pub = androidpub.publish_if_needed(t, self.cfg)
        self.assertIsNone(pub)
        self.assertEqual(t.published_path, "")
        self.assertTrue(os.path.isfile(t.final_path))  # intacto
        self.assertEqual(FakeMainActivity.calls, [])

    def test_config_desligada(self):
        self.cfg.android_public_downloads = False
        t = self.tarefa()
        self.assertIsNone(androidpub.publish_if_needed(t, self.cfg))
        self.assertTrue(os.path.isfile(t.final_path))
        self.assertEqual(FakeMainActivity.calls, [])

    def test_somente_estado_done(self):
        t = self.tarefa(state=TaskState.QUEUED)
        self.assertIsNone(androidpub.publish_if_needed(t, self.cfg))
        self.assertTrue(os.path.isfile(t.final_path))
        self.assertEqual(FakeMainActivity.calls, [])

    def test_dest_fora_da_base_padrao(self):
        # download_base explícito é escolha do usuário: não publica
        outro = os.path.join(self.tmp.name, "outra")
        os.makedirs(outro, exist_ok=True)
        t = Task(url="http://exemplo/b.bin", dest_dir=outro,
                 filename="b.bin", state=TaskState.DONE)
        with open(t.final_path, "wb") as f:
            f.write(b"x")
        self.assertIsNone(androidpub.publish_if_needed(t, self.cfg))
        self.assertTrue(os.path.isfile(t.final_path))
        self.assertEqual(FakeMainActivity.calls, [])

    def test_arquivo_ausente_preserva_publicado(self):
        t = self.tarefa()
        os.remove(t.final_path)             # já publicado/removido antes
        t.published_path = "/Download/Videos/a.mp4"
        self.assertIsNone(androidpub.publish_if_needed(t, self.cfg))
        self.assertEqual(t.published_path, "/Download/Videos/a.mp4")
        self.assertEqual(FakeMainActivity.calls, [])

    def test_falha_java_nao_lanca_e_nao_move(self):
        # publicação é melhor-esforço: erro Java não pode derrubar o worker
        FakeMainActivity.fail = True
        t = self.tarefa()
        pub = androidpub.publish_if_needed(t, self.cfg)
        self.assertIsNone(pub)
        self.assertEqual(t.published_path, "")
        self.assertTrue(os.path.isfile(t.final_path))  # intacto

    def test_colisao_vira_nome_1(self):
        alvo = os.path.join(self.public, "Videos")
        os.makedirs(alvo, exist_ok=True)
        with open(os.path.join(alvo, "a.mp4"), "wb") as f:
            f.write(b"antigo")
        t = self.tarefa()
        pub = androidpub.publish_if_needed(t, self.cfg)
        self.assertEqual(os.path.basename(pub), "a (1).mp4")
        with open(pub, "rb") as f:
            self.assertEqual(f.read(), b"conteudo")     # o novo, não o velho
        with open(os.path.join(alvo, "a.mp4"), "rb") as f:
            self.assertEqual(f.read(), b"antigo")       # pré-existente intacto

    def test_tarefa_direto_na_base_vai_para_raiz(self):
        t = self.tarefa(subdir=None)       # dest_dir == base, sem categoria
        pub = androidpub.publish_if_needed(t, self.cfg)
        self.assertEqual(FakeMainActivity.calls[0][1], "")  # subdir vazio
        self.assertEqual(os.path.dirname(pub), self.public)

    def test_caminho_retornado_igual_ao_origem(self):
        # "já está na pasta pública": publicação no-op (não registra)
        FakeMainActivity.same = True
        t = self.tarefa()
        self.assertIsNone(androidpub.publish_if_needed(t, self.cfg))
        self.assertEqual(t.published_path, "")
        self.assertTrue(os.path.isfile(t.final_path))


class TestSchedulerPublica(unittest.TestCase):
    """O worker do scheduler publica antes de emitir "done" (ponta a ponta)."""

    def test_worker_publica_ao_concluir(self):
        from minidm import scheduler as sched_mod
        from minidm.config import AppConfig
        from minidm.scheduler import Scheduler
        from minidm.store import TaskStore

        with tempfile.TemporaryDirectory() as tmp:
            private = os.path.join(tmp, "privado", "Downloads")
            public = os.path.join(tmp, "publico")
            os.makedirs(private, exist_ok=True)
            FakeMainActivity.reset(public)

            cfg = AppConfig(data_dir=os.path.join(tmp, "dados"))
            store = TaskStore(cfg.data_dir)
            t = Task(url="http://exemplo/a.mp4",
                     dest_dir=os.path.join(private, "Videos"),
                     filename="a.mp4", state=TaskState.QUEUED)
            store.add(t)

            done = threading.Event()

            def on_event(ev, task):
                if ev == "done":
                    done.set()

            class FakeDL:
                def __init__(self, task):
                    self.task = task

                def run(self):
                    os.makedirs(self.task.dest_dir, exist_ok=True)
                    with open(self.task.final_path, "wb") as f:
                        f.write(b"x")
                    self.task.state = TaskState.DONE
                    self.task.finished_at = time.time()

                def request_stop(self):
                    pass

            with mock.patch.object(
                    sched_mod, "make_downloader",
                    lambda task, **kw: FakeDL(task)), \
                 mock.patch.object(androidpub, "DEFAULT_BASE", private), \
                 mock.patch.object(androidpub, "_JAVA", FakeMainActivity):
                s = Scheduler(store, cfg, on_event=on_event)
                s.start()
                s.submit()
                self.assertTrue(done.wait(10), "worker não emitiu done")
                s.shutdown()

            self.assertEqual(len(FakeMainActivity.calls), 1)
            self.assertTrue(t.published_path)
            self.assertTrue(os.path.isfile(t.published_path))
            self.assertFalse(os.path.exists(t.final_path))
            self.assertEqual(t.state, TaskState.DONE)

            # o snapshot (API/webui) expõe onde o arquivo foi publicado
            row = next((r for r in s.snapshot() if r["id"] == t.id), None)
            self.assertIsNotNone(row)
            self.assertEqual(row["published_path"], t.published_path)


if __name__ == "__main__":
    unittest.main()
