#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Baixa o QuickJS (quickjs-ng) para dentro do APK como `libqjs.so`.

O yt-dlp precisa de um runtime JS p/ resolver os desafios de extração do
YouTube (componentes EJS). No desktop há node/deno/bun; no Android não
existe nenhum — então embarcamos o binário do quickjs-ng.

Por que `libqjs.so` em jniLibs e não um executável comum em assets?
O Android 10+ (targetSdk 29+) bloqueia `execve()` nos diretórios
graváveis do app (regra W^X). O único local de onde um binário pode ser
executado é o `nativeLibraryDir`, e só arquivos no padrão `lib*.so`
declarados em `src/main/jniLibs/<abi>/` são extraídos para lá (exige
`useLegacyPackaging = true`, ver `android/app/build.gradle`). O nome
`libqjs.so` também é o padrão exigido pelo merge de libs do Gradle.

O yt-dlp invoca o binário como PROGRAMA (`qjs --script arquivo.js`),
não o carrega como biblioteca — o conteúdo é um executável ELF normal;
o prefixo `lib` é só o "disfarce" de empacotamento. O binário do
quickjs-ng para Linux é estático (sem PT_INTERP/DT_NEEDED: verificado),
então roda no kernel Linux do Android sem glibc.

Idempotente: se o destino já bate com o sha256 esperado, não baixa.
Qualquer falha termina com exit 1 (a CI aborta antes do Gradle).

Uso:  python android/setup_qjs.py
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

# release quickjs-ng v0.18.0 — binários oficiais Linux (estáticos).
# sha256 conferido no release (asset) e reconfirmado a cada download.
_TARGETS = (
    # (abi do Android, nome do asset, sha256 hex)
    ("arm64-v8a", "qjs-linux-aarch64",
     "2c005c7171424a598d31c53c8dc4a0ae2e4958eb6fbb37908c885a307c78187c"),
    ("x86_64", "qjs-linux-x86_64",
     "85ded23b9fabe8c1644fc40a8d2bbdd71157affcbd807359c618cc717ee24d7d"),
)
_BASE = "https://github.com/quickjs-ng/quickjs/releases/download/v0.18.0/"
_JNILIBS = Path(__file__).resolve().parent / "app" / "src" / "main" / "jniLibs"


def _fetch(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "minidm-setup"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    falhas: list[str] = []
    for abi, asset, esperado in _TARGETS:
        destino = _JNILIBS / abi / "libqjs.so"
        url = _BASE + asset

        if destino.is_file():
            atual = _sha256(destino.read_bytes())
            if atual == esperado:
                print(f"[setup_qjs] {abi}: ja presente ({destino})")
                continue
            print(f"[setup_qjs] {abi}: hash diferente ({atual[:12]}…), "
                  "baixando de novo")

        print(f"[setup_qjs] {abi}: baixando {asset} …")
        try:
            data = _fetch(url)
        except Exception as exc:                    # noqa: BLE001
            falhas.append(f"{abi}: download falhou: {exc}")
            print(f"[setup_qjs] ERRO {abi}: {exc}", file=sys.stderr)
            continue

        atual = _sha256(data)
        if atual != esperado:
            falhas.append(f"{abi}: sha256 {atual} != {esperado}")
            print(f"[setup_qjs] ERRO {abi}: sha256 divergente "
                  f"({atual})", file=sys.stderr)
            continue

        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(data)
        print(f"[setup_qjs] {abi}: ok -> {destino} "
              f"({len(data)} bytes, sha256 ok)")

    if falhas:
        print("[setup_qjs] FALHOU:", *falhas, sep="\n  ", file=sys.stderr)
        return 1
    print("[setup_qjs] todos os ABIs prontos em "
          f"{_JNILIBS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
