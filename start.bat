@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% -m pip install -r requirements.txt
if %errorlevel% neq 0 (
  echo.
  echo ERRO ao instalar as dependencias.
  pause
  exit /b 1
)
%PY% app.py
pause
