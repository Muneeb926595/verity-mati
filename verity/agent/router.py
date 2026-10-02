"""Question router (design §16): deterministic rules pick the intent recipe; the local model is only a fallback
classifier when no rule matches. Also extracts scenario inputs ("heading = 270, speed 80, valid true") and topic words."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..retrieval.cards import STOP, split_ident

INTENTS: list[tuple[str, str]] = [
    ("impact", r"\b(if i|if we|if you|when i|should i)?\s*(change|modify|edit|refactor|rename|remove|delete|rewrite)\b.*"
               r"\b(affect|affected|impact|break|else|risk)|\bimpact of\b|\bblast radius\b|\bwhat (else )?(could|would|will|might) "
               r"(be )?(affected|break)|\bwho depends on\b"),
    ("scenario", r"(\b(what happens|which branch|what would happen|what will happen|what does .* do)\b.*"
                 r"(=|\b\d+(\.\d+)?\b|\btrue\b|\bfalse\b))|\bscenario\b|\bsimulat|\bwhich branch(es)? (execute|run|is taken)"),
    ("which_fn", r"^\s*how (is|are|does|do) (the |a |an )?[\w\s-]{2,40}? (verified|checked|validated|parsed|encoded|decoded|"
                 r"serialized|deserialized|converted|formatted|sent|transmitted|stored|saved|loaded|initiali[sz]ed|scheduled|"
                 r"allocated|freed|locked|unlocked|synchroni[sz]ed)\b"),
    ("where_computed", r"^\s*where (is|are|does|do) (the |a |an )?[\w\s.>-]{1,50}? (set|stored|saved|assigned|written|"
                       r"calculated|computed|updated|incremented|decremented|reset|cleared|changed|initiali[sz]ed|invalidated|flushed|"
                       r"dropped|discarded)\b"),
    ("triggers", r"^\s*(what|which (code|function|event|condition|timer)s?) (triggers?|causes?|starts?|fires?|initiates?|"
                 r"kicks? off|sets? off)\b|^\s*when (does|do|is|are) .* (triggered|fired|started|kicked off|initiated|sent|transmitted|"
                 r"published|emitted|written|called|invoked|executed|run|raised|reported|logged|reset|cleared|updated)\b|"
                 r"^\s*(under what conditions?|in which case|how often) .*\b(sent|called|run|executed|written|raised|reported)\b"),
    ("failure_missing", r"\b(disappears?|lost|loses|unavailable|"
                        # "missing" is data that stopped arriving — not "a message missing a required FIELD" (malformed)
                        r"missing(?!\s+(a|an|the|its|some|one)?\s*(required|mandatory|expected)?\s*\w*\s*(field|value|parameter|"
                        r"argument|bytes?|element|entry|item|key|header|tag|attribute|property)s?\b)|"
                        r"stops? (sending|arriving|working)|times? out|timeout|"
                        r"no (signal|data|fix|input|response|reply|link|connection)|goes away|drops? out|dropout|silent|stale|not received|never arrives?)\b"),
    ("failure_invalid", r"\b(invalid|bad|corrupt(ed)?|wrong|malformed|garbage|errors?|fails?|failure|failing|rejects?|rejected|"
                        r"missing (a|an|the|its|some|one)?\s*(required|mandatory|expected)?\s*\w*\s*(field|value|parameter|argument|"
                        r"bytes?|element|entry|item|key|header|tag|attribute|property)s?|"
                        r"out of range|checksum|too (low|high|small|large|big|old|slow|fast|few|many|much|long)|more than|below|above|exceeds?|"
                        r"not enough|insufficient|poor)\b"),
    ("failure_invalid", r"^\s*what (happens|will happen|would happen|does the (code|program|system) do) "
                        r"(on|when|if|after|during|in case of|upon)\b(?!.*\d)"),
    ("origin", r"\bwhere (does|do|did) .* (come from|originate|get (its|their) value)|\borigin of\b|\bsource of\b|"
               r"\bcomes? from\b|\bwhere .* (get|gets) (set|assigned|written|its value)"),
    ("receives", r"\b(which|what) (function|method|code|handler)s? (first )?(receives?|gets?|handles?|accepts?|takes?)\b|"
                 r"\breceives? the\b|\bentry point for\b"),
    ("flow", r"\btrace\b.*\bfrom\b|\bhow does .* (reach|get to|end up|arrive|make it to)\b|\bfrom .* to (where|the)\b.*"
             r"\b(used|goes)\b"),
    ("where_computed", r"\bwhere (is|are|does|do) .* (calculated|computed|derived|determined|updated|calculate|compute|filled|"
                       r"incremented|decremented|increased|decreased|reset|cleared|toggled|counted|advanced|"
                       r"written|stored|populated|modified|assigned|set|changed)\b|"
                       r"\bhow (is|are) .* (calculated|computed|derived|determined)\b|\bwhich (function|code) (calculates|computes)\b"),
    ("downstream", r"\b(called|invoked|executed|run|happens?) after\b|\bafter .* (returns|finishes|runs|is called)\b|"
                   r"\bcallees? of\b|\bwhat does .* call\b|\bwhich functions does .* call\b|\bdownstream\b"),
    ("entries", r"\b(which|what|list|all|how many)\b[^?]*\b(threads?|tasks?|interrupts?|isrs?|interrupt handlers?|"
                r"entry points?|timers?|callbacks?|event handlers?|background (jobs?|workers?))\b|\bwhere does (the )?"
                r"(program|application|execution|it) (start|begin)\b|\bwhat runs (in parallel|concurrently|at startup)\b"),
    ("which_fn", r"\b(which|what) (function|method|routine|code)s? (\w+s|\w+es)\b|"
                 r"^\s*how (is|are|does|do) (the |a |an )?[\w\s-]{2,40}? (verified|checked|validated|parsed|encoded|decoded|"
                 r"serialized|deserialized|converted|formatted|sent|transmitted|stored|saved|loaded|initiali[sz]ed|scheduled|"
                 r"allocated|freed|locked|unlocked|synchroni[sz]ed)\b"),
    ("callers", r"\bwho calls\b|\bhow often\b|\bwhat calls\b|\bcallers? of\b|\bwhere is .* (called|invoked|used)\b|\bcalled (from|by)\b|"
                r"\bwho uses\b|\bwhere .* used\b"),
    ("influence", r"\b(which|what) (variables?|inputs?|values?|factors?|things?|parameters?|state) (can )?(affect|influence|"
                  r"determine|change|impact|feed|control)\b|\bwhat does .* depend on\b|\bdepends? on\b"),
    ("flow", r"\bdata ?flow\b|\bflows?\b|\btravels?\b|\bpipeline\b|\bpath of\b|\bthrough (this|the) (app|application|system|"
             r"code|program)\b|\btrace\b|\bend.to.end\b|\bjourney\b|\bgoes through\b|\bmoves through\b"),
    ("purpose", r"\bpurpose\b|\brole of\b|\bwhat is .* (for|used for)\b|\bwhy (is|does) .* exist\b|\bmeaning of\b|\bused for\b"),
    ("explain", r"\bwhat (does|do|is|are)\b|\bexplain\b|\bdescribe\b|\bhow does\b|\btell me about\b|\bsummar"),
]
INTENT_LIST = ["entries", "which_fn", "triggers", "flow", "origin", "receives", "where_computed", "failure_invalid", "failure_missing", "downstream", "callers",
               "influence", "scenario", "impact", "purpose", "explain", "search"]
SCEN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_ .>-]{0,40}?)\s*(?:=|==|:|\bis\b|\bof\b|\bat\b|\bto\b)?\s*"
                     r"(-?\d+(?:\.\d+)?|true|false|yes|no|valid|invalid)\b", re.I)


@dataclass
class Route:
    intent: str
    question: str
    mentions: list[dict] = field(default_factory=list)       # explicit code identifiers
    topic: list[str] = field(default_factory=list)           # content words
    inputs: dict[str, Any] = field(default_factory=dict)     # scenario inputs
    by: str = "rules"

    def as_dict(self) -> dict[str, Any]:
        return {"intent": self.intent, "mentions": self.mentions, "topic": self.topic, "inputs": self.inputs, "by": self.by}


NUM_NAME_RE = re.compile(r"\b(-?\d+(?:\.\d+)?)\s+([A-Za-z_][A-Za-z0-9_]{2,})\b")


def parse_inputs(q: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for m in NUM_NAME_RE.finditer(q):                   # "3 satellites", "80 knots"
        name = m.group(2).lower()
        if name in STOP or name in ("times", "seconds", "ms", "minutes", "percent", "degrees", "knots", "meters"):
            continue
        out[name] = float(m.group(1)) if "." in m.group(1) else int(m.group(1))
    for m in SCEN_RE.finditer(q):
        name, val = m.group(1).strip(), m.group(2).lower()
        words = [w for w in re.split(r"[^A-Za-z0-9_]+", name) if w]
        words = [w for w in words if w.lower() not in STOP and w.lower() not in ("sends", "send", "gets", "is", "has",
                                                                                 "equal", "equals", "set", "when", "if")]
        if not words:
            continue
        key = words[-1]
        if val in ("true", "yes"):
            v: Any = True
        elif val in ("false", "no"):
            v = False
        elif val in ("valid",):
            v = True
            key = key if key.lower() != "is" else "valid"
        elif val in ("invalid",):
            v = False
        else:
            v = float(val) if "." in val else int(val)
        out[key] = v
    # "valid true" / "valid is true" / "invalid" phrasing
    if re.search(r"\binvalid\b", q, re.I) and "valid" not in out:
        out["valid"] = False
    return out


def topic_words(q: str) -> list[str]:
    words = []
    acronyms = {t.lower() for t in re.findall(r"\b[A-Z]{2}\b", q)}      # IP, IO, RX, TX: short but meaningful
    acronyms |= {"id", "io", "ip", "rx", "tx", "fd", "hw", "sw", "cb", "ms"} & {w.lower() for w in re.findall(r"\b\w+\b", q)}
    for w in split_ident(q):
        if w in STOP and w not in acronyms or (len(w) < 3 and w not in acronyms) or w.isdigit():
            continue
        if w in ("happens", "happen", "affected", "affect", "change", "branch", "execute", "executes", "called", "calls",
                 "function", "functions", "variable", "variables", "receives", "receive", "calculated", "come", "comes",
                 "invalid", "unavailable", "disappears", "becomes", "travels", "application", "flow", "final", "else",
                 "might", "purpose", "sends", "true", "false", "which", "after", "through", "explain", "data",
                 "cannot", "unable", "able", "while", "during", "whilst"):
            continue
        if w not in words:
            words.append(w)
    return words


def route(question: str, searcher, llm=None) -> Route:
    q = question.strip()
    ql = q.lower()
    intent = None
    for name, pat in INTENTS:
        if re.search(pat, ql):
            intent = name
            break
    mentions = searcher.resolve_mentions(q)
    r = Route(intent or "search", q, mentions, topic_words(q))
    if intent is None and NUM_NAME_RE.search(q) and re.search(r"\b(what happens|what if|when)\b", ql):
        intent = "scenario"
        r.intent = "scenario"
    if intent == "scenario" or (intent in ("failure_invalid", "failure_missing") and re.search(r"=\s*-?\d|\b\d+\b", q)):
        r.inputs = parse_inputs(q)
        if r.inputs and intent != "scenario" and len([k for k in r.inputs if k != "valid"]) >= 1:
            r.intent = "scenario"
    if intent is None and llm is not None:
        try:
            schema = {"type": "object", "properties": {"intent": {"type": "string", "enum": INTENT_LIST}},
                      "required": ["intent"], "additionalProperties": False}
            res = llm.chat([{"role": "system", "content":
                             "You route questions about a C/C# codebase to one analysis recipe. flow=how data travels; "
                             "origin=where a value comes from; receives=which function receives some input; "
                             "where_computed=where something is calculated; failure_invalid=what happens on bad/invalid "
                             "data or errors; failure_missing=what happens when input stops/times out; downstream=what runs "
                             "after a function; callers=who calls/uses something; influence=what affects a value; "
                             "scenario=concrete input values; impact=effects of changing code; purpose=why something "
                             "exists; explain=what something does; search=find code."},
                            {"role": "user", "content": q}], schema=schema, max_tokens=60, reasoning="low")
            it = (res.get("json") or {}).get("intent")
            if it in INTENT_LIST:
                r.intent, r.by = it, "model"
        except Exception:  # noqa: BLE001 — routing falls back to search
            pass
    if r.intent == "search" and mentions:
        r.intent = "explain"
    return r
