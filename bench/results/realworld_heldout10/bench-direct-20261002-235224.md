# Verity benchmark — direct mode (20261002-235224)

Cases: 14 · passed: 4 · mean score: 0.429 · mean time per question: 1.14 s · right place within the top 3 candidates: 4

| metric | result |
|---|---|
| symbol identification | 4/14 passed · accuracy 0.429 |
| unsupported claims (rejected by verifier) | 0 of 330 (0.00%) — removed before display |
| hallucinated code names in final answers | 0 case(s) |
| citation precision (claims whose cited line re-verified) | 330/330 (100.00%) |
| answer paragraphs without any evidence | 0 of 53 |

## Cases

| id | category | intent | passed | score | failing checks |
|---|---|---|---|---|---|
| cj-deep-nesting | scenario | failure_invalid | ✓ | 1.0 |  |
| cj-minus-no-digits | scenario | failure_invalid | ✗ | 0.0 | symbols: {"missing": ["parse_number", "number_c_string == after_end"]} |
| cj-unterminated-string | scenario | failure_invalid | ✗ | 0.0 | symbols: {"missing": ["parse_string", "input_end - input_buffer->content"]} |
| cj-bad-surrogate | scenario | scenario | ✓ | 1.0 |  |
| cj-member-alloc-fail | scenario | failure_invalid | ✗ | 0.0 | symbols: {"missing": ["parse_object", "new_item == NULL"]} |
| cj-prealloc-too-small | scenario | failure_invalid | ✗ | 0.5 | symbols: {"missing": ["p->noalloc"]} |
| cj-trailing-garbage | scenario | failure_invalid | ✗ | 0.5 | symbols: {"missing": ["require_null_terminated"]} |
| cj-key-case | scenario | explain | ✗ | 0.0 | symbols: {"missing": ["get_object_item", "case_insensitive_strcmp"]} |
| cj-pointer-leading-zero | scenario | scenario | ✓ | 1.0 |  |
| cj-patch-unknown-op | scenario | failure_invalid | ✗ | 0.5 | symbols: {"missing": ["status = 3"]} |
| cj-string-parser-callers | structural | callers | ✓ | 1.0 |  |
| cj-custom-allocator | structural | where_computed | ✗ | 0.0 | symbols: {"missing": ["cJSON_InitHooks", "global_hooks"]} |
| cj-object-indent | structural | which_fn | ✗ | 0.5 | symbols: {"missing": ["output_buffer->depth"]} |
| cj-generate-patches | structural | which_fn | ✗ | 0.0 | symbols: {"missing": ["create_patches", "compose_patch"]} |
