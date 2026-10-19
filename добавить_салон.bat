@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title Добавить салон
if not exist ".venv\Scripts\python.exe" (
  echo Сначала один раз запустите run.bat — он установит всё нужное.
  pause
  exit /b 1
)
echo Перед этим создайте НОВОГО бота для салона в @BotFather командой /newbot
echo и скопируйте его токен. У каждого салона должен быть свой бот.
echo.
".venv\Scripts\python.exe" manager.py add
echo.
echo Чтобы новый салон заработал — закройте окно run.bat и запустите его снова.
pause
