#include <stdint.h>
#include "vehicle_controller.h"
#include "nav_config.h"
#include "geo_math.h"
#include "hal.h"

static double s_last_command_deg = 0.0;   /* last steering command sent to the motor */

/* Converts the heading error into a rate-limited steering command and sends it to the motor. */
void VehicleController_SetDirection(double current_heading, double target_heading)
{
    double error = wrap180(target_heading - current_heading);
    double command = clamp(error,
                           s_last_command_deg - MAX_TURN_RATE_DEG,
                           s_last_command_deg + MAX_TURN_RATE_DEG);

    command = clamp(command, -30.0, 30.0);
    s_last_command_deg = command;
    motor_set_steering((int16_t)(command * 10.0));
}

/* Centres the steering and signals that the route is no longer being followed. */
void VehicleController_Hold(void)
{
    s_last_command_deg = 0.0;
    motor_set_steering(0);
    led_set_status(0);
}
