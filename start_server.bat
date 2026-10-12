@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
 echo Run setup_env.bat first.
 pause
 exit /b 1
)
".venv\Scripts\python.exe" -m itmanager.launcher server
if errorlevel 1 pause
