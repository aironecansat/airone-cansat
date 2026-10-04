/*
 * Append-only frame log on the W25Q128 NOR flash -- recovery logic.
 * ---------------------------------------------------------------------
 * The flash log is simply AirOne frames written back to back from address 0
 * (each frame carries its own MAGIC + length + CRC, so no extra index is
 * needed and a raw dump can be fed straight to the decoder). Erased NOR
 * reads 0xFF, so the end of the log is the first record slot that starts
 * with FF FF FF FF.
 *
 * flashlog_find_end() walks the records at boot to find the append point.
 * A record torn by a power loss (header written, body partly written) is
 * skipped using its declared length; real garbage triggers a forward scan
 * for the next MAGIC or a run of erased bytes. Pure C++ with a read
 * callback so it is unit-tested on the host.
 */
#ifndef AIRONE_FLASHLOG_H
#define AIRONE_FLASHLOG_H

#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define FLASHLOG_MAX_RECORD 8192u          // larger declared lengths = garbage
#define FLASHLOG_ERASED_RUN 16u            // this many 0xFF = end of log

// read(addr, buf, n) -> true on success. tick() is called periodically
// (watchdog). Returns the append address (== capacity when full).
typedef bool (*flashlog_read_fn)(uint32_t addr, uint8_t* buf, size_t n, void* ctx);

static inline uint32_t flashlog_find_end(uint32_t capacity, flashlog_read_fn rd, void* ctx,
                                         void (*tick)(void), uint32_t* records_out,
                                         uint32_t* skipped_out) {
  static const uint8_t MAGIC[4] = {0xA1, 0x60, 0x4E, 0x45};
  uint32_t pos = 0, records = 0, skipped = 0, n_iter = 0;
  uint8_t h[21];
  while (pos + 21 <= capacity) {
    if (tick && (++n_iter & 63) == 0) tick();
    if (!rd(pos, h, sizeof(h), ctx)) break;
    if (h[0] == 0xFF && h[1] == 0xFF && h[2] == 0xFF && h[3] == 0xFF) {
      // Make sure this is really erased space, not a 0xFF run inside a torn record.
      bool erased = true;
      for (size_t i = 4; i < sizeof(h); i++) if (h[i] != 0xFF) { erased = false; break; }
      if (erased) break;
    }
    if (memcmp(h, MAGIC, 4) == 0) {
      uint32_t plen = (uint32_t)h[18] | ((uint32_t)h[19] << 8);
      uint32_t tag = (h[20] & 0x08) ? 8u : 0u;
      uint32_t total = 21u + plen + tag + 4u;
      if (plen <= FLASHLOG_MAX_RECORD && pos + total <= capacity) {
        pos += total;
        records++;
        continue;
      }
    }
    // Garbage: scan forward byte-wise (in chunks) for MAGIC or an erased run.
    skipped++;
    uint32_t scan = pos + 1;
    uint8_t chunk[256 + 16];
    bool found = false;
    while (scan + 4 <= capacity && !found) {
      if (tick && (++n_iter & 63) == 0) tick();
      uint32_t n = capacity - scan;
      if (n > sizeof(chunk)) n = sizeof(chunk);
      if (!rd(scan, chunk, n, ctx)) { scan = capacity; break; }
      uint32_t ff_run = 0;
      for (uint32_t i = 0; i < n; i++) {
        if (i + 4 <= n && memcmp(chunk + i, MAGIC, 4) == 0) { scan += i; found = true; break; }
        ff_run = (chunk[i] == 0xFF) ? ff_run + 1 : 0;
        if (ff_run >= FLASHLOG_ERASED_RUN) { scan += i + 1 - ff_run; found = true; break; }
      }
      if (!found) scan += (n > 16 ? n - 16 : n);   // overlap so MAGIC/runs on a boundary are seen
    }
    pos = found ? scan : capacity;
    if (pos >= capacity) break;
  }
  if (pos > capacity) pos = capacity;
  if (records_out) *records_out = records;
  if (skipped_out) *skipped_out = skipped;
  return pos;
}

#endif  // AIRONE_FLASHLOG_H
