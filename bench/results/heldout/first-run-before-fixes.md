# Verity benchmark — direct mode (20261001-003856)

Cases: 15 · passed: 5 · mean score: 0.522 · mean time per question: 0.0 s

| metric | result |
|---|---|
| symbol identification | 6/15 passed · accuracy 0.556 |
| scenario results | 0/1 passed · accuracy 0.000 |
| unsupported claims (rejected by verifier) | 0 of 105 (0.00%) — removed before display |
| hallucinated code names in final answers | 0 case(s) |
| citation precision (claims whose cited line re-verified) | 105/105 (100.00%) |
| answer paragraphs without any evidence | 0 of 46 |

## Cases

| id | category | intent | passed | score | failing checks |
|---|---|---|---|---|---|
| h-steer-decide | dataflow | explain | ✗ | 0.333 | symbols: {"missing": ["bearing_deg", "currentHeading"]} |
| h-ringbuf-fill | symbol | search | ✗ | 0.0 | symbols: {"missing": ["rb_push", "USART2_IRQHandler"]} |
| h-tick-caller | callpath | callers | ✗ | 0.5 | symbols: {"missing": ["NAV_TICK_MS"]} |
| h-speed-trace | dataflow | callers | ✗ | 0.0 | symbols: {"missing": ["handle_rmc", "speed_knots", "CalculateHeading"]} |
| h-3-sats | scenario | scenario | ✗ | 0.5 | scenario: {"why": "no run"} |
| h-parse-coord | symbol | search | ✗ | 0.0 | symbols: {"missing": ["nmea_parse_coord"]} |
| h-invalid-handler | symbol | explain | ✓ | 1.0 |  |
| h-bearing-called | callpath | explain | ✓ | 1.0 |  |
| h-remove-wrap | callpath | impact | ✓ | 1.0 |  |
| h-callback-var | symbol | explain | ✗ | 0.5 | symbols: {"missing": ["ProcessGpsData"]} |
| h-led-off | callpath | search | ✓ | 1.0 |  |
| h-cs-quality | scenario | explain | ✗ | 0.5 | symbols: {"missing": ["OnFix"]} |
| h-cs-bank | dataflow | explain | ✗ | 0.0 | symbols: {"missing": ["SendBankCommand", "Send"]} |
| h-cs-subscribe | callpath | explain | ✓ | 1.0 |  |
| h-cs-tick | symbol | explain | ✗ | 0.5 | symbols: {"missing": ["SendBankCommand"]} |
