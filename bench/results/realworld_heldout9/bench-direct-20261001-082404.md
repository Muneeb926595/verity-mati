# Verity benchmark — direct mode (20261001-082404)

Cases: 12 · passed: 7 · mean score: 0.75 · mean time per question: 0.05 s

| metric | result |
|---|---|
| symbol identification | 6/11 passed · accuracy 0.727 |
| no invention traps | 1/1 passed · accuracy 1.000 |
| unsupported claims (rejected by verifier) | 15 of 117 (12.82%) — removed before display |
| hallucinated code names in final answers | 0 case(s) |
| citation precision (claims whose cited line re-verified) | 102/117 (87.18%) |
| answer paragraphs without any evidence | 0 of 45 |

## Cases

| id | category | intent | passed | score | failing checks |
|---|---|---|---|---|---|
| nm-slave-busy | scenario | failure_invalid | ✗ | 0.5 | symbols: {"missing": ["SlaveDeviceBusy"]} |
| nm-acknowledge | scenario | failure_invalid | ✗ | 0.5 | symbols: {"missing": ["Acknowledge"]} |
| nm-wrong-function | scenario | failure_invalid | ✓ | 1.0 |  |
| nm-wrong-slave | scenario | failure_invalid | ✓ | 1.0 |  |
| nm-timeout | scenario | failure_missing | ✓ | 1.0 |  |
| nm-checksum | scenario | failure_invalid | ✓ | 1.0 |  |
| nm-wait-negative | scenario | failure_invalid | ✗ | 0.5 | symbols: {"missing": ["ArgumentException"]} |
| nm-validate-callers | callpath | callers | ✗ | 0.0 | symbols: {"missing": ["UnicastMessage"]} |
| nm-exception-detect | symbol | explain | ✗ | 0.5 | symbols: {"missing": ["ExceptionOffset"]} |
| nm-retries-set | symbol | where_computed | ✓ | 1.0 |  |
| nm-crc-calc | symbol | where_computed | ✓ | 1.0 |  |
| nm-invented | honesty | explain | ✓ | 1.0 |  |
