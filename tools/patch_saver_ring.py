"""One-off patch (2026-11-08): screensaver gets the same two-segment current ring inside a larger
SOC ring, no extra label."""
from pathlib import Path

root = Path(__file__).resolve().parent.parent
p = root / "src/ui.cpp"; t = p.read_text(encoding="utf-8")
def rep(a, b):
    global t
    assert a in t, a[:60]
    t = t.replace(a, b, 1)

rep("static lv_obj_t *s_tile_arc, *s_tile_soc, *s_tile_name, *s_tile_cur, *s_tile_pwr;",
    "static lv_obj_t *s_tile_arc, *s_tile_cur1, *s_tile_cur2, *s_tile_soc, *s_tile_name, *s_tile_cur, *s_tile_pwr;\nstatic lv_anim_t s_tile_pulse;\nstatic bool s_tile_pulsing = false;")
rep("static constexpr int TILE_W = 294, TILE_H = 156;", "static constexpr int TILE_W = 330, TILE_H = 160;")
rep('''    s_tile_arc = make_arc(s_tile, 0, 6, 114, 12);
    s_tile_soc = make_label(s_tile, &lv_font_montserrat_32, C_TEXT, "--");
    lv_obj_align_to(s_tile_soc, s_tile_arc, LV_ALIGN_CENTER, 0, 0);
    s_tile_name = make_label(s_tile, &lv_font_montserrat_16, C_DIM, model.charger_name);
    lv_obj_set_pos(s_tile_name, 138, 4);
    s_tile_cur = make_label(s_tile, &lv_font_montserrat_48, C_TEXT, "-- A");
    lv_obj_set_pos(s_tile_cur, 136, 28);
    s_tile_pwr = make_label(s_tile, &lv_font_montserrat_20, C_DIM, "");
    lv_obj_set_pos(s_tile_pwr, 138, 94);''',
    '''    // SOC ring 150 px with the two-segment total-current ring inside, text block to the right
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
    lv_obj_set_pos(s_tile_name, 172, 10);
    s_tile_cur = make_label(s_tile, &lv_font_montserrat_48, C_TEXT, "-- A");
    lv_obj_set_pos(s_tile_cur, 170, 36);
    s_tile_pwr = make_label(s_tile, &lv_font_montserrat_20, C_DIM, "");
    lv_obj_set_pos(s_tile_pwr, 172, 104);''')

# shared ring computation: factor the page-0 block into a helper used by both
rep('''static void cur_pulse_cb(void *, int32_t v)
{
    lv_obj_set_style_arc_opa(s_arc_cur, v, LV_PART_INDICATOR);
    lv_obj_set_style_arc_opa(s_arc_cur2, v, LV_PART_INDICATOR);
}''',
    '''static void cur_pulse_cb(void *, int32_t v)
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
}''')

rep('''    {
        float c[2] = {0, 0}, scale = 0;
        bool any = false;
        for (int i = 0; i < Model::MAX_CHARGERS; i++) {
            const Model::ChargerSlot &s = m.chargers[i];
            if (s.instance < 0) continue;
            c[i] = (s.instance == m.charger_instance && cur.has()) ? cur.v : s.current.v;   // own unit: newest source
            if (c[i] > 0.5f) any = true;
            if (s.limit.has()) scale += s.limit.v;
        }
        if (m.chargers[0].instance < 0 && cur.has()) { c[0] = cur.v; any = cur.v > 0.5f; }   // CAN only, Cerbo unseen
        if (scale <= 0) scale = m.link_limit.has() ? m.link_limit.v : 60;                     // until the limits arrive
        set_current_ring(c[0], c[1], scale, any && (chg_fresh || m.venus_fresh()));
    }''',
    '''    {
        const RingData r = ring_data(m, cur, chg_fresh);
        set_current_ring(r.c1, r.c2, r.scale, r.charging);
    }''')

# screensaver update: ring + pulse
rep('''    const Sample &cur = live_current(m);
    const bool chg_fresh = charger_data_fresh(m);
    const bool running = cur.has() && cur.v > 0.5f && chg_fresh;
    if (cur.has()) { snprintf(buf, sizeof buf, "%.1f A", cur.v); lv_label_set_text(s_tile_cur, buf); }
    lv_obj_set_style_text_color(s_tile_cur, running ? C_GREEN : chg_fresh ? C_TEXT : C_TRACK, 0);''',
    '''    const Sample &cur = live_current(m);
    const bool chg_fresh = charger_data_fresh(m);
    const bool running = cur.has() && cur.v > 0.5f && chg_fresh;
    if (cur.has()) { snprintf(buf, sizeof buf, "%.1f A", cur.v); lv_label_set_text(s_tile_cur, buf); }
    lv_obj_set_style_text_color(s_tile_cur, running ? C_GREEN : chg_fresh ? C_TEXT : C_TRACK, 0);
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
    }''')
rep('''static void saver_update(const Model &m, bool fresh)
{''', '''static void tile_pulse_cb(void *, int32_t v)
{
    lv_obj_set_style_arc_opa(s_tile_cur1, v, LV_PART_INDICATOR);
    lv_obj_set_style_arc_opa(s_tile_cur2, v, LV_PART_INDICATOR);
}

static void saver_update(const Model &m, bool fresh)
{''')
p.write_text(t, encoding="utf-8")
print("saver ring added")
