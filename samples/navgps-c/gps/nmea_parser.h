#ifndef NMEA_PARSER_H
#define NMEA_PARSER_H

#include "gps_types.h"

#define NMEA_MAX_FIELDS 20
#define NMEA_MAX_LEN    96

/* One NMEA 0183 sentence split into its comma-separated fields. */
typedef struct {
    char type[6];                        /* e.g. "GPRMC" */
    int  field_count;
    char fields[NMEA_MAX_FIELDS][16];
} nmea_sentence_t;

int    nmea_checksum_ok(const char *line);
int    nmea_split(const char *line, nmea_sentence_t *out);
double nmea_parse_coord(const char *value, const char *hemisphere);

#endif /* NMEA_PARSER_H */
