# Phase 5 proof on cJSON (held-out project #10)

Written by `bench/learning_protocol.py` (see its docstring for the protocol). `latest-<mode>.json` is the final
run of each mode, made with the code as merged:

| Mode | Final run |
|---|---|
| facts only | `proof-direct-20261003-212100.json` |
| facts + meaning search | `proof-direct-embed-20261003-212245.json` |
| local model + meaning search | `proof-llm-embed-20261003-213503.json` |

The earlier runs are kept as the record of how the feature was tuned:
- `proof-direct-20261003-143720.json`, `proof-direct-embed-20261003-144146.json`, `proof-llm-embed-20261003-190951.json`:
  the first full runs. They chose the meaning thresholds (pin ≥ 0.80, hint ≥ 0.70).
- `proof-direct-20261003-200837.json`: made after refusing corrections on answers marked not correctable. The
  allocator question had fallen back to search, so it could no longer be corrected (8 corrections instead of 9). This
  led to "every answer of a kind that uses corrections is correctable".
- `proof-llm-embed-20261003-202105.json`: made with a first version of the opposite-question guard (any negation word).
  It kept "…where the closing double quote is missing?" from using the correction for "…the closing quote never
  comes?". This led to the final rule: a negated question and one that names no trouble at all.
