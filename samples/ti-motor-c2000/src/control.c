#include "motor.h"

#pragma CODE_SECTION(CurrentLoop, "ramfuncs");

static float32 s_integral = 0.0f;

/* PI current controller: runs every PWM period and sets the duty cycle. */
void CurrentLoop(void)
{
    float32 error = g_currentRef - g_phaseCurrent;
    s_integral += KI * error;
    if (s_integral > 1.0f) {
        s_integral = 1.0f;
    } else if (s_integral < 0.0f) {
        s_integral = 0.0f;
    }
    float32 duty = KP * error + s_integral;
    if (duty > 0.95f) {
        duty = 0.95f;
    }
    if (duty < 0.0f) {
        duty = 0.0f;
    }
    EPwm1Regs.CMPA.half.CMPA = (Uint16)(duty * PWM_PERIOD);
}

interrupt void epwm1_isr(void)
{
    if (g_pwmEnabled) {
        CurrentLoop();
    }
    EPwm1Regs.ETCLR.bit.INT = 1;
    PieCtrlRegs.PIEACK.all = PIEACK_GROUP3;
}

void InitMotorPwm(void)
{
    EALLOW;
    EPwm1Regs.TBPRD = PWM_PERIOD;
    EPwm1Regs.TBCTL.bit.CTRMODE = TB_COUNT_UPDOWN;
    EPwm1Regs.ETSEL.bit.INTEN = 1;
    EDIS;
    g_pwmEnabled = 1;
}
