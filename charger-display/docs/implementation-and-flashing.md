# charger-display: implementation and flash process (state of 2026-11-08)

Companion to `board-notes.md` (hardware facts from the schematic and the shipped firmware) and to
the user-level skill `waveshare-esp32s3-amoled` (general lessons). This file is the concrete
"how it is built and how it gets onto the board" for this project.

## Units

| | MAC (USB serial number) | role |
|---|---|---|
| board 1 | `28:84:85:8d:4a:0c` | display build **d1**, mounted on the replacement charger "Huawei 1" (since 2026-11-18); usually COM11 |
| board 2 | `28:84:85:8d:4a:d8` | display build **d2**, mounted on charger "Huawei 2" (R4875G1); golden flash image of this board kept in `_local/unit2-golden` |

Golden image of unit 2 (shipped FactoryProgram, 2026-06-02, IDF 5.5.2):
`_local/unit2-golden/unit2-flash-16MB.bin`, eFuse summary beside it. Git-ignored, keep it.

The M5Stack Atom Lite that may also hang on this notebook belongs to another project and another
agent. It shows up as an FTDI "USB Serial Converter", never as VID 303A. Do not touch it.

## Toolchain

- PlatformIO core 6.1.19 at `%USERPROFILE%\.platformio\penv\Scripts\pio.exe` (not on PATH).
- Platform: pioarduino 54.03.21 (Arduino-ESP32 3.2.1, ESP-IDF 5.4.1), pinned by URL in
  `platformio.ini`. 55.x needs PlatformIO core >= 6.2.
- `click<8.2` pinned inside the penv (pioarduino's esptool 5.0.0-dev1 breaks with 8.2+).
- System Python 3.12 has `esptool` 5.6 and `pyserial` (used for golden-image work and
  `tools/serial_capture.py`), plus `pypdf` (schematic text extraction).
- Libraries (lib_deps): lvgl 8.4.0, PubSubClient 2.8, ArduinoJson 7.4.2. Vendored:
  `lib/esp_lcd_sh8601` (Espressif, Apache-2.0, from the Waveshare demo, reviewed).

## Source map

```
include/board_pins.h   pins (panel CS = 46!, CAN TX 17 / RX 18), resolution, x offset 0x14,
                       LCD_PCLK_HZ 40 MHz, LCD_ROTATION 90 (numeric; 270 = turned by 180 degrees),
                       UI_W/UI_H 456x280, UI_FW_VERSION
include/lv_conf.h      LVGL 8.4: 16 bpp, swap on, LV_MEM_CUSTOM 1, tick from esp_timer,
                       Montserrat 12/14/16/20/24/32/40/48, demos off
include/secrets.h      WLAN SSID/password, MQTT_HOST, MQTT_PORT, VRM_PORTAL_ID (git-ignored;
                       template in secrets.h.example)
src/display_hw.c       esp_lcd bring-up in C: SPI2 QSPI bus, panel IO (cs 46, 32-bit cmd,
                       quad_mode), SH8601 panel with the demo init table, reset, display on,
                       brightness via 0x51; display_hw_selftest() (fills + register reads),
                       on_color_trans_done -> lv_disp_flush_ready (guarded)
src/display.cpp        LVGL display driver: logical 456x280, 2 draw buffers of 40 rows + 1
                       rotation buffer (all internal DMA RAM), rounder (even/odd), flush_cb does
                       the 90/270 transpose and sends physical windows with +0x14 x offset;
                       touch indev with the same rotation; flush counters for the status line
src/touch.cpp          FT3168 over Wire (SDA 47, SCL 48, 400 kHz), write/stop/read, NACK = no touch
src/model.h            Model: link states (wifi/mqtt/can), Venus system/vebus/battery values,
                       charger service values, CAN-decoded values, venus_fresh()
src/net.cpp            WiFi STA: one plain begin(), SDK auto-reconnect, restart only after 60 s and never
                       while associated; roaming (scan for the strongest node when RSSI < -70 dBm, by BSSID)
                       only once connected. Never scan or begin() again while a join is in progress
                       (2026-11-18: that broke joining completely). PubSubClient to
                       the Cerbo, ~50 subscriptions (system, vebus, battery, charger), keepalive
                       every 30 s, JSON parsing into the model
src/can.cpp            TWAI listen-only on the Huawei bus (125 kbit/s): decodes status registers
                       (0x70-0x88), set commands (0x00 V, 0x03 current ratio, 0x32 standby),
                       acks and unsolicited load ratio; CAN badge = frames within 5 s
src/ui.cpp             status bar + tile view with 6 pages + page dots + screensaver:
                         0 Charger (arc, charger card, loads card)  1 Overview (boxes + flows)
                         2 Battery (BMS details)  3 Charger details  4 Status  5 CAN bus
                       charger values prefer CAN when fresh (< 3 s), else Cerbo; gestures: swipe
                       towards the left edge on page 0 -> screensaver, onwards from the last
                       page -> page 0; screensaver after 120 s idle, touch/BOOT returns
src/main.cpp           setup: display, ui, brightness 200, net, can; loop: net, can, status line
                       every 10 s, lv_timer_handler
tools/serial_capture.py  dump COMx for N seconds, ANSI stripped, credential-looking lines masked
tools/patch_saver.py   one-off patch script (historical)
_local/ws-demo-06      Waveshare's 06_LVGL_Test built unchanged with this toolchain (reference)
_local/unit2-golden    full flash dump + eFuse summary of the untouched unit 2 (restore image)
```

## Venus MQTT specifics used

**Venus host since 2026-11-19:** a Raspberry Pi 5 with a newer Venus OS at the Cerbo's old address (the
Cerbo GX broke down). Its portal id is `2ccf676684b1` (the Cerbo's was `48e7da89187b`); `VRM_PORTAL_ID` in
`include/secrets.h` must match the host, otherwise the displays subscribe to topics nobody publishes and stay
at "waiting for data". The ported driver on the Pi publishes the same charger paths (checked 2026-11-19).

- Topics `N/<portal id>/<service>/<instance>/<path>`, payload `{"value": x}`; `null` = invalid.
- The Cerbo publishes only while `R/<portal id>/keepalive` arrives (every 30 s here; every
  keepalive republishes all values, which is also the initial snapshot after subscribing).
- Charger service paths come from the Huawei driver (`/Dc/0/Current`, `/Dc/0/Voltage`,
  `/Dc/0/Power`, `/Dc/0/Temperature`, `/Link/ChargeCurrent`, `/Huawei/State`,
  `/Huawei/LimitingReason`, `/CustomName`, `/State`, `/Connected`, `/Ac/In/L1/P`, `/Ac/In/L1/V`).
- System paths: `system/0/Dc/Battery/{Soc,Power,Voltage,Current,TimeToGo}`,
  `Ac/Consumption/L1/Power`, `Ac/Grid/L1/Power`, `Ac/Genset/L1/Power`, `Dc/Charger/Power`,
  `SystemState/State`, `Ac/ActiveIn/Source`.

## Build, flash, watch

From `C:\Dev-Free\charger-display` in Git Bash (PowerShell: set the env var with `$env:`):

```
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1
"$USERPROFILE/.platformio/penv/Scripts/pio.exe" run                 # build only
"$USERPROFILE/.platformio/penv/Scripts/pio.exe" run -t upload       # build + flash COM11
python tools/serial_capture.py COM11 30                             # boot log, status lines
```

- Upload fails with "A device which does not exist was specified" when the board is in a crash
  loop or re-enumerating: retry a few times, or hold BOOT, press and release RST, release BOOT.
- A status line like
  `status flush=526/526 err=0 wifi=2 mqtt=2 msgs=31 fresh=1 | soc=97% ... | chg[293 Huawei 2] Ready ...`
  every 10 s means panel path, WLAN, MQTT and data are all alive (flush counters: started/done).
- Optional boot self-test: add `-DDISPLAY_SELFTEST` to `build_flags`; the panel then shows white,
  red, green, blue for ~2 s each with a brightness sweep before the GUI starts.


## Two displays, one firmware

`platformio.ini` has one env per device: `d1` (DISPLAY_ID 1, board ..4A:0C, shows "Huawei 1", OTA hostname
charger-display-1, USB COM11) and `d2` (DISPLAY_ID 2, board ..4A:D8, shows "Huawei 2", charger-display-2, USB
port from secrets.ini). Names swapped 2026-11-19 to match where the boards are mounted. `include/devices.h` maps the id to the charger name and hostname. The
own charger is bound by the CustomName the Venus driver publishes, never by "first service seen";
until that name appears the charger card says "not found". Battery, loads and the total ring are
identical on both displays.

## Network since 2026-11-19: the Pi's own access point

The displays no longer use the house WLAN. The Venus Pi (wired, 192.168.1.14) runs the 2.4 GHz access point
`charger-net` (connman tethering, gateway/bridge `tether` 192.168.0.1/24, DHCP, NAT to eth0, channel 1) a metre
from the chargers. `include/secrets.h`: `WIFI_SSID charger-net`, `MQTT_HOST 192.168.0.1`, `VRM_PORTAL_ID 2ccf676684b1`.
Because the displays are NATed, the notebook cannot reach them directly: use `tools/push_via_pi.sh d1|d2`, which
builds, copies the image to the Pi and runs the curl upload there (board found by MAC via `ip neigh show dev tether`),
then prints `/status`. Full write-up: `C:\Dev-Free\CerboGX
aspbi\charger-net-ap-and-displays.md`.

## Network since 2026-11-22: two WLANs, the stronger wins

- The Pi's access point is now called **`OrbiPV`** (same passphrase as `charger-net` before; connman tethering,
  bridge `tether` 192.168.0.1/24, NAT to eth0). It did not come back by itself after the Pi reboot of 2026-11-21
  (wlan0 rejoined the Orbi as a client), so `/data/ap/orbipv.sh` (started from `/data/rc.local`, 60 s delay)
  re-enables it and enslaves wlan0 to the bridge when connman forgets to.
- `include/secrets.h` knows **two networks**: `WIFI_SSID OrbiPV` / `WIFI_SSID2 Orbi` with their passwords.
  `wifi_start()` does one blocking scan (about 1.5 s, only while nothing is connecting) and joins the strongest node
  of either network by BSSID and channel; `roam_check()` considers both networks too. The status page and `/status`
  show the SSID in use. `MQTT_HOST 192.168.1.14` (the Pi's LAN address) works from both: on OrbiPV the Pi is the
  gateway and delivers to itself.
- A display on `Orbi` is reachable from the notebook directly (`d1_http`/`d2_http` envs or curl); on `OrbiPV` it sits
  behind the Pi's NAT, use `tools/push_via_pi.sh`.
- **Display 2 without CAN:** its transceiver was removed when the WLAN antenna went in (2026-11-22). Build `d2` has
  `-DCAN_ENABLED=0` (`include/board_pins.h` default 1): no TWAI driver, no CAN badge, no CAN page (5 pages), status
  page "CAN BUS: not fitted", `/status` `can.enabled 0`. Display 1 (`d1`) keeps the CAN listener.
- Display 2 ran the 2026-11-08 build until 2026-11-22 (Orbi credentials, no `/update`); it was updated from the Pi
  with the espota protocol: `tools/push_espota_via_pi.sh d2 <ip>` (espota.py and the image are copied to the Pi, which
  is on the same LAN and has no inbound firewall rule). Result: build 23:03, joined OrbiPV at -60 dBm (antenna fitted),
  MQTT up, "Huawei 2".

## Updates over WLAN

Two mechanisms are built in; use the HTTP one on this PC:

- **HTTP (works here):** the board serves `http://<ip>/` (basic auth user `ota`, password from
  secrets) with an upload form, and `POST /update`. PlatformIO envs `d1_http` / `d2_http` push the
  build with curl: `pio run -e d1_http -t upload`. The PC connects *out* to the board, so no
  firewall exception is needed. Verified 2026-11-08 on unit 1 (104 s incl. build, "OK, rebooting").
- **espota (ArduinoOTA, `amoled164_ota`):** authenticates, then the board must connect back to
  the PC. On this PC the WLAN is a *Public* profile with an inbound block rule for python.exe, so
  it stops at "No response from device". Optional admin fix (PowerShell, elevated):
  `Set-NetConnectionProfile -Name "<WLAN name>" -NetworkCategory Private` or
  `New-NetFirewallRule -DisplayName "PlatformIO espota" -Direction Inbound -Program "$env:USERPROFILE\.platformio\penv\Scripts\python.exe" -Action Allow -Profile Public`.
  Also `.local` names did not resolve on this PC; `secrets.ini` holds the IPs instead.
- The display shows "Firmware update... N KB" while an image is written and reboots into the new
  slot; a failed upload leaves the old slot running (two app slots in default_16MB.csv).

## Recovery when the screen stays black

1. Check the status line: `flush` counting and `err=0` means the panel path is being driven.
2. Confirm `PIN_LCD_CS` is 46 (the demo's 9 leaves this panel revision black).
3. Restore the golden image and power-cycle:
   ```
   python -m esptool --port COM11 --chip esp32s3 --baud 921600 write-flash 0x0 _local/unit2-golden/unit2-flash-16MB.bin
   ```
   then unplug and replug USB. The factory colour test and clock must appear. Only then flash
   the project again.
4. A board that was driven with the wrong CS needs that USB power cycle even after a correct
   firmware is flashed.

## Open items

- CAN: code is in place (listen-only, TJA1051T/3 on GPIO17/18). The **decoder is verified** on the
  device by serial replay of real bus recordings against the Venus driver's reference decoder
  (`python tools/can_replay.py COM11 <capture.jsonl>`; 2026-11-08: 3341 + 11400 real frames from the
  October bench captures and `tools/synthetic-frames.jsonl` with known raw values, all fields match:
  out A/V/W, AC V/A/W/Hz, temperatures, limit and load ratio, rated current, set V and ratio, acks,
  output-on / AC-present bits). Still unverified until the transceiver is wired: the TWAI driver on
  the real bus (bit timing 125 kbit/s, listen-only acks), badge turning green, two-node bus load.
- The firmware accepts `can <id hex> <data hex>` and `dump` on the USB serial port for such tests.
- Touch works for swipes and taps; FT3168 NACK once per ~14 s while idle is silenced.
- `docs/board-notes.md` still lists GPIO46 as IMU INT1 per the schematic; on the shipped hardware
  INT1 is presumably GPIO9 (unverified, the IMU is not used).

## CAN side, requirements (stated 2026-11-08, not built yet)

- Transceiver TJA1051T/3 on the ESP32-S3 TWAI controller, header P1: TXD <- GPIO17, RXD -> GPIO18,
  S (silent, high = transmitter off) <- GPIO16 driven high at boot; VCC 5 V, VIO 3V3, GND.
- The display hangs on the **same CAN bus the charger module's control board uses**, as a second
  node that only listens: it is a CAN slave, never a master, it does not send commands or acks.
- It decodes both directions: the control board's commands/setpoints to the charger and the
  charger's answers (status registers, acks). Protocol and register tables: the Huawei driver in
  `C:\Dev-Free\CerboGX_R4850G2_CAN\src\driver\huawei\protocol.py`.
- The "CAN" badge in the status bar turns green when frames are being received.
