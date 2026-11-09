// Screen: landscape 456 x 280, styled after Victron GUI v2 (dark scheme).
//
// A fixed status bar (26 px) sits on top; below it a horizontal tile view with five pages the
// user swipes through; page dots at the bottom. A separate screensaver screen (black with
// gliding live values) takes over after SAVER_AFTER_MS without touch; touch or BOOT returns.
//
//   page 0  Charger      arc gauge of the battery + charger card + loads card (the daily view)
//   page 1  Overview     AC in -> Quattro -> loads, battery and charger, with animated flows
//   page 2  Battery      Pylontech details (BMS limits, modules, cells, temperature, time to go)
//   page 3  Charger      Huawei details (AC in, output, limits, acks, firmware)
//   page 4  Status       WLAN / Cerbo / CAN links, uptime, memory, firmware
//   page 5  CAN bus      what the listener hears on the charger bus (sets, status, acks)
//
// Colours: Victron gui-v2 themes/color/ColorDesign.json (dark scheme).
#include "ui.h"

#include <Arduino.h>
#include <lvgl.h>
#include <math.h>
#include "model.h"
#include "board_pins.h"

// --- palette (gui-v2 ColorDesign.json) ---------------------------------------------------
static const lv_color_t C_BG       = LV_COLOR_MAKE(0x14, 0x14, 0x14);  // gray1
static const lv_color_t C_CARD     = LV_COLOR_MAKE(0x27, 0x26, 0x22);  // gray2
static const lv_color_t C_TRACK    = LV_COLOR_MAKE(0x50, 0x4F, 0x4B);  // gray3
static const lv_color_t C_DIM      = LV_COLOR_MAKE(0x96, 0x95, 0x91);  // dimWhite
static const lv_color_t C_TEXT     = LV_COLOR_MAKE(0xFF, 0xFF, 0xFF);
static const lv_color_t C_BLUE     = LV_COLOR_MAKE(0x38, 0x7D, 0xC5);
static const lv_color_t C_LIGHTBLUE= LV_COLOR_MAKE(0x73, 0xA2, 0xD3);
static const lv_color_t C_GREEN    = LV_COLOR_MAKE(0x72, 0xB8, 0x4C);
static const lv_color_t C_ORANGE   = LV_COLOR_MAKE(0xF0, 0x96, 0x2E);
static const lv_color_t C_RED      = LV_COLOR_MAKE(0xF3, 0x5C, 0x58);

// --- geometry ----------------------------------------------------------------------------
static constexpr int BAR_H  = 26;
static constexpr int PAGE_H = UI_H - BAR_H;        // 254
static constexpr int PAGES  = 6;

// --- small helpers -----------------------------------------------------------------------
static lv_obj_t *make_label(lv_obj_t *parent, const lv_font_t *font, lv_color_t color, const char *txt = "")
{
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, color, 0);
    lv_label_set_text(l, txt);
    return l;
}

static lv_obj_t *make_card(lv_obj_t *parent, int x, int y, int w, int h)
{
    lv_obj_t *c = lv_obj_create(parent);
    lv_obj_remove_style_all(c);
    lv_obj_set_size(c, w, h);
    lv_obj_set_pos(c, x, y);
    lv_obj_set_style_bg_color(c, C_CARD, 0);
    lv_obj_set_style_bg_opa(c, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(c, 12, 0);
    lv_obj_set_style_pad_all(c, 10, 0);
    lv_obj_clear_flag(c, LV_OBJ_FLAG_SCROLLABLE);
    return c;
}

static lv_obj_t *make_badge(lv_obj_t *parent, const char *txt)
{
    lv_obj_t *b = lv_obj_create(parent);
    lv_obj_remove_style_all(b);
    lv_obj_set_size(b, 30, 18);
    lv_obj_set_style_radius(b, 4, 0);
    lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(b, C_TRACK, 0);
    lv_obj_clear_flag(b, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_t *l = make_label(b, &lv_font_montserrat_12, C_BG, txt);
    lv_obj_center(l);
    return b;
}

static lv_obj_t *make_arc(lv_obj_t *parent, int x, int y, int d, int width)
{
    lv_obj_t *arc = lv_arc_create(parent);
    lv_obj_set_size(arc, d, d);
    lv_obj_set_pos(arc, x, y);
    lv_arc_set_rotation(arc, 135);
    lv_arc_set_bg_angles(arc, 0, 270);
    lv_arc_set_range(arc, 0, 1000);
    lv_arc_set_value(arc, 0);
    lv_obj_remove_style(arc, nullptr, LV_PART_KNOB);
    lv_obj_clear_flag(arc, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_arc_width(arc, width, LV_PART_MAIN);
    lv_obj_set_style_arc_width(arc, width, LV_PART_INDICATOR);
    lv_obj_set_style_arc_rounded(arc, true, LV_PART_MAIN);
    lv_obj_set_style_arc_rounded(arc, true, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(arc, C_TRACK, LV_PART_MAIN);
    lv_obj_set_style_arc_color(arc, C_BLUE, LV_PART_INDICATOR);
    return arc;
}

static lv_obj_t *make_dot(lv_obj_t *parent, int d, lv_color_t c)
{
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_remove_style_all(o);
    lv_obj_set_size(o, d, d);
    lv_obj_set_style_radius(o, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(o, c, 0);
    return o;
}

// label/value pair for the detail pages
struct KV { lv_obj_t *key; lv_obj_t *val; };
static KV make_kv(lv_obj_t *parent, int x, int y, const char *key, int w = 140)
{
    KV kv;
    kv.key = make_label(parent, &lv_font_montserrat_12, C_DIM, key);
    lv_obj_set_pos(kv.key, x, y);
    kv.val = make_label(parent, &lv_font_montserrat_16, C_TEXT, "--");
    lv_obj_set_pos(kv.val, x, y + 14);
    lv_obj_set_width(kv.val, w);
    lv_label_set_long_mode(kv.val, LV_LABEL_LONG_DOT);
    return kv;
}
static void kv_set(KV &kv, const Sample &s, const char *fmt, float scale = 1.0f)
{
    if (!s.has()) { lv_label_set_text(kv.val, "--"); return; }
    char buf[32];
    snprintf(buf, sizeof buf, fmt, s.v * scale);
    lv_label_set_text(kv.val, buf);
}

static lv_color_t soc_color(float soc) { return soc < 20 ? C_RED : soc < 50 ? C_ORANGE : C_BLUE; }

static const char *system_state_text(int s)
{
    switch (s) {
        case 0: return "Off";          case 1: return "Low power";    case 2: return "Fault";
        case 3: return "Bulk";         case 4: return "Absorption";   case 5: return "Float";
        case 6: return "Storage";      case 7: return "Equalize";     case 8: return "Passthru";
        case 9: return "Inverting";    case 10: return "Assisting";   case 11: return "Power supply";
        case 244: return "Sustain";    case 245: return "Wake-up";    case 246: return "Repeated absorption";
        case 247: return "Auto equalize"; case 248: return "Battery safe"; case 249: return "Load shedding";
        case 250: return "Blocked";    case 251: return "Test";       case 252: return "External control";
        case 253: return "Discharging"; case 254: return "Discharging"; case 255: return "Unknown";
        default: return "";
    }
}

static const char *source_text(int s)
{
    switch (s) {
        case 1: return "Grid"; case 2: return "Generator"; case 3: return "Shore";
        case 0: case 240: return "No AC in"; default: return "";
    }
}

static const char *vebus_mode_text(int m)
{
    switch (m) { case 1: return "Charger only"; case 2: return "Inverter only"; case 3: return "On"; case 4: return "Off"; default: return ""; }
}

static const char *charger_state_text()
{
    if (!model.charger_seen) return "not found";
    if (model.chg_connected.has() && model.chg_connected.v < 0.5f) return "Disconnected";
    if (model.charger_hw_state[0]) return model.charger_hw_state;
    if (!model.chg_state.has()) return "";
    int s = (int)model.chg_state.v;
    return s == 0 ? "Off" : s == 252 ? "Ready" : system_state_text(s);
}

// Charger output values come from two sources: the CAN bus (module status, about once a second
// while the bus is alive) and the Cerbo (5 s poll, republished over MQTT with every keepalive).
// Whichever sample was RECEIVED more recently wins; nothing older is ever preferred.
// Battery, loads and AC-in have no CAN source.
static const Sample &pick(const Sample &can, const Sample &mqtt)
{
    if (!can.has()) return mqtt;
    if (!mqtt.has()) return can;
    return (int32_t)(can.t - mqtt.t) >= 0 ? can : mqtt;
}
static constexpr uint32_t CAN_FRESH_MS = 3000;   // only used for the "data alive" judgement
static const Sample &live_current(const Model &m) { return pick(m.can_out_a, m.chg_current); }
static const Sample &live_voltage(const Model &m) { return pick(m.can_out_v, m.chg_voltage); }
static const Sample &live_power(const Model &m)   { return pick(m.can_out_w, m.chg_power); }
static const Sample &live_temp(const Model &m)    { return pick(m.can_temp_out, m.chg_temp); }
static bool charger_data_fresh(const Model &m)    { return m.venus_fresh() || (m.can_out_a.has() && m.can_out_a.age_ms() < CAN_FRESH_MS); }

static void fmt_power(char *buf, size_t n, float p, bool arrows = true)
{
    if (arrows) snprintf(buf, n, "%s %d W", p > 5 ? LV_SYMBOL_DOWN : p < -5 ? LV_SYMBOL_UP : "", (int)lroundf(fabsf(p)));
    else        snprintf(buf, n, "%d W", (int)lroundf(p));
}

// Subtle breathing of a label (text opacity 100 % <-> 65 %) while the charger is charging.
struct Breath { lv_anim_t anim; bool on = false; };
static void breath_cb(void *o, int32_t v) { lv_obj_set_style_text_opa((lv_obj_t *)o, v, 0); }
static void set_breath(Breath &b, lv_obj_t *label, bool on)
{
    if (on == b.on) return;
    b.on = on;
    lv_anim_del(label, breath_cb);
    if (!on) { lv_obj_set_style_text_opa(label, LV_OPA_COVER, 0); return; }
    lv_anim_init(&b.anim);
    lv_anim_set_var(&b.anim, label);
    lv_anim_set_exec_cb(&b.anim, breath_cb);
    lv_anim_set_values(&b.anim, LV_OPA_COVER, LV_OPA_60 + 10);
    lv_anim_set_time(&b.anim, 1200);
    lv_anim_set_playback_time(&b.anim, 1200);
    lv_anim_set_repeat_count(&b.anim, LV_ANIM_REPEAT_INFINITE);
    lv_anim_set_path_cb(&b.anim, lv_anim_path_ease_in_out);
    lv_anim_start(&b.anim);
}
static Breath s_breath_main, s_breath_tile, s_breath_main_w, s_breath_tile_w;

// --- animated flow: a dot travelling along a straight line -------------------------------
struct Flow {
    lv_obj_t *dot = nullptr;
    int x1, y1, x2, y2;      // path (dot top-left coordinates)
    int dir = 0;             // 0 = off, +1 = x1y1 -> x2y2, -1 = reverse
    lv_anim_t ax, ay;
};
static void flow_x_cb(void *o, int32_t v) { lv_obj_set_x((lv_obj_t *)o, v); }
static void flow_y_cb(void *o, int32_t v) { lv_obj_set_y((lv_obj_t *)o, v); }
static void flow_init(Flow &f, lv_obj_t *parent, int x1, int y1, int x2, int y2, lv_color_t c)
{
    f.dot = make_dot(parent, 8, c);
    f.x1 = x1; f.y1 = y1; f.x2 = x2; f.y2 = y2;
    lv_obj_add_flag(f.dot, LV_OBJ_FLAG_HIDDEN);
}
static void flow_set(Flow &f, int dir, uint32_t ms = 1000)
{
    if (dir == f.dir) return;
    lv_anim_del(f.dot, flow_x_cb);
    lv_anim_del(f.dot, flow_y_cb);
    f.dir = dir;
    if (!dir) { lv_obj_add_flag(f.dot, LV_OBJ_FLAG_HIDDEN); return; }
    lv_obj_clear_flag(f.dot, LV_OBJ_FLAG_HIDDEN);
    const int sx = dir > 0 ? f.x1 : f.x2, ex = dir > 0 ? f.x2 : f.x1;
    const int sy = dir > 0 ? f.y1 : f.y2, ey = dir > 0 ? f.y2 : f.y1;
    lv_anim_init(&f.ax);
    lv_anim_set_var(&f.ax, f.dot);
    lv_anim_set_time(&f.ax, ms);
    lv_anim_set_repeat_count(&f.ax, LV_ANIM_REPEAT_INFINITE);
    lv_anim_set_path_cb(&f.ax, lv_anim_path_ease_in_out);
    f.ay = f.ax;
    lv_anim_set_exec_cb(&f.ax, flow_x_cb); lv_anim_set_values(&f.ax, sx, ex); lv_anim_start(&f.ax);
    lv_anim_set_exec_cb(&f.ay, flow_y_cb); lv_anim_set_values(&f.ay, sy, ey); lv_anim_start(&f.ay);
}

static lv_obj_t *make_line(lv_obj_t *parent, lv_point_t *pts, int n, lv_color_t c)
{
    lv_obj_t *l = lv_line_create(parent);
    lv_line_set_points(l, pts, n);
    lv_obj_set_style_line_width(l, 2, 0);
    lv_obj_set_style_line_color(l, c, 0);
    lv_obj_set_style_line_rounded(l, true, 0);
    return l;
}

// --- widgets -----------------------------------------------------------------------------
static lv_obj_t *s_main_scr, *s_tv;
static lv_obj_t *s_title, *s_icon_wifi, *s_icon_gx, *s_icon_can;
static lv_obj_t *s_dots[PAGES];

// page 0
static lv_obj_t *s_arc, *s_arc_cur, *s_arc_cur2, *s_soc, *s_soc_unit, *s_batt_power, *s_batt_sub;
static lv_anim_t s_cur_pulse;
static bool s_cur_pulsing = false;
static lv_obj_t *s_chg_state, *s_chg_current, *s_chg_volt, *s_chg_watt, *s_chg_temp, *s_chg_limit;
static lv_obj_t *s_sys_loads, *s_sys_source, *s_sys_state;
static Flow s_flow0;
static int32_t s_arc_shown = 0;

// page 1 (overview)
static lv_obj_t *s_ov_in_src, *s_ov_in_w, *s_ov_q_state, *s_ov_q_sub, *s_ov_loads_w, *s_ov_loads_sub;
static lv_obj_t *s_ov_bat_soc, *s_ov_bat_w, *s_ov_bat_sub, *s_ov_bat_arc, *s_ov_chg_name, *s_ov_chg_a, *s_ov_chg_sub;
static Flow s_fl_in, s_fl_out, s_fl_bat, s_fl_chg;

// page 2 (battery)
static lv_obj_t *s_b_soc, *s_b_arc;
static KV s_b_v, s_b_a, s_b_w, s_b_t, s_b_ttg, s_b_soh, s_b_mod, s_b_ccl, s_b_cvl, s_b_dcl, s_b_cell, s_b_block;

// page 3 (charger details)
static lv_obj_t *s_c_name, *s_c_state;
static KV s_c_model, s_c_fw, s_c_out, s_c_cur, s_c_lim, s_c_sent, s_c_ack, s_c_temp, s_c_ac, s_c_acf, s_c_fault, s_c_reason;

// page 4 (status)
static KV s_s_wifi, s_s_ip, s_s_mqtt, s_s_age, s_s_can, s_s_up, s_s_heap, s_s_fw, s_s_inst;

// pulsing of the connecting icon
static lv_anim_t s_pulse_anim;
static lv_obj_t *s_pulsing = nullptr;
static void pulse_cb(void *obj, int32_t v) { lv_obj_set_style_opa((lv_obj_t *)obj, v, 0); }
static void update_pulse()
{
    lv_obj_t *want = nullptr;
    if (model.wifi == LinkState::Connecting) want = s_icon_wifi;
    else if (model.mqtt == LinkState::Connecting) want = s_icon_gx;
    if (want == s_pulsing) return;
    if (s_pulsing) { lv_anim_del(s_pulsing, pulse_cb); lv_obj_set_style_opa(s_pulsing, LV_OPA_COVER, 0); }
    s_pulsing = want;
    if (!want) return;
    lv_anim_init(&s_pulse_anim);
    lv_anim_set_var(&s_pulse_anim, want);
    lv_anim_set_exec_cb(&s_pulse_anim, pulse_cb);
    lv_anim_set_values(&s_pulse_anim, LV_OPA_COVER, LV_OPA_30);
    lv_anim_set_time(&s_pulse_anim, 600);
    lv_anim_set_playback_time(&s_pulse_anim, 600);
    lv_anim_set_repeat_count(&s_pulse_anim, LV_ANIM_REPEAT_INFINITE);
    lv_anim_start(&s_pulse_anim);
}
static void set_link_color(lv_obj_t *obj, LinkState st, bool is_badge)
{
    lv_color_t c = st == LinkState::Up ? C_GREEN : st == LinkState::Connecting ? C_ORANGE : C_TRACK;
    if (is_badge) lv_obj_set_style_bg_color(obj, c, 0);
    else          lv_obj_set_style_text_color(obj, c, 0);
}

static void arc_anim_cb(void *, int32_t v) { s_arc_shown = v; lv_arc_set_value(s_arc, v); }

static void cur_pulse_cb(void *, int32_t v)
{
    lv_obj_set_style_arc_opa(s_arc_cur, v, LV_PART_INDICATOR);
    lv_obj_set_style_arc_opa(s_arc_cur2, v, LV_PART_INDICATOR);
}

// Total-current ring inputs shared by page 0 and the screensaver.
struct RingData { float c1 = 0, c2 = 0, scale = 60; bool charging = false; };
static RingData ring_data(const Model &m, const Sample &own_cur, bool chg_fresh)
{
    RingData r;
    float scale = 0;
    bool any = false;
    for (int i = 0; i < Model::MAX_CHARGERS; i++) {
        const Model::ChargerSlot &s = m.chargers[i];
        if (s.instance < 0) continue;
        float c = (s.instance == m.charger_instance && own_cur.has()) ? own_cur.v : s.current.v;   // own unit: newest source
        if (i == 0) r.c1 = c; else r.c2 = c;
        if (c > 0.5f) any = true;
        if (s.limit.has()) scale += s.limit.v;
    }
    if (m.chargers[0].instance < 0 && own_cur.has()) { r.c1 = own_cur.v; any = own_cur.v > 0.5f; }   // CAN only, Cerbo unseen
    r.scale = scale > 0 ? scale : (m.link_limit.has() ? m.link_limit.v : 60);                       // until the limits arrive
    r.charging = any && (chg_fresh || m.venus_fresh());
    return r;
}

static void draw_ring(lv_obj_t *top, lv_obj_t *below, const RingData &r)
{
    float c1 = r.c1 < 0 ? 0 : r.c1, c2 = r.c2 < 0 ? 0 : r.c2, scale = r.scale < 10 ? 10 : r.scale;
    const int32_t range = (int32_t)lroundf(scale * 10);
    lv_arc_set_range(below, 0, range);
    lv_arc_set_range(top, 0, range);
    float total = c1 + c2; if (total > scale) total = scale;
    lv_arc_set_value(below, (int32_t)lroundf(total * 10));
    lv_arc_set_value(top, (int32_t)lroundf((c1 > scale ? scale : c1) * 10));
}
// c1 = first charger (lowest instance), c2 = second, scale = sum of their configured limits.
static void set_current_ring(float c1, float c2, float scale, bool charging)
{
    c1 = c1 < 0 ? 0 : c1; c2 = c2 < 0 ? 0 : c2;
    if (scale < 10) scale = 10;
    const int32_t range = (int32_t)lroundf(scale * 10);
    lv_arc_set_range(s_arc_cur2, 0, range);
    lv_arc_set_range(s_arc_cur, 0, range);
    float total = c1 + c2; if (total > scale) total = scale;
    lv_arc_set_value(s_arc_cur2, (int32_t)lroundf(total * 10));
    lv_arc_set_value(s_arc_cur, (int32_t)lroundf((c1 > scale ? scale : c1) * 10));
    if (charging == s_cur_pulsing) return;
    s_cur_pulsing = charging;
    lv_anim_del(nullptr, cur_pulse_cb);
    if (!charging) { cur_pulse_cb(nullptr, LV_OPA_COVER); return; }
    lv_anim_init(&s_cur_pulse);
    lv_anim_set_var(&s_cur_pulse, s_arc_cur);
    lv_anim_set_exec_cb(&s_cur_pulse, cur_pulse_cb);
    lv_anim_set_values(&s_cur_pulse, LV_OPA_COVER, LV_OPA_40);
    lv_anim_set_time(&s_cur_pulse, 900);
    lv_anim_set_playback_time(&s_cur_pulse, 900);
    lv_anim_set_repeat_count(&s_cur_pulse, LV_ANIM_REPEAT_INFINITE);
    lv_anim_set_path_cb(&s_cur_pulse, lv_anim_path_ease_in_out);
    lv_anim_start(&s_cur_pulse);
}

// --- page 0: charger ---------------------------------------------------------------------
static void build_page_charger(lv_obj_t *pg)
{
    constexpr int ARC_X = 14, ARC_Y = 14, ARC_D = 200;
    constexpr int COL_X = 232, COL_W = UI_W - COL_X - 10;

    s_arc = make_arc(pg, ARC_X, ARC_Y, ARC_D, 14);
    // inner ring: total charging current of all chargers. Full scale = sum of the per-unit limits
    // configured in the Venus driver (Settings/ChargeCurrentLimit). Two stacked segments:
    // s_arc_cur2 draws 0..(c1+c2) in the second charger's colour underneath, s_arc_cur draws 0..c1
    // in the first charger's colour on top. Pulsates while charging. No label of its own.
    s_arc_cur2 = make_arc(pg, ARC_X + 22, ARC_Y + 22, ARC_D - 44, 8);
    lv_obj_set_style_arc_color(s_arc_cur2, C_CARD, LV_PART_MAIN);
    lv_obj_set_style_arc_color(s_arc_cur2, C_LIGHTBLUE, LV_PART_INDICATOR);
    s_arc_cur = make_arc(pg, ARC_X + 22, ARC_Y + 22, ARC_D - 44, 8);
    lv_obj_set_style_arc_opa(s_arc_cur, LV_OPA_TRANSP, LV_PART_MAIN);   // track drawn by the one below
    lv_obj_set_style_arc_color(s_arc_cur, C_GREEN, LV_PART_INDICATOR);
    s_soc = make_label(pg, &lv_font_montserrat_48, C_TEXT, "--");
    lv_obj_align_to(s_soc, s_arc, LV_ALIGN_CENTER, -8, -14);
    s_soc_unit = make_label(pg, &lv_font_montserrat_20, C_DIM, "%");
    lv_obj_align_to(s_soc_unit, s_soc, LV_ALIGN_OUT_RIGHT_BOTTOM, 2, -8);
    s_batt_power = make_label(pg, &lv_font_montserrat_20, C_DIM, "");
    lv_obj_align_to(s_batt_power, s_arc, LV_ALIGN_CENTER, 0, 30);
    s_batt_sub = make_label(pg, &lv_font_montserrat_12, C_DIM, "Battery");
    lv_obj_align_to(s_batt_sub, s_arc, LV_ALIGN_BOTTOM_MID, 0, -6);

    flow_init(s_flow0, pg, COL_X - 4, ARC_Y + ARC_D / 2 - 4, ARC_X + ARC_D - 8, ARC_Y + ARC_D / 2 - 4, C_GREEN);

    lv_obj_t *card = make_card(pg, COL_X, 8, COL_W, 146);
    lv_obj_t *t = make_label(card, &lv_font_montserrat_12, C_DIM, "CHARGER");
    lv_obj_set_pos(t, 0, 0);
    s_chg_state = make_label(card, &lv_font_montserrat_12, C_DIM, "");
    lv_obj_align(s_chg_state, LV_ALIGN_TOP_RIGHT, 0, 0);
    s_chg_current = make_label(card, &lv_font_montserrat_40, C_TEXT, "-- A");
    lv_obj_set_pos(s_chg_current, 0, 14);
    s_chg_watt = make_label(card, &lv_font_montserrat_40, C_TEXT, "-- W");      // same style, below the amperes
    lv_obj_set_pos(s_chg_watt, 0, 58);
    s_chg_volt = make_label(card, &lv_font_montserrat_14, C_DIM, "");
    lv_obj_set_pos(s_chg_volt, 0, 106);
    s_chg_temp = make_label(card, &lv_font_montserrat_14, C_DIM, "");
    lv_obj_align(s_chg_temp, LV_ALIGN_TOP_RIGHT, 0, 106);
    s_chg_limit = make_label(card, &lv_font_montserrat_14, C_DIM, "");
    lv_obj_align(s_chg_limit, LV_ALIGN_TOP_MID, 0, 106);

    card = make_card(pg, COL_X, 160, COL_W, 82);
    t = make_label(card, &lv_font_montserrat_12, C_DIM, "LOADS");
    lv_obj_set_pos(t, 0, 0);
    s_sys_loads = make_label(card, &lv_font_montserrat_20, C_TEXT, "-- W");
    lv_obj_set_pos(s_sys_loads, 0, 16);
    t = make_label(card, &lv_font_montserrat_12, C_DIM, "AC IN");
    lv_obj_set_pos(t, 100, 0);
    s_sys_source = make_label(card, &lv_font_montserrat_20, C_TEXT, "--");
    lv_obj_set_pos(s_sys_source, 100, 16);
    s_sys_state = make_label(card, &lv_font_montserrat_12, C_DIM, "");
    lv_obj_align(s_sys_state, LV_ALIGN_TOP_RIGHT, 0, 0);
}

static void update_page_charger(const Model &m, bool fresh)
{
    char buf[64];
    lv_color_t val_col = fresh ? C_TEXT : C_TRACK;
    if (m.soc.has()) {
        int32_t target = (int32_t)lroundf(m.soc.v * 10);
        if (target != s_arc_shown) {
            lv_anim_t a;
            lv_anim_init(&a);
            lv_anim_set_exec_cb(&a, arc_anim_cb);
            lv_anim_set_values(&a, s_arc_shown, target);
            lv_anim_set_time(&a, 600);
            lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
            lv_anim_start(&a);
        }
        snprintf(buf, sizeof buf, "%d", (int)lroundf(m.soc.v));
        lv_label_set_text(s_soc, buf);
        lv_obj_align_to(s_soc, s_arc, LV_ALIGN_CENTER, -8, -14);
        lv_obj_align_to(s_soc_unit, s_soc, LV_ALIGN_OUT_RIGHT_BOTTOM, 2, -8);
        lv_obj_set_style_text_color(s_soc, val_col, 0);
        lv_obj_set_style_arc_color(s_arc, soc_color(m.soc.v), LV_PART_INDICATOR);
    }
    if (m.batt_power.has()) {
        float p = m.batt_power.v;
        fmt_power(buf, sizeof buf, p);
        lv_label_set_text(s_batt_power, buf);
        lv_obj_set_style_text_color(s_batt_power, p > 5 ? C_GREEN : p < -5 ? C_ORANGE : C_DIM, 0);
        lv_obj_align_to(s_batt_power, s_arc, LV_ALIGN_CENTER, 0, 30);
        lv_label_set_text(s_batt_sub, p > 5 ? "charging" : p < -5 ? "discharging" : "idle");
        lv_obj_align_to(s_batt_sub, s_arc, LV_ALIGN_BOTTOM_MID, 0, -6);
    }
    lv_label_set_text(s_chg_state, charger_state_text());
    lv_obj_align(s_chg_state, LV_ALIGN_TOP_RIGHT, 0, 0);
    const Sample &cur = live_current(m), &vol = live_voltage(m), &pwr = live_power(m), &tmp = live_temp(m);
    const bool chg_fresh = charger_data_fresh(m);
    const bool running = cur.has() && cur.v > 0.5f && chg_fresh;
    if (cur.has()) { snprintf(buf, sizeof buf, "%.1f A", cur.v); lv_label_set_text(s_chg_current, buf); }
    else lv_label_set_text(s_chg_current, "-- A");
    lv_obj_set_style_text_color(s_chg_current, running ? C_GREEN : chg_fresh ? C_TEXT : C_TRACK, 0);
    set_breath(s_breath_main, s_chg_current, running);
    if (vol.has()) { snprintf(buf, sizeof buf, "%.1f V", vol.v); lv_label_set_text(s_chg_volt, buf); }
    if (pwr.has()) { snprintf(buf, sizeof buf, "%d W", (int)lroundf(pwr.v)); lv_label_set_text(s_chg_watt, buf); }
    else lv_label_set_text(s_chg_watt, "-- W");
    lv_obj_set_style_text_color(s_chg_watt, running ? C_GREEN : chg_fresh ? C_TEXT : C_TRACK, 0);
    set_breath(s_breath_main_w, s_chg_watt, running);
    if (tmp.has()) {
        snprintf(buf, sizeof buf, "%d °C", (int)lroundf(tmp.v));
        lv_label_set_text(s_chg_temp, buf);
        lv_obj_align(s_chg_temp, LV_ALIGN_TOP_RIGHT, 0, 106);
        lv_obj_set_style_text_color(s_chg_temp, tmp.v > 70 ? C_RED : tmp.v > 55 ? C_ORANGE : C_DIM, 0);
    }
    if (m.link_limit.has()) {
        snprintf(buf, sizeof buf, "lim %d A", (int)lroundf(m.link_limit.v));
        lv_label_set_text(s_chg_limit, buf);
        lv_obj_align(s_chg_limit, LV_ALIGN_TOP_MID, 0, 106);
    }
    {
        const RingData r = ring_data(m, cur, chg_fresh);
        set_current_ring(r.c1, r.c2, r.scale, r.charging);
    }
    if (m.consumption.has()) { snprintf(buf, sizeof buf, "%d W", (int)lroundf(m.consumption.v)); lv_label_set_text(s_sys_loads, buf); }
    lv_obj_set_style_text_color(s_sys_loads, val_col, 0);
    {
        const int src = m.active_in_source.has() ? (int)m.active_in_source.v : 240;
        const bool ac_in = src == 1 || src == 2 || src == 3;
        if (ac_in && m.ac_in_power.has()) snprintf(buf, sizeof buf, "%d W", (int)lroundf(m.ac_in_power.v));
        else snprintf(buf, sizeof buf, "-- W");
        lv_label_set_text(s_sys_source, buf);
        lv_obj_set_style_text_color(s_sys_source, val_col, 0);
    }
    if (m.system_state.has()) { lv_label_set_text(s_sys_state, system_state_text((int)m.system_state.v)); lv_obj_align(s_sys_state, LV_ALIGN_TOP_RIGHT, 0, 0); }
}

// --- page 1: system overview (gui-v2 overview idiom: boxes and flows) ----------------------
// Layout (page coords):   [AC IN]--->[QUATTRO]--->[LOADS]      row 1, y 14..86
//                         [BATTERY]<--|      [CHARGER]--->    row 2, y 134..242
static void build_page_overview(lv_obj_t *pg)
{
    constexpr int BW = 128, BH = 72, BY = 14;
    constexpr int X_IN = 10, X_Q = 164, X_LD = 318;
    constexpr int BY2 = 100, BH2 = 142, X_BAT = 10, X_CHG = 318, BW2 = 128;
    constexpr int CY = BY + BH / 2;

    static lv_point_t l_in[]  = {{X_IN + BW, CY}, {X_Q, CY}};
    static lv_point_t l_out[] = {{X_Q + BW, CY}, {X_LD, CY}};
    static lv_point_t l_bat[] = {{X_Q + BW / 2, BY + BH}, {X_Q + BW / 2, BY2 + 50}, {X_BAT + BW2, BY2 + 50}};
    static lv_point_t l_chg[] = {{X_CHG, BY2 + 100}, {X_BAT + BW2, BY2 + 100}};
    make_line(pg, l_in, 2, C_TRACK);
    make_line(pg, l_out, 2, C_TRACK);
    make_line(pg, l_bat, 3, C_TRACK);
    make_line(pg, l_chg, 2, C_TRACK);

    lv_obj_t *b, *t;
    b = make_card(pg, X_IN, BY, BW, BH);
    t = make_label(b, &lv_font_montserrat_12, C_DIM, "AC IN"); lv_obj_set_pos(t, 0, 0);
    s_ov_in_w = make_label(b, &lv_font_montserrat_24, C_TEXT, "-- W"); lv_obj_set_pos(s_ov_in_w, 0, 16);
    s_ov_in_src = make_label(b, &lv_font_montserrat_12, C_DIM, ""); lv_obj_set_pos(s_ov_in_src, 0, 44);

    b = make_card(pg, X_Q, BY, BW, BH);
    t = make_label(b, &lv_font_montserrat_12, C_DIM, "QUATTRO"); lv_obj_set_pos(t, 0, 0);
    s_ov_q_state = make_label(b, &lv_font_montserrat_16, C_TEXT, "--"); lv_obj_set_pos(s_ov_q_state, 0, 16);
    lv_obj_set_width(s_ov_q_state, BW - 20); lv_label_set_long_mode(s_ov_q_state, LV_LABEL_LONG_DOT);
    s_ov_q_sub = make_label(b, &lv_font_montserrat_12, C_DIM, ""); lv_obj_set_pos(s_ov_q_sub, 0, 38);

    b = make_card(pg, X_LD, BY, BW, BH);
    t = make_label(b, &lv_font_montserrat_12, C_DIM, "AC LOADS"); lv_obj_set_pos(t, 0, 0);
    s_ov_loads_w = make_label(b, &lv_font_montserrat_24, C_TEXT, "--"); lv_obj_set_pos(s_ov_loads_w, 0, 16);
    s_ov_loads_sub = make_label(b, &lv_font_montserrat_12, C_DIM, ""); lv_obj_set_pos(s_ov_loads_sub, 0, 44);

    b = make_card(pg, X_BAT, BY2, BW2, BH2);
    t = make_label(b, &lv_font_montserrat_12, C_DIM, "BATTERY"); lv_obj_set_pos(t, 0, 0);
    s_ov_bat_arc = make_arc(b, 22, 14, 64, 7);
    s_ov_bat_soc = make_label(b, &lv_font_montserrat_16, C_TEXT, "--");
    lv_obj_align_to(s_ov_bat_soc, s_ov_bat_arc, LV_ALIGN_CENTER, 0, 0);
    s_ov_bat_w = make_label(b, &lv_font_montserrat_20, C_TEXT, ""); lv_obj_set_pos(s_ov_bat_w, 0, 82);
    s_ov_bat_sub = make_label(b, &lv_font_montserrat_12, C_DIM, ""); lv_obj_set_pos(s_ov_bat_sub, 0, 106);

    b = make_card(pg, X_CHG, BY2, BW2, BH2);
    s_ov_chg_name = make_label(b, &lv_font_montserrat_12, C_DIM, "CHARGER"); lv_obj_set_pos(s_ov_chg_name, 0, 0);
    s_ov_chg_a = make_label(b, &lv_font_montserrat_32, C_TEXT, "-- A"); lv_obj_set_pos(s_ov_chg_a, 0, 24);
    s_ov_chg_sub = make_label(b, &lv_font_montserrat_12, C_DIM, ""); lv_obj_set_pos(s_ov_chg_sub, 0, 106);
    lv_obj_set_width(s_ov_chg_sub, BW2 - 20); lv_label_set_long_mode(s_ov_chg_sub, LV_LABEL_LONG_DOT);

    flow_init(s_fl_in,  pg, X_IN + BW - 4, CY - 4, X_Q - 4, CY - 4, C_ORANGE);
    flow_init(s_fl_out, pg, X_Q + BW - 4, CY - 4, X_LD - 4, CY - 4, C_BLUE);
    flow_init(s_fl_bat, pg, X_Q + BW / 2 - 4, BY + BH - 4, X_Q + BW / 2 - 4, BY2 + 46, C_BLUE);
    flow_init(s_fl_chg, pg, X_CHG - 4, BY2 + 96, X_BAT + BW2 - 4, BY2 + 96, C_GREEN);
}

static void update_page_overview(const Model &m, bool fresh)
{
    char buf[48];
    lv_color_t val_col = fresh ? C_TEXT : C_TRACK;
    const int src = m.active_in_source.has() ? (int)m.active_in_source.v : 240;
    const bool ac_in = src == 1 || src == 2 || src == 3;
    lv_label_set_text(s_ov_in_src, ac_in ? source_text(src) : "");
    if (ac_in && m.ac_in_power.has()) { fmt_power(buf, sizeof buf, m.ac_in_power.v, false); lv_label_set_text(s_ov_in_w, buf); }
    else lv_label_set_text(s_ov_in_w, "-- W");
    lv_obj_set_style_text_color(s_ov_in_w, val_col, 0);
    flow_set(s_fl_in, ac_in && m.ac_in_power.has() && fabsf(m.ac_in_power.v) > 20 ? 1 : 0, 1200);

    if (m.vebus_state.has()) lv_label_set_text(s_ov_q_state, system_state_text((int)m.vebus_state.v));
    if (m.vebus_out_v.has() && m.vebus_out_f.has()) { snprintf(buf, sizeof buf, "out %.0f V %.1f Hz", m.vebus_out_v.v, m.vebus_out_f.v); lv_label_set_text(s_ov_q_sub, buf); }
    lv_obj_set_style_text_color(s_ov_q_state, val_col, 0);

    if (m.consumption.has()) { fmt_power(buf, sizeof buf, m.consumption.v, false); lv_label_set_text(s_ov_loads_w, buf); }
    lv_obj_set_style_text_color(s_ov_loads_w, val_col, 0);
    if (m.dc_system_power.has() && fabsf(m.dc_system_power.v) > 5) { snprintf(buf, sizeof buf, "DC %d W", (int)lroundf(m.dc_system_power.v)); lv_label_set_text(s_ov_loads_sub, buf); }
    else lv_label_set_text(s_ov_loads_sub, m.vebus_mode.has() ? vebus_mode_text((int)m.vebus_mode.v) : "");
    flow_set(s_fl_out, m.consumption.has() && m.consumption.v > 20 ? 1 : 0, 1200);

    if (m.soc.has()) {
        lv_arc_set_value(s_ov_bat_arc, (int32_t)lroundf(m.soc.v * 10));
        lv_obj_set_style_arc_color(s_ov_bat_arc, soc_color(m.soc.v), LV_PART_INDICATOR);
        snprintf(buf, sizeof buf, "%d", (int)lroundf(m.soc.v));
        lv_label_set_text(s_ov_bat_soc, buf);
        lv_obj_align_to(s_ov_bat_soc, s_ov_bat_arc, LV_ALIGN_CENTER, 0, 0);
    }
    const float bp = m.batt_power.has() ? m.batt_power.v : 0;
    if (m.batt_power.has()) { fmt_power(buf, sizeof buf, bp); lv_label_set_text(s_ov_bat_w, buf); }
    lv_obj_set_style_text_color(s_ov_bat_w, bp > 5 ? C_GREEN : bp < -5 ? C_ORANGE : C_DIM, 0);
    if (m.batt_voltage.has() && m.batt_current.has()) { snprintf(buf, sizeof buf, "%.1f V  %.0f A", m.batt_voltage.v, m.batt_current.v); lv_label_set_text(s_ov_bat_sub, buf); }
    // battery <-> Quattro: the battery flow minus what the charger puts in
    const float chg_w = m.chg_power.has() ? m.chg_power.v : 0;
    const float inv_w = bp - chg_w;                 // >0: Quattro charging the battery, <0: battery feeding the inverter
    flow_set(s_fl_bat, fabsf(inv_w) > 20 ? (inv_w > 0 ? 1 : -1) : 0, 1200);

    lv_label_set_text(s_ov_chg_name, m.charger_name);
    const Sample &cur = live_current(m), &pwr = live_power(m);
    const bool running = cur.has() && cur.v > 0.5f && charger_data_fresh(m);
    if (cur.has()) { snprintf(buf, sizeof buf, "%.1f A", cur.v); lv_label_set_text(s_ov_chg_a, buf); }
    lv_obj_set_style_text_color(s_ov_chg_a, running ? C_GREEN : val_col, 0);
    if (running && pwr.has()) snprintf(buf, sizeof buf, "%d W · %s", (int)lroundf(pwr.v), charger_state_text());
    else snprintf(buf, sizeof buf, "%s", charger_state_text());
    lv_label_set_text(s_ov_chg_sub, buf);
    flow_set(s_fl_chg, running ? 1 : 0, 1000);
}

// --- page 2: battery details -------------------------------------------------------------
static void build_page_battery(lv_obj_t *pg)
{
    lv_obj_t *t = make_label(pg, &lv_font_montserrat_12, C_DIM, "PYLONTECH BATTERY"); lv_obj_set_pos(t, 12, 6);
    s_b_arc = make_arc(pg, 12, 26, 120, 10);
    s_b_soc = make_label(pg, &lv_font_montserrat_32, C_TEXT, "--");
    lv_obj_align_to(s_b_soc, s_b_arc, LV_ALIGN_CENTER, 0, 0);
    s_b_w   = make_kv(pg, 12, 156, "POWER");
    s_b_ttg = make_kv(pg, 12, 200, "TIME TO GO");

    constexpr int C1 = 160, C2 = 310, R0 = 6, DR = 48;
    s_b_v    = make_kv(pg, C1, R0 + 0 * DR, "VOLTAGE");
    s_b_a    = make_kv(pg, C2, R0 + 0 * DR, "CURRENT");
    s_b_t    = make_kv(pg, C1, R0 + 1 * DR, "TEMPERATURE");
    s_b_soh  = make_kv(pg, C2, R0 + 1 * DR, "STATE OF HEALTH");
    s_b_ccl  = make_kv(pg, C1, R0 + 2 * DR, "CHARGE LIMIT (BMS)");
    s_b_cvl  = make_kv(pg, C2, R0 + 2 * DR, "CHARGE VOLTAGE (BMS)");
    s_b_dcl  = make_kv(pg, C1, R0 + 3 * DR, "DISCHARGE LIMIT (BMS)");
    s_b_mod  = make_kv(pg, C2, R0 + 3 * DR, "MODULES ONLINE");
    s_b_cell = make_kv(pg, C1, R0 + 4 * DR, "CELL MIN / MAX");
    s_b_block = make_kv(pg, C2, R0 + 4 * DR, "BLOCKING CHG / DIS");
}

static void update_page_battery(const Model &m, bool)
{
    char buf[48];
    if (m.soc.has()) {
        lv_arc_set_value(s_b_arc, (int32_t)lroundf(m.soc.v * 10));
        lv_obj_set_style_arc_color(s_b_arc, soc_color(m.soc.v), LV_PART_INDICATOR);
        snprintf(buf, sizeof buf, "%d%%", (int)lroundf(m.soc.v));
        lv_label_set_text(s_b_soc, buf);
        lv_obj_align_to(s_b_soc, s_b_arc, LV_ALIGN_CENTER, 0, 0);
    }
    if (m.batt_power.has()) { fmt_power(buf, sizeof buf, m.batt_power.v); lv_label_set_text(s_b_w.val, buf); }
    if (m.batt_time_to_go.has() && m.batt_time_to_go.v > 0) {
        int s = (int)m.batt_time_to_go.v;
        snprintf(buf, sizeof buf, "%dh %02dmin", s / 3600, (s % 3600) / 60);
        lv_label_set_text(s_b_ttg.val, buf);
    } else lv_label_set_text(s_b_ttg.val, m.batt_power.has() && m.batt_power.v >= 0 ? "charging" : "--");
    kv_set(s_b_v, m.batt_voltage, "%.2f V");
    kv_set(s_b_a, m.batt_current, "%.1f A");
    kv_set(s_b_t, m.batt_temp, "%.0f °C");
    kv_set(s_b_soh, m.bms_soh, "%.0f %%");
    kv_set(s_b_ccl, m.bms_ccl, "%.0f A");
    kv_set(s_b_cvl, m.bms_cvl, "%.1f V");
    kv_set(s_b_dcl, m.bms_dcl, "%.0f A");
    kv_set(s_b_mod, m.bms_modules, "%.0f");
    if (m.bms_cell_min.has() && m.bms_cell_max.has()) { snprintf(buf, sizeof buf, "%.3f / %.3f V", m.bms_cell_min.v, m.bms_cell_max.v); lv_label_set_text(s_b_cell.val, buf); }
    if (m.bms_block_chg.has() && m.bms_block_dis.has()) {
        snprintf(buf, sizeof buf, "%.0f / %.0f", m.bms_block_chg.v, m.bms_block_dis.v);
        lv_label_set_text(s_b_block.val, buf);
        lv_obj_set_style_text_color(s_b_block.val, (m.bms_block_chg.v > 0 || m.bms_block_dis.v > 0) ? C_ORANGE : C_TEXT, 0);
    }
}

// --- page 3: charger details -------------------------------------------------------------
static void build_page_chgdetail(lv_obj_t *pg)
{
    s_c_name = make_label(pg, &lv_font_montserrat_12, C_DIM, "CHARGER"); lv_obj_set_pos(s_c_name, 12, 6);
    s_c_state = make_label(pg, &lv_font_montserrat_12, C_DIM, ""); lv_obj_align(s_c_state, LV_ALIGN_TOP_RIGHT, -12, 6);
    constexpr int C1 = 12, C2 = 162, C3 = 312, R0 = 26, DR = 46;
    s_c_model = make_kv(pg, C1, R0 + 0 * DR, "MODEL");
    s_c_fw    = make_kv(pg, C2, R0 + 0 * DR, "FIRMWARE");
    s_c_temp  = make_kv(pg, C3, R0 + 0 * DR, "TEMPERATURE");
    s_c_out   = make_kv(pg, C1, R0 + 1 * DR, "OUTPUT");
    s_c_cur   = make_kv(pg, C2, R0 + 1 * DR, "CURRENT");
    s_c_lim   = make_kv(pg, C3, R0 + 1 * DR, "DVCC LIMIT");
    s_c_sent  = make_kv(pg, C1, R0 + 2 * DR, "SENT SETPOINT");
    s_c_ack   = make_kv(pg, C2, R0 + 2 * DR, "ACKNOWLEDGED");
    s_c_fault = make_kv(pg, C3, R0 + 2 * DR, "FAULT");
    s_c_ac    = make_kv(pg, C1, R0 + 3 * DR, "AC IN", 240);
    s_c_acf   = make_kv(pg, C3, R0 + 3 * DR, "AC FREQUENCY");
    s_c_reason = make_kv(pg, C1, R0 + 4 * DR, "LIMITING REASON", UI_W - 24);
}

static void update_page_chgdetail(const Model &m, bool)
{
    char buf[48];
    lv_label_set_text(s_c_name, "CHARGER DETAILS");
    lv_label_set_text(s_c_state, charger_state_text());
    lv_obj_align(s_c_state, LV_ALIGN_TOP_RIGHT, -12, 6);
    lv_label_set_text(s_c_model.val, m.charger_model[0] ? m.charger_model : "--");
    lv_label_set_text(s_c_fw.val, m.charger_fw[0] ? m.charger_fw : "--");
    kv_set(s_c_temp, m.chg_temp, "%.0f °C");
    if (m.chg_voltage.has() && m.chg_power.has()) { snprintf(buf, sizeof buf, "%.2f V  %.0f W", m.chg_voltage.v, m.chg_power.v); lv_label_set_text(s_c_out.val, buf); }
    kv_set(s_c_cur, m.chg_current, "%.1f A");
    kv_set(s_c_lim, m.link_limit, "%.1f A");
    kv_set(s_c_sent, m.hw_sent_current, "%.1f A");
    if (m.hw_ack_ok.has()) {
        lv_label_set_text(s_c_ack.val, m.hw_ack_ok.v > 0.5f ? "yes" : "no");
        lv_obj_set_style_text_color(s_c_ack.val, m.hw_ack_ok.v > 0.5f ? C_TEXT : C_ORANGE, 0);
    }
    if (m.hw_fault.has()) {
        lv_label_set_text(s_c_fault.val, m.hw_fault.v > 0.5f ? "FAULT" : "none");
        lv_obj_set_style_text_color(s_c_fault.val, m.hw_fault.v > 0.5f ? C_RED : C_TEXT, 0);
    }
    if (m.ac_voltage.has()) {
        if (m.ac_voltage.v < 50) lv_label_set_text(s_c_ac.val, "off");
        else { snprintf(buf, sizeof buf, "%.0f V  %.1f A  %.0f W", m.ac_voltage.v, m.ac_current.v, m.ac_power.v); lv_label_set_text(s_c_ac.val, buf); }
    }
    kv_set(s_c_acf, m.ac_freq, "%.1f Hz");
    lv_label_set_text(s_c_reason.val, m.limiting_reason[0] ? m.limiting_reason : "none");
}

// --- page 4: status ------------------------------------------------------------------------
extern uint32_t g_wifi_drops;
extern uint32_t g_wifi_roams;
extern int g_wifi_last_reason;

static void build_page_status(lv_obj_t *pg)
{
    lv_obj_t *t = make_label(pg, &lv_font_montserrat_12, C_DIM, "DISPLAY STATUS"); lv_obj_set_pos(t, 12, 6);
    constexpr int C1 = 12, C2 = 162, C3 = 312, R0 = 26, DR = 46;
    s_s_wifi = make_kv(pg, C1, R0 + 0 * DR, "WLAN");
    s_s_ip   = make_kv(pg, C2, R0 + 0 * DR, "IP ADDRESS");
    s_s_can  = make_kv(pg, C3, R0 + 0 * DR, "CAN BUS");
    s_s_mqtt = make_kv(pg, C1, R0 + 1 * DR, "CERBO GX (MQTT)");
    s_s_age  = make_kv(pg, C2, R0 + 1 * DR, "LAST DATA");
    s_s_inst = make_kv(pg, C3, R0 + 1 * DR, "CHARGER SERVICE");
    s_s_up   = make_kv(pg, C1, R0 + 2 * DR, "UPTIME");
    s_s_heap = make_kv(pg, C2, R0 + 2 * DR, "FREE MEMORY");
    s_s_fw   = make_kv(pg, C3, R0 + 2 * DR, "DISPLAY FIRMWARE");
}

static void update_page_status(const Model &m, bool)
{
    char buf[48];
    if (m.wifi == LinkState::Up) snprintf(buf, sizeof buf, "%d dBm · %s", m.wifi_rssi, m.wifi_node);
    else snprintf(buf, sizeof buf, "%s · reason %d", m.wifi == LinkState::Connecting ? "connecting" : "down", g_wifi_last_reason);
    lv_label_set_text(s_s_wifi.val, buf);
    snprintf(buf, sizeof buf, "%lu drops · %lu roams · last %d", (unsigned long)g_wifi_drops, (unsigned long)g_wifi_roams, g_wifi_last_reason);
    lv_label_set_text(s_s_wifi.key, buf);
    lv_label_set_text(s_s_ip.val, m.wifi == LinkState::Up ? m.ip : "--");
    if (m.can == LinkState::Up) snprintf(buf, sizeof buf, "%lu frames", (unsigned long)m.can_frames);
    else snprintf(buf, sizeof buf, "no transceiver");
    lv_label_set_text(s_s_can.val, buf);
    if (m.mqtt == LinkState::Up) snprintf(buf, sizeof buf, "connected · %lu msgs", (unsigned long)m.mqtt_msgs);
    else snprintf(buf, sizeof buf, "%s", m.mqtt == LinkState::Connecting ? "connecting" : "down");
    lv_label_set_text(s_s_mqtt.val, buf);
    if (m.mqtt_last_rx) { uint32_t s = (millis() - m.mqtt_last_rx) / 1000; snprintf(buf, sizeof buf, "%lu s ago", (unsigned long)s); }
    else snprintf(buf, sizeof buf, "never");
    lv_label_set_text(s_s_age.val, buf);
    if (m.charger_instance >= 0) snprintf(buf, sizeof buf, "instance %d", m.charger_instance); else snprintf(buf, sizeof buf, "not seen");
    lv_label_set_text(s_s_inst.val, buf);
    uint32_t up = millis() / 1000;
    snprintf(buf, sizeof buf, "%lud %02lu:%02lu", (unsigned long)(up / 86400), (unsigned long)((up % 86400) / 3600), (unsigned long)((up % 3600) / 60));
    lv_label_set_text(s_s_up.val, buf);
    snprintf(buf, sizeof buf, "%u KB", ESP.getFreeHeap() / 1024);
    lv_label_set_text(s_s_heap.val, buf);
    lv_label_set_text(s_s_fw.val, UI_FW_VERSION);
}

// --- page 5: CAN bus (passive listener) ----------------------------------------------------
static KV s_n_link, s_n_addr, s_n_set, s_n_out, s_n_ac, s_n_temp, s_n_flags, s_n_load, s_n_rated, s_n_ack, s_n_limit, s_n_standby;
static void build_page_can(lv_obj_t *pg)
{
    lv_obj_t *t = make_label(pg, &lv_font_montserrat_12, C_DIM, "CAN BUS (LISTENER)"); lv_obj_set_pos(t, 12, 6);
    constexpr int C1 = 12, C2 = 162, C3 = 312, R0 = 26, DR = 46;
    s_n_link  = make_kv(pg, C1, R0 + 0 * DR, "BUS");
    s_n_addr  = make_kv(pg, C2, R0 + 0 * DR, "CHARGER ADDRESS");
    s_n_rated = make_kv(pg, C3, R0 + 0 * DR, "RATED CURRENT");
    s_n_set   = make_kv(pg, C1, R0 + 1 * DR, "BOARD SETPOINT");
    s_n_ack   = make_kv(pg, C2, R0 + 1 * DR, "LAST ACK");
    s_n_standby = make_kv(pg, C3, R0 + 1 * DR, "STANDBY CMD");
    s_n_out   = make_kv(pg, C1, R0 + 2 * DR, "OUTPUT");
    s_n_limit = make_kv(pg, C2, R0 + 2 * DR, "LIMIT RATIO");
    s_n_load  = make_kv(pg, C3, R0 + 2 * DR, "LOAD RATIO");
    s_n_ac    = make_kv(pg, C1, R0 + 3 * DR, "AC IN", 240);
    s_n_temp  = make_kv(pg, C3, R0 + 3 * DR, "TEMP OUT / IN");
    s_n_flags = make_kv(pg, C1, R0 + 4 * DR, "STATUS BITS", 300);
}
static void update_page_can(const Model &m, bool)
{
    char buf[48];
    if (m.can == LinkState::Up) snprintf(buf, sizeof buf, "%lu frames", (unsigned long)m.can_frames);
    else snprintf(buf, sizeof buf, m.can_frames ? "silent (%lu frames)" : "no frames", (unsigned long)m.can_frames);
    lv_label_set_text(s_n_link.val, buf);
    if (m.can_addr) snprintf(buf, sizeof buf, "%d", m.can_addr); else snprintf(buf, sizeof buf, "--");
    lv_label_set_text(s_n_addr.val, buf);
    kv_set(s_n_rated, m.can_rated_a, "%.0f A");
    if (m.can_set_v.has() || m.can_set_ratio.has()) {
        const float nominal = m.can_rated_a.has() ? m.can_rated_a.v : 0;
        if (nominal > 0 && m.can_set_ratio.has()) snprintf(buf, sizeof buf, "%.1f A  %.2f V  (%lu)", m.can_set_ratio.v * nominal, m.can_set_v.v, (unsigned long)m.can_sets);
        else snprintf(buf, sizeof buf, "ratio %.3f  %.2f V  (%lu)", m.can_set_ratio.v, m.can_set_v.v, (unsigned long)m.can_sets);
        lv_label_set_text(s_n_set.val, buf);
    }
    if (m.can_ack_ok.has()) { snprintf(buf, sizeof buf, "%s (%lu)", m.can_ack_ok.v > 0.5f ? "ok" : "error", (unsigned long)m.can_acks); lv_label_set_text(s_n_ack.val, buf); }
    if (m.can_standby.has()) lv_label_set_text(s_n_standby.val, m.can_standby.v > 0.5f ? "standby" : "run");
    if (m.can_out_v.has() || m.can_out_a.has()) { snprintf(buf, sizeof buf, "%.2f V  %.1f A  %.0f W", m.can_out_v.v, m.can_out_a.v, m.can_out_w.v); lv_label_set_text(s_n_out.val, buf); }
    kv_set(s_n_limit, m.can_limit_ratio, "%.3f");
    kv_set(s_n_load, m.can_load_ratio, "%.3f");
    if (m.can_ac_v.has()) { snprintf(buf, sizeof buf, "%.0f V  %.1f A  %.0f W  %.1f Hz", m.can_ac_v.v, m.can_ac_a.v, m.can_ac_w.v, m.can_ac_f.v); lv_label_set_text(s_n_ac.val, buf); }
    if (m.can_temp_out.has()) { snprintf(buf, sizeof buf, "%.0f / %.0f °C", m.can_temp_out.v, m.can_temp_in.v); lv_label_set_text(s_n_temp.val, buf); }
    if (m.can_output_on.has()) {
        snprintf(buf, sizeof buf, "output %s · AC %s", m.can_output_on.v > 0.5f ? "on" : "off", m.can_ac_present.v > 0.5f ? "present" : "absent");
        lv_label_set_text(s_n_flags.val, buf);
    }
}

// --- screensaver ---------------------------------------------------------------------------
// AMOLED burn-in protection without dimming: black screen (unlit pixels) and the key live values
// in the normal colours, no box, gliding to a new random place every few seconds. Any touch, or
// the BOOT button, returns to the main screen. Starts after SAVER_AFTER_MS without input.
static constexpr uint32_t SAVER_AFTER_MS = 120000;
static constexpr int TILE_W = 380, TILE_H = 160;
static constexpr uint32_t TILE_HOP_MS = 8000;

static lv_obj_t *s_saver_scr, *s_tile;
static lv_obj_t *s_tile_arc, *s_tile_cur1, *s_tile_cur2, *s_tile_soc, *s_tile_name, *s_tile_cur, *s_tile_watt, *s_tile_pwr;
static lv_anim_t s_tile_pulse;
static bool s_tile_pulsing = false;
static bool s_saver_on = false;

static void tile_hop(lv_timer_t *)
{
    if (!s_saver_on) return;
    int nx = (int)random(0, UI_W - TILE_W + 1);
    int ny = (int)random(0, UI_H - TILE_H + 1);
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, s_tile);
    lv_anim_set_time(&a, TILE_HOP_MS - 1500);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_in_out);
    lv_anim_set_exec_cb(&a, flow_x_cb);
    lv_anim_set_values(&a, lv_obj_get_x(s_tile), nx);
    lv_anim_start(&a);
    lv_anim_set_exec_cb(&a, flow_y_cb);
    lv_anim_set_values(&a, lv_obj_get_y(s_tile), ny);
    lv_anim_start(&a);
}

static void saver_show(bool on)
{
    if (on == s_saver_on) return;
    s_saver_on = on;
    lv_scr_load_anim(on ? s_saver_scr : s_main_scr, LV_SCR_LOAD_ANIM_FADE_ON, 400, 0, false);
    if (on) tile_hop(nullptr);
}

static void saver_build()
{
    s_saver_scr = lv_obj_create(nullptr);
    lv_obj_set_style_bg_color(s_saver_scr, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(s_saver_scr, LV_OPA_COVER, 0);
    lv_obj_clear_flag(s_saver_scr, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_saver_scr, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(s_saver_scr, [](lv_event_t *) { saver_show(false); }, LV_EVENT_PRESSED, nullptr);

    s_tile = make_card(s_saver_scr, (UI_W - TILE_W) / 2, (UI_H - TILE_H) / 2, TILE_W, TILE_H);
    lv_obj_set_style_bg_opa(s_tile, LV_OPA_TRANSP, 0);
    lv_obj_set_style_pad_all(s_tile, 0, 0);
    lv_obj_add_flag(s_tile, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(s_tile, [](lv_event_t *) { saver_show(false); }, LV_EVENT_PRESSED, nullptr);

    // SOC ring 150 px with the two-segment total-current ring inside, text block to the right
    s_tile_arc = make_arc(s_tile, 0, 4, 150, 12);
    s_tile_cur2 = make_arc(s_tile, 18, 22, 114, 8);
    lv_obj_set_style_arc_color(s_tile_cur2, C_CARD, LV_PART_MAIN);
    lv_obj_set_style_arc_color(s_tile_cur2, C_LIGHTBLUE, LV_PART_INDICATOR);
    s_tile_cur1 = make_arc(s_tile, 18, 22, 114, 8);
    lv_obj_set_style_arc_opa(s_tile_cur1, LV_OPA_TRANSP, LV_PART_MAIN);
    lv_obj_set_style_arc_color(s_tile_cur1, C_GREEN, LV_PART_INDICATOR);
    s_tile_soc = make_label(s_tile, &lv_font_montserrat_32, C_TEXT, "--");
    lv_obj_align_to(s_tile_soc, s_tile_arc, LV_ALIGN_CENTER, 0, 0);
    s_tile_name = make_label(s_tile, &lv_font_montserrat_16, C_DIM, model.charger_name);
    lv_obj_set_pos(s_tile_name, 172, 6);
    s_tile_cur = make_label(s_tile, &lv_font_montserrat_48, C_TEXT, "-- A");
    lv_obj_set_pos(s_tile_cur, 170, 44);
    s_tile_watt = make_label(s_tile, &lv_font_montserrat_48, C_TEXT, "-- W");   // same size as the amperes
    lv_obj_set_pos(s_tile_watt, 170, 98);
    s_tile_pwr = make_label(s_tile, &lv_font_montserrat_20, C_DIM, "");   // battery total: no longer shown
    lv_obj_add_flag(s_tile_pwr, LV_OBJ_FLAG_HIDDEN);

    lv_timer_create(tile_hop, TILE_HOP_MS, nullptr);
}

static void tile_pulse_cb(void *, int32_t v)
{
    lv_obj_set_style_arc_opa(s_tile_cur1, v, LV_PART_INDICATOR);
    lv_obj_set_style_arc_opa(s_tile_cur2, v, LV_PART_INDICATOR);
}

static void saver_update(const Model &m, bool fresh)
{
    if (!s_saver_on && lv_disp_get_inactive_time(nullptr) > SAVER_AFTER_MS) saver_show(true);
    if (s_saver_on && digitalRead(PIN_BOOT_BTN) == LOW) { lv_disp_trig_activity(nullptr); saver_show(false); }
    if (!s_saver_on) return;
    char buf[32];
    lv_label_set_text(s_tile_name, m.charger_name);
    if (m.soc.has()) {
        lv_arc_set_value(s_tile_arc, (int32_t)lroundf(m.soc.v * 10));
        snprintf(buf, sizeof buf, "%d", (int)lroundf(m.soc.v));
        lv_label_set_text(s_tile_soc, buf);
        lv_obj_align_to(s_tile_soc, s_tile_arc, LV_ALIGN_CENTER, 0, 0);
        lv_obj_set_style_arc_color(s_tile_arc, soc_color(m.soc.v), LV_PART_INDICATOR);
    }
    const Sample &cur = live_current(m);
    const bool chg_fresh = charger_data_fresh(m);
    const bool running = cur.has() && cur.v > 0.5f && chg_fresh;
    if (cur.has()) { snprintf(buf, sizeof buf, "%.1f A", cur.v); lv_label_set_text(s_tile_cur, buf); }
    lv_obj_set_style_text_color(s_tile_cur, running ? C_GREEN : chg_fresh ? C_TEXT : C_TRACK, 0);
    set_breath(s_breath_tile, s_tile_cur, running);
    {
        const Sample &pw = live_power(m);
        if (pw.has()) { snprintf(buf, sizeof buf, "%d W", (int)lroundf(pw.v)); lv_label_set_text(s_tile_watt, buf); }
        lv_obj_set_style_text_color(s_tile_watt, running ? C_GREEN : chg_fresh ? C_TEXT : C_TRACK, 0);
        set_breath(s_breath_tile_w, s_tile_watt, running);
    }
    {
        const RingData r = ring_data(m, cur, chg_fresh);
        draw_ring(s_tile_cur1, s_tile_cur2, r);
        if (r.charging != s_tile_pulsing) {
            s_tile_pulsing = r.charging;
            lv_anim_del(nullptr, tile_pulse_cb);
            if (!r.charging) tile_pulse_cb(nullptr, LV_OPA_COVER);
            else {
                lv_anim_init(&s_tile_pulse);
                lv_anim_set_var(&s_tile_pulse, s_tile_cur1);
                lv_anim_set_exec_cb(&s_tile_pulse, tile_pulse_cb);
                lv_anim_set_values(&s_tile_pulse, LV_OPA_COVER, LV_OPA_40);
                lv_anim_set_time(&s_tile_pulse, 900);
                lv_anim_set_playback_time(&s_tile_pulse, 900);
                lv_anim_set_repeat_count(&s_tile_pulse, LV_ANIM_REPEAT_INFINITE);
                lv_anim_set_path_cb(&s_tile_pulse, lv_anim_path_ease_in_out);
                lv_anim_start(&s_tile_pulse);
            }
        }
    }
    if (m.batt_power.has()) {
        fmt_power(buf, sizeof buf, m.batt_power.v);
        lv_label_set_text(s_tile_pwr, buf);
        lv_obj_set_style_text_color(s_tile_pwr, m.batt_power.v > 5 ? C_GREEN : m.batt_power.v < -5 ? C_ORANGE : C_DIM, 0);
    }
}

// --- page dots -----------------------------------------------------------------------------
static void update_dots()
{
    lv_obj_t *act = lv_tileview_get_tile_act(s_tv);
    for (int i = 0; i < PAGES; i++) {
        bool on = act && (int)lv_obj_get_index(act) == i;
        lv_obj_set_style_bg_color(s_dots[i], on ? C_TEXT : C_TRACK, 0);
    }
}

// --- build -------------------------------------------------------------------------------
void ui_begin()
{
    lv_obj_t *scr = lv_scr_act();
    s_main_scr = scr;
    pinMode(PIN_BOOT_BTN, INPUT_PULLUP);
    lv_obj_set_style_bg_color(scr, C_BG, 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    // status bar
    s_title = make_label(scr, &lv_font_montserrat_14, C_DIM, model.charger_name);
    lv_obj_set_pos(s_title, 12, 7);
    s_icon_can = make_badge(scr, "CAN");
    lv_obj_align(s_icon_can, LV_ALIGN_TOP_RIGHT, -10, 5);
    s_icon_gx = make_badge(scr, "GX");
    lv_obj_align_to(s_icon_gx, s_icon_can, LV_ALIGN_OUT_LEFT_MID, -6, 0);
    s_icon_wifi = make_label(scr, &lv_font_montserrat_16, C_TRACK, LV_SYMBOL_WIFI);
    lv_obj_align_to(s_icon_wifi, s_icon_gx, LV_ALIGN_OUT_LEFT_MID, -8, 0);

    // pages
    s_tv = lv_tileview_create(scr);
    lv_obj_remove_style_all(s_tv);
    lv_obj_set_size(s_tv, UI_W, PAGE_H);
    lv_obj_set_pos(s_tv, 0, BAR_H);
    lv_obj_set_scrollbar_mode(s_tv, LV_SCROLLBAR_MODE_OFF);
    lv_obj_add_event_cb(s_tv, [](lv_event_t *) { update_dots(); }, LV_EVENT_VALUE_CHANGED, nullptr);
    // gestures bubble up to the screen: a swipe towards the left edge while on the first page
    // (finger moving right, where no page exists) starts the screensaver
    // ... and a swipe onwards from the last page wraps around to the first one.
    lv_obj_add_event_cb(scr, [](lv_event_t *) {
        const lv_dir_t dir = lv_indev_get_gesture_dir(lv_indev_get_act());
        lv_obj_t *act = lv_tileview_get_tile_act(s_tv);
        if (!act) return;
        const int idx = (int)lv_obj_get_index(act);
        if (dir == LV_DIR_RIGHT && idx == 0) saver_show(true);
        else if (dir == LV_DIR_LEFT && idx == PAGES - 1) lv_obj_set_tile_id(s_tv, 0, 0, LV_ANIM_OFF);
    }, LV_EVENT_GESTURE, nullptr);

    lv_obj_t *pg;
    pg = lv_tileview_add_tile(s_tv, 0, 0, LV_DIR_RIGHT);  build_page_charger(pg);
    pg = lv_tileview_add_tile(s_tv, 1, 0, LV_DIR_HOR);    build_page_overview(pg);
    pg = lv_tileview_add_tile(s_tv, 2, 0, LV_DIR_HOR);    build_page_battery(pg);
    pg = lv_tileview_add_tile(s_tv, 3, 0, LV_DIR_HOR);    build_page_chgdetail(pg);
    pg = lv_tileview_add_tile(s_tv, 4, 0, LV_DIR_HOR);    build_page_status(pg);
    pg = lv_tileview_add_tile(s_tv, 5, 0, LV_DIR_LEFT);   build_page_can(pg);
    for (int i = 0; i < PAGES; i++) {
        lv_obj_clear_flag(lv_obj_get_child(s_tv, i), LV_OBJ_FLAG_SCROLLABLE);   // no scrolling inside a page
    }

    // page dots
    for (int i = 0; i < PAGES; i++) {
        s_dots[i] = make_dot(scr, 6, C_TRACK);
        lv_obj_set_pos(s_dots[i], UI_W / 2 - (PAGES * 12) / 2 + i * 12 + 3, UI_H - 10);
    }
    update_dots();

    saver_build();

    lv_timer_create([](lv_timer_t *) {
        Model &m = model;
        const bool fresh = m.venus_fresh();

        set_link_color(s_icon_wifi, m.wifi, false);
        set_link_color(s_icon_gx, m.mqtt == LinkState::Up && !fresh ? LinkState::Connecting : m.mqtt, true);
        set_link_color(s_icon_can, m.can, true);
        update_pulse();
        lv_label_set_text(s_title, m.charger_name);

        if (!s_saver_on) {
            lv_obj_t *act = lv_tileview_get_tile_act(s_tv);
            int idx = act ? (int)lv_obj_get_index(act) : 0;
            switch (idx) {
                case 0: update_page_charger(m, fresh); break;
                case 1: update_page_overview(m, fresh); break;
                case 2: update_page_battery(m, fresh); break;
                case 3: update_page_chgdetail(m, fresh); break;
                case 4: update_page_status(m, fresh); break;
                default: update_page_can(m, fresh); break;
            }
            if (idx != 1) { flow_set(s_fl_in, 0); flow_set(s_fl_out, 0); flow_set(s_fl_bat, 0); flow_set(s_fl_chg, 0); }
        }
        saver_update(m, fresh);
    }, 250, nullptr);
}
