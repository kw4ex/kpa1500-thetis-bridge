# KPA1500 Thetis Bridge — v2.31

A Windows program that connects an **Elecraft KPA1500** amplifier (and,
optionally, a **FlexRadio FlexControl** tuning knob and a **Telepost LP-100A**
wattmeter) to **Thetis**, **Thetis HL2** (Hermes Lite 2) or **SDR-VST3**.
Written for operators, not programmers. By KW4EX.

**What it does:** the amp follows the radio's frequency; safe TUNE (amp to
STANDBY *before* the radio keys); Standby-on-Tune; a "No antenna?" warning;
meters and lit buttons in windows *inside* Thetis; a browser control panel;
FlexControl knob support (VFO, AF volume, AGC gain, mute, VOX… every press
programmable — works without the amplifier too). One-page summary:
[docs/KPA1500 Thetis Bridge - Overview.pdf](docs/KPA1500%20Thetis%20Bridge%20-%20Overview.pdf).

## Download

**Start at the [Releases page](https://github.com/kw4ex/kpa1500-thetis-bridge/releases/latest).**
The top entry marked **Latest** is the newest version; older versions stay listed
below it. Click **KPA1500_Bridge_Complete_v2.31.zip** in the **Assets** list (not
"Source code"), then in your Downloads folder right-click it, **Extract All…**,
**Extract**. Step-by-step download instructions are on the release page.

Using NereusSDR, or want the Applet window? Use the newer
[KPA1500 SDR Bridge](https://github.com/kw4ex/kpa1500-sdr-bridge/releases/latest); it replaces this program.

## Install (about 5 minutes)

1. In Thetis: **Setup › Serial/Network/Midi CAT › Network**, TCI Server
   **127.0.0.1:50001**, tick **TCI Server Running**, **Apply**, **OK**, then
   close Thetis normally (File › Exit).
2. Open the extracted **KPA1500_Bridge** folder and double-click
   **1_RUN_THIS_Installer.bat**. If Windows says "Windows protected your PC",
   click **More info** › **Run anyway**.
3. Answer the questions — press Enter to accept the suggested answer.
4. Start the bridge (Desktop shortcut **KPA1500 Bridge**), then Thetis.

Full instructions: [KPA1500_Bridge/README_Install.txt](KPA1500_Bridge/README_Install.txt).
Full help with pictures: download
[KPA1500_Bridge_Help.html](KPA1500_Bridge/KPA1500_Bridge_Help.html) and open it
in your browser (it's also in the zip).

## Needs

Windows 10 or 11 · Thetis 2.10.3.12+, Thetis HL2 2.10.3.15+ or SDR-VST3 5.x with
TCI on · the KPA1500 on your network · Python 3.8+ (the installer offers to
install it).

**Only one program should control the amplifier.** Don't run this bridge at
the same time as the KPA1500 feature for Zeus
([kw4ex/kpa1500-for-zeus](https://github.com/kw4ex/kpa1500-for-zeus)).

## Updating

Download the new zip and run its installer; choose **Keep your current
options**.

"Elecraft", "KPA1500", "FlexRadio", "FlexControl"
and "LP-100A" are names of their owners; this is an independent, community-made
program.

## No warranty — use at your own risk

This program is shared by one radio operator, **as is**. There is **no
warranty of any kind**: no promise that it works, that it is safe, or that it
suits your equipment or your purpose. Amplifiers, radios and antennas can be
damaged, and RF can be dangerous. You are responsible for checking that it
behaves correctly with your own station, and for operating within your
license. The author is not liable for any damage or loss. It is not made,
approved or supported by Elecraft, FlexRadio, Telepost, Thetis or Zeus.
Licensed under the [MIT License](LICENSE).
