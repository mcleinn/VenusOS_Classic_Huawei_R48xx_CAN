// Per-device configuration, selected by the build flag DISPLAY_ID (1 or 2), see platformio.ini.
// Each display shows "its" charger, identified by the CustomName the Venus driver publishes.
// Physical assignment fixed 2026-11-19: display 1 (board ..4A:0C) sits on the replacement "Huawei 1",
// display 2 (board ..4A:D8) on "Huawei 2"; the names were the other way round before.
// (Settings -> Device name on the Cerbo). The battery, loads and the total-current ring are the
// same on both displays.
#pragma once

#ifndef DISPLAY_ID
#define DISPLAY_ID 1
#endif

#if DISPLAY_ID == 1
#define OWN_CHARGER_NAME   "Huawei 1"          // board MAC ..4A:0C, mounted on the replacement unit 1 (2026-11-18)
#define OTA_HOSTNAME       "charger-display-1"
#elif DISPLAY_ID == 2
#define OWN_CHARGER_NAME   "Huawei 2"          // board MAC ..4A:D8, mounted on unit 2 (R4875G1 E4308598A003)
#define OTA_HOSTNAME       "charger-display-2"
#else
#error "DISPLAY_ID must be 1 or 2"
#endif
