/*
 * AirOne radio link: EBYTE E22-400M30S (Semtech SX1268) in native SPI mode.
 * ---------------------------------------------------------------------
 * Driven with RadioLib (jgromes/RadioLib >= 7.x). The E22 has no UART here:
 * the ESP32 talks to the SX1268 over the shared VSPI bus (NSS below) and
 * controls the module's RF switch with RXEN / TXEN. BUSY and DIO1 are SX1268
 * push-pull outputs, so input-only GPIOs are fine for them.
 *
 * THIS FILE MUST BE IDENTICAL in airone_cansat/ and airone_ground_bridge/
 * (the radio parameters below are the link contract). Override any value
 * with -D at build time; both ends must use the same RADIO_FREQ_MHZ / BW /
 * SF / CR / SYNC.
 *
 * Transmission is NON-BLOCKING: radio_send() starts a packet and returns;
 * radio_poll() (called every loop) finishes it when DIO1 signals TX_DONE.
 * Every SPI access holds the shared SPI mutex (SpiLock, airone_spibus.h) so
 * the SD / flash logging task on core 0 never collides with the radio.
 *
 * DUTY CYCLE: UK IR2030 / ETSI EN 300 220 limit the 433.05-434.79 MHz band to
 * 10 mW e.r.p. and 10 % duty cycle. radio_send() refuses to start a packet
 * until the previous airtime x (100/RADIO_DUTY_PCT - 1) has elapsed, so the
 * long-run duty cycle never exceeds RADIO_DUTY_PCT. Frames that are skipped
 * are still logged on board (SD / flash) at the full 2 Hz.
 */
#ifndef AIRONE_RADIO_H
#define AIRONE_RADIO_H

#include <Arduino.h>
#include <SPI.h>
#include <RadioLib.h>
#include "airone_spibus.h"

// ---- Pin map (verify against the PCB; see README "Radio wiring") --------
#ifndef RADIO_PIN_NSS
#define RADIO_PIN_NSS   13      // SX1268 NSS (SPI chip select)
#endif
#ifndef RADIO_PIN_BUSY
#define RADIO_PIN_BUSY  27      // SX1268 BUSY  (input)
#endif
#ifndef RADIO_PIN_DIO1
#define RADIO_PIN_DIO1  35      // SX1268 DIO1  IRQ (input-only pin is fine)
#endif
#ifndef RADIO_PIN_NRST
#define RADIO_PIN_NRST  -1      // SX1268 NRESET; -1 = not connected (soft reset only)
#endif
#ifndef RADIO_PIN_RXEN
#define RADIO_PIN_RXEN  14      // E22 RXEN (LNA path)
#endif
#ifndef RADIO_PIN_TXEN
#define RADIO_PIN_TXEN  33      // E22 TXEN (PA path)
#endif
#ifndef RADIO_PIN_PWR_EN
// Enable of the TPS22919 load switch that gates the TPS61022 5 V boost feeding
// the E22. -1 = not driven by firmware (switch hard-wired on / pulled up).
// If your board pulls this enable LOW, you MUST set it, or the radio is unpowered.
#define RADIO_PIN_PWR_EN -1
#endif
#ifndef RADIO_RETRY_MS
#define RADIO_RETRY_MS  10000   // re-try a failed radio init this often
#endif

// ---- Link parameters (MUST match the ground bridge) ---------------------
#ifndef RADIO_FREQ_MHZ
#define RADIO_FREQ_MHZ  433.92f // centre of the 433.05-434.79 MHz SRD band
#endif
#ifndef RADIO_BW_KHZ
#define RADIO_BW_KHZ    250.0f  // 250 kHz stays inside the 1.74 MHz band
#endif
#ifndef RADIO_SF
#define RADIO_SF        7
#endif
#ifndef RADIO_CR
#define RADIO_CR        5       // coding rate 4/5
#endif
#ifndef RADIO_SYNC
#define RADIO_SYNC      0x12    // private LoRa sync word
#endif
#ifndef RADIO_PREAMBLE
#define RADIO_PREAMBLE  8
#endif
#ifndef RADIO_TCXO_V
#define RADIO_TCXO_V    1.8f    // E22-400M30S TCXO powered from DIO3
#endif
// SX1268 core output power in dBm (-9..+22). The E22-400M30S adds an external
// PA (up to 30 dBm module output at +22), so the ANTENNA power is this value
// plus the PA gain. Default is the minimum; measure the conducted output and
// raise it only as far as the 10 mW e.r.p. licence-exempt limit allows.
#ifndef RADIO_SX_POWER_DBM
#define RADIO_SX_POWER_DBM -9
#endif
#ifndef RADIO_DUTY_PCT
#define RADIO_DUTY_PCT  10
#endif
#define RADIO_MAX_PACKET 255

#ifndef RADIO_BUSY_TIMEOUT_MS
#define RADIO_BUSY_TIMEOUT_MS 100   // RadioLib default is 1000 ms per SPI command
#endif

// Optional hook called while RadioLib waits (BUSY polling, delays). The flight
// sketch points it at its TPS3823 strobe: with a dead/missing module RadioLib's
// chip search (10 tries x BUSY timeouts) could otherwise outlast the 1.6 s
// hardware watchdog and boot-loop the whole CanSat.
static void (*g_radio_wait_hook)() = nullptr;

class AironeRadioHal : public ArduinoHal {
 public:
  explicit AironeRadioHal(SPIClass& spi) : ArduinoHal(spi) {}
  void yield() override {
    if (g_radio_wait_hook) g_radio_wait_hook();
    ArduinoHal::yield();
  }
  void delay(RadioLibTime_t ms) override {
    while (ms > 0) {
      RadioLibTime_t step = ms > 100 ? 100 : ms;
      if (g_radio_wait_hook) g_radio_wait_hook();
      ArduinoHal::delay(step);
      ms -= step;
    }
  }
};

static AironeRadioHal g_radio_hal(SPI);
static Module g_radio_mod(&g_radio_hal, RADIO_PIN_NSS, RADIO_PIN_DIO1,
                          RADIO_PIN_NRST < 0 ? RADIOLIB_NC : RADIO_PIN_NRST,
                          RADIO_PIN_BUSY);
static SX1268 g_radio(&g_radio_mod);
static bool g_radio_ok = false;
static volatile bool g_radio_irq = false;
static bool g_radio_tx_busy = false;
static uint32_t g_radio_tx_start_ms = 0;
static uint32_t g_radio_next_ok_ms = 0;
static uint32_t g_radio_tx_count = 0, g_radio_tx_skipped = 0, g_radio_tx_errors = 0;
static int16_t g_radio_last_err = 0;

static void IRAM_ATTR radio_isr() { g_radio_irq = true; }

static const char* radio_err_hint(int16_t e) {
  switch (e) {
    case RADIOLIB_ERR_CHIP_NOT_FOUND: return "chip not found (SPI/NSS/BUSY wiring, module power)";
    case RADIOLIB_ERR_SPI_CMD_TIMEOUT: return "BUSY never released (BUSY pin / TCXO voltage)";
    case RADIOLIB_ERR_INVALID_TCXO_VOLTAGE: return "invalid TCXO voltage";
    default: return "";
  }
}

// Called once from setup() after SPI.begin() and spi_bus_init().
// Leaves the radio in RX when rx_mode is true (ground bridge), else standby.
static bool radio_begin(bool rx_mode) {
  static bool powered = false;
  if (RADIO_PIN_PWR_EN >= 0 && !powered) {
    pinMode(RADIO_PIN_PWR_EN, OUTPUT);
    digitalWrite(RADIO_PIN_PWR_EN, HIGH);
    delay(20);                        // boost + module start-up
    powered = true;
  }
  SpiLock lk(2000);
  if (!lk.ok()) { Serial.println(F("[RADIO] SPI bus lock timeout")); return false; }
  g_radio_mod.spiConfig.timeout = RADIO_BUSY_TIMEOUT_MS;
  g_radio.setRfSwitchPins(RADIO_PIN_RXEN, RADIO_PIN_TXEN);
  int16_t st = g_radio.begin(RADIO_FREQ_MHZ, RADIO_BW_KHZ, RADIO_SF, RADIO_CR, RADIO_SYNC,
                             RADIO_SX_POWER_DBM, RADIO_PREAMBLE, RADIO_TCXO_V, false);
  if (st == RADIOLIB_ERR_NONE) st = g_radio.setCurrentLimit(140.0f);
  if (st == RADIOLIB_ERR_NONE) st = g_radio.setCRC(true);
  if (st != RADIOLIB_ERR_NONE) {
    g_radio_last_err = st;
    Serial.printf("[RADIO] SX1268 init FAILED (%d) %s\n", st, radio_err_hint(st));
    return false;
  }
  g_radio.setDio1Action(radio_isr);
  if (rx_mode) {
    st = g_radio.startReceive();
    if (st != RADIOLIB_ERR_NONE) { Serial.printf("[RADIO] startReceive failed (%d)\n", st); return false; }
  }
  g_radio_ok = true;
  Serial.printf("[RADIO] SX1268 OK: %.3f MHz BW%.0f SF%d CR4/%d sync 0x%02X, SX power %d dBm, duty <= %d%%\n",
                (double)RADIO_FREQ_MHZ, (double)RADIO_BW_KHZ, RADIO_SF, RADIO_CR, RADIO_SYNC,
                RADIO_SX_POWER_DBM, RADIO_DUTY_PCT);
  Serial.printf("[RADIO] pins NSS=%d BUSY=%d DIO1=%d NRST=%d RXEN=%d TXEN=%d PWR_EN=%d\n",
                RADIO_PIN_NSS, RADIO_PIN_BUSY, RADIO_PIN_DIO1, RADIO_PIN_NRST,
                RADIO_PIN_RXEN, RADIO_PIN_TXEN, RADIO_PIN_PWR_EN);
  return true;
}

// Re-try a failed init every RADIO_RETRY_MS (e.g. module powered late or a
// loose connector). Cheap no-op while the radio is up.
static void radio_maintain(bool rx_mode) {
  static uint32_t last_try = 0;
  if (g_radio_ok) return;
  uint32_t now = millis();
  if (now - last_try < RADIO_RETRY_MS) return;
  last_try = now;
  radio_begin(rx_mode);
}

// Airtime in ms for a packet of len bytes with the configured parameters.
static uint32_t radio_airtime_ms(size_t len) {
  return (uint32_t)((g_radio.getTimeOnAir(len) + 999UL) / 1000UL);
}

// Finishes a packet in flight. Call every loop iteration (cheap when idle).
static void radio_poll() {
  if (!g_radio_ok || !g_radio_tx_busy) return;
  bool done = g_radio_irq;
  bool timeout = (millis() - g_radio_tx_start_ms) > 2000;   // >> max airtime
  if (!done && !timeout) return;
  SpiLock lk(200);
  if (!lk.ok()) return;                 // try again next loop
  g_radio_irq = false;
  g_radio.finishTransmit();             // clears IRQ, standby, RF switch off
  g_radio_tx_busy = false;
  if (timeout && !done) { g_radio_tx_errors++; Serial.println(F("[RADIO] TX timeout (DIO1 never fired)")); }
}

/*
 * Starts sending one packet (<= 255 bytes). Returns 1 = started,
 * 0 = skipped (previous packet still in the air, or duty-cycle wait),
 * -1 = error.
 */
static int radio_send(const uint8_t* data, size_t len) {
  if (!g_radio_ok || len == 0 || len > RADIO_MAX_PACKET) return -1;
  radio_poll();
  uint32_t now = millis();
  if (g_radio_tx_busy || (int32_t)(now - g_radio_next_ok_ms) < 0) { g_radio_tx_skipped++; return 0; }
  SpiLock lk(100);
  if (!lk.ok()) { g_radio_tx_skipped++; return 0; }
  uint32_t air = radio_airtime_ms(len);
  g_radio_irq = false;
  int16_t st = g_radio.startTransmit(data, len);
  if (st != RADIOLIB_ERR_NONE) {
    g_radio_tx_errors++; g_radio_last_err = st;
    return -1;
  }
  g_radio_tx_busy = true;
  g_radio_tx_start_ms = now;
  // Off-time so that airtime / (airtime + off) <= RADIO_DUTY_PCT %.
  g_radio_next_ok_ms = now + air + (air * (100UL - RADIO_DUTY_PCT)) / RADIO_DUTY_PCT;
  g_radio_tx_count++;
  return 1;
}

static void radio_print_status() {
  Serial.printf("[RADIO] %s tx=%lu skipped(duty/busy)=%lu errors=%lu last_err=%d\n",
                g_radio_ok ? "OK" : "DOWN", (unsigned long)g_radio_tx_count,
                (unsigned long)g_radio_tx_skipped, (unsigned long)g_radio_tx_errors, g_radio_last_err);
}

#endif  // AIRONE_RADIO_H
