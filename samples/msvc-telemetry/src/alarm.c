#include "telemetry.h"

static HANDLE s_alarmPipe = NULL;

static void SendAlarmMessage(_In_z_ LPCSTR text)
{
    if (s_alarmPipe == NULL) {
        s_alarmPipe = CreateFileA("\\\\.\\pipe\\TelemetryAlarm", GENERIC_WRITE, 0, NULL, OPEN_EXISTING, 0, NULL);
    }
    if (s_alarmPipe != INVALID_HANDLE_VALUE) {
        DWORD written = 0;
        WriteFile(s_alarmPipe, text, (DWORD)strlen(text), &written, NULL);
    }
}

/* Raises the over-temperature alarm once; repeated calls while it is active do nothing. */
void RaiseAlarm(double temperatureC)
{
    if (InterlockedExchange(&g_alarmActive, 1) == 0) {
        char text[64];
        sprintf_s(text, sizeof(text), "ALARM over-temperature %.1f C", temperatureC);
        SendAlarmMessage(text);
        LogEvent(text);
    }
}

void ClearAlarm(void)
{
    InterlockedExchange(&g_alarmActive, 0);
    SendAlarmMessage("ALARM cleared");
}
