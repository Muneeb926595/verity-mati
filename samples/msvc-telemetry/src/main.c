#include "telemetry.h"

int wmain(int argc, wchar_t **argv)
{
    UNREFERENCED_PARAMETER(argc);
    UNREFERENCED_PARAMETER(argv);
    /* calibration comes from the site's ini file at run time */
    g_calibrationOffsetC = GetPrivateProfileIntA("sensor", "offset_centi", 0, ".\\telemetry.ini") / 100.0;
    HANDLE port = OpenComPort("\\\\.\\COM3");
    if (port == INVALID_HANDLE_VALUE) {
        return 1;
    }
    HANDLE threads[2];
    threads[0] = CreateThread(NULL, 0, ReaderThread, port, 0, NULL);
    threads[1] = CreateThread(NULL, 0, WatchdogThread, NULL, 0, NULL);
    WaitForMultipleObjects(2, threads, TRUE, INFINITE);
    CloseHandle(port);
    return 0;
}
