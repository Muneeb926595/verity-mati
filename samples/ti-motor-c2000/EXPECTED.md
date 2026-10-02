# ti-motor-c2000 — hand-verified answers

| Question | Correct answer |
|---|---|
| Which interrupts does the code use? | `adc_isr` (registered as `PieVectTable.ADCINT1`, main.c:18) and `epwm1_isr` (`PieVectTable.EPWM1_INT`, main.c:19). |
| Where does the phase current come from? | ADC result register `AdcResult.ADCRESULT0` read in `adc_isr` (adc.c:6), scaled `raw * CURRENT_SCALE - CURRENT_OFFSET` into `g_phaseCurrent` (adc.c:7). |
| Who calls CurrentLoop? | `epwm1_isr` (control.c:31), only when `g_pwmEnabled` is set. It runs from the PWM interrupt, not from main. |
| What happens on overcurrent? | `CheckFaults` (main loop) sees `g_phaseCurrent > OVERCURRENT_A` (12 A) or `< -12 A` → sets `FAULT_OVERCURRENT` in `g_faultFlags` and calls `TripPWM` → one-shot trip `EPwm1Regs.TZFRC.bit.OST = 1`, `g_pwmEnabled = 0` (so `epwm1_isr` stops calling `CurrentLoop`), fault LED `GpioDataRegs.GPASET.bit.GPIO31 = 1`. |
| What happens when the phase current is 13 A? | Same as overcurrent: 13 > 12 → fault flag set, PWM tripped, LED on. |
| What does CurrentLoop do? | PI controller: `error = g_currentRef - g_phaseCurrent`, integral `+= KI*error` clamped to [0,1], `duty = KP*error + integral` clamped to [0, 0.95], written to `EPwm1Regs.CMPA.half.CMPA = duty * PWM_PERIOD`. |
