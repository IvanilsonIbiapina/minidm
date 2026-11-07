@echo off
REM Build do minidm p/ Windows (CI): deps + PyInstaller onedir.
REM Saída esperada: dist\minidm\ e dist\minidm-gui\ (onedir).
setlocal
python -m pip install --upgrade pip
if errorlevel 1 exit /b 1
python -m pip install -r packaging\requirements.txt
if errorlevel 1 exit /b 1
python -m PyInstaller packaging\minidm.spec --noconfirm
exit /b %errorlevel%
