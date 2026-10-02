#ifndef NAV_CONFIG_H
#define NAV_CONFIG_H

#define GPS_MAX_HDOP            2.5f     /* reject fixes with worse horizontal precision */
#define GPS_MIN_SATELLITES      4        /* minimum satellites for a usable fix */
#define GPS_TIMEOUT_MS          2000u    /* no fix for this long = GPS lost */
#define NAV_TICK_MS             100u     /* navigation loop period */
#define MIN_SPEED_FOR_HEADING   2.0      /* knots; below this the GPS course is noise */
#define HEADING_FILTER_ALPHA    0.25     /* exponential smoothing factor for the heading */
#define MAX_TURN_RATE_DEG       5.0      /* max steering change per navigation tick */
#define MAX_INVALID_FIXES       5        /* consecutive bad fixes before degraded mode */

/* Optional feature, not defined by default:
 * #define ENABLE_DEAD_RECKONING  keeps steering on the last heading when GPS is lost */

#endif /* NAV_CONFIG_H */
