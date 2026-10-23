@echo off
rem  minidm — atalho para a interface gráfica
rem  Duplo clique abre a GUI sem terminal.
cd /d "%~dp0"
start "" pythonw -m minidm gui
