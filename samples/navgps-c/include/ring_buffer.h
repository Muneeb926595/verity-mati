#ifndef RING_BUFFER_H
#define RING_BUFFER_H

#include <stdint.h>

#define RB_SIZE 256

/* Single-producer (ISR) / single-consumer (main loop) byte queue. */
typedef struct {
    volatile uint8_t  data[RB_SIZE];
    volatile uint16_t head;
    volatile uint16_t tail;
} ring_buffer_t;

void rb_init(ring_buffer_t *rb);
int  rb_push(ring_buffer_t *rb, uint8_t byte);
int  rb_pop(ring_buffer_t *rb, uint8_t *out);

#endif /* RING_BUFFER_H */
