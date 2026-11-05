/* Default (empty) application. Any function defined in the project's app/ code
 * replaces these. */
#include <stdio.h>

#include "app_api.h"

__attribute__((weak)) void app_init(void) {}

__attribute__((weak)) void app_tick(void) {}

__attribute__((weak)) int app_command(const char *args, char *out, size_t cap) {
  (void)args;
  snprintf(out, cap, "no app installed");
  return APP_ERR_ARGS;
}