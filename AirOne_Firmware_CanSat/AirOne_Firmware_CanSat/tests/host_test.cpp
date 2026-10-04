// Host-side unit tests for the pure-C++ firmware headers.
//   g++ -std=c++17 -O1 -Wall -I../airone_cansat host_test.cpp -o host_test && ./host_test
#include <cstdio>
#include <cstdlib>
#include <cmath>
#include <vector>
#include <string>
#include "airone_frame.h"
#include "airone_compact.h"
#include "airone_flashlog.h"

static int g_fail = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #c); g_fail++; } } while (0)

// Pulls "value" of field `name` out of a JSON payload (test helper only).
static bool json_value(const std::string& js, const char* name, double* v) {
  std::string key = std::string("\"") + name + "\":{\"value\":";
  size_t p = js.find(key);
  if (p == std::string::npos) return false;
  *v = strtod(js.c_str() + p + key.size(), nullptr);
  return true;
}

static void test_compact_roundtrip() {
  AcEncoder e; ac_reset(&e);
  double in[AC_NUM_FIELDS];
  for (int i = 0; i < AC_NUM_FIELDS; i++) {
    switch (AC_FIELDS[i].type) {
      case AC_I32E7: in[i] = (i == 30) ? 51.5072178 : -0.1275862; break;
      case AC_U32:   in[i] = 1000 + i; break;
      default:       in[i] = -12.345 + i * 3.1;
    }
    CHECK(ac_set(&e, i, in[i]));
  }
  CHECK(!ac_set(&e, 0, NAN));            // non-finite rejected
  CHECK(!ac_set(&e, 28, -1));            // negative U32 rejected
  e.flags |= AC_FLAG_ALT_BME688;
  uint8_t pay[AC_MAX_PAYLOAD];
  size_t plen = ac_encode(&e, pay, sizeof(pay));
  CHECK(plen == (size_t)AC_MAX_PAYLOAD);

  // Wrap exactly as the flight loop does, with authentication on.
  const uint8_t key[16] = {1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16};
  uint8_t fr[255];
  size_t flen = airone_pack_frame_auth(fr, sizeof(fr), AIRONE_PT_COMPACT, 77, 1700000000123456ULL,
                                       pay, plen, key, sizeof(key));
  CHECK(flen > 0 && flen <= 255);
  printf("compact radio frame (all %d fields, HMAC): %zu bytes\n", AC_NUM_FIELDS, flen);

  AironeFrameView v;
  CHECK(airone_parse_frame(fr, flen, key, sizeof(key), &v) == AIRONE_PARSE_OK);
  CHECK(v.packet_type == AIRONE_PT_COMPACT && v.sequence == 77 && v.timestamp_us == 1700000000123456ULL);
  uint8_t wrong[16] = {0};
  CHECK(airone_parse_frame(fr, flen, wrong, sizeof(wrong), &v) == AIRONE_PARSE_AUTH);
  fr[30] ^= 1;
  CHECK(airone_parse_frame(fr, flen, key, sizeof(key), &v) == AIRONE_PARSE_CRC);
  fr[30] ^= 1;
  CHECK(airone_parse_frame(fr, flen, key, sizeof(key), &v) == AIRONE_PARSE_OK);

  char js[4096];
  size_t jl = ac_expand_json(v.payload, v.payload_len, js, sizeof(js), -97.5, 6.25);
  CHECK(jl > 0);
  std::string s(js, jl);
  for (int i = 0; i < AC_NUM_FIELDS; i++) {
    double got = NAN;
    CHECK(json_value(s, AC_FIELDS[i].name, &got));
    double tol = AC_FIELDS[i].type == AC_I32E7 ? 1e-7 : (AC_FIELDS[i].type == AC_U32 ? 0 : fabs(in[i]) * 1e-6 + 1e-6);
    if (fabs(got - in[i]) > tol) { printf("  field %s: %.10g != %.10g\n", AC_FIELDS[i].name, got, in[i]); g_fail++; }
  }
  CHECK(s.find("\"sensor_id\":\"BME688\"}") != std::string::npos);
  CHECK(s.find("\"radio_rssi\":{\"value\":-97.5") != std::string::npos);
  CHECK(s.find("\"mission_state\"") == std::string::npos);   // code 1035 is not a valid state

  // Partial set + mission_state expansion.
  ac_reset(&e);
  ac_set(&e, 35, 5);   // DESCENT
  ac_set(&e, 20, 3.91);
  plen = ac_encode(&e, pay, sizeof(pay));
  CHECK(plen == AC_HEADER_SIZE + 8);
  jl = ac_expand_json(pay, plen, js, sizeof(js), NAN, NAN);
  s.assign(js, jl);
  CHECK(s.find("\"mission_state\":{\"value\":\"DESCENT\"") != std::string::npos);
  CHECK(s.find("radio_rssi") == std::string::npos);
  CHECK(ac_expand_json(pay, plen - 1, js, sizeof(js), NAN, NAN) == 0);   // length mismatch
}

// ---- Flash log recovery ------------------------------------------------
static std::vector<uint8_t> g_flash;
static bool rd(uint32_t a, uint8_t* b, size_t n, void*) {
  if (a + n > g_flash.size()) return false;
  memcpy(b, g_flash.data() + a, n);
  return true;
}
static size_t put_frame(uint32_t at, uint32_t seq, size_t plen) {
  std::vector<uint8_t> pay(plen, 'x');
  uint8_t fr[4096];
  size_t n = airone_pack_frame_auth(fr, sizeof(fr), AIRONE_PT_SENSOR_DATA, seq, 0, pay.data(), plen, nullptr, 0);
  memcpy(g_flash.data() + at, fr, n);
  return n;
}

static void test_flashlog() {
  const uint32_t CAP = 64 * 1024;
  g_flash.assign(CAP, 0xFF);
  uint32_t rec = 0, sk = 0;
  CHECK(flashlog_find_end(CAP, rd, nullptr, nullptr, &rec, &sk) == 0 && rec == 0);

  uint32_t pos = 0;
  for (int i = 0; i < 10; i++) pos += put_frame(pos, i, 500 + i);
  CHECK(flashlog_find_end(CAP, rd, nullptr, nullptr, &rec, &sk) == pos && rec == 10 && sk == 0);

  // Torn record: full header, body cut short by a power loss.
  uint32_t torn_at = pos;
  size_t n = put_frame(torn_at, 10, 800);
  memset(g_flash.data() + torn_at + 100, 0xFF, n - 100);
  uint32_t end = flashlog_find_end(CAP, rd, nullptr, nullptr, &rec, &sk);
  CHECK(end == torn_at + n);           // skipped by declared length, append after it
  CHECK(rec == 11);

  // Torn header (only 10 bytes programmed).
  g_flash.assign(CAP, 0xFF);
  pos = put_frame(0, 0, 300);
  uint8_t hdr[32];
  memcpy(hdr, g_flash.data(), 10);
  memcpy(g_flash.data() + pos, hdr, 10);
  end = flashlog_find_end(CAP, rd, nullptr, nullptr, &rec, &sk);
  CHECK(end == pos + 10 && rec == 1 && sk == 1);
  // Appending there and re-scanning finds the new record.
  size_t m = put_frame(end, 1, 200);
  CHECK(flashlog_find_end(CAP, rd, nullptr, nullptr, &rec, &sk) == end + m && rec == 2);

  // Full device.
  g_flash.assign(4096, 0x00);
  CHECK(flashlog_find_end(4096, rd, nullptr, nullptr, &rec, &sk) == 4096);
}

// Same formula as radio_send(): off-time keeps airtime share <= duty.
static void test_duty_cycle() {
  for (uint32_t air = 1; air < 2000; air += 37) {
    for (uint32_t duty : {1u, 10u, 50u, 100u}) {
      uint32_t period = air + (air * (100UL - duty)) / duty;
      CHECK(air * 100 <= period * duty + duty * 100);   // integer rounding <= 1 ms
    }
  }
}

int main() {
  test_compact_roundtrip();
  test_flashlog();
  test_duty_cycle();
  printf(g_fail ? "%d FAILURE(S)\n" : "ALL TESTS PASSED\n", g_fail);
  return g_fail ? 1 : 0;
}
