/*
 * AirOne compact radio payload (PACKET_TYPE 0x10, flight -> ground bridge only).
 * ---------------------------------------------------------------------
 * A LoRa packet on the SX1268 is limited to 255 bytes, but the full JSON
 * SENSOR_DATA payload is ~2.5 kB. Over the radio the CanSat therefore sends
 * the SAME measurements as a fixed-schema binary payload wrapped in a normal
 * AirOne frame (MAGIC/VERSION/SEQ/TIMESTAMP/CRC32 [+ HMAC tag]) with
 * PACKET_TYPE = AIRONE_PT_COMPACT. The ground bridge (airone_ground_bridge)
 * expands it back into the standard JSON SENSOR_DATA frame, so the laptop
 * ground software needs no change. The full JSON frame is what is written to
 * the MicroSD card / W25Q128 flash on board.
 *
 * Compact payload layout (little-endian):
 *   0      1   schema version (AC_SCHEMA_VERSION)
 *   1      1   flags: bit0 = altitude source is BME688 (else BMP581)
 *   2      8   presence mask, bit i = field AC_FIELDS[i] present
 *   10     4*k one 4-byte value per present field, in index order
 *              AC_F32   IEEE-754 float
 *              AC_I32E7 int32, value * 1e7   (GNSS lat/lon, ~1 cm)
 *              AC_U32   uint32, rounded       (counters, enums, bitmasks)
 *
 * THIS FILE MUST BE IDENTICAL in airone_cansat/ and airone_ground_bridge/
 * (the host test checks this). Append new fields at the END only and bump
 * AC_SCHEMA_VERSION if an existing index ever changes meaning.
 */
#ifndef AIRONE_COMPACT_H
#define AIRONE_COMPACT_H

#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdio.h>
#include <math.h>

#define AIRONE_PT_COMPACT 0x10
#define AC_SCHEMA_VERSION 1
#define AC_HEADER_SIZE    10
#define AC_FLAG_ALT_BME688 0x01

enum { AC_F32 = 0, AC_I32E7 = 1, AC_U32 = 2 };

typedef struct {
  const char* name;
  const char* unit;
  const char* sensor_id;   // "@ALT" = altitude source (BMP581 / BME688)
  uint8_t type;
  uint8_t digits;          // significant digits when re-expanded to JSON
} AcField;

static const AcField AC_FIELDS[] = {
  /* 0 */ {"bme688_temperature",    "K",       "BME688",   AC_F32,   7},
  /* 1 */ {"bme688_pressure",       "Pa",      "BME688",   AC_F32,   8},
  /* 2 */ {"bme688_humidity",       "%",       "BME688",   AC_F32,   7},
  /* 3 */ {"bme688_gas_resistance", "Ohm",     "BME688",   AC_F32,   7},
  /* 4 */ {"bmp581_temperature",    "K",       "BMP581",   AC_F32,   7},
  /* 5 */ {"bmp581_pressure",       "Pa",      "BMP581",   AC_F32,   8},
  /* 6 */ {"sgp41_voc_raw",         "ticks",   "SGP41",    AC_U32,   7},
  /* 7 */ {"sgp41_nox_raw",         "ticks",   "SGP41",    AC_U32,   7},
  /* 8 */ {"sgp41_voc",             "index",   "SGP41",    AC_U32,   7},
  /* 9 */ {"sgp41_nox",             "index",   "SGP41",    AC_U32,   7},
  /*10 */ {"ens160_status",         "flag",    "ENS160",   AC_U32,   7},
  /*11 */ {"ens160_aqi",            "index",   "ENS160",   AC_U32,   7},
  /*12 */ {"ens160_tvoc",           "ppb",     "ENS160",   AC_U32,   7},
  /*13 */ {"ens160_eco2",           "ppm",     "ENS160",   AC_U32,   7},
  /*14 */ {"veml6075_uva",          "counts",  "VEML6075", AC_F32,   7},
  /*15 */ {"veml6075_uvb",          "counts",  "VEML6075", AC_F32,   7},
  /*16 */ {"opt3001_lux",           "lux",     "OPT3001",  AC_F32,   7},
  /*17 */ {"mag_x",                 "uT",      "MMC5603",  AC_F32,   7},
  /*18 */ {"mag_y",                 "uT",      "MMC5603",  AC_F32,   7},
  /*19 */ {"mag_z",                 "uT",      "MMC5603",  AC_F32,   7},
  /*20 */ {"battery_voltage",       "V",       "INA219",   AC_F32,   7},
  /*21 */ {"battery_current_ma",    "mA",      "INA219",   AC_F32,   7},
  /*22 */ {"imu_accel_x",           "m/s^2",   "BMI270",   AC_F32,   7},
  /*23 */ {"imu_accel_y",           "m/s^2",   "BMI270",   AC_F32,   7},
  /*24 */ {"imu_accel_z",           "m/s^2",   "BMI270",   AC_F32,   7},
  /*25 */ {"imu_gyro_x",            "deg/s",   "BMI270",   AC_F32,   7},
  /*26 */ {"imu_gyro_y",            "deg/s",   "BMI270",   AC_F32,   7},
  /*27 */ {"imu_gyro_z",            "deg/s",   "BMI270",   AC_F32,   7},
  /*28 */ {"radiation_cpm",         "CPM",     "SEN0463",  AC_U32,   7},
  /*29 */ {"radiation_counts",      "counts",  "SEN0463",  AC_U32,   10},
  /*30 */ {"gnss_lat",              "deg",     "MAX-M10S", AC_I32E7, 10},
  /*31 */ {"gnss_lon",              "deg",     "MAX-M10S", AC_I32E7, 10},
  /*32 */ {"gnss_altitude",         "m",       "MAX-M10S", AC_F32,   7},
  /*33 */ {"altitude_rel",          "m",       "@ALT",     AC_F32,   7},
  /*34 */ {"vertical_speed",        "m/s",     "@ALT",     AC_F32,   7},
  /*35 */ {"mission_state_code",    "enum",    "FSM",      AC_U32,   7},
  /*36 */ {"sensor_health_mask",    "bitmask", "FSM",      AC_U32,   10},
  /*37 */ {"gnss_satellites",       "count",   "MAX-M10S", AC_U32,   7},
  /*38 */ {"gnss_hdop",             "hdop",    "MAX-M10S", AC_F32,   7},
};
#define AC_NUM_FIELDS ((int)(sizeof(AC_FIELDS) / sizeof(AC_FIELDS[0])))
#define AC_MAX_PAYLOAD (AC_HEADER_SIZE + 4 * AC_NUM_FIELDS)
// Whole radio frame = 21 header + payload + 8 auth tag + 4 CRC must fit 255.
typedef char ac_fits_one_lora_packet[(21 + AC_MAX_PAYLOAD + 8 + 4 <= 255) ? 1 : -1];
typedef char ac_fits_mask[(AC_NUM_FIELDS <= 64) ? 1 : -1];

// Mission state names, index = mission_state_code.
static const char* const AC_STATE_NAMES[] = {
  "BOOT", "SELF_TEST", "PRELAUNCH", "ASCENT", "APOGEE", "DESCENT", "LANDED"
};
#define AC_NUM_STATES 7

static inline int ac_field_index(const char* name) {
  for (int i = 0; i < AC_NUM_FIELDS; i++)
    if (strcmp(AC_FIELDS[i].name, name) == 0) return i;
  return -1;
}

// ---------------------------------------------------------------------
// Encoder (flight side)
// ---------------------------------------------------------------------
typedef struct {
  uint64_t mask;
  uint32_t raw[AC_NUM_FIELDS];   // 4-byte wire representation
  uint8_t  flags;
} AcEncoder;

static inline void ac_reset(AcEncoder* e) { e->mask = 0; e->flags = 0; }

// Stores a value by field index. Returns false if out of range / not finite.
static inline int ac_set(AcEncoder* e, int idx, double v) {
  if (idx < 0 || idx >= AC_NUM_FIELDS || !isfinite(v)) return 0;
  uint32_t r;
  switch (AC_FIELDS[idx].type) {
    case AC_I32E7: {
      double s = v * 1e7;
      if (s > 2147483647.0 || s < -2147483648.0) return 0;
      int32_t q = (int32_t)llround(s);
      memcpy(&r, &q, 4);
      break;
    }
    case AC_U32: {
      if (v < 0 || v > 4294967295.0) return 0;
      r = (uint32_t)llround(v);
      break;
    }
    default: {
      float f = (float)v;
      if (!isfinite(f)) return 0;
      memcpy(&r, &f, 4);
    }
  }
  e->raw[idx] = r;
  e->mask |= (1ULL << idx);
  return 1;
}

// Serialises the payload. Returns its length, or 0 if out_cap is too small.
static inline size_t ac_encode(const AcEncoder* e, uint8_t* out, size_t out_cap) {
  size_t need = AC_HEADER_SIZE;
  for (int i = 0; i < AC_NUM_FIELDS; i++) if (e->mask & (1ULL << i)) need += 4;
  if (need > out_cap) return 0;
  out[0] = AC_SCHEMA_VERSION;
  out[1] = e->flags;
  for (int b = 0; b < 8; b++) out[2 + b] = (uint8_t)(e->mask >> (8 * b));
  size_t o = AC_HEADER_SIZE;
  for (int i = 0; i < AC_NUM_FIELDS; i++) {
    if (!(e->mask & (1ULL << i))) continue;
    for (int b = 0; b < 4; b++) out[o++] = (uint8_t)(e->raw[i] >> (8 * b));
  }
  return o;
}

// ---------------------------------------------------------------------
// Decoder / JSON expander (ground bridge side, also used by host tests)
// ---------------------------------------------------------------------
// Decodes field idx into *v. Returns 1 if present.
static inline int ac_decode_values(const uint8_t* p, size_t len, double values[AC_NUM_FIELDS],
                                   uint64_t* mask_out, uint8_t* flags_out) {
  if (len < AC_HEADER_SIZE || p[0] != AC_SCHEMA_VERSION) return 0;
  uint64_t mask = 0;
  for (int b = 0; b < 8; b++) mask |= (uint64_t)p[2 + b] << (8 * b);
  // Unknown (future) bits beyond our table cannot be sized -> reject.
  if (AC_NUM_FIELDS < 64 && (mask >> AC_NUM_FIELDS) != 0) return 0;
  size_t need = AC_HEADER_SIZE;
  for (int i = 0; i < AC_NUM_FIELDS; i++) if (mask & (1ULL << i)) need += 4;
  if (need != len) return 0;
  size_t o = AC_HEADER_SIZE;
  for (int i = 0; i < AC_NUM_FIELDS; i++) {
    if (!(mask & (1ULL << i))) continue;
    uint32_t r = (uint32_t)p[o] | ((uint32_t)p[o + 1] << 8) |
                 ((uint32_t)p[o + 2] << 16) | ((uint32_t)p[o + 3] << 24);
    o += 4;
    switch (AC_FIELDS[i].type) {
      case AC_I32E7: { int32_t q; memcpy(&q, &r, 4); values[i] = q / 1e7; break; }
      case AC_U32:   values[i] = (double)r; break;
      default:       { float f; memcpy(&f, &r, 4); values[i] = f; }
    }
  }
  *mask_out = mask;
  *flags_out = p[1];
  return 1;
}

static inline int ac_json_append(char* out, size_t cap, size_t* len, const char* s, int n) {
  if (n <= 0 || *len + (size_t)n + 2 > cap) return 0;   // keep room for '}' + NUL
  memcpy(out + *len, s, (size_t)n);
  *len += (size_t)n;
  return 1;
}

/*
 * Expands a compact payload into the standard JSON SENSOR_DATA payload
 * (field -> {"value","unit","sensor_id"}), adding mission_state (string) from
 * mission_state_code and, when finite, ground-measured radio_rssi / radio_snr.
 * Returns the JSON length (NUL-terminated), or 0 on a malformed payload or if
 * out_cap is too small.
 */
static inline size_t ac_expand_json(const uint8_t* p, size_t plen, char* out, size_t cap,
                                    double rssi_dbm, double snr_db) {
  double v[AC_NUM_FIELDS];
  uint64_t mask; uint8_t flags;
  if (cap < 4 || !ac_decode_values(p, plen, v, &mask, &flags)) return 0;
  const char* alt_src = (flags & AC_FLAG_ALT_BME688) ? "BME688" : "BMP581";
  size_t len = 0;
  out[len++] = '{';
  int first = 1;
  char tmp[192];
  for (int i = 0; i < AC_NUM_FIELDS; i++) {
    if (!(mask & (1ULL << i))) continue;
    const AcField* f = &AC_FIELDS[i];
    const char* sid = (strcmp(f->sensor_id, "@ALT") == 0) ? alt_src : f->sensor_id;
    int n = snprintf(tmp, sizeof(tmp), "%s\"%s\":{\"value\":%.*g,\"unit\":\"%s\",\"sensor_id\":\"%s\"}",
                     first ? "" : ",", f->name, (int)f->digits, v[i], f->unit, sid);
    if (!ac_json_append(out, cap, &len, tmp, n)) return 0;
    first = 0;
    if (i == 35 /* mission_state_code */ && v[i] >= 0 && v[i] < AC_NUM_STATES) {
      n = snprintf(tmp, sizeof(tmp), ",\"mission_state\":{\"value\":\"%s\",\"unit\":\"enum\",\"sensor_id\":\"FSM\"}",
                   AC_STATE_NAMES[(int)v[i]]);
      if (!ac_json_append(out, cap, &len, tmp, n)) return 0;
    }
  }
  if (isfinite(rssi_dbm)) {
    int n = snprintf(tmp, sizeof(tmp), "%s\"radio_rssi\":{\"value\":%.4g,\"unit\":\"dBm\",\"sensor_id\":\"GROUND_RX\"}",
                     first ? "" : ",", rssi_dbm);
    if (!ac_json_append(out, cap, &len, tmp, n)) return 0;
    first = 0;
  }
  if (isfinite(snr_db)) {
    int n = snprintf(tmp, sizeof(tmp), "%s\"radio_snr\":{\"value\":%.4g,\"unit\":\"dB\",\"sensor_id\":\"GROUND_RX\"}",
                     first ? "" : ",", snr_db);
    if (!ac_json_append(out, cap, &len, tmp, n)) return 0;
  }
  out[len++] = '}';
  out[len] = '\0';
  return len;
}

#endif  // AIRONE_COMPACT_H
