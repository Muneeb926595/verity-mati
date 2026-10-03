"""The learning operations for one project, shared by every client (engine JSON-RPC, CLI, MCP).

Every operation validates its input and refuses with a plain-language LearnError: a pick must be one of the
alternatives the answer offered, a line must be a line of the indexed project's code, and so on."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .. import config
from ..retrieval.cards import STOP, split_ident
from ..retrieval.search import expand_words
from .anchor import _line, _lines, file_unchanged, locate, reanchor, resolve_line
from .apply import Learning, day
from .store import LearnError, LearnStore
from .text import norm_code, qnorm, short

EXPORT_VERSION = 1
CONST_RE = re.compile(r"\b(?:[A-Z][A-Z0-9]*_[A-Z0-9_]+|[a-z]{1,3}[A-Z][A-Za-z0-9_]{2,}|E[A-Z0-9]{3,})\b(?!\s*\()")
SUCCESS_RE = re.compile(r"ERR_?NONE\b|\bNO_?ERR(OR)?\b|_OK\b|^OK$|SUCCESS|ERR_?OK\b|\bE_?OK\b|^pdPASS$|^pdTRUE$", re.I)
# words that say what kind of event the question is about, not what the code calls things: never glossary terms
NOT_TERMS = {"wrong", "bad", "invalid", "corrupt", "corrupted", "malformed", "incorrect", "broken", "illegal", "unknown",
             "fail", "fails", "failed", "failing", "failure", "error", "errors", "happen", "happens", "happened", "full",
             "empty", "too", "long", "short", "large", "small", "big", "many", "few", "much", "more", "less", "lost",
             "missing", "timeout", "time", "times", "out", "zero", "negative", "null", "none", "busy", "late", "stale",
             "rejected", "reject", "rejects", "exceeds", "exceed", "overflow", "overflows", "exist", "exists", "existing",
             "already", "first", "last", "next", "same", "different", "other", "another", "new", "old", "start", "stop",
             "value", "values", "data", "thing", "things", "something", "anything", "code", "program", "called", "call"}
TERM_RE = re.compile(r"^[a-z0-9][a-z0-9 _./+#-]{0,47}$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:]{0,119}$")


def norm_term(term: str) -> str:
    return " ".join((term or "").lower().split())


class Learner:
    def __init__(self, fs, root: str | Path, data_dir: str | Path):
        self.fs = fs
        self.root = Path(root)
        self.store = LearnStore(Path(data_dir) / "learned.db")
        self._anchor_lock = threading.Lock()          # one re-check at a time (the index thread and an answer)

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------------------------------ answering
    def refresh(self, force: bool = False) -> dict:
        with self._anchor_lock:
            return reanchor(self.store, self.fs, self.root, force=force)

    def for_question(self, route, question: str, qvec: list[float] | None = None,
                     embed: Callable[[str], list[float] | None] | None = None) -> Learning:
        try:
            self.refresh()
        except Exception:  # noqa: BLE001 — a learning problem must never stop an answer
            pass
        if qvec and embed is not None:
            # corrections made while the embedding model was off (or with another one) get their meaning vector now
            for c in self.store.corrections("active"):
                if not c.get("qvec") or len(c["qvec"]) != len(qvec):
                    try:
                        v = embed(c["question"])
                    except Exception:  # noqa: BLE001
                        v = None
                    if v:
                        self.store.update_correction(c["id"], qvec=v)
        qwords = {w.lower() for w in expand_words(set(split_ident(question)) | set(getattr(route, "topic", []) or []))
                  if len(w) > 2} - STOP

        def relevant(c: dict) -> bool:
            # a same-meaning match must also share a word with the corrected code (its function, check or side)
            try:
                words, idents = self._target_words(c)
            except Exception:  # noqa: BLE001
                return False
            code = {w.lower() for w in words} | {w.lower() for i in idents for w in split_ident(i)}
            return bool(qwords & code)
        return Learning.load(self.store, route, question, qvec, relevant)

    def glossary_map(self) -> dict[str, list[str]]:
        """Accepted terms (this machine's, plus the repository's verity.toml [glossary]): term -> identifiers."""
        out: dict[str, list[str]] = {}
        for t in self.store.glossary("accepted"):
            out[t["term"]] = list(t["identifiers"])
        for term, ids in self._toml_glossary().items():
            out.setdefault(term, [])
            out[term] = list(dict.fromkeys(out[term] + ids))
        return out

    def _toml_glossary(self) -> dict[str, list[str]]:
        g = config.load_hints(self.root).get("glossary")
        out: dict[str, list[str]] = {}
        if isinstance(g, dict):
            for k, v in g.items():
                ids = [v] if isinstance(v, str) else [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
                t = norm_term(str(k))
                if t and ids:
                    out[t] = ids
        return out

    # ------------------------------------------------------------------------------------------ answers
    def _answer(self, answer_id: str) -> dict:
        if not answer_id or not re.fullmatch(r"[0-9a-f]{6,40}", str(answer_id)):
            raise LearnError("That is not an answer id (it looks like 3f2a91c0d4be).")
        r = self.fs.one("SELECT result FROM answer WHERE id=?", (answer_id,))
        if not r:
            raise LearnError(f"Answer {answer_id} is not stored any more (answers are kept with the index). Ask the "
                             "question again, then correct the new answer.")
        try:
            return json.loads(r["result"])
        except ValueError:
            raise LearnError(f"Answer {answer_id} could not be read.") from None

    # ------------------------------------------------------------------------------------------ correct
    def correct(self, answer_id: str, pick: int | None = None, target: dict | None = None, note: str = "",
                author: str | None = None, embed: Callable[[str], list[float] | None] | None = None) -> dict:
        ans = self._answer(answer_id)
        meta = ans.get("meta") or {}
        lm = meta.get("learning") or {}
        route = ans.get("route") or {}
        question = ans.get("question") or ""
        note = " ".join(str(note or "").split())[:500]
        cands = lm.get("candidates") or []
        if pick not in (None, "") and target:
            raise LearnError("Give either an alternative to pick or a line, not both.")
        if not lm.get("correctable"):
            raise LearnError("This kind of answer does not hinge on one place in the code, so it cannot be corrected "
                             "by pointing at one. Ask a question about where or how something is decided instead.")
        try:
            self.refresh()                        # older corrections first follow any code that changed
        except Exception:  # noqa: BLE001
            pass
        if pick not in (None, ""):
            try:
                pick = int(pick)
            except (TypeError, ValueError):
                raise LearnError("The pick must be the number of an alternative (2 or 3).") from None
            if pick == 1:
                raise LearnError("Place 1 is the answer shown. If it was right, confirm the answer instead.")
            alts = lm.get("alternatives") or []
            chosen = next((a for a in alts if a.get("rank") == pick), None)
            if chosen is None:
                offered = ", ".join(str(a["rank"]) for a in alts)
                raise LearnError(f"This answer offered no alternative {pick}" +
                                 (f" (it offered {offered})" if offered else " (it offered none)") +
                                 ". Pick one of those, or pick a line in the code instead.")
            self._same_code_as_answer(answer_id, chosen.get("file"))
            line = chosen.get("line")
            if chosen.get("side") == "case" and chosen.get("span"):
                line = chosen["span"][0]          # the case's own label line (every case shares the switch's line)
            elif line is None and chosen.get("fn"):
                # a function (not a check) was offered: its definition line
                row = self.fs.one("SELECT file, line FROM symbol WHERE key=? AND is_def=1 ORDER BY line LIMIT 1",
                                  (chosen["fn"],))
                if row:
                    chosen = dict(chosen, file=row["file"])
                    line = row["line"]
            tgt = resolve_line(self.fs, self.root, chosen.get("file"), line)
            if chosen.get("side") and tgt.get("branch_line"):
                tgt["side"] = chosen["side"]
            if chosen.get("fn") and chosen["fn"] != tgt["target_fn"] and \
                    self.fs.one("SELECT 1 FROM symbol WHERE key=?", (chosen["fn"],)):
                tgt["target_fn"] = chosen["fn"]
            tgt["target_label"] = chosen.get("label") or tgt["target_label"]
            kind = "pick"
        elif target:
            if not isinstance(target, dict):
                raise LearnError("The line must be given as {file, line}.")
            tgt = resolve_line(self.fs, self.root, target.get("file"), target.get("line"))
            kind = "line"
        else:
            raise LearnError("Say which place is right: pick one of the alternatives, or pick a line in the code.")
        wrong = cands[0] if cands else None
        if wrong and _same_place(wrong, tgt):
            raise LearnError("That is the place this answer already describes. If it was right, confirm the answer "
                             "instead.")
        # the person corrected an answer that used a correction for this very question: the new one replaces it (a
        # correction matched by meaning or by topic words was recorded for another question and stays as it is)
        qn = qnorm(question)
        used_pins = []
        for u in lm.get("used") or []:
            old = self.store.correction(u.get("id") or "") if u.get("how") == "pin" else None
            if old and old["status"] == "active" and old.get("qnorm") == qn:
                used_pins.append(u)
        now = time.time()
        qvec = None
        if embed is not None:
            try:
                qvec = embed(question)
            except Exception:  # noqa: BLE001
                qvec = None
        rec = {"created": now, "updated": now, "author": str(author or "")[:80], "note": note,
               "answer_id": answer_id, "question": question, "qnorm": qn,
               "intent": route.get("intent") or meta.get("intent"), "topic": lm.get("topic") or route.get("topic") or [],
               "qvec": qvec, "wrong": {k: wrong.get(k) for k in ("fn", "file", "line", "side", "label")} if wrong else None,
               "kind": kind, "target_fn": tgt["target_fn"], "target_name": tgt["target_name"],
               "target_file": tgt["target_file"], "target_line": tgt["target_line"],
               "branch_line": tgt["branch_line"], "side": tgt["side"], "target_cond": tgt["target_cond"],
               "target_label": tgt["target_label"], "anchor": tgt["anchor"], "branch_anchor": tgt["branch_anchor"],
               "rel_line": tgt["rel_line"], "status": "active", "status_reason": "", "conflict": [],
               "replaces": used_pins[0]["id"] if used_pins else None, "uses": 0, "source": "local"}
        c = self.store.add_correction(rec)
        cid = c["id"]
        for u in used_pins:
            if self.store.update_correction_if(u["id"], "active", status="removed", status_reason=f"replaced by {cid}"):
                self.store.log("replace", u["id"], {"by": cid})
        sugg = self._suggest_glossary(lm.get("topic") or route.get("topic") or [], tgt, cid)
        return {"correction": self.public(c), "glossary_suggestions": sugg, "replaced": [u["id"] for u in used_pins],
                "message": f"Saved correction {cid}. It is used for this question and similar ones on this project, "
                           f"and every answer that uses it says so."}

    def _same_code_as_answer(self, answer_id: str, file: str | None) -> None:
        """An alternative is a place in the code as it was when the answer was made: refuse it if that file changed."""
        r = self.fs.one("SELECT deps FROM answer WHERE id=?", (answer_id,))
        try:
            deps = json.loads(r["deps"]) if r and r["deps"] else {}
        except (ValueError, TypeError):
            deps = {}
        cur = self.fs.one("SELECT sha FROM file WHERE path=?", (file,)) if file else None
        old = deps.get(file) if isinstance(deps, dict) else None
        if old and cur and cur["sha"] and old != cur["sha"]:
            raise LearnError(f"{file} changed since this answer was made. Ask the question again, then correct the new "
                             "answer (or pick a line in the code as it is now).")

    # ------------------------------------------------------------------------------------------ confirm
    def confirm(self, answer_id: str, note: str = "") -> dict:
        ans = self._answer(answer_id)
        meta = ans.get("meta") or {}
        lm = meta.get("learning") or {}
        route = ans.get("route") or {}
        cands = lm.get("candidates") or []
        main = cands[0] if cands else None
        name, file, line, fn = None, None, None, None
        if main:
            fn, file, line = main.get("fn"), main.get("file"), main.get("line")
            row = self.fs.one("SELECT * FROM symbol WHERE key=?", (fn,)) if fn else None
            name = (dict(row)["name"] if row else None) or _fn_name_from_label(main.get("label") or "")
        must = self._must_mention_answer(ans, name, main)
        if not must:
            raise LearnError("This answer names no code the test could check, so it cannot be kept as a test case.")
        kid = f"k-{answer_id[:12]}"
        rec = {"id": kid, "answer_id": answer_id, "created": time.time(), "question": ans.get("question"),
               "intent": route.get("intent") or meta.get("intent"), "topic": lm.get("topic") or route.get("topic") or [],
               "target_fn": fn, "target_name": name, "target_file": file, "target_line": line, "must_mention": must,
               "note": " ".join((note or "").split())[:500], "status": "active"}
        k = self.store.add_confirmation(rec)
        return {"confirmation": k, "message": "Thanks. This answer is kept as a test case for this project "
                                              "(export it with the corrections to run it as a regression test)."}

    def _must_mention_answer(self, ans: dict, name: str | None, main: dict | None) -> list[str]:
        text = " ".join(ans.get("answer") or [])
        must: list[str] = []
        if name and name.lower() in text.lower():
            must.append(name)
        key = None
        src = " ".join([(main or {}).get("text") or ""])
        for m in CONST_RE.findall(src):
            if not SUCCESS_RE.search(m) and m in text and m not in must:
                key = m
                break
        if key is None:
            for m in re.findall(r"`([^`]{3,60})`", text):
                if m not in must and not m.endswith("()"):
                    key = m
                    break
        if key:
            must.append(key)
        if not must:
            names = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\(\)", text)
            must = list(dict.fromkeys(names))[:2]
        return must[:3]

    # ------------------------------------------------------------------------------------------ manage
    def public(self, c: dict | None) -> dict:
        if not c:
            return {}
        d = {k: v for k, v in c.items() if k not in ("qvec",)}
        d["created_label"] = day(c.get("created"))
        return d

    def listing(self, status: str | None = None) -> dict:
        try:
            self.refresh()
        except Exception:  # noqa: BLE001
            pass
        allc = self.store.corrections()
        shown = [c for c in allc if not status or status == "all" or c["status"] == status]
        counts = {s: sum(1 for c in allc if c["status"] == s) for s in ("active", "stale", "removed")}
        gl = self.glossary_list()
        confs = self.store.confirmations("active")
        counts.update(suggested=sum(1 for t in gl if t["status"] == "suggested"),
                      accepted=sum(1 for t in gl if t["status"] == "accepted"), confirmations=len(confs))
        return {"corrections": [self.public(c) for c in shown], "confirmations": confs, "glossary": gl,
                "counts": counts, "recovered_from": self.store.recovered_from,
                "events": self.store.events(50)}

    def _get(self, cid: str) -> dict:
        c = self.store.correction(str(cid or "").strip())
        if not c:
            raise LearnError(f"There is no correction {cid} for this project.")
        return c

    def remove(self, cid: str) -> dict:
        c = self._get(cid)
        if c["status"] == "removed":
            return {"correction": self.public(c), "message": f"{cid} was already undone."}
        c = self.store.update_correction(c["id"], status="removed", status_reason="undone")
        self.store.log("undo", c["id"])
        for o in self.store.corrections():
            # an undone correction no longer disagrees with anything
            if c["id"] in (o.get("conflict") or []):
                self.store.update_correction(o["id"], conflict=[x for x in o["conflict"] if x != c["id"]])
        return {"correction": self.public(c), "message": f"Undid {cid}. Answers no longer use it (restore it any time)."}

    def restore(self, cid: str) -> dict:
        c = self._get(cid)
        if c["status"] != "removed":
            return {"correction": self.public(c), "message": f"{cid} is {c['status']}, not undone."}
        self.store.update_correction(c["id"], status="active", status_reason="restored")
        self.store.log("restore", c["id"])
        self.refresh(force=True)                 # it may have gone stale while it was undone
        c = self.store.correction(c["id"])
        msg = f"Restored {cid}." if c["status"] == "active" else \
            f"Restored {cid}, but the code it points at has changed, so it stays stale: {c['status_reason']}."
        return {"correction": self.public(c), "message": msg}

    def set_note(self, cid: str, note: str) -> dict:
        c = self._get(cid)
        c = self.store.update_correction(c["id"], note=" ".join(str(note or "").split())[:500])
        self.store.log("note", c["id"], {"note": c["note"]})
        return {"correction": self.public(c)}

    def remove_confirmation(self, kid: str) -> dict:
        if not self.store.confirmation(kid):
            raise LearnError(f"There is no confirmation {kid} for this project.")
        self.store.set_confirmation_status(kid, "removed")
        self.store.log("unconfirm", kid)
        return {"ok": True}

    # ------------------------------------------------------------------------------------------ glossary
    def _target_words(self, tgt: dict) -> tuple[set[str], list[str]]:
        """The words of the corrected place's code (function, check, the side's first lines) and its identifiers,
        most distinctive first."""
        lines = _lines(self.root, tgt["target_file"])
        a = tgt.get("branch_line") or tgt["target_line"]
        b = tgt["target_line"] + 3
        if tgt.get("branch_line"):
            br = self.fs.one("SELECT * FROM branch WHERE fn=? AND file=? AND line=?",
                             (tgt["target_fn"], tgt["target_file"], tgt["branch_line"]))
            if br:
                b = max(b, min((br["else_e"] if tgt.get("side") == "else" else br["then_e"]) or b, a + 12))
        code = " ".join(norm_code(_line(lines, n)) for n in range(a, b + 1))
        words = set(split_ident(tgt["target_name"])) | set(split_ident(code))
        words |= {w for s in re.findall(r'"([^"]*)"', code) for w in split_ident(s)}
        idents = [m for m in CONST_RE.findall(re.sub(r'"[^"]*"', " ", code)) if not SUCCESS_RE.search(m)]
        idents = list(dict.fromkeys(idents))
        idents.append(tgt["target_name"])
        return words, idents

    def _suggest_glossary(self, topic: list[str], tgt: dict, cid: str) -> list[dict]:
        """Question words the corrected code does not contain ("bus-off" when the code says ERRTX_BUS_OFF only after
        expansion): suggested as glossary terms. A person accepts or rejects them; nothing applies before that."""
        try:
            words, idents = self._target_words(tgt)
        except Exception:  # noqa: BLE001
            return []
        if not idents:
            return []
        known = {w.lower() for w in words}
        missing = []
        for w in topic or []:
            w = str(w).lower()
            if len(w) < 3 or w in STOP or w in NOT_TERMS or w in missing or w.isdigit():
                continue
            ex = {x.lower() for x in expand_words({w})}
            if ex & known or any(len(k) > 3 and (k.startswith(w) or w.startswith(k)) for k in known):
                continue
            missing.append(w)
        out = []
        existing = {t["term"]: t for t in self.store.glossary()}
        for w in missing[:3]:
            old = existing.get(w)
            if old and old["status"] in ("accepted", "rejected"):
                continue
            t = self.store.put_glossary(w, idents[:3], cid, "suggested")
            self.store.log("suggest", w, {"identifiers": t["identifiers"], "from": cid})
            out.append(t)
        return out

    def glossary_list(self) -> list[dict]:
        out = self.store.glossary()
        mine = {t["term"] for t in out}
        for term, ids in self._toml_glossary().items():
            out.append({"term": term, "identifiers": ids, "source": "verity.toml", "status": "accepted",
                        "read_only": True, "created": None, "updated": None})
            if term in mine:
                out[-1]["also_local"] = True
        return sorted(out, key=lambda t: ({"suggested": 0, "accepted": 1, "rejected": 2}.get(t["status"], 3), t["term"]))

    def _check_term(self, term: str) -> str:
        t = norm_term(term)
        if not t or not TERM_RE.match(t):
            raise LearnError("A glossary term is a short word or phrase (letters, digits, spaces, - . / + #).")
        return t

    def _check_idents(self, identifiers: list[str] | str | None) -> list[str]:
        if isinstance(identifiers, str):
            identifiers = [x for x in re.split(r"[\s,]+", identifiers) if x]
        ids = [str(x).strip() for x in identifiers or [] if str(x).strip()]
        bad = [x for x in ids if not IDENT_RE.match(x)]
        if bad:
            raise LearnError(f"Not an identifier: {', '.join(bad[:3])}.")
        return list(dict.fromkeys(ids))[:12]

    def glossary_accept(self, term: str, identifiers: list[str] | None = None) -> dict:
        t = self._check_term(term)
        old = self.store.glossary_term(t)
        ids = self._check_idents(identifiers) if identifiers else (old or {}).get("identifiers") or []
        if not ids:
            raise LearnError(f"Say which identifiers '{t}' stands for.")
        g = self.store.put_glossary(t, ids, (old or {}).get("source") or "manual", "accepted")
        self.store.log("accept", t, {"identifiers": g["identifiers"]})
        return {"term": g, "message": f"'{t}' now also finds {', '.join(g['identifiers'][:3])} in this project."}

    def glossary_add(self, term: str, identifiers: list[str] | str) -> dict:
        t = self._check_term(term)
        ids = self._check_idents(identifiers)
        if not ids:
            raise LearnError(f"Say which identifiers '{t}' stands for.")
        g = self.store.put_glossary(t, ids, "manual", "accepted")
        self.store.log("add_term", t, {"identifiers": ids})
        return {"term": g, "message": f"Added '{t}'."}

    def glossary_reject(self, term: str) -> dict:
        t = self._check_term(term)
        old = self.store.glossary_term(t)
        if not old:
            raise LearnError(f"There is no glossary term '{t}'.")
        g = self.store.put_glossary(t, old["identifiers"], old["source"], "rejected")
        self.store.log("reject", t)
        return {"term": g, "message": f"'{t}' will not be suggested again."}

    def glossary_remove(self, term: str) -> dict:
        t = self._check_term(term)
        if not self.store.glossary_term(t):
            if t in self._toml_glossary():
                raise LearnError(f"'{t}' comes from the repository's verity.toml; edit that file to remove it.")
            raise LearnError(f"There is no glossary term '{t}'.")
        self.store.delete_glossary(t)
        self.store.log("remove_term", t)
        return {"ok": True, "message": f"Removed '{t}'."}

    # ------------------------------------------------------------------------------------------ export / import
    def export(self, fmt: str = "json") -> dict:
        fmt = (fmt or "json").lower()
        name = self.root.name
        stamp = time.strftime("%Y%m%d")
        if fmt == "json":
            data = {"verity_learning": EXPORT_VERSION, "project": name, "exported": time.time(),
                    "corrections": [self.public(c) for c in self.store.corrections() if c["status"] != "removed"],
                    "glossary": [t for t in self.store.glossary() if t["status"] in ("accepted", "suggested")],
                    "confirmations": self.store.confirmations("active")}
            return {"format": "json", "filename": f"{name}-verity-learning-{stamp}.json", "data": data}
        if fmt == "bench":
            return {"format": "bench", "filename": f"{name}-verity-learned-suite-{stamp}.json", "data": self.suite()}
        if fmt == "toml":
            acc = [t for t in self.store.glossary("accepted")]
            lines = ["# Accepted Verity glossary terms for this project. Add to verity.toml in the repository root;",
                     "# everyone who indexes the repository then gets them.", "[glossary]"]
            for t in acc:
                lines.append(f"{json.dumps(t['term'])} = {json.dumps(t['identifiers'])}")
            return {"format": "toml", "filename": "verity-glossary.toml", "data": "\n".join(lines) + "\n"}
        raise LearnError("Export as json, bench or toml.")

    def suite(self) -> dict:
        """Active corrections and confirmations as a known-answer suite (bench/run.py format): each case asks the
        question again and must mention the right function and its deciding code."""
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", self.root.name).strip("-").lower() or "project"
        cases = []
        for c in self.store.corrections("active"):
            must = [c["target_name"]]
            key = self._key_token(c)
            if key:
                must.append(key)
            cases.append({"id": f"learned-{c['id']}", "project": name, "category": "correction", "q": c["question"],
                          "must_mention": must, "_target": f"{c['target_file']}:{c['target_line']}",
                          "_note": c.get("note") or ""})
        for k in self.store.confirmations("active"):
            cases.append({"id": f"confirmed-{k['id']}", "project": name, "category": "confirmation",
                          "q": k["question"], "must_mention": k["must_mention"]})
        return {"_doc": f"Known-answer cases learned on {self.root.name}: every active correction (asked again, the "
                        "answer must name the corrected place) and every confirmed answer. Exported by Verity on "
                        f"{time.strftime('%Y-%m-%d')}; run with: python -m verity.bench.run --suite <this file>.",
                "projects": {name: str(self.root)}, "cases": cases}

    def _key_token(self, c: dict) -> str | None:
        try:
            _, idents = self._target_words({**c})
        except Exception:  # noqa: BLE001
            idents = []
        consts = [i for i in idents if i != c["target_name"]]
        cond = c.get("target_cond") or ""
        in_cond = [i for i in consts if i in cond]
        if in_cond:
            return in_cond[0]
        if consts:
            return consts[0]
        if cond and len(cond) <= 60:
            return cond
        return None

    def import_(self, data: Any) -> dict:
        """Corrections, glossary terms and confirmed answers exported on another machine (or another copy of the code).
        Every correction is checked against this copy first and imported active only where its code is found; the
        whole import is one transaction, so a bad file changes nothing."""
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8", errors="replace")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                raise LearnError("That file is not Verity learning data (it is not JSON).") from None
        if not isinstance(data, dict) or data.get("verity_learning") != EXPORT_VERSION:
            raise LearnError("That file is not a Verity learning export (verity_learning: 1).")
        if self.fs.meta_get("indexed_at") is None:
            raise LearnError("Index this project first: imported corrections are matched to its code.")
        have = self.store.corrections()
        imported, dup, skipped, stale = [], [], [], []
        rows: list[dict] = []
        seen: set[tuple] = set()
        for c in _list(data.get("corrections")):
            rec, why = _clean_correction(c)
            if rec is None:
                skipped.append(f"{_ref(c)} ({why})")
                continue
            key = (qnorm(rec["question"]), rec["target_fn"], rec.get("branch_line") or rec.get("target_line"))
            if key in seen or any(h["status"] != "removed" and (h.get("qnorm"), h["target_fn"],
                                  h.get("branch_line") or h.get("target_line")) == key for h in have):
                dup.append(_ref(c))
                continue
            seen.add(key)
            if not self.fs.one("SELECT 1 FROM file WHERE path=?", (rec["target_file"],)):
                skipped.append(f"{_ref(c)} ({rec['target_file']} is not part of this project)")
                continue
            try:
                ok, changes, reason = locate(self.fs, self.root, rec)
            except Exception as e:  # noqa: BLE001
                ok, changes, reason = False, {}, f"could not be checked ({type(e).__name__})"
            now = time.time()
            rec.update(changes)
            rec.update(created=rec.get("created") or now, updated=now, qnorm=qnorm(rec["question"]), answer_id=None,
                       conflict=[], uses=0, source="import", _event="import",
                       status="active" if ok else "stale",
                       status_reason=f"imported (was {_ref(c)})" + ("" if ok else f"; here {reason}"))
            rows.append(rec)
        confs = []
        for k in _list(data.get("confirmations")):
            if isinstance(k, dict) and isinstance(k.get("id"), str) and isinstance(k.get("question"), str) and \
                    isinstance(k.get("must_mention"), list) and not self.store.confirmation(k["id"]):
                confs.append({"id": k["id"][:40], "answer_id": None, "created": _num(k.get("created")) or time.time(),
                              "question": k["question"][:2000], "intent": _str(k.get("intent")),
                              "topic": [str(x) for x in _list(k.get("topic"))][:30],
                              "target_fn": _str(k.get("target_fn")), "target_name": _str(k.get("target_name")),
                              "target_file": _str(k.get("target_file")), "target_line": _int(k.get("target_line")),
                              "must_mention": [str(x)[:200] for x in k["must_mention"]][:5],
                              "note": _str(k.get("note"))[:500], "status": "active"})
        terms, kept = [], []
        local = {t["term"]: t for t in self.store.glossary()}
        for t in _list(data.get("glossary")):
            try:
                term = self._check_term(t.get("term"))
                ids = self._check_idents(t.get("identifiers"))
            except (LearnError, AttributeError, TypeError):
                continue
            if not ids:
                continue
            old = local.get(term)
            status = "accepted" if t.get("status") == "accepted" else "suggested"
            if old and (old["status"] == "rejected" or old["status"] == "accepted" and status != "accepted"):
                kept.append(term)                 # never undo a decision made on this machine
                continue
            terms.append((term, ids, status))
        with self.store.tx():
            for rec in rows:
                c = self.store.add_correction(rec)
                imported.append(c["id"])
                if c["status"] == "stale":
                    stale.append(c["id"])
            for k in confs:
                self.store.add_confirmation(k)
            for term, ids, status in terms:
                self.store.put_glossary(term, ids, "import", status)
            self.store.log("import", "", {"imported": imported, "duplicates": dup, "stale": stale,
                                          "terms": [t[0] for t in terms], "confirmations": len(confs)})
        return {"imported": imported, "duplicates": dup, "skipped": skipped, "stale": stale,
                "glossary": [t[0] for t in terms], "confirmations": len(confs),
                "message": f"Imported {len(imported)} correction(s)" + (f", {len(stale)} stale here because the code "
                                                                       "differs" if stale else "") +
                           (f", {len(dup)} already here" if dup else "") +
                           (f", {len(skipped)} skipped" if skipped else "") +
                           (f", {len(terms)} glossary term(s)" if terms else "") +
                           (f", {len(confs)} confirmed answer(s)" if confs else "") + "."}


def _list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _str(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def _ref(c: Any) -> str:
    return str(c.get("id"))[:40] if isinstance(c, dict) and c.get("id") else "an entry"


def _clean_correction(c: Any) -> tuple[dict | None, str]:
    """An exported correction with every field of the right type, or (None, why not)."""
    if not isinstance(c, dict):
        return None, "not a correction"
    for k in ("question", "target_fn", "target_file", "anchor"):
        if not isinstance(c.get(k), str) or not c[k].strip():
            return None, f"no {k}"
    if _int(c.get("target_line")) is None:
        return None, "no target_line"
    side = c.get("side") if c.get("side") in ("then", "else", "case") else None
    rec = {"question": c["question"][:2000], "target_fn": c["target_fn"][:400],
           "target_file": c["target_file"].replace("\\", "/")[:400], "anchor": c["anchor"][:64],
           "target_line": c["target_line"], "branch_line": _int(c.get("branch_line")), "side": side,
           "rel_line": c["rel_line"] if isinstance(c.get("rel_line"), int) and not isinstance(c.get("rel_line"), bool)
           else 0, "branch_anchor": _str(c.get("branch_anchor"))[:64] or None,
           "note": " ".join(_str(c.get("note")).split())[:500], "intent": _str(c.get("intent"))[:40] or None,
           "topic": [str(x)[:60] for x in _list(c.get("topic")) if isinstance(x, (str, int))][:30],
           "kind": c.get("kind") if c.get("kind") in ("pick", "line") else "line",
           "target_name": _str(c.get("target_name"))[:200], "target_cond": _str(c.get("target_cond"))[:2000] or None,
           "target_label": _str(c.get("target_label"))[:400], "author": _str(c.get("author"))[:80],
           "created": _num(c.get("created")),
           "wrong": {k: v for k, v in c["wrong"].items() if k in ("fn", "file", "line", "side", "label") and
                     isinstance(v, (str, int, type(None)))} if isinstance(c.get("wrong"), dict) else None}
    if rec["branch_line"] and not rec["branch_anchor"]:
        return None, "no branch_anchor"
    return rec, ""


def _same_place(cand: dict, tgt: dict) -> bool:
    """The picked place is the one the answer already gave (nothing to correct)."""
    if cand.get("fn") != tgt["target_fn"]:
        return False
    cl = cand.get("line")
    if cl is None:
        return True                     # the answer named this function: a line inside it changes nothing
    if tgt.get("branch_line"):
        side = tgt.get("side") or "then"
        if cl != tgt["branch_line"] or (cand.get("side") or "then") != side:
            return False
        span = cand.get("span")
        if side == "case" and span and tgt["target_line"] != tgt["branch_line"]:
            return span[0] <= tgt["target_line"] <= span[1]
        return True
    return cl == tgt["target_line"]


def _fn_name_from_label(label: str) -> str | None:
    m = re.match(r"\s*([A-Za-z_][\w.]*)\(\)", label or "")
    return m.group(1).split(".")[-1] if m else None


def summarize(lst: dict) -> str:
    """One line for the CLI: what this project has learned."""
    c = lst["counts"]
    parts = [f"{c['active']} active correction(s)"]
    if c.get("stale"):
        parts.append(f"{c['stale']} stale")
    if c.get("accepted") or c.get("suggested"):
        parts.append(f"{c.get('accepted', 0)} glossary term(s)" + (f", {c['suggested']} suggested" if c.get("suggested")
                                                                    else ""))
    if c.get("confirmations"):
        parts.append(f"{c['confirmations']} confirmed answer(s)")
    return ", ".join(parts)


__all__ = ["Learner", "LearnError", "summarize", "short"]
