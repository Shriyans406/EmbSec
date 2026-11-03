/*
 * AEISP Phase 1 - echo / handshake firmware (throwaway test firmware).
 *
 * Line protocol over USB serial. Host sends one command per line ("\n" ends it).
 * Firmware answers with exactly one line per command.
 *
 *   HELLO        -> OK HELLO AEISP-ECHO 0.1.0 <board-unique-id>
 *   PING         -> OK PONG
 *   ECHO <text>  -> OK <text>
 *   UPTIME       -> OK UPTIME <milliseconds-since-boot>
 *   BOOTSEL      -> OK BOOTSEL   (then the board reboots into the UF2 bootloader)
 *   anything else-> ERR UNKNOWN
 *   ECHO (no text) -> ERR USAGE
 *   line > 128 chars -> ERR TOOLONG
 */
#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "pico/unique_id.h"
#include "pico/bootrom.h"

#define FW_NAME      "AEISP-ECHO"
#define FW_VERSION   "0.1.0"
#define LINE_MAX_LEN 128

static char g_uid[2 * PICO_UNIQUE_BOARD_ID_SIZE_BYTES + 1];

static void reply(const char *text) {
    printf("%s\n", text);
    fflush(stdout);
}

static void handle_line(const char *line) {
    if (line[0] == '\0') {
        return; /* ignore empty lines, no reply */
    }
    if (strcmp(line, "HELLO") == 0) {
        printf("OK HELLO %s %s %s\n", FW_NAME, FW_VERSION, g_uid);
        fflush(stdout);
    } else if (strcmp(line, "PING") == 0) {
        reply("OK PONG");
    } else if (strcmp(line, "UPTIME") == 0) {
        printf("OK UPTIME %lu\n", (unsigned long)to_ms_since_boot(get_absolute_time()));
        fflush(stdout);
    } else if (strcmp(line, "ECHO") == 0) {
        reply("ERR USAGE");
    } else if (strncmp(line, "ECHO ", 5) == 0) {
        printf("OK %s\n", line + 5);
        fflush(stdout);
    } else if (strcmp(line, "BOOTSEL") == 0) {
        reply("OK BOOTSEL");
        sleep_ms(100);
        reset_usb_boot(0, 0);
    } else {
        reply("ERR UNKNOWN");
    }
}

int main(void) {
    stdio_init_all();
    pico_get_unique_board_id_string(g_uid, sizeof(g_uid));

    char line[LINE_MAX_LEN + 1];
    size_t len = 0;
    bool overflow = false;

    while (true) {
        int c = getchar_timeout_us(1000);
        if (c == PICO_ERROR_TIMEOUT || c == '\r') {
            continue;
        }
        if (c == '\n') {
            if (overflow) {
                reply("ERR TOOLONG");
            } else {
                line[len] = '\0';
                handle_line(line);
            }
            len = 0;
            overflow = false;
            continue;
        }
        if (overflow) {
            continue;
        }
        if (len < LINE_MAX_LEN) {
            line[len++] = (char)c;
        } else {
            overflow = true;
        }
    }
}