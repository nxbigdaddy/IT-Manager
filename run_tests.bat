@echo off
setlocal
cd /d "%~dp0"
set QT_QPA_PLATFORM=offscreen
if not exist ".venv\Scripts\python.exe" (
 echo Run setup_env.bat first.
 pause
 exit /b 1
)
".venv\Scripts\python.exe" -m pytest tests -q -p no:cacheprovider
set RESULT=%ERRORLEVEL%
pause
exit /b %RESULT%
