/* Verity shim: MSVC intrinsics used for synchronisation */
#include "windows.h"
#define _InterlockedExchange InterlockedExchange
#define _InterlockedIncrement InterlockedIncrement
#define _InterlockedDecrement InterlockedDecrement
#define _InterlockedCompareExchange InterlockedCompareExchange
#define _ReadWriteBarrier() ((void)0)
#define __debugbreak() ((void)0)
