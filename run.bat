@echo off
REM Start the app on Windows. First run creates a virtualenv and installs dependencies.
setlocal
cd /d "%~dp0"

if not exist .venv (
  echo Creating virtualenv...
  python -m venv .venv
  .venv\Scripts\python -m pip install --quiet --upgrade pip
  .venv\Scripts\python -m pip install --quiet -r requirements.txt
  echo Dependencies installed.
)

if not exist .env (
  copy .env.example .env >nul
  echo.
  echo Created .env -- open it in Notepad, paste in your API key, then run this again.
  exit /b 1
)

if "%PORT%"=="" set PORT=8000
echo Starting on http://127.0.0.1:%PORT%
.venv\Scripts\python -m uvicorn backend.main:app --host 127.0.0.1 --port %PORT%
