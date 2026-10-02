#include <string.h>
#include <stdlib.h>
#include "gps_receiver.h"
#include "nmea_parser.h"
#include "ring_buffer.h"
#include "hal.h"

extern ring_buffer_t g_gps_rx;           /* filled by USART2_IRQHandler */

gps_stats_t g_gps_stats;
static gps_fix_callback_t g_fix_callback = 0;
static char s_line[NMEA_MAX_LEN];
static int s_line_len = 0;
static float s_last_hdop = 99.0f;
static uint8_t s_last_satellites = 0;

typedef int (*nmea_handler_fn)(const nmea_sentence_t *s);

typedef struct {
    const char     *type;
    nmea_handler_fn handler;
} nmea_handler_t;

static int handle_rmc(const nmea_sentence_t *s);
static int handle_gga(const nmea_sentence_t *s);

/* Sentence dispatch table: which parser handles which NMEA sentence type. */
static const nmea_handler_t k_handlers[] = {
    { "GPRMC", handle_rmc },
    { "GPGGA", handle_gga },
};

void gps_receiver_init(void)
{
    rb_init(&g_gps_rx);
    memset(&g_gps_stats, 0, sizeof(g_gps_stats));
    s_line_len = 0;
}

/* Registers the function that receives every decoded fix. */
void gps_receiver_set_callback(gps_fix_callback_t cb)
{
    g_fix_callback = cb;
}

/* GGA carries fix quality (satellites, HDOP). It does not produce a fix on its own. */
static int handle_gga(const nmea_sentence_t *s)
{
    if (s->field_count < 8) {
        return -1;
    }
    s_last_satellites = (uint8_t)atoi(s->fields[6]);
    s_last_hdop = (float)atof(s->fields[7]);
    return 0;
}

/* RMC carries position, speed and course: this is where a GPS fix is built. */
static int handle_rmc(const nmea_sentence_t *s)
{
    gps_fix_t fix;

    if (s->field_count < 8) {
        return -1;
    }
    memset(&fix, 0, sizeof(fix));
    fix.valid        = (s->fields[1][0] == 'A');
    fix.latitude     = nmea_parse_coord(s->fields[2], s->fields[3]);
    fix.longitude    = nmea_parse_coord(s->fields[4], s->fields[5]);
    fix.speed_knots  = atof(s->fields[6]);
    fix.heading_deg  = atof(s->fields[7]);
    fix.hdop         = s_last_hdop;
    fix.satellites   = s_last_satellites;
    fix.timestamp_ms = hal_get_tick_ms();

    if (g_fix_callback != 0) {
        g_fix_callback(&fix);
    }
    return 0;
}

/* Parses one complete NMEA line and dispatches it by sentence type. */
int gps_receiver_process_line(const char *line)
{
    nmea_sentence_t sentence;
    size_t i;

    g_gps_stats.sentences++;
    if (!nmea_checksum_ok(line)) {
        g_gps_stats.checksum_errors++;
        return -1;
    }
    if (nmea_split(line, &sentence) != 0) {
        return -1;
    }
    for (i = 0; i < sizeof(k_handlers) / sizeof(k_handlers[0]); i++) {
        if (strcmp(sentence.type, k_handlers[i].type) == 0) {
            return k_handlers[i].handler(&sentence);
        }
    }
    g_gps_stats.unknown_sentences++;
    return 0;
}

/* Called from the main loop: drains bytes received by the UART interrupt and assembles lines. */
void gps_receiver_poll(void)
{
    uint8_t byte;

    while (rb_pop(&g_gps_rx, &byte) == 0) {
        if (byte == '\n') {
            s_line[s_line_len] = '\0';
            (void)gps_receiver_process_line(s_line);
            s_line_len = 0;
        } else if (byte != '\r' && s_line_len < NMEA_MAX_LEN - 1) {
            s_line[s_line_len++] = (char)byte;
        }
    }
}
