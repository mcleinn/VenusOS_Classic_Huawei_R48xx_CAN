// Charger display for the Waveshare ESP32-S3-Touch-AMOLED-1.64.
// Data: Cerbo GX over MQTT (Venus dbus-mqtt), later Huawei R48xx over CAN.
#include <Arduino.h>
#include <lvgl.h>
#include "display.h"
#include "net.h"
#include "can.h"
#include "ui.h"
#include "model.h"

Model model;

void setup()
{
    Serial.begin(115200);
    delay(300);
    Serial.println("\ncharger-display 0.1 booting");
    Serial.printf("heap %u, psram %u\n", ESP.getFreeHeap(), ESP.getFreePsram());

    display_begin();
    ui_begin();
    display_set_brightness(200);
    lv_timer_handler();           // first frame before the radio comes up

    net_begin();
    can_begin();
}

extern uint32_t g_flush_count;
extern int g_flush_last_err;
extern "C" volatile uint32_t g_flush_done_count;
extern uint32_t g_wifi_drops;
extern int g_wifi_last_reason;
extern uint32_t g_wifi_roams;

// One status line every 10 s so the data path can be checked over USB without seeing the screen.
static void report()
{
    static uint32_t last = 0;
    if (millis() - last < 10000) return;
    last = millis();
    const Model &m = model;
    Serial.printf("status flush=%lu/%lu err=%d wifi=%d(drops %lu, roams %lu, last reason %d, %d dBm) mqtt=%d msgs=%lu fresh=%d | soc=%.0f%% batt=%.0fW %.2fV | chg[%d %s] %s %.1fA %.2fV %.0fW %.0fC limit=%.0fA | loads=%.0fW src=%d sys=%d | heap=%u\n",
                  (unsigned long)g_flush_count, (unsigned long)g_flush_done_count, g_flush_last_err,
                  (int)m.wifi, (unsigned long)g_wifi_drops, (unsigned long)g_wifi_roams, g_wifi_last_reason, m.wifi_rssi, (int)m.mqtt, (unsigned long)m.mqtt_msgs, (int)m.venus_fresh(),
                  m.soc.v, m.batt_power.v, m.batt_voltage.v,
                  m.charger_instance, m.charger_name, m.charger_hw_state,
                  m.chg_current.v, m.chg_voltage.v, m.chg_power.v, m.chg_temp.v, m.link_limit.v,
                  m.consumption.v, (int)m.active_in_source.v, (int)m.system_state.v,
                  ESP.getFreeHeap());
}

// Serial commands for bench tests:  "can 1081407F 0181000000000ABC"  injects a frame,
// "dump" prints the CAN-decoded model values on one line (parsed by tools/can_replay.py).
static void serial_commands()
{
    static char line[96];
    static size_t n = 0;
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\r') continue;
        if (c != '\n') { if (n < sizeof line - 1) line[n++] = c; continue; }
        line[n] = 0; n = 0;
        if (!strncmp(line, "can ", 4)) {
            char *end;
            uint32_t id = strtoul(line + 4, &end, 16);
            uint8_t d[8] = {0}; uint8_t len = 0;
            while (*end == ' ') end++;
            for (; len < 8 && end[0] && end[1]; len++, end += 2) {
                char b[3] = {end[0], end[1], 0};
                d[len] = (uint8_t)strtoul(b, nullptr, 16);
            }
            can_inject(id, d, len);
        } else if (!strcmp(line, "dump")) {
            const Model &m = model;
            Serial.printf("dump frames=%lu addr=%d sets=%lu acks=%lu out_v=%.3f out_a=%.3f out_w=%.3f ac_v=%.3f ac_a=%.3f ac_w=%.3f ac_f=%.3f "
                          "t_out=%.3f t_in=%.3f limit=%.4f rated=%.2f out_on=%d ac_present=%d set_v=%.4f set_ratio=%.4f standby=%d ack_ok=%d load=%.4f\n",
                          (unsigned long)m.can_frames, m.can_addr, (unsigned long)m.can_sets, (unsigned long)m.can_acks,
                          m.can_out_v.v, m.can_out_a.v, m.can_out_w.v, m.can_ac_v.v, m.can_ac_a.v, m.can_ac_w.v, m.can_ac_f.v,
                          m.can_temp_out.v, m.can_temp_in.v, m.can_limit_ratio.v, m.can_rated_a.v,
                          m.can_output_on.has() ? (int)m.can_output_on.v : -1, m.can_ac_present.has() ? (int)m.can_ac_present.v : -1,
                          m.can_set_v.v, m.can_set_ratio.v, m.can_standby.has() ? (int)m.can_standby.v : -1,
                          m.can_ack_ok.has() ? (int)m.can_ack_ok.v : -1, m.can_load_ratio.v);
        }
    }
}

void loop()
{
    net_loop();
    can_loop();
    serial_commands();
    report();
    uint32_t wait = lv_timer_handler();
    if (wait > 20) wait = 20;
    delay(wait ? wait : 1);
}
