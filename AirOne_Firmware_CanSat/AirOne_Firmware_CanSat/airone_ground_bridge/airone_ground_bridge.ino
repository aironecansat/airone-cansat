/*
 * AirOne ground bridge  --  ESP32 + EBYTE E22-400M30S (SX1268, SPI)
 * Authored by Team AirOne.
 * =====================================================================
 * Receives the CanSat's compact LoRa frames (PACKET_TYPE 0x10), verifies
 * CRC32 (and the HMAC tag when a link key is configured), expands the payload
 * back into the standard JSON SENSOR_DATA payload and writes a normal AirOne
 * frame (PACKET_TYPE 0x01, SAME sequence and timestamp) to the USB serial
 * port at 115200 baud. The laptop ground software reads that port exactly as
 * before -- no ground software change is needed. Ground-measured radio_rssi
 * (dBm) and radio_snr (dB) are added to every expanded frame.
 *
 * Radio parameters and pins come from airone_radio.h, which must be identical
 * to the flight copy (override pins with -DRADIO_PIN_xxx for your ground
 * board). Build: board "ESP32 Dev Module" or "ESP32 Wrover Module",
 * library RadioLib 7.x.
 *
 * Link key: build with the SAME -DAIRONE_LINK_KEY_HEX="\"...\"" as the CanSat.
 * With a key, unauthenticated or wrongly-signed packets are dropped and the
 * expanded frame is re-signed so the ground software can verify it. Without
 * a key, packets are accepted on CRC alone and forwarded unauthenticated.
 *
 * Diagnostics: one-line "#..." text at boot; per-packet text only when built
 * with -DBRIDGE_DEBUG=1 (the ground software resynchronises on MAGIC+CRC, but
 * keeping the stream binary-only is cleaner).
 */
#include <Arduino.h>
#include <SPI.h>
#include "airone_frame.h"
#include "airone_spibus.h"
#include "airone_radio.h"
#include "airone_compact.h"

#ifndef BRIDGE_DEBUG
#define BRIDGE_DEBUG 0
#endif
#ifndef BRIDGE_SPI_SCK
#define BRIDGE_SPI_SCK  18
#endif
#ifndef BRIDGE_SPI_MISO
#define BRIDGE_SPI_MISO 19
#endif
#ifndef BRIDGE_SPI_MOSI
#define BRIDGE_SPI_MOSI 23
#endif
#ifndef AIRONE_LINK_KEY_HEX
#define AIRONE_LINK_KEY_HEX ""
#endif

static uint8_t g_key[64];
static size_t g_key_len = 0;
static uint32_t g_rx_ok = 0, g_rx_bad = 0, g_rx_dup = 0;
static uint32_t g_last_seq = 0xFFFFFFFFu;

void setup() {
  Serial.begin(115200);
  delay(200);
  pinMode(RADIO_PIN_NSS, OUTPUT); digitalWrite(RADIO_PIN_NSS, HIGH);
  SPI.begin(BRIDGE_SPI_SCK, BRIDGE_SPI_MISO, BRIDGE_SPI_MOSI);
  spi_bus_init();
  g_key_len = airone_hex_to_key(AIRONE_LINK_KEY_HEX, g_key, sizeof(g_key));
  if (g_key_len && g_key_len < AIRONE_MIN_LINK_KEY_BYTES) {
    Serial.println(F("#BRIDGE link key too short (<16 bytes) -- ignoring it"));
    g_key_len = 0;
  }
  Serial.printf("#BRIDGE AirOne ground bridge, auth %s\n", g_key_len ? "ENABLED" : "NOT_CONFIGURED");
  if (!radio_begin(true)) Serial.println(F("#BRIDGE radio init FAILED -- check wiring"));
}

static void handle_packet(const uint8_t* buf, size_t len, float rssi, float snr) {
  AironeFrameView v;
  int st = airone_parse_frame(buf, len, g_key, g_key_len, &v);
  if (st != AIRONE_PARSE_OK) {
    g_rx_bad++;
    if (BRIDGE_DEBUG) Serial.printf("#RX bad frame (%d) len=%u rssi=%.1f\n", st, (unsigned)len, rssi);
    return;
  }
  if (v.sequence == g_last_seq) { g_rx_dup++; return; }
  g_last_seq = v.sequence;

  static char json[3072];
  static uint8_t out[3072 + 64 + AIRONE_AUTH_TAG_SIZE];
  const uint8_t* payload = v.payload;
  size_t plen = v.payload_len;
  uint8_t ptype = v.packet_type;
  if (ptype == AIRONE_PT_COMPACT) {
    plen = ac_expand_json(v.payload, v.payload_len, json, sizeof(json), rssi, snr);
    if (plen == 0) { g_rx_bad++; if (BRIDGE_DEBUG) Serial.println(F("#RX compact payload malformed")); return; }
    payload = (const uint8_t*)json;
    ptype = AIRONE_PT_SENSOR_DATA;
  }
  size_t n = airone_pack_frame_auth(out, sizeof(out), ptype, v.sequence, v.timestamp_us,
                                    payload, plen, g_key_len ? g_key : NULL, g_key_len);
  if (n == 0) { g_rx_bad++; return; }
  Serial.write(out, n);
  g_rx_ok++;
  if (BRIDGE_DEBUG)
    Serial.printf("\n#RX seq=%lu rssi=%.1f snr=%.1f radio=%uB json=%uB ok=%lu bad=%lu dup=%lu\n",
                  (unsigned long)v.sequence, rssi, snr, (unsigned)len, (unsigned)plen,
                  (unsigned long)g_rx_ok, (unsigned long)g_rx_bad, (unsigned long)g_rx_dup);
}

void loop() {
  radio_maintain(true);   // re-tries a failed init every RADIO_RETRY_MS
  if (!g_radio_ok || !g_radio_irq) return;
  static uint8_t buf[RADIO_MAX_PACKET];
  size_t len = 0;
  float rssi = NAN, snr = NAN;
  int16_t st;
  {
    SpiLock lk(100);
    if (!lk.ok()) return;
    g_radio_irq = false;
    len = g_radio.getPacketLength();
    if (len > sizeof(buf)) len = sizeof(buf);
    st = g_radio.readData(buf, len);
    rssi = g_radio.getRSSI();
    snr = g_radio.getSNR();
    g_radio.startReceive();
  }
  if (st == RADIOLIB_ERR_NONE) handle_packet(buf, len, rssi, snr);
  else { g_rx_bad++; if (BRIDGE_DEBUG) Serial.printf("#RX radio error %d\n", st); }
}
