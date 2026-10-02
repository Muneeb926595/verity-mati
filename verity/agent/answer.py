"""Answer orchestration (design §16): resolve → route → recipe (bounded) → verify → (optional) model rewrite with
grounding → render. The model only rephrases verified facts; with no model (Direct mode, C0) the template answer is
used as-is."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..analysis.graph import CodeGraph
from ..render.render import render
from ..retrieval.search import Searcher
from ..verify.verifier import Verifier
from .evidence import PLAIN_STATUS
from .recipes import note_missing_names, note_variant_build, variant_subjects, RECIPES, Ctx
from .router import route

PROSE_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
                "additionalProperties": False}
PROSE_SYSTEM = (
    "You explain source code to people who may not be programmers. You are given a question and a numbered list of "
    "verified FACTS about the code. Write a SHORT plain-language summary (at most three sentences) that directly answers "
    "the question. The complete facts are shown to the reader right after your summary, so do not try to repeat them "
    "all. Rules: use ONLY the facts; keep every function, variable, file and number exactly as written and wrap code "
    "names in backticks; never add a code name, file, value or behaviour that is not in the facts. Never restate a "
    "condition in your own words and never say which way a check goes unless a fact says it in so many words — if you "
    "mention a condition, copy it exactly, in backticks. Steps listed after 'otherwise' do NOT happen when the check "
    "is true. If a fact says something could not be found, say exactly that; do not turn it into a claim about what the "
    "code does. If a fact is marked INFERENCE or RUNTIME-DEPENDENT, say it is likely or depends on runtime. No advice, no "
    "headings. Reply as JSON: {\"answer\": \"...\"}.")


def _code_names(text: str) -> set[str]:
    names = set()
    for m in re.finditer(r"`([^`]+)`|\b([A-Za-z_][A-Za-z0-9_]*\(\)|[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+|[a-z]+[A-Z][A-Za-z0-9]*|"
                         r"[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*)\b", text):
        tok = (m.group(2) or "").rstrip("()")
        if tok and len(tok) > 2:
            names.add(tok)
    return names


class Answerer:
    def __init__(self, store, root: str | Path, runtime=None, runner=None, embedder=None):
        self.st = store
        self.root = Path(root)
        self.runtime = runtime
        self.runner = runner
        self.searcher = Searcher(store, embedder)
        self.searcher_all = Searcher(store, embedder, variant="+ALL_FEATURES")
        self._graph: CodeGraph | None = None
        self._graphs: dict[str, CodeGraph] = {}
        self._mark = None
        self._lock = threading.Lock()

    def graph(self, config: str = "default") -> CodeGraph:
        """The code graph of the default build, or of a variant build ("+M": switch M on; "+ALL_FEATURES")."""
        with self._lock:
            mark = self.st.meta_get("indexed_at")
            if mark != self._mark:
                self._graphs = {}
                self._graph = None
                self._mark = mark
            if config == "default":
                if self._graph is None:
                    self._graph = CodeGraph(self.st, self.root)
                return self._graph
            if config not in self._graphs:
                self._graphs[config] = CodeGraph(self.st, self.root, config=config)
            return self._graphs[config]

    def _has_variant(self, cfg: str) -> bool:
        return self.st.one("SELECT 1 FROM symbol WHERE cfg=? LIMIT 1", (cfg,)) is not None

    def _run(self, g: CodeGraph, r) -> Ctx:
        cx = Ctx(g, self.searcher_all if g.config != "default" else self.searcher, self.st, r, runner=self.runner)
        cx.llm = None                    # a trial run on the all-features build: no model calls
        try:
            RECIPES.get(r.intent, RECIPES["search"])(cx)
        except Exception:  # noqa: BLE001 — a trial run; the default answer stands
            cx.fell_back = True
        return cx

    def _variant_for(self, g: CodeGraph, r) -> str | None:
        """A question about code that exists only in a variant build (compiled out by default): that build's config."""
        for m in r.mentions:
            if not m.get("explicit"):
                continue                 # an English word that happens to name something ("message") decides nothing
            cands = [c for c in m["candidates"] if c in g.sym]
            if not cands or any(not str(g.sym[c].get("cfg") or "default").startswith("+") for c in cands):
                continue
            cfg = str(g.sym[cands[0]]["cfg"]).split(";")[0]
            if cfg.startswith("+"):
                return cfg
        return None

    def ask(self, question: str, on_progress: Callable[[dict], None] | None = None, cancel: threading.Event | None = None,
            use_llm: bool = True) -> dict[str, Any]:
        t0 = time.monotonic()
        prog = on_progress or (lambda e: None)
        cancel = cancel or threading.Event()
        steps = ["Understanding your question", "Looking through the code", "Checking the answer against the code",
                 "Writing the answer"]

        def p(i: int, detail: str = "") -> None:
            prog({"type": "ask.progress", "step": i + 1, "steps": len(steps), "label": steps[i], "detail": detail,
                  "elapsed_s": round(time.monotonic() - t0, 1)})
            if cancel.is_set():
                raise InterruptedError("cancelled")

        question = (question or "").strip()
        if not question:
            raise ValueError("Please type a question.")
        if len(question) > 2000:
            question = question[:2000]
        state = self.st.meta_get("index_state")
        if state not in ("complete",) and not self.st.one("SELECT 1 FROM symbol LIMIT 1"):
            raise RuntimeError("This project has not been indexed yet.")
        p(0)
        g = self.graph()
        llm = self._llm() if use_llm else None
        r = route(question, self.searcher, llm if llm and llm.available()[0] else None)
        p(1, f"Plan: {r.intent.replace('_', ' ')}")
        variant = self._variant_for(g, r)
        if variant:
            g = self.graph(variant)
        cx = Ctx(g, self.searcher_all if variant else self.searcher, self.st, r, runner=self.runner)
        cx.llm = llm if llm is not None and llm.available()[0] else None      # grounded re-ranking only
        if variant:
            note_variant_build(cx, variant)
        note_missing_names(cx)
        recipe = RECIPES.get(r.intent, RECIPES["search"])
        try:
            recipe(cx)
        except InterruptedError:
            raise
        except Exception as e:  # noqa: BLE001 — a recipe bug must not crash the engine; answer honestly
            import traceback
            cx.pack.unknowns.append(f"The analysis for this kind of question failed ({type(e).__name__}: {e}).")
            cx.pack.notes.append(traceback.format_exc()[-600:])
            if not cx.sent:
                try:
                    RECIPES["search"](cx)
                except Exception:
                    pass
        if not variant and (getattr(cx, "fell_back", False) or not cx.sent) and self._has_variant("+ALL_FEATURES"):
            # nothing specific in the default build: the code may exist only when optional features are switched on
            cx2 = self._run(self.graph("+ALL_FEATURES"), r)
            subj = variant_subjects(cx2, "+ALL_FEATURES") if not getattr(cx2, "fell_back", False) and cx2.sent else []
            if subj:
                cx = Ctx(cx2.g, self.searcher_all, self.st, r, runner=self.runner)
                cx.llm = llm if llm is not None and llm.available()[0] else None
                note_variant_build(cx, "+ALL_FEATURES", subj[0])
                RECIPES.get(r.intent, RECIPES["search"])(cx)
                g = cx.g
        if not cx.sent:
            cx.say("I could not find anything in the code that answers this question.")
        p(2)
        ver = Verifier(self.st, g, self.root)
        report = ver.verify(cx.pack)
        # template answer: drop sentences whose every claim failed verification
        paragraphs = []
        for text, cl in cx.sent:
            if cl and all(cx.pack.claims[i].verified is False for i in cl):
                continue
            paragraphs.append(text)
        template = paragraphs[:]
        prose_meta: dict[str, Any] = {"mode": "direct"}
        # a search-only answer (no recipe could build one) is shown as found: rewording a list of matches invites the
        # model to describe behaviour nobody checked
        weak = bool(getattr(cx, "fell_back", False)) or bool(paragraphs) and paragraphs[0].startswith(
            ("I could not find", "There is no `", "Nothing in this code"))   # "not found" is said exactly, not reworded
        if weak:
            prose_meta = {"mode": "direct", "skipped": "search results are shown as found" if getattr(cx, "fell_back", False)
                          else "a 'not found' answer is shown exactly as checked"}
        if llm is not None and llm.available()[0] and use_llm and paragraphs and not weak:
            p(3, "Using the local AI model to phrase the answer")
            try:
                text, prose_meta = self._rephrase(llm, question, cx, paragraphs, ver)
                if text and prose_meta.get("mode") == "model+facts":
                    paragraphs = [text] + paragraphs
                elif text:
                    paragraphs = [text]
            except Exception as e:  # noqa: BLE001
                why = str(e)
                if "Insufficient Memory" in why or "out of memory" in why.lower():
                    why = "the computer did not have enough free memory for it"
                prose_meta = {"mode": "direct", "error": f"{type(e).__name__}: {e}",
                              "fallback": f"the AI model could not be used ({why[:160]}), so the checked facts are shown"}
        else:
            p(3)
        meta = {"seconds": round(time.monotonic() - t0, 2), "prose": prose_meta, "template": template,
                "intent": r.intent, "model_choices": getattr(cx, "model_choices", 0), "model": (llm.label if llm is not None and str(prose_meta.get("mode", "")).startswith("model") else None)}
        ans = render(question, r.as_dict(), cx.pack, paragraphs, report, meta)
        self._store(question, ans, cx)
        return ans

    def _llm(self):
        if self.runtime is None:
            return None
        return self.runtime.chat_server()

    def _rephrase(self, llm, question: str, cx: Ctx, paragraphs: list[str], ver: Verifier) -> tuple[str, dict]:
        facts = []
        for i, ptxt in enumerate(paragraphs):
            facts.append(f"{i + 1}. {ptxt}")
        sts = {}
        for c in cx.pack.claims:
            if c.verified and c.status != "CONFIRMED":
                sts[c.status] = sts.get(c.status, 0) + 1
        note = ""
        if sts:
            note = "\nSome facts are uncertain: " + ", ".join(f"{v} {k}" for k, v in sts.items()) + \
                   ". Uncertain statements: " + " | ".join(c.text for c in cx.pack.claims if c.verified and c.status in
                                                           ("INFERENCE", "RUNTIME-DEPENDENT"))[:600]
        if cx.pack.assumptions:
            note += "\nAssumptions: " + " | ".join(cx.pack.assumptions)
        msgs = [{"role": "system", "content": PROSE_SYSTEM},
                {"role": "user", "content": f"Question: {question}\n\nFACTS:\n" + "\n".join(facts) + note}]
        res = llm.chat(msgs, schema=PROSE_SCHEMA, max_tokens=700, temperature=0.2, reasoning="low")
        data = res.get("json") or {}
        text = (data.get("answer") or "").strip()
        if not text:
            return "", {"mode": "direct", "error": "model returned no text"}
        g = ver.ground_prose(text, cx.pack, paragraphs, question)
        # coverage: the model may shorten, but must keep the code names the verified answer is about
        # the model may summarise, but must keep the KEY entities: the ones the question names, the ones the first
        # verified paragraph is about, and (for runs) the observed results
        first = re.sub(r"[A-Za-z0-9_/.-]+\.(?:c|h|cs)(?::\d+)?", "", " ".join(paragraphs[:6]))  # paths are not entities
        key = _code_names(first) | {m["text"].rstrip("()") for m in cx.r.mentions if m.get("explicit")}
        # named constants with their values are high-value facts: every one must survive the rewording
        must = set()
        for ptxt in paragraphs:
            for mm in re.finditer(r"\b([A-Z][A-Z0-9]*_[A-Z0-9_]+) = ", ptxt):
                must.add(mm.group(1))
        key |= must
        if cx.pack.run and cx.pack.run.get("runs"):
            assumed = " ".join(cx.pack.assumptions) + " " + json.dumps(cx.pack.run["runs"][-1].get("inputs") or {})
            for v in (cx.pack.run["runs"][-1].get("state") or {}).values():
                num = str(v).split(" ")[0]
                if re.fullmatch(r"-?\d+(\.\d+)?", num) and num not in ("0", "1"):
                    short = num.rstrip("0").rstrip(".") if "." in num else num
                    if not re.search(rf"(?<![\d.]){re.escape(short)}(?![\d])", assumed):
                        key.add(short)
        kept_names = {k for k in key if k.lower() in g["text"].lower()}
        coverage = len(kept_names) / len(key) if key else 1.0
        meta = {"mode": "model", "model": llm.label, "seconds": res.get("seconds"), "tok_per_s": llm.tok_per_s,
                "kept": g["kept"], "total": g["total"], "dropped": g["dropped"], "coverage": round(coverage, 2),
                "omitted": sorted(key - kept_names)[:12]}
        missing_must = sorted(m for m in must if m.lower() not in g["text"].lower())
        if missing_must:
            meta["omitted"] = missing_must + meta["omitted"]
        # code the model quotes must be quoted from the facts: a condition paraphrased inside backticks is a new claim
        facts_txt = re.sub(r"\s+", " ", " ".join(paragraphs))
        bad_quotes = [q for q in re.findall(r"`([^`]{3,})`", g["text"])
                      if re.sub(r"\s+", " ", q).strip().rstrip("()") not in facts_txt]
        if g["total"] == 0 or g["kept"] / g["total"] < 0.7 or len(g["text"]) < 40 or bad_quotes:
            meta["mode"] = "direct"
            meta["fallback"] = ("the model quoted code that is not in the checked facts" if bad_quotes else
                                "the model's wording used names or numbers not found in the evidence") + \
                ", so the checked template answer is shown"
            if bad_quotes:
                meta["bad_quotes"] = bad_quotes[:5]
            return "", meta
        # the model writes a short lead; the complete checked facts always follow it (a summary can simplify, and the
        # reader must be able to see exactly what was verified)
        meta["mode"] = "model+facts"
        meta["note"] = "the AI wording is a short summary; the complete checked facts follow it"
        # the facts are numbered for the model only: "(fact 4)" / "listed in fact 4" mean nothing to the reader
        out = re.sub(r"\s*\((?:see )?facts? \d+(?:\s*(?:,|and|–|-)\s*\d+)*\)", "", g["text"])
        out = re.sub(r"\b(?:in |from )?facts? \d+(?:\s*(?:,|and|–|-)\s*\d+)*", "below", out)
        return out, meta

    def _store(self, question: str, ans: dict, cx: Ctx) -> None:
        files = sorted({e["file"] for e in ans["evidence"] if e.get("file")})
        deps = {f: (self.st.one("SELECT sha FROM file WHERE path=?", (f,)) or {"sha": None})["sha"] for f in files}
        aid = hashlib.sha1(f"{question}{time.time()}".encode()).hexdigest()[:12]
        ans["id"] = aid
        try:
            self.st.exec("INSERT OR REPLACE INTO answer VALUES(?,?,?,?,?)",
                         (aid, question, time.time(), json.dumps(ans, default=str)[:2_000_000], json.dumps(deps)))
        except Exception:
            pass
