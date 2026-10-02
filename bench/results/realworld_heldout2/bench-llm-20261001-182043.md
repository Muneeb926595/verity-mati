# Verity benchmark — llm mode (20261001-182043)

Cases: 12 · passed: 12 · mean score: 1.0 · mean time per question: 19.99 s

| metric | result |
|---|---|
| symbol identification | 11/11 passed · accuracy 1.000 |
| file line references | 1/1 passed · accuracy 1.000 |
| no invention traps | 1/1 passed · accuracy 1.000 |
| unsupported claims (rejected by verifier) | 2 of 125 (1.60%) — removed before display |
| hallucinated code names in final answers | 0 case(s) |
| citation precision (claims whose cited line re-verified) | 123/125 (98.40%) |
| answer paragraphs without any evidence | 0 of 72 |

## Cases

| id | category | intent | passed | score | failing checks |
|---|---|---|---|---|---|
| lfs-verify-mismatch | scenario | failure_invalid | ✓ | 1.0 |  |
| lfs-no-space | scenario | failure_invalid | ✓ | 1.0 |  |
| lfs-name-too-long | scenario | failure_invalid | ✓ | 1.0 |  |
| lfs-bad-version | scenario | failure_invalid | ✓ | 1.0 |  |
| lfs-crc-fn | symbol | where_computed | ✓ | 1.0 |  |
| lfs-erase-callers | callpath | callers | ✓ | 1.0 |  |
| lfs-prog-target | callpath | explain | ✓ | 1.0 |  |
| lfs-block-count | symbol | where_computed | ✓ | 1.0 |  |
| lfs-flush-callers | callpath | callers | ✓ | 1.0 |  |
| lfs-sync | symbol | explain | ✓ | 1.0 |  |
| lfs-cache-drop | symbol | where_computed | ✓ | 1.0 |  |
| lfs-invented | honesty | explain | ✓ | 1.0 |  |
