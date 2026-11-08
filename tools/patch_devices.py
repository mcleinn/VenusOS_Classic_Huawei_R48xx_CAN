"""One-off patch (2026-11-08): fix OTA declaration order; per-device build (DISPLAY_ID) selecting
the charger to show by its Venus CustomName and the OTA hostname."""
from pathlib import Path

root = Path(__file__).resolve().parent.parent

# --- devices.h
(root / "include/devices.h").write_text('''// Per-device configuration, selected by the build flag DISPLAY_ID (1 or 2), see platformio.ini.
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
''', encoding="utf-8")

# --- secrets.h: OTA_HOSTNAME now comes from devices.h
for name in ("include/secrets.h", "include/secrets.h.example"):
    p = root / name; t = p.read_text(encoding="utf-8")
    lines = [l for l in t.splitlines() if not l.startswith("#define OTA_HOSTNAME")]
    t = "\n".join(lines) + "\n"
    t = t.replace("// Over-the-air update (ArduinoOTA): mDNS hostname and password. Same password in secrets.ini.",
                  "// Over-the-air update password (hostname comes from include/devices.h). Same password in secrets.ini.")
    p.write_text(t, encoding="utf-8")

# --- net.cpp
p = root / "src/net.cpp"; t = p.read_text(encoding="utf-8")
def rep(a, b):
    global t
    assert a in t, a[:60]
    t = t.replace(a, b, 1)
# declaration order: the ArduinoOTA section uses these before the HTTP section defines them
rep('''static bool s_ota_started = false;
static lv_obj_t *s_ota_label = nullptr;
bool g_ota_active = false;

// --- HTTP firmware upload (served by the board)''', "// --- HTTP firmware upload (served by the board)")
rep("// --- over-the-air update -------------------------------------------------------------------",
    '''static bool s_ota_started = false;
static lv_obj_t *s_ota_label = nullptr;
bool g_ota_active = false;

// --- over-the-air update -------------------------------------------------------------------''')
rep('#include "secrets.h"\n', '#include "secrets.h"\n#include "devices.h"\n')
# own charger: bind by CustomName, never by "first seen"
rep('''        if (m.charger_instance < 0) {
            m.charger_instance = inst;
            Serial.printf("mqtt: charger instance %d\\n", inst);
        }
''', '''        // this display's own charger is the service whose CustomName matches OWN_CHARGER_NAME
        if (!strcmp(p, "CustomName")) {
            const char *nm = val.as<const char *>();
            if (nm && !strcmp(nm, OWN_CHARGER_NAME) && m.charger_instance != inst) {
                m.charger_instance = inst;
                m.charger_seen = false;                       // values below refill from this service
                m.chg_connected = m.chg_state = m.chg_mode = Sample();
                m.chg_current = m.chg_voltage = m.chg_power = m.chg_temp = Sample();
                m.link_limit = m.ac_power = m.ac_voltage = m.ac_current = m.ac_freq = Sample();
                m.hw_sent_current = m.hw_ack_ok = m.hw_fault = Sample();
                Serial.printf("mqtt: own charger '%s' is instance %d\\n", OWN_CHARGER_NAME, inst);
            }
        }
''')
p.write_text(p.read_text(encoding="utf-8") if False else t, encoding="utf-8")

# --- model: default name shows what we are waiting for
p = root / "src/model.h"; t = p.read_text(encoding="utf-8")
assert 'char charger_name[24] = "Charger";' in t
t = t.replace('char charger_name[24] = "Charger";', 'char charger_name[24] = OWN_CHARGER_NAME;')
t = t.replace('#include <Arduino.h>\n', '#include <Arduino.h>\n#include "devices.h"\n', 1)
p.write_text(t, encoding="utf-8")

# --- platformio.ini: per-device envs
p = root / "platformio.ini"; t = p.read_text(encoding="utf-8")
if "[env:d1]" not in t:
    t = t.replace("default_envs = amoled164\n", "default_envs = d1\n")
    t += '''
; ---- per device: d1 = display at charger "Huawei 2" (unit 1 board, COM11), d2 = display at "Huawei 1"
[env:d1]
extends = env:amoled164
build_flags = ${env:amoled164.build_flags} -DDISPLAY_ID=1
upload_port = COM11

[env:d2]
extends = env:amoled164
build_flags = ${env:amoled164.build_flags} -DDISPLAY_ID=2
upload_port = ${ota.port2}

[env:d1_http]
extends = env:d1
upload_protocol = custom
upload_command = curl -s -S --max-time 120 -u ota:${ota.password} -F "firmware=@$SOURCE" http://${ota.host}/update

[env:d2_http]
extends = env:d2
upload_protocol = custom
upload_command = curl -s -S --max-time 120 -u ota:${ota.password} -F "firmware=@$SOURCE" http://${ota.host2}/update
'''
    p.write_text(t, encoding="utf-8")
# secrets.ini: host2 / port2 placeholders
p = root / "secrets.ini"; t = p.read_text(encoding="utf-8")
if "host2" not in t:
    t += "host2 = charger-display-2.local\nport2 = COM12\n"
    p.write_text(t, encoding="utf-8")
p = root / "secrets.ini.example"; t = p.read_text(encoding="utf-8")
if "host2" not in t:
    t += "host2 = charger-display-2.local\nport2 = COM12\n"
    p.write_text(t, encoding="utf-8")
print("devices patched")
