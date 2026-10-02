#ifndef GPS_TYPES_H
#define GPS_TYPES_H

#include <stdint.h>

/* One decoded GPS position/velocity fix. */
typedef struct {
    double   latitude;      /* degrees, +N */
    double   longitude;     /* degrees, +E */
    double   speed_knots;   /* speed over ground, knots */
    double   heading_deg;   /* course over ground, degrees true [0,360) */
    float    hdop;          /* horizontal dilution of precision */
    uint8_t  satellites;    /* satellites used in the fix */
    int      valid;         /* 1 if the receiver reported an active fix ('A') */
    uint32_t timestamp_ms;  /* local receive time */
} gps_fix_t;

typedef enum {
    GPS_OK = 0,
    GPS_NO_FIX,
    GPS_INVALID_RANGE,
    GPS_LOW_QUALITY
} gps_status_t;

typedef struct {
    uint32_t sentences;
    uint32_t checksum_errors;
    uint32_t unknown_sentences;
} gps_stats_t;

/* Called for every decoded GPS fix. */
typedef void (*gps_fix_callback_t)(const gps_fix_t *fix);

#endif /* GPS_TYPES_H */
