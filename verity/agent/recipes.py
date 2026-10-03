"""Intent recipes (design §16): deterministic procedures over the graphs that produce claims + evidence + the
"how the data flows" steps + a plain-language template answer. The local model may later rephrase the template, but
every fact comes from here and is re-checked by the verifier."""
from __future__ import annotations

import heapq
import json
import re
from collections import defaultdict
from types import SimpleNamespace
from typing import Any

from ..analysis.graph import CallSite, CodeGraph, Edge
from ..retrieval.cards import STOP, split_ident
from ..retrieval.search import TEST_WORDS_RE, expand_words
from .evidence import CONFIRMED, INFERENCE, OBSERVED, RUNTIME, UNKNOWN, Pack, weaker
from .router import Route

from ..learn.apply import Learning, Loc

FN_KINDS = ("function", "method", "constructor")
VAR_KINDS = ("global", "field", "property", "local", "param")
CALC_VERBS = ("calculate", "calc", "compute", "update", "filter", "derive", "estimate", "determine", "process", "convert",
              "increment", "decrement", "advance")
ERR_WORDS = {"invalid", "valid", "status", "error", "err", "ok", "fail", "failed", "checksum", "crc", "parity", "range",
             "quality", "reject", "bad", "degraded", "timeout", "overflow", "underflow", "corrupt", "sync", "bounds", "limit"}
ERR_ASSIGN_RE = re.compile(r"^\s*[A-Za-z_]\w*\s*=\s*\(?\s*(-\s*\d+|\w*(ERR|FAIL|INVAL|TIMEOUT|NOSPC|NOMEM)\w*|"
                           r"E[A-Z]{3,}\w*)\s*\)?\s*$")
DIAG_NAME_RE = re.compile(r"assert|debug|printf|^puts$|trace|dump", re.I)
SYNC_NAME_RE = re.compile(r"critical|(^|_)(un)?lock|irq|interrupt|(dis|en)able_?int|semaphore_?(give|take)", re.I)
STRING_RE = re.compile(r'"((?:\\.|[^"\\])*)"')
# a condition that is only a diagnostics macro (debug printing / tracing), not a decision of the program
DIAG_COND_RE = re.compile(r"^\s*\w*(DEBUG|DBG|PRINT|TRACE|LOG)\w*\s*\(", re.I)
# topic words too vague to pick out a particular check on their own
WEAK_TOPIC = {"data", "value", "values", "information", "info", "thing", "things", "stuff", "input", "output", "code",
              "return", "returns", "returned", "much", "many", "there", "runs", "run", "out", "gets", "goes", "asks",
              "does", "happens", "happen", "not", "no", "none"}
TIME_WORDS = {"timeout", "time", "ms", "tick", "last", "age", "stale", "elapsed", "now", "deadline", "watchdog", "lost",
              "expired", "expire", "expires", "timer"}


def _partial(o: dict) -> bool:
    """A write to a whole object that only changed one of its fields (C: the base of `g.x = …`)."""
    v = o.get("via")
    return bool(v) and ("@FI@" in v or v == "deref")


KIND_WORDS = {"function": "function", "method": "method", "constructor": "constructor", "global": "global variable",
              "field": "field", "property": "property", "local": "local variable", "param": "parameter",
              "struct": "struct", "union": "union", "class": "class", "interface": "interface", "record": "record",
              "enum": "enum", "enum_const": "enum value", "macro": "constant", "typedef": "type", "event": "event",
              "delegate": "delegate"}


def _question_acronyms(g: CodeGraph, r: Route) -> dict[str, set[str]]:
    """'task control blocks' -> tcb, when the code really uses that token (OS_TCB): the phrase and the acronym are one
    idea. Adds the acronym to the question's topic words."""
    vocab = getattr(g, "_vocab", None)
    if vocab is None:
        vocab = set()
        for sy in g.sym.values():
            vocab.update(split_ident(sy["name"]))
        g._vocab = vocab
    words = [w.lower() for w in re.findall(r"[A-Za-z]+", r.question)]
    out: dict[str, set[str]] = {}
    for n in (4, 3, 2):
        for i in range(len(words) - n + 1):
            run = words[i:i + n]
            if any(w in STOP or len(w) < 3 for w in run):
                continue
            acr = "".join(w[0] for w in run)
            if len(acr) >= 3 and acr in vocab and acr not in out and not any(acr in a for a in out):
                out[acr] = set(run) | {w[:-1] for w in run if w.endswith("s") and len(w) > 3}
    for acr in out:
        if acr not in r.topic:
            r.topic.append(acr)
    return out


class Ctx:
    def __init__(self, g: CodeGraph, searcher, store, route: Route, runner=None):
        self.g = g
        self.s = searcher
        self.st = store
        self.r = route
        self.runner = runner
        self.pack = Pack()
        self.sent: list[tuple[str, list[int]]] = []        # template answer sentences -> claim indexes
        self.fid = {r["path"]: r["fidelity"] for r in store.q("SELECT path, fidelity FROM file")}
        self.cfg = store.meta_get("c_profile") or {}
        self.about_tests = bool(TEST_WORDS_RE.search(route.question or ""))
        self.llm = None                  # the local model, when the answer may use it (grounded re-ranking only)
        self.acronyms = _question_acronyms(g, route)
        self.learn = Learning()          # this project's corrections for the question (inert unless the Answerer sets it)

    # --------------------------------------------------------------------------------------------- evidence
    def ev(self, kind: str, file: str | None, line: int | None, sym: str | None = None, note: str = "",
           status: str | None = None, end_line: int | None = None) -> str | None:
        if not file or not line:
            return None
        text = self.g.src.line(file, line).strip()
        st = status or self.file_status(file)[0]
        return self.pack.add(kind, file, line, text, sym, note, st, end_line)

    def ev_def(self, key: str) -> str | None:
        s = self.g.sym.get(key)
        if not s or not s["file"] or s["file"] == "<stub>":
            return None
        return self.ev("def", s["file"], s["line"], key, f"definition of {self.g.display(key)}",
                       end_line=s["end_line"])

    def file_status(self, file: str | None) -> tuple[str, str]:
        f = self.fid.get(file or "", "F2")
        if f in ("F1", "F2"):
            return CONFIRMED, ""
        if f == "F3":
            return INFERENCE, f"{file} was analysed with stand-ins for missing headers/declarations"
        return INFERENCE, f"{file} could only be read syntactically"

    def call_status(self, c: CallSite) -> tuple[str, str]:
        st, why = self.file_status(c.file)
        if c.res in ("table",):
            return weaker(st, INFERENCE), f"call goes through the dispatch table {self.g.display(c.via) if c.via else ''}".strip()
        if c.res == "flow":
            what = "event" if (c.via or "").startswith("E:") or c.kind == "event" else \
                ("delegate" if (c.via or "")[:2] in ("F:", "P:") else "function pointer")
            return weaker(st, INFERENCE), (f"call goes through the {what} {self.g.display(c.via)}; the target was found "
                                           f"by following where it is assigned/subscribed") if c.via else "indirect call"
        if c.res in ("signature", "name", "cha", "rta"):
            return weaker(st, INFERENCE), {"signature": "target matched by signature only",
                                           "name": "matched by name only (file read syntactically)",
                                           "cha": "virtual call: every override is a possible target",
                                           "rta": "virtual call: overrides of instantiated types"}[c.res]
        if c.callee.startswith("?"):
            return RUNTIME, "indirect call whose target could not be determined statically"
        if c.res == "stub":
            return INFERENCE, "the called function's declaration was missing and was stood in for"
        return st, why

    def cfg_note(self, cfg: str | None) -> str:
        if not cfg or cfg == "default":
            return ""
        parts = []
        for p in cfg.split(";"):
            if p == "+ALL_FEATURES":
                parts.append("only in builds with optional features switched on")
            elif p == "-ALL_FEATURES":
                parts.append("only while optional features stay off (the default)")
            elif p.startswith("+"):
                parts.append(f"only when {p[1:]} is defined")
            elif p.startswith("-"):
                parts.append(f"only when {p[1:]} is not defined (the default)")
        return "; ".join(parts)

    def say(self, text: str, claims: list[int] | None = None) -> None:
        self.sent.append((text, claims or []))

    def claim(self, text: str, pred: str, args: list[Any], ev: list[str | None], status: str = CONFIRMED, why: str = "") -> int:
        self.pack.claim(text, pred, args, [e for e in ev if e], status, why)
        return len(self.pack.claims) - 1

    def flow_step(self, label: str, file: str | None = None, line: int | None = None, ev: str | None = None,
                  detail: str = "") -> None:
        self.pack.flow.append({"label": label, "file": file, "line": line, "ev": ev, "detail": detail})

    # --------------------------------------------------------------------------------------------- lookup
    def D(self, key: str) -> str:
        return self.g.display(key)

    def loc(self, file: str | None, line: int | None) -> str:
        return f"{file}:{line}" if file and line else (file or "")

    def targets(self, kinds: tuple[str, ...] | None = None, k: int = 8) -> list[str]:
        """Symbols the question is about: explicit identifiers first, then search over the topic words."""
        out = self.mentioned(kinds)
        if out:
            return out[:k]
        if not self.r.topic:
            return []
        hits = self.s.search(" ".join(self.r.topic), k=30, kinds=kinds)
        return [h.key for h in hits][:k]

    def targets_ranked(self, kinds: tuple[str, ...] | None = None, k: int = 15) -> list[str]:
        """Like targets(), but an ordinary English word that happens to be a symbol name ("timeout") does not override
        the search over all the question's words; only identifiers written as code do."""
        if any(m.get("explicit") for m in self.r.mentions):
            return self.targets(kinds, k)
        hits = [h.key for h in self.s.search(" ".join(self.r.topic), k=30, kinds=kinds)] if self.r.topic else []
        return list(dict.fromkeys(hits + self.mentioned(kinds)))[:k]

    def mentioned(self, kinds: tuple[str, ...] | None = None) -> list[str]:
        """Only the symbols the question names (no search fallback)."""
        out: list[str] = []
        for m in self.r.mentions:
            for c in m["candidates"]:
                s = self.g.sym.get(c)
                if s and s["kind"] == "macro" and s.get("sig") == "function-like":
                    # xQueueSend(...) -> xQueueGenericSend(...): the macro stands for the function it calls
                    for name in re.findall(r"([A-Za-z_]\w*)\s*\(", s.get("type") or ""):
                        for k2 in self.g.by_name.get(name, []):
                            if k2 in self.g.defined and k2 not in out and (kinds is None or self.g.sym[k2]["kind"] in kinds):
                                out.append(k2)
                                self.pack.assumptions.append(f"{s['name']} is a macro for {self.g.display(k2)} "
                                                             f"({self.loc(s['file'], s['line'])}).")
                        if out:
                            break
                if s and (kinds is None or s["kind"] in kinds) and c not in out:
                    out.append(c)
        return out

    def topic_score(self, key: str) -> float:
        words = set(split_ident(self.g.name(key)))
        s = self.g.sym.get(key)
        if s:
            words |= set(split_ident(s.get("type") or ""))
        tw = set(self.r.topic)
        return len(words & tw) + 0.5 * len({w for w in words for t in tw if len(t) > 3 and (t in w or w in t)})

    def word_weight(self, w: str) -> float:
        """How specific a word is in THIS project: 1.0 for a word few function names use, ~0.3 for one that names a
        tenth of the code (e.g. the protocol or product name the whole project is about)."""
        g = self.g
        df = getattr(g, "_name_df", None)
        if df is None:
            df = defaultdict(int)
            fns = [k for k in g.defined if g.sym[k]["kind"] in FN_KINDS]
            for k in fns:
                for x in set(split_ident(g.sym[k]["name"])):
                    df[x] += 1
            g._name_df, g._name_n = df, max(2, len(fns))
        import math
        n = g._name_n
        return max(0.15, min(1.0, math.log(n / max(1, df.get(w, 0))) / math.log(n)))

    def body_lines(self, fn: str) -> tuple[str | None, int, int]:
        s = self.g.sym.get(fn)
        if not s:
            return None, 0, 0
        return s["file"], s["line"], s["end_line"] or s["line"]


# ================================================================================================= describing a function
def _macro_internal(cx: Ctx, br: dict) -> bool:
    """A 'decision' that only exists inside a macro's expansion (`timeradd(&a, &b, &c);` hides an if): the source line
    has no if/while/for/switch/?: of its own."""
    t = STRING_RE.sub('""', cx.g.src.line(br["file"], br["line"]))
    t = re.sub(r"/\*.*?\*/|//.*$", "", t)
    return not re.search(r"\b(if|while|for|switch|do)\b|\?", t) and \
        br["kind"] in ("if", "ternary", "while", "for", "do", "switch")     # `timeradd()` hides a do { } while (0)


def _rerank(cx: Ctx, items: list, render, what: str = "check") -> list:
    """Let the local model pick, among the top deterministic candidates, the one whose code answers the question.
    Only the ORDER changes; every candidate is real code whose facts are verified. A changed choice is disclosed."""
    if cx.llm is None or len(items) < 2:
        return items
    from . import rerank
    top = items[:rerank.MAX_CANDIDATES]
    shown = []
    for it in top:
        try:
            shown.append(render(it))
        except Exception:  # noqa: BLE001
            shown.append("(not shown)")
    i, why = rerank.pick(cx.llm, cx.r.question, shown)
    cx.rerank_used = True
    if i is None or i == 0:
        return items
    chosen = top[i]
    cx.model_choices = getattr(cx, "model_choices", 0) + 1
    cx.pack.assumptions.append(f"The local model chose this {what} from {len(top)} candidates the analysis found" +
                               (f" ({why.rstrip('.')})" if why else "") + "; the facts are verified, the choice is the "
                               "model's judgement.")
    return [chosen] + [x for x in items if x is not chosen]


def _side_of(br: dict) -> str:
    return "case" if br.get("_case") else "else" if br.get("_else") else "then"


def _branch_loc(fn: str, br: dict) -> Loc:
    span = (br["then_s"], br.get("then_e") or br["then_s"]) if br.get("_case") and br.get("then_s") else None
    return Loc(fn, br.get("file"), br.get("line"), _side_of(br), span)


def _branch_label(cx: Ctx, fn: str, br: dict) -> str:
    """How a candidate check is named to people: `f() checks `cond` (file:line)`, `… when `cond` is not true`."""
    cond = " ".join((br.get("_pos_cond") or br.get("cond") or "").split())
    if br.get("_case"):
        return f"{cx.D(fn)} in the case {' / '.join(f'`{x}`' for x in br['_case'])} ({cx.loc(br['file'], br['line'])})"
    if br.get("_else"):
        return f"{cx.D(fn)} when `{cond[:160]}` is not true ({cx.loc(br['file'], br['line'])})"
    if br.get("kind") == "catch":
        return f"{cx.D(fn)} catches `{cond[:160]}` ({cx.loc(br['file'], br['line'])})"
    return f"{cx.D(fn)} checks `{cond[:160]}` ({cx.loc(br['file'], br['line'])})"


def _learned_branch(cx: Ctx, c: dict) -> tuple | None:
    """A correction's target as a (fn, branch, actions) candidate, built from the real code at the corrected line."""
    g = cx.g
    fn, bl = c.get("target_fn"), c.get("branch_line")
    if fn not in g.defined or not bl:
        return None
    br = next((b for b in g.branches.get(fn, []) if b["line"] == bl and b["file"] == c.get("target_file")), None) or \
        next((b for b in g.branches.get(fn, []) if b["line"] == bl), None)
    if br is None or not br.get("cond") and br["kind"] != "catch":
        return None
    side = c.get("side") or "then"
    if br["kind"] == "switch":
        for cs in _case_sides(cx, fn, br):
            if cs["then_s"] <= (c.get("target_line") or 0) <= (cs["then_e"] or cs["then_s"]):
                return fn, cs, _branch_actions(cx, fn, cs)
        return None
    if side == "else":
        if not br.get("else_s"):
            return None
        b2 = dict(br, cond=f"!({br['cond']})", then_s=br["else_s"], then_e=br["else_e"], _else=True, _pos_cond=br["cond"])
        return fn, b2, _branch_actions(cx, fn, br, then=False)
    return fn, br, _branch_actions(cx, fn, br)


def _fn_label(cx: Ctx, key: str) -> str:
    s = cx.g.sym.get(key) or {}
    return f"{cx.D(key)} ({cx.loc(s.get('file'), s.get('line'))})" if s.get("file") else cx.D(key)


def _render_function(cx: Ctx, fn: str, max_lines: int = 10) -> str:
    g = cx.g
    s = g.sym.get(fn) or {}
    f, a, b = s.get("file"), s.get("line") or 0, s.get("end_line") or s.get("line") or 0
    body = [" ".join(t.split())[:120] for _, t in g.src.snippet(f, a, min(b, a + max_lines))] if f else []
    doc = f" — {s['doc'][:160]}" if s.get("doc") else ""
    return f"function {g.name(fn)}() at {f}:{a}{doc}:\n    " + "\n    ".join(body)


def _render_branch(cx: Ctx, x) -> str:
    """A candidate check as the model sees it: function, condition, and the code that runs on that side."""
    g = cx.g
    fn, br, act = x
    neg = bool(br.get("_else"))
    cond = br.get("_pos_cond") or (br["cond"][2:-1] if neg and br["cond"].startswith("!(") else br["cond"])
    body = [" ".join(t.split())[:120] for n, t in act.get("lines", []) if n > br["line"] or neg][:7]
    out = (f"{g.name(fn)}() at {br['file']}:{br['line']} checks `{' '.join(cond.split())[:200]}`; "
           f"when that is {'NOT ' if neg else ''}true it runs:\n    " + "\n    ".join(body))
    j = act.get("jump")
    if j:
        out += f"\n    … then at label {j['label']}: " + "; ".join(g.name(c.callee) + "()" for c in j["calls"][:4])
    return out


def _diag_line(cx: Ctx, file: str | None, line: int | None) -> bool:
    """The statement on this line is diagnostics only: a debug-print / trace macro, or any macro (LFS_ERROR(…),
    LOG_WARN(…)) whose expansion only prints."""
    if not (file and line):
        return False
    t = cx.g.src.line(file, line).strip()
    if DIAG_COND_RE.match(t):
        return True
    m = re.match(r"^([A-Z][A-Z0-9_]*)\s*\(", t)
    if m:
        names = cx.g.calls_on_line(file, line)
        if names and all(PRINT_RE.match(n) for n in names):
            return True
        # a logging macro (LFS_ERROR, LOG_WARN, MY_TRACE…): prints, and any other call on the line only formats
        # its message
        return bool(re.search(r"(^|_)(ERROR|ERR|WARN|WARNING|INFO|DEBUG|DBG|TRACE|LOG|PRINTF?)(_|$)", m.group(1))) and \
            any(PRINT_RE.match(n) for n in names)
    return False


PRINT_RE = re.compile(r"^(f?printf|v?fprintf|puts|fputs|perror|syslog|OutputDebugString\w*|_?trace\w*|log\w*|print\w*)$", re.I)
DEBUG_FLAG_RE = re.compile(r"\b\w*(debug|verbose|trace|dbg)\w*\b", re.I)


def _debug_only(cx: Ctx, fn: str, br: dict) -> tuple[int, int] | None:
    """`if (ctx->debug) { fprintf(stderr, …); }`: a side that only prints, under a debug/verbose flag. Returns its line
    range (narration leaves it out: it is not what the program does)."""
    if br["kind"] != "if" or not DEBUG_FLAG_RE.search(_cond_code(br["cond"] or "")) or br.get("else_s"):
        return None
    s, e = br["then_s"], br["then_e"]
    if not s:
        return None
    g = cx.g
    calls = [c for c in g.out_calls.get(fn, []) if s <= c.line <= e]
    if any(not PRINT_RE.match(g.name(c.callee)) for c in calls):
        return None
    body = " ".join(t for _, t in g.src.snippet(br["file"], s, e))
    if re.search(r"\breturn\b|\bgoto\b|\bbreak\b|=(?!=)", STRING_RE.sub("", body).split("{", 1)[-1]):
        return None
    return (s, e)


def fn_steps(cx: Ctx, fn: str, max_steps: int = 14) -> list[dict[str, Any]]:
    """What a function does, in source order: decisions, calls, writes to non-local state, returns."""
    g = cx.g
    file, a, b = cx.body_lines(fn)
    if not file:
        return []
    items: list[dict[str, Any]] = []
    quiet = [r for r in (_debug_only(cx, fn, br) for br in g.branches.get(fn, [])) if r]

    def hidden(n: int) -> bool:
        return any(a <= n <= b for a, b in quiet)

    def here(f, n) -> bool:
        # one definition only: a function defined in two ports (oshw/linux and contrib/erika) must not mix their lines
        return f == file and a <= (n or 0) <= b
    for br in g.branches.get(fn, []):
        if br["kind"] in ("if", "switch", "while", "for", "do", "ternary", "catch") and br["cond"] and \
                not DIAG_COND_RE.match(br["cond"]) and not hidden(br["line"]) and here(br["file"], br["line"]) and \
                not _macro_internal(cx, br):          # a ?: inside PB_RETURN_ERROR's expansion is not a step
            items.append({"line": br["line"], "kind": "branch", "cond": br["cond"], "br": br})
    for c in g.out_calls.get(fn, []):
        if not _diag_line(cx, c.file, c.line) and not hidden(c.line) and here(c.file, c.line):
            items.append({"line": c.line, "kind": "call", "call": c})
    for sym, o in g.fn_occ(fn):
        if not here(o["file"], o["line"]):
            continue
        if o["fn"] == fn and o["role"] in ("write", "rw") and "#" not in sym and not _partial(o):
            items.append({"line": o["line"], "kind": "write", "sym": sym, "occ": o})
        elif o["fn"] == fn and o["role"] in ("write", "rw") and sym.startswith(fn + "#param:") and o.get("via") == "deref":
            items.append({"line": o["line"], "kind": "write", "sym": sym, "occ": o, "outparam": True})
        elif o["fn"] == fn and o["role"] in ("write", "rw") and sym.startswith(fn + "#local:") and not _partial(o):
            # a computed local (error = ref - measured): part of what the function does, unless trivial
            rhs = g.src.line(o["file"], o["line"]).split("=", 1)[-1]
            if re.search(r"[A-Za-z_]\w*", re.sub(r"\b(\d+(\.\d+)?[fFuUlL]*|NULL|TRUE|FALSE|true|false)\b", "", rhs)) \
                    and "=" in g.src.line(o["file"], o["line"]):
                items.append({"line": o["line"], "kind": "write", "sym": sym, "occ": o, "local": True})
    rets = [o for o in g.occ.get(f"{fn}#ret", []) if o["fn"] == fn and here(o["file"], o["line"])]
    for o in rets:
        items.append({"line": o["line"], "kind": "return", "occ": o})
    items.sort(key=lambda x: (x["line"], {"branch": 0, "call": 1, "write": 2, "return": 3}[x["kind"]]))
    seen_lines: set[tuple] = set()
    out = []
    for it in items:
        key = (it["line"], it["kind"], it.get("sym"), it.get("call").callee if it.get("call") else None)
        if key in seen_lines:
            continue
        seen_lines.add(key)
        out.append(it)
    # merge: a write whose line also has a call -> "stores the result of"
    return out[:max_steps * 2]


def _expanding_macro(g: CodeGraph, line_text: str, callee_name: str) -> str | None:
    """The function-like macro whose expansion makes a call that the source line does not spell out
    (`FD_ZERO(&fdset);` calls memset): its name, or None when the line names the callee itself."""
    if not callee_name or re.search(rf"\b{re.escape(callee_name)}\b", line_text):
        return None
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", STRING_RE.sub('""', line_text)):
        if f"c:macro@{m.group(1)}" in g.sym:
            return m.group(1)
    return None


def describe_steps(cx: Ctx, fn: str, steps: list[dict[str, Any]]) -> list[str]:
    g = cx.g
    lines = []
    call_lines = defaultdict(list)
    for it in steps:
        if it["kind"] == "call":
            call_lines[it["line"]].append(it["call"])
    used_call_lines = set()
    used_return_lines: set[int] = set()
    for it in steps:
        ln = it["line"]
        if it["kind"] == "branch":
            br = it["br"]
            kind = br["kind"]
            if kind in ("while", "for", "do"):
                lines.append(f"repeats while `{br['cond']}` (line {ln})")
            elif kind == "switch":
                lines.append(f"chooses a case based on `{br['cond']}` (line {ln})")
            elif kind == "ternary" and ln not in used_call_lines:
                # `return id = (id == MAX) ? 1 : id + 1;` is one statement, not a branch that returns straight away
                st = _stmt(g, cx.body_lines(fn)[0], ln)
                mv = g.macro_values()
                kc = [f"{t} = {mv[t]}" for t in dict.fromkeys(re.findall(r"\b[A-Z_][A-Z0-9_]{2,}\b", st)) if t in mv][:3]
                kc_txt = f" ({', '.join(kc)})" if kc else ""
                if st.startswith("return"):
                    lines.append(f"returns `{st[6:].strip()}`{kc_txt} (line {ln})")
                    used_return_lines.add(ln)
                else:
                    lines.append(f"{'sets' if re.search(r'[^=!<>]=(?!=)', st) else 'evaluates'} `{st}`{kc_txt} (line {ln})")
                used_call_lines.add(ln)
            elif kind == "ternary":
                continue
            else:
                # decisions nested in this side are described as their own steps: their lines are not this side's
                inner = [b for b in g.branches.get(fn, []) if b is not br and br["then_s"] and
                         br["then_s"] <= b["line"] <= (br["then_e"] or 0) and b["line"] != br["line"] and
                         not _macro_internal(cx, b)]

                def nested_line(n, inner=inner):
                    return any(n == b["line"] or _encloses(b, n) for b in inner)
                then_calls = [c for c in g.out_calls.get(fn, []) if br["then_s"] and br["then_s"] <= c.line <= (br["then_e"] or 0)
                              and c.line != br["line"] and not nested_line(c.line) and c.file == br["file"]]
                exits = g.src.snippet(cx.body_lines(fn)[0], br["then_s"] or ln, br["then_e"] or ln) if br["then_s"] else []
                ret_lines = [(n, t) for n, t in exits if _leaves(cx, br["file"], n, t, br["then_s"])]
                mv = g.macro_values()
                consts = [f"{t} = {mv[t]}" for t in dict.fromkeys(re.findall(r"\b[A-Z_][A-Z0-9_]{2,}\b", br["cond"])) if t in mv]
                desc = (f"handles `{br['cond']}` (line {ln})" if kind == "catch" else
                        f"checks `{br['cond']}`" + (f" ({', '.join(consts)})" if consts else "") + f" (line {ln})")
                then_writes = []
                if br["then_s"]:
                    for it2 in steps:
                        if it2["kind"] == "write" and br["then_s"] <= it2["line"] <= (br["then_e"] or 0) and it2["line"] != ln \
                                and not nested_line(it2["line"]) and not _is_exit(g, g.src.line(br["file"], it2["line"])):
                            then_writes.append(it2["line"])
                if then_calls:
                    desc += "; if true it calls " + ", ".join(dict.fromkeys(cx.D(c.callee) for c in then_calls))
                    used_call_lines |= {c.line for c in then_calls}
                if then_writes:
                    wt = [f"`{t}`" for t in dict.fromkeys(_code(g.src.line(cx.body_lines(fn)[0], w)) for w in then_writes) if t][:5]
                    desc += (" and sets " if then_calls else "; if true it sets ") + ", ".join(wt)
                    used_call_lines |= set(then_writes)
                ret_lines = [(n, t) for n, t in ret_lines if not nested_line(n)]
                if br["then_s"] and not (then_calls or then_writes or ret_lines):
                    lj = _loop_jump(g, br["file"], br["then_s"], br["then_e"] or br["then_s"], nested_line)
                    if lj:
                        desc += "; if true it " + " and ".join(lj)
                gt = next((m.group(1) for n, t in exits if not nested_line(n) and
                           (m := re.search(r"\bgoto\s+([A-Za-z_]\w*)\s*(;|$)", _code(t)))), None)
                if gt and not ret_lines:
                    desc += (" and " if then_calls or then_writes else "; if true it ") + f"jumps to `{gt}`"
                if ret_lines:
                    thrown = bool(re.search(r"\bthrow\b", ret_lines[0][1]))
                    via_macro = not re.search(r"\b(return|throw)\b", _code(ret_lines[0][1]))
                    rv = re.sub(r"^.*?\b(return|throw)\b\s*", "", ret_lines[0][1]).strip().rstrip(";").strip()
                    rv = re.sub(r"\s+", " ", rv)
                    desc += (" and " if then_calls or then_writes else "; if true it ") + (
                        f"returns through `{_code(ret_lines[0][1]).strip().rstrip(';')}`" if via_macro else
                        (f"throws `{rv}`" if rv else "throws the exception again") if thrown else
                        (f"returns `{rv}` straight away" if rv else "stops there (returns)"))
                    used_return_lines.add(ret_lines[0][0])
                if br.get("else_s"):
                    # the else side is not what happens next: say "otherwise" (`if (outstanding) rc = FAILURE; else send a
                    # new ping` must not read as "fails, then sends a new ping")
                    es, ee = br["else_s"], br["else_e"] or br["else_s"]
                    e_inner = [b for b in g.branches.get(fn, []) if b is not br and es <= b["line"] <= ee and
                               not _macro_internal(cx, b)]

                    def e_nested(n, e_inner=e_inner):
                        return any(n == b["line"] or _encloses(b, n) for b in e_inner)
                    e_calls = [c for c in g.out_calls.get(fn, []) if es <= c.line <= ee and not _diag_line(cx, c.file, c.line)
                               and not e_nested(c.line) and c.file == br["file"]]
                    e_writes = [it2["line"] for it2 in steps if it2["kind"] == "write" and es <= it2["line"] <= ee
                                and not e_nested(it2["line"]) and not _is_exit(g, g.src.line(br["file"], it2["line"]))]
                    e_parts = []
                    if e_calls:
                        e_parts.append("calls " + ", ".join(dict.fromkeys(cx.D(c.callee) for c in e_calls)))
                        used_call_lines |= {c.line for c in e_calls}
                    if e_writes:
                        e_txt = [t for t in dict.fromkeys(_code(g.src.line(cx.body_lines(fn)[0], w)) for w in e_writes) if t][:5]
                        if e_txt:
                            e_parts.append("sets " + ", ".join(f"`{t}`" for t in e_txt))
                        used_call_lines |= set(e_writes)
                    e_ret = [(n, t) for n, t in g.src.snippet(cx.body_lines(fn)[0], es, ee) if _leaves(cx, br["file"], n, t, es)
                             and not e_nested(n)]
                    if e_ret:
                        ec = _code(e_ret[0][1])
                        if not re.search(r"\b(return|throw)\b", ec):
                            e_parts.append(f"returns through `{ec.strip().rstrip(';')}`")
                        else:
                            kw = "throws" if re.search(r"\bthrow\b", ec) else "returns"
                            erv = re.sub(r"\s+", " ", re.sub(r"^.*?\b(return|throw)\b\s*", "", ec).strip().rstrip(";").strip())
                            e_parts.append(f"{kw} `{erv}`" if erv else ("throws the exception again" if kw == "throws" else "returns"))
                        used_return_lines.add(e_ret[0][0])
                        used_call_lines.add(e_ret[0][0])
                    if not e_parts:
                        e_parts += _loop_jump(g, br["file"], es, ee, e_nested)
                    if e_parts:
                        desc += "; otherwise it " + " and ".join(e_parts)
                if kind == "catch":
                    desc = desc.replace("; if true it ", "; there it ")
                lines.append(desc)
        elif it["kind"] == "call":
            if ln in used_call_lines:
                continue
            c = it["call"]
            if any(b["line"] == ln and b["cond"] and cx.D(c.callee).rstrip("()") in b["cond"] for b in g.branches.get(fn, [])):
                continue
            txt = _code(g.src.line(c.file, c.line))
            m = re.match(r"^(?:[\w\s\*]+?\s)?([\w.\->\[\]]+)\s*=\s*", txt)
            via = f" through {cx.D(c.via)}" if c.via and c.kind == "fnptr" else ""
            same = [x for x in call_lines.get(ln, []) if x.via and x.via == c.via and x.callee != c.callee]
            if c.via and same:
                targets = list(dict.fromkeys([cx.D(c.callee)] + [cx.D(x.callee) for x in same]))
                lines.append(f"calls one of {', '.join(targets[:6])}{' …' if len(targets) > 6 else ''}{via} — which one "
                             f"depends on the data at run time (line {ln})")
                used_call_lines.add(ln)
                continue
            # short arguments carry the meaning (`vTaskPlaceOnEventList(&( pxQueue->xTasksWaitingToSend ), xTicksToWait)`)
            a_txt = ", ".join(a.strip() for a in c.args if a.strip())
            shown = f"{cx.D(c.callee)[:-2]}({a_txt})" if a_txt and len(a_txt) <= 70 and cx.D(c.callee).endswith("()") else cx.D(c.callee)
            macro = _expanding_macro(g, txt, g.name(c.callee).split(".")[-1])
            if macro:
                # the call comes from a macro's expansion (`FD_ZERO(&fdset)` is memset(…)): its "arguments" are only the
                # macro's own text, so name the macro instead
                shown = f"{cx.D(c.callee)} (inside the macro {macro})"
            if m and cx.D(c.callee).rstrip("()") in txt:
                lines.append(f"calls {shown}{via} and stores the result in `{m.group(1)}` (line {ln})")
            else:
                lines.append(f"calls {shown}{via} (line {ln})")
            used_call_lines.add(ln)
        elif it["kind"] == "write":
            if ln in used_call_lines or any(b["line"] == ln for b in g.branches.get(fn, [])):
                continue                          # `if ((rc = send(…)) == OK)`: the decision on that line says it
            txt = _code(g.src.line(it["occ"]["file"], ln))
            m = re.match(r"^([A-Za-z_]\w*)(?:\.|->)(\w+)\s*=", txt)
            if m and lines and isinstance(lines[-1], tuple) and lines[-1][0] == m.group(1):
                base, fields, a0, _ = lines[-1]
                lines[-1] = (base, fields + [m.group(2)], a0, ln)
            elif m:
                lines.append((m.group(1), [m.group(2)], ln, ln))
            else:
                lines.append(f"sets `{_stmt(g, it['occ']['file'], ln)}` (line {ln})")
        elif it["kind"] == "return":
            if ln in used_return_lines:
                continue
            txt = _code(g.src.line(it["occ"]["file"], ln))
            if txt != "return":
                lines.append(f"returns `{txt[6:].strip()}` (line {ln})" if txt.startswith("return") else f"returns (line {ln})")
    out = []
    for s in lines:
        if isinstance(s, tuple):
            base, fields, a0, b0 = s
            if len(fields) == 1:
                s = f"sets `{_code(g.src.line(cx.body_lines(fn)[0], a0))}` (line {a0})"
            else:
                s = f"updates {base} ({', '.join(dict.fromkeys(fields))}; lines {a0}–{b0})"
        if s not in out:
            out.append(s)
    return out


def how_reached(cx: Ctx, fn: str) -> tuple[str, list[int]]:
    g = cx.g
    callers = g.callers(fn)
    cl: list[int] = []
    if not callers:
        s0 = g.sym.get(fn, {})
        if "dllexport" in g.src.line(s0.get("file"), s0.get("line")) or "EXPORT" in g.src.line(s0.get("file"), s0.get("line")):
            i = cx.claim(f"{cx.D(fn)} is exported ({cx.loc(s0['file'], s0['line'])}).", "defined",
                         [fn, s0["file"], s0["line"]], [cx.ev_def(fn)])
            return (f"{cx.D(fn)} is exported from the library (`__declspec(dllexport)`), so it is called by other "
                    f"programs rather than by code in this project."), [i]
        e = g.entries.get(fn)
        if e:
            ok_res = e["res"] in ("exact", "keyword", "apipack")
            i = cx.claim(f"{cx.D(fn)} is an entry point ({e['detail']}).", "entry", [fn], [cx.ev_def(fn)],
                         CONFIRMED if ok_res else INFERENCE, "" if ok_res else "found by naming convention")
            return f"{cx.D(fn)} is an entry point: {e['detail']}.", [i]
        return f"Nothing in this code calls {cx.D(fn)} directly (it may be called by a library, the OS or hardware).", []
    parts = []
    for c in callers[:4]:
        st, why = cx.call_status(c)
        evs = [cx.ev("call", c.file, c.line, c.callee)]
        extra = ""
        if c.via and c.res in ("flow", "table"):
            # where the pointer got its value: the table (a global array of entries) or the registration call
            fas = [fa for fa in g.fnaddr if fa["fn"] == fn and (fa["dst"] == c.via or fa["dst"].startswith(c.via.split("#")[0])
                                                                or fa["kind"] in ("arg", "table"))]
            fas.sort(key=lambda fa: (g.sym.get(fa["dst"], {}).get("kind") != "global", fa["file"], fa["line"]))
            if fas:
                fa = fas[0]
                evs.append(cx.ev("flow", fa["file"], fa["line"], fn, "function address stored here"))
                tbl = g.sym.get(fa["dst"], {})
                extra = (f" ({cx.D(fn)} is listed in the table {tbl['name']} at {cx.loc(fa['file'], fa['line'])})"
                         if tbl.get("kind") == "global" else
                         f" ({cx.D(fn)} is registered at {cx.loc(fa['file'], fa['line'])})")
        how = _via_phrase(cx, c)
        text = f"{cx.D(c.caller)} calls {cx.D(fn)}{(' ' + how) if how else ''} at {cx.loc(c.file, c.line)}{extra}."
        cl.append(cx.claim(text, "calls", [c.caller, fn, c.file, c.line], evs, st, why))
        conds = _guard_text(cx, c.guards)
        args = ""
        if c.args and len(callers) <= 3:
            srcs = _arg_sources(cx, c)
            args = f" with ({', '.join(f'`{a}`' for a in c.args[:5] if a.strip())})" + (f" — {'; '.join(srcs)}" if srcs else "")
        parts.append(f"{cx.D(c.caller)}{(' (' + how + ')') if how else ''} at {cx.loc(c.file, c.line)}{extra}{args}" +
                     (f", {conds}" if conds else ""))
    more = ""
    rest = [c for c in callers[4:] if cx.about_tests or not g.is_test(c.caller)]
    if rest:
        # every other call site, briefly (a caller list that stops at four hides real callers)
        for c in rest[:16]:
            st, why = cx.call_status(c)
            cl.append(cx.claim(f"{cx.D(c.caller)} calls {cx.D(fn)} at {cx.loc(c.file, c.line)}.", "calls",
                               [c.caller, fn, c.file, c.line], [cx.ev("call", c.file, c.line, c.callee)], st, why))
        more = " It is also called by " + ", ".join(f"{cx.D(c.caller)} ({cx.loc(c.file, c.line)})" for c in rest[:16]) + \
            (f" and {len(rest) - 16} more call sites" if len(rest) > 16 else "") + "."
    paths = g.paths_from_entries(fn, limit=1)
    chain = ""
    if paths and paths[0]:
        chain = " The full chain from the program's start is: " + " → ".join(
            [cx.D(paths[0][0].caller)] + [cx.D(c.callee) for c in paths[0]]) + "."
    return f"It is called by {'; '.join(parts)}.{more}{chain}", cl


def _via_phrase(cx: Ctx, c: CallSite) -> str:
    if not c.via or c.res not in ("flow", "table", "cha", "rta"):
        return ""
    v = c.via
    if c.res in ("cha", "rta"):
        return "through a virtual/interface call (the implementation depends on the object)"
    if v.startswith("E:") or c.kind == "event":
        return f"through the event {cx.D(v)}"
    if v[:2] in ("F:", "P:") or c.kind == "delegate":
        return f"through the delegate {cx.D(v)}"
    if c.res == "table":
        return f"through the dispatch table {cx.D(v)}"
    return f"through the function pointer {cx.D(v)}"


def _guard_text(cx: Ctx, guards: list[dict] | None) -> str:
    if not guards:
        return ""
    mv = cx.g.macro_values()
    out = []
    for gd in guards[-3:]:
        consts = [f"{t} = {mv[t]}" for t in dict.fromkeys(re.findall(r"\b[A-Z_][A-Z0-9_]{2,}\b", gd["cond"])) if t in mv]
        cst = f" ({', '.join(consts)})" if consts else ""
        if gd["kind"] == "loop":
            out.append(f"repeatedly inside `{gd['cond']}`")
        elif gd["kind"] == "early_exit":
            out.append(f"only when `{gd['cond']}` is false{cst}")
        elif gd["kind"] == "case":
            out.append(f"in the case `{gd['cond']}`")
        else:
            out.append(f"only when `{gd['cond']}` is {'true' if gd['polarity'] else 'false'}{cst}")
    return ", ".join(out)


def _arg_sources(cx: Ctx, c: CallSite) -> list[str]:
    """Where the caller's argument values come from (locals assigned just before the call)."""
    g = cx.g
    out = []
    for a in c.args[:5]:
        m = re.fullmatch(r"[A-Za-z_]\w*", a.strip())
        if not m:
            continue
        loc = next((k for k in g.sym if k.startswith(c.caller + "#local:" + a.strip() + "@")), None)
        if not loc:
            continue
        for o in g.writers(loc):
            if o["line"] <= c.line:
                out.append(f"`{_code(g.src.line(o['file'], o['line']))}` (line {o['line']})")
                break
    return out[:3]


def explain_function(cx: Ctx, fn: str, brief: bool = False) -> None:
    g = cx.g
    s = g.sym[fn]
    d = cx.ev_def(fn)
    doc = (s.get("doc") or "").strip()
    c0 = cx.claim(f"{cx.D(fn)} is defined in {cx.loc(s['file'], s['line'])}.", "defined", [fn, s["file"], s["line"]], [d])
    steps = fn_steps(cx, fn)
    desc = describe_steps(cx, fn, steps)
    step_claims = []
    for it in steps:
        if it["kind"] == "call":
            c = it["call"]
            st, why = cx.call_status(c)
            step_claims.append(cx.claim(f"{cx.D(fn)} calls {cx.D(c.callee)} at line {c.line}.", "calls",
                                        [fn, c.callee, c.file, c.line], [cx.ev("call", c.file, c.line, c.callee)], st, why))
        elif it["kind"] == "write":
            o = it["occ"]
            step_claims.append(cx.claim(f"{cx.D(fn)} writes {cx.D(it['sym'])} at line {o['line']}.", "writes",
                                        [fn, it["sym"], o["file"], o["line"]], [cx.ev("write", o["file"], o["line"], it["sym"])]))
        elif it["kind"] == "branch":
            br = it["br"]
            step_claims.append(cx.claim(f"{cx.D(fn)} branches on `{br['cond']}` at line {br['line']}.", "branch",
                                        [fn, br["file"], br["line"]], [cx.ev("branch", br["file"], br["line"], fn)]))
    intro = f"{cx.D(fn)} ({cx.loc(s['file'], s['line'])})"
    if doc:
        intro += f" — {doc.rstrip().rstrip('.')}."
    else:
        intro += "."
    cx.say(intro, [c0])
    if desc:
        if len(desc) > 10:
            important = [d for d in desc[10:] if re.search(r"\bcalls\b", d) and not all(
                _is_utility(cx, k) for k in [c.callee for c in g.out_calls.get(fn, []) if cx.D(c.callee).rstrip("()") in d])]
            shown = desc[:10] + important[:4]
        else:
            shown = desc
        cx.say("Step by step, it " + "; then it ".join(shown) + ("; …" if len(desc) > len(shown) else "") + ".",
               step_claims[:16])
    if not brief:
        txt, cl = how_reached(cx, fn)
        cx.say(txt, cl)
        # state it leaves behind, and who uses it
        written = sorted({it["sym"] for it in steps if it["kind"] == "write"})
        users = defaultdict(set)
        for w in written:
            for o in g.readers(w):
                if o["fn"] and o["fn"] != fn:
                    users[o["fn"]].add(w)
        if users:
            items = []
            cls = []
            for ufn, syms in sorted(users.items(), key=lambda kv: -len(kv[1]))[:4]:
                o = next(o for w in sorted(syms) for o in g.readers(w) if o["fn"] == ufn)
                cls.append(cx.claim(f"{cx.D(ufn)} reads {cx.D(o['sym'])} (line {o['line']}).", "reads",
                                    [ufn, o["sym"], o["file"], o["line"]],
                                    [cx.ev("read", o["file"], o["line"], o["sym"])]))
                items.append(f"{cx.D(ufn)} (uses {', '.join(sorted(cx.D(x) for x in syms)[:4])})")
            cx.say("The values it updates are used later by " + "; ".join(items) + ".", cls)
    cx.pack.related.append({"key": fn, "label": cx.D(fn), "file": s["file"], "line": s["line"], "end_line": s["end_line"]})
    for it in steps:
        if it["kind"] == "call" and it["call"].callee in g.defined:
            cs = g.sym[it["call"].callee]
            cx.pack.related.append({"key": it["call"].callee, "label": cx.D(it["call"].callee), "file": cs["file"],
                                    "line": cs["line"], "end_line": cs["end_line"]})


def explain_variable(cx: Ctx, v: str) -> None:
    g = cx.g
    s = g.sym[v]
    kind_word = {"global": "a global variable", "field": "a field", "property": "a property", "local": "a local variable",
                 "event": "an event",
                 "param": "a parameter", "macro": "a constant (macro)", "enum_const": "an enum constant"}.get(s["kind"], s["kind"])
    d = cx.ev_def(v)
    typ = s.get("type") or ""
    doc = (s.get("doc") or "").strip()
    c0 = cx.claim(f"{cx.D(v)} is {kind_word} of type {typ} defined in {cx.loc(s['file'], s['line'])}.", "defined",
                  [v, s["file"], s["line"]], [d])
    intro = f"{cx.D(v)} is {kind_word}" + (f" of type `{typ}`" if typ and s["kind"] not in ("macro",) else "") + \
            f", defined in {cx.loc(s['file'], s['line'])}"
    intro += f" — the code describes it as: \"{doc}\"." if doc else "."
    if v in g.consts:
        intro += f" Its value is {g.consts[v]}."
    cx.say(intro, [c0])
    writers = g.writers(v)
    readers = g.readers(v)
    wl, wc = [], []
    for o in writers[:6]:
        txt = _code(g.src.line(o["file"], o["line"]))
        wc.append(cx.claim(f"{cx.D(o['fn']) if o['fn'] else o['file']} writes {cx.D(v)} at {cx.loc(o['file'], o['line'])}.",
                           "writes", [o["fn"], v, o["file"], o["line"]], [cx.ev("write", o["file"], o["line"], v)],
                           cx.file_status(o["file"])[0], cfg_why(cx, o.get("cfg"))))
        wl.append(f"{cx.D(o['fn']) if o['fn'] else 'the initializer'} (`{txt}`, line {o['line']})")
    if not writers:
        # initializer flows / never written
        init = [e for e in g.bwd.get(v, []) if e.kind == "init"]
        if init:
            cx.say(f"It is only set by its initializer ({init[0].expr}).")
        else:
            cx.say(f"Nothing in this code assigns {cx.D(v)} after its declaration.")
    else:
        cx.say(f"It is set by {'; '.join(wl)}.", wc)
    rf = defaultdict(list)
    for o in readers:
        rf[o["fn"]].append(o)
    rl, rc = [], []
    for fn, os_ in list(rf.items())[:6]:
        o = os_[0]
        rc.append(cx.claim(f"{cx.D(fn) if fn else o['file']} reads {cx.D(v)} at {cx.loc(o['file'], o['line'])}.", "reads",
                           [fn, v, o["file"], o["line"]], [cx.ev("read", o["file"], o["line"], v)]))
        rl.append(f"{cx.D(fn) if fn else o['file']} (line {o['line']})")
    if readers:
        cx.say(f"It is read by {', '.join(rl)}.", rc)
    # where does it lead? forward slice to outputs
    sink_syms = {io["sym"] for io in g.io if io["dir"] == "sink"}
    path = g.flow_path(v, lambda n: n.split("#", 1)[0] in sink_syms and "#param" in n, "forward")
    sinks = [path[-1].dst] if path else []
    if sinks:
        sk = sinks[0].split("#", 1)[0]
        chain = [_stage_label(st) for st in _stages(cx, path)]
        io = g.io_by_sym[sk][0]
        evs = [cx.ev("flow", e.file, e.line, e.dst) for e in path[:10]]
        c = cx.claim(f"The value of {cx.D(v)} reaches {cx.D(sk)} ({io['api']}).", "flows_to", [v, sinks[0]], evs,
                     INFERENCE if any(_through_field(cx, e) for e in path) else CONFIRMED,
                     "the data path passes through struct fields (all instances of the struct are treated alike)"
                     if any(_through_field(cx, e) for e in path) else "")
        cx.say(f"Following where its value goes: {cx.D(v)} → {' → '.join(chain)} → {cx.D(sk)} — so it ultimately "
               f"influences {cx.D(sk)}, which is an output ({io['api']}).", [c])
    # a function pointer / delegate: what it points to, and who calls through it
    targets = sorted({c.callee for c in g.calls if c.via == v and not c.callee.startswith("?")})
    if targets:
        tc = []
        for fa in g.fnaddr:
            if fa["fn"] in targets and fa["kind"] in ("arg", "assign", "init", "table"):
                tc.append(cx.claim(f"The address of {cx.D(fa['fn'])} is stored at {cx.loc(fa['file'], fa['line'])}.",
                                   "fnaddr", [fa["fn"], fa["dst"], fa["file"], fa["line"]],
                                   [cx.ev("flow", fa["file"], fa["line"], fa["fn"], "function address stored here")],
                                   INFERENCE, "found by following where the pointer is assigned"))
        callers_via = sorted({c.caller for c in g.calls if c.via == v})
        cx.say(f"{cx.D(v)} holds a function: at run time it points to {', '.join(cx.D(t) for t in targets)}" +
               (f" (registered at {', '.join(cx.loc(fa['file'], fa['line']) for fa in g.fnaddr if fa['fn'] in targets and fa['kind'] == 'arg')})"
                if any(fa['fn'] in targets and fa['kind'] == 'arg' for fa in g.fnaddr) else "") +
               (f"; it is called by {', '.join(cx.D(x) for x in callers_via)}" if callers_via else "") + ".", tc)
    purpose = _purpose_line(cx, v, writers, readers, sinks)
    if purpose:
        cx.say(purpose)
    cx.pack.related.append({"key": v, "label": cx.D(v), "file": s["file"], "line": s["line"], "end_line": s["end_line"]})


def _purpose_line(cx: Ctx, v: str, writers, readers, sinks) -> str:
    wf = sorted({cx.D(o["fn"]) for o in writers if o["fn"]})
    rf = sorted({cx.D(o["fn"]) for o in readers if o["fn"]})
    if not wf and not rf:
        return ""
    s = f"In short: {cx.D(v)} carries a value"
    if wf:
        s += f" produced by {', '.join(wf[:3])}"
    if rf:
        s += f" and consumed by {', '.join(rf[:3])}"
    if sinks:
        s += f", feeding {cx.D(sinks[0].split('#', 1)[0])}"
    both = set(wf) & set(rf)
    if both:
        s += f". {', '.join(sorted(both))} both reads and updates it, so it also keeps state between calls"
    return s + "."


def cfg_why(cx: Ctx, cfg: str | None) -> str:
    return cx.cfg_note(cfg)


def _through_field(cx: Ctx, e: Edge) -> bool:
    for k in (e.src, e.dst):
        s = cx.g.sym.get(k)
        if s and s["kind"] == "field":
            return True
    return False


# ================================================================================================= paths over flows
def _edge_path(edges: list[Edge], start: str, goal: str, forward: bool = True, prefer=None) -> list[Edge]:
    """Cheapest path start→goal over slice edges (forward) or goal←start (backward edges given as src→dst)."""
    adj: dict[str, list[Edge]] = defaultdict(list)
    for e in edges:
        if forward:
            adj[e.src].append(e)
        else:
            adj[e.dst].append(e)
    dist = {start: 0.0}
    prev: dict[str, Edge] = {}
    pq = [(0.0, start)]
    while pq:
        d, x = heapq.heappop(pq)
        if x == goal:
            break
        if d > dist.get(x, 1e18):
            continue
        for e in adj.get(x, []):
            y = e.dst if forward else e.src
            w = 1.0 if prefer is None else prefer(y)
            if d + w < dist.get(y, 1e18):
                dist[y] = d + w
                prev[y] = e
                heapq.heappush(pq, (d + w, y))
    if goal not in prev and goal != start:
        return []
    path = []
    cur = goal
    while cur != start and cur in prev:
        e = prev[cur]
        path.append(e)
        cur = e.src if forward else e.dst
    path.reverse()
    return path


def _node_fn(cx: Ctx, key: str) -> str | None:
    f = cx.g.fn_of(key)
    return f


def _fn_chain(cx: Ctx, path: list[Edge]) -> list[str]:
    """Collapse a value-flow path into readable stages (functions and shared data)."""
    out: list[str] = []
    for e in path:
        for k in (e.src, e.dst):
            f = _node_fn(cx, k)
            label = cx.D(f) if f else cx.D(k)
            if f and cx.g._external(f) and k.endswith("#param:0") is False and not k.endswith("#ret"):
                continue
            if not out or out[-1] != label:
                out.append(label)
    return out


UTILITY_RE = re.compile(r"(^|_)(wrap|clamp|min|max|abs|sqr|sqrt|round|swap|lerp|sat|limit|norm|deg|rad|hex|crc|"
                        r"checksum|strn?cpy|memcpy|memset|strcmp|atoi|atof)", re.I)


def _is_utility(cx: Ctx, fn: str | None) -> bool:
    if not fn:
        return False
    g = cx.g
    if g._external(fn):
        return True
    callers = {c.caller for c in g.callers(fn)}
    return len(callers) >= 2 or bool(UTILITY_RE.search(g.name(fn)))


def _stages(cx: Ctx, path: list[Edge]) -> list[dict[str, Any]]:
    """Readable pipeline stages from a flow path: each stage = a function (or shared variable) + the lines that move
    the data on, with evidence. Utility calls that return to their caller (wrap180(), fmod()…) are folded into the
    caller's stage and listed as `uses`."""
    owner: dict[int, str | None] = {}
    helper_of_edge: dict[int, str] = {}
    stack: list[tuple[str | None, int, str | None]] = []       # (caller, index of arg edge, callee)
    for i, e in enumerate(path):
        returning = (e.src.endswith("#ret") and e.kind != "fnptr_ret") or e.kind in ("outparam", "api")
        if e.kind == "arg":
            stack.append((e.fn, i, _node_fn(cx, e.dst)))
        elif returning and stack:
            caller, j, callee = stack.pop()
            if _is_utility(cx, callee):
                for k in range(j, i):            # the outermost caller wins (inner frames closed earlier)
                    owner[k] = caller
                    helper_of_edge[k] = callee
        if "#" not in e.dst:
            stack.clear()
    stages: list[dict[str, Any]] = []
    for i, e in enumerate(path):
        f_dst = _node_fn(cx, e.dst)
        f_src = _node_fn(cx, e.src)
        where = owner.get(i) or e.fn or f_dst or f_src
        label = cx.D(where) if where else cx.D(e.dst)
        if i in helper_of_edge:
            u = cx.D(helper_of_edge[i])
            if stages and stages[-1]["label"] == label:
                if u not in stages[-1]["uses"]:
                    stages[-1]["uses"].append(u)
                continue
            what = f"uses {u}"
            ev = cx.ev("flow", e.file, e.line, e.dst, what)
            stages.append({"label": label, "fn": where, "file": e.file, "line": e.line, "steps": [(what, e, ev)],
                           "uses": [u]})
            continue
        what = _edge_phrase(cx, e)
        ev = cx.ev("flow", e.file, e.line, e.dst, what)
        if stages and stages[-1]["label"] == label:
            stages[-1]["steps"].append((what, e, ev))
            continue
        stages.append({"label": label, "fn": where, "file": e.file, "line": e.line, "steps": [(what, e, ev)], "uses": []})
    # "A → B → A": the second A is the caller using B's result
    for i in range(2, len(stages)):
        if stages[i]["label"] == stages[i - 2]["label"]:
            stages[i]["label_display"] = f"back in {stages[i]['label']}"
    return stages


def _stage_label(st: dict) -> str:
    lab = st.get("label_display") or st["label"]
    return lab + (f" (uses {', '.join(st['uses'][:3])})" if st["uses"] else "")


def _code(t: str) -> str:
    t = re.sub(r"/\*.*?\*/\s*$", "", t.strip())       # trailing /* comment */
    t = re.sub(r"(?<!:)//.*$", "", t)                   # trailing // comment (not inside URLs)
    return re.sub(r"\s+", " ", t.strip().rstrip(";").strip())


def _edge_phrase(cx: Ctx, e: Edge) -> str:
    txt = _code(cx.g.src.line(e.file, e.line))
    if e.kind == "arg":
        return f"passes {e.expr} to {cx.D(e.dst.split('#')[0])}"
    if e.kind == "fnptr_arg":
        return f"calls {cx.D(e.dst.split('#')[0])} through a function pointer"
    if e.kind == "outparam":
        return f"{cx.D(e.src.split('#')[0])} fills in {cx.D(e.dst)}"
    if e.kind == "ret":
        return f"returns `{txt[6:].strip() if txt.startswith('return') else txt}`"
    return f"`{txt}`"


# ================================================================================================= recipes
def _named_free_macro(cx: Ctx) -> str | None:
    fms = _free_macros(cx)
    for m in re.findall(r"\b[A-Z_][A-Z0-9_]{2,}\b", cx.r.question):
        if m in fms:
            return m
    return None


def r_explain(cx: Ctx) -> None:
    fm = _named_free_macro(cx)
    if fm:
        return r_config(cx, fm)
    tg = cx.targets()
    if tg and not any(m.get("explicit") for m in cx.r.mentions):
        # described in words: the symbol whose (run-together) name covers most of them; behaviour ("how does …")
        # prefers functions
        g0 = cx.g
        concepts = _concepts(cx, {"how", "does", "what", "works", "work"})
        allc = set().union(*concepts) if concepts else set()
        how = bool(re.search(r"^\s*how (does|do|is|are)\b", cx.r.question, re.I))
        wrap_q = bool(re.search(r"\bwraps?( around)?\b|\broll(s|ing)? over\b|\bwrap-?around\b", cx.r.question, re.I))

        def wraps(k: str) -> int:
            # "how does the id wrap around?": code that actually wraps — modulo, a reset to 0/1 after reaching a
            # maximum, or a power-of-two mask
            if not wrap_q or k not in g0.defined or g0.sym[k]["kind"] not in FN_KINDS:
                return 0
            f, a, b = cx.body_lines(k)
            body = " ".join(STRING_RE.sub('""', _code(t)) for _, t in g0.src.snippet(f, a, min(b, a + 40))) if f else ""
            return 1 if re.search(r"\w\s*%\s*\w|\?\s*\(?\w*\)?\s*[01]\s*:|(>=|==|>)\s*\w*(MAX|Max|SIZE|Size|LEN|Len)\w*[^;]*"
                                  r"(=\s*[01]\b|\?)|(>=|==|>)\s*\d+\s*\)\s*\{?\s*\w+\s*=\s*[01]\b|"
                                  r"&\s*\(\s*\w+\s*-\s*1\s*\)", body) else 0
        tg = [t for _, t in sorted(enumerate(cx.targets_ranked(k=20) or tg), key=lambda it: (
            -_coverage(concepts, _name_words(g0.name(it[1]), allc)) - wraps(it[1]),
            -(1 if how and g0.sym.get(it[1], {}).get("kind") in FN_KINDS else 0), it[0]))]
        cov = {t: _coverage(concepts, _name_words(g0.name(t), allc)) + wraps(t) +
               (0.5 if how and g0.sym.get(t, {}).get("kind") in FN_KINDS else 0) - 0.001 * i for i, t in enumerate(tg)}
        loc_e = lambda k: Loc(k, (g0.sym.get(k) or {}).get("file"), None, None)      # noqa: E731
        tg = cx.learn.rank(tg, loc_e, lambda k: cov.get(k, 0.0))
        pf = cx.learn.pin_function()
        if pf and pf in g0.sym:
            cx.learn.use_pin(changed=not tg or tg[0] != pf)
            tg = [pf] + [t for t in tg if t != pf]
        cx.learn.offer([t for t in tg if cx.about_tests or not g0.is_test(t)] or tg, loc_e,
                       lambda k: (_fn_label(cx, k), _render_function(cx, k) if k in g0.defined else cx.D(k)),
                       lambda k: cov.get(k, 0.0), what="symbol")
    if tg and not cx.about_tests:
        tg = [t for t in tg if not cx.g.is_test(t)] or tg     # the program's code, not a unit test named like it
    if not tg:
        return r_search(cx)
    key = tg[0]
    s = cx.g.sym.get(key)
    if s and s["kind"] in FN_KINDS and key not in cx.g.defined:
        # an output / library function with no code here: what matters is the code that drives it
        callers = [c for c in cx.g.callers(key) if c.caller in cx.g.defined]
        io = cx.g.io_by_sym.get(key)
        c0 = cx.claim(f"{cx.D(key)} is declared at {cx.loc(s['file'], s['line'])}.", "defined", [key, s["file"], s["line"]],
                      [cx.ev_def(key)]) if s.get("file") else None
        cx.say(f"{cx.D(key)} has no code in this project" + (f" — it is an output ({io[0]['api']})" if io else "") +
               (f" ({cx.loc(s['file'], s['line'])})" if s.get("file") else "") + ". The decision is made by the code that "
               f"calls it:", [c0] if c0 is not None else [])
        seen = set()
        for c in callers[:2]:
            if c.caller in seen:
                continue
            seen.add(c.caller)
            explain_function(cx, c.caller)
        return
    if s and s["kind"] in FN_KINDS:
        explain_function(cx, key)
    elif s and s["kind"] in VAR_KINDS + ("macro", "enum_const"):
        explain_variable(cx, key)
    elif s and s["kind"] in ("struct", "union", "class", "record", "typedef", "enum", "interface"):
        explain_type(cx, key)
    else:
        return r_search(cx)
    _note_alternatives(cx, tg, key)


def _note_alternatives(cx: Ctx, tg: list[str], chosen: str) -> None:
    same = [t for t in tg[1:] if cx.g.name(t) == cx.g.name(chosen)]
    if same:
        cx.pack.assumptions.append(f"Several symbols are named {cx.g.name(chosen)}; this answer is about the one in "
                                   f"{cx.loc(*cx.g.loc(chosen))}. Others: " +
                                   ", ".join(cx.loc(*cx.g.loc(t)) for t in same[:4]))


def r_purpose(cx: Ctx) -> None:
    r_explain(cx)


def explain_type(cx: Ctx, t: str) -> None:
    g = cx.g
    s = g.sym[t]
    fields = [k for k, v in g.sym.items() if v.get("container") == t and v["kind"] == "field"]
    if not fields and s["kind"] == "typedef":
        for k, v in g.sym.items():
            if v["kind"] in ("struct", "union") and v["name"] == s["name"]:
                t2 = k
                fields = [k2 for k2, v2 in g.sym.items() if v2.get("container") == t2 and v2["kind"] == "field"]
                break
    c0 = cx.claim(f"{s['name']} is defined in {cx.loc(s['file'], s['line'])}.", "defined", [t, s["file"], s["line"]],
                  [cx.ev_def(t)])
    fl = []
    for f in sorted(fields, key=lambda k: g.sym[k]["line"])[:14]:
        fs = g.sym[f]
        fl.append(f"`{fs['name']}`" + (f" ({fs['doc']})" if fs.get("doc") else ""))
    cx.say(f"{s['name']} ({s['kind']}, {cx.loc(s['file'], s['line'])})" + (f" — {s['doc']}" if s.get("doc") else "") +
           (f". It holds: {', '.join(fl)}." if fl else "."), [c0])
    subs = cx.st.q("SELECT sub, kind FROM inherit WHERE base=? AND sub LIKE 'T:%'", (t,))
    if subs:
        cl = []
        names = []
        for r in subs:
            ss = g.sym.get(r["sub"])
            if ss:
                cl.append(cx.claim(f"{ss['name']} {'implements' if r['kind'] == 'implements' else 'extends'} {s['name']} "
                                   f"({cx.loc(ss['file'], ss['line'])}).", "defined", [r["sub"], ss["file"], ss["line"]],
                                   [cx.ev_def(r["sub"])]))
                names.append(f"{ss['name']} ({cx.loc(ss['file'], ss['line'])})")
        verb = "implemented by" if s["kind"] == "interface" else "extended by"
        cx.say(f"{s['name']} is {verb}: {', '.join(names)}.", cl)
        # which implementation runs where it is called
        for r in cx.st.q("SELECT DISTINCT c.file, c.line, c.caller FROM call c JOIN inherit i ON i.base = c.callee "
                         "WHERE c.kind IN ('interface','virtual') AND c.callee LIKE ? AND c.derived=0 LIMIT 3",
                         (t.replace("T:", "M:") + ".%",)):
            cx.say(f"It is called through the {s['kind']} in {cx.D(r['caller'])} ({cx.loc(r['file'], r['line'])}); which "
                   f"implementation runs depends on the object created at runtime.")
    bases = cx.st.q("SELECT base, kind FROM inherit WHERE sub=?", (t,))
    if bases:
        bn = [g.sym[b["base"]]["name"] if b["base"] in g.sym else b["base"].split(".")[-1] for b in bases]
        cx.say(f"{s['name']} derives from / implements: {', '.join(bn)}.")
    users = sorted({k for k, v in g.sym.items() if v["kind"] in FN_KINDS and s["name"] in (v.get("sig") or "")})
    if users:
        cx.say(f"Functions that take or return it: {', '.join(cx.D(u) for u in users[:8])}.")


def r_search(cx: Ctx) -> None:
    fm = _named_free_macro(cx)
    if fm:
        return r_config(cx, fm)
    cx.fell_back = True                       # a weak answer: the caller may try the all-features build
    hits = cx.s.search(cx.r.question, k=8)
    if not hits:
        cx.pack.unknowns.append("Nothing in the index matches this question.")
        cx.say("I could not find code related to this question in the indexed project.")
        return
    lines = []
    cls = []
    for h in hits[:6]:
        e = cx.ev_def(h.key)
        cls.append(cx.claim(f"{cx.D(h.key)} is defined at {cx.loc(h.file, h.line)}.", "defined", [h.key, h.file, h.line], [e]))
        lines.append(f"{cx.D(h.key)} ({KIND_WORDS.get(h.kind, h.kind)}, {cx.loc(h.file, h.line)})")
        cx.pack.related.append({"key": h.key, "label": cx.D(h.key), "file": h.file, "line": h.line})
    cx.say("These parts of the code look most relevant: " + "; ".join(lines) + ".", cls)
    top = hits[0]
    if top.kind in FN_KINDS:
        explain_function(cx, top.key, brief=True)


# ------------------------------------------------------------------------------------------------- flow
def topic_nodes(cx: Ctx) -> set[str]:
    tw = expand_words(cx.r.topic) - WEAK_TOPIC
    out = set()
    for k, s in cx.g.sym.items():
        words = _name_words(s["name"], tw) | set(split_ident(s.get("type") or ""))
        if words & tw:
            out.add(k)
    return out


def _specific_var(cx: Ctx) -> str | None:
    """A question about ONE value ("the speed value") rather than a whole pipeline ("GPS data")."""
    g = cx.g
    for m in cx.r.mentions:
        for c in m["candidates"]:
            if g.sym.get(c, {}).get("kind") in ("global", "field", "property"):
                return c
    tw = set(cx.r.topic)
    best_fn = max((len(set(split_ident(g.sym[f]["name"])) & tw) for f in g.defined), default=0)
    for w in cx.r.topic:
        n_all = sum(1 for s in g.sym.values() if w in split_ident(s["name"]))
        if n_all == 0 or n_all > 8:
            continue
        cands = [k for k, s in g.sym.items() if s["kind"] in ("field", "global", "property") and w in split_ident(s["name"])
                 and s.get("file")]
        if cands:
            best_var = max(len(set(split_ident(g.sym[c]["name"])) & tw) for c in cands)
            if best_fn > best_var:
                return None          # the question names a function ("the bank command"), not one value
            cx._var_copies = cands
            # the most UPSTREAM candidate: the others are derived from it
            for c in cands:
                sl = g.slice(c, "forward", max_nodes=500)
                if any(o in sl["nodes"] for o in cands if o != c):
                    return c
            return cands[0]
    return None


def r_flow(cx: Ctx) -> None:
    g = cx.g
    if r_call_path(cx):
        return
    v = _specific_var(cx)
    if v:
        r_origin_of(cx, v)
        copies = [c for c in getattr(cx, "_var_copies", [v]) if c != v]
        uses = g.readers(v) + [o for c in copies for o in g.readers(c)]
        for c in copies[:3]:
            for o in g.writers(c)[:1]:
                cx.say(f"It is copied into {cx.D(c)} by {cx.D(o['fn'])} (`{_code(g.src.line(o['file'], o['line']))}`, "
                       f"{cx.loc(o['file'], o['line'])}).",
                       [cx.claim(f"{cx.D(o['fn'])} writes {cx.D(c)} at {cx.loc(o['file'], o['line'])}.", "writes",
                                 [o["fn"], c, o["file"], o["line"]], [cx.ev("write", o["file"], o["line"], c)])])
        # passed on as an argument: into which functions
        for e in g.fwd.get(v, []):
            if e.kind == "arg":
                callee = e.dst.split("#", 1)[0]
                cx.say(f"{cx.D(e.fn)} passes it to {cx.D(callee)} (`{e.expr}`, {cx.loc(e.file, e.line)}).",
                       [cx.claim(f"{cx.D(e.fn)} passes {cx.D(v)} to {cx.D(callee)} at {cx.loc(e.file, e.line)}.", "flow_edge",
                                 [e.src, e.dst, e.file, e.line], [cx.ev("flow", e.file, e.line, e.dst)])])
        uf = sorted({o["fn"] for o in uses if o["fn"]})
        if uf:
            ucl = [cx.claim(f"{cx.D(o['fn'])} reads {cx.D(v)} at {cx.loc(o['file'], o['line'])}.", "reads",
                            [o["fn"], v, o["file"], o["line"]], [cx.ev("read", o["file"], o["line"], v)]) for o in uses[:6] if o["fn"]]
            cx.say(f"Where it is used: {', '.join(cx.D(f) for f in uf[:8])}.", ucl)
        sink_syms = {io["sym"] for io in g.io if io["dir"] == "sink"}
        path = g.flow_path(v, lambda n: n.split("#", 1)[0] in sink_syms and "#param" in n, "forward")
        if path:
            chain = [_stage_label(st) for st in _stages(cx, path)]
            sk = path[-1].dst.split("#", 1)[0]
            cx.say(f"From there it continues: {cx.D(v)} → {' → '.join(chain)} → {cx.D(sk)}.")
        return
    tnodes = topic_nodes(cx)
    explicit = [c for m in cx.r.mentions for c in m["candidates"] if m.get("explicit")]
    sources = sorted({(io["sym"]) for io in g.io if io["dir"] == "source"})
    sinks = sorted({io["sym"] for io in g.io if io["dir"] == "sink"})
    best = None
    sink_set = set(sinks)

    def pref(node, tn=tnodes):
        return 0.35 if (node in tn or node.split("#", 1)[0] in tn) else 1.0
    for src in sources:
        sl = g.slice(src, "forward", max_nodes=3000, max_depth=200)
        hit_topic = [n for n in sl["nodes"] if n in tnodes or (n.split("#", 1)[0] in tnodes)]
        if tnodes and not hit_topic:
            continue
        reach = {n.split("#", 1)[0] for n in sl["nodes"] if "#param" in n} & sink_set
        for sk in sorted(reach):
            path = g.flow_path(src, lambda n, sk=sk: n.split("#", 1)[0] == sk and "#param" in n, "forward", pref)
            if not path:
                continue
            cover = sum(1 for e in path if e.dst in tnodes or e.dst.split("#", 1)[0] in tnodes)
            score = cover * 2 - len(path) * 0.1
            if explicit:
                score += 5 * sum(1 for e in path if e.dst in explicit or e.dst.split("#", 1)[0] in explicit or e.fn in explicit)
            if best is None or score > best[0]:
                best = (score, src, sk, path)
    if best is None:
        # no source→sink chain: describe what we can from the topic's central function
        tg = cx.targets(FN_KINDS)
        if tg:
            cx.say(f"I could not trace a complete path from an input to an output for this topic; here is the central "
                   f"function instead.")
            cx.pack.unknowns.append("No complete input→output data path was found (inputs or outputs may be outside "
                                    "this code, or reached only through calls the analysis could not resolve).")
            explain_function(cx, tg[0])
        else:
            r_search(cx)
        return
    _, src, sk, path = best
    stages = _stages(cx, path)
    src_io = g.io_by_sym[src][0]
    sk_io = g.io_by_sym[sk][0]
    field_path = any(_through_field(cx, e) for e in path)
    fnptr = any(e.kind in ("fnptr_arg", "fnptr_ret") for e in path)
    status = INFERENCE if (field_path or fnptr or any(cx.file_status(e.file)[0] != CONFIRMED for e in path)) else CONFIRMED
    why = []
    if field_path:
        why.append("the path passes through struct fields (every instance of the struct is treated alike)")
    if fnptr:
        why.append("one step is a call through a function pointer, resolved by following its assignment")
    evs = [st["steps"][0][2] for st in stages]
    c_all = cx.claim(f"Data read from {cx.D(src)} ({src_io['api']}) flows to {cx.D(sk)} ({sk_io['api']}).", "flows_to",
                     [src, path[-1].dst], evs, status, "; ".join(why))
    # stage-by-stage flow steps (the "How the data flows" chain)
    src_ev = cx.ev("io", src_io["file"], src_io["line"], src, f"input: {src_io['api']}")
    cx.flow_step(f"{cx.D(src_io['fn']) if src_io['fn'] else cx.D(src)} reads {cx.D(src)}", src_io["file"], src_io["line"],
                 src_ev, src_io["api"])
    stage_claims = []
    for k, stg in enumerate(stages):
        what = stg["steps"][0][0]
        e0 = stg["steps"][0][1]
        if not (k == 0 and src_io["fn"] and stg["fn"] == src_io["fn"]):
            cx.flow_step(_stage_label(stg), e0.file, e0.line, stg["steps"][0][2], "; ".join(s[0] for s in stg["steps"][:3]))
        pred = "flow_edge" if e0.kind not in ("api",) else "api_step"
        stage_claims.append(cx.claim(f"{_stage_label(stg)}: {what}.", pred, [e0.src, e0.dst, e0.file, e0.line],
                                     [stg["steps"][0][2]], INFERENCE if _through_field(cx, e0) else cx.file_status(e0.file)[0]))
    sk_ev = cx.ev("io", sk_io["file"], sk_io["line"], sk, f"output: {sk_io['api']}")
    if not cx.pack.flow or cx.pack.flow[-1]["label"] != cx.D(sk):
        cx.flow_step(cx.D(sk), sk_io["file"], sk_io["line"], sk_ev, sk_io["api"])
    pretty = []
    for st in stages:
        lab = _stage_label(st)
        if not pretty or pretty[-1] != lab:
            pretty.append(lab)
    topic = " ".join(cx.r.topic[:2]) if cx.r.topic else "the data"
    cx.say(f"The {topic} data enters the program at {cx.D(src_io['fn']) if src_io['fn'] else cx.D(src)}, which reads "
           f"{cx.D(src)} ({src_io['api']}, {cx.loc(src_io['file'], src_io['line'])}).", [c_all])
    cx.say("From there it passes through: " + " → ".join(pretty) + ".", stage_claims[:10])
    # narrate the key transformations (calls to functions that change the representation)
    key_moves = []
    for st in stages:
        for what, e, ev in st["steps"]:
            if e.kind in ("assign", "init") and ("#ret" in e.src and not cx.g._external(e.src.split("#")[0]) or
                                                 _through_field(cx, e)):
                key_moves.append(f"{st['label']}: {what} ({cx.loc(e.file, e.line)})")
    if key_moves:
        cx.say("Key steps: " + "; ".join(key_moves[:6]) + ".")
    cx.say(f"Finally it reaches {cx.D(sk)} ({sk_io['api']}, {cx.loc(sk_io['file'], sk_io['line'])}).", [c_all])
    if fnptr:
        vias = sorted({e.src.split("#")[0] for e in path if e.kind == "fnptr_arg"})
        cx.pack.assumptions.append("The call through " + ", ".join(cx.D(v) for v in vias) +
                                   " was resolved by following where the function pointer is assigned.")
    _add_path_calls(cx, stages)
    for stg in stages:
        if stg["fn"] and stg["fn"] in g.defined:
            s = g.sym[stg["fn"]]
            cx.pack.related.append({"key": stg["fn"], "label": cx.D(stg["fn"]), "file": s["file"], "line": s["line"],
                                    "end_line": s["end_line"]})


def _add_path_calls(cx: Ctx, stages: list[dict]) -> None:
    """Execution path for the flow: the call chain from an entry point to each stage's function."""
    g = cx.g
    fns = [st["fn"] for st in stages if st["fn"] in g.defined]
    if not fns:
        return
    last = fns[-1]
    for p in g.paths_from_entries(last, limit=1):
        steps = [{"label": cx.D(p[0].caller) if p else cx.D(last), "file": g.sym[p[0].caller]["file"] if p else None,
                  "line": g.sym[p[0].caller]["line"] if p else None}]
        for c in p:
            st, why = cx.call_status(c)
            steps.append({"label": f"{cx.D(c.callee)}", "file": c.file, "line": c.line,
                          "ev": cx.ev("call", c.file, c.line, c.callee), "status": st, "note": why})
        cx.pack.path = steps


# ------------------------------------------------------------------------------------------------- origin
def _same_name_fields(cx: Ctx) -> list[str]:
    """An identifier the question writes that names fields of several different structs (CANrxData in the SDO server,
    the SDO client, the PDOs…)."""
    g = cx.g
    for m in cx.r.mentions:
        if not m.get("explicit"):
            continue
        fields = [c for c in m["candidates"] if g.sym.get(c, {}).get("kind") == "field" and
                  (cx.about_tests or not g.is_test(c))]
        if len({g.sym[c].get("container") for c in fields}) > 1:
            return fields
    return []


def r_origin(cx: Ctx) -> None:
    g = cx.g
    same = _same_name_fields(cx)
    if same:
        # ambiguous: say where each one is filled, then trace the one with the most direct writers
        parts, cl = [], []
        for f in same[:5]:
            ws = [o for o in g.writers(f) if o.get("fn") and (cx.about_tests or not g.is_test(o["fn"]))][:2]
            if not ws:
                parts.append(f"{cx.D(f)}: not assigned in this code")
                continue
            for o in ws[:1]:
                cl.append(cx.claim(f"{cx.D(o['fn'])} writes {cx.D(f)} at {cx.loc(o['file'], o['line'])}.", "writes",
                                   [o["fn"], f, o["file"], o["line"]], [cx.ev("write", o["file"], o["line"], f)]))
            parts.append(f"{cx.D(f)} is filled by {cx.D(ws[0]['fn'])} (`{_stmt(g, ws[0]['file'], ws[0]['line'])}`, "
                         f"{cx.loc(ws[0]['file'], ws[0]['line'])})")
        cx.say(f"`{g.sym[same[0]]['name']}` is a field of {len({g.sym[c].get('container') for c in same})} different "
               f"structs: " + "; ".join(parts) + ".", cl)
    tg = cx.targets(VAR_KINDS)
    if not any(m.get("explicit") for m in cx.r.mentions):
        # described in words ("the DHCP offered IP address"): the variable whose full name (struct.field) covers the
        # most of those words, keeping search order among equals
        tg = [t for t in cx.targets_ranked(VAR_KINDS, k=20) if cx.about_tests or not g.is_test(t)] or tg
        concepts = _concepts(cx, {"come", "comes", "from", "originate", "where"})
        tg = [t for _, t in sorted(enumerate(tg), key=lambda it: (
            -_coverage(concepts, set(split_ident(g.sym[it[1]].get("qual") or g.name(it[1])))), it[0]))]
    if not tg:
        return r_search(cx)
    r_origin_of(cx, None, tg)


def r_origin_of(cx: Ctx, v0: str | None, tg: list[str] | None = None) -> None:
    g = cx.g
    tg = tg or [v0]
    # prefer the most "downstream" variable among same-named candidates (e.g. g_nav.latitude over fix.latitude)
    name = g.name(tg[0])
    best = tg[0]
    cands = [t for t in tg if g.sym[t]["name"] == g.sym[tg[0]]["name"]]
    if len(cands) > 1:
        for c in cands:
            sl = g.slice(c, "backward", max_nodes=400)
            if any(o in sl["nodes"] for o in cands if o != c):
                best = c
                break
    v = v0 or best
    sl = g.slice(v, "backward", max_nodes=1500, max_depth=150)
    terms = [t for t in sl["terminals"] if t != v]
    cyclic = False
    if not terms and len(sl["nodes"]) > 2:
        # a pointer handed to a function that may fill it makes the flow graph cyclic (`deserialize(&topic)`), so
        # nothing is a terminal: the sources are the nodes nothing deeper flows into — the code that produces the value
        dep = sl["nodes"]
        into: dict[str, list[int]] = defaultdict(list)
        for e in sl["edges"]:
            into[e.dst].append(dep.get(e.src, 0))
        terms = sorted((n for n, d in dep.items() if n != v and d >= 2 and all(x <= d for x in into.get(n, []))),
                       key=lambda n: -dep[n])[:60]
        cyclic = bool(terms)
    s = g.sym[v]
    cx.say(f"{cx.D(v)} ({s['kind']}, {cx.loc(s['file'], s['line'])})" + (f" — \"{s['doc']}\"." if s.get("doc") else "."),
           [cx.claim(f"{cx.D(v)} is defined at {cx.loc(s['file'], s['line'])}.", "defined", [v, s["file"], s["line"]],
                     [cx.ev_def(v)])])
    if not terms:
        writers = g.writers(v)
        if writers:
            explain_variable(cx, v)
        else:
            cx.say(f"Nothing in this code assigns {cx.D(v)}; its value must come from outside this code.")
        return
    # rank origins: real inputs first
    def rank(t):
        base = t.split("#", 1)[0]
        return (0 if base in g.io_by_sym else 1 if "#param" in t else 2, sl["nodes"].get(t, 99))
    terms.sort(key=rank)
    # among real inputs, the main carrier of the value: the path that matches the question's topic, then the longest
    inputs_ = [t for t in terms if g.is_input(t)][:5]
    scored = []
    for t in inputs_:
        pth = g.flow_path(v, t, "backward")
        words = set()
        for e in pth:
            words |= set(split_ident(g.name(e.src)))
        scored.append((len(words & set(cx.r.topic)), len(pth), t, pth))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    if scored:
        origin, path = scored[0][2], scored[0][3]
    else:
        # no real input among the origins: take the path that enters v through one of its actual (non-constant)
        # assignments, carries data out of a buffer, and passes through code named like the question
        wlines = {(o["file"], o["line"]) for o in g.writers(v)
                  if re.search(r"[A-Za-z_]", re.sub(r"\b(0x[0-9a-fA-F]+|\d+|NULL|0)\b", "",
                                                    _code(g.src.line(o["file"], o["line"])).split("=", 1)[-1]))}
        tw = expand_words(cx.r.topic)
        best_t = None
        for t in terms[:60]:
            pth = g.flow_path(v, t, "backward")
            if not pth:
                continue
            sc = (3 if (pth[-1].file, pth[-1].line) in wlines else 0) + (2 if any(e.kind == "overlay" for e in pth) else 0) + \
                len({w for e in pth if e.fn for w in split_ident(g.name(e.fn))} & tw) + \
                (len({e.fn for e in pth if e.fn}) / 4 if cyclic else -len(pth) / 20)   # cyclic: the deepest producer
            if best_t is None or sc > best_t[0]:
                best_t = (sc, t, pth)
        origin = best_t[1] if best_t else terms[0]
        path = best_t[2] if best_t else g.flow_path(v, origin, "backward")
    fwd_path = path
    stages = _stages(cx, fwd_path)
    what = g.classify_origin(origin)
    evs = [st["steps"][0][2] for st in stages]
    field_path = any(_through_field(cx, e) for e in path)
    c1 = cx.claim(f"{cx.D(v)} gets its value from {cx.D(origin)} ({what}).", "flows_to", [origin, v], evs,
                  INFERENCE if field_path else CONFIRMED,
                  "the path passes through struct fields (every instance treated alike)" if field_path else "")
    # immediate writers (the most concrete answer)
    wr = g.writers(v)
    wl, wc = [], []
    for o in wr[:4]:
        txt = _code(g.src.line(o["file"], o["line"]))
        wc.append(cx.claim(f"{cx.D(o['fn'])} assigns {cx.D(v)} at {cx.loc(o['file'], o['line'])}.", "writes",
                           [o["fn"], v, o["file"], o["line"]], [cx.ev("write", o["file"], o["line"], v)]))
        wl.append(f"`{txt}` in {cx.D(o['fn'])} ({cx.loc(o['file'], o['line'])})")
    if wl:
        cx.say(f"It is assigned by {'; '.join(wl)}.", wc)
    # read through a pointer parameter (`… = msg_in->yiaddr`): where the callers get that pointer from
    for o in wr[:2]:
        fn_o = o.get("fn")
        if not fn_o:
            continue
        line = _code(g.src.line(o["file"], o["line"]))
        written_base = re.match(r"^\W*([A-Za-z_]\w*)", re.sub(r"^[A-Za-z_]\w*\s*\(", "", line))
        params = {g.sym[k]["name"]: k for k in g.occ_keys_of(fn_o) if "#param:" in k and k in g.sym}
        for base in dict.fromkeys(re.findall(r"\b([A-Za-z_]\w*)\s*(?:->|\.)\s*[A-Za-z_]", line)):
            if base not in params or (written_base and base == written_base.group(1)):
                continue
            idx = params[base].rsplit("#param:", 1)[1]
            if not idx.isdigit():
                continue
            i = int(idx)
            calls = [c for c in g.callers(fn_o) if len(c.args) > i and (cx.about_tests or not g.is_test(c.caller))][:3]
            if not calls:
                continue
            ccl, parts = [], []
            for c in calls:
                st, why = cx.call_status(c)
                ccl.append(cx.claim(f"{cx.D(c.caller)} calls {cx.D(fn_o)} at {cx.loc(c.file, c.line)}.", "calls",
                                    [c.caller, fn_o, c.file, c.line], [cx.ev("call", c.file, c.line, fn_o)], st, why))
                src = [x for x in _arg_sources(cx, c) if c.args[i].strip() in x]
                parts.append(f"{cx.D(c.caller)} passes `{c.args[i]}` ({cx.loc(c.file, c.line)})" +
                             (f", set by {src[0]}" if src else ""))
            cx.say(f"`{base}` is a parameter of {cx.D(fn_o)}: " + "; ".join(parts) + ".", ccl)
            break
    chain = []
    for st in stages:
        lab = _stage_label(st)
        if not chain or chain[-1] != lab:
            chain.append(lab)
    cx.say(f"Tracing back further, the value originates at {cx.D(origin)} ({what}) and travels: " +
           " → ".join(chain + [cx.D(v)]) + ".", [c1])
    for st in stages:
        e0 = st["steps"][0][1]
        cx.flow_step(_stage_label(st), e0.file, e0.line, st["steps"][0][2], "; ".join(x[0] for x in st["steps"][:3]))
    cx.flow_step(cx.D(v), s["file"], s["line"], cx.ev_def(v))
    for _, _, t2, p2 in scored[1:4]:
        st2 = _stages(cx, p2)
        ch2 = []
        for st in st2:
            lab = _stage_label(st)
            if not ch2 or ch2[-1] != lab:
                ch2.append(lab)
        evs2 = [st["steps"][0][2] for st in st2]
        c2 = cx.claim(f"{cx.D(v)} also depends on {cx.D(t2)} ({g.classify_origin(t2)}).", "flows_to", [t2, v], evs2,
                      INFERENCE if any(_through_field(cx, e) for e in p2) else CONFIRMED)
        cx.say(f"It also depends on a second input: {cx.D(t2)} ({g.classify_origin(t2)}), via " +
               " → ".join(ch2 + [cx.D(v)]) + ".", [c2])
    others = [t for t in terms if t not in inputs_ and ("#param" in t) and t.split("#", 1)[0] in g.entries][:3]
    if others:
        cx.say("Values supplied from outside this code also feed it: " +
               ", ".join(f"{cx.D(t)} ({g.classify_origin(t)})" for t in others) + ".")


# ------------------------------------------------------------------------------------------------- receives
def r_receives(cx: Ctx) -> None:
    g = cx.g
    tw = set(cx.r.topic) - {"packet", "message", "frame", "sentence", "input", "first"}
    types = [k for k, s in g.sym.items() if s["kind"] in ("struct", "class", "record")
             and set(split_ident(s["name"])) & tw and s.get("file")]
    type_names = {g.sym[t]["name"] for t in types}
    recv = []
    for f in g.defined:
        s = g.sym[f]
        sig = s.get("sig") or ""
        hits = [tn for tn in type_names if tn in sig]
        if hits:
            recv.append((f, hits))
    # raw input: io sources in functions whose names match the topic or whose data reaches topic nodes
    raw = []
    tnodes = topic_nodes(cx)
    for io in g.io:
        if io["dir"] != "source" or not io["fn"]:
            continue
        sl = g.slice(io["sym"], "forward", max_nodes=1500, max_depth=150)
        if any(n in tnodes or n.split("#", 1)[0] in tnodes for n in sl["nodes"]):
            raw.append(io)
    if not recv and not raw:
        return r_search(cx)
    cls = []
    parts = []
    if raw:
        io = raw[0]
        cls.append(cx.claim(f"{cx.D(io['fn'])} reads raw input from {cx.D(io['sym'])} at {cx.loc(io['file'], io['line'])}.",
                            "io", [io["sym"], io["file"], io["line"]], [cx.ev("io", io["file"], io["line"], io["sym"])],
                            cx.file_status(io["file"])[0], cx.file_status(io["file"])[1]))
        parts.append(f"The raw data first arrives in {cx.D(io['fn'])}, which reads {cx.D(io['sym'])} "
                     f"({io['api']}, {cx.loc(io['file'], io['line'])})")
    # order receivers along the call graph: callers before callees
    def depth(f):
        return min((len(p) for p in g.paths_from_entries(f, limit=2)), default=99)
    recv.sort(key=lambda x: depth(x[0]))
    for f, hits in recv[:5]:
        s = g.sym[f]
        cls.append(cx.claim(f"{cx.D(f)} takes a {', '.join(hits)} as input ({cx.loc(s['file'], s['line'])}).", "signature",
                            [f, hits[0]], [cx.ev_def(f)]))
    # the stage that receives a complete message (line/packet/frame) on the way from the raw input
    msg_fn = None
    recv_set = {f for f, _ in recv}
    type_fields = {k for k, s2 in g.sym.items() if s2["kind"] == "field" and s2.get("container") in set(types)}
    if raw:
        src_sym = raw[0]["sym"]
        path = g.flow_path(src_sym, lambda n: n.split("#", 1)[0] in recv_set or n in type_fields, "forward")
        for e in path:
            if e.kind == "arg" and "#param" in e.dst:
                f = e.dst.split("#", 1)[0]
                if f in g.defined and re.search(r"(line|packet|message|msg|frame|sentence|process|handle|parse)",
                                                g.name(f), re.I) and f not in [x for x, _ in recv]:
                    msg_fn = (f, e)
                    break
    if msg_fn:
        f, e = msg_fn
        ps = g.sym.get(e.dst, {})
        cls.append(cx.claim(f"{cx.D(e.fn)} passes the assembled data to {cx.D(f)} at {cx.loc(e.file, e.line)}.", "flow_edge",
                            [e.src, e.dst, e.file, e.line], [cx.ev("flow", e.file, e.line, e.dst)]))
        parts.append(f"A complete message is handed to {cx.D(f)}" + (f" as `{ps.get('name')}`" if ps.get("name") else "") +
                     f" by {cx.D(e.fn)} ({cx.loc(e.file, e.line)})")
    if recv:
        lst = [f"{cx.D(f)} (receives a {', '.join(h)}; {cx.loc(g.sym[f]['file'], g.sym[f]['line'])})" for f, h in recv[:4]]
        parts.append("The decoded data is received by " + "; ".join(lst))
    cx.say(". ".join(parts) + ".", cls)
    # the main receiver: the deepest pipeline function (receives the complete, decoded packet): most callees among recv
    main = None
    for f, _ in recv:
        if g.entries.get(f) or any(c.res in ("flow", "table") for c in g.callers(f)):
            main = f
            break
    main = main or (recv[0][0] if recv else None)
    if main:
        txt, cl = how_reached(cx, main)
        cx.say(f"The main handler for the complete data is {cx.D(main)}. " + txt, cl)
        explain_function(cx, main, brief=True)


# ------------------------------------------------------------------------------------------------- where computed
SET_VERBS = {"set", "stored", "store", "saved", "save", "assigned", "written", "write", "updated", "update", "changed",
             "reset", "cleared", "initialized", "initialised", "configured"}
VERB_OPS = {"invalidat": r"=\s*\(?\s*\w*(NULL|INVALID|NONE)\w*|=\s*-\s*1\b", "decrement": r"--|-=|-\s*1\b", "decrease": r"--|-=|-\s*\w", "increment": r"\+\+|\+=|\+\s*1\b",
            "increase": r"\+\+|\+=|\+\s*\w", "count": r"\+\+|\+=", "reset": r"=\s*0\b|memset|\bclear", "clear": r"=\s*0\b|memset|\bclear",
            "toggle": r"\^=|=\s*!", "advance": r"\+\+|\+="}


def _where_set_to_const(cx: Ctx) -> bool:
    """'Where is the state set to ESTABLISHED?': the assignments that store that constant, ranked by the question's
    other words (which variable)."""
    g = cx.g
    consts = [c for m in cx.r.mentions if m.get("explicit") for c in m["candidates"]
              if g.sym.get(c, {}).get("kind") in ("enum_const", "macro")]
    if not consts:
        return False
    tw = expand_words(cx.r.topic) - {w for c in consts for w in split_ident(g.sym[c]["name"])}
    hits = []
    for c in consts[:3]:
        for o in g.occ.get(c, []):
            if not o.get("fn") or (not cx.about_tests and g.is_test(o["fn"])):
                continue
            line = _code(g.src.line(o["file"], o["line"]))
            if not re.search(rf"[^=!<>]=\s*\(?\s*(\([\w\s]+\)\s*)?{re.escape(g.sym[c]['name'])}\b", line):
                continue                           # read in a comparison, not stored
            lhs = line.split("=", 1)[0]
            hits.append((len(set(split_ident(lhs)) & tw), c, o, line))
    if not hits:
        return False
    top = max(h[0] for h in hits)
    hits = [h for h in hits if h[0] == top]
    hits.sort(key=lambda h: (h[2]["file"], h[2]["line"]))
    cl = [cx.claim(f"{cx.D(o['fn'])} stores {g.sym[c]['name']} at {cx.loc(o['file'], o['line'])}: `{line}`.", "reads",
                   [o["fn"], c, o["file"], o["line"]], [cx.ev("read", o["file"], o["line"], c)]) for _, c, o, line in hits[:6]]
    name = g.sym[hits[0][1]]["name"]
    cx.say(f"{name} is stored in " + "; ".join(f"{cx.D(o['fn'])} (`{line}`, {cx.loc(o['file'], o['line'])})"
                                             for _, c, o, line in hits[:6]) + ".", cl)
    fns = list(dict.fromkeys(o["fn"] for _, _, o, _ in hits))
    for fn in fns[:2]:
        txt, cl2 = how_reached(cx, fn)
        cx.say(f"{cx.D(fn)}: " + txt, cl2)
    return True


def r_where_computed(cx: Ctx) -> None:
    g = cx.g
    explicit = [c for m in cx.r.mentions if m.get("explicit") for c in m["candidates"]
                if g.sym.get(c, {}).get("kind") in VAR_KINDS]
    if explicit:
        return explain_variable(cx, explicit[0])
    pinned = cx.learn.pin_function() if cx.learn.pin_function() in g.defined else None
    if not pinned and re.search(r"\bset (to|as)\b|\bassigned\b|\bchanged? to\b|\bbecomes?\b", cx.r.question, re.I) and \
            _where_set_to_const(cx):
        return
    tw = set(cx.r.topic)
    qverb = next((v for w in split_ident(cx.r.question) for v in VERB_OPS if w.startswith(v)), None) if not pinned else None
    named = [f for f in g.defined if qverb and any(w.startswith(qverb[:6]) for w in split_ident(g.sym[f]["name"]))
             and set(split_ident(g.sym[f]["name"])) & tw and (cx.about_tests or not g.is_test(f))]
    if qverb and not named:
        # "where is the TTL decremented?": writes to the topic's variables that perform that operation
        tws = expand_words(tw) - {w for w in tw if w.startswith(qverb[:5])}
        cand = []
        for k, sy in g.sym.items():
            if sy["kind"] not in ("global", "field") or not set(split_ident(sy["name"])) & tws:
                continue
            for o in g.writers(k):
                if not o.get("fn") or _partial(o) or (not cx.about_tests and g.is_test(o["fn"])):
                    continue
                line = _code(g.src.line(o["file"], o["line"]))
                if re.search(VERB_OPS[qverb], line):
                    words = set(split_ident(g.name(k))) | set(split_ident(g.name(o["fn"])))
                    cand.append((-len(words & tws), o["file"], o["line"], k, o, line))
        if cand:
            cand.sort()
            seen = set()
            picked = []
            for c_ in cand:
                if (c_[1], c_[2]) not in seen:
                    seen.add((c_[1], c_[2]))
                    picked.append(c_)
            picked = [c_ for c_ in picked if c_[0] == picked[0][0]][:4]
            cx.learn.offer(picked, lambda c_: Loc(c_[4]["fn"], c_[1], c_[2], None),
                           lambda c_: (f"`{c_[5][:120]}` in {cx.D(c_[4]['fn'])} ({cx.loc(c_[1], c_[2])})", c_[5]),
                           lambda c_: float(-c_[0]), what="assignment")
            cl = [cx.claim(f"{cx.D(o['fn'])} writes {cx.D(k)} at {cx.loc(o['file'], o['line'])}.", "writes",
                           [o["fn"], k, o["file"], o["line"]], [cx.ev("write", o["file"], o["line"], k)])
                  for _, _, _, k, o, _ in picked]
            cx.say("; ".join(f"{cx.D(k)} is {qverb}ed in {cx.D(o['fn'])} (`{line}`, {cx.loc(o['file'], o['line'])})"
                             for _, _, _, k, o, line in picked) + ".", cl)
            fn0 = picked[0][4]["fn"]
            txt, cl2 = how_reached(cx, fn0)
            cx.say(f"{cx.D(fn0)}: " + txt, cl2)
            return
    fns = []
    # the question's own verb ("incremented" -> increment, "set") named in a function name is the strongest signal;
    # the question's nouns must match first ("which function computes the CRC" is about the CRC, not "compute")
    qwords_all = split_ident(cx.r.question)
    qverbs = {w[:-2] if w.endswith("ed") else w for w in qwords_all if w.endswith("ed") and len(w) > 5}
    qverbs |= {v[:-1] for v in qverbs if v.endswith("e")}
    qverbs |= {w for w in qwords_all if w in SET_VERBS}
    qverbs |= {x for w in qwords_all if w.endswith("ed") and len(w) > 5 for x in expand_words({w}) if len(x) > 3}
    calc_q = any(w.startswith(("calcul", "comput", "deriv", "estimat", "determin", "convert", "filter")) for w in qwords_all)
    verbish = {w for w in tw if any(w.startswith(v[:5]) for v in CALC_VERBS + tuple(VERB_OPS)) or w in SET_VERBS or
               w.endswith("ed") or w.startswith(("comput", "calcul"))}
    nouns = expand_words(tw - verbish) or expand_words(tw)
    for f in g.defined:
        if not cx.about_tests and g.is_test(f):
            continue
        words = set(split_ident(g.sym[f]["name"]))
        nh = words & nouns
        if nh:
            verb_hit = any(w.startswith(v) or v.startswith(w) for w in words for v in qverbs if len(w) > 2 and len(v) > 2)
            score = 2 * len({c for c in tw - verbish if expand_words({c}) & words}) + \
                (3 if calc_q and words & set(CALC_VERBS + ("compute", "calc")) else 0) + (4 if verb_hit else 0)
            fns.append((score, f))
    # equal scores: the function the code calls most is the canonical one (crc16_ccitt over crc16_ccitt_single)
    fns.sort(key=lambda x: (-x[0], -len({c.caller for c in g.callers(x[1])}), len(g.name(x[1]))))
    loc_w = lambda x: Loc(x[1], (g.sym.get(x[1]) or {}).get("file"), None, None)      # noqa: E731
    fns = cx.learn.rank(fns, loc_w, lambda x: float(x[0]))
    if cx.llm is not None and len([f for f in fns if f[0] >= 2]) > 1:
        cand = [f for f in fns if f[0] >= 2][:6]
        picked = _rerank(cx, cand, lambda f: _render_function(cx, f[1]), what="function")
        if picked[0] is not cand[0]:
            fns = [(max(4, picked[0][0]), picked[0][1])] + [f for f in fns if f[1] != picked[0][1]]
    if pinned:
        cx.learn.use_pin(changed=not fns or fns[0][1] != pinned)
        fns = [(max(4, fns[0][0] if fns else 4), pinned)] + [f_ for f_ in fns if f_[1] != pinned]
    n_nouns = len(tw - verbish)
    if fns and fns[0][0] < 4 and n_nouns and fns[0][0] >= 2 * n_nouns and (
            len(fns) == 1 or fns[1][0] < fns[0][0] or
            len({c.caller for c in g.callers(fns[0][1])}) > len({c.caller for c in g.callers(fns[1][1])})):
        fns[0] = (4, fns[0][1])                     # the function covering every noun of the question
    vars_ = [k for k, s in g.sym.items() if s["kind"] in ("global", "field") and _name_words(s["name"], tw) & tw]
    # a data structure named in the question: who writes its fields
    types = [k for k, s in g.sym.items() if s["kind"] in ("struct", "class", "record") and len(set(split_ident(s["name"])) & tw) >= 1
             and s.get("file")]
    types.sort(key=lambda k: -len(set(split_ident(g.sym[k]["name"])) & tw))
    if types and len(set(split_ident(g.sym[types[0]]["name"])) & tw) >= 2:
        t = types[0]
        fields = [k for k, s in g.sym.items() if s.get("container") == t and s["kind"] == "field"]
        wf: dict[str, list] = defaultdict(list)
        for f in fields:
            for o in g.writers(f):
                if o["fn"]:
                    wf[o["fn"]].append((f, o))
        if wf:
            cls = []
            items = []
            for fn_, lst in sorted(wf.items(), key=lambda kv: g.sym.get(kv[0], {}).get("line", 0)):
                f, o = lst[0]
                cls.append(cx.claim(f"{cx.D(fn_)} writes {cx.D(f)} at {cx.loc(o['file'], o['line'])}.", "writes",
                                    [fn_, f, o["file"], o["line"]], [cx.ev("write", o["file"], o["line"], f)]))
                items.append(f"{cx.D(fn_)} ({', '.join(dict.fromkeys(g.sym[x]['name'] for x, _ in lst))}; "
                             f"{cx.loc(o['file'], o['line'])})")
            cx.say(f"{g.sym[t]['name']} is written by: " + "; ".join(items) + ".", cls)
            for fn_ in list(wf)[:3]:
                txt, cl = how_reached(cx, fn_)
                cx.say(f"{cx.D(fn_)}: " + txt, cl)
            return
    if not fns and not vars_:
        return r_search(cx)
    # a variable the question names exactly ("where is errmsg set") beats a function that only shares a word with the
    # question (pb_message_set_to_defaults)
    qtok = {w.lower() for w in re.findall(r"[A-Za-z_]\w*", cx.r.question)}
    # (only names that are the code's own vocabulary: no function name uses them as a word, unlike "message")
    fn_words = getattr(g, "_fn_name_words", None)
    if fn_words is None:
        fn_words = g._fn_name_words = {w for f in g.defined for w in split_ident(g.sym[f]["name"])}
    exact_vars = [v for v in vars_ if len(g.sym[v]["name"]) > 3 and g.sym[v]["name"].lower() in qtok and
                  g.sym[v]["name"].lower() not in fn_words]
    if exact_vars and fns and fns[0][0] >= 4 and g.sym[exact_vars[0]]["name"].lower() not in g.sym[fns[0][1]]["name"].lower():
        fns = [(min(3, sc), f_) for sc, f_ in fns]
        vars_ = exact_vars
    if fns and fns[0][0] >= 4:
        cx.learn.offer(fns, loc_w, lambda x: (_fn_label(cx, x[1]), _render_function(cx, x[1])), lambda x: float(x[0]),
                       what="function")
        f = fns[0][1]
        s = g.sym[f]
        cl = [cx.claim(f"{cx.D(f)} is defined in {cx.loc(s['file'], s['line'])}.", "defined", [f, s["file"], s["line"]],
                       [cx.ev_def(f)])]
        verb = next((w for w in re.findall(r"[A-Za-z]+", cx.r.question.lower())
                     if w.endswith("ed") and len(w) > 4 or w in SET_VERBS or w.startswith(("comput", "calcul"))), "calculated")
        verb = {"computes": "computed", "calculates": "calculated", "compute": "computed", "calculate": "calculated"}.get(verb, verb)
        cx.say(f"It is {verb} in {cx.D(f)}:", cl)
        explain_function(cx, f, brief=True)
        inner = [(v, o) for v in vars_ for o in g.writers(v) if o["fn"] == f]
        if inner:
            icl = [cx.claim(f"{cx.D(f)} writes {cx.D(v)} at {cx.loc(o['file'], o['line'])}.", "writes",
                            [f, v, o["file"], o["line"]], [cx.ev("write", o["file"], o["line"], v)]) for v, o in inner[:4]]
            cx.say("The update itself: " + "; ".join(f"`{_code(g.src.line(o['file'], o['line']))}` "
                                                    f"({cx.loc(o['file'], o['line'])})" for v, o in inner[:4]) + ".", icl)
        # where the result is stored
        for e in [e for e in g.fwd.get(f"{f}#ret", []) if cx.about_tests or not g.is_test(e.fn)][:4]:
            if e.kind in ("assign", "init"):
                c = cx.claim(f"{cx.D(e.fn)} stores the result of {cx.D(f)} in {cx.D(e.dst)} ({cx.loc(e.file, e.line)}).",
                             "flow_edge", [e.src, e.dst, e.file, e.line], [cx.ev("flow", e.file, e.line, e.dst)])
                cx.say(f"The result is stored in {cx.D(e.dst)} by {cx.D(e.fn)} ({cx.loc(e.file, e.line)}: "
                       f"`{g.src.line(e.file, e.line).strip().rstrip(';')}`).", [c])
    # other places the topic's variables are computed
    others = []
    via_macro: dict[tuple[str, str], list[dict]] = defaultdict(list)    # written inside a macro's expansion
    for v in vars_:
        for o in g.writers(v):
            if fns and fns[0][0] >= 4 and o["fn"] == fns[0][1] or not o.get("fn") or \
                    (not cx.about_tests and g.is_test(o["fn"])):
                continue
            txt = g.src.line(o["file"], o["line"]).strip().rstrip(";")
            if "=" in txt:
                others.append((o, v, txt))
            else:
                mac = _assigning_macro(g, txt, g.sym[v]["name"])
                if mac:
                    via_macro[(v, mac)].append(o)

    def keep_score(item):
        # state that persists (a global, or a field reached through a pointer) set by a function named for it
        # (a setter) is what "where is X stored/set" asks about; a field of a local struct is a temporary copy
        o, v, txt = item
        fw = set(split_ident(g.name(o["fn"])))
        lhs = txt.split("=", 1)[0]
        base = re.match(r"^\s*([A-Za-z_]\w*)\s*\.", lhs)
        transient = bool(base) and any(k.startswith(f"{o['fn']}#local:{base.group(1)}@") for k in g.occ_keys_of(o["fn"]))
        vw = _name_words(g.sym.get(v, {}).get("name") or "", tw)
        return (3 * len(vw & tw) + 2 * len(fw & tw) + (2 if fw & {"set", "init", "config", "configure", "store", "save"} else 0)
                + (2 if "->" in lhs or g.sym.get(v, {}).get("kind") == "global" else 0) - (3 if transient else 0))
    if via_macro and not (fns and fns[0][0] >= 4):
        for (v, mac), uses in sorted(via_macro.items(), key=lambda kv: -len(kv[1]))[:2]:
            ms = g.sym[mac]
            body = re.sub(r"^\s*#\s*define\s+", "", g.src.line(ms["file"], ms["line"]) or "").strip() or \
                re.sub(r"\s+", " ", ms.get("type") or "").strip()
            fns_u = list(dict.fromkeys(o["fn"] for o in uses))
            cl = [cx.claim(f"{ms['name']} is defined in {cx.loc(ms['file'], ms['line'])}.", "defined",
                           [mac, ms["file"], ms["line"]], [cx.ev_def(mac)])]
            cl += [cx.claim(f"{cx.D(o['fn'])} writes {cx.D(v)} at {cx.loc(o['file'], o['line'])}.", "writes",
                            [o["fn"], v, o["file"], o["line"]], [cx.ev("write", o["file"], o["line"], v)]) for o in uses[:4]]
            cx.say(f"{cx.D(v)} is set by the macro {ms['name']} ({cx.loc(ms['file'], ms['line'])}: `{body[:120]}`), which "
                   f"runs at {len(uses)} place{'s' if len(uses) != 1 else ''} in {len(fns_u)} function"
                   f"{'s' if len(fns_u) != 1 else ''}, for example " +
                   ", ".join(f"{cx.D(o['fn'])} (`{_code(g.src.line(o['file'], o['line'])).strip().rstrip(';')}`, "
                             f"{cx.loc(o['file'], o['line'])})" for o in uses[:3]) + ".", cl)
    others.sort(key=lambda it: -keep_score(it))
    others = _rerank(cx, others, lambda it: f"{g.name(it[0]['fn'])}() at {it[0]['file']}:{it[0]['line']}: `{it[2][:160]}`",
                     what="assignment")
    if others and not (fns and fns[0][0] >= 4):
        cx.learn.offer(others, lambda it: Loc(it[0]["fn"], it[0]["file"], it[0]["line"], None),
                       lambda it: (f"`{it[2][:120]}` in {cx.D(it[0]['fn'])} ({cx.loc(it[0]['file'], it[0]['line'])})", it[2]),
                       lambda it: float(keep_score(it)), what="assignment")
    if others:
        cls = [cx.claim(f"{cx.D(o['fn'])} writes {cx.D(v)} at {cx.loc(o['file'], o['line'])}.", "writes",
                        [o["fn"], v, o["file"], o["line"]], [cx.ev("write", o["file"], o["line"], v)]) for o, v, _ in others[:6]]
        lead_txt = "Related values are also set here: " if fns and fns[0][0] >= 4 else "It is written here: "
        cx.say(lead_txt + "; ".join(f"`{t}` in {cx.D(o['fn'])} ({cx.loc(o['file'], o['line'])})"
                                                           for o, v, t in others[:6]) + ".", cls)
        # `OSCPUUsage = (INT8U)usage`: the computation is the local's assignment just before
        defs, dcl = [], []
        for o, v, t in others[:4]:
            for k, o2, t2 in _local_defs(cx, o["fn"], o["file"], o["line"], t):
                if (o2["file"], o2["line"]) in {(x[1]["file"], x[1]["line"]) for x in defs}:
                    continue
                defs.append((k, o2, t2))
                dcl.append(cx.claim(f"{cx.D(o2['fn'])} writes {cx.D(k)} at {cx.loc(o2['file'], o2['line'])}.", "writes",
                                    [o2["fn"], k, o2["file"], o2["line"]], [cx.ev("write", o2["file"], o2["line"], k)]))
        if defs:
            cx.say("The value is computed just before: " + "; ".join(f"`{t2}` ({cx.loc(o2['file'], o2['line'])})"
                                                                     for _, o2, t2 in defs[:3]) + ".", dcl)
    if not (fns and fns[0][0] >= 4) and not others:
        r_search(cx)


# ------------------------------------------------------------------------------------------------- failures
def _branch_actions(cx: Ctx, fn: str, br: dict, then: bool = True) -> dict[str, Any]:
    g = cx.g
    s, e = (br["then_s"], br["then_e"]) if then else (br["else_s"], br["else_e"])
    if not s:
        return {"calls": [], "writes": [], "returns": False, "lines": []}
    # the condition's own calls (`… || minor > disk_version_minor(lfs)` on the if's continuation line) are not the
    # side's reaction
    body0 = next((n for n, t in g.src.snippet(br["file"], s, min(e, s + 8)) if "{" in STRING_RE.sub("", t)), s) if then else s
    cond_names = set(re.findall(r"([A-Za-z_]\w*)\s*\(", br.get("cond") or ""))
    calls = [c for c in g.out_calls.get(fn, []) if s <= c.line <= e and not _diag_line(cx, c.file, c.line) and
             not (c.line <= body0 and g.name(c.callee) in cond_names)]
    writes = []
    for sym, o in g.fn_occ(fn):
        out_param = "#param:" in sym and o.get("via") == "deref"
        if o["fn"] == fn and s <= o["line"] <= e and o["role"] in ("write", "rw") and (not _partial(o) or out_param) and (
                "#" not in sym or "#local:" in sym and _local_outcome(g.src.line(o["file"], o["line"])) or out_param):
            # state, a local error code / next state, or a result handed back through a pointer (`*perr = OS_ERR_…`)
            writes.append((sym, o))
    lines = g.src.snippet(br["file"], s, e)
    returns = any(_is_exit(g, t) for _, t in lines)     # return / throw (C#) / a returning macro (PB_RETURN_ERROR)
    # decisions nested inside this side, and whether a return happens regardless of them
    quiet = [r for r in (_debug_only(cx, fn, b) for b in g.branches.get(fn, []) if s <= b["line"] <= e) if r]
    if quiet:
        calls = [c for c in calls if not any(a <= c.line <= b for a, b in quiet)]
    nested = [b for b in g.branches.get(fn, []) if b is not br and s <= b["line"] <= e and b["kind"] == "if" and
              b["line"] != br["line"] and not DIAG_COND_RE.match(b["cond"] or "") and not _debug_only(cx, fn, b) and
              not _macro_internal(cx, b)]
    def inside_nested(n):
        return any((b["then_s"] or 0) <= n <= (b["then_e"] or 0) or (b["else_s"] or 0) <= n <= (b["else_e"] or -1)
                   for b in nested)
    returns_top = any(_leaves(cx, br["file"], n, t, s) and not inside_nested(n) for n, t in lines)
    # `goto label;` (the C error-exit idiom): the code at the label runs next and the rest in between is skipped
    jump = None
    for n, t in lines:
        m = re.search(r"\bgoto\s+([A-Za-z_]\w*)\s*(;|$)", _code(t))
        if m and not inside_nested(n):
            jump = _label_block(cx, fn, m.group(1))
            break
    if jump:
        returns = returns_top = True
    return {"calls": calls, "writes": writes, "returns": returns, "returns_top": returns_top, "nested": nested,
            "lines": lines, "range": (s, e), "cfgs": {o.get("cfg") for _, o in writes}, "jump": jump}


def _local_defs(cx: Ctx, fn: str, file: str, line: int, txt: str, limit: int = 2) -> list[tuple[str, dict, str]]:
    """Locals on the right of `X = (T)usage`: each one's nearest assignment before that line."""
    g = cx.g
    rhs = txt.split("=", 1)[1] if "=" in txt else ""
    locs = {g.sym[k]["name"]: k for k in g.occ_keys_of(fn) if "#local:" in k and k in g.sym}
    out = []
    for nm in dict.fromkeys(re.findall(r"\b([A-Za-z_]\w*)\b", STRING_RE.sub(" ", rhs))):
        k = locs.get(nm)
        if not k:
            continue
        prev = [o for o in g.writers(k) if o["file"] == file and o["line"] < line and not _partial(o)]
        if prev:
            o = max(prev, key=lambda o: o["line"])
            t = _stmt(g, o["file"], o["line"])
            if re.search(r"[A-Za-z_]", t.split("=", 1)[-1]):          # computed, not just `usage = 0`
                out.append((k, o, t))
    return out[:limit]


def _encloses(b: dict, line: int) -> bool:
    """Line lies inside one of the branch's sides (or a loop's body)."""
    return any(a and z and a <= line <= z for a, z in ((b.get("then_s"), b.get("then_e")), (b.get("else_s"), b.get("else_e")))) \
        or b["kind"] in ("while", "for", "do") and b["line"] < line <= (b.get("end_line") or 0)


def _failure_tail(cx: Ctx, fn: str, br: dict) -> dict | None:
    """`if (ok) { …; return result; }  …; *perr = ERR; return NULL;` — code written success-first returns inside the if,
    so the few statements after it are the check's other side. Returns that side as a branch (cond negated), or None."""
    g = cx.g
    if br["kind"] != "if" or br.get("else_s") or not br.get("then_e"):
        return None
    sy = g.sym.get(fn) or {}
    end = sy.get("end_line")
    if not end or sy.get("file") != br["file"] or end <= br["then_e"]:
        return None
    brs = g.branches.get(fn, [])
    # nested (inside an if / a catch block): the other side runs to the end of the enclosing block
    sides = [(a, z) for b in brs if b is not br and b["line"] != br["line"]
             for a, z in ((b.get("then_s"), b.get("then_e")), (b.get("else_s"), b.get("else_e"))) if a and z and a < br["line"] <= z]
    loops = [b for b in brs if b["kind"] in ("while", "for", "do") and b["line"] < br["line"] <= (b.get("end_line") or 0)]
    nested = bool(sides)
    if sides:
        end = min(z for _, z in sides)
    if loops and (not sides or min(b.get("end_line") or 0 for b in loops) < end):
        return None                                   # inside a loop body, falling through goes round again
    if not _branch_actions(cx, fn, br).get("returns_top"):
        return None
    code = [(n, t) for n, t in g.src.snippet(br["file"], br["then_e"] + 1, end)
            if _code(t).strip() not in ("", "{", "}", "};") and not t.lstrip().startswith("#")]
    # the other side ends at its first return (`return pb_dec_submessage(…);`) and never runs into the next
    # `case …:` / `default:` label of a switch
    later = [b for b in brs if b is not br and b["line"] > br["then_e"]]
    cut = []
    for n, t in code:
        if re.match(r"^(case\b[^:]*|default)\s*:", _code(t).strip()):
            break
        cut.append((n, t))
        if _leaves(cx, br["file"], n, t, code[0][0]) and not any(
                (b.get("then_s") or 0) <= n <= (b.get("then_e") or 0) or (b.get("else_s") or 0) <= n <= (b.get("else_e") or -1)
                for b in later):
            break
    code = cut
    inner = [b for b in brs if b is not br and code and code[0][0] <= b["line"] <= code[-1][0]]
    if not code or len(code) > (9 if nested else 6) or (inner and not nested):
        return None
    if not any(re.search(r"\breturn\b", _code(t)) for _, t in code) and not g.out_calls.get(fn):
        return None
    return dict(br, cond=_negated(br["cond"]), then_s=code[0][0], then_e=code[-1][0], else_s=None, else_e=None,
                _else=True, _tail=True, _pos_cond=br["cond"])


def _negated(cond: str) -> str:
    """`!(c)`, without a double negation: `!decode_field(&s, t, it)` → `decode_field(&s, t, it)`."""
    m = re.match(r"^!\s*([A-Za-z_][\w.\->\[\]]*\s*(\((?:[^()]|\([^()]*\))*\))?)$", (cond or "").strip())
    return m.group(1) if m else f"!({cond})"


def _case_sides(cx: Ctx, fn: str, br: dict) -> list[dict]:
    """A switch's cases as sides of their own: `case OS_STAT_PEND_TO: default: …; *perr = OS_ERR_TIMEOUT; break;`.
    Each is anchored on the switch's line (that is the decision point); `_case` holds the labels."""
    g = cx.g
    if br["kind"] != "switch" or not br.get("then_s"):
        return []
    lines = g.src.snippet(br["file"], br["then_s"], br["then_e"] or br["then_s"])
    labels: list[tuple[int, str]] = []
    depth = 0
    for n, t in lines:
        c = _code(t)
        m = re.match(r"^\s*(case\s+(.+?)|default)\s*:(?!:)", c)
        if m and depth <= 1:
            labels.append((n, (m.group(2) or "default").strip()))
        depth += c.count("{") - c.count("}")
    out = []
    i = 0
    while i < len(labels):
        j = i
        while j + 1 < len(labels) and not any(_code(t).strip() and not re.match(r"^\s*(case\s+.+?|default)\s*:", _code(t))
                                              for n, t in lines if labels[j][0] < n < labels[j + 1][0]):
            j += 1                                    # `case A: case B:` share one body
        start = labels[j][0]
        stop = labels[j + 1][0] - 1 if j + 1 < len(labels) else (br["then_e"] or start)
        names = [lb for _, lb in labels[i:j + 1]]
        if stop >= start:
            out.append(dict(br, then_s=start, then_e=stop, else_s=None, else_e=None, _case=names,
                            cond=f"{br['cond']} is " + " or ".join(names)))
        i = j + 1
    return out


def _stmt(g: CodeGraph, file: str | None, line: int | None, max_lines: int = 4) -> str:
    """The statement starting on a line, joined with its continuation lines (a ternary or call split over lines)."""
    t = _code(g.src.line(file, line))
    raw = g.src.line(file, line).rstrip()
    n = line or 0
    while raw and not re.search(r"[;{}]\s*(/[/*].*)?$", raw) and n < (line or 0) + max_lines:
        n += 1
        nxt = g.src.line(file, n).strip()
        if not nxt or nxt.startswith("#"):
            break
        raw = nxt
        t = _code(t + " " + nxt)
    return t


def _records(cx: Ctx, writes: list[tuple[str, dict]], limit: int = 4) -> list[str]:
    """['records it (`a = 1`, `b++`)'] — the state a branch side changes, one phrase for all of it."""
    out = []
    lines = list(dict.fromkeys(_stmt(cx.g, o["file"], o["line"]) for k, o in writes if "#param:" not in k))
    if lines:
        out.append("records it (" + ", ".join(f"`{t}`" for t in lines[:limit]) + (", …" if len(lines) > limit else "") + ")")
    # results handed back through a pointer parameter (`*perr = OS_ERR_TIMEOUT`): what the caller is told
    back = list(dict.fromkeys(_stmt(cx.g, o["file"], o["line"]) for k, o in writes if "#param:" in k))
    if back:
        out.append("reports " + ", ".join(f"`{t}`" for t in back[:limit]) + " to its caller")
    return out


def _local_outcome(line: str) -> bool:
    """A local assignment that decides an outcome: an error code (`err = LFS_ERR_INVAL`) or a named state
    (`NMTstateCpy = … ? CO_NMT_STOPPED : CO_NMT_PRE_OPERATIONAL`)."""
    t = _code(line)
    if ERR_ASSIGN_RE.search(t):
        return True
    m = re.match(r"^\s*[A-Za-z_]\w*\s*=(?!=)\s*(.+)$", t)
    return bool(m) and bool(OUTCOME_CONST_RE.search(STRING_RE.sub(" ", m.group(1))))


def _loop_jump(g: CodeGraph, file: str, a: int, b: int, nested=lambda n: False) -> list[str]:
    """`break;` / `continue;` on a side (outside its own nested decisions), worded."""
    for n, t in g.src.snippet(file, a, b):
        if nested(n):
            continue
        c = _code(t)
        if re.search(r"\bbreak\b", c):
            return ["leaves the loop (`break`)"]
        if re.search(r"\bcontinue\b", c):
            return ["goes on with the next iteration (`continue`)"]
    return []


def _assigning_macro(g: CodeGraph, line_text: str, field: str, depth: int = 0) -> str | None:
    """The function-like macro on this line whose expansion assigns `field` (`PB_RETURN_ERROR(...)` → PB_SET_ERROR,
    `#define PB_SET_ERROR(stream, msg) (stream->errmsg = …)`), followed through macros it uses."""
    for name in re.findall(r"\b([A-Za-z_]\w*)\s*\(", _code(line_text)):
        for k in g.by_name.get(name, []):
            sy = g.sym.get(k) or {}
            if sy.get("kind") != "macro":
                continue
            body = sy.get("type") or ""
            if re.search(rf"(\.|->|\b){re.escape(field)}\s*=(?!=)", body):
                return k
            if depth < 2:
                inner = _assigning_macro(g, body, field, depth + 1)
                if inner:
                    return inner
    return None


def _return_macros(g: CodeGraph) -> set[str]:
    """Function-like macros that leave the function (`#define PB_RETURN_ERROR(s, m) return PB_SET_ERROR(s, m), false`)."""
    rm = getattr(g, "_return_macros", None)
    if rm is None:
        rm = {sy["name"] for k, sy in g.sym.items() if sy["kind"] == "macro" and sy.get("sig") == "function-like"
              and re.search(r"\b(return|throw|goto)\b", sy.get("type") or "")}
        g._return_macros = rm
    return rm


def _is_exit(g: CodeGraph, t: str) -> bool:
    """The statement leaves the function: return / throw, or a macro whose expansion returns."""
    c = _code(t)
    if re.search(r"\b(return|throw)\b", c):
        return True
    rm = _return_macros(g)
    return bool(rm) and any(n in rm for n in re.findall(r"\b([A-Za-z_]\w*)\s*\(", c))


def _braceless_guarded(g: CodeGraph, file: str, line: int, lo: int) -> bool:
    """The line is the body of a brace-less `if (…)` / `else` / loop header above it (`if (!ok)\\n return -1;`) that is
    itself inside the side being judged (from line lo), so it only runs under that condition, even when no decision was
    recorded for the header (compiled-out code). The side's own header is above lo."""
    ls = g.src.lines(file)
    hdr: list[str] = []
    n = line - 1
    while n >= 1 and len(hdr) < 6:
        t = _code(ls[n - 1]).strip()
        n -= 1
        if not t or t.startswith("#"):
            continue
        if t.endswith((";", "{", "}")) or ";" in t:
            break
        hdr.insert(0, t)
        joined = " ".join(hdr)
        if n + 1 < lo:
            break
        if re.match(r"^(\}\s*)?else\s*$", joined) or (
                re.match(r"^(\}\s*)?(else\s+)?(if|while|for)\s*\(", joined) and joined.count("(") == joined.count(")")):
            return True
    return False


def _compiled_out(cx: Ctx, file: str, line: int) -> bool:
    """The line sits in an `#ifdef M` block (or the `#else` of `#ifndef M`) for a macro the code never defines."""
    if not hasattr(cx, "_free_macro_names"):
        try:
            cx._free_macro_names = set(_free_macros(cx))
        except Exception:
            cx._free_macro_names = set()
    free = cx._free_macro_names
    for c, _ in _enclosing_conditions(cx.g, file, line):
        m = re.match(r"^#(?:ifdef\s+|if\s+(?:defined\s*\(?\s*)?)([A-Za-z_]\w*)\s*\)?\s*$", c)
        if m and m.group(1) in free:
            return True
        m = re.match(r"^#else of `#ifndef ([A-Za-z_]\w*)`$", c)
        if m and m.group(1) in free:
            return True
    return False


def _leaves(cx: Ctx, file: str, line: int, t: str, lo: int) -> bool:
    """The statement leaves the function whenever its side (starting at line lo) runs: not under a brace-less if inside
    that side, not compiled out."""
    return _is_exit(cx.g, t) and not _braceless_guarded(cx.g, file, line, lo) and not _compiled_out(cx, file, line)


def _exit_macro_call(g: CodeGraph, act: dict) -> str | None:
    """`PB_RETURN_ERROR(stream, "zero tag")` when the side leaves through a returning macro."""
    rm = _return_macros(g)
    for _, t in act.get("lines", []):
        c = _code(t)
        for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", c):
            if m.group(1) in rm:
                return c.strip().rstrip(";")
    return None


def _thrown_value(act: dict) -> str | None:
    """The exception a branch side throws at its own level (`throw new MqttConnectionException(…);`)."""
    for _, t in act.get("lines", []):
        m = re.search(r"\bthrow\s+([^;]+);", t)
        if m:
            return m.group(1).strip()
        if re.search(r"\bthrow\s*;", t):
            return "the same exception again"
    return None


def _returned_value(act: dict) -> str | None:
    """The value a branch side returns at its own level (`return ERR_MEM;` -> ERR_MEM)."""
    for _, t in act.get("lines", []):
        m = re.search(r"\breturn\s+([^;]+);", t)
        if m:
            return m.group(1).strip()
    return None


def _label_block(cx: Ctx, fn: str, label: str) -> dict[str, Any] | None:
    """The statements after `label:` in fn, up to the next return / label / end of the function."""
    g = cx.g
    f, s, e = cx.body_lines(fn)
    if not f:
        return None
    start = None
    end = e
    for n, t in g.src.snippet(f, s, e):
        if start is None:
            if re.match(rf"^\s*{re.escape(label)}\s*:(?!:)", t):
                start = n
            continue
        if re.match(r"^\s*[A-Za-z_]\w*\s*:(?!:)", t) and not re.match(r"^\s*(default|case)\b", t):
            end = n - 1
            break
        if re.search(r"\breturn\b", _code(t)):
            end = n
            break
    if start is None:
        return None
    calls = [c for c in g.out_calls.get(fn, []) if start < c.line <= end]
    writes = [(sym, o) for sym, o in g.fn_occ(fn)
              if o["fn"] == fn and start < o["line"] <= end and o["role"] in ("write", "rw") and "#" not in sym and not _partial(o)]
    return {"label": label, "file": f, "line": start, "end": end, "calls": calls, "writes": writes}


def _written_values(cx: Ctx, writes: list[tuple[str, dict]]) -> dict[str, Any]:
    """Constant values a handler stores ("g_pwmEnabled = 0", "g_nav.mode = NAV_MODE_DEGRADED"), by lvalue and leaf."""
    g = cx.g
    env: dict[str, Any] = {}
    for sym, o in writes:
        line = _code(g.src.line(o["file"], o["line"]))
        m = re.match(r"^([A-Za-z_][\w.\->\[\]]*)\s*=\s*(.+)$", line)
        if not m or "==" in line[:len(m.group(1)) + 3]:
            continue
        val, unk = g.eval_value(m.group(2), {})
        if val is None:
            continue
        lhs = m.group(1).replace("->", ".")
        env[lhs] = val
        env[lhs.split(".")[-1]] = val
    return env


def _effects_of_writes(cx: Ctx, writes: list[tuple[str, dict]], exclude_fn: str) -> list[tuple[str, dict, dict]]:
    """For state written by a handler: which guarded decisions elsewhere read it (consequences)."""
    g = cx.g
    out = []
    syms = {w for w, _ in writes}
    for fn, brs in g.branches.items():
        if fn == exclude_fn:
            continue
        for br in brs:
            # a `?:` inside a macro's expansion (`PB_SET_ERROR` keeps the first errmsg) is not a decision of the code,
            # and a `?:` picks a value — it cannot "return early" or skip calls
            if set(br["reads"]) & syms and not _macro_internal(cx, br) and br["kind"] != "ternary":
                out.append((fn, br, {"reads": sorted(set(br["reads"]) & syms)}))
    return out


QTY_Q_RE = re.compile(r"\b(more|fewer|less|greater|larger|bigger|smaller|longer|shorter)\b[^?]*\bthan\b|\bexceed|"
                      r"\btoo (many|much|few|long|big|large|small|short)\b|\bnot enough\b|\binsufficient\b", re.I)


FAIL_ADJ = r"(?:bad|invalid|wrong|corrupt(?:ed)?|malformed|incorrect|mismatched|failed|failing|broken|garbled|illegal|unknown|" \
           r"unsupported|unexpected|unrecognized|unrecognised|out-of-range)"


EXHAUST_CONST_RE = re.compile(r"NO_?(SPC|SPACE|MEM|MEMORY|BUFS?|RES|ROOM|FREE|MORE)(\b|_)|ENOSPC|ENOMEM|ENOBUFS|ERR_MEM\b|_FULL\b|"
                              r"FULL_|EXHAUST|DEPLETED|NON_?AVAIL|UNAVAIL",
                              re.I)
_OPND = r"(?P<lhs>[A-Za-z_][\w.\->\[\]]*)"
VALUE_WORDS = [("zero", [_OPND + r"\s*==\s*0[uUlL]*\b", r"!\s*" + _OPND]),
               ("null", [_OPND + r"\s*==\s*\(?\s*(NULL|null|nullptr)\b", r"!\s*" + _OPND]),
               ("negative", [_OPND + r"\s*<\s*0[uUlL]*\b"]),
               ("empty", [_OPND + r"\s*==\s*0[uUlL]*\b", _OPND + r"\.(Count|Length)\s*==\s*0"])]
SUCCESS_CONST_RE = re.compile(r"ERR_?NONE\b|\bNO_?ERR(OR)?\b|_OK\b|^OK$|SUCCESS|ERR_?OK\b|\bE_?OK\b|^pdPASS$|^pdTRUE$", re.I)
ABSENT_CONST_RE = re.compile(r"NO_?(SPC|SPACE|MEM|MEMORY|BUFS?|RES|ROOM|FREE|MORE)(\b|_)|ENOSPC|ENOMEM|ENOBUFS|ERR_MEM\b|"
                             r"EXHAUST|DEPLETED|NON_?AVAIL|UNAVAIL", re.I)
CODEC_DIR_RE = {"decode": re.compile(r"^(decod\w*|pars(e|es|ed|er|ers|ing)|deseriali\w*|unpack(s|ed|ing|er)?|unmarshal\w*)$", re.I),
                "encode": re.compile(r"^(encod\w*|seriali\w*|pack(s|ed|ing|er)?|marshal\w*)$", re.I)}


def _codec_direction(question: str) -> str | None:
    """"decoded / parsing / deserialising" → decode, "encoding / serialising / packing" → encode, else None."""
    ws = split_ident(question)
    dirs = {d for w in ws for d, pat in CODEC_DIR_RE.items() if pat.match(w)}
    return dirs.pop() if len(dirs) == 1 else None


ACQUIRE_WORDS = {"get", "alloc", "allocate", "malloc", "take", "acquire", "pend", "request", "obtain", "pop", "claim", "lock",
                 "accept", "wait", "fetch", "reserve"}
RELEASE_WORDS = {"put", "free", "release", "give", "post", "push", "unlock", "del", "delete", "dealloc", "return"}
ACQUIRE_Q_RE = re.compile(r"\b(asks? for|asking for|requests?|requested|gets?|takes?|allocat\w*|acquires?|obtains?|needs?|"
                          r"wants?|waits? (on|for)|waiting (on|for)|pends?|reserves?)\b", re.I)
RELEASE_Q_RE = re.compile(r"\b(releases?|released|gives? back|given back|puts? back|frees? (a|the|it|its)|freed|deletes?|"
                          r"deleted|posts?|posted)\b", re.I)
STORAGE_CONST_RE = re.compile(r"NO_?(SPC|SPACE)|ENOSPC|DISK_?FULL|_FULL\b|FULL_", re.I)
MEMORY_CONST_RE = re.compile(r"NO_?(MEM|MEMORY|BUFS?)|ENOMEM|ENOBUFS|ERR_MEM\b|OUT_?OF_?MEM|MEM\w*NO_?FREE|"
                             r"NO_?FREE_?(BLKS?|BLOCKS?|BUFS?)", re.I)
OUTCOME_CONST_RE = re.compile(r"\b(?:[A-Z][A-Z0-9]*_[A-Z0-9_]+|[a-z]{2,}[A-Z][A-Z0-9_]{2,}|E[A-Z0-9]{3,})\b(?!\s*\()")
def _ring_compare(cond: str) -> bool:
    """`elist.head == elist.tail`: a ring buffer's two positions compared for (in)equality — ring words on BOTH sides,
    not `pcap_send(…, txbuflength) == PCAP_ERROR`."""
    for m in re.finditer(r"([\w.\->\[\]()]+)\s*(==|!=)\s*([\w.\->\[\]()]+)", STRING_RE.sub(" ", cond or "")):
        if set(split_ident(m.group(1))) & RING_WORDS and set(split_ident(m.group(3))) & RING_WORDS:
            return True
    return False


RING_WORDS = {"head", "tail", "wr", "rd", "wptr", "rptr", "widx", "ridx", "front", "rear", "in", "out", "put", "get",
              "write", "read", "next", "first", "last", "count", "size", "len", "cnt"}
STOP_RV = {"the", "a", "an", "is", "when", "what", "does", "do", "it", "to", "of", "on", "in", "x", "pd"}
STATE_WORDS = {"full", "empty", "ready", "busy", "idle", "valid", "enabled", "open", "connected", "locked", "active",
               "running", "pending", "available", "expired", "set", "initialized", "initialised", "blocked"}


OCCUPIED_Q_RE = re.compile(r"\b(owns?|owned|held|holds|taken|in use|occupied)\b", re.I)


def _state_polarity(question: str, cond: str) -> float:
    """+3 when this side of the check holds the state the question names ("a FULL queue" -> the side where
    `IsFull()` is true), -3 for the opposite side ("an INVALID fix" -> the side where `valid` is false)."""
    qw = set(split_ident(question))
    code = cond.strip()
    neg = bool(re.match(r"^!\s*[\w(]", code)) or bool(
        re.search(r"==\s*\(?\s*(pdFALSE|false|FALSE|0|NULL)\b", code) and not re.search(r"&&|\|\|", code))
    score = 0.0
    for b in set(split_ident(code)) & STATE_WORDS:
        # the negation may be inside the name itself: notEmpty, is_not_full, nonEmpty
        inner = bool(re.search(rf"(not|non|no)_?{b}", code, re.I))
        neg_b = neg != inner
        if re.search(rf"!=\s*[\w.:>-]*{b}", code, re.I):
            neg_b = not neg_b                      # `code != SlaveDeviceBusy`: the true side is "not busy"
        want = None
        if b in qw:
            want = 1
        if any(p + b in qw for p in ("in", "un", "dis", "non")) or re.search(rf"\bnot (yet )?{b}\b", question, re.I):
            want = -1
        if want is None and b in ("available", "idle") and OCCUPIED_Q_RE.search(question):
            want = -1                    # "a mutex that another task owns" is the side where it is NOT available
        if want is None:
            continue
        score += 3 if (-1 if neg_b else 1) == want else -3
    return score


def _failure_subject(q: str) -> set[str]:
    """The thing the question says is wrong: 'a bad checksum' -> checksum; 'the length is invalid' -> length."""
    from ..retrieval.cards import STOP
    ws = [w for pair in re.findall(rf"\b{FAIL_ADJ}\s+(\w+)(?:\s+(\w+))?", q, re.I) for w in pair if w]
    ws += re.findall(rf"\b(\w+)\s+(?:is|are|was|were|looks?)\s+(?:too \w+|{FAIL_ADJ})", q, re.I)
    ws += [w for w in re.findall(r"\b(\w+)\s+fails\b", q, re.I) if not w.lower().endswith("ing")]
    # "when SENDING on the channel fails": the action that fails is the gerund
    ws += [w for w in re.findall(r"\b(\w{4,}ing)\b(?:\s+\w+){0,5}\s+fails?\b", q, re.I)
           if w.lower() not in ("something", "anything", "nothing", "everything", "happening", "during")]
    ws += re.findall(r"\b(\w+)\s+(?:reaches|hits|drops to|falls to|becomes|goes to|overflows|wraps)\b", q, re.I)
    ws += [w for pair in re.findall(r"\b(?:runs?|ran|running) out of (\w+)(?:\s+(\w+))?|\bno (?:more )?(\w+)(?:\s+(\w+))? left\b",
                                    q, re.I) for w in pair if w]
    return expand_words({w.lower() for w in ws if w.lower() not in STOP and w.lower() not in WEAK_TOPIC})


def r_failure_invalid(cx: Ctx) -> None:
    g = cx.g
    if re.search(r"\b(not enough|insufficient|out of (memory|space|room)|no (free |more )?(memory|space|room)|"
                 r"runs? out|exhausted)\b", cx.r.question, re.I) and not cx.learn.pin_target() and r_insufficient(cx):
        return
    tnodes = topic_nodes(cx)
    ring_q = bool(re.search(r"\b(full|overflows?|overrun|empty)\b", cx.r.question, re.I))
    # the question says something is wrong (vs. asking what happens in a state: full, zero, empty, too much)
    err_q = bool(re.search(rf"\b{FAIL_ADJ}\b|\bfails?\b|\bfailure\b|\berrors? (occurs?|happens?|is raised|is reported)\b|"
                           r"\bgoes (bus.?off|wrong|down)\b|\bbus.?off\b|\bcorrupt|\bdoes not match\b|\bmismatch",
                           cx.r.question, re.I))
    # the function the question names (a unit test's helper of the same name never answers for the program)
    explicit_fn = next((c for c in cx.mentioned(FN_KINDS) if c in g.defined and (cx.about_tests or not g.is_test(c))),
                       None) if any(m.get("explicit") for m in cx.r.mentions) else None
    users = {o["fn"] for t in tnodes for o in g.occ.get(t, []) if o.get("fn")}
    helpers: set[str] = set()
    if explicit_fn:
        # the named function's own checking helpers (err = check_args(...); if (err) return err;) decide too
        helpers = {c.callee for c in g.out_calls.get(explicit_fn, []) if c.callee in g.defined and c.callee != explicit_fn
                   and not g.is_test(c.callee) and not DIAG_NAME_RE.search(g.name(c.callee)) and
                   any(e.fn == explicit_fn for e in g.fwd.get(f"{c.callee}#ret", []))}
    cand_fns = [explicit_fn] + sorted(helpers) if explicit_fn else sorted({f for f in g.defined if (f in tnodes or f in users or any(
        n in tnodes for n in [c.callee for c in g.out_calls.get(f, [])])) and (cx.about_tests or not g.is_test(f))})
    if not explicit_fn and cx.s.embedder is not None:
        # meaning search: functions whose description is close to the question even without shared words
        sem = [h.key for h in cx.s.search(cx.r.question, k=15, kinds=FN_KINDS) if "meaning" in h.why and h.key in g.defined]
        cand_fns = sorted(set(cand_fns) | set(sem))
        cx.semantic_fns = set(sem)
    topic = expand_words(cx.r.topic) - WEAK_TOPIC
    subject = _failure_subject(cx.r.question)          # "bad CHECKSUM", "the LENGTH is wrong": the property that fails
    ring_q = bool(re.search(r"\b(full|overflows?|overrun|empty)\b", cx.r.question, re.I))
    subject_words = [w for w in subject if w in set(split_ident(cx.r.question))] or list(subject)
    storage_q = bool(re.search(r"\b(blocks?|space|disk|flash|storage|sectors?|file ?system|eeprom|partition|pages?)\b",
                               cx.r.question, re.I))
    memory_q = bool(re.search(r"\b(memory|heap|ram|buffers?|pool|allocat\w*)\b", cx.r.question, re.I))
    exhaustion = bool(re.search(r"\b(runs?|ran|running) out\b|\bout of (memory|space|room|buffers?|blocks?)\b|\bexhaust|"
                                r"\bno (more )?(free )?(\w+ ){1,3}(left|available)\b|\bnone (left|available|remaining)\b|"
                                r"\bis full\b", cx.r.question, re.I))
    neg_q = bool(re.search(r"\b(not|doesn't|does not|isn't|is not|never|without|missing|non)\b|n't\b", cx.r.question, re.I))
    qwords_raw = set(split_ident(cx.r.question))
    zero_q = bool(re.search(r"\b(is|are|equals?|set to|becomes|of)\s+(zero|0|null|empty|false|none|disabled|off)\b",
                            cx.r.question, re.I))
    # running out ("none left", "no free blocks") is answered by NO_FREE / NOSPC codes, not by FULL ones
    absent_q = exhaustion and not re.search(r"\bis full\b|\bfull\b", cx.r.question, re.I)
    direction = "acquire" if ACQUIRE_Q_RE.search(cx.r.question) else "release" if RELEASE_Q_RE.search(cx.r.question) else None
    codec_dir = _codec_direction(cx.r.question)
    # the question's concepts (each word with its abbreviations); a check should cover as many of them as it can
    concepts = _concepts(cx)
    found = []

    def side_says(b) -> bool:
        # the condition names nothing from the question, but the side's own error code or log message does
        # (`if (ckpoint <= 0) { LFS_ERROR("No more free space"); return LFS_ERR_NOSPC; }`)
        head = " ".join(t for _, t in g.src.snippet(b["file"], b["then_s"], min(b["then_e"] or b["then_s"], b["then_s"] + 6)))
        said = {w for m in STRING_RE.findall(head) for w in split_ident(m) if len(w) > 3}
        said |= {w for c in OUTCOME_CONST_RE.findall(STRING_RE.sub(" ", head)) for w in split_ident(c)}
        # C#: `throw new ArgumentException(Resources.WaitRetryGreaterThanZero)` — the exception and its message say it
        for m in re.finditer(r"\bthrow\s+new\s+([\w.]+)\s*\(([^;]*)", STRING_RE.sub(" ", head)):
            said |= {w for w in split_ident(m.group(1) + " " + m.group(2)) if len(w) > 3}
        return bool(said & (topic | subject) - STOP_RV)
    for fn in cand_fns:
        for br in g.branches.get(fn, []):
            if br["kind"] == "switch" and br["cond"] and not _macro_internal(cx, br):
                # each case is a side of its own: the one whose outcome names the question is a candidate
                for cs in _case_sides(cx, fn, br):
                    if side_says(cs):
                        act = _branch_actions(cx, fn, cs)
                        if act["calls"] or act["writes"] or act["returns"]:
                            found.append((fn, cs, act))
                continue
            if br["kind"] == "catch":
                # C#: a catch block handles the failure of its try block — it is error handling by definition
                act = _branch_actions(cx, fn, br)
                if act["calls"] or act["writes"] or act["returns"]:
                    found.append((fn, br, act))
                continue
            if br["kind"] not in ("if", "ternary") or not br["cond"] or DIAG_COND_RE.match(br["cond"]) or \
                    _macro_internal(cx, br):
                continue
            words = set(split_ident(br["cond"]))
            reads_names = set()
            for r in br["reads"]:
                reads_names |= set(split_ident(_read_name(g, r)))
            cond_hit = bool((words | reads_names) & ERR_WORDS) or \
                bool((words | reads_names | _name_words(br["cond"], topic)) & topic) or (ring_q and _ring_compare(br["cond"]))
            if zero_q and not br.get("else_s") and br.get("then_s"):
                # "what happens when the period is zero?": `if (period != 0) { start keep-alive }` — when the test is
                # false (the question's case) the side is skipped, and that is the answer
                m0 = re.match(r"^\s*\(?\s*([A-Za-z_][\w.\->]*)\s*(!=\s*(0[uUlL]*|NULL|null|nullptr|false|FALSE)|>\s*0[uUlL]*)\s*\)?\s*$",
                              br["cond"])
                if m0 and set(split_ident(m0.group(1))) & topic:
                    act0 = _branch_actions(cx, fn, br)
                    if act0["calls"] or act0["writes"]:
                        found.append((fn, dict(br, cond=f"!({br['cond']})", _else=True, _skip=True, _pos_cond=br["cond"]),
                                      act0))
            tail = _failure_tail(cx, fn, br)
            if tail and (cond_hit or side_says(tail)):
                act3 = _branch_actions(cx, fn, tail)
                if act3["calls"] or act3["writes"] or act3["returns"]:
                    found.append((fn, tail, act3))
            if cond_hit or br.get("then_s") and side_says(br):
                act = _branch_actions(cx, fn, br)
                if act["calls"] or act["writes"] or act["returns"]:
                    found.append((fn, br, act))
                if br.get("else_s"):
                    act2 = _branch_actions(cx, fn, br, then=False)
                    if act2["calls"] or act2["writes"] or act2["returns"]:
                        b2 = dict(br, cond=f"!({br['cond']})", then_s=br["else_s"], then_e=br["else_e"], _else=True,
                                  _pos_cond=br["cond"])
                        found.append((fn, b2, act2))
    if not found and cx.learn.pin_target():
        # nothing found by the analysis, but a person said where the answer is: build that candidate from the code
        found = cx.learn.pin(found, lambda x: _branch_loc(x[0], x[1]), lambda c: _learned_branch(cx, c))
        if not found and cx.learn.pin_function() in g.defined:
            cx.learn.use_pin()
            return _failure_modes_of(cx, cx.learn.pin_function())
    if not found:
        if explicit_fn:
            return _failure_modes_of(cx, explicit_fn)
        return r_search(cx)
    # rank: branches that call a handler / change state rank first; then by pipeline depth
    qwords = expand_words(set(split_ident(cx.r.question))) & expand_words(ERR_WORDS | {
        "wrong", "corrupt", "missing", "low", "high", "empty", "full", "late", "stale", "negative", "zero", "null",
        "illegal", "unknown", "unsupported", "unexpected", "malformed", "exception", "overflow", "busy"})
    outcome_sig: dict[int, float] = {}
    _tries: dict[tuple, set[str]] = {}

    def try_words(fn: str, br: dict) -> set[str]:
        """Words of the code a catch block guards: from the nearest `try` before it to the catch."""
        k = (fn, br["line"])
        if k not in _tries:
            start = (g.sym.get(fn) or {}).get("line") or max(1, br["line"] - 40)
            body = g.src.snippet(br["file"], max(start, br["line"] - 60), br["line"] - 1)
            tl = [n for n, t in body if re.search(r"\btry\b", _code(t))]
            code = " ".join(_code(t) for n, t in body if not tl or n > tl[-1])
            # exceptions come from calls: the calls the try block makes and what they are called on
            # (`_channel.Send(msgBytes)` -> channel, send), not every name it mentions (`SendTimeout = …`)
            calls = re.findall(r"([A-Za-z_][\w.]*?)\.?([A-Za-z_]\w*)\s*\(", STRING_RE.sub(" ", code))
            ws = set()
            for recv, meth in calls:
                if meth in ("if", "while", "for", "switch", "catch", "lock", "using", "return", "new", "sizeof", "typeof"):
                    continue
                ws |= set(split_ident(meth)) | set(split_ident(recv.split(".")[-1] if recv else ""))
            _tries[k] = {w for w in ws if len(w) > 2} - STOP
        return _tries[k]
    dbg: dict[int, dict] = {}
    cx.rank_dbg = dbg

    def score(x):
        rel, failure = parts(x)
        f = min(12.0, failure)                    # a long, busy side must not out-shout a relevant, short one
        if not err_q:
            f *= 0.25                             # "when the list is full" asks what happens, not where errors go
        return -(rel * (1 + 0.06 * max(0.0, f)) + 0.35 * f)

    def parts(x):
        fn, br, act = x
        handler = min(2, sum(1 for c in act["calls"] if c.line != br["line"] and
                             re.search(r"invalid|error|fail|reject|fault|bad|exception", g.name(c.callee), re.I)))
        status_cmp = 1 if any(r.startswith(("c:@E", "c:@Ea", "c:@EA")) for r in br["reads"]) or \
            re.search(r"\b(status|valid|ok|result)\b", br["cond"], re.I) else 0
        # words of the condition's code, of its message strings (LWIP_ERROR("bad proto", …)), of the calls it makes and
        # of the function it is in — weighted by how specific each word is in this project
        cond_code = STRING_RE.sub(" ", br["cond"])
        cwords = set(split_ident(cond_code)) | {w for r in br["reads"] for w in split_ident(_read_name(g, r))}
        if br["kind"] == "catch":
            cwords |= try_words(fn, br)                  # what failed is what the try block was doing
        # "the buffer is full" is a capacity test: `bytes_written + count > max_size`, `count >= size`
        if "full" in qwords_raw and (re.search(r"(?<![-=<>])(>=?)(?!>)\s*\(?\s*[\w.\->]*(max|size|capacity|cap|limit|len)\w*",
                                               cond_code, re.I) or     # `>` of a comparison, not of `->`
                                     br.get("_else") and re.search(r"(?<![<-])<(?![<=])\s*\(?\s*[\w.\->]*(max|size|capacity|cap|limit|len)\w*",
                                                                   cond_code, re.I)):   # the NOT side of `count < length`
            cwords.add("full")
        # "a tag of zero" is `tag == 0`, "a null pointer" is `== NULL`, "a negative value" is `< 0`
        for word, pats in VALUE_WORDS:
            # …but only when what is compared is what the question names: "a TAG of zero" is `tag == 0`, not
            # `bytes_left == 0`
            if word in qwords_raw and any(set(split_ident(m.group("lhs") or "")) & (topic - {word})
                                          for pat in pats for m in re.finditer(pat, cond_code)):
                cwords.add(word)
        cwords |= _name_words(cond_code, topic) & topic          # elist -> list, slavecount -> slave, count
        if ring_q and _ring_compare(cond_code):
            cwords |= set(re.findall(r"\b(full|empty|overflows?|overrun)\b", cx.r.question.lower()))   # head meets tail
        mwords = set(split_ident(" ".join(STRING_RE.findall(br["cond"])))) - cwords
        kwords = {w for c in act["calls"] for w in split_ident(g.name(c.callee))} - cwords
        fwords = set(split_ident(g.name(fn))) - cwords
        words = cwords | kwords | mwords
        tp = topic - {"invalid"}
        W = cx.word_weight

        def per_concept(ws: set[str]) -> float:
            # each concept of the question counts once, by its best-matching word (length/len/size are one idea)
            groups = concepts or [{w} for w in tp]
            return sum(max((W(w) for w in c & ws & tp), default=0.0) for c in groups)
        # what the tested locals were computed from (`DLC = CO_CANrxMsg_readDLC(msg)`: DLC is the message's length)
        dwords = _origin_words(g, fn, br) - cwords
        ntopic = per_concept(cwords) + 0.5 * per_concept(kwords) + 0.3 * per_concept(mwords) + \
            0.5 * per_concept(fwords) + 0.5 * per_concept(dwords)
        cmp_topic = max((W(w) for w in topic if len(w) > 2 and re.search(
            rf"{re.escape(w)}\w*(\([^()]*\))?\s*(<=|>=|<(?!<)|(?<!-)>(?!>)|!=|==)|(<=|>=|(?<!<)<|(?<![->])>|!=|==)\s*[\w.>-]*{re.escape(w)}", cond_code, re.I)),
            default=0)
        if cmp_topic and QTY_Q_RE.search(cx.r.question) and any(len(w) > 2 and re.search(
                rf"{re.escape(w)}\w*(\([^()]*\))?\s*(<=|>=|<(?!<)|(?<!-)>(?!>))|(<=|>=|(?<!<)<|(?<![->])>)\s*[\w.>-]*{re.escape(w)}",
                cond_code, re.I) for w in topic):
            cmp_topic += 0.5          # "needs more bytes than are left": an ordering test (`bytes_left < count`), not `!= 0`
        # which side is the FAILURE side: error returns, error counters, error/log calls (not the success path)
        body = " ".join(t for _, t in act["lines"][:14])     # a handler's first lines; long sides are not handlers
        jump = act.get("jump")
        jw = jump["writes"] if jump else []
        fail = 2 if jump else 0                              # `goto` out of a check is the C error-exit idiom
        rm = re.search(r"\breturn\s*\(?\s*(FALSE|false|0|-\d+|NULL|nullptr|pdFAIL|pdFALSE|\w*(ERR|FAIL|INVALID|NO_|BAD|TIMEOUT|FULL|EMPTY|BUSY)\w*)\s*\)?\s*;",
                       body, re.I)
        if rm and not SUCCESS_CONST_RE.search(rm.group(1)):        # `return OS_ERR_NONE;` is the success return
            fail += 4
        if re.search(r"\bthrow\b", body):
            fail += 4                                     # C#: throwing an exception is the failure path
        if br["kind"] == "catch":
            fail += 3
        fail += 3 * min(2, sum(1 for sym, _ in act["writes"] + jw if re.search(
            r"err|fail|invalid|reject|drop|bad|lost|timeout|miss", g.name(sym), re.I)))
        fail += 2 * min(2, sum(1 for c in act["calls"] if re.search(r"log|error|fail|report|warn|alarm|fault", g.name(c.callee),
                                                                    re.I)))
        success = sum(1 for sym, _ in act["writes"] if re.search(r"(^|[._])(ok|good|success|valid)", g.name(sym), re.I))
        # the question's own key word (invalid / checksum / crc / range / timeout…) named by this branch's code
        names = words | {w for sym, _ in act["writes"] for w in split_ident(g.name(sym))} | set(split_ident(body))
        size = act["range"][1] - act["range"][0] + 1 if act.get("range") else 0
        # the side OPENS with the error handling (an error response / errno / error return as its first statement)
        body_ls = [t for n, t in act["lines"] if n > br["line"] and _code(t) not in ("", "{", "}", "else", "} else {")]
        first = " ".join(body_ls[:3]).split(";", 1)[0]
        if re.search(r"\b\w*(exception|error|fail|reject|invalid|errno)\w*\s*[=(]|\breturn\s+(-\d|\w*(ERR|FAIL|NULL))",
                     _code(first), re.I) or ERR_ASSIGN_RE.search(_code(first)):
            fail += 3                                    # …or sets the result to a failure (`rc = FAILURE;`)
        # relevance to the question comes first; looking like error handling only orders equally relevant checks
        subj_groups = [expand_words({w}) & subject for w in subject_words] or [subject]
        rel = 6 * ntopic + 5 * cmp_topic + 3 * len(qwords & names) + \
            6 * sum(max((W(w) for w in g_ & cwords), default=0.0) for g_ in subj_groups if g_) + \
            3 * sum(max((W(w) for w in g_ & fwords), default=0.0) for g_ in subj_groups if g_)  # Send() for "sending fails"
        rel += _state_polarity(cx.r.question, cond_code)
        if br.get("_skip"):
            rel += 8                                   # tests exactly the state the question names (`period != 0`)
        if fn in getattr(cx, "semantic_fns", ()):
            rel += 3                                   # meaning search found this function for the question
        if direction:
            # "asks for a block" is the get/alloc/pend side; "returns a block" the put/free/post side
            fw = set(split_ident(g.name(fn)))
            want, other = (ACQUIRE_WORDS, RELEASE_WORDS) if direction == "acquire" else (RELEASE_WORDS, ACQUIRE_WORDS)
            rel += 2 if fw & want else -2 if fw & other else 0
        if codec_dir:
            # "a DECODED message" is answered by the decoder, not by encode_field() in pb_encode.c
            fw2 = set(split_ident(g.name(fn))) | set(split_ident(re.sub(r"\.\w+$", "", br["file"].rsplit("/", 1)[-1])))
            sides = {w for w in fw2 for d, pat in CODEC_DIR_RE.items() if pat.match(w) and d != codec_dir}
            if sides and not any(CODEC_DIR_RE[codec_dir].match(w) for w in fw2):
                rel -= 3
        if ring_q and _ring_compare(cond_code):
            rel += 6                                   # "the list/queue/buffer is full": head meets tail
        # the side's own outcome names the question ("a full queue" -> `return errQUEUE_FULL;`, "an illegal data
        # address" -> `response_exception(…, MODBUS_EXCEPTION_ILLEGAL_DATA_ADDRESS, …)`): the constants it returns,
        # stores or reports in its first statements
        rv = _returned_value(act) or ""
        head = " ".join(t for _, t in act["lines"][:8]) + " " + rv
        consts = {c for c in set(OUTCOME_CONST_RE.findall(STRING_RE.sub(" ", head))) - set(OUTCOME_CONST_RE.findall(br["cond"] or ""))
                  if not SUCCESS_CONST_RE.search(c)}                 # OS_ERR_NONE / ERR_OK say nothing went wrong
        consts |= set(re.findall(r"\bnew\s+([A-Z]\w*(?:Exception|Error))\b", head))   # C#: the exception it throws
        if absent_q and any(EXHAUST_CONST_RE.search(c) for c in consts) and \
                not any(ABSENT_CONST_RE.search(c) for c in consts):
            rel += 3                 # "none left" answered by a FULL code is the other direction (putting into a full pool)
        elif exhaustion and any(EXHAUST_CONST_RE.search(c) for c in consts):
            # "runs out of …" answered by the code's out-of-resource error — of the right kind: storage (blocks, space,
            # flash) is NOSPC/FULL, memory (heap, RAM, buffers) is NOMEM/ERR_MEM
            st_ok = any(STORAGE_CONST_RE.search(c) for c in consts)
            mem_ok = any(MEMORY_CONST_RE.search(c) for c in consts)
            # "a memory block … the partition" names both kinds: either kind of code answers it
            kind_ok = (st_ok or mem_ok) if storage_q and memory_q else (not storage_q or st_ok) and (not memory_q or mem_ok)
            rel += 10 if kind_ok else 2
        ow = set()
        for c in consts:
            toks = split_ident(c)
            # OS_ERR_TASK_NOT_EXIST says the opposite of "already exists": a negated word only answers a negated question
            ow |= {w for i, w in enumerate(toks) if neg_q or i == 0 or toks[i - 1] not in ("not", "non")}
        qx = expand_words(set(split_ident(cx.r.question))) - {"return", "returns"} - STOP_RV
        in_concepts = set().union(*concepts) if concepts else set()
        # each idea of the question counts once (free + blocks are two; "task control blocks" is one)
        out_hits = sum(1 for c in concepts if c & ow) + len((ow & qx) - in_concepts)
        # weighted like the topic: "client" in MqttClientException says little in a project full of MqttClient*
        rel += 6 * (sum(max(0.6, max(W(w) for w in c & ow)) for c in concepts if c & ow) +
                    sum(max(0.6, W(w)) for w in (ow & qx) - in_concepts))
        # …and its log message says it in words (`LFS_ERROR("No more free space …")`)
        msg = {w for m in STRING_RE.findall(" ".join(t for _, t in act["lines"][:12])) for w in split_ident(m) if len(w) > 3}
        msg |= {w for w in expand_words(msg) if len(w) > 3}        # "connecting to the broker" says connect
        msg_hits = len(msg & expand_words(set(cx.r.topic)) - STOP_RV)
        rel += min(6, 2 * msg_hits) + 6 * sum(W(w) for w in msg & subject)
        # how much the side's own outcome (codes, log text) says what the question describes — a separate signal
        outcome_sig[id(x)] = out_hits + msg_hits + (3 if exhaustion and any(EXHAUST_CONST_RE.search(c) for c in consts)
                                                   else 0)
        dbg[id(x)] = {"ntopic": round(ntopic, 2), "cmp": cmp_topic, "out_hits": out_hits, "msg_hits": msg_hits,
                      "ow": sorted(ow), "pre_cov_rel": round(rel, 2), "cw": sorted(cwords & topic),
                      "dw": sorted(dwords & topic), "fw": sorted(fwords & topic), "kw": sorted(kwords & topic)}
        allw_msg = msg
        # coverage counts what the check and its reaction are about (condition, calls, stored names, function),
        # not every token of a long body
        allw = words | fwords | {w for sym, _ in act["writes"] for w in split_ident(g.name(sym))} | allw_msg | dwords | ow
        # the module names the role of its code ("the decoder" covers pb_read() in pb_decode.c) — coverage only
        allw |= {w for w in split_ident(re.sub(r"\.\w+$", "", br["file"].rsplit("/", 1)[-1])) if len(w) > 2} - STOP
        if concepts:
            rel *= 0.25 + 0.75 * sum(1 for c in concepts if c & allw) / len(concepts)
        if fn in helpers:
            rel *= 0.85                               # one call away from the function the question names
        qside = _question_side(g, fn, br, topic, cx.r.question)
        if qside and (qside > 0) == bool(br.get("_else")):
            rel *= 0.4                                # the side that runs when the question's case does NOT hold
        elif qside:
            rel *= 1.5                                # the question names the value this check tests, and this side is it
        failure = 4 * handler + 3 * status_cmp + fail - 3 * success + 0.5 * min(4, len(act["writes"])) + \
            (1 if act["returns"] else 0) - min(6.0, 0.05 * max(0, size - 15))
        return rel, failure
    found.sort(key=score)
    loc_f = lambda x: _branch_loc(x[0], x[1])          # noqa: E731
    # similar questions: corrected places get a bonus (and join the candidates, with their own score, if missing)
    found = cx.learn.rank(found, loc_f, lambda x: -score(x), lambda c: _learned_branch(cx, c))
    if cx.llm is not None and len(found) > 1:
        # the model can only choose well from a pool that holds the answer: the best by overall score, plus the best
        # whose own outcome (error code / log message) matches the question, plus meaning-search functions
        pool = found[:6]
        by_outcome = sorted((x for x in found[6:60] if outcome_sig.get(id(x), 0) > 0), key=lambda x: -outcome_sig[id(x)])
        sem = [x for x in found[6:80] if x[0] in getattr(cx, "semantic_fns", ())]
        for x in by_outcome[:3] + sem[:2]:
            if x not in pool:
                pool.append(x)
        chosen = _rerank(cx, pool, lambda x: _render_branch(cx, x))
        if chosen[0] is not found[0]:
            found = [chosen[0]] + [x for x in found if x is not chosen[0]]
    # the same question corrected by a person: that place goes first (after the model, so the correction holds)
    found = cx.learn.pin(found, loc_f, lambda c: _learned_branch(cx, c))
    if cx.learn.pin_target() and not cx.learn.pin_applied and cx.learn.pin_function() in g.defined and \
            not cx.learn.pin_target().get("branch_line"):
        cx.learn.use_pin()
        return _failure_modes_of(cx, cx.learn.pin_function())
    cx.learn.offer(found, loc_f, lambda x: (_branch_label(cx, x[0], x[1]), _render_branch(cx, x)), lambda x: -score(x))
    cx.rank_debug = [(g.name(f), b["line"], b["cond"][:60], round(score((f, b, a)), 2)) for f, b, a in found[:8]]
    cx.rank_all = found
    cx.rank_parts = parts
    main_fn, main_br, act = found[0]
    case = main_br.get("_case")
    when = (f"In the case {' / '.join(f'`{x}`' for x in case)}" if case else f"When `{main_br['cond']}` is true")
    c_br = cx.claim(f"{cx.D(main_fn)} " + (f"switches on `{main_br['cond'].rsplit(' is ', 1)[0]}`" if case else
                                          f"checks `{main_br['cond']}`") + f" at {cx.loc(main_br['file'], main_br['line'])}.",
                    "branch", [main_fn, main_br["file"], main_br["line"]],
                    [cx.ev("branch", main_br["file"], main_br["line"], main_fn)])
    what = []
    cls = [c_br]
    for c in act["calls"]:
        st, why = cx.call_status(c)
        cls.append(cx.claim(f"{when}, {cx.D(main_fn)} calls {cx.D(c.callee)} ({cx.loc(c.file, c.line)}).",
                            "calls", [main_fn, c.callee, c.file, c.line], [cx.ev("call", c.file, c.line, c.callee)], st, why))
    for sym, o in act["writes"]:
        cls.append(cx.claim(f"{when}, {cx.D(main_fn)} writes {cx.D(sym)} ({cx.loc(o['file'], o['line'])}).",
                            "writes", [main_fn, sym, o["file"], o["line"]], [cx.ev("write", o["file"], o["line"], sym)]))
    if act.get("nested"):
        # the side has its own decisions: describe it step by step (a return inside them is conditional)
        rs, re_ = act["range"]
        sub = describe_steps(cx, main_fn, [it for it in fn_steps(cx, main_fn, max_steps=400)
                                           if rs <= it["line"] <= re_ and it["line"] != main_br["line"]][:40])
        for b in act["nested"][:4]:
            cls.append(cx.claim(f"{cx.D(main_fn)} then checks `{b['cond']}` ({cx.loc(b['file'], b['line'])}).", "branch",
                                [main_fn, b["file"], b["line"]], [cx.ev("branch", b["file"], b["line"], main_fn)]))
        what += sub[:6]
        if len(sub) > 6:
            # the side goes on: the state it changes that the question is about (`ptcb->OSTCBPrio = pcp`), if not yet said
            tw = expand_words(set(cx.r.topic)) - WEAK_TOPIC
            shown = " ".join(sub[:6])
            key = []
            for sym, o in act["writes"]:
                t = _stmt(g, o["file"], o["line"])
                if "#" in sym or t in shown or not re.match(r"^[^=!<>]*[^=!<>+\-*/&|^%]=(?!=)", t):
                    continue
                if set(split_ident(g.name(sym))) & tw and t not in [k for k, _ in key]:
                    key.append((t, o["line"]))
            if key:
                what.append("also sets " + " and ".join(f"`{t}` (line {n})" for t, n in key[:2]))
    else:
        what += [_call_phrase(cx, c) for c in act["calls"]]
        # a write made by the returning macro itself (`PB_RETURN_ERROR` sets errmsg) is said once, as the return
        what += _records(cx, [w for w in act["writes"] if not _is_exit(g, g.src.line(w[1]["file"], w[1]["line"]))])
    what = list(dict.fromkeys(what))              # `OS_EXIT_CRITICAL()` expands to the same call twice
    sep = "; then it " if act.get("nested") else " and "
    said_return = any(w.startswith("returns") or " returns " in w for w in what)
    jump = act.get("jump")
    if jump:
        jd = [f"calls {cx.D(c.callee)}" for c in jump["calls"] if not g.name(c.callee).startswith(("printf", "puts"))]
        jd += _records(cx, jump["writes"])
        for c in jump["calls"]:
            cls.append(cx.claim(f"After `{jump['label']}:` {cx.D(main_fn)} calls {cx.D(c.callee)} ({cx.loc(c.file, c.line)}).",
                                "calls", [main_fn, c.callee, c.file, c.line], [cx.ev("call", c.file, c.line, c.callee)]))
        what.append(f"jumps to `{jump['label']}` (line {jump['line']})" + (", where it " + " and ".join(jd) if jd else "") +
                    f", skipping the rest of {cx.D(main_fn)}")
    elif act.get("returns_top") and not said_return and _thrown_value(act) and not _returned_value(act):
        what.append(f"throws `{_thrown_value(act)}`, so the rest of {cx.D(main_fn)} is skipped and the exception goes to "
                    f"its caller")
    elif act.get("returns_top") and not said_return:
        rv = _returned_value(act)
        em = None if rv else _exit_macro_call(g, act)
        what.append((f"returns `{rv}`" if rv else f"returns through `{em}`" if em else "returns immediately") +
                    f", so the rest of {cx.D(main_fn)} is skipped")
    elif act.get("returns_top") and what:
        what[-1] += f", so the rest of {cx.D(main_fn)} is skipped"
    if not what:
        what = [f"runs lines {act['range'][0]}–{act['range'][1]}"]
    if re.search(r"\breturns?\b", cx.r.question, re.I) and not act.get("returns_top"):
        # "what does F return when …": the value it returns after this side (return notEmpty;)
        after = sorted((o for o in g.occ.get(f"{main_fn}#ret", []) if o["line"] > (act["range"][1] or 0)),
                       key=lambda o: o["line"])
        if after:
            what.append(f"afterwards returns `{_code(g.src.line(after[0]['file'], after[0]['line']))[6:].strip()}` "
                        f"(line {after[0]['line']})")
    if main_br["kind"] == "catch":
        exc = re.sub(r"^catch\s*\((.*?)\)(.*)$", r"\1\2", main_br["cond"]).strip()
        cx.say(f"This is handled in {cx.D(main_fn)} ({cx.loc(main_br['file'], main_br['line'])}): when the code in its try "
               f"block throws `{exc}`, it " + sep.join(what) + ".", cls)
    elif case:
        cx.say(f"This is handled in {cx.D(main_fn)} ({cx.loc(main_br['file'], main_br['line'])}): it switches on "
               f"`{main_br['cond'].rsplit(' is ', 1)[0]}`, and in the case {' / '.join(f'`{x}`' for x in case)} "
               f"(line {main_br['then_s']}) it " + sep.join(what) + ".", cls)
    elif main_br.get("_skip"):
        orig = main_br.get("_pos_cond") or main_br["cond"][2:-1]
        skipped = [_call_phrase(cx, c) for c in act["calls"]] + [f"`{_stmt(g, o['file'], o['line'])}`" for _, o in act["writes"]]
        cx.say(f"This is decided in {cx.D(main_fn)} ({cx.loc(main_br['file'], main_br['line'])}): it checks `{orig}`; when "
               f"that is NOT true — the case in the question — it skips lines {main_br['then_s']}–{main_br['then_e']}, so "
               f"this does not happen: " + "; ".join(dict.fromkeys(skipped)) + ".", cls)
    elif main_br.get("_else"):
        orig = main_br.get("_pos_cond") or main_br["cond"][2:-1]
        cx.say(f"This is decided in {cx.D(main_fn)} ({cx.loc(main_br['file'], main_br['line'])}): it checks `{orig}`"
               f"{_local_origin(cx, main_fn, main_br)}; when that is NOT true it " + sep.join(what) + ".", cls)
    else:
        lead = "Invalid data is caught in" if err_q and qwords & {"invalid", "bad", "corrupt", "wrong", "crc", "checksum",
                                                                 "error", "err", "fail", "reject", "illegal", "malformed"} \
            else "This is handled in"
        cx.say(f"{lead} {cx.D(main_fn)} ({cx.loc(main_br['file'], main_br['line'])}): it checks "
               f"`{main_br['cond']}`{_local_origin(cx, main_fn, main_br)}, and when that is true it " + sep.join(what) + ".",
               cls)
    cx.flow_step(f"{cx.D(main_fn)} " + (f"switches on `{main_br['cond'].rsplit(' is ', 1)[0]}` (case "
                                        f"{' / '.join(case)})" if case else f"checks `{main_br['cond']}`"),
                 main_br["file"], main_br["line"], cls[0])
    # what the skipped part would have done
    stop = jump["line"] if jump else 10 ** 9
    other_side = (main_br.get("else_s"), main_br.get("else_e")) if not main_br.get("_else") else (None, None)
    # code that cannot come next: another catch handler, or the other side of an if/else this check sits in
    # (`if (exceptionResponse != null) {…} else if (ShouldRetryResponse(…))`)
    not_next = []
    ml = main_br["line"]
    for b in g.branches.get(main_fn, []):
        if b["line"] == ml or b.get("file") not in (None, main_br["file"]):
            continue
        t_in = bool(b.get("then_s")) and b["then_s"] <= ml <= (b.get("then_e") or 0)
        e_in = bool(b.get("else_s")) and b["else_s"] <= ml <= (b.get("else_e") or 0)
        if b["kind"] == "catch" and not t_in:
            not_next.append((b.get("then_s") or b["line"], b.get("then_e") or b["line"]))
        elif t_in and b.get("else_s"):
            not_next.append((b["else_s"], b.get("else_e") or b["else_s"]))
        elif e_in and b.get("then_s"):
            not_next.append((b["then_s"], b.get("then_e") or b["then_s"]))
    rest = [c for c in g.out_calls.get(main_fn, []) if (main_br["then_e"] or main_br["line"]) < c.line < stop and
            c.file == main_br["file"] and                      # one definition: not another port's copy of main_fn
            not _diag_line(cx, c.file, c.line) and not DIAG_NAME_RE.search(g.name(c.callee)) and
            not (other_side[0] and other_side[0] <= c.line <= (other_side[1] or 0)) and   # the else side does not run
            not any(a_ <= c.line <= z_ for a_, z_ in not_next)]
    rest_w = sorted({sym for sym, o in g.fn_occ(main_fn) if o["fn"] == main_fn and
                     (main_br["then_e"] or 0) < o["line"] < stop
                     and o["role"] in ("write", "rw") and "#" not in sym and not _partial(o)}, key=lambda k: cx.D(k))
    items = [cx.D(c.callee) for c in rest][:4] + [cx.D(w) for w in rest_w if g.sym.get(w, {}).get("kind") == "global"][:3]
    if act.get("returns_top") and items:
        cx.say(f"Because it returns early, these do not happen in this case: " + ", ".join(dict.fromkeys(items)) +
               " — the previous values stay in place.")
    elif rest and not act.get("returns_top"):
        # the path can continue: what the function goes on to do (blocking, retrying, waiting…)
        cand = [c for c in rest if not UTILITY_RE.search(g.name(c.callee)) and not g.name(c.callee).startswith(("trace", "mtCOVERAGE"))]
        tw = set(cx.r.topic)

        def rank(c):
            words = set(split_ident(" ".join(c.args) + " " + g.name(c.callee)))
            sync = bool(re.search(r"(Enter|Exit)Critical|Suspend|Resume|Lock|Unlock|Yield", g.name(c.callee)))
            return -(2 * len(words & tw) + (1 if c.args else 0) - (2 if sync else 0))
        chosen = {id(c) for c in sorted(cand, key=rank)[:5]}
        nxt = [c for c in cand if id(c) in chosen]
        if nxt:
            ncl = [cx.claim(f"{cx.D(main_fn)} calls {cx.D(c.callee)} at {cx.loc(c.file, c.line)}.", "calls",
                            [main_fn, c.callee, c.file, c.line], [cx.ev("call", c.file, c.line, c.callee)]) for c in nxt]
            cx.say(f"When it does not return there, {cx.D(main_fn)} goes on to call " +
                   "; ".join(f"{cx.D(c.callee)}" + (f" with ({', '.join(f'`{a}`' for a in c.args[:3] if a.strip())})" if any(a.strip() for a in c.args) else "") +
                             f" (line {c.line})" for c in nxt) + ".", ncl)
    # state this side stores itself (`SYNC->receiveError = DLC | 0x40`): where it is checked later
    own = [(sym, o) for sym, o in act["writes"] if "#" not in sym and g.sym.get(sym, {}).get("kind") in ("field", "global")]
    if own:
        _narrate_consequences(cx, own, main_fn)
    # the handler(s): what they change (at most two; diagnostics such as asserts and debug printing are not handlers)
    handlers = list(dict.fromkeys(c.callee for c in act["calls"] if c.callee in g.defined and not g.is_test(c.callee) and
                                  not DIAG_NAME_RE.search(g.name(c.callee))))[:2]
    for h in handlers:
        steps = describe_steps(cx, h, fn_steps(cx, h))
        hc = []
        for br in g.branches.get(h, []):
            hc.append(cx.claim(f"{cx.D(h)} checks `{br['cond']}` at {cx.loc(br['file'], br['line'])}.", "branch",
                               [h, br["file"], br["line"]], [cx.ev("branch", br["file"], br["line"], h)]))
        hw = []
        for sym, o in g.fn_occ(h):
            if o["fn"] == h and o["role"] in ("write", "rw") and "#" not in sym and not _partial(o):
                hw.append((sym, o))
                hc.append(cx.claim(f"{cx.D(h)} writes {cx.D(sym)} at {cx.loc(o['file'], o['line'])}.", "writes",
                                   [h, sym, o["file"], o["line"]], [cx.ev("write", o["file"], o["line"], sym)]))
        if steps:
            cx.say(f"{cx.D(h)} " + "; then it ".join(steps[:6]) + ".", hc)
        cx.flow_step(f"{cx.D(h)} records the problem", g.sym[h]["file"], g.sym[h]["line"], cx.ev_def(h))
        _narrate_consequences(cx, hw, h)
    # what the callers do when this function reports the problem (error propagation, up to two levels)
    _tv = _thrown_value(act) or ""
    _nt = re.match(r"new\s+([\w.]+)", _tv)
    _propagate_failure(cx, main_fn, depth=2, throws=bool(_tv), thrown_type=_nt.group(1).split(".")[-1] if _nt else None,
                       fail_value=_returned_value(act) if act.get("returns_top") else _local_result(cx, main_fn, act, main_br))
    # the rules that decide "invalid"
    validators = [c.callee for c in g.out_calls.get(main_fn, []) if c.callee in g.defined and
                  set(split_ident(g.name(c.callee))) & {"validate", "check", "verify", "valid", "ok", "parse"}]
    for vf in validators[:1]:
        rules = []
        rc = []
        for br in g.branches.get(vf, []):
            act2 = _branch_actions(cx, vf, br)
            ret = [t for _, t in act2["lines"] if "return" in t]
            if ret:
                rv = re.sub(r".*return\s+", "", ret[0]).strip().rstrip(";")
                rules.append(f"`{br['cond']}` → {rv}")
                rc.append(cx.claim(f"{cx.D(vf)} returns {rv} when `{br['cond']}` ({cx.loc(br['file'], br['line'])}).",
                                   "branch", [vf, br["file"], br["line"]], [cx.ev("branch", br["file"], br["line"], vf)]))
        if rules:
            cx.say(f"{cx.D(vf)} decides what counts as invalid: " + "; ".join(rules) + ".", rc)
    layers = []
    lc = []
    best_rel = parts(found[0])[0]
    cx.trigger_cond = main_br["cond"]
    def failure_write(sym, o):
        if "#param:" in sym:                       # `*perr = OS_ERR_X`: rejection only when it reports an error
            rhs = _stmt(g, o["file"], o["line"]).split("=", 1)[-1]
            return bool(re.search(r"ERR|FAIL|INVALID|NO_|BAD|-\d", rhs)) and not SUCCESS_CONST_RE.search(rhs.strip(" ;()"))
        return bool(re.search(r"err|fail|invalid|reject|drop|bad", g.name(sym), re.I))
    for fn, br, a in (found[1:] if err_q else []):  # "bad input is also rejected…" answers questions about bad input
        if fn == main_fn or br["kind"] != "if" or not a["returns"] or score((fn, br, a)) > -4 or \
                parts((fn, br, a))[0] < 0.5 * best_rel or \
                (subject and not set(split_ident(STRING_RE.sub(" ", br["cond"]))) & subject):
            continue                               # only other places that clearly reject the SAME kind of problem
        # the side itself rejects: its failure return / error report is not inside a further decision (a side that
        # goes on decoding and may fail somewhere deeper is not a rejection)
        nest = a.get("nested") or []

        def in_nested(n, nest=nest):
            return any((b["then_s"] or 0) <= n <= (b["then_e"] or 0) or (b["else_s"] or 0) <= n <= (b["else_e"] or -1)
                       for b in nest)
        body = " ".join(t for n, t in a["lines"] if not in_nested(n))
        top_writes = [(sym, o) for sym, o in a["writes"] if not in_nested(o["line"])]
        rv = re.search(r"\breturn\s+\(?\s*(FALSE|false|0|-\d+|NULL|[A-Z_]*(ERR|FAIL|INVALID|NO_|BAD)[A-Z_]*)\s*\)?\s*;", body)
        if not a.get("returns_top") or (not (rv and not SUCCESS_CONST_RE.search(rv.group(1))) and
                                        not any(failure_write(sym, o) for sym, o in top_writes)):
            continue
        if rv and rv.group(1) == "0" and (_fails_falsy(g, fn) is False or br.get("_tail")):
            continue                               # `return 0;` after the error check is the success path
        if any(x[0] == fn for x in layers):
            continue
        counted = [f"{cx.D(sym)}" for sym, o in a["writes"] if "#" not in sym and (o.get("role") == "rw" or
                   re.search(r"\+\+|--|\+=|-=", _stmt(g, o["file"], o["line"])))][:2]     # counters (STATS_INC(drop))
        reported = [f"`{_stmt(g, o['file'], o['line'])}`" for sym, o in top_writes if "#param:" in sym and failure_write(sym, o)][:1]
        lc.append(cx.claim(f"{cx.D(fn)} rejects data when `{br['cond']}` ({cx.loc(br['file'], br['line'])}).", "branch",
                           [fn, br["file"], br["line"]], [cx.ev("branch", br["file"], br["line"], fn)]))
        layers.append((fn, f"{cx.D(fn)} rejects it when `{br['cond']}` ({cx.loc(br['file'], br['line'])})" +
                       (f", counting it in {', '.join(counted)}" if counted else "") +
                       (f", reporting {reported[0]}" if reported else "")))
        if len(layers) >= 3:
            break
    if layers:
        cx.say("Bad input is also rejected in other places: " + "; ".join(t for _, t in layers) + ".", lc)
    _scenario_hint(cx, main_fn)


CAPACITY_WORDS = {"free", "remaining", "available", "avail", "size", "length", "len", "count", "capacity", "max", "space",
                  "bytes", "limit", "room", "left", "waiting", "full", "empty"}


def r_insufficient(cx: Ctx) -> bool:
    """'What does F do when there is not enough X?': the capacity check that fails, and what the function returns on
    that path (the value its result variable keeps when the guarded assignment is skipped). True if answered."""
    g = cx.g
    fn = next((c for c in cx.targets(FN_KINDS) if c in g.defined), None)
    if not fn:
        return False
    best = None
    for br in g.branches.get(fn, []):
        if br["kind"] != "if" or not re.search(r"(<=|>=|<|>)", br["cond"] or ""):
            continue
        words = set(split_ident(br["cond"]))
        if words & CAPACITY_WORDS:
            score = len(words & CAPACITY_WORDS)
            if best is None or score > best[0]:
                best = (score, br)
    if not best:
        return False
    br = best[1]
    file, a, b = cx.body_lines(fn)
    # the function's result: `return X;` after the branch, and X's writers
    rets = [o for o in g.occ.get(f"{fn}#ret", []) if o["line"] > (br["then_e"] or br["line"])]
    ret_expr = None
    if rets:
        t = _code(g.src.line(rets[-1]["file"], rets[-1]["line"]))
        ret_expr = re.sub(r"^return\s*", "", t).strip("() ")
    cl = [cx.claim(f"{cx.D(fn)} checks `{br['cond']}` at {cx.loc(br['file'], br['line'])}.", "branch",
                   [fn, br["file"], br["line"]], [cx.ev("branch", br["file"], br["line"], fn)])]
    text = (f"The capacity check is in {cx.D(fn)} ({cx.loc(br['file'], br['line'])}): `{br['cond']}`. When there is not "
            f"enough, that condition is false and the block at lines {br['then_s']}–{br['then_e']} is skipped")
    if ret_expr and re.fullmatch(r"[A-Za-z_]\w*", ret_expr):
        local = next((k for k in g.sym if k.startswith(fn + "#local:" + ret_expr + "@")), None)
        if local:
            ws = g.writers(local)
            outside = [o for o in ws if not ((br["then_s"] or 0) <= o["line"] <= (br["then_e"] or 0))]
            inside = [o for o in ws if (br["then_s"] or 0) <= o["line"] <= (br["then_e"] or 0)]
            if outside:
                o = outside[0]
                init = _code(g.src.line(o["file"], o["line"]))
                val = init.split("=", 1)[-1].strip() if "=" in init else init
                cl.append(cx.claim(f"{cx.D(fn)} sets `{ret_expr}` to `{val}` at {cx.loc(o['file'], o['line'])}.", "writes",
                                   [fn, local, o["file"], o["line"]], [cx.ev("write", o["file"], o["line"], local)]))
                ro = rets[-1]
                cl.append(cx.claim(f"{cx.D(fn)} returns `{ret_expr}` at {cx.loc(ro['file'], ro['line'])}.", "defined",
                                   [fn, g.sym[fn]["file"], g.sym[fn]["line"]], [cx.ev("def", ro["file"], ro["line"], fn)]))
                text += (f", so `{ret_expr}` keeps the value it was given at line {o['line']} (`{val}`)" +
                         (f" — it is only changed inside the skipped block (line {inside[0]['line']})" if inside else "") +
                         f", and {cx.D(fn)} returns `{val}` (line {ro['line']})")
    cx.say(text + ".", cl)
    # code switched off by the configuration inside this function (#if MACRO == 1 with MACRO = 0)
    mv = g.macro_values()
    for n, t in g.src.snippet(file, a, b):
        m = re.match(r"\s*#\s*if\s*\(?\s*([A-Za-z_]\w*)\s*==\s*1\s*\)?", t) or re.match(r"\s*#\s*if\s+([A-Za-z_]\w*)\s*$", t)
        if m and mv.get(m.group(1)) == 0:
            body = [_code(x) for _, x in g.src.snippet(file, n + 1, min(n + 8, b))]
            calls = [c for c in re.findall(r"([A-Za-z_]\w*)\s*\(", " ".join(body)) if c not in ("if", "while", "for", "return")]
            if calls:
                cx.say(f"Only if {m.group(1)} were 1 (it is 0 in this configuration, line {n}) would it also call "
                       f"{', '.join(dict.fromkeys(calls[:3]))}().")
    return True


def _narrate_consequences(cx: Ctx, hw: list[tuple[str, dict]], h: str) -> None:
    """Later checks elsewhere that read the state h just wrote, evaluated with the values it stored."""
    g = cx.g
    env = _written_values(cx, hw)
    eff = _effects_of_writes(cx, hw, h)
    for fn2, br2, info in eff[:3]:
        a2 = _branch_actions(cx, fn2, br2)
        val, _unk = g.eval_cond(br2["cond"], env)
        later = [x for x in g.out_calls.get(fn2, []) if x.line > (br2["then_e"] or br2["line"])]
        cons = []
        if val is True:
            if a2["returns"]:
                cons.append("returns early" + (f", so {', '.join(dict.fromkeys(cx.D(x.callee) for x in later[:3]))} "
                                               f"{'is' if len(later) == 1 else 'are'} no longer called" if later else ""))
            cons += [_call_phrase(cx, x) for x in a2["calls"]]
        elif val is False:
            cons += [f"no longer calls {cx.D(x.callee)}" for x in a2["calls"]]
        else:
            if a2["returns"]:
                cons.append("may return early" + (f", skipping {', '.join(dict.fromkeys(cx.D(x.callee) for x in later[:3]))}"
                                                  if later else ""))
            cons += [_may(_call_phrase(cx, x)) for x in a2["calls"]]
        cond_ids = set(re.findall(r"[A-Za-z_]\w*(?:(?:\.|->)\w+)*", br2["cond"]))
        cond_ids |= {c.replace("->", ".") for c in cond_ids} | {c.replace("->", ".").split(".")[-1] for c in cond_ids}
        state = ", ".join(f"{k} = {v}" for k, v in env.items() if k in cond_ids and "." not in k or
                          k in cond_ids and k.replace(".", "") != k) or ", ".join(f"{k} = {v}" for k, v in list(env.items())[:2])
        c2 = cx.claim(f"{cx.D(fn2)} reads {', '.join(cx.D(r) for r in info['reads'])} in `{br2['cond']}` at "
                      f"{cx.loc(br2['file'], br2['line'])}.", "branch", [fn2, br2["file"], br2["line"]],
                      [cx.ev("branch", br2["file"], br2["line"], fn2)], INFERENCE,
                      "the consequence depends on the state reaching this check at runtime")
        if cons:
            how = f"with {state}, `{br2['cond']}` is {'true' if val else 'false'}" if val is not None else \
                f"`{br2['cond']}` depends on runtime values"
            cx.say(f"Consequence: {cx.D(fn2)} checks `{br2['cond']}` ({cx.loc(br2['file'], br2['line'])}); {how}, "
                   f"so it " + " and ".join(cons) + ".", [c2])
            cx.flow_step(f"{cx.D(fn2)}: `{br2['cond']}` → " + " and ".join(cons), br2["file"], br2["line"],
                         cx.pack.claims[c2].ev[0] if cx.pack.claims[c2].ev else None)


def _call_phrase(cx: Ctx, c: CallSite) -> str:
    """'calls f()', or 'calls f(em, …, CO_EM_CAN_TX_BUS_OFF, …)' when the arguments carry named codes."""
    if any(OUTCOME_CONST_RE.search(a) for a in c.args):
        if any(not a.strip() for a in c.args) or any(a.count("(") != a.count(")") for a in c.args):
            # expanded from a macro (CO_errorReport(…) -> CO_error(…)): quote the line as written
            return f"runs `{_code(cx.g.src.line(c.file, c.line))}`"
        return f"calls {cx.g.name(c.callee)}({', '.join(clip_arg(a) for a in c.args[:5])}{', …' if len(c.args) > 5 else ''})"
    return f"calls {cx.D(c.callee)}"


def _may(phrase: str) -> str:
    """'calls f()' -> 'may call f()'; 'runs `X(…)`' -> 'may run `X(…)`'."""
    for verb, may in (("calls ", "may call "), ("runs ", "may run ")):
        if phrase.startswith(verb):
            return may + phrase[len(verb):]
    return "may " + phrase


def clip_arg(a: str) -> str:
    a = " ".join(a.split())
    return a if len(a) <= 40 else a[:37] + "…"


def _catch_type(cond: str) -> str:
    m = re.match(r"^catch\s*\(\s*([\w.<>]+)", cond or "")
    return (m.group(1) if m else "Exception").split(".")[-1]


def _catches(cx: Ctx, catch_t: str, thrown_t: str | None) -> bool:
    """Does `catch (catch_t)` handle an exception of type thrown_t? Exact type, a known base class (this project's
    inheritance facts), or Exception itself; an unknown thrown type is only caught by a catch-all."""
    if catch_t in ("Exception", "System.Exception", "object"):
        return True
    if not thrown_t:
        return False
    if catch_t == thrown_t:
        return True
    seen, todo = set(), [thrown_t]
    while todo:
        t = todo.pop()
        if t in seen:
            continue
        seen.add(t)
        for r in cx.st.q("SELECT base FROM inherit WHERE sub LIKE ?", (f"%.{t}",)):
            b = (r["base"] or "").split(".")[-1].split("(")[0]
            if b == catch_t:
                return True
            todo.append(b)
    return False


def _catch_around(cx: Ctx, fn: str, c, thrown_t: str | None = None) -> dict | None:
    """The catch block that handles an exception thrown at a call site: among the catches of the try block around
    the call, the first whose type matches (`catch (SlaveException)` does not handle an IOException)."""
    g = cx.g
    cands = []
    for b in sorted(g.branches.get(fn, []), key=lambda b: b["line"]):
        if b["kind"] != "catch" or b["line"] <= c.line or b["file"] != c.file:
            continue
        body = g.src.snippet(b["file"], max(1, c.line - 60), b["line"] - 1)
        tl = [n for n, t in body if re.search(r"\btry\b", _code(t))]
        if not (tl and tl[-1] < c.line):
            break
        # the catches of one try follow each other: stop at the first line of code that is not inside a catch
        if cands and any(_code(t).strip() not in ("", "{", "}") and not any(_encloses(x, n) or n == x["line"] for x in cands)
                         for n, t in g.src.snippet(b["file"], cands[-1]["then_e"] + 1, b["line"] - 1)):
            break
        cands.append(b)
    for b in cands:
        if _catches(cx, _catch_type(b["cond"]), thrown_t):
            return b
    return None


def _eval_with(cond: str | None, names: set[str], value: str | None, mv: dict) -> bool | None:
    """Whether the caller's test is TRUE when the tested result is the failing value (`wkc > 0` with -3 → False;
    `(wkc > 0) && (rdat > 0)` with wkc = 0 → False). None when it cannot be told."""
    if not value or not cond:
        return None
    v = value.strip().strip("()").strip()
    v = {"FALSE": "0", "false": "0", "NULL": "0", "nullptr": "0", "pdFALSE": "0", "TRUE": "1", "true": "1",
         "pdTRUE": "1"}.get(v, v)
    if v in mv:
        v = str(mv[v])
    v = v.strip("()").strip()
    if not re.fullmatch(r"-?\s*\d+[uUlL]*", v):
        return None
    num = int(re.sub(r"[uUlL\s]", "", v))
    if "||" in cond:
        return None
    seen_any = False
    for part in re.split(r"&&", cond):
        p = part.strip().strip("()").strip()
        for n in names:
            if not n:
                continue
            call = rf"{re.escape(n)}\s*(\((?:[^()]|\([^()]*\))*\))?"
            m = re.fullmatch(rf"\(?\s*{call}\s*\)?\s*(==|!=|<=|>=|<|>)\s*\(?\s*(-?\s*\w+)\s*\)?", p)
            if m:
                rhs = m.group(3).replace(" ", "")
                rhs = str(mv.get(rhs, rhs)).rstrip("uUlL")
                if not re.fullmatch(r"-?\d+", rhs):
                    return None
                r = int(rhs)
                res = {"==": num == r, "!=": num != r, "<=": num <= r, ">=": num >= r, "<": num < r, ">": num > r}[m.group(2)]
            elif re.fullmatch(rf"\(?\s*{call}\s*\)?", p):
                res = num != 0
            elif re.fullmatch(rf"!\s*\(?\s*{call}\s*\)?", p):
                res = num == 0
            else:
                continue
            seen_any = True
            if not res:
                return False                          # one false conjunct makes the whole test false
            break
    return True if seen_any and "&&" not in cond else None


def _test_polarity(cond: str | None, names: set[str]) -> int:
    """+1 when the test is TRUE for a truthy result (`f(…)`, `ok`, `f(…) != 0`, `f(…) == TRUE`), -1 when it is TRUE
    for a falsy one (`!f(…)`, `!ok`, `f(…) == 0`, `== FALSE`), 0 when it is something else (`f(…) < 0`)."""
    c = re.sub(r"\s+", " ", (cond or "").strip())
    for n in names:
        if not n:
            continue
        call = rf"{re.escape(n)}\s*(\((?:[^()]|\([^()]*\))*\))?"
        if re.fullmatch(rf"\(?\s*{call}\s*\)?(\s*(!=\s*(0|NULL|FALSE|false)|==\s*(TRUE|true|1)))?", c):
            return 1
        if re.fullmatch(rf"!\s*\(?\s*{call}\s*\)?|\(?\s*{call}\s*\)?\s*==\s*(0|NULL|FALSE|false|nullptr)", c):
            return -1
    return 0


def _fails_falsy(g: CodeGraph, fn: str) -> bool | None:
    """True when fn reports failure with a falsy value (a BOOL/bool function, `return FALSE;`), False when it reports it
    with a non-zero code (`return -1;`, `return LFS_ERR_INVAL;`), None when that cannot be told."""
    sy = g.sym.get(fn) or {}
    rtype = (sy.get("type") or "").split("(", 1)[0].strip()
    if re.search(r"\b(bool|BOOL|_Bool|boolean|bool_t|BaseType_t)\b", rtype) and "*" not in rtype:
        return True
    if "*" in rtype:
        return True                                   # a pointer: NULL is the failure
    vals = [_code(t).split("return", 1)[1].strip().rstrip(";").strip("() ")
            for _, t in g.src.snippet(*cx_body(g, fn)) if re.search(r"\breturn\b", _code(t))] if cx_body(g, fn)[0] else []
    if not vals:
        return None
    if all(v in ("true", "false", "TRUE", "FALSE", "0", "1", "pdTRUE", "pdFALSE") for v in vals) and \
            {"0", "false", "FALSE", "pdFALSE"} & set(vals):
        return True
    if any(re.fullmatch(r"-\d+|[A-Z_]*(ERR|FAIL|INVALID|E[A-Z]{2,})[A-Z_0-9]*", v) and not SUCCESS_CONST_RE.search(v)
           for v in vals):
        return False
    return None


def cx_body(g: CodeGraph, fn: str) -> tuple:
    """(file, first line, last line) of fn's definition, or (None, 0, 0)."""
    sy = g.sym.get(fn) or {}
    if not sy.get("file") or not sy.get("end_line"):
        return (None, 0, 0)
    return (sy["file"], sy.get("line") or 0, sy["end_line"])


def _success_test(cond: str | None) -> bool:
    """`err == OS_ERR_NONE`, `rc == SUCCESS`, `status == pdPASS`: the check is TRUE on success, so a failure takes the
    other side."""
    for m in re.finditer(r"([\w.\->]+)\s*==\s*\(?\s*([\w.\->]+)|([\w.\->]+)\s*==\s*\(?\s*([\w.\->]+)", cond or ""):
        for tok in (m.group(2), m.group(1)):
            if tok and SUCCESS_CONST_RE.search(tok.split(".")[-1].split("->")[-1]):
                return True
    return False


def _local_result(cx: Ctx, fn: str, act: dict, br: dict | None = None) -> str | None:
    """The failing result when the side stores it in the local the function returns at its end (`wkc = 0;` …
    `return wkc;`): the value it stores, if that is a literal or a named constant. None otherwise."""
    g = cx.g
    f, a, b = cx.body_lines(fn)
    if not f:
        return None
    last = [t for _, t in g.src.snippet(f, a, b) if re.search(r"\breturn\b", _code(t))]
    m = re.search(r"\breturn\s+\(?\s*([A-Za-z_]\w*)\s*\)?\s*;?\s*$", _code(last[-1])) if last else None
    if not m:
        return None
    lit = r"-?\s*\d+[uUlL]*|[A-Z_][A-Z0-9_]*|false|true|NULL"
    asg = rf"(?<![\w.>])\b{re.escape(m.group(1))}\s*=(?!=)\s*([^;]+?)\s*;?\s*$"
    for _, t in reversed(act.get("lines") or []):
        w = re.search(asg, _code(t))
        if w:
            rhs = w.group(1).strip()
            return rhs if re.fullmatch(lit, rhs) else None
    if br is None or not act.get("range"):
        return None
    # the side does not set it: the code that still runs after it on this path (`if (abort) {…} wkc = 0;` inside
    # the enclosing block), skipping the sides of other decisions, up to a return
    ml = br["line"]
    others = [(x.get("then_s"), x.get("then_e")) for x in g.branches.get(fn, []) if x.get("then_s")] + \
        [(x.get("else_s"), x.get("else_e")) for x in g.branches.get(fn, []) if x.get("else_s")]
    others = [(a_, z_ or a_) for a_, z_ in others if not (a_ <= ml <= (z_ or a_))]
    for n, t in g.src.snippet(f, (act["range"][1] or ml) + 1, b):
        if any(a_ <= n <= z_ for a_, z_ in others):
            continue
        c = _code(t)
        if re.search(r"\breturn\b", c):
            break
        w = re.search(asg, c)
        if w:
            rhs = w.group(1).strip()
            return rhs if re.fullmatch(lit, rhs) else None
    return None


def _propagate_failure(cx: Ctx, fn: str, depth: int = 2, seen: set | None = None, throws: bool = False,
                       thrown_type: str | None = None, fail_value: str | None = None) -> None:
    """Callers that test fn's result: `if (!minmea_check(...)) return MINMEA_INVALID;` -> the caller's reaction."""
    g = cx.g
    seen = seen or {fn}
    name = g.name(fn)
    # the callers the question names come first ("does not answer a PING": Ping() is the caller that reacts)
    qw = expand_words(set(cx.r.topic))
    cands = [c for c in g.callers(fn) if cx.about_tests or not g.is_test(c.caller)]
    cands.sort(key=lambda c: -len(set(split_ident(g.name(c.caller).split(".")[-1])) & qw))
    for c in cands[:6]:
        caller = c.caller
        if caller in seen:
            continue
        # C#: an exception thrown by fn lands in the catch block around the call, if there is one
        cb = _catch_around(cx, caller, c, thrown_type) if throws else None
        if cb is not None:
            act = _branch_actions(cx, caller, cb)
            tv, rv = _thrown_value(act), _returned_value(act)
            if act.get("nested"):
                # the handler decides (`if (attempt++ > _retries) throw;`): its steps, not a flat list
                steps = describe_steps(cx, caller, [it for it in fn_steps(cx, caller, max_steps=60)
                                                    if cb["then_s"] <= it["line"] <= (cb["then_e"] or 0)])
                what = ["; then it ".join(steps[:5])] if steps else []
            else:
                what = [_call_phrase(cx, x) for x in act["calls"] if not DIAG_NAME_RE.search(g.name(x.callee))][:3]
                what += [f"throws `{tv}`"] if tv else [f"returns `{rv}`"] if rv else []
            if what:
                cl = cx.claim(f"{cx.D(caller)} catches exceptions from {cx.D(fn)} at {cx.loc(cb['file'], cb['line'])}.",
                              "branch", [caller, cb["file"], cb["line"]], [cx.ev("branch", cb["file"], cb["line"], caller)])
                cx.say(f"{cx.D(fn)} is called by {cx.D(caller)} inside a try block ({cx.loc(c.file, c.line)}); its "
                       f"`{cb['cond']}` handler ({cx.loc(cb['file'], cb['line'])}) " + " and ".join(dict.fromkeys(what)) + ".",
                       [cl])
                cx.flow_step(f"{cx.D(caller)} catches it", cb["file"], cb["line"], cx.pack.claims[cl].ev[0])
                seen.add(caller)
                if depth > 1 and tv:
                    nt = re.match(r"new\s+([\w.]+)", tv)
                    _propagate_failure(cx, caller, depth - 1, seen, throws=True,
                                       thrown_type=nt.group(1).split(".")[-1] if nt else thrown_type)
                continue
        # the result tested where it is called, or stored in a variable tested just after (err = f(); if (err) …)
        stored = {e.dst for e in g.fwd.get(f"{fn}#ret", []) if e.fn == caller and e.line == c.line}
        # through a function pointer the result first reaches the pointer's return value, then the variable
        stored |= {e2.dst for d in list(stored) if d.endswith("#ret") for e2 in g.fwd.get(d, [])
                   if e2.fn == caller and e2.line == c.line}
        for br in sorted(g.branches.get(caller, []), key=lambda b: b["line"]):
            direct = br["line"] == c.line and name in (br["cond"] or "")
            later = stored and c.line < br["line"] <= c.line + 6 and br["kind"] == "if" and set(br["reads"]) & stored
            retry = stored and br["kind"] in ("do", "while", "for") and set(br["reads"]) & stored and \
                br["line"] <= c.line <= (br.get("end_line") or br["line"])
            if retry:
                cl = cx.claim(f"{cx.D(caller)} loops while `{br['cond']}` ({cx.loc(br['file'], br['line'])}).", "branch",
                              [caller, br["file"], br["line"]], [cx.ev("branch", br["file"], br["line"], caller)])
                cx.say(f"{cx.D(fn)} is called by {cx.D(caller)} inside a loop, which keeps retrying while `{br['cond']}` "
                       f"({cx.loc(br['file'], br['line'])}).", [cl])
                seen.add(caller)
                if depth > 1:
                    _propagate_failure(cx, caller, depth - 1, seen)
                break
            if not (direct or later) or br["kind"] == "switch":
                continue                              # a switch on the result is told case by case below
            ok_test = _success_test(br["cond"])
            # which side the FAILURE takes: `if (ParseFrame(…))` with a BOOL ParseFrame fails on its false side,
            # `if (lfs_dir_fetch(…))` with an error code on its true side; `if (!f(…))` the other way round
            tested = {name} | {g.name(d).split(".")[-1] for d in stored if "#local:" in d}
            pol = _test_polarity(br["cond"], tested)
            ff = _fails_falsy(g, fn)
            fail_then = not ok_test
            if not ok_test and pol and ff is not None:
                fail_then = (pol > 0) != ff
            ev = _eval_with(br["cond"], tested, fail_value, g.macro_values())
            if ev is not None:
                fail_then = ev                        # `wkc > 0` with wkc = EC_SLAVECOUNTEXCEEDED (-3): false
            if not fail_then and not br.get("else_s"):
                # nothing runs on the failure side: what the caller skips
                act_t = _branch_actions(cx, caller, br)
                sk = list(dict.fromkeys(_stmt(g, o["file"], o["line"]) for k, o in act_t["writes"] if "#" not in k))[:2]
                sk_calls = list(dict.fromkeys(cx.D(x.callee) for x in act_t["calls"] if x.line != br["line"] and
                                              not DIAG_NAME_RE.search(g.name(x.callee))))[:3]
                parts_ = [f"`{t}`" for t in sk] + [k for k in sk_calls if k.rstrip("()") not in " ".join(sk)]
                if ok_test or not parts_:
                    continue                          # only the success side is written out here
                cl = cx.claim(f"{cx.D(caller)} tests the result of {cx.D(fn)} in `{br['cond']}` "
                              f"({cx.loc(br['file'], br['line'])}).", "branch", [caller, br["file"], br["line"]],
                              [cx.ev("branch", br["file"], br["line"], caller)])
                cx.say(f"{cx.D(fn)}'s caller {cx.D(caller)} checks `{br['cond']}` ({cx.loc(br['file'], br['line'])}); "
                       f"when {cx.D(fn)} fails that is false, so it skips " + ", ".join(parts_) + ".", [cl])
                seen.add(caller)
                break
            act = _branch_actions(cx, caller, br, then=fail_then)
            ret = [t for _, t in act["lines"] if re.search(r"\breturn\b", t)]
            what = []
            if ret:
                rv = _code(re.sub(r"^.*?\breturn\b\s*", "", ret[0]))
                what.append(f"returns `{rv}`" if rv else "returns")
            # the state it changes (`OSTCBPrioTbl[prio] = (OS_TCB *)0`: frees the priority) before the calls, and
            # critical-section / lock macros last; each call once (`OS_ENTER_CRITICAL()` expands to two)
            st_lines = list(dict.fromkeys(_stmt(g, o["file"], o["line"]) for k, o in act["writes"] if "#" not in k))[:2]
            what += [f"sets {', '.join(f'`{t}`' for t in st_lines)}"] if st_lines else []
            calls = list(dict.fromkeys(cx.D(x.callee) for x in act["calls"] if x.line != br["line"] and
                                       not _diag_line(cx, x.file, x.line) and not DIAG_NAME_RE.search(g.name(x.callee))))
            sync = [k for k in calls if SYNC_NAME_RE.search(k)]
            what += [f"calls {k}" for k in [k for k in calls if k not in sync] + sync][:3]
            if not ret:
                what += _records(cx, [(k, o) for k, o in act["writes"] if "#local:" in k or "#param:" in k])
                # `rc = FAILURE;` … `exit: if (rc == SUCCESS) … else MQTTCloseSession(c);` — where that result is acted on
                locs = {k for k, o in act["writes"] if "#local:" in k}
                for b2 in sorted(g.branches.get(caller, []), key=lambda b: b["line"]):
                    if not locs or b2["line"] <= (act["range"][1] or br["line"]) or not set(b2["reads"]) & locs:
                        continue
                    ok2 = _success_test(b2["cond"])
                    s2, e2 = (b2.get("else_s"), b2.get("else_e")) if ok2 else (b2.get("then_s"), b2.get("then_e"))
                    if not s2:
                        continue
                    lc2 = [x for x in g.out_calls.get(caller, []) if s2 <= x.line <= (e2 or s2) and
                           not _diag_line(cx, x.file, x.line) and not DIAG_NAME_RE.search(g.name(x.callee))]
                    if lc2:
                        what.append(f"later, where it tests `{b2['cond']}` (line {b2['line']}), " +
                                    ("when that is not true " if ok2 else "") + "calls " +
                                    ", ".join(dict.fromkeys(cx.D(x.callee) for x in lc2[:3])))
                        break
            if not what:
                continue
            cl = cx.claim(f"{cx.D(caller)} tests the result of {cx.D(fn)} in `{br['cond']}` "
                          f"({cx.loc(br['file'], br['line'])}).", "branch", [caller, br["file"], br["line"]],
                          [cx.ev("branch", br["file"], br["line"], caller)])
            cx.say(f"{cx.D(fn)}'s caller {cx.D(caller)} checks `{br['cond']}` ({cx.loc(br['file'], br['line'])}) and "
                   + ("when that is false it " if not fail_then else "then ") + " and ".join(what) + ".", [cl])
            cx.flow_step(f"{cx.D(caller)}: `{br['cond']}` → " + " and ".join(what), br["file"], br["line"],
                         cx.pack.claims[cl].ev[0] if cx.pack.claims[cl].ev else None)
            seen.add(caller)
            if depth > 1:
                _propagate_failure(cx, caller, depth - 1, seen,
                                   fail_value=re.sub(r"^.*?\breturn\b\s*", "", _code(ret[0])).strip().rstrip(";") if ret else None)
            break
    # callers that SWITCH on the result (case MINMEA_INVALID: ...)
    for c in g.callers(fn)[:6]:
        for br in g.branches.get(c.caller, []):
            if br["kind"] == "switch" and name in (br["cond"] or "") and c.caller not in seen:
                lines = g.src.snippet(br["file"], br["line"], br["end_line"] or br["line"])
                cases = [(n, _code(t)) for n, t in lines if re.match(r"\s*case\s+\w*(INVALID|ERR|FAIL|UNKNOWN)\w*", t, re.I)]
                if cases:
                    n, t = cases[0]
                    body = [_code(x) for _, x in g.src.snippet(br["file"], n + 1, min(n + 4, br["end_line"] or n + 4))]
                    body = [b for b in body if b and not b.startswith(("case", "}", "default"))][:2]
                    cl = cx.claim(f"{cx.D(c.caller)} switches on {cx.D(fn)}'s result ({cx.loc(br['file'], br['line'])}).",
                                  "branch", [c.caller, br["file"], br["line"]], [cx.ev("branch", br["file"], br["line"], c.caller)])
                    cx.say(f"{cx.D(c.caller)} switches on the result (`{br['cond']}`, {cx.loc(br['file'], br['line'])}); "
                           f"for `{t.rstrip(':')}` it " + ("runs " + "; ".join(f"`{b}`" for b in body) if body else "does nothing")
                           + ".", [cl])
                    seen.add(c.caller)


def _failure_modes_of(cx: Ctx, fn: str) -> None:
    """'Why might this method fail?' for a function without explicit validity words: every early exit / error return."""
    g = cx.g
    cls = []
    items = []
    for br in g.branches.get(fn, []):
        a = _branch_actions(cx, fn, br)
        if a["returns"]:
            ret = [t for _, t in a["lines"] if "return" in t]
            rv = re.sub(r".*return\s*", "", ret[0]).strip().rstrip(";") if ret else ""
            cls.append(cx.claim(f"{cx.D(fn)} exits early when `{br['cond']}` ({cx.loc(br['file'], br['line'])}).", "branch",
                                [fn, br["file"], br["line"]], [cx.ev("branch", br["file"], br["line"], fn)]))
            items.append(f"`{br['cond']}` (line {br['line']})" + (f" → returns {rv}" if rv else ""))
    for c in g.out_calls.get(fn, []):
        if c.callee.startswith("?"):
            # (an unresolved delegate or function pointer may have no recorded holder: name what the call goes through)
            via = cx.D(c.via) if c.via else (cx.g.name(c.callee[1:]) if len(c.callee) > 1 else "")
            items.append(f"the call through {via} (line {c.line}) goes to a target that cannot be known statically"
                         if via and via not in ("unknown", "(unknown)") else
                         f"an indirect call (line {c.line}) goes to a target that cannot be known statically")
    if items:
        cx.say(f"{cx.D(fn)} can stop early or fail in these cases: " + "; ".join(items) + ".", cls)
    else:
        cx.say(f"{cx.D(fn)} has no early exits or error returns of its own.")
    for c in g.out_calls.get(fn, []):
        if c.callee in g.defined and any(_branch_actions(cx, c.callee, b)["returns"] for b in g.branches.get(c.callee, [])):
            cx.pack.notes.append(f"{cx.D(c.callee)} (called at line {c.line}) has its own early exits.")


def _scenario_hint(cx: Ctx, fn: str) -> None:
    if cx.runner is None:
        return
    g = cx.g
    params = [k for k in g.sym if k.startswith(fn + "#param:")]
    def input_struct(p):                      # a struct the function READS (const pointer or by value), not an output
        t = g.sym[p].get("sig") or g.sym[p].get("type") or ""
        return bool(g.record_fields(t, g.sym[p].get("type") or "")) and ("const" in t or "*" not in t)
    buildable = cx.r.inputs or any(input_struct(p) for p in params) or not params
    if not buildable:
        cx.pack.notes.append(f"Not run: {cx.D(fn)} takes raw buffers, and a meaningful test input cannot be built "
                             f"without knowing their format.")
        return
    inputs = dict(cx.r.inputs) if cx.r.inputs else {}
    if cx.r.intent == "failure_invalid" and "valid" not in inputs:
        inputs["valid"] = False
    try:
        res = cx.runner.run_for(cx, fn, inputs, purpose=cx.r.intent)
    except Exception as e:  # noqa: BLE001
        cx.pack.notes.append(f"Could not run the code to confirm this ({type(e).__name__}: {e}).")
        return
    if res:
        _narrate_run(cx, res)


def _const_like(tok: str) -> bool:
    """`OS_STAT_PEND_TO`, `PUBREC` (C macros/enums) or `SlaveExceptionCodes.Acknowledge` (a C# enum member) — a named
    value, not a variable (`packet_type`, `se.SlaveExceptionCode`)."""
    tok = tok.strip("()& ")
    last = re.split(r"\.|->", tok)[-1]
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", last) or
                ("." in tok and "->" not in tok and re.match(r"[A-Z]\w*\.", tok) and re.fullmatch(r"[A-Z]\w*", last)))


NEG_Q_RE = re.compile(r"\b(not|no|never|isn't|doesn't|don't|didn't|fails? to|without|cannot|can't|unable)\b", re.I)


def _question_side(g: CodeGraph, fn: str, br: dict, topic: set[str], question: str) -> int:
    """+1 when the question names the constant an equality in the check tests (`readAgain = code == Acknowledge;
    if (readAgain)` and "responds with ACKNOWLEDGE": the condition is TRUE in the question's case), -1 for `!=`
    (`if (code != SlaveDeviceBusy) throw;` and "is busy": FALSE), 0 when the question does not say."""
    cond = STRING_RE.sub(" ", br.get("_pos_cond") or br["cond"])
    read_names = {g.name(r).split(".")[-1].split("#")[0] for r in br.get("reads", [])} | \
        {re.sub(r"@\d+$", "", r.rsplit(":", 1)[-1]) for r in br.get("reads", []) if "#" in r}

    def value(tok: str) -> bool:
        # a named value: a macro or enum constant of the code, or a library enum member (`SocketError.TimedOut`);
        # never a variable the test reads (`DLC == 1U`: DLC is a local, all capitals or not)
        tok = tok.strip("()& ")
        last = re.split(r"\.|->", tok)[-1]
        if not last or last in read_names:
            return False
        syms = [g.sym.get(k) or {} for k in g.by_name.get(last, [])]
        kinds = {sy.get("kind") for sy in syms}
        if kinds & {"macro", "enum_const"} or any(
                sy.get("kind") in ("field", "global") and re.search(r"\b(const|readonly)\b", sy.get("sig") or "")
                for sy in syms):                      # C#: `const byte Acknowledge = 5` in a static class
            return True
        return not kinds - {None} and _const_like(tok)

    def named(text: str) -> int:
        for m in re.finditer(r"([\w.\->\[\]()]+)\s*(==|!=)\s*([\w.\->\[\]]+)", text):
            for const, other in ((m.group(3), m.group(1)), (m.group(1), m.group(3))):
                if not value(const) or value(other):
                    continue                  # `packet_type == PUBREC`: packet_type is the variable, not the value
                if re.fullmatch(r"\(?\s*-?(0[xX][0-9a-fA-F]+|\d+)[uUlL]*\s*\)?", other.strip()) or \
                        re.search(r"&\s*\(?\s*" + re.escape(const.strip("()& ")), text):
                    continue                  # `(err & CO_CAN_ERRTX_BUS_OFF) != 0U` is a bit test, not "≠ BUS_OFF"
                # the value's specific part is its last word (`SlaveDeviceBusy` → busy, `OS_ERR_Q_FULL` → full); generic
                # parts (`OS_PRIO_MUTEX_CEIL_DIS` shares "mutex"/"prio" with any mutex question) do not name it
                cw = [w for w in split_ident(re.split(r"\.|->", const)[-1]) if len(w) > 2]
                if cw and cw[-1] not in set(split_ident(other)) and cw[-1] in expand_words(topic):
                    return 1 if m.group(2) == "==" else -1
        return 0
    side = named(cond)
    if not side:
        # a tested local computed from the equality just before (`readAgain = … == SlaveExceptionCodes.Acknowledge`)
        for r in br.get("reads", []):
            if "#local:" not in r:
                continue
            lname = g.name(r).split(".")[-1]
            m = re.match(rf"^\s*\(?\s*(!?)\s*{re.escape(lname)}\s*\)?\s*$", cond)
            ws = [o for o in g.writers(r) if o.get("fn") == fn and o["line"] <= br["line"]]
            if m and ws:
                o = max(ws, key=lambda o: o["line"])
                side = named(_code(g.src.line(o["file"], o["line"])).split("=", 1)[-1])
                if m.group(1):
                    side = -side
                break
    if side and NEG_Q_RE.search(question):
        side = -side
    return side


def _local_origin(cx: Ctx, fn: str, br: dict) -> str:
    """` (readAgain = code == SlaveExceptionCodes.Acknowledge, line 156)` when the check tests a bare local that a
    comparison or expression just before computed — what the test really asks. Empty otherwise."""
    g = cx.g
    cond = (br.get("_pos_cond") or br["cond"] or "").strip()
    m = re.fullmatch(r"!?\s*\(?\s*([A-Za-z_]\w*)\s*\)?", cond)
    if not m:
        return ""
    for r in br.get("reads", []):
        if "#local:" not in r or g.name(r).split(".")[-1] != m.group(1):
            continue
        ws = [o for o in g.writers(r) if o.get("fn") == fn and o["line"] < br["line"]]
        if not ws:
            return ""
        o = max(ws, key=lambda o: o["line"])
        t = _stmt(g, o["file"], o["line"])
        rhs = t.split("=", 1)[-1].strip() if "=" in t else ""
        if not rhs or re.fullmatch(r"(true|false|TRUE|FALSE|0|1|NULL|null)", rhs) or len(t) > 120:
            return ""                                  # `readAgain = false;` says nothing about the test
        return f" (`{t}`, line {o['line']})"
    return ""


def _origin_words(g: CodeGraph, fn: str, br: dict) -> set[str]:
    """Words of the expressions that computed the locals a condition tests (the closest assignment before it)."""
    out: set[str] = set()
    for r in br.get("reads", []):
        if "#local:" not in r:
            continue
        ws = [o for o in g.writers(r) if o.get("fn") == fn and o["line"] <= br["line"]]
        if ws:
            o = max(ws, key=lambda o: o["line"])
            rhs = _code(g.src.line(o["file"], o["line"])).split("=", 1)[-1]
            out |= set(split_ident(STRING_RE.sub(" ", rhs)))
    return out


def _name_words(name: str, words: set[str]) -> set[str]:
    """An identifier's words, plus question words written inside it without separators (slavecount -> slave, count;
    elist -> list)."""
    out = set(split_ident(name))
    low = name.lower()
    out |= {w for w in words if len(w) >= 3 and w in low and not w.isdigit()}
    return out


def _read_name(g: CodeGraph, key: str) -> str:
    """The name a condition reads: a parameter or local by its own name (not prefixed with its function), a field with
    its struct (struct.field), anything else as displayed."""
    s = g.sym.get(key)
    if s and s["kind"] in ("param", "local"):
        return s["name"]
    return g.name(key)


def _missing_topic(cx: Ctx, fn: str, words: set[str]) -> float:
    """How much a timeout check is about what the question names (its condition, the variables it reads, the function
    it is in), with abbreviations (heartbeat -> hb) and the project's word rarity."""
    tw = expand_words(cx.r.topic) - WEAK_TOPIC - {"timeout", "times", "time", "out"}
    have = words | _name_words(cx.g.name(fn), tw)
    score = 3 * sum(cx.word_weight(w) for w in have & tw)
    # the code that waits through it: select() called from _modbus_receive_msg is the RESPONSE wait — a weak tie-break
    up = {w for c in cx.g.callers(fn)[:8] for w in _name_words(cx.g.name(c.caller), tw)} - have
    return score + 0.5 * sum(cx.word_weight(w) for w in up & tw)


def _narrate_retry_loop(cx: Ctx, fn: str, br: dict, act: dict) -> None:
    """A loop that keeps trying until an answer comes or the time runs out: what it repeats, what ends it, and what the
    function returns when the time ran out (the variable's value from before the loop)."""
    g = cx.g
    c0 = cx.claim(f"{cx.D(fn)} loops while `{br['cond']}` ({cx.loc(br['file'], br['line'])}).", "branch",
                  [fn, br["file"], br["line"]], [cx.ev("branch", br["file"], br["line"], fn)])
    body = [c for c in act["calls"] if not DIAG_NAME_RE.search(g.name(c.callee))][:4]
    cl = [c0] + [cx.claim(f"{cx.D(fn)} calls {cx.D(c.callee)} at {cx.loc(c.file, c.line)}.", "calls",
                          [fn, c.callee, c.file, c.line], [cx.ev("call", c.file, c.line, c.callee)]) for c in body]
    end = br.get("end_line") or br["line"]
    rets = sorted((o for o in g.occ.get(f"{fn}#ret", []) if o["line"] >= end), key=lambda o: o["line"])
    tail = ""
    if rets:
        rv = _code(g.src.line(rets[0]["file"], rets[0]["line"]))[6:].strip()
        tail = f"; when the time runs out the loop ends and {cx.D(fn)} returns `{rv}` (line {rets[0]['line']})"
        loc = next((k for k in g.occ_keys_of(fn) if k.startswith(f"{fn}#local:{rv}@")), None) if re.fullmatch(r"\w+", rv) else None
        if loc:
            w0 = sorted((o for o in g.writers(loc) if o["line"] < br["line"]), key=lambda o: o["line"])
            if w0:
                tail += f", which still holds its starting value (`{_stmt(g, w0[0]['file'], w0[0]['line'])}`, line " \
                        f"{w0[0]['line']}) if no answer ever came"
    cx.say(f"This is handled in {cx.D(fn)} ({cx.loc(br['file'], br['line'])}): it keeps " +
           (", ".join(f"calling {cx.D(c.callee)}" for c in body) or "trying") +
           f" while `{br['cond']}`{tail}.", cl)
    cx.flow_step(f"{cx.D(fn)} retries while `{br['cond']}`", br["file"], br["line"], cx.pack.claims[c0].ev[0])
    _propagate_failure(cx, fn, depth=2)


def _narrate_timeout_case(cx: Ctx, fn: str, br: dict, act: dict) -> None:
    """A wait whose timeout is one case of a status switch: where the status is set to 'timed out', and what the
    waiting function does in that case."""
    g = cx.g
    subj = br["cond"].rsplit(" is ", 1)[0]
    labels = br["_case"]
    c0 = cx.claim(f"{cx.D(fn)} switches on `{subj}` at {cx.loc(br['file'], br['line'])}.", "branch",
                  [fn, br["file"], br["line"]], [cx.ev("branch", br["file"], br["line"], fn)])
    cls = [c0]
    acts = list(dict.fromkeys([_call_phrase(cx, c) for c in act["calls"] if not _diag_line(cx, c.file, c.line)] +
                              _records(cx, act["writes"])))      # one call is one step, in every build variant
    for c in act["calls"]:
        st, why = cx.call_status(c)
        cls.append(cx.claim(f"In the case {' / '.join(labels)}, {cx.D(fn)} calls {cx.D(c.callee)} ({cx.loc(c.file, c.line)}).",
                            "calls", [fn, c.callee, c.file, c.line], [cx.ev("call", c.file, c.line, c.callee)], st, why))
    for sym, o in act["writes"]:
        cls.append(cx.claim(f"In the case {' / '.join(labels)}, {cx.D(fn)} writes {cx.D(sym)} ({cx.loc(o['file'], o['line'])}).",
                            "writes", [fn, sym, o["file"], o["line"]], [cx.ev("write", o["file"], o["line"], sym)]))
    setters = _case_setters(cx, br, labels)
    if setters:
        scl, parts = [], []
        for r, o, t, guards in setters:
            scl.append(cx.claim(f"{cx.D(o['fn'])} writes {cx.D(r)} at {cx.loc(o['file'], o['line'])}.", "writes",
                                [o["fn"], r, o["file"], o["line"]], [cx.ev("write", o["file"], o["line"], r)]))
            parts.append(f"{cx.D(o['fn'])} sets `{t}` ({cx.loc(o['file'], o['line'])})" +
                         (" when " + " and ".join(f"`{x['cond']}`" for x in guards) if guards else ""))
            cx.flow_step(f"{cx.D(o['fn'])} sets `{t}`", o["file"], o["line"], cx.pack.claims[scl[-1]].ev[0])
        cx.say("The timeout is detected where the status is set: " + "; ".join(parts) + ".", scl)
    cx.say(f"The waiting code then handles it in {cx.D(fn)} ({cx.loc(br['file'], br['line'])}): it switches on `{subj}`, "
           f"and in the case {' / '.join(f'`{x}`' for x in labels)} (line {br['then_s']}) it " +
           (" and ".join(acts) if acts else f"runs lines {br['then_s']}–{br['then_e']}") + ".", cls)
    cx.flow_step(f"{cx.D(fn)} switches on `{subj}` (case {' / '.join(labels)})", br["file"], br["line"], cx.pack.claims[c0].ev[0])
    _propagate_failure(cx, fn, depth=1)


def _readable_run_output(out: str) -> str:
    """The sandbox's output as a person reads it: hardware calls and recorded values, the program's own prints;
    not the harness's internal markers (function-entry addresses, phase marks)."""
    lines = []
    phase = ""
    for ln in out.splitlines():
        if ln.startswith(("__VR_ENTER ", "__VR_BASE ", "__VR_CALL_TARGET")):
            continue
        if ln.startswith("__VR_PHASE "):
            phase = ln.split(" ", 1)[1].strip()
            continue
        if ln.startswith("__VR_HW "):
            lines.append("hardware call: " + ln[8:].strip())
        elif ln.startswith("__VR_STATE "):
            k, _, v = ln[11:].partition("=")
            lines.append(f"{k.strip()} = {v.strip()}" + (f"  ({phase})" if phase else ""))
        elif ln.startswith("__VR_"):
            continue
        else:
            lines.append(ln)
    return "\n".join(lines)


TIMEOUT_OUT_RE = re.compile(r"((?<![=!<>])=(?!=)|\breturn)\s*\(?\s*[\w:]*\b(E|WSAE)?[A-Z_]*TIME_?D?_?OUT[A-Z_]*\b|"
                            r"((?<![=!<>])=(?!=)|\breturn)\s*\(?\s*[a-z]{2,}TIMEOUT\b")


def _case_setters(cx: Ctx, br: dict, labels: list[str]) -> list[tuple[str, dict, str, list]]:
    """Who puts the switch's subject into this case: writes of what the switch reads whose statement names the case's
    constant (`ptcb->OSTCBStatPend = OS_STAT_PEND_TO` in OSTimeTick), with the conditions guarding each write."""
    g = cx.g
    consts = [lb for lb in labels if re.fullmatch(r"[A-Za-z_]\w*", lb) and lb != "default"]
    out = []
    for r in br.get("reads") or []:
        if "#" in r:
            continue
        for o in g.writers(r):
            if not o.get("fn") or (not cx.about_tests and g.is_test(o["fn"])):
                continue
            t = _stmt(g, o["file"], o["line"])
            if (o["file"], o["line"]) in {(x[1]["file"], x[1]["line"]) for x in out}:
                continue
            if any(re.search(rf"=\s*\(?\s*{re.escape(c)}\b", t) for c in consts):
                guards = [x for x in (o.get("guards") or []) if x.get("kind") == "if" and x.get("polarity")]
                # `if (dly != 0) { dly--; if (dly == 0) …` — the outer test is about the value before the change
                norm = [re.sub(r"\s*(!=|==)\s*", "==", x["cond"]) for x in guards]
                guards = [x for i, x in enumerate(guards) if norm[i] not in norm[i + 1:]]
                out.append((r, o, t, guards[-2:]))
    return out[:3]


def r_failure_missing(cx: Ctx) -> None:
    g = cx.g
    found = []
    for fn, brs in g.branches.items():
        for br in brs:
            if not br["cond"] or (not cx.about_tests and g.is_test(fn)):
                continue
            words = set(split_ident(br["cond"]))
            for r in br["reads"]:
                words |= set(split_ident(_read_name(g, r)))
            if br["kind"] in ("while", "do", "for"):
                # retry until time runs out: `do { send; wait } while (!reply && !timer_expired)`
                if words & {"expired", "expire", "expires", "timeout", "elapsed", "deadline"}:
                    act = _branch_actions(cx, fn, br)
                    found.append((4 + _missing_topic(cx, fn, words) + min(3, len(act["calls"])), fn, br, act))
                continue
            if br["kind"] == "switch" and not _macro_internal(cx, br) and (cx.about_tests or not g.is_test(fn)):
                # `switch (status) { … case PEND_TO: *perr = OS_ERR_TIMEOUT; break; }`: the case whose outcome is a timeout
                for cs in _case_sides(cx, fn, br):
                    head = " ".join(t for _, t in g.src.snippet(br["file"], cs["then_s"], min(cs["then_e"], cs["then_s"] + 6)))
                    if TIMEOUT_OUT_RE.search(STRING_RE.sub(" ", head)):
                        act = _branch_actions(cx, fn, cs)
                        found.append((4 + _missing_topic(cx, fn, words) + min(3, len(act["calls"]) + len(act["writes"])),
                                      fn, cs, act))
                continue
            if br["kind"] not in ("if", "ternary") or _macro_internal(cx, br):
                continue
            if words & TIME_WORDS and (words & {"timeout", "last", "age", "stale", "elapsed", "lost", "deadline", "watchdog",
                                                "expired", "expire", "expires"}):
                act = _branch_actions(cx, fn, br)
                # `tv.tv_sec = ctx->byte_timeout.tv_sec` before select(): setting up the wait, not detecting that it ran out
                setup = bool(act["writes"]) and not act["calls"] and all(
                    re.search(r"timeout|tmo|period|interval|delay", _stmt(g, o["file"], o["line"]).split("=", 1)[-1], re.I)
                    for _, o in act["writes"])
                if act["calls"] or act["writes"] or act["returns"]:      # a check that does nothing reportable is no answer
                    found.append((3 + _missing_topic(cx, fn, words) + min(3, len(act["calls"]) + len(act["writes"])) -
                                  (4 if setup else 0), fn, br, act))
                continue
            # a check whose OUTCOME is a timeout (`errno = ETIMEDOUT`, `return pdTIMEOUT`, `WAIT_TIMEOUT`), whatever
            # its condition looks like (`if (s_rc == 0)` after select())
            if not cx.about_tests and g.is_test(fn):
                continue
            head = " ".join(t for n, t in g.src.snippet(br["file"], br["then_s"] or br["line"],
                                                      min((br["then_e"] or br["line"]), (br["then_s"] or br["line"]) + 6))
                            if n > br["line"] or not br["then_s"])
            if TIMEOUT_OUT_RE.search(STRING_RE.sub(" ", head)):
                act = _branch_actions(cx, fn, br)
                found.append((4 + _missing_topic(cx, fn, words) + min(3, len(act["calls"]) + len(act["writes"])), fn, br, act))
    if not found and cx.learn.pin_target():
        # nothing found, but a person said where the answer is: build it from the code (a miss is disclosed)
        found = cx.learn.pin([], lambda x: _branch_loc(x[1], x[2]),
                             lambda c: (lambda b: (9, *b) if b else None)(_learned_branch(cx, c)))
    if not found:
        cx.pack.unknowns.append("No timeout or 'data stopped arriving' check was found in this code.")
        cx.say("I could not find any code that detects the data stopping (no timeout or last-update check). If the data "
               "stops, the last received values would simply stay in place.")
        tg = cx.targets(FN_KINDS)
        if tg:
            txt, cl = how_reached(cx, tg[0])
            cx.say(f"{cx.D(tg[0])} would simply stop being called. " + txt, cl)
        return
    found.sort(key=lambda x: -x[0])
    loc_m = lambda x: _branch_loc(x[1], x[2])          # noqa: E731
    found = cx.learn.rank(found, loc_m, lambda x: x[0], lambda c: (lambda b: (min((x[0] for x in found), default=1) * 0.5,
                                                                              *b) if b else None)(_learned_branch(cx, c)))
    found = _rerank(cx, found, lambda x: _render_branch(cx, (x[1], x[2], x[3])))
    found = cx.learn.pin(found, loc_m, lambda c: (lambda b: (found[0][0] if found else 9, *b) if b else None)(
        _learned_branch(cx, c)))
    cx.learn.offer(found, loc_m, lambda x: (_branch_label(cx, x[1], x[2]), _render_branch(cx, (x[1], x[2], x[3]))),
                   lambda x: x[0])
    _, fn, br, act = found[0]
    if br["kind"] in ("while", "do", "for"):
        return _narrate_retry_loop(cx, fn, br, act)
    if br.get("_case"):
        return _narrate_timeout_case(cx, fn, br, act)
    c0 = cx.claim(f"{cx.D(fn)} checks `{br['cond']}` at {cx.loc(br['file'], br['line'])}.", "branch",
                  [fn, br["file"], br["line"]], [cx.ev("branch", br["file"], br["line"], fn)])
    # constants in the condition
    consts = []
    mv = g.macro_values()
    for tok in re.findall(r"[A-Z_][A-Z0-9_]{2,}", br["cond"]):
        if tok in mv:
            ms = g.sym.get(f"c:macro@{tok}")
            consts.append((tok, mv[tok], ms))
    cx.say(f"Losing the data is detected in {cx.D(fn)} ({cx.loc(br['file'], br['line'])}), which checks "
           f"`{br['cond']}`" + ("" if not consts else " (" + ", ".join(f"{t} = {v}" for t, v, _ in consts) + ")") + ".",
           [c0] + [cx.claim(f"{t} is {v}.", "const", [t, v], [cx.ev("macro", ms["file"], ms["line"], f"c:macro@{t}")])
                   for t, v, ms in consts if ms])
    cx.flow_step(f"{cx.D(fn)} checks `{br['cond']}`", br["file"], br["line"], cx.pack.claims[c0].ev[0])
    # default-configuration actions and configuration variants
    default_writes = [(s, o) for s, o in act["writes"] if not (o.get("cfg") or "default").startswith("+")]
    cls = []
    acts = []
    for sym, o in default_writes:
        txt = g.src.line(o["file"], o["line"]).strip().rstrip(";")
        cls.append(cx.claim(f"{cx.D(fn)} sets `{txt}` at {cx.loc(o['file'], o['line'])}.", "writes",
                            [fn, sym, o["file"], o["line"]], [cx.ev("write", o["file"], o["line"], sym)],
                            CONFIRMED, cx.cfg_note(o.get("cfg"))))
        acts.append(f"sets `{txt}`")
    for c in act["calls"]:
        st, why = cx.call_status(c)
        note = cx.cfg_note(c.cfg)
        cls.append(cx.claim(f"{cx.D(fn)} calls {cx.D(c.callee)} at {cx.loc(c.file, c.line)}.", "calls",
                            [fn, c.callee, c.file, c.line], [cx.ev("call", c.file, c.line, c.callee)], st,
                            "; ".join(x for x in (why, note) if x)))
        sub = describe_steps(cx, c.callee, fn_steps(cx, c.callee)) if c.callee in g.defined else []
        acts.append(f"calls {cx.D(c.callee)}" + (f" (which {', '.join(sub[:3])})" if sub else ""))
        cx.flow_step(f"{cx.D(c.callee)}", c.file, c.line, cls[-1] and cx.pack.claims[cls[-1]].ev[0])
    if act.get("returns_top"):                    # a return inside a further check is said below, with its condition
        later = [x for x in g.out_calls.get(fn, []) if x.line > (br["then_e"] or br["line"])]
        acts.append("returns, so " + ", ".join(dict.fromkeys(cx.D(x.callee) for x in later[:3])) + " no longer run" if later
                    else "returns")
    cx.say("When that happens it " + ("; ".join(acts) if acts else f"runs lines {act['range'][0]}–{act['range'][1]}") + ".",
           cls)
    # later early exits in the same function (e.g. "only steers while tracking")
    later_exits = []
    lec = []
    for b2 in sorted(g.branches.get(fn, []), key=lambda b: b["line"]):
        if b2["line"] <= br["line"] or b2["kind"] != "if":
            continue
        a2 = _branch_actions(cx, fn, b2)
        if a2["returns"]:
            later = [x for x in g.out_calls.get(fn, []) if x.line > (b2["then_e"] or b2["line"])]
            lec.append(cx.claim(f"{cx.D(fn)} also stops early when `{b2['cond']}` ({cx.loc(b2['file'], b2['line'])}).",
                                "branch", [fn, b2["file"], b2["line"]], [cx.ev("branch", b2["file"], b2["line"], fn)]))
            later_exits.append(f"`{b2['cond']}` (line {b2['line']})" +
                               (f", which skips {', '.join(dict.fromkeys(cx.D(x.callee) for x in later[:3]))}" if later else ""))
    # conditions that must hold for the check to trigger
    guard_words = re.split(r"\s*&&\s*", br["cond"])
    if len(guard_words) > 1:
        cx.say(f"Note that this only triggers when all of these hold: " + "; ".join(f"`{w.strip()}`" for w in guard_words) +
               f". For example, when `{guard_words[0].strip()}` is false this path is not taken.")
    if later_exits:
        cx.say(f"{cx.D(fn)} also stops early when " + "; or when ".join(later_exits) +
               " — so without fresh valid data it does not reach those calls either.", lec)
    # what the callers do with the reported timeout
    _propagate_failure(cx, fn, depth=2)
    # configuration variants (free macros)
    var_writes = [(s, o) for s, o in act["writes"] if (o.get("cfg") or "").startswith("+")]
    for c in g.out_calls.get(fn, []):
        pass
    macros = sorted({r["cfg"][1:] for r in cx.st.q("SELECT DISTINCT cfg FROM occ WHERE fn=? AND cfg LIKE '+%'", (fn,))}
                    - {"ALL_FEATURES"})                 # the all-features build is not a macro of this code
    cfg = cx.cfg
    free = (cfg.get("free_macros") or {})
    if macros or var_writes:
        for m in macros:
            vo = [o for o in _variant_occ(cx, fn, m) if o["role"] in ("write", "rw")]
            if vo:
                txts = sorted({g.src.line(o["file"], o["line"]).strip().rstrip(";") for o in vo})
                cls2 = [cx.claim(f"With {m} defined, {cx.D(fn)} sets `{t}`.", "writes_cfg", [fn, m], [cx.ev("write", vo[0]["file"],
                                 vo[0]["line"], vo[0]["sym"], status=CONFIRMED)], RUNTIME, f"depends on whether {m} is defined when building")
                        for t in txts[:1]]
                cx.say(f"This behaviour depends on the build configuration: {m} is not defined anywhere in the code, so "
                       f"by default the path above is taken. If {m} is defined when building, it instead sets "
                       + ", ".join(f"`{t}`" for t in txts) + " and keeps going.", cls2)
                cx.pack.assumptions.append(f"Default build configuration: {m} is not defined (it is only tested in "
                                           f"#ifdef, never defined in this code).")
    # run it: feed one good input to the function that records the data, then let time pass
    cands = []
    for r in br["reads"]:
        for o in g.writers(r):
            f2 = o["fn"]
            if not f2 or f2 == fn or f2 not in g.defined or re.search(r"init|reset|setup|clear", g.name(f2), re.I):
                continue
            takes_struct = any("*" in (g.sym[k].get("type") or "") for k in g.sym if k.startswith(f2 + "#param:"))
            cands.append((0 if takes_struct else 1, 0 if any(c.res in ("flow", "table") for c in g.callers(f2)) else 1, f2))
    cands.sort()
    _scenario_hint(cx, cands[0][2] if cands else fn)


def _variant_occ(cx: Ctx, fn: str, macro: str) -> list[dict]:
    rows = cx.st.q("SELECT * FROM occ WHERE fn=? AND cfg=?", (fn, f"+{macro}"))
    return [dict(r) for r in rows]


# ------------------------------------------------------------------------------------------------- downstream / callers
def r_downstream(cx: Ctx) -> None:
    g = cx.g
    tg = cx.targets(FN_KINDS)
    if not tg:
        return r_search(cx)
    fn = tg[0]
    calls = g.callees(fn)
    cls = []
    items = []
    seen_calls: set[tuple] = set()
    for c in calls:
        st, why = cx.call_status(c)
        cond = ""
        if c.guards:
            gd = c.guards[-1]
            cond = f", only if `{gd['cond']}` is {'true' if gd['polarity'] else 'false'}"
        if (c.callee, c.line, cond) in seen_calls:
            continue                  # one macro expanding to the same call several times is one call in the code
        seen_calls.add((c.callee, c.line, cond))
        cls.append(cx.claim(f"{cx.D(fn)} calls {cx.D(c.callee)} at {cx.loc(c.file, c.line)}{cond}.", "calls",
                            [fn, c.callee, c.file, c.line], [cx.ev("call", c.file, c.line, c.callee)], st, why))
        items.append(f"{cx.D(c.callee)} (line {c.line}{cond})")
        cx.pack.path.append({"label": cx.D(c.callee), "file": c.file, "line": c.line, "ev": cx.pack.claims[cls[-1]].ev[0],
                             "status": st, "note": cond.strip(", ")})
    if items:
        cx.say(f"Inside {cx.D(fn)}, these calls happen in this order: " + "; ".join(items) + ".", cls)
    else:
        cx.say(f"{cx.D(fn)} does not call any other functions.")
    # nested calls one level down
    nested = []
    for c in calls:
        if c.callee in g.defined:
            sub = [x for x in g.callees(c.callee) if not x.callee.startswith("?")]
            if sub:
                nested.append(f"{cx.D(c.callee)} in turn calls {', '.join(dict.fromkeys(cx.D(x.callee) for x in sub))}")
    if nested:
        cx.say("; ".join(nested) + ".")
    # what runs after it returns: the caller continues, and consumers of the state it wrote
    written = sorted({s for s, occs in g.occ.items() for o in occs if o["fn"] == fn and o["role"] in ("write", "rw")
                      and "#" not in s and not _partial(o)})
    consumers = defaultdict(set)
    own = set(g.transitive_callees(fn))
    for w in written:
        for o in g.readers(w):
            if o["fn"] and o["fn"] != fn and o["fn"] not in own:
                consumers[o["fn"]].add(w)
    cc = []
    ci = []
    for ufn, syms in sorted(consumers.items(), key=lambda kv: -len(kv[1]))[:3]:
        o = next(o for w in sorted(syms) for o in g.readers(w) if o["fn"] == ufn)
        cc.append(cx.claim(f"{cx.D(ufn)} reads {cx.D(o['sym'])}, which {cx.D(fn)} writes (line {o['line']}).",
                           "reads", [ufn, o["sym"], o["file"], o["line"]], [cx.ev("read", o["file"], o["line"], o["sym"])],
                           INFERENCE, "runs later in time (ordering comes from the program's main loop / scheduling)"))
        callees2 = [x for x in g.callees(ufn) if not x.callee.startswith("?")]
        calls_txt = f"; it calls {', '.join(dict.fromkeys(cx.D(x.callee) for x in callees2[:5]))}" if callees2 else ""
        ci.append(f"{cx.D(ufn)} (uses {', '.join(sorted(cx.D(s) for s in syms)[:4])}{calls_txt})")
    if ci:
        cx.say(f"After {cx.D(fn)} returns, the values it stored are picked up later by " + "; ".join(ci) + ".", cc)
    callers = g.callers(fn)
    if callers:
        c = callers[0]
        after = [x for x in g.callees(c.caller) if x.line > c.line]
        if after:
            cx.say(f"Back in its caller {cx.D(c.caller)}, execution continues with " +
                   ", ".join(dict.fromkeys(cx.D(x.callee) for x in after[:4])) + ".")


def r_callers(cx: Ctx) -> None:
    g = cx.g
    # "who calls ValidateResponse?": the name written as code, and a function — not the verb "calls", which may happen to
    # be some local variable's name
    explicit = [c for m in cx.r.mentions if m.get("explicit") for c in m["candidates"] if c in g.sym]
    tg = [c for c in explicit if g.sym[c]["kind"] in FN_KINDS] or explicit or cx.targets(FN_KINDS) or cx.targets()
    if not tg:
        return r_search(cx)
    key = tg[0]
    s = g.sym[key]
    if s["kind"] in FN_KINDS:
        callers = g.callers(key)
        if not callers:
            total_unres = sum(1 for c in g.calls if c.callee.startswith("?"))
            cid = cx.pack.add("absence", s["file"], s["line"], "", key,
                              f"searched all {len(g.calls)} call sites in {len(cx.fid)} files; "
                              f"{total_unres} indirect call(s) unresolved")
            cx.claim(f"No code in this project calls {cx.D(key)}.", "no_callers", [key], [cid],
                     INFERENCE if total_unres else CONFIRMED,
                     f"{total_unres} indirect call(s) could not be resolved and might reach it" if total_unres else
                     "every call site in the project was checked")
            cx.say(f"Nothing in this project calls {cx.D(key)}" + (f" directly; {total_unres} indirect call(s) could not be "
                                                                  f"resolved, so one of those might." if total_unres else "."),
                   [len(cx.pack.claims) - 1])
            e = g.entries.get(key)
            if e:
                cx.say(f"It is an entry point: {e['detail']}.")
            return
        txt, cl = how_reached(cx, key)
        cx.say(txt, cl)
        for c in callers:
            cx.pack.path.append({"label": f"{cx.D(c.caller)} → {cx.D(key)}", "file": c.file, "line": c.line,
                                 "ev": cx.ev("call", c.file, c.line, key)})
    else:
        explain_variable(cx, key)


# ------------------------------------------------------------------------------------------------- influence
def _fn_macros(cx: Ctx, f: str) -> list[str]:
    g = cx.g
    file, a, b = cx.body_lines(f)
    out = []
    if not file:
        return out
    for mk, mocc in g.occ.items():
        if mk.startswith("c:macro@") and any(o["file"] == file and a <= o["line"] <= b for o in mocc):
            out.append(mk[8:])
    return sorted(out)


def _cond_vars(cx: Ctx, reads: list[str]) -> list[str]:
    g = cx.g
    return [r for r in reads if not r.startswith(("c:@E", "c:@Ea", "c:@EA")) and g.sym.get(r, {}).get("kind") != "enum_const"]


def r_influence(cx: Ctx) -> None:
    g = cx.g
    tg = cx.targets(VAR_KINDS) or cx.targets()
    if not tg:
        return r_search(cx)
    tg.sort(key=lambda k: (0 if g.sym[k]["kind"] == "global" else 1 if g.sym[k]["kind"] == "field" else 2))
    v = tg[0]
    s = g.sym[v]
    mv = g.macro_values()
    c0 = cx.claim(f"{cx.D(v)} is defined at {cx.loc(s['file'], s['line'])}.", "defined", [v, s["file"], s["line"]],
                  [cx.ev_def(v)])
    cx.say(f"{cx.D(v)} ({cx.loc(s['file'], s['line'])}) is affected in two ways: by the values it is computed from, and "
           f"by the conditions that decide whether it is updated at all.", [c0])
    data: list[tuple[str, str]] = []           # (display, why)
    dcl: list[int] = []
    gates: list[str] = []
    gcl: list[int] = []
    consts: set[str] = set()
    for o in g.writers(v):
        # the value written at this statement
        for e in g.bwd.get(v, []):
            if e.file != o["file"] or e.line != o["line"]:
                continue
            base = e.src.split("#", 1)[0]
            if e.src.endswith("#ret") and base in g.defined:
                f = base
                # f's inputs: its parameters bound at this call site, and the globals it reads
                for pe in [x for k2, lst in g.bwd.items() if k2.startswith(f + "#param:") for x in lst]:
                    if pe.file == e.file and (pe.site or pe.line) == (e.site or e.line) and pe.kind == "arg":
                        pname = g.sym.get(pe.dst, {}).get("name", pe.dst.rsplit(":", 1)[-1])
                        data.append((f"`{pe.expr}`", f"passed to {cx.D(f)} as {pname}"))
                        dcl.append(cx.claim(f"`{pe.expr}` is passed to {cx.D(f)} at {cx.loc(pe.file, pe.line)}.", "flow_edge",
                                            [pe.src, pe.dst, pe.file, pe.line], [cx.ev("flow", pe.file, pe.line, pe.dst)]))
                for oc in cx.g.occ_in(f):
                    if oc["role"] == "read" and "#" not in oc["sym"] and g.sym.get(oc["sym"], {}).get("kind") in ("global", "field"):
                        why = "its previous value is fed back (the calculation keeps state)" if oc["sym"] == v else f"read by {cx.D(f)}"
                        if (cx.D(oc["sym"]), why) not in data:
                            data.append((cx.D(oc["sym"]), why))
                            dcl.append(cx.claim(f"{cx.D(f)} reads {cx.D(oc['sym'])} at line {oc['line']}.", "reads",
                                                [f, oc["sym"], oc["file"], oc["line"]], [cx.ev("read", oc["file"], oc["line"], oc["sym"])]))
                consts |= set(_fn_macros(cx, f))
                # decisions inside f that change the result
                for br in g.branches.get(f, []):
                    if br["kind"] == "if":
                        cv = _cond_vars(cx, br["reads"])
                        gates.append(f"inside {cx.D(f)}: `{br['cond']}` (line {br['line']})")
                        gcl.append(cx.claim(f"{cx.D(f)} decides on `{br['cond']}` at {cx.loc(br['file'], br['line'])}.",
                                            "branch", [f, br["file"], br["line"]], [cx.ev("branch", br["file"], br["line"], f)]))
            elif "#" not in e.src or e.src.split("#", 1)[0] == o["fn"]:
                data.append((cx.D(e.src), f"assigned in {cx.D(o['fn'])}"))
        # gates: conditions guarding this write
        for gd in o.get("guards") or []:
            if gd["kind"] == "loop":
                continue
            pol = "false" if not gd["polarity"] else "true"
            gates.append(f"in {cx.D(o['fn'])}: the update only happens when `{gd['cond']}` is {pol} (line {gd['line']})")
            gcl.append(cx.claim(f"The write to {cx.D(v)} at {cx.loc(o['file'], o['line'])} is guarded by `{gd['cond']}`.",
                                "guard", [v, o["file"], gd["line"]], [cx.ev("branch", o["file"], gd["line"], o["fn"])]))
            # one level into validators: a guard on a local assigned from a call result
            for r in _cond_vars(cx, gd.get("reads") or []):
                for e2 in g.bwd.get(r, []):
                    b2 = e2.src.split("#", 1)[0]
                    if e2.src.endswith("#ret") and b2 in g.defined:
                        vars2, conds2 = [], []
                        for br in g.branches.get(b2, []):
                            conds2.append(f"`{br['cond']}`")
                            vars2 += [cx.D(x) for x in _cond_vars(cx, br["reads"]) if "#" not in x]
                        consts |= set(_fn_macros(cx, b2))
                        if conds2:
                            gates.append(f"{cx.D(r)} comes from {cx.D(b2)}, which checks " + ", ".join(conds2[:4]))
                            gcl.append(cx.claim(f"{cx.D(r)} is assigned from {cx.D(b2)} at {cx.loc(e2.file, e2.line)}.",
                                                "flow_edge", [e2.src, e2.dst, e2.file, e2.line], [cx.ev("flow", e2.file, e2.line, e2.dst)]))
                        for x in dict.fromkeys(vars2):
                            data.append((x, f"checked by {cx.D(b2)}"))
    if data:
        seen = []
        for d, why in data:
            if d not in [x for x, _ in seen]:
                seen.append((d, why))
        cx.say("Values it is computed from: " + "; ".join(f"{d} ({why})" for d, why in seen[:12]) + ".", dcl[:12])
    if gates:
        cx.say("Conditions that decide whether (and how) it changes: " + "; ".join(dict.fromkeys(gates)) + ".", gcl[:10])
    if consts:
        cx.say("Constants involved: " + ", ".join(f"{m} = {mv[m]}" if m in mv else m for m in sorted(consts)) + ".")
    src_syms = {io["sym"] for io in g.io if io["dir"] == "source"}
    up = g.flow_path(v, lambda n: n.split("#", 1)[0] in src_syms, "backward")
    if up:
        st = _stages(cx, up)
        chain = []
        for x in st:
            lab = _stage_label(x)
            if not chain or chain[-1] != lab:
                chain.append(lab)
        cx.say(f"Further upstream, these values all originate from {cx.D(up[0].src)} "
               f"({g.io_by_sym[up[0].src.split('#', 1)[0]][0]['api']}), via " + " → ".join(chain[:8]) + ".")


# ------------------------------------------------------------------------------------------------- impact
def r_impact(cx: Ctx) -> None:
    g = cx.g
    tg = cx.targets(FN_KINDS)
    if not tg:
        tg = cx.targets()
    if not tg:
        return r_search(cx)
    fn = tg[0]
    s = g.sym[fn]
    if s["kind"] not in FN_KINDS:
        return _impact_var(cx, fn)
    imp = g.impact(fn)
    c0 = cx.claim(f"{cx.D(fn)} is defined in {cx.loc(s['file'], s['line'])}.", "defined", [fn, s["file"], s["line"]],
                  [cx.ev_def(fn)])
    cx.say(f"Changing {cx.D(fn)} ({cx.loc(s['file'], s['line'])}) can affect three kinds of things: the code that calls "
           f"it, the data that depends on its result, and the outputs that data reaches.", [c0])
    direct = g.callers(fn)
    dc = []
    for c in direct:
        st, why = cx.call_status(c)
        dc.append(cx.claim(f"{cx.D(c.caller)} calls {cx.D(fn)} at {cx.loc(c.file, c.line)}.", "calls",
                           [c.caller, fn, c.file, c.line], [cx.ev("call", c.file, c.line, fn)], st, why))
    trans = sorted(imp["callers"].items(), key=lambda kv: kv[1])
    if direct:
        cx.say(f"Called directly by: {', '.join(dict.fromkeys(cx.D(c.caller) for c in direct))}. "
               + (f"Indirectly (through the call chain): {', '.join(cx.D(k) for k, d in trans if d > 1)}." if any(d > 1 for _, d in trans) else ""),
               dc)
    else:
        cx.say(f"Nothing in this code calls {cx.D(fn)} directly.")
    # data depending on the result
    res_edges = [e for e in g.fwd.get(f"{fn}#ret", [])]
    rc = []
    for e in res_edges:
        rc.append(cx.claim(f"The result of {cx.D(fn)} is stored in {cx.D(e.dst)} at {cx.loc(e.file, e.line)}.", "flow_edge",
                           [e.src, e.dst, e.file, e.line], [cx.ev("flow", e.file, e.line, e.dst)]))
    stored = [e.dst for e in res_edges]
    if stored:
        cx.say(f"Its result is stored in {', '.join(dict.fromkeys(cx.D(x) for x in stored))}"
               f" ({', '.join(cx.loc(e.file, e.line) for e in res_edges[:3])}).", rc)
    wr = [w for w in imp["writes"] if g.sym.get(w, {}).get("kind") in ("global", "field", "property")]
    if wr:
        wc = []
        for w in wr[:8]:
            o = next((o for o in g.writers(w) if o["fn"] == fn), None)
            if o:
                wc.append(cx.claim(f"{cx.D(fn)} writes {cx.D(w)} at {cx.loc(o['file'], o['line'])}.", "writes",
                                   [fn, w, o["file"], o["line"]], [cx.ev("write", o["file"], o["line"], w)]))
        readers = sorted({cx.D(o["fn"]) for w in wr for o in g.readers(w) if o["fn"] and o["fn"] != fn})
        cx.say(f"It changes this state: {', '.join(cx.D(w) for w in wr[:8])}" +
               (f", which is read by {', '.join(readers[:8])}" if readers else "") + ".", wc)
    users = imp["functions_using_result"]
    uc = []
    for u in users[:8]:
        o = next((o for k in imp["data"] for o in g.occ.get(k, []) if o["fn"] == u and o["role"] in ("read", "rw")), None)
        if o:
            uc.append(cx.claim(f"{cx.D(u)} uses data derived from {cx.D(fn)} ({cx.loc(o['file'], o['line'])}).", "reads",
                               [u, o["sym"], o["file"], o["line"]], [cx.ev("read", o["file"], o["line"], o["sym"])], INFERENCE,
                               "follows the data forward; whether a change matters depends on what the change is"))
    if users:
        cx.say(f"Functions that use values derived from it: {', '.join(dict.fromkeys(cx.D(u) for u in users[:10]))}.", uc)
    if imp["sinks"]:
        sk = imp["sinks"][0].split("#", 1)[0]
        io = g.io_by_sym[sk][0]
        cx.say(f"Through that data it can ultimately change what is sent to {cx.D(sk)} ({io['api']}) — a change in "
               f"{cx.D(fn)} can change the physical output.",
               [cx.claim(f"Data derived from {cx.D(fn)} reaches {cx.D(sk)}.", "flows_to", [f"{fn}#ret", imp["sinks"][0]],
                         [cx.ev("io", io["file"], io["line"], sk)], INFERENCE, "found by following the data forward")])
    # its own inputs: things a change must keep consistent
    reads = [r for r in imp["reads"] if "#" not in r]
    mac = []
    file, a, b = cx.body_lines(fn)
    for mk, mocc in g.occ.items():
        if mk.startswith("c:macro@") and any(o["file"] == file and a <= o["line"] <= b for o in mocc):
            mac.append(mk[8:])
    if reads or mac:
        dep = list(dict.fromkeys([cx.D(r) for r in reads if r in g.sym and g.sym[r].get("file")] + mac))
        if dep:
            cx.say(f"It depends on {', '.join(dep[:14])} — changes to those also change its behaviour.")
    for p in imp["entry_paths"][:1]:
        if p:
            steps = [cx.D(p[0].caller)] + [cx.D(c.callee) for c in p]
            cx.say("It runs on this path from the program start: " + " → ".join(steps) + ".")
            cx.pack.path = [{"label": cx.D(p[0].caller), "file": g.sym[p[0].caller]["file"], "line": g.sym[p[0].caller]["line"]}] + \
                [{"label": cx.D(c.callee), "file": c.file, "line": c.line, "ev": cx.ev("call", c.file, c.line, c.callee),
                  "status": cx.call_status(c)[0], "note": cx.call_status(c)[1]} for c in p]
    tests = [k for k in g.defined if re.search(r"test", g.sym[k]["name"], re.I) and fn in g.transitive_callees(k)]
    if tests:
        cx.say("Tests that exercise it: " + ", ".join(cx.D(t) for t in tests[:6]) + ".")
    else:
        cx.pack.notes.append(f"No tests in this project call {cx.D(fn)}, so a change would not be caught automatically.")
    for u in [fn] + users[:5]:
        if u in g.defined:
            us = g.sym[u]
            cx.pack.related.append({"key": u, "label": cx.D(u), "file": us["file"], "line": us["line"], "end_line": us["end_line"]})


def _impact_var(cx: Ctx, v: str) -> None:
    explain_variable(cx, v)


# ------------------------------------------------------------------------------------------------- scenario
def r_scenario(cx: Ctx) -> None:
    g = cx.g
    inputs = cx.r.inputs
    if not inputs:
        cx.pack.unknowns.append("No input values were found in the question.")
        return r_explain(cx)
    fn, mapping = pick_scenario_target(cx, inputs)
    if not fn:
        cx.pack.unknowns.append("Could not find a function whose inputs match the values in the question.")
        cx.say("I could not find a function that takes these values as input, so I cannot simulate this scenario.")
        return r_search(cx)
    s = g.sym[fn]
    mp = ", ".join(f"{k} → {v}" for k, v in mapping.items())
    cx.pack.assumptions.append(f"The values in the question are delivered to {cx.D(fn)} ({cx.loc(s['file'], s['line'])}); "
                               f"mapped as: {mp}.")
    res = None
    if cx.runner is not None:
        try:
            res = cx.runner.run_for(cx, fn, inputs, purpose="scenario")
        except Exception as e:  # noqa: BLE001
            cx.pack.notes.append(f"Running the code was not possible ({type(e).__name__}: {e}); falling back to reading it.")
    if res and res.get("ok"):
        _narrate_run(cx, res)
    else:
        if res and not res.get("ok"):
            cx.pack.notes.append("Running the code was not possible: " + res.get("why", "unknown reason"))
        static_scenario(cx, fn, mapping, inputs)


def pick_scenario_target(cx: Ctx, inputs: dict[str, Any]) -> tuple[str | None, dict[str, str]]:
    """The function whose parameters (or the fields of its struct parameter) best match the input names."""
    g = cx.g
    explicit = [c for m in cx.r.mentions for c in m["candidates"] if c in g.defined]
    best = (0.0, None, {})
    qwords = set(split_ident(cx.r.question))
    # globals a function READS in its decisions can also carry the scenario's values ("the phase current is 13 A")
    reads_in_branch: dict[str, set[str]] = defaultdict(set)
    for fn_, brs in g.branches.items():
        for br in brs:
            for r_ in br["reads"]:
                if g.sym.get(r_, {}).get("kind") == "global":
                    reads_in_branch[fn_].add(r_)
    for f in (explicit or list(g.defined)):
        params = sorted([k for k in g.sym if k.startswith(f + "#param:")], key=lambda k: g.sym[k].get("idx") or 0)
        fields: dict[str, str] = {}
        for p in params:
            ps = g.sym[p]
            fields[ps["name"]] = p
            for k2 in g.record_fields(ps.get("sig") or "", ps.get("type") or ""):
                fields[f"{ps['name']}->{g.sym[k2]['name']}"] = k2
        for gk in reads_in_branch.get(f, ()):
            fields.setdefault(f"global:{g.sym[gk]['name']}", gk)
        mapping = {}
        score = 0.0
        for inp in inputs:
            iw = set(split_ident(inp))
            bestf, bs = None, 0.0
            for fname in fields:
                leaf = fname.split("->")[-1].split(":")[-1]
                fw = set(split_ident(leaf))
                sc = len(iw & fw) / max(1, len(iw)) + (0.5 if leaf.lower().startswith(inp.lower()) else 0)
                if fname.startswith("global:"):
                    sc += 0.25 * len((fw - iw) & qwords) - 0.1      # "phase current" -> g_phaseCurrent over g_currentRef
                if sc > bs:
                    bestf, bs = fname, sc
            if bestf and bs >= 0.5:
                mapping[inp] = bestf
                score += bs
        if not mapping:
            continue
        # prefer pipeline stages that do more with the data (callers over their callees) and entry/callback targets
        score += 0.1 * len(g.transitive_callees(f)) + (0.5 if any(c.res in ("flow", "table") for c in g.callers(f)) else 0)
        if explicit and f in explicit:
            score += 10
        if score > best[0]:
            best = (score, f, mapping)
    return best[1], best[2]


def static_scenario(cx: Ctx, fn: str, mapping: dict[str, str], inputs: dict[str, Any]) -> None:
    """Without running: evaluate each decision in fn (and callees) with the given values; unknown values → RUNTIME."""
    g = cx.g
    env = {}
    for inp, fname in mapping.items():
        v = inputs[inp]
        env[fname.split("->")[-1].split(":")[-1]] = int(v) if isinstance(v, bool) else v
    lines = []
    cls = []
    for br in sorted(g.branches.get(fn, []), key=lambda b: b["line"]):
        if br["kind"] not in ("if", "ternary"):
            continue
        val, unknown = g.eval_cond(br["cond"], env)
        if val is None:
            cls.append(cx.claim(f"`{br['cond']}` cannot be decided from the given values (needs {', '.join(unknown[:3])}).",
                                "branch_eval", [fn, br["line"], None], [cx.ev("branch", br["file"], br["line"], fn)], RUNTIME,
                                "depends on values not given in the question"))
            lines.append(f"`{br['cond']}` (line {br['line']}) depends on {', '.join(unknown[:3])}, which the question does not give")
        else:
            cls.append(cx.claim(f"`{br['cond']}` is {'true' if val else 'false'} for these values.", "branch_eval",
                                [fn, br["line"], val], [cx.ev("branch", br["file"], br["line"], fn)], INFERENCE,
                                "evaluated by reading the code, not by running it"))
            lines.append(f"`{br['cond']}` (line {br['line']}) is {'true' if val else 'false'}")
    cx.say(f"Reading {cx.D(fn)} with these values: " + ("; ".join(lines) if lines else "it has no decisions of its own") + ".", cls)
    for c in g.callees(fn):
        if c.callee in g.defined:
            sub = []
            for br in g.branches.get(c.callee, []):
                val, unk = g.eval_cond(br["cond"], env)
                if val is not None:
                    sub.append(f"`{br['cond']}` is {'true' if val else 'false'}")
            if sub:
                cx.say(f"In {cx.D(c.callee)}: " + "; ".join(sub) + ".")


def _narrate_run(cx: Ctx, res: dict[str, Any]) -> None:
    """Turn a sandboxed run (runtime_exec) into OBSERVED claims."""
    g = cx.g
    pack = cx.pack
    if not res.get("ok"):
        pack.notes.append("Running the code was not possible: " + str(res.get("why") or "unknown reason"))
        return
    pack.run = res
    runs = res.get("runs") or [res]
    for run in runs:
        label = run.get("label", "")
        rid = pack.add("run", None, None, _readable_run_output(run.get("stdout_tail", ""))[:2000], None,
                       f"sandboxed run {label}".strip(), status="OBSERVED")
        cls = []
        taken = []
        order = {n: i for i, n in reversed(list(enumerate(run.get("calls", []))))}
        brs = [b for b in run.get("branches", []) if b["taken"] is not None]
        brs.sort(key=lambda b: (order.get(g.name(b["fn"]), 999), b["line"]))
        by_fn: dict[str, list[str]] = {}
        for b in brs:
            e = cx.ev("branch", b["file"], b["line"], b.get("fn"), status="OBSERVED")
            cls.append(cx.claim(f"`{b['cond']}` was {'true' if b['taken'] else 'false'} ({cx.loc(b['file'], b['line'])}).",
                                "branch_taken", [b["file"], b["line"], b["taken"]], [e, rid], OBSERVED,
                                "seen in a sandboxed run of the real code"))
            if _is_utility(cx, b["fn"]):
                continue
            ev_n, then_n = b.get("evaluated") or 1, b.get("then_count") or 0
            if ev_n > 1 and 0 < then_n < ev_n:
                verdict = f"TRUE {then_n} of {ev_n} times"
            else:
                verdict = "TRUE" if b["taken"] else "false"
            by_fn.setdefault(cx.D(b["fn"]), []).append(f"`{b['cond']}` was {verdict} (line {b['line']})")
        for fname, items in by_fn.items():
            taken.append(f"in {fname}: " + ", ".join(items))
        trace = run.get("calls", [])
        ins = ", ".join(f"{k} = {v}" for k, v in (run.get("inputs") or {}).items())
        head = f"I ran the real code in a sandbox{(' (' + label + ')') if label else ''}" + (f" with {ins}" if ins else "") + "."
        cx.say(head, [])
        if trace:
            cx.say("Functions that ran, in order: " + " → ".join(t if t.endswith(("()", ")")) or " × " in t else f"{t}()"
                                                                 for t in _dedupe_seq(trace)[:18]) + ".",
                   [cx.claim("Call sequence observed: " + " → ".join(_dedupe_seq(trace)[:18]), "run_trace", [trace[:40]], [rid],
                             OBSERVED, "recorded by instrumentation during the run")])
            for t in _dedupe_seq(trace)[:18]:
                k = next((k for k in g.defined if g.sym[k]["name"] == t), None)
                if k:
                    s = g.sym[k]
                    pack.path.append({"label": f"{t}()", "file": s["file"], "line": s["line"], "ev": cx.ev_def(k),
                                      "status": OBSERVED, "note": "ran"})
        if taken:
            cx.say("Decisions, in the order they ran: " + "; ".join(taken[:10]) + ".", cls)
        outs = run.get("state") or {}
        if outs:
            oc = cx.claim("Final values: " + ", ".join(f"{k} = {v}" for k, v in outs.items()), "run_state", [outs], [rid],
                          OBSERVED, "printed by the harness after the run")
            cx.say("Resulting values: " + ", ".join(f"{k} = {v}" for k, v in list(outs.items())[:12]) + ".", [oc])
        hw = run.get("hw_calls") or []
        if hw:
            hc = cx.claim("Hardware/OS calls: " + "; ".join(hw[:10]), "run_hw", [hw], [rid], OBSERVED,
                          "recorded by the stand-in (stub) functions during the run")
            cx.say("Outputs it produced: " + "; ".join(hw[:10]) + ".", [hc])
        elif run.get("hw_expected"):
            cx.say("It did not call any output functions (" + ", ".join(run["hw_expected"][:4]) + " were not called).")
        for a in run.get("assumptions", []):
            if a not in pack.assumptions:
                pack.assumptions.append(a)
    if res.get("explain"):
        cx.say(res["explain"])


def _dedupe_seq(xs: list[str]) -> list[str]:
    out: list[str] = []
    for x in xs:
        if not out or out[-1] != x:
            out.append(x)
    # compress a repeated cycle: a b c a b c a b c -> "(a → b → c) × 3"
    for size in range(1, 7):
        i = 0
        res: list[str] = []
        changed = False
        while i < len(out):
            chunk = out[i:i + size]
            n = 1
            while out[i + n * size:i + (n + 1) * size] == chunk and len(chunk) == size:
                n += 1
            if n > 1 and size > 0:
                label = chunk[0] if size == 1 else "(" + " → ".join(f"{c}()" for c in chunk) + ")"
                res.append(f"{label} × {n}" if size > 1 else f"{chunk[0]} × {n}")
                i += n * size
                changed = True
            else:
                res.append(out[i])
                i += 1
        if changed:
            out = res
    return out


ENTRY_WORDS = {"main": "program start", "isr": "interrupt handler", "thread": "thread", "timer": "timer callback",
               "callback": "callback", "event": "event handler", "handler": "handler", "test": "test", "export": "exported API"}


def _reach(g: CodeGraph, fn: str, cap: int = 3000) -> int:
    """How many functions of this code an entry point can reach (bounded)."""
    seen = {fn}
    todo = [fn]
    while todo and len(seen) < cap:
        f = todo.pop()
        for c in g.out_calls.get(f, []):
            if c.callee in g.defined and c.callee not in seen:
                seen.add(c.callee)
                todo.append(c.callee)
    return len(seen)


def r_entries(cx: Ctx) -> None:
    """Where execution starts: program entry, threads, interrupts, timers, callbacks, event handlers, exported APIs."""
    g = cx.g
    q = cx.r.question.lower()
    want = None
    for k, words in (("thread", ("thread", "task", "background", "parallel", "concurrent")), ("isr", ("interrupt", "isr")),
                     ("timer", ("timer",)), ("callback", ("callback",)), ("event", ("event",))):
        if any(w in q for w in words):
            want = k
            break
    rows = [(k, e) for k, e in g.entries.items() if k in g.sym]
    # exported functions (DLL exports) are entry points too
    for k, s in g.sym.items():
        if s["kind"] == "function" and k in g.defined and "dllexport" in (g.src.line(s["file"], s["line"]) or ""):
            rows.append((k, {"kind": "export", "detail": "exported from the DLL (__declspec(dllexport))", "res": "exact"}))
    if want:
        sel = [(k, e) for k, e in rows if e["kind"] == want or (want == "thread" and e["kind"] == "task")]
    else:
        sel = rows
    if not sel:
        cx.pack.unknowns.append("No matching entry points were found.")
        cx.say(f"I found no {ENTRY_WORDS.get(want, 'entry points') + 's' if want else 'entry points'} in this code." +
               (" Entry points found: " + ", ".join(f"{cx.D(k)} ({ENTRY_WORDS.get(e['kind'], e['kind'])})" for k, e in rows[:8])
                + "." if rows else ""))
        return
    order = {"main": 0, "thread": 1, "isr": 2, "timer": 3, "event": 4, "callback": 5, "handler": 6, "export": 7, "test": 8}
    # within a kind: the program's own code before tests/examples, and the entries that drive the most code first
    # (the stack's main thread before a demo app's)
    conf = {"keyword": 0, "exact": 0, "apipack": 0, "wrapper": 1, "flow": 1, "table": 1, "trampoline": 3, "pattern": 2,
            "heuristic": 2}
    sel.sort(key=lambda ke: (order.get(ke[1]["kind"], 9), g.is_test(ke[0]), conf.get(ke[1].get("res"), 2),
                             -_reach(g, ke[0]) if ke[1]["kind"] in ("thread", "task") else 0,
                             g.sym[ke[0]].get("file") or "", g.sym[ke[0]].get("line") or 0))
    items, cls = [], []
    for k, e in sel[:20]:
        s = g.sym[k]
        st = CONFIRMED if e["res"] in ("exact", "apipack", "keyword") else INFERENCE
        why = "" if st == CONFIRMED else "recognised by naming convention or because its address is stored"
        # where it is started / registered
        reg = ""
        for fa in g.fnaddr:
            if fa["fn"] == k and fa["kind"] in ("arg", "assign", "table", "init"):
                reg = f", registered at {cx.loc(fa['file'], fa['line'])}"
                cls.append(cx.claim(f"The address of {cx.D(k)} is passed/stored at {cx.loc(fa['file'], fa['line'])}.",
                                    "fnaddr", [k, fa["dst"], fa["file"], fa["line"]],
                                    [cx.ev("flow", fa["file"], fa["line"], k, "registered here")], st, why))
                break
        cls.append(cx.claim(f"{cx.D(k)} is a {ENTRY_WORDS.get(e['kind'], e['kind'])} ({e['detail']}).",
                            "entry" if e["kind"] != "export" else "defined",
                            [k] if e["kind"] != "export" else [k, s["file"], s["line"]], [cx.ev_def(k)], st, why))
        items.append(f"{cx.D(k)} — {ENTRY_WORDS.get(e['kind'], e['kind'])}: {e['detail']}{reg}")
        cx.pack.related.append({"key": k, "label": cx.D(k), "file": s["file"], "line": s["line"], "end_line": s["end_line"]})
    label = {"thread": "threads", "isr": "interrupt handlers", "timer": "timer callbacks", "callback": "callbacks",
             "event": "event handlers"}.get(want or "", "entry points (places where execution starts)")
    cx.say(f"The code has {len(sel)} {label}: " + "; ".join(items) + ".", cls)
    for k, e in sel[:3]:
        if e["kind"] in ("thread", "isr", "timer", "callback", "event"):
            steps = describe_steps(cx, k, fn_steps(cx, k))
            if steps:
                cx.say(f"{cx.D(k)} " + "; then it ".join(steps[:5]) + ".")


def _free_macros(cx: Ctx) -> dict[str, dict]:
    return {r["name"]: dict(r) for r in cx.st.q("SELECT * FROM freemacro")}


def r_config(cx: Ctx, macro: str) -> None:
    """A build-configuration switch (#ifdef M, never defined in the code): what changes when it is defined."""
    g = cx.g
    fm = _free_macros(cx).get(macro) or {}
    files = json.loads(fm.get("files") or "[]") if fm else []
    blocks = []
    for rel in files:
        lines = g.src.lines(rel)
        i = 0
        while i < len(lines):
            m = re.match(rf"\s*#\s*(ifdef\s+{macro}\b|if\s+.*\b{macro}\b|ifndef\s+{macro}\b)", lines[i])
            if m:
                negated = "ifndef" in m.group(1) or re.search(rf"!\s*defined\s*\(?\s*{macro}", lines[i])
                depth, j, else_at = 0, i + 1, None
                while j < len(lines):
                    t = lines[j].strip()
                    if re.match(r"#\s*if", t):
                        depth += 1
                    elif re.match(r"#\s*endif", t):
                        if depth == 0:
                            break
                        depth -= 1
                    elif re.match(r"#\s*(else|elif)", t) and depth == 0 and else_at is None:
                        else_at = j
                    j += 1
                # 1-based ranges of the code inside the branches (excluding the directive lines themselves)
                on = (i + 2, else_at if else_at is not None else j)
                off = (else_at + 2, j) if else_at is not None else None
                if negated:
                    on, off = off, on
                blocks.append((rel, i + 1, on, off))
                i = j
            i += 1
    if not blocks:
        cx.pack.unknowns.append(f"No #ifdef {macro} blocks were found.")
        return r_search(cx)
    cls = []
    parts = []
    for rel, at, on, off in blocks[:6]:
        fn = cx.g.fn_of("") or None
        owner = next((k for k in g.defined if g.sym[k]["file"] == rel and g.sym[k]["line"] <= at <= (g.sym[k]["end_line"] or 0)),
                     None)
        code_on = [_code(t) for _, t in g.src.snippet(rel, on[0], on[1])] if on else []
        code_on = [c for c in code_on if c and not c.startswith(("/*", "//", "*", "#"))]
        code_off = [_code(t) for _, t in g.src.snippet(rel, off[0], off[1])] if off else []
        code_off = [c for c in code_off if c and not c.startswith(("/*", "//", "*", "#"))]
        ev = cx.pack.add("def", rel, at, g.src.line(rel, at).strip(), None, f"#ifdef {macro}", CONFIRMED, end_line=(on or off)[1])
        cls.append(cx.claim(f"{cx.loc(rel, at)} is compiled differently depending on {macro}.", "guard", [macro, rel, at], [ev],
                            RUNTIME, f"depends on whether {macro} is defined when building"))
        where = f"in {cx.D(owner)} ({cx.loc(rel, at)})" if owner else f"at {cx.loc(rel, at)}"
        txt = f"{where}: when {macro} is defined, " + ("it runs " + "; ".join(f"`{c}`" for c in code_on[:4]) if code_on
                                                      else "nothing extra runs")
        if off is not None:
            txt += "; otherwise (the default) it runs " + ("; ".join(f"`{c}`" for c in code_off[:4]) if code_off else "nothing")
        parts.append(txt)
        cx.flow_step(f"#ifdef {macro} {where}", rel, at, ev)
    cx.say(f"{macro} is a build switch: the code tests it but never defines it, so by default it is OFF. " +
           ". ".join(p[0].upper() + p[1:] for p in parts) + ".", cls)
    cx.pack.assumptions.append(f"Default build: {macro} is not defined (it is only tested in #if/#ifdef).")
    # the variant's own facts, when the index analysed it
    plus = cx.st.q("SELECT DISTINCT fn FROM occ WHERE cfg=? AND fn IS NOT NULL", (f"+{macro}",))
    minus = cx.st.q("SELECT DISTINCT caller FROM call WHERE cfg LIKE ?", (f"%-{macro}%",))
    if plus or minus:
        cx.say("Verity analysed both builds: " + (f"with it, extra behaviour appears in {', '.join(cx.D(r['fn']) for r in plus)}"
                                                    if plus else "") +
               ("; " if plus and minus else "") +
               (f"with it, calls disappear from {', '.join(cx.D(r['caller']) for r in minus)}" if minus else "") + ".")


VERB_SYNONYMS = {"verif": {"verify", "check", "validate", "valid"}, "check": {"check", "verify", "validate"},
                 "validat": {"validate", "check", "verify", "valid"}, "pars": {"parse", "scan", "decode", "split"},
                 "decod": {"decode", "parse"}, "encod": {"encode", "serialize", "format"}, "sent": {"send", "transmit", "write", "tx"},
                 "send": {"send", "transmit", "write", "tx"}, "transmitt": {"transmit", "send", "tx"}, "stor": {"store", "save", "write", "put"},
                 "sav": {"save", "store", "write"}, "load": {"load", "read", "get"}, "initiali": {"init", "initialize", "setup", "start"},
                 "schedul": {"schedule", "switch", "select"}, "allocat": {"alloc", "malloc", "create", "new"},
                 "freed": {"free", "release", "delete", "destroy"}, "lock": {"lock", "take", "acquire", "enter"},
                 "unlock": {"unlock", "give", "release", "exit"}, "convert": {"convert", "to"}, "format": {"format", "print"}}


CHECK_VERB_RE = re.compile(r"\b(verif(y|ied|ies)|check(s|ed)?|validat(e|es|ed))\b", re.I)


def _cond_code(cond: str) -> str:
    """A condition's code without its message strings, comments and preprocessor lines."""
    t = STRING_RE.sub(" ", cond)
    t = re.sub(r"/\*.*?\*/", " ", t)
    return re.sub(r"#\s*(?:if|ifdef|ifndef|elif)\s+[^|&()]*|#\s*(?:else|endif)\b", " ", t)


def _find_check(cx: Ctx) -> bool:
    """'How is the IPv4 header checksum verified?': the decision in the code that tests that value — found by the
    question's concepts in the condition (and the function around it), not by function names alone."""
    g = cx.g
    concepts = _concepts(cx, {"verified", "checked", "validated", "verify", "check", "validate", "how"})
    if len(concepts) < 2:
        return False
    W = cx.word_weight
    best = []
    for fn in g.defined:
        if not cx.about_tests and g.is_test(fn):
            continue
        fwords = set(split_ident(g.name(fn)))
        for br in g.branches.get(fn, []):
            if br["kind"] != "if" or not br["cond"] or DIAG_COND_RE.match(br["cond"]) or \
                    re.match(r"^\s*\w*ASSERT\w*\s*\(", br["cond"]):
                continue
            code = _cond_code(br["cond"])
            cwords = set(split_ident(code))
            # abbreviations run together inside one identifier ("iphdr" holds "hdr") also count
            cwords |= {w for c in concepts for w in c if len(w) >= 3 for t in cwords if len(t) > len(w) and w in t}
            hit_c = [c for c in concepts if c & cwords]
            if not hit_c:
                continue                            # the value itself must be tested in the condition
            cov = _coverage(concepts, cwords | fwords)
            spec = sum(max(W(w) for w in c & cwords) for c in hit_c)
            compares = 1 if re.search(r"(==|!=|<=|>=|<|>)", code) else 0
            best.append([cov, compares, len(hit_c), spec, fn, br])
    if not best:
        return False
    top = max(b[0] for b in best)
    for b in best:
        if b[0] < top:
            continue
        fn, br = b[4], b[5]
        # a verification computes the value (a call named for it inside the test) and rejects on failure
        computes = any(set(split_ident(g.name(c.callee))) & set().union(*concepts) for c in g.out_calls.get(fn, [])
                       if c.line == br["line"])
        a = _branch_actions(cx, fn, br)
        rejects = bool(a.get("jump") or a.get("returns_top") or any(
            re.search(r"err|drop|fail|invalid|bad", g.name(sym), re.I) for sym, _ in a["writes"]))
        b[1] += int(computes) + int(rejects)
    best.sort(key=lambda x: (-x[0], -x[1], -x[2], -x[3], g.sym[x[4]]["file"], x[5]["line"]))
    best = [(b[0], b[3], b[4], b[5]) for b in best]
    loc_c = lambda b: _branch_loc(b[2], b[3])          # noqa: E731
    best = cx.learn.rank(best, loc_c, lambda b: float(b[0]))
    best = _rerank(cx, best, lambda b: _render_branch(cx, (b[2], b[3], _branch_actions(cx, b[2], b[3]))))
    cov, _, fn, br = best[0]
    if cov < 0.6:
        return False
    cx.learn.offer(best, loc_c, lambda b: (_branch_label(cx, b[2], b[3]),
                                           _render_branch(cx, (b[2], b[3], _branch_actions(cx, b[2], b[3])))),
                   lambda b: float(b[0]))
    act = _branch_actions(cx, fn, br)
    cls = [cx.claim(f"{cx.D(fn)} checks `{br['cond']}` at {cx.loc(br['file'], br['line'])}.", "branch",
                    [fn, br["file"], br["line"]], [cx.ev("branch", br["file"], br["line"], fn)])]
    comp = [c for c in g.out_calls.get(fn, []) if c.line == br["line"] and c.callee in g.defined]
    what = _records(cx, act["writes"])
    what += [f"calls {cx.D(c.callee)}" for c in act["calls"] if c.line != br["line"]][:3]
    if act.get("jump"):
        j = act["jump"]
        what.append(f"jumps to `{j['label']}` (line {j['line']})")
        what += [f"calls {cx.D(c.callee)}" for c in j["calls"]][:2]
    elif act.get("returns_top"):
        what.append("returns")
    cx.say(f"It is verified in {cx.D(fn)} ({cx.loc(br['file'], br['line'])}): it checks `{br['cond']}`" +
           (f", using {', '.join(cx.D(c.callee) for c in comp)}" if comp else "") +
           (f"; when that check fails it " + " and ".join(what) if what else "") + ".", cls)
    _, a0, b0 = cx.body_lines(fn)
    if b0 - a0 <= 40:
        explain_function(cx, fn, brief=True)        # a small checker: its steps are the answer
    for c in comp[:1]:
        explain_function(cx, c.callee, brief=True)
    others = [(f2, b2) for c2, _, f2, b2 in best[1:12] if c2 >= cov and f2 != fn][:3]
    if others:
        ocl = [cx.claim(f"{cx.D(f2)} checks `{b2['cond']}` at {cx.loc(b2['file'], b2['line'])}.", "branch",
                        [f2, b2["file"], b2["line"]], [cx.ev("branch", b2["file"], b2["line"], f2)]) for f2, b2 in others]
        cx.say("Similar checks: " + "; ".join(f"{cx.D(f2)} (`{b2['cond']}`, {cx.loc(b2['file'], b2['line'])})"
                                            for f2, b2 in others) + ".", ocl)
    return True


IO_VERB_RE = re.compile(r"\b(sent|sends?|sending|transmit(s|ted)?|written|writes?|output(s|ted)?|received?|receives|read|"
                       r"reads)\b", re.I)


def _find_io_fn(cx: Ctx) -> bool:
    """'How is a request sent over the serial line?': the function that performs that input/output — found through the
    I/O calls the code makes (WriteFile = "Win32 file/serial write") and the function's own name."""
    g = cx.g
    m = IO_VERB_RE.search(cx.r.question)
    if not m:
        return False
    out = not m.group(1).lower().startswith(("receiv", "read"))
    concepts = _concepts(cx, {m.group(1).lower(), "how", "over", "via", "through"})
    if not concepts:
        return False
    verbs = {"send", "transmit", "write", "tx", "output", "put"} if out else {"receive", "recv", "read", "rx", "input", "get"}
    by_fn: dict[str, list[dict]] = defaultdict(list)
    for io in g.io:
        if io["dir"] != ("sink" if out else "source") or not io.get("fn") or re.search(r"log|console|debug", io["api"] or "", re.I):
            continue
        if not cx.about_tests and g.is_test(io["fn"]):
            continue
        by_fn[io["fn"]].append(io)
    best = []
    for fn, ios in by_fn.items():
        allc = set().union(*concepts) if concepts else set()
        words = _name_words(g.name(fn), allc) | {w for io in ios for w in split_ident(io["api"] or "")}
        cov = _coverage(concepts, words)
        vhit = bool(words & verbs)
        best.append((cov, vhit, -len(g.name(fn)), fn, ios))
    if not best:
        return False
    best.sort(key=lambda x: (-x[0], -x[1], -x[2]))
    loc_i = lambda b: Loc(b[3], (cx.g.sym.get(b[3]) or {}).get("file"), None, None)      # noqa: E731
    best = cx.learn.rank(best, loc_i, lambda b: float(b[0]))
    cx.learn.offer(best, loc_i, lambda b: (_fn_label(cx, b[3]), _render_function(cx, b[3])), lambda b: float(b[0]),
                   what="function")
    cov, vhit, _, fn, ios = best[0]
    if cov < 0.3 or not vhit and cov < 0.6:
        return False                               # the medium named in the question must show up in the I/O it does
    s = g.sym[fn]
    io = ios[0]
    api_name = g.name(io["sym"])
    cl = [cx.claim(f"{cx.D(fn)} calls {api_name} at {cx.loc(io['file'], io['line'])}.", "io",
                   [io["sym"], io["file"], io["line"]], [cx.ev("io", io["file"], io["line"], io["sym"])])]
    cx.say(f"This is done by {cx.D(fn)} ({cx.loc(s['file'], s['line'])}): it calls {api_name}() ({io['api']}) at "
           f"{cx.loc(io['file'], io['line'])}.", cl)
    explain_function(cx, fn, brief=True)
    others = [(f2, i2) for c2, _, _, f2, i2 in best[1:6] if c2 >= cov][:3]
    if others:
        cx.say("Other places that do the same kind of " + ("output" if out else "input") + ": " +
               "; ".join(f"{cx.D(f2)} ({g.name(i2[0]['sym'])}(), {cx.loc(i2[0]['file'], i2[0]['line'])})" for f2, i2 in others)
               + ".")
    return True


def r_which_fn(cx: Ctx) -> None:
    """'Which function parses/computes/sends X?': search restricted to functions, then explain the best one."""
    pinned = cx.learn.pin_function() if cx.learn.pin_function() in cx.g.defined else None
    if not pinned and CHECK_VERB_RE.search(cx.r.question) and not any(m.get("explicit") for m in cx.r.mentions) and \
            _find_check(cx):
        return
    if not pinned and not any(m.get("explicit") for m in cx.r.mentions) and \
            re.search(r"\b(over|via|through|to|from|on)\s+(the\s+)?\w+", cx.r.question) and _find_io_fn(cx):
        return
    hits = cx.s.search(cx.r.question, k=12, kinds=FN_KINDS)
    hits = [h for h in hits if h.key in cx.g.defined] or hits
    qv = set()
    for w in split_ident(cx.r.question):
        for stem, syns in VERB_SYNONYMS.items():
            if w.startswith(stem):
                qv |= syns
    if qv:
        for h in hits:
            if set(split_ident(cx.g.name(h.key))) & qv:
                h.score *= 2.0
        hits.sort(key=lambda h: -h.score)
    loc_h = lambda h: Loc(h.key, getattr(h, "file", None), None, None)      # noqa: E731
    hits = cx.learn.rank(hits, loc_h, lambda h: float(h.score))
    if pinned:
        h0 = next((h for h in hits if h.key == pinned), None)
        if h0 is None:
            ps = cx.g.sym[pinned]
            h0 = SimpleNamespace(key=pinned, name=ps["name"], kind=ps["kind"], file=ps["file"], line=ps["line"],
                                 score=hits[0].score if hits else 1.0, why=["correction"])
        cx.learn.use_pin(changed=not hits or hits[0] is not h0)
        hits = [h0] + [h for h in hits if h is not h0]
    if not hits:
        return r_search(cx)
    cx.learn.offer(hits, loc_h, lambda h: (_fn_label(cx, h.key), _render_function(cx, h.key)), lambda h: float(h.score),
                   what="function")
    top = hits[0]
    s = cx.g.sym[top.key]
    c0 = cx.claim(f"{cx.D(top.key)} is defined in {cx.loc(s['file'], s['line'])}.", "defined", [top.key, s["file"], s["line"]],
                  [cx.ev_def(top.key)])
    cx.say(f"The best match is {cx.D(top.key)} ({cx.loc(s['file'], s['line'])}).", [c0])
    explain_function(cx, top.key)
    others = [h for h in hits[1:4] if h.score > top.score * 0.5]
    if others:
        cx.say("Other candidates: " + ", ".join(f"{cx.D(h.key)} ({cx.loc(h.file, h.line)})" for h in others) + ".")


def _concepts(cx: Ctx, drop: set[str] | None = None) -> list[set[str]]:
    """The question's content words, each with the abbreviations code uses for it. A word written as one token
    (EtherCAT, CANopen) is ONE concept even though it splits into several words."""
    drop = (drop or set()) | WEAK_TOPIC | {"happens", "happen", "triggers", "trigger", "causes", "cause", "reach", "reaches"}
    topic = set(cx.r.topic)
    out: list[set[str]] = []
    seen: set[str] = set()
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", cx.r.question):
        ws = [w for w in split_ident(tok) if w in topic and w not in drop and not re.fullmatch(FAIL_ADJ, w)]
        if not ws or all(w in seen for w in ws):
            continue
        seen.update(ws)
        c = expand_words(set(ws))
        if len(ws) > 1:
            c.add(tok.lower())
        out.append(c)
    # a phrase the code writes as an acronym ("task control block" -> TCB) is ONE concept: the acronym, or the phrase
    # written as one word — not any of its words alone (BLKS is not a task control block)
    for acr, ws in getattr(cx, "acronyms", {}).items():
        part = [c for c in out if c & ws]
        if len(part) >= 2:
            out = [c for c in out if not c & ws] + [{acr, acr + "s", "".join(sorted(ws, key=len))}]
    return out


def _coverage(concepts: list[set[str]], words: set[str]) -> float:
    return sum(1 for c in concepts if c & words) / len(concepts) if concepts else 0.0


VERB_CALLEE = {"sent": ("send", "tx", "transmit", "write", "put"), "transmitted": ("send", "tx", "transmit", "write"),
               "written": ("write", "put", "store", "save", "prog"), "logged": ("log", "print", "trace"),
               "reported": ("report", "error", "log", "raise"), "raised": ("raise", "error", "set"),
               "published": ("publish", "send", "post"), "emitted": ("emit", "send", "post")}


def _best_call_site(cx: Ctx, concepts: list[set[str]]):
    """(coverage, call) of the call whose callee + arguments cover most of the question's concepts, when the callee
    also performs the question's verb (sent -> send/tx…)."""
    g = cx.g
    verb = next((v for v in VERB_CALLEE if re.search(rf"\b{v}\b", cx.r.question, re.I)), None)
    if not verb or not concepts:
        return None
    verbs = set(VERB_CALLEE[verb])
    best = None
    for c in g.calls:
        if c.caller not in g.defined or (not cx.about_tests and g.is_test(c.caller)):
            continue
        cw = set(split_ident(g.name(c.callee)))
        if not cw & verbs and not any(v in w for w in cw for v in verbs):
            continue
        words = cw | {w for a in c.args for w in split_ident(a)} | set(split_ident(g.name(c.caller)))
        cov = _coverage(concepts, words)
        argcov = _coverage(concepts, {w for a in c.args for w in split_ident(a)})
        setup = bool(re.search(r"init|create|setup|config|register|alloc|open|new", g.name(c.callee), re.I))
        key = (cov, not setup, bool(cw & verbs), argcov)
        if cov > 0 and (best is None or key > best[0]):
            best = (key, c)
    return (best[0][0] + 0.01 * best[0][3], best[1]) if best else None


def _narrate_call_site(cx: Ctx, c: CallSite) -> None:
    g = cx.g
    st, why = cx.call_status(c)
    cl = [cx.claim(f"{cx.D(c.caller)} calls {cx.D(c.callee)} at {cx.loc(c.file, c.line)}.", "calls",
                   [c.caller, c.callee, c.file, c.line], [cx.ev("call", c.file, c.line, c.callee)], st, why)]
    conds = _guard_text(cx, c.guards)
    for gd in (c.guards or [])[-3:]:
        if gd.get("line"):
            cl.append(cx.claim(f"{cx.D(c.caller)} checks `{gd['cond']}` ({cx.loc(c.file, gd['line'])}).", "branch",
                               [c.caller, c.file, gd["line"]], [cx.ev("branch", c.file, gd["line"], c.caller)]))
    cx.say(f"{cx.D(c.caller)} does it with `{_code(g.src.line(c.file, c.line))}` ({cx.loc(c.file, c.line)})" +
           (f", {conds}" if conds else ", every time it runs") + ".", cl)
    txt, cl2 = how_reached(cx, c.caller)
    cx.say(f"{cx.D(c.caller)} itself: " + txt, cl2)


def r_triggers(cx: Ctx) -> None:
    """'What triggers X?' / 'When is X started?': the function that does X, and the conditions under which its callers
    call it."""
    g = cx.g
    tg = [t for t in cx.targets_ranked(FN_KINDS, k=15) if t in g.defined and (cx.about_tests or not g.is_test(t))]
    concepts = _concepts(cx, {"when", "sent", "send", "called", "run", "executed", "written", "triggered", "message"})
    # the action may be one CALL inside a bigger function (`CO_CANsend(NMT->HB_CANdevTx, NMT->HB_TXbuff)` sends the
    # heartbeat): score call sites by the question's concepts in the callee and its arguments
    site = _best_call_site(cx, concepts)
    fn_cov = max((_coverage(concepts, set(split_ident(g.name(t)))) for t in tg), default=0.0)
    if site and site[0] > fn_cov:
        return _narrate_call_site(cx, site[1])
    if not tg:
        return r_search(cx)
    ranked = sorted(enumerate(tg), key=lambda it: (-_coverage(concepts, set(split_ident(g.name(it[1])))), it[0]))
    best_cov = _coverage(concepts, set(split_ident(g.name(ranked[0][1]))))
    chosen = [t for _, t in ranked if _coverage(concepts, set(split_ident(g.name(t)))) == best_cov][:2]
    for fn in chosen:
        s = g.sym[fn]
        c0 = cx.claim(f"{cx.D(fn)} is defined in {cx.loc(s['file'], s['line'])}.", "defined", [fn, s["file"], s["line"]],
                      [cx.ev_def(fn)])
        callers = [c for c in g.callers(fn) if cx.about_tests or not g.is_test(c.caller)]
        if not callers:
            txt, cl = how_reached(cx, fn)
            cx.say(f"{cx.D(fn)} ({cx.loc(s['file'], s['line'])}) does this. " + txt, [c0] + cl)
            continue
        parts, cl = [], [c0]
        for c in callers[:4]:
            st, why = cx.call_status(c)
            cl.append(cx.claim(f"{cx.D(c.caller)} calls {cx.D(fn)} at {cx.loc(c.file, c.line)}.", "calls",
                               [c.caller, fn, c.file, c.line], [cx.ev("call", c.file, c.line, fn)], st, why))
            conds = _guard_text(cx, c.guards)
            parts.append(f"{cx.D(c.caller)} calls it at {cx.loc(c.file, c.line)}" + (f", {conds}" if conds else ""))
            for gd in (c.guards or [])[-2:]:
                if gd.get("line"):
                    cl.append(cx.claim(f"{cx.D(c.caller)} checks `{gd['cond']}` ({cx.loc(c.file, gd['line'])}).", "branch",
                                       [c.caller, c.file, gd["line"]], [cx.ev("branch", c.file, gd["line"], c.caller)]))
        cx.say(f"{cx.D(fn)} ({cx.loc(s['file'], s['line'])}) does this. It is triggered where it is called: "
               + "; ".join(parts) + ".", cl)
        # one level up: what runs the caller (a timer, a thread, an interrupt…)
        up = callers[0].caller
        txt, cl2 = how_reached(cx, up)
        cx.say(f"{cx.D(up)} itself: " + txt, cl2)


REACH_RE = re.compile(r"\b(?:reach(?:es)?|gets? to|ends? up in|arrives? (?:at|in)|makes? it to|leads? to|goes to)\s+(?:the\s+)?"
                      r"([A-Za-z_]\w*)(?:\(\))?", re.I)


def r_call_path(cx: Ctx) -> bool:
    """'How does a received frame reach tcp_input?': the call chain from where that data enters to the named function."""
    g = cx.g
    m = REACH_RE.search(cx.r.question)
    if not m:
        return False
    dst = next((k for k in g.by_name.get(m.group(1), []) if k in g.defined), None)
    if not dst:
        return False
    head = cx.r.question[:m.start()]
    words = [w for w in split_ident(head) if w not in ("how", "does", "do", "a", "an", "the", "is", "are", "what", "path")]
    cands = [h.key for h in cx.s.search(" ".join(words), k=20, kinds=FN_KINDS)] if words else []
    cands = [c for c in cands if c in g.defined and c != dst and not g.is_test(c)]
    best = None
    for rank, src in enumerate(cands[:8]):
        path = g.call_path(src, dst, max_len=8)
        if path and not any(g.is_test(c.callee) for c in path):
            key = (rank + 2 * len(path), rank)
            if best is None or key < best[0]:
                best = (key, src, path)
    if best is None:
        ps = g.paths_from_entries(dst, limit=1)
        if not ps or not ps[0]:
            return False
        path = ps[0]
        src = path[0].caller
    else:
        _, src, path = best
    cl = []
    hops = []
    for c in path:
        st, why = cx.call_status(c)
        cl.append(cx.claim(f"{cx.D(c.caller)} calls {cx.D(c.callee)} at {cx.loc(c.file, c.line)}.", "calls",
                           [c.caller, c.callee, c.file, c.line], [cx.ev("call", c.file, c.line, c.callee)], st, why))
        how = _via_phrase(cx, c)
        conds = _guard_text(cx, c.guards[-1:]) if c.guards else ""
        hops.append(f"{cx.D(c.caller)} calls {cx.D(c.callee)}{(' ' + how) if how else ''} ({cx.loc(c.file, c.line)}"
                    + (f", {conds}" if conds else "") + ")")
        cx.flow_step(f"{cx.D(c.caller)} → {cx.D(c.callee)}", c.file, c.line, cx.pack.claims[cl[-1]].ev[0])
    s0 = g.sym[src]
    cx.say(f"It enters at {cx.D(src)} ({cx.loc(s0['file'], s0['line'])}). The call chain to {cx.D(dst)} is: " +
           " → ".join([cx.D(src)] + [cx.D(c.callee) for c in path]) + ".", cl[:1])
    cx.say("Step by step: " + "; ".join(hops) + ".", cl)
    txt, cl2 = how_reached(cx, src)
    cx.say(f"{cx.D(src)} itself: " + txt, cl2)
    return True


def _enclosing_conditions(g: CodeGraph, file: str | None, line: int | None) -> list[tuple[str, int]]:
    """The preprocessor conditions a line sits inside (`#if …`, `#ifdef …`, `#else` of …), include guards left out."""
    if not file or not line:
        return []
    ls = g.src.lines(file)
    stack: list[tuple[str, int, bool]] = []
    for n in range(1, min(line, len(ls) + 1)):
        t = re.sub(r"/\*.*?\*/|//.*$", "", ls[n - 1]).strip()
        m = re.match(r"^#\s*(if|ifdef|ifndef|elif|else|endif)\b\s*(.*)$", t)
        if not m:
            continue
        kw, rest = m.group(1), m.group(2).strip()
        if kw in ("if", "ifdef", "ifndef"):
            guard = kw == "ifndef" and n < len(ls) and re.match(rf"^\s*#\s*define\s+{re.escape(rest)}\s*$", ls[n])
            stack.append((f"#{kw} {rest}", n, bool(guard)))
        elif kw == "elif" and stack:
            stack[-1] = (f"#elif {rest}", n, False)
        elif kw == "else" and stack:
            stack[-1] = (f"#else of `{stack[-1][0]}`", n, False)
        elif kw == "endif" and stack:
            stack.pop()
    return [(c, n) for c, n, guard in stack if not guard]


def variant_subjects(cx: Ctx, variant: str) -> list[str]:
    """Symbols the answer is about (claim arguments) that exist only in the given variant build."""
    out = []
    for c in cx.pack.claims:
        for a in c.args:
            if isinstance(a, str) and str(cx.g.sym.get(a, {}).get("cfg") or "").startswith(variant) and a not in out:
                out.append(a)
    return out


def note_variant_build(cx: Ctx, variant: str, key: str | None = None) -> None:
    """The question is about code the default build compiles out: say so first, with the conditions that enable it,
    and answer from the variant build."""
    g = cx.g
    keys = [key] if key else \
        [c for m in cx.r.mentions for c in m["candidates"] if str(g.sym.get(c, {}).get("cfg") or "").startswith(variant)]
    if not keys:
        return
    k = keys[0]
    s = g.sym[k]
    conds = _enclosing_conditions(g, s.get("file"), s.get("line"))
    knobs = (cx.cfg or {}).get("knobs") or {}
    used = [(n, knobs[n]) for n in knobs if any(re.search(rf"\b{re.escape(n)}\b", c) for c, _ in conds)]
    cl = [cx.claim(f"{cx.D(k)} is defined at {cx.loc(s['file'], s['line'])}.", "defined", [k, s["file"], s["line"]],
                   [cx.ev_def(k)])]
    what = "; ".join(f"`{c}` ({cx.loc(s['file'], n)})" for c, n in conds[-3:]) or "a build option"
    if variant == "+ALL_FEATURES":
        dflt = ("; by default " + ", ".join(f"`{n}` is `{i['default']}` ({i['where']})" for n, i in used[:3])) if used else ""
        cx.say(f"{cx.D(k)} is compiled only when the build switches on optional features: {what}{dflt}. The default "
               f"configuration of this code leaves it out, so the answer below describes a build with those features "
               f"switched on.", cl)
    else:
        cx.say(f"{cx.D(k)} is compiled only when {variant[1:]} is defined ({what}); it is not defined anywhere in this "
               f"code, so the answer below describes a build with {variant[1:]} defined.", cl)
    cx.pack.assumptions.append(f"Answered for the {'all-features' if variant == '+ALL_FEATURES' else variant[1:]} build: "
                               f"{cx.D(k)} is compiled out of the default configuration.")


def _source_mentions(cx: Ctx, name: str, limit: int = 3) -> tuple[list[tuple[str, int, str]], bool]:
    """Where a name is written as CODE in the project's source files (comments and strings do not count), even in code
    the analysis did not compile; and whether it appears in comments/strings at all (then it is a word, not a name)."""
    from ..extract.recon import strip_comments_keep_lines
    out, in_text = [], False
    rx = re.compile(rf"\b{re.escape(name)}\b")
    for r in cx.st.q("SELECT path FROM file ORDER BY path"):
        raw = "\n".join(cx.g.src.lines(r["path"]))
        if not rx.search(raw):
            continue
        in_text = True
        code = STRING_RE.sub('""', strip_comments_keep_lines(raw)).split("\n")
        for n, t in enumerate(code, 1):
            if rx.search(t):
                out.append((r["path"], n, t.strip()))
                if len(out) >= limit:
                    return out, True
    return out, in_text


def note_missing_names(cx: Ctx) -> None:
    """Names the question writes as code (modbus_reset_crc_table, ParseGpsFrame()) that this project does not define:
    said first and plainly, so what follows is not mistaken for an answer about them."""
    from ..retrieval.search import IDENT_RE, _looks_like_identifier
    g = cx.g
    named = {m["text"].rstrip("()") for m in cx.r.mentions}
    lower = getattr(g, "_names_lower", None)
    if lower is None:
        lower = {s["name"].lower() for s in g.sym.values() if s.get("name")}
        g._names_lower = lower
    for m in IDENT_RE.finditer(cx.r.question):
        tok = m.group(0).rstrip("()")
        base = re.split(r"\.|->|::", tok)[-1]
        if not _looks_like_identifier(m.group(0)) or tok in named or base.lower() in lower or len(base) < 4:
            continue
        seen, in_text = _source_mentions(cx, base)
        if in_text and not seen:
            continue                     # only in comments/strings: a word of the domain (EtherCAT), not a code name
        if seen:
            # written in the source, but in code no analysed build compiles: never claim it does not exist
            f0, n0, _ = seen[0]
            conds = _enclosing_conditions(g, f0, n0)
            ev = cx.ev("source", f0, n0, None, "name found in the source text")
            cx.say(f"`{base}` appears in the source ({cx.loc(f0, n0)}) but only inside code that none of the analysed "
                   f"builds compiles" + (": " + "; ".join(f"`{c}` (line {n})" for c, n in conds[-3:]) if conds else "") +
                   ". Facts about it are therefore not available; the closest analysed code is described below.",
                   [cx.claim(f"{base} is written at {cx.loc(f0, n0)}.", "source_text", [base, f0, n0], [ev])])
            continue
        hits = [h for h in cx.s.search(" ".join(split_ident(base)), k=4) if h.key in g.sym][:3]
        cid = cx.pack.add("absence", None, None, "", None, f"searched all {len(g.sym)} symbols for {base}")
        i = cx.claim(f"{base} is not defined in this code.", "not_defined", [base], [cid])
        cx.say(f"There is no `{base}` in this code (none of its {len(g.sym)} indexed symbols has that name)" +
               (". The closest names are " + ", ".join(f"{cx.D(h.key)} ({cx.loc(h.file, h.line)})" for h in hits) +
                ", described below" if hits else "") + ".", [i])


RECIPES = {"entries": r_entries, "which_fn": r_which_fn, "explain": r_explain, "purpose": r_purpose, "flow": r_flow, "origin": r_origin, "receives": r_receives,
           "where_computed": r_where_computed, "failure_invalid": r_failure_invalid,
           "failure_missing": r_failure_missing, "downstream": r_downstream, "callers": r_callers,
           "influence": r_influence, "impact": r_impact, "scenario": r_scenario, "search": r_search, "triggers": r_triggers}
