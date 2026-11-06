/* Application code for project "demo".
 * The harness firmware calls app_init() once, app_tick() about every millisecond (keep it short!)
 * and app_command() for every request line of the form  APP <args>.
 * See firmware/harness/src/app_api.h for the rules.
 */
#include <stdio.h>
#include <string.h>

#include "app_api.h"

void app_init(void) {
}

void app_tick(void) {
}

int app_command(const char *args, char *out, size_t cap) {
    if (strcmp(args, "HELLO") == 0) {
        snprintf(out, cap, "hello from demo");
        return APP_OK;
    }
    snprintf(out, cap, "unknown app command");
    return APP_ERR_ARGS;
}
