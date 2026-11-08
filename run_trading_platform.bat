@echo off
title Automated Crypto Trading Platform - Live Daemon
echo ========================================================
echo   Automated Crypto Trading Platform (Paper/Live Daemon)
echo ========================================================
echo.

cd /d "%~dp0backend"
echo [1/2] Starting Backend API Server & Strategy Runner on http://127.0.0.1:8000...
start "Trading Engine Backend" cmd /k "python -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

timeout /t 3 /nobreak >nul

cd /d "%~dp0frontend"
echo [2/2] Starting Frontend Web Dashboard on http://127.0.0.1:5173...
start "Trading Engine Frontend" cmd /k "npm.cmd run dev -- --host 127.0.0.1 --port 5173"

echo.
echo All services launched!
echo Open your browser at: http://127.0.0.1:5173
echo.
pause
