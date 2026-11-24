/*
 * AEISP on-device profiler. Pure C (no SDK calls), so it is unit-tested on a PC.
 *
 * What it measures, with the microsecond clock given to prof_init():
 *   - every protocol command: count, average / min / max execution time
 *   - the main loop: device_tick and app_tick time, the longest loop pass (the
 *     watchdog allows 3 000 000 us), busy time vs. time spent waiting for input
 *   - buffer use: the longest request line and the longest reply so far
 *   - up to PROF_APP_SLOTS sections of application code, marked by the app with
 *     app_prof_begin(slot, "name") / app_prof_end(slot)
 *
 * The RP2040 has no CPU-load counter; "busy" is measured here: time in the loop
 * minus time spent blocked waiting for a character.
 */
#ifndef PROF_H
#define PROF_H

#include <stddef.h>
#include <stdint.h>

#define PROF_CMD_SLOTS 18 /* one per command, the last one for unknown */
#define PROF_APP_SLOTS 8

typedef struct {
  uint32_t n;
  uint64_t total_us;
  uint32_t min_us;
  uint32_t max_us;
} prof_stat_t;

typedef struct {
  uint64_t (*now_us)(void);
  uint64_t window_start_us;
  prof_stat_t cmd[PROF_CMD_SLOTS];
  prof_stat_t dev_tick;
  prof_stat_t app_tick;
  prof_stat_t loop; /* one main-loop pass, without the input wait */
  uint64_t busy_us;
  uint64_t wait_us;
  uint32_t rx_hwm;
  uint32_t reply_hwm;
  prof_stat_t app[PROF_APP_SLOTS];
  const char *app_name[PROF_APP_SLOTS];
  uint64_t app_start[PROF_APP_SLOTS];
  uint8_t app_open[PROF_APP_SLOTS];
} prof_t;

void prof_init(prof_t *p, uint64_t (*now_us)(void));
void prof_reset(prof_t *p); /* clears every number, keeps the clock and names */
void prof_add(prof_stat_t *s, uint64_t us);
uint32_t prof_avg(const prof_stat_t *s);

/* main loop helpers */
void prof_loop(prof_t *p, uint64_t loop_us, uint64_t wait_us);
void prof_rx(prof_t *p, size_t line_len);
void prof_reply(prof_t *p, size_t reply_len);

/* busy share of the window in hundredths of a percent (0..10000) */
uint32_t prof_busy_x100(const prof_t *p);

/* the instance app_prof_begin / app_prof_end use (set by device_init) */
extern prof_t *prof_global;

#endif
