/*
 * AirOne on-board data storage: MicroSD (primary) + W25Q128 NOR flash (fallback)
 * =============================================================================
 * Every frame the CanSat builds (the FULL JSON SENSOR_DATA frame, byte-for-byte
 * what the ground software parses) is pushed into a RAM ring buffer by the
 * flight loop. A dedicated FreeRTOS task on core 0 drains that buffer so a slow
 * or failing card can never stall sensing, telemetry or the watchdog strobe.
 *
 *   MicroSD  /AIRONE/Fnnnn.BIN   raw AirOne frames back to back (new file per boot)
 *            fsync'd every STORAGE_SD_SYNC_MS (1 s) and on mission-state changes.
 *   W25Q128  raw AirOne frames back to back from address 0 (append-only).
 *
 * FAILOVER: frames written to the card since the last successful fsync are kept
 * in RAM. If any SD write/fsync fails (e.g. the card unseats under launch
 * shock), the card is dropped and those not-yet-durable frames are replayed to
 * the flash, followed by every new frame. The task tries to re-mount the card
 * every STORAGE_SD_RETRY_MS; on success it opens a NEW file and switches back.
 * Flash writes are durable as soon as the page program completes.
 *
 * SPI BUS: SD, W25Q128, BMI270 and the SX1268 radio share VSPI. All access goes
 * through spi_lock()/SpiLock (FreeRTOS mutex). Lock hold times are kept short:
 * the flash busy-wait releases the bus between status polls.
 *
 * Both log formats decode with tools/airone_logtool.py (CSV / JSON lines).
 */
#ifndef AIRONE_STORAGE_H
#define AIRONE_STORAGE_H

#include <Arduino.h>
#include <SPI.h>
#include <SD.h>
#include <stdio.h>
#include <unistd.h>
#include <sys/stat.h>
#include "airone_flashlog.h"
#include "airone_spibus.h"
#include "airone_frame.h"

#ifndef STORAGE_RING_BYTES
#define STORAGE_RING_BYTES   (64 * 1024)   // ~12 s of 2.6 kB frames at 2 Hz
#endif
#ifndef STORAGE_SD_SYNC_MS
#define STORAGE_SD_SYNC_MS   1000
#endif
#ifndef STORAGE_SD_RETRY_MS
#define STORAGE_SD_RETRY_MS  10000
#endif
#ifndef STORAGE_SD_HZ
#define STORAGE_SD_HZ        10000000
#endif
#ifndef STORAGE_FLASH_HZ
#define STORAGE_FLASH_HZ     8000000
#endif
#ifndef STORAGE_PREERASE_BYTES
#define STORAGE_PREERASE_BYTES (512u * 1024u)   // keep 512 kB erased ahead for fast failover
#endif
#define STORAGE_MAX_FRAME    4096

static void wdt_kick_ext();   // provided by the sketch

// =====================================================================
// W25Q128 driver (standard SPI NOR commands, 3-byte addressing)
// =====================================================================
class W25Q {
 public:
  bool begin(int cs) {
    cs_ = cs;
    pinMode(cs_, OUTPUT);
    digitalWrite(cs_, HIGH);
    {
      SpiLock l(1000);
      if (!l.ok()) return false;
      cmd1(0xAB);                       // release from power-down
    }
    delayMicroseconds(50);
    uint8_t id[3] = {0, 0, 0};
    {
      SpiLock l(1000);
      if (!l.ok()) return false;
      sel();
      SPI.transfer(0x9F);
      for (int i = 0; i < 3; i++) id[i] = SPI.transfer(0);
      desel();
    }
    mfr_ = id[0];
    dev_ = ((uint16_t)id[1] << 8) | id[2];
    // Winbond = 0xEF; capacity byte = log2(bytes). W25Q128 -> 0x18 (16 MB).
    if (id[0] == 0x00 || id[0] == 0xFF || id[2] < 0x10 || id[2] > 0x18) return false;
    capacity_ = 1UL << id[2];
    return true;
  }

  uint32_t capacity() const { return capacity_; }
  uint8_t manufacturer() const { return mfr_; }
  uint16_t device() const { return dev_; }

  bool read(uint32_t addr, uint8_t* buf, size_t n) {
    if (addr + n > capacity_) return false;
    while (n) {
      size_t chunk = n > 1024 ? 1024 : n;   // bound the lock hold time
      SpiLock l(1000);
      if (!l.ok()) return false;
      sel();
      SPI.transfer(0x03);
      addr24(addr);
      for (size_t i = 0; i < chunk; i++) buf[i] = SPI.transfer(0);
      desel();
      addr += chunk; buf += chunk; n -= chunk;
    }
    return true;
  }

  // Page program; caller guarantees the range is erased. Splits on 256 B pages.
  bool program(uint32_t addr, const uint8_t* data, size_t n) {
    if (addr + n > capacity_) return false;
    while (n) {
      size_t room = 256 - (addr & 0xFF);
      size_t chunk = n < room ? n : room;
      {
        SpiLock l(1000);
        if (!l.ok()) return false;
        cmd1(0x06);                     // WREN
        sel();
        SPI.transfer(0x02);
        addr24(addr);
        for (size_t i = 0; i < chunk; i++) SPI.transfer(data[i]);
        desel();
      }
      if (!wait_ready(20)) return false;  // tPP max 3 ms
      addr += chunk; data += chunk; n -= chunk;
    }
    return true;
  }

  bool erase_sector(uint32_t addr) {   // 4 kB, tSE max 400 ms
    {
      SpiLock l(1000);
      if (!l.ok()) return false;
      cmd1(0x06);
      sel();
      SPI.transfer(0x20);
      addr24(addr & ~0xFFFUL);
      desel();
    }
    return wait_ready(1000);
  }

  bool sector_blank(uint32_t addr) {
    uint8_t b[256];
    addr &= ~0xFFFUL;
    for (uint32_t o = 0; o < 4096; o += sizeof(b)) {
      if (!read(addr + o, b, sizeof(b))) return false;
      for (size_t i = 0; i < sizeof(b); i++) if (b[i] != 0xFF) return false;
    }
    return true;
  }

 private:
  void sel() { SPI.beginTransaction(SPISettings(STORAGE_FLASH_HZ, MSBFIRST, SPI_MODE0)); digitalWrite(cs_, LOW); }
  void desel() { digitalWrite(cs_, HIGH); SPI.endTransaction(); }
  void cmd1(uint8_t c) { sel(); SPI.transfer(c); desel(); }
  void addr24(uint32_t a) { SPI.transfer((uint8_t)(a >> 16)); SPI.transfer((uint8_t)(a >> 8)); SPI.transfer((uint8_t)a); }
  // Poll WIP, releasing the bus between polls so other SPI users keep running.
  bool wait_ready(uint32_t timeout_ms) {
    uint32_t t0 = millis();
    for (;;) {
      uint8_t sr;
      {
        SpiLock l(200);
        if (l.ok()) { sel(); SPI.transfer(0x05); sr = SPI.transfer(0); desel(); }
        else sr = 0x01;
      }
      if (!(sr & 0x01)) return true;
      if (millis() - t0 > timeout_ms) return false;
      vTaskDelay(1);
    }
  }
  int cs_ = -1;
  uint32_t capacity_ = 0;
  uint8_t mfr_ = 0;
  uint16_t dev_ = 0;
};

// =====================================================================
// Storage state
// =====================================================================
static W25Q g_flash;
static volatile bool g_flash_ok = false;     // chip detected
static volatile bool g_flash_full = false;
static uint32_t g_flash_wp = 0;              // append address
static uint32_t g_flash_erased_upto = 0;     // [wp, erased_upto) known erased
static uint32_t g_flash_boot_records = 0;

static volatile bool g_sd_ok = false;        // card mounted and file open
static FILE*    g_sd_file = NULL;
static char     g_sd_path[40] = "";
static int      g_sd_cs = -1;
static uint32_t g_sd_last_sync_ms = 0;
static uint32_t g_sd_last_retry_ms = 0;
static uint32_t g_sd_unsynced = 0;

static volatile uint32_t g_log_frames_sd = 0, g_log_frames_flash = 0;
static volatile uint32_t g_log_dropped = 0, g_log_sd_failovers = 0;
static volatile bool g_storage_sync_req = false;
static volatile bool g_storage_erase_req = false;

// Ring buffer of [u16 len][frame] records. Positions are free-running counters.
static uint8_t* g_ring = NULL;
static uint32_t g_ring_cap = 0;
static volatile uint32_t g_ring_wr = 0;      // producer
static volatile uint32_t g_ring_rd = 0;      // next record to write to the sink
static volatile uint32_t g_ring_keep = 0;    // oldest byte not yet durable
static portMUX_TYPE g_ring_mux = portMUX_INITIALIZER_UNLOCKED;
static TaskHandle_t g_storage_task = NULL;

static void ring_copy_in(uint32_t pos, const uint8_t* src, size_t n) {
  uint32_t i = pos % g_ring_cap, first = g_ring_cap - i;
  if (first >= n) memcpy(g_ring + i, src, n);
  else { memcpy(g_ring + i, src, first); memcpy(g_ring, src + first, n - first); }
}
static void ring_copy_out(uint32_t pos, uint8_t* dst, size_t n) {
  uint32_t i = pos % g_ring_cap, first = g_ring_cap - i;
  if (first >= n) memcpy(dst, g_ring + i, n);
  else { memcpy(dst, g_ring + i, first); memcpy(dst + first, g_ring, n - first); }
}

// Producer API (flight loop). Never blocks. Returns false if the frame was dropped.
static bool storage_log(const uint8_t* frame, size_t len) {
  if (!g_ring || len == 0 || len > STORAGE_MAX_FRAME) { g_log_dropped = g_log_dropped + 1; return false; }
  uint32_t need = (uint32_t)len + 2;
  portENTER_CRITICAL(&g_ring_mux);
  uint32_t wr = g_ring_wr, keep = g_ring_keep;
  portEXIT_CRITICAL(&g_ring_mux);
  if (g_ring_cap - (wr - keep) < need) { g_log_dropped = g_log_dropped + 1; return false; }
  uint8_t hdr[2] = {(uint8_t)len, (uint8_t)(len >> 8)};
  ring_copy_in(wr, hdr, 2);
  ring_copy_in(wr + 2, frame, len);
  portENTER_CRITICAL(&g_ring_mux);
  g_ring_wr = wr + need;
  portEXIT_CRITICAL(&g_ring_mux);
  if (g_storage_task) xTaskNotifyGive(g_storage_task);
  return true;
}

static void storage_request_sync() { g_storage_sync_req = true; if (g_storage_task) xTaskNotifyGive(g_storage_task); }

// =====================================================================
// SD card
// =====================================================================
static bool sd_open_new_file() {
  SpiLock l(2000);
  if (!l.ok()) return false;
  mkdir("/sd/AIRONE", 0777);
  struct stat st;
  for (int i = 1; i <= 9999; i++) {
    snprintf(g_sd_path, sizeof(g_sd_path), "/sd/AIRONE/F%04d.BIN", i);
    if (stat(g_sd_path, &st) != 0) {
      g_sd_file = fopen(g_sd_path, "wb");
      if (!g_sd_file) return false;
      setvbuf(g_sd_file, NULL, _IOFBF, 4096);
      return true;
    }
    if ((i & 31) == 0) wdt_kick_ext();
  }
  return false;
}

static bool sd_mount() {
  {
    SpiLock l(2000);
    if (!l.ok()) return false;
    SD.end();
    if (!SD.begin(g_sd_cs, SPI, STORAGE_SD_HZ, "/sd", 2, false)) { SD.end(); return false; }
    if (SD.cardType() == CARD_NONE) { SD.end(); return false; }
  }
  if (!sd_open_new_file()) { SpiLock l(2000); SD.end(); return false; }
  g_sd_last_sync_ms = millis();
  g_sd_unsynced = 0;
  return true;
}

static void sd_drop(const char* why) {
  Serial.printf("[STORAGE] SD FAILURE (%s) -- failing over to W25Q128 flash\n", why);
  SpiLock l(2000);
  if (g_sd_file) { fclose(g_sd_file); g_sd_file = NULL; }
  SD.end();
  g_sd_ok = false;
  g_log_sd_failovers = g_log_sd_failovers + 1;
  g_sd_last_retry_ms = millis();
}

static bool sd_write(const uint8_t* d, size_t n) {
  SpiLock l(1000);
  if (!l.ok()) return false;
  return fwrite(d, 1, n, g_sd_file) == n;
}

static bool sd_sync() {
  SpiLock l(2000);
  if (!l.ok()) return false;
  if (fflush(g_sd_file) != 0) return false;
  return fsync(fileno(g_sd_file)) == 0;
}

// =====================================================================
// Flash log
// =====================================================================
static bool flash_read_cb(uint32_t a, uint8_t* b, size_t n, void*) { return g_flash.read(a, b, n); }

static bool flash_append(const uint8_t* d, size_t n) {
  if (!g_flash_ok || g_flash_full) return false;
  if (g_flash_wp + n > g_flash.capacity()) {
    g_flash_full = true;
    Serial.println(F("[STORAGE] W25Q128 FULL -- flash logging stopped"));
    return false;
  }
  // Erase every sector the record will touch that is not known-erased.
  while (g_flash_erased_upto < g_flash_wp + n) {
    if (!g_flash.erase_sector(g_flash_erased_upto)) return false;
    g_flash_erased_upto += 4096;
  }
  if (!g_flash.program(g_flash_wp, d, n)) return false;
  g_flash_wp += n;
  return true;
}

static void flash_erase_log() {
  uint32_t end = g_flash_erased_upto > g_flash_wp ? g_flash_erased_upto : g_flash_wp;
  end = (end + 4095) & ~4095UL;
  Serial.printf("[STORAGE] erasing flash log (%lu kB)...\n", (unsigned long)(end / 1024));
  bool ok = true;
  for (uint32_t a = 0; a < end && ok; a += 4096) {
    ok = g_flash.sector_blank(a) || g_flash.erase_sector(a);
    if ((a & 0xFFFF) == 0) Serial.printf("[STORAGE]   %lu / %lu kB\n", (unsigned long)(a / 1024), (unsigned long)(end / 1024));
  }
  g_flash_wp = 0;
  g_flash_erased_upto = ok ? end : 0;
  g_flash_full = false;
  Serial.printf("[STORAGE] flash erase %s\n", ok ? "complete" : "FAILED");
}

// =====================================================================
// Storage task (core 0)
// =====================================================================
static void storage_task(void*) {
  static uint8_t rec[STORAGE_MAX_FRAME];
  for (;;) {
    ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(100));

    if (g_storage_erase_req) {
      g_storage_erase_req = false;
      if (g_flash_ok) flash_erase_log();
    }

    // Re-mount a lost card periodically.
    if (!g_sd_ok && g_sd_cs >= 0 && millis() - g_sd_last_retry_ms > STORAGE_SD_RETRY_MS) {
      g_sd_last_retry_ms = millis();
      if (sd_mount()) {
        g_sd_ok = true;
        Serial.printf("[STORAGE] SD re-mounted, logging to %s\n", g_sd_path);
      }
    }

    // Drain the ring.
    for (int burst = 0; burst < 16; burst++) {
      portENTER_CRITICAL(&g_ring_mux);
      uint32_t rd = g_ring_rd, wr = g_ring_wr;
      portEXIT_CRITICAL(&g_ring_mux);
      if (rd == wr) break;
      uint8_t hdr[2];
      ring_copy_out(rd, hdr, 2);
      uint32_t len = (uint32_t)hdr[0] | ((uint32_t)hdr[1] << 8);
      ring_copy_out(rd + 2, rec, len);
      uint32_t next = rd + 2 + len;

      if (g_sd_ok) {
        if (sd_write(rec, len)) {
          g_log_frames_sd = g_log_frames_sd + 1;
          g_sd_unsynced++;
          portENTER_CRITICAL(&g_ring_mux);
          g_ring_rd = next;              // keep stays put until fsync succeeds
          portEXIT_CRITICAL(&g_ring_mux);
        } else {
          sd_drop("write");
          portENTER_CRITICAL(&g_ring_mux);
          g_ring_rd = g_ring_keep;        // replay everything not yet durable
          portEXIT_CRITICAL(&g_ring_mux);
        }
        continue;
      }

      // Fallback sink: flash (durable immediately).
      if (flash_append(rec, len)) g_log_frames_flash = g_log_frames_flash + 1;
      else g_log_dropped = g_log_dropped + 1;   // no sink left: count, move on
      portENTER_CRITICAL(&g_ring_mux);
      g_ring_rd = next;
      g_ring_keep = next;
      portEXIT_CRITICAL(&g_ring_mux);
    }

    // Make SD writes durable.
    if (g_sd_ok && g_sd_unsynced &&
        (g_storage_sync_req || millis() - g_sd_last_sync_ms >= STORAGE_SD_SYNC_MS)) {
      g_storage_sync_req = false;
      if (sd_sync()) {
        portENTER_CRITICAL(&g_ring_mux);
        g_ring_keep = g_ring_rd;
        portEXIT_CRITICAL(&g_ring_mux);
        g_sd_unsynced = 0;
        g_sd_last_sync_ms = millis();
      } else {
        sd_drop("fsync");
        portENTER_CRITICAL(&g_ring_mux);
        g_ring_rd = g_ring_keep;
        portEXIT_CRITICAL(&g_ring_mux);
      }
    } else if (g_storage_sync_req && !g_sd_unsynced) {
      g_storage_sync_req = false;
    }

    // Idle: keep flash erased ahead of the write pointer so a failover is fast.
    portENTER_CRITICAL(&g_ring_mux);
    bool idle = (g_ring_rd == g_ring_wr);
    portEXIT_CRITICAL(&g_ring_mux);
    if (idle && g_flash_ok && g_sd_ok && !g_flash_full &&
        g_flash_erased_upto < g_flash.capacity() &&
        g_flash_erased_upto < g_flash_wp + STORAGE_PREERASE_BYTES) {
      if (g_flash.sector_blank(g_flash_erased_upto) || g_flash.erase_sector(g_flash_erased_upto))
        g_flash_erased_upto += 4096;
    }
  }
}

// =====================================================================
// Public API
// =====================================================================
static void storage_begin(int sd_cs, int flash_cs) {
  spi_bus_init();
  g_sd_cs = sd_cs;

  g_ring_cap = STORAGE_RING_BYTES;
  g_ring = (uint8_t*)malloc(g_ring_cap);
  if (!g_ring) {
    g_ring_cap = STORAGE_RING_BYTES / 2;
    g_ring = (uint8_t*)malloc(g_ring_cap);
  }
  Serial.printf("[STORAGE] RAM ring buffer: %lu bytes%s\n", (unsigned long)(g_ring ? g_ring_cap : 0),
                g_ring ? "" : " -- ALLOCATION FAILED, logging disabled");

  // ---- W25Q128 ----
  if (flash_cs >= 0) {
    g_flash_ok = g_flash.begin(flash_cs);
    if (g_flash_ok) {
      uint32_t skipped = 0;
      g_flash_wp = flashlog_find_end(g_flash.capacity(), flash_read_cb, NULL, wdt_kick_ext,
                                     &g_flash_boot_records, &skipped);
      g_flash_erased_upto = (g_flash_wp + 4095) & ~4095UL;   // rest of current sector is blank
      g_flash_full = g_flash_wp + STORAGE_MAX_FRAME > g_flash.capacity();
      Serial.printf("[STORAGE] W25Q128: OK (JEDEC %02X %04X, %lu MB) log=%lu frames, %lu kB used%s\n",
                    g_flash.manufacturer(), g_flash.device(),
                    (unsigned long)(g_flash.capacity() >> 20), (unsigned long)g_flash_boot_records,
                    (unsigned long)(g_flash_wp / 1024), skipped ? " (torn/garbage records skipped)" : "");
    } else {
      Serial.println(F("[STORAGE] W25Q128: FAIL (no JEDEC ID)"));
    }
  }
  wdt_kick_ext();

  // ---- MicroSD ----
  if (sd_cs >= 0) {
    g_sd_ok = sd_mount();
    if (g_sd_ok) {
      uint64_t mb = SD.cardSize() / (1024ULL * 1024ULL);
      Serial.printf("[STORAGE] MicroSD: OK (%llu MB) logging to %s\n", (unsigned long long)mb, g_sd_path);
    } else {
      Serial.println(F("[STORAGE] MicroSD: FAIL -- logging to W25Q128 flash"));
      g_sd_last_retry_ms = millis();
    }
  }
  wdt_kick_ext();

  if (g_ring) xTaskCreatePinnedToCore(storage_task, "storage", 8192, NULL, 2, &g_storage_task, 0);
}

static void storage_request_flash_erase() { g_storage_erase_req = true; if (g_storage_task) xTaskNotifyGive(g_storage_task); }

static void storage_print_status() {
  portENTER_CRITICAL(&g_ring_mux);
  uint32_t pending = g_ring_wr - g_ring_rd, unsynced = g_ring_rd - g_ring_keep;
  portEXIT_CRITICAL(&g_ring_mux);
  Serial.printf("[STORAGE] SD=%s %s | flash=%s wp=%lu/%lu kB%s | frames sd=%lu flash=%lu dropped=%lu failovers=%lu | ring pending=%lu unsynced=%lu B\n",
                g_sd_ok ? "OK" : "DOWN", g_sd_ok ? g_sd_path : "",
                g_flash_ok ? "OK" : "FAIL", (unsigned long)(g_flash_wp / 1024),
                (unsigned long)(g_flash.capacity() / 1024), g_flash_full ? " FULL" : "",
                (unsigned long)g_log_frames_sd, (unsigned long)g_log_frames_flash,
                (unsigned long)g_log_dropped, (unsigned long)g_log_sd_failovers,
                (unsigned long)pending, (unsigned long)unsynced);
}

/*
 * Dumps the flash log over Serial (blocking; ground use only):
 *   "AIRONE_FLASH_DUMP <nbytes>\n" <raw bytes> "\nAIRONE_FLASH_DUMP_END <crc32 hex>\n"
 * tools/airone_logtool.py dump captures this into a .bin file.
 */
static void storage_dump_flash() {
  if (!g_flash_ok) { Serial.println(F("[STORAGE] no flash")); return; }
  uint32_t n = g_flash_wp;
  Serial.printf("\nAIRONE_FLASH_DUMP %lu\n", (unsigned long)n);
  Serial.flush();
  static uint8_t buf[1024];
  uint32_t crc = 0xFFFFFFFFu;
  for (uint32_t a = 0; a < n; a += sizeof(buf)) {
    size_t c = (n - a) < sizeof(buf) ? (n - a) : sizeof(buf);
    if (!g_flash.read(a, buf, c)) memset(buf, 0xFF, c);
    for (size_t i = 0; i < c; i++) {
      crc ^= buf[i];
      for (int k = 0; k < 8; k++) crc = (crc >> 1) ^ (0xEDB88320u & -(crc & 1u));
    }
    Serial.write(buf, c);
    wdt_kick_ext();
  }
  Serial.printf("\nAIRONE_FLASH_DUMP_END %08lx\n", (unsigned long)(~crc));
}

#endif  // AIRONE_STORAGE_H
