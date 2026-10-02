#ifndef HAL_H
#define HAL_H

#include <stdint.h>

/* Board support package. Implemented by the board's BSP, which is not part of this repository. */
uint32_t hal_get_tick_ms(void);                       /* milliseconds since boot */
void     motor_set_steering(int16_t angle_tenths);    /* steering actuator, 0.1 degree units */
void     led_set_status(int on);                      /* status LED */

#endif /* HAL_H */
