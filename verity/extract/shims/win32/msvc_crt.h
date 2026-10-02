/* Verity shim: MSVC CRT extensions missing from other C libraries (secure *_s functions, _ names). Implemented
 * with standard calls so analysis sees real signatures and sandboxed runs behave the same. */
#ifndef VERITY_WIN32_SHIM_MSVC_CRT_H
#define VERITY_WIN32_SHIM_MSVC_CRT_H
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <stdarg.h>
#include <ctype.h>
typedef int errno_t;
#ifndef _TRUNCATE
#define _TRUNCATE ((size_t)-1)
#endif
static inline int sprintf_s(char *buf, size_t size, const char *fmt, ...)
{ va_list ap; va_start(ap, fmt); int n = vsnprintf(buf, size, fmt, ap); va_end(ap); return n; }
static inline int _snprintf(char *buf, size_t size, const char *fmt, ...)
{ va_list ap; va_start(ap, fmt); int n = vsnprintf(buf, size, fmt, ap); va_end(ap); return n; }
static inline int _snprintf_s(char *buf, size_t size, size_t count, const char *fmt, ...)
{ va_list ap; (void)count; va_start(ap, fmt); int n = vsnprintf(buf, size, fmt, ap); va_end(ap); return n; }
static inline int vsprintf_s(char *buf, size_t size, const char *fmt, va_list ap) { return vsnprintf(buf, size, fmt, ap); }
static inline errno_t strcpy_s(char *dst, size_t size, const char *src)
{ if (!dst || !src || size == 0) return 22; size_t n = strlen(src); if (n >= size) { dst[0] = 0; return 34; } memcpy(dst, src, n + 1); return 0; }
static inline errno_t strncpy_s(char *dst, size_t size, const char *src, size_t count)
{ if (!dst || !src || size == 0) return 22; size_t n = strnlen(src, count); if (n >= size) n = size - 1; memcpy(dst, src, n); dst[n] = 0; return 0; }
static inline errno_t strcat_s(char *dst, size_t size, const char *src)
{ size_t a = strlen(dst), b = strlen(src); if (a + b >= size) return 34; memcpy(dst + a, src, b + 1); return 0; }
static inline errno_t memcpy_s(void *dst, size_t dsize, const void *src, size_t n)
{ if (n > dsize) return 34; memcpy(dst, src, n); return 0; }
static inline errno_t memmove_s(void *dst, size_t dsize, const void *src, size_t n)
{ if (n > dsize) return 34; memmove(dst, src, n); return 0; }
static inline errno_t fopen_s(FILE **f, const char *name, const char *mode) { *f = fopen(name, mode); return *f ? 0 : 2; }
static inline int _stricmp(const char *a, const char *b)
{ while (*a && tolower((unsigned char)*a) == tolower((unsigned char)*b)) { a++; b++; } return tolower((unsigned char)*a) - tolower((unsigned char)*b); }
static inline int _strnicmp(const char *a, const char *b, size_t n)
{ while (n && *a && tolower((unsigned char)*a) == tolower((unsigned char)*b)) { a++; b++; n--; } return n ? tolower((unsigned char)*a) - tolower((unsigned char)*b) : 0; }
#define stricmp _stricmp
#define strnicmp _strnicmp
#define _strdup strdup
#define sscanf_s sscanf
#define _itoa_s(v, b, s, r) (snprintf((b), (s), (r) == 16 ? "%x" : "%d", (v)), 0)
#define _countof(a) (sizeof(a) / sizeof((a)[0]))
#endif
