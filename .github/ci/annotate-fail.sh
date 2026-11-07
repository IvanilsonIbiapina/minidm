#!/usr/bin/env bash
# Emite as últimas N linhas de um log como ANOTAÇÃO ::error do check-run.
# Uso: annotate-fail.sh "<titulo>" <arquivo.log> [n_linhas=40]
#
# As anotações são visíveis na API pública
# (/check-runs/{id}/annotations) sem login — é assim que a gente
# diagnostica falhas de job na CI sem ter acesso ao log completo.
titulo=${1:?titulo}
arquivo=${2:?arquivo de log}
n=${3:-40}
resumo=$(tail -n "$n" "$arquivo" 2>/dev/null || echo "(sem log)")
resumo=${resumo//%/%25}
resumo=${resumo//$'\r'/%0D}
resumo=${resumo//$'\n'/%0A}
echo "::error title=${titulo}::${resumo}"
