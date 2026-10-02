#include "stm32f4xx.h"      /* vendor device header: not part of this repository */
#include "ring_buffer.h"

/* Bytes from the GPS module, filled by the UART interrupt, drained by gps_receiver_poll(). */
ring_buffer_t g_gps_rx;

/* USART2 receive interrupt: the GPS module is wired to USART2. */
void USART2_IRQHandler(void)
{
    if (USART2->SR & USART_SR_RXNE) {
        uint8_t byte = (uint8_t)(USART2->DR & 0xFFu);
        (void)rb_push(&g_gps_rx, byte);
    }
}
