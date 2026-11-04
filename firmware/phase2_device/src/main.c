/*
 * AEISP Phase 2 - device-management firmware for the Shrike Lite (RP2040).
 * Protocol and command list: see src/proto.h and src/device.c.
 *
 * Board facts (Vicharak Shrike-Lite):
 *   GPIO4  = user LED
 *   GPIO12 = FPGA_PWR, GPIO13 = FPGA_EN   <- this firmware never touches these
 */
#include <stdio.h>
#include <string.h>

#include "hardware/adc.h"
#include "hardware/clocks.h"
#include "hardware/structs/watchdog.h"
#include "hardware/watchdog.h"
#include "pico/bootrom.h"
#include "pico/stdlib.h"
#include "pico/unique_id.h"
#include "pico/version.h"


#include "device.h"
#include "proto.h"

#define LED_PIN 4
#define LED_ACTIVE_HIGH                                                        \
  1 /* set to 0 if the LED turns ON when the pin is driven low  */
#define WATCHDOG_MS                                                            \
  3000 /* main loop must run at least this often or the chip resets */
#define LINE_IDLE_TIMEOUT_MS                                                   \
  2000 /* a half-received line is dropped after this much silence   */

static char g_uid[2 * PICO_UNIQUE_BOARD_ID_SIZE_BYTES + 1];
static device_t g_dev;

/* ---------- hardware abstraction for the device core ---------- */

static void hal_led_write(bool on) {
  gpio_put(LED_PIN, LED_ACTIVE_HIGH ? on : !on);
}

static uint32_t hal_uptime_ms(void) {
  return to_ms_since_boot(get_absolute_time());
}

static uint64_t hal_time_us(void) { return time_us_64(); }

static int32_t hal_temp_milli_c(uint16_t *raw_out) {
  adc_select_input(4); /* channel 4 = on-chip temperature sensor */
  uint32_t sum = 0;
  for (int i = 0; i < 16; i++) {
    sum += adc_read();
  }
  uint16_t raw = (uint16_t)(sum / 16u);
  float volts = (float)raw * 3.3f / 4096.0f;
  float celsius = 27.0f - (volts - 0.706f) / 0.001721f;
  *raw_out = raw;
  return (int32_t)(celsius * 1000.0f);
}

static device_hal_t g_hal = {
    .led_write = hal_led_write,
    .uptime_ms = hal_uptime_ms,
    .time_us = hal_time_us,
    .temp_milli_c = hal_temp_milli_c,
    .reset_reason = "UNKNOWN",
    .uid = g_uid,
    .sdk_version = PICO_SDK_VERSION_STRING,
    .sys_clock_mhz = 0,
};

/* ---------- actions that must happen after the reply has been sent ----------
 */

static void run_action(device_action_t a) {
  switch (a) {
  case ACT_REBOOT:
    sleep_ms(100);             /* let the USB reply leave */
    watchdog_reboot(0, 0, 50); /* normal restart through the watchdog */
    for (;;)
      tight_loop_contents();
  case ACT_BOOTSEL:
    sleep_ms(100);
    /* don't let our watchdog fire while the board sits in the bootloader */
    hw_clear_bits(&watchdog_hw->ctrl, WATCHDOG_CTRL_ENABLE_BITS);
    reset_usb_boot(0, 0);
    for (;;)
      tight_loop_contents();
  case ACT_HANG:
    /* Deliberate lock-up to prove the watchdog recovers the board. */
    for (;;)
      tight_loop_contents();
  default:
    break;
  }
}

int main(void) {
  /* Read the reset cause BEFORE enabling our own watchdog (that overwrites
   * scratch[4]). */
  if (!watchdog_caused_reboot()) {
    g_hal.reset_reason = "POWER_ON_OR_RESET";
  } else if (watchdog_enable_caused_reboot()) {
    g_hal.reset_reason = "WATCHDOG_TIMEOUT";
  } else {
    g_hal.reset_reason = "SOFTWARE_REBOOT";
  }

  stdio_init_all();
  pico_get_unique_board_id_string(g_uid, sizeof(g_uid));
  g_hal.sys_clock_mhz = clock_get_hz(clk_sys) / 1000000u;

  gpio_init(LED_PIN);
  gpio_set_dir(LED_PIN, GPIO_OUT);

  adc_init();
  adc_set_temp_sensor_enabled(true);

  device_init(&g_dev, &g_hal);
  watchdog_enable(WATCHDOG_MS, true);

  char line[PROTO_LINE_MAX + 1];
  char reply[PROTO_REPLY_MAX];
  size_t len = 0;
  bool overflow = false;
  uint32_t last_rx_ms = hal_uptime_ms();

  while (true) {
    watchdog_update();
    device_tick(&g_dev);

    int c = getchar_timeout_us(1000);
    uint32_t now = hal_uptime_ms();

    if (c == PICO_ERROR_TIMEOUT) {
      if ((len > 0 || overflow) &&
          (uint32_t)(now - last_rx_ms) > LINE_IDLE_TIMEOUT_MS) {
        size_t n = device_error_reply(&g_dev, PE_TIMEOUT, reply, sizeof(reply));
        fwrite(reply, 1, n, stdout);
        fflush(stdout);
        len = 0;
        overflow = false;
      }
      continue;
    }

    last_rx_ms = now;
    if (c == '\r') {
      continue;
    }
    if (c == '\n') {
      size_t n;
      if (overflow) {
        n = device_error_reply(&g_dev, PE_TOO_LONG, reply, sizeof(reply));
      } else {
        line[len] = '\0';
        n = device_handle_line(&g_dev, line, reply, sizeof(reply));
      }
      if (n > 0) {
        fwrite(reply, 1, n, stdout);
        fflush(stdout);
      }
      len = 0;
      overflow = false;
      if (g_dev.pending != ACT_NONE) {
        device_action_t a = g_dev.pending;
        g_dev.pending = ACT_NONE;
        run_action(a);
      }
      continue;
    }
    if (overflow) {
      continue;
    }
    if (len < PROTO_LINE_MAX) {
      line[len++] = (char)c;
    } else {
      overflow = true;
    }
  }
}