#include <stdio.h>
#include <string.h>
#include "app_api.h"

int app_command(const char *args, char *out, size_t cap) {
    if (strcmp(args, "HELLO") == 0) { snprintf(out, cap, "hello from the API"); return APP_OK; }
    snprintf(out, cap, "unknown");
    return APP_ERR_ARGS;
}
