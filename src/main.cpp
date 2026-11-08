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

void loop()
{
    net_loop();
    can_loop();
    report();
    uint32_t wait = lv_timer_handler();
    if (wait > 20) wait = 20;
    delay(wait ? wait : 1);
}
