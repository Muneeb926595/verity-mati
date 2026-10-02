#ifndef GEO_MATH_H
#define GEO_MATH_H

double wrap360(double deg);
double wrap180(double deg);
double clamp(double value, double lo, double hi);
double bearing_deg(double lat1, double lon1, double lat2, double lon2);

#endif /* GEO_MATH_H */
