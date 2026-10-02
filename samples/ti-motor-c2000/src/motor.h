#ifndef MOTOR_H
#define MOTOR_H
#include "DSP28x_Project.h"     /* TI device support: not part of this repository */

#define CURRENT_SCALE      0.00805f   /* amps per ADC count */
#define CURRENT_OFFSET     16.5f      /* amps at mid-scale */
#define OVERCURRENT_A      12.0f      /* trip above this phase current */
#define PWM_PERIOD         1500       /* TBPRD for 20 kHz */
#define KP                 0.8f
#define KI                 0.02f
#define FAULT_OVERCURRENT  0x0001u
#define FAULT_OVERTEMP     0x0002u

extern volatile float32 g_phaseCurrent;   /* latest measured phase current, amps */
extern volatile float32 g_currentRef;     /* requested current, amps */
extern volatile Uint16  g_faultFlags;     /* FAULT_* bits, latched until reset */
extern volatile Uint16  g_pwmEnabled;

interrupt void adc_isr(void);
interrupt void epwm1_isr(void);
void CurrentLoop(void);
void TripPWM(void);
void CheckFaults(void);
void InitMotorPwm(void);
#endif
