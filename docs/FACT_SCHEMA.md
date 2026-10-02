# Verity fact schema (extractor → engine contract)

Every extractor (C via libclang, C# via the Roslyn worker, tree-sitter / cppcheck fallbacks) emits **JSON Lines**:
one JSON object per line, each with a `"t"` (type) field. Paths are **relative to the repository root, using `/`**.
Lines are **1-based**. Unknown fields are ignored by the ingester; missing optional fields are fine.

Symbol keys must be **stable and unique** across the repository:
- C: Clang USR (`c:@F@ProcessGpsData`, `c:@S@gps_fix_t@FI@heading_deg`, `c:nav.c@F@helper` for statics).
- C#: Roslyn `DocumentationCommentId` (`M:Nav.NavigationController.ProcessGpsData(Nav.GpsFix)`, `F:…`, `P:…`, `T:…`, `E:…`),
  locals/params: `<method-id>#param:<name>` / `<method-id>#local:<name>@<line>`.

## Records

| t | fields | meaning |
|---|---|---|
| `file` | `path, lang ("c"\|"csharp"), sha (sha256 hex of bytes), loc, fidelity ("F1".."F4"), errors (int), stubs ([str]), profile (str)` | one per source file |
| `symbol` | `key, kind, name, qual, sig, type, file, line, end_line, col, container (key\|null), is_def (bool), static (bool), extern (bool), doc (str\|null)` | declaration/definition. kinds: `function method constructor field global local param struct union class interface record enum enum_const macro typedef property event delegate namespace` |
| `occ` | `sym, file, line, col, role, in (enclosing function key\|null), via (base symbol key, optional), guards ([guard], optional)` | a reference. roles: `read write rw call addr type macro` |
| `call` | `from, to (key\|null), file, line, col, kind, res, via (key, optional), args ([str]), guards ([guard])` | call site. kinds: `direct virtual interface fnptr delegate event macro ctor`; res: `exact cha table flow signature unresolved` |
| `flow` | `src, dst, kind, file, line, fn, expr` | value flows from symbol `src` to `dst`. kinds: `assign compound init arg ret return_to api` (`ret` = into the function's return node `<fnkey>#ret`) |
| `fnaddr` | `fn, dst, file, line, kind ("init"\|"assign"\|"arg"\|"table")` | address of function/method `fn` stored into variable/field/param `dst` |
| `branch` | `fn, file, line, end_line, kind (if\|switch\|case\|while\|for\|do\|ternary\|early_exit), cond, reads ([keys]), then ([start,end]), else ([start,end]\|null)` | a decision point inside a function |
| `const` | `sym, value` | enum values, object-like macros, const-initialised globals |
| `entry` | `sym, kind (main\|isr\|callback\|thread\|timer\|event\|test\|export\|handler), detail, res (exact\|pattern\|heuristic)` | where execution can start |
| `io` | `sym, dir (source\|sink), api, file, line, fn, res (apipack\|heuristic\|mmio)` | where data enters/leaves |
| `inherit` | `sub, base, kind (extends\|implements\|overrides)` | type/method hierarchy (C#) |
| `instantiate` | `type, file, line, fn` | `new T(...)` sites (for RTA) |
| `subscribe` | `event, handler, file, line, fn, op ("+="\|"-=")` | C# event wiring |
| `include` | `file, target, resolved (bool), stub (bool)` | `#include` edges (C) |
| `diag` | `file, line, severity, msg` | extractor diagnostics |
| `config_key` | `key, sym, file, line, via` | configuration keys (C# IConfiguration / C macros tested in #if) |

### guard object
`{"cond": "fix->speed_knots < MIN_SPEED_FOR_HEADING", "polarity": false, "line": 57, "kind": "if|else|early_exit|loop|case|ternary", "reads": [keys]}`
`polarity: true` means the statement executes only when `cond` is true; `false` means only when it is false.

### Worked C example (abridged)
```
{"t":"symbol","key":"c:@F@ProcessGpsData","kind":"function","name":"ProcessGpsData","qual":"ProcessGpsData","sig":"void (const gps_fix_t *)","file":"src/nav/navigation.c","line":41,"end_line":78,"is_def":true}
{"t":"call","from":"c:@F@ProcessGpsData","to":"c:@F@CalculateHeading","file":"src/nav/navigation.c","line":63,"kind":"direct","res":"exact","args":["fix->heading_deg","fix->speed_knots"],"guards":[{"cond":"status != GPS_OK","polarity":false,"line":52,"kind":"early_exit"}]}
{"t":"occ","sym":"c:@currentHeading","file":"src/nav/navigation.c","line":63,"role":"write","in":"c:@F@ProcessGpsData"}
{"t":"flow","src":"c:@F@CalculateHeading#ret","dst":"c:@currentHeading","kind":"return_to","file":"src/nav/navigation.c","line":63,"fn":"c:@F@ProcessGpsData","expr":"currentHeading = CalculateHeading(...)"}
```
