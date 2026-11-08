# charger-display

Status display for the Huawei R48xx chargers on the Victron Cerbo GX ESS, running on a
Waveshare ESP32-S3-Touch-AMOLED-1.64 (ESP32-S3R8, 16 MB flash, 8 MB PSRAM, 280 x 456 AMOLED
with CO5300 controller over QSPI, FT3168 touch, QMI8658 IMU, TF slot, Li-ion header).

Data sources:

- Cerbo GX over MQTT (Venus `dbus-mqtt`, plain MQTT on port 1883 inside the house network).
- Huawei R48xx over CAN, once the CAN transceiver board is attached (not yet).

Style guide: Victron GUI v2 (`ref/gui-v2`, shallow clone of github.com/victronenergy/gui-v2,
git-ignored). Only the colours, proportions and wording are borrowed; the UI is LVGL 8.4.

Detailed notes: `docs/implementation-and-flashing.md` (source map, flash process, recovery),
`docs/board-notes.md` (hardware facts). General lessons live in the user skill `waveshare-esp32s3-amoled`.

## Build

PlatformIO with the pioarduino platform (Arduino-ESP32 core 3.x on ESP-IDF 5.x, needed for
the `esp_lcd` QSPI panel API). The PlatformIO core installed with VS Code is used:

```
%USERPROFILE%\.platformio\penv\Scripts\pio.exe run
%USERPROFILE%\.platformio\penv\Scripts\pio.exe run -t upload
%USERPROFILE%\.platformio\penv\Scripts\pio.exe device monitor
```

The board enumerates as Espressif USB-Serial/JTAG (VID 303A, PID 1001); `platformio.ini`
pins the port to COM11. **Panel CS is GPIO46** on boards shipped with the 2026 factory firmware;
the Waveshare demo's GPIO9 leaves the screen black. If upload fails to enter the bootloader: hold BOOT, press and
release RST, release BOOT.

Environment notes (2026-11-08, PlatformIO core 6.1.19):

- pioarduino is pinned to release 54.03.21; the "stable" 55.x releases need core >= 6.2.
- pioarduino's esptool 5.0.0-dev1 breaks with click >= 8.2 (`get_metavar() missing ctx`);
  `click<8.2` was pinned inside `%USERPROFILE%\.platformio\penv`.
- The same esptool prints Unicode; when its output is redirected to a file set
  `PYTHONIOENCODING=utf-8` (otherwise PlatformIO dies with a cp1252 UnicodeEncodeError
  right after "Uploading stub flasher").
- `tools/serial_capture.py COM11 45` dumps the boot log for 45 s with credential-looking
  lines masked. The firmware prints a `status ...` line every 10 s.

## Credentials

`include/secrets.h` (git-ignored) holds the WLAN SSID and password, the Cerbo host and the
VRM portal id. Start from `include/secrets.h.example`.

## Layout

```
src/main.cpp        setup/loop: display, UI, network
src/display_hw.c    esp_lcd bring-up of the CO5300/SH8601 panel over QSPI (C)
src/display.cpp     LVGL display driver, draw buffers, touch input device
src/touch.cpp       FT3168 over Wire
src/net.cpp         WLAN + MQTT (PubSubClient), Venus keepalive, topic parsing into the model
src/model.h         data model shared between network and UI
src/ui.cpp          the screen (status icons, battery arc, charger card, system card)
include/board_pins.h  pin map from the Waveshare demo
include/lv_conf.h   LVGL config (16-bit colour, byte swap on, Montserrat 12..48)
lib/esp_lcd_sh8601  Espressif's SH8601 panel driver (Apache-2.0), as shipped in the demo
```

## Venus MQTT notes

- Topics: `N/<portal id>/<service>/<instance>/<path>`, payload `{"value": x}`.
- The broker publishes only while `R/<portal id>/keepalive` arrives at least every 60 s;
  each keepalive republishes every value (our initial snapshot). We send one every 30 s.
- Charger service: `charger/<instance>/...` with the driver's paths (`Dc/0/Current`,
  `Link/ChargeCurrent`, `Huawei/State`, `Huawei/LimitingReason`, ...). The display locks onto
  the first charger instance it sees.
