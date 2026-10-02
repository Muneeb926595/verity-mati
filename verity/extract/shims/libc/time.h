#ifndef VR_TIME_H
#define VR_TIME_H
typedef long time_t; typedef long clock_t; time_t time(time_t *t); clock_t clock(void);
struct timespec { time_t tv_sec; long tv_nsec; };   /* C11 */
#define CLOCKS_PER_SEC 1000000
#endif
