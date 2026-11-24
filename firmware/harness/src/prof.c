#include "prof.h"

#include <string.h>

#include "app_api.h"

prof_t *prof_global = 0;

static void stat_clear(prof_stat_t *s) {
  s->n = 0;
  s->total_us = 0;
  s->min_us = 0;
  s->max_us = 0;
}

void prof_init(prof_t *p, uint64_t (*now_us)(void)) {
  memset(p, 0, sizeof(*p));
  p->now_us = now_us;
  p->window_start_us = now_us ? now_us() : 0;
}

void prof_reset(prof_t *p) {
  for (int i = 0; i < PROF_CMD_SLOTS; i++)
    stat_clear(&p->cmd[i]);
  for (int i = 0; i < PROF_APP_SLOTS; i++)
    stat_clear(&p->app[i]);
  stat_clear(&p->dev_tick);
  stat_clear(&p->app_tick);
  stat_clear(&p->loop);
  p->busy_us = 0;
  p->wait_us = 0;
  p->rx_hwm = 0;
  p->reply_hwm = 0;
  p->window_start_us = p->now_us ? p->now_us() : 0;
}

void prof_add(prof_stat_t *s, uint64_t us) {
  uint32_t v = us > 0xFFFFFFFFu ? 0xFFFFFFFFu : (uint32_t)us;
  if (s->n == 0 || v < s->min_us)
    s->min_us = v;
  if (v > s->max_us)
    s->max_us = v;
  s->total_us += us;
  if (s->n < 0xFFFFFFFFu)
    s->n++;
}

uint32_t prof_avg(const prof_stat_t *s) {
  return s->n ? (uint32_t)(s->total_us / s->n) : 0;
}

void prof_loop(prof_t *p, uint64_t loop_us, uint64_t wait_us) {
  uint64_t busy = loop_us > wait_us ? loop_us - wait_us : 0;
  prof_add(&p->loop, busy);
  p->busy_us += busy;
  p->wait_us += wait_us;
}

void prof_rx(prof_t *p, size_t line_len) {
  if (line_len > p->rx_hwm)
    p->rx_hwm = (uint32_t)line_len;
}

void prof_reply(prof_t *p, size_t reply_len) {
  if (reply_len > p->reply_hwm)
    p->reply_hwm = (uint32_t)reply_len;
}

uint32_t prof_busy_x100(const prof_t *p) {
  uint64_t all = p->busy_us + p->wait_us;
  if (all == 0)
    return 0;
  return (uint32_t)((p->busy_us * 10000u) / all);
}

/* ---------- the application's own sections (app_api.h) ---------- */

void app_prof_begin(int slot, const char *name) {
  prof_t *p = prof_global;
  if (!p || !p->now_us || slot < 0 || slot >= PROF_APP_SLOTS)
    return;
  p->app_name[slot] = name;
  p->app_start[slot] = p->now_us();
  p->app_open[slot] = 1;
}

void app_prof_end(int slot) {
  prof_t *p = prof_global;
  if (!p || !p->now_us || slot < 0 || slot >= PROF_APP_SLOTS ||
      !p->app_open[slot])
    return;
  prof_add(&p->app[slot], p->now_us() - p->app_start[slot]);
  p->app_open[slot] = 0;
}
