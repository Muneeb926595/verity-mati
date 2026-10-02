# ti-motor-c2000 — TI C2000-style test project (uncompiled on purpose)

A small motor current controller in the style of TI C2000 code: `interrupt` functions registered in `PieVectTable`,
`#pragma CODE_SECTION`, `EALLOW/EDIS`, `Uint16/float32` types and register blocks (`AdcResult`, `EPwm1Regs`,
`GpioDataRegs`, `PieCtrlRegs`) that come from TI device headers (`DSP28x_Project.h`) which are NOT in the repository.
Answers to test questions are in `EXPECTED.md`.
