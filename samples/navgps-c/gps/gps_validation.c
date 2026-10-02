#include "gps_validation.h"
#include "nav_config.h"

/* Decides whether a GPS fix is trustworthy enough to steer by. */
gps_status_t gps_validate_fix(const gps_fix_t *fix)
{
    if (fix == 0 || !fix->valid) {
        return GPS_NO_FIX;
    }
    if (fix->latitude < -90.0 || fix->latitude > 90.0 ||
        fix->longitude < -180.0 || fix->longitude > 180.0) {
        return GPS_INVALID_RANGE;
    }
    if (fix->hdop > GPS_MAX_HDOP || fix->satellites < GPS_MIN_SATELLITES) {
        return GPS_LOW_QUALITY;
    }
    return GPS_OK;
}
