#include "ring_buffer.h"

void rb_init(ring_buffer_t *rb)
{
    rb->head = 0;
    rb->tail = 0;
}

/* Called from the UART interrupt. Returns -1 and drops the byte when the buffer is full. */
int rb_push(ring_buffer_t *rb, uint8_t byte)
{
    uint16_t next = (uint16_t)((rb->head + 1u) % RB_SIZE);

    if (next == rb->tail) {
        return -1;
    }
    rb->data[rb->head] = byte;
    rb->head = next;
    return 0;
}

/* Called from the main loop. Returns -1 when the buffer is empty. */
int rb_pop(ring_buffer_t *rb, uint8_t *out)
{
    if (rb->head == rb->tail) {
        return -1;
    }
    *out = rb->data[rb->tail];
    rb->tail = (uint16_t)((rb->tail + 1u) % RB_SIZE);
    return 0;
}
