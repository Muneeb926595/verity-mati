"""Generate a large, realistic C project (many modules, structs, function-pointer tables, callbacks, config macros)
for performance testing. Deterministic (seeded)."""
import random
import sys
from pathlib import Path


def main(out: str, modules: int = 120, funcs: int = 18) -> None:
    rnd = random.Random(42)
    root = Path(out)
    (root / "include").mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "include" / "common.h").write_text(
        "#ifndef COMMON_H\n#define COMMON_H\n#include <stdint.h>\n#define MAX_ITEMS 64\n#define SCALE 3\n"
        "typedef struct { int32_t id; double value; uint8_t flags; } item_t;\n"
        "typedef int (*handler_t)(item_t *it, int arg);\n#endif\n")
    total = 0
    for m in range(modules):
        h = [f"#ifndef MOD{m}_H", f"#define MOD{m}_H", '#include "common.h"',
             f"typedef struct {{ item_t items[MAX_ITEMS]; int count; double acc; int state; }} mod{m}_ctx_t;",
             f"extern mod{m}_ctx_t g_mod{m};"]
        c = [f'#include "mod{m}.h"', *(f'#include "mod{d}.h"' for d in range(max(0, m - 3), m)),
             f"mod{m}_ctx_t g_mod{m};", f"static int s_calls_{m} = 0;"]
        for f in range(funcs):
            name = f"mod{m}_f{f}"
            h.append(f"int {name}(item_t *it, int arg);")
            body = [f"int {name}(item_t *it, int arg)", "{", "    int r = 0;", f"    s_calls_{m}++;"]
            body.append("    if (it == 0) { return -1; }")
            for k in range(rnd.randint(4, 12)):
                kind = rnd.random()
                if kind < 0.3 and f > 0:
                    body.append(f"    r += mod{m}_f{rnd.randrange(f)}(it, arg + {k});")
                elif kind < 0.45 and m > 0:
                    d = rnd.randrange(max(0, m - 3), m)
                    body.append(f"    r += mod{d}_f{rnd.randrange(funcs)}(it, r);")
                elif kind < 0.6:
                    body.append(f"    if (arg > {rnd.randint(1, 50)} && it->flags) {{ g_mod{m}.acc += it->value * SCALE; }} else {{ r--; }}")
                elif kind < 0.75:
                    body.append(f"    for (int i = 0; i < g_mod{m}.count && i < MAX_ITEMS; i++) {{ r += g_mod{m}.items[i].id; }}")
                elif kind < 0.85:
                    body.append(f"    switch (g_mod{m}.state) {{ case 0: g_mod{m}.state = 1; break; case 1: r += 2; break; default: break; }}")
                else:
                    body.append(f"    it->value = it->value * 0.5 + (double)r;")
            body += ["    return r;", "}", ""]
            c += body
        c.append(f"static const handler_t k_mod{m}_table[] = {{ " + ", ".join(f"mod{m}_f{i}" for i in range(0, funcs, 3)) + " };")
        c.append(f"int mod{m}_dispatch(item_t *it, int which) {{ return k_mod{m}_table[which % {len(range(0, funcs, 3))}](it, which); }}")
        h.append(f"int mod{m}_dispatch(item_t *it, int which);")
        h.append("#endif")
        (root / "include" / f"mod{m}.h").write_text("\n".join(h) + "\n")
        (root / "src" / f"mod{m}.c").write_text("\n".join(c) + "\n")
        total += len(c) + len(h)
    main_c = ['#include "common.h"'] + [f'#include "mod{m}.h"' for m in range(modules)] + [
        "int main(void) {", "    item_t it = {1, 2.0, 1};", "    int r = 0;"] + \
        [f"    r += mod{m}_dispatch(&it, {m});" for m in range(modules)] + ["    return r;", "}"]
    (root / "src" / "main.c").write_text("\n".join(main_c) + "\n")
    print(f"generated {modules} modules, ~{total + len(main_c)} lines in {root}")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 120)
