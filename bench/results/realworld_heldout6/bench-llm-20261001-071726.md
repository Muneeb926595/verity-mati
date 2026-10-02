# Verity benchmark — llm mode (20261001-071726)

Cases: 14 · passed: 11 · mean score: 0.786 · mean time per question: 9.98 s

| metric | result |
|---|---|
| symbol identification | 10/13 passed · accuracy 0.769 |
| no invention traps | 1/1 passed · accuracy 1.000 |
| unsupported claims (rejected by verifier) | 1 of 62 (1.61%) — removed before display |
| hallucinated code names in final answers | 0 case(s) |
| citation precision (claims whose cited line re-verified) | 61/62 (98.39%) |
| answer paragraphs without any evidence | 0 of 27 |

## Cases

| id | category | intent | passed | score | failing checks |
|---|---|---|---|---|---|
| paho-keepalive-timeout | scenario | failure_invalid | ✓ | 1.0 |  |
| paho-buffer-overflow | scenario | failure_invalid | ✓ | 1.0 |  |
| paho-no-handler | scenario | failure_invalid | ✓ | 1.0 |  |
| paho-sub-rejected | scenario | failure_invalid | ✗ | 0.0 | symbols: {"missing": ["MQTTSubscribeWithResults", "0x80"]} |
| paho-publish-disconnected | scenario | failure_invalid | ✓ | 1.0 |  |
| paho-remlen-too-long | scenario | failure_invalid | ✓ | 1.0 |  |
| paho-serialize-short | scenario | failure_invalid | ✓ | 1.0 |  |
| paho-packetid-wrap | symbol | explain | ✗ | 0.0 | symbols: {"missing": ["getNextPacketId", "MAX_PACKET_ID"]} |
| paho-cycle-callers | callpath | callers | ✓ | 1.0 |  |
| paho-sendpacket-callers | callpath | callers | ✓ | 1.0 |  |
| paho-mqttread | symbol | explain | ✓ | 1.0 |  |
| paho-topic-origin | dataflow | origin | ✗ | 0.0 | symbols: {"missing": ["MQTTDeserialize_publish", "readMQTTLenString"]} |
| paho-isconnected | symbol | where_computed | ✓ | 1.0 |  |
| paho-invented | honesty | explain | ✓ | 1.0 |  |
