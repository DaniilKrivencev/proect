@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title Очистить сервис
if not exist ".venv\Scripts\python.exe" (
  echo Сначала один раз запустите run.bat — он установит всё нужное.
  pause
  exit /b 1
)
echo ============================================================
echo  ОЧИСТКА СЕРВИСА ОТ ТЕСТОВЫХ ДАННЫХ
echo  Перед этим ЗАКРОЙТЕ окно run.bat.
echo  Копия всего, что было, сохранится рядом с папкой проекта.
echo ============================================================
".venv\Scripts\python.exe" manager.py reset
echo.
pause
