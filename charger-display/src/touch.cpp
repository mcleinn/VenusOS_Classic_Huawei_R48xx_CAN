#include "touch.h"

#include <Arduino.h>
#include <Wire.h>
#include "esp_log.h"
#include "board_pins.h"

static bool s_present = false;

// The FT3168 drops into a low-power monitor mode when idle and then NACKs the first
// transaction. The ESP-IDF I2C driver logs every NACK as an error, so we use a plain
// write / stop / read pair (no repeated start) and treat a NACK as "no touch".
static bool read_regs(uint8_t reg, uint8_t *buf, size_t len)
{
    Wire.beginTransmission(FT3168_ADDR);
    Wire.write(reg);
    if (Wire.endTransmission(true) != 0) return false;
    if (Wire.requestFrom((int)FT3168_ADDR, (int)len) != (int)len) return false;
    for (size_t i = 0; i < len; i++) buf[i] = Wire.read();
    return true;
}

void touch_begin()
{
    Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, 400000);
    Wire.setTimeOut(5);   // ms; a sleeping controller must not stall the LVGL loop
    esp_log_level_set("i2c.master", ESP_LOG_NONE);   // NACKs are expected while the panel sleeps
    // Register 0x00 = 0: normal operating mode (as in the Waveshare demo).
    Wire.beginTransmission(FT3168_ADDR);
    Wire.write(0x00);
    Wire.write(0x00);
    s_present = Wire.endTransmission() == 0;
    Serial.printf("touch: FT3168 %s\n", s_present ? "found" : "not answering");
}

bool touch_read(uint16_t &x, uint16_t &y)
{
    if (!s_present) return false;
    uint8_t n = 0;
    if (!read_regs(0x02, &n, 1) || (n & 0x0F) == 0) return false;
    uint8_t b[4];
    if (!read_regs(0x03, b, 4)) return false;
    x = ((b[0] & 0x0F) << 8) | b[1];
    y = ((b[2] & 0x0F) << 8) | b[3];
    if (x >= LCD_H_RES) x = LCD_H_RES - 1;
    if (y >= LCD_V_RES) y = LCD_V_RES - 1;
    return true;
}
