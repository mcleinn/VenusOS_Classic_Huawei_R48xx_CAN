"""One-off patch (2026-11-08): HTTP firmware upload served BY the board (PC connects outbound), as
an alternative to espota, which needs an inbound connection to the PC that the firewall blocks."""
from pathlib import Path

root = Path(__file__).resolve().parent.parent
p = root / "src/net.cpp"; t = p.read_text(encoding="utf-8")
def rep(a, b):
    global t
    assert a in t, a[:60]
    t = t.replace(a, b, 1)

rep("#include <ArduinoOTA.h>\n", "#include <ArduinoOTA.h>\n#include <WebServer.h>\n#include <Update.h>\n")
rep("static void mqtt_try_connect()\n{",
'''// --- HTTP firmware upload (served by the board) -----------------------------------------------
// POST /update with the firmware as multipart field "firmware", HTTP basic auth ota:<OTA_PASSWORD>.
// The PC connects OUT to the board, so no firewall exception is needed on the PC. Used by the
// PlatformIO env "amoled164_http" (curl). GET / shows a minimal upload form for a browser.
static WebServer s_http(80);
static bool s_http_started = false;

static void http_begin()
{
    if (s_http_started) return;
    s_http.on("/", HTTP_GET, [] {
        if (!s_http.authenticate("ota", OTA_PASSWORD)) return s_http.requestAuthentication();
        s_http.send(200, "text/html",
            "<html><body style='font-family:sans-serif'><h3>charger-display firmware update</h3>"
            "<form method='POST' action='/update' enctype='multipart/form-data'>"
            "<input type='file' name='firmware'> <input type='submit' value='Update'></form>"
            "<p>Running: " UI_FW_VERSION "</p></body></html>");
    });
    s_http.on("/update", HTTP_POST,
        [] {
            if (!s_http.authenticate("ota", OTA_PASSWORD)) return s_http.requestAuthentication();
            const bool ok = !Update.hasError();
            s_http.sendHeader("Connection", "close");
            s_http.send(ok ? 200 : 500, "text/plain", ok ? "OK, rebooting\\n" : "FAILED\\n");
            if (ok) { delay(300); ESP.restart(); }
        },
        [] {
            if (!s_http.authenticate("ota", OTA_PASSWORD)) return;
            HTTPUpload &up = s_http.upload();
            if (up.status == UPLOAD_FILE_START) {
                g_ota_active = true;
                Serial.printf("http-ota: start %s\\n", up.filename.c_str());
                if (!Update.begin(UPDATE_SIZE_UNKNOWN)) Update.printError(Serial);
                s_ota_label = lv_label_create(lv_layer_top());
                lv_obj_set_style_text_color(s_ota_label, lv_color_white(), 0);
                lv_obj_set_style_bg_color(s_ota_label, lv_color_black(), 0);
                lv_obj_set_style_bg_opa(s_ota_label, LV_OPA_80, 0);
                lv_obj_set_style_pad_all(s_ota_label, 12, 0);
                lv_label_set_text(s_ota_label, "Firmware update...");
                lv_obj_center(s_ota_label);
                lv_timer_handler();
            } else if (up.status == UPLOAD_FILE_WRITE) {
                if (Update.write(up.buf, up.currentSize) != up.currentSize) Update.printError(Serial);
                static uint32_t last = 0;
                if (millis() - last > 500 && s_ota_label) {
                    last = millis();
                    char buf[48];
                    snprintf(buf, sizeof buf, "Firmware update... %u KB", (unsigned)(up.totalSize / 1024));
                    lv_label_set_text(s_ota_label, buf);
                    lv_obj_center(s_ota_label);
                    lv_timer_handler();
                }
            } else if (up.status == UPLOAD_FILE_END) {
                if (Update.end(true)) Serial.printf("http-ota: done, %u bytes\\n", up.totalSize);
                else Update.printError(Serial);
                g_ota_active = false;
            } else if (up.status == UPLOAD_FILE_ABORTED) {
                Update.abort();
                g_ota_active = false;
                if (s_ota_label) { lv_obj_del(s_ota_label); s_ota_label = nullptr; }
                Serial.println("http-ota: aborted");
            }
        });
    s_http.begin();
    s_http_started = true;
    Serial.println("http-ota: listening on port 80");
}

static void mqtt_try_connect()
{''')
rep('''        ota_begin();
        ArduinoOTA.handle();
        if (g_ota_active) return;          // nothing else while an image is being written''',
'''        ota_begin();
        http_begin();
        ArduinoOTA.handle();
        s_http.handleClient();
        if (g_ota_active) return;          // nothing else while an image is being written''')
# the ota_begin section defines s_ota_label/g_ota_active after the http code needs them: move declarations up
rep('''static bool s_ota_started = false;
static lv_obj_t *s_ota_label = nullptr;
bool g_ota_active = false;
''', '')
rep("// --- HTTP firmware upload (served by the board)", '''static bool s_ota_started = false;
static lv_obj_t *s_ota_label = nullptr;
bool g_ota_active = false;

// --- HTTP firmware upload (served by the board)''')
t = t.replace('"OK, rebooting\\\\n"', '"OK, rebooting\\n"').replace('"FAILED\\\\n"', '"FAILED\\n"')
p.write_text(t, encoding="utf-8")

# board_pins already has UI_FW_VERSION; net.cpp needs the header
t = p.read_text(encoding="utf-8")
if '#include "board_pins.h"' not in t:
    t = t.replace('#include "secrets.h"\n', '#include "secrets.h"\n#include "board_pins.h"\n', 1)
    p.write_text(t, encoding="utf-8")

# platformio env with curl upload
p = root / "platformio.ini"; t = p.read_text(encoding="utf-8")
if "amoled164_http" not in t:
    t += '''
; Firmware upload over HTTP to the board's own update endpoint (PC connects outbound -> no firewall
; exception needed). Needs curl on the PATH (Windows 10+ ships it).
[env:amoled164_http]
extends = env:amoled164
upload_protocol = custom
upload_command = curl -s -S --max-time 120 -u ota:${ota.password} -F "firmware=@$SOURCE" http://${ota.host}/update
'''
    p.write_text(t, encoding="utf-8")
print("http ota added")
