#ifndef VR_SYS_TIME_H
#define VR_SYS_TIME_H
/* Verity knowledge-pack header: POSIX <sys/time.h>, for hosts without it (Windows). Declarations only. */
#include <time.h>                /* struct timespec: C11, and part of the Windows C runtime */
/* winsock.h / winsock2.h define struct timeval without a guard: only define it when they have not */
#if !defined(_WINSOCKAPI_) && !defined(_WINSOCK2API_) && !defined(_TIMEVAL_DEFINED)
#define _TIMEVAL_DEFINED
struct timeval { long tv_sec; long tv_usec; };
#endif
struct timezone { int tz_minuteswest; int tz_dsttime; };
int gettimeofday(struct timeval *tv, void *tz);
#ifndef timerisset
#define timerisset(tvp) ((tvp)->tv_sec || (tvp)->tv_usec)
#define timerclear(tvp) ((tvp)->tv_sec = (tvp)->tv_usec = 0)
#define timercmp(a, b, CMP) (((a)->tv_sec == (b)->tv_sec) ? ((a)->tv_usec CMP (b)->tv_usec) : ((a)->tv_sec CMP (b)->tv_sec))
#define timeradd(a, b, result) do { (result)->tv_sec = (a)->tv_sec + (b)->tv_sec; \
    (result)->tv_usec = (a)->tv_usec + (b)->tv_usec; \
    if ((result)->tv_usec >= 1000000) { ++(result)->tv_sec; (result)->tv_usec -= 1000000; } } while (0)
#define timersub(a, b, result) do { (result)->tv_sec = (a)->tv_sec - (b)->tv_sec; \
    (result)->tv_usec = (a)->tv_usec - (b)->tv_usec; \
    if ((result)->tv_usec < 0) { --(result)->tv_sec; (result)->tv_usec += 1000000; } } while (0)
#endif
#endif
