@echo off
cd /d %~dp0
echo [1/2] 自动递增版本号...
.venv\Scripts\python.exe bump_version.py
if errorlevel 1 exit /b 1
echo [2/2] PyInstaller 打包...
.venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed --name yuketang-gui --collect-all playwright gui.py
echo.
echo 打包完成：dist\yuketang-gui.exe
timeout /t 5 >nul
