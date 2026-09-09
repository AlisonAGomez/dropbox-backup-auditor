@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "PY_RUN=%CD%\.venv\Scripts\python.exe"
if not exist "%PY_RUN%" (
  echo ERRO: ambiente isolado .venv nao encontrado.
  echo Execute instalar.bat primeiro.
  pause
  exit /b 3
)

"%PY_RUN%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if errorlevel 1 (
  echo ERRO: o ambiente .venv requer Python 3.11 ou superior.
  pause
  exit /b 3
)

set "PYTHONUTF8=1"
"%PY_RUN%" validar_dropbox.py %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo Validacao online do Dropbox REPROVADA.
  pause
  exit /b %RC%
)
echo.
echo Validacao online do Dropbox APROVADA.
pause
exit /b 0
