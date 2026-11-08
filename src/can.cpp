#include "can.h"

#include <Arduino.h>
#include "driver/twai.h"
#include "model.h"
#include "board_pins.h"

// Huawei R48xx CAN protocol (29-bit ids, 125 kbit/s), as decoded in the Venus driver
// (C:\Dev-Free\CerboGX_R4850G2_CAN\src\driver\huawei\protocol.py):
//   id = proto[6] << 23 | addr[7] << 16 | cmd[8] << 8 | to_psu << 7 | group[5] << 2 | sw_addr << 1 | cont
//   proto 0x21 controller<->psu, 0x20 psu unsolicited; group 0x1F
//   cmd 0x40 status (reply: d[1] = register, value = d[4..7] big-endian, /1024 for most)
//   cmd 0x80 set (to psu: d[1] = register 0x00 voltage, 0x03 current ratio; from psu: ack)
//   cmd 0x11 unsolicited (d[6..7] = load ratio x 1024)
static constexpr uint8_t PROTO_CTL = 0x21, PROTO_PSU = 0x20, GROUP_ALL = 0x1F;
static constexpr uint8_t CMD_UNSOL = 0x11, CMD_DATA = 0x40, CMD_SET = 0x80, CMD_GET = 0x82;

static bool s_started = false;
static uint32_t s_last_rx = 0;
static uint32_t s_last_recover = 0;

static inline uint32_t be32(const uint8_t *d) { return (uint32_t)d[4] << 24 | (uint32_t)d[5] << 16 | (uint32_t)d[6] << 8 | d[7]; }

static void decode(const twai_message_t &f)
{
    if (!f.extd || f.rtr) return;
    const uint32_t id = f.identifier;
    const uint8_t proto = (id >> 23) & 0x3F, addr = (id >> 16) & 0x7F, cmd = (id >> 8) & 0xFF;
    const bool to_psu = id & 0x80;
    if ((proto != PROTO_CTL && proto != PROTO_PSU) || ((id >> 2) & 0x1F) != GROUP_ALL) return;

    uint8_t d[8] = {0};
    memcpy(d, f.data, f.data_length_code > 8 ? 8 : f.data_length_code);
    Model &m = model;
    m.can_frames++;
    s_last_rx = millis();
    const bool from_psu = !to_psu || cmd == CMD_UNSOL;
    if (from_psu && addr) m.can_addr = addr;

    if ((cmd == CMD_DATA || cmd == CMD_GET) && from_psu) {
        const uint32_t raw = be32(d);
        const float v = raw / 1024.0f;
        switch (d[1]) {
            case 0x70: m.can_ac_w.set(v); break;
            case 0x71: m.can_ac_f.set(v); break;
            case 0x72: m.can_ac_a.set(v); break;
            case 0x73: m.can_out_w.set(v); break;
            case 0x75: if (raw) m.can_out_v.set(v); break;      // 0 on the G5 burst, 0x85 carries it there
            case 0x85: m.can_out_v.set(v); break;
            case 0x76: m.can_limit_ratio.set(v); break;
            case 0x78: m.can_ac_v.set(v); break;
            case 0x7F: m.can_temp_out.set(v); break;
            case 0x80: m.can_temp_in.set(v); break;
            case 0x81: m.can_out_a.set(v); break;
            case 0x83:
                m.can_output_on.set((raw >> 27) & 1);
                m.can_ac_present.set(((raw >> 29) & 1) ? 0 : 1);   // bit 29 of bytes 4-7 = input power failure
                break;
            case 0x88: m.can_rated_a.set(((uint16_t)d[2] << 8 | d[3]) / 100.0f); break;
            default: break;
        }
    } else if (cmd == CMD_SET && !to_psu) {
        m.can_acks++;
        m.can_ack_ok.set((d[0] == 0x21 || (d[0] & 0x20) || (d[0] & 0x0F) == 2) ? 0 : 1);
    } else if (cmd == CMD_SET) {
        const uint32_t raw = be32(d);
        if (d[1] == 0x00)      m.can_set_v.set(raw / 1024.0f);
        else if (d[1] == 0x03) m.can_set_ratio.set(raw / 1024.0f);   // x nominal current = A
        else if (d[1] == 0x32) m.can_standby.set(d[3]);
        m.can_sets++;
    } else if (cmd == CMD_UNSOL) {
        m.can_load_ratio.set(((uint16_t)d[6] << 8 | d[7]) / 1024.0f);
    }
}

// Feed a frame as if received from the bus (serial replay of recorded captures, see
// tools/can_replay.py). Marks the link alive like a real frame.
void can_inject(uint32_t id, const uint8_t *data, uint8_t len)
{
    twai_message_t f = {};
    f.identifier = id;
    f.extd = 1;
    f.data_length_code = len > 8 ? 8 : len;
    memcpy(f.data, data, f.data_length_code);
    decode(f);
}

void can_begin()
{
    twai_general_config_t g = TWAI_GENERAL_CONFIG_DEFAULT((gpio_num_t)PIN_CAN_TX, (gpio_num_t)PIN_CAN_RX, TWAI_MODE_LISTEN_ONLY);
    g.rx_queue_len = 32;
    g.alerts_enabled = TWAI_ALERT_BUS_OFF | TWAI_ALERT_ERR_PASS | TWAI_ALERT_BUS_RECOVERED;
    twai_timing_config_t t = TWAI_TIMING_CONFIG_125KBITS();
    twai_filter_config_t fcfg = TWAI_FILTER_CONFIG_ACCEPT_ALL();
    pinMode(PIN_CAN_RX, INPUT_PULLUP);    // idle = recessive while no transceiver is attached
    if (twai_driver_install(&g, &t, &fcfg) != ESP_OK || twai_start() != ESP_OK) {
        Serial.println("can: driver start failed");
        return;
    }
    s_started = true;
    Serial.printf("can: listen-only on TX %d / RX %d, 125 kbit/s\n", PIN_CAN_TX, PIN_CAN_RX);
}

void can_loop()
{
    if (!s_started) return;
    twai_message_t f;
    int n = 0;
    while (n < 64 && twai_receive(&f, 0) == ESP_OK) { decode(f); n++; }

    uint32_t alerts = 0;
    if (twai_read_alerts(&alerts, 0) == ESP_OK && (alerts & TWAI_ALERT_BUS_OFF)) {
        // listen-only nodes do not go bus-off by themselves, but recover if it ever happens
        if (millis() - s_last_recover > 5000) { twai_initiate_recovery(); s_last_recover = millis(); }
    }
    model.can = (s_last_rx && millis() - s_last_rx < 5000) ? LinkState::Up : LinkState::Down;
}
