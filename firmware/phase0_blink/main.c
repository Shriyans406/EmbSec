#include "pico/stdlib.h"
#include <stdio.h>

int main(void) {
    stdio_init_all();
    const uint LED = 25; /* Pico default; Shrike Lite pin verified in Phase 1 */
    gpio_init(LED);
    gpio_set_dir(LED, GPIO_OUT);
    while (true) {
        gpio_put(LED, 1); sleep_ms(250);
        gpio_put(LED, 0); sleep_ms(250);
        printf("tick\n");
    }
}
