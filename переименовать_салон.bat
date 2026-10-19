@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Сначала один раз запустите run.bat — он установит всё нужное.
  pause
  exit /b 1
)
title Переименовать салон
echo Перед переименованием закройте окно run.bat.
echo.
".venv\Scripts\python.exe" manager.py rename
pause
