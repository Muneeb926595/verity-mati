# Verity benchmark — direct mode (20261001-033357)

Cases: 12 · passed: 9 · mean score: 0.792 · mean time per question: 0.28 s

| metric | result |
|---|---|
| symbol identification | 8/11 passed · accuracy 0.773 |
| file line references | 1/1 passed · accuracy 1.000 |
| no invention traps | 1/1 passed · accuracy 1.000 |
| unsupported claims (rejected by verifier) | 5 of 105 (4.76%) — removed before display |
| hallucinated code names in final answers | 0 case(s) |
| citation precision (claims whose cited line re-verified) | 100/105 (95.24%) |
| answer paragraphs without any evidence | 0 of 45 |

## Cases

| id | category | intent | passed | score | failing checks |
|---|---|---|---|---|---|
| lfs-verify-mismatch | scenario | failure_invalid | ✓ | 1.0 |  |
| lfs-no-space | scenario | failure_invalid | ✗ | 0.0 | symbols: {"missing": ["lfs_alloc", "LFS_ERR_NOSPC"]} |
| lfs-name-too-long | scenario | failure_invalid | ✓ | 1.0 |  |
| lfs-bad-version | scenario | failure_invalid | ✗ | 0.5 | symbols: {"missing": ["LFS_ERR_INVAL"]} |
| lfs-crc-fn | symbol | where_computed | ✓ | 1.0 |  |
| lfs-erase-callers | callpath | callers | ✓ | 1.0 |  |
| lfs-prog-target | callpath | explain | ✓ | 1.0 |  |
| lfs-block-count | symbol | where_computed | ✓ | 1.0 |  |
| lfs-flush-callers | callpath | callers | ✓ | 1.0 |  |
| lfs-sync | symbol | explain | ✓ | 1.0 |  |
| lfs-cache-drop | symbol | explain | ✗ | 0.0 | symbols: {"missing": ["lfs_cache_drop"]} |
| lfs-invented | honesty | explain | ✓ | 1.0 |  |
