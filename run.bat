@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" auto.py %*
echo.
echo Script finished. Check log.txt for details.
pause
