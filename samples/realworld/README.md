# Real open-source C projects (validation data — not written by Verity's author)

| Project | Upstream | Commit | License | Notes |
|---|---|---|---|---|
| minmea | github.com/kosma/minmea | c43c9e7 (2026-09-20) | WTFPL / MIT (see LICENSE) | small NMEA parser |
| FreeRTOS-Kernel | github.com/FreeRTOS/FreeRTOS-Kernel | 8be86d4 (2026-08-26) | MIT | trimmed to one port (portable/GCC/ARM_CM4F + MemMang/heap_4.c) as a real project would be; **no FreeRTOSConfig.h** (as shipped), so it is analysed uncompiled |
| lwip | github.com/lwip-tcpip/lwip | d08f477 (2026-09-01) | BSD-3-Clause (see COPYING) | full tree incl. tests and apps (~170K lines) |
| libmodbus | github.com/stephane/libmodbus | a9b025d (2026-07-02) | LGPL-2.1 (see COPYING.LESSER) | **held-out**: added after the lwIP fixes and never tuned on (`bench/realworld_heldout.json`); autotools, no config.h (uncompiled) |
| littlefs | github.com/littlefs-project/littlefs | 6cb4e86 (2026-03-24) | BSD-3-Clause (see LICENSE.md) | **second held-out**: added after the libmodbus fixes; tuned-on after its first run (`bench/realworld_heldout2.json`) |
| CANopenNode | github.com/CANopenNode/CANopenNode | 9b8beed (2026-07-10) | Apache-2.0 (see LICENSE) | **third held-out** (Phase 3): industrial CANopen stack, only the blank driver template (`bench/realworld_heldout3.json`); tuned-on after its first run |
| SOEM | github.com/OpenEtherCATsociety/SOEM | 88e8ed4 (2026-09-08) | GPLv3 / commercial dual licence (see LICENSE.md) | **fourth held-out** (Phase 3): EtherCAT master with Linux + Windows (wpcap) ports; generated ec_options.h missing (`bench/realworld_heldout4.json`) |
| uC-OS2 | github.com/weston-embedded/uC-OS2 | bfc6120 (2023-01-05) | Apache-2.0 (see LICENSE) | **fifth held-out** (Phase 4): uC/OS-II RTOS kernel trimmed to the kernel, the os_cfg.h template and one port (Win32/Visual Studio — Windows/MSVC-style code); `ucos_ii.c` #includes every kernel .c file (`bench/realworld_heldout5.json`) |
| paho-mqtt | github.com/eclipse-paho/paho.mqtt.embedded-c | 6035ea2 (2026-07-03) | EPL-2.0 / EDL-1.0 (see LICENSE) | **sixth held-out** (Phase 4): MQTT packet library + C client with the Linux port; C++ client and vendor-SDK ports removed; platform header chosen by an undefined build macro (`bench/realworld_heldout6.json`) |
| m2mqtt | github.com/nanoframework/nanoFramework.m2mqtt | 3721733 (2026-09-24) | EPL-1.0 (see LICENSE.md) | **seventh held-out** (Phase 4), first real **C#** project: MQTT client for .NET nanoFramework, shipped tree incl. tests (`bench/realworld_heldout7.json`) |
| nanopb | github.com/nanopb/nanopb | 2c88fc8 (2026-09-26) | zlib (see LICENSE.txt) | **eighth held-out** (Phase 4 final round): Protocol Buffers core library + two example applications; generated `*.pb.h` absent (`bench/realworld_heldout8.json`) |
| nmodbus | github.com/NModbus/NModbus | baddfca (2026-06-30) | MIT (see LICENSE.txt) | **ninth held-out** (Phase 4 final round), C#: Modbus RTU/ASCII/TCP/UDP library, shipped tree incl. unit and integration tests (`bench/realworld_heldout9.json`) |
| cJSON | github.com/DaveGamble/cJSON | c859b25 (2025-09-09) | MIT (see LICENSE) | **tenth held-out** (Phase 5): JSON parser/printer library + cJSON_Utils (JSON Pointer/Patch); bundled test framework removed; questions written before Verity was run (`bench/realworld_heldout10.json`) |

Used only to validate Verity (`bench/realworld.json`, `bench/realworld_lwip.json`, `bench/realworld_heldout.json`, `bench/realworld_heldout2.json`, `bench/realworld_heldout3.json`, `bench/realworld_heldout4.json`, `bench/realworld_heldout5.json` … `bench/realworld_heldout9.json`, `bench/realworld_heldout10.json`). Git metadata was removed.
