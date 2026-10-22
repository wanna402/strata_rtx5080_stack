@echo off
chcp 65001 >nul
title 切换方案 A [全能视觉版]
cd /d "H:\Strata-custom"

echo ========================================================
echo  正在切换主脑配置为 [方案 A: 全能视觉版]
echo  - 视觉多模态支持: 开启 (strata-vision + mmproj)
echo  - 上下文长度: 128K (131072)
echo  - KV-Resident 流式驻留: 32768
echo  - 异步专家换页: 开启
echo ========================================================

copy /Y "configs\strata-scheme-A-vision.json" "strata-abliterated-iq3_s.json" >nul
copy /Y "configs\strata-scheme-A-vision.json" "..\Strata-main\strata-abliterated-iq3_s.json" >nul
if exist "..\Strata-main\strata-abliterated-iq3_s-novision.json" del /F /Q "..\Strata-main\strata-abliterated-iq3_s-novision.json" >nul

echo.
echo [成功] 已切换为【方案 A 全能视觉版】！
echo 重启 Strata 后即可直接读取图片与视觉分析。
echo.
pause
