@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Сначала один раз запустите run.bat — он установит всё нужное.
  pause
  exit /b 1
)
title Резервная копия
".venv\Scripts\python.exe" manager.py backup
echo.
echo Копии лежат в папке backups. Самую свежую можно перенести на сервер.
pause
