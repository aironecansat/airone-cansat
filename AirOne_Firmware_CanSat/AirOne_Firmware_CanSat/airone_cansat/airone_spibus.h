/*
 * Shared VSPI bus arbitration (BMI270, SX1268, MicroSD, W25Q128).
 * Every SPI transaction from any task holds this FreeRTOS mutex.
 * THIS FILE MUST BE IDENTICAL in airone_cansat/ and airone_ground_bridge/.
 */
#ifndef AIRONE_SPIBUS_H
#define AIRONE_SPIBUS_H

#include <Arduino.h>

static SemaphoreHandle_t g_spi_mutex = NULL;

static void spi_bus_init() {
  if (!g_spi_mutex) g_spi_mutex = xSemaphoreCreateMutex();
}

class SpiLock {
 public:
  explicit SpiLock(uint32_t timeout_ms)
      : ok_(g_spi_mutex && xSemaphoreTake(g_spi_mutex, pdMS_TO_TICKS(timeout_ms)) == pdTRUE) {}
  ~SpiLock() { if (ok_) xSemaphoreGive(g_spi_mutex); }
  bool ok() const { return ok_; }
 private:
  bool ok_;
};

#endif  // AIRONE_SPIBUS_H
