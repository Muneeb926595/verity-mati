#ifndef VR_STDDEF_H
#define VR_STDDEF_H
/* the compiler's own types for the target: unsigned long on 64-bit Unix, unsigned long long on Windows */
typedef __SIZE_TYPE__ size_t; typedef __PTRDIFF_TYPE__ ptrdiff_t; typedef __WCHAR_TYPE__ wchar_t;
#define NULL ((void*)0)
#define offsetof(t, m) __builtin_offsetof(t, m)
#endif
