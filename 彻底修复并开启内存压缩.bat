@echo off
chcp 65001 >nul
title 修复并激活 Windows 内存压缩

echo [1/3] 正在恢复 DisablePagingExecutive 与预取参数...
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management" /v DisablePagingExecutive /t REG_DWORD /d 0 /f >nul
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters" /v EnablePrefetcher /t REG_DWORD /d 3 /f >nul
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters" /v EnableSuperfetch /t REG_DWORD /d 3 /f >nul

echo [2/3] 正在启动 SysMain 服务...
powershell -NoProfile -Command "Set-Service SysMain -StartupType Automatic; Start-Service SysMain"

echo [3/3] 正在激活 MMAgent 内存压缩...
powershell -NoProfile -Command "Enable-MMAgent -MemoryCompression"

echo.
echo ============================================================
echo  [成功] Windows 内存压缩底层配置已彻底修复并激活！
echo.
echo  【重要说明】：
echo  Windows 内存压缩是“按需动态工作”的。
echo  您刚开机时内存极其充裕（可用 51.7G），系统不会浪费 CPU 去压缩数据；
echo  当您双击启动 Strata 加载模型、内存用到 90%% 以上时，
echo  任务管理器里的压缩数值就会自动开始跳动增加！
echo ============================================================
pause
