// Waveshare ESP32-S3-Touch-AMOLED-1.64 pin map.
// Source: Waveshare demo package (Arduino/examples/06_LVGL_Test/lcd_config.h, 03_SD_Card).
#pragma once

// AMOLED panel: CO5300 (SH8601-compatible command set) over QSPI, 280 x 456, RGB565.
#define LCD_H_RES        280
#define LCD_V_RES        456
// The panel RAM starts 20 columns left of the visible area; the demo adds 0x14 to every x.
#define LCD_X_OFFSET     0x14

// Boards shipped with the June-2026 factory firmware use GPIO46 for the panel CS (the April-2025
// demo package and the Nov-2025 schematic say GPIO9, which leaves this panel revision black).
#define PIN_LCD_CS       46
#define PIN_LCD_PCLK     10
#define PIN_LCD_D0       11
#define PIN_LCD_D1       12
#define PIN_LCD_D2       13
#define PIN_LCD_D3       14
#define PIN_LCD_RST      21
// No backlight pin: brightness is the panel's 0x51 register (0..255).
// QSPI clock: 40 MHz, as in the factory firmware (10 MHz was used during bring-up).
#define LCD_PCLK_HZ      (40 * 1000 * 1000)

// Capacitive touch FT3168 on I2C (shared with the QMI8658 IMU).
#define PIN_I2C_SDA      47
#define PIN_I2C_SCL      48
#define FT3168_ADDR      0x38

// TF card (SPI mode), not used yet.
#define PIN_SD_CS        38
#define PIN_SD_MOSI      39
#define PIN_SD_MISO      40
#define PIN_SD_CLK       41

// BOOT side button is GPIO0 (programmable).
#define PIN_BOOT_BTN     0

// Mounting: landscape. The CO5300 has no hardware rotation; display.cpp rotates in the flush path.
// 270 or 90 (degrees) selects which long edge is the top; plain numbers so #if can test them.
#define LCD_ROTATION     90
#define UI_W             LCD_V_RES   // logical width after rotation
#define UI_H             LCD_H_RES

// Display firmware version shown on the status page.
#define UI_FW_VERSION    "0.2"

// CAN transceiver TJA1051T/3 (3.3 V logic via its VIO pin) on the P1 header: TXD <- GPIO17, RXD -> GPIO18.
// Passive listener on the charger's bus (TWAI listen-only), see src/can.cpp.
#define PIN_CAN_TX       17
#define PIN_CAN_RX       18
