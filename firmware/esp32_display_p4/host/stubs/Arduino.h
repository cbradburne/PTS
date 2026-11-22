#pragma once
// Host stand-ins for the little of Arduino and ESP-IDF that the P4 UI uses,
// so host/render.sh can build hub_display.cpp on a Mac.  Nothing here runs on
// the board.
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <math.h>

#define IRAM_ATTR

uint32_t millis();

struct HostSerial {
    void begin(unsigned long) {}
    void flush() {}
    int printf(const char *fmt, ...) {
        va_list ap; va_start(ap, fmt);
        int n = vfprintf(stderr, fmt, ap);
        va_end(ap);
        return n;
    }
    void println(const char *s = "") { fprintf(stderr, "%s\n", s); }
    void print(const char *s) { fputs(s, stderr); }
};
extern HostSerial Serial;

#ifndef constrain
#define constrain(x, lo, hi) ((x) < (lo) ? (lo) : (x) > (hi) ? (hi) : (x))
#endif

// esp_system.h / esp_heap_caps.h
#define MALLOC_CAP_INTERNAL  (1 << 0)
#define MALLOC_CAP_SPIRAM    (1 << 1)
#define MALLOC_CAP_DMA       (1 << 2)
#define MALLOC_CAP_8BIT      (1 << 3)
static inline void  *heap_caps_malloc(size_t n, uint32_t) { return malloc(n); }
static inline void  *heap_caps_calloc(size_t c, size_t n, uint32_t) { return calloc(c, n); }
static inline void  *heap_caps_aligned_alloc(size_t a, size_t n, uint32_t) { return aligned_alloc(a, (n + a - 1) / a * a); }
static inline void   heap_caps_free(void *p) { free(p); }
static inline size_t heap_caps_get_free_size(uint32_t) { return 400 * 1024; }
static inline uint32_t esp_get_free_heap_size() { return 400 * 1024; }
static inline uint32_t esp_get_minimum_free_heap_size() { return 300 * 1024; }
static inline int esp_reset_reason() { return 1; }
static inline int64_t esp_timer_get_time() { return (int64_t)millis() * 1000; }

// FreeRTOS: the harness is one thread, so a mutex always succeeds.
typedef void *SemaphoreHandle_t;
typedef void *TaskHandle_t;
typedef uint32_t StackType_t;
typedef struct { int unused; } StaticTask_t;
typedef int BaseType_t;
typedef uint32_t TickType_t;
#define portMAX_DELAY 0xFFFFFFFFu
#define pdTRUE 1
#define pdFALSE 0
#define pdMS_TO_TICKS(ms) (ms)
static inline SemaphoreHandle_t xSemaphoreCreateMutex() { return (void *)1; }
static inline int xSemaphoreTake(SemaphoreHandle_t, TickType_t) { return 1; }
static inline int xSemaphoreGive(SemaphoreHandle_t) { return 1; }
void vTaskDelay(TickType_t ms);   // the harness's: it moves the fake clock on
static inline TaskHandle_t xTaskCreateStaticPinnedToCore(void (*)(void *), const char *, uint32_t,
                                                         void *, int, StackType_t *, StaticTask_t *, int) {
    return (void *)1;   // never started: the harness drives LVGL itself
}
