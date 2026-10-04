/*
 * AirOne binary frame builder for the flight firmware.
 * ---------------------------------------------------------------------
 * Produces frames byte-for-byte identical to what src/telemetry/protocol.py
 * on the ground station parses. All multi-byte integers are little-endian.
 * The CRC32 is the standard CRC-32/ISO-HDLC (poly 0xEDB88320, init 0xFFFFFFFF,
 * reflected in/out, final XOR 0xFFFFFFFF) -- identical to Python's
 * binascii.crc32.
 *
 * Optional frame authentication (FLAGS bit 0x08): an 8-byte tag equal to the
 * first 8 bytes of HMAC-SHA256(link_key, header || payload) is inserted
 * between the payload and the CRC. LEN still counts payload bytes only; the
 * CRC covers header + payload + tag. The SHA-256/HMAC below are portable C
 * (no mbedtls dependency) so the same header compiles on the ESP32 and on the
 * ground-station host for the byte-parity test. Authentication provides
 * integrity/origin only -- the payload is NOT encrypted.
 */
#ifndef AIRONE_FRAME_H
#define AIRONE_FRAME_H

#include <stdint.h>
#include <stddef.h>
#include <string.h>

static const uint8_t AIRONE_MAGIC[4] = {0xA1, 0x60, 0x4E, 0x45};
static const uint8_t AIRONE_VERSION  = 0x71;

// Packet types (mirror protocol.PacketType).
enum {
  AIRONE_PT_SENSOR_DATA   = 0x01,
  AIRONE_PT_GPS           = 0x02,
  AIRONE_PT_SYSTEM_STATUS = 0x03,
  AIRONE_PT_COMMAND       = 0x04,
  AIRONE_PT_ACK           = 0x05,
  AIRONE_PT_HEARTBEAT     = 0x06,
  AIRONE_PT_FEC_DATA      = 0x07,
  AIRONE_PT_ERROR         = 0x08,
};

// CRC-32/ISO-HDLC, matches Python binascii.crc32(data) & 0xFFFFFFFF.
static inline uint32_t airone_crc32(const uint8_t* data, size_t len) {
  uint32_t crc = 0xFFFFFFFFu;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (int k = 0; k < 8; k++) {
      uint32_t mask = -(crc & 1u);
      crc = (crc >> 1) ^ (0xEDB88320u & mask);
    }
  }
  return ~crc;
}

// Little-endian writers.
static inline void airone_put_u16(uint8_t* b, uint16_t v) {
  b[0] = (uint8_t)(v & 0xFF);
  b[1] = (uint8_t)((v >> 8) & 0xFF);
}
static inline void airone_put_u32(uint8_t* b, uint32_t v) {
  b[0] = (uint8_t)(v & 0xFF);
  b[1] = (uint8_t)((v >> 8) & 0xFF);
  b[2] = (uint8_t)((v >> 16) & 0xFF);
  b[3] = (uint8_t)((v >> 24) & 0xFF);
}
static inline void airone_put_u64(uint8_t* b, uint64_t v) {
  for (int i = 0; i < 8; i++) b[i] = (uint8_t)((v >> (8 * i)) & 0xFF);
}

// ---------------------------------------------------------------------
// Portable SHA-256 (FIPS 180-4) and HMAC (RFC 2104) for the frame tag.
// ---------------------------------------------------------------------
#define AIRONE_AUTH_TAG_SIZE 8
#define AIRONE_FLAG_AUTHENTICATED 0x08
#define AIRONE_MIN_LINK_KEY_BYTES 16

typedef struct {
  uint32_t state[8];
  uint64_t bitlen;
  uint8_t  buf[64];
  size_t   buflen;
} airone_sha256_ctx;

static const uint32_t AIRONE_SHA256_K[64] = {
  0x428a2f98u,0x71374491u,0xb5c0fbcfu,0xe9b5dba5u,0x3956c25bu,0x59f111f1u,0x923f82a4u,0xab1c5ed5u,
  0xd807aa98u,0x12835b01u,0x243185beu,0x550c7dc3u,0x72be5d74u,0x80deb1feu,0x9bdc06a7u,0xc19bf174u,
  0xe49b69c1u,0xefbe4786u,0x0fc19dc6u,0x240ca1ccu,0x2de92c6fu,0x4a7484aau,0x5cb0a9dcu,0x76f988dau,
  0x983e5152u,0xa831c66du,0xb00327c8u,0xbf597fc7u,0xc6e00bf3u,0xd5a79147u,0x06ca6351u,0x14292967u,
  0x27b70a85u,0x2e1b2138u,0x4d2c6dfcu,0x53380d13u,0x650a7354u,0x766a0abbu,0x81c2c92eu,0x92722c85u,
  0xa2bfe8a1u,0xa81a664bu,0xc24b8b70u,0xc76c51a3u,0xd192e819u,0xd6990624u,0xf40e3585u,0x106aa070u,
  0x19a4c116u,0x1e376c08u,0x2748774cu,0x34b0bcb5u,0x391c0cb3u,0x4ed8aa4au,0x5b9cca4fu,0x682e6ff3u,
  0x748f82eeu,0x78a5636fu,0x84c87814u,0x8cc70208u,0x90befffau,0xa4506cebu,0xbef9a3f7u,0xc67178f2u
};

static inline uint32_t airone_rotr(uint32_t x, uint32_t n) { return (x >> n) | (x << (32 - n)); }

static inline void airone_sha256_block(airone_sha256_ctx* c, const uint8_t* p) {
  uint32_t w[64];
  for (int i = 0; i < 16; i++)
    w[i] = ((uint32_t)p[4*i] << 24) | ((uint32_t)p[4*i+1] << 16) |
           ((uint32_t)p[4*i+2] << 8) | (uint32_t)p[4*i+3];
  for (int i = 16; i < 64; i++) {
    uint32_t s0 = airone_rotr(w[i-15], 7) ^ airone_rotr(w[i-15], 18) ^ (w[i-15] >> 3);
    uint32_t s1 = airone_rotr(w[i-2], 17) ^ airone_rotr(w[i-2], 19) ^ (w[i-2] >> 10);
    w[i] = w[i-16] + s0 + w[i-7] + s1;
  }
  uint32_t a = c->state[0], b = c->state[1], cc = c->state[2], d = c->state[3];
  uint32_t e = c->state[4], f = c->state[5], g = c->state[6], h = c->state[7];
  for (int i = 0; i < 64; i++) {
    uint32_t S1 = airone_rotr(e, 6) ^ airone_rotr(e, 11) ^ airone_rotr(e, 25);
    uint32_t ch = (e & f) ^ ((~e) & g);
    uint32_t t1 = h + S1 + ch + AIRONE_SHA256_K[i] + w[i];
    uint32_t S0 = airone_rotr(a, 2) ^ airone_rotr(a, 13) ^ airone_rotr(a, 22);
    uint32_t maj = (a & b) ^ (a & cc) ^ (b & cc);
    uint32_t t2 = S0 + maj;
    h = g; g = f; f = e; e = d + t1; d = cc; cc = b; b = a; a = t1 + t2;
  }
  c->state[0] += a; c->state[1] += b; c->state[2] += cc; c->state[3] += d;
  c->state[4] += e; c->state[5] += f; c->state[6] += g; c->state[7] += h;
}

static inline void airone_sha256_init(airone_sha256_ctx* c) {
  c->state[0] = 0x6a09e667u; c->state[1] = 0xbb67ae85u; c->state[2] = 0x3c6ef372u;
  c->state[3] = 0xa54ff53au; c->state[4] = 0x510e527fu; c->state[5] = 0x9b05688cu;
  c->state[6] = 0x1f83d9abu; c->state[7] = 0x5be0cd19u;
  c->bitlen = 0; c->buflen = 0;
}

static inline void airone_sha256_update(airone_sha256_ctx* c, const uint8_t* data, size_t len) {
  for (size_t i = 0; i < len; i++) {
    c->buf[c->buflen++] = data[i];
    if (c->buflen == 64) {
      airone_sha256_block(c, c->buf);
      c->bitlen += 512;
      c->buflen = 0;
    }
  }
}

static inline void airone_sha256_final(airone_sha256_ctx* c, uint8_t out[32]) {
  c->bitlen += (uint64_t)c->buflen * 8u;
  c->buf[c->buflen++] = 0x80;
  if (c->buflen > 56) {
    while (c->buflen < 64) c->buf[c->buflen++] = 0;
    airone_sha256_block(c, c->buf);
    c->buflen = 0;
  }
  while (c->buflen < 56) c->buf[c->buflen++] = 0;
  for (int i = 7; i >= 0; i--) c->buf[c->buflen++] = (uint8_t)((c->bitlen >> (8 * i)) & 0xFF);
  airone_sha256_block(c, c->buf);
  for (int i = 0; i < 8; i++) {
    out[4*i]   = (uint8_t)(c->state[i] >> 24);
    out[4*i+1] = (uint8_t)(c->state[i] >> 16);
    out[4*i+2] = (uint8_t)(c->state[i] >> 8);
    out[4*i+3] = (uint8_t)(c->state[i]);
  }
}

static inline void airone_sha256(const uint8_t* data, size_t len, uint8_t out[32]) {
  airone_sha256_ctx c;
  airone_sha256_init(&c);
  airone_sha256_update(&c, data, len);
  airone_sha256_final(&c, out);
}

// HMAC-SHA256 (RFC 2104). Keys longer than 64 bytes are hashed first.
static inline void airone_hmac_sha256(const uint8_t* key, size_t key_len,
                                      const uint8_t* msg, size_t msg_len,
                                      uint8_t out[32]) {
  uint8_t k[64];
  uint8_t khash[32];
  memset(k, 0, sizeof(k));
  if (key_len > 64) {
    airone_sha256(key, key_len, khash);
    memcpy(k, khash, 32);
  } else {
    memcpy(k, key, key_len);
  }
  uint8_t ipad[64], opad[64], inner[32];
  for (int i = 0; i < 64; i++) { ipad[i] = k[i] ^ 0x36; opad[i] = k[i] ^ 0x5c; }
  airone_sha256_ctx c;
  airone_sha256_init(&c);
  airone_sha256_update(&c, ipad, 64);
  airone_sha256_update(&c, msg, msg_len);
  airone_sha256_final(&c, inner);
  airone_sha256_init(&c);
  airone_sha256_update(&c, opad, 64);
  airone_sha256_update(&c, inner, 32);
  airone_sha256_final(&c, out);
}

// Truncated tag: first 8 bytes of HMAC-SHA256(key, header || payload).
static inline void airone_auth_tag(const uint8_t* key, size_t key_len,
                                   const uint8_t* header_and_payload, size_t len,
                                   uint8_t out[AIRONE_AUTH_TAG_SIZE]) {
  uint8_t full[32];
  airone_hmac_sha256(key, key_len, header_and_payload, len, full);
  memcpy(out, full, AIRONE_AUTH_TAG_SIZE);
}

// Decode a hex string into key bytes. Returns the byte count, or 0 when the
// string is empty, has odd length, contains non-hex characters, or would not
// fit. A zero return means "link authentication disabled".
static inline size_t airone_hex_to_key(const char* hex, uint8_t* out, size_t out_cap) {
  if (hex == NULL) return 0;
  size_t n = strlen(hex);
  if (n == 0 || (n % 2) != 0 || n / 2 > out_cap) return 0;
  for (size_t i = 0; i < n; i += 2) {
    int v = 0;
    for (int j = 0; j < 2; j++) {
      char ch = hex[i + j];
      int d;
      if (ch >= '0' && ch <= '9') d = ch - '0';
      else if (ch >= 'a' && ch <= 'f') d = ch - 'a' + 10;
      else if (ch >= 'A' && ch <= 'F') d = ch - 'A' + 10;
      else return 0;
      v = (v << 4) | d;
    }
    out[i / 2] = (uint8_t)v;
  }
  return n / 2;
}

/*
 * Pack a complete frame (header + payload [+ auth tag] + trailing CRC32).
 * When key != NULL and key_len >= AIRONE_MIN_LINK_KEY_BYTES the frame is
 * authenticated (FLAGS |= 0x08, 8-byte tag after the payload). A key that is
 * too short is REFUSED (returns 0) rather than silently sending unauthenticated.
 * Returns the total frame length, or 0 if it would not fit in out_cap or the
 * payload exceeds the uint16 length field.
 */
static inline size_t airone_pack_frame_auth(uint8_t* out, size_t out_cap,
                                            uint8_t packet_type, uint32_t sequence,
                                            uint64_t timestamp_us,
                                            const uint8_t* payload, size_t payload_len,
                                            const uint8_t* key, size_t key_len) {
  const size_t HEADER_SIZE = 21;
  const size_t CRC_SIZE = 4;
  const int authenticated = (key != NULL && key_len > 0);
  if (authenticated && key_len < AIRONE_MIN_LINK_KEY_BYTES) return 0;
  if (payload_len > 0xFFFF) return 0;
  size_t total = HEADER_SIZE + payload_len + (authenticated ? AIRONE_AUTH_TAG_SIZE : 0) + CRC_SIZE;
  if (total > out_cap) return 0;

  size_t o = 0;
  memcpy(out + o, AIRONE_MAGIC, 4); o += 4;   // 0: MAGIC
  out[o++] = AIRONE_VERSION;                   // 4: VERSION
  out[o++] = packet_type;                      // 5: TYPE
  airone_put_u32(out + o, sequence); o += 4;   // 6: SEQ
  airone_put_u64(out + o, timestamp_us); o += 8; // 10: TS_US
  airone_put_u16(out + o, (uint16_t)payload_len); o += 2; // 18: LEN
  out[o++] = authenticated ? AIRONE_FLAG_AUTHENTICATED : 0x00; // 20: FLAGS

  memcpy(out + o, payload, payload_len); o += payload_len; // 21: PAYLOAD

  if (authenticated) {                         // tag over header+payload
    airone_auth_tag(key, key_len, out, o, out + o);
    o += AIRONE_AUTH_TAG_SIZE;
  }

  uint32_t crc = airone_crc32(out, o);         // CRC over header+payload(+tag)
  airone_put_u32(out + o, crc); o += 4;
  return o;
}

/*
 * Pack an UNAUTHENTICATED frame (header + payload + trailing CRC32) into out.
 * Returns the total frame length, or 0 if it would not fit in out_cap or the
 * payload exceeds the uint16 length field.
 */
static inline size_t airone_pack_frame(uint8_t* out, size_t out_cap,
                                       uint8_t packet_type, uint32_t sequence,
                                       uint64_t timestamp_us,
                                       const uint8_t* payload, size_t payload_len) {
  return airone_pack_frame_auth(out, out_cap, packet_type, sequence, timestamp_us,
                                payload, payload_len, NULL, 0);
}

// ---------------------------------------------------------------------
// Frame parser (used by the ground bridge and the host tests).
// ---------------------------------------------------------------------
typedef struct {
  uint8_t  packet_type;
  uint32_t sequence;
  uint64_t timestamp_us;
  uint16_t payload_len;
  uint8_t  flags;
  const uint8_t* payload;
  size_t   total_len;
} AironeFrameView;

enum {
  AIRONE_PARSE_OK = 0, AIRONE_PARSE_SHORT = -1, AIRONE_PARSE_MAGIC = -2,
  AIRONE_PARSE_VERSION = -3, AIRONE_PARSE_CRC = -4, AIRONE_PARSE_AUTH = -5
};

static inline uint32_t airone_get_u32(const uint8_t* b) {
  return (uint32_t)b[0] | ((uint32_t)b[1] << 8) | ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24);
}

/*
 * Validates one complete frame at data[0..len). Checks MAGIC, VERSION, CRC32
 * and -- when key_len > 0 -- requires and verifies the HMAC tag. When no key
 * is configured an authenticated frame is accepted on CRC alone (the tag
 * cannot be checked). Returns AIRONE_PARSE_OK and fills *v on success.
 */
static inline int airone_parse_frame(const uint8_t* data, size_t len,
                                     const uint8_t* key, size_t key_len,
                                     AironeFrameView* v) {
  if (len < 25) return AIRONE_PARSE_SHORT;
  if (memcmp(data, AIRONE_MAGIC, 4) != 0) return AIRONE_PARSE_MAGIC;
  if (data[4] != AIRONE_VERSION) return AIRONE_PARSE_VERSION;
  uint16_t plen = (uint16_t)(data[18] | (data[19] << 8));
  uint8_t flags = data[20];
  size_t tag = (flags & AIRONE_FLAG_AUTHENTICATED) ? AIRONE_AUTH_TAG_SIZE : 0;
  size_t total = 21 + (size_t)plen + tag + 4;
  if (len < total) return AIRONE_PARSE_SHORT;
  if (airone_crc32(data, total - 4) != airone_get_u32(data + total - 4)) return AIRONE_PARSE_CRC;
  if (key_len > 0) {
    if (!tag) return AIRONE_PARSE_AUTH;
    uint8_t want[AIRONE_AUTH_TAG_SIZE];
    airone_auth_tag(key, key_len, data, 21 + plen, want);
    uint8_t diff = 0;
    for (int i = 0; i < AIRONE_AUTH_TAG_SIZE; i++) diff |= (uint8_t)(want[i] ^ data[21 + plen + i]);
    if (diff) return AIRONE_PARSE_AUTH;
  }
  v->packet_type = data[5];
  v->sequence = airone_get_u32(data + 6);
  v->timestamp_us = (uint64_t)airone_get_u32(data + 10) | ((uint64_t)airone_get_u32(data + 14) << 32);
  v->payload_len = plen;
  v->flags = flags;
  v->payload = data + 21;
  v->total_len = total;
  return AIRONE_PARSE_OK;
}

#endif  // AIRONE_FRAME_H
