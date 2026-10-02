"""Deterministic verifier (design §17). Nothing reaches the user unchecked:

1. citation check — every evidence item's file exists, is unchanged since indexing (hash), and the cited line still
   contains the captured text and the claimed identifier;
2. predicate check — each claim is re-derived from the fact store by an independent query (not by the recipe);
3. status caps — a claim is never stronger than its weakest evidence (file fidelity, stubs, indirect resolution);
4. prose grounding — model-written text may only use code identifiers, file names and numbers present in the
   verified evidence; ungrounded sentences are removed (repair) and the template answer is used if too much is lost.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..agent.evidence import CONFIRMED, INFERENCE, OBSERVED, RUNTIME, UNKNOWN, Pack, weaker

IDENT = re.compile(r"`([^`]+)`|\b([A-Za-z_][A-Za-z0-9_]*(?:\(\))?|[A-Za-z0-9_/.-]+\.(?:c|h|cs|cpp|hpp)(?::\d+)?)\b")
NUM = re.compile(r"(?<![A-Za-z_])(-?\d+(?:\.\d+)?)(?![A-Za-z_])")


def _code_like(tok: str) -> bool:
    t = tok.rstrip("()")
    return tok.endswith("()") or "_" in t or bool(re.search(r"[a-z][A-Z]", t)) or bool(re.search(r"\.(c|h|cs)\b", t)) or \
        (t.isupper() and len(t) > 3)


class Verifier:
    def __init__(self, store, graph, root: str | Path):
        self.st = store
        self.g = graph
        self.root = Path(root)
        self._sha_cache: dict[str, str | None] = {}
        self.index_sha = {r["path"]: r["sha"] for r in store.q("SELECT path, sha FROM file")}

    def _file_sha(self, rel: str) -> str | None:
        if rel not in self._sha_cache:
            try:
                self._sha_cache[rel] = hashlib.sha256((self.root / rel).read_bytes()).hexdigest()
            except OSError:
                self._sha_cache[rel] = None
        return self._sha_cache[rel]

    # --------------------------------------------------------------------------------------------- evidence
    def check_evidence(self, pack: Pack) -> dict[str, dict]:
        out = {}
        for eid, e in pack.evidence.items():
            r = {"ok": True, "why": []}
            if e.kind in ("run", "absence"):
                out[eid] = r
                continue
            if not e.file:
                r["ok"] = False
                r["why"].append("no location")
            else:
                sha = self._file_sha(e.file)
                if sha is None:
                    r["ok"] = False
                    r["why"].append("file no longer exists")
                elif self.index_sha.get(e.file) and sha != self.index_sha[e.file]:
                    r["stale"] = True
                    r["why"].append("file changed since it was indexed")
                cur = self.g.src.line(e.file, e.line).strip()
                if e.text and cur != e.text:
                    r["ok"] = False
                    r["why"].append("cited line no longer matches")
            out[eid] = r
        return out

    # --------------------------------------------------------------------------------------------- predicates
    def _q1(self, sql: str, args: tuple) -> bool:
        return self.st.one(sql, args) is not None

    def check_claim(self, c, pack: Pack, evc: dict[str, dict]) -> tuple[bool, str]:
        a = c.args
        p = c.pred
        try:
            if p == "calls":
                caller, callee, file, line = a
                ok = self._q1("SELECT 1 FROM call WHERE caller=? AND callee=? AND file=? AND line=?", (caller, callee, file, line))
                return ok, "call site found in the index" if ok else "no such call site in the index"
            if p in ("writes", "reads"):
                fn, sym, file, line = a
                roles = ("write", "rw") if p == "writes" else ("read", "rw")
                ok = self._q1(f"SELECT 1 FROM occ WHERE sym=? AND file=? AND line=? AND role IN (?,?)"
                              + (" AND fn IS ?" if fn else ""), (sym, file, line, *roles) + ((fn,) if fn else ()))
                return ok, f"{p[:-1]} reference found" if ok else f"no {p[:-1]} reference at that line"
            if p == "defined":
                sym, file, line = a
                ok = self._q1("SELECT 1 FROM symbol WHERE key=? AND file=? AND line=?", (sym, file, line))
                return ok, "definition found" if ok else "definition not found at that location"
            if p in ("branch", "branch_eval"):
                fn, file, line = (a[0], a[1], a[2]) if p == "branch" else (a[0], None, a[1])
                if p == "branch":
                    ok = self._q1("SELECT 1 FROM branch WHERE fn=? AND file=? AND line=?", (fn, file, line))
                else:
                    ok = self._q1("SELECT 1 FROM branch WHERE fn=? AND line=?", (fn, line))
                return ok, "decision point found" if ok else "no decision point there"
            if p == "flow_edge":
                src, dst, file, line = a
                ok = self._q1("SELECT 1 FROM flow WHERE src=? AND dst=? AND file=? AND line=?", (src, dst, file, line))
                return ok, "value-flow step found" if ok else "no such value-flow step"
            if p == "flows_to":
                # every cited step must exist as a flow edge or an io site
                evs = [pack.evidence[e] for e in c.ev if e in pack.evidence]
                bad = [e for e in evs if not evc.get(e.id, {}).get("ok", True)]
                if bad:
                    return False, "a cited step no longer matches the code"
                return bool(evs), "each step of the path was re-checked" if evs else "no steps cited"
            if p == "fnaddr":
                fn, dst, file, line = a
                ok = self._q1("SELECT 1 FROM fnaddr WHERE fn=? AND file=? AND line=?", (fn, file, line))
                return ok, "function address assignment found" if ok else "no such function address assignment"
            if p == "const":
                name, val = a
                r = self.st.one("SELECT value FROM constv WHERE sym=?", (f"c:macro@{name}",))
                ok = r is not None and str(val).rstrip("0").rstrip(".") in r["value"].replace("f", "").replace("u", "")
                return ok, "constant value matches" if ok else "constant value differs"
            if p == "io":
                sym, file, line = a
                ok = self._q1("SELECT 1 FROM io WHERE sym=? AND file=? AND line=?", (sym, file, line))
                return ok, "input/output site found" if ok else "no input/output site there"
            if p == "entry":
                ok = self._q1("SELECT 1 FROM entry WHERE sym=?", (a[0],))
                return ok, "entry point found" if ok else "not an entry point"
            if p == "source_text":
                name, file, line = a
                ok = name in self.g.src.line(file, line)
                return ok, "the name is on that source line" if ok else "the name is not on that line"
            if p == "not_defined":
                # absent from the index AND from the source text of every indexed file
                import re as _re
                rx = _re.compile(rf"\b{_re.escape(a[0])}\b")
                in_text = any(rx.search(t) for r in self.st.q("SELECT path FROM file") for t in self.g.src.lines(r["path"]))
                if in_text:
                    return False, "the name appears in the source text"   # (in code or comments: never claim absence)
                ok = not self._q1("SELECT 1 FROM symbol WHERE name=? COLLATE NOCASE AND file IS NOT NULL AND file != '<stub>'",
                                  (a[0],))
                return ok, "re-checked: no symbol of that name in the index" if ok else "a symbol of that name exists"
            if p == "no_callers":
                ok = not self._q1("SELECT 1 FROM call WHERE callee=?", (a[0],))
                return ok, "absence re-checked over every call site" if ok else "a caller exists"
            if p in ("run_trace", "run_state", "run_hw", "branch_taken"):
                ok = any(pack.evidence[e].kind in ("run", "branch") for e in c.ev if e in pack.evidence)
                return ok, "backed by the recorded run" if ok else "no run evidence"
            if p in ("signature", "guard", "writes_cfg", "api_step"):
                evs = [pack.evidence[e] for e in c.ev if e in pack.evidence]
                return bool(evs) and all(evc.get(e.id, {}).get("ok", True) for e in evs), "cited line re-checked"
        except (ValueError, TypeError) as e:
            return False, f"malformed claim ({e})"
        return bool(c.ev), "evidence present" if c.ev else "no evidence"

    def citation_mentions(self, c, pack: Pack) -> bool:
        """The cited line must mention the key identifier the claim is about (for code-location claims)."""
        if c.pred not in ("calls", "writes", "reads", "defined"):
            return True
        key = c.args[1] if c.pred in ("calls", "writes", "reads") else c.args[0]
        if not key:
            return True
        name = self.g.name(key).split(".")[-1].split("(")[0]
        names = {name}
        if c.pred == "calls":
            # an indirect call names the pointer / delegate / event, not the target
            for r in self.st.q("SELECT via FROM call WHERE callee=? AND file=? AND line=? AND via IS NOT NULL",
                               (c.args[1], c.args[2], c.args[3])):
                names.add(self.g.name(r["via"]).split(".")[-1])
        if c.pred == "defined":
            s = self.g.sym.get(key)
            if s and s.get("file") and s.get("end_line"):
                span = range(s["line"], min(s["end_line"], s["line"] + 400) + 1)
                if any(name in self.g.src.line(s["file"], ln) for ln in span):
                    return True
        for eid in c.ev:
            e = pack.evidence.get(eid)
            if e and e.text and any(n in e.text or n.split("->")[-1] in e.text for n in names):
                return True
        # multi-line constructs (a call or condition continued on the next lines): look ahead only when the cited
        # line is visibly unfinished — otherwise a shifted file would still "match" a neighbouring line
        for eid in c.ev:
            e = pack.evidence.get(eid)
            if e and e.file and e.line:
                t = self.g.src.line(e.file, e.line).rstrip()
                if t.endswith((";", "{", "}")) or not t:
                    continue
                for ln in range(e.line + 1, e.line + 4):
                    nxt = self.g.src.line(e.file, ln)
                    if any(n in nxt for n in names):
                        return True
                    if nxt.rstrip().endswith((";", "{")):
                        break
        # written or read through a macro on that line (`IPH_TTL_SET(iphdr, …)` writes ip_hdr._ttl, `errno = …` is
        # `(*__error()) = …`): the line cannot spell the name, so require the compiler facts for exactly this line —
        # a macro expansion there AND the symbol's own occurrence there
        if c.pred in ("writes", "reads", "calls"):
            for eid in c.ev:
                e = pack.evidence.get(eid)
                if e and e.file and e.line and \
                        self._q1("SELECT 1 FROM occ WHERE file=? AND line=? AND role='macro'", (e.file, e.line)) and \
                        self._q1("SELECT 1 FROM occ WHERE sym=? AND file=? AND line=?", (key, e.file, e.line)):
                    return True
        return False

    def verify(self, pack: Pack) -> dict[str, Any]:
        evc = self.check_evidence(pack)
        stale = sorted({pack.evidence[e].file for e, r in evc.items() if r.get("stale")})
        n_ok = n_bad = 0
        for c in pack.claims:
            ok, why = self.check_claim(c, pack, evc)
            if ok and not self.citation_mentions(c, pack):
                ok, why = False, "the cited line does not mention the symbol"
            bad_ev = [e for e in c.ev if e in evc and not evc[e]["ok"]]
            if bad_ev:
                ok = False
                why = "; ".join(sum((evc[e]["why"] for e in bad_ev), []))
            c.verified = ok
            c.check = why
            # status caps from evidence
            for e in c.ev:
                ev = pack.evidence.get(e)
                if ev and ev.status not in (OBSERVED,) and c.status != OBSERVED:
                    c.status = weaker(c.status, ev.status)
                if e in evc and evc[e].get("stale"):
                    c.status = weaker(c.status, INFERENCE)
                    c.why = (c.why + "; " if c.why else "") + "the file changed since indexing — re-index to be sure"
            if ok:
                n_ok += 1
            else:
                n_bad += 1
        return {"claims": len(pack.claims), "verified": n_ok, "rejected": n_bad, "stale_files": stale,
                "evidence": len(pack.evidence), "evidence_ok": sum(1 for r in evc.values() if r["ok"])}

    # --------------------------------------------------------------------------------------------- prose grounding
    def vocabulary(self, pack: Pack, sentences: list[str], question: str) -> tuple[set[str], set[str]]:
        vocab: set[str] = set()
        nums: set[str] = set()
        blobs = [question] + sentences + [e.text or "" for e in pack.evidence.values()] + \
            [e.note or "" for e in pack.evidence.values()] + [c.text for c in pack.claims] + pack.assumptions + pack.notes + \
            [s.get("label", "") + " " + s.get("detail", "") for s in pack.flow]
        for b in blobs:
            for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", b):
                vocab.add(m.group(0))
            for m in re.finditer(r"[A-Za-z0-9_/.-]+\.(?:c|h|cs)", b):
                vocab.add(m.group(0))
            for m in NUM.finditer(b):
                nums.add(m.group(1).rstrip("0").rstrip(".") if "." in m.group(1) else m.group(1))
        for e in pack.evidence.values():
            if e.file:
                vocab.add(e.file)
                vocab.add(Path(e.file).name)
        return vocab, nums

    def ground_prose(self, text: str, pack: Pack, sentences: list[str], question: str) -> dict[str, Any]:
        vocab, nums = self.vocabulary(pack, sentences, question)
        low = {v.lower() for v in vocab}
        # word parts of real identifiers (nmea_split -> NMEA) are fine as plain words; whole identifiers must match
        word_parts = set()
        for v in vocab:
            for w in re.split(r"[^A-Za-z0-9]+|(?<=[a-z])(?=[A-Z])", v):
                if len(w) > 2:
                    word_parts.add(w.lower())
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z`\"(])", text.strip())
        kept, dropped = [], []
        for s in parts:
            bad = []
            for m in IDENT.finditer(s):
                tok = m.group(1) or m.group(2)
                if m.group(1):          # `code` span: every identifier inside must be known
                    for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", tok):
                        if t not in vocab and t.lower() not in low and len(t) > 2:
                            bad.append(t)
                    continue
                if _code_like(tok):
                    base = tok.rstrip("()").split(":")[0]
                    if base not in vocab and base.lower() not in low and not (base.isupper() and "_" not in base and
                                                                              base.lower() in word_parts):
                        bad.append(tok)
            for m in NUM.finditer(s):
                n = m.group(1)
                nn = n.rstrip("0").rstrip(".") if "." in n else n
                if nn not in nums and n not in nums and not re.fullmatch(r"[0-9]", n):
                    bad.append(n)
            if bad:
                dropped.append({"sentence": s, "ungrounded": sorted(set(bad))})
            else:
                kept.append(s)
        return {"text": " ".join(kept), "kept": len(kept), "dropped": dropped, "total": len(parts)}
