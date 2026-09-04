# AirOne V7.1 — PACKAGE 1 of 2 — FIRMWARE (flash onto the CanSat ESP32)

This package contains ONLY the flight firmware. The ground-station software for the laptop is in PACKAGE 2 (AirOne_V7.1_Software_Laptop.zip).

---

# AirOne V7.1 CanSat Flight Firmware (ESP32)

Reference firmware for the AirOne V7.1 CanSat. It reads the real sensor suite
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
Adafruit BME680, SparkFun BMP581, Sensirion I2C SGP41, Adafruit VEML6075,
ClosedCube OPT3001, Adafruit MMC56x3, Adafruit INA219, TinyGPSPlus.

Disable any sensor you have not wired with its `ENABLE_*` flag (top of the
`.ino`); that field is then simply never transmitted.

## Board / wiring quick reference
- Board: ESP32-WROVER-E
- E22 on UART2: ESP32 GPIO16(RX2)↔E22 TXD, GPIO17(TX2)↔E22 RXD; tie E22
  M0=M1=GND for transparent mode; baud must match the ground station (115200).
- MAX-M10S GNSS on UART1: GPIO4(RX1)↔GNSS TX, GPIO2(TX1)↔GNSS RX @ 9600.
- I2C sensors on the default bus (SDA=21, SCL=22).
- SEN0463 Geiger: pulse output (falling edge per count) on GPIO27; counted by
  a hardware ISR and converted to CPM over a rolling 60 s window. Change
  `GEIGER_PULSE_PIN` if you wire it elsewhere, or set `ENABLE_GEIGER 0` to omit
  the field. `read_geiger_cpm()` returns `-1` only until the first second of
  counts has elapsed (honest "no data yet").

See `../docs/hardware_connection.md` for full wiring, E22 configuration, and
troubleshooting.
