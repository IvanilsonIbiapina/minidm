#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Baixa o FFmpeg estático para Android e o empacota no APK como
`libffmpeg.so` / `libffprobe.so`.

Sem ffmpeg o APK não consegue fazer mux de vídeo+áudio (streams DASH/HLS
com faixa separada ficam mudos) nem conversão para mp3 — é a última
dependência externa que faltava no Android (yt-dlp e quickjs já são
embutidos).

Por que `lib*.so` em jniLibs (mesmo truque do QuickJS, ver
`android/setup_qjs.py`)? O Android 10+ (targetSdk 29+) proíbe executar
binários dos diretórios graváveis do app (regra W^X): o único local
executável é o `nativeLibraryDir`, e só arquivos `lib*.so` de
`src/main/jniLibs/<abi>/` são extraídos para lá. Os binários são
executáveis ELF normais; o prefixo `lib` é só o "disfarce" de
empacotamento — nada é carregado via System.loadLibrary.

Os nomes `libffmpeg.so`/`libffprobe.so` ainda caem redondos no yt-dlp:
`--ffmpeg-location <dir>/libffmpeg.so` faz ele derivar o ffprobe via
`filename.replace("ffmpeg", "ffprobe")` → `libffprobe.so` (ver
`yt_dlp/postprocessor/ffmpeg.py::_determine_executables`).

Origem: rhythmcache/ffmpeg-android (build 264, FFmpeg 8.0) — binários
**completamente estáticos** (sem PT_INTERP: verificado, sem glibc no
Android), sha256 conferido tanto do zip quanto de cada binário
extraído. Licença dos binários: FFmpeg GPL (código-fonte público em
https://ffmpeg.org).

Idempotente: se os `.so` já batem com o sha256 esperado, não baixa.
Qualquer falha termina com exit 1 (a CI aborta antes do Gradle).

Uso:  python android/setup_ffmpeg.py
"""

from __future__ import annotations

import hashlib
import io
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

# rhythmcache/ffmpeg-android — release build-264 (FFmpeg 8.0-ee2eb6c).
# sha256 do zip = digest oficial da release; sha256 de cada binário =
# calculado da extração (bin/ffmpeg, bin/ffprobe) e conferido sempre.
_BASE = ("https://github.com/rhythmcache/ffmpeg-android/releases/"
         "download/build-264/")
_ZIP = "ffmpeg-8.0-ee2eb6c-Static-android-{abi}.zip"
_TARGETS = (
    # (abi do Android, sha256 do zip, {nome final: sha256 do binário})
    ("arm64-v8a",
     "a18551c916b3cb60c41930d0affdf44763497fe318ccec9555ab93097a9c28c8",
     {"libffmpeg.so":
      "67e703ba03ea4598da4ee10117312e70b089129e423e2f5b139aafc8755f0b15",
      "libffprobe.so":
      "679cb86033929394017e1d960b4a8ee7ef443d7a02de97379026165df641afe7"}),
    ("x86_64",
     "f41fb1339c4d609e376bc99ea0982a11db5cebeb2dce2e302c60cce9dd0ffe0e",
     {"libffmpeg.so":
      "1dd7052a9f15a2ac96e25b04677465ac03b00bf66b7bc46bd9065d0956fa3612",
      "libffprobe.so":
      "53c47516f5f333e30f6d086eab75ad3a56bbc2cb917a3ba21160fa8c909c69cf"}),
)
# nome no tar.xz interno do zip (módulo Magisk: bin/ffmpeg + bin/ffprobe)
_TAR_INNER = "ffmpeg.tar.xz"
_TAR_NAMES = {"libffmpeg.so": "bin/ffmpeg", "libffprobe.so": "bin/ffprobe"}
_JNILIBS = Path(__file__).resolve().parent / "app" / "src" / "main" / "jniLibs"


def _fetch(url: str, timeout: int = 600) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "minidm-setup"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bins_from_zip(zip_bytes: bytes) -> dict[str, bytes]:
    """Lê o `ffmpeg.tar.xz` de dentro do zip e devolve {final: bytes}."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        with zf.open(_TAR_INNER) as inner:
            tar_bytes = inner.read()
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:xz") as tf:
        for final, nome in _TAR_NAMES.items():
            fh = tf.extractfile(nome)
            if fh is None:
                raise ValueError(f"{_TAR_INNER} sem {nome}")
            out[final] = fh.read()
    return out


def main() -> int:
    falhas: list[str] = []
    for abi, zip_sha, esperados in _TARGETS:
        destinos = {nome: _JNILIBS / abi / nome for nome in esperados}

        # já presente e íntegro? então nem baixa
        if all(p.is_file() and _sha256(p.read_bytes()) == esperados[nome]
               for nome, p in destinos.items()):
            print(f"[setup_ffmpeg] {abi}: já presente "
                  f"({', '.join(destinos)})")
            continue

        url = _BASE + _ZIP.format(abi=abi)
        print(f"[setup_ffmpeg] {abi}: baixando {_ZIP.format(abi=abi)} …",
              flush=True)
        try:
            data = _fetch(url)
        except Exception as exc:                    # noqa: BLE001
            falhas.append(f"{abi}: download falhou: {exc}")
            print(f"[setup_ffmpeg] ERRO {abi}: {exc}", file=sys.stderr)
            continue

        atual = _sha256(data)
        if atual != zip_sha:
            falhas.append(f"{abi}: sha256 do zip {atual} != {zip_sha}")
            print(f"[setup_ffmpeg] ERRO {abi}: sha256 do zip divergente "
                  f"({atual})", file=sys.stderr)
            continue

        try:
            bins = _bins_from_zip(data)
        except Exception as exc:                    # noqa: BLE001
            falhas.append(f"{abi}: extração falhou: {exc}")
            print(f"[setup_ffmpeg] ERRO {abi}: extração: {exc}",
                  file=sys.stderr)
            continue

        ok = True
        for nome, esperado in esperados.items():
            blob = bins.get(nome, b"")
            sha = _sha256(blob)
            if sha != esperado:
                ok = False
                falhas.append(f"{abi}/{nome}: sha256 {sha} != {esperado}")
                print(f"[setup_ffmpeg] ERRO {abi}/{nome}: sha256 "
                      f"divergente ({sha})", file=sys.stderr)
        if not ok:
            continue

        for nome, p in destinos.items():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(bins[nome])
            print(f"[setup_ffmpeg] {abi}: ok -> {p} "
                  f"({len(bins[nome])} bytes, sha256 ok)")

    if falhas:
        print("[setup_ffmpeg] FALHOU:", *falhas, sep="\n  ", file=sys.stderr)
        return 1
    print(f"[setup_ffmpeg] todos os ABIs prontos em {_JNILIBS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
