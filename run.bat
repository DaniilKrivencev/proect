@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
title Салоны — онлайн-запись

rem ===== 1. Python: ищем, если нет — ставим автоматически =====
set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY (
  where py >nul 2>nul && py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo Python не найден — устанавливаю автоматически, 1-3 минуты...
  winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PY (
  echo.
  echo [!] Не получилось установить Python автоматически.
  echo     Скачайте его с https://www.python.org/downloads/ и при установке
  echo     поставьте галочку "Add python.exe to PATH". Затем запустите run.bat снова.
  pause
  exit /b 1
)

rem ===== 2. Библиотеки (только при первом запуске) =====
if not exist ".venv\Scripts\python.exe" (
  echo Устанавливаю библиотеки, 1-2 минуты...
  "%PY%" -m venv .venv 2>nul || %PY% -m venv .venv
  if not exist ".venv\Scripts\python.exe" (echo [!] Не удалось создать окружение & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q || (echo [!] Не удалось установить библиотеки. Проверьте интернет. & pause & exit /b 1)
)

rem ===== 3. Туннель для кабинета мастера =====
if not exist "cloudflared.exe" (
  echo Скачиваю cloudflared для кабинета мастера...
  curl -L -s -o cloudflared.exe https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
)

rem ===== 4. Запуск всех салонов (менеджер сам спросит, чего не хватает) =====
".venv\Scripts\python.exe" manager.py
echo.
echo Все салоны остановлены.
pause
