#ifndef VR_STDLIB_H
#define VR_STDLIB_H
#include <stddef.h>
double atof(const char *s); int atoi(const char *s); long atol(const char *s); double strtod(const char *s, char **e);
float strtof(const char *s, char **e); long strtol(const char *s, char **e, int b); unsigned long strtoul(const char *s, char **e, int b);
void *malloc(size_t n); void *calloc(size_t n, size_t m); void *realloc(void *p, size_t n); void free(void *p);
void exit(int c); void abort(void); int abs(int x); long labs(long x); int atexit(void (*f)(void));
void qsort(void *b, size_t n, size_t s, int (*c)(const void *, const void *));
#endif
