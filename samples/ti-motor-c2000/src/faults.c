#include "motor.h"

/* Forces both PWM outputs low and stops the current loop. */
void TripPWM(void)
{
    EALLOW;
    EPwm1Regs.TZFRC.bit.OST = 1;     /* one-shot trip: outputs forced low */
    EDIS;
    g_pwmEnabled = 0;
    GpioDataRegs.GPASET.bit.GPIO31 = 1;   /* fault LED on */
}

/* Background fault monitor, called from the main loop. */
void CheckFaults(void)
{
    if (g_phaseCurrent > OVERCURRENT_A || g_phaseCurrent < -OVERCURRENT_A) {
        g_faultFlags |= FAULT_OVERCURRENT;
        TripPWM();
    }
}
