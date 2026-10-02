#include "motor.h"

volatile float32 g_phaseCurrent = 0.0f;
volatile float32 g_currentRef   = 2.0f;
volatile Uint16  g_faultFlags   = 0;
volatile Uint16  g_pwmEnabled   = 0;

void main(void)
{
    InitSysCtrl();
    DINT;
    InitPieCtrl();
    IER = 0x0000;
    IFR = 0x0000;
    InitPieVectTable();

    EALLOW;
    PieVectTable.ADCINT1   = &adc_isr;     /* ADC conversion complete */
    PieVectTable.EPWM1_INT = &epwm1_isr;   /* PWM period: run the current loop */
    EDIS;

    InitAdc();
    InitMotorPwm();
    PieCtrlRegs.PIEIER1.bit.INTx1 = 1;
    PieCtrlRegs.PIEIER3.bit.INTx1 = 1;
    IER |= M_INT1 | M_INT3;
    EINT;
    ERTM;

    for (;;) {
        CheckFaults();
    }
}
