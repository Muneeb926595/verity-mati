"""Evidence packs and claims (design §16/§17). Every statement in an answer is a Claim that cites Evidence items;
each Evidence item is a concrete source location (file:line) with the exact source text captured at answer time.
Statuses: CONFIRMED (compiler-grade fact), OBSERVED (seen in a sandboxed run), INFERENCE (derived with an
approximation), RUNTIME-DEPENDENT (depends on values only known when the program runs), UNKNOWN."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CONFIRMED = "CONFIRMED"
OBSERVED = "OBSERVED"
INFERENCE = "INFERENCE"
RUNTIME = "RUNTIME-DEPENDENT"
UNKNOWN = "UNKNOWN"
STATUS_ORDER = [CONFIRMED, OBSERVED, INFERENCE, RUNTIME, UNKNOWN]
PLAIN_STATUS = {CONFIRMED: "Confirmed in the code", OBSERVED: "Observed by running the code",
                INFERENCE: "Inferred (likely, not certain)", RUNTIME: "Depends on runtime values",
                UNKNOWN: "Could not be determined"}


def weaker(a: str, b: str) -> str:
    """The less certain of two statuses (OBSERVED and CONFIRMED are both strong; a run beats nothing but a proof)."""
    return a if STATUS_ORDER.index(a) >= STATUS_ORDER.index(b) else b


@dataclass
class Evidence:
    id: str
    kind: str                 # def call write read flow branch io entry run macro absence
    file: str | None
    line: int | None
    text: str                 # the source line (captured) or run output
    sym: str | None = None
    note: str = ""
    status: str = CONFIRMED
    end_line: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "file": self.file, "line": self.line, "end_line": self.end_line,
                "text": self.text, "sym": self.sym, "note": self.note, "status": self.status}


@dataclass
class Claim:
    text: str                 # plain-language statement (may contain identifiers)
    pred: str                 # machine-checkable predicate name
    args: list[Any]
    ev: list[str]             # evidence ids
    status: str = CONFIRMED
    why: str = ""             # why this status (shown in "verification")
    verified: bool | None = None
    check: str = ""           # what the verifier did

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "pred": self.pred, "args": self.args, "ev": self.ev, "status": self.status,
                "why": self.why, "verified": self.verified, "check": self.check}


@dataclass
class Pack:
    evidence: dict[str, Evidence] = field(default_factory=dict)
    claims: list[Claim] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    flow: list[dict[str, Any]] = field(default_factory=list)          # "How the data flows" steps
    path: list[dict[str, Any]] = field(default_factory=list)          # "Show execution path" steps
    related: list[dict[str, Any]] = field(default_factory=list)       # "Show related code"
    notes: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    run: dict[str, Any] | None = None
    _index: dict[tuple, str] = field(default_factory=dict)

    def add(self, kind: str, file: str | None, line: int | None, text: str, sym: str | None = None, note: str = "",
            status: str = CONFIRMED, end_line: int | None = None) -> str:
        k = (kind, file, line, sym, note if kind in ("run", "absence") else "")
        if k in self._index:
            return self._index[k]
        eid = f"E{len(self.evidence) + 1}"
        self.evidence[eid] = Evidence(eid, kind, file, line, text, sym, note, status, end_line)
        self._index[k] = eid
        return eid

    def claim(self, text: str, pred: str, args: list[Any], ev: list[str], status: str = CONFIRMED, why: str = "") -> Claim:
        c = Claim(text, pred, args, [e for e in ev if e], status, why)
        self.claims.append(c)
        return c

    def overall(self) -> str:
        if not self.claims:
            return UNKNOWN
        sts = [c.status for c in self.claims if c.verified is not False]
        if not sts:
            return UNKNOWN
        return max(sts, key=STATUS_ORDER.index)
