"""Robustness (design §21): interrupted/cancelled/corrupted indexes, stale files, malformed and huge inputs,
missing headers, concurrency, invalid input, path safety."""
import os
import shutil
import threading
import time

import pytest

from conftest import ROOT, ask
from verity.engine.core import EngineError
from verity.index import Cancelled, IndexBusy, Indexer


def fresh_copy(tmp_path, name="navgps-c"):
    d = tmp_path / name
    shutil.copytree(ROOT / "samples" / name, d)
    return d


def test_incremental_reindex_only_changed_file(tmp_path):
    repo = fresh_copy(tmp_path)
    ix = Indexer(repo)
    ix.run(full=True)
    s2 = Indexer(repo).run()
    assert s2["phases"]["c"]["detail"].startswith("No C changes")
    f = repo / "nav" / "geo_math.c"
    f.write_text(f.read_text() + "\n/* touched */\n")
    s3 = Indexer(repo).run()
    assert s3["phases"]["c"]["files"] == 1


def test_header_change_reindexes_includers(tmp_path):
    repo = fresh_copy(tmp_path)
    Indexer(repo).run(full=True)
    h = repo / "nav" / "geo_math.h"
    h.write_text(h.read_text() + "\n/* touched */\n")
    s = Indexer(repo).run()
    assert s["phases"]["c"]["files"] >= 2          # geo_math.c + navigation.c + vehicle_controller.c include it


def test_cancel_then_resume(tmp_path):
    repo = fresh_copy(tmp_path)
    ev = threading.Event()
    seen = []

    def prog(e):
        seen.append(e)
        if e["phase"] == "c" and e.get("done"):
            ev.set()
    ix = Indexer(repo, on_progress=prog, cancel=ev)
    with pytest.raises(Cancelled):
        ix.run(full=True)
    assert ix.store.meta_get("index_state") == "cancelled"
    s = Indexer(repo).run()                       # resumes: the rest of the files are read
    assert s["state"] == "complete"
    assert Indexer(repo).store.one("SELECT count(*) n FROM file WHERE lang='c' AND path LIKE '%.c'")["n"] == 9


def test_concurrent_indexers_are_serialised(tmp_path):
    repo = fresh_copy(tmp_path)
    a = Indexer(repo)
    a._acquire()
    try:
        with pytest.raises(IndexBusy):
            Indexer(repo).run()
    finally:
        a._release()


def test_malformed_and_garbage_files_do_not_break_indexing(tmp_path):
    repo = fresh_copy(tmp_path)
    (repo / "broken.c").write_text("int f( { return ;;; }}} #include <nope.h>\nvoid g(void) { h(1); }\n")
    (repo / "binary.c").write_bytes(os.urandom(4000))
    (repo / "empty.c").write_text("")
    s = Indexer(repo).run(full=True)
    assert s["state"] == "complete"
    st = Indexer(repo).store
    assert st.one("SELECT 1 FROM symbol WHERE name='g'")            # still extracted something from broken.c
    assert st.one("SELECT count(*) n FROM symbol WHERE name='ProcessGpsData'")["n"] == 1


def test_huge_file_is_indexed_by_name_only(tmp_path):
    repo = fresh_copy(tmp_path)
    big = repo / "huge_table.c"
    with open(big, "w") as f:
        f.write("const int table[] = {\n")
        for i in range(700_000):
            f.write(f"{i},\n")
        f.write("};\n")
    assert big.stat().st_size > 4 * 1024 * 1024
    s = Indexer(repo).run(full=True)
    assert s["state"] == "complete" and any("very large" in w for w in s["warnings"])


def test_missing_headers_are_stubbed(core, c_indexed):
    st = core.project(str(c_indexed)).store
    f = st.one("SELECT fidelity, stubs FROM file WHERE path='drivers/uart_isr.c'")
    assert f["fidelity"] == "F3" and "USART2" in f["stubs"]
    # claims built on that file are capped below CONFIRMED
    ans = ask(core, c_indexed, "Which function receives the GPS packet?")
    raw = [c for c in ans["sections"]["reasoning"] if "uart_isr.c" in c["text"] or "USART2_IRQHandler" in c["text"]]
    assert raw and all(c["status"] != "CONFIRMED" for c in raw)


def test_stale_file_is_detected_and_capped(core, tmp_path):
    repo = fresh_copy(tmp_path)
    j = core.start_index(str(repo), full=True)
    assert core.wait(j["id"], 300)["state"] == "done"
    f = repo / "nav" / "navigation.c"
    txt = f.read_text()
    f.write_text(txt.replace("currentHeading = CalculateHeading(", "currentHeading =  CalculateHeading("))
    st = core.project_status(str(repo))
    assert "nav/navigation.c" in st["stale"]
    ans = ask(core, repo, "What does ProcessGpsData() do?")
    rep = ans["sections"]["verification"]["report"]
    assert "nav/navigation.c" in rep["stale_files"]
    # nothing citing the changed file may be presented as CONFIRMED
    for c in ans["sections"]["reasoning"]:
        if any(e["file"] == "nav/navigation.c" for e in ans["evidence"] if e["id"] in c["ev"]):
            assert c["status"] != "CONFIRMED"
    # now shift every line: indexed line numbers point at the wrong code and the citation check must reject claims
    f.write_text("/* inserted */\n" + f.read_text())
    ans = ask(core, repo, "What does ProcessGpsData() do?")
    assert ans["sections"]["verification"]["report"]["rejected"] >= 1


def test_invalid_inputs(core, c_indexed, tmp_path):
    with pytest.raises(EngineError):
        core.start_ask(str(c_indexed), "   ")
    with pytest.raises(EngineError):
        core.project(str(tmp_path / "does-not-exist"))
    with pytest.raises(EngineError):
        core.source(str(c_indexed), "../../etc/passwd")
    with pytest.raises(EngineError):
        core.start_ask(str(tmp_path), "anything")          # not indexed


def test_empty_folder(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    s = Indexer(d).run()
    assert s["state"] == "complete" and "No C or C# source files" in s["phases"]["scan"]["detail"]


def test_concurrent_questions(core, c_indexed):
    qs = ["Who calls CalculateHeading?", "What is the purpose of currentHeading?", "Where does latitude come from?",
          "What does ProcessGpsData() do?"]
    jobs = [core.start_ask(str(c_indexed), q) for q in qs]
    res = [core.wait(j["id"], 120) for j in jobs]
    assert all(r["state"] == "done" for r in res)


def test_ask_can_be_cancelled(core, c_indexed):
    j = core.start_ask(str(c_indexed), "What happens when GPS sends heading 45, speed 60, valid true?")
    core.cancel(j["id"])
    r = core.wait(j["id"], 60)
    assert r["state"] in ("cancelled", "done")        # finished before the cancel landed, or cancelled


def test_worker_crash_is_contained(tmp_path, monkeypatch):
    """A parser crash (native segfault) costs one file, not the index."""
    repo = fresh_copy(tmp_path)
    (repo / "crash_me.c").write_text("int VERITY_TEST_CRASH;\n")
    monkeypatch.setenv("VERITY_TEST_CRASH_FILE", "crash_me.c")
    s = Indexer(repo).run(full=True)
    assert s["state"] == "complete"
    st = Indexer(repo).store
    assert st.one("SELECT fidelity FROM file WHERE path='crash_me.c'")["fidelity"] == "F4"
    assert st.one("SELECT count(*) n FROM symbol WHERE name='ProcessGpsData'")["n"] == 1
