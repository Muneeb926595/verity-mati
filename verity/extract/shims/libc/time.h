#ifndef VR_TIME_H
#define VR_TIME_H
typedef long time_t; typedef long clock_t; time_t time(time_t *t); clock_t clock(void);
#define CLOCKS_PER_SEC 1000000
#endif
