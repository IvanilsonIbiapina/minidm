"""Testes dos módulos novos: perfis, template de nome, archive, yt-dlp dl.

Rodar com:  python -m unittest discover -s tests -p "test_profiles.py"
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from minidm.archive import Archive, url_id
from minidm.config import AppConfig
from minidm.meta import Task
from minidm.nametpl import preview, render
from minidm.profiles import (CommandTemplate, Profile, ProfileStore,
                             apply_profile)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-prof-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ------------------------------------------------------------- nametpl
class TestNameTemplate(unittest.TestCase):
    def test_render_substitui_placeholders(self):
        got = render("{title} [{id}].{ext}",
                     {"title": "Aula 1", "id": "abc", "ext": "mp4"})
        self.assertEqual(got, "Aula 1 [abc].mp4")

    def test_render_faltando_ctx_vira_vazio(self):
        got = render("{title} - {uploader}.{ext}",
                     {"title": "X", "ext": "mp3"})
        # uploader vazio -> " - " solto é removido
        self.assertEqual(got, "X.mp3")

    def test_render_ctx_completo(self):
        got = render("{title} - {uploader}.{ext}",
                     {"title": "X", "uploader": "Canal", "ext": "mp3"})
        self.assertEqual(got, "X - Canal.mp3")

    def test_render_remove_colchetes_vazios(self):
        # id vazio -> "aula []" vira "aula"
        got = render("{title} [{id}].{ext}",
                     {"title": "aula", "id": "", "ext": "mp4"})
        self.assertEqual(got, "aula.mp4")

    def test_render_mantem_colchete_com_id(self):
        got = render("{title} [{id}].{ext}",
                     {"title": "aula", "id": "abc", "ext": "mp4"})
        self.assertEqual(got, "aula [abc].mp4")

    def test_render_chaves_duplas_sao_literais(self):
        got = render("{{literal}} {title}.{ext}",
                     {"title": "ok", "ext": "ts"})
        self.assertEqual(got, "{literal} ok.ts")

    def test_render_remove_invalidos_windows(self):
        got = render("{title}.{ext}",
                     {"title": 'a<b>c:d"e/f\\g|h?i*j', "ext": "mp4"})
        self.assertNotRegex(got, r'[<>:"/\\|?*]')

    def test_render_cai_no_fallback(self):
        self.assertEqual(render("", {}), "download")
        self.assertEqual(render("   ", {}), "download")

    def test_render_aplica_fallback_ext(self):
        got = render("{title}", {"title": "filme"}, fallback_ext="mp4")
        self.assertEqual(got, "filme.mp4")

    def test_render_corta_stem_longo(self):
        got = render("{title}.{ext}", {"title": "x" * 500, "ext": "mp4"})
        self.assertTrue(got.endswith(".mp4"))
        self.assertLessEqual(len(got), 130)

    def test_preview_mostra_faltando(self):
        got = preview("{title} [{id}].{ext}", {"title": "T", "ext": "mp4"})
        self.assertEqual(got, "T [{id}].mp4")

    def test_render_escapa_html_no_titulo(self):
        # título com tags não vaza markup (mesmo que o destino seja arquivo)
        got = render("{title}.{ext}", {"title": "<script>x</script>", "ext": "mp4"})
        self.assertNotIn("<", got)
        self.assertNotIn(">", got)


# ------------------------------------------------------------- profiles
class TestProfiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-prof-")
        self.store = ProfileStore(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defaults_existentes(self):
        names = [p.name for p in self.store.profiles]
        self.assertIn("Música", names)
        self.assertIn("Vídeo", names)
        self.assertIn("Padrão", names)

    def test_get_profile_case_insensitive(self):
        self.assertIsNotNone(self.store.get_profile("música"))
        self.assertIsNotNone(self.store.get_profile("MÚSICA"))

    def test_resolve_profile_com_template(self):
        r = self.store.resolve_profile("Música")
        self.assertEqual(r.profile.name, "Música")
        self.assertTrue(r.templates)

    def test_ytdlp_args_audio(self):
        r = self.store.resolve_profile("Música")
        args = r.ytdlp_args(media_type="audio")
        self.assertIn("-x", args)
        self.assertIn("--audio-format", args)
        self.assertIn("mp3", args)
        self.assertIn("--embed-thumbnail", args)

    def test_ytdlp_args_video_formato(self):
        r = self.store.resolve_profile("Vídeo")
        args = r.ytdlp_args(media_type="video")
        self.assertIn("-f", args)
        i = args.index("-f")
        self.assertIn("mp4", args[i + 1])

    def test_template_url_regex(self):
        c = CommandTemplate(title="curto", content="--no-playlist",
                            url_regex=[r"/shorts/"])
        self.assertTrue(c.matches("https://x/shorts/abc"))
        self.assertFalse(c.matches("https://x/watch?v=1"))

    def test_templates_for_url_filtra_tipo(self):
        audio_only = [c for c in self.store.command_templates
                      if not c.use_as_extra_command_video]
        self.assertTrue(audio_only)  # existe pelo menos um só-áudio

    def test_persistencia_roundtrip(self):
        self.store.profiles.append(Profile(name="Podcast", quality="bestaudio"))
        self.store.save()
        reloaded = ProfileStore(self.tmp)
        self.assertIsNotNone(reloaded.get_profile("Podcast"))

    def test_apply_profile_preenche_task(self):
        task = Task(url="http://x/a.mp4", dest_dir=self.tmp, filename="a.mp4")
        apply_profile(task, self.store, "Música")
        self.assertEqual(task.profile, "Música")
        self.assertTrue(task.ytdlp_args)
        self.assertTrue(task.filename.endswith((".mp3", ".mp4")))

    def test_apply_profile_override_dir(self):
        dest = os.path.join(self.tmp, "saida")
        task = Task(url="http://x/a.mp4", dest_dir=self.tmp, filename="a.mp4")
        apply_profile(task, self.store, "Vídeo", dir_override=dest)
        self.assertEqual(task.dest_dir, dest)

    def test_profile_inexistente_usa_primeiro(self):
        r = self.store.resolve_profile("nao-existe")
        self.assertEqual(r.profile.name, self.store.profiles[0].name)


# ------------------------------------------------------------- archive
class TestArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-arch-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_url_id_estavel(self):
        self.assertEqual(url_id("http://a"), url_id("http://a"))
        self.assertNotEqual(url_id("http://a"), url_id("http://b"))

    def test_add_e_has(self):
        a = Archive(self.tmp)
        self.assertFalse(a.has("http://x/1"))
        a.add("http://x/1")
        self.assertTrue(a.has("http://x/1"))
        self.assertFalse(a.has("http://x/2"))

    def test_persiste_entre_instancias(self):
        Archive(self.tmp).add("http://x/1")
        b = Archive(self.tmp)
        self.assertTrue(b.has("http://x/1"))

    def test_add_duplicado_retorna_false(self):
        a = Archive(self.tmp)
        self.assertTrue(a.add("http://x/1"))
        self.assertFalse(a.add("http://x/1"))

    def test_arquivo_no_formato_ytdlp(self):
        a = Archive(self.tmp)
        a.add("http://x/1")
        with open(a.path, encoding="utf-8") as f:
            line = f.read().strip()
        self.assertTrue(line.startswith("minidm "))


# ---------------------------------------------- scheduler + archive
class TestSchedulerArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minidm-sched-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scheduler_pula_url_no_archive(self):
        from minidm.meta import TaskState
        from minidm.scheduler import Scheduler
        from minidm.store import TaskStore

        cfg = AppConfig(data_dir=self.tmp, download_archive=True,
                        max_connections=2)
        store = TaskStore(self.tmp)
        # registra a URL como já baixada
        Archive(self.tmp).add("http://ja-baixado/x.mp4")
        task = Task(url="http://ja-baixado/x.mp4", dest_dir=self.tmp,
                    filename="x.mp4", state=TaskState.QUEUED)
        store.add(task)
        sched = Scheduler(store, cfg)
        sched.start()
        sched.submit([task.id])
        self.assertTrue(sched.wait_idle(timeout=10))
        got = store.get(task.id)
        self.assertEqual(got.state, TaskState.DONE)
        sched.shutdown(wait=False)


if __name__ == "__main__":
    unittest.main()
