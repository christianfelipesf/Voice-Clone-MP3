@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PY=C:\Users\Christian Felipe\AppData\Local\Programs\Python\Python311\python.exe"
if not exist "%PY%" (
  where python >nul 2>nul && set "PY=python" || set "PY=py"
)

"%PY%" dublar_gui.py
if errorlevel 1 (
  echo.
  echo  Erro ao iniciar o menu grafico. Verifique se o Python e as
  echo  dependencias estao instalados (pip install -r requirements.txt).
  pause
)
