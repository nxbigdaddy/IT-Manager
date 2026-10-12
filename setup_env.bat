@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -m venv .venv
) else (
  python -m venv .venv
)
if errorlevel 1 goto error
:install
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto error
".venv\Scripts\python.exe" -m itmanager.launcher check
if errorlevel 1 goto error
echo Environment ready. Run start_local.bat.
pause
exit /b 0
:error
echo Setup failed. Install Python 3.11 or newer, then retry.
pause
exit /b 1
