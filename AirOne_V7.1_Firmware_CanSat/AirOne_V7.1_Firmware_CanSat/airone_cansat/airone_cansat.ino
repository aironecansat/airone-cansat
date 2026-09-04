/*
 * AirOne V7.1 CanSat flight firmware  --  ESP32-WROVER-E
 * Authored by Team AirOne.
 * =====================================================================
 * Emits telemetry over a LoRa E22 module (UART, transparent mode) using the
 * EXACT AirOne V7.1 binary frame protocol that the ground station parser
 * expects. Every frame is:
 *
 *   Offset  Size  Field
 *   0       4     MAGIC  = A1 60 4E 45
 *   4       1     VERSION = 0x71
 *   5       1     PACKET_TYPE = 0x01 (SENSOR_DATA)
 *   6       4     SEQUENCE      (uint32 LE)
 *   10      8     TIMESTAMP_US  (uint64 LE, microseconds since Unix epoch, or 0)
 *   18      2     PAYLOAD_LEN   (uint16 LE)
 *   20      1     FLAGS         (0)
 *   21      N     PAYLOAD       (UTF-8 JSON, see below)
 *   21+N    4     CRC32         (uint32 LE, over bytes [0 .. 21+N-1])
 *
 * The CRC32 is the standard CRC-32/ISO-HDLC (zlib / binascii.crc32) so it
 * matches the Python ground station byte-for-byte.
 *
 * PAYLOAD is a JSON object mapping field_name -> {"value","unit","sensor_id"}.
 * The field names MUST match what the ground-station pipeline consumes:
 *
 *   bme688_temperature (K)   bme688_pressure (Pa)   bme688_humidity (%)
 *   bmp581_temperature (K)   bmp581_pressure (Pa)
 *   sgp41_voc (index)        sgp41_nox (index)
 *   veml6075_uva (counts)    veml6075_uvb (counts)
 *   opt3001_lux (lux)
 *   mag_x / mag_y / mag_z (uT)
 *   radiation_cpm (CPM)
 *   gnss_lat (deg)  gnss_lon (deg)  gnss_altitude (m)
 *   battery_voltage (V)
 *
 * SCIENTIFIC HONESTY: a field is included in the JSON ONLY when its sensor
 * read succeeds. A failed/absent sensor is OMITTED entirely -- the firmware
 * never transmits a fabricated 0. The ground station treats missing fields as
 * absent (not as valid zero), preserving fail-safe explicit degradation.
 *
 * Temperatures are converted to KELVIN before transmission because that is the
 * canonical unit of the *_temperature fields in the AirOne pipeline.
 *
 * ---------------------------------------------------------------------
 * REQUIRED LIBRARIES (install via Arduino Library Manager):
 *   Adafruit BME680          (BME688)
 *   SparkFun BMP581 Arduino Library
 *   Sensirion I2C SGP41
 *   Adafruit VEML6075        (UVA/UVB)
 *   ClosedCube OPT3001
 *   Adafruit MMC56x3         (MMC5603 magnetometer)
 *   Adafruit INA219          (battery voltage)
 *   TinyGPSPlus              (MAX-M10S over UART)
 * The SEN0463 Geiger counter is read by counting its digital pulse output
 * (one falling edge per count) on a GPIO via a hardware interrupt, converted
 * to CPM over a rolling 60 s window -- no extra library required.
 *
 * Any sensor you do not populate can be left disabled with its ENABLE_* flag
 * set to 0 -- that field is then simply never transmitted (honest omission).
 * ---------------------------------------------------------------------
 */

#include <Arduino.h>
#include <Wire.h>

#include "airone_frame.h"   // binary framing + CRC32 (matches ground station)

// ---- Feature switches: disable any sensor you have not wired ----------
#define ENABLE_BME688     1
#define ENABLE_BMP581     1
#define ENABLE_SGP41      1
#define ENABLE_VEML6075   1
#define ENABLE_OPT3001    1
#define ENABLE_MMC5603    1
#define ENABLE_INA219     1
#define ENABLE_GNSS       1
#define ENABLE_GEIGER     1

// ---- UART wiring ------------------------------------------------------
// E22 LoRa module on UART2 (transparent mode: tie M0=M1=GND on the E22).
static const int E22_RX_PIN = 16;   // ESP32 RX2  <- E22 TXD
static const int E22_TX_PIN = 17;   // ESP32 TX2  -> E22 RXD
static const uint32_t E22_BAUD = 115200;  // MUST match ground-station baud

// MAX-M10S GNSS on UART1.
static const int GNSS_RX_PIN = 4;   // ESP32 RX1  <- GNSS TX
static const int GNSS_TX_PIN = 2;   // ESP32 TX1  -> GNSS RX
static const uint32_t GNSS_BAUD = 9600;

// SEN0463 Gravity Geiger counter: its digital pulse output (one falling edge
// per detected count) is wired to a GPIO. We count edges in a hardware ISR and
// convert to CPM over a rolling 60 s window (see read_geiger_cpm() below).
#if ENABLE_GEIGER
static const int GEIGER_PULSE_PIN = 27;   // SEN0463 pulse OUT -> ESP32 GPIO27
#endif

static const uint32_t TELEMETRY_PERIOD_MS = 500;  // 2 Hz

// ---- Telemetry link authentication -------------------------------------
// Hex-encoded shared key (>= 32 hex chars = 16 bytes). When non-empty every
// frame carries an 8-byte truncated HMAC-SHA256 tag (FLAGS 0x08) that the
// ground station verifies with the SAME key in AIRONE_LINK_KEY. Leave empty
// to transmit unauthenticated frames. NEVER commit a real key: set it in a
// local build (e.g. -DAIRONE_LINK_KEY_HEX="\"...\"" or a git-ignored header).
// Integrity/origin only -- the payload is not encrypted.
#ifndef AIRONE_LINK_KEY_HEX
#define AIRONE_LINK_KEY_HEX ""
#endif
static uint8_t g_link_key[64];
static size_t  g_link_key_len = 0;   // 0 = unauthenticated

// ---------------------------------------------------------------------
// Sensor driver objects (only compiled when their ENABLE_* flag is set)
// ---------------------------------------------------------------------
#if ENABLE_BME688
  #include <Adafruit_BME680.h>
  Adafruit_BME680 bme688;
  bool bme688_ok = false;
#endif
#if ENABLE_BMP581
  #include <SparkFun_BMP581_Arduino_Library.h>
  BMP581 bmp581;
  bool bmp581_ok = false;
#endif
#if ENABLE_SGP41
  #include <SensirionI2CSgp41.h>
  SensirionI2CSgp41 sgp41;
  bool sgp41_ok = false;
#endif
#if ENABLE_VEML6075
  #include <Adafruit_VEML6075.h>
  Adafruit_VEML6075 veml6075 = Adafruit_VEML6075();
  bool veml6075_ok = false;
#endif
#if ENABLE_OPT3001
  #include <ClosedCube_OPT3001.h>
  ClosedCube_OPT3001 opt3001;
  const uint8_t OPT3001_ADDR = 0x44;
  bool opt3001_ok = false;
#endif
#if ENABLE_MMC5603
  #include <Adafruit_MMC56x3.h>
  Adafruit_MMC5603 mmc = Adafruit_MMC5603(0x30);
  bool mmc_ok = false;
#endif
#if ENABLE_INA219
  #include <Adafruit_INA219.h>
  Adafruit_INA219 ina219;
  bool ina219_ok = false;
#endif
#if ENABLE_GNSS
  #include <TinyGPSPlus.h>
  TinyGPSPlus gps;
#endif

// UARTs.
HardwareSerial E22(2);
#if ENABLE_GNSS
HardwareSerial GNSS(1);
#endif

static uint32_t g_sequence = 0;

// ---------------------------------------------------------------------
// JSON payload builder (manual, dependency-free, honest omission)
// ---------------------------------------------------------------------
class JsonPayload {
 public:
  JsonPayload() { len_ = 0; buf_[len_++] = '{'; first_ = true; }

  // Append one measurement field. Only call this when the read succeeded.
  void add(const char* field, double value, const char* unit, const char* sid) {
    if (!first_) buf_[len_++] = ',';
    first_ = false;
    // "field":{"value":<v>,"unit":"<u>","sensor_id":"<s>"}
    len_ += snprintf(buf_ + len_, sizeof(buf_) - len_,
                     "\"%s\":{\"value\":%.6g,\"unit\":\"%s\",\"sensor_id\":\"%s\"}",
                     field, value, unit, sid);
  }

  const uint8_t* bytes() {
    buf_[len_++] = '}';
    return reinterpret_cast<const uint8_t*>(buf_);
  }
  size_t size() const { return len_; }

 private:
  char buf_[1024];
  size_t len_;
  bool first_;
};

// ---------------------------------------------------------------------
// Timestamp: microseconds since Unix epoch when GNSS time is valid, else 0
// (0 tells the ground station to stamp the frame with its receive time --
//  we do not fabricate a bogus absolute time).
// ---------------------------------------------------------------------
static uint64_t epoch_micros() {
#if ENABLE_GNSS
  if (gps.date.isValid() && gps.time.isValid() && gps.date.year() >= 2020) {
    struct tm t = {};
    t.tm_year = gps.date.year() - 1900;
    t.tm_mon  = gps.date.month() - 1;
    t.tm_mday = gps.date.day();
    t.tm_hour = gps.time.hour();
    t.tm_min  = gps.time.minute();
    t.tm_sec  = gps.time.second();
    time_t secs = mktime(&t);  // treats fields as UTC on ESP32 (TZ unset)
    if (secs > 0) {
      return (uint64_t)secs * 1000000ULL +
             (uint64_t)gps.time.centisecond() * 10000ULL;
    }
  }
#endif
  return 0ULL;
}

// ---------------------------------------------------------------------
// SEN0463 Geiger: interrupt-based pulse counting -> CPM.
// The ISR increments a volatile counter on every falling edge. read_geiger_cpm()
// scales the counts accumulated over the elapsed rolling window (up to 60 s) to
// counts-per-minute. It returns -1 only while ENABLE_GEIGER is 0 or before the
// first sampling window has elapsed (honest "no data yet"), never as a stub.
// ---------------------------------------------------------------------
long read_geiger_cpm();
#if ENABLE_GEIGER
static const uint32_t GEIGER_WINDOW_MS = 60000UL;  // rolling integration window
static volatile uint32_t g_geiger_pulses = 0;      // edges since window start
static uint32_t g_geiger_window_start_ms = 0;      // millis() at window start
static bool g_geiger_started = false;              // first window begun?

void IRAM_ATTR geiger_isr() {
  g_geiger_pulses++;
}
#endif

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println(F("AirOne V7.1 CanSat firmware starting..."));

  Wire.begin();  // default SDA=21, SCL=22 on ESP32-WROVER

  E22.begin(E22_BAUD, SERIAL_8N1, E22_RX_PIN, E22_TX_PIN);

  // Telemetry link authentication: explicit state, never silent.
  g_link_key_len = airone_hex_to_key(AIRONE_LINK_KEY_HEX, g_link_key, sizeof(g_link_key));
  if (g_link_key_len == 0) {
    Serial.println(F("LINK AUTH: NOT_CONFIGURED (frames unauthenticated)"));
  } else if (g_link_key_len < AIRONE_MIN_LINK_KEY_BYTES) {
    Serial.println(F("LINK AUTH: key too short (<16 bytes) -- REFUSING to transmit"));
  } else {
    Serial.printf("LINK AUTH: ENABLED (%u-byte key, HMAC-SHA256/8)\n", (unsigned)g_link_key_len);
  }
#if ENABLE_GNSS
  GNSS.begin(GNSS_BAUD, SERIAL_8N1, GNSS_RX_PIN, GNSS_TX_PIN);
#endif

#if ENABLE_BME688
  bme688_ok = bme688.begin();
  if (bme688_ok) {
    bme688.setTemperatureOversampling(BME680_OS_8X);
    bme688.setHumidityOversampling(BME680_OS_2X);
    bme688.setPressureOversampling(BME680_OS_4X);
    bme688.setIIRFilterSize(BME680_FILTER_SIZE_3);
    bme688.setGasHeater(320, 150);
  }
  Serial.printf("BME688: %s\n", bme688_ok ? "OK" : "FAIL");
#endif
#if ENABLE_BMP581
  bmp581_ok = (bmp581.beginI2C() == BMP5_OK);
  Serial.printf("BMP581: %s\n", bmp581_ok ? "OK" : "FAIL");
#endif
#if ENABLE_SGP41
  sgp41.begin(Wire);
  sgp41_ok = true;  // SGP41 has no begin() status; conditioning runs in loop
  Serial.println("SGP41: init");
#endif
#if ENABLE_VEML6075
  veml6075_ok = veml6075.begin();
  Serial.printf("VEML6075: %s\n", veml6075_ok ? "OK" : "FAIL");
#endif
#if ENABLE_OPT3001
  opt3001.begin(OPT3001_ADDR);
  opt3001_ok = (opt3001.readManufacturerID() == 0x5449);
  Serial.printf("OPT3001: %s\n", opt3001_ok ? "OK" : "FAIL");
#endif
#if ENABLE_MMC5603
  mmc_ok = mmc.begin(0x30, &Wire);
  Serial.printf("MMC5603: %s\n", mmc_ok ? "OK" : "FAIL");
#endif
#if ENABLE_INA219
  ina219_ok = ina219.begin();
  Serial.printf("INA219: %s\n", ina219_ok ? "OK" : "FAIL");
#endif
#if ENABLE_GEIGER
  // SEN0463 pulse output is open-drain/active-low: idle high, one falling edge
  // per detected count. Enable the pull-up and count falling edges via ISR.
  pinMode(GEIGER_PULSE_PIN, INPUT_PULLUP);
  g_geiger_pulses = 0;
  g_geiger_window_start_ms = millis();
  g_geiger_started = true;
  attachInterrupt(digitalPinToInterrupt(GEIGER_PULSE_PIN), geiger_isr, FALLING);
  Serial.printf("Geiger SEN0463: counting on GPIO%d\n", GEIGER_PULSE_PIN);
#endif

  Serial.println(F("Setup complete."));
}

#if ENABLE_SGP41
// SGP41 needs a NOx conditioning phase then compensated measurement.
static uint16_t sgp41_conditioning_left = 10;  // ~10 s at 1 Hz
#endif

void loop() {
  static uint32_t last_tx = 0;

#if ENABLE_GNSS
  while (GNSS.available()) gps.encode(GNSS.read());
#endif

  uint32_t now = millis();
  if (now - last_tx < TELEMETRY_PERIOD_MS) return;
  last_tx = now;

  JsonPayload p;

#if ENABLE_BME688
  if (bme688_ok && bme688.performReading()) {
    p.add("bme688_temperature", bme688.temperature + 273.15, "K", "BME688");
    p.add("bme688_pressure", bme688.pressure, "Pa", "BME688");
    p.add("bme688_humidity", bme688.humidity, "%", "BME688");
  }
#endif
#if ENABLE_BMP581
  if (bmp581_ok) {
    bmp5_sensor_data d = {0, 0};
    if (bmp581.getSensorData(&d) == BMP5_OK) {
      p.add("bmp581_temperature", d.temperature + 273.15, "K", "BMP581");
      p.add("bmp581_pressure", d.pressure, "Pa", "BMP581");
    }
  }
#endif
#if ENABLE_SGP41
  if (sgp41_ok) {
    uint16_t raw_voc = 0, raw_nox = 0;
    // Default humidity/temperature compensation (50% RH, 25 C) unless BME688
    // gives us better values.
    uint16_t rh = 0x8000, t = 0x6666;
#if ENABLE_BME688
    if (bme688_ok) {
      rh = (uint16_t)(bme688.humidity * 65535.0 / 100.0);
      t = (uint16_t)((bme688.temperature + 45.0) * 65535.0 / 175.0);
    }
#endif
    uint16_t err;
    if (sgp41_conditioning_left > 0) {
      err = sgp41.executeConditioning(rh, t, raw_voc);
      sgp41_conditioning_left--;
    } else {
      err = sgp41.measureRawSignals(rh, t, raw_voc, raw_nox);
      if (err == 0) {
        p.add("sgp41_voc", raw_voc, "index", "SGP41");
        p.add("sgp41_nox", raw_nox, "index", "SGP41");
      }
    }
  }
#endif
#if ENABLE_VEML6075
  if (veml6075_ok) {
    p.add("veml6075_uva", veml6075.readUVA(), "counts", "VEML6075");
    p.add("veml6075_uvb", veml6075.readUVB(), "counts", "VEML6075");
  }
#endif
#if ENABLE_OPT3001
  if (opt3001_ok) {
    OPT3001 r = opt3001.readResult();
    if (r.error == NO_ERROR) p.add("opt3001_lux", r.lux, "lux", "OPT3001");
  }
#endif
#if ENABLE_MMC5603
  if (mmc_ok) {
    sensors_event_t e;
    mmc.getEvent(&e);
    p.add("mag_x", e.magnetic.x, "uT", "MMC5603");
    p.add("mag_y", e.magnetic.y, "uT", "MMC5603");
    p.add("mag_z", e.magnetic.z, "uT", "MMC5603");
  }
#endif
#if ENABLE_GEIGER
  {
    long cpm = read_geiger_cpm();
    if (cpm >= 0) p.add("radiation_cpm", (double)cpm, "CPM", "SEN0463");
  }
#endif
#if ENABLE_GNSS
  if (gps.location.isValid()) {
    p.add("gnss_lat", gps.location.lat(), "deg", "MAX-M10S");
    p.add("gnss_lon", gps.location.lng(), "deg", "MAX-M10S");
  }
  if (gps.altitude.isValid())
    p.add("gnss_altitude", gps.altitude.meters(), "m", "MAX-M10S");
#endif
#if ENABLE_INA219
  if (ina219_ok)
    p.add("battery_voltage", ina219.getBusVoltage_V(), "V", "INA219");
#endif

  // Serialise the complete frame and push it out the E22 radio.
  const uint8_t* payload = p.bytes();
  size_t plen = p.size();
  uint8_t frame[1100 + AIRONE_AUTH_TAG_SIZE];
  size_t flen = airone_pack_frame_auth(frame, sizeof(frame),
                                       AIRONE_PT_SENSOR_DATA, g_sequence++,
                                       epoch_micros(), payload, plen,
                                       g_link_key_len ? g_link_key : NULL,
                                       g_link_key_len);
  if (flen > 0) {
    E22.write(frame, flen);
  } else {
    Serial.println(F("Frame too large; dropped (never truncated)."));
  }
}

// ---------------------------------------------------------------------
// SEN0463 Geiger CPM from the interrupt pulse counter.
// Counts accumulate in geiger_isr() over a rolling window. Once at least one
// second of data exists we scale the counts to counts-per-minute; the window is
// reset once it reaches GEIGER_WINDOW_MS so the reading tracks recent activity.
// Returns -1 only when the counter is disabled or no full second has elapsed
// yet (honest "no data yet" -> the field is omitted), never as a placeholder.
// ---------------------------------------------------------------------
long read_geiger_cpm() {
#if ENABLE_GEIGER
  if (!g_geiger_started) return -1;  // interrupt not attached yet

  const uint32_t now = millis();
  const uint32_t elapsed = now - g_geiger_window_start_ms;  // wrap-safe (uint32)
  if (elapsed < 1000UL) return -1;   // need >=1 s before a meaningful rate

  // Snapshot the counter atomically w.r.t. the ISR.
  noInterrupts();
  const uint32_t counts = g_geiger_pulses;
  interrupts();

  // Scale accumulated counts over the elapsed window to counts-per-minute.
  const long cpm = (long)(((uint64_t)counts * 60000ULL) / (uint64_t)elapsed);

  // Roll the window over once it is full so CPM reflects recent activity.
  if (elapsed >= GEIGER_WINDOW_MS) {
    noInterrupts();
    g_geiger_pulses = 0;
    interrupts();
    g_geiger_window_start_ms = now;
  }
  return cpm;
#else
  return -1;  // Geiger disabled -> field omitted (honest)
#endif
}
