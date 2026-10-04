# AirOne — PACKAGE 1 of 2 — FIRMWARE (flash onto the CanSat ESP32)

This package contains ONLY the flight firmware. The ground-station software for the laptop is in the `AirOne_Software_Laptop/` folder of this repository.

---

# AirOne CanSat Flight Firmware (ESP32)

Reference firmware for the AirOne CanSat. It reads the real sensor suite
and transmits telemetry over a **LoRa E22** module (UART, transparent mode)
using the **exact AirOne binary frame protocol** the ground station parses.

## Files
- `airone_cansat/airone_cansat.ino` — main sketch (sensor reads + telemetry loop)
- `airone_cansat/airone_frame.h` — binary framing + CRC32 (matches
  `src/telemetry/protocol.py` byte-for-byte; verified in CI-style test)

## Protocol compatibility (verified)
The C frame builder produces frames **identical** to the Python
`protocol.pack_frame`, and the Python parser unpacks the C output with a valid
CRC. Frame layout (little-endian):

```
MAGIC A1 60 4E 45 | VER 0x71 | TYPE | SEQ u32 | TS_US u64 | LEN u16 | FLAGS u8 | PAYLOAD | [AUTH_TAG 8] | CRC32 u32
```

CRC32 = standard CRC-32/ISO-HDLC (== Python `binascii.crc32`), covering
everything before it (including the tag when present).

## Link authentication (optional, recommended)
Set `AIRONE_LINK_KEY_HEX` at the top of the `.ino` (or via a build define) to a
hex key of **at least 16 bytes** — the same value as `AIRONE_LINK_KEY` on the
ground station. Every frame then carries `FLAGS |= 0x08` and an 8-byte
`HMAC-SHA256(key, header‖payload)` tag (`airone_pack_frame_auth`, portable
SHA-256 in `airone_frame.h`, verified against RFC 4231 and byte-for-byte
against the Python implementation). With an empty key the firmware prints
`LINK AUTH: NOT_CONFIGURED` at boot and sends plain frames. Keys shorter than
16 bytes are refused (frame not sent). The tag authenticates origin and
integrity only — telemetry is **not** encrypted on air. Rotate the key per
campaign; anyone with the flash image can read it.

The PAYLOAD is UTF-8 JSON: `{field_name: {"value", "unit", "sensor_id"}}`.
Field names match the ground-station pipeline exactly (see the header comment
in the `.ino`). Temperatures are transmitted in **Kelvin** (the canonical unit
of the `*_temperature` fields).

## Scientific honesty
A field is included **only** when its sensor read succeeds. A failed or
un-wired sensor is **omitted** — the firmware never transmits a fabricated `0`.
Missing fields are treated as absent by the ground station, not as valid zero.

## Required Arduino libraries
Install with the Arduino Library Manager (board: **ESP32 Wrover Module**, esp32 core 2.0.x or 3.x):

| Library | Used for |
|---|---|
| Adafruit BME680 Library | BME688 (temperature, humidity, pressure, gas resistance) |
| SparkFun BMP581 Arduino Library | BMP581 high-resolution pressure |
| Sensirion I2C SGP41 + Sensirion Gas Index Algorithm | SGP41 VOC/NOx index |
| DFRobot_ENS160 | ENS160 TVOC / eCO₂ / AQI |
| Adafruit VEML6075 Library | UV-A / UV-B |
| ClosedCube OPT3001 | Ambient light |
| Adafruit MMC56x3 | MMC5603 magnetometer |
| Adafruit INA219 | Battery voltage / current |
| SparkFun BMI270 Arduino Library | BMI270 IMU (SPI) |
| TinyGPSPlus | MAX-M10S GNSS |
| RTClib (Adafruit) | DS3231 real-time clock |
| Adafruit FRAM I2C | MB85RC512 FRAM (persistent sequence + mission state) |

Dependencies: Adafruit BusIO, Adafruit Unified Sensor, Sensirion Core.

Disable any sensor you have not fitted with its `ENABLE_*` flag (top of the
`.ino`); that field is then simply never transmitted.

## Mission state machine
`BOOT → SELF_TEST → PRELAUNCH → ASCENT → APOGEE → DESCENT → LANDED`

The state is sent in every frame as `mission_state` (string) and
`mission_state_code` (0–6). Sequence number and state are saved to FRAM, so a
brownout/reset mid-flight resumes in the correct phase. A TPS3823 hardware
watchdog is strobed every cycle.

## Secondary mission
The firmware also collects the data for the **AirChem-Rad** secondary mission
(vertical profiles of air chemistry, UV and ionising radiation, plus descent
spin dynamics). See [`SECONDARY_MISSION.md`](../../SECONDARY_MISSION.md) in the
repository root.

## Pin map (ESP32-WROVER-E)

| Function | ESP32 pin(s) | Notes |
|---|---|---|
| I²C bus | SDA 21, SCL 22 | All I²C sensors sit behind a PCA9548A mux at 0x70 |
| SPI bus | SCK 18, MISO 19, MOSI 23 | Shared |
| BMI270 IMU | CS 15 | SPI |
| MicroSD | CS 5 | Kept deselected by the flight loop |
| W25Q128 NOR flash | CS 4 | Kept deselected |
| E22-400M30S LoRa | UART1: RX 16 ← TXD, TX 17 → RXD; M0 13, M1 14, AUX 35 | Transparent mode, 115200 baud |
| MAX-M10S GNSS | UART2: RX 25 ← TXD, TX 26 → RXD | 9600 baud |
| SEN0463 Geiger | GPIO39 | Pulse input (ISR, rolling 60 s CPM) |
| TPS3823 watchdog | GPIO32 | WDI strobe |

| Mux channel | Devices |
|---|---|
| 0 | BME688 (0x76), BMP581 (0x47) |
| 1 | SGP41 (0x59), ENS160 (0x52) |
| 2 | VEML6075 (0x10), OPT3001 (0x44) |
| 3 | INA219 (0x40), MMC5603 (0x30), DS3231 (0x68), FRAM (0x50) |

> **Check on your PCB:** on WROVER modules GPIO16/17 are normally used by the
> internal PSRAM. The firmware follows the hardware spec, but if the radio is
> silent, check this first. GPIO35 and GPIO39 are input-only with no internal
> pull-ups.

> **Link budget:** a full frame is about 2.6 kB. Both E22 modules must use
> 115200 baud UART and the 62.5 kbps air rate for 2 Hz telemetry; otherwise
> set `TELEMETRY_PERIOD_MS` to 1000.

> The firmware has **not** yet been compiled or flown on the real hardware —
> bench-test every sensor before flight.

See `docs/hardware_connection.md` for E22 configuration and troubleshooting.
