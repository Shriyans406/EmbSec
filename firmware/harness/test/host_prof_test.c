/*
 * Host test of the harness profiler and the PROF command (runs on a PC, not on
 * the board). Build and run from the harness folder, e.g. with MinGW:
 *   gcc -std=c11 -Wall -Wextra -Isrc test/host_prof_test.c src/device.c
 *       src/proto.c src/prof.c -o host_prof_test && ./host_prof_test
 * The clock is fake, so every number is exact.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_api.h"
#include "device.h"

static uint64_t g_now = 1000;
static uint64_t fake_us(void) { return g_now; }
static uint32_t fake_ms(void) { return (uint32_t)(g_now / 1000u); }
static void fake_led(bool on) { (void)on; }
static int32_t fake_temp(uint16_t *raw) {
  *raw = 876;
  return 27100;
}

/* the "application": APP SLOW takes 250 us inside a profiled section */
void app_init(void) {}
void app_tick(void) {}
int app_command(const char *args, char *out, size_t cap) {
  if (strcmp(args, "SLOW") == 0) {
    app_prof_begin(2, "slow_part");
    g_now += 250;
    app_prof_end(2);
    snprintf(out, cap, "done");
    return APP_OK;
  }
  snprintf(out, cap, "unknown");
  return APP_ERR_ARGS;
}

static device_hal_t hal = {fake_led, fake_ms, fake_us, fake_temp,
                           "POWER_ON_OR_RESET", "DE61C48787331A35", "1.5.1",
                           125};
static device_t dev;
static int failures = 0;

static const char *send(const char *text) {
  static char out[PROTO_REPLY_MAX];
  char line[PROTO_LINE_MAX + 1];
  snprintf(line, sizeof(line), "%s", text);
  size_t n = device_handle_line(&dev, line, out, sizeof(out));
  while (n > 0 && (out[n - 1] == '\n' || out[n - 1] == '\r'))
    out[--n] = '\0';
  return out;
}

static void expect(const char *text, const char *want) {
  const char *got = send(text);
  if (strstr(got, want) == NULL) {
    printf("FAIL  %-22s -> %s\n      wanted: %s\n", text, got, want);
    failures++;
  } else {
    printf("ok    %-22s -> %s\n", text, got);
  }
}

int main(void) {
  device_init(&dev, &hal);
  expect("VERSION", "fw=0.4.0");
  expect("PROF RESET", "OK PROF reset");
  for (int i = 0; i < 3; i++)
    send("APP SLOW");
  expect("PROF CMD APP", "cmd=APP n=3 avg_us=250 min_us=250 max_us=250");
  expect("PROF APP 2", "app=2 name=slow_part n=3 avg_us=250 min_us=250 max_us=250");
  expect("PROF APP 0", "app=0 name=- n=0 avg_us=0");
  expect("PROF APP 8", "ERR 3 BAD_RANGE");
  expect("PROF CMD NOPE", "ERR 2 BAD_ARGS");
  expect("PROF JUNK", "ERR 2 BAD_ARGS");
  expect("PROF CMD PING", "cmd=PING n=0");
  send("PING");
  expect("PROF CMD PING", "cmd=PING n=1 avg_us=0");

  /* main loop numbers: 3 passes busy 100/200/300 us, waiting 900/800/700 us */
  prof_reset(&dev.prof);
  prof_loop(&dev.prof, 1000, 900);
  prof_loop(&dev.prof, 1000, 800);
  prof_loop(&dev.prof, 1000, 700);
  prof_add(&dev.prof.app_tick, 40);
  prof_add(&dev.prof.app_tick, 60);
  g_now += 3000;
  expect("PROF", "window_ms=3 loops=3 busy_pct=20.00 loop_max_us=300 loop_avg_us=200 "
                 "tick_max_us=60 app_tick_avg_us=50");
  expect("ECHO 0123456789012345678901234567890123456789", "OK 0123");
  expect("PROF", "rx_hwm=45");

  /* a reset clears everything */
  expect("PROF RESET", "OK PROF reset");
  expect("PROF CMD APP", "n=0 avg_us=0 min_us=0 max_us=0");

  printf("%s (%d failures)\n", failures ? "FAILED" : "ALL PASSED", failures);
  return failures ? 1 : 0;
}
