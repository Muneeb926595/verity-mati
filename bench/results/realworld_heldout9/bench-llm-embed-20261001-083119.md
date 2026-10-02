# Verity benchmark — llm-embed mode (20261001-083119)

Cases: 12 · passed: 9 · mean score: 0.833 · mean time per question: 10.45 s

| metric | result |
|---|---|
| symbol identification | 8/11 passed · accuracy 0.818 |
| no invention traps | 1/1 passed · accuracy 1.000 |
| unsupported claims (rejected by verifier) | 15 of 103 (14.56%) — removed before display |
| hallucinated code names in final answers | 0 case(s) |
| citation precision (claims whose cited line re-verified) | 88/103 (85.44%) |
| answer paragraphs without any evidence | 0 of 49 |

## Cases

| id | category | intent | passed | score | failing checks |
|---|---|---|---|---|---|
| nm-slave-busy | scenario | failure_invalid | ✓ | 1.0 |  |
| nm-acknowledge | scenario | failure_invalid | ✓ | 1.0 |  |
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
