#include "telemetry.h"

HANDLE OpenComPort(_In_z_ LPCSTR portName)
{
    HANDLE port = CreateFileA(portName, GENERIC_READ | GENERIC_WRITE, 0, NULL, OPEN_EXISTING, 0, NULL);
    if (port == INVALID_HANDLE_VALUE) {
        LogEvent("cannot open serial port");
        return INVALID_HANDLE_VALUE;
    }
    DCB dcb;
    ZeroMemory(&dcb, sizeof(dcb));
    dcb.DCBlength = sizeof(dcb);
    dcb.BaudRate = CBR_115200;
    dcb.ByteSize = 8;
    dcb.Parity = NOPARITY;
    dcb.StopBits = ONESTOPBIT;
    if (!SetCommState(port, &dcb)) {
        CloseHandle(port);
        return INVALID_HANDLE_VALUE;
    }
    return port;
}

/* Reads whatever bytes are available; returns the number of bytes read (0 on error or timeout). */
DWORD ReadSerial(_In_ HANDLE port, _Out_writes_bytes_(capacity) BYTE *buffer, _In_ DWORD capacity)
{
    DWORD bytesRead = 0;
    if (!ReadFile(port, buffer, capacity, &bytesRead, NULL)) {
        return 0;
    }
    return bytesRead;
}
