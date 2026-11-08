"""One-off patch: remove the footer line and add the screensaver to src/ui.cpp (2026-11-08)."""
import re
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "src" / "ui.cpp"
t = p.read_text(encoding="utf-8")

# 1. footer out
t = t.replace("static lv_obj_t *s_flow_dot;\nstatic lv_obj_t *s_footer;\n", "static lv_obj_t *s_flow_dot;\n")
t = t.replace('''    s_footer = make_label(scr, &lv_font_montserrat_12, C_TRACK, "waiting for Cerbo...");
    lv_obj_align(s_footer, LV_ALIGN_BOTTOM_LEFT, 12, -6);

''', "")
t = t.replace('''        // footer
        if (m.wifi != LinkState::Up)            lv_label_set_text(s_footer, "connecting to WLAN...");
        else if (m.mqtt != LinkState::Up)       lv_label_set_text(s_footer, "connecting to Cerbo...");
        else if (!m.mqtt_last_rx)               lv_label_set_text(s_footer, "waiting for data...");
        else if (!fresh)                        lv_label_set_text(s_footer, "Cerbo data stale");
        else { snprintf(buf, sizeof buf, "live · %lu msgs · %d dBm", (unsigned long)m.mqtt_msgs, m.wifi_rssi); lv_label_set_text(s_footer, buf); }
        lv_obj_align(s_footer, LV_ALIGN_BOTTOM_LEFT, 12, -6);
''', '''        saver_update(m, fresh);
''')
t = t.replace("//   footer (link state)  │ 860 W        No AC in      │", "//                        │ 860 W        No AC in      │")

# 2. screensaver code before the build section
saver = r'''
// --- screensaver ---------------------------------------------------------------------------
// AMOLED burn-in protection without dimming: black screen (unlit pixels) and one compact live
// tile in the normal colours that glides to a new random place every few seconds. Any touch, or
// the BOOT button, returns to the main screen. Starts after SAVER_AFTER_MS without input.
static constexpr uint32_t SAVER_AFTER_MS = 120000;
static constexpr int TILE_W = 196, TILE_H = 104;
static constexpr uint32_t TILE_HOP_MS = 8000;       // one glide every 8 s

static lv_obj_t *s_main_scr, *s_saver_scr, *s_tile;
static lv_obj_t *s_tile_arc, *s_tile_soc, *s_tile_name, *s_tile_cur, *s_tile_pwr;
static bool s_saver_on = false;

static void tile_x_cb(void *o, int32_t v) { lv_obj_set_x((lv_obj_t *)o, v); }
static void tile_y_cb(void *o, int32_t v) { lv_obj_set_y((lv_obj_t *)o, v); }

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
    lv_anim_set_exec_cb(&a, tile_x_cb);
    lv_anim_set_values(&a, lv_obj_get_x(s_tile), nx);
    lv_anim_start(&a);
    lv_anim_set_exec_cb(&a, tile_y_cb);
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
    lv_obj_add_flag(s_tile, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(s_tile, [](lv_event_t *) { saver_show(false); }, LV_EVENT_PRESSED, nullptr);

    s_tile_arc = lv_arc_create(s_tile);
    lv_obj_set_size(s_tile_arc, 76, 76);
    lv_obj_set_pos(s_tile_arc, 0, 4);
    lv_arc_set_rotation(s_tile_arc, 135);
    lv_arc_set_bg_angles(s_tile_arc, 0, 270);
    lv_arc_set_range(s_tile_arc, 0, 1000);
    lv_obj_remove_style(s_tile_arc, nullptr, LV_PART_KNOB);
    lv_obj_clear_flag(s_tile_arc, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_arc_width(s_tile_arc, 8, LV_PART_MAIN);
    lv_obj_set_style_arc_width(s_tile_arc, 8, LV_PART_INDICATOR);
    lv_obj_set_style_arc_rounded(s_tile_arc, true, LV_PART_MAIN);
    lv_obj_set_style_arc_rounded(s_tile_arc, true, LV_PART_INDICATOR);
    lv_obj_set_style_arc_color(s_tile_arc, C_TRACK, LV_PART_MAIN);
    lv_obj_set_style_arc_color(s_tile_arc, C_BLUE, LV_PART_INDICATOR);
    s_tile_soc = make_label(s_tile, &lv_font_montserrat_20, C_TEXT, "--");
    lv_obj_align_to(s_tile_soc, s_tile_arc, LV_ALIGN_CENTER, 0, 0);

    s_tile_name = make_label(s_tile, &lv_font_montserrat_12, C_DIM, model.charger_name);
    lv_obj_set_pos(s_tile_name, 92, 2);
    s_tile_cur = make_label(s_tile, &lv_font_montserrat_32, C_TEXT, "-- A");
    lv_obj_set_pos(s_tile_cur, 90, 18);
    s_tile_pwr = make_label(s_tile, &lv_font_montserrat_14, C_DIM, "");
    lv_obj_set_pos(s_tile_pwr, 92, 60);

    lv_timer_create(tile_hop, TILE_HOP_MS, nullptr);
}

static void saver_update(const Model &m, bool fresh)
{
    // enter after idle time, leave on touch (event) or BOOT button (polled here)
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
        lv_obj_set_style_arc_color(s_tile_arc, m.soc.v < 20 ? C_RED : m.soc.v < 50 ? C_ORANGE : C_BLUE, LV_PART_INDICATOR);
    }
    const bool running = m.charger_seen && m.chg_current.has() && m.chg_current.v > 0.5f && fresh;
    if (m.chg_current.has()) { snprintf(buf, sizeof buf, "%.1f A", m.chg_current.v); lv_label_set_text(s_tile_cur, buf); }
    lv_obj_set_style_text_color(s_tile_cur, running ? C_GREEN : fresh ? C_TEXT : C_TRACK, 0);
    if (m.batt_power.has()) {
        float p = m.batt_power.v;
        snprintf(buf, sizeof buf, "%s %d W", p > 5 ? LV_SYMBOL_DOWN : p < -5 ? LV_SYMBOL_UP : "", (int)lroundf(fabsf(p)));
        lv_label_set_text(s_tile_pwr, buf);
        lv_obj_set_style_text_color(s_tile_pwr, p > 5 ? C_GREEN : p < -5 ? C_ORANGE : C_DIM, 0);
    }
}

// --- build -------------------------------------------------------------------------------
'''
t = t.replace("// --- build -------------------------------------------------------------------------------\n", saver, 1)

# 3. hook into ui_begin
t = t.replace('''    lv_obj_t *scr = lv_scr_act();
    lv_obj_set_style_bg_color(scr, C_BG, 0);''', '''    lv_obj_t *scr = lv_scr_act();
    s_main_scr = scr;
    pinMode(PIN_BOOT_BTN, INPUT_PULLUP);
    lv_obj_set_style_bg_color(scr, C_BG, 0);''')
t = t.replace('''    lv_timer_create([](lv_timer_t *) {
        Model &m = model;''', '''    saver_build();

    lv_timer_create([](lv_timer_t *) {
        Model &m = model;''')

p.write_text(t, encoding="utf-8")
print("footer refs left:", len(re.findall(r"s_footer", t)),
      "| saver_update calls:", t.count("saver_update("),
      "| saver_build calls:", t.count("saver_build()"))
