@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" main.py
  exit /b 0
)
where pyw >nul 2>&1
if %errorlevel%==0 (
  start "" pyw main.py
  exit /b 0
)
where pythonw >nul 2>&1
if %errorlevel%==0 (
  start "" pythonw main.py
  exit /b 0
)
where py >nul 2>&1
if %errorlevel%==0 (
  start "" py main.py
  exit /b 0
)
start "" python main.py
