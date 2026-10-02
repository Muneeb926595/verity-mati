#ifndef VR_MATH_H
#define VR_MATH_H
double sin(double); double cos(double); double tan(double); double asin(double); double acos(double); double atan(double);
double atan2(double, double); double sqrt(double); double fabs(double); double fmod(double, double); double floor(double);
double ceil(double); double round(double); double pow(double, double); double exp(double); double log(double); double log10(double);
float sinf(float); float cosf(float); float sqrtf(float); float fabsf(float); float atan2f(float, float);
#define M_PI 3.14159265358979323846
#define NAN (__builtin_nanf(""))
#define INFINITY (__builtin_inff())
#define isnan(x) __builtin_isnan(x)
#endif
