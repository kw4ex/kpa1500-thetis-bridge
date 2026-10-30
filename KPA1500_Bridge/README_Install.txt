KPA1500 / Thetis Bridge  -  how to install                    (KW4EX)
=====================================================================

NOTICE - NO WARRANTY, USE AT YOUR OWN RISK
  This program is shared by one radio operator, "as is". There is no
  warranty of any kind: no promise that it works, that it is safe, or that it
  suits your equipment or purpose. Amplifiers, radios and antennas can be
  damaged, and RF can be dangerous. You are responsible for checking that it
  behaves correctly with your own station and for operating within your
  license. The author is not liable for any damage or loss. Not made,
  approved or supported by Elecraft, FlexRadio, Telepost, Thetis or Zeus.
  Licensed under the MIT License (see LICENSE.txt).

WHAT IT IS
  A small program (the "bridge") that connects an Elecraft KPA1500
  amplifier to the Thetis SDR program. It works with three Thetis versions
  (one running at a time): Thetis, Thetis HL2 (MI0BOT, Hermes Lite 2) and
  SDR-VST3. Where this file says "Thetis" it means whichever one you use.
    - the amp follows the Thetis band, Standby-on-Tune (SoT)
    - a browser control panel (http://127.0.0.1:8080)
    - windows INSIDE Thetis:
        KPA1500 Telemetry - bars for output power, SWR and drive (with a
                            yellow peak-hold band), PA temperature, volts,
                            amps, fan, faults
        KPA1500 Controls  - lit buttons: OPERATE, ATU, ANT 1/2, AMP power,
                            TUNE (safe: amp to STANDBY before the radio
                            keys), ATU TUNE, SoT, DUMMY OK, FAULT, FAN - / +
        KPA1500 Knob      - (optional) what a FlexControl knob does right now
    - the amp's own frequency read back, so you can see it follow the VFO
    - a "No antenna?" warning when the receive level is too low (per-band
      limits, DUMMY OK button, optional: keep the amp in STANDBY)
    - FlexRadio FlexControl (FlexKnob) USB tuning knob: VFO tuning, AF volume,
      AGC gain, mute, VOX ... every press programmable (Settings > FlexControl
      tuning knob). Works without the amplifier too. Remove it from DDUtil first.
    - the browser panel can also look exactly like the Thetis windows
      (?layout=Thetis) - all settings are under its Settings button
    - optional Telepost LP-100A wattmeter readings
  Pictures of the Thetis windows are in KPA1500_Bridge_Help.html, section
  "The Thetis windows (meters and buttons)".

WHAT YOU NEED
  - Windows 10 or 11
  - Thetis 2.10.3.12 or newer, Thetis HL2 2.10.3.15 or newer, or SDR-VST3 5.x,
    started at least once (so it has saved settings)
  - Its TCI server switched ON - see GETTING THETIS READY below
  - The KPA1500 on your network (cable to your router) - know its IP address
    if you can (amp NET menu, or your router's device list)
  - Python 3.8 or newer. The installer offers to install it for you.
  - pyserial (Python serial-port helper, needed only for the LP-100A).
    The installer checks for it and installs it automatically (needs internet).
  - Nothing else: the bridge and the Thetis setup use only parts that come
    with Python. Windows PowerShell (built in to Windows 10/11) runs the installer.

GETTING THETIS READY  (do this once in each program you use)
  1. Start the program the usual way. Thetis HL2: always use its own shortcut -
     it starts Thetis with  -dbid:HL2  so its settings stay separate.
  2. Setup > Serial/Network/Midi CAT > Network tab, "TCI Server" box:
       Bind IP:Port   127.0.0.1:50001
       tick  TCI Server Running
     Leave the other boxes as they are. Click Apply, then OK.
  3. Close the program normally (File > Exit or its X) so it saves.
  Only one of them can run at a time: they all use TCI port 50001 and
  the same radio. The installer checks this setting and warns if it is off.

  Before updating or installing another Thetis version, back up
     %APPDATA%\OpenHPSDR   (all their settings)
  by copying the folder somewhere safe, e.g. C:\RadioScripts\Thetis_Backups.
  All three programs run as "Thetis.exe" (also in Task Manager); the bridge
  tells them apart by their folders:
     Thetis      C:\Program Files\OpenHPSDR\Thetis
     Thetis HL2  C:\Program Files\OpenHPSDR\Thetis-HL2
     SDR-VST3    C:\Program Files\OpenHPSDR\SDR-VST3
  Close them with File > Exit, not Task Manager: a force-closed program may
  not save, and can leave the radio or the sound device stuck until the radio
  is switched off and on (or the PC restarted).

EASY WAY (recommended)  - about 5 minutes
  1. Unzip KPA1500_Bridge_Complete.zip (right-click > Extract All...).
  2. Open the KPA1500_Bridge folder and double-click 1_RUN_THIS_Installer.bat
     If Windows SmartScreen says "Windows protected your PC":
        click "More info", then "Run anyway".
  3. Answer the questions. Just press Enter to accept the answer in [brackets].
       Step 1  Python             found, or offered to install
       Step 2  Folder             C:\RadioScripts (recommended). The bridge gets its
                                  own subfolder there: C:\RadioScripts\KPA1500_Thetis_Bridge
                                  (your other programs in C:\RadioScripts are never touched)
       Step 3  Amplifier          type its IP address, or let it search
       Step 4  Thetis             same computer? use the Thetis windows? (Yes)
                                  The start file can also launch Thetis, Thetis HL2
                                  or SDR-VST3 for you - pick one
       Step 5  LP-100A            only if you have one
       Step 6  Browser panel      allow phones/other PCs? (normally No). A password is
                                  optional (Enter = none); this PC never needs it and it can
                                  be removed any time in the panel: Settings > Panel access
       Step 7  Shortcuts          Desktop shortcut, auto-start, self-test
       Step 8  Thetis windows     If more than one version is installed, choose
                                  which ones get the windows (e.g. 1,3 or A = all).
                                  A running one is closed for you the normal way
                                  (it asks first). Each program's settings are
                                  backed up before anything is changed.
  4. Start the bridge (Desktop shortcut "KPA1500 Bridge"), then start Thetis.
     The KPA1500 windows appear. Drag and resize them where you like;
     Thetis remembers their place.

THE THETIS WINDOWS - what the setup does (no programming needed)
  Thetis keeps its meter windows ("containers") in its settings file:
     Thetis      %APPDATA%\OpenHPSDR\Thetis-x64\DB\...\database.xml
     Thetis HL2  the same DB folder, a separate database (HL2_dbman_settings.json)
     SDR-VST3    %APPDATA%\OpenHPSDR\SDR-VST3-x64\DB\...\database.xml
                 (SDR-VST3 stores the windows in a newer format; the setup
                  writes that format for it automatically)
  Setup_Thetis_Containers.bat (also run by installer Step 8):
    - asks which program when more than one is installed
    - refuses to run while that program is open (it would overwrite the change)
    - saves a backup copy in <install folder>\thetis_backups
      (Thetis HL2 and SDR-VST3 in its subfolders Thetis-HL2 and SDR-VST3)
    - adds or fixes two Multi Meter I/O entries in Thetis:
         readings IN  : UDP listener 127.0.0.1 port 50100, format XML
         buttons  OUT : sends to 127.0.0.1 port 50101, format Raw
      (Thetis: Setup > Serial/Network/Midi CAT > Multi Meter I/O)
    - adds the KPA1500 Telemetry, Controls and (optional) Knob windows (if they
      are already there they are replaced, keeping their position)
  Nothing in the Thetis program folder is changed, so Thetis updates do
  not undo it. If a Thetis update ever resets its settings, just double-click
  Setup_Thetis_Containers.bat again.

  How the pieces talk:
     KPA1500  <-- network UDP 1500 -->  BRIDGE  <-- TCI 50001 -->  Thetis (band, TX)
                                        BRIDGE  -- UDP 50100 -->   Thetis windows (readings)
                                        BRIDGE  <-- UDP 50101 --   Thetis buttons (commands)
  The bridge must be running for the windows to work. If it is not, the
  windows show a red line "!! BRIDGE NOT RUNNING - data frozen !!" and the
  buttons go dark.

  To undo:   close Thetis, double-click Undo_Thetis_Containers.bat
             (asks which program, then puts back its settings saved just
             before the last setup)
  To redo:   close Thetis, double-click Setup_Thetis_Containers.bat

  Safety options (browser panel > Settings):
    "Allow Thetis to power off the amp"      - the AMP button may switch it off
    "Block power off while transmitting"     - recommended ON

IF ANTIVIRUS BLOCKS THE INSTALLER (manual way)
  1. Install Python from https://www.python.org/downloads/
     (tick "Add python.exe to PATH" on the first screen).
  2. Copy everything in the "manual" folder to C:\RadioScripts\KPA1500_Thetis_Bridge
     LP-100A users: open a Command Prompt and type   py -m pip install pyserial
  3. Open start_kpa_bridge.bat in Notepad if you want to change options
     (amplifier address, ports, LP-100A ...). Double-click it to start.
  4. Close Thetis, double-click Setup_Thetis_Containers.bat for the windows.

UPDATING TO A NEW VERSION
  Run the new 1_RUN_THIS_Installer.bat, choose the same folder and answer Y to
  "Move them now?" if you are asked (an older install directly in the folder is
  moved into its own KPA1500_Thetis_Bridge subfolder; shortcuts are updated) and to
  "Keep your current start options". If the bridge is running, the installer
  offers to close it (make sure the radio is not transmitting or tuning), and
  at the end offers to start the new version. Your settings are kept.

AMPLIFIER ON ANOTHER SUBNET
  Control works if your router passes UDP port 1500 between the networks.
  "Search my network" looks at one subnet at a time (type e.g. 10.1.2.0/24).
  Remote power-on (Wake-on-LAN) needs the router to forward a broadcast to
  the amp's subnet; give its address in the installer, or add
  --wol-broadcast 10.1.2.255 to BRIDGE_FLAGS in start_kpa_bridge.bat.

THETIS ON ANOTHER COMPUTER
  Run the setup (Setup_Thetis_Containers.bat + kpa1500_thetis_setup.py) on
  the Thetis computer, then in Thetis Multi Meter I/O point the buttons
  entry at the bridge computer, port 50101. Details in the help file.

TO REMOVE
  Run Uninstall_KPA1500_Bridge.bat in the KPA1500_Thetis_Bridge folder. It asks
  whether to remove the Thetis windows too, and deletes ONLY the bridge's own
  files (never other programs). Thetis backups are kept.

IF THE AMPLIFIER IS NOT FOUND
  Make sure it is switched on (not asleep), then double-click
  KPA_Network_Check.bat in the bridge folder. It changes nothing and writes
  kpa_network_check.txt with this PC's networks and whether the amp answers.

HELP
  KPA1500_Bridge_Help.html (in this folder and in the install folder), or
  press W in the bridge window. Troubleshooting is near the end.
