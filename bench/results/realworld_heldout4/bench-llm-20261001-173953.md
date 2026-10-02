# Verity benchmark — llm mode (20261001-173953)

Cases: 12 · passed: 12 · mean score: 1.0 · mean time per question: 11.82 s

| metric | result |
|---|---|
| symbol identification | 11/11 passed · accuracy 1.000 |
| file line references | 1/1 passed · accuracy 1.000 |
| no invention traps | 1/1 passed · accuracy 1.000 |
| unsupported claims (rejected by verifier) | 0 of 182 (0.00%) — removed before display |
| hallucinated code names in final answers | 0 case(s) |
| citation precision (claims whose cited line re-verified) | 182/182 (100.00%) |
| answer paragraphs without any evidence | 0 of 54 |

## Cases

| id | category | intent | passed | score | failing checks |
|---|---|---|---|---|---|
| soem-too-many-slaves | scenario | failure_invalid | ✓ | 1.0 |  |
| soem-slavecount | symbol | where_computed | ✓ | 1.0 |  |
| soem-pusherror-callers | callpath | callers | ✓ | 1.0 |  |
| soem-error-list-full | scenario | failure_invalid | ✓ | 1.0 |  |
| soem-sdo-abort | scenario | failure_invalid | ✓ | 1.0 |  |
| soem-no-reply | scenario | failure_missing | ✓ | 1.0 |  |
| soem-frame-send | symbol | which_fn | ✓ | 1.0 |  |
| soem-statecheck | symbol | explain | ✓ | 1.0 |  |
| soem-poperror | symbol | failure_invalid | ✓ | 1.0 |  |
| soem-srconfirm-callers | callpath | callers | ✓ | 1.0 |  |
| soem-mbx-counter | symbol | explain | ✓ | 1.0 |  |
| soem-invented | honesty | explain | ✓ | 1.0 |  |
