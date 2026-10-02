#include "navigation.h"
#include "nav_config.h"
#include "geo_math.h"
#include "gps_validation.h"
#include "vehicle_controller.h"

double currentHeading = 0.0;    /* filtered vehicle heading, degrees true [0, 360) */
nav_state_t g_nav;

void navigation_init(double target_lat, double target_lon)
{
    g_nav.mode = NAV_MODE_INIT;
    g_nav.target_lat = target_lat;
    g_nav.target_lon = target_lon;
    g_nav.last_fix_ms = 0;
    g_nav.consecutive_invalid = 0;
    g_nav.has_fix = 0;
    currentHeading = 0.0;
}

/* Counts consecutive bad fixes; too many switches navigation to degraded mode. */
static void nav_handle_invalid_fix(gps_status_t status)
{
    g_nav.consecutive_invalid++;
    if (g_nav.consecutive_invalid >= MAX_INVALID_FIXES) {
        g_nav.mode = NAV_MODE_DEGRADED;
    }
    (void)status;
}

/* Smooths the GPS course over ground into the vehicle heading.
 * Below MIN_SPEED_FOR_HEADING the GPS course is unreliable, so the previous heading is kept. */
double CalculateHeading(double gps_heading, double speed_knots)
{
    double delta;

    if (speed_knots < MIN_SPEED_FOR_HEADING) {
        return currentHeading;
    }
    delta = wrap180(gps_heading - currentHeading);
    return wrap360(currentHeading + HEADING_FILTER_ALPHA * delta);
}

/* Entry point for every decoded GPS fix (registered as the GPS receiver callback in main). */
void ProcessGpsData(const gps_fix_t *fix)
{
    gps_status_t status = gps_validate_fix(fix);

    if (status != GPS_OK) {
        nav_handle_invalid_fix(status);
        return;
    }
    g_nav.consecutive_invalid = 0;
    g_nav.latitude    = fix->latitude;
    g_nav.longitude   = fix->longitude;
    g_nav.speed_knots = fix->speed_knots;
    g_nav.last_fix_ms = fix->timestamp_ms;
    g_nav.has_fix     = 1;
    currentHeading = CalculateHeading(fix->heading_deg, fix->speed_knots);
    g_nav.mode = NAV_MODE_TRACKING;
}

/* Periodic navigation step (every NAV_TICK_MS): detects GPS loss and steers toward the target. */
void NavigationTick(uint32_t now_ms)
{
    double target_heading;

    if (g_nav.has_fix && (now_ms - g_nav.last_fix_ms) > GPS_TIMEOUT_MS) {
#ifdef ENABLE_DEAD_RECKONING
        g_nav.mode = NAV_MODE_DEAD_RECKONING;
#else
        g_nav.mode = NAV_MODE_HOLD;
        VehicleController_Hold();
        return;
#endif
    }
    if (g_nav.mode != NAV_MODE_TRACKING && g_nav.mode != NAV_MODE_DEAD_RECKONING) {
        return;
    }
    target_heading = bearing_deg(g_nav.latitude, g_nav.longitude, g_nav.target_lat, g_nav.target_lon);
    VehicleController_SetDirection(currentHeading, target_heading);
}
