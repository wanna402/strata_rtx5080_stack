@echo off
powershell -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -Command Set-Service SysMain -StartupType Automatic; Start-Service SysMain; Enable-MMAgent -MemoryCompression; Write-Host \"[OK] Memory Compression is enabled!\" -ForegroundColor Green; Start-Sleep -Seconds 3'"
