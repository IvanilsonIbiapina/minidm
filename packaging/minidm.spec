# -*- mode: python ; coding: utf-8 -*-
"""Spec do PyInstaller — minidm-gui (janela) + minidm (console).

Rodar da raiz do projeto:

    pyinstaller packaging/minidm.spec --noconfirm

Saída em `dist/`:
    minidm-gui(.exe)   interface gráfica (sem console, ícone packaging/minidm.ico)
    minidm(.exe)       CLI (console)

Notas:
* onedir (não onefile): inicia mais rápido e o antivirus reclama menos;
* `minidm.ytdlp`/`minidm.player` usam subprocesso — yt-dlp e ffmpeg são
  programas EXTERNOS (não embutimos: yt-dlp é GPL, ffmpeg é LGPL/GPL);
* tkinterdnd2 é opcional: se ausente, a GUI cai em Ctrl+V (mesmo parsing).
"""

import os

from PyInstaller.utils.hooks import collect_submodules

# pacote inteiro + submódulos importados dinamicamente (try/except)
hidden = collect_submodules("minidm")

# PIL (ícones/player) e paramiko (SFTP) são importados sob demanda
for extra in ("PIL", "paramiko"):
    try:
        hidden += collect_submodules(extra)
    except Exception:
        pass

block_cipher = None
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))          # noqa: F821
ICON = os.path.join(ROOT, "packaging", "minidm.ico")
VERSION = "6.42.63.3"   # só p/ info de arquivo; a real está em minidm.__version__

# NUNCA embutir yt-dlp: a integração é só via CLI externa (`minidm.ytdlp`
# faz `import yt_dlp` como último fallback — o PyInstaller seguiria o import
# estático e traria o pacote GPL inteiro + deps (numpy, curl_cffi…)).
# Com o exclude, o bundle acha yt-dlp só no PATH/MINIDM_YTDLP, como sempre.
#
# numpy (26 MB: numpy + numpy.libs/OpenBLAS) também fica de fora: nenhum
# código do minidm importa numpy; quem o puxa é o grafo do PyInstaller
# (hook-numpy roda só porque ele aparece), e o PIL já o trata como opcional
# (hook-PIL.ImageFilter o exclui). Só pesava o bundle.
EXCLUDES = [
    "yt_dlp",
    "youtube_dl",
    "numpy",
    "numpy.typing",
    "tkinter.test",
    "test",
    "unittest",
]

# dados: browser_ext/ p/ a extensão do navegador servida pela API local
datas = [
    (os.path.join(ROOT, "browser_ext"),
     "browser_ext"),
]

common = dict(
    hiddenimports=hidden,
    datas=datas,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    cipher=block_cipher,
    noarchive=False,
)

# --------------------------------------------------------------- GUI (janela)
gui_a = Analysis(
    [os.path.join(ROOT, "minidm", "gui.py")],
    pathex=[ROOT],
    binaries=[],
    **common,
)
gui_pyz = PYZ(gui_a.pure, gui_a.zipped_data, cipher=block_cipher)   # noqa: F821
gui_exe = EXE(
    gui_pyz,
    gui_a.scripts,
    [],
    exclude_binaries=True,
    name="minidm-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # sem terminal (duplo clique)
    icon=ICON if os.path.exists(ICON) else None,
    version=None,
)
gui_col = COLLECT(
    gui_exe,
    gui_a.binaries,
    gui_a.zipfiles,
    gui_a.datas,
    strip=False,
    upx=False,
    name="minidm-gui",
)

# ------------------------------------------------------------ CLI (console)
cli_a = Analysis(
    [os.path.join(ROOT, "minidm", "cli.py")],
    pathex=[ROOT],
    binaries=[],
    **common,
)
cli_pyz = PYZ(cli_a.pure, cli_a.zipped_data, cipher=block_cipher)   # noqa: F821
cli_exe = EXE(
    cli_pyz,
    cli_a.scripts,
    [],
    exclude_binaries=True,
    name="minidm",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,           # terminal p/ subcomandos
)
cli_col = COLLECT(
    cli_exe,
    cli_a.binaries,
    cli_a.zipfiles,
    cli_a.datas,
    strip=False,
    upx=False,
    name="minidm",
)
