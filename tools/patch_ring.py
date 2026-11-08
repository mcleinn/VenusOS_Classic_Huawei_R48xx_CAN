"""One-off patch (2026-11-08): inner ring = total charging current of all chargers, full scale =
sum of the per-unit limits configured in the Venus driver, one coloured segment per charger."""
from pathlib import Path

root = Path(__file__).resolve().parent.parent

# --- model: charger slots with current and configured limit
p = root / "src/model.h"; t = p.read_text(encoding="utf-8")
old = "    // Huawei charger service (com.victronenergy.charger.huawei_*)"
new = '''    // All charger services seen, for the total-current ring: actual current and the per-unit
    // limit configured in the Venus driver (Settings/ChargeCurrentLimit). Lowest instance first.
    struct ChargerSlot { int instance = -1; Sample current; Sample limit; };
    static constexpr int MAX_CHARGERS = 2;
    ChargerSlot chargers[MAX_CHARGERS];

    // Huawei charger service (com.victronenergy.charger.huawei_*)'''
assert old in t; t = t.replace(old, new, 1); p.write_text(t, encoding="utf-8")

# --- net: subscribe to the limit, fill the slots for every charger instance
p = root / "src/net.cpp"; t = p.read_text(encoding="utf-8")
old = '    "charger/+/Huawei/State",\n'
assert old in t; t = t.replace(old, '    "charger/+/Settings/ChargeCurrentLimit",\n' + old, 1)
old = '''        if (m.charger_instance < 0) {
            m.charger_instance = inst;
            Serial.printf("mqtt: charger instance %d\\n", inst);
        }
        if (inst != m.charger_instance) return;   // one charger on this display'''
new = '''        if (m.charger_instance < 0) {
            m.charger_instance = inst;
            Serial.printf("mqtt: charger instance %d\\n", inst);
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
        if (inst != m.charger_instance) return;   // the rest is this display's own charger'''
assert old in t, "net slot"; t = t.replace(old, new, 1); p.write_text(t, encoding="utf-8")

# --- ui: two stacked arcs, full scale = sum of the configured limits
p = root / "src/ui.cpp"; t = p.read_text(encoding="utf-8")
def rep(a, b):
    global t
    assert a in t, a[:60]
    t = t.replace(a, b, 1)
rep("static lv_obj_t *s_arc, *s_arc_cur, *s_soc, *s_soc_unit, *s_batt_power, *s_batt_sub;",
    "static lv_obj_t *s_arc, *s_arc_cur, *s_arc_cur2, *s_soc, *s_soc_unit, *s_batt_power, *s_batt_sub;")
rep("static const lv_color_t C_BLUE     = LV_COLOR_MAKE(0x38, 0x7D, 0xC5);",
    "static const lv_color_t C_BLUE     = LV_COLOR_MAKE(0x38, 0x7D, 0xC5);\nstatic const lv_color_t C_LIGHTBLUE= LV_COLOR_MAKE(0x73, 0xA2, 0xD3);")
rep('''    // inner ring: charger current 0..60 A, green, pulsating while charging; no label of its own
    s_arc_cur = make_arc(pg, ARC_X + 22, ARC_Y + 22, ARC_D - 44, 8);
    lv_arc_set_range(s_arc_cur, 0, 600);                 // tenths of an ampere
    lv_obj_set_style_arc_color(s_arc_cur, C_CARD, LV_PART_MAIN);
    lv_obj_set_style_arc_color(s_arc_cur, C_GREEN, LV_PART_INDICATOR);''',
    '''    // inner ring: total charging current of all chargers. Full scale = sum of the per-unit limits
    // configured in the Venus driver (Settings/ChargeCurrentLimit). Two stacked segments:
    // s_arc_cur2 draws 0..(c1+c2) in the second charger's colour underneath, s_arc_cur draws 0..c1
    // in the first charger's colour on top. Pulsates while charging. No label of its own.
    s_arc_cur2 = make_arc(pg, ARC_X + 22, ARC_Y + 22, ARC_D - 44, 8);
    lv_obj_set_style_arc_color(s_arc_cur2, C_CARD, LV_PART_MAIN);
    lv_obj_set_style_arc_color(s_arc_cur2, C_LIGHTBLUE, LV_PART_INDICATOR);
    s_arc_cur = make_arc(pg, ARC_X + 22, ARC_Y + 22, ARC_D - 44, 8);
    lv_obj_set_style_arc_opa(s_arc_cur, LV_OPA_TRANSP, LV_PART_MAIN);   // track drawn by the one below
    lv_obj_set_style_arc_color(s_arc_cur, C_GREEN, LV_PART_INDICATOR);''')
rep('''static void cur_pulse_cb(void *o, int32_t v) { lv_obj_set_style_arc_opa((lv_obj_t *)o, v, LV_PART_INDICATOR); }
static void set_current_ring(float amps, bool charging)
{
    if (amps < 0) amps = 0;
    if (amps > 60) amps = 60;
    lv_arc_set_value(s_arc_cur, (int32_t)lroundf(amps * 10));
    if (charging == s_cur_pulsing) return;
    s_cur_pulsing = charging;
    lv_anim_del(s_arc_cur, cur_pulse_cb);
    if (!charging) { lv_obj_set_style_arc_opa(s_arc_cur, LV_OPA_COVER, LV_PART_INDICATOR); return; }
    lv_anim_init(&s_cur_pulse);
    lv_anim_set_var(&s_cur_pulse, s_arc_cur);
    lv_anim_set_exec_cb(&s_cur_pulse, cur_pulse_cb);''',
    '''static void cur_pulse_cb(void *, int32_t v)
{
    lv_obj_set_style_arc_opa(s_arc_cur, v, LV_PART_INDICATOR);
    lv_obj_set_style_arc_opa(s_arc_cur2, v, LV_PART_INDICATOR);
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
    lv_anim_set_exec_cb(&s_cur_pulse, cur_pulse_cb);''')
rep('''    set_current_ring(cur.has() ? cur.v : 0, running);''',
    '''    {
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
    }''')
p.write_text(t, encoding="utf-8")
print("ring reworked")
