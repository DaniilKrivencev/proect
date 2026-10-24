@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Лендинг: http://localhost:8080
echo Не закрывайте это окно, пока смотрите сайт. Остановить - Ctrl+C.
start "" http://localhost:8080
where py >nul 2>nul && (py -m http.server 8080) || (python -m http.server 8080)
pause
