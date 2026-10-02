# Verity benchmark — llm mode (20261001-083405)

Cases: 12 · passed: 9 · mean score: 0.75 · mean time per question: 12.9 s

| metric | result |
|---|---|
| symbol identification | 8/11 passed · accuracy 0.727 |
| no invention traps | 1/1 passed · accuracy 1.000 |
| unsupported claims (rejected by verifier) | 0 of 140 (0.00%) — removed before display |
| hallucinated code names in final answers | 0 case(s) |
| citation precision (claims whose cited line re-verified) | 140/140 (100.00%) |
| answer paragraphs without any evidence | 0 of 46 |

## Cases

| id | category | intent | passed | score | failing checks |
|---|---|---|---|---|---|
| np-stream-full | scenario | failure_invalid | ✓ | 1.0 |  |
| np-end-of-stream | scenario | failure_invalid | ✓ | 1.0 |  |
| np-missing-required | scenario | failure_missing | ✗ | 0.0 | symbols: {"missing": ["pb_decode_inner", "missing required field"]} |
| np-varint-overflow | scenario | failure_invalid | ✓ | 1.0 |  |
| np-wrong-wire-type | scenario | failure_invalid | ✓ | 1.0 |  |
| np-max-depth | scenario | failure_invalid | ✗ | 0.0 | symbols: {"missing": ["pb_make_string_substream", "PB_MESSAGE_NESTING_MAX"]} |
| np-array-max | scenario | failure_invalid | ✓ | 1.0 |  |
| np-zero-tag | scenario | failure_invalid | ✓ | 1.0 |  |
| np-read-callers | callpath | callers | ✓ | 1.0 |  |
| np-bytes-written | symbol | where_computed | ✓ | 1.0 |  |
| np-errmsg | symbol | where_computed | ✗ | 0.0 | symbols: {"missing": ["PB_SET_ERROR"]} |
| np-invented | honesty | explain | ✓ | 1.0 |  |
