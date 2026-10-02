#include "motor.h"

/* ADC end-of-conversion: converts the phase-current sample to amps. */
interrupt void adc_isr(void)
{
    Uint16 raw = AdcResult.ADCRESULT0;
    g_phaseCurrent = (float32)raw * CURRENT_SCALE - CURRENT_OFFSET;
    AdcRegs.ADCINTFLGCLR.bit.ADCINT1 = 1;      /* clear the ADC interrupt flag */
    PieCtrlRegs.PIEACK.all = PIEACK_GROUP1;    /* acknowledge the PIE group */
}
