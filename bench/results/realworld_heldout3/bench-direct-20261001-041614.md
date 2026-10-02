# Verity benchmark — direct mode (20261001-041614)

Cases: 13 · passed: 4 · mean score: 0.385 · mean time per question: 0.09 s

| metric | result |
|---|---|
| symbol identification | 3/12 passed · accuracy 0.333 |
| file line references | 1/1 passed · accuracy 1.000 |
| no invention traps | 1/1 passed · accuracy 1.000 |
| unsupported claims (rejected by verifier) | 0 of 208 (0.00%) — removed before display |
| hallucinated code names in final answers | 0 case(s) |
| citation precision (claims whose cited line re-verified) | 208/208 (100.00%) |
| answer paragraphs without any evidence | 0 of 59 |

## Cases

| id | category | intent | passed | score | failing checks |
|---|---|---|---|---|---|
| co-hb-timeout | scenario | failure_missing | ✗ | 0.0 | symbols: {"missing": ["CO_HBconsumer_process", "CO_HBconsumer_TIMEOUT"]} |
| co-sync-length | scenario | failure_invalid | ✗ | 0.5 | symbols: {"missing": ["CO_EM_SYNC_LENGTH"]} |
| co-bus-off | scenario | failure_invalid | ✗ | 0.5 | symbols: {"missing": ["CO_EM_CAN_TX_BUS_OFF"]} |
| co-crc-fn | symbol | where_computed | ✓ | 1.0 |  |
| co-crc-callers | callpath | callers | ✗ | 0.0 | symbols: {"missing": ["validateAndWriteToOD", "readFromOd", "CO_SDOserver_process"]} |
| co-nmt-state | symbol | where_computed | ✓ | 1.0 |  |
| co-hb-send | symbol | search | ✗ | 0.0 | symbols: {"missing": ["CO_NMT_process", "CO_CANsend"]} |
| co-err-operational | scenario | failure_invalid | ✗ | 0.0 | symbols: {"missing": ["CO_NMT_process", "CO_NMT_ERR_TO_STOPPED"]} |
| co-sdo-no-object | scenario | failure_invalid | ✗ | 0.0 | symbols: {"missing": ["CO_SDOserver_process", "CO_SDO_ST_ABORT"]} |
| co-fifo-full | scenario | explain | ✗ | 0.0 | symbols: {"missing": ["count - i"]} |
| co-rx-callback | callpath | explain | ✓ | 1.0 |  |
| co-sdo-rxdata | dataflow | origin | ✗ | 0.0 | symbols: {"missing": ["CO_SDO_receive"]} |
| co-invented | honesty | explain | ✓ | 1.0 |  |
