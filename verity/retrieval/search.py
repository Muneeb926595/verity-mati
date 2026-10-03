"""Hybrid retrieval (design §15): exact identifier match + identifier-aware BM25 (FTS5) + optional vector similarity,
fused with reciprocal-rank fusion. Also resolves the code entities a question mentions ("mention resolution")."""
from __future__ import annotations

import re
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..pathkinds import TEST_WORDS_RE, is_secondary_path, is_test_path  # noqa: F401 (re-exported)
from .cards import STOP, split_ident

SYNONYMS = {
    "receive": ("recv", "rx", "input"), "received": ("recv", "rx", "input"), "receives": ("recv", "rx", "input"),
    "transmit": ("tx", "send"), "send": ("tx", "transmit", "snd"), "message": ("msg",), "buffer": ("buf", "rb"),
    "calculate": ("calc", "compute"), "calculates": ("calc", "compute"), "calculated": ("calc", "compute"),
    "compute": ("calc",), "initialize": ("init",), "initialise": ("init",), "position": ("pos", "lat", "lon"),
    "coordinate": ("coord",), "coordinates": ("coord",), "degree": ("deg",), "degrees": ("deg",),
    "number": ("num", "count"), "count": ("cnt",), "index": ("idx",), "value": ("val",), "length": ("len", "dlc", "size"),
    "pointer": ("ptr",), "error": ("err",), "errors": ("err",), "configuration": ("config", "cfg"),
    "temperature": ("temp",), "velocity": ("vel", "speed"), "speed": ("vel",), "heading": ("hdg", "course"),
    "direction": ("heading", "steer"), "latitude": ("lat",), "longitude": ("lon", "lng"),
    "satellite": ("sat", "satellites"), "satellites": ("sat",), "timestamp": ("ts", "time"), "millisecond": ("ms",),
    "callback": ("cb",), "handler": ("handle",), "steering": ("steer",), "steer": ("steering",), "motor": ("steer",),
    "sentence": ("nmea", "line"), "packet": ("sentence", "frame", "msg", "pkt"), "fill": ("push", "write"),
    "filled": ("push", "write"), "empty": ("pop",), "parse": ("split",), "parses": ("split",), "valid": ("validate",),
    "timeout": ("timeout", "lost"), "lost": ("timeout",), "turn": ("steer",), "light": ("led",),
    # abbreviations C code commonly uses for ordinary words
    "checksum": ("chksum", "cksum", "csum", "crc"), "segment": ("seg",), "segments": ("seg",), "timer": ("tmr", "tim"),
    "timers": ("tmr",), "address": ("addr",), "addresses": ("addr",), "connection": ("conn",), "request": ("req",),
    "response": ("resp", "rsp"), "retransmit": ("rexmit", "retx"), "retransmission": ("rexmit", "rto", "retx"),
    "statistics": ("stats",), "sequence": ("seq",), "acknowledge": ("ack",), "acknowledgement": ("ack",),
    "memory": ("mem",), "allocate": ("alloc",), "allocates": ("alloc",), "allocation": ("alloc",), "window": ("wnd",),
    "source": ("src",), "destination": ("dst", "dest"), "interface": ("netif", "iface", "intf"), "interrupt": ("isr", "irq"),
    "interrupts": ("isr", "irq"), "register": ("reg",), "registers": ("regs", "reg"), "previous": ("prev",),
    "current": ("cur", "curr"), "maximum": ("max",), "minimum": ("min",), "threshold": ("thresh",), "frequency": ("freq",),
    "command": ("cmd",), "commands": ("cmd",), "parameter": ("param",), "parameters": ("params", "param"),
    "argument": ("arg",), "status": ("stat",), "control": ("ctrl", "ctl"), "device": ("dev",), "semaphore": ("sem",),
    "mutex": ("mtx",), "string": ("str",), "character": ("char",), "increment": ("inc", "incr"), "thread": ("task",),
    "threads": ("task",), "task": ("thread",), "tasks": ("thread",), "packets": ("pkt",), "frames": ("frame",),
    "invalidate": ("drop", "clear", "reset", "flush", "discard"), "invalidated": ("drop", "clear", "reset", "flush", "discard"),
    "space": ("spc", "nospc"), "header": ("hdr",), "match": ("cmp", "eq", "equal", "compare"),
    "matches": ("cmp", "eq", "equal"), "mismatch": ("cmp", "neq", "differ"), "compare": ("cmp",),
    "exist": ("find", "found", "lookup", "exists"), "exists": ("find", "found", "lookup"), "existing": ("find", "found"),
    "mailbox": ("mbx", "mbox"), "mailboxes": ("mbx", "mbox"), "counter": ("cnt", "ctr", "count"),
    "counters": ("cnt", "ctr", "count"), "heartbeat": ("hb",), "heartbeats": ("hb",), "object": ("obj",), "objects": ("obj",),
    "program": ("prog",), "programming": ("prog",), "programmed": ("prog",), "erase": ("erase",), "headers": ("hdr",), "ipv4": ("ip4",), "ipv6": ("ip6",), "ip": ("ip4", "ip6"), "ethernet": ("eth", "ether"),
    "already": ("exist", "exists", "dup", "duplicate"), "taken": ("exist", "used", "reserved", "busy"),
    "duplicate": ("dup", "exist", "exists"), "time": ("timeout", "timer", "tmo"), "output": ("out", "write", "ostream", "tx"), "input": ("in", "read", "istream", "rx"), "decoder": ("decode", "dec"), "encoder": ("encode", "enc"), "parser": ("parse",),
    "sender": ("send", "tx"), "receiver": ("receive", "recv", "rx"), "reader": ("read", "rd"), "writer": ("write", "wr"), "answer": ("reply", "response", "resp", "receive", "received"),
    "reply": ("response", "resp", "answer", "receive"), "block": ("blk",), "blocks": ("blk", "blks"), "priority": ("prio", "pri"), "priorities": ("prio", "pri"), "queue": ("q",), "queues": ("q",),
    "delay": ("dly",), "ready": ("rdy",), "owner": ("own",), "owns": ("own", "owner"), "owned": ("own", "owner"),
}


# This project's accepted glossary words while one question is answered (word -> the words of its identifiers). Set by
# the Answerer, per thread and per question, so one project's glossary never leaks into another's answers.
_PROJECT_WORDS: ContextVar[dict[str, frozenset[str]] | None] = ContextVar("verity_project_words", default=None)


def set_project_words(words: dict[str, frozenset[str]] | None) -> Token:
    return _PROJECT_WORDS.set(words or None)


def reset_project_words(token: Token) -> None:
    _PROJECT_WORDS.reset(token)


def expand_words(words) -> set[str]:
    """Question words plus the abbreviations code uses for them (checksum -> chksum, timer -> tmr, …), plus the
    words of this project's accepted glossary terms."""
    out = _expand_words(words)
    pw = _PROJECT_WORDS.get()
    if pw:
        for w in list(out):
            extra = pw.get(w)
            if extra:
                out.update(extra)
    return out


def _expand_words(words) -> set[str]:
    out = set(words)
    for w in list(out):
        out.update(SYNONYMS.get(w, ()))
        # an abbreviation's plural is written as one token too: blk -> blks, buf -> bufs, pkt -> pkts
        out.update(a + "s" for a in SYNONYMS.get(w, ()) if a.isalpha() and 2 < len(a) <= 4 and a[-1] not in "aeiouys"
                   and sum(ch in "aeiou" for ch in a) <= 1)
        # inflections only (forwarded -> forward, packets -> packet); not -er (header is not "head", timer not "tim")
        m = re.match(r"^(\w{4,}?)(ed|ing|ies|es|s)$", w)
        if m and not w.endswith(("ss", "us", "is")):
            st = m.group(1) + ("y" if m.group(2) == "ies" else "")
            out.add(st)
            out.update(SYNONYMS.get(st, ()))
            if m.group(2) in ("ed", "ing", "es") and st[-1] not in "aeiouy":
                for v in (st + "e",) + ((st[:-1],) if len(st) > 4 and st[-1] == st[-2] else ()):
                    out.add(v)                        # created -> create, received -> receive, stopped -> stop
                    out.update(SYNONYMS.get(v, ()))
        # the noun names the verb the code uses: subscription -> subscribe, allocation -> allocate,
        # connection -> connect, acknowledgement -> acknowledge
        for suf, reps in (("ptions", ("be",)), ("ption", ("be",)), ("tions", ("te", "t")), ("tion", ("te", "t")),
                          ("ments", ("",)), ("ment", ("",))):
            if w.endswith(suf) and len(w) - len(suf) >= 4:
                for rep in reps:
                    v = w[: -len(suf)] + rep
                    out.add(v)
                    out.update(SYNONYMS.get(v, ()))
                break
    return out


def _stem(w: str) -> str:
    for suf in ("ations", "ation", "ings", "ing", "ies", "es", "ed", "s", "ers", "er"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w[:6] if len(w) > 8 else w


IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:(?:\.|->|::)[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?")
KIND_BONUS = {"function": 1.0, "method": 1.0, "global": 0.9, "field": 0.8, "property": 0.8, "struct": 0.7,
              "class": 0.7, "macro": 0.6, "enum_const": 0.5, "enum": 0.5, "typedef": 0.3, "param": 0.2, "local": 0.2}


@dataclass
class Hit:
    key: str
    name: str
    kind: str
    file: str | None
    line: int | None
    score: float = 0.0
    why: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "kind": self.kind, "file": self.file, "line": self.line,
                "score": round(self.score, 4), "why": self.why}


class Searcher:
    def __init__(self, store, embedder: Any = None, variant: str | None = None):
        self.st = store
        self.embedder = embedder
        self.variant = variant           # also return symbols that exist only in this variant build ("+ALL_FEATURES")
        self._vec_cache: tuple[Any, list[str], np.ndarray] | None = None

    # ---------------------------------------------------------------------------------------------- pieces
    def exact(self, names: list[str]) -> list[str]:
        out: list[str] = []
        for n in names:
            parts = re.split(r"\.|->|::", n.rstrip("()"))
            base = parts[-1]
            rows = self.st.q("SELECT key, kind, file, is_def, qual FROM symbol WHERE name=? COLLATE NOCASE AND file IS NOT NULL "
                             "AND file != '<stub>' ORDER BY is_def DESC", (base,))
            if len(parts) > 1:
                qualified = [r for r in rows if parts[-2].lower() in (r["qual"] or "").lower().split(".")[-2:][0:1] or
                             f"{parts[-2]}.{base}".lower() in (r["qual"] or "").lower()]
                rows = qualified or rows
            rows = sorted(rows, key=lambda r: (-KIND_BONUS.get(r["kind"], 0.1), 0 if r["is_def"] else 1))
            for r in rows:
                if r["key"] not in out:
                    out.append(r["key"])
        return out

    def fts(self, text: str, k: int = 40) -> list[str]:
        words = [w for w in dict.fromkeys(split_ident(text)) if w not in STOP and len(w) > 1]
        if not words:
            return []
        terms = []
        for w in words[:16]:
            terms.append(f'"{w}"')
            for syn in SYNONYMS.get(w, ()):
                terms.append(f'"{syn}"')
            stem = _stem(w)
            if len(stem) >= 4 and stem != w:
                terms.append(f'"{stem}"*')       # parses -> pars*, coordinates -> coord*
        q = " OR ".join(dict.fromkeys(terms))
        try:
            rows = self.st.q("SELECT sym, bm25(card_fts, 0.0, 10.0, 5.0, 1.0) AS s FROM card_fts WHERE card_fts MATCH ? "
                             "ORDER BY s LIMIT ?", (q, k))
        except Exception:
            return []
        return [r["sym"] for r in rows]

    def vectors(self, text: str, k: int = 40) -> list[str]:
        if self.embedder is None:
            return []
        mark = self.st.meta_get("indexed_at")
        if self._vec_cache is None or self._vec_cache[0] != mark:
            rows = self.st.q("SELECT sym, dim, v FROM vec WHERE model=?", (self.embedder.model_id,))
            if not rows:
                return []
            keys = [r["sym"] for r in rows]
            mat = np.vstack([np.frombuffer(r["v"], dtype=np.float32) for r in rows])
            mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
            self._vec_cache = (mark, keys, mat)
        _, keys, mat = self._vec_cache
        try:
            qv = self.embedder.embed([text], query=True)[0].astype(np.float32)
        except Exception:
            return []
        qv /= np.linalg.norm(qv) + 1e-9
        sims = mat @ qv
        idx = np.argsort(-sims)[:k]
        return [keys[i] for i in idx if sims[i] > 0.2]

    # ---------------------------------------------------------------------------------------------- fused
    def search(self, text: str, k: int = 15, kinds: tuple[str, ...] | None = None) -> list[Hit]:
        mentions = [m.group(0) for m in IDENT_RE.finditer(text)]
        ident_like = [m for m in mentions if _looks_like_identifier(m)]
        # exact name matches count for identifiers written as code, or for a single-word query; several plain words
        # ("dhcp offered address") are ranked by the keyword index as a whole instead of by one word's exact hits
        lists = {"exact": self.exact(ident_like or (mentions if len(mentions) == 1 else [])),
                 "keyword": self.fts(text), "meaning": self.vectors(text)}
        scores: dict[str, float] = {}
        why: dict[str, list[str]] = {}
        weight = {"exact": 3.0, "keyword": 1.0, "meaning": 0.8}
        for name, lst in lists.items():
            for rank, key in enumerate(lst):
                scores[key] = scores.get(key, 0.0) + weight[name] / (20 + rank)
                why.setdefault(key, []).append(name)
        if not scores:
            return []
        keys = list(scores)
        about_tests = bool(TEST_WORDS_RE.search(text))
        meta = {r["key"]: r for r in self._rows(keys)}
        hits = []
        for key in keys:
            r = meta.get(key)
            if r is None:
                continue
            if kinds and r["kind"] not in kinds:
                continue
            vcfg = str(r["cfg"] or "")
            if vcfg.startswith("+") and not (self.variant and vcfg.startswith(self.variant)):
                continue                     # compiled out of the build being answered about
            s = scores[key] * (0.6 + 0.4 * KIND_BONUS.get(r["kind"], 0.1))
            if not about_tests and is_secondary_path(r["file"]):
                s *= 0.3                   # tests / examples / tool models answer "how is it tested", not "how it works"
            hits.append(Hit(key, r["name"], r["kind"], r["file"], r["line"], s, why[key]))
        hits.sort(key=lambda h: -h.score)
        return hits[:k]

    def _rows(self, keys: list[str]) -> list:
        out = []
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            out += self.st.q(f"SELECT key, name, kind, file, line, cfg FROM symbol WHERE key IN ({','.join('?' * len(chunk))})",
                             chunk)
        return out

    def resolve_mentions(self, question: str) -> list[dict[str, Any]]:
        """Code entities the question names explicitly (identifiers), with candidates. Ambiguity is reported."""
        out = []
        seen = set()
        for m in IDENT_RE.finditer(question):
            tok = m.group(0)
            if tok.lower() in STOP or tok in seen:
                continue
            seen.add(tok)
            cands = self.exact([tok])
            if not cands:
                continue
            if not _looks_like_identifier(tok):
                # an ordinary English word that happens to be a symbol name ("heading"): keep only exact-case hits
                cands = [c for c in cands if self._name(c) == tok.rstrip("()")]
                if not cands:
                    continue
            out.append({"text": tok, "candidates": cands[:8], "explicit": _looks_like_identifier(tok)})
        return out

    def _name(self, key: str) -> str | None:
        r = self.st.one("SELECT name FROM symbol WHERE key=?", (key,))
        return r["name"] if r else None


def _looks_like_identifier(tok: str) -> bool:
    t = tok.rstrip("()")
    if t.isupper() and "_" not in t and not any(ch.isdigit() for ch in t) and len(t) <= 5:
        return False                       # an acronym used as a word (TCP, CRC, UART, GPS), unless code spells it so
    return tok.endswith("()") or "_" in t or "." in t or "->" in t or bool(re.search(r"[a-z][A-Z]", t)) or \
        (t.isupper() and len(t) > 2) or bool(re.search(r"[A-Z].*[A-Z]", t[1:]))
