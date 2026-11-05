/*
 * AEISP device core: command handlers and device state, independent of the
 * RP2040 hardware. Everything that touches real hardware goes through
 * device_hal_t, so this file can be unit-tested on a PC with a fake HAL.
 */
#ifndef DEVICE_H
#define DEVICE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "proto.h"

#define FW_NAME "AEISP-DEV"
#define FW_VERSION "0.3.0"
#define BLINK_MIN_MS 50
#define BLINK_MAX_MS 10000

/* Filled in by the build system so a flashed board can be traced back to its
 * build record. */
#ifndef AEISP_BUILD_ID
#define AEISP_BUILD_ID "0"
#endif
#ifndef AEISP_SRC_HASH
#define AEISP_SRC_HASH "none"
#endif

typedef struct {
  void (*led_write)(bool on);             /* drive the user LED          */
  uint32_t (*uptime_ms)(void);            /* milliseconds since boot     */
  uint64_t (*time_us)(void);              /* microseconds since boot     */
  int32_t (*temp_milli_c)(uint16_t *raw); /* die temperature, 1/1000 degC */
  const char *reset_reason;               /* why the chip last restarted */
  const char *uid;                        /* 16 hex chars board unique id */
  const char *sdk_version;                /* e.g. "1.5.1"                */
  uint32_t sys_clock_mhz;
} device_hal_t;

typedef enum {
  ACT_NONE = 0,
  ACT_REBOOT,
  ACT_BOOTSEL,
  ACT_HANG
} device_action_t;
typedef enum { TIMER_IDLE = 0, TIMER_RUNNING, TIMER_STOPPED } timer_state_t;

typedef struct {
  const device_hal_t *hal;
  bool led_on;
  uint32_t blink_ms;
  uint32_t blink_last_ms;
  timer_state_t timer_state;
  uint64_t timer_start_us;
  uint64_t timer_elapsed_us;
  uint32_t cmd_count;
  uint32_t err_count;
  device_action_t
      pending; /* action main() must perform AFTER the reply has been sent */
} device_t;

void device_init(device_t *d, const device_hal_t *hal);

/* Handles one complete request line (modified in place). Writes the framed
 * reply to `out` (at least PROTO_REPLY_MAX bytes) and returns its length;
 * returns 0 if no reply is due (blank line). */
size_t device_handle_line(device_t *d, char *line, char *out, size_t cap);

/* Reply for conditions detected by the transport layer (PE_TIMEOUT,
 * PE_TOO_LONG). */
size_t device_error_reply(device_t *d, proto_err_t e, char *out, size_t cap);

/* Call often from the main loop: drives the BLINK feature. */
void device_tick(device_t *d);

#endif