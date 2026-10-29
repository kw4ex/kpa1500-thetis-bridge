@echo off
title KPA1500 - undo the Thetis container setup
REM  Puts back the Thetis settings saved just before the last container setup.
REM  If Thetis is running, it offers to close it (the normal way) and to start it again.
cd /d "%~dp0"
set "PYCMD=py -3"
%PYCMD% --version >nul 2>&1
if errorlevel 1 set "PYCMD=py -3"
%PYCMD% kpa1500_thetis_setup.py --restore
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
