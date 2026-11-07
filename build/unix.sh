#!/usr/bin/env bash
# Build do minidm p/ Linux/macOS (CI): deps + PyInstaller onedir.
# Saída esperada: dist/minidm/ e dist/minidm-gui/ (onedir).
set -euo pipefail
python -m pip install --upgrade pip
python -m pip install -r packaging/requirements.txt
python -m PyInstaller packaging/minidm.spec --noconfirm
