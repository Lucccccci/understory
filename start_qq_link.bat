@echo off
chcp 65001 >nul
cd /d "%~dp0"
title mind - QQ

echo.
echo   ============================================
echo     mind · QQ 通道
echo   ============================================
echo.
echo   会做两件事：
echo     1) 启动服务并用浏览器打开
echo     2) 你点左栏「接上 QQ」——它会自动注入并拉起 bridge
echo.
echo   QQ 号 / SnowLuma 目录 / 面板密码：在设置向导里填。
echo.
pause

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo   [!] 没有 .venv，改用系统 python
  python desktop.py --browser
  goto :end
)
"%~dp0.venv\Scripts\python.exe" desktop.py --browser

:end
echo.
pause
