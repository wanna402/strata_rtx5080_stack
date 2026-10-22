@echo off
chcp 65001 >nul
title 切换方案 B [纯文本极速版]
cd /d "H:\Strata-custom"

echo ========================================================
echo  正在切换主脑配置为 [方案 B: 纯文本极速版]
echo  - 视觉多模态支持: 卸载 (释放 1.5GB 显存给专家缓存)
echo  - 纯文本/代码生成冲刺: 65-75+ tok/s
echo  - 上下文长度: 128K (131072)
echo ========================================================

copy /Y "configs\strata-scheme-B-novision.json" "strata-abliterated-iq3_s.json" >nul
copy /Y "configs\strata-scheme-B-novision.json" "..\Strata-main\strata-abliterated-iq3_s.json" >nul
if exist "..\Strata-main\strata-abliterated-iq3_s-novision.json" del /F /Q "..\Strata-main\strata-abliterated-iq3_s-novision.json" >nul

echo.
echo [成功] 已切换为【方案 B 纯文本极速版】！
echo 重启 Strata 后即生效。
echo.
pause
