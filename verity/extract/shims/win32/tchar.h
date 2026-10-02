/* Verity shim: TCHAR mapping */
#include "windows.h"
#ifdef UNICODE
#define _tmain wmain
#define _tcslen wcslen
#define _tprintf wprintf
#else
#define _tmain main
#define _tcslen strlen
#define _tprintf printf
#define _tcscpy strcpy
#define _tcscmp strcmp
#define _stprintf sprintf
#endif
