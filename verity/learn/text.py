"""Small text helpers shared by the learning modules: question matching, line fingerprints, display names."""
from __future__ import annotations

import hashlib
import math
import re
from typing import Iterable

from ..retrieval.cards import STOP, split_ident

FN_KINDS = ("function", "method", "constructor")
# words that say what kind of question it is, not what it is about (the intent already captures the kind)
FRAME_WORDS = {"what", "happens", "happen", "when", "which", "where", "who", "how", "does", "do", "is", "are", "the",
               "a", "an", "if", "it", "its", "this", "that", "code", "program", "function", "will", "would", "get", "gets"}


def qnorm(question: str) -> str:
    """The question as compared for 'the same question': case, punctuation and spacing do not matter."""
    return " ".join(re.sub(r"[^a-z0-9_]+", " ", (question or "").lower()).split())


def stem(w: str) -> str:
    w = w.lower()
    for suf in ("ing", "ies", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf) and not w.endswith(("ss", "us", "is")):
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def stems(words: Iterable[str]) -> set[str]:
    out = set()
    for w in words or ():
        for p in split_ident(str(w)) or [str(w).lower()]:
            p = p.lower()
            if len(p) > 1 and p not in STOP and p not in FRAME_WORDS:
                out.add(stem(p))
    return out


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def norm_code(line: str) -> str:
    """A line's code as fingerprinted: whitespace and a trailing // comment do not matter."""
    t = re.sub(r"//.*$", "", line or "")
    return " ".join(t.split())


def anchor_of(line: str) -> str | None:
    t = norm_code(line)
    return hashlib.sha1(t.encode("utf-8", "replace")).hexdigest()[:16] if t else None


def sym_display(row: dict | None, key: str = "") -> str:
    """How a function is named in answers: `name()` for C, `Class.Method()` for C#."""
    if not row:
        tail = re.split(r"[@.:]", key)[-1] if key else "?"
        return f"{tail}()"
    if (row.get("key") or key)[:2] in ("M:",):
        q = row.get("qual") or row.get("name") or ""
        parts = q.split("(")[0].split(".")
        return (".".join(parts[-2:]) if len(parts) >= 2 else q) + "()"
    return f"{row.get('name')}()"


def short(text: str, n: int = 160) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"
