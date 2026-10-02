#ifndef VR_STDIO_H
#define VR_STDIO_H
#include <stddef.h>
#include <stdarg.h>
typedef struct _vr_FILE FILE; extern FILE *stdin, *stdout, *stderr;
#define EOF (-1)
int printf(const char *f, ...); int fprintf(FILE *s, const char *f, ...); int sprintf(char *b, const char *f, ...);
int snprintf(char *b, size_t n, const char *f, ...); int vsnprintf(char *b, size_t n, const char *f, va_list a);
int scanf(const char *f, ...); int sscanf(const char *s, const char *f, ...); int fscanf(FILE *s, const char *f, ...);
int puts(const char *s); int fputs(const char *s, FILE *f); int getchar(void); int fgetc(FILE *f); int getc(FILE *f);
char *fgets(char *b, int n, FILE *f); FILE *fopen(const char *p, const char *m); int fclose(FILE *f);
size_t fread(void *p, size_t s, size_t n, FILE *f); size_t fwrite(const void *p, size_t s, size_t n, FILE *f); int fflush(FILE *f);
#endif
