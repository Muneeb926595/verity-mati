"""Uncompiled MSVC/Win32 and TI C2000 code analysed WITHOUT their SDK/device headers (knowledge-pack shims and the
usage-guided stub loop), including sandboxed runs of that code on a non-target machine."""
import json
import shutil

import pytest

from conftest import ROOT, ask, needs_sandbox
from verity.bench.run import grade, vocabulary
from verity.extract.recon import add_stub_declarations

SUITE = json.loads((ROOT / "bench" / "dialects.json").read_text())


@pytest.fixture(scope="module")
def dialect_repos(core, tmp_path_factory):
    out = {}
    for k, rel in SUITE["projects"].items():
        d = tmp_path_factory.mktemp("dialect") / rel.split("/")[-1]
        shutil.copytree(ROOT / rel, d)
        j = core.start_index(str(d), full=True)
        assert core.wait(j["id"], 600)["state"] == "done"
        out[k] = d
    return out


def test_msvc_parses_cleanly_without_windows_sdk(core, dialect_repos):
    st = core.project(str(dialect_repos["msvc"])).store
    rows = st.q("SELECT path, fidelity, errors FROM file WHERE path LIKE '%.c'")
    assert rows and all(r["fidelity"] == "F2" and r["errors"] == 0 for r in rows), [dict(r) for r in rows]
    assert st.meta_get("c_profile")["shim_dirs"]


def test_ti_register_blocks_are_synthesised(core, dialect_repos):
    st = core.project(str(dialect_repos["ti"])).store
    rows = st.q("SELECT fidelity, errors FROM file WHERE path LIKE '%.c'")
    assert all(r["errors"] == 0 for r in rows) and all(r["fidelity"] == "F3" for r in rows)   # stand-ins => F3, honest
    isr = {r["sym"]: r["res"] for r in st.q("SELECT sym, res FROM entry WHERE kind='isr'")}
    assert isr == {"c:@F@adc_isr": "keyword", "c:@F@epwm1_isr": "keyword"}


RUNS = ("run_state", "run_state_any", "run_state_absent", "hw_calls")      # cases checked against a real run


@pytest.mark.parametrize("case", [pytest.param(c, marks=needs_sandbox) if any(k in c for k in RUNS) else c
                                  for c in SUITE["cases"]], ids=[c["id"] for c in SUITE["cases"]])
def test_dialect_case(core, dialect_repos, case):
    repo = dialect_repos[case["project"]]
    ans = ask(core, repo, case["q"])
    g = grade(case, ans, vocabulary(core.project(str(repo)).store))
    failing = {k: v for k, v in g["checks"].items() if not k.startswith("_") and not v["ok"]}
    assert g["passed"], (failing, ans["answer"])


def test_stub_loop_classifies_by_usage(tmp_path):
    pre = tmp_path / "p.h"
    pre.write_text("")
    text = ("Uint16 raw = AdcResult.ADCRESULT0; float32 x = 1.0f; GpioDataRegs.GPASET.bit.GPIO31 = 1;"
            "void f(_In_ int a, _Out_writes_(n) char *b); InitSysCtrl();")
    diags = ["unknown type name 'Uint16'", "unknown type name 'float32'", "use of undeclared identifier 'AdcResult'",
             "use of undeclared identifier 'GpioDataRegs'", "unknown type name '_In_'", "use of undeclared identifier '_Out_writes_'"]
    add_stub_declarations(str(pre), diags, text, set())
    out = pre.read_text()
    assert "typedef unsigned int Uint16;" in out and "typedef float float32;" in out
    assert "#define _In_" in out
    # types first: identifiers wait for the next round, so locals are never stubbed as globals
    add_stub_declarations(str(pre), diags[2:4] + ["use of undeclared identifier '_Out_writes_'"], text, set())
    out = pre.read_text()
    assert "struct { struct { volatile unsigned long long GPIO31; } bit; } GPASET;" in out
    assert "extern volatile struct __vr_regs_AdcResult AdcResult;" in out
    assert "#define _Out_writes_(...)" in out
