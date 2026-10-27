@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" server\app.py serve
  pause
  exit /b
)
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 server\app.py serve
) else (
  python server\app.py serve
)
pause
