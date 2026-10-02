#include <math.h>
#include "geo_math.h"

#define PI 3.14159265358979323846

/* Normalises an angle into [0, 360). */
double wrap360(double deg)
{
    double r = fmod(deg, 360.0);

    if (r < 0.0) {
        r += 360.0;
    }
    return r;
}

/* Normalises an angle difference into [-180, 180). */
double wrap180(double deg)
{
    return wrap360(deg + 180.0) - 180.0;
}

double clamp(double value, double lo, double hi)
{
    if (value < lo) return lo;
    if (value > hi) return hi;
    return value;
}

/* Initial great-circle bearing from point 1 to point 2, degrees true. */
double bearing_deg(double lat1, double lon1, double lat2, double lon2)
{
    double p1 = lat1 * PI / 180.0;
    double p2 = lat2 * PI / 180.0;
    double dl = (lon2 - lon1) * PI / 180.0;
    double y = sin(dl) * cos(p2);
    double x = cos(p1) * sin(p2) - sin(p1) * cos(p2) * cos(dl);

    return wrap360(atan2(y, x) * 180.0 / PI);
}
