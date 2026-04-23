@echo off
title SafeRoute Launcher
cd /d "%~dp0"

echo Stopping any existing SafeRoute server...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8000 "') do (
    taskkill /PID %%a /F >nul 2>&1
)
timeout /t 2 /nobreak >nul

echo Starting SafeRoute backend...
start "SafeRoute Backend" cmd /k "cd /d "%~dp0" && python -m uvicorn backend.main:app --reload --host 0.0.0.0"

echo Waiting for server to be ready...
timeout /t 6 /nobreak >nul

echo Opening browser...
start "" "http://localhost:8000/login.html"

echo.
echo ================================================
echo  SafeRoute is running at http://localhost:8000
echo  Keep the "SafeRoute Backend" window open.
echo  Close it to stop the server.
echo ================================================
