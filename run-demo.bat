@echo off
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set LOG=%~dp0demo\run-log.txt
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY if exist "%USERPROFILE%\anaconda3\python.exe" set PY="%USERPROFILE%\anaconda3\python.exe"
if not defined PY ( echo No Python found. & pause & exit /b 1 )

set OLL=ollama
where ollama >nul 2>nul || set OLL="%LOCALAPPDATA%\Programs\Ollama\ollama.exe"

curl -s http://localhost:11434/api/tags >nul 2>nul || (
  echo Starting Ollama...
  start "" /min %OLL% serve
  timeout /t 8 >nul
)

if exist demo\live-run rmdir /s /q demo\live-run
xcopy /e /i /q demo\messy-downloads demo\live-run >nul

(
echo ==== slothsort demo run %DATE% %TIME% ====
%PY% --version
%OLL% --version
echo.
echo ==== BEFORE ====
dir /b demo\live-run
echo.
echo ==== PLAN ====
%PY% slothsort.py plan demo\live-run
echo.
echo ==== APPLY ====
%PY% slothsort.py apply demo\live-run
echo.
echo ==== AFTER ====
tree /f /a demo\live-run
echo.
echo ==== UNDO ====
%PY% slothsort.py undo demo\live-run
dir /b demo\live-run
echo.
echo ==== SECOND PLAN, cached ====
%PY% slothsort.py plan demo\live-run
echo ==== DONE ====
) > "%LOG%" 2>&1
type "%LOG%"
echo.
echo Log saved to %LOG%. You can close this window.
pause
