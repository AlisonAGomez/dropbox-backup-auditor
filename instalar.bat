@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "PY_BASE="
py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul && set "PY_BASE=py -3.12"
if not defined PY_BASE py -3.11 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul && set "PY_BASE=py -3.11"
if not defined PY_BASE py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul && set "PY_BASE=py -3"
if not defined PY_BASE python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul && set "PY_BASE=python"

if not defined PY_BASE (
    echo ERRO: Python 3.11 ou superior nao foi encontrado.
    echo Recomendado: Python 3.12 de 64 bits.
    pause
    exit /b 1
)

echo Python base selecionado: %PY_BASE%

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
    if errorlevel 1 (
        echo ERRO: a pasta .venv existe, mas usa Python inferior a 3.11 ou esta corrompida.
        echo Renomeie/remova a pasta .venv e execute instalar.bat novamente.
        pause
        exit /b 1
    )
) else (
    echo Criando ambiente Python isolado em .venv ...
    %PY_BASE% -m venv .venv
    if errorlevel 1 (
        echo ERRO: nao foi possivel criar o ambiente virtual .venv.
        pause
        exit /b 1
    )
)

set "PY_RUN=%CD%\.venv\Scripts\python.exe"

if not exist "config.yaml" if exist "config.example.yaml" (
    copy /Y "config.example.yaml" "config.yaml" >nul
    echo Configuracao local criada a partir de config.example.yaml.
    echo Revise config.yaml antes de executar em producao.
)

"%PY_RUN%" -m pip install --upgrade pip
if errorlevel 1 (
    echo ERRO: falha ao atualizar o pip dentro do ambiente isolado.
    pause
    exit /b 1
)

"%PY_RUN%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERRO: falha ao instalar as dependencias do Auditor.
    pause
    exit /b 1
)

echo.
echo Dependencias instaladas no ambiente isolado .venv.
echo Executando validacao local...
"%PY_RUN%" validar.py
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo Instalacao e validacao concluidas com sucesso.
) else (
    echo Instalacao concluida, mas a validacao local foi reprovada.
)
pause
exit /b %RC%
