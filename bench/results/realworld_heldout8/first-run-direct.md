# Verity benchmark — direct mode (20261001-082314)

Cases: 12 · passed: 5 · mean score: 0.5 · mean time per question: 0.12 s

| metric | result |
|---|---|
| symbol identification | 4/11 passed · accuracy 0.455 |
| no invention traps | 1/1 passed · accuracy 1.000 |
| unsupported claims (rejected by verifier) | 0 of 154 (0.00%) — removed before display |
| hallucinated code names in final answers | 0 case(s) |
| citation precision (claims whose cited line re-verified) | 154/154 (100.00%) |
| answer paragraphs without any evidence | 0 of 39 |

## Cases

| id | category | intent | passed | score | failing checks |
|---|---|---|---|---|---|
| np-stream-full | scenario | failure_invalid | ✗ | 0.5 | symbols: {"missing": ["stream full"]} |
| np-end-of-stream | scenario | failure_invalid | ✗ | 0.0 | symbols: {"missing": ["pb_read", "end-of-stream"]} |
| np-missing-required | scenario | failure_missing | ✗ | 0.0 | symbols: {"missing": ["pb_decode_inner", "missing required field"]} |
| np-varint-overflow | scenario | failure_invalid | ✓ | 1.0 |  |
| np-wrong-wire-type | scenario | failure_invalid | ✓ | 1.0 |  |
| np-max-depth | scenario | failure_invalid | ✗ | 0.0 | symbols: {"missing": ["pb_make_string_substream", "PB_MESSAGE_NESTING_MAX"]} |
| np-array-max | scenario | failure_invalid | ✗ | 0.0 | symbols: {"missing": ["encode_array", "array max size exceeded"]} |
| np-zero-tag | scenario | failure_invalid | ✗ | 0.5 | symbols: {"missing": ["zero tag"]} |
| np-read-callers | callpath | callers | ✓ | 1.0 |  |
| np-bytes-written | symbol | where_computed | ✓ | 1.0 |  |
| np-errmsg | symbol | where_computed | ✗ | 0.0 | symbols: {"missing": ["PB_SET_ERROR"]} |
| np-invented | honesty | explain | ✓ | 1.0 |  |
