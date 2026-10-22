@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Обновление
echo Скачиваю свежий код из GitHub...
git pull --ff-only
if errorlevel 1 (
  echo.
  echo [!] Не получилось обновить. Пришлите разработчику текст выше.
  pause
  exit /b 1
)
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
echo.
echo Готово! Код обновлён. Данные салонов не тронуты.
echo Если run.bat был открыт - закройте его и запустите снова.
pause
