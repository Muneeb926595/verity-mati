# msvc-telemetry — MSVC/Win32-style test project (uncompiled on purpose)

A small Windows telemetry service in the style of real MSVC code: `windows.h`, SAL annotations, `__declspec(dllexport)`,
`__stdcall`, `__int64`, `#pragma pack`, `CreateThread` worker threads, a COM-port read (`ReadFile`), a CRC-checked
frame parser, a handler dispatch table, a watchdog, an ini-file calibration read at run time, and an optional
`USE_LEGACY_PROTOCOL` build switch. It has a `.vcxproj` but is analysed WITHOUT Windows headers or a compiler
(Verity reconstructs the build). Answers to test questions are in `EXPECTED.md`.
