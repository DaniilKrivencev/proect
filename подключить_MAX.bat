@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title Подключить MAX
if not exist ".venv\Scripts\python.exe" (
  echo Сначала один раз запустите run.bat — он установит всё нужное.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" manager.py max
echo.
pause
