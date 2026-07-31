// ---------------------------------------------------------------------------
// crash_report.h — print the previous crash at boot
// ---------------------------------------------------------------------------
//
// The ESP32 writes a core dump to flash when it panics.  Nothing read it back,
// so a crash left only "reset reason: PANIC(crash)" with no fault address and
// no backtrace — useless for finding the cause, and the panic text itself is
// gone by the time anything reconnects.
//
// crash_report_print() reads that dump on the next boot and prints a summary:
// the task that died, its PC, and a backtrace.  On the hub this lands in the
// USB serial stream, which the PC app extracts into comms.log as "HUB SERIAL:"
// lines — so a crash is recoverable in the field with no cable attached.
//
// Requires a `coredump` partition (already present in default_8MB.csv, which
// both the hub and the mount bridge build against) and the core-dump support
// the Arduino ESP32 core compiles in by default (CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH
// + DATA_FORMAT_ELF).  If either is missing this compiles to nothing.
//
// The dump is deliberately NOT erased after printing: a fresh panic overwrites
// the partition anyway, and keeping it means a crash that happened while
// nothing was listening can still be read later.  Lines are therefore prefixed
// [COREDUMP] and describe the LAST crash, which may predate this boot.
//
// To resolve the PC/backtrace addresses to source lines:
//   xtensa-esp32s3-elf-addr2line -pfiaC -e .build/<target>/<sketch>.elf <addr…>

#pragma once

#include <Arduino.h>

#if __has_include("esp_core_dump.h")
#include "esp_core_dump.h"
#endif

inline void crash_report_print() {
#if defined(CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH) && defined(CONFIG_ESP_COREDUMP_DATA_FORMAT_ELF)
    if (esp_core_dump_image_check() != ESP_OK) return;   // none stored, or corrupt

    // ~600 bytes — too big for the stack on some tasks, so take it from the heap.
    esp_core_dump_summary_t *s =
        (esp_core_dump_summary_t *)malloc(sizeof(esp_core_dump_summary_t));
    if (!s) return;

    if (esp_core_dump_get_summary(s) == ESP_OK) {
        Serial.println("[COREDUMP] ---- previous crash ----");
        Serial.printf("[COREDUMP] task '%s' faulted at PC 0x%08lx\n",
                      s->exc_task, (unsigned long)s->exc_pc);

        char reason[64] = {0};
        if (esp_core_dump_get_panic_reason(reason, sizeof(reason)) == ESP_OK && reason[0])
            Serial.printf("[COREDUMP] reason: %s\n", reason);

        uint32_t depth = s->exc_bt_info.depth;
        if (depth > 16) depth = 16;
        Serial.printf("[COREDUMP] backtrace (%lu frames%s):\n",
                      (unsigned long)depth,
                      s->exc_bt_info.corrupted ? ", CORRUPTED" : "");
        // Print the frames a few per line, flushing as we go, rather than
        // building one long String and emitting it in a single printf.
        //
        // That is what the first field capture did, and the frames never
        // appeared: the reader saw "backtrace (13 frames):" followed straight
        // by the end marker.  A ~150-character write at this point in boot has
        // to fit the USB CDC TX buffer in one go with no host necessarily
        // draining it yet, and String also puts the whole thing on the heap
        // before any of it is sent.  Either can silently swallow the payload —
        // and the frame addresses ARE the payload, the one thing this exists to
        // recover.  Short writes with an explicit flush cannot lose them all.
        for (uint32_t i = 0; i < depth; i++) {
            if (i % 4 == 0) Serial.printf("[COREDUMP] bt");
            Serial.printf(" 0x%08lx", (unsigned long)s->exc_bt_info.bt[i]);
            if (i % 4 == 3 || i + 1 == depth) {
                Serial.println();
                Serial.flush();
            }
        }
        Serial.println("[COREDUMP] ---- end ----");
    }
    free(s);
#endif
}
