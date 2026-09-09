@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "PY_RUN=%CD%\.venv\Scripts\python.exe"
if not exist "%PY_RUN%" (
    echo ERRO: ambiente isolado .venv nao encontrado.
    echo Execute instalar.bat uma vez antes de iniciar o Auditor.
    pause
    exit /b 3
)

"%PY_RUN%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if errorlevel 1 (
    echo ERRO: o ambiente .venv nao possui Python 3.11 ou superior.
    echo Renomeie/remova .venv e execute instalar.bat novamente.
    pause
    exit /b 3
)

set "PYTHONUTF8=1"
if "%~1"=="" (
    "%PY_RUN%" auditoria.py menu
) else (
    "%PY_RUN%" auditoria.py %*
)
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo O Auditor encerrou com codigo %RC%. Consulte os relatorios e logs antes de repetir a operacao.
    pause
)
exit /b %RC%
