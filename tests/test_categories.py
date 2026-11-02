# -*- coding: utf-8 -*-
"""Testes das categorias estilo IDM (pasta + perfil).

Rodar com:  python -m unittest discover -s tests -p "test_categories.py"
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from minidm.categories import (Category, category_folder, classify_filename,
                               classify_url, default_categories,
                               default_category, effective_category,
                               ensure_folder, get_category, resolve_base)


class TestCategories(unittest.TestCase):
    def test_conjunto_completo_e_default(self):
        cats = default_categories()
        names = [c.name for c in cats]
        self.assertEqual(names, ["Vídeos", "Músicas", "Programas",
                                 "Documentos", "Imagens", "Comprimidos"])
        # Vídeos é a pré-definida
        self.assertEqual(default_category().name, "Vídeos")
        self.assertTrue(default_category().default)

    def test_get_category_case_insensitive(self):
        self.assertIsNotNone(get_category("videos"))
        self.assertIsNotNone(get_category("MÚSICAS"))
        self.assertIsNotNone(get_category("músicas"))
        self.assertIsNone(get_category("inexistente"))
        self.assertIsNone(get_category(""))

    def test_categoria_aponta_perfil(self):
        self.assertEqual(get_category("Vídeos").profile, "Vídeo")
        self.assertEqual(get_category("Músicas").profile, "Música")
        self.assertEqual(get_category("Documentos").profile, "Padrão")

    def test_resolve_base_vazia_usa_downloads(self):
        class C: download_base = ""
        base = resolve_base(C())
        self.assertTrue(base.endswith("Downloads"))

    def test_resolve_base_customizada(self):
        class C: download_base = "/tmp/minibase"
        self.assertEqual(resolve_base(C()), os.path.expanduser("/tmp/minibase"))

    def test_category_folder_junta_base_e_subpasta(self):
        cat = get_category("Vídeos")
        class C: download_base = "/base"
        self.assertEqual(category_folder(cat, C()),
                         os.path.join("/base", "Vídeos"))

    def test_ensure_folder_cria(self):
        tmp = tempfile.mkdtemp(prefix="minidm-catf-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        p = os.path.join(tmp, "Vídeos")
        ensure_folder(p)
        self.assertTrue(os.path.isdir(p))
        ensure_folder(p)   # idempotente
        self.assertTrue(os.path.isdir(p))


class TestClassifier(unittest.TestCase):
    """Classificação automática por extensão (estilo IDM) — sidebar."""

    def test_classifica_por_extensao(self):
        self.assertEqual(classify_filename("filme.mkv"), "Vídeos")
        self.assertEqual(classify_filename("musica.mp3"), "Músicas")
        self.assertEqual(classify_filename("instalar.exe"), "Programas")
        self.assertEqual(classify_filename("doc.pdf"), "Documentos")
        self.assertEqual(classify_filename("foto.PNG"), "Imagens")   # case-ins.
        self.assertEqual(classify_filename("pack.zip"), "Comprimidos")

    def test_streams_vao_pra_videos(self):
        self.assertEqual(classify_filename("master.m3u8"), "Vídeos")
        self.assertEqual(classify_filename("manifest.mpd"), "Vídeos")

    def test_extensao_desconhecida_ou_ausente(self):
        self.assertEqual(classify_filename("sem_extensao"), "")
        self.assertEqual(classify_filename(""), "")
        # múltiplas extensões usa a ÚLTIMA (tar.gz -> Comprimidos)
        self.assertEqual(classify_filename("backup.tar.gz"), "Comprimidos")

    def test_classify_url_ignora_query(self):
        self.assertEqual(classify_url("https://cdn/f/filme.mp4?tok=1"),
                         "Vídeos")
        self.assertEqual(classify_url("https://cdn/path/musica.m4a"),
                         "Músicas")

    def test_effective_categoria_prefere_explicita(self):
        class T:
            filename = "x.bin"
            category = "Comprimidos"
        self.assertEqual(effective_category(T()), "Comprimidos")

    def test_effective_dedu_de_categoria_vazia(self):
        # tarefa antiga (category="") -> deduz pela extensão
        class T:
            filename = "filme.mkv"
            category = ""
        self.assertEqual(effective_category(T()), "Vídeos")

    def test_effective_sem_categoria_nem_extensao(self):
        class T:
            filename = "weird"
            category = ""
        self.assertEqual(effective_category(T()), "")


if __name__ == "__main__":
    unittest.main()
