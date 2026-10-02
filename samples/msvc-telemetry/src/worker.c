#include "telemetry.h"

void LogEvent(_In_z_ LPCSTR message)
{
    OutputDebugStringA(message);
}

/* Reads frames from the serial port forever and hands every valid frame to the dispatcher. */
DWORD WINAPI ReaderThread(LPVOID param)
{
    HANDLE port = (HANDLE)param;
    BYTE buffer[MAX_PAYLOAD];
    TELEMETRY_FRAME frame;
    for (;;) {
        DWORD n = ReadSerial(port, buffer, sizeof(buffer));
        if (n == 0) {
            Sleep(10);
            continue;
        }
        if (ParseFrame(buffer, n, &frame)) {
            g_stats.framesOk++;
            g_lastFrameTicks = (__int64)GetTickCount64();
            InterlockedExchange(&g_linkState, LINK_UP);
            DispatchFrame(&frame);
        }
    }
    return 0;
}

/* Marks the link as lost when no valid frame arrived for FRAME_TIMEOUT_MS. */
DWORD WINAPI WatchdogThread(LPVOID param)
{
    UNREFERENCED_PARAMETER(param);
    for (;;) {
        Sleep(1000);
        __int64 now = (__int64)GetTickCount64();
        if (g_linkState == LINK_UP && now - g_lastFrameTicks > FRAME_TIMEOUT_MS) {
            InterlockedExchange(&g_linkState, LINK_LOST);
            LogEvent("telemetry link lost");
        }
    }
    return 0;
}
