# Verity benchmark — direct mode (20261001-044441)

Cases: 12 · passed: 5 · mean score: 0.5 · mean time per question: 0.05 s

| metric | result |
|---|---|
| symbol identification | 4/11 passed · accuracy 0.455 |
| file line references | 0/1 passed · accuracy 0.000 |
| no invention traps | 1/1 passed · accuracy 1.000 |
| unsupported claims (rejected by verifier) | 0 of 164 (0.00%) — removed before display |
| hallucinated code names in final answers | 0 case(s) |
| citation precision (claims whose cited line re-verified) | 164/164 (100.00%) |
| answer paragraphs without any evidence | 0 of 42 |

## Cases

| id | category | intent | passed | score | failing checks |
|---|---|---|---|---|---|
| soem-too-many-slaves | scenario | failure_invalid | ✓ | 1.0 |  |
| soem-slavecount | symbol | where_computed | ✗ | 0.0 | symbols: {"missing": ["ecx_detect_slaves"]}; citations: {"missing": ["src/ec_config.c:78"]} |
| soem-pusherror-callers | callpath | callers | ✓ | 1.0 |  |
| soem-error-list-full | scenario | failure_invalid | ✗ | 0.5 | symbols: {"missing": ["tail"]} |
| soem-sdo-abort | scenario | failure_invalid | ✓ | 1.0 |  |
| soem-no-reply | scenario | failure_missing | ✗ | 0.0 | symbols: {"missing": ["ecx_srconfirm", "EC_NOFRAME"]} |
| soem-frame-send | symbol | which_fn | ✗ | 0.0 | symbols: {"missing": ["ecx_outframe"]} |
| soem-statecheck | symbol | explain | ✗ | 0.5 | symbols: {"missing": ["osal_timer_is_expired"]} |
| soem-poperror | symbol | failure_invalid | ✗ | 0.0 | symbols: {"missing": ["ecaterror"]} |
| soem-srconfirm-callers | callpath | callers | ✓ | 1.0 |  |
| soem-mbx-counter | symbol | explain | ✗ | 0.0 | symbols: {"missing": ["ec_nextmbxcnt"]} |
| soem-invented | honesty | explain | ✓ | 1.0 |  |
