@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Meme Quant Launcher

set "ROOT=%CD%"
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
set "FRONTEND=%ROOT%\frontend"
set "BACKEND_PORT=8000"
set "FRONTEND_PORT=5173"

if not exist "%PYTHON%" (
  echo [ERROR] Missing project Python: %PYTHON%
  echo Please restore the project virtual environment first.
  pause
  exit /b 1
)

if not exist "%ROOT%\scripts\start_runtime.py" (
  echo [ERROR] Missing backend launcher: scripts\start_runtime.py
  pause
  exit /b 1
)

if not exist "%ROOT%\scripts\start_frontend.py" (
  echo [ERROR] Missing frontend launcher: scripts\start_frontend.py
  pause
  exit /b 1
)

if not exist "%FRONTEND%\package.json" (
  echo [ERROR] Missing frontend\package.json
  pause
  exit /b 1
)

where npm.cmd >nul 2>&1
if errorlevel 1 (
  echo [ERROR] npm.cmd was not found in PATH.
  pause
  exit /b 1
)

set "BACKEND_PID="
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /R /C:":%BACKEND_PORT% .*LISTENING"') do set "BACKEND_PID=%%P"
if defined BACKEND_PID (
  echo [OK] Backend already running on port %BACKEND_PORT% ^(PID !BACKEND_PID!^).
) else (
  echo [START] Backend...
  call "%PYTHON%" scripts\start_runtime.py --reload
  if errorlevel 1 goto TIMEOUT
)

set "FRONTEND_PID="
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /R /C:":%FRONTEND_PORT% .*LISTENING"') do set "FRONTEND_PID=%%P"
if defined FRONTEND_PID (
  echo [OK] Frontend already running on port %FRONTEND_PORT% ^(PID !FRONTEND_PID!^).
) else (
  echo [START] Frontend...
  call "%PYTHON%" scripts\start_frontend.py
  if errorlevel 1 goto TIMEOUT
)

echo [WAIT] Checking services...
set /a TRY=0
:CHECK_LOOP
set /a TRY+=1
set "BACKEND_OK="
set "FRONTEND_OK="
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /R /C:":%BACKEND_PORT% .*LISTENING"') do set "BACKEND_OK=1"
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /R /C:":%FRONTEND_PORT% .*LISTENING"') do set "FRONTEND_OK=1"
if defined BACKEND_OK if defined FRONTEND_OK goto READY
if !TRY! GEQ 30 goto TIMEOUT
ping 127.0.0.1 -n 2 >nul
goto CHECK_LOOP

:READY
powershell -NoProfile -Command "try { if ((Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 3).StatusCode -ne 200) { exit 1 } } catch { exit 1 }"
if errorlevel 1 goto TIMEOUT
echo.
echo [OK] Backend:  http://127.0.0.1:8000
echo [OK] Frontend: http://127.0.0.1:5173
echo [OK] Both services are running in the background without taskbar console windows.
start "" "http://127.0.0.1:5173/"
exit /b 0

:TIMEOUT
echo.
if defined BACKEND_OK (
  echo [OK] Backend port is listening.
) else (
  echo [WARN] Backend did not become ready. Check logs\backend.err.log.
)
if defined FRONTEND_OK (
  echo [OK] Frontend port is listening.
) else (
  echo [WARN] Frontend did not become ready. Check logs\frontend.err.log.
)
echo.
pause
exit /b 2
