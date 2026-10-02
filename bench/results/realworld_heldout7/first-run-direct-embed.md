# Verity benchmark — direct-embed mode (20261001-080833)

Cases: 12 · passed: 7 · mean score: 0.792 · mean time per question: 0.13 s

| metric | result |
|---|---|
| symbol identification | 7/11 passed · accuracy 0.773 |
| no invention traps | 1/1 passed · accuracy 1.000 |
| unsupported claims (rejected by verifier) | 0 of 79 (0.00%) — removed before display |
| hallucinated code names in final answers | 3 case(s) |
| citation precision (claims whose cited line re-verified) | 79/79 (100.00%) |
| answer paragraphs without any evidence | 0 of 35 |

## Cases

| id | category | intent | passed | score | failing checks |
|---|---|---|---|---|---|
| m2-ping-timeout | scenario | failure_invalid | ✗ | 0.0 | symbols: {"missing": ["Ping", "OnConnectionClosing"]} |
| m2-send-fail | scenario | failure_invalid | ✗ | 0.5 | symbols: {"missing": ["MqttCommunicationException"]} |
| m2-connect-fail | scenario | failure_invalid | ✗ | 0.5 | symbols: {"missing": ["MqttConnectionException"]}; hallucinated: EventWaitHandle, WaitHandle |
| m2-keepalive-zero | scenario | failure_invalid | ✗ | 0.5 | symbols: {"missing": ["_keepAlivePeriod != 0"]}; hallucinated: EventWaitHandle |
| m2-msgid-wrap | symbol | explain | ✓ | 1.0 |  |
| m2-ping-callers | callpath | callers | ✓ | 1.0 |  |
| m2-inflight-callers | callpath | callers | ✓ | 1.0 |  |
| m2-threads | symbol | entries | ✗ | 1.0 | hallucinated: EventWaitHandle, WaitHandle |
| m2-lastcomm | symbol | where_computed | ✓ | 1.0 |  |
| m2-isconnected | symbol | where_computed | ✓ | 1.0 |  |
| m2-varbyte | symbol | which_fn | ✓ | 1.0 |  |
| m2-invented | honesty | explain | ✓ | 1.0 |  |
