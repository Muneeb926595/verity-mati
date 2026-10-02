#include <stdint.h>
#include "hal.h"
#include "nav_config.h"
#include "gps_receiver.h"
#include "navigation.h"

#define TARGET_LAT 33.7000
#define TARGET_LON 73.0600

int main(void)
{
    uint32_t last_tick = 0;

    gps_receiver_init();
    navigation_init(TARGET_LAT, TARGET_LON);
    gps_receiver_set_callback(ProcessGpsData);
    led_set_status(1);

    for (;;) {
        uint32_t now = hal_get_tick_ms();

        gps_receiver_poll();
        if ((now - last_tick) >= NAV_TICK_MS) {
            last_tick = now;
            NavigationTick(now);
        }
    }
    return 0;
}
