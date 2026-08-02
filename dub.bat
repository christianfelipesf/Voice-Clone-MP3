@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY=C:\Users\Christian Felipe\AppData\Local\Programs\Python\Python311\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" dublar_gui.py
if errorlevel 1 (
  echo.
  echo  Erro ao iniciar o menu grafico.
  pause
)
