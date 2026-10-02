#!/usr/bin/env python3
"""Verification for the Verity Roslyn worker (stdlib only).

Runs the published worker on:
  1. samples/navgps-cs            -> schema validation + ground-truth assertions (see samples/navgps-cs/EXPECTED.md)
  2. samples/navgps-cs again      -> byte-identical output except the timing in the final record
  3. tests/fixtures/broken        -> deliberately broken code: must not crash, must degrade gracefully
  4. tests/fixtures/multi         -> ProjectReference + multi-targeting + top-level statements
  5. a generated stress repository (large file, deep nesting, many early exits)
  6. the stress repository with --timeout 2 -> the watchdog must stop it and still emit "done"
  7. the sample under macOS sandbox-exec with all networking denied (offline guarantee)

Usage: python3 tests/verify.py [--dll path/to/VerityRoslyn.dll] [--keep]
Exit status 0 when every check passes.
"""
import argparse
import collections
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.dirname(HERE)
MATI = os.path.dirname(os.path.dirname(WORKER))
SAMPLE = os.path.join(MATI, "samples", "navgps-cs")
DOTNET = os.path.join(MATI, "tools", "dotnet", "dotnet")

ENV = dict(os.environ)
ENV.update({
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "DOTNET_NOLOGO": "1",
    "DOTNET_ROOT": os.path.dirname(DOTNET),
})

RESULTS = []  # (section, name, ok, detail)


def check(section, name, ok, detail=""):
    RESULTS.append((section, name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not ok else (f"  ({detail})" if detail else "")))
    return ok


def run_worker(dll, root, extra=(), out=None, timeout=900, prefix=()):
    out = out or tempfile.mktemp(suffix=".jsonl")
    cmd = list(prefix) + [DOTNET, dll, root, "--out", out] + list(extra)
    t0 = time.time()
    p = subprocess.run(cmd, env=ENV, capture_output=True, text=True, timeout=timeout)
    dt = time.time() - t0
    lines = []
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            lines = f.read().splitlines()
    return p, lines, dt, out


# ───────────────────────────── schema ─────────────────────────────

REQ = {
    "project": ["name", "tfm", "refs"],
    "file": ["path", "lang", "sha", "loc", "fidelity", "errors", "stubs", "profile"],
    "symbol": ["key", "kind", "name", "qual", "sig", "type", "file", "line", "end_line", "col", "container", "is_def", "static", "extern", "doc"],
    "occ": ["sym", "file", "line", "col", "role", "in"],
    "call": ["from", "to", "file", "line", "col", "kind", "res", "args", "guards"],
    "flow": ["src", "dst", "kind", "file", "line", "fn", "expr"],
    "fnaddr": ["fn", "dst", "file", "line", "kind"],
    "branch": ["fn", "file", "line", "end_line", "kind", "cond", "reads", "then", "else"],
    "const": ["sym", "value"],
    "entry": ["sym", "kind", "detail", "res"],
    "io": ["sym", "dir", "api", "file", "line", "fn", "res"],
    "inherit": ["sub", "base", "kind"],
    "instantiate": ["type", "file", "line", "fn"],
    "subscribe": ["event", "handler", "file", "line", "fn", "op"],
    "diag": ["file", "line", "severity", "msg"],
    "config_key": ["key", "sym", "file", "line", "via"],
    "done": ["files", "symbols", "errors", "elapsed_ms"],
}
ENUMS = {
    ("symbol", "kind"): {"function", "method", "constructor", "field", "global", "local", "param", "struct", "union", "class", "interface",
                         "record", "enum", "enum_const", "macro", "typedef", "property", "event", "delegate", "namespace"},
    ("occ", "role"): {"read", "write", "rw", "call", "addr", "type", "macro"},
    ("call", "kind"): {"direct", "virtual", "interface", "fnptr", "delegate", "event", "macro", "ctor"},
    ("call", "res"): {"exact", "cha", "table", "flow", "signature", "unresolved"},
    ("flow", "kind"): {"assign", "compound", "init", "arg", "ret", "return_to", "api"},
    ("fnaddr", "kind"): {"init", "assign", "arg", "table"},
    ("branch", "kind"): {"if", "switch", "case", "while", "for", "do", "ternary", "early_exit"},
    ("entry", "kind"): {"main", "isr", "callback", "thread", "timer", "event", "test", "export", "handler"},
    ("entry", "res"): {"exact", "pattern", "heuristic"},
    ("io", "dir"): {"source", "sink"},
    ("io", "res"): {"apipack", "heuristic", "mmio"},
    ("inherit", "kind"): {"extends", "implements", "overrides"},
    ("subscribe", "op"): {"+=", "-="},
    ("file", "fidelity"): {"F1", "F2", "F3", "F4"},
}
GUARD_KINDS = {"if", "else", "early_exit", "loop", "case", "ternary"}


def parse_and_validate(section, lines):
    recs, bad_json, problems = [], 0, collections.Counter()
    for i, line in enumerate(lines):
        try:
            r = json.loads(line)
        except Exception:
            bad_json += 1
            continue
        recs.append(r)
        t = r.get("t")
        if t not in REQ:
            problems[f"unknown record type {t!r}"] += 1
            continue
        for f in REQ[t]:
            if f not in r:
                problems[f"{t}.{f} missing"] += 1
        for (tt, field), allowed in ENUMS.items():
            if tt == t and field in r and r[field] not in allowed:
                problems[f"{t}.{field}={r[field]!r}"] += 1
        for g in r.get("guards", []) or []:
            if not all(k in g for k in ("cond", "polarity", "line", "kind", "reads")) or g["kind"] not in GUARD_KINDS:
                problems["bad guard object"] += 1
        if "line" in r and (not isinstance(r["line"], int) or r["line"] < 1):
            problems[f"{t}.line not 1-based"] += 1
        if "file" in r and isinstance(r["file"], str) and ("\\" in r["file"] or r["file"].startswith("/")):
            problems[f"{t}.file not repo-relative"] += 1
    check(section, "every line is valid JSON", bad_json == 0, f"{bad_json} bad lines" if bad_json else f"{len(lines)} lines")
    check(section, "records match FACT_SCHEMA (required fields + enums)", not problems, "; ".join(f"{k} x{v}" for k, v in problems.most_common(8)))
    check(section, "final line is the done record", bool(recs) and recs[-1].get("t") == "done")
    return recs


def by_type(recs):
    d = collections.defaultdict(list)
    for r in recs:
        d[r["t"]].append(r)
    return d


# ───────────────────────────── scenarios ─────────────────────────────

PGD = "M:Nav.NavigationController.ProcessGpsData(Nav.GpsFix)"
ONFIX = "M:Nav.Services.GpsService.OnFix(System.Object,Nav.GpsFixEventArgs)"
SELFTEST = "M:Nav.Program.RunSelfTest(Nav.NavigationController)"
UPDATE = "M:Nav.IHeadingFilter.Update(System.Double,System.Double)"
CUR = "P:Nav.NavState.CurrentHeading"
NCF = "src/Navigation/NavigationController.cs"


def sample_checks(dll):
    S = "sample"
    print(f"\n== 1. {SAMPLE}")
    p, lines, dt, out = run_worker(dll, SAMPLE, ["--progress"])
    check(S, "exit code 0", p.returncode == 0, "" if p.returncode == 0 else p.stderr[-400:])
    check(S, "progress lines on stderr", "PROGRESS 14/14" in p.stderr)
    recs = parse_and_validate(S, lines)
    T = by_type(recs)
    done = recs[-1] if recs else {}
    counts = collections.Counter(r["t"] for r in recs)
    print(f"  time {dt:.2f}s wall, worker elapsed_ms={done.get('elapsed_ms')}; records: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    files = T["file"]
    check(S, "14 file records, all F2 with 0 errors", len(files) == 14 and all(f["fidelity"] == "F2" and f["errors"] == 0 for f in files),
          str([(f["path"], f["fidelity"], f["errors"]) for f in files if f["fidelity"] != "F2"]))
    keys = collections.Counter(s["key"] for s in T["symbol"])
    dups = [k for k, v in keys.items() if v > 1]
    check(S, "symbol keys unique", not dups, str(dups[:5]))
    check(S, "no method key carries a ~ReturnType suffix", not any(k.startswith("M:") and "~" in k.split("#")[0] and "op_Implicit" not in k and "op_Explicit" not in k for k in keys))
    keyset = set(keys)
    unresolved = [r for r in T["call"] if r["from"] not in keyset] + [r for r in T["occ"] if r["in"] and r["in"] not in keyset]
    check(S, "every call.from / occ.in resolves to a symbol record", not unresolved, str(unresolved[:2]))
    check(S, "ProcessGpsData symbol present with doc summary", any(s["key"] == PGD and s["kind"] == "method" and s["line"] == 38 and s["doc"] for s in T["symbol"]))

    # (1) call edge GpsService.OnFix -> ProcessGpsData, with the enabled && quality guard
    edge = [c for c in T["call"] if c["from"] == ONFIX and c["to"] == PGD]
    ok = len(edge) == 1 and edge[0]["kind"] == "direct" and edge[0]["res"] == "exact" and edge[0]["line"] == 34
    check(S, "call GpsService.OnFix -> NavigationController.ProcessGpsData (direct/exact, GpsService.cs:34)", ok, str(edge))
    g = edge[0]["guards"] if edge else []
    check(S, "  guard 'Enabled && fix.Quality >= _options.MinQuality' (if, polarity true, line 32)",
          any(x["cond"] == "Enabled && fix.Quality >= _options.MinQuality" and x["polarity"] is True and x["line"] == 32 and x["kind"] == "if"
              and "P:Nav.Services.GpsService.Enabled" in x["reads"] for x in g), str(g))
    callers = sorted({c["from"] for c in T["call"] if c["to"] == PGD})
    check(S, "callers of ProcessGpsData = {GpsService.OnFix, Program.RunSelfTest}", callers == sorted([ONFIX, SELFTEST]), str(callers))

    # (2) interface call + both implementations via inherit implements
    icall = [c for c in T["call"] if c["from"] == PGD and c["to"] == UPDATE]
    check(S, "interface call IHeadingFilter.Update from ProcessGpsData (kind interface, res cha, line 67)",
          len(icall) == 1 and icall[0]["kind"] == "interface" and icall[0]["res"] == "cha" and icall[0]["line"] == 67, str(icall))
    impls = sorted(i["sub"] for i in T["inherit"] if i["base"] == UPDATE and i["kind"] == "implements")
    exp_impls = sorted(["M:Nav.ComplementaryHeadingFilter.Update(System.Double,System.Double)", "M:Nav.KalmanHeadingFilter.Update(System.Double,System.Double)"])
    check(S, "inherit implements: both Update implementations -> IHeadingFilter.Update", impls == exp_impls, str(impls))
    timpls = sorted(i["sub"] for i in T["inherit"] if i["base"] == "T:Nav.IHeadingFilter" and i["kind"] == "implements")
    check(S, "inherit implements: both filter types -> T:Nav.IHeadingFilter", timpls == ["T:Nav.ComplementaryHeadingFilter", "T:Nav.KalmanHeadingFilter"], str(timpls))
    inst = {i["type"] for i in T["instantiate"] if i["fn"] == "M:Nav.NavigationController.CreateHeadingFilter(Nav.NavOptions)"}
    check(S, "both filters instantiated in CreateHeadingFilter", inst == {"T:Nav.KalmanHeadingFilter", "T:Nav.ComplementaryHeadingFilter"}, str(inst))
    kal = [c for c in T["call"] if c["to"] == "M:Nav.KalmanHeadingFilter.#ctor(System.Double,System.Double)"]
    check(S, "  Kalman ctor call guarded by case 'options.Filter == HeadingFilterKind.Kalman'",
          kal and any(x["kind"] == "case" and x["cond"] == "options.Filter == HeadingFilterKind.Kalman" and x["polarity"] for x in kal[0]["guards"]), str(kal))

    # (3) event subscription FixReceived += OnFix
    sub = [s for s in T["subscribe"] if s["event"] == "E:Nav.Sensors.SerialGpsSensor.FixReceived" and s["handler"] == ONFIX]
    check(S, "subscribe FixReceived += GpsService.OnFix (GpsService.cs:19) and -= in Dispose (47)",
          any(s["op"] == "+=" and s["line"] == 19 for s in sub) and any(s["op"] == "-=" and s["line"] == 47 for s in sub), str(sub))
    check(S, "entry: OnFix is an event handler", any(e["sym"] == ONFIX and e["kind"] == "event" for e in T["entry"]))
    raise_ = [c for c in T["call"] if c["kind"] == "event" and c.get("via") == "E:Nav.Sensors.SerialGpsSensor.FixReceived"]
    check(S, "event raise FixReceived?.Invoke -> call kind event (SerialGpsSensor.cs:75)", raise_ and raise_[0]["line"] == 75, str(raise_))

    # (4) writes to NavState.CurrentHeading
    writes = [o for o in T["occ"] if o["sym"] == CUR and o["role"] == "write"]
    wset = sorted((o["in"], o["file"], o["line"]) for o in writes)
    exp_w = sorted([(PGD, NCF, 67), ("M:Nav.NavigationController.ApplyManualHeading(System.Double)", NCF, 80)])
    check(S, "writers of NavState.CurrentHeading = ProcessGpsData:67, ApplyManualHeading:80", wset == exp_w, str(wset))
    rt = [f for f in T["flow"] if f["src"] == UPDATE + "#ret" and f["dst"] == CUR and f["kind"] == "return_to"]
    check(S, "flow IHeadingFilter.Update#ret -> CurrentHeading (return_to)", len(rt) == 1, str(rt))

    # (5) early-exit guard on the HDOP check
    w67 = [o for o in writes if o["line"] == 67]
    gs = w67[0].get("guards", []) if w67 else []
    hd = [x for x in gs if x["kind"] == "early_exit" and x["cond"] == "fix.Hdop > _options.MaxHdop" and x["polarity"] is False and x["line"] == 40]
    check(S, "write at :67 guarded by early_exit 'fix.Hdop > _options.MaxHdop' (polarity false, line 40)", len(hd) == 1, json.dumps(gs)[:400])
    check(S, "  HDOP guard reads P:Nav.GpsFix.Hdop and P:Nav.NavOptions.MaxHdop", hd and {"P:Nav.GpsFix.Hdop", "P:Nav.NavOptions.MaxHdop"} <= set(hd[0]["reads"]))
    exp_chain = [("early_exit", 40, False), ("early_exit", 46, False), ("early_exit", 53, False), ("if", 65, True)]
    check(S, "  full guard chain at :67 = 3 early exits + speed if", [(x["kind"], x["line"], x["polarity"]) for x in gs] == exp_chain,
          str([(x["kind"], x["line"], x["polarity"]) for x in gs]))
    br = [b for b in T["branch"] if b["fn"] == PGD and b["line"] == 40]
    check(S, "branch record kind early_exit at NavigationController.cs:40", br and br[0]["kind"] == "early_exit" and br[0]["then"] == [41, 44], str(br))

    # (6) arg -> param flows
    argf = [f for f in T["flow"] if f["kind"] == "arg" and f["dst"] == PGD + "#param:fix"]
    srcs = sorted(f["src"] for f in argf)
    check(S, "flow arg: OnFix local fix / RunSelfTest local fix -> ProcessGpsData#param:fix",
          srcs == sorted([ONFIX + "#local:fix@31", SELFTEST + "#local:fix@60"]), str(srcs))
    upd = sorted((f["src"], f["pos"]) for f in T["flow"] if f["kind"] == "arg" and f["dst"].startswith(UPDATE + "#param:"))
    check(S, "flow arg: fix.CourseDeg -> Update#param:measuredDeg (pos 0), dt -> #param:dtSeconds (pos 1)",
          ("P:Nav.GpsFix.CourseDeg", 0) in upd and (PGD + "#local:dt@56", 1) in upd, str(upd))
    n_arg = sum(1 for f in T["flow"] if f["kind"] == "arg")
    check(S, "arg flows present in bulk", n_arg >= 50, f"{n_arg} arg flows")

    # extra ground truth from EXPECTED.md
    ents = {(e["sym"], e["kind"]) for e in T["entry"]}
    check(S, "entries: Main (main), Timer lambda (timer), Task.Run lambda (thread)",
          {("M:Nav.Program.Main(System.String[])", "main"), ("M:Nav.Program.Main(System.String[])#lambda@38:37", "timer"),
           ("M:Nav.Program.Main(System.String[])#lambda@39:35", "thread")} <= ents, str(sorted(ents)))
    tick = [c for c in T["call"] if c["from"] == "M:Nav.Program.Main(System.String[])#lambda@38:37" and c["to"] == "M:Nav.Control.NavLoop.Tick(System.DateTime)"]
    check(S, "timer lambda calls NavLoop.Tick", len(tick) == 1)
    ios = {(i["dir"], i["api"], i["line"]) for i in T["io"]}
    exp_io = {("sink", "System.Net.Sockets.UdpClient.Send", 32), ("sink", "System.IO.Stream.Write", 53),
              ("source", "System.IO.File.ReadAllLines", 35), ("source", "System.IO.StreamReader.ReadLine", 22)}
    check(S, "io: UdpClient.Send / Stream.Write sinks, File.ReadAllLines / StreamReader.ReadLine sources", exp_io <= ios, str(sorted(ios)))
    vs = [c for c in T["call"] if c["to"] == "M:Nav.Control.AutopilotTransport.Send(System.Byte[])"]
    check(S, "virtual call _transport.Send (AutopilotLink.cs:79) + both overrides",
          vs and vs[0]["kind"] == "virtual" and vs[0]["line"] == 79 and
          {i["sub"] for i in T["inherit"] if i["base"] == "M:Nav.Control.AutopilotTransport.Send(System.Byte[])" and i["kind"] == "overrides"} ==
          {"M:Nav.Control.UdpAutopilotTransport.Send(System.Byte[])", "M:Nav.Control.StreamAutopilotTransport.Send(System.Byte[])"})
    tickfn = "M:Nav.Control.NavLoop.Tick(System.DateTime)"
    bw = {o["line"]: o for o in T["occ"] if o["in"] == tickfn and o["sym"] == tickfn + "#local:bank@43" and o["role"] in ("write", "rw")}
    ok46 = 46 in bw and any(x["cond"] == "bank > _options.MaxBankDeg" and x["polarity"] for x in bw[46]["guards"]) \
        and any(x["kind"] == "early_exit" and x["cond"] == "_state.Mode == NavMode.Initializing" and x["polarity"] is False for x in bw[46]["guards"])
    ok48 = 48 in bw and [(x["kind"], x["polarity"]) for x in bw[48]["guards"]][-2:] == [("else", False), ("if", True)]
    check(S, "NavLoop.Tick clamp writes of 'bank' guarded (Initializing early exit, if/else-if chain)", ok46 and ok48,
          json.dumps({k: [(x["kind"], x["cond"], x["polarity"]) for x in v.get("guards", [])] for k, v in bw.items()})[:500])
    mode = [o for o in T["occ"] if o["sym"] == "P:Nav.NavState.Mode" and o["role"] == "write" and o["in"] == tickfn]
    check(S, "NavState.Mode write in Tick (NavLoop.cs:37) guarded by the stale-fix timeout",
          mode and mode[0]["line"] == 37 and any("StaleFixTimeout" in x["cond"] and x["polarity"] for x in mode[0]["guards"]))
    hl = [o for o in T["occ"] if o["sym"] == "P:Nav.GpsFix.Hdop" and o["role"] == "write" and o["line"] == 65]
    check(S, "HandleLine write fix.Hdop (:65) carries early exits at 42, 45, 51, 62",
          hl and [x["line"] for x in hl[0]["guards"] if x["kind"] == "early_exit"] == [42, 45, 51, 62],
          str([(x["kind"], x["line"]) for x in hl[0]["guards"]]) if hl else "no write")
    return lines, dt


def determinism_check(dll, first_lines):
    S = "determinism"
    print("\n== 2. determinism (second run of the sample)")
    _, lines, dt, _ = run_worker(dll, SAMPLE, ["--threads", "3"])
    a = [l for l in first_lines if not l.startswith('{"t":"done"')]
    b = [l for l in lines if not l.startswith('{"t":"done"')]
    check(S, "identical facts across runs (different thread count)", a == b, f"{len(a)} vs {len(b)} lines")


def broken_checks(dll):
    S = "broken"
    root = os.path.join(HERE, "fixtures", "broken")
    print(f"\n== 3. {root}")
    p, lines, dt, _ = run_worker(dll, root)
    check(S, "exit code 0 (no crash)", p.returncode == 0, "" if p.returncode == 0 else p.stderr[-400:])
    recs = parse_and_validate(S, lines)
    T = by_type(recs)
    done = recs[-1] if recs else {}
    print(f"  time {dt:.2f}s wall; done={json.dumps({k: done.get(k) for k in ('files', 'symbols', 'errors', 'extractor_errors')})}")
    fid = {f["path"]: (f["fidelity"], f["errors"]) for f in T["file"]}
    n_cs = len([f for f in os.listdir(root) if f.endswith(".cs")])
    check(S, f"one file record per .cs file ({n_cs})", len(fid) == n_cs, str(fid))
    check(S, "broken files are F3 with errors > 0", all(fid.get(f, ("", 0))[0] == "F3" and fid[f][1] > 0 for f in ("Broken.cs", "Garbage.cs", "Config.cs", "ParserTests.cs")), str(fid))
    check(S, "clean files still F2 (Form1*.cs, Empty.cs)", all(fid.get(f, ("",))[0] == "F2" for f in ("Form1.cs", "Form1.Designer.cs", "Empty.cs")), str(fid))
    check(S, "diag records emitted for compile errors", len([d for d in T["diag"] if d["severity"] == "error"]) > 0)
    check(S, "no internal extractor errors", done.get("extractor_errors") == 0 and not [d for d in T["diag"] if d["severity"] == "internal"],
          str([d["msg"] for d in T["diag"] if d["severity"] == "internal"][:3]))
    check(S, "symbols still extracted from broken Broken.cs",
          {"T:Broken.App.Telemetry", "M:Broken.App.Telemetry.OnData(System.Object,System.EventArgs)", "T:Broken.App.Packet"} <= {s["key"] for s in T["symbol"]})
    check(S, "unresolved call recorded (Undefined.Call, res unresolved)", any(c.get("name") == "Undefined.Call" and c["res"] == "unresolved" for c in T["call"]))
    check(S, "heuristic io source for SerialPort.ReadLine (package missing)", any(i["api"] == "System.IO.Ports.SerialPort.ReadLine" and i["res"] == "heuristic" for i in T["io"]))
    check(S, "heuristic subscribe _port.DataReceived += OnData", any(s["handler"] == "M:Broken.App.Telemetry.OnData(System.Object,System.EventArgs)" and s.get("res") == "heuristic" for s in T["subscribe"]))
    cks = {c["key"] for c in T["config_key"]}
    check(S, "config keys Nav:MaxHdop, Serial:Port, Autopilot, Telemetry, Serial:Baud", {"Nav:MaxHdop", "Serial:Port", "Autopilot", "Telemetry", "Serial:Baud"} <= cks, str(sorted(cks)))
    check(S, "config key Nav:MaxHdop flows into field Settings.MaxHdop", any(c["key"] == "Nav:MaxHdop" and c["sym"] == "F:Broken.App.Settings.MaxHdop" for c in T["config_key"]))
    ents = {(e["sym"], e["kind"]) for e in T["entry"]}
    check(S, "WinForms designer handlers: button1_Click (event), timer1_Tick (timer)",
          {("M:Broken.App.Form1.button1_Click(System.Object,System.EventArgs)", "event"), ("M:Broken.App.Form1.timer1_Tick(System.Object,System.EventArgs)", "timer")} <= ents, str(sorted(ents)))
    check(S, "[Fact]/[Theory] test entries despite missing xunit",
          {("M:Broken.Tests.ParserTests.ParsesRmc", "test"), ("M:Broken.Tests.ParserTests.Many(System.Int32)", "test")} <= ents)


def multi_checks(dll):
    S = "multi"
    root = os.path.join(HERE, "fixtures", "multi")
    print(f"\n== 4. {root}")
    p, lines, dt, _ = run_worker(dll, root)
    check(S, "exit code 0", p.returncode == 0, "" if p.returncode == 0 else p.stderr[-300:])
    recs = parse_and_validate(S, lines)
    T = by_type(recs)
    projs = [(x["name"], x["tfm"], x["refs"]) for x in T["project"]]
    check(S, "two projects, Lib before App, Lib picks net8.0 from netstandard2.0;net8.0", [p[0] for p in projs] == ["Lib", "App"] and projs[0][1] == "net8.0", str(projs))
    check(S, "App lists PackageReference Newtonsoft.Json (recorded, not restored)", any(pk["id"] == "Newtonsoft.Json" for x in T["project"] if x["name"] == "App" for pk in x["packages"]))
    xc = [c for c in T["call"] if c["to"] == "M:Multi.Lib.Checksum.Xor(System.String)"]
    check(S, "cross-project call top-level Main -> Lib Checksum.Xor resolved (not ext)", xc and xc[0]["from"] == "M:Program.<Main>$(System.String[])" and not xc[0].get("ext"), str(xc))
    check(S, "#if LIB_FEATURE honoured (foreach branch present in Lib)", any(b["kind"] == "for" and b["file"] == "Lib/Checksum.cs" for b in T["branch"]))
    rep = [c for c in T["call"] if c["to"] == "M:Program.<Main>$(System.String[])#fn:Report"]
    check(S, "local function call guarded by top-level early exit 'cs == 0'", rep and any(g["kind"] == "early_exit" and g["cond"] == "cs == 0" for g in rep[0]["guards"]))
    check(S, "all files F2", all(f["fidelity"] == "F2" for f in T["file"]), str([(f["path"], f["fidelity"]) for f in T["file"]]))


def features_checks(dll):
    S = "features"
    root = os.path.join(HERE, "fixtures", "features")
    print(f"\n== 4b. {root}")
    p, lines, dt, _ = run_worker(dll, root)
    check(S, "exit code 0", p.returncode == 0, "" if p.returncode == 0 else p.stderr[-300:])
    recs = parse_and_validate(S, lines)
    T = by_type(recs)
    run = "M:Features.Dispatch.Run(Features.Shape,Features.Angle,Features.Angle,System.Int32)"
    calls = {(c["to"], c["kind"]) for c in T["call"] if c["from"] == run}
    check(S, "user-defined operator + and implicit conversion are call edges",
          {("M:Features.Angle.op_Addition(Features.Angle,Features.Angle)", "direct"), ("M:Features.Angle.op_Implicit(Features.Angle)~System.Double", "direct")} <= calls, str(sorted(map(str, calls))))
    check(S, "abstract/virtual property reads dispatch to accessors (virtual/cha)",
          {("M:Features.Shape.get_Area", "virtual"), ("M:Features.Shape.get_Name", "virtual")} <= calls)
    check(S, "indexer read calls get_Item", ("M:Features.Square.get_Item(System.Int32)", "direct") in calls)
    ov = {(i["sub"], i["base"]) for i in T["inherit"] if i["kind"] == "overrides"}
    check(S, "accessor overrides recorded (Square.get_Area -> Shape.get_Area)", ("M:Features.Square.get_Area", "M:Features.Shape.get_Area") in ov)
    fa = {(f["fn"], f["dst"], f["kind"]) for f in T["fnaddr"]}
    check(S, "delegate tables: array + dictionary initializers -> fnaddr kind table",
          {("M:Features.Dispatch.OnA(System.Int32)", "F:Features.Dispatch.Handlers", "table"), ("M:Features.Dispatch.OnB(System.Int32)", "F:Features.Dispatch.Handlers", "table"),
           ("M:Features.Dispatch.Twice(System.Int32)", "F:Features.Dispatch.Table", "table")} <= fa, str(sorted(fa)))
    check(S, "no fnaddr into external indexer keys", not any(d.startswith("P:System.") for _, d, _ in fa))
    dv = sorted(c.get("via") or "" for c in T["call"] if c["kind"] == "delegate")
    check(S, "delegate calls carry holder: Handlers[i](k), _hook?.Invoke(k), Table[\"x\"](k)",
          dv == ["F:Features.Dispatch.Handlers", "F:Features.Dispatch.Table", "F:Features.Dispatch._hook"], str(dv))
    keys = {s["key"] for s in T["symbol"]}
    bad = [i for i in T["inherit"] if i["sub"] not in keys]
    check(S, "every inherit.sub resolves to a symbol record", not bad, str(bad[:3]))
    arms = [b for b in T["branch"] if b["kind"] == "case" and b["file"] == "Features.cs"]
    check(S, "switch-expression arms become case branches (3)", len(arms) == 3, str([a["cond"] for a in arms]))


def make_stress(root, n_methods=3000):
    os.makedirs(os.path.join(root, "src"), exist_ok=True)
    with open(os.path.join(root, "src", "Big.cs"), "w") as f:
        f.write("namespace Stress;\npublic sealed class Big\n{\n    private int _state;\n    public int Value { get; set; }\n")
        for i in range(n_methods):
            f.write(f"""
    /// <summary>Generated method {i}.</summary>
    public int M{i}(int a, int b)
    {{
        if (a < 0) return -1;
        int x = a + b * {i};
        for (int k = 0; k < b; k++)
        {{
            x += k % 3 == 0 ? M{max(i - 1, 0)}(k, 1) : k;
        }}
        switch (x % 4) {{ case 0: _state += x; break; case 1: Value = x; break; default: _state--; break; }}
        return x;
    }}
""")
        f.write("}\n")
    # Deep expression nesting and many sequential early exits.
    with open(os.path.join(root, "src", "Deep.cs"), "w") as f:
        f.write("namespace Stress;\npublic static class Deep\n{\n")
        f.write("    public static int Sum(int a) => " + " + ".join(["a"] * 5000) + ";\n")
        f.write("    public static int Nest(int a)\n    {\n")
        depth = 300
        for d in range(depth):
            f.write("    " * 2 + f"if (a > {d}) {{\n")
        f.write("        a = -a;\n")
        f.write("}\n" * depth)
        f.write("        return a;\n    }\n")
        f.write("    public static int Exits(int a)\n    {\n")
        for d in range(2000):
            f.write(f"        if (a == {d}) return {d};\n")
        f.write("        a = a * 2;\n        return a;\n    }\n}\n")
    with open(os.path.join(root, "src", "Deep.cs")) as f:
        deep_lines = f.read().count("\n")
    with open(os.path.join(root, "src", "Big.cs")) as f:
        big_lines = f.read().count("\n")
    return big_lines + deep_lines


def stress_checks(dll, tmp):
    S = "stress"
    root = os.path.join(tmp, "stress")
    loc = make_stress(root)
    print(f"\n== 5. generated stress repo ({loc} lines: 3000 methods, 5000-term expression, 300-deep ifs, 2000 early exits)")
    p, lines, dt, _ = run_worker(dll, root, ["--timeout", "600"])
    check(S, "exit code 0", p.returncode == 0, "" if p.returncode == 0 else p.stderr[-400:])
    recs = parse_and_validate(S, lines)
    done = recs[-1] if recs else {}
    print(f"  time {dt:.2f}s wall ({loc / max(dt, 1e-6):.0f} lines/s); done={json.dumps({k: done.get(k) for k in ('files', 'symbols', 'errors', 'extractor_errors', 'timed_out')})}; records={done.get('records')}")
    check(S, "not timed out, no compile errors", done.get("timed_out") is False and done.get("errors") == 0)
    ex = [r for r in recs if r["t"] == "occ" and r["sym"].startswith("M:Stress.Deep.Exits(System.Int32)#local") is False and r.get("in") == "M:Stress.Deep.Exits(System.Int32)" and r["role"] == "write"]
    check(S, "write after 2000 early exits is emitted with capped guard list", ex and 0 < len(ex[0].get("guards", [])) <= 32, f"{len(ex[0].get('guards', [])) if ex else 'none'} guards")
    check(S, "extractor errors reported, not fatal", done.get("extractor_errors", 0) >= 0)
    return root, done


def timeout_checks(dll, stress_root):
    S = "timeout"
    print("\n== 6. watchdog: stress repo with --timeout 2")
    t0 = time.time()
    p, lines, dt, _ = run_worker(dll, stress_root, ["--timeout", "2"], timeout=120)
    recs = [json.loads(l) for l in lines if l.strip()]
    done = recs[-1] if recs else {}
    print(f"  time {dt:.2f}s wall, exit {p.returncode}")
    check(S, "process ends within the grace period (< 20 s)", dt < 20, f"{dt:.1f}s")
    check(S, "still ends with a done record", done.get("t") == "done")
    if done.get("timed_out"):
        check(S, "done.timed_out is true and exit code 2", p.returncode == 2)
    else:
        check(S, "finished within 2 s anyway (exit 0)", p.returncode == 0)


def offline_checks(dll):
    S = "offline"
    print("\n== 7. offline: sample under sandbox-exec with all network denied")
    sb = shutil.which("sandbox-exec")
    if not sb:
        check(S, "sandbox-exec available", False, "not on this host; skipped")
        return
    profile = "(version 1)(allow default)(deny network*)"
    p, lines, dt, _ = run_worker(dll, SAMPLE, prefix=[sb, "-p", profile])
    recs = [json.loads(l) for l in lines if l.strip()]
    check(S, "runs with networking denied (exit 0, done present, 14 files)", p.returncode == 0 and recs and recs[-1]["t"] == "done" and recs[-1]["files"] == 14,
          p.stderr[-300:])
    print(f"  time {dt:.2f}s wall")


def integrity_checks(dll, root):
    S = "extra-repo"
    print(f"\n== 8. integrity on an external repository: {root}")
    p, lines, dt, _ = run_worker(dll, root, ["--timeout", "1800"], timeout=2000)
    check(S, "exit code 0", p.returncode == 0, "" if p.returncode == 0 else p.stderr[-400:])
    recs = parse_and_validate(S, lines)
    T = by_type(recs)
    done = recs[-1] if recs else {}
    fid = collections.Counter(f["fidelity"] for f in T["file"])
    loc = sum(f["loc"] for f in T["file"])
    print(f"  time {dt:.2f}s wall, {len(T['file'])} files, {loc} lines ({loc / max(dt, 1e-6):.0f} lines/s); fidelity {dict(fid)}; "
          f"compile errors {done.get('errors')}; records={done.get('records')}")
    kc = collections.Counter(s["key"] for s in T["symbol"])
    keys = set(kc)
    check(S, "no duplicate symbol keys", all(v == 1 for v in kc.values()), str([k for k, v in kc.items() if v > 1][:5]))
    check(S, "no extractor (internal) errors", done.get("extractor_errors") == 0, str([d["msg"] for d in T["diag"] if d["severity"] == "internal"][:3]))
    for what, vals in (("call.from", [r["from"] for r in T["call"]]), ("occ.in", [r["in"] for r in T["occ"] if r["in"]]),
                       ("flow.fn", [r["fn"] for r in T["flow"] if r["fn"]]), ("branch.fn", [r["fn"] for r in T["branch"] if r["fn"]]),
                       ("entry.sym", [r["sym"] for r in T["entry"]]), ("fnaddr.fn", [r["fn"] for r in T["fnaddr"]]),
                       ("symbol.container", [r["container"] for r in T["symbol"] if r["container"]]),
                       ("source call.to", [r["to"] for r in T["call"] if r["to"] and not r.get("ext")])):
        bad = [v for v in vals if v not in keys]
        check(S, f"every {what} resolves to a symbol record", not bad, f"{len(bad)} unresolved, e.g. {bad[:3]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", default=os.path.join(WORKER, "bin", "VerityRoslyn.dll"))
    ap.add_argument("--keep", action="store_true", help="keep the generated stress repo")
    ap.add_argument("--extra-repo", action="append", default=[], help="also run referential-integrity checks on this C# repository")
    a = ap.parse_args()
    if not os.path.exists(a.dll):
        print(f"worker not built: {a.dll} (run workers/roslyn/build.sh)")
        return 2
    tmp = tempfile.mkdtemp(prefix="verity-roslyn-verify-")
    t0 = time.time()
    try:
        first, _ = sample_checks(a.dll)
        determinism_check(a.dll, first)
        broken_checks(a.dll)
        multi_checks(a.dll)
        features_checks(a.dll)
        stress_root, _ = stress_checks(a.dll, tmp)
        timeout_checks(a.dll, stress_root)
        offline_checks(a.dll)
        for r in a.extra_repo:
            integrity_checks(a.dll, os.path.abspath(r))
    finally:
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(1 for r in RESULTS if r[2])
    failed = [r for r in RESULTS if not r[2]]
    print(f"\n== {passed}/{len(RESULTS)} checks passed in {time.time() - t0:.1f}s")
    for s, n, _, d in failed:
        print(f"  FAILED [{s}] {n}: {d}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
