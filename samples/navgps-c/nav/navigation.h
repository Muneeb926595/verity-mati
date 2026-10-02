#ifndef NAVIGATION_H
#define NAVIGATION_H

#include <stdint.h>
#include "gps_types.h"

typedef enum {
    NAV_MODE_INIT = 0,
    NAV_MODE_TRACKING,
    NAV_MODE_DEGRADED,
    NAV_MODE_HOLD,
    NAV_MODE_DEAD_RECKONING
} nav_mode_t;

/* Navigation state shared by the GPS path and the navigation loop. */
typedef struct {
    nav_mode_t mode;
    double     latitude;
    double     longitude;
    double     speed_knots;
    double     target_lat;
    double     target_lon;
    uint32_t   last_fix_ms;
    int        consecutive_invalid;
    int        has_fix;
} nav_state_t;

extern double currentHeading;
extern nav_state_t g_nav;

void   navigation_init(double target_lat, double target_lon);
void   ProcessGpsData(const gps_fix_t *fix);
double CalculateHeading(double gps_heading, double speed_knots);
void   NavigationTick(uint32_t now_ms);

#endif /* NAVIGATION_H */
