#include "device.h"

#include <stdio.h>
#include <string.h>

typedef proto_err_t (*cmd_fn)(device_t *d, proto_request_t *r, char *out,
                              size_t cap);

/* ---------- small helpers ---------- */

static proto_err_t fail(char *out, size_t cap, proto_err_t e,
                        const char *detail) {
  snprintf(out, cap, "%s", detail);
  return e;
}

static void fmt_u64(char *dst, uint64_t v) { /* dst: at least 21 bytes */
  char tmp[21];
  int i = 0;
  if (v == 0) {
    tmp[i++] = '0';
  }
  while (v > 0) {
    tmp[i++] = (char)('0' + (int)(v % 10u));
    v /= 10u;
  }
  int j = 0;
  while (i > 0) {
    dst[j++] = tmp[--i];
  }
  dst[j] = '\0';
}

/* 0 = ok, 1 = not a number, 2 = negative or too large */
static int parse_u32(const char *s, uint32_t *out) {
  bool neg = false;
  if (*s == '-') {
    neg = true;
    s++;
  }
  if (*s == '\0') {
    return 1;
  }
  uint32_t v = 0;
  int digits = 0;
  for (; *s; s++) {
    if (*s < '0' || *s > '9') {
      return 1;
    }
    if (digits < 9) {
      v = v * 10u + (uint32_t)(*s - '0');
    }
    digits++;
  }
  if (neg || digits > 9) {
    return 2;
  }
  *out = v;
  return 0;
}

static void led_set(device_t *d, bool on) {
  d->led_on = on;
  d->hal->led_write(on);
}

static const char *timer_name(timer_state_t s) {
  switch (s) {
  case TIMER_RUNNING:
    return "RUNNING";
  case TIMER_STOPPED:
    return "STOPPED";
  default:
    return "IDLE";
  }
}

/* ---------- command handlers: on success write the text after "OK ", on error
 * the detail ---------- */

static proto_err_t h_hello(device_t *d, proto_request_t *r, char *out,
                           size_t cap) {
  if (r->rest[0])
    return fail(out, cap, PE_BAD_ARGS, "HELLO takes no arguments");
  snprintf(out, cap, "HELLO %s %s %s", FW_NAME, FW_VERSION, d->hal->uid);
  return PE_NONE;
}

static proto_err_t h_ping(device_t *d, proto_request_t *r, char *out,
                          size_t cap) {
  (void)d;
  if (r->rest[0])
    return fail(out, cap, PE_BAD_ARGS, "PING takes no arguments");
  snprintf(out, cap, "PONG");
  return PE_NONE;
}

static proto_err_t h_echo(device_t *d, proto_request_t *r, char *out,
                          size_t cap) {
  (void)d;
  if (!r->rest[0])
    return fail(out, cap, PE_BAD_ARGS, "usage: ECHO <text>");
  snprintf(out, cap, "%s", r->rest);
  return PE_NONE;
}

static proto_err_t h_uptime(device_t *d, proto_request_t *r, char *out,
                            size_t cap) {
  if (r->rest[0])
    return fail(out, cap, PE_BAD_ARGS, "UPTIME takes no arguments");
  snprintf(out, cap, "UPTIME %lu", (unsigned long)d->hal->uptime_ms());
  return PE_NONE;
}

static proto_err_t h_version(device_t *d, proto_request_t *r, char *out,
                             size_t cap) {
  if (r->rest[0])
    return fail(out, cap, PE_BAD_ARGS, "VERSION takes no arguments");
  snprintf(out, cap, "VERSION fw=%s proto=%d sdk=%s", FW_VERSION, PROTO_VERSION,
           d->hal->sdk_version);
  return PE_NONE;
}

static proto_err_t h_info(device_t *d, proto_request_t *r, char *out,
                          size_t cap) {
  if (r->rest[0])
    return fail(out, cap, PE_BAD_ARGS, "INFO takes no arguments");
  snprintf(out, cap,
           "INFO name=%s fw=%s proto=%d uid=%s board=shrike-lite mcu=rp2040 "
           "clk_mhz=%lu",
           FW_NAME, FW_VERSION, PROTO_VERSION, d->hal->uid,
           (unsigned long)d->hal->sys_clock_mhz);
  return PE_NONE;
}

static proto_err_t h_status(device_t *d, proto_request_t *r, char *out,
                            size_t cap) {
  if (r->rest[0])
    return fail(out, cap, PE_BAD_ARGS, "STATUS takes no arguments");
  snprintf(
      out, cap,
      "STATUS uptime_ms=%lu led=%s blink_ms=%lu timer=%s cmds=%lu errors=%lu",
      (unsigned long)d->hal->uptime_ms(), d->led_on ? "ON" : "OFF",
      (unsigned long)d->blink_ms, timer_name(d->timer_state),
      (unsigned long)d->cmd_count, (unsigned long)d->err_count);
  return PE_NONE;
}

static proto_err_t h_led(device_t *d, proto_request_t *r, char *out,
                         size_t cap) {
  char *argv[1];
  if (proto_split_args(r->rest, argv, 1) != 1) {
    return fail(out, cap, PE_BAD_ARGS, "usage: LED ON|OFF|TOGGLE|GET");
  }
  if (strcmp(argv[0], "ON") == 0) {
    d->blink_ms = 0;
    led_set(d, true);
  } else if (strcmp(argv[0], "OFF") == 0) {
    d->blink_ms = 0;
    led_set(d, false);
  } else if (strcmp(argv[0], "TOGGLE") == 0) {
    d->blink_ms = 0;
    led_set(d, !d->led_on);
  } else if (strcmp(argv[0], "GET") != 0) {
    return fail(out, cap, PE_BAD_ARGS, "usage: LED ON|OFF|TOGGLE|GET");
  }
  snprintf(out, cap, "LED state=%s", d->led_on ? "ON" : "OFF");
  return PE_NONE;
}

static proto_err_t h_blink(device_t *d, proto_request_t *r, char *out,
                           size_t cap) {
  char *argv[1];
  uint32_t ms = 0;
  if (proto_split_args(r->rest, argv, 1) != 1) {
    return fail(out, cap, PE_BAD_ARGS,
                "usage: BLINK <ms>  (0 = stop, 50..10000)");
  }
  int st = parse_u32(argv[0], &ms);
  if (st == 1)
    return fail(out, cap, PE_BAD_ARGS, "interval must be a whole number");
  if (st == 2 || (ms != 0 && (ms < BLINK_MIN_MS || ms > BLINK_MAX_MS))) {
    return fail(out, cap, PE_BAD_RANGE, "interval must be 0 or 50..10000 ms");
  }
  d->blink_ms = ms;
  if (ms > 0) {
    d->blink_last_ms = d->hal->uptime_ms();
    led_set(d, true);
  }
  snprintf(out, cap, "BLINK ms=%lu", (unsigned long)ms);
  return PE_NONE;
}

static proto_err_t h_timer(device_t *d, proto_request_t *r, char *out,
                           size_t cap) {
  char *argv[1];
  char num[21];
  if (proto_split_args(r->rest, argv, 1) != 1) {
    return fail(out, cap, PE_BAD_ARGS, "usage: TIMER START|STOP|READ|RESET");
  }
  if (strcmp(argv[0], "START") == 0) {
    if (d->timer_state == TIMER_RUNNING)
      return fail(out, cap, PE_BAD_STATE, "timer is already running");
    d->timer_start_us = d->hal->time_us();
    d->timer_elapsed_us = 0;
    d->timer_state = TIMER_RUNNING;
    snprintf(out, cap, "TIMER state=RUNNING");
  } else if (strcmp(argv[0], "STOP") == 0) {
    if (d->timer_state != TIMER_RUNNING)
      return fail(out, cap, PE_BAD_STATE, "timer is not running");
    d->timer_elapsed_us = d->hal->time_us() - d->timer_start_us;
    d->timer_state = TIMER_STOPPED;
    fmt_u64(num, d->timer_elapsed_us);
    snprintf(out, cap, "TIMER state=STOPPED elapsed_us=%s", num);
  } else if (strcmp(argv[0], "READ") == 0) {
    uint64_t e = d->timer_elapsed_us;
    if (d->timer_state == TIMER_RUNNING) {
      e = d->hal->time_us() - d->timer_start_us;
    }
    fmt_u64(num, e);
    snprintf(out, cap, "TIMER state=%s elapsed_us=%s",
             timer_name(d->timer_state), num);
  } else if (strcmp(argv[0], "RESET") == 0) {
    d->timer_state = TIMER_IDLE;
    d->timer_elapsed_us = 0;
    snprintf(out, cap, "TIMER state=IDLE elapsed_us=0");
  } else {
    return fail(out, cap, PE_BAD_ARGS, "usage: TIMER START|STOP|READ|RESET");
  }
  return PE_NONE;
}

static proto_err_t h_temp(device_t *d, proto_request_t *r, char *out,
                          size_t cap) {
  uint16_t raw = 0;
  if (r->rest[0])
    return fail(out, cap, PE_BAD_ARGS, "TEMP takes no arguments");
  int32_t mc = d->hal->temp_milli_c(&raw);
  uint32_t a = (mc < 0) ? (uint32_t)(-mc) : (uint32_t)mc;
  snprintf(out, cap, "TEMP celsius=%s%lu.%lu raw=%u", mc < 0 ? "-" : "",
           (unsigned long)(a / 1000u), (unsigned long)((a % 1000u) / 100u),
           (unsigned)raw);
  return PE_NONE;
}

static proto_err_t h_resetinfo(device_t *d, proto_request_t *r, char *out,
                               size_t cap) {
  if (r->rest[0])
    return fail(out, cap, PE_BAD_ARGS, "RESETINFO takes no arguments");
  snprintf(out, cap, "RESETINFO reason=%s", d->hal->reset_reason);
  return PE_NONE;
}

static proto_err_t h_reboot(device_t *d, proto_request_t *r, char *out,
                            size_t cap) {
  if (r->rest[0])
    return fail(out, cap, PE_BAD_ARGS, "REBOOT takes no arguments");
  d->pending = ACT_REBOOT;
  snprintf(out, cap, "REBOOT");
  return PE_NONE;
}

static proto_err_t h_bootsel(device_t *d, proto_request_t *r, char *out,
                             size_t cap) {
  if (r->rest[0])
    return fail(out, cap, PE_BAD_ARGS, "BOOTSEL takes no arguments");
  d->pending = ACT_BOOTSEL;
  snprintf(out, cap, "BOOTSEL");
  return PE_NONE;
}

static proto_err_t h_hang(device_t *d, proto_request_t *r, char *out,
                          size_t cap) {
  if (r->rest[0])
    return fail(out, cap, PE_BAD_ARGS, "HANG takes no arguments");
  d->pending = ACT_HANG;
  snprintf(out, cap, "HANG");
  return PE_NONE;
}

static const struct {
  const char *name;
  cmd_fn fn;
} COMMANDS[] = {
    {"HELLO", h_hello},   {"PING", h_ping},       {"ECHO", h_echo},
    {"UPTIME", h_uptime}, {"VERSION", h_version}, {"INFO", h_info},
    {"STATUS", h_status}, {"LED", h_led},         {"BLINK", h_blink},
    {"TIMER", h_timer},   {"TEMP", h_temp},       {"RESETINFO", h_resetinfo},
    {"REBOOT", h_reboot}, {"BOOTSEL", h_bootsel}, {"HANG", h_hang},
};

/* ---------- public API ---------- */

void device_init(device_t *d, const device_hal_t *hal) {
  memset(d, 0, sizeof(*d));
  d->hal = hal;
  d->timer_state = TIMER_IDLE;
  led_set(d, false);
}

size_t device_handle_line(device_t *d, char *line, char *out, size_t cap) {
  const char *s = line;
  while (*s == ' ')
    s++;
  if (*s == '\0') {
    return 0; /* blank line: ignored, no reply */
  }
  d->cmd_count++;

  proto_request_t r;
  char detail[PROTO_BODY_MAX];
  detail[0] = '\0';
  proto_err_t e = proto_parse(line, &r);
  if (e == PE_NONE) {
    e = PE_UNKNOWN_CMD;
    for (size_t i = 0; i < sizeof(COMMANDS) / sizeof(COMMANDS[0]); i++) {
      if (strcmp(r.cmd, COMMANDS[i].name) == 0) {
        e = COMMANDS[i].fn(d, &r, detail, sizeof(detail));
        break;
      }
    }
  }

  char body[PROTO_BODY_MAX + 32];
  if (e == PE_NONE) {
    snprintf(body, sizeof(body), "OK %s", detail);
  } else {
    d->err_count++;
    if (detail[0]) {
      snprintf(body, sizeof(body), "ERR %d %s %s", (int)e, proto_err_name(e),
               detail);
    } else {
      snprintf(body, sizeof(body), "ERR %d %s", (int)e, proto_err_name(e));
    }
  }
  return proto_format_reply(out, cap, &r, body);
}

size_t device_error_reply(device_t *d, proto_err_t e, char *out, size_t cap) {
  proto_request_t r;
  char body[48];
  memset(&r, 0, sizeof(r));
  d->err_count++;
  snprintf(body, sizeof(body), "ERR %d %s", (int)e, proto_err_name(e));
  return proto_format_reply(out, cap, &r, body);
}

void device_tick(device_t *d) {
  if (d->blink_ms == 0) {
    return;
  }
  uint32_t now = d->hal->uptime_ms();
  if ((uint32_t)(now - d->blink_last_ms) >= d->blink_ms) {
    d->blink_last_ms = now;
    led_set(d, !d->led_on);
  }
}