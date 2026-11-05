/*
 * AEISP wire protocol, version 1 - line parsing, checksum, reply framing.
 * Pure C, no hardware access, so it can be unit-tested on a PC.
 *
 * Request line  :  [#<id> ]<COMMAND>[ <args>][*<CRC16>]        (ends with '\n' on the wire)
 * Reply line    :  [#<id> ]OK <payload>[*<CRC16>]
 *               or [#<id> ]ERR <code> <NAME>[ <detail>][*<CRC16>]
 *
 *  - <id>    : '#' + 1..5 digits. If the request has one, the reply repeats it.
 *  - <CRC16> : '*' + exactly 4 hex digits = CRC-16/CCITT-FALSE of every byte before the '*'.
 *              If the request has one it must be correct, and the reply carries one too.
 *  - Printable ASCII only (0x20..0x7E). Maximum request length PROTO_LINE_MAX bytes.
 */
#ifndef PROTO_H
#define PROTO_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define PROTO_VERSION    1
#define PROTO_LINE_MAX   128   /* longest accepted request line, not counting the newline */
#define PROTO_BODY_MAX   192   /* longest reply text before id/checksum are added          */
#define PROTO_REPLY_MAX  256   /* buffer size callers must provide for a framed reply      */

typedef enum {
    PE_NONE        = 0,
    PE_UNKNOWN_CMD = 1,  /* command word not recognised                      */
    PE_BAD_ARGS    = 2,  /* missing, extra or non-numeric arguments          */
    PE_BAD_RANGE   = 3,  /* number understood but outside the allowed range  */
    PE_BAD_CRC     = 4,  /* checksum present but wrong                       */
    PE_TOO_LONG    = 5,  /* line longer than PROTO_LINE_MAX                  */
    PE_BAD_FORMAT  = 6,  /* non-printable bytes, bad #id, or no command      */
    PE_TIMEOUT     = 7,  /* partial line abandoned after idle timeout        */
    PE_BAD_STATE   = 8   /* command not allowed in the current state         */
} proto_err_t;

typedef struct {
    bool     has_id;
    uint32_t id;
    bool     has_crc;
    char    *cmd;    /* command word, NUL-terminated (points into the line buffer)    */
    char    *rest;   /* everything after the command word (may be ""), same buffer    */
} proto_request_t;

const char *proto_err_name(proto_err_t e);

/* CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no final xor. "123456789" -> 0x29B1 */
uint16_t proto_crc16(const uint8_t *data, size_t len);

/* Parses `line` in place (NUL-terminated, no newline). Returns PE_NONE or the error to report.
 * Even when an error is returned, r->has_id / r->has_crc are set if they could be read,
 * so the error reply can mirror them. */
proto_err_t proto_parse(char *line, proto_request_t *r);

/* Splits `rest` in place into space-separated words. Returns the number of words found,
 * capped at max+1 (so a caller that wants exactly N words can test for != N). */
int proto_split_args(char *rest, char *argv[], int max);

/* Builds "[#id ]body[*CRC]\n" into out. Returns bytes written (excluding the final NUL). */
size_t proto_format_reply(char *out, size_t cap, const proto_request_t *r, const char *body);

#endif