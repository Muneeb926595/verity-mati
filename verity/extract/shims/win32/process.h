/* Verity shim: MSVC process.h (threads) */
#include "windows.h"
uintptr_t _beginthreadex(void *security, unsigned stack_size, unsigned (__stdcall *start_address)(void *), void *arglist,
                         unsigned initflag, unsigned *thrdaddr);
uintptr_t _beginthread(void (__cdecl *start_address)(void *), unsigned stack_size, void *arglist);
void _endthreadex(unsigned retval);
