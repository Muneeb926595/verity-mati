# Verity benchmark — direct mode (20261001-023649)

Cases: 14 · passed: 8 · mean score: 0.75 · mean time per question: 0.04 s

| metric | result |
|---|---|
| symbol identification | 10/14 passed · accuracy 0.786 |
| file line references | 0/1 passed · accuracy 0.000 |
| unsupported claims (rejected by verifier) | 8 of 129 (6.20%) — removed before display |
| hallucinated code names in final answers | 1 case(s) |
| citation precision (claims whose cited line re-verified) | 121/129 (93.80%) |
| answer paragraphs without any evidence | 1 of 37 |

## Cases

| id | category | intent | passed | score | failing checks |
|---|---|---|---|---|---|
| rw-minmea-lat | dataflow | origin | ✓ | 1.0 |  |
| rw-minmea-checksum | symbol | failure_invalid | ✗ | 0.5 | symbols: {"missing": ["hex2int"]} |
| rw-minmea-callers-check | callpath | callers | ✓ | 1.0 |  |
| rw-minmea-invalid | scenario | failure_invalid | ✗ | 0.5 | symbols: {"missing": ["MINMEA_INVALID"]} |
| rw-minmea-rmc | symbol | explain | ✗ | 1.0 | hallucinated: variation_directi |
| rw-minmea-tocoord | symbol | which_fn | ✓ | 1.0 |  |
| rw-rtos-delay | symbol | explain | ✓ | 1.0 |  |
| rw-rtos-callers | callpath | callers | ✓ | 1.0 |  |
| rw-rtos-queue-full | scenario | failure_invalid | ✗ | 0.0 | symbols: {"missing": ["errQUEUE_FULL", "xTasksWaitingToSend"]} |
| rw-rtos-tick | symbol | search | ✗ | 0.5 | citations: {"missing": ["tasks.c:4856"]} |
| rw-rtos-isrs | symbol | entries | ✗ | 0.0 | symbols: {"missing": ["xPortSysTickHandler", "xPortPendSVHandler", "vPortSVCHandler"]} |
| rw-rtos-malloc | scenario | failure_invalid | ✓ | 1.0 |  |
| rw-rtos-impact | callpath | impact | ✓ | 1.0 |  |
| rw-rtos-tickcount | symbol | explain | ✓ | 1.0 |  |
