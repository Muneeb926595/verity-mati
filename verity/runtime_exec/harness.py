"""Scenario execution (design §19): slice-and-stub harnesses run in a sandbox, so "what happens when…" questions are
answered by OBSERVING the real code run, not by guessing.

For a target function and input values the harness:
  1. binds the inputs to the target's parameters (fields of a struct parameter are matched by name);
  2. fills values the question does not give with defaults that pass the code's own validation checks
     (and says so as an assumption);
  3. replays the program's own initialisation (the statements of main() before its main loop) by compiling main.c
     with `main` renamed;
  4. stands in for every function the code declares but does not define (hardware, OS) with logging stubs, and
     defines globals whose defining file cannot be compiled;
  5. calls the target, then the functions the main loop calls that read the state the target wrote (with time
     advanced as the scenario requires);
  6. compiles with coverage + call instrumentation, runs under the OS sandbox (no network, writes only to the run
     folder, time-limited), and reads back: functions called in order, branch outcomes, final state, stub calls.
Everything happens in <repo index>/runs/<id>; the user's folder is only read."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .. import config, osutil

TIME_NAME = re.compile(r"(^|_)(now|time|tick|ms|millis|timestamp|t)($|_)", re.I)
TIME_FN = re.compile(r"(tick|millis|micros|time|clock|uptime)", re.I)


class RunError(Exception):
    pass


def _c_type_parts(t: str) -> tuple[str, list[str]]:
    """'void (int16_t, double)' -> ('void', ['int16_t','double'])"""
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", t or "")
    if not m:
        return "int", []
    ret, args = m.group(1).strip(), m.group(2).strip()
    if not args or args == "void":
        return ret, []
    parts, depth, cur = [], 0, ""
    for ch in args:
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
            continue
        depth += ch in "(<["
        depth -= ch in ")>]"
        cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return ret, parts


def _fmt_for(t: str) -> tuple[str, str]:
    t = t.replace("const", "").strip()
    if "*" in t:
        return "%p", "(void*)"
    if t in ("float", "double", "long double"):
        return "%.4f", "(double)"
    if t.startswith("unsigned") or t.startswith("uint") or t in ("size_t", "bool", "_Bool"):
        return "%llu", "(unsigned long long)"
    return "%lld", "(long long)"


def sandbox_available() -> tuple[bool, str]:
    if platform.system() == "Darwin":
        return (bool(shutil.which("sandbox-exec")), "sandbox-exec")
    if shutil.which("bwrap"):
        return True, "bubblewrap"
    return False, "none"


class Runner:
    def __init__(self, store, root: str | Path, timeout_s: float = 10.0):
        self.st = store
        self.root = Path(root).resolve()
        self.timeout = timeout_s
        self.runs_dir = config.repo_dir(self.root) / "runs"
        self.cc = (shutil.which("clang") or shutil.which("cc")) if osutil.mac_dev_tools() else None
        self._compiles: dict[str, bool] = {}

    # ----------------------------------------------------------------------------------------- availability
    def available(self) -> tuple[bool, str]:
        if not self.cc:
            return False, "no C compiler on this machine"
        ok, how = sandbox_available()
        if not ok:
            return False, "no sandbox available (running untrusted code is disabled)"
        return True, how

    # ----------------------------------------------------------------------------------------- planning
    def run_for(self, cx, fn: str, inputs: dict[str, Any], purpose: str = "scenario") -> dict[str, Any] | None:
        ok, why = self.available()
        if not ok:
            return {"ok": False, "why": why}
        g = cx.g
        s = g.sym.get(fn)
        if not s or not s["file"] or not s["file"].endswith(".c"):
            return {"ok": False, "why": "the target is not a C function with a body"}
        from ..agent.recipes import pick_scenario_target
        mapping = {}
        if inputs:
            f2, mapping = pick_scenario_target(cx, inputs)
            if f2 != fn and mapping:
                fn = f2
        plan = self._plan(cx, fn, inputs, mapping, purpose)
        if not plan.get("ok", True):
            return plan
        runs = []
        for variant in plan["variants"]:
            r = self._execute(cx, fn, plan, variant)
            if not r.get("ok"):
                return r
            runs.append(r)
        return {"ok": True, "runs": runs, "target": fn, "explain": plan.get("explain", "")}

    def _params(self, g, fn: str) -> list[dict]:
        ps = sorted([dict(g.sym[k], key=k) for k in g.sym if k.startswith(fn + "#param:")],
                    key=lambda p: p.get("idx") if p.get("idx") is not None else 0)
        return ps

    def _struct_fields(self, g, tname: str, canonical: str = "") -> list[dict]:
        return [dict(g.sym[f], key=f) for f in g.record_fields(canonical, tname)]

    def _plan(self, cx, fn: str, inputs: dict[str, Any], mapping: dict[str, str], purpose: str) -> dict[str, Any]:
        g = cx.g
        params = self._params(g, fn)
        assigns: dict[str, Any] = {}          # "param->field" or "param" -> value
        given = {}
        assumptions = []
        q = getattr(cx.r, "question", "") or ""
        fields_by_path = {}
        for p in params:
            for f in self._struct_fields(g, p.get("type") or "", p.get("sig") or ""):
                fields_by_path[f"{p['name']}->{f['name']}"] = f
        for inp, target in mapping.items():
            v = inputs[inp]
            v = int(v) if isinstance(v, bool) else v
            f = fields_by_path.get(target)
            if f is not None and isinstance(v, (int, float)) and not isinstance(inputs[inp], bool) \
                    and f["name"] not in q:
                factor, why = unit_factor(f["name"], f.get("doc") or "", q, inp)
                if factor != 1:
                    nv = v * factor
                    nv = int(round(nv)) if float(nv).is_integer() or "int" in (f.get("type") or "").lower() \
                        or (f.get("type") or "").upper() in ("INT16", "UINT16", "WORD", "DWORD", "BYTE", "SHORT") else nv
                    assumptions.append(f"{inp} = {v} was given; {f['name']} stores {why}, so {f['name']} = {nv} was used")
                    v = nv
            assigns[target] = v
            given[target] = inp
        # defaults that pass the code's own checks (validators reachable from fn, early exits in fn)
        checks = self._rejecting_checks(cx, fn)
        for p in params:
            ptype = p.get("type") or ""
            if "*" in (p.get("sig") or ptype) and self._struct_fields(g, ptype, p.get("sig") or ""):
                for f in self._struct_fields(g, ptype, p.get("sig") or ""):
                    key = f"{p['name']}->{f['name']}"
                    if key in assigns:
                        continue
                    val = self._passing_value(f["name"], checks, g, purpose, inputs)
                    if val is not None:
                        assigns[key] = val
                        assumptions.append(f"{f['name']} was not given; used {val} so it passes the code's own check "
                                           f"({val_reason(f['name'], checks)})")
        mv = g.macro_values()
        variants = []
        consumers = self._consumers(cx, fn)
        trigger = self._trigger_assigns(cx, fn, params, getattr(cx, "trigger_cond", None)) \
            if purpose == "failure_invalid" and not given else None
        if trigger:
            assigns.update(trigger["assigns"])
            assumptions.append(f"No values were given, so the run uses an input that triggers the check "
                               f"`{trigger['cond']}`: " + ", ".join(f"{k.split(':')[-1].split('->')[-1]} = {v}"
                                                                   for k, v in trigger["assigns"].items()) + ".")
            variants.append({"label": "an input that triggers the check", "repeat": 1, "assigns": dict(assigns),
                             "consumers": [(c, "tick") for c in self._consumers(cx, fn)]})
            for v in variants:
                v["inputs"] = {k.split(":")[-1].split("->")[-1]: val for k, val in trigger["assigns"].items()}
            return {"variants": variants, "params": params, "assumptions": assumptions, "given": given}
        if purpose == "failure_invalid":
            reps = 1
            for c in g.callees(fn):
                for br in g.branches.get(c.callee, []):
                    m = re.search(r">=\s*([A-Z_][A-Z0-9_]+)", br["cond"])
                    if m and m.group(1) in mv:
                        reps = int(mv[m.group(1)])
            if not any(k.endswith("->valid") or k == "valid" for k in given):
                for k in list(assigns):
                    if k.endswith("->valid"):
                        assigns[k] = 0
            variants.append({"label": "one invalid input", "repeat": 1, "advance": [], "assigns": dict(assigns)})
            if reps > 1:
                variants.append({"label": f"{reps} invalid inputs in a row", "repeat": reps, "advance": [],
                                 "assigns": dict(assigns)})
            for v in variants:
                v["consumers"] = [(c, "tick") for c in consumers]
        elif purpose == "failure_missing":
            timeout = None
            for c in consumers:
                for br in g.branches.get(c, []):
                    for t in re.findall(r"\b[A-Z_][A-Z0-9_]*TIMEOUT[A-Z0-9_]*\b", br["cond"]):
                        if t in mv:
                            timeout = mv[t]
            timeout = timeout or 2000
            variants.append({"label": f"one good input, then no data for {int(timeout) + 100} ms", "repeat": 1,
                             "assigns": dict(assigns), "consumers": [(c, "tick") for c in consumers] +
                             [(c, ("after", int(timeout) + 100)) for c in consumers]})
            assumptions.append(f"The input stops after one good value; the main loop keeps running for {int(timeout) + 100} ms "
                               f"(longer than the {int(timeout)} ms timeout).")
        else:
            variants.append({"label": "", "repeat": 1, "assigns": dict(assigns),
                             "consumers": [(c, "tick") for c in consumers]})
        if consumers:
            assumptions.append("After the input is processed, the main loop's next step runs once: " +
                               ", ".join(f"{g.display(c)}" for c in consumers) + ".")
        for v in variants:
            shown = {inp: inputs[inp] for inp in mapping if inp in inputs}
            v["inputs"] = shown or {k.split("->")[-1]: val for k, val in v["assigns"].items()}
        return {"variants": variants, "params": params, "assumptions": assumptions, "given": given}

    def _trigger_assigns(self, cx, fn: str, params: list[dict], cond: str | None) -> dict | None:
        """Values that make a checked condition TRUE (e.g. current > OVERCURRENT_A -> current = limit + 10%), bound to
        a parameter, a field of a struct parameter, or a global the function reads."""
        if not cond:
            return None
        g = cx.g
        mv = g.macro_values()
        neg = cond.startswith("!(") and cond.endswith(")")
        body = cond[2:-1] if neg else cond
        glob = {g.sym[k]["name"] for k in g.sym if g.sym[k]["kind"] == "global"}
        fields = {}
        for p in params:
            for f in self._struct_fields(g, p.get("type") or "", p.get("sig") or ""):
                fields[f["name"]] = f"{p['name']}->{f['name']}"
        pnames = {p["name"] for p in params}
        for m in re.finditer(r"([A-Za-z_]\w*(?:(?:\.|->)\w+)*)\s*(>=|<=|>|<|==|!=)\s*(-?[\w.]+)", body):
            name, op, bound = m.group(1), m.group(2), m.group(3)
            b = mv.get(bound)
            if b is None:
                try:
                    b = float(bound.rstrip("fFuUlL"))
                except ValueError:
                    continue
            if neg:                                   # we need the inner condition FALSE
                op = {">": "<=", "<": ">=", ">=": "<", "<=": ">", "==": "!=", "!=": "=="}[op]
            step = max(1.0, abs(b) * 0.1)
            val = {">": b + step, ">=": b, "<": b - step, "<=": b, "==": b, "!=": b + 1}[op]
            val = int(val) if float(val).is_integer() and float(b).is_integer() else round(val, 3)
            leaf = name.replace("->", ".").split(".")[-1]
            if name in glob:
                return {"assigns": {f"global:{name}": val}, "cond": cond}
            if leaf in fields:
                return {"assigns": {fields[leaf]: val}, "cond": cond}
            if name in pnames:
                return {"assigns": {name: val}, "cond": cond}
        return None

    def _rejecting_checks(self, cx, fn: str) -> list[tuple[str, str, float]]:
        """(field, op, bound) for comparisons in validators/early exits that reject data."""
        g = cx.g
        mv = g.macro_values()
        out = []
        fns = [fn] + [c.callee for c in g.callees(fn) if c.callee in g.defined]
        for f in fns:
            for br in g.branches.get(f, []):
                for m in re.finditer(r"!\s*(?:\w+(?:->|\.))?(\w+)\b(?!\s*\()", br["cond"]):
                    out.append((m.group(1), "==", 0.0))       # rejects when the flag is 0
                for m in re.finditer(r"(?:->|\.)(\w+)\s*==\s*0\b", br["cond"]):
                    out.append((m.group(1), "==", 0.0))
                for m in re.finditer(r"(?:->|\.)?(\w+)\s*(<=|>=|<|>)\s*(-?[\w.]+)", br["cond"]):
                    name, op, bound = m.group(1), m.group(2), m.group(3)
                    b = mv.get(bound)
                    if b is None:
                        try:
                            b = float(bound)
                        except ValueError:
                            continue
                    out.append((name, op, float(b)))
        return out

    def _passing_value(self, field: str, checks, g, purpose: str, inputs) -> Any:
        rel = [(op, b) for (n, op, b) in checks if n == field]
        if not rel:
            return None
        if any(op == "==" for op, _ in rel):
            return 1
        lo, hi = -1e18, 1e18
        for op, b in rel:          # the check REJECTS when true, so we need the opposite
            if op in ("<", "<="):
                lo = max(lo, b if op == "<" else b + 1e-9)
            else:
                hi = min(hi, b if op == ">" else b - 1e-9)
        if lo > -1e17 and hi < 1e17:
            if lo < 0 < hi:
                return 0.0
            return round((lo + hi) / 2, 4)
        if lo > -1e17:
            v = lo * 2 if lo > 0 else lo + 1
            return int(v) if float(v).is_integer() else round(v, 4)
        if hi < 1e17:
            v = hi / 2 if hi > 0 else hi - 1
            return round(v, 4)
        return None

    def _consumers(self, cx, fn: str) -> list[str]:
        """Functions the main loop calls that read state fn writes (they run 'next')."""
        g = cx.g
        written = {o["sym"] for o in g.occ_in(fn) if o["role"] in ("write", "rw") and "#" not in o["sym"]}
        for c in g.callees(fn):
            if c.callee in g.defined:
                written |= {o["sym"] for o in g.occ_in(c.callee) if o["role"] in ("write", "rw") and "#" not in o["sym"]}
        mains = [e for e, d in g.entries.items() if d["kind"] == "main" and e in g.defined]
        out = []
        for m in mains:
            loop = [b for b in g.branches.get(m, []) if b["kind"] in ("for", "while", "do")]
            for c in g.callees(m):
                if not loop or not any((b["then_s"] or 0) <= c.line <= (b["then_e"] or 0) for b in loop):
                    continue
                if c.callee == fn or c.callee not in g.defined or c.callee in out:
                    continue
                reads = {o["sym"] for o in g.occ_in(c.callee) if o["role"] in ("read", "rw")}
                if reads & written:
                    out.append(c.callee)
        return out

    # ----------------------------------------------------------------------------------------- building
    def _profile_args(self) -> list[str]:
        prof = self.st.meta_get("c_profile") or {}
        args = []
        work = config.repo_dir(self.root) / "work"
        from ..extract import recon
        scan = recon.scan_repo(self.root)
        for d in sorted({str((self.root / h).parent) for h in scan.h_files}):
            args.append("-I" + d)
        for k, v in (prof.get("defines") or {}).items():
            if k.startswith("_M") or k in ("_WIN32", "_WIN64", "_MSC_VER"):
                continue
            args.append(f"-D{k}={v}" if v else f"-D{k}")
        # dialect language extensions (MSVC: __int64, __declspec…) and the API knowledge-pack headers
        for a in prof.get("args") or []:
            if a in ("-fms-extensions", "-fdeclspec") and a not in args:
                args.append(a)
        for d in prof.get("shim_dirs") or []:
            args += ["-isystem", d]
        if "msvc" in (prof.get("packs") or []) and os.name != "nt":
            args += ["-include", str(config.SHIMS / "win32" / "msvc_crt.h")]
        return args

    # embedded / legacy C that its own compilers accept: implicit declarations, int<->pointer stores
    LENIENT = ["-Wno-error=implicit-function-declaration", "-Wno-error=int-conversion", "-Wno-error=incompatible-pointer-types",
               "-Wno-error=incompatible-function-pointer-types", "-Wno-error=return-type", "-Wno-error=implicit-int"]
    INSTR = ["-O0", "-g", "-w", "-std=gnu17", "-fprofile-instr-generate", "-fcoverage-mapping", "-finstrument-functions",
             *LENIENT]

    def _prelude_for(self, rel: str | None) -> Path:
        """The stand-in prelude the index used for this file (missing headers' types, register blocks, SAL…)."""
        work = config.repo_dir(self.root) / "work"
        if rel:
            tu = work / "tu" / (re.sub(r"[^A-Za-z0-9_.-]", "_", rel) + ".default.h")
            if tu.exists():
                return tu
        return work / "prelude.h"

    def _compile_unit(self, rel: str, args: list[str], rdir: Path) -> Path | None:
        """Compile one project file (instrumented) with the same stand-ins the index used; None if it can't."""
        obj = rdir / (re.sub(r"\W", "_", rel) + ".o")
        pre = self._prelude_for(rel)
        stub_dir = config.repo_dir(self.root) / "work" / "stubs"
        cmd = [self.cc, "-c", *self.INSTR, *args, "-I", str(stub_dir)] + (["-include", str(pre)] if pre.exists() else []) + \
            [str(self.root / rel), "-o", str(obj)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            (rdir / (obj.name + ".log")).write_text(r.stderr)
            return None
        return obj

    def _stub_object_defs(self, cx, preludes: list[Path], rdir: Path, args: list[str]) -> list[Path]:
        """Stand-in objects declared by the preludes (register blocks, unknown globals) need ONE definition each."""
        g = cx.g
        repo_globals = {sy["name"] for k, sy in g.sym.items() if sy["kind"] == "global" and sy.get("is_def")
                        and sy.get("file") and sy["file"] != "<stub>"}
        taken: set[str] = set()
        out = []
        for i, pre in enumerate(dict.fromkeys(preludes)):
            if not pre.exists():
                continue
            txt = pre.read_text(errors="replace")
            defs = []
            for m in re.finditer(r"extern volatile struct (__vr_regs_\w+) (\w+);", txt):
                if m.group(2) not in taken and m.group(2) not in repo_globals:
                    defs.append(f"volatile struct {m.group(1)} {m.group(2)};")
                    taken.add(m.group(2))
            for m in re.finditer(r"extern struct (__vr_regs_\w+) \*(\w+);", txt):
                if m.group(2) not in taken and m.group(2) not in repo_globals:
                    defs.append(f"static struct {m.group(1)} __vr_obj_{m.group(2)}; struct {m.group(1)} *{m.group(2)} = "
                                f"&__vr_obj_{m.group(2)};")
                    taken.add(m.group(2))
            for m in re.finditer(r"extern int (\w+); /\* stub", txt):
                if m.group(1) not in taken and m.group(1) not in repo_globals:
                    defs.append(f"int {m.group(1)};")
                    taken.add(m.group(1))
            if not defs:
                continue
            src = rdir / f"vr_defs_{i}.c"
            src.write_text(f'/* Verity: one definition for each stand-in object */\n#include "{pre}"\n' + "\n".join(defs) + "\n")
            obj = src.with_suffix(".o")
            r = subprocess.run([self.cc, "-c", "-O0", "-w", "-std=gnu17", *self.LENIENT, *args, str(src), "-o", str(obj)],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                out.append(obj)
        return out

    def _execute(self, cx, fn: str, plan: dict, variant: dict) -> dict[str, Any]:
        g = cx.g
        key = hashlib.sha1(json.dumps([fn, variant, time.time()], default=str).encode()).hexdigest()[:10]
        self._prune_runs(keep=30)
        rdir = self.runs_dir / key
        rdir.mkdir(parents=True, exist_ok=True)
        rdir = Path(os.path.realpath(rdir))        # sandbox rules match real paths (/var -> /private/var on macOS)
        args = self._profile_args()
        mains = [e for e, d in g.entries.items() if d["kind"] == "main" and e in g.defined]
        main_file = g.sym[mains[0]]["file"] if mains else None
        c_files = [r["path"] for r in self.st.q("SELECT path FROM file WHERE lang='c' AND path LIKE '%.c'")]
        objs, units, excluded, preludes = [], [], [], []
        for rel in c_files:
            if rel == main_file:
                continue
            o = self._compile_unit(rel, args, rdir)
            if o is not None:
                objs.append(o)
                units.append(rel)
                preludes.append(self._prelude_for(rel))
            else:
                excluded.append(rel)
        main_pre = self._prelude_for(main_file)
        preludes.append(main_pre)
        objs += self._stub_object_defs(cx, preludes, rdir, args)
        src = self._harness_source(cx, fn, plan, variant, main_file, mains[0] if mains else None, excluded)
        # register-block stand-ins declared only in OTHER files' preludes (same layout project-wide): declare them here
        main_txt = main_pre.read_text(errors="replace") if main_pre.exists() else ""
        extra, have = [], set(re.findall(r"struct (__vr_regs_\w+) \{", main_txt))
        for pre in dict.fromkeys(preludes):
            if not pre.exists() or pre == main_pre:
                continue
            for ln in pre.read_text(errors="replace").splitlines():
                m = re.match(r"struct (__vr_regs_\w+) \{", ln)
                if m and m.group(1) not in have:
                    have.add(m.group(1))
                    extra.append(ln)
        src = src.replace("/*__VR_EXTRA_DECLS__*/", "\n".join(extra))
        hpath = rdir / "vr_harness.c"
        exe = rdir / "vr_run"
        stub_dir = config.repo_dir(self.root) / "work" / "stubs"
        stubs_extra = ""
        for attempt in range(5):
            hpath.write_text(src.replace("/*__VR_EXTRA_STUBS__*/", stubs_extra))
            cmd = [self.cc, *self.INSTR, *args, "-I", str(stub_dir)] + \
                (["-include", str(main_pre)] if main_pre.exists() else []) + [str(hpath), *[str(o) for o in objs],
                                                                            "-lm", "-o", str(exe)]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(rdir))
            if r.returncode == 0:
                break
            missing = re.findall(r'"_?([A-Za-z_]\w*)", referenced from', r.stderr) + \
                re.findall(r"undefined reference to [`']([A-Za-z_]\w*)'", r.stderr)
            if not missing:
                (rdir / "build.log").write_text(r.stderr)
                return {"ok": False, "why": "the harness did not compile: " + _first_error(r.stderr)}
            stubs_extra += self._stub_defs(cx, sorted(set(missing)), already=stubs_extra + src)
        else:
            (rdir / "build.log").write_text(r.stderr)
            return {"ok": False, "why": "the harness could not be linked: " + _first_error(r.stderr)}
        # run in the sandbox
        prof_raw = rdir / "run.profraw"
        env = {"LLVM_PROFILE_FILE": str(prof_raw), "PATH": "/usr/bin:/bin", "HOME": str(rdir)}
        sb = rdir / "sandbox.sb"
        sb.write_text(_sandbox_profile(exe, rdir))
        t0 = time.monotonic()
        try:
            if platform.system() == "Darwin":
                p = subprocess.run(["sandbox-exec", "-f", str(sb), str(exe)], capture_output=True, text=True,
                                   timeout=self.timeout, cwd=str(rdir), env=env)
            else:
                p = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--bind", str(rdir), str(rdir), "--unshare-all",
                                    "--die-with-parent", str(exe)], capture_output=True, text=True, timeout=self.timeout,
                                   cwd=str(rdir), env=env)
        except subprocess.TimeoutExpired:
            return {"ok": False, "why": f"the code did not finish within {self.timeout:.0f} s (possible infinite loop)"}
        dt = time.monotonic() - t0
        out = p.stdout
        (rdir / "stdout.txt").write_text(out)
        res = self._parse_output(cx, exe, out)
        res.update(self._coverage(cx, exe, prof_raw, rdir, fn))
        res.update({"ok": p.returncode == 0 or bool(res.get("calls")), "exit": p.returncode, "seconds": round(dt, 3),
                    "label": variant.get("label", ""), "inputs": variant.get("inputs", {}), "run_dir": str(rdir),
                    "excluded": excluded, "assumptions": plan["assumptions"] +
                    ([f"{', '.join(excluded)} could not be compiled on this machine (missing headers); the functions and "
                      f"data it defines were replaced by stand-ins."] if excluded else []),
                    "stdout_tail": out[-1500:],
                    "hw_expected": [g.display(k) for k in self._stubbed_fns(cx)]})
        if p.returncode not in (0,) and not res.get("calls"):
            res["ok"] = False
            if "sandbox_apply" in (p.stderr or ""):
                # macOS cannot start a sandbox inside another one (Verity itself launched from a sandboxed process)
                res["why"] = ("the safety sandbox could not be started here (Verity is itself running inside a "
                              "sandbox, and macOS does not allow one inside another)")
            else:
                res["why"] = f"the program crashed (exit code {p.returncode}): {p.stderr[-300:]}"
        return res

    def _prune_runs(self, keep: int = 30) -> None:
        """Run folders are evidence for recent answers; keep the newest few, delete the rest."""
        try:
            runs = sorted((d for d in self.runs_dir.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
        except OSError:
            return
        for d in runs[:-keep] if len(runs) > keep else []:
            shutil.rmtree(d, ignore_errors=True)

    def _stubbed_fns(self, cx) -> list[str]:
        g = cx.g
        return sorted(k for k, s in g.sym.items() if s["kind"] == "function" and k not in g.defined and s.get("file")
                      and s["file"] != "<stub>" and not s.get("extern"))

    def _stub_defs(self, cx, names: list[str], already: str) -> str:
        g = cx.g
        out = []
        for n in names:
            if re.search(rf"\b{re.escape(n)}\s*\(", already) and f"__vr_stub_{n}" in already:
                continue
            k = next((k for k in g.by_name.get(n, []) if g.sym[k]["kind"] in ("function", "global")), None)
            s = g.sym.get(k) if k else None
            if s and s["kind"] == "global":
                t = s.get("type") or "int"
                inc = _type_include(g, t, self.root)
                if inc and inc not in already:
                    out.append(inc)
                out.append(f"{_decl(t, n)}; /* stand-in definition */")
            else:
                t = (s.get("type") if s else "") or "int ()"
                proto = (s.get("sig") if s else "") or ""
                out.append(_stub_from_proto(n, proto, t, time_fn=bool(TIME_FN.search(n))))
        return "\n".join(out) + "\n"

    def _harness_source(self, cx, fn: str, plan: dict, variant: dict, main_file: str | None, main_key: str | None,
                        excluded: list[str]) -> str:
        g = cx.g
        s = g.sym[fn]
        lines = ["/* Generated by Verity: scenario harness. */", "#include <stdio.h>", "#include <string.h>",
                 "#include <stdint.h>", "#include <stdlib.h>"]
        lines.append("static unsigned long long __vr_now = 0;")
        lines.append('__attribute__((no_instrument_function)) void __cyg_profile_func_enter(void *f, void *c)'
                     ' { printf("__VR_ENTER %p\\n", f); }')
        lines.append('__attribute__((no_instrument_function)) void __cyg_profile_func_exit(void *f, void *c) { }')
        # stubs for functions declared in the repository but not defined anywhere (hardware / OS)
        stubbed = self._stubbed_fns(cx)
        # functions defined only in excluded files also need stubs
        for rel in excluded:
            for k in g.defined:
                if g.sym[k]["file"] == rel and k not in stubbed and not g.entries.get(k):
                    stubbed.append(k)
        if main_file:
            lines.append("#define main __vr_app_main")
            lines.append(f'#include "{self.root / main_file}"')
            lines.append("#undef main")
        else:
            hdr = s["file"].rsplit(".", 1)[0] + ".h"
            if (self.root / hdr).exists():
                lines.append(f'#include "{self.root / hdr}"')
        # headers declaring the types we need
        for p in plan["params"]:
            t = (p.get("type") or "").replace("const", "").replace("*", "").strip()
            for k, sy in g.sym.items():
                if sy["kind"] in ("typedef", "struct") and sy["name"] == t and sy.get("file", "").endswith(".h"):
                    lines.append(f'#include "{self.root / sy["file"]}"')
                    break
        decl_hdr = [sy["file"] for k, sy in g.sym.items() if k == fn]
        fdecl = next((sy["file"] for k2, sy in g.sym.items() if sy["name"] == s["name"] and sy.get("file", "").endswith(".h")),
                     None)
        for k, sy in g.sym.items():
            if sy["kind"] == "function" and sy["name"] == s["name"] and sy.get("file", "").endswith(".h"):
                lines.append(f'#include "{self.root / sy["file"]}"')
                break
        for k in stubbed:
            sy = g.sym[k]
            inc = f'#include "{self.root / sy["file"]}"'
            if sy.get("file", "").endswith(".h") and inc not in lines:
                lines.append(inc)
        for k in stubbed:
            sy = g.sym[k]
            lines.append(_stub_fn(sy["name"], sy.get("type") or "int ()", time_fn=bool(TIME_FN.search(sy["name"]))))
        for rel in excluded:     # data defined only in files that cannot be compiled here
            for k, sy in g.sym.items():
                if sy["kind"] == "global" and sy["file"] == rel and not sy.get("extern"):
                    t = sy.get("type") or "int"
                    inc = _type_include(g, t, self.root)
                    if inc and inc not in lines:
                        lines.append(inc)
                    lines.append(f"{_decl(t, sy['name'])}; /* stand-in for {rel} */")
        lines.append("/*__VR_EXTRA_DECLS__*/")
        lines.append("/*__VR_EXTRA_STUBS__*/")
        # state to print: globals/fields written by the target, its callees and consumers
        watch = self._watch_list(cx, fn, [c for c, _ in variant.get("consumers", [])])
        lines.append("__attribute__((no_instrument_function)) static void __vr_dump(const char *phase) {")
        lines.append('  printf("__VR_PHASE %s\\n", phase);')
        for expr, t in watch:
            fmt, cast = _fmt_for(t)
            if fmt == "%p":
                continue
            lines.append(f'  printf("__VR_STATE {expr}={fmt}\\n", {cast}({expr}));')
        lines.append("}")
        lines.append("int main(void) {")
        lines.append('  printf("__VR_BASE %p\\n", (void*)&__vr_dump);')
        # replay main()'s initialisation
        if main_key:
            init = self._init_statements(cx, main_key)
            if init:
                # main()'s own parameters, so its start-up statements can refer to them
                for mp in self._params(g, main_key):
                    ct = (mp.get("sig") or mp.get("type") or "int")
                    if "**" in ct:
                        elem = ct.replace("*", "").replace("const", "").strip() or "char"
                        lines.append(f"  static {elem} __vr_arg0[1]; {elem} *__vr_argv[2] = {{ __vr_arg0, 0 }}; "
                                     f"{mp.get('type') or ct} {mp['name']} = ({mp.get('type') or ct})__vr_argv;")
                    elif "*" in ct:
                        lines.append(f"  {mp.get('type') or ct} {mp['name']} = 0;")
                    else:
                        lines.append(f"  {mp.get('type') or ct} {mp['name']} = 1;")
            lines += ["  " + st for st in init]
        # build the input
        call_args = []
        for p in plan["params"]:
            t = (p.get("type") or "int").strip()
            base = t.replace("const", "").replace("*", "").strip()
            if "*" in (p.get("sig") or t) and self._struct_fields(g, t, p.get("sig") or ""):
                var = f"__vr_in_{p['name']}"
                lines.append(f"  {base} {var}; memset(&{var}, 0, sizeof({var}));")
                for k, v in variant["assigns"].items():
                    if k.startswith(p["name"] + "->"):
                        lines.append(f"  {var}.{k.split('->', 1)[1]} = {_c_lit(v)};")
                call_args.append(f"&{var}")
            else:
                v = variant["assigns"].get(p["name"], 0)
                call_args.append(_c_lit(v))
        tick = 100
        mv = g.macro_values()
        if main_key:
            for c in g.callees(main_key):
                for gd in c.guards or []:
                    m = re.search(r">=\s*([A-Z_][A-Z0-9_]+)", gd["cond"])
                    if m and m.group(1) in mv and TIME_NAME.search(gd["cond"]):
                        tick = int(mv[m.group(1)])
        for k, v in variant["assigns"].items():
            if k.startswith("global:"):
                lines.append(f"  {k[7:]} = {_c_lit(v)};")
        lines.append('  __vr_dump("before");')
        for i in range(int(variant.get("repeat", 1))):
            lines.append(f'  printf("__VR_CALL_TARGET\\n"); {s["name"]}({", ".join(call_args)});')
        lines.append('  __vr_dump("after_input");')
        for c, when in variant.get("consumers", []):
            cs = g.sym[c]
            cargs = []
            for p in self._params(g, c):
                if TIME_NAME.search(p["name"]):
                    if when == "tick":
                        cargs.append(f"(__vr_now += {tick})")
                    else:
                        cargs.append(f"(__vr_now += {when[1]})")
                else:
                    cargs.append("0")
            lines.append(f'  {cs["name"]}({", ".join(cargs)});')
            lines.append(f'  __vr_dump("after_{cs["name"]}");')
        lines.append("  return 0;\n}")
        return "\n".join(lines) + "\n"

    SKIP_CALLS = re.compile(r"\b(CreateThread|_beginthread\w*|pthread_\w+|thrd_create|WaitFor\w+|Sleep|usleep|sleep|"
                            r"getchar|getch|_getch|scanf|fgets|gets|select|poll|accept|listen|recv\w*|ReadFile|"
                            r"xTaskCreate\w*|vTaskStartScheduler|osKernelStart|exit|abort|MessageLoop|GetMessage\w*|"
                            r"DispatchMessage\w*|RunLoop|app_run)\s*\(")

    def _init_statements(self, cx, main_key: str) -> list[str]:
        """The program's own start-up: the top-level statements of main() before its first loop, minus anything
        that would block, start threads, read input or return (error branches)."""
        g = cx.g
        s = g.sym[main_key]
        try:
            import tree_sitter as ts
            import tree_sitter_c as tsc
            parser = ts.Parser(ts.Language(tsc.language()))
            src = (self.root / s["file"]).read_bytes()
            tree = parser.parse(src)
        except Exception:
            return []
        fn_node = None
        stack = [tree.root_node]
        while stack:
            n = stack.pop()
            if n.type == "function_definition" and n.start_point[0] + 1 <= s["line"] <= n.end_point[0] + 1:
                fn_node = n
                break
            stack.extend(n.children)
        if fn_node is None:
            return []
        body = fn_node.child_by_field_name("body")
        out: list[str] = []
        for st in (body.named_children if body is not None else []):
            text = src[st.start_byte:st.end_byte].decode("utf-8", "replace")
            if st.type in ("for_statement", "while_statement", "do_statement"):
                break                              # the main loop starts here
            if st.type in ("return_statement", "comment", "goto_statement", "labeled_statement"):
                continue
            if re.search(r"\breturn\b|\bexit\s*\(|\bgoto\b", text) and st.type != "declaration":
                continue                           # error handling that would end start-up
            if self.SKIP_CALLS.search(text):
                continue
            out.append(text if text.rstrip().endswith((";", "}")) else text + ";")
        return out

    def _watch_list(self, cx, fn: str, consumers: list[str]) -> list[tuple[str, str]]:
        g = cx.g
        fns = [fn] + [c.callee for c in g.callees(fn) if c.callee in g.defined] + consumers
        out: list[tuple[str, str]] = []
        seen = set()
        for f in fns:
            for o in g.occ_in(f):
                if o["role"] not in ("write", "rw"):
                    continue
                sym = o["sym"]
                sy = g.sym.get(sym)
                if not sy or "#" in sym:
                    continue
                if sy["kind"] == "global" and not sy.get("static"):
                    if o.get("via") and ("@FI@" in o["via"] or o["via"] == "deref"):
                        continue          # a field of this global was written; the field is watched instead
                    t = sy.get("type") or ""
                    if any(g.sym[k2]["kind"] in ("struct", "union", "typedef") and g.sym[k2]["name"] == t.replace("struct ", "")
                           for k2 in g.by_name.get(t.replace("struct ", ""), [])) and not re.search(r"(int|double|float|char|long|short|bool|size_t)", t):
                        continue
                    expr = sy["name"]
                elif sy["kind"] == "field" and o.get("via") is None:
                    # field written through a global struct: the whole lvalue chain "g.a.b.x" from the source line
                    line = g.src.line(o["file"], o["line"])
                    m = re.search(rf"\b([A-Za-z_]\w*)((?:\s*\.\s*[A-Za-z_]\w*)*?\s*\.\s*{re.escape(sy['name'])})\b", line)
                    if not m:
                        continue
                    base = m.group(1)
                    bsym = next((k for k in g.by_name.get(base, []) if g.sym[k]["kind"] == "global" and
                                 not g.sym[k].get("static")), None)
                    if not bsym:
                        continue
                    expr, t = re.sub(r"\s+", "", base + m.group(2)), sy.get("type") or "unsigned int"
                else:
                    continue
                if expr in seen:
                    continue
                seen.add(expr)
                out.append((expr, t))
        # stand-in register blocks (missing device headers): watch the exact register written
        for f in fns:
            for o in g.occ_in(f):
                if o["role"] not in ("write", "rw") or "__vr_regs_" not in o["sym"]:
                    continue
                line = g.src.line(o["file"], o["line"])
                m = re.match(r"^\s*([A-Za-z_]\w*(?:\s*(?:\.|->)\s*\w+(?:\[[^\]]*\])?)+)\s*(\|=|&=|\^=|\+=|-=|=)", line)
                if m:
                    expr = re.sub(r"\s+", "", m.group(1))
                    if expr not in seen and "[" not in expr:
                        seen.add(expr)
                        out.append((expr, "unsigned long long"))
        return out[:32]

    # ----------------------------------------------------------------------------------------- reading results
    def _parse_output(self, cx, exe: Path, out: str) -> dict[str, Any]:
        g = cx.g
        nm = subprocess.run(["nm", "-n", str(exe)], capture_output=True, text=True).stdout
        addr: dict[int, str] = {}
        for ln in nm.splitlines():
            parts = ln.split()
            if len(parts) == 3 and parts[1].lower() == "t":
                nm_name = parts[2][1:] if platform.system() == "Darwin" and parts[2].startswith("_") else parts[2]
                addr[int(parts[0], 16)] = nm_name
        base_nm = next((a for a, n in addr.items() if n == "__vr_dump"), None)
        repo_fns = {g.sym[k]["name"] for k in g.defined}
        slide = None
        calls, hw, state, phases = [], [], {}, []
        phase = "start"
        started = False
        for ln in out.splitlines():
            if ln.startswith("__VR_BASE ") and base_nm is not None:
                slide = int(ln.split()[1], 16) - base_nm
            elif ln.startswith("__VR_ENTER ") and slide is not None:
                a = int(ln.split()[1], 16) - slide
                name = addr.get(a)
                if name and not name.startswith("__sx") and started and name in repo_fns:
                    calls.append(name)
            elif ln.startswith("__VR_CALL_TARGET"):
                started = True
            elif ln.startswith("__VR_HW "):
                if started:
                    hw.append(ln[8:])
            elif ln.startswith("__VR_PHASE "):
                phase = ln.split(" ", 1)[1]
                phases.append(phase)
            elif ln.startswith("__VR_STATE "):
                k, _, v = ln[11:].partition("=")
                state.setdefault(phase, {})[k] = v
        before = state.get("before", {})
        final_phase = phases[-1] if phases else None
        final = state.get(final_phase, {}) if final_phase else {}
        changed = {k: v for k, v in final.items() if before.get(k) != v}
        pretty = {}
        for k, v in changed.items():
            pretty[k] = _pretty_value(g, k, v)
        return {"calls": calls, "hw_calls": hw, "state": pretty, "state_by_phase": state,
                "stubs_note": "hardware functions were replaced by stand-ins that record their calls"}

    def _coverage(self, cx, exe: Path, raw: Path, rdir: Path, fn: str) -> dict[str, Any]:
        g = cx.g
        if not raw.exists():
            return {"branches": []}
        prof = rdir / "run.profdata"
        try:
            subprocess.run(["xcrun", "llvm-profdata", "merge", "-sparse", str(raw), "-o", str(prof)] if
                           platform.system() == "Darwin" else ["llvm-profdata", "merge", "-sparse", str(raw), "-o", str(prof)],
                           capture_output=True, timeout=60, check=True)
            ex = subprocess.run((["xcrun"] if platform.system() == "Darwin" else []) +
                                ["llvm-cov", "export", str(exe), f"-instr-profile={prof}", "-format=text"],
                                capture_output=True, text=True, timeout=60, check=True)
            data = json.loads(ex.stdout)
        except Exception:
            return {"branches": []}
        segs: dict[str, list[list]] = {}
        line_hits: dict[str, dict[int, int]] = {}
        for f in data.get("data", [{}])[0].get("files", []):
            try:
                rel = Path(f["filename"]).resolve().relative_to(self.root).as_posix()
            except ValueError:
                continue
            segs[rel] = f.get("segments", [])
            hits: dict[int, int] = {}
            for seg in segs[rel]:
                if seg[3]:
                    hits[seg[0]] = max(hits.get(seg[0], 0), seg[2])
            line_hits[rel] = hits
        out = []
        interesting = {fn} | {c.callee for c in g.callees(fn)} | set(self._consumers(cx, fn))
        for f in list(interesting):
            for c in g.callees(f):
                if c.callee in g.defined:
                    interesting.add(c.callee)
        for f in interesting:
            for br in g.branches.get(f, []):
                if br["kind"] not in ("if",):
                    continue
                sg = segs.get(br["file"], [])
                cond_hits = line_hits.get(br["file"], {}).get(br["line"], 0)
                if not cond_hits:
                    continue
                # the then-block's region entry: first region-entry segment after the "{" (or the closing ")")
                ts = br["then_s"] or br["line"]
                src_line = g.src.line(br["file"], ts)
                col = (src_line.rfind("{") + 1) if "{" in src_line else (src_line.rfind(")") + 1)
                then_count = None
                for seg in sg:
                    ln, cl, cnt, has, entry = seg[0], seg[1], seg[2], seg[3], seg[4]
                    gap = seg[5] if len(seg) > 5 else False
                    if not has or not entry or gap:
                        continue
                    if (ln, cl) >= (ts, col) and ln <= (br["then_e"] or ts):
                        then_count = cnt
                        break
                out.append({"fn": f, "file": br["file"], "line": br["line"], "cond": br["cond"],
                            "taken": (then_count > 0) if then_count is not None else None,
                            "evaluated": cond_hits, "then_count": then_count})
        out.sort(key=lambda b: (b["file"], b["line"]))
        return {"branches": out, "coverage_files": len(line_hits)}


# ---------------------------------------------------------------------------------------------- helpers
def _type_include(g, t: str, root: Path) -> str | None:
    base = re.sub(r"\b(const|volatile|struct|union|enum)\b|\*|\[.*\]", "", t).strip()
    hdr = next((g.sym[k2]["file"] for k2 in g.by_name.get(base, [])
                if g.sym[k2]["kind"] in ("typedef", "struct", "union", "enum")
                and (g.sym[k2].get("file") or "").endswith(".h")), None)
    return f'#include "{root / hdr}"' if hdr else None


def unit_factor(name: str, doc: str, question: str, inp: str) -> tuple[float, str]:
    """How many field units make one of the question's units (1/100 °C field, value in °C → 100)."""
    from ..retrieval.cards import split_ident
    words = set(split_ident(name))
    d = doc.lower()
    table = [
        ({"centi", "hundredths", "cent"}, ("1/100", "0.01", "hundredth", "centi"), 100, "hundredths"),
        ({"deci", "tenths", "tenth"}, ("1/10 ", "0.1 ", "tenth", "deci"), 10, "tenths"),
        ({"milli"}, ("1/1000", "0.001", "milli"), 1000, "thousandths"),
        ({"micro"}, ("1e-6", "micro"), 1_000_000, "millionths"),
        ({"x10"}, ("x10", "times 10"), 10, "ten times the value"),
        ({"x100"}, ("x100", "times 100"), 100, "a hundred times the value"),
    ]
    for wset, dpats, factor, label in table:
        if words & wset or any(pp in d for pp in dpats):
            return factor, f"{label} of a unit" + (f" (its comment: \"{doc.strip()}\")" if doc else "")
    # time fields in ms when the question speaks in seconds
    if (words & {"ms", "msec", "millis"} or "millisecond" in d) and re.search(rf"{re.escape(inp)}\D{{0,20}}\d+(\.\d+)?\s*(s|sec|second)s?\b", question, re.I):
        return 1000, "milliseconds"
    return 1, ""


def val_reason(field: str, checks) -> str:
    rel = [f"rejects {field} {op} {b:g}" if op != "==" else f"rejects {field} = 0" for (n, op, b) in checks if n == field]
    return "; ".join(rel)


def _c_lit(v: Any) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return repr(v)
    return str(v)


def _decl(t: str, name: str) -> str:
    m = re.match(r"^(.*)\[(\d*)\]$", t.strip())
    if m:
        return f"{m.group(1).strip()} {name}[{m.group(2) or 1}]"
    return f"{t} {name}"


HANDLE_TYPES = re.compile(r"^(HANDLE|HWND|HMODULE|HINSTANCE|HKEY|SOCKET|HDC|FARPROC)$")


def _stub_fn(name: str, ftype: str, time_fn: bool = False) -> str:
    """A logging stand-in. Return values follow the platform's success conventions (BOOL TRUE, a valid handle,
    real memory for allocators, the simulated clock for time functions) — each run lists them as assumptions."""
    ret, params = _c_type_parts(ftype)
    ps = [f"{t} a{i}" for i, t in enumerate(params)]
    fmts, casts = [], []
    for i, t in enumerate(params):
        fmt, cast = _fmt_for(t)
        fmts.append(fmt)
        casts.append(f"{cast}a{i}")
    fmt = ", ".join(fmts)
    printf = f'printf("__VR_HW {name}({fmt})\\n"{"".join(", " + c for c in casts)});'
    body = printf
    r = ret.replace("WINAPI", "").replace("__stdcall", "").strip()
    if ret != "void" and r != "void":
        if time_fn:
            body += f" return ({ret})__vr_now;"
        elif r == "BOOL":
            body += " return 1;"
        elif re.search(r"Alloc|alloc$", name) and ("*" in r or r in ("LPVOID", "PVOID", "HGLOBAL", "HLOCAL")):
            body += f" return ({ret})calloc(1, 65536);"
        elif HANDLE_TYPES.match(r):
            body += f" return ({ret})(uintptr_t)0x1000;"
        else:
            body += f" return ({ret})0;"
    return (f"__attribute__((no_instrument_function)) {ret} {name}({', '.join(ps) or 'void'}) "
            f"{{ {body} }} /* __vr_stub_{name} */")


def _stub_from_proto(name: str, proto: str, ftype: str, time_fn: bool = False) -> str:
    """A stand-in built from the declaration text itself (exact parameter types: const, arrays, callbacks)."""
    m = re.search(rf"\b{re.escape(name)}\s*\((.*)\)\s*;?\s*$", proto or "", re.S)
    if not m:
        return _stub_fn(name, ftype, time_fn)
    ret, _ = _c_type_parts(ftype)
    inner = m.group(1).strip()
    params, depth, cur = [], 0, ""
    for ch in inner:
        if ch == "," and depth == 0:
            params.append(cur.strip())
            cur = ""
            continue
        depth += ch in "(["
        depth -= ch in ")]"
        cur += ch
    if cur.strip():
        params.append(cur.strip())
    if params in ([], ["void"]):
        params = []
    decls, names, types = [], [], []
    for i, p in enumerate(params):
        if p == "...":
            decls.append("...")
            continue
        mm = re.match(r"^(.*?)([A-Za-z_]\w*)\s*(\[[^\]]*\])?$", p)
        is_named = bool(mm) and mm.group(2) not in ("int", "char", "long", "short", "unsigned", "signed", "float", "double",
                                                  "void", "const", "volatile") and mm.group(1).strip() not in ("", "const", "struct", "unsigned", "signed")
        if "(" in p:                               # function-pointer parameter: keep as written, print nothing
            decls.append(p if re.search(r"\(\s*\*\s*\w+", p) else p.replace("(*", f"(*a{i}", 1))
            continue
        if is_named:
            nm = mm.group(2)
            ty = (mm.group(1) + (" *" if mm.group(3) else "")).strip()
            decls.append(p)
        else:
            nm, ty = f"a{i}", p
            decls.append(f"{p} a{i}")
        names.append(nm)
        types.append(ty)
    fmts, casts = [], []
    for j, (nm, ty) in enumerate(zip(names, types)):
        t0 = re.sub(r"\s+", " ", ty.replace("const", "")).strip()
        nxt = names[j + 1] if j + 1 < len(names) else ""
        if t0 in ("LPCSTR", "LPSTR", "PCSTR", "PSTR", "LPCTSTR", "LPTSTR", "char *", "char*"):
            fmts.append("\\\"%.200s\\\"")
            casts.append(f"{nm} ? (const char *){nm} : \"(null)\"")
        elif t0 in ("LPCVOID", "LPVOID", "void *", "PVOID", "BYTE *", "LPBYTE", "PBYTE", "unsigned char *") and \
                re.search(r"(len|size|count|bytes|nNumber|cb)", nxt, re.I):
            fmts.append("\\\"%.*s\\\"")
            casts.append(f"(int)((size_t){nxt} > 200 ? 200 : (size_t){nxt}), {nm} ? (const char *){nm} : \"\"")
        else:
            fmt, cast = _fmt_for(ty if "*" not in ty else "void *")
            fmts.append(fmt)
            casts.append(f"{cast}{nm}")
    body = f'printf("__VR_HW {name}({", ".join(fmts)})\\n"{"".join(", " + c for c in casts)});'
    r = ret.replace("WINAPI", "").replace("__stdcall", "").strip()
    if ret != "void" and r != "void":
        if time_fn:
            body += f" return ({ret})__vr_now;"
        elif r == "BOOL":
            body += " return 1;"
        elif re.search(r"Alloc|alloc$", name) and ("*" in r or r in ("LPVOID", "PVOID", "HGLOBAL", "HLOCAL")):
            body += f" return ({ret})calloc(1, 65536);"
        elif HANDLE_TYPES.match(r):
            body += f" return ({ret})(uintptr_t)0x1000;"
        else:
            body += f" return ({ret})0;"
    return (f"__attribute__((no_instrument_function)) {ret} {name}({', '.join(decls) or 'void'}) "
            f"{{ {body} }} /* __vr_stub_{name} */")


def _pretty_value(g, k: str, v: str) -> str:
    # enum values -> names
    name = k.split(".")[-1]
    fld = [kk for kk in g.by_name.get(name, []) if g.sym[kk]["kind"] in ("field", "global")]
    if fld:
        t = g.sym[fld[0]].get("type") or ""
        for ek, es in g.sym.items():
            if es["kind"] == "enum" and es["name"] == t:
                for ck, cs in g.sym.items():
                    if cs.get("container") == ek and g.consts.get(ck) == v:
                        return f"{v} ({cs['name']})"
    return v


def _first_error(stderr: str) -> str:
    for ln in stderr.splitlines():
        if "error" in ln:
            return ln.strip()[:240]
    return stderr.strip()[:240]


def _sandbox_profile(exe: Path, rdir: Path) -> str:
    return f"""(version 1)
(deny default)
(allow process-exec (literal "{exe}"))
(allow process-fork)
(allow file-read*)
(allow file-write* (subpath "{rdir}"))
(allow file-write-data (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr"))
(allow sysctl-read)
(deny network*)
"""
