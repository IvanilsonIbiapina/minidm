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
# A API do GitHub corta a mensagem em 4096 chars mantendo o INÍCIO; a
# causa raiz dos stack traces (Gradle/PyInstaller) fica no FIM — se
# passar do limite, corta mantendo o final, com marcador no começo.
max=4000
if [ "${#resumo}" -gt "$max" ]; then
  resumo="...(trecho inicial omitido)...${resumo: -$max}"
fi
echo "::error title=${titulo}::${resumo}"
