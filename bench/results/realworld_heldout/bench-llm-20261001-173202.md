# Verity benchmark — llm mode (20261001-173202)

Cases: 14 · passed: 14 · mean score: 1.0 · mean time per question: 10.45 s

| metric | result |
|---|---|
| symbol identification | 13/13 passed · accuracy 1.000 |
| file line references | 1/1 passed · accuracy 1.000 |
| no invention traps | 1/1 passed · accuracy 1.000 |
| unsupported claims (rejected by verifier) | 0 of 134 (0.00%) — removed before display |
| hallucinated code names in final answers | 0 case(s) |
| citation precision (claims whose cited line re-verified) | 134/134 (100.00%) |
| answer paragraphs without any evidence | 0 of 61 |

## Cases

| id | category | intent | passed | score | failing checks |
|---|---|---|---|---|---|
| mb-bad-crc | scenario | failure_invalid | ✓ | 1.0 |  |
| mb-crc-fn | symbol | where_computed | ✓ | 1.0 |  |
| mb-crc-callers | callpath | callers | ✓ | 1.0 |  |
| mb-integrity-callers | callpath | callers | ✓ | 1.0 |  |
| mb-timeout | scenario | failure_missing | ✓ | 1.0 |  |
| mb-bad-baud | scenario | explain | ✓ | 1.0 |  |
| mb-slave-set | symbol | where_computed | ✓ | 1.0 |  |
| mb-illegal-address | scenario | failure_invalid | ✓ | 1.0 |  |
| mb-read-registers | symbol | explain | ✓ | 1.0 |  |
| mb-too-many-registers | scenario | failure_invalid | ✓ | 1.0 |  |
| mb-timeout-set | symbol | where_computed | ✓ | 1.0 |  |
| mb-serial-write | symbol | which_fn | ✓ | 1.0 |  |
| mb-backend-select | callpath | explain | ✓ | 1.0 |  |
| mb-invented | honesty | explain | ✓ | 1.0 |  |
