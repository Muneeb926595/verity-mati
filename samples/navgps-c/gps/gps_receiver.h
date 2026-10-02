#ifndef GPS_RECEIVER_H
#define GPS_RECEIVER_H

#include "gps_types.h"

void gps_receiver_init(void);
void gps_receiver_set_callback(gps_fix_callback_t cb);
void gps_receiver_poll(void);
int  gps_receiver_process_line(const char *line);

extern gps_stats_t g_gps_stats;

#endif /* GPS_RECEIVER_H */
