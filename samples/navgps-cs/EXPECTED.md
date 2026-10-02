# navgps-cs — ground truth for extractor validation

Hand-verified facts about this sample (paths relative to `samples/navgps-cs`, lines 1-based).
Symbol keys use the Verity C# format (Roslyn DocumentationCommentId without a return-type suffix;
`<method>#param:<name>`, `<method>#local:<name>@<line>`, `<method>#lambda@<line>:<col>`).
`workers/roslyn/tests/verify.py` checks every item below against the extractor's output.

The project builds with `dotnet build` (net10.0, no packages) and has 0 compile errors, so every
file should come out with fidelity `F2`.

## 1. Callers of `NavigationController.ProcessGpsData`
Key: `M:Nav.NavigationController.ProcessGpsData(Nav.GpsFix)`, defined at `src/Navigation/NavigationController.cs:38`.

| caller | call site | guards on the call |
|---|---|---|
| `M:Nav.Services.GpsService.OnFix(System.Object,Nav.GpsFixEventArgs)` | `src/Services/GpsService.cs:34` | `Enabled && fix.Quality >= _options.MinQuality` (if, polarity true, line 32) |
| `M:Nav.Program.RunSelfTest(Nav.NavigationController)` | `src/Program.cs:69` | none |

Exactly these two direct (`kind: direct`, `res: exact`) call edges exist.
Argument binding: `OnFix`'s local `fix` (`…OnFix(…)#local:fix@31`) flows to `…ProcessGpsData(Nav.GpsFix)#param:fix` (flow kind `arg`), and so does `RunSelfTest`'s local `fix@60`.

## 2. Implementations of `IHeadingFilter`
Interface `T:Nav.IHeadingFilter` at `src/Navigation/HeadingFilters.cs:6`; member `M:Nav.IHeadingFilter.Update(System.Double,System.Double)` at line 9.

| implementation | class line | `Update` line |
|---|---|---|
| `T:Nav.ComplementaryHeadingFilter` | `src/Navigation/HeadingFilters.cs:16` | 27 |
| `T:Nav.KalmanHeadingFilter` | `src/Navigation/HeadingFilters.cs:48` | 61 |

Expected `inherit` records: `{sub: T:Nav.ComplementaryHeadingFilter, base: T:Nav.IHeadingFilter, kind: implements}`, the same for Kalman, plus
method level `{sub: M:Nav.ComplementaryHeadingFilter.Update(System.Double,System.Double), base: M:Nav.IHeadingFilter.Update(System.Double,System.Double), kind: implements}` and the same for Kalman (and for `Reset(System.Double)`).

The interface call site: `src/Navigation/NavigationController.cs:67`, `_headingFilter.Update(fix.CourseDeg, dt)`, `kind: interface`, `res: cha`, receiver `F:Nav.NavigationController._headingFilter`.
The concrete types are created in `CreateHeadingFilter` (switch on `options.Filter`): `new KalmanHeadingFilter(...)` at line 28 (case `options.Filter == HeadingFilterKind.Kalman`) and `new ComplementaryHeadingFilter(...)` at line 30 (default).

## 3. Writers of `NavState.CurrentHeading`
Key `P:Nav.NavState.CurrentHeading` (declared `src/Navigation/NavState.cs:15`).

| writer | line | guards |
|---|---|---|
| `M:Nav.NavigationController.ProcessGpsData(Nav.GpsFix)` | `src/Navigation/NavigationController.cs:67` | early_exit `fix.Hdop > _options.MaxHdop` (false, line 40); early_exit `!GeoMath.IsValidPosition(fix.LatitudeDeg, fix.LongitudeDeg)` (false, line 46); early_exit `speedMps > _options.MaxSpeedMps` (false, line 53); if `fix.SpeedKnots >= _options.MinSpeedForHeadingKnots` (true, line 65) |
| `M:Nav.NavigationController.ApplyManualHeading(System.Double)` | `src/Navigation/NavigationController.cs:80` | none |

The value written at line 67 comes from the interface call: flow `M:Nav.IHeadingFilter.Update(System.Double,System.Double)#ret → P:Nav.NavState.CurrentHeading` (kind `return_to`).
Readers: `NavLoop.Tick` (`src/Control/NavLoop.cs:42`) and `Program.Main` (`src/Program.cs:44`).
`NavState.LastFixUtc` is written only at `src/Navigation/NavigationController.cs:70` (same three early-exit guards).

## 4. Event wiring
- Event `E:Nav.Sensors.SerialGpsSensor.FixReceived` declared at `src/Sensors/SerialGpsSensor.cs:20`.
- Subscribed: `_sensor.FixReceived += OnFix;` at `src/Services/GpsService.cs:19` (inside the `GpsService` constructor); handler `M:Nav.Services.GpsService.OnFix(System.Object,Nav.GpsFixEventArgs)` → `entry` kind `event`.
- Unsubscribed: `-=` at `src/Services/GpsService.cs:47` (`Dispose`).
- Raised: `FixReceived?.Invoke(this, new GpsFixEventArgs(fix))` at `src/Sensors/SerialGpsSensor.cs:75` → `call` kind `event`, `via: E:Nav.Sensors.SerialGpsSensor.FixReceived`.
- Chain: `SerialGpsSensor.Pump` (25) → `HandleLine` (34) → `OnFixReceived` (69) → event → `GpsService.OnFix` → `ProcessGpsData` (GpsService.cs:34).
- `Console.CancelKeyPress += (_, e) => …` at `src/Program.cs:32` (lambda handler).

## 5. Guards / branches
- `ProcessGpsData`: three early exits (lines 40, 46, 53) → `branch` records with `kind: early_exit`; the heading `if` at line 65 → `kind: if`.
- `GpsService.OnFix`: `if (Enabled && fix.Quality >= _options.MinQuality)` at line 32 (the ProcessGpsData call is under polarity true; the `else` branch increments `_rejected`).
- `NavLoop.Tick` (`src/Control/NavLoop.cs:25`): early exit `_state.Mode == NavMode.Initializing` (line 30, polarity false for everything after it); stale-fix check `now - _state.LastFixUtc > _options.StaleFixTimeout` (line 33) guarding the write of `NavState.Mode` at line 37; clamp writes of local `bank` at line 46 (`bank > _options.MaxBankDeg`, true) and line 48 (else of line 45 and `bank < -_options.MaxBankDeg`, true); `bank *= 0.5` at line 51 under `_state.Mode == NavMode.DeadReckoning`.
- `SerialGpsSensor.HandleLine`: early exits at lines 42 (`line.Length == 0`), 45 (checksum), 51 (GGA block, returns), 62 (`fix == null`); the write `fix.Hdop = _lastHdop` at line 65 carries all of them.

## 6. Entry points
- `M:Nav.Program.Main(System.String[])` (`src/Program.cs:10`) — `main`, res `exact`.
- `M:Nav.Services.GpsService.OnFix(System.Object,Nav.GpsFixEventArgs)` — `event`.
- Timer lambda `M:Nav.Program.Main(System.String[])#lambda@38:37` (`new Timer(_ => loop.Tick(DateTime.UtcNow), …)`) — `timer`; it calls `M:Nav.Control.NavLoop.Tick(System.DateTime)`.
- `Task.Run` lambda `M:Nav.Program.Main(System.String[])#lambda@39:35` — `thread`; it calls `SerialGpsSensor.Pump`.
- `Console.CancelKeyPress` lambda at line 32 — `handler`.

## 7. I/O sites
| dir | api | location |
|---|---|---|
| sink | `System.Net.Sockets.UdpClient.Send` | `src/Control/AutopilotLink.cs:32` (UdpAutopilotTransport.Send) |
| sink | `System.IO.Stream.Write` | `src/Control/AutopilotLink.cs:53` (StreamAutopilotTransport.Send) |
| source | `System.IO.File.ReadAllLines` | `src/Sensors/ILineSource.cs:35` |
| source | `System.IO.StreamReader.ReadLine` | `src/Sensors/ILineSource.cs:22` |
| source | `System.Console.OpenStandardInput` | `src/Program.cs:22` |

`AutopilotLink.SendBankCommand` (`src/Control/AutopilotLink.cs:72`) reaches both sinks through the virtual call `_transport.Send(payload)` at line 79 (`kind: virtual`; `UdpAutopilotTransport.Send` and `StreamAutopilotTransport.Send` both `overrides` `AutopilotTransport.Send`). It is called from `NavLoop.Tick` at `src/Control/NavLoop.cs:54`.
