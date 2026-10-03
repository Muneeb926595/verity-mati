"""Using corrections while answering one question.

    learning = Learning.load(store, index, route, question, embedder, root)

A recipe that ranks candidate places hands its ranked list to the Learning object:

    found = cx.learn.rank(found, loc, goodness)       # hints: +50% for the corrected check, +25% for its function
    found = cx.learn.pin(found, loc, build)           # the same question: the corrected place goes first
    cx.learn.offer(found, loc, describe, goodness)    # the top candidates (alternatives the person can pick from)

`loc(item)` says where a candidate is: Loc(fn, file, line, side) with line = the check's line (or the target line for
a candidate that is not a check) and side = then | else | case | None. `build(correction)` makes a candidate from the
corrected place when the analysis did not find it (None when it cannot). Nothing here adds text to an answer; the
disclosure ("this answer uses correction c-0007") travels in the answer's meta and every client shows it.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from .store import LearnStore
from .text import cosine, jaccard, qnorm, short, stems

HINT_OVERLAP = 0.5            # same kind of question and this much topic-word overlap (Jaccard)
# Meaning similarity (embedding model). Tuned in P5-6 on cJSON, whose paraphrases and neighbours were written before
# any run: paraphrases 0.60-0.89 (7 of 10 >= 0.80), neighbours 0.45-0.76, unrelated questions <= 0.63. The spec's
# starting value 0.85 caught 1 paraphrase of 10.
MEANING_SAME = 0.80           # the same question in other words: used like the same question (pin), if relevant
HINT_MEANING = 0.70           # a similar question: ranking bonus only
HINT_BRANCH_BONUS = 0.5       # +50% for the corrected check
HINT_FN_BONUS = 0.25          # +25% for other checks in the corrected function
CORRECTABLE = {"failure_invalid", "failure_missing", "explain", "purpose", "where_computed", "which_fn"}
ALT_RATIO = 0.6               # an alternative is offered when it scores at least this share of the first candidate
ALT_MAX = 2                   # at most this many alternatives are shown (candidates 2 and 3)
CANDIDATES_KEPT = 3           # how many top candidates are recorded in the answer (for picking and for top-k grading)
# Words that turn a question into its opposite ("…cannot be opened" / "…is opened"). The router's topic words leave
# them out, and meaning vectors barely see them, so a question is matched as the same one only when both agree.
NEGATION_RE = re.compile(r"\b(?:not|no|never|cannot|without|unable|nothing|none|neither|nor)\b|n't\b", re.I)
# How strongly a pin matches, strongest first: the same words, the same kind of question about the same things, the
# same meaning. The strongest match wins; the newest correction wins among equals.
TIER = {"same": 0, "words": 1, "meaning": 2}


@dataclass(frozen=True)
class Loc:
    fn: str
    file: str | None
    line: int | None
    side: str | None = None
    span: tuple[int, int] | None = None   # a switch case: its lines (all cases share the switch's line)


def negated(question: str) -> bool:
    return bool(NEGATION_RE.search(question or ""))


class Learning:
    """What this project's corrections say about one question. Inert (changes nothing) when there are none."""

    def __init__(self, store: LearnStore | None = None, route=None, question: str = ""):
        self.store = store
        self.route = route
        self.question = question
        self.qn = qnorm(question)
        self.topic = stems(getattr(route, "topic", []) or [])
        self.intent = getattr(route, "intent", None)
        self.pins: list[dict] = []
        self.hints: list[dict] = []
        self.conflicts: dict[str, list[str]] = {}
        self.used: dict[str, dict] = {}
        self.candidates: list[dict] = []
        self.alternatives: list[dict] = []
        self.offered = False
        self.pin_applied = False
        self.pin_missed: dict | None = None
        self.glossary_used: list[str] = []
        self.meaning: dict[str, float] = {}          # correction id -> meaning similarity, for meaning matches
        self.match: dict[str, str] = {}              # correction id -> same | words | meaning | similar
        self.rerouted: str | None = None             # the kind of question taken from a same-meaning correction
        self.neg = negated(question)
        self._pending_hint: tuple[dict, bool, Any] | None = None    # (correction, changed, the item it put first)

    # ------------------------------------------------------------------------------------------ loading
    @classmethod
    def load(cls, store: LearnStore | None, route, question: str, qvec: list[float] | None = None,
             relevant: Callable[[dict], bool] | None = None) -> "Learning":
        """relevant(correction) says whether the corrected code has anything to do with this question (a word in
        common); a same-meaning match whose code is unrelated is only a hint."""
        lr = cls(store, route, question)
        if store is None:
            return lr
        active = store.corrections("active")
        if not active:
            return lr
        pins, hints = [], []
        for c in active:
            how = lr._match(c, qvec)
            if how == "meaning" and not (relevant is None or relevant(c)):
                how = "similar"                      # the same meaning, but the corrected code shares no word with it
            lr.match[c["id"]] = how or ""
            if how in TIER:
                pins.append(c)
            elif how == "similar":
                hints.append(c)
        # the strongest match wins, then the newest correction
        pins.sort(key=lambda c: (TIER[lr.match[c["id"]]], -(c.get("seq") or 0)))
        lr.pins = pins
        lr.hints = sorted(hints, key=lambda c: -(c.get("seq") or 0))
        if len(pins) > 1:
            # corrections that disagree on the same question (equally strong matches): the newest is used, all flagged
            first = pins[0]
            tier = lr.match[first["id"]]
            others = [c for c in pins[1:] if lr.match[c["id"]] == tier and not _same_target(c, first)]
            if others:
                lr.conflicts[first["id"]] = [c["id"] for c in others]
                _flag_conflicts(store, [first] + others)
            lr.pins = [first]
        return lr

    def _match(self, c: dict, qvec: list[float] | None) -> str | None:
        """same | words | meaning (used like the same question: pin) · similar (a ranking hint) · None."""
        if c.get("qnorm") and c["qnorm"] == self.qn:
            return "same"
        sim = cosine(qvec, c["qvec"]) if qvec and c.get("qvec") else 0.0
        if sim >= HINT_MEANING:
            self.meaning[c["id"]] = round(sim, 3)
        same_sense = negated(c.get("question") or "") == self.neg
        if sim >= MEANING_SAME and same_sense:
            return "meaning"                         # the same question in other words (any kind of question)
        same_kind = bool(self.intent) and c.get("intent") == self.intent
        ct = stems(c.get("topic") or [])
        if same_kind and ct and ct == self.topic and same_sense:
            return "words"
        if same_kind and jaccard(ct, self.topic) >= HINT_OVERLAP:
            return "similar"
        if sim >= HINT_MEANING:
            return "similar"
        return None

    @property
    def active(self) -> bool:
        return bool(self.pins or self.hints)

    def reset_run(self) -> None:
        """Before a recipe runs again (on another build of the code): forget what the previous run recorded."""
        self.used = {}
        self.candidates = []
        self.alternatives = []
        self.offered = False
        self.pin_applied = False
        self.pin_missed = None
        self._pending_hint = None

    # ------------------------------------------------------------------------------------------ ranking hooks
    def bonus(self, loc: Loc) -> tuple[float, dict | None]:
        """The hint multiplier for a candidate (1.0 = none) and the correction that gives it."""
        best, by = 1.0, None
        for c in self.hints:
            if loc.fn != c["target_fn"]:
                continue
            if _matches(c, loc):
                m = 1 + HINT_BRANCH_BONUS
            else:
                m = 1 + HINT_FN_BONUS
            if m > best:
                best, by = m, c
        return best, by

    def rank(self, items: list, loc: Callable[[Any], Loc], goodness: Callable[[Any], float],
             build: Callable[[dict], Any] | None = None) -> list:
        """Similar questions: corrected places get a ranking bonus (they still have to score close to the best). With
        build, a corrected place the analysis did not find joins the candidates first, with its own score."""
        if not self.hints or (not items and build is None):
            return items
        items = list(items)
        if build is not None:
            for c in self.hints:
                if any(_safe_match(c, loc, it) for it in items):
                    continue
                try:
                    it = build(c)
                except Exception:  # noqa: BLE001
                    it = None
                if it is not None:
                    items.append(it)
        if not items:
            return items
        scored = []
        for i, it in enumerate(items):
            try:
                g = float(goodness(it))
                lc = loc(it)
            except Exception:  # noqa: BLE001
                scored.append((float("-inf"), i, it, None))
                continue
            m, by = self.bonus(lc)
            scored.append((g + (m - 1) * abs(g), i, it, by if m > 1 else None))
        order = sorted(scored, key=lambda t: (-t[0], t[1]))
        if order and order[0][3] is not None:
            # disclosed only if the hinted place is still first once the ranking is final (the local model may still
            # choose another candidate): settled in offer(), or in meta() for recipes that offer nothing
            self._pending_hint = (order[0][3], order[0][2] is not items[0], order[0][2])
        else:
            self._pending_hint = None
        return [t[2] for t in order]

    def pin_target(self) -> dict | None:
        return self.pins[0] if self.pins else None

    def pin(self, items: list, loc: Callable[[Any], Loc], build: Callable[[dict], Any] | None = None) -> list:
        """The same question: the corrected place goes first (built from the code when it was not a candidate)."""
        c = self.pin_target()
        if c is None:
            return items
        for i, it in enumerate(items):
            try:
                if _matches(c, loc(it)):
                    self.pin_applied = True
                    self._use(c, "pin", changed=i > 0)
                    return [it] + items[:i] + items[i + 1:]
            except Exception:  # noqa: BLE001
                continue
        if build is not None:
            try:
                it = build(c)
            except Exception:  # noqa: BLE001
                it = None
            if it is not None:
                self.pin_applied = True
                self._use(c, "pin", changed=True)
                return [it] + list(items)
        self.pin_missed = c
        return items

    def pin_function(self) -> str | None:
        """For recipes that choose a function rather than a check: the corrected function of a matching pin."""
        c = self.pin_target()
        return c["target_fn"] if c else None

    def use_pin(self, changed: bool = True) -> None:
        c = self.pin_target()
        if c is not None:
            self.pin_applied = True
            self._use(c, "pin", changed=changed)

    def _use(self, c: dict, how: str, changed: bool = True) -> None:
        if c["id"] in self.used and self.used[c["id"]]["how"] == "pin":
            return
        match = self.match.get(c["id"]) or ("same" if how == "pin" else "similar")
        self.used[c["id"]] = {"id": c["id"], "how": how, "match": match if how == "pin" else "similar",
                              "changed": changed, "created": c.get("created"), "note": c.get("note") or "",
                              "target": c.get("target_label"), "question": c.get("question"),
                              "conflicts": self.conflicts.get(c["id"], [])}
        if c["id"] in self.meaning and qnorm(c.get("question") or "") != self.qn:
            self.used[c["id"]]["meaning"] = self.meaning[c["id"]]

    def _settle_hint(self, first: Any = None, loc: Callable[[Any], Loc] | None = None) -> None:
        """Disclose a pending hint if its place is first in the final ranking (or when that cannot be known)."""
        if self._pending_hint is None:
            return
        c, changed, item = self._pending_hint
        self._pending_hint = None
        if loc is not None and first is not None and first is not item and not _safe_match(c, loc, first):
            return                                   # something else came first in the end: the hint changed nothing
        self._use(c, "hint", changed=changed)

    # ------------------------------------------------------------------------------------------ alternatives
    def offer(self, items: list, loc: Callable[[Any], Loc], describe: Callable[[Any], tuple[str, str]],
              goodness: Callable[[Any], float] | None = None, what: str = "check") -> None:
        """Record the top candidates of the final ranking. describe(item) -> (label shown to people, text the grader
        and the model see). Alternatives are the close runners-up a person can pick from."""
        self.offered = True
        self._settle_hint(items[0] if items else None, loc)
        out = []
        for rank, it in enumerate(items[:CANDIDATES_KEPT], 1):
            try:
                lc = loc(it)
                label, text = describe(it)
                g = float(goodness(it)) if goodness else None
            except Exception:  # noqa: BLE001
                continue
            out.append({"rank": rank, "fn": lc.fn, "file": lc.file, "line": lc.line, "side": lc.side,
                        "span": list(lc.span) if lc.span else None, "label": short(label, 240), "text": text[:1200],
                        "score": round(g, 3) if g is not None else None, "what": what})
        self.candidates = out
        alts = []
        if len(out) > 1:
            top = out[0].get("score")
            for c in out[1:1 + ALT_MAX]:
                s = c.get("score")
                if top is None or s is None:
                    close = top is None and s is None      # unranked lists (no scores): offer them as they are
                elif top > 0:
                    close = s >= ALT_RATIO * top
                else:
                    close = s >= top - abs(top) * (1 - ALT_RATIO)
                if close:
                    alts.append({k: c[k] for k in ("rank", "fn", "file", "line", "side", "span", "label")})
        self.alternatives = alts

    # ------------------------------------------------------------------------------------------ result
    def meta(self, recipe_supports: bool) -> dict:
        self._settle_hint()
        used = list(self.used.values())
        # failure questions can always be corrected by picking a line, even when the analysis found no candidate
        out = {"correctable": bool(recipe_supports and (self.offered and self.candidates or
                                                        self.intent in ("failure_invalid", "failure_missing"))),
               "candidates": self.candidates, "alternatives": self.alternatives if recipe_supports else [],
               "used": used}
        if self.glossary_used:
            out["glossary_used"] = sorted(set(self.glossary_used))
        if self.rerouted:
            out["rerouted"] = self.rerouted
        if self.pin_missed is not None and not self.pin_applied:
            c = self.pin_missed
            out["pin_not_applied"] = {"id": c["id"], "target": c.get("target_label"),
                                      "why": "the corrected place could not be used for this kind of question"}
        return out

    def disclosure(self) -> list[str]:
        """One plain line per correction the answer used (for the CLI, MCP text and the answer's assumptions)."""
        lines = []
        for u in self.used.values():
            when = day(u.get("created"))
            match = u.get("match") or ("same" if u["how"] == "pin" else "similar")
            how = "recorded for " + MATCH_WORDS.get(match, MATCH_WORDS["similar"])
            if match != "same":
                how += f" (“{short(u.get('question') or '', 90)}”)"
            s = f"Uses correction {u['id']} ({when}), {how}"
            if u.get("note"):
                s += f": {u['note']}"
            if u.get("conflicts"):
                s += f". It disagrees with {', '.join(u['conflicts'])}; the newest is used"
            lines.append(s + ".")
        if self.rerouted and self.pins:
            lines.append(f"Answered as the same kind of question as correction {self.pins[0]['id']}, because it means "
                         "the same.")
        return lines


MATCH_WORDS = {"same": "this question", "words": "a question about the same thing",
               "meaning": "a question with the same meaning", "similar": "a similar question"}


def day(ts: float | None) -> str:
    """2 Oct 2026 (portable: no %-d, which Windows' strftime lacks)."""
    if not ts:
        return ""
    t = time.localtime(ts)
    return f"{t.tm_mday} {time.strftime('%b %Y', t)}"


def _matches(c: dict, loc: Loc) -> bool:
    """A candidate is the correction's target: same function and same check line and side (a target that is not a
    check matches the candidate at its line, or any candidate of that function when it names the function itself)."""
    if loc.fn != c["target_fn"]:
        return False
    if c.get("branch_line"):
        side = c.get("side") or "then"
        if loc.line != c["branch_line"]:
            return False
        if side == "case":
            if loc.side not in ("case", None):
                return False
            tl = c.get("target_line")
            # the case the corrected line is in (all cases share the switch's line); the switch line itself = any case
            return not (loc.span and tl and tl != c["branch_line"]) or loc.span[0] <= tl <= loc.span[1]
        return (loc.side or "then") == side
    return loc.line in (c.get("target_line"), None) or loc.line is None


def _safe_match(c: dict, loc: Callable[[Any], Loc], it: Any) -> bool:
    try:
        return _matches(c, loc(it))
    except Exception:  # noqa: BLE001
        return False


def _same_target(a: dict, b: dict) -> bool:
    return a["target_fn"] == b["target_fn"] and (a.get("branch_line") or a.get("target_line")) == \
        (b.get("branch_line") or b.get("target_line")) and (a.get("side") or "then") == (b.get("side") or "then")


def _flag_conflicts(store: LearnStore, group: list[dict]) -> None:
    ids = [c["id"] for c in group]
    for c in group:
        others = sorted(set(ids) - {c["id"]})
        if sorted(c.get("conflict") or []) != others:
            store.update_correction(c["id"], conflict=others)
            store.log("conflict", c["id"], {"with": others})
