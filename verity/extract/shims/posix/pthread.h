#ifndef VR_PTHREAD_H
#define VR_PTHREAD_H
/* Verity knowledge-pack header: POSIX threads, for hosts without them (Windows). Declarations only. */
typedef unsigned long pthread_t;
typedef struct { int __vr; } pthread_attr_t;
typedef struct { int __vr; } pthread_mutex_t;
typedef struct { int __vr; } pthread_mutexattr_t;
typedef struct { int __vr; } pthread_cond_t;
typedef struct { int __vr; } pthread_condattr_t;
typedef int pthread_once_t;
typedef unsigned int pthread_key_t;
#define PTHREAD_MUTEX_INITIALIZER {0}
#define PTHREAD_COND_INITIALIZER {0}
#define PTHREAD_ONCE_INIT 0
int pthread_create(pthread_t *thread, const pthread_attr_t *attr, void *(*start_routine)(void *), void *arg);
int pthread_join(pthread_t thread, void **retval);
int pthread_detach(pthread_t thread);
void pthread_exit(void *retval);
pthread_t pthread_self(void);
int pthread_equal(pthread_t a, pthread_t b);
int pthread_cancel(pthread_t thread);
int pthread_attr_init(pthread_attr_t *attr);
int pthread_attr_destroy(pthread_attr_t *attr);
int pthread_attr_setstacksize(pthread_attr_t *attr, unsigned long stacksize);
int pthread_attr_setdetachstate(pthread_attr_t *attr, int state);
int pthread_mutex_init(pthread_mutex_t *mutex, const pthread_mutexattr_t *attr);
int pthread_mutex_destroy(pthread_mutex_t *mutex);
int pthread_mutex_lock(pthread_mutex_t *mutex);
int pthread_mutex_trylock(pthread_mutex_t *mutex);
int pthread_mutex_unlock(pthread_mutex_t *mutex);
int pthread_cond_init(pthread_cond_t *cond, const pthread_condattr_t *attr);
int pthread_cond_destroy(pthread_cond_t *cond);
int pthread_cond_wait(pthread_cond_t *cond, pthread_mutex_t *mutex);
int pthread_cond_signal(pthread_cond_t *cond);
int pthread_cond_broadcast(pthread_cond_t *cond);
int pthread_once(pthread_once_t *once, void (*init_routine)(void));
int pthread_key_create(pthread_key_t *key, void (*destructor)(void *));
void *pthread_getspecific(pthread_key_t key);
int pthread_setspecific(pthread_key_t key, const void *value);
#define PTHREAD_CREATE_JOINABLE 0
#define PTHREAD_CREATE_DETACHED 1
#endif
