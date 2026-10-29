@echo off
title KPA1500 - network check
cd /d "%~dp0"
echo Checking the network and the amplifier connection (about 15 seconds, changes nothing)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0kpa_netcheck.ps1" > "%~dp0kpa_network_check.txt" 2>&1
echo.
echo Done. The results are in kpa_network_check.txt in this folder.
timeout /t 8 >nul
