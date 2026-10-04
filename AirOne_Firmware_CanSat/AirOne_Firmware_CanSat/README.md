# AirOne CanSat — Flight Firmware (ESP32-WROVER-E)

This folder holds the firmware that runs inside the CanSat. The laptop ground-station software is in [`AirOne_Software_Laptop/`](../../AirOne_Software_Laptop/AirOne_Software_Laptop/). The full system description (every chip, missions, ground station, compliance) is in the [repository README](../../README.md).

| | |
|---|---|
| Board | ESP32-WROVER-E-N16R8 (Arduino board **ESP32 Wrover Module**) |
| Build | ✅ Compiles (`esp32:esp32:esp32wrover`): flight **474,729 B flash (36 %)**, **39,696 B static RAM (12 %)**; ground bridge 333,387 B (25 %) |
| Host tests | ✅ `tests/run_host_tests.sh` (compact payload round trip, frame parser/HMAC, flash-log recovery, duty-cycle maths) |
| Hardware test | ❌ Not yet run on the flight board — bench-test every sensor, the radio and both log media first |
| Logging | Full JSON frame every 500 ms → MicroSD, automatic failover to the W25Q128 flash |
| Telemetry | E22-400M30S (SX1268) over **SPI** via RadioLib; compact binary frame (≤199 B) expanded to the normal JSON frame by the ground bridge |

> ⚠️ **Radio pins are an assumption.** The E22 control pins (NSS 13, RXEN 14, TXEN 33, BUSY 27, DIO1 35, NRST not connected) re-use the pads of the old UART E22 header plus the V7.1 TXEN/BUSY assignment. The V7.1 map put RXEN on GPIO32 (now the TPS3823 WDI) and NRST/DIO1 on GPIO16/17 (PSRAM on WROVER), so it could not be used as-is. **Check against the PCB** and override with `-DRADIO_PIN_xxx=n` if different.

---

## Files

| File | Contents |
|---|---|
| `airone_cansat/airone_cansat.ino` | Pin map, sensor drivers, altitude filter, state machine, FRAM persistence, timekeeping, Geiger ISR, telemetry loop |
| `airone_cansat/airone_frame.h` | Frame packer and parser, CRC-32, portable SHA-256 and HMAC-SHA256 (no mbedtls), hex key parser. Also compiles on a PC |
| `airone_cansat/airone_radio.h` | SX1268 driver wrapper (RadioLib): pins, link parameters, non-blocking TX, duty-cycle limiter |
| `airone_cansat/airone_compact.h` | Compact radio payload (PACKET_TYPE 0x10) encoder and JSON expander |
| `airone_cansat/airone_storage.h` | MicroSD + W25Q128 logging task (core 0), RAM ring buffer, failover/re-mount |
| `airone_cansat/airone_flashlog.h` | Flash-log end-of-log recovery after power loss |
| `airone_cansat/airone_spibus.h` | Shared SPI bus mutex (BMI270, SX1268, SD, flash) |
| `airone_ground_bridge/` | Ground ESP32 + E22-400M30S: receives compact frames and outputs standard AirOne frames on USB (115200) for the laptop software |
| `tests/` | Host unit tests (`run_host_tests.sh`) |
| `docs/hardware_connection.md` | Wiring, E22 configuration, troubleshooting |
| `docs/telemetry_protocol.md` | Frame specification |
| `docs/security_model.md` | Link authentication design |

---

## Build and flash

### Libraries (Arduino Library Manager)

| Library | Part | Bus |
|---|---|---|
| Adafruit BME680 Library | BME688 | I²C |
| SparkFun BMP581 Arduino Library | BMP581 | I²C |
| Sensirion I2C SGP41 | SGP41 | I²C |
| Sensirion Gas Index Algorithm | SGP41 VOC/NOx index | — |
| DFRobot_ENS160 | ENS160 | I²C |
| Adafruit VEML6075 Library | VEML6075 | I²C |
| ClosedCube OPT3001 | OPT3001 | I²C |
| Adafruit MMC56x3 | MMC5603 | I²C |
| Adafruit INA219 | INA219 | I²C |
| RTClib (Adafruit) | DS3231 | I²C |
| Adafruit FRAM I2C | MB85RC512 (via `Adafruit_EEPROM_I2C`) | I²C |
| SparkFun BMI270 Arduino Library | BMI270 | SPI |
| TinyGPSPlus | MAX-M10S | UART2 |
| RadioLib (7.x, tested 7.8.1) | E22-400M30S / SX1268 | SPI |

Dependencies: Adafruit BusIO, Adafruit Unified Sensor, Sensirion Core.

### Arduino IDE

1. Install the **esp32** board package by Espressif (2.0.x tested).
2. Tools → Board → **ESP32 Wrover Module**. Upload speed 921600.
3. Open `airone_cansat/airone_cansat.ino`, then compile and upload.
4. Serial monitor at **115200**. Each device prints `OK` or `FAIL` at boot.

### arduino-cli

```bash
arduino-cli core install esp32:esp32@2.0.17
arduino-cli compile -b esp32:esp32:esp32wrover airone_cansat
arduino-cli upload  -b esp32:esp32:esp32wrover -p /dev/ttyUSB0 airone_cansat
# with a link key (never commit the key):
arduino-cli compile -b esp32:esp32:esp32wrover \
  --build-property 'compiler.cpp.extra_flags=-DAIRONE_LINK_KEY_HEX="\"00112233445566778899aabbccddeeff\""' airone_cansat
```

### Compile-time options

| Option | Default | Effect |
|---|---|---|
| `ENABLE_BME688` … `ENABLE_I2C_MUX` | 1 | Set to 0 to remove a device; its fields are then never sent |
| `ENABLE_STORAGE` / `ENABLE_RADIO` | 1 | Set to 0 to disable SD/flash logging or the LoRa downlink |
| `RADIO_PIN_NSS/BUSY/DIO1/NRST/RXEN/TXEN` | 13/27/35/-1/14/33 | E22 control pins (see warning above) |
| `RADIO_FREQ_MHZ`, `RADIO_BW_KHZ`, `RADIO_SF`, `RADIO_CR`, `RADIO_SYNC` | 433.92, 250, 7, 5, 0x12 | LoRa link parameters — **must match the ground bridge** |
| `RADIO_SX_POWER_DBM` | -9 | SX1268 core power. The E22's PA adds gain on top; measure before raising (10 mW e.r.p. limit) |
| `RADIO_DUTY_PCT` | 10 | Transmitter on-time limit. Frames that would exceed it are not sent by radio (still logged) |
| `RADIO_TCXO_V` | 1.8 | TCXO supply on DIO3 |
| `AIRONE_LINK_KEY_HEX` | `""` | Hex key, at least 32 hex characters. Empty = unauthenticated frames. Too short = no frames are sent |
| `TELEMETRY_PERIOD_MS` | 500 | Sensor/log frame period |
| `JSON_BUF_SIZE` | 3072 | Payload buffer (a full payload is ≈2,534 B) |

### Serial commands

| Key | Action |
|---|---|
| `S` | Print state, sequence, health mask, relative altitude, vertical speed, resumed flag |
| `R` | Clear the FRAM mission record, reset the sequence and restart SELF_TEST (do this before each flight) |
| `L` | Log and radio status (SD/flash state, frames written, drops, failovers, radio TX/skip/error counts) |
| `D` | Dump the flash log over serial (`AIRONE_FLASH_DUMP <n>` … `AIRONE_FLASH_DUMP_END <crc>`) |
| `E` `E` | Erase the flash log (second `E` within 3 s) |

---

## Pin map

| Function | GPIO | Notes |
|---|---|---|
| I²C SDA / SCL | 21 / 22 | 400 kHz, behind a PCA9548A at 0x70 |
| SPI SCK / MISO / MOSI | 18 / 19 / 23 | Shared |
| BMI270 CS | 15 | |
| MicroSD CS / W25Q128 CS | 5 / 4 | Logging (shared SPI) |
| E22 NSS / BUSY / DIO1 | 13 / 27 / 35 | SPI CS, SX1268 busy, TX-done IRQ (⚠️ assumed — check PCB) |
| E22 RXEN / TXEN / NRST | 14 / 33 / — | RF switch; NRST not connected by default (⚠️ assumed) |
| GNSS UART2 RX / TX | 25 / 26 | 9600 baud |
| Geiger pulse | 39 | Input-only, needs an external pull-up, falling edge |
| TPS3823 WDI | 32 | Watchdog timeout ≈1.6 s |

| Mux ch | Devices (address) |
|---|---|
| 0 | BME688 (0x76), BMP581 (0x47) |
| 1 | SGP41 (0x59), ENS160 (0x52) |
| 2 | VEML6075 (0x10), OPT3001 (0x44) |
| 3 | INA219 (0x40), MMC5603 (0x30), DS3231 (0x68), FRAM (0x50) |

---

## Drivers and device settings

| Part | Start-up check | Configuration | Per-cycle behaviour |
|---|---|---|---|
| BME688 | `begin(0x76)` | T×8, H×2, P×4, IIR 3, heater 320 °C / 150 ms | T (K), P, RH; gas Ω only if > 0 |
| BMP581 | `beginI2C() == BMP5_OK` | Library defaults | T (K), P; main altitude source |
| SGP41 | Serial number read | T/RH compensation from BME688 | 1 Hz; 10 s conditioning; raw ticks + Gas Index (omitted during algorithm warm-up) |
| ENS160 | `begin() == 0` | Standard mode; T/RH from BME688 each cycle | Status always; AQI/TVOC/eCO₂ only when status 0 or 2 and in range |
| VEML6075 | `begin()` | 100 ms integration | Raw UV-A/UV-B counts after an ACK check |
| OPT3001 | ID `0x5449` + config write | Continuous, auto-range, 100 ms (it starts in shutdown) | Lux if no error |
| MMC5603 | `begin(0x30)` | — | µT, 3 axes |
| INA219 | `begin()` | 32 V / 2 A calibration (0.1 Ω shunt assumed) | Bus V, mA |
| BMI270 | `beginSPI(15, 1 MHz)` | ±16 g, ±2000 °/s, 100 Hz | m/s², °/s; > 3 g marks a launch cue |
| DS3231 | `begin()`, `lostPower()`, plausible year | Synced to a second boundary at boot; set from GNSS ≤ once / 10 min | Timestamp fallback |
| MB85RC512 | `begin(0x50)` | `Adafruit_EEPROM_I2C` (the FRAM class rejects the 512 Kbit ID) | Saves state every cycle with CRC-8 |
| MAX-M10S | — | NMEA, 9600 baud | Position if fix < 2 s; time if < 1.5 s |
| SEN0463 | — | IRAM ISR, 50 µs dead time | Rolling 60 s CPM + total counts |
| PCA9548A | ACK at 0x70 (absent → flat bus) | Cached channel | Selected before every I²C access |
| TPS3823 | — | Pulsed first in `setup()` | Pulsed every 500 ms and during long waits |

**Honest data rule:** a field is sent only when the read succeeded and the value is finite and in range. A failed or missing sensor is left out, never sent as `0`.

---

## State machine

`BOOT(0) → SELF_TEST(1) → PRELAUNCH(2) → ASCENT(3) → APOGEE(4) → DESCENT(5) → LANDED(6)`

| Constant | Value | Used for |
|---|---|---|
| `BASELINE_SAMPLES` | 10 (5 s) | Launch-site altitude average |
| `SELF_TEST_TIMEOUT_MS` | 30 s | Go on without a barometer |
| `PRELAUNCH_MIN_MS` | 5 s | Minimum time on the pad |
| `LAUNCH_ALT_M` / `LAUNCH_VZ_MS` | 2 m / 0.5 m/s | Launch detection (or > 3 g within 2 s), 2 frames |
| `ASCENT_MIN_MS` | 5 s | Minimum ascent before apogee can be called |
| `APOGEE_VZ_MS` | −0.3 m/s | Apogee detection, 2 frames |
| `ASCENT_TIMEOUT_MS` | 300 s | Failsafe → APOGEE |
| `APOGEE_HOLD_MS` | 1 s | APOGEE visible in ≥ 2 frames |
| `LANDED_WINDOW_M` / `LANDED_STABLE_MS` / `LANDED_VZ_MS` | ±5 m / 10 s / 0.3 m/s | Landing detection |

Altitude uses the ISA formula `44330 × (1 − (p/101325)^0.190295)` with an EMA filter (α 0.5) and a vertical-speed filter (α 0.4). BMP581 is used first and BME688 is the fallback.

**Brownout recovery:** sequence, state, baseline and maximum altitude are saved to FRAM every cycle. After a reset the sequence always continues. ASCENT, APOGEE and DESCENT are resumed; other states start a new pad session.

| FRAM addr | Content |
|---|---|
| 0x00–0x03 | Sequence (uint32 LE) |
| 0x04 | State |
| 0x05 | Sentinel 0xA1 |
| 0x06 | CRC-8 of 0x00–0x04 |
| 0x10–0x13 | Baseline altitude (float) |
| 0x14 | Baseline valid |
| 0x15–0x18 | Max relative altitude (float) |
| 0x19 | CRC-8 of 0x10–0x18 |

**Timestamps:** GNSS UTC → DS3231 + `millis()` → 0 (0 = "use the ground receive time").

---

## Telemetry frame

```text
MAGIC A1 60 4E 45 | VER 0x71 | TYPE u8 | SEQ u32 | TS_US u64 | LEN u16 | FLAGS u8 | PAYLOAD (LEN B) | [TAG 8 B] | CRC32 u32
```

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 4 | MAGIC | `A1 60 4E 45` |
| 4 | 1 | VERSION | `0x71` wire-protocol ID |
| 5 | 1 | TYPE | `0x01` SENSOR_DATA (0x02–0x08 reserved: GPS, SYSTEM_STATUS, COMMAND, ACK, HEARTBEAT, FEC_DATA, ERROR) |
| 6 | 4 | SEQUENCE | uint32 LE, kept across resets |
| 10 | 8 | TIMESTAMP_US | uint64 LE, µs since Unix epoch |
| 18 | 2 | PAYLOAD_LEN | uint16 LE, payload only |
| 20 | 1 | FLAGS | `0x08` = authenticated (0x01 FEC, 0x02 compressed, 0x04 encrypted are defined but not used) |
| 21 | N | PAYLOAD | UTF-8 JSON |
| 21+N | 8 | TAG | Only with 0x08: HMAC-SHA256(key, header‖payload)[0:8] |
| end−4 | 4 | CRC32 | CRC-32/ISO-HDLC over all bytes before it (= Python `binascii.crc32`) |

Payload: `{"<field>":{"value":<number|string>,"unit":"<unit>","sensor_id":"<sensor>"}, ...}`

| Group | Fields (unit) |
|---|---|
| Barometry | `bme688_temperature` (K), `bme688_pressure` (Pa), `bme688_humidity` (%), `bme688_gas_resistance` (Ohm), `bmp581_temperature` (K), `bmp581_pressure` (Pa) |
| Gas | `sgp41_voc_raw`, `sgp41_nox_raw` (ticks), `sgp41_voc`, `sgp41_nox` (index), `ens160_status` (flag), `ens160_aqi` (index), `ens160_tvoc` (ppb), `ens160_eco2` (ppm) |
| Light | `veml6075_uva`, `veml6075_uvb` (counts), `opt3001_lux` (lux) |
| Motion | `mag_x/y/z` (uT), `imu_accel_x/y/z` (m/s^2), `imu_gyro_x/y/z` (deg/s) |
| Power | `battery_voltage` (V), `battery_current_ma` (mA) |
| Radiation | `radiation_cpm` (CPM), `radiation_counts` (counts) |
| GNSS | `gnss_lat`, `gnss_lon` (deg), `gnss_altitude` (m) |
| Flight | `altitude_rel` (m), `vertical_speed` (m/s), `mission_state` (string), `mission_state_code` (0–6), `sensor_health_mask` (bitmask) |

`sensor_health_mask` bits: 0 BME688, 1 BMP581, 2 SGP41, 3 ENS160, 4 VEML6075, 5 OPT3001, 6 MMC5603, 7 INA219, 8 BMI270, 9 DS3231, 10 FRAM, 11 PCA9548A (`0xFFF` = all OK).

Size with all 38 fields: **≈2,534 B payload, ≈2,560 B frame** (+8 B with the tag).

---

## Efficiency notes

- Frame and JSON buffers are static (not on the 8 KB loop stack). Radio TX is interrupt-driven (DIO1) and SD/flash writes run in a separate task, so neither blocks the sensor loop.
- GNSS is parsed on every loop pass. Sensors are read one mux channel at a time, and the mux channel is cached.
- SGP41 runs at 1 Hz (its specified rate), so it heats half as often.
- **Radio:** the radio sends a compact binary frame (all 39 fields + HMAC = 199 B instead of ≈2.6 kB). At SF7 / 250 kHz that is roughly 160 ms on air (estimate — `L` prints the real counts), so with the 10 % duty-cycle limit about one frame every ~1.6 s goes out by radio. Every 500 ms frame is still logged on board.
- Wi-Fi/BT are never started. `setCpuFrequencyMhz(80)` would cut ESP32 current further; the loop needs very little CPU.
- FRAM endurance (~10¹³ writes) is enough for a write every 500 ms for far longer than the mission.

---

## Hardware checks (cannot be fixed in firmware)

- **E22 control pins** — confirm NSS/BUSY/DIO1/RXEN/TXEN/NRST against the schematic (see the top of this file). GPIO16/17 are PSRAM pins on WROVER-E and are not used.
- **GPIO39** is input-only with no internal pull-up — the board must provide it. GPIO35 (DIO1) is driven push-pull by the SX1268, so it needs none.
- **GPIO39 erratum:** spurious edges are possible while ADC1/Wi-Fi are active. Neither is used here, and the ISR has a dead-time filter.
- **INA219 shunt:** calibration assumes 0.1 Ω.
- **UK radio rules:** 433 MHz licence-exempt use is generally limited to 10 mW e.r.p. and a 10 % duty cycle. Confirm the allowed limits with the organisers (repository README, section 10).

See [`docs/hardware_connection.md`](docs/hardware_connection.md) for wiring and troubleshooting.

### Ground bridge

Second ESP32 + E22-400M30S at the ground station. Build `airone_ground_bridge/` with the same RadioLib parameters (and the same `AIRONE_LINK_KEY_HEX`), plug it into the laptop and point the ground software at its USB port at 115200 baud. It outputs ordinary SENSOR_DATA frames with the CanSat's sequence and timestamp, plus `radio_rssi` and `radio_snr`. The shared headers must be identical in both sketches — `tests/run_host_tests.sh` checks this.
