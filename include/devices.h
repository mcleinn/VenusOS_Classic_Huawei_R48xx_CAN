// Per-device configuration, selected by the build flag DISPLAY_ID (1 or 2), see platformio.ini.
// Each display shows "its" charger, identified by the CustomName the Venus driver publishes
// (Settings -> Device name on the Cerbo). The battery, loads and the total-current ring are the
// same on both displays.
#pragma once

#ifndef DISPLAY_ID
#define DISPLAY_ID 1
#endif

#if DISPLAY_ID == 1
#define OWN_CHARGER_NAME   "Huawei 2"          // the R4875G1 (unit 2), on the bus since 2026-11
#define OTA_HOSTNAME       "charger-display-1"
#elif DISPLAY_ID == 2
#define OWN_CHARGER_NAME   "Huawei 1"          // the R4875G5 (unit 1), back from the engineer later
#define OTA_HOSTNAME       "charger-display-2"
#else
#error "DISPLAY_ID must be 1 or 2"
#endif
