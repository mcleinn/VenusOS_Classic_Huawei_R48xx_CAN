#pragma once
#include <stdint.h>

// FT3168 capacitive touch over I2C (Wire). Returns true while a finger is down.
void touch_begin();
bool touch_read(uint16_t &x, uint16_t &y);
