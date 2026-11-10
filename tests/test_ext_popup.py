"""Testes de conteúdo do popup da extensão (browser_ext/popup.*).

São as DUAS correções de usabilidade relatadas com a extensão:

1. a mídia DETECTADA na aba (.ts/HLS) aparece NO TOPO do popup e o card
   genérico "baixar esta página" vai para baixo (`placeCard`);
2. o botão "Baixar…" do item passa a PERMITIR ESCOLHER A QUALIDADE
   quando o link é singular (.ts/playlist de mídia): as alturas REAIS do
   vídeo da página (análise yt-dlp já feita pelo card) entram no seletor
   e a escolha vai no payload como `height`/`category: Músicas` — igual
   ao card, em vez de um "envio cego" que escolhia a melhor sozinho.

Lê os arquivos fonte como bytes (mesmo padrão dos testes do webui) —
não executa JS: o comportamento em si é coberto pelos testes do servidor
(`height` -> seletor yt-dlp, categoria -> pasta/perfil).

Rodar com:  python -m unittest discover -s tests -p "test_ext_popup.py"
"""

from __future__ import annotations

import os
import unittest

from minidm.extsetup import default_ext_dir


def _read(name: str) -> bytes:
    path = os.path.join(default_ext_dir(), name)
    with open(path, "rb") as f:
        return f.read()


class PopupOrdemTest(unittest.TestCase):
    """(1) lista de mídia detectada acima do card da página."""

    def test_placeCard_existe_e_decide_ordem(self):
        js = _read("popup.js")
        self.assertIn(b"function placeCard", js)
        # com itens: card vai para o FIM do <main> (lista primeiro)
        self.assertIn(b"main.appendChild(card)", js)
        # sem itens: card continua no topo (é tudo que há)
        self.assertIn(b"main.insertBefore(card, list)", js)

    def test_placeCard_chamado_na_renderizacao(self):
        js = _read("popup.js")
        # na lista (render) e no card (renderPageDownload)
        self.assertRegex(js, rb"function render\(items\) \{[\s\S]{0,200}?"
                           rb"placeCard\(\)")
        self.assertRegex(js, rb"function renderPageDownload\([\s\S]*?"
                           rb"placeCard\(\)")

    def test_html_tem_card_e_lista(self):
        html = _read("popup.html")
        self.assertIn(b'id="pageDl"', html)
        self.assertIn(b'id="list"', html)


class PopupQualidadeTest(unittest.TestCase):
    """(2) "Baixar…" escolhe a qualidade quando o link é singular."""

    def test_dialogo_tem_os_tres_casos(self):
        js = _read("popup.js")
        # a) link com variantes: select do próprio item (índice)
        self.assertIn(b'mode = qualities.length > 1 ? "item"', js)
        # b) link singular + análise da página: qualidades do VÍDEO
        self.assertIn(b'"page"', js)
        self.assertIn(b"mode === \"page\"", js)
        # c) sem nada: aviso de formato único (não some com a linha)
        self.assertIn(b"\xc3\xbanico formato deste link", js)

    def test_opcoes_da_pagina(self):
        js = _read("popup.js")
        self.assertIn(b"h:${h}", js)             # alturas reais (yt-dlp)
        self.assertIn(b"(v\xc3\xaddeo da p\xc3\xa1gina)", js)
        self.assertIn(b"S\xc3\xb3 \xc3\xa1udio", js)
        # padrão = o link detectado (sem surpresa)
        self.assertIn(b'qualitySelect.value = "item"', js)

    def test_analise_da_pagina_reusada_e_esperada(self):
        js = _read("popup.js")
        # análise do card guarda a promessa e o resultado
        self.assertIn(b"_pageAnalysisPromise", js)
        self.assertIn(b"async function ensurePageAnalysis", js)
        # diálogo espera a análise p/ link singular (bug do clique rápido)
        self.assertIn(b"analisando qualidades da p\xc3\xa1gina", js)

    def test_payload_da_pagina_espelha_o_card(self):
        js = _read("popup.js")
        # ramo "daPagina": URL da aba + altura (ou Músicas p/ áudio)
        self.assertIn(b"const daPagina = escolha !== \"item\"", js)
        self.assertIn(b"payload.height = parseInt(escolha.slice(2), 10)", js)
        self.assertIn(b'payload.category = "M\xc3\xbasicas"', js)
        self.assertIn(b"payload.quality_label", js)
        # sem analisar de novo no servidor: manda direto igual ao card
        self.assertIn(b"url: pageUrl", js)

    def test_nome_so_manda_se_editado(self):
        js = _read("popup.js")
        # nome pré-preenchido é do .ts detectado — não serve p/ a página
        self.assertIn(b"const nomeOriginal = nameInput.value", js)
        self.assertIn(b"nome !== nomeOriginal", js)

    def test_fluxo_do_item_continua_intacto(self):
        js = _read("popup.js")
        # o caminho original (qualidade do próprio manifesto) permanece
        self.assertIn(b"quality_url: q.url || item.url", js)
        self.assertIn(b"rep_id: (q.dash && q.dash.id)", js)


if __name__ == "__main__":
    unittest.main()
