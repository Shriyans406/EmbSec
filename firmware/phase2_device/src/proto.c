#include "proto.h"

#include <stdio.h>
#include <string.h>

static const char *const ERR_NAMES[] = {
    "NONE",     "UNKNOWN_CMD", "BAD_ARGS", "BAD_RANGE", "BAD_CRC",
    "TOO_LONG", "BAD_FORMAT",  "TIMEOUT",  "BAD_STATE"};

const char *proto_err_name(proto_err_t e) {
  if ((unsigned)e > (unsigned)PE_BAD_STATE) {
    return "UNKNOWN";
  }
  return ERR_NAMES[(int)e];
}

uint16_t proto_crc16(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFF;
  while (len--) {
    crc ^= (uint16_t)((uint16_t)(*data++) << 8);
    for (int i = 0; i < 8; i++) {
      crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021)
                           : (uint16_t)(crc << 1);
    }
  }
  return crc;
}

static int hexval(char c) {
  if (c >= '0' && c <= '9')
    return c - '0';
  if (c >= 'A' && c <= 'F')
    return c - 'A' + 10;
  if (c >= 'a' && c <= 'f')
    return c - 'a' + 10;
  return -1;
}

proto_err_t proto_parse(char *line, proto_request_t *r) {
  memset(r, 0, sizeof(*r));
  r->cmd = "";
  r->rest = "";

  size_t n = strlen(line);
  for (size_t i = 0; i < n; i++) {
    unsigned char c = (unsigned char)line[i];
    if (c < 0x20 || c > 0x7E) {
      return PE_BAD_FORMAT;
    }
  }

  /* Optional checksum: the last 5 characters are '*' + 4 hex digits. */
  bool crc_bad = false;
  if (n >= 5 && line[n - 5] == '*') {
    int h0 = hexval(line[n - 4]);
    int h1 = hexval(line[n - 3]);
    int h2 = hexval(line[n - 2]);
    int h3 = hexval(line[n - 1]);
    if (h0 >= 0 && h1 >= 0 && h2 >= 0 && h3 >= 0) {
      uint16_t given = (uint16_t)((h0 << 12) | (h1 << 8) | (h2 << 4) | h3);
      r->has_crc = true;
      crc_bad = (given != proto_crc16((const uint8_t *)line, n - 5));
      line[n - 5] = '\0';
    }
  }

  char *p = line;
  while (*p == ' ')
    p++;
  size_t m = strlen(p);
  while (m > 0 && p[m - 1] == ' ')
    p[--m] = '\0';

  /* Optional request id: '#' + 1..5 digits, then a space or end of line. */
  bool id_bad = false;
  if (*p == '#') {
    char *q = p + 1;
    uint32_t v = 0;
    int digits = 0;
    while (*q >= '0' && *q <= '9' && digits < 6) {
      v = v * 10u + (uint32_t)(*q - '0');
      q++;
      digits++;
    }
    if (digits >= 1 && digits <= 5 && (*q == ' ' || *q == '\0')) {
      r->has_id = true;
      r->id = v;
      p = q;
      while (*p == ' ')
        p++;
    } else {
      id_bad = true;
    }
  }

  if (crc_bad)
    return PE_BAD_CRC;
  if (id_bad)
    return PE_BAD_FORMAT;
  if (*p == '\0')
    return PE_BAD_FORMAT; /* nothing but an id / checksum */

  r->cmd = p;
  while (*p && *p != ' ')
    p++;
  if (*p) {
    *p++ = '\0';
    while (*p == ' ')
      p++;
  }
  r->rest = p;
  return PE_NONE;
}

int proto_split_args(char *rest, char *argv[], int max) {
  int argc = 0;
  char *p = rest;
  while (*p) {
    while (*p == ' ')
      p++;
    if (!*p)
      break;
    if (argc < max) {
      argv[argc] = p;
    }
    argc++;
    if (argc > max) {
      return max + 1;
    }
    while (*p && *p != ' ')
      p++;
    if (*p)
      *p++ = '\0';
  }
  return argc;
}

size_t proto_format_reply(char *out, size_t cap, const proto_request_t *r,
                          const char *body) {
  char tmp[PROTO_BODY_MAX + 16];
  int n;
  if (r->has_id) {
    n = snprintf(tmp, sizeof(tmp), "#%lu %s", (unsigned long)r->id, body);
  } else {
    n = snprintf(tmp, sizeof(tmp), "%s", body);
  }
  if (n < 0) {
    out[0] = '\0';
    return 0;
  }
  if ((size_t)n >= sizeof(tmp)) {
    n = (int)sizeof(tmp) - 1;
  }
  int w;
  if (r->has_crc) {
    w = snprintf(out, cap, "%s*%04X\n", tmp,
                 (unsigned)proto_crc16((const uint8_t *)tmp, (size_t)n));
  } else {
    w = snprintf(out, cap, "%s\n", tmp);
  }
  if (w < 0) {
    out[0] = '\0';
    return 0;
  }
  if ((size_t)w >= cap) {
    w = (int)cap - 1;
  }
  return (size_t)w;
}