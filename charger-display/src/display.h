#pragma once
#include <stdint.h>

// Brings up panel, LVGL display driver, draw buffers and the touch input device.
void display_begin();
// 0..255
void display_set_brightness(uint8_t level);
