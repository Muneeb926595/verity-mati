#include <stdlib.h>
#include <string.h>
#include "nmea_parser.h"

static int hex_value(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* Validates the "*hh" checksum: XOR of every character between '$' and '*'. */
int nmea_checksum_ok(const char *line)
{
    unsigned char sum = 0;
    const char *p = line;

    if (*p != '$') {
        return 0;
    }
    p++;
    while (*p && *p != '*') {
        sum ^= (unsigned char)*p;
        p++;
    }
    if (*p != '*' || hex_value(p[1]) < 0 || hex_value(p[2]) < 0) {
        return 0;
    }
    return sum == (unsigned char)((hex_value(p[1]) << 4) | hex_value(p[2]));
}

/* Splits "$GPRMC,a,b,...*hh" into the sentence type and its comma-separated fields. */
int nmea_split(const char *line, nmea_sentence_t *out)
{
    int field = -1;              /* -1 while still reading the sentence type */
    int pos = 0;
    const char *p = line + 1;    /* skip '$' */

    memset(out, 0, sizeof(*out));
    while (*p && *p != '*') {
        if (*p == ',') {
            field++;
            pos = 0;
            if (field >= NMEA_MAX_FIELDS) {
                return -1;
            }
        } else if (field < 0) {
            if (pos < (int)sizeof(out->type) - 1) {
                out->type[pos++] = *p;
            }
        } else if (pos < 15) {
            out->fields[field][pos++] = *p;
        }
        p++;
    }
    out->field_count = field + 1;
    return 0;
}

/* Converts NMEA "ddmm.mmmm" plus hemisphere into signed decimal degrees. */
double nmea_parse_coord(const char *value, const char *hemisphere)
{
    double raw = atof(value);
    int degrees = (int)(raw / 100.0);
    double minutes = raw - (double)degrees * 100.0;
    double result = (double)degrees + minutes / 60.0;

    if (hemisphere[0] == 'S' || hemisphere[0] == 'W') {
        result = -result;
    }
    return result;
}
