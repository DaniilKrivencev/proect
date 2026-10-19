@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Сначала один раз запустите run.bat — он установит всё нужное.
  pause
  exit /b 1
)
title Восстановить из копии
if "%~1"=="" (
  echo Перетащите файл копии backup_....zip прямо на этот файл восстановить_из_копии.bat
  pause
  exit /b 1
)
echo Перед восстановлением закройте окно run.bat.
".venv\Scripts\python.exe" manager.py restore "%~1"
pause
