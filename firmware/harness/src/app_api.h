/*
 * AEISP app interface (version 1).
 *
 * The harness firmware owns the USB protocol, watchdog, reset handling and
 * board safety. Generated application code lives in the project's app/ folder
 * and plugs in through the three functions below. Define any of them in your
 * own .c file; anything you leave out falls back to a harmless default (see
 * app_default.c).
 *
 *   app_init()     called once at start-up, after the hardware is ready.
 *   app_tick()     called every main-loop pass (about once per millisecond).
 * Must return quickly: if it blocks for 3 seconds the watchdog resets the
 * board. app_command()  called for the request line  "APP <args>".  Write the
 * reply text (what follows "OK " on success, or a short explanation on failure)
 * into `out`, at most `cap` bytes, and return APP_OK or one of the APP_ERR_*
 * codes.
 *
 * Rules for application code: do not use GPIO 12 or 13 (FPGA power / enable on
 * the Shrike Lite); GPIO 4 is the user LED, which the harness also drives.
 */
#ifndef APP_API_H
#define APP_API_H

#include <stddef.h>
#include <stdint.h>

#define APP_API_VERSION 1

#define APP_OK 0
#define APP_ERR_ARGS                                                           \
  2 /* bad or missing arguments          -> "ERR 2 BAD_ARGS"  */
#define APP_ERR_RANGE                                                          \
  3 /* number outside the allowed range  -> "ERR 3 BAD_RANGE" */
#define APP_ERR_STATE                                                          \
  8 /* not allowed right now             -> "ERR 8 BAD_STATE" */

void app_init(void);
void app_tick(void);
int app_command(const char *args, char *out, size_t cap);

#endif