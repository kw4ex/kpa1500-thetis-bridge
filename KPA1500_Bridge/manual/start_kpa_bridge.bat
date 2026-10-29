@echo off
REM ======================================================================
REM  start_kpa_bridge.bat  -  starts the KPA1500 / Thetis / LP-100A bridge
REM ======================================================================
REM
REM  WHAT THIS FILE DOES
REM    1. Checks whether the bridge is already running. It never starts twice.
REM    2. If it is not running, starts it in its own window.
REM    3. Optionally starts Thetis as well (see section C).
REM
REM  Put this file in the same folder as kpa1500_thetis_bridge.py
REM  (normally C:\RadioScripts). Double-click it to start the bridge.
REM  To stop the bridge, click its window and press Ctrl+C, or close it.
REM
REM ----------------------------------------------------------------------
REM  A. STARTUP FLAGS - how to change what the bridge does
REM ----------------------------------------------------------------------
REM  Flags are options added to the BRIDGE_FLAGS line in the SETTINGS block
REM  below. Separate them with spaces. Save the file, then close the bridge
REM  window and run this file again for the change to take effect.
REM
REM  Connection to the amplifier and radio
REM    --amp-ip 192.168.0.175     IP address of the KPA1500. Use  --amp-ip auto
REM                               to find it on the network at every start.
REM                               Without this flag the address last chosen in
REM                               the panel is used, else 192.168.0.175.
REM    --discover                 search the network, list KPA1500s and exit.
REM                               Run it from Command Prompt, not from here.
REM    --discover-net 192.168.1.0/24   search this network instead of the PC's own
REM    --amp-port 1500            amplifier port (leave alone)
REM    --tci-host 127.0.0.1       where Thetis TCI is (this PC by default)
REM    --tci-port 50001           Thetis TCI port
REM    --tci-path /               TCI WebSocket path (Zeus may need another)
REM    --rx 0                     receiver number (0 = RX1)
REM
REM  Behaviour
REM    --no-standby-on-tune       start with Standby-on-Tune switched OFF
REM    --send-band                also send a band-change command to the amp
REM    --dry-run                  READ ONLY: nothing is sent to the amp.
REM                               Use this to test safely.
REM    --verbose                  show every message (for troubleshooting).
REM                               Also writes them to the log, so do not leave
REM                               it on for normal use.
REM    --no-log                   do not write a log file at all
REM    --log-max-mb 2             trim the log when it passes this size, keeping
REM                               the newest part (default 2, 0 = never trim)
REM    --logfile name.log         log file name (default kpa1500_bridge.log)
REM
REM  Browser control panel (opens at http://127.0.0.1:8080)
REM    --web-port 8080            port for the panel
REM    --no-web                   turn the panel off
REM    --web-bind 0.0.0.0         let other devices on your network use it
REM    --web-token PASSWORD       password for the panel; always use this
REM                               together with --web-bind 0.0.0.0
REM                               (then browse to http://PC-IP:8080/?t=PASSWORD)
REM
REM  LP-100A power meter (needs: py -m pip install pyserial)
REM    --lp100-port COM5          the meter's COM port (see Device Manager)
REM    --lp100-baud 115200        38400 for firmware older than 1.2.0.0
REM    --lp100-debug              log the raw meter frames (first-time check)
REM    You can also pick or change the LP-100A port while the bridge is
REM    running: open the panel, click Layout, use the LP-100A port box and
REM    press Connect. That choice is saved in kpa1500_settings.json (next to
REM    the bridge) and is used at the next start instead of --lp100-port.
REM    To go back to the flag, delete kpa1500_settings.json.
REM
REM  Settings you can change while the bridge runs (panel: click Layout)
REM    Ask before Power OFF   untick to switch the amp off with one click
REM    Write log file         tick or untick; Size limit 1, 2, 5 or 10 MB
REM    LP-100A port           type or pick a port and press Connect
REM    These are saved in kpa1500_settings.json next to the bridge, are shared
REM    by the panel and the bridge window, and are used at the next start in
REM    preference to the flags above. Delete that file to go back to the flags.
REM    The bridge still refuses Power OFF while anything is transmitting.
REM
REM  Feed into Thetis containers (Multi Meter I/O)
REM    --mmio-port 50100          send readings to the Thetis UDP In entry
REM    --mmio-format xml          xml, json or raw - must match Thetis
REM    --mmio-term none           none, cr, lf or crlf - must match Thetis
REM    --mmio-listen 50101        listen for Thetis button strings
REM
REM  Remote power-on
REM    --amp-mac 54:10:EC:11:22:33   amp MAC address. Normally not needed,
REM                                  it is learned while the amp is on.
REM
REM  Example with several flags, all on one line:
REM    set "BRIDGE_FLAGS=--mmio-port 50100 --mmio-format xml --mmio-listen 50101 --lp100-port COM5"
REM
REM ----------------------------------------------------------------------
REM  B. START THE BRIDGE AUTOMATICALLY WHEN WINDOWS STARTS
REM ----------------------------------------------------------------------
REM  Method 1 - Startup folder (simplest)
REM    1. Right-click this .bat file, choose Show more options, Send to,
REM       Desktop (create shortcut).
REM    2. Press the Windows key + R, type   shell:startup   and press Enter.
REM    3. Drag the new shortcut from the desktop into the folder that opens.
REM    4. Right-click the shortcut in that folder, choose Properties, and set
REM       Run to Minimized, then OK.
REM    The bridge now starts every time you sign in to Windows.
REM    To undo: delete the shortcut from that folder.
REM
REM  Method 2 - Task Scheduler (if you want a delay after sign-in)
REM    1. Start menu, type Task Scheduler, open it.
REM    2. Action menu, Create Basic Task. Name it KPA1500 Bridge.
REM    3. Trigger: When I log on.
REM    4. Action: Start a program. Program/script: browse to this .bat file.
REM    5. Finish. To add a delay, open the task, Triggers tab, Edit, tick
REM       Delay task for, and choose 30 seconds.
REM
REM  Use ONE method, not both.
REM
REM ----------------------------------------------------------------------
REM  C. HAVE THETIS START THE BRIDGE (preferred method)
REM ----------------------------------------------------------------------
REM  I could not find any setting inside Thetis that launches another
REM  program, so this file does the job the other way round: use it to
REM  start Thetis. It starts the bridge first (only if it is not already
REM  running) and then starts Thetis. Starting Thetis from the shortcut
REM  below therefore always brings the bridge up as well.
REM
REM    1. In the SETTINGS block below, change START_THETIS to 1.
REM    2. Check THETIS_EXE points at your Thetis.exe. To find it, right-click
REM       your normal Thetis shortcut, choose Properties, and copy the
REM       Target path.
REM       Thetis HL2 (MI0BOT):  C:\Program Files\OpenHPSDR\Thetis-HL2\Thetis.exe
REM                             with  -dbid:HL2  in THETIS_FLAGS (its own settings).
REM       SDR-VST3:             C:\Program Files\OpenHPSDR\SDR-VST3\Thetis.exe
REM       (The installer fills these in when you choose the program.)
REM    3. Optional: put   -autostart   in THETIS_FLAGS to power the radio on
REM       2 seconds after Thetis starts. Other Thetis flags exist, for
REM       example  -datapath:c:\folder\  but you will not need them.
REM    4. Right-click this .bat file, Send to, Desktop (create shortcut).
REM       Rename the shortcut to Thetis + Bridge. Right-click it,
REM       Properties, Change Icon, Browse to Thetis.exe to give it the
REM       Thetis icon, and set Run to Minimized.
REM    5. From now on start Thetis with this shortcut, or pin it to the
REM       taskbar in place of the normal Thetis icon.
REM
REM  If the bridge is already running, it is left alone. If Thetis is
REM  already running, it is left alone too.
REM  Do not also use method B, or the bridge starts at sign-in even when
REM  you are not using the radio.
REM
REM ----------------------------------------------------------------------
REM  D. THE BRIDGE WINDOW
REM ----------------------------------------------------------------------
REM  - Where is it? With START_MINIMIZED=1 below, the bridge window starts
REM    minimized. Look on the taskbar for a Command Prompt or Windows Terminal
REM    button titled KPA1500 Bridge and click it. Set START_MINIMIZED=0 to have
REM    the window open normally each time.
REM  - Revision number: first line of the window, the window title, and the
REM    panel (top right, and under Layout).
REM  - Keyboard commands: click inside the window, then press a key.
REM      H key list   W open the full help in your browser
REM      I read the help in this window   O operate/standby
REM      A ATU inline/bypass   N antenna 1/2   L LP-100A port
REM      F minimum fan speed (type 0-5)   + or - fan speed up or down
REM      D find the amplifier on the network
REM      Y ask-before-power-off on/off   G log file on/off
REM      X power off   P power on   C clear fault   S standby-on-tune
REM      V version   Q quit
REM  - HELP: the full help (install, use, customizing, Thetis button strings)
REM    is built in. Click Help in the browser panel, press W in the bridge
REM    window, or open KPA1500_Bridge_Help.html in the bridge folder. It can
REM    be re-made with:  py kpa1500_thetis_bridge.py --write-help
REM  - Log file: kept small automatically (see --log-max-mb). Press G or use
REM    the panel to switch it off, or start with --no-log.
REM  - Panel tile labels (FWD, DRV and so on) can be renamed: the steps are
REM    in the notes at the top of kpa1500_thetis_bridge.py.
REM
REM ----------------------------------------------------------------------
REM  E. IF SOMETHING GOES WRONG
REM ----------------------------------------------------------------------
REM  - A window flashes and disappears: open Command Prompt, go to this
REM    folder, and run this file by typing its name to see the message.
REM  - The py launcher was not found: install Python from python.org and tick
REM    Add Python to PATH during installation.
REM  - The bridge window shows an error: it stays open on purpose. Read the
REM    last lines, or look in kpa1500_bridge.log in the bridge folder.
REM  - To confirm the panel is up, open http://127.0.0.1:8080 in a browser.
REM ======================================================================

REM ==================== SETTINGS - EDIT THESE ===========================
REM  Folder with kpa1500_thetis_bridge.py (no backslash at the end):
set "BRIDGE_DIR=C:\RadioScripts\KPA1500_Thetis_Bridge"
set "BRIDGE_SCRIPT=kpa1500_thetis_bridge.py"
REM  Flags for the bridge (see section A):
set "BRIDGE_FLAGS=--mmio-port 50100 --mmio-format xml --mmio-listen 50101"
REM  1 = run the bridge window minimized, 0 = normal window:
set "START_MINIMIZED=1"
REM  1 = also start Thetis (see section C), 0 = bridge only:
set "START_THETIS=0"
set "THETIS_EXE=C:\Program Files\OpenHPSDR\Thetis\Thetis.exe"
set "PYCMD=py -3"
set "THETIS_FLAGS="
REM  Seconds to wait after starting the bridge before starting Thetis:
set "THETIS_DELAY=3"
REM ======================================================================

REM ---------- nothing below this line needs editing ----------

%PYCMD% --version >nul 2>&1
if errorlevel 1 goto no_python

if exist "%BRIDGE_DIR%\%BRIDGE_SCRIPT%" goto have_script
echo.
echo Cannot find the bridge script at:
echo   %BRIDGE_DIR%\%BRIDGE_SCRIPT%
echo Edit BRIDGE_DIR near the top of this file.
echo.
pause
exit /b 1

:no_python
echo.
echo Python (%PYCMD%) was not found.
echo Install Python from python.org and tick Add Python to PATH.
echo.
pause
exit /b 1

:have_script
set "WINSTATE="
if "%START_MINIMIZED%"=="1" set "WINSTATE=/min"

pushd "%BRIDGE_DIR%"
%PYCMD% "%BRIDGE_SCRIPT%" --check-running %BRIDGE_FLAGS% >nul 2>&1
if "%errorlevel%"=="3" goto bridge_running
echo Starting the KPA1500 bridge...
start "KPA1500 Bridge" %WINSTATE% cmd /k %PYCMD% "%BRIDGE_SCRIPT%" %BRIDGE_FLAGS%
popd
goto after_bridge

:bridge_running
popd
echo The bridge is already running - leaving it alone.

:after_bridge
if not "%START_THETIS%"=="1" goto done
if exist "%THETIS_EXE%" goto have_thetis
echo.
echo Thetis was not found at:
echo   %THETIS_EXE%
echo Edit THETIS_EXE near the top of this file.
echo.
pause
goto done

:have_thetis
tasklist /FI "IMAGENAME eq Thetis.exe" 2>nul | find /I "Thetis.exe" >nul
if not errorlevel 1 goto thetis_running
timeout /t %THETIS_DELAY% /nobreak >nul
for %%I in ("%THETIS_EXE%") do set "THETIS_DIR=%%~dpI"
echo Starting Thetis...
pushd "%THETIS_DIR%"
start "" "%THETIS_EXE%" %THETIS_FLAGS%
popd
goto done

:thetis_running
echo Thetis is already running - leaving it alone.

:done
exit /b 0
