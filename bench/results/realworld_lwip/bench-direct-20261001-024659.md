# Verity benchmark — direct mode (20261001-024659)

Cases: 15 · passed: 5 · mean score: 0.433 · mean time per question: 1.65 s

| metric | result |
|---|---|
| symbol identification | 4/14 passed · accuracy 0.393 |
| file line references | 0/2 passed · accuracy 0.000 |
| no invention traps | 1/1 passed · accuracy 1.000 |
| unsupported claims (rejected by verifier) | 7 of 148 (4.73%) — removed before display |
| hallucinated code names in final answers | 0 case(s) |
| citation precision (claims whose cited line re-verified) | 141/148 (95.27%) |
| answer paragraphs without any evidence | 1 of 44 |

## Cases

| id | category | intent | passed | score | failing checks |
|---|---|---|---|---|---|
| lw-callers-tcp-process | callpath | callers | ✓ | 1.0 |  |
| lw-callers-ip4-input | callpath | callers | ✓ | 1.0 |  |
| lw-tcp-bad-checksum | scenario | failure_invalid | ✗ | 0.0 | symbols: {"missing": ["chkerr", "pbuf_free"]} |
| lw-ip-checksum | symbol | which_fn | ✗ | 0.0 | symbols: {"missing": ["ip4_input", "inet_chksum"]} |
| lw-established | symbol | where_computed | ✗ | 0.0 | symbols: {"missing": ["tcp_process"]}; citations: {"missing": ["src/core/tcp_in.c:877"]} |
| lw-ttl-decrement | symbol | where_computed | ✗ | 0.0 | symbols: {"missing": ["ip4_forward"]}; citations: {"missing": ["src/core/ipv4/ip4.c:319"]} |
| lw-ttl-zero | scenario | failure_invalid | ✓ | 1.0 |  |
| lw-callers-dhcp-fine | callpath | callers | ✗ | 0.5 | symbols: {"missing": ["lwip_cyclic_timers"]} |
| lw-offered-ip | dataflow | origin | ✗ | 0.0 | symbols: {"missing": ["dhcp_handle_offer", "yiaddr", "dhcp_recv"]} |
| lw-tcp-write-full | scenario | explain | ✗ | 0.5 | symbols: {"missing": ["ERR_MEM"]} |
| lw-threads | symbol | entries | ✗ | 0.0 | symbols: {"missing": ["tcpip_thread"]} |
| lw-path-eth-tcp | callpath | flow | ✗ | 0.0 | symbols: {"missing": ["ethernet_input", "ip4_input", "tcp_input"]} |
| lw-retransmit | symbol | failure_missing | ✗ | 0.5 | symbols: {"missing": ["rto"]} |
| lw-input-fn | callpath | explain | ✓ | 1.0 |  |
| lw-invented | honesty | explain | ✓ | 1.0 |  |
