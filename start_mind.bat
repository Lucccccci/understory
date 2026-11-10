@echo off
chcp 65001 >nul
cd /d "%~dp0"
title mind-app

echo.
echo   ============================================
echo     mind-app
echo   ============================================
echo.
echo   1) starts the local service
echo   2) opens it as an app window
echo.
echo   keep this window open; closing it stops the app.
echo.
pause

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo   [!] .venv not found. Falling back to system python.
  python desktop.py
  goto :end
)

"%~dp0.venv\Scripts\python.exe" desktop.py

:end
echo.
echo   exited.
pause
