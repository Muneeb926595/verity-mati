#include "telemetry.h"

static const HANDLER_ENTRY g_handlers[] = {
    { FRAME_TEMPERATURE, OnTemperature },
    { FRAME_PRESSURE,    OnPressure },
    { FRAME_HEARTBEAT,   OnHeartbeat },
};

/* Routes a decoded frame to its handler by frame type. */
BOOL DispatchFrame(_In_ const TELEMETRY_FRAME *frame)
{
    for (size_t i = 0; i < sizeof(g_handlers) / sizeof(g_handlers[0]); i++) {
        if (g_handlers[i].type == frame->type) {
            return g_handlers[i].handler(frame);
        }
    }
    g_stats.unknownFrames++;
    return FALSE;
}

/* Converts the raw reading to degrees, applies the calibration and checks the alarm threshold. */
BOOL OnTemperature(const TELEMETRY_FRAME *frame)
{
    double celsius = frame->temperatureCenti / 100.0 + g_calibrationOffsetC;
    g_temperatureC = celsius;
    if (celsius > ALARM_THRESHOLD_C) {
        RaiseAlarm(celsius);
    } else if (g_alarmActive) {
        ClearAlarm();
    }
    return TRUE;
}

BOOL OnPressure(const TELEMETRY_FRAME *frame)
{
    char line[64];
    sprintf_s(line, sizeof(line), "pressure %u hPa", (unsigned)frame->pressureHpa);
    LogEvent(line);
    return TRUE;
}

BOOL OnHeartbeat(const TELEMETRY_FRAME *frame)
{
    UNREFERENCED_PARAMETER(frame);
    return TRUE;
}
