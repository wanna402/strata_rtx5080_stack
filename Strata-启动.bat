@echo off
title Strata 智能栈一键启动
cd /d "H:\Strata-main"
if not exist "log" mkdir log

"C:\Python312\python.exe" "H:\Strata-custom\launcher.py"

echo.
echo ========================================================
echo  [提示] Strata 引擎已退出。按任意键关闭窗口...
echo ========================================================
pause >nul
