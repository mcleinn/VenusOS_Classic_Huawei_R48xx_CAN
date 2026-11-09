#include "net.h"

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoOTA.h>
#include <WebServer.h>
#include <Update.h>
#include <lvgl.h>
#include <ArduinoJson.h>
#include "model.h"
#include "secrets.h"
#include "devices.h"
#include "board_pins.h"

// Venus OS MQTT (dbus-mqtt): values arrive on N/<portal>/<service>/<instance>/<path> as {"value": x}.
// The broker only publishes while it receives R/<portal>/keepalive at least every 60 s; every
// keepalive triggers a full republish of all values, which also serves as our initial snapshot.

static WiFiClient s_tcp;
static PubSubClient s_mqtt(s_tcp);

static uint32_t s_wifi_started = 0;
static uint32_t s_mqtt_next_try = 0;
static uint32_t s_last_keepalive = 0;
static char s_prefix[32];          // "N/<portal>/"
static size_t s_prefix_len = 0;

static constexpr uint32_t KEEPALIVE_MS = 30000;
static constexpr uint32_t MQTT_RETRY_MS = 5000;
// A failed WPA handshake (seen once right after a flash: 4WAY_HANDSHAKE_TIMEOUT / AUTH_FAIL)
// leaves the station idle; restart the join if nothing connected within this time.
static constexpr uint32_t WIFI_RESTART_MS = 12000;

// Topic suffixes after N/<portal>/. '+' = any charger instance; we lock onto the first one seen.
static const char *const SUBS[] = {
    "system/0/Dc/Battery/Soc",
    "system/0/Dc/Battery/Power",
    "system/0/Dc/Battery/Voltage",
    "system/0/Dc/Battery/Current",
    "system/0/Dc/Battery/TimeToGo",
    "system/0/Ac/Consumption/L1/Power",
    "system/0/Ac/Grid/L1/Power",
    "system/0/Ac/Genset/L1/Power",
    "system/0/Dc/Charger/Power",
    "system/0/SystemState/State",
    "system/0/Ac/ActiveIn/Source",
    "charger/+/Connected",
    "charger/+/State",
    "charger/+/Mode",
    "charger/+/CustomName",
    "charger/+/Dc/0/Current",
    "charger/+/Dc/0/Voltage",
    "charger/+/Dc/0/Power",
    "charger/+/Dc/0/Temperature",
    "charger/+/Link/ChargeCurrent",
    "charger/+/Ac/In/L1/P",
    "charger/+/Ac/In/L1/V",
    "charger/+/Settings/ChargeCurrentLimit",
    "charger/+/Huawei/State",
    "charger/+/Huawei/LimitingReason",
    "charger/+/Huawei/Model",
    "charger/+/Huawei/SentCurrent",
    "charger/+/Huawei/AckOk",
    "charger/+/Huawei/Fault",
    "charger/+/FirmwareVersion",
    "charger/+/Ac/In/L1/I",
    "charger/+/Ac/In/L1/F",
    "system/0/Dc/Battery/Temperature",
    "system/0/Ac/ActiveIn/L1/Power",
    "system/0/Dc/System/Power",
    "vebus/+/State",
    "vebus/+/Mode",
    "vebus/+/Ac/ActiveIn/L1/V",
    "vebus/+/Ac/ActiveIn/L1/F",
    "vebus/+/Ac/Out/L1/V",
    "vebus/+/Ac/Out/L1/F",
    "vebus/+/Ac/ActiveIn/CurrentLimit",
    "battery/+/Info/MaxChargeCurrent",
    "battery/+/Info/MaxChargeVoltage",
    "battery/+/Info/MaxDischargeCurrent",
    "battery/+/Soh",
    "battery/+/System/NrOfModulesOnline",
    "battery/+/System/NrOfModulesBlockingCharge",
    "battery/+/System/NrOfModulesBlockingDischarge",
    "battery/+/System/MinCellVoltage",
    "battery/+/System/MaxCellVoltage",
};

static void publish_keepalive()
{
    char topic[40];
    snprintf(topic, sizeof topic, "R/%s/keepalive", VRM_PORTAL_ID);
    s_mqtt.publish(topic, "");
    s_last_keepalive = millis();
}

static void copy_str(char *dst, size_t n, JsonVariantConst v)
{
    const char *s = v.as<const char *>();
    if (!s) s = "";
    strncpy(dst, s, n - 1);
    dst[n - 1] = 0;
}

static void on_message(char *topic, uint8_t *payload, unsigned int len)
{
    if (strncmp(topic, s_prefix, s_prefix_len) != 0) return;
    const char *rest = topic + s_prefix_len;

    JsonDocument doc;
    if (deserializeJson(doc, payload, len) != DeserializationError::Ok) return;
    JsonVariantConst val = doc["value"];
    if (val.isNull()) return;           // Venus publishes {"value": null} for invalid paths
    model.mqtt_last_rx = millis();
    model.mqtt_msgs++;

    Model &m = model;
    if (strncmp(rest, "system/0/", 9) == 0) {
        const char *p = rest + 9;
        float f = val.as<float>();
        if      (!strcmp(p, "Dc/Battery/Soc"))            m.soc.set(f);
        else if (!strcmp(p, "Dc/Battery/Power"))          m.batt_power.set(f);
        else if (!strcmp(p, "Dc/Battery/Voltage"))        m.batt_voltage.set(f);
        else if (!strcmp(p, "Dc/Battery/Current"))        m.batt_current.set(f);
        else if (!strcmp(p, "Dc/Battery/TimeToGo"))       m.batt_time_to_go.set(f);
        else if (!strcmp(p, "Ac/Consumption/L1/Power"))   m.consumption.set(f);
        else if (!strcmp(p, "Ac/Grid/L1/Power"))          m.grid_power.set(f);
        else if (!strcmp(p, "Ac/Genset/L1/Power"))        m.genset_power.set(f);
        else if (!strcmp(p, "Dc/Charger/Power"))          m.dc_charger_power.set(f);
        else if (!strcmp(p, "SystemState/State"))         m.system_state.set(f);
        else if (!strcmp(p, "Ac/ActiveIn/Source"))        m.active_in_source.set(f);
        else if (!strcmp(p, "Dc/Battery/Temperature"))    m.batt_temp.set(f);
        else if (!strcmp(p, "Ac/ActiveIn/L1/Power"))      m.ac_in_power.set(f);
        else if (!strcmp(p, "Dc/System/Power"))           m.dc_system_power.set(f);
        return;
    }
    if (strncmp(rest, "vebus/", 6) == 0) {
        const char *p = strchr(rest + 6, '/');
        if (!p) return;
        p++;
        float f = val.as<float>();
        if      (!strcmp(p, "State"))                    m.vebus_state.set(f);
        else if (!strcmp(p, "Mode"))                     m.vebus_mode.set(f);
        else if (!strcmp(p, "Ac/ActiveIn/L1/V"))         m.vebus_in_v.set(f);
        else if (!strcmp(p, "Ac/ActiveIn/L1/F"))         m.vebus_in_f.set(f);
        else if (!strcmp(p, "Ac/Out/L1/V"))              m.vebus_out_v.set(f);
        else if (!strcmp(p, "Ac/Out/L1/F"))              m.vebus_out_f.set(f);
        else if (!strcmp(p, "Ac/ActiveIn/CurrentLimit")) m.vebus_in_limit.set(f);
        return;
    }
    if (strncmp(rest, "battery/", 8) == 0) {
        const char *p = strchr(rest + 8, '/');
        if (!p) return;
        p++;
        float f = val.as<float>();
        if      (!strcmp(p, "Info/MaxChargeCurrent"))               m.bms_ccl.set(f);
        else if (!strcmp(p, "Info/MaxChargeVoltage"))               m.bms_cvl.set(f);
        else if (!strcmp(p, "Info/MaxDischargeCurrent"))            m.bms_dcl.set(f);
        else if (!strcmp(p, "Soh"))                                 m.bms_soh.set(f);
        else if (!strcmp(p, "System/NrOfModulesOnline"))            m.bms_modules.set(f);
        else if (!strcmp(p, "System/NrOfModulesBlockingCharge"))    m.bms_block_chg.set(f);
        else if (!strcmp(p, "System/NrOfModulesBlockingDischarge")) m.bms_block_dis.set(f);
        else if (!strcmp(p, "System/MinCellVoltage"))               m.bms_cell_min.set(f);
        else if (!strcmp(p, "System/MaxCellVoltage"))               m.bms_cell_max.set(f);
        return;
    }
    if (strncmp(rest, "charger/", 8) == 0) {
        int inst = atoi(rest + 8);
        const char *p = strchr(rest + 8, '/');
        if (!p) return;
        p++;
        // this display's own charger is the service whose CustomName matches OWN_CHARGER_NAME
        if (!strcmp(p, "CustomName")) {
            const char *nm = val.as<const char *>();
            if (nm && !strcmp(nm, OWN_CHARGER_NAME) && m.charger_instance != inst) {
                m.charger_instance = inst;
                m.charger_seen = false;                       // values below refill from this service
                m.chg_connected = m.chg_state = m.chg_mode = Sample();
                m.chg_current = m.chg_voltage = m.chg_power = m.chg_temp = Sample();
                m.link_limit = m.ac_power = m.ac_voltage = m.ac_current = m.ac_freq = Sample();
                m.hw_sent_current = m.hw_ack_ok = m.hw_fault = Sample();
                Serial.printf("mqtt: own charger '%s' is instance %d\n", OWN_CHARGER_NAME, inst);
            }
        }
        // every charger's current and configured limit feed the total ring (slots by instance)
        const bool is_cur = !strcmp(p, "Dc/0/Current"), is_lim = !strcmp(p, "Settings/ChargeCurrentLimit");
        if (is_cur || is_lim) {
            int slot = -1;
            for (int i = 0; i < Model::MAX_CHARGERS; i++) if (m.chargers[i].instance == inst) slot = i;
            if (slot < 0) {
                for (int i = 0; i < Model::MAX_CHARGERS && slot < 0; i++) if (m.chargers[i].instance < 0) slot = i;
                if (slot >= 0) {
                    m.chargers[slot].instance = inst;
                    // keep ascending instance order so segment colours stay with the same unit
                    for (int i = 0; i < Model::MAX_CHARGERS - 1; i++)
                        for (int j = 0; j < Model::MAX_CHARGERS - 1 - i; j++)
                            if (m.chargers[j + 1].instance >= 0 && (m.chargers[j].instance < 0 || m.chargers[j].instance > m.chargers[j + 1].instance)) {
                                Model::ChargerSlot tmp = m.chargers[j]; m.chargers[j] = m.chargers[j + 1]; m.chargers[j + 1] = tmp;
                            }
                    for (int i = 0; i < Model::MAX_CHARGERS; i++) if (m.chargers[i].instance == inst) slot = i;
                }
            }
            if (slot >= 0) (is_cur ? m.chargers[slot].current : m.chargers[slot].limit).set(val.as<float>());
        }
        if (inst != m.charger_instance) return;   // the rest is this display's own charger
        m.charger_seen = true;
        if      (!strcmp(p, "CustomName"))             { copy_str(m.charger_name, sizeof m.charger_name, val); return; }
        else if (!strcmp(p, "Huawei/State"))           { copy_str(m.charger_hw_state, sizeof m.charger_hw_state, val); return; }
        else if (!strcmp(p, "Huawei/LimitingReason"))  { copy_str(m.limiting_reason, sizeof m.limiting_reason, val); return; }
        else if (!strcmp(p, "Huawei/Model"))           { copy_str(m.charger_model, sizeof m.charger_model, val); return; }
        else if (!strcmp(p, "FirmwareVersion"))        { copy_str(m.charger_fw, sizeof m.charger_fw, val); return; }
        float f = val.as<float>();
        if      (!strcmp(p, "Connected"))          m.chg_connected.set(f);
        else if (!strcmp(p, "State"))              m.chg_state.set(f);
        else if (!strcmp(p, "Mode"))               m.chg_mode.set(f);
        else if (!strcmp(p, "Dc/0/Current"))       m.chg_current.set(f);
        else if (!strcmp(p, "Dc/0/Voltage"))       m.chg_voltage.set(f);
        else if (!strcmp(p, "Dc/0/Power"))         m.chg_power.set(f);
        else if (!strcmp(p, "Dc/0/Temperature"))   m.chg_temp.set(f);
        else if (!strcmp(p, "Link/ChargeCurrent")) m.link_limit.set(f);
        else if (!strcmp(p, "Ac/In/L1/P"))         m.ac_power.set(f);
        else if (!strcmp(p, "Ac/In/L1/V"))         m.ac_voltage.set(f);
        else if (!strcmp(p, "Ac/In/L1/I"))         m.ac_current.set(f);
        else if (!strcmp(p, "Ac/In/L1/F"))         m.ac_freq.set(f);
        else if (!strcmp(p, "Huawei/SentCurrent")) m.hw_sent_current.set(f);
        else if (!strcmp(p, "Huawei/AckOk"))       m.hw_ack_ok.set(f);
        else if (!strcmp(p, "Huawei/Fault"))       m.hw_fault.set(f);
    }
}

// Every WLAN event with its reason code, so drops can be explained from the serial log.
uint32_t g_wifi_drops = 0;
int g_wifi_last_reason = 0;
static void on_wifi_event(WiFiEvent_t ev, WiFiEventInfo_t info)
{
    switch (ev) {
        case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
            g_wifi_drops++;
            g_wifi_last_reason = info.wifi_sta_disconnected.reason;
            Serial.printf("wifi: disconnected, reason %d (drop #%lu, rssi was %d)\n",
                          g_wifi_last_reason, (unsigned long)g_wifi_drops, model.wifi_rssi);
            break;
        case ARDUINO_EVENT_WIFI_STA_CONNECTED:
            Serial.printf("wifi: associated with %s, channel %d\n", WiFi.BSSIDstr().c_str(), WiFi.channel());
            break;
        case ARDUINO_EVENT_WIFI_STA_GOT_IP:
            Serial.println("wifi: got ip");
            break;
        default:
            break;
    }
}

// First join: let the radio pick. Every retry after a failed join: blocking scan (about 1.5 s)
// for the strongest node of our SSID and connect to it by BSSID. In a mesh the radio's own pick
// can be a node that is out of reach from where the display hangs.
static uint32_t g_wifi_joins = 0;
static void wifi_start()
{
    static bool hooked = false;
    if (!hooked) { WiFi.onEvent(on_wifi_event); hooked = true; }
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.setAutoReconnect(true);
    g_wifi_joins++;
    bool pinned = false;
    if (g_wifi_joins > 1) {
        int16_t n = WiFi.scanNetworks(false, false, false, 120, 0, WIFI_SSID);
        int best = -1, best_rssi = -127;
        for (int i = 0; i < n; i++) if (WiFi.RSSI(i) > best_rssi) { best = i; best_rssi = WiFi.RSSI(i); }
        if (best >= 0) {
            uint8_t bssid[6];
            memcpy(bssid, WiFi.BSSID(best), 6);
            const int ch = WiFi.channel(best);
            Serial.printf("wifi: retry %lu: %d node(s) of our SSID, joining %s ch %d %d dBm\n",
                          (unsigned long)g_wifi_joins, n, WiFi.BSSIDstr(best).c_str(), ch, best_rssi);
            WiFi.scanDelete();
            WiFi.begin(WIFI_SSID, WIFI_PASSWORD, ch, bssid);
            pinned = true;
        } else {
            Serial.printf("wifi: retry %lu: our SSID not seen in the scan\n", (unsigned long)g_wifi_joins);
            WiFi.scanDelete();
        }
    }
    if (!pinned) WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    s_wifi_started = millis();
    model.wifi = LinkState::Connecting;
    Serial.println("wifi: connecting");
}

// --- roaming --------------------------------------------------------------------------------
// The ESP32 joins the first access point that answers and never roams. In a mesh (Orbi) that
// can be a far satellite: -85 dBm was seen right next to the router. When the signal is poor we
// scan for the strongest node of our SSID and reconnect to it explicitly (BSSID + channel).
static constexpr int ROAM_RSSI_POOR = -70;        // dBm; below this we look for a better node
static constexpr int ROAM_MIN_GAIN = 8;           // dB; switch only for a clearly better node
static constexpr uint32_t ROAM_CHECK_MS = 30000;
static uint32_t s_roam_next = 0;
static bool s_scan_running = false;
uint32_t g_wifi_roams = 0;

static void roam_check(uint32_t now)
{
    if (s_scan_running) {
        int16_t n = WiFi.scanComplete();
        if (n == WIFI_SCAN_RUNNING) return;
        s_scan_running = false;
        if (n > 0) {
            int best = -1, best_rssi = -127;
            for (int i = 0; i < n; i++) {
                if (WiFi.SSID(i) == WIFI_SSID && WiFi.RSSI(i) > best_rssi) { best = i; best_rssi = WiFi.RSSI(i); }
            }
            if (best >= 0) {
                const int cur_rssi = WiFi.RSSI();
                const bool other = memcmp(WiFi.BSSID(best), WiFi.BSSID(), 6) != 0;
                Serial.printf("wifi: scan: %d APs, best %s ch %d %d dBm, current %d dBm\n",
                              n, WiFi.BSSIDstr(best).c_str(), WiFi.channel(best), best_rssi, cur_rssi);
                if (other && best_rssi >= cur_rssi + ROAM_MIN_GAIN) {
                    uint8_t bssid[6];
                    memcpy(bssid, WiFi.BSSID(best), 6);
                    const int ch = WiFi.channel(best);
                    WiFi.scanDelete();
                    g_wifi_roams++;
                    Serial.printf("wifi: roaming to the stronger node (roam #%lu)\n", (unsigned long)g_wifi_roams);
                    WiFi.disconnect();
                    WiFi.begin(WIFI_SSID, WIFI_PASSWORD, ch, bssid);
                    s_wifi_started = now;
                    model.wifi = LinkState::Connecting;
                    model.mqtt = LinkState::Down;
                    return;
                }
            }
        }
        WiFi.scanDelete();
        return;
    }
    if (now < s_roam_next) return;
    s_roam_next = now + ROAM_CHECK_MS;
    if (WiFi.RSSI() > ROAM_RSSI_POOR) return;
    Serial.printf("wifi: rssi %d dBm, scanning for a better node\n", WiFi.RSSI());
    // async scan limited to our SSID, 120 ms per channel: the connection survives it
    WiFi.scanNetworks(true, false, false, 120, 0, WIFI_SSID);
    s_scan_running = true;
}

static bool s_ota_started = false;
static lv_obj_t *s_ota_label = nullptr;
bool g_ota_active = false;

// --- over-the-air update -------------------------------------------------------------------
// ArduinoOTA: PlatformIO env "amoled164_ota" pushes a build over WLAN (espota protocol, password
// from secrets). The display shows a progress text while the image is written; the board reboots
// into the new slot when done. Two app slots (default_16MB.csv) keep a failed upload harmless.

static void ota_begin()
{
    if (s_ota_started) return;
    ArduinoOTA.setHostname(OTA_HOSTNAME);
    ArduinoOTA.setPassword(OTA_PASSWORD);
    ArduinoOTA.setRebootOnSuccess(true);
    ArduinoOTA.onStart([] {
        g_ota_active = true;
        Serial.println("ota: start");
        s_ota_label = lv_label_create(lv_layer_top());
        lv_obj_set_style_text_color(s_ota_label, lv_color_white(), 0);
        lv_obj_set_style_bg_color(s_ota_label, lv_color_black(), 0);
        lv_obj_set_style_bg_opa(s_ota_label, LV_OPA_80, 0);
        lv_obj_set_style_pad_all(s_ota_label, 12, 0);
        lv_label_set_text(s_ota_label, "Firmware update...");
        lv_obj_center(s_ota_label);
        lv_timer_handler();
    });
    ArduinoOTA.onProgress([](unsigned int done, unsigned int total) {
        static int last = -1;
        int pct = total ? (int)(done * 100ULL / total) : 0;
        if (pct / 5 != last / 5 && s_ota_label) {
            last = pct;
            char buf[40];
            snprintf(buf, sizeof buf, "Firmware update... %d %%", pct);
            lv_label_set_text(s_ota_label, buf);
            lv_obj_center(s_ota_label);
            lv_timer_handler();
        }
    });
    ArduinoOTA.onEnd([] { Serial.println("ota: done, rebooting"); });
    ArduinoOTA.onError([](ota_error_t e) {
        Serial.printf("ota: error %d\n", (int)e);
        g_ota_active = false;
        if (s_ota_label) { lv_obj_del(s_ota_label); s_ota_label = nullptr; }
    });
    ArduinoOTA.begin();
    s_ota_started = true;
    Serial.printf("ota: ready as %s.local\n", OTA_HOSTNAME);
}

// --- HTTP firmware upload (served by the board) -----------------------------------------------
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
            s_http.send(ok ? 200 : 500, "text/plain", ok ? "OK, rebooting\n" : "FAILED\n");
            if (ok) { delay(300); ESP.restart(); }
        },
        [] {
            if (!s_http.authenticate("ota", OTA_PASSWORD)) return;
            HTTPUpload &up = s_http.upload();
            if (up.status == UPLOAD_FILE_START) {
                g_ota_active = true;
                Serial.printf("http-ota: start %s\n", up.filename.c_str());
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
                if (Update.end(true)) Serial.printf("http-ota: done, %u bytes\n", up.totalSize);
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
{
    model.mqtt = LinkState::Connecting;
    // Client id must differ per board: the low 24 bits of getEfuseMac() are the vendor OUI and
    // identical on all boards (two displays then kick each other off the broker every few seconds,
    // rc=-3). Use the device-specific upper bytes.
    const uint64_t mac = ESP.getEfuseMac();
    char client_id[32];
    snprintf(client_id, sizeof client_id, "chgdisp-%d-%02X%02X%02X", DISPLAY_ID,
             (unsigned)((mac >> 40) & 0xFF), (unsigned)((mac >> 32) & 0xFF), (unsigned)((mac >> 24) & 0xFF));
    Serial.printf("mqtt: connecting to %s:%d\n", MQTT_HOST, MQTT_PORT);
    if (!s_mqtt.connect(client_id)) {
        Serial.printf("mqtt: failed, rc=%d\n", s_mqtt.state());
        model.mqtt = LinkState::Down;
        s_mqtt_next_try = millis() + MQTT_RETRY_MS;
        return;
    }
    char topic[96];
    for (const char *suffix : SUBS) {
        snprintf(topic, sizeof topic, "%s%s", s_prefix, suffix);
        s_mqtt.subscribe(topic);
    }
    publish_keepalive();
    model.mqtt = LinkState::Up;
    Serial.println("mqtt: connected, subscribed, keepalive sent");
}

void net_begin()
{
    snprintf(s_prefix, sizeof s_prefix, "N/%s/", VRM_PORTAL_ID);
    s_prefix_len = strlen(s_prefix);
    s_mqtt.setServer(MQTT_HOST, MQTT_PORT);
    s_mqtt.setBufferSize(512);
    s_mqtt.setKeepAlive(30);
    s_mqtt.setSocketTimeout(5);
    s_mqtt.setCallback(on_message);
    wifi_start();
}

void net_loop()
{
    const uint32_t now = millis();

    // WLAN
    if (WiFi.status() == WL_CONNECTED) {
        if (model.wifi != LinkState::Up) {
            model.wifi = LinkState::Up;
            strncpy(model.ip, WiFi.localIP().toString().c_str(), sizeof model.ip - 1);
            snprintf(model.wifi_node, sizeof model.wifi_node, "%s ch%d", WiFi.BSSIDstr().c_str() + 12, WiFi.channel());
            Serial.printf("wifi: up, ip %s, rssi %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
            s_mqtt_next_try = 0;
        }
        model.wifi_rssi = WiFi.RSSI();
        ota_begin();
        http_begin();
        ArduinoOTA.handle();
        s_http.handleClient();
        if (g_ota_active) return;          // nothing else while an image is being written
        roam_check(now);
        if (model.wifi != LinkState::Up) return;   // roaming just restarted the join
    } else {
        if (model.wifi == LinkState::Up) {
            Serial.println("wifi: lost");
            model.mqtt = LinkState::Down;
        }
        model.wifi = LinkState::Connecting;
        if (now - s_wifi_started > WIFI_RESTART_MS) {
            WiFi.disconnect();
            wifi_start();
        }
        return;
    }

    // MQTT
    if (!s_mqtt.connected()) {
        if (model.mqtt == LinkState::Up) {
            Serial.printf("mqtt: lost, rc=%d\n", s_mqtt.state());
            model.mqtt = LinkState::Down;
        }
        if (now >= s_mqtt_next_try) mqtt_try_connect();
        return;
    }
    s_mqtt.loop();
    if (now - s_last_keepalive >= KEEPALIVE_MS) publish_keepalive();
}
