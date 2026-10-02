# msvc-telemetry — hand-verified answers

| Question | Correct answer (file:line) |
|---|---|
| Where does the temperature value come from? | Serial port: `ReadFile` in `ReadSerial` (src/serial_port.c:34) → `ReaderThread` buffer → `ParseFrame` copies it into `TELEMETRY_FRAME.temperatureCenti` (memcpy, frame_parser.c:21) → `DispatchFrame` → `OnTemperature` (dispatcher.c:24) computes `temperatureCenti / 100.0 + g_calibrationOffsetC` → `g_temperatureC`. The calibration offset comes from the ini file (`GetPrivateProfileIntA`, main.c:8). |
| Which threads does the program start? | `ReaderThread` and `WatchdogThread`, both via `CreateThread` in `wmain` (main.c:14-15). |
| What happens if the serial link goes silent? | `WatchdogThread` (worker.c:36) wakes every second; when `g_linkState == LINK_UP && now - g_lastFrameTicks > FRAME_TIMEOUT_MS` (3000 ms) it sets `g_linkState = LINK_LOST` and logs "telemetry link lost". `Telemetry_GetTemperature` then returns FALSE. |
| What happens when a frame has a bad CRC? | `ParseFrame` compares `out->crc` with `Crc32(...)`; on mismatch `g_stats.crcErrors++` and returns FALSE (frame_parser.c:26-29); `ReaderThread` then skips the frame (no dispatch, no `framesOk`, `g_lastFrameTicks` not updated). |
| Who calls OnTemperature? | `DispatchFrame` through the handler table `g_handlers` (dispatcher.c:3, call at :14). |
| What happens when the temperature is 85.5 degrees? | Above `ALARM_THRESHOLD_C` (80.0): `RaiseAlarm(85.5)` → `g_alarmActive` 0→1 via `InterlockedExchange`, `SendAlarmMessage` writes "ALARM over-temperature 85.5 C" to the alarm pipe (`WriteFile`), `LogEvent`. `g_temperatureC = 85.5` (calibration offset 0). The raw field is `temperatureCenti = 8550`. |
| What does Telemetry_GetTemperature do? | Exported (`__declspec(dllexport)`, `__stdcall`): returns FALSE if the pointer is NULL or `g_linkState != LINK_UP`, otherwise copies `g_temperatureC` to `*temperatureC` and returns TRUE (api.c:4). |
| What is g_alarmActive for? | 1 while the over-temperature alarm is raised; set by `RaiseAlarm` / cleared by `ClearAlarm` (InterlockedExchange), read by `OnTemperature` to decide whether to clear. |
| What changes when USE_LEGACY_PROTOCOL is defined? | `ParseFrame` multiplies `temperatureCenti` by 10 (legacy tenths) before the CRC check (frame_parser.c:22-25). |
