/* Verity Win32 API knowledge pack (shim). Used ONLY when the real Windows SDK is not available, so that
 * uncompiled MSVC-style code can be analysed on any machine. Types have their Win64 (LLP64) sizes. Pure helpers
 * that change memory (Interlocked*, ZeroMemory, secure CRT) are implemented so sandboxed runs behave like
 * Windows; OS and hardware APIs are declared only (analysis treats them as I/O; runs replace them with stubs). */
#ifndef VERITY_WIN32_SHIM_WINDOWS_H
#define VERITY_WIN32_SHIM_WINDOWS_H
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include <stdarg.h>
#include "sal.h"

#ifndef _WIN32
#define _WIN32 1
#endif
#ifndef _WIN64
#define _WIN64 1
#endif
#define WINAPI      __stdcall
#define APIENTRY    WINAPI
#define CALLBACK    __stdcall
#define WINAPIV     __cdecl
#define WINBASEAPI
#define DECLSPEC_IMPORT
#define CONST       const
#define VOID        void
#ifndef FORCEINLINE
#define FORCEINLINE static inline
#endif

typedef unsigned char       BYTE, UCHAR, BOOLEAN, UINT8, *PBYTE, *LPBYTE;
typedef signed char         CHAR8, INT8;
typedef char                CHAR, *PCHAR, *LPSTR, *PSTR;
typedef const char          *LPCSTR, *PCSTR;
typedef unsigned short      WORD, USHORT, UINT16, WCHAR, *PWORD, *LPWORD;
typedef short               SHORT, INT16;
typedef int                 BOOL, INT, INT32, LONG, *PBOOL, *LPBOOL, *PLONG, *LPLONG;
typedef unsigned int        UINT, UINT32, DWORD, ULONG, DWORD32, *PDWORD, *LPDWORD, *PULONG, *PUINT;
typedef long long           LONGLONG, INT64, LONG64, LONG_PTR, INT_PTR, SSIZE_T, LPARAM, LRESULT;
typedef unsigned long long  ULONGLONG, UINT64, DWORD64, ULONG64, QWORD, ULONG_PTR, UINT_PTR, DWORD_PTR, SIZE_T, WPARAM;
typedef float               FLOAT;
typedef double              DOUBLE;
typedef long                HRESULT;
typedef WCHAR               *LPWSTR, *PWSTR;
typedef const WCHAR         *LPCWSTR, *PCWSTR;
typedef void                *PVOID, *LPVOID, *HANDLE, *HMODULE, *HINSTANCE, *HWND, *HKEY, *HDC, *HMENU, *HICON,
                            *HBRUSH, *HFONT, *HGLOBAL, *HLOCAL, *FARPROC, *HCURSOR, *HBITMAP, *HPEN;
typedef const void          *LPCVOID, *PCVOID;
typedef HANDLE              *PHANDLE, *LPHANDLE;
typedef WORD                ATOM;
typedef DWORD               COLORREF;
#ifdef UNICODE
typedef WCHAR TCHAR, *LPTSTR; typedef const WCHAR *LPCTSTR;
#define TEXT(s) L##s
#else
typedef char TCHAR, *LPTSTR; typedef const char *LPCTSTR;
#define TEXT(s) s
#endif
#define _T(s) TEXT(s)
#define __TEXT(s) TEXT(s)

typedef union _LARGE_INTEGER { struct { DWORD LowPart; LONG HighPart; } u; LONGLONG QuadPart; } LARGE_INTEGER, *PLARGE_INTEGER;
typedef union _ULARGE_INTEGER { struct { DWORD LowPart; DWORD HighPart; } u; ULONGLONG QuadPart; } ULARGE_INTEGER;
typedef struct _FILETIME { DWORD dwLowDateTime; DWORD dwHighDateTime; } FILETIME, *LPFILETIME;
typedef struct _SYSTEMTIME { WORD wYear, wMonth, wDayOfWeek, wDay, wHour, wMinute, wSecond, wMilliseconds; } SYSTEMTIME, *LPSYSTEMTIME;
typedef struct _SECURITY_ATTRIBUTES { DWORD nLength; LPVOID lpSecurityDescriptor; BOOL bInheritHandle; } SECURITY_ATTRIBUTES, *LPSECURITY_ATTRIBUTES;
typedef struct _OVERLAPPED { ULONG_PTR Internal, InternalHigh; DWORD Offset, OffsetHigh; HANDLE hEvent; } OVERLAPPED, *LPOVERLAPPED;
typedef struct _CRITICAL_SECTION { LONG LockCount; LONG RecursionCount; HANDLE OwningThread; ULONG_PTR SpinCount; } CRITICAL_SECTION, *LPCRITICAL_SECTION;
typedef struct _GUID { DWORD Data1; WORD Data2; WORD Data3; BYTE Data4[8]; } GUID;
typedef struct _POINT { LONG x, y; } POINT;
typedef struct _RECT { LONG left, top, right, bottom; } RECT;
typedef struct _DCB {
    DWORD DCBlength; DWORD BaudRate; DWORD fBinary: 1; DWORD fParity: 1; DWORD fOutxCtsFlow: 1; DWORD fOutxDsrFlow: 1;
    DWORD fDtrControl: 2; DWORD fDsrSensitivity: 1; DWORD fTXContinueOnXoff: 1; DWORD fOutX: 1; DWORD fInX: 1;
    DWORD fErrorChar: 1; DWORD fNull: 1; DWORD fRtsControl: 2; DWORD fAbortOnError: 1; DWORD fDummy2: 17;
    WORD wReserved; WORD XonLim; WORD XoffLim; BYTE ByteSize; BYTE Parity; BYTE StopBits; char XonChar; char XoffChar;
    char ErrorChar; char EofChar; char EvtChar; WORD wReserved1;
} DCB, *LPDCB;
typedef struct _COMMTIMEOUTS { DWORD ReadIntervalTimeout, ReadTotalTimeoutMultiplier, ReadTotalTimeoutConstant,
                               WriteTotalTimeoutMultiplier, WriteTotalTimeoutConstant; } COMMTIMEOUTS, *LPCOMMTIMEOUTS;
typedef struct _COMSTAT { DWORD fFlags; DWORD cbInQue; DWORD cbOutQue; } COMSTAT, *LPCOMSTAT;
typedef DWORD (WINAPI *PTHREAD_START_ROUTINE)(LPVOID lpThreadParameter);
typedef PTHREAD_START_ROUTINE LPTHREAD_START_ROUTINE;
typedef void (CALLBACK *TIMERPROC)(HWND, UINT, UINT_PTR, DWORD);
typedef LRESULT (CALLBACK *WNDPROC)(HWND, UINT, WPARAM, LPARAM);

#define TRUE  1
#define FALSE 0
#ifndef NULL
#define NULL ((void *)0)
#endif
#define INVALID_HANDLE_VALUE ((HANDLE)(LONG_PTR)-1)
#define INFINITE            0xFFFFFFFFu
#define WAIT_OBJECT_0       0x00000000u
#define WAIT_ABANDONED      0x00000080u
#define WAIT_TIMEOUT        0x00000102u
#define WAIT_FAILED         0xFFFFFFFFu
#define GENERIC_READ        0x80000000u
#define GENERIC_WRITE       0x40000000u
#define FILE_SHARE_READ     0x00000001u
#define FILE_SHARE_WRITE    0x00000002u
#define CREATE_NEW          1
#define CREATE_ALWAYS       2
#define OPEN_EXISTING       3
#define OPEN_ALWAYS         4
#define FILE_ATTRIBUTE_NORMAL 0x00000080u
#define FILE_FLAG_OVERLAPPED  0x40000000u
#define MAX_PATH            260
#define CBR_9600 9600
#define CBR_19200 19200
#define CBR_38400 38400
#define CBR_57600 57600
#define CBR_115200 115200
#define NOPARITY 0
#define ODDPARITY 1
#define EVENPARITY 2
#define ONESTOPBIT 0
#define TWOSTOPBITS 2
#define PURGE_RXCLEAR 0x0008
#define PURGE_TXCLEAR 0x0004
#define ERROR_SUCCESS 0L
#define ERROR_INVALID_HANDLE 6L
#define ERROR_TIMEOUT 1460L
#define S_OK ((HRESULT)0L)
#define E_FAIL ((HRESULT)0x80004005L)
#define SUCCEEDED(hr) (((HRESULT)(hr)) >= 0)
#define FAILED(hr) (((HRESULT)(hr)) < 0)
#define HKEY_LOCAL_MACHINE ((HKEY)(ULONG_PTR)0x80000002)
#define HKEY_CURRENT_USER ((HKEY)(ULONG_PTR)0x80000001)
#define KEY_READ 0x20019
#define REG_DWORD 4
#define REG_SZ 1
#define MB_OK 0x0
#define MB_ICONERROR 0x10
#define WM_USER 0x0400
#define WM_TIMER 0x0113

#define UNREFERENCED_PARAMETER(P) ((void)(P))
#define ZeroMemory(d, n)     memset((d), 0, (n))
#define SecureZeroMemory(d, n) memset((d), 0, (n))
#define CopyMemory(d, s, n)  memcpy((d), (s), (n))
#define MoveMemory(d, s, n)  memmove((d), (s), (n))
#define FillMemory(d, n, v)  memset((d), (v), (n))
#define ARRAYSIZE(a)         (sizeof(a) / sizeof((a)[0]))
#define _countof(a)          (sizeof(a) / sizeof((a)[0]))
#define MAKEWORD(a, b)       ((WORD)(((BYTE)(a)) | ((WORD)((BYTE)(b))) << 8))
#define MAKELONG(a, b)       ((LONG)(((WORD)(a)) | ((DWORD)((WORD)(b))) << 16))
#define LOWORD(l)            ((WORD)((DWORD_PTR)(l) & 0xffff))
#define HIWORD(l)            ((WORD)((DWORD_PTR)(l) >> 16))
#define LOBYTE(w)            ((BYTE)((DWORD_PTR)(w) & 0xff))
#define HIBYTE(w)            ((BYTE)((DWORD_PTR)(w) >> 8))
#define min(a, b)            (((a) < (b)) ? (a) : (b))
#define max(a, b)            (((a) > (b)) ? (a) : (b))

/* ---- implemented: atomic and memory helpers (so runs behave like Windows) ---- */
FORCEINLINE LONG InterlockedExchange(LONG volatile *target, LONG value) { LONG old = *target; *target = value; return old; }
FORCEINLINE LONG InterlockedIncrement(LONG volatile *target) { return ++(*target); }
FORCEINLINE LONG InterlockedDecrement(LONG volatile *target) { return --(*target); }
FORCEINLINE LONG InterlockedAdd(LONG volatile *target, LONG value) { return (*target += value); }
FORCEINLINE LONG InterlockedExchangeAdd(LONG volatile *target, LONG value) { LONG old = *target; *target += value; return old; }
FORCEINLINE LONG InterlockedCompareExchange(LONG volatile *dest, LONG exchange, LONG comparand)
{ LONG old = *dest; if (old == comparand) *dest = exchange; return old; }
FORCEINLINE PVOID InterlockedExchangePointer(PVOID volatile *target, PVOID value) { PVOID old = *target; *target = value; return old; }
FORCEINLINE int lstrlenA(LPCSTR s) { return (int)strlen(s); }
FORCEINLINE LPSTR lstrcpyA(LPSTR d, LPCSTR s) { return strcpy(d, s); }
FORCEINLINE int lstrcmpA(LPCSTR a, LPCSTR b) { return strcmp(a, b); }
FORCEINLINE int lstrcmpiA(LPCSTR a, LPCSTR b) { while (*a && (*a | 32) == (*b | 32)) { a++; b++; } return (*a | 32) - (*b | 32); }
#include "msvc_crt.h"

/* ---- declared only: OS / hardware (analysis: I/O and entry points; runs: logged stand-ins) ---- */
HANDLE WINAPI CreateFileA(LPCSTR lpFileName, DWORD dwDesiredAccess, DWORD dwShareMode, LPSECURITY_ATTRIBUTES lpSecurityAttributes,
                          DWORD dwCreationDisposition, DWORD dwFlagsAndAttributes, HANDLE hTemplateFile);
HANDLE WINAPI CreateFileW(LPCWSTR lpFileName, DWORD dwDesiredAccess, DWORD dwShareMode, LPSECURITY_ATTRIBUTES lpSecurityAttributes,
                          DWORD dwCreationDisposition, DWORD dwFlagsAndAttributes, HANDLE hTemplateFile);
BOOL   WINAPI ReadFile(HANDLE hFile, LPVOID lpBuffer, DWORD nNumberOfBytesToRead, LPDWORD lpNumberOfBytesRead, LPOVERLAPPED lpOverlapped);
BOOL   WINAPI WriteFile(HANDLE hFile, LPCVOID lpBuffer, DWORD nNumberOfBytesToWrite, LPDWORD lpNumberOfBytesWritten, LPOVERLAPPED lpOverlapped);
BOOL   WINAPI CloseHandle(HANDLE hObject);
BOOL   WINAPI FlushFileBuffers(HANDLE hFile);
BOOL   WINAPI DeviceIoControl(HANDLE hDevice, DWORD dwIoControlCode, LPVOID lpInBuffer, DWORD nInBufferSize, LPVOID lpOutBuffer,
                              DWORD nOutBufferSize, LPDWORD lpBytesReturned, LPOVERLAPPED lpOverlapped);
BOOL   WINAPI GetCommState(HANDLE hFile, LPDCB lpDCB);
BOOL   WINAPI SetCommState(HANDLE hFile, LPDCB lpDCB);
BOOL   WINAPI SetCommTimeouts(HANDLE hFile, LPCOMMTIMEOUTS lpCommTimeouts);
BOOL   WINAPI GetCommTimeouts(HANDLE hFile, LPCOMMTIMEOUTS lpCommTimeouts);
BOOL   WINAPI PurgeComm(HANDLE hFile, DWORD dwFlags);
BOOL   WINAPI ClearCommError(HANDLE hFile, LPDWORD lpErrors, LPCOMSTAT lpStat);
BOOL   WINAPI SetupComm(HANDLE hFile, DWORD dwInQueue, DWORD dwOutQueue);
HANDLE WINAPI CreateThread(LPSECURITY_ATTRIBUTES lpThreadAttributes, SIZE_T dwStackSize, LPTHREAD_START_ROUTINE lpStartAddress,
                           LPVOID lpParameter, DWORD dwCreationFlags, LPDWORD lpThreadId);
void   WINAPI ExitThread(DWORD dwExitCode);
BOOL   WINAPI TerminateThread(HANDLE hThread, DWORD dwExitCode);
DWORD  WINAPI WaitForSingleObject(HANDLE hHandle, DWORD dwMilliseconds);
DWORD  WINAPI WaitForMultipleObjects(DWORD nCount, const HANDLE *lpHandles, BOOL bWaitAll, DWORD dwMilliseconds);
HANDLE WINAPI CreateEventA(LPSECURITY_ATTRIBUTES lpEventAttributes, BOOL bManualReset, BOOL bInitialState, LPCSTR lpName);
HANDLE WINAPI CreateEventW(LPSECURITY_ATTRIBUTES lpEventAttributes, BOOL bManualReset, BOOL bInitialState, LPCWSTR lpName);
BOOL   WINAPI SetEvent(HANDLE hEvent);
BOOL   WINAPI ResetEvent(HANDLE hEvent);
HANDLE WINAPI CreateMutexA(LPSECURITY_ATTRIBUTES lpMutexAttributes, BOOL bInitialOwner, LPCSTR lpName);
BOOL   WINAPI ReleaseMutex(HANDLE hMutex);
HANDLE WINAPI CreateSemaphoreA(LPSECURITY_ATTRIBUTES lpSemaphoreAttributes, LONG lInitialCount, LONG lMaximumCount, LPCSTR lpName);
BOOL   WINAPI ReleaseSemaphore(HANDLE hSemaphore, LONG lReleaseCount, LPLONG lpPreviousCount);
void   WINAPI InitializeCriticalSection(LPCRITICAL_SECTION lpCriticalSection);
void   WINAPI EnterCriticalSection(LPCRITICAL_SECTION lpCriticalSection);
void   WINAPI LeaveCriticalSection(LPCRITICAL_SECTION lpCriticalSection);
void   WINAPI DeleteCriticalSection(LPCRITICAL_SECTION lpCriticalSection);
void   WINAPI Sleep(DWORD dwMilliseconds);
DWORD  WINAPI GetTickCount(void);
ULONGLONG WINAPI GetTickCount64(void);
BOOL   WINAPI QueryPerformanceCounter(LARGE_INTEGER *lpPerformanceCount);
BOOL   WINAPI QueryPerformanceFrequency(LARGE_INTEGER *lpFrequency);
void   WINAPI GetSystemTime(LPSYSTEMTIME lpSystemTime);
void   WINAPI GetLocalTime(LPSYSTEMTIME lpSystemTime);
void   WINAPI GetSystemTimeAsFileTime(LPFILETIME lpSystemTimeAsFileTime);
DWORD  WINAPI GetLastError(void);
void   WINAPI SetLastError(DWORD dwErrCode);
void   WINAPI OutputDebugStringA(LPCSTR lpOutputString);
void   WINAPI OutputDebugStringW(LPCWSTR lpOutputString);
UINT   WINAPI GetPrivateProfileIntA(LPCSTR lpAppName, LPCSTR lpKeyName, INT nDefault, LPCSTR lpFileName);
DWORD  WINAPI GetPrivateProfileStringA(LPCSTR lpAppName, LPCSTR lpKeyName, LPCSTR lpDefault, LPSTR lpReturnedString, DWORD nSize, LPCSTR lpFileName);
BOOL   WINAPI WritePrivateProfileStringA(LPCSTR lpAppName, LPCSTR lpKeyName, LPCSTR lpString, LPCSTR lpFileName);
LONG   WINAPI RegOpenKeyExA(HKEY hKey, LPCSTR lpSubKey, DWORD ulOptions, DWORD samDesired, HKEY *phkResult);
LONG   WINAPI RegQueryValueExA(HKEY hKey, LPCSTR lpValueName, LPDWORD lpReserved, LPDWORD lpType, LPBYTE lpData, LPDWORD lpcbData);
LONG   WINAPI RegSetValueExA(HKEY hKey, LPCSTR lpValueName, DWORD Reserved, DWORD dwType, const BYTE *lpData, DWORD cbData);
LONG   WINAPI RegCloseKey(HKEY hKey);
DWORD  WINAPI GetEnvironmentVariableA(LPCSTR lpName, LPSTR lpBuffer, DWORD nSize);
HMODULE WINAPI LoadLibraryA(LPCSTR lpLibFileName);
FARPROC WINAPI GetProcAddress(HMODULE hModule, LPCSTR lpProcName);
BOOL   WINAPI FreeLibrary(HMODULE hLibModule);
HMODULE WINAPI GetModuleHandleA(LPCSTR lpModuleName);
DWORD  WINAPI GetModuleFileNameA(HMODULE hModule, LPSTR lpFilename, DWORD nSize);
LPVOID WINAPI VirtualAlloc(LPVOID lpAddress, SIZE_T dwSize, DWORD flAllocationType, DWORD flProtect);
BOOL   WINAPI VirtualFree(LPVOID lpAddress, SIZE_T dwSize, DWORD dwFreeType);
HANDLE WINAPI GetProcessHeap(void);
LPVOID WINAPI HeapAlloc(HANDLE hHeap, DWORD dwFlags, SIZE_T dwBytes);
BOOL   WINAPI HeapFree(HANDLE hHeap, DWORD dwFlags, LPVOID lpMem);
HANDLE WINAPI GetCurrentThread(void);
DWORD  WINAPI GetCurrentThreadId(void);
DWORD  WINAPI GetCurrentProcessId(void);
BOOL   WINAPI SetThreadPriority(HANDLE hThread, int nPriority);
UINT_PTR WINAPI SetTimer(HWND hWnd, UINT_PTR nIDEvent, UINT uElapse, TIMERPROC lpTimerFunc);
BOOL   WINAPI KillTimer(HWND hWnd, UINT_PTR uIDEvent);
int    WINAPI MessageBoxA(HWND hWnd, LPCSTR lpText, LPCSTR lpCaption, UINT uType);
int    WINAPI MessageBoxW(HWND hWnd, LPCWSTR lpText, LPCWSTR lpCaption, UINT uType);
BOOL   WINAPI PostMessageA(HWND hWnd, UINT Msg, WPARAM wParam, LPARAM lParam);
LRESULT WINAPI SendMessageA(HWND hWnd, UINT Msg, WPARAM wParam, LPARAM lParam);
BOOL   WINAPI SetWindowTextA(HWND hWnd, LPCSTR lpString);
int    WINAPI GetWindowTextA(HWND hWnd, LPSTR lpString, int nMaxCount);
BOOL   WINAPI Beep(DWORD dwFreq, DWORD dwDuration);
#endif
