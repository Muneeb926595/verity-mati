"""Symbol cards (design §15): one compact, factual text per symbol, built only from facts (name, kind, signature,
doc comment, location, callers/callees, reads/writes). Cards feed the identifier-aware FTS5 index and — when the local
embedding model is available — vectors. Embeddings are a locator only: nothing is ever claimed from a vector hit."""
from __future__ import annotations

import hashlib
import re
import threading
from collections import defaultdict
from typing import Any, Callable

import numpy as np

CARD_KINDS = ("function", "method", "constructor", "global", "field", "struct", "union", "enum", "enum_const",
              "macro", "typedef", "class", "interface", "record", "property", "event", "delegate")
_SPLIT1 = re.compile(r"([a-z0-9])([A-Z])")
_SPLIT2 = re.compile(r"([A-Z]+)([A-Z][a-z])")
STOP = {"the", "a", "an", "of", "to", "in", "is", "it", "and", "or", "for", "on", "at", "by", "be", "this", "that",
        "what", "which", "how", "does", "do", "where", "when", "why", "who", "if", "i", "me", "my", "we", "our", "you",
        "your", "are", "was", "were", "can", "could", "would", "should", "will", "with", "from", "into", "about",
        "there", "their", "its", "has", "have", "had", "any", "all", "show", "tell", "explain", "happens", "happen",
        "code", "function", "method", "variable", "value", "data", "c", "g", "s", "k", "get", "set"}


def split_ident(s: str) -> list[str]:
    """ProcessGpsData -> [process, gps, data]; g_nav.last_fix_ms -> [g, nav, last, fix, ms]; HTTPServer -> [http, server]"""
    s = _SPLIT2.sub(r"\1 \2", _SPLIT1.sub(r"\1 \2", s))
    out = []
    for w in re.split(r"[^A-Za-z0-9]+", s):
        if not w:
            continue
        w = w.lower()
        out.append(w)
        m = re.match(r"^([a-z]{2,})\d+$", w)
        if m:
            out.append(m.group(1))            # crc16 -> also crc, gpio31 -> gpio, ip4 -> ip
        if len(w) > 4 and w.endswith("s") and not w.endswith(("ss", "us", "is")) and w.isalpha():
            out.append(w[:-1])                # slaves -> also slave, bytes -> byte
    return out


def segment_word(t: str, vocab: dict[str, int], min_count: int = 3) -> list[str]:
    """Split a run-together lowercase token into 2–3 words that occur on their own in the project (nextmbxcnt ->
    next, mbx, cnt); [] when no confident split exists."""
    best: dict[int, list[str] | None] = {0: []}
    for i in range(1, len(t) + 1):
        cands = []
        for j in range(max(0, i - 12), i - 1):
            w = t[j:i]
            if best.get(j) is not None and len(w) >= 2 and vocab.get(w, 0) >= min_count:
                cands.append(best[j] + [w])
        best[i] = min(cands, key=len) if cands else None
    seg = best.get(len(t))
    return seg if seg and 2 <= len(seg) <= 3 else []


def words_of(text: str) -> list[str]:
    out = []
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text):
        out += split_ident(tok)
    return out


def build(store, embedder: Any = None, cancel: threading.Event | None = None,
          progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    st = store
    cancel = cancel or threading.Event()
    progress = progress or (lambda d, t: None)
    names = {r["key"]: r["name"] for r in st.q("SELECT key, name FROM symbol")}
    callers: dict[str, set[str]] = defaultdict(set)
    callees: dict[str, set[str]] = defaultdict(set)
    for r in st.q("SELECT caller, callee FROM call WHERE callee IS NOT NULL AND callee NOT LIKE 'name:%'"):
        if r["caller"] in names and r["callee"] in names:
            callers[r["callee"]].add(names[r["caller"]])
            callees[r["caller"]].add(names[r["callee"]])
    writers: dict[str, set[str]] = defaultdict(set)
    readers: dict[str, set[str]] = defaultdict(set)
    touches: dict[str, set[str]] = defaultdict(set)
    for r in st.q("SELECT sym, role, fn FROM occ WHERE fn IS NOT NULL AND role IN ('read','write','rw')"):
        fn = names.get(r["fn"])
        if not fn:
            continue
        (writers if r["role"] in ("write", "rw") else readers)[r["sym"]].add(fn)
        sname = names.get(r["sym"])
        if sname:
            touches[r["fn"]].add(sname)
    consts = {r["sym"]: r["value"] for r in st.q("SELECT sym, value FROM constv")}
    # the default build's symbols, plus code that exists only when optional features are switched on (searchable
    # only by answers about that build — see Searcher.variant)
    rows = st.q(f"SELECT * FROM symbol WHERE kind IN ({','.join('?' * len(CARD_KINDS))}) AND file IS NOT NULL "
                f"AND file != '<stub>' AND (cfg NOT LIKE '+%' OR cfg = '+ALL_FEATURES')", CARD_KINDS)
    old = {r["sym"]: r["hash"] for r in st.q("SELECT sym, hash FROM card")}
    cards = []
    for r in rows:
        k = r["key"]
        parts = [f"{r['kind']} {r['qual'] or r['name']}"]
        if r["sig"] and r["kind"] in ("function", "method", "constructor"):
            parts.append(f"signature: {r['sig']}")
        elif r["type"] and r["kind"] in ("global", "field", "property"):
            parts.append(f"type: {r['type']}")
        if r["doc"]:
            parts.append(f"comment: {r['doc']}")
        if str(r["cfg"] or "").startswith("+"):
            parts.append("only in builds with optional features switched on")
        if k in consts:
            parts.append(f"value: {consts[k]}")
        parts.append(f"defined in {r['file']}:{r['line']}")
        if callers.get(k):
            parts.append("called by: " + ", ".join(sorted(callers[k])[:12]))
        if callees.get(k):
            parts.append("calls: " + ", ".join(sorted(callees[k])[:16]))
        if writers.get(k):
            parts.append("written by: " + ", ".join(sorted(writers[k])[:12]))
        if readers.get(k):
            parts.append("read by: " + ", ".join(sorted(readers[k])[:12]))
        if touches.get(k):
            parts.append("uses: " + ", ".join(sorted(touches[k])[:20]))
        text = "\n".join(parts)
        h = hashlib.sha1(text.encode()).hexdigest()[:16]
        cards.append((k, r["name"], r["qual"] or r["name"], text, h))
    # run-together names (nextmbxcnt, slavecount): split them into words the project uses on their own elsewhere
    vocab: dict[str, int] = defaultdict(int)
    for n in names.values():
        for w in set(split_ident(n)):
            vocab[w] += 1
    seg_cache: dict[str, list[str]] = {}

    def extra_words(n: str) -> list[str]:
        out = []
        for t in split_ident(n):
            if len(t) >= 6 and vocab.get(t, 0) <= 2 and t.isalpha():
                if t not in seg_cache:
                    seg_cache[t] = segment_word(t, vocab)
                out += seg_cache[t]
        return out
    live = {c[0] for c in cards}
    with st._lock:
        c = st.conn
        c.execute("BEGIN")
        c.execute("DELETE FROM card_fts")
        for k in set(old) - live:
            c.execute("DELETE FROM card WHERE sym=?", (k,))
            c.execute("DELETE FROM vec WHERE sym=?", (k,))
        c.executemany("INSERT OR REPLACE INTO card VALUES(?,?,?)", [(k, t, h) for k, _, _, t, h in cards])
        c.executemany("INSERT INTO card_fts(sym, name, words, body) VALUES(?,?,?,?)",
                      [(k, f"{n} {q}", " ".join(split_ident(q) + split_ident(n) + extra_words(n)), " ".join(words_of(t)))
                       for k, n, q, t, h in cards])
        c.execute("COMMIT")
    stats: dict[str, Any] = {"cards": len(cards), "embedded": 0, "vectors": "off"}
    if embedder is not None:
        have = {r["sym"]: r["hash"] for r in st.q("SELECT sym, hash FROM vec WHERE model=?", (embedder.model_id,))}
        todo = [(k, t, h) for k, _, _, t, h in cards if have.get(k) != h]
        B = 32
        try:
            for i in range(0, len(todo), B):
                if cancel.is_set():
                    break
                batch = todo[i:i + B]
                vecs = embedder.embed([t for _, t, _ in batch])
                with st._lock:
                    st.conn.execute("BEGIN")
                    st.conn.executemany("INSERT OR REPLACE INTO vec VALUES(?,?,?,?,?)",
                                        [(k, embedder.model_id, int(v.shape[0]), v.astype(np.float32).tobytes(), h)
                                         for (k, _, h), v in zip(batch, vecs)])
                    st.conn.execute("COMMIT")
                stats["embedded"] += len(batch)
                progress(min(i + B, len(todo)), len(todo))
            stats["vectors"] = "on"
        except Exception as e:  # noqa: BLE001 — embeddings are optional; keyword search always works
            stats["vectors"] = f"unavailable ({type(e).__name__})"
    return stats


def describe(stats: dict[str, Any]) -> str:
    s = f"Indexed {stats.get('cards', 0)} symbols for search"
    if stats.get("vectors") == "on":
        s += " (keyword + meaning search)"
    else:
        s += " (keyword search; meaning search off)"
    return s
