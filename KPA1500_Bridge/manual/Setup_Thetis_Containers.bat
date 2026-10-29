@echo off
title KPA1500 - set up the Thetis containers
REM  Puts the KPA1500 Telemetry and Controls windows into Thetis.
REM  If Thetis is running, the setup offers to close it (the normal way) and to start it again.
REM  A backup of your Thetis settings is saved before anything changes.
cd /d "%~dp0"
set "PYCMD=py -3"
%PYCMD% --version >nul 2>&1
if errorlevel 1 set "PYCMD=py -3"
%PYCMD% kpa1500_thetis_setup.py %*
if errorlevel 1 goto problem
echo.
echo All done. This window closes by itself in 10 seconds (or press any key).
timeout /t 10 >nul
exit /b 0
:problem
echo.
echo Something did not work - please read the messages above.
pause
exit /b 1
