/*
 * AirOne CanSat flight firmware  --  ESP32-WROVER-E-N16R8
 * Authored by Team AirOne.
 * =====================================================================
 * Includes all 15 documented bug fixes and the "AirChem-Rad" secondary
 * mission (vertical profile of atmospheric chemistry, UV and ionising
 * radiation, plus descent spin dynamics). See SECONDARY_MISSION.md.
 *
 * Every 2 Hz sample is built as the UNCHANGED AirOne binary frame
 * (airone_frame.h) and logged by airone_storage.h to MicroSD, with W25Q128
 * NOR flash as failover. Over the air the EBYTE E22-400M30S (SX1268, SPI,
 * RadioLib -- airone_radio.h) carries the SAME measurements as a compact
 * frame (PACKET_TYPE 0x10, airone_compact.h, <= 255 B) with the same
 * sequence/timestamp. A 10 % duty-cycle limiter applies (UK 433 MHz). The ground
 * bridge sketch (../airone_ground_bridge) expands it back to the frame below:
 *
 *   Offset  Size  Field
 *   0       4     MAGIC  = A1 60 4E 45
 *   4       1     VERSION = 0x71
 *   5       1     PACKET_TYPE = 0x01 (SENSOR_DATA)
 *   6       4     SEQUENCE      (uint32 LE)  -- persisted in FRAM (survives brownout)
 *   10      8     TIMESTAMP_US  (uint64 LE, us since Unix epoch: GNSS -> DS3231 -> 0)
 *   18      2     PAYLOAD_LEN   (uint16 LE)
 *   20      1     FLAGS         (0, or 0x08 when HMAC-authenticated)
 *   21      N     PAYLOAD       (UTF-8 JSON)
 *   21+N    4     CRC32         (CRC-32/ISO-HDLC == Python binascii.crc32)
 *
 * PAYLOAD is a JSON object: field_name -> {"value","unit","sensor_id"}.
 *
 *  PRIMARY MISSION (unchanged field names)
 *   bme688_temperature (K)  bme688_pressure (Pa)  bme688_humidity (%)
 *   bmp581_temperature (K)  bmp581_pressure (Pa)
 *   gnss_lat (deg)  gnss_lon (deg)  gnss_altitude (m)
 *   battery_voltage (V)
 *  EXISTING SCIENCE FIELDS
 *   sgp41_voc (index)  sgp41_nox (index)   -- Sensirion Gas Index Algorithm
 *   veml6075_uva (counts)  veml6075_uvb (counts)
 *   opt3001_lux (lux)
 *   mag_x / mag_y / mag_z (uT)
 *   radiation_cpm (CPM)
 *  SECONDARY MISSION FIELDS (secondary mission + housekeeping)
 *   bme688_gas_resistance (Ohm)                     -- bug #8
 *   ens160_tvoc (ppb)  ens160_eco2 (ppm)  ens160_aqi (1-5)  ens160_status (flag)
 *   imu_accel_x/y/z (m/s^2)  imu_gyro_x/y/z (deg/s) -- BMI270 over SPI
 *   battery_current_ma (mA)                         -- bug #11
 *   sgp41_voc_raw (ticks)  sgp41_nox_raw (ticks)    -- raw SRAW signals
 *   radiation_counts (counts)                       -- cumulative Geiger counts
 *   altitude_rel (m)  vertical_speed (m/s)          -- baro, vs. launch site
 *   mission_state (enum STRING)  mission_state_code (0..6 numeric)
 *
 * NOTE on mission_state: the ground parser only accepts finite numeric
 * "value"s, so it marks the STRING field as an invalid measurement (the frame
 * and every other field are still accepted -- nothing is dropped). The numeric
 * twin mission_state_code is therefore also sent so the ground station can use
 * the state with no parser change:
 *   0=BOOT 1=SELF_TEST 2=PRELAUNCH 3=ASCENT 4=APOGEE 5=DESCENT 6=LANDED
 *
 * SCIENTIFIC HONESTY: a field is included ONLY when its sensor read succeeds
 * (return code checked, device ACKs on the bus, value finite). A failed/absent
 * sensor is OMITTED -- the firmware never transmits a fabricated 0.
 *
 * ---------------------------------------------------------------------
 * BUG FIXES (numbering = firmware audit)
 *   #1  GNSS RX pin 4 -> GPIO25 (GPIO4 is the W25Q128 flash CS)
 *   #2  GNSS TX pin 2 -> GPIO26 (GPIO2 is a boot strapping pin)
 *   #3  GNSS on UART1 -> UART2 (the E22 UART code has since been replaced
 *       by the SPI SX1268 driver -- the E22-400M30S has no UART mode)
 *   #4  TPS3823 watchdog strobe on GPIO32 (every 500 ms cycle + during setup)
 *   #5  PCA9548A I2C mux channel selection before every I2C access
 *   #6  ENS160 driver (TVOC / eCO2 / AQI)
 *   #7  BMI270 driver over SPI (CS = GPIO15)
 *   #8  BME688 gas resistance now transmitted
 *   #9  DS3231 RTC timestamp fallback (and GNSS disciplines the RTC)
 *   #10 FRAM (MB85RC512) persistence of sequence + mission state
 *   #11 INA219 current transmitted
 *   #12 Geiger pulse input GPIO27 -> GPIO39
 *   #13 JSON buffer 1024 -> 3072 bytes (>= 2048 required; full payload
 *       is ~2.6 kB) and the builder is overflow-safe, never truncates
 *   #14 Mission state machine BOOT..LANDED, transmitted every frame
 *   #15 Secondary mission sensors (ENS160, BMI270) implemented
 * ADDITIONAL DEFECTS FOUND AND FIXED DURING THE REWRITE
 *   - BME688 begin() used the Adafruit default 0x77; board strap is 0x76.
 *   - OPT3001 was never configured: it powers up in SHUTDOWN, so the old code would
 *     have returned a stale reading. Now set to continuous / auto-range.
 *   - SGP41 was marked OK with no bus check; now verified via serial number.
 *   - SGP41 raw SRAW ticks (~20000-40000) were labelled "index" -- the ground
 *     science module expects a 0-500 Gas Index. The Sensirion Gas Index
 *     Algorithm now produces the real index; raw ticks go in *_raw fields.
 *     SGP41 is sampled at its specified 1 Hz (every 2nd 2 Hz frame).
 *   - JSON printed NaN/Inf as bare `nan` (invalid JSON -> whole frame lost).
 *   - JSON snprintf overflow could run len_ past the buffer.
 *   - GNSS lat/lon printed with %.6g (~11 m precision); now %.10g.
 *   - GNSS time used even when stale (TinyGPS keeps isValid() after loss).
 *   - Geiger CPM window reset every 60 s (saw-tooth); now a true rolling
 *     60 s window, plus ISR dead-time filter.
 *   - Frame/payload buffers moved off the 8 KB loop-task stack.
 *   - Unused SPI chip selects (flash, SD) are driven HIGH so they cannot
 *     contend with the BMI270 on the shared SPI bus.
 *   - The old E22 UART/transparent-mode code (M0/M1/AUX on GPIO16/17) could
 *     never work: the E22-400M30S in the parts list is SPI-only, and
 *     GPIO16/17 are PSRAM pins on the WROVER. It is replaced by airone_radio.h.
 *     All SPI users (BMI270, radio, SD, flash) share one bus mutex
 *     (airone_spibus.h).
 *   - Adafruit_FRAM_I2C::begin() hard-rejects any product ID other than the
 *     MB85RC256V (0x510); the fitted MB85RC512 reports a different ID and
 *     would ALWAYS fail. We use the same library's Adafruit_EEPROM_I2C class
 *     (identical 16-bit-address byte protocol, which FRAM speaks natively).
 *
 * ---------------------------------------------------------------------
 * HARDWARE NOTES THE TEAM MUST CHECK (not fixable in firmware)
 *   ! RADIO PINS ARE AN ASSUMPTION: NSS 13, BUSY 27, DIO1 35, RXEN 14,
 *     TXEN 33, NRST not connected (re-using the old UART-E22 pads; the V7.1
 *     map's GPIO32/16/17 clash with the WDT and PSRAM). Verify against the
 *     PCB and override with -DRADIO_PIN_* if different. NOT hardware-tested.
 *   ! GPIO35 (radio DIO1) and GPIO39 (Geiger) are input-only with NO internal
 *     pull-ups -- DIO1 is push-pull from the SX1268; the Geiger output needs
 *     an external pull-up if it is open-drain.
 *   ! GPIO39 has a documented ESP32 erratum (spurious edges when ADC1/Wi-Fi
 *     are active). This firmware uses neither; the ISR has a dead-time filter.
 *   ! LINK BUDGET / REGULATORY: a full authenticated compact frame is 199 B,
 *     ~160 ms airtime at SF7/BW250 (estimate). The 10 % duty-cycle limiter
 *     therefore lets roughly one frame per ~1.6 s out. Frames in between are
 *     skipped on air but still logged. SX power defaults to -9 dBm (+ E22 PA
 *     ~ 10 mW e.r.p. with a 0 dBi antenna -- calculated, NOT measured).
 *     Measure it before flight. Never transmit without an antenna attached.
 *
 * ---------------------------------------------------------------------
 * REQUIRED LIBRARIES (Arduino Library Manager; suggested versions):
 *   Adafruit BME680 Library          2.0.x  (BME688)
 *   SparkFun BMP581 Arduino Library  1.0.x
 *   Sensirion I2C SGP41              1.0.x
 *   Sensirion Gas Index Algorithm    3.2.x  (VOC/NOx index)
 *   DFRobot_ENS160                   1.0.x  (ScioSense ENS160 TVOC/eCO2)
 *   Adafruit VEML6075 Library        2.2.x
 *   ClosedCube OPT3001               1.1.x
 *   Adafruit MMC56x3                 1.0.x  (MMC5603)
 *   Adafruit INA219                  1.2.x
 *   TinyGPSPlus                      1.0.x  (MAX-M10S)
 *   SparkFun BMI270 Arduino Library  1.0.x
 *   RTClib (Adafruit)                2.1.x  (DS3231)
 *   Adafruit FRAM I2C                2.0.x  (MB85RC512)
 *   RadioLib                         7.x    (SX1268 / E22-400M30S, built with 7.8.1)
 *   SD, SPI                          bundled with the ESP32 Arduino core
 *   (+ dependencies: Adafruit BusIO, Adafruit Unified Sensor, Sensirion Core)
 * Board: "ESP32 Wrover Module" (esp32 core 2.0.x or 3.x).
 * The SEN0463 Geiger counter is read by counting pulses in an ISR.
 * ---------------------------------------------------------------------
 */

#include <Arduino.h>
#include <Wire.h>
#include <SPI.h>
#include <math.h>
#include <time.h>

#include "airone_frame.h"   // binary framing + CRC32 (matches ground station)
#include "airone_storage.h" // MicroSD + W25Q128 failover logging task, SPI mutex
#include "airone_radio.h"   // E22-400M30S / SX1268 over SPI (RadioLib)
#include "airone_compact.h" // compact radio payload (expanded by the ground bridge)

#define FW_VERSION_STR "AirOne"

// ---- Feature switches: disable any sensor you have not fitted ----------
// (each may also be overridden with -DENABLE_xxx=0 on the compiler line)
#ifndef ENABLE_BME688
#define ENABLE_BME688 1
#endif
#ifndef ENABLE_BMP581
#define ENABLE_BMP581 1
#endif
#ifndef ENABLE_SGP41
#define ENABLE_SGP41 1
#endif
#ifndef ENABLE_ENS160
#define ENABLE_ENS160 1
#endif
#ifndef ENABLE_VEML6075
#define ENABLE_VEML6075 1
#endif
#ifndef ENABLE_OPT3001
#define ENABLE_OPT3001 1
#endif
#ifndef ENABLE_MMC5603
#define ENABLE_MMC5603 1
#endif
#ifndef ENABLE_INA219
#define ENABLE_INA219 1
#endif
#ifndef ENABLE_BMI270
#define ENABLE_BMI270 1
#endif
#ifndef ENABLE_GNSS
#define ENABLE_GNSS 1
#endif
#ifndef ENABLE_GEIGER
#define ENABLE_GEIGER 1
#endif
#ifndef ENABLE_RTC
#define ENABLE_RTC 1
#endif
#ifndef ENABLE_FRAM
#define ENABLE_FRAM 1
#endif
#ifndef ENABLE_I2C_MUX
#define ENABLE_I2C_MUX 1
#endif

#ifndef ENABLE_STORAGE
#define ENABLE_STORAGE 1    // MicroSD + W25Q128 logging
#endif
#ifndef ENABLE_RADIO
#define ENABLE_RADIO 1      // SX1268 LoRa downlink
#endif

// =====================================================================
// PIN MAP (authoritative hardware spec)                     -- bugs #1-3, #12
// =====================================================================
static const int PIN_I2C_SDA      = 21;
static const int PIN_I2C_SCL      = 22;
static const int PIN_SPI_SCK      = 18;
static const int PIN_SPI_MISO     = 19;
static const int PIN_SPI_MOSI     = 23;
static const int PIN_BMI270_CS    = 15;
static const int PIN_FLASH_CS     = 4;    // W25Q128 NOR flash (fallback log)
static const int PIN_SD_CS        = 5;    // MicroSD (primary log)

static const int PIN_WDT_WDI      = 32;   // TPS3823 WDI, timeout 1.6 s typ
// TPS22919 load-switch enable for the MAX-M10S rail. V7.1 routes EN_GNSS to
// GPIO12 with a 10 k pull-down (strap-safe), i.e. the GNSS is OFF unless the
// firmware drives it HIGH. -1 = not driven (rail hard-wired on).
#ifndef PIN_EN_GNSS
#define PIN_EN_GNSS 12
#endif

// E22-400M30S / SX1268: SPI on the shared VSPI bus. Pins are defined in
// airone_radio.h (RADIO_PIN_NSS/BUSY/DIO1/NRST/RXEN/TXEN, -D overridable).

static const int GNSS_RX_PIN      = 25;   // ESP32 RX2 <- MAX-M10S TXD
static const int GNSS_TX_PIN      = 26;   // ESP32 TX2 -> MAX-M10S RXD
static const uint32_t GNSS_BAUD   = 9600;

static const int GEIGER_PULSE_PIN = 39;   // SEN0463 pulse OUT (input-only)

// =====================================================================
// I2C addresses and PCA9548A channel map                         -- bug #5
// =====================================================================
static const uint8_t ADDR_MUX      = 0x70;
static const uint8_t ADDR_BME688   = 0x76;
static const uint8_t ADDR_BMP581   = 0x47;
static const uint8_t ADDR_SGP41    = 0x59;
static const uint8_t ADDR_ENS160   = 0x52;
static const uint8_t ADDR_VEML6075 = 0x10;
static const uint8_t ADDR_OPT3001  = 0x44;
static const uint8_t ADDR_INA219   = 0x40;
static const uint8_t ADDR_MMC5603  = 0x30;
static const uint8_t ADDR_DS3231   = 0x68;
static const uint8_t ADDR_FRAM     = 0x50;

static const uint8_t MUX_CH_BARO   = 0;   // BME688, BMP581
static const uint8_t MUX_CH_GAS    = 1;   // SGP41, ENS160
static const uint8_t MUX_CH_LIGHT  = 2;   // VEML6075, OPT3001
static const uint8_t MUX_CH_SYS    = 3;   // INA219, MMC5603, DS3231, FRAM

static const uint32_t TELEMETRY_PERIOD_MS = 500;  // 2 Hz
// bug #13: was 1024. The full field set measures ~2.6 kB of JSON
// (38 fields x ~68 B), so 2048 B would STILL drop ~8 fields every frame.
// 3072 B holds the complete payload with margin (requirement: >= 2048).
static const size_t   JSON_BUF_SIZE       = 3072;

// ---- Telemetry link authentication (unchanged) -----------------------
// Hex-encoded shared key (>= 32 hex chars). Empty = unauthenticated. NEVER
// commit a real key: pass -DAIRONE_LINK_KEY_HEX="\"...\"" in a local build.
#ifndef AIRONE_LINK_KEY_HEX
#define AIRONE_LINK_KEY_HEX ""
#endif
static uint8_t g_link_key[64];
static size_t  g_link_key_len = 0;

// =====================================================================
// Sensor driver objects
// =====================================================================
#if ENABLE_BME688
  #include <Adafruit_BME680.h>
  Adafruit_BME680 bme688(&Wire);
  bool bme688_ok = false;
#endif
#if ENABLE_BMP581
  #include <SparkFun_BMP581_Arduino_Library.h>
  BMP581 bmp581;
  bool bmp581_ok = false;
#endif
#if ENABLE_SGP41
  #include <SensirionI2CSgp41.h>
  #include <VOCGasIndexAlgorithm.h>
  #include <NOxGasIndexAlgorithm.h>
  SensirionI2CSgp41 sgp41;
  VOCGasIndexAlgorithm voc_algorithm;   // default 1.0 s sampling interval
  NOxGasIndexAlgorithm nox_algorithm;
  bool sgp41_ok = false;
#endif
#if ENABLE_ENS160
  #include <DFRobot_ENS160.h>
  DFRobot_ENS160_I2C ens160(&Wire, ADDR_ENS160);
  bool ens160_ok = false;
#endif
#if ENABLE_VEML6075
  #include <Adafruit_VEML6075.h>
  Adafruit_VEML6075 veml6075 = Adafruit_VEML6075();
  bool veml6075_ok = false;
#endif
#if ENABLE_OPT3001
  #include <ClosedCube_OPT3001.h>
  ClosedCube_OPT3001 opt3001;
  bool opt3001_ok = false;
#endif
#if ENABLE_MMC5603
  #include <Adafruit_MMC56x3.h>
  Adafruit_MMC5603 mmc = Adafruit_MMC5603(0x30);
  bool mmc_ok = false;
#endif
#if ENABLE_INA219
  #include <Adafruit_INA219.h>
  Adafruit_INA219 ina219(ADDR_INA219);
  bool ina219_ok = false;
#endif
#if ENABLE_BMI270
  #include <SparkFun_BMI270_Arduino_Library.h>
  BMI270 imu;
  bool imu_ok = false;
#endif
#if ENABLE_GNSS
  #include <TinyGPSPlus.h>
  TinyGPSPlus gps;
#endif
#if ENABLE_RTC
  #include <RTClib.h>
  RTC_DS3231 rtc;
  bool rtc_ok = false;          // device present
  bool rtc_time_valid = false;  // oscillator never stopped + plausible date
  uint32_t g_rtc_sync_epoch = 0;   // unix seconds at g_rtc_sync_ms
  uint32_t g_rtc_sync_ms = 0;
  uint32_t g_rtc_last_discipline_ms = 0;
#endif
#if ENABLE_FRAM
  // Adafruit FRAM I2C library. Adafruit_FRAM_I2C::begin() only accepts the
  // MB85RC256V product ID; the MB85RC512 speaks the identical protocol, so we
  // use the library's generic 16-bit-address class.
  #include <Adafruit_EEPROM_I2C.h>
  Adafruit_EEPROM_I2C fram;
  bool fram_ok = false;
#endif

// GNSS on UART2 (bug #3). The E22 radio is on SPI, not a UART.
#if ENABLE_GNSS
HardwareSerial GNSS(2);
#endif

// Mission states (declared before any function for Arduino auto-prototypes).
enum MissionState : uint8_t {
  ST_BOOT = 0, ST_SELF_TEST = 1, ST_PRELAUNCH = 2, ST_ASCENT = 3,
  ST_APOGEE = 4, ST_DESCENT = 5, ST_LANDED = 6
};

static uint32_t g_sequence = 0;
static bool     g_mux_present = false;
static int      g_mux_channel = -1;      // cached selection, -1 = unknown

// =====================================================================
// Watchdog strobe (TPS3823)                                      -- bug #4
// =====================================================================
static inline void wdt_kick() {
  digitalWrite(PIN_WDT_WDI, HIGH);
  delayMicroseconds(2);                  // WDI pulse >= 100 ns
  digitalWrite(PIN_WDT_WDI, LOW);
}
static void wdt_kick_ext() { wdt_kick(); }   // used by airone_storage.h long scans

// Delay that keeps the external watchdog fed (for long setup waits).
static void wdt_safe_delay(uint32_t ms) {
  uint32_t t0 = millis();
  while (millis() - t0 < ms) { wdt_kick(); delay(ms - (millis() - t0) > 100 ? 100 : 1); }
  wdt_kick();
}

// =====================================================================
// PCA9548A I2C multiplexer                                       -- bug #5
// =====================================================================
static bool selectMuxChannel(uint8_t ch) {
#if ENABLE_I2C_MUX
  if (!g_mux_present) return true;       // no mux fitted -> flat bus
  if (ch > 7) return false;
  if (g_mux_channel == (int)ch) return true;
  Wire.beginTransmission(ADDR_MUX);
  Wire.write((uint8_t)(1u << ch));
  if (Wire.endTransmission() != 0) { g_mux_channel = -1; return false; }
  g_mux_channel = ch;
  return true;
#else
  (void)ch;
  return true;
#endif
}

// ACK probe -- used for drivers that cannot report a failed read themselves.
static bool i2c_present(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

// Select channel and confirm the device answers.
static bool i2c_ready(uint8_t ch, uint8_t addr) {
  return selectMuxChannel(ch) && i2c_present(addr);
}

// =====================================================================
// JSON payload builder (dependency-free, honest omission, overflow-safe)
// =====================================================================
class JsonPayload {
 public:
  JsonPayload() { reset(); }
  void reset() {
    len_ = 0; buf_[len_++] = '{'; first_ = true; closed_ = false; dropped_ = 0;
    ac_reset(&compact);
  }

  // Numeric field. Non-finite values are omitted (would be invalid JSON).
  // Every field that exists in the compact schema is mirrored into `compact`
  // so the radio packet carries exactly the same measurements as the log.
  void add(const char* field, double value, const char* unit, const char* sid, int digits = 7) {
    if (!isfinite(value)) return;
    ac_set(&compact, ac_field_index(field), value);
    if (strcmp(sid, "BME688") == 0 && (strcmp(field, "altitude_rel") == 0 ||
                                       strcmp(field, "vertical_speed") == 0))
      compact.flags |= AC_FLAG_ALT_BME688;
    char tmp[160];
    int n = snprintf(tmp, sizeof(tmp),
                     "\"%s\":{\"value\":%.*g,\"unit\":\"%s\",\"sensor_id\":\"%s\"}",
                     field, digits, value, unit, sid);
    append(tmp, n);
  }

  // String-valued field (e.g. mission_state).
  void addString(const char* field, const char* value, const char* unit, const char* sid) {
    char tmp[160];
    int n = snprintf(tmp, sizeof(tmp),
                     "\"%s\":{\"value\":\"%s\",\"unit\":\"%s\",\"sensor_id\":\"%s\"}",
                     field, value, unit, sid);
    append(tmp, n);
  }

  const uint8_t* bytes() {
    if (!closed_) { buf_[len_++] = '}'; closed_ = true; }
    return reinterpret_cast<const uint8_t*>(buf_);
  }
  size_t size() const { return len_; }
  unsigned dropped() const { return dropped_; }
  AcEncoder compact;

 private:
  void append(const char* s, int n) {
    if (closed_ || n <= 0 || n >= 160) { dropped_++; return; }
    size_t need = (size_t)n + (first_ ? 0 : 1) + 1;   // comma + closing brace
    if (len_ + need > sizeof(buf_)) { dropped_++; return; }  // never truncate
    if (!first_) buf_[len_++] = ',';
    memcpy(buf_ + len_, s, (size_t)n);
    len_ += (size_t)n;
    first_ = false;
  }
  char buf_[JSON_BUF_SIZE];
  size_t len_;
  bool first_, closed_;
  unsigned dropped_;
};

// =====================================================================
// Mission state machine                                   -- bugs #14, #10
// =====================================================================
// (MissionState enum is declared near the top, before the first function,
//  so the Arduino prototype generator can see it.)
static const char* const STATE_NAMES[] = {
  "BOOT", "SELF_TEST", "PRELAUNCH", "ASCENT", "APOGEE", "DESCENT", "LANDED"
};

// Thresholds (per mission requirements)
static const float    LAUNCH_ALT_M          = 2.0f;    // > 2 m above baseline
static const float    LAUNCH_VZ_MS          = 0.5f;    // > 0.5 m/s upward
static const uint32_t PRELAUNCH_MIN_MS      = 5000;    // in PRELAUNCH > 5 s
static const uint32_t ASCENT_MIN_MS         = 5000;    // in ASCENT > 5 s before apogee
static const float    APOGEE_VZ_MS          = -0.3f;   // rate sign change (+hysteresis)
static const uint32_t ASCENT_TIMEOUT_MS     = 300000;  // failsafe: force APOGEE
static const uint32_t APOGEE_HOLD_MS        = 1000;    // APOGEE reported >= 2 frames
static const float    LANDED_WINDOW_M       = 5.0f;    // stable within 5 m
static const uint32_t LANDED_STABLE_MS      = 10000;   // for > 10 s
static const float    LANDED_VZ_MS          = 0.3f;    // |vz| < 0.3 m/s
static const float    IMU_BOOST_G           = 3.0f;    // auxiliary launch cue
static const uint8_t  BASELINE_SAMPLES      = 10;      // 5 s of 2 Hz samples
static const uint32_t SELF_TEST_TIMEOUT_MS  = 30000;

static MissionState g_state = ST_BOOT;
static uint32_t g_state_entered_ms = 0;
static bool     g_resumed = false;

// Barometric altitude estimator
static bool   g_alt_valid = false;      // current-cycle altitude available
static float  g_alt_raw = NAN;          // ISA altitude this cycle (m)
static float  g_alt_f = NAN;            // filtered altitude (m)
static float  g_vz = 0.0f;              // filtered vertical speed (m/s)
static bool   g_vz_valid = false;
static uint32_t g_alt_last_ms = 0;
static const char* g_alt_src = "BMP581";

static bool   g_baseline_valid = false;
static float  g_baseline_alt = 0.0f;    // ISA altitude of the launch site
static float  g_baseline_acc = 0.0f;
static uint8_t g_baseline_n = 0;
static float  g_max_rel_alt = -1e9f;

static uint8_t  g_launch_confirm = 0, g_apogee_confirm = 0;
static bool     g_seen_positive_vz = false;
static float    g_land_anchor_alt = NAN;
static uint32_t g_land_anchor_ms = 0;
static uint32_t g_imu_boost_ms = 0;      // last time |a| > IMU_BOOST_G

static void fram_save(bool full);

static void set_state(MissionState s) {
  if (s == g_state) return;
  Serial.printf("[STATE] %s -> %s (t=%lu ms)\n", STATE_NAMES[g_state], STATE_NAMES[s],
                (unsigned long)millis());
  g_state = s;
  g_state_entered_ms = millis();
  g_launch_confirm = g_apogee_confirm = 0;
  if (s == ST_ASCENT) g_seen_positive_vz = false;
  if (s == ST_DESCENT) { g_land_anchor_alt = NAN; g_land_anchor_ms = 0; }
  fram_save(true);   // store state on every transition
#if ENABLE_STORAGE
  storage_request_sync();   // make everything up to the transition durable on SD
#endif
}

static inline uint32_t in_state_ms() { return millis() - g_state_entered_ms; }

// International Standard Atmosphere pressure altitude.
static float pressure_to_altitude(float pa) {
  return 44330.0f * (1.0f - powf(pa / 101325.0f, 0.190295f));
}

// Feed one pressure sample (Pa) into the altitude / vertical-speed filter.
static void altitude_update(float pa, const char* src) {
  if (!isfinite(pa) || pa < 1000.0f || pa > 120000.0f) return;
  uint32_t now = millis();
  float alt = pressure_to_altitude(pa);
  g_alt_raw = alt;
  g_alt_valid = true;
  bool source_changed = (src != g_alt_src);
  g_alt_src = src;
  if (!isfinite(g_alt_f) || source_changed || (now - g_alt_last_ms) > 3000) {
    g_alt_f = alt;                       // (re)initialise filter
    g_vz = 0.0f;
    g_vz_valid = false;
    g_alt_last_ms = now;
    return;
  }
  float dt = (now - g_alt_last_ms) / 1000.0f;
  if (dt < 0.05f) return;
  float prev = g_alt_f;
  g_alt_f += 0.5f * (alt - g_alt_f);     // EMA on altitude
  float vz_raw = (g_alt_f - prev) / dt;  // rate from successive readings
  g_vz = g_vz_valid ? g_vz + 0.4f * (vz_raw - g_vz) : vz_raw;
  g_vz_valid = true;
  g_alt_last_ms = now;
}

static void state_machine_update() {
  const float rel = g_baseline_valid && isfinite(g_alt_f) ? g_alt_f - g_baseline_alt : NAN;
  if (isfinite(rel) && rel > g_max_rel_alt &&
      (g_state == ST_ASCENT || g_state == ST_APOGEE)) g_max_rel_alt = rel;

  switch (g_state) {
    case ST_BOOT:
      set_state(ST_SELF_TEST);
      break;

    case ST_SELF_TEST:
      // Capture the launch-site baseline: average BASELINE_SAMPLES altitudes.
      if (g_alt_valid) {
        g_baseline_acc += g_alt_raw;
        if (++g_baseline_n >= BASELINE_SAMPLES) {
          g_baseline_alt = g_baseline_acc / g_baseline_n;
          g_baseline_valid = true;
          g_max_rel_alt = 0.0f;
          Serial.printf("[STATE] baseline altitude %.2f m ISA (%s)\n", g_baseline_alt, g_alt_src);
          set_state(ST_PRELAUNCH);
        }
      } else if (in_state_ms() > SELF_TEST_TIMEOUT_MS) {
        Serial.println(F("[STATE] no barometer -- PRELAUNCH without baseline (launch detection disabled)"));
        set_state(ST_PRELAUNCH);
      }
      break;

    case ST_PRELAUNCH:
      if (!g_baseline_valid) {
        // Barometer appeared late: capture a baseline now.
        if (g_alt_valid) { g_baseline_alt = g_alt_f; g_baseline_valid = true; g_max_rel_alt = 0.0f; }
        break;
      }
      if (g_alt_valid && isfinite(rel)) {
        // Slowly track weather-driven pressure drift while sitting on the pad.
        if (fabsf(rel) < 1.0f) g_baseline_alt += 0.01f * rel;
        bool baro_launch = rel > LAUNCH_ALT_M && g_vz_valid && g_vz > LAUNCH_VZ_MS;
        bool imu_launch  = rel > LAUNCH_ALT_M && g_imu_boost_ms != 0 &&
                           (millis() - g_imu_boost_ms) < 2000;   // boost too fast for baro lag
        if ((baro_launch || imu_launch) && in_state_ms() > PRELAUNCH_MIN_MS) {
          if (++g_launch_confirm >= 2) { g_max_rel_alt = rel; set_state(ST_ASCENT); }
        } else {
          g_launch_confirm = 0;
        }
      }
      break;

    case ST_ASCENT:
      if (g_vz_valid && g_vz > 0.3f) g_seen_positive_vz = true;
      if (in_state_ms() > ASCENT_MIN_MS && g_seen_positive_vz && g_vz_valid &&
          g_vz < APOGEE_VZ_MS) {
        if (++g_apogee_confirm >= 2) set_state(ST_APOGEE);
      } else {
        g_apogee_confirm = 0;
      }
      if (in_state_ms() > ASCENT_TIMEOUT_MS) {
        Serial.println(F("[STATE] ascent timeout failsafe"));
        set_state(ST_APOGEE);
      }
      break;

    case ST_APOGEE:
      if (in_state_ms() >= APOGEE_HOLD_MS) set_state(ST_DESCENT);
      break;

    case ST_DESCENT:
      if (g_alt_valid && isfinite(g_alt_f)) {
        uint32_t now = millis();
        // Restart the stability window when the altitude leaves the +/-5 m band
        // or the CanSat is still clearly moving (prevents anchoring mid-descent).
        if (!isfinite(g_land_anchor_alt) || fabsf(g_alt_f - g_land_anchor_alt) > LANDED_WINDOW_M ||
            (g_vz_valid && fabsf(g_vz) > 1.0f)) {
          g_land_anchor_alt = g_alt_f;
          g_land_anchor_ms = now;
        } else if ((now - g_land_anchor_ms) > LANDED_STABLE_MS && g_vz_valid &&
                   fabsf(g_vz) < LANDED_VZ_MS) {
          set_state(ST_LANDED);
        }
      }
      break;

    case ST_LANDED:
    default:
      break;
  }
}

// =====================================================================
// FRAM persistence (MB85RC512, I2C 0x50, mux ch3)                -- bug #10
// Layout:
//   0x00..0x03  g_sequence (uint32 LE)
//   0x04        mission state
//   0x05        sentinel 0xA1 (record has been written at least once)
//   0x06        CRC-8 over 0x00..0x04
//   0x10..0x13  baseline altitude (float)   0x14 baseline valid
//   0x15..0x18  max relative altitude (float)
//   0x19        CRC-8 over 0x10..0x18
// =====================================================================
static const uint16_t FRAM_ADDR_CORE     = 0x00;
static const uint16_t FRAM_ADDR_SENTINEL = 0x05;
static const uint16_t FRAM_ADDR_CORE_CRC = 0x06;
static const uint16_t FRAM_ADDR_EXT      = 0x10;
static const uint16_t FRAM_ADDR_EXT_CRC  = 0x19;
static const uint8_t  FRAM_SENTINEL      = 0xA1;

static uint8_t crc8(const uint8_t* d, size_t n) {   // CRC-8/ATM, poly 0x07
  uint8_t c = 0x00;
  for (size_t i = 0; i < n; i++) {
    c ^= d[i];
    for (int k = 0; k < 8; k++) c = (c & 0x80) ? (uint8_t)((c << 1) ^ 0x07) : (uint8_t)(c << 1);
  }
  return c;
}

static void fram_save(bool full) {
#if ENABLE_FRAM
  if (!fram_ok || !selectMuxChannel(MUX_CH_SYS)) return;
  uint8_t core[5];
  core[0] = (uint8_t)(g_sequence);
  core[1] = (uint8_t)(g_sequence >> 8);
  core[2] = (uint8_t)(g_sequence >> 16);
  core[3] = (uint8_t)(g_sequence >> 24);
  core[4] = (uint8_t)g_state;
  uint8_t crc = crc8(core, sizeof(core));
  bool ok = fram.write(FRAM_ADDR_CORE, core, sizeof(core)) &&
            fram.write(FRAM_ADDR_CORE_CRC, crc) &&
            fram.write(FRAM_ADDR_SENTINEL, FRAM_SENTINEL);
  if (full) {
    uint8_t ext[9];
    memcpy(ext, &g_baseline_alt, 4);
    ext[4] = g_baseline_valid ? 1 : 0;
    memcpy(ext + 5, &g_max_rel_alt, 4);
    ok = ok && fram.write(FRAM_ADDR_EXT, ext, sizeof(ext)) &&
         fram.write(FRAM_ADDR_EXT_CRC, crc8(ext, sizeof(ext)));
  }
  if (!ok) Serial.println(F("[FRAM] write failed"));
#else
  (void)full;
#endif
}

// Returns true if an in-flight state was resumed.
static bool fram_restore() {
#if ENABLE_FRAM
  if (!fram_ok || !selectMuxChannel(MUX_CH_SYS)) return false;
  uint8_t core[7];
  if (!fram.read(FRAM_ADDR_CORE, core, sizeof(core))) return false;
  if (core[5] != FRAM_SENTINEL || crc8(core, 5) != core[6]) {
    Serial.println(F("[FRAM] no valid record (fresh mission)"));
    return false;
  }
  uint32_t seq = (uint32_t)core[0] | ((uint32_t)core[1] << 8) |
                 ((uint32_t)core[2] << 16) | ((uint32_t)core[3] << 24);
  uint8_t st = core[4];
  g_sequence = seq;   // always keep the sequence monotonic across resets
  Serial.printf("[FRAM] record valid: seq=%lu state=%s\n", (unsigned long)seq,
                st <= ST_LANDED ? STATE_NAMES[st] : "?");

  uint8_t ext[10];
  if (fram.read(FRAM_ADDR_EXT, ext, sizeof(ext)) && crc8(ext, 9) == ext[9] && ext[4] == 1) {
    float b, m;
    memcpy(&b, ext, 4);
    memcpy(&m, ext + 5, 4);
    if (isfinite(b)) { g_baseline_alt = b; g_baseline_valid = true; }
    if (isfinite(m)) g_max_rel_alt = m;
  }

  // Only in-flight states are resumed. BOOT/SELF_TEST/PRELAUNCH/LANDED restart
  // the normal sequence (a reboot after landing is a new pad session).
  if (st == ST_ASCENT || st == ST_APOGEE || st == ST_DESCENT) {
    g_state = (MissionState)st;
    g_state_entered_ms = millis();
    Serial.printf("[FRAM] BROWNOUT RECOVERY: resuming %s (baseline %s)\n",
                  STATE_NAMES[st], g_baseline_valid ? "restored" : "lost");
    return true;
  }
  g_baseline_valid = false;
  return false;
#else
  return false;
#endif
}

static void fram_clear() {
#if ENABLE_FRAM
  if (fram_ok && selectMuxChannel(MUX_CH_SYS)) {
    fram.write(FRAM_ADDR_SENTINEL, 0x00);
    fram.write(FRAM_ADDR_EXT_CRC, (uint8_t)~0);   // invalidate ext block
    Serial.println(F("[FRAM] mission record cleared"));
  }
#endif
}

// =====================================================================
// Timestamps: GNSS (when fresh) -> DS3231 -> 0                   -- bug #9
// 0 tells the ground station to use its receive time (never fabricated).
// =====================================================================
#if ENABLE_GNSS
// Unix seconds from the GNSS date/time, 0 if not valid/fresh.
static time_t gnss_epoch_secs(uint32_t* age_ms) {
  if (!(gps.date.isValid() && gps.time.isValid() && gps.date.year() >= 2020)) return 0;
  uint32_t age = gps.time.age();
  if (age > 1500) return 0;              // stale (fix lost)
  struct tm t = {};
  t.tm_year = gps.date.year() - 1900;
  t.tm_mon  = gps.date.month() - 1;
  t.tm_mday = gps.date.day();
  t.tm_hour = gps.time.hour();
  t.tm_min  = gps.time.minute();
  t.tm_sec  = gps.time.second();
  time_t secs = mktime(&t);              // UTC (TZ unset on ESP32)
  if (secs <= 0) return 0;
  if (age_ms) *age_ms = age;
  return secs;
}
#endif

static uint64_t epoch_micros() {
#if ENABLE_GNSS
  uint32_t age = 0;
  time_t secs = gnss_epoch_secs(&age);
  if (secs > 0) {
    uint64_t us = (uint64_t)secs * 1000000ULL +
                  (uint64_t)gps.time.centisecond() * 10000ULL +
                  (uint64_t)age * 1000ULL;   // time elapsed since the NMEA fix
#if ENABLE_RTC
    // Discipline the DS3231 from GNSS (at most once per 10 min).
    if (rtc_ok && age < 500 &&
        (g_rtc_last_discipline_ms == 0 || millis() - g_rtc_last_discipline_ms > 600000UL)) {
      uint32_t gnss_s = (uint32_t)(us / 1000000ULL);
      if (selectMuxChannel(MUX_CH_SYS)) {
        rtc.adjust(DateTime(gnss_s));
        g_rtc_sync_epoch = gnss_s;
        g_rtc_sync_ms = millis() - (uint32_t)((us % 1000000ULL) / 1000ULL);
        rtc_time_valid = true;
        g_rtc_last_discipline_ms = millis();
        Serial.println(F("[RTC] disciplined from GNSS"));
      }
    }
#endif
    return us;
  }
#endif
#if ENABLE_RTC
  if (rtc_time_valid) {
    // DS3231 second boundary captured at boot + millis() for sub-second.
    return (uint64_t)g_rtc_sync_epoch * 1000000ULL +
           (uint64_t)(millis() - g_rtc_sync_ms) * 1000ULL;
  }
#endif
  return 0ULL;
}

#if ENABLE_RTC
// Align to a DS3231 second rollover so millis() extrapolation is accurate.
static void rtc_sync_from_device() {
  if (!rtc_ok || !selectMuxChannel(MUX_CH_SYS)) return;
  if (rtc.lostPower()) {
    Serial.println(F("[RTC] oscillator stopped -- time invalid until GNSS fix"));
    rtc_time_valid = false;
    return;
  }
  DateTime first = rtc.now();
  if (first.year() < 2024 || first.year() > 2099) {
    Serial.println(F("[RTC] implausible date -- time invalid until GNSS fix"));
    rtc_time_valid = false;
    return;
  }
  uint32_t t0 = millis();
  uint32_t s0 = first.unixtime();
  while (millis() - t0 < 1100) {
    wdt_kick();
    DateTime d = rtc.now();
    if (d.unixtime() != s0) {
      g_rtc_sync_epoch = d.unixtime();
      g_rtc_sync_ms = millis();
      rtc_time_valid = true;
      Serial.printf("[RTC] time %04d-%02d-%02d %02d:%02d:%02d UTC\n", d.year(), d.month(),
                    d.day(), d.hour(), d.minute(), d.second());
      return;
    }
    delay(5);
  }
  // No rollover seen (should not happen) -- use the coarse reading.
  g_rtc_sync_epoch = s0;
  g_rtc_sync_ms = t0;
  rtc_time_valid = true;
}
#endif

// =====================================================================
// SEN0463 Geiger: ISR pulse counting -> true rolling 60 s CPM
// =====================================================================
#if ENABLE_GEIGER
static const uint32_t GEIGER_WINDOW_MS = 60000UL;
static const uint32_t GEIGER_DEADTIME_US = 50;       // reject edge glitches
static volatile uint32_t g_geiger_total = 0;
static volatile uint32_t g_geiger_last_us = 0;
static const size_t GEIGER_HIST = 128;               // >= 60 s at 2 Hz
static uint32_t g_gh_ms[GEIGER_HIST];
static uint32_t g_gh_cnt[GEIGER_HIST];
static size_t   g_gh_head = 0, g_gh_n = 0;

void IRAM_ATTR geiger_isr() {
  uint32_t now = (uint32_t)micros();
  if (now - g_geiger_last_us >= GEIGER_DEADTIME_US) {
    g_geiger_total = g_geiger_total + 1;   // (no ++ on volatile: C++20)
    g_geiger_last_us = now;
  }
}

static uint32_t geiger_total() {
  noInterrupts();
  uint32_t c = g_geiger_total;
  interrupts();
  return c;
}

// CPM over the trailing <= 60 s. Returns -1 until >= 1 s of data exists.
static long read_geiger_cpm() {
  uint32_t now = millis();
  uint32_t total = geiger_total();
  g_gh_ms[g_gh_head] = now;
  g_gh_cnt[g_gh_head] = total;
  g_gh_head = (g_gh_head + 1) % GEIGER_HIST;
  if (g_gh_n < GEIGER_HIST) g_gh_n++;
  // Oldest sample still inside the window.
  size_t oldest = (g_gh_head + GEIGER_HIST - g_gh_n) % GEIGER_HIST;
  for (size_t i = 0; i < g_gh_n; i++) {
    size_t idx = (oldest + i) % GEIGER_HIST;
    if (now - g_gh_ms[idx] <= GEIGER_WINDOW_MS) { oldest = idx; break; }
  }
  uint32_t elapsed = now - g_gh_ms[oldest];
  if (elapsed < 1000UL) return -1;
  return (long)(((uint64_t)(total - g_gh_cnt[oldest]) * 60000ULL) / elapsed);
}
#endif

// =====================================================================
// Sensor health bitmask (transmitted as sensor_health_mask)
// =====================================================================
enum {
  H_BME688 = 1 << 0, H_BMP581 = 1 << 1, H_SGP41 = 1 << 2, H_ENS160 = 1 << 3,
  H_VEML = 1 << 4, H_OPT = 1 << 5, H_MMC = 1 << 6, H_INA = 1 << 7,
  H_IMU = 1 << 8, H_RTC = 1 << 9, H_FRAM = 1 << 10, H_MUX = 1 << 11
};
static uint32_t g_health = 0;

// =====================================================================
// setup()
// =====================================================================
void setup() {
  // ---- Watchdog FIRST: the TPS3823 starts timing at power-up (bug #4) ----
  pinMode(PIN_WDT_WDI, OUTPUT);
  digitalWrite(PIN_WDT_WDI, LOW);
  wdt_kick();

  // ---- Deselect every SPI device before touching the bus -----------------
  pinMode(PIN_BMI270_CS, OUTPUT); digitalWrite(PIN_BMI270_CS, HIGH);
  pinMode(PIN_FLASH_CS, OUTPUT);  digitalWrite(PIN_FLASH_CS, HIGH);
  pinMode(PIN_SD_CS, OUTPUT);     digitalWrite(PIN_SD_CS, HIGH);
  pinMode(RADIO_PIN_NSS, OUTPUT); digitalWrite(RADIO_PIN_NSS, HIGH);
  // E22 RF switch off (neither PA nor LNA) until RadioLib takes over.
  pinMode(RADIO_PIN_RXEN, OUTPUT); digitalWrite(RADIO_PIN_RXEN, LOW);
  pinMode(RADIO_PIN_TXEN, OUTPUT); digitalWrite(RADIO_PIN_TXEN, LOW);

  // ---- Power-gated rails ON (TPS22919 load switches) ---------------------
  if (PIN_EN_GNSS >= 0) { pinMode(PIN_EN_GNSS, OUTPUT); digitalWrite(PIN_EN_GNSS, HIGH); }
  // (radio boost enable, if wired, is RADIO_PIN_PWR_EN -- handled in radio_begin)

  Serial.begin(115200);
  wdt_safe_delay(200);
  Serial.println(F(FW_VERSION_STR " CanSat firmware starting..."));

  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
  Wire.setClock(400000);
  SPI.begin(PIN_SPI_SCK, PIN_SPI_MISO, PIN_SPI_MOSI);
  spi_bus_init();   // shared-bus mutex: BMI270, SX1268, MicroSD, W25Q128

#if ENABLE_RADIO
  g_radio_wait_hook = wdt_kick_ext;   // keep TPS3823 alive during RadioLib waits
  radio_begin(false);
  wdt_kick();
#endif

  g_link_key_len = airone_hex_to_key(AIRONE_LINK_KEY_HEX, g_link_key, sizeof(g_link_key));
  if (g_link_key_len == 0) {
    Serial.println(F("LINK AUTH: NOT_CONFIGURED (frames unauthenticated)"));
  } else if (g_link_key_len < AIRONE_MIN_LINK_KEY_BYTES) {
    Serial.println(F("LINK AUTH: key too short (<16 bytes) -- REFUSING to transmit"));
  } else {
    Serial.printf("LINK AUTH: ENABLED (%u-byte key, HMAC-SHA256/8)\n", (unsigned)g_link_key_len);
  }

#if ENABLE_GNSS
  GNSS.setRxBufferSize(1024);
  GNSS.begin(GNSS_BAUD, SERIAL_8N1, GNSS_RX_PIN, GNSS_TX_PIN);
  Serial.printf("GNSS: UART2 RX=GPIO%d TX=GPIO%d\n", GNSS_RX_PIN, GNSS_TX_PIN);
#endif

  // ---- PCA9548A ------------------------------------------------------------
#if ENABLE_I2C_MUX
  g_mux_present = i2c_present(ADDR_MUX);
  g_mux_channel = -1;
  if (g_mux_present) g_health |= H_MUX;
  Serial.printf("PCA9548A: %s\n", g_mux_present ? "OK" : "ABSENT -- using flat I2C bus");
#endif
  wdt_kick();

  // ---- Channel 3: FRAM, RTC, INA219, MMC5603 --------------------------------
#if ENABLE_FRAM
  fram_ok = selectMuxChannel(MUX_CH_SYS) && fram.begin(ADDR_FRAM, &Wire);
  if (fram_ok) g_health |= H_FRAM;
  Serial.printf("FRAM MB85RC512: %s\n", fram_ok ? "OK" : "FAIL");
#endif
#if ENABLE_RTC
  rtc_ok = selectMuxChannel(MUX_CH_SYS) && rtc.begin(&Wire);
  if (rtc_ok) { g_health |= H_RTC; rtc_sync_from_device(); }
  Serial.printf("DS3231: %s%s\n", rtc_ok ? "OK" : "FAIL",
                rtc_ok && !rtc_time_valid ? " (time not set)" : "");
#endif
#if ENABLE_INA219
  ina219_ok = selectMuxChannel(MUX_CH_SYS) && ina219.begin(&Wire);
  if (ina219_ok) g_health |= H_INA;
  Serial.printf("INA219: %s\n", ina219_ok ? "OK" : "FAIL");
#endif
#if ENABLE_MMC5603
  mmc_ok = selectMuxChannel(MUX_CH_SYS) && mmc.begin(ADDR_MMC5603, &Wire);
  if (mmc_ok) g_health |= H_MMC;
  Serial.printf("MMC5603: %s\n", mmc_ok ? "OK" : "FAIL");
#endif
  wdt_kick();

  // ---- Channel 0: BME688, BMP581 -------------------------------------------
#if ENABLE_BME688
  bme688_ok = selectMuxChannel(MUX_CH_BARO) && bme688.begin(ADDR_BME688);
  if (bme688_ok) {
    bme688.setTemperatureOversampling(BME680_OS_8X);
    bme688.setHumidityOversampling(BME680_OS_2X);
    bme688.setPressureOversampling(BME680_OS_4X);
    bme688.setIIRFilterSize(BME680_FILTER_SIZE_3);
    bme688.setGasHeater(320, 150);       // 320 C for 150 ms
    g_health |= H_BME688;
  }
  Serial.printf("BME688: %s\n", bme688_ok ? "OK" : "FAIL");
#endif
#if ENABLE_BMP581
  bmp581_ok = selectMuxChannel(MUX_CH_BARO) && (bmp581.beginI2C(ADDR_BMP581, Wire) == BMP5_OK);
  if (bmp581_ok) g_health |= H_BMP581;
  Serial.printf("BMP581: %s\n", bmp581_ok ? "OK" : "FAIL");
#endif
  wdt_kick();

  // ---- Channel 1: SGP41, ENS160 --------------------------------------------
#if ENABLE_SGP41
  if (selectMuxChannel(MUX_CH_GAS)) {
    sgp41.begin(Wire);
    uint16_t sn[3] = {0, 0, 0};
    sgp41_ok = (sgp41.getSerialNumber(sn) == 0);
  }
  if (sgp41_ok) g_health |= H_SGP41;
  Serial.printf("SGP41: %s\n", sgp41_ok ? "OK" : "FAIL");
#endif
#if ENABLE_ENS160
  ens160_ok = selectMuxChannel(MUX_CH_GAS) && (ens160.begin() == 0 /* NO_ERR */);
  Wire.setClock(400000);                 // ENS160 begin() re-runs Wire.begin()
  if (ens160_ok) {
    ens160.setPWRMode(ENS160_STANDARD_MODE);
    ens160.setTempAndHum(25.0f, 50.0f);  // refreshed from BME688 every cycle
    g_health |= H_ENS160;
  }
  Serial.printf("ENS160: %s\n", ens160_ok ? "OK" : "FAIL");
#endif
  wdt_kick();

  // ---- Channel 2: VEML6075, OPT3001 ----------------------------------------
#if ENABLE_VEML6075
  veml6075_ok = selectMuxChannel(MUX_CH_LIGHT) && veml6075.begin(VEML6075_100MS, false, false, &Wire);
  if (veml6075_ok) g_health |= H_VEML;
  Serial.printf("VEML6075: %s\n", veml6075_ok ? "OK" : "FAIL");
#endif
#if ENABLE_OPT3001
  if (selectMuxChannel(MUX_CH_LIGHT) && opt3001.begin(ADDR_OPT3001) == NO_ERROR &&
      opt3001.readManufacturerID() == 0x5449) {
    OPT3001_Config cfg;
    cfg.rawData = 0;
    cfg.RangeNumber = 0x0C;              // automatic full-scale
    cfg.ConvertionTime = 0;              // 100 ms conversions
    cfg.Latch = 1;
    cfg.ModeOfConversionOperation = 0x03;  // continuous (powers up in shutdown!)
    opt3001_ok = (opt3001.writeConfig(cfg) == NO_ERROR);
  }
  if (opt3001_ok) g_health |= H_OPT;
  Serial.printf("OPT3001: %s\n", opt3001_ok ? "OK" : "FAIL");
#endif
  wdt_kick();

  // ---- SPI: BMI270 (bug #7) --------------------------------------------------
#if ENABLE_BMI270
  // bmi270_init uploads an 8 kB config blob; 1 MHz keeps that well under the
  // watchdog window (BMI270 supports up to 10 MHz).
  {
    SpiLock lk(2000);
    imu_ok = lk.ok() && (imu.beginSPI(PIN_BMI270_CS, 1000000) == BMI2_OK);
  }
  wdt_kick();
  if (imu_ok) {
    SpiLock lk(2000);
    bmi2_sens_config acc;  acc.type = BMI2_ACCEL;
    bmi2_sens_config gyr;  gyr.type = BMI2_GYRO;
    if (imu.getConfig(&acc) == BMI2_OK) {
      acc.cfg.acc.range = BMI2_ACC_RANGE_16G;   // launch / deployment shocks
      acc.cfg.acc.odr = BMI2_ACC_ODR_100HZ;
      imu.setConfig(acc);
    }
    if (imu.getConfig(&gyr) == BMI2_OK) {
      gyr.cfg.gyr.range = BMI2_GYR_RANGE_2000;  // fast tumble during descent
      gyr.cfg.gyr.odr = BMI2_GYR_ODR_100HZ;
      imu.setConfig(gyr);
    }
    g_health |= H_IMU;
  }
  Serial.printf("BMI270 (SPI CS=GPIO%d): %s\n", PIN_BMI270_CS, imu_ok ? "OK" : "FAIL");
#endif

  // ---- Geiger (bug #12) --------------------------------------------------------
#if ENABLE_GEIGER
  // GPIO39 is input-only with no internal pull-up: the SEN0463 board / PCB
  // must provide the pull-up. One falling edge per detected particle.
  pinMode(GEIGER_PULSE_PIN, INPUT);
  g_geiger_total = 0;
  attachInterrupt(digitalPinToInterrupt(GEIGER_PULSE_PIN), geiger_isr, FALLING);
  Serial.printf("Geiger SEN0463: counting on GPIO%d\n", GEIGER_PULSE_PIN);
#endif

  // ---- Mission state: resume after brownout or start fresh ------------------
  g_state = ST_BOOT;
  g_state_entered_ms = millis();
  g_resumed = fram_restore();
  if (!g_resumed) {
    set_state(ST_SELF_TEST);
    Serial.printf("SELF_TEST: health mask 0x%03lX\n", (unsigned long)g_health);
  }
  fram_save(true);

  // ---- On-board logging (starts the storage task on core 0) -----------------
#if ENABLE_STORAGE
  storage_begin(PIN_SD_CS, PIN_FLASH_CS);
#endif

  Serial.println(F("Setup complete. Serial commands: 'R' = clear FRAM mission record, 'S' = status,"));
  Serial.println(F("  'L' = log/radio status, 'D' = dump flash log, 'E' twice = erase flash log"));
  wdt_kick();
}

// =====================================================================
// loop()
// =====================================================================
#if ENABLE_SGP41
static uint16_t sgp41_conditioning_left = 10;   // 10 s at 1 Hz (Sensirion max)
static bool     sgp41_phase = false;            // SGP41 runs at 1 Hz
#endif

static void handle_serial_commands() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == 'R' || c == 'r') {
      fram_clear();
      g_sequence = 0;
      g_baseline_valid = false;
      g_baseline_n = 0;
      g_baseline_acc = 0.0f;
      g_state = ST_BOOT;
      set_state(ST_SELF_TEST);
    } else if (c == 'S' || c == 's') {
      Serial.printf("state=%s seq=%lu health=0x%03lX alt_rel=%.2f vz=%.2f resumed=%d\n",
                    STATE_NAMES[g_state], (unsigned long)g_sequence, (unsigned long)g_health,
                    g_baseline_valid && isfinite(g_alt_f) ? g_alt_f - g_baseline_alt : NAN,
                    g_vz, (int)g_resumed);
    }
#if ENABLE_STORAGE
    else if (c == 'L' || c == 'l') {
      storage_print_status();
#if ENABLE_RADIO
      radio_print_status();
#endif
    } else if (c == 'D' || c == 'd') {
      storage_dump_flash();
    } else if (c == 'E' || c == 'e') {
      // Destructive: require a second 'E' within 3 s.
      static uint32_t armed_ms = 0;
      if (armed_ms && millis() - armed_ms < 3000) {
        armed_ms = 0;
        storage_request_flash_erase();
        Serial.println(F("[STORAGE] flash log erase requested"));
      } else {
        armed_ms = millis() | 1u;
        Serial.println(F("[STORAGE] press 'E' again within 3 s to ERASE the flash log"));
      }
    }
#endif
  }
}

void loop() {
  static uint32_t last_tx = 0;
  static JsonPayload p;                                   // off the task stack
  static uint8_t frame[JSON_BUF_SIZE + 64 + AIRONE_AUTH_TAG_SIZE];

#if ENABLE_GNSS
  while (GNSS.available()) gps.encode(GNSS.read());
#endif
  handle_serial_commands();
#if ENABLE_RADIO
  radio_poll();     // completes a LoRa packet in flight (non-blocking)
  radio_maintain(false);   // re-tries a failed init every RADIO_RETRY_MS
#endif

  uint32_t now = millis();
  if (now - last_tx < TELEMETRY_PERIOD_MS) return;
  last_tx = now;

  // ---- Strobe the TPS3823 once per 500 ms cycle, regardless of sensors ----
  wdt_kick();

  p.reset();
  g_alt_valid = false;
  float bme_t_c = NAN, bme_rh = NAN;     // for SGP41 / ENS160 compensation
  float bmp_pa = NAN, bme_pa = NAN;

  // ---- Channel 0: BME688 / BMP581 ------------------------------------------
#if ENABLE_BME688
  if (bme688_ok && selectMuxChannel(MUX_CH_BARO) && bme688.performReading()) {
    bme_t_c = bme688.temperature;
    bme_rh  = bme688.humidity;
    bme_pa  = bme688.pressure;
    p.add("bme688_temperature", bme688.temperature + 273.15, "K", "BME688");
    p.add("bme688_pressure", bme688.pressure, "Pa", "BME688", 8);
    p.add("bme688_humidity", bme688.humidity, "%", "BME688");
    // Gas resistance is only meaningful when the heater reached temperature.
    if (bme688.gas_resistance > 0)
      p.add("bme688_gas_resistance", (double)bme688.gas_resistance, "Ohm", "BME688");
  }
#endif
#if ENABLE_BMP581
  if (bmp581_ok && selectMuxChannel(MUX_CH_BARO)) {
    bmp5_sensor_data d = {0, 0};
    if (bmp581.getSensorData(&d) == BMP5_OK && d.pressure > 0) {
      bmp_pa = d.pressure;
      p.add("bmp581_temperature", d.temperature + 273.15, "K", "BMP581");
      p.add("bmp581_pressure", d.pressure, "Pa", "BMP581", 8);
    }
  }
#endif
  // Altitude source: BMP581 (high precision) with BME688 as fallback.
  if (isfinite(bmp_pa)) altitude_update(bmp_pa, "BMP581");
  else if (isfinite(bme_pa)) altitude_update(bme_pa, "BME688");

  // ---- Channel 1: SGP41 / ENS160 -------------------------------------------
#if ENABLE_SGP41
  sgp41_phase = !sgp41_phase;
  if (sgp41_ok && sgp41_phase && selectMuxChannel(MUX_CH_GAS)) {
    uint16_t rh_ticks = 0x8000, t_ticks = 0x6666;   // 50 %RH / 25 C defaults
    if (isfinite(bme_rh) && isfinite(bme_t_c)) {
      rh_ticks = (uint16_t)constrain(bme_rh * 65535.0f / 100.0f, 0.0f, 65535.0f);
      t_ticks  = (uint16_t)constrain((bme_t_c + 45.0f) * 65535.0f / 175.0f, 0.0f, 65535.0f);
    }
    uint16_t raw_voc = 0, raw_nox = 0, err;
    if (sgp41_conditioning_left > 0) {
      err = sgp41.executeConditioning(rh_ticks, t_ticks, raw_voc);
      if (err == 0) {
        sgp41_conditioning_left--;
        p.add("sgp41_voc_raw", raw_voc, "ticks", "SGP41");
      }
    } else {
      err = sgp41.measureRawSignals(rh_ticks, t_ticks, raw_voc, raw_nox);
      if (err == 0) {
        p.add("sgp41_voc_raw", raw_voc, "ticks", "SGP41");
        p.add("sgp41_nox_raw", raw_nox, "ticks", "SGP41");
        int32_t voc_idx = voc_algorithm.process(raw_voc);
        int32_t nox_idx = nox_algorithm.process(raw_nox);
        // The algorithm returns 0 during its start-up blackout: omit, not 0.
        if (voc_idx > 0) p.add("sgp41_voc", voc_idx, "index", "SGP41");
        if (nox_idx > 0) p.add("sgp41_nox", nox_idx, "index", "SGP41");
      }
    }
  }
#endif
#if ENABLE_ENS160
  if (ens160_ok && i2c_ready(MUX_CH_GAS, ADDR_ENS160)) {
    if (isfinite(bme_t_c) && isfinite(bme_rh)) ens160.setTempAndHum(bme_t_c, bme_rh);
    uint8_t validity = ens160.getENS160Status();   // 0 normal,1 warm-up,2 start-up,3 invalid
    p.add("ens160_status", validity, "flag", "ENS160");
    if (validity == 0 || validity == 2) {
      uint8_t aqi = ens160.getAQI();
      uint16_t tvoc = ens160.getTVOC();
      uint16_t eco2 = ens160.getECO2();
      if (aqi >= 1 && aqi <= 5) p.add("ens160_aqi", aqi, "index", "ENS160");
      if (tvoc <= 65000)        p.add("ens160_tvoc", tvoc, "ppb", "ENS160");
      if (eco2 >= 400)          p.add("ens160_eco2", eco2, "ppm", "ENS160");
    }
  }
#endif

  // ---- Channel 2: VEML6075 / OPT3001 ---------------------------------------
#if ENABLE_VEML6075
  if (veml6075_ok && i2c_ready(MUX_CH_LIGHT, ADDR_VEML6075)) {
    float uva = veml6075.readUVA();
    float uvb = veml6075.readUVB();
    p.add("veml6075_uva", uva, "counts", "VEML6075");
    p.add("veml6075_uvb", uvb, "counts", "VEML6075");
  }
#endif
#if ENABLE_OPT3001
  if (opt3001_ok && selectMuxChannel(MUX_CH_LIGHT)) {
    OPT3001 r = opt3001.readResult();
    if (r.error == NO_ERROR) p.add("opt3001_lux", r.lux, "lux", "OPT3001");
  }
#endif

  // ---- Channel 3: MMC5603 / INA219 -----------------------------------------
#if ENABLE_MMC5603
  if (mmc_ok && selectMuxChannel(MUX_CH_SYS)) {
    sensors_event_t e;
    if (mmc.getEvent(&e)) {
      p.add("mag_x", e.magnetic.x, "uT", "MMC5603");
      p.add("mag_y", e.magnetic.y, "uT", "MMC5603");
      p.add("mag_z", e.magnetic.z, "uT", "MMC5603");
    }
  }
#endif
#if ENABLE_INA219
  if (ina219_ok && i2c_ready(MUX_CH_SYS, ADDR_INA219)) {
    p.add("battery_voltage", ina219.getBusVoltage_V(), "V", "INA219");
    p.add("battery_current_ma", ina219.getCurrent_mA(), "mA", "INA219");   // bug #11
  }
#endif

  // ---- SPI: BMI270 ---------------------------------------------------------
#if ENABLE_BMI270
  bool imu_read = false;
  if (imu_ok) { SpiLock lk(50); imu_read = lk.ok() && imu.getSensorData() == BMI2_OK; }
  if (imu_read) {
    const float G = 9.80665f;
    float ax = imu.data.accelX * G, ay = imu.data.accelY * G, az = imu.data.accelZ * G;
    p.add("imu_accel_x", ax, "m/s^2", "BMI270");
    p.add("imu_accel_y", ay, "m/s^2", "BMI270");
    p.add("imu_accel_z", az, "m/s^2", "BMI270");
    p.add("imu_gyro_x", imu.data.gyroX, "deg/s", "BMI270");
    p.add("imu_gyro_y", imu.data.gyroY, "deg/s", "BMI270");
    p.add("imu_gyro_z", imu.data.gyroZ, "deg/s", "BMI270");
    float a_g = sqrtf(ax * ax + ay * ay + az * az) / G;
    if (a_g > IMU_BOOST_G) g_imu_boost_ms = millis() | 1u;
  }
#endif

  // ---- Geiger --------------------------------------------------------------
#if ENABLE_GEIGER
  {
    long cpm = read_geiger_cpm();
    if (cpm >= 0) p.add("radiation_cpm", (double)cpm, "CPM", "SEN0463");
    p.add("radiation_counts", (double)geiger_total(), "counts", "SEN0463", 10);
  }
#endif

  // ---- GNSS ----------------------------------------------------------------
#if ENABLE_GNSS
  if (gps.location.isValid() && gps.location.age() < 2000) {
    p.add("gnss_lat", gps.location.lat(), "deg", "MAX-M10S", 10);
    p.add("gnss_lon", gps.location.lng(), "deg", "MAX-M10S", 10);
  }
  if (gps.altitude.isValid() && gps.altitude.age() < 2000)
    p.add("gnss_altitude", gps.altitude.meters(), "m", "MAX-M10S");
  if (gps.satellites.isValid() && gps.satellites.age() < 2000)
    p.add("gnss_satellites", gps.satellites.value(), "count", "MAX-M10S");
  if (gps.hdop.isValid() && gps.hdop.age() < 2000)
    p.add("gnss_hdop", gps.hdop.hdop(), "hdop", "MAX-M10S");
#endif

  // ---- Mission state machine ----------------------------------------------
  state_machine_update();
  if (g_alt_valid && g_baseline_valid && isfinite(g_alt_f))
    p.add("altitude_rel", g_alt_f - g_baseline_alt, "m", g_alt_src);
  if (g_alt_valid && g_vz_valid)
    p.add("vertical_speed", g_vz, "m/s", g_alt_src);
  p.addString("mission_state", STATE_NAMES[g_state], "enum", "FSM");
  p.add("mission_state_code", (double)g_state, "enum", "FSM");
  p.add("sensor_health_mask", (double)g_health, "bitmask", "FSM", 10);

  // ---- Frame + transmit ---------------------------------------------------
  if (p.dropped()) Serial.printf("WARN: %u field(s) did not fit the JSON buffer\n", p.dropped());
  const uint8_t* payload = p.bytes();
  size_t plen = p.size();
  uint64_t ts_us = epoch_micros();
  const uint8_t* key = g_link_key_len ? g_link_key : NULL;
  size_t flen = airone_pack_frame_auth(frame, sizeof(frame), AIRONE_PT_SENSOR_DATA,
                                       g_sequence, ts_us, payload, plen, key, g_link_key_len);
  if (flen > 0) {
    // 1) Full JSON frame -> on-board log (MicroSD, W25Q128 on failover).
#if ENABLE_STORAGE
    if (!storage_log(frame, flen)) Serial.println(F("WARN: log ring full, frame not logged"));
#endif
    // 2) Same measurements, compact binary, same SEQUENCE/TIMESTAMP -> LoRa.
    //    The ground bridge expands it back into the identical JSON layout.
#if ENABLE_RADIO
    static uint8_t cpay[AC_MAX_PAYLOAD];
    static uint8_t rframe[RADIO_MAX_PACKET];
    size_t clen = ac_encode(&p.compact, cpay, sizeof(cpay));
    size_t rlen = clen ? airone_pack_frame_auth(rframe, sizeof(rframe), AIRONE_PT_COMPACT,
                                                g_sequence, ts_us, cpay, clen, key, g_link_key_len)
                       : 0;
    if (rlen > 0) radio_send(rframe, rlen);   // skipped if duty cycle not yet allowed
#endif
    g_sequence++;
  } else {
    Serial.println(F("Frame too large or auth refused; dropped (never truncated)."));
  }

  // ---- Persist sequence + state every cycle (brownout recovery) ----------
  fram_save(true);
}
