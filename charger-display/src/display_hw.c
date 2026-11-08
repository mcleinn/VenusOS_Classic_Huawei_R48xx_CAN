#include "display_hw.h"

#include "driver/spi_master.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_panel_ops.h"
#include "esp_log.h"
#include "esp_lcd_sh8601.h"
#include "lvgl.h"
#include "board_pins.h"

#define LCD_HOST SPI2_HOST
static const char *TAG = "display_hw";

static esp_lcd_panel_io_handle_t s_io = NULL;

// Init sequence from the Waveshare demo (lcd_bsp.c). 0x51 = brightness, 0x53 = control display.
static const sh8601_lcd_init_cmd_t s_init_cmds[] = {
    {0x11, (uint8_t[]){0x00}, 0, 80},
    {0xC4, (uint8_t[]){0x80}, 1, 0},
    {0x35, (uint8_t[]){0x00}, 1, 0},
    {0x53, (uint8_t[]){0x20}, 1, 1},
    {0x63, (uint8_t[]){0xFF}, 1, 1},
    {0x51, (uint8_t[]){0x00}, 1, 1},
    {0x29, (uint8_t[]){0x00}, 0, 10},
    {0x51, (uint8_t[]){0xFF}, 1, 0},
};

volatile uint32_t g_flush_done_count = 0;

static bool on_color_trans_done(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *edata, void *user_ctx)
{
    (void)io; (void)edata;
    g_flush_done_count++;
    lv_disp_drv_t *drv = (lv_disp_drv_t *)user_ctx;
    if (drv && drv->draw_buf) lv_disp_flush_ready(drv);   // self-test runs before LVGL has a draw buffer
    return false;
}

esp_lcd_panel_handle_t display_hw_init(void *lv_disp_drv)
{
    const spi_bus_config_t buscfg = SH8601_PANEL_BUS_QSPI_CONFIG(
        PIN_LCD_PCLK, PIN_LCD_D0, PIN_LCD_D1, PIN_LCD_D2, PIN_LCD_D3,
        LCD_H_RES * LCD_V_RES * 2);
    ESP_ERROR_CHECK(spi_bus_initialize(LCD_HOST, &buscfg, SPI_DMA_CH_AUTO));

    esp_lcd_panel_io_spi_config_t io_config =
        SH8601_PANEL_IO_QSPI_CONFIG(PIN_LCD_CS, on_color_trans_done, lv_disp_drv);
    io_config.pclk_hz = LCD_PCLK_HZ;
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)LCD_HOST, &io_config, &s_io));

    sh8601_vendor_config_t vendor_config = {
        .init_cmds = s_init_cmds,
        .init_cmds_size = sizeof(s_init_cmds) / sizeof(s_init_cmds[0]),
        .flags = { .use_qspi_interface = 1 },
    };
    const esp_lcd_panel_dev_config_t panel_config = {
        .reset_gpio_num = PIN_LCD_RST,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
        .vendor_config = &vendor_config,
    };
    esp_lcd_panel_handle_t panel = NULL;
    ESP_ERROR_CHECK(esp_lcd_new_panel_sh8601(s_io, &panel_config, &panel));
    ESP_ERROR_CHECK(esp_lcd_panel_reset(panel));
    ESP_ERROR_CHECK(esp_lcd_panel_init(panel));
    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(panel, true));
    printf("display_hw: panel up, %dx%d, io=%p panel=%p\n", LCD_H_RES, LCD_V_RES, (void *)s_io, (void *)panel);
    return panel;
}

// Boot self-test without LVGL: fill the panel with solid colours straight through esp_lcd and
// sweep the brightness register, so a dark screen can be attributed to the panel path and not
// to the GUI. Also tries to read back COLMOD (0x0C, expect 0x55) over the read opcode 0x03.
void display_hw_selftest(esp_lcd_panel_handle_t panel)
{
    uint8_t rd[4] = {0};
    for (int reg = 0x0A; reg <= 0x0F; reg++) {
        uint32_t cmd = ((uint32_t)reg << 8) | ((uint32_t)0x03 << 24);
        esp_err_t e = esp_lcd_panel_io_rx_param(s_io, cmd, rd, 1);
        printf("display_hw: read 0x%02X -> %s 0x%02X\n", reg, esp_err_to_name(e), rd[0]);
    }
    static uint16_t line[LCD_H_RES * 8];
    const uint16_t colours[] = {0xFFFF, 0xF800, 0x07E0, 0x001F};   // white, red, green, blue (RGB565, byte order irrelevant for white)
    for (int c = 0; c < 4; c++) {
        for (int i = 0; i < LCD_H_RES * 8; i++) line[i] = colours[c];
        for (int y = 0; y < LCD_V_RES; y += 8) {
            esp_lcd_panel_draw_bitmap(panel, LCD_X_OFFSET, y, LCD_X_OFFSET + LCD_H_RES, y + 8, line);
            vTaskDelay(1);
        }
        printf("display_hw: filled 0x%04X\n", colours[c]);
        for (int b = 0; b <= 255; b += 51) { display_hw_set_brightness((uint8_t)b); vTaskDelay(pdMS_TO_TICKS(120)); }
        display_hw_set_brightness(255);
        vTaskDelay(pdMS_TO_TICKS(600));
    }
}

bool display_hw_set_brightness(uint8_t level)
{
    if (!s_io) return false;
    uint32_t cmd = ((uint32_t)0x51 << 8) | ((uint32_t)0x02 << 24);  // QSPI write-command opcode 0x02
    return esp_lcd_panel_io_tx_param(s_io, cmd, &level, 1) == ESP_OK;
}
