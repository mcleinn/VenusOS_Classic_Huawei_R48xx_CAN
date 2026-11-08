# Waveshare ESP32-S3-Touch-AMOLED-1.64, board notes

Source: Waveshare schematic PDF (Altium export dated 2025-11-29, text extracted with pypdf,
2026-11-08) and the demo package. Pin numbers below are ESP32-S3 GPIOs.

## Power

- USB-C 5 V -> ETA6098 Li-ion charger (ISET selectable by resistor) -> VBAT; AO3401 P-FET
  passes USB_5V/VBAT to VCC; MP1605 buck makes 3V3 (FB 200k/44.2 -> 3.314 V, 2 A max).
- The AMOLED module and the touch panel run from 3V3 only: VCI, VDDIO, the panel's VBAT
  pins (29/30) and TP_VCC are all on 3V3.
- **No GPIO-controlled display supply.** Connector pins PWR_EN (21), TE (27), VPP (13),
  TP_INT (7) and TP_RESET (9) share one net pulled to 3V3 through a 10 kΩ resistor.
  IM0 (17) = GND, IM1 (15) = 3V3 -> QSPI interface mode. LCD_RESET has its own 10 kΩ pull-up.
- BAT_ADC: battery voltage divider (100k / 200k) on GPIO4 ("about 1960" counts in the demo).

## Display connector (34 pin)

| Function | GPIO | connector pin |
|---|---|---|
| OLED_CS | **46** on boards with the Jun-2026 firmware (schematic/demo say 9) | 6 |
| OLED_CLK | 10 | 4 |
| OLED_SIO0 (D0) | 11 | 14 |
| OLED_SI1 (D1) | 12 | 12 |
| OLED_SI2 (D2) | 13 | 10 |
| OLED_SI3 (D3) | 14 | 8 |
| OLED_RESET | 21 | 19 |
| TP_SDA (shared with IMU) | 47 | 5 |
| TP_SCL (shared with IMU) | 48 | 3 |

Panel RAM is 20 columns wider than the glass: the demo adds 0x14 to every x coordinate.
The controller wants even start / odd end coordinates (2-pixel alignment).

## Other peripherals

- QMI8658C IMU on the same I2C bus (SDA 47, SCL 48), INT1 -> GPIO46 per the Nov-2025 schematic;
  on the shipped hardware CS and INT1 appear swapped (INT1 presumably GPIO9), unverified.
- TF card: SDIO/SPI on GPIO 38 (CS/D3), 39 (MOSI/CMD), 40 (MISO/D0), 41 (SCLK), D1/D2 via 0 Ω.
- W25Q128 flash on the SPI pins, 8 MB PSRAM in the ESP32-S3R8 package (OPI).
- Headers P1/P2 (11 pins each): IO1 2 3 5 6 7 8 15 16 17 18, RXD/TXD (GPIO44/43), SDA/SCL,
  USB D-/D+, IO45, BAT, 3V3, 5 V, GND.
- Buttons: BOOT = GPIO0, RESET = CHIP_PU. LED2 on VBAT via 3 kΩ (power LED), LED1 charge status.
- External antenna option: 0 Ω jumper R17 towards the U.FL (J3).

## Software facts

- Demo init (CO5300 via the SH8601 esp_lcd driver): MADCTL 0x00, COLMOD 0x55 (RGB565), then
  0x11 (80 ms), 0xC4 0x80, 0x35 0x00, 0x53 0x20, 0x63 0xFF, 0x51 0x00, 0x29 (10 ms), 0x51 0xFF.
  QSPI opcodes: 0x02 write command, 0x32 write colour, 0x03 read. 40 MHz PCLK.
- Register read-back over the QSPI read opcode returned 0xFF for every register in the first
  attempt (2026-11-08); not yet known whether the panel supports reads in this mode.

## Panel revision and factory firmware (2026-11-08, learned the hard way)

- The shipped firmware on these boards is FactoryProgram built **2026-06-02 on ESP-IDF v5.5.2**.
  Waveshare's download package (demo sources and `Firmware/*-factory.bin`) is from **2025-04-25,
  IDF v5.1.4** and does NOT light this panel revision: a firmware using the package's init
  sequence, and the package's factory image itself, leave the AMOLED completely black while
  everything else (SPI transfers, LVGL, WiFi) runs normally.
- A unit left in that state shows nothing even after reflashing a working image until USB power
  is removed and reapplied: the panel's RESET pin pulse is not enough, it needs a real power cycle.
- Recovery: `esptool --chip esp32s3 write-flash 0x0 _local/unit2-golden/unit2-flash-16MB.bin`
  (full 16 MB image read from the untouched second unit), then unplug/replug USB.
- Golden image layout: bootloader 0x0, partitions 0x8000 (nvs 0x9000/0x6000, phy_init 0xf000/0x1000,
  factory app 0x10000/0x600000), nothing programmed above 0x2c8990. Bootloader header byte 3 = 0x3f.
- Unit 1 = MAC 28:84:85:8d:4a:0c (COM11), unit 2 = MAC ...:4a:d8 (COM12). Unit 2 is the untouched
  reference; experiments run on unit 1 only.
- Root cause (independent review of the disassembled shipped app vs the Apr-2025 factory app):
  identical init table, pixel format, PCLK, reset pin and I2C setup; the ONLY display-relevant
  difference is the panel chip-select: **GPIO46 in the shipped firmware, GPIO9 in the demo/old
  factory image**. The sh8601 component is v2.0.1 in the shipped app, v1.0.0 in the old one.
