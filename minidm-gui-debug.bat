@echo off
rem  minidm — abre a interface gráfica COM janela de terminal (p/ depuração)
cd /d "%~dp0"
python -m minidm gui
pause
