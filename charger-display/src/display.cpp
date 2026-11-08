#include "display.h"

#include <Arduino.h>
#include <lvgl.h>
#include "esp_heap_caps.h"
#include "esp_lcd_panel_ops.h"
#include "display_hw.h"
#include "touch.h"
#include "board_pins.h"

// Landscape on a portrait panel.
//
// LVGL renders a logical UI_W x UI_H (456 x 280) screen into partial buffers of LINES rows.
// flush_cb transposes each chunk into a portrait-oriented buffer and sends it with physical
// coordinates. We do the rotation ourselves instead of LVGL's sw_rotate because the latter
// splits rotated areas into chunks whose edges are not 2-pixel aligned, and the CO5300 drops
// the odd rows/columns of such a window (visible as black lines).
//
// Mapping, LCD_ROTATION == 270:  Xp = y,            Yp = (UI_W - 1) - x
//          LCD_ROTATION == 90:   Xp = (UI_H - 1) - y, Yp = x
// With logical areas rounded to even starts / odd ends, the physical window is aligned too.

static constexpr int LINES = 40;                      // logical rows per buffer (even)
static constexpr size_t BUF_PX = UI_W * LINES;        // 18240 px = 36.5 KB at 16 bpp

static lv_disp_draw_buf_t s_draw_buf;
static lv_disp_drv_t s_disp_drv;
static lv_indev_drv_t s_indev_drv;
static lv_color_t *s_rot = nullptr;                   // transposed chunk, DMA capable

uint32_t g_flush_count = 0;
int g_flush_last_err = 0;

static void flush_cb(lv_disp_drv_t *drv, const lv_area_t *area, lv_color_t *color_map)
{
    auto panel = static_cast<esp_lcd_panel_handle_t>(drv->user_data);
    const int w = area->x2 - area->x1 + 1;
    const int h = area->y2 - area->y1 + 1;

    int xp1, yp1;   // physical window origin; physical size is h x w
#if LCD_ROTATION == 270
    xp1 = area->y1;
    yp1 = (UI_W - 1) - area->x2;
    for (int y = 0; y < h; y++) {
        const lv_color_t *src = color_map + y * w;
        for (int x = 0; x < w; x++) {
            // physical row = (UI_W-1-(x1+x)) - yp1 = (w-1-x), physical col = y
            s_rot[(w - 1 - x) * h + y] = src[x];
        }
    }
#else
    xp1 = (UI_H - 1) - area->y2;
    yp1 = area->x1;
    for (int y = 0; y < h; y++) {
        const lv_color_t *src = color_map + y * w;
        for (int x = 0; x < w; x++) {
            // physical row = x, physical col = (UI_H-1-(y1+y)) - xp1 = (h-1-y)
            s_rot[x * h + (h - 1 - y)] = src[x];
        }
    }
#endif
    g_flush_last_err = esp_lcd_panel_draw_bitmap(panel,
                              xp1 + LCD_X_OFFSET, yp1,
                              xp1 + h + LCD_X_OFFSET, yp1 + w,
                              s_rot);
    g_flush_count++;
    // flush_ready is signalled from the panel IO completion callback (display_hw.c);
    // until then LVGL does not call flush again, so one rotation buffer is enough.
}

// The panel wants even start and odd end coordinates (2-pixel alignment); rounding the logical
// area this way keeps the rotated physical window aligned as well.
static void rounder_cb(lv_disp_drv_t *, lv_area_t *area)
{
    area->x1 &= ~1;
    area->y1 &= ~1;
    area->x2 |= 1;
    area->y2 |= 1;
}

static void touch_cb(lv_indev_drv_t *, lv_indev_data_t *data)
{
    uint16_t xp, yp;
    if (touch_read(xp, yp)) {
#if LCD_ROTATION == 270
        data->point.x = (UI_W - 1) - yp;
        data->point.y = xp;
#else
        data->point.x = yp;
        data->point.y = (UI_H - 1) - xp;
#endif
        data->state = LV_INDEV_STATE_PRESSED;
    } else {
        data->state = LV_INDEV_STATE_RELEASED;
    }
}

void display_begin()
{
    lv_init();
    lv_disp_drv_init(&s_disp_drv);

    esp_lcd_panel_handle_t panel = display_hw_init(&s_disp_drv);
#ifdef DISPLAY_SELFTEST
    display_hw_selftest(panel);
#endif

    auto alloc = [] { return static_cast<lv_color_t *>(heap_caps_malloc(BUF_PX * sizeof(lv_color_t), MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL)); };
    lv_color_t *buf1 = alloc();
    lv_color_t *buf2 = alloc();
    s_rot = alloc();
    if (!buf1 || !buf2 || !s_rot) {
        Serial.println("display: draw buffer allocation failed");
        while (true) delay(1000);
    }
    lv_disp_draw_buf_init(&s_draw_buf, buf1, buf2, BUF_PX);

    s_disp_drv.hor_res = UI_W;
    s_disp_drv.ver_res = UI_H;
    s_disp_drv.flush_cb = flush_cb;
    s_disp_drv.rounder_cb = rounder_cb;
    s_disp_drv.draw_buf = &s_draw_buf;
    s_disp_drv.user_data = panel;
    lv_disp_t *disp = lv_disp_drv_register(&s_disp_drv);

    touch_begin();
    lv_indev_drv_init(&s_indev_drv);
    s_indev_drv.type = LV_INDEV_TYPE_POINTER;
    s_indev_drv.disp = disp;
    s_indev_drv.read_cb = touch_cb;
    lv_indev_drv_register(&s_indev_drv);
}

void display_set_brightness(uint8_t level)
{
    display_hw_set_brightness(level);
}
