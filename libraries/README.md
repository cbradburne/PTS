# Arduino Libraries

Third-party libraries required to compile the firmware in this project.
Install these by copying each folder into your Arduino `libraries` directory
(usually `~/Documents/Arduino/libraries/` on macOS/Linux).

## Library Versions

| Library | Version | Used by | Notes |
|---------|---------|---------|-------|
| [GFX_Library_for_Arduino](https://github.com/moononournation/Arduino_GFX) | 1.6.5 | esp_mount_amoled175 | Display driver |
| [lvgl](https://github.com/lvgl/lvgl) | 9.5.0 | esp32_display | LVGL v9 UI framework |
| [ESP_Async_WebServer](https://github.com/mathieucarbou/ESPAsyncWebServer) | 3.10.3 | esp32_hub | Async HTTP + WebSocket server |
| [Async_TCP](https://github.com/mathieucarbou/AsyncTCP) | 3.4.10 | esp32_hub | Dependency of ESP_Async_WebServer |
| [SensorLib](https://github.com/lewisxhe/SensorsLib) | 0.4.1 | esp32_display | QMI8658 IMU + GT911 touch (TouchDrv.hpp, SensorQMI8658.hpp) |
| [TAMC_GT911](https://github.com/TAMCTec/gt911-arduino) | 1.0.2 | esp32_display | GT911 capacitive touch fallback driver |
| [TMCStepper](https://github.com/teemuatlut/TMCStepper) | 0.7.3 | teensy41_mount | TMC2209 stepper driver communication |
| [TeensyStep4](https://github.com/luni64/TeensyStep4) | 0.0.3 | teensy41_mount | Non-blocking stepper motion for Teensy 4.1 |

## Board Packages Required

These libraries are built into the board packages and do **not** need to be installed separately:

### ESP32 (Arduino-ESP32 v3.x)
- `WiFi.h`, `Wire.h`, `EEPROM.h`
- `esp_now.h`, `esp_wifi.h`, `esp_timer.h`
- `esp_heap_caps.h`, `esp_lcd_panel_ops.h`, `esp_lcd_panel_rgb.h`
- `freertos/FreeRTOS.h`, `freertos/queue.h`, `freertos/semphr.h`, `freertos/task.h`

### Teensy (Teensyduino)
- `Wire.h`, `EEPROM.h`, `Arduino.h`

## Board Settings

### esp32_hub (XIAO ESP32S3)
- Board: `XIAO_ESP32S3`
- Flash: 8MB, Partition: `Default 4MB with spiffs`
- USB CDC On Boot: Enabled

### esp32_display (Waveshare ESP32-S3-Touch-LCD-7)
- Board: `ESP32S3 Dev Module`
- Flash: 16MB, Partition: `8M with spiffs`
- PSRAM: `OPI PSRAM` (required)
- USB CDC On Boot: Enabled

### teensy41_mount (Teensy 4.1)
- Board: `Teensy 4.1`
- CPU Speed: `600 MHz`
- Requires Teensyduino installed on top of Arduino IDE

## Notes

- `examples/`, `tests/`, `demos/`, `docs/` subfolders have been removed from
  libraries to reduce repository size. Only the source files needed to compile
  are included.
- `lvgl` requires a `lv_conf.h` file in the sketch folder. Each firmware target
  that uses LVGL has its own `lv_conf.h` with the appropriate configuration.
