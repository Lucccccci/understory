@echo off
chcp 65001 >nul
echo.
echo   ===== 完整性级别探测 =====
echo.
echo   如果你看到 "High Mandatory Level"  -> 这个窗口是管理员
echo   如果你看到 "Medium Mandatory Level" -> 这个窗口是普通权限（我们要的）
echo.
whoami /groups | findstr /i "Mandatory Level"
echo.
pause
