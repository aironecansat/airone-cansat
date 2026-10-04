# AirOne CanSat

**AirOne** is a CanSat for the UK CanSat competition. It measures the atmosphere, air chemistry, UV light and ionising radiation during ascent and descent, sends the data live to a laptop ground station over LoRa, and keeps flying through brownouts and resets.

The repository has two parts:

| Folder | What it is | Runs on |
|---|---|---|
| [`AirOne_Firmware_CanSat/`](AirOne_Firmware_CanSat/AirOne_Firmware_CanSat/) | Flight firmware (Arduino sketch) | ESP32-WROVER-E inside the CanSat |
| [`AirOne_Software_Laptop/`](AirOne_Software_Laptop/AirOne_Software_Laptop/) | Ground station: receive, check, store, analyse and display telemetry | Laptop / PC (Python) |
| [`SECONDARY_MISSION.md`](SECONDARY_MISSION.md) | Secondary mission plan (AirChem-Rad), also as PDF / DOCX | — |

---

## Contents

1. [Project status — read this first](#1-project-status--read-this-first)
2. [Missions](#2-missions)
3. [System overview](#3-system-overview)
4. [Hardware: every chip and what it does](#4-hardware-every-chip-and-what-it-does)
5. [Pin map and I²C bus](#5-pin-map-and-ic-bus)
6. [Firmware drivers](#6-firmware-drivers)
7. [Flight software](#7-flight-software)
8. [Telemetry packet structure](#8-telemetry-packet-structure)
9. [Efficiency: memory, CPU, radio and power](#9-efficiency-memory-cpu-radio-and-power)
10. [Radio link and UK compliance](#10-radio-link-and-uk-compliance)
11. [Ground station](#11-ground-station)
12. [Getting started](#12-getting-started)
13. [Pre-flight checklist](#13-pre-flight-checklist)
14. [Repository structure](#14-repository-structure)
15. [Known limitations and next steps](#15-known-limitations-and-next-steps)

---

## 1. Project status — read this first

| Item | Status |
|---|---|
| Firmware compiles | ✅ Compiles with `arduino-cli`, board `esp32:esp32:esp32wrover` (ESP32 core 2.0.17): **391,802 B flash (29 %)**, **33,024 B static RAM (10 %)** |
| Firmware on real hardware | ❌ **Not yet run or flown.** Bench-test every sensor before flight |
| Ground station tests | ✅ 152 passed, 2 skipped (`pytest`) |
| Frame format C ↔ Python | ✅ C frame builder output parses in the Python parser with a valid CRC |
| Radio driver vs. fitted module | ⚠️ **Mismatch — see below** |
| UK radio power / duty-cycle limits | ⚠️ **Needs checking — see [section 10](#10-radio-link-and-uk-compliance)** |
| MicroSD / W25Q128 onboard logging | ⏳ Hardware fitted, **not used by the firmware yet** (chip-selects held high) |

> ⚠️ **Radio module mismatch.** The parts list says **EBYTE E22-400M30S**. That is an **SPI** module (Semtech SX1268 with pins NSS, SCK, MOSI, MISO, BUSY, DIO1, NRST, TXEN, RXEN). The firmware drives the radio as a **UART "transparent-mode" E22** (UART1 on GPIO16/17 with M0, M1, AUX). That is how the **E22-400T30S / T30D** UART modules work. As written, the firmware **will not talk to an E22-400M30S.** Either fit a UART E22-400T module, or switch the radio code to an SX1268 driver (for example RadioLib) once the SPI/BUSY/DIO1/RESET/TXEN/RXEN wiring is known. The frame format does not change either way.

> ⚠️ **GPIO16/17 on WROVER.** On ESP32-WROVER-E modules, GPIO16 and GPIO17 are used inside the module by the PSRAM. They are not free I/O. Check on the schematic that the radio really connects to these pads.

---

## 2. Missions

### Primary mission (UK CanSat)

The primary mission measures **air pressure and air temperature** during the descent and sends them to the ground at least once per second. AirOne sends a full frame **every 500 ms (2 Hz)** with:

- Pressure: BMP581 (main) and BME688 (backup)
- Temperature: BMP581 and BME688, sent in kelvin
- Barometric altitude above the launch site and vertical speed
- GNSS position and altitude

### Secondary mission — AirChem-Rad

AirChem-Rad builds one time-aligned **vertical profile** of these quantities during ascent and descent:

| Quantity | Sensors |
|---|---|
| Air chemistry | BME688 gas resistance, SGP41 VOC/NOx index, ENS160 TVOC/eCO₂/AQI |
| UV and visible light | VEML6075 UV-A/UV-B, OPT3001 lux |
| Ionising radiation | SEN0463 Geiger counter (CPM and total counts) |
| Descent dynamics | BMI270 accelerometer/gyroscope, MMC5603 magnetometer |

Science questions:

1. Is the air in the **boundary layer** chemically different from the air above it? The ground station estimates the boundary-layer top from the steepest drop in TVOC/eCO₂ (`gas_boundary_layer_height`).
2. Does **UV rise with altitude** as expected, and how do clouds change it?
3. Does the **radiation dose rate** change measurably over the flight altitude range?
4. How does the CanSat **spin and swing** under the parachute, and does that affect the light sensor readings?

No extra hardware is needed. Full plan, method and success criteria: [`SECONDARY_MISSION.md`](SECONDARY_MISSION.md).

---

## 3. System overview

```text
 ┌──────────────────────────── AirOne CanSat ─────────────────────────────┐
 │                                                                        │
 │   TPS3823 watchdog ◄── WDI (GPIO32) ──┐                                │
 │                                       │                                │
 │   PCA9548A I²C mux (0x70)        ┌────┴──────────────┐   UART1         │
 │    ch0 BME688, BMP581   ◄──I²C──►│                   │◄──────► E22 LoRa ─╋─► 433 MHz
 │    ch1 SGP41, ENS160             │  ESP32-WROVER-E   │   UART2         │
 │    ch2 VEML6075, OPT3001         │  N16R8            │◄──────► MAX-M10S GNSS
 │    ch3 INA219, MMC5603,          │  240 MHz × 2      │   SPI           │
 │        DS3231, MB85RC512 FRAM    │  16 MB flash      │◄──────► BMI270 IMU
 │                                  │  8 MB PSRAM       │         (W25Q128, µSD idle)
 │   SEN0463 Geiger ── pulse ──────►│  GPIO39 ISR       │                 │
 │                                  └───────────────────┘                 │
 └────────────────────────────────────────────────────────────────────────┘
                                         │ binary frames, CRC-32, optional HMAC
                                         ▼
 ┌──────────────────────────── Ground station ────────────────────────────┐
 │ E22 receiver ─USB─► serial transport ─► frame parser ─► 20-stage       │
 │ pipeline ─► SQLite storage ─► science modules / anomaly ML ─► REST API │
 │ ─► PyQt5 operator GUI                                                  │
 └────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Hardware: every chip and what it does

Values below are typical datasheet figures. Check them against the exact part numbers on your BOM.

### 4.1 Processing, timing and safety

| Part | Role | Key features | How the firmware uses it |
|---|---|---|---|
| **ESP32-WROVER-E-N16R8** | Flight computer | Dual-core Xtensa LX6 up to 240 MHz, 520 KB SRAM, 16 MB flash, 8 MB PSRAM, 3 hardware UARTs, Wi-Fi/BT | Single-threaded loop at 2 Hz. Wi-Fi and Bluetooth are never started. UART1 = radio, UART2 = GNSS |
| **TPS3823-33DBVR** | Supply supervisor + watchdog | Resets the ESP32 if 3.3 V sags (≈2.93 V threshold) or if WDI is not toggled within ≈1.6 s (typ.) | Strobed **first thing in `setup()`**, during every long wait (`wdt_safe_delay`) and once every 500 ms cycle |
| **DS3231** | Real-time clock | TCXO, ±2 ppm (0–40 °C), battery backup | Fallback timestamp when there is no GNSS fix. Re-set from GNSS at most once every 10 min. Ignored if its oscillator stopped |
| **MB85RC512** | FRAM, 64 KB | Byte-writable, no erase, no write delay, ~10¹³ write cycles | Saves sequence number, mission state, baseline altitude and maximum altitude **every cycle**, each block protected by CRC-8 |

### 4.2 Atmosphere (primary mission)

| Part | Measures | Key features | Firmware settings |
|---|---|---|---|
| **BMP581** | Pressure, temperature | Bosch capacitive barometer, very low noise (≈0.1 Pa RMS class, about 1 cm of altitude) | Main altitude source. Library default configuration |
| **BME688** | Temperature, humidity, pressure, gas resistance | Bosch 4-in-1 with heated MOx gas plate | Oversampling T×8, H×2, P×4; IIR filter 3; gas heater **320 °C for 150 ms** each cycle. Address **0x76** |

### 4.3 Air chemistry (secondary mission)

| Part | Measures | Key features | Firmware settings |
|---|---|---|---|
| **SGP41** | VOC and NOx (raw ticks plus index) | Sensirion MOx. Gas Index Algorithm turns raw signals into VOC index 1–500 (100 = normal) and NOx index 1–500 (1 = clean) | Sampled at its specified **1 Hz** (every second frame). Humidity and temperature compensation from the BME688. 10 s conditioning at start-up. The index is omitted while the algorithm is still warming up |
| **ENS160** | TVOC (ppb), eCO₂ (ppm), AQI (UBA 1–5) | ScioSense 4-element MOx with on-chip processing. Needs ~3 min warm-up and ~1 h initial start-up on first use | Standard mode. Temperature and humidity compensation from the BME688 every cycle. Values sent only when status is "normal" or "initial start-up"; the status flag is always sent |

### 4.4 Light and UV

| Part | Measures | Key features | Firmware settings |
|---|---|---|---|
| **VEML6075** | UV-A (~365 nm), UV-B (~330 nm) | Vishay UV sensor, configurable integration time. **Note:** Vishay lists it as end-of-life — buy spares | 100 ms integration, normal dynamic range. Raw counts are sent and converted on the ground |
| **OPT3001** | Visible light (lux) | TI, eye-like spectral response, 0.01–83,000 lux, auto-range | Starts up in **shutdown** mode, so the firmware sets continuous mode, auto full-scale and 100 ms conversions. Checked by manufacturer ID `0x5449` |

### 4.5 Motion and navigation

| Part | Measures | Key features | Firmware settings |
|---|---|---|---|
| **BMI270** | 3-axis acceleration, 3-axis rotation | Bosch low-power IMU, ±2…16 g, ±125…2000 °/s. Needs an 8 KB config blob uploaded at start-up | SPI at 1 MHz, CS GPIO15. **±16 g, ±2000 °/s, 100 Hz** ODR to capture launch shock and fast tumbling. A reading above 3 g also helps detect launch |
| **MMC5603** | 3-axis magnetic field (µT) | MEMSIC AMR magnetometer, ±30 G range, 20-bit, SET/RESET degaussing | Read every cycle for spin and heading analysis |
| **MAX-M10S** | GNSS position, altitude, UTC time | u-blox M10, tracks GPS, Galileo, GLONASS and BeiDou at once, very low power (~25 mW tracking) | UART2, 9600 baud, NMEA parsed with TinyGPSPlus. A fix older than 2 s is not sent. Time is used only if it is less than 1.5 s old |

### 4.6 Radiation, power and radio

| Part | Measures / role | Key features | Firmware settings |
|---|---|---|---|
| **SEN0463** | Ionising radiation | DFRobot Geiger–Müller tube module, one pulse per detected particle | Falling-edge interrupt on GPIO39 with a 50 µs glitch filter. **True rolling 60 s** count rate (128-slot history) plus cumulative counts |
| **INA219** | Battery voltage and current | TI 12-bit high-side monitor | Adafruit default calibration (32 V / 2 A, **assumes a 0.1 Ω shunt** — change it if your shunt differs) |
| **E22 LoRa (433 MHz)** | Telemetry downlink | SX1268-based, up to 30 dBm (1 W) | UART transparent mode, 115200 baud, channel 23 (410 + 23 = **433 MHz**). Waits for AUX = idle before sending. See the mismatch warning in [section 1](#1-project-status--read-this-first) |
| **PCA9548A** | I²C multiplexer | 8 channels; lets sensors with clashing addresses share one bus | Channel chosen before **every** I²C access. The last channel is cached so no extra bus writes are made |
| **W25Q128** / **MicroSD** | Bulk storage | 16 MB NOR flash / FAT card | Not used yet. Their chip-selects are driven HIGH so they cannot disturb the BMI270 on the shared SPI bus |

---

## 5. Pin map and I²C bus

### 5.1 ESP32 pins

| Function | GPIO | Direction | Notes |
|---|---|---|---|
| I²C SDA / SCL | 21 / 22 | bidir | 400 kHz, all I²C parts behind the PCA9548A |
| SPI SCK / MISO / MOSI | 18 / 19 / 23 | — | Shared bus |
| BMI270 CS | 15 | out | Strapping pin; keep the pull-up |
| W25Q128 CS | 4 | out | Held HIGH |
| MicroSD CS | 5 | out | Held HIGH (strapping pin) |
| E22 RX ← module TXD | 16 | in | UART1 — see the WROVER/PSRAM warning |
| E22 TX → module RXD | 17 | out | UART1 |
| E22 M0 / M1 | 13 / 14 | out | Both LOW = transparent mode |
| E22 AUX | 35 | in | Input-only, no internal pull-up. HIGH = idle |
| GNSS RX ← MAX-M10S TXD | 25 | in | UART2 |
| GNSS TX → MAX-M10S RXD | 26 | out | UART2 |
| Geiger pulse | 39 | in | Input-only, no internal pull-up; needs an external pull-up |
| TPS3823 WDI | 32 | out | Pulsed every cycle |

### 5.2 I²C addresses by mux channel

| Mux channel | Device | Address |
|---|---|---|
| — | PCA9548A | 0x70 |
| 0 (barometry) | BME688 | 0x76 |
| 0 | BMP581 | 0x47 |
| 1 (gas) | SGP41 | 0x59 |
| 1 | ENS160 | 0x52 |
| 2 (light) | VEML6075 | 0x10 |
| 2 | OPT3001 | 0x44 |
| 3 (system) | INA219 | 0x40 |
| 3 | MMC5603 | 0x30 |
| 3 | DS3231 | 0x68 |
| 3 | MB85RC512 FRAM | 0x50 |

If the mux does not answer at start-up, the firmware uses a flat I²C bus. This lets you bench-test on a breadboard without the mux.

---

## 6. Firmware drivers

All drivers are open-source Arduino libraries (install them with the Library Manager). Each sensor can be turned off at compile time with an `ENABLE_<SENSOR>` flag at the top of the sketch, or with `-DENABLE_<SENSOR>=0`.

| Part | Library | Bus | Health check at start-up | Read each cycle |
|---|---|---|---|---|
| BME688 | Adafruit BME680 Library | I²C ch0 | `begin(0x76)` | `performReading()`; gas value only if > 0 (heater reached temperature) |
| BMP581 | SparkFun BMP581 Arduino Library | I²C ch0 | `beginI2C() == BMP5_OK` | `getSensorData()`, pressure > 0 |
| SGP41 | Sensirion I2C SGP41 + Sensirion Gas Index Algorithm | I²C ch1 | Serial number read succeeds | `executeConditioning()` for 10 s, then `measureRawSignals()` → VOC/NOx index |
| ENS160 | DFRobot_ENS160 | I²C ch1 | `begin() == 0` | Status, AQI, TVOC, eCO₂ with range checks |
| VEML6075 | Adafruit VEML6075 Library | I²C ch2 | `begin()` | `readUVA()`, `readUVB()` after an ACK check |
| OPT3001 | ClosedCube OPT3001 | I²C ch2 | Manufacturer ID `0x5449` + config write | `readResult()`, error must be `NO_ERROR` |
| MMC5603 | Adafruit MMC56x3 | I²C ch3 | `begin(0x30)` | `getEvent()` |
| INA219 | Adafruit INA219 | I²C ch3 | `begin()` | Bus voltage and current after an ACK check |
| DS3231 | RTClib (Adafruit) | I²C ch3 | `begin()`, `lostPower()`, year 2024–2099 | Read once at boot, lined up to a second boundary; `millis()` adds sub-seconds |
| MB85RC512 | Adafruit FRAM I2C (`Adafruit_EEPROM_I2C` class) | I²C ch3 | `begin(0x50)` | Write every cycle |
| BMI270 | SparkFun BMI270 Arduino Library | SPI, CS 15 | `beginSPI() == BMI2_OK` | `getSensorData()` |
| MAX-M10S | TinyGPSPlus | UART2 | — | NMEA bytes decoded on every `loop()` pass |
| SEN0463 | none (own ISR) | GPIO39 | — | Rolling 60 s CPM |
| E22 | none (HardwareSerial) | UART1 | Optional volatile register setup (`E22_AUTOCONFIG`) | Wait for AUX, then write the frame |
| PCA9548A | none (raw I²C) | I²C | ACK at 0x70 | Channel select (cached) |
| TPS3823 | none (GPIO) | GPIO32 | — | 2 µs HIGH pulse |

**Why `Adafruit_EEPROM_I2C` for the FRAM?** `Adafruit_FRAM_I2C::begin()` only accepts the product ID of the MB85RC256V. The fitted MB85RC512 reports a different ID, so it would always fail. The library's EEPROM class uses the same 16-bit-address byte protocol, which the FRAM supports natively.

**Honest data rule.** A field is sent **only** if the sensor answered and the value is finite and in range. A missing or failed sensor is **left out** of the frame. The firmware never sends a made-up `0`.

Extra dependencies: Adafruit BusIO, Adafruit Unified Sensor, Sensirion Core.

---

## 7. Flight software

### 7.1 Main loop (every 500 ms)

```text
loop():
  feed GNSS bytes to the NMEA parser (every pass, not only every 500 ms)
  handle serial commands ('S' status, 'R' reset mission record)
  every 500 ms:
    1. kick the watchdog
    2. ch0  BME688 + BMP581      -> altitude filter (BMP581 first, BME688 fallback)
    3. ch1  SGP41 (1 Hz) + ENS160 (both compensated with BME688 T/RH)
    4. ch2  VEML6075 + OPT3001
    5. ch3  MMC5603 + INA219
    6. SPI  BMI270               -> launch "boost" cue if |a| > 3 g
    7. Geiger CPM + total counts
    8. GNSS position / altitude  (only if fix is < 2 s old)
    9. state machine update      -> altitude_rel, vertical_speed, mission_state
   10. build JSON -> pack frame (CRC-32, optional HMAC) -> wait AUX -> send
   11. save sequence + state + baseline to FRAM
```

### 7.2 Altitude and vertical speed

- Pressure → altitude with the International Standard Atmosphere formula: `h = 44330 × (1 − (p / 101325)^0.190295)`.
- Altitude is smoothed with an exponential filter (α = 0.5). Vertical speed is the filtered rate of change (α = 0.4).
- The filter restarts if the pressure source changes (BMP581 ↔ BME688) or if no sample arrives for 3 s.
- **Launch-site baseline:** the average of the first 10 altitude samples (5 s). While on the pad, the baseline slowly follows weather-driven pressure drift (only while within ±1 m).

### 7.3 Mission state machine

```text
 BOOT ─► SELF_TEST ─► PRELAUNCH ─► ASCENT ─► APOGEE ─► DESCENT ─► LANDED
 (0)       (1)          (2)          (3)       (4)       (5)        (6)
```

| Transition | Condition |
|---|---|
| BOOT → SELF_TEST | Immediately after start-up (sensor health mask is printed) |
| SELF_TEST → PRELAUNCH | 10 altitude samples averaged into the baseline, **or** 30 s without a barometer (launch detection is then disabled) |
| PRELAUNCH → ASCENT | In PRELAUNCH > 5 s **and** altitude > 2 m above baseline **and** (vertical speed > 0.5 m/s **or** a > 3 g within the last 2 s); confirmed on 2 frames in a row |
| ASCENT → APOGEE | In ASCENT > 5 s, upward motion seen, then vertical speed < −0.3 m/s on 2 frames in a row. **Failsafe:** forced after 300 s |
| APOGEE → DESCENT | After 1 s (so APOGEE appears in at least 2 frames) |
| DESCENT → LANDED | Altitude within ±5 m for more than 10 s **and** \|vertical speed\| < 0.3 m/s |

### 7.4 Brownout and reset recovery

Every transition and every cycle is saved to FRAM. After a reset:

- The **sequence number** always continues from where it stopped, so the ground station never sees repeated numbers.
- If the saved state was **ASCENT, APOGEE or DESCENT**, the CanSat resumes in that state with the saved baseline and maximum altitude.
- Any other saved state starts a fresh pad session.
- Send `R` over USB serial to clear the mission record before a new flight.

FRAM layout:

| Address | Content |
|---|---|
| 0x00–0x03 | Sequence number (uint32 LE) |
| 0x04 | Mission state |
| 0x05 | Sentinel `0xA1` (record valid) |
| 0x06 | CRC-8 (poly 0x07) over 0x00–0x04 |
| 0x10–0x13 | Baseline altitude (float) |
| 0x14 | Baseline valid flag |
| 0x15–0x18 | Maximum relative altitude (float) |
| 0x19 | CRC-8 over 0x10–0x18 |

### 7.5 Timestamps

Priority: **GNSS UTC** (fix < 1.5 s old, includes centiseconds and fix age) → **DS3231** (+ `millis()` for sub-seconds) → **0**. A timestamp of 0 tells the ground station to use its own receive time. A time is never invented.

### 7.6 Serial console (115200 baud, USB)

| Command | Action |
|---|---|
| `S` | Print state, sequence, health mask, relative altitude, vertical speed and the resumed flag |
| `R` | Clear the FRAM mission record, reset the sequence to 0 and restart at SELF_TEST |

---

## 8. Telemetry packet structure

### 8.1 Frame layout (binary, little-endian)

```text
┌────────┬─────┬──────┬──────────┬──────────────┬─────────┬───────┬───────────┬────────────┬────────┐
│ MAGIC  │ VER │ TYPE │ SEQUENCE │ TIMESTAMP_US │ PAY_LEN │ FLAGS │  PAYLOAD  │ [AUTH TAG] │ CRC32  │
│ 4 B    │ 1 B │ 1 B  │ 4 B      │ 8 B          │ 2 B     │ 1 B   │  N B      │ [8 B]      │ 4 B    │
└────────┴─────┴──────┴──────────┴──────────────┴─────────┴───────┴───────────┴────────────┴────────┘
 offset 0   4     5      6          10             18        20      21          21+N         21+N(+8)
```

| Offset | Size | Field | Value / meaning |
|---|---|---|---|
| 0 | 4 | MAGIC | `A1 60 4E 45` — marks the start of a frame so the parser can resync |
| 4 | 1 | VERSION | `0x71` — wire-protocol identifier (must match the ground station) |
| 5 | 1 | TYPE | `0x01` SENSOR_DATA (see table below) |
| 6 | 4 | SEQUENCE | uint32, +1 per frame, stored in FRAM so it survives resets. Used to count lost frames and reject duplicates |
| 10 | 8 | TIMESTAMP_US | uint64, microseconds since the Unix epoch (GNSS → RTC → 0) |
| 18 | 2 | PAYLOAD_LEN | uint16, payload bytes only (the tag is not counted) |
| 20 | 1 | FLAGS | bit 0x01 FEC, 0x02 compressed, 0x04 encrypted, **0x08 authenticated**. The firmware sets only 0x08 |
| 21 | N | PAYLOAD | UTF-8 JSON (below) |
| 21+N | 8 | AUTH TAG | Only if FLAGS & 0x08: first 8 bytes of HMAC-SHA256(key, header ‖ payload) |
| end − 4 | 4 | CRC32 | CRC-32/ISO-HDLC (poly 0xEDB88320, init/xorout 0xFFFFFFFF, reflected) over everything before it. Same as Python `binascii.crc32` |

Fixed overhead: **25 bytes** (21-byte header + 4-byte CRC), or **33 bytes** with the auth tag.

| TYPE | Name | Sent by the firmware |
|---|---|---|
| 0x01 | SENSOR_DATA | ✅ every 500 ms |
| 0x02 | GPS | reserved |
| 0x03 | SYSTEM_STATUS | reserved |
| 0x04 | COMMAND | reserved (uplink) |
| 0x05 | ACK | reserved |
| 0x06 | HEARTBEAT | reserved |
| 0x07 | FEC_DATA | reserved |
| 0x08 | ERROR | reserved |

**Link authentication (optional, recommended).** Build with `-DAIRONE_LINK_KEY_HEX="\"<hex>\""` (at least 16 bytes = 32 hex characters) and set the same key as `AIRONE_LINK_KEY` on the ground station. Keys shorter than 16 bytes are **refused** (nothing is sent) rather than silently sending unauthenticated frames. The tag proves origin and integrity only. The payload is **not encrypted**. Never commit a real key.

### 8.2 Payload (JSON)

Each measurement is one key with value, unit and source sensor:

```json
{"bmp581_pressure":{"value":101324.87,"unit":"Pa","sensor_id":"BMP581"},
 "mission_state":{"value":"DESCENT","unit":"enum","sensor_id":"FSM"}, ...}
```

Numbers use `%.7g` (8 digits for pressure, 10 for GNSS lat/lon, about 1 cm). NaN/Inf values are left out, because they would make the JSON invalid. The builder never truncates: if a field does not fit the 3,072-byte buffer, it is dropped and a warning is printed.

### 8.3 All payload fields

| Field | Unit | Sensor | Rate | Sent when |
|---|---|---|---|---|
| `bme688_temperature` | K | BME688 | 2 Hz | read OK |
| `bme688_pressure` | Pa | BME688 | 2 Hz | read OK |
| `bme688_humidity` | % | BME688 | 2 Hz | read OK |
| `bme688_gas_resistance` | Ω | BME688 | 2 Hz | heater reached temperature |
| `bmp581_temperature` | K | BMP581 | 2 Hz | read OK |
| `bmp581_pressure` | Pa | BMP581 | 2 Hz | read OK, > 0 |
| `sgp41_voc_raw` | ticks | SGP41 | 1 Hz | conditioning and measuring |
| `sgp41_nox_raw` | ticks | SGP41 | 1 Hz | after 10 s conditioning |
| `sgp41_voc` | index 1–500 | SGP41 | 1 Hz | Gas Index algorithm past warm-up |
| `sgp41_nox` | index 1–500 | SGP41 | 1 Hz | Gas Index algorithm past warm-up |
| `ens160_status` | flag 0–3 | ENS160 | 2 Hz | device answers (0 normal, 1 warm-up, 2 initial start-up, 3 invalid) |
| `ens160_aqi` | index 1–5 | ENS160 | 2 Hz | status 0 or 2, value 1–5 |
| `ens160_tvoc` | ppb | ENS160 | 2 Hz | status 0 or 2, ≤ 65,000 |
| `ens160_eco2` | ppm | ENS160 | 2 Hz | status 0 or 2, ≥ 400 |
| `veml6075_uva` | counts | VEML6075 | 2 Hz | device answers |
| `veml6075_uvb` | counts | VEML6075 | 2 Hz | device answers |
| `opt3001_lux` | lux | OPT3001 | 2 Hz | no read error |
| `mag_x`, `mag_y`, `mag_z` | µT | MMC5603 | 2 Hz | read OK |
| `battery_voltage` | V | INA219 | 2 Hz | device answers |
| `battery_current_ma` | mA | INA219 | 2 Hz | device answers |
| `imu_accel_x/y/z` | m/s² | BMI270 | 2 Hz (sample of 100 Hz ODR) | read OK |
| `imu_gyro_x/y/z` | °/s | BMI270 | 2 Hz (sample of 100 Hz ODR) | read OK |
| `radiation_cpm` | CPM | SEN0463 | 2 Hz (rolling 60 s window) | after ≥ 1 s of data |
| `radiation_counts` | counts | SEN0463 | 2 Hz | always (cumulative since boot) |
| `gnss_lat`, `gnss_lon` | ° | MAX-M10S | 2 Hz (GNSS updates at 1 Hz) | fix < 2 s old |
| `gnss_altitude` | m | MAX-M10S | 2 Hz | fix < 2 s old |
| `altitude_rel` | m | BMP581 / BME688 | 2 Hz | baseline captured |
| `vertical_speed` | m/s | BMP581 / BME688 | 2 Hz | filter has 2+ samples |
| `mission_state` | enum string | FSM | 2 Hz | always |
| `mission_state_code` | 0–6 | FSM | 2 Hz | always |
| `sensor_health_mask` | bitmask | FSM | 2 Hz | always |

**38 fields** when everything works. Measured size of a realistic full payload: **2,534 bytes** of JSON, so a full frame is **≈2,560 bytes** (≈2,570 with the auth tag).

`mission_state` is a string. The ground station reads it into the frame's mission phase. `mission_state_code` is the numeric twin (0 BOOT, 1 SELF_TEST, 2 PRELAUNCH, 3 ASCENT, 4 APOGEE, 5 DESCENT, 6 LANDED).

`sensor_health_mask` bits (set at start-up when the sensor passes its check):

| Bit | Value | Device | Bit | Value | Device |
|---|---|---|---|---|---|
| 0 | 0x001 | BME688 | 6 | 0x040 | MMC5603 |
| 1 | 0x002 | BMP581 | 7 | 0x080 | INA219 |
| 2 | 0x004 | SGP41 | 8 | 0x100 | BMI270 |
| 3 | 0x008 | ENS160 | 9 | 0x200 | DS3231 |
| 4 | 0x010 | VEML6075 | 10 | 0x400 | FRAM |
| 5 | 0x020 | OPT3001 | 11 | 0x800 | PCA9548A |

`0xFFF` = all 12 devices passed. GNSS and Geiger have no bit: they have no start-up handshake, so check that their fields appear in the frame.

---

## 9. Efficiency: memory, CPU, radio and power

### 9.1 Memory (measured at compile time)

| Resource | Used | Available | % |
|---|---|---|---|
| Program flash | 391,802 B | 1,310,720 B (default app partition) | 29 % |
| Static RAM (globals) | 33,024 B | 327,680 B | 10 % |

- The 3 KB JSON buffer and the 3.1 KB frame buffer are **static**, not on the 8 KB loop-task stack, so a full frame cannot overflow the stack.
- The UART1 TX buffer is enlarged so a whole frame is queued at once and `loop()` does not block while it is sent.
- The 8 MB PSRAM is not needed by the current firmware.

### 9.2 CPU and buses

- One cooperative loop and no RTOS tasks, so the timing is predictable and easy to check.
- GNSS bytes are parsed on every loop pass, so the 1 KB UART2 buffer never overflows between 500 ms cycles.
- The I²C bus runs at 400 kHz. Sensors are grouped by mux channel and read channel by channel. The cached mux channel avoids repeated channel writes.
- The SGP41 runs at its specified 1 Hz instead of 2 Hz. This halves its heater cycles and matches the Gas Index algorithm timing.
- The Geiger ISR is in IRAM and only increments a counter. All maths happens in the main loop.

### 9.3 Radio — the main bottleneck

| Quantity | Value |
|---|---|
| Frame size | ≈2,560 B |
| UART time per frame at 115200 baud | ≈0.22 s |
| Air time per frame at 62.5 kbps (best case) | ≥0.33 s, plus LoRa packet overhead (11 sub-packets of 240 B) |
| Transmitter on-time at 2 Hz | **≥65 %** |
| Throughput needed at 2 Hz | ≈41 kbps |

The **JSON payload is about 17× larger** than the same data packed as binary (38 values × 4 B ≈ 150 B). The JSON format was kept on purpose, because the ground-station parser, database and tests all use it. The trade-off is that the radio is nearly always transmitting. That costs power, makes the air rate high (which shortens range) and conflicts with UK duty-cycle limits ([section 10](#10-radio-link-and-uk-compliance)).

Options, from easiest to most effective:

1. Set `TELEMETRY_PERIOD_MS = 1000` (1 Hz). This still meets the primary mission and halves radio time.
2. Send short field keys or a reduced field set during flight, and log the full set onboard (MicroSD/W25Q128).
3. Add a compact binary payload as a new packet type. The frame header already supports this (`TYPE` and the `FLAGS` compressed bit). The ground parser would need a matching decoder.

### 9.4 Power

Measure the real budget with the INA219 (`battery_current_ma` is in every frame). Main consumers, from datasheet values:

- **E22 transmitter:** by far the largest. A 30 dBm (1 W) PA draws several hundred mA while sending, and at 2 Hz it is sending most of the time.
- **ESP32:** tens of mA at 240 MHz with Wi-Fi/BT off. `setCpuFrequencyMhz(80)` would save more, because the loop needs very little CPU.
- **Heated gas sensors** (BME688 heater, SGP41, ENS160): a few mA to tens of mA together.
- GNSS, IMU, magnetometer, light sensors, RTC and FRAM: small (mA or below).

FRAM wear is not a concern: one write every 500 ms uses up 10¹³ cycles only after about 150,000 years.

---

## 10. Radio link and UK compliance

| Setting | Value | Where |
|---|---|---|
| Frequency | 433 MHz (channel 23 = 410 + 23 MHz) | `E22_CHANNEL` |
| UART | 115200 8N1 | `E22_BAUD`, must match on both modules |
| Air data rate | 62.5 kbps (with `E22_AUTOCONFIG = 1`) | REG0 = `0xE7` |
| Mode | Transparent (M0 = M1 = LOW) | — |
| TX power | 30 dBm in the auto-config register set | REG1 = `0x00` |

`E22_AUTOCONFIG = 1` programs the CanSat module at boot with the **volatile** `C2` command. Nothing is written to the module's own flash. The ground module must be set to the same UART speed, air rate and channel with the EBYTE configuration tool.

> ⚠️ **Check before flying.** In the UK, the licence-exempt 433.05–434.79 MHz band (Ofcom IR 2030 / ETSI EN 300 220) is generally limited to **10 mW e.r.p.**, with a **10 % duty cycle**, and lower limits for wide modulation bandwidths. The current setup (30 dBm, ≥65 % on-time, 500 kHz LoRa bandwidth at 62.5 kbps) is **well outside** those general limits. The 30 dBm E22 parts also cannot be set as low as 10 mW. Confirm the allowed frequency, power and duty cycle with the UK CanSat organisers, and change the module, the power setting (or add an attenuator), the air rate and the frame rate to match. Reducing the payload size ([section 9.3](#93-radio--the-main-bottleneck)) is what makes a lower air rate and duty cycle possible.

---

## 11. Ground station

Python application in `AirOne_Software_Laptop/AirOne_Software_Laptop/`. Full details are in its own [README](AirOne_Software_Laptop/AirOne_Software_Laptop/README.md) and `docs/`.

### 11.1 Data path

```text
E22 (USB serial) ─► communication/serial_transport.py
                 ─► telemetry/parser.py      magic resync, CRC-32, HMAC check, duplicate + gap detection
                 ─► workers/packet_processor JSON → TelemetryFrame (mission_state → mission phase)
                 ─► data_processing/pipeline 20 stages: authenticate, CRC, FEC, schema, units → SI
                                             (e.g. °/s → rad/s), timestamp, range, rate-of-change,
                                             duplicate/order, sensor quality, calibration, filters, fusion
                 ─► storage/                 SQLite with migrations
                 ─► scientific/              atmosphere, gases (incl. boundary-layer height),
                                             radiation, UV/solar, magnetic, descent, power, GNSS
                 ─► ml/                      anomaly detection with drift checks and model provenance
                 ─► api/ (REST, JWT, RBAC)   ─► gui/ (PyQt5 operator display)
```

### 11.2 Features

- Live telemetry, flight state, packet statistics (lost / duplicate / bad CRC)
- Data logging to SQLite and export
- Graphs, GNSS map data, flight replay
- Built-in **flight simulator** that produces frames through the same real pipeline (`--simulate`)
- Secondary mission analysis: `ens160_profile`, `gas_boundary_layer_height`, radiation and UV versus altitude
- Security: JWT login, roles, account lockout, rate limiting and a tamper-evident audit log

Range limits are applied to known fields (for example `radiation_cpm` 0–1000, `ens160_eco2` 400–65,000 ppm, `imu_accel_*` ±50 m/s², gyro ±2000 °/s). Fields without a configured limit are stored as received.

---

## 12. Getting started

### 12.1 Clone

```bash
git clone https://github.com/aironecansat/airone-cansat.git
cd airone-cansat
```

### 12.2 Flight firmware

1. Install the Arduino IDE (or `arduino-cli`) and the **esp32** board package (2.0.x tested).
2. Install the libraries from [section 6](#6-firmware-drivers).
3. Open `AirOne_Firmware_CanSat/AirOne_Firmware_CanSat/airone_cansat/airone_cansat.ino`.
4. Board: **ESP32 Wrover Module**. Upload speed 921600. Serial monitor at 115200.
5. Optional settings at the top of the sketch: `ENABLE_*`, `E22_AUTOCONFIG`, `TELEMETRY_PERIOD_MS`, `AIRONE_LINK_KEY_HEX` (pass it as a build flag, never commit it).
6. Upload, open the serial monitor and check that each sensor prints `OK`.

Command line:

```bash
arduino-cli compile -b esp32:esp32:esp32wrover AirOne_Firmware_CanSat/AirOne_Firmware_CanSat/airone_cansat
arduino-cli upload  -b esp32:esp32:esp32wrover -p /dev/ttyUSB0 AirOne_Firmware_CanSat/AirOne_Firmware_CanSat/airone_cansat
```

### 12.3 Ground station

```bash
cd AirOne_Software_Laptop/AirOne_Software_Laptop
pip install -r requirements.txt
cp .env.example .env                      # then set AIRONE_JWT_SECRET
python launcher.py --validate-only        # check the installation
python launcher.py --simulate --gui       # simulated flight with the GUI
python launcher.py --serial-port /dev/ttyUSB0 --baud 115200 --force-baud   # real receiver
python -m pytest -q                       # run the tests
```

---

## 13. Pre-flight checklist

- [ ] Radio module type matches the driver (UART E22-T **or** SPI driver for E22-400M30S)
- [ ] Radio power, frequency, air rate and duty cycle approved by the organisers
- [ ] Both E22 modules: same channel, air rate and 115200 UART
- [ ] Serial log shows every sensor `OK`; `sensor_health_mask` = `0xFFF`
- [ ] GNSS fix outdoors; `gnss_lat/lon` appear in frames
- [ ] Geiger `radiation_counts` increases (background is roughly 10–30 CPM)
- [ ] ENS160 has had its first ~1 h start-up run; `ens160_status` = 0 after warm-up
- [ ] INA219 shunt value matches the calibration
- [ ] DS3231 battery fitted and time set (or GNSS fix before launch)
- [ ] `R` sent over serial to clear the old mission record
- [ ] CanSat in PRELAUNCH with a baseline captured (`S` command)
- [ ] Ground station shows 0 CRC errors and increasing sequence numbers
- [ ] Link key (if used) is the same on both ends and not committed to git

---

## 14. Repository structure

```text
airone-cansat/
├── AirOne_Firmware_CanSat/
│   └── AirOne_Firmware_CanSat/
│       ├── airone_cansat/
│       │   ├── airone_cansat.ino      flight firmware (drivers, state machine, telemetry)
│       │   └── airone_frame.h         frame builder, CRC-32, SHA-256/HMAC
│       ├── docs/
│       │   ├── hardware_connection.*  wiring + E22 setup + troubleshooting
│       │   ├── telemetry_protocol.*   frame specification
│       │   └── security_model.*       link authentication
│       └── README.md                  firmware build guide and technical reference
├── AirOne_Software_Laptop/
│   └── AirOne_Software_Laptop/
│       ├── src/                       api, communication, core, data_processing, gui,
│       │                              ml, scientific, security, simulation, storage,
│       │                              telemetry, workers
│       ├── tests/                     pytest suite
│       ├── docs/                      operator, engineering, API, deployment manuals
│       ├── launcher.py                entry point
│       ├── requirements.txt
│       ├── Dockerfile, docker-compose.yml
│       └── README.md
├── SECONDARY_MISSION.md / .pdf / .docx
└── README.md                          (this file)
```

---

## 15. Known limitations and next steps

| # | Item | Impact | Suggested fix |
|---|---|---|---|
| 1 | UART radio driver vs. SPI E22-400M30S | No telemetry | Fit an E22-400T module, or add an SX1268 (RadioLib) driver |
| 2 | Radio power and duty cycle vs. UK rules | Compliance | See [section 10](#10-radio-link-and-uk-compliance) |
| 3 | No onboard logging to MicroSD / W25Q128 | Data lost if the radio drops | Add a log writer for the same frames |
| 4 | JSON payload ≈2.5 kB | High air time and power | Binary payload / 1 Hz ([section 9.3](#93-radio--the-main-bottleneck)) |
| 5 | GNSS not set to an airborne dynamic model | Possible fix quality issues during fast changes | Send UBX-CFG-VALSET (dynamic model "airborne < 1 g") at boot |
| 6 | BMP581 uses library default oversampling | More pressure noise than needed | Raise pressure oversampling and enable the IIR filter |
| 7 | Not yet run on the flight hardware | Unknown bugs | Bench test each subsystem, then a drop test |

---

## License

See the `LICENSE` file in each project folder.
