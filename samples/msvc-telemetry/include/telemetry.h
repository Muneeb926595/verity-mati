#pragma once
#include "pch.h"

#define FRAME_SYNC            0xA5
#define FRAME_TEMPERATURE     0x01
#define FRAME_PRESSURE        0x02
#define FRAME_HEARTBEAT       0x03
#define FRAME_TIMEOUT_MS      3000      /* no frame for this long = link lost */
#define ALARM_THRESHOLD_C     80.0      /* raise the alarm above this temperature */
#define MAX_PAYLOAD           32

#pragma pack(push, 1)
typedef struct _TELEMETRY_FRAME {
    BYTE   sync;              /* always FRAME_SYNC */
    BYTE   type;              /* FRAME_TEMPERATURE / FRAME_PRESSURE / FRAME_HEARTBEAT */
    WORD   length;            /* payload bytes */
    INT16  temperatureCenti;  /* temperature in 1/100 degC */
    UINT16 pressureHpa;       /* pressure in hPa */
    DWORD  crc;               /* CRC-32 of the bytes before this field */
} TELEMETRY_FRAME;
#pragma pack(pop)

typedef enum _LINK_STATE { LINK_DOWN = 0, LINK_UP = 1, LINK_LOST = 2 } LINK_STATE;

typedef struct _STATS {
    DWORD framesOk;
    DWORD crcErrors;
    DWORD unknownFrames;
} STATS;

typedef BOOL (*FRAME_HANDLER)(const TELEMETRY_FRAME *frame);

typedef struct _HANDLER_ENTRY {
    BYTE          type;
    FRAME_HANDLER handler;
} HANDLER_ENTRY;

/* state.c */
extern volatile LONG g_alarmActive;
extern volatile LONG g_linkState;
extern __int64       g_lastFrameTicks;
extern double        g_temperatureC;
extern double        g_calibrationOffsetC;
extern STATS         g_stats;

/* serial_port.c */
HANDLE OpenComPort(_In_z_ LPCSTR portName);
DWORD  ReadSerial(_In_ HANDLE port, _Out_writes_bytes_(capacity) BYTE *buffer, _In_ DWORD capacity);

/* frame_parser.c */
DWORD Crc32(_In_reads_bytes_(len) const BYTE *data, _In_ size_t len);
BOOL  ParseFrame(_In_reads_bytes_(len) const BYTE *data, _In_ size_t len, _Out_ TELEMETRY_FRAME *out);

/* dispatcher.c */
BOOL DispatchFrame(_In_ const TELEMETRY_FRAME *frame);
BOOL OnTemperature(const TELEMETRY_FRAME *frame);
BOOL OnPressure(const TELEMETRY_FRAME *frame);
BOOL OnHeartbeat(const TELEMETRY_FRAME *frame);

/* alarm.c */
void RaiseAlarm(double temperatureC);
void ClearAlarm(void);

/* worker.c */
DWORD WINAPI ReaderThread(LPVOID param);
DWORD WINAPI WatchdogThread(LPVOID param);
void LogEvent(_In_z_ LPCSTR message);

/* api.c */
__declspec(dllexport) BOOL __stdcall Telemetry_GetTemperature(_Out_ double *temperatureC);
