# navgps-c — Verity validation project

A small embedded-style GPS navigation firmware, written to exercise Verity.
There is deliberately **no build system** and one **vendor header is missing** (`stm32f4xx.h`),
like code received as source only. The board support package (`hal/hal.h`) is declared but not implemented.

Data path:
USART2_IRQHandler (UART ISR) → g_gps_rx ring buffer → gps_receiver_poll (main loop) → gps_receiver_process_line
→ k_handlers[] dispatch table → handle_rmc builds gps_fix_t → g_fix_callback (= ProcessGpsData, registered in main)
→ gps_validate_fix → CalculateHeading → currentHeading → NavigationTick (every 100 ms) → VehicleController_SetDirection
→ motor_set_steering (HAL actuator).

Traps: function-pointer dispatch table, callback registered at runtime, stateful heading filter (the result depends on the
previous heading, so static analysis alone cannot give the value), timeout logic that depends on the clock, an undefined
configuration macro (ENABLE_DEAD_RECKONING), a degraded mode that stops steering without centring it, and no magnetometer.
