// Panel bring-up in C (esp_lcd designated initialisers do not compile cleanly as C++).
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "esp_lcd_panel_ops.h"

#ifdef __cplusplus
extern "C" {
#endif

// Initialises the QSPI bus, panel IO and the CO5300/SH8601 panel, switches the display on.
// lv_disp_drv is handed back to lv_disp_flush_ready() when a colour transfer completes.
esp_lcd_panel_handle_t display_hw_init(void *lv_disp_drv);

// Panel brightness 0..255 (register 0x51).
bool display_hw_set_brightness(uint8_t level);

// Fills the panel with white/red/green/blue and sweeps brightness; prints register read-backs.
void display_hw_selftest(esp_lcd_panel_handle_t panel);

#ifdef __cplusplus
}
#endif
