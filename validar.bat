@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "PY_RUN=%CD%\.venv\Scripts\python.exe"
if not exist "%PY_RUN%" (
    echo ERRO: ambiente isolado .venv nao encontrado.
    echo Execute instalar.bat primeiro.
    pause
    exit /b 1
)
"%PY_RUN%" validar.py %*
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
