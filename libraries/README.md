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
- `WiFi.h`, `Wire.h`, `EEPROM.h`, `Preferences.h` (NVS — pairing/config storage)
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
- `lvgl` is configured by [`lv_conf.h`](lv_conf.h) in this folder — LVGL finds
  it next to the `lvgl/` library directory (both for `tools/build.sh` and for
  an Arduino IDE setup that copies `libraries/` wholesale, including this
  file). Exception: `esp_mount_amoled175` defines `LV_CONF_INCLUDE_SIMPLE` and
  carries its own sketch-folder `lv_conf.h` (PSRAM-pool setup for the bridge
  board); the display target has no sketch-local config.
- After changing `lv_conf.h`, rebuild **clean** — and note that `arduino-cli`
  caches compiled libraries *outside* `.build/`, so `rm -rf .build` alone is not
  enough: use `arduino-cli cache clean` (or, in the IDE, quit and
  `rm -rf ~/Library/Caches/arduino/sketches`). A stale build silently keeps the
  old config, which hides real breakage until someone finally builds clean.
- If your copy of `lv_conf.h` is out of date, the usual symptom is a compile
  error naming a font, e.g. `'lv_font_montserrat_22' was not declared in this
  scope`. Copy this folder's `lv_conf.h` over the one next to your Arduino
  `lvgl/` directory. (Re-copy it whenever this file changes in the repo.)
- **Keep `LV_MEM_SIZE` small.** It is a *static internal-DRAM* pool. The 7″
  display's RGB panel needs a ~96 KB bounce buffer in internal DRAM; a large
  LVGL pool starves it and `esp_lcd_new_rgb_panel()` aborts at boot — a blank
  screen with no serial output, because it dies before USB CDC enumerates.
  Big-UI capacity comes from the PSRAM overflow pools in `init_lvgl()` instead.
