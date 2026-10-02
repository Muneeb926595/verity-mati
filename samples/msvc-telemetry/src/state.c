#include "telemetry.h"

volatile LONG g_alarmActive = 0;       /* 1 while the over-temperature alarm is raised */
volatile LONG g_linkState   = LINK_DOWN;
__int64       g_lastFrameTicks = 0;     /* GetTickCount64() when the last good frame arrived */
double        g_temperatureC = 0.0;     /* latest calibrated temperature, degC */
double        g_calibrationOffsetC = 0.0;
STATS         g_stats;
