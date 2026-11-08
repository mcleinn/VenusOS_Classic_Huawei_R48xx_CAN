// Shared data model: filled by net.cpp (MQTT) and later by the CAN side, read by ui.cpp.
// Single-threaded (everything runs from loop()), so no locking.
#pragma once
#include <stdint.h>
#include <Arduino.h>
#include "devices.h"

struct Sample {
    float v = 0;
    uint32_t t = 0;          // millis() of the last update, 0 = never
    void set(float nv) { v = nv; t = millis() ? millis() : 1; }
    bool has() const { return t != 0; }
    uint32_t age_ms() const { return has() ? millis() - t : UINT32_MAX; }
};

enum class LinkState : uint8_t { Down, Connecting, Up };

struct Model {
    // Links
    LinkState wifi = LinkState::Down;
    LinkState mqtt = LinkState::Down;
    LinkState can  = LinkState::Down;   // no CAN board yet; stays Down
    int8_t wifi_rssi = 0;
    uint32_t mqtt_last_rx = 0;          // millis() of the last Venus message
    uint32_t mqtt_msgs = 0;
    uint32_t can_frames = 0;
    char ip[16] = "";

    // Venus system (com.victronenergy.system)
    Sample soc, batt_power, batt_voltage, batt_current, batt_time_to_go, batt_temp;
    Sample consumption, grid_power, genset_power, dc_charger_power, dc_system_power, ac_in_power;
    Sample system_state, active_in_source;

    // Multi / Quattro (com.victronenergy.vebus)
    Sample vebus_state, vebus_mode, vebus_in_v, vebus_in_f, vebus_out_v, vebus_out_f, vebus_in_limit;

    // Battery BMS (com.victronenergy.battery, Pylontech)
    Sample bms_ccl, bms_cvl, bms_dcl, bms_soh, bms_modules, bms_block_chg, bms_block_dis;
    Sample bms_cell_min, bms_cell_max;

    // All charger services seen, for the total-current ring: actual current and the per-unit
    // limit configured in the Venus driver (Settings/ChargeCurrentLimit). Lowest instance first.
    struct ChargerSlot { int instance = -1; Sample current; Sample limit; };
    static constexpr int MAX_CHARGERS = 2;
    ChargerSlot chargers[MAX_CHARGERS];

    // Huawei charger service (com.victronenergy.charger.huawei_*)
    bool charger_seen = false;
    int charger_instance = -1;
    char charger_name[24] = OWN_CHARGER_NAME;
    char charger_hw_state[24] = "";      // /Huawei/State text, e.g. "ready"
    char limiting_reason[48] = "";
    char charger_model[16] = "";
    char charger_fw[16] = "";
    Sample chg_connected, chg_state, chg_mode;
    Sample chg_current, chg_voltage, chg_power, chg_temp;
    Sample link_limit, ac_power, ac_voltage, ac_current, ac_freq;
    Sample hw_sent_current, hw_ack_ok, hw_fault;

    // CAN listener (Huawei bus, decoded both directions)
    int can_addr = 0;
    uint32_t can_sets = 0, can_acks = 0;
    Sample can_out_v, can_out_a, can_out_w, can_ac_v, can_ac_a, can_ac_w, can_ac_f;
    Sample can_temp_out, can_temp_in, can_limit_ratio, can_rated_a, can_output_on, can_ac_present;
    Sample can_set_v, can_set_ratio, can_standby, can_ack_ok, can_load_ratio;

    // True when Venus data arrived in the last 60 s.
    bool venus_fresh() const { return mqtt_last_rx && millis() - mqtt_last_rx < 60000; }
};

extern Model model;
