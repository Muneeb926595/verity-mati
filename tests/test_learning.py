"""Learning from corrections (Phase 5): pin, hint, glossary, test cases; anchors and stale corrections; undo; export,
import and the bench suite; learned.db surviving an index reset; every client surface (RPC, CLI, MCP)."""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import ROOT, ask
from verity.engine.core import EngineError
from verity.learn.apply import Learning, Loc
from verity.learn.store import LearnError, LearnStore

Q = "What happens when the checksum of an NMEA sentence is wrong?"
TARGET = {"file": "gps/nmea_parser.c", "line": 26}          # `if (*p != '*' || hex_value(p[1]) < 0 || …)`


def fresh(core, tmp_path, name="navgps-c"):
    """A private, indexed copy of the C sample (its own learned.db)."""
    d = tmp_path / name
    shutil.copytree(ROOT / "samples" / "navgps-c", d)
    r = core.wait(core.start_index(str(d), full=True)["id"], 600)
    assert r["state"] == "done", r
    return d


def reindex(core, repo):
    r = core.wait(core.start_index(str(repo))["id"], 600)
    assert r["state"] == "done", r
    return r


def lm(ans):
    return ans["meta"].get("learning") or {}


def text(ans):
    return " ".join(ans["answer"])


# ------------------------------------------------------------------------------------------------ units
def test_store_ids_updates_and_corruption_recovery(tmp_path):
    ls = LearnStore(tmp_path / "learned.db")
    a, _ = ls.next_id()
    b, _ = ls.next_id()
    assert (a, b) == ("c-0001", "c-0002")
    ls.add_correction({"id": a, "seq": 1, "question": "q", "target_fn": "f", "status": "active", "topic": ["x"],
                       "conflict": []})
    assert ls.correction(a)["topic"] == ["x"]
    assert ls.update_correction(a, status="stale", status_reason="r")["status"] == "stale"
    assert [e["action"] for e in ls.events()] == ["correct"]
    ls.close()
    (tmp_path / "learned.db").write_bytes(b"not a database at all" * 100)
    ls2 = LearnStore(tmp_path / "learned.db")
    assert ls2.recovered_from and Path(ls2.recovered_from).is_file()       # the damaged file is kept, never deleted
    assert ls2.corrections() == []
    ls2.close()


def test_rank_hint_bonus_and_pin_order():
    class R:
        intent, topic = "failure_invalid", ["checksum", "sentence", "wrong"]
    lr = Learning(None, R, "x")
    lr.hints = [{"id": "c-0001", "target_fn": "f2", "branch_line": 20, "side": "then", "target_line": 20, "seq": 1}]
    items = [("f1", 10, 10.0), ("f2", 20, 7.0), ("f3", 30, 2.0)]
    loc = lambda it: Loc(it[0], "a.c", it[1], "then")          # noqa: E731
    out = lr.rank(items, loc, lambda it: it[2])
    assert [i[0] for i in out] == ["f2", "f1", "f3"]           # 7 * 1.5 = 10.5 beats 10
    assert not lr.used                                         # disclosed once the final order is known
    lr.offer(out, loc, lambda it: (it[0], it[0]), lambda it: it[2])
    assert lr.used["c-0001"]["how"] == "hint" and lr.used["c-0001"]["match"] == "similar"
    over = Learning(None, R, "x")                               # the local model then chose another candidate
    over.hints = lr.hints
    ranked = over.rank(items, loc, lambda it: it[2])
    over.offer([ranked[1], ranked[0], ranked[2]], loc, lambda it: (it[0], it[0]), lambda it: it[2])
    assert not over.used and not over.meta(True)["used"]        # the hint changed nothing: not disclosed, no Undo
    quiet = Learning(None, R, "x")                              # a recipe that offers nothing: disclosed by meta()
    quiet.hints = lr.hints
    quiet.rank(items, loc, lambda it: it[2])
    assert [u["id"] for u in quiet.meta(True)["used"]] == ["c-0001"]
    lr2 = Learning(None, R, "x")
    lr2.hints = lr.hints
    far = [("f1", 10, 20.0), ("f2", 20, 7.0)]
    assert [i[0] for i in lr2.rank(far, loc, lambda it: it[2])] == ["f1", "f2"]   # never outweighs a clearly better one
    assert not lr2.used
    lr3 = Learning(None, R, "x")
    lr3.pins = [{"id": "c-0002", "target_fn": "f3", "branch_line": 30, "side": "then", "target_line": 31, "seq": 2}]
    assert [i[0] for i in lr3.pin(items, loc)] == ["f3", "f1", "f2"] and lr3.pin_applied
    lr4 = Learning(None, R, "x")
    lr4.pins = [{"id": "c-0003", "target_fn": "f9", "branch_line": 90, "side": "else", "target_line": 90, "seq": 3}]
    built = lr4.pin(items, loc, build=lambda c: ("f9", 90, 0.0))
    assert built[0][0] == "f9" and lr4.used["c-0003"]["how"] == "pin"


def test_topk_rank_counts_the_right_place_among_the_runners_up():
    from verity.bench.run import topk_rank
    case = {"must_mention": ["ecx_pusherror", "elist.tail"]}
    ans = {"meta": {"learning": {"candidates": [
        {"rank": 1, "label": "ecx_poperror() checks `notEmpty`", "text": ""},
        {"rank": 2, "label": "ecx_pusherror() checks `head == tail`", "text": "context->elist.head == context->elist.tail"},
        {"rank": 3, "label": "x", "text": ""}]}}}
    assert topk_rank(case, ans, False) == 2
    assert topk_rank(case, ans, True) == 1
    assert topk_rank({"must_mention": ["nothing_here"]}, ans, False) is None


def test_expanding_macro_names_the_macro_not_its_text():
    from verity.agent.recipes import _expanding_macro

    class G:
        sym = {"c:macro@FD_ZERO": {}}
    assert _expanding_macro(G, "FD_ZERO(&fdset);", "memset") == "FD_ZERO"
    assert _expanding_macro(G, "memset(&fdset, 0, sizeof fdset);", "memset") is None
    assert _expanding_macro(G, "x = compute(y);", "memset") is None


# ------------------------------------------------------------------------------------------------ pin / undo
def test_correct_by_line_pins_the_same_question_and_keeps_verifying(core, tmp_path):
    repo = fresh(core, tmp_path)
    a = ask(core, repo, Q)
    assert "gps_receiver_process_line" in text(a) and lm(a)["correctable"] and not lm(a)["used"]
    r = core.call("answer.correct", {"path": str(repo), "answer_id": a["id"], "target": TARGET,
                                     "note": "the *hh field itself is checked here"})
    cid = r["correction"]["id"]
    assert cid == "c-0001" and r["correction"]["status"] == "active" and r["correction"]["side"] == "then"
    b = ask(core, repo, Q)
    assert "nmea_checksum_ok" in text(b) and "nmea_parser.c:26" in text(b)
    used = lm(b)["used"]
    assert [u["id"] for u in used] == [cid] and used[0]["how"] == "pin" and used[0]["note"]
    assert any(cid in s for s in b["sections"]["assumptions"])                  # disclosed with the answer
    rep = b["sections"]["verification"]["report"]
    assert rep["claims"] and rep["verified"] == rep["claims"]                    # the verifier still checks everything
    # the same question in other words-of-the-same-kind: case and punctuation do not matter
    c = ask(core, repo, "what happens when the checksum of an nmea sentence is wrong")
    assert [u["id"] for u in lm(c)["used"]] == [cid]
    # an unrelated question is untouched
    d = ask(core, repo, "Who calls CalculateHeading()?")
    assert not lm(d)["used"]
    # undo: back to the analysis' own answer; restore: used again
    core.call("corrections.remove", {"path": str(repo), "id": cid})
    e = ask(core, repo, Q)
    assert not lm(e)["used"] and e["answer"] == a["answer"]
    assert core.call("corrections.restore", {"path": str(repo), "id": cid})["correction"]["status"] == "active"
    assert [u["id"] for u in lm(ask(core, repo, Q))["used"]] == [cid]
    lst = core.call("corrections.list", {"path": str(repo)})
    assert lst["counts"]["active"] == 1 and [ev["action"] for ev in lst["events"]][:3] == ["restore", "undo", "correct"]
    assert core.call("corrections.list", {"path": str(repo)})["corrections"][0]["uses"] >= 3


def test_pick_an_alternative_replaces_the_correction_it_corrects(core, tmp_path):
    repo = fresh(core, tmp_path)
    a = ask(core, repo, Q)
    c1 = core.call("answer.correct", {"path": str(repo), "answer_id": a["id"], "target": TARGET})["correction"]["id"]
    b = ask(core, repo, Q)
    alts = lm(b)["alternatives"]
    assert alts and alts[0]["rank"] == 2 and "gps_receiver_process_line" in alts[0]["label"]
    r = core.call("answer.correct", {"path": str(repo), "answer_id": b["id"], "pick": 2})
    c2 = r["correction"]["id"]
    assert r["replaced"] == [c1] and r["correction"]["kind"] == "pick"
    st = {c["id"]: c for c in core.call("corrections.list", {"path": str(repo)})["corrections"]}
    assert st[c1]["status"] == "removed" and "replaced by" in st[c1]["status_reason"]
    d = ask(core, repo, Q)
    assert "gps_receiver.c:88" in text(d) and [u["id"] for u in lm(d)["used"]] == [c2]


def test_requests_that_are_refused_say_why(core, tmp_path):
    repo = fresh(core, tmp_path)
    a = ask(core, repo, Q)
    p = {"path": str(repo), "answer_id": a["id"]}

    def refused(extra, word):
        with pytest.raises(EngineError) as e:
            core.call("answer.correct", {**p, **extra})
        assert e.value.code == -32602 and word in str(e.value), str(e.value)

    refused({"pick": 1}, "confirm")
    refused({"pick": 9}, "no alternative 9")
    refused({}, "Say which place")
    refused({"pick": 2, "target": TARGET}, "not both")
    refused({"target": {"file": "nope/missing.c", "line": 3}}, "not part of the indexed project")
    refused({"target": {"file": "gps/nmea_parser.c", "line": 9999}}, "does not exist")
    refused({"target": {"file": "gps/nmea_parser.c", "line": 1}}, "not inside a function")
    refused({"target": {"file": "gps/nmea_parser.c", "line": 4}}, "empty")
    refused({"target": {"file": "gps/gps_receiver.c", "line": 88}}, "already describes")
    with pytest.raises(EngineError) as e:
        core.call("answer.correct", {"path": str(repo), "answer_id": "0123456789ab", "target": TARGET})
    assert "not stored" in str(e.value)
    with pytest.raises(EngineError):
        core.call("answer.correct", {"path": str(repo), "answer_id": "../../etc", "target": TARGET})
    with pytest.raises(EngineError):
        core.call("corrections.remove", {"path": str(repo), "id": "c-9999"})
    assert core.call("corrections.list", {"path": str(repo)})["counts"]["active"] == 0


# ------------------------------------------------------------------------------------------------ anchors
def test_edited_code_turns_the_correction_stale_and_undoing_the_edit_revives_it(core, tmp_path):
    repo = fresh(core, tmp_path)
    a = ask(core, repo, Q)
    cid = core.call("answer.correct", {"path": str(repo), "answer_id": a["id"], "target": TARGET})["correction"]["id"]
    f = repo / "gps" / "nmea_parser.c"
    orig = f.read_text()
    f.write_text(orig.replace("if (*p != '*' || hex_value(p[1]) < 0", "if (*p != '#' || hex_value(p[1]) < 0"))
    reindex(core, repo)
    c = core.call("corrections.list", {"path": str(repo)})["corrections"][0]
    assert c["status"] == "stale" and "changed or was removed" in c["status_reason"]
    b = ask(core, repo, Q)
    assert not lm(b)["used"] and "gps_receiver_process_line" in text(b)        # a stale correction is never applied
    f.write_text(orig)
    reindex(core, repo)
    c = core.call("corrections.list", {"path": str(repo)})["corrections"][0]
    assert c["id"] == cid and c["status"] == "active" and "back" in c["status_reason"]
    assert [u["id"] for u in lm(ask(core, repo, Q))["used"]] == [cid]


def test_a_moved_line_is_followed(core, tmp_path):
    repo = fresh(core, tmp_path)
    a = ask(core, repo, Q)
    core.call("answer.correct", {"path": str(repo), "answer_id": a["id"], "target": TARGET})
    f = repo / "gps" / "nmea_parser.c"
    f.write_text("/* added\n * three lines\n */\n" + f.read_text())
    r = reindex(core, repo)
    assert r["result"].get("learning", {}).get("moved")
    c = core.call("corrections.list", {"path": str(repo)})["corrections"][0]
    assert c["status"] == "active" and c["target_line"] == 29 and c["branch_line"] == 29 and "followed" in c["status_reason"]
    b = ask(core, repo, Q)
    assert "nmea_parser.c:29" in text(b) and lm(b)["used"]


def test_learned_db_survives_an_index_reset(core, tmp_path):
    from verity.engine.core import EngineCore
    repo = fresh(core, tmp_path)
    a = ask(core, repo, Q)
    cid = core.call("answer.correct", {"path": str(repo), "answer_id": a["id"], "target": TARGET})["correction"]["id"]
    pr = core.project(str(repo))
    idx = pr.dir / "index.db"
    assert (pr.dir / "learned.db").is_file()
    other = EngineCore(llm_mode="direct", embeddings=False)
    try:
        pr2 = other.project(str(repo))
        pr2.store.close()
        idx.write_bytes(b"\0garbage" * 4096)                      # a corrupted index: moved aside and rebuilt
        from verity.store.db import FactStore
        FactStore(idx).close()
        other.projects.clear()
        r = other.wait(other.start_index(str(repo), full=True)["id"], 600)
        assert r["state"] == "done"
        b = other.ask(str(repo), Q, use_llm=False)["result"]
        assert [u["id"] for u in lm(b)["used"]] == [cid] and "nmea_checksum_ok" in text(b)
    finally:
        other.shutdown()


# ------------------------------------------------------------------------------------------------ other kinds
@pytest.mark.parametrize("question,target,expect", [
    ("What happens if GPS data stops arriving?", {"file": "nav/navigation.c", "line": 68}, "NavigationTick"),
    ("Where is the latitude stored?", {"file": "nav/navigation.c", "line": 54}, "ProcessGpsData"),
    ("Which function validates the fix?", {"file": "gps/gps_validation.c", "line": 10}, "gps_validate_fix"),
])
def test_other_question_kinds_use_their_correction(core, tmp_path, question, target, expect):
    repo = fresh(core, tmp_path)
    a = ask(core, repo, question)
    assert lm(a)["correctable"], (a["meta"]["intent"], lm(a))
    try:
        r = core.call("answer.correct", {"path": str(repo), "answer_id": a["id"], "target": target})
    except EngineError as e:
        assert "already describes" in str(e)               # the analysis already answered with that place
        return
    b = ask(core, repo, question)
    assert expect in text(b) and [u["id"] for u in lm(b)["used"]] == [r["correction"]["id"]], (text(b)[:300], lm(b))


def test_a_question_kind_that_cannot_be_corrected_is_only_confirmable(core, tmp_path):
    repo = fresh(core, tmp_path)
    a = ask(core, repo, "Who calls CalculateHeading()?")
    assert not lm(a)["correctable"] and not lm(a)["alternatives"]
    k = core.call("answer.confirm", {"path": str(repo), "answer_id": a["id"]})["confirmation"]
    assert k["must_mention"] and core.call("corrections.summary", {"path": str(repo)})["counts"]["confirmations"] == 1
    core.call("answer.unconfirm", {"path": str(repo), "id": k["id"]})
    assert core.call("corrections.summary", {"path": str(repo)})["counts"]["confirmations"] == 0


def test_conflicting_corrections_newest_wins_and_both_are_flagged(core, tmp_path):
    repo = fresh(core, tmp_path)
    a = ask(core, repo, Q)
    c1 = core.call("answer.correct", {"path": str(repo), "answer_id": a["id"], "target": TARGET})["correction"]
    a2 = ask(core, repo, "What happens when the checksum of an NMEA sentence is wrong")     # used c1 already
    lr = core.project(str(repo)).learner()
    # a second correction for the same question that did NOT come from correcting a corrected answer (an import)
    data = core.call("corrections.export", {"path": str(repo)})["data"]
    other = dict(data["corrections"][0], target_line=19, target_label="nmea_checksum_ok() returns at line 19",
                 branch_line=18, anchor=None, question=Q)
    from verity.learn.anchor import resolve_line
    other.update(resolve_line(lr.fs, repo, "gps/nmea_parser.c", 19))
    res = core.call("corrections.import", {"path": str(repo), "data": json.dumps(dict(data, corrections=[other]))})
    assert len(res["imported"]) == 1
    b = ask(core, repo, Q)
    u = lm(b)["used"][0]
    assert u["id"] == res["imported"][0] and u["conflicts"] == [c1["id"]] and "nmea_parser.c:18" in text(b)
    flags = {c["id"]: c["conflict"] for c in core.call("corrections.list", {"path": str(repo)})["corrections"]}
    assert flags[c1["id"]] == [u["id"]] and flags[u["id"]] == [c1["id"]]
    assert a2["id"]


# ------------------------------------------------------------------------------------------------ glossary
def test_glossary_suggest_accept_reject_and_project_scope(core, tmp_path):
    repo = fresh(core, tmp_path)
    a = ask(core, repo, "What happens when the trailer of an NMEA sentence is malformed?")
    r = core.call("answer.correct", {"path": str(repo), "answer_id": a["id"], "target": TARGET})
    terms = {t["term"]: t for t in r["glossary_suggestions"]}
    assert "trailer" in terms and terms["trailer"]["status"] == "suggested"
    assert "malformed" not in terms and "wrong" not in terms                     # the kind of failure is not a term
    assert terms["trailer"]["identifiers"]
    assert "trailer" not in core.project(str(repo)).learner().glossary_map()     # nothing applies before acceptance
    core.call("glossary.accept", {"path": str(repo), "term": "trailer"})
    assert "trailer" in core.project(str(repo)).learner().glossary_map()
    b = ask(core, repo, "Where is the trailer checked?")
    assert "trailer" in lm(b).get("glossary_used", [])
    # project scope: the glossary never reaches another project
    other = fresh(core, tmp_path, "other")
    c = ask(core, other, "Where is the trailer checked?")
    assert not lm(c).get("glossary_used")
    core.call("glossary.reject", {"path": str(repo), "term": "trailer"})
    assert "trailer" not in core.project(str(repo)).learner().glossary_map()
    core.call("glossary.add", {"path": str(repo), "term": "bus-off", "identifiers": "GPS_INVALID_RANGE, gps_validate_fix"})
    g = {t["term"]: t for t in core.call("glossary.list", {"path": str(repo)})}
    assert g["bus-off"]["status"] == "accepted" and g["bus-off"]["identifiers"] == ["GPS_INVALID_RANGE", "gps_validate_fix"]
    with pytest.raises(EngineError):
        core.call("glossary.add", {"path": str(repo), "term": "x" * 80, "identifiers": "A"})
    with pytest.raises(EngineError):
        core.call("glossary.add", {"path": str(repo), "term": "ok", "identifiers": "not an identifier!"})
    core.call("glossary.remove", {"path": str(repo), "term": "bus-off"})
    assert "bus-off" not in {t["term"] for t in core.call("glossary.list", {"path": str(repo)})}


def test_repository_glossary_in_verity_toml(core, tmp_path):
    repo = fresh(core, tmp_path)
    (repo / "verity.toml").write_text('[glossary]\n"sentence guard" = ["nmea_checksum_ok"]\n')
    lst = {t["term"]: t for t in core.call("glossary.list", {"path": str(repo)})}
    assert lst["sentence guard"]["read_only"] and lst["sentence guard"]["status"] == "accepted"
    a = ask(core, repo, "What does the sentence guard do?")
    assert "sentence guard" in lm(a).get("glossary_used", [])
    with pytest.raises(EngineError) as e:
        core.call("glossary.remove", {"path": str(repo), "term": "sentence guard"})
    assert "verity.toml" in str(e.value)


# ------------------------------------------------------------------------------------------------ share
def test_export_as_a_bench_suite_and_it_passes(core, tmp_path):
    from verity.bench.run import grade, vocabulary
    repo = fresh(core, tmp_path)
    a = ask(core, repo, Q)
    core.call("answer.correct", {"path": str(repo), "answer_id": a["id"], "target": TARGET})
    k = ask(core, repo, "Who calls CalculateHeading()?")
    core.call("answer.confirm", {"path": str(repo), "answer_id": k["id"]})
    suite = core.call("corrections.export", {"path": str(repo), "format": "bench"})["data"]
    assert len(suite["cases"]) == 2 and list(suite["projects"].values()) == [str(repo)]
    vocab = vocabulary(core.project(str(repo)).store)
    for case in suite["cases"]:
        g = grade(case, ask(core, repo, case["q"]), vocab)
        assert g["passed"], (case, g["checks"])


def test_export_import_round_trip(core, tmp_path):
    repo = fresh(core, tmp_path)
    a = ask(core, repo, Q)
    core.call("answer.correct", {"path": str(repo), "answer_id": a["id"], "target": TARGET, "note": "n"})
    data = core.call("corrections.export", {"path": str(repo), "format": "json"})["data"]
    assert data["verity_learning"] == 1 and data["corrections"][0]["note"] == "n" and "qvec" not in data["corrections"][0]
    same = fresh(core, tmp_path, "same")
    r = core.call("corrections.import", {"path": str(same), "data": json.dumps(data)})
    assert len(r["imported"]) == 1 and not r["stale"]
    assert lm(ask(core, same, Q))["used"]
    again = core.call("corrections.import", {"path": str(same), "data": data})
    assert again["duplicates"] and not again["imported"]
    changed = fresh(core, tmp_path, "changed")
    f = changed / "gps" / "nmea_parser.c"
    f.write_text(f.read_text().replace("p[1]) < 0 || hex_value(p[2]) < 0", "p[1]) < 0"))
    reindex(core, changed)
    r = core.call("corrections.import", {"path": str(changed), "data": data})
    assert r["imported"] and r["stale"] == r["imported"]                       # different code there: stale, not used
    assert not lm(ask(core, changed, Q))["used"]
    with pytest.raises(EngineError):
        core.call("corrections.import", {"path": str(same), "data": "{\"not\": \"ours\"}"})
    toml = core.call("corrections.export", {"path": str(repo), "format": "toml"})["data"]
    assert toml.startswith("# Accepted Verity glossary") and "[glossary]" in toml


def test_export_save_writes_a_file(core, tmp_path, monkeypatch):
    repo = fresh(core, tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Downloads").mkdir()
    r = core.call("corrections.export", {"path": str(repo), "format": "bench", "save": True})
    p = Path(r["saved"])
    assert p.parent == tmp_path / "Downloads" and json.loads(p.read_text())["cases"] == []
    r2 = core.call("corrections.export", {"path": str(repo), "format": "bench", "save": True})
    assert Path(r2["saved"]) != p                                               # never overwrites an earlier export


# ------------------------------------------------------------------------------------------------ CLI + MCP
def _cli(*args):
    return subprocess.run([sys.executable, "-m", "verity", *args], cwd=str(ROOT), capture_output=True, text=True,
                          timeout=300)


def test_cli_correct_confirm_corrections_glossary(core, tmp_path):
    repo = fresh(core, tmp_path)
    a = ask(core, repo, Q)
    p = _cli("correct", str(repo), a["id"], "--line", "gps/nmea_parser.c:26", "--note", "the trailer check")
    assert p.returncode == 0, p.stderr
    assert "Saved correction c-0001" in p.stdout and "Undo: verity corrections" in p.stdout
    p = _cli("corrections", str(repo))
    assert p.returncode == 0 and "c-0001" in p.stdout and "nmea_parser.c:26" in p.stdout
    p = _cli("ask", str(repo), Q, "--direct", "--no-embed")
    assert p.returncode == 0 and "Learned: uses correction c-0001" in p.stdout and "Answer " in p.stdout
    import re
    aid = re.search(r"^Answer ([0-9a-f]{12})$", p.stdout, re.M).group(1)
    # the command to copy stays on one line, however long the path (rich would wrap it at 80 columns)
    assert len(f"verity confirm {repo} {aid}") > 80 and f"verity confirm {repo} {aid}" in p.stdout.splitlines()[-1]
    p = _cli("correct", str(repo), a["id"], "--pick", "1")
    assert p.returncode == 2 and "confirm" in p.stdout + p.stderr
    p = _cli("corrections", str(repo), "undo", "c-0001")
    assert p.returncode == 0 and "Undid c-0001" in p.stdout
    p = _cli("corrections", str(repo), "export", "--format", "bench")
    assert p.returncode == 0 and json.loads(p.stdout)["cases"] == []
    p = _cli("glossary", str(repo), "add", "trailer", "nmea_checksum_ok")
    assert p.returncode == 0 and "Added" in p.stdout
    p = _cli("glossary", str(repo))
    assert "trailer" in p.stdout
    p = _cli("confirm", str(repo), a["id"])
    assert p.returncode == 0 and "test case" in p.stdout


def test_mcp_verity_correct_tool(core, tmp_path):
    repo = fresh(core, tmp_path)
    a = ask(core, repo, Q)
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
                                                                          "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "verity_correct", "arguments": {
                "path": str(repo), "answer_id": a["id"], "file": "gps/nmea_parser.c", "line": 26, "note": "user said so"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "verity_correct", "arguments": {
                "path": str(repo), "answer_id": a["id"], "pick": 9}}}]
    p = subprocess.Popen([sys.executable, "-m", "verity", "mcp"], cwd=str(ROOT), stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out = {}
    try:
        for m in msgs:                                   # one at a time: the tools share one learned.db
            p.stdin.write(json.dumps(m) + "\n")
            p.stdin.flush()
            if "id" in m:
                line = p.stdout.readline()
                d = json.loads(line)
                out[d["id"]] = d
        p.stdin.close()
        p.wait(60)
    finally:
        if p.poll() is None:
            p.kill()
    assert "verity_correct" in {t["name"] for t in out[2]["result"]["tools"]}
    assert not out[3]["result"]["isError"] and "Saved correction" in out[3]["result"]["content"][0]["text"]
    assert out[4]["result"]["isError"] and "no alternative 9" in out[4]["result"]["content"][0]["text"]
    assert lm(ask(core, repo, Q))["used"][0]["note"] == "user said so"


def test_disclosure_dates_are_portable():
    from verity.learn.apply import day
    assert day(time.mktime((2026, 10, 2, 12, 0, 0, 0, 0, -1))) == "2 Oct 2026" and day(None) == ""


# ------------------------------------------------------------------------------------------------ hardening (review)
def test_store_never_moves_a_file_it_merely_cannot_open(tmp_path):
    # a locked or unreadable learned.db is not damaged: refuse to open it, never move it aside
    d = tmp_path / "learned.db"
    d.mkdir()                                                   # sqlite cannot open a folder: OperationalError
    with pytest.raises(LearnError):
        LearnStore(d)
    assert d.is_dir() and not list(tmp_path.glob("*.corrupt-*"))
    ls = LearnStore(tmp_path / "ok.db")
    c = ls.add_correction({"question": "q", "target_fn": "f", "status": "active"})      # id taken in the same transaction
    assert c["id"] == "c-0001" and ls.add_correction({"question": "q2", "target_fn": "f", "status": "active"})["id"] == "c-0002"
    assert not ls.update_correction_if("c-0001", "removed", status="stale")               # a status read earlier: no-op
    assert ls.correction("c-0001")["status"] == "active"
    with pytest.raises(RuntimeError):
        with ls.tx():
            ls.add_correction({"question": "q3", "target_fn": "f", "status": "active"})
            raise RuntimeError("boom")
    assert [c["id"] for c in ls.corrections()] == ["c-0002", "c-0001"]                  # rolled back
    ls.close()


class _FakeStore:
    def __init__(self, cs):
        self.cs = cs

    def corrections(self, status=None):
        return [c for c in self.cs if not status or c["status"] == status]

    def update_correction(self, cid, **f):
        pass

    def log(self, *a, **k):
        pass


def test_pins_rank_by_match_strength_and_negation_is_not_the_same_question():
    class R:
        intent, topic = "failure_invalid", ["socket", "open"]
    base = {"status": "active", "intent": "failure_invalid", "topic": ["socket", "open"], "target_fn": "f",
            "branch_line": 10, "target_line": 10, "side": "then"}
    exact = dict(base, id="c-0001", seq=1, question="What happens when the socket is opened?",
                 qnorm=Learning(None, R, "What happens when the socket is opened?").qn)
    meaning = dict(base, id="c-0002", seq=2, question="What does opening the socket do?", qnorm="x", qvec=[1.0, 0.0],
                   target_fn="g")
    lr = Learning.load(_FakeStore([exact, meaning]), R, "What happens when the socket is opened?", qvec=[1.0, 0.0])
    assert lr.pins[0]["id"] == "c-0001" and not lr.conflicts      # the same words beat a newer same-meaning match
    neg = dict(base, id="c-0003", seq=3, question="What happens when the socket cannot be opened?", qnorm="y")
    lr2 = Learning.load(_FakeStore([neg]), R, "What happens when the socket is opened?")
    assert not lr2.pins and [c["id"] for c in lr2.hints] == ["c-0003"]   # the opposite question: a hint at most
    lr3 = Learning.load(_FakeStore([dict(neg, qvec=[1.0, 0.0])]), R, "What happens when the socket is opened?",
                        qvec=[1.0, 0.0])
    assert not lr3.pins                                          # not even by meaning
    from verity.learn.apply import opposite
    assert not opposite("What happens when the closing quote never comes?", "What if the closing quote is missing?")
    assert not opposite("What if the buffer is too small?", "What if the buffer is not big enough?")
    assert opposite("Why can the file not be read?", "How is the file read?")
    lr4 = Learning.load(_FakeStore([dict(neg, question="When is the socket opened, then?", qnorm="z")]), R,
                        "What happens when the socket is opened?")
    assert lr4.pins and lr4.match["c-0003"] == "words"
    assert lr4.meta(True)["pin_not_applied"]["id"] == "c-0003"     # matched but not used yet: never silent
    assert "could not be used here" in lr4.disclosure()[-1]
    lr4.use_pin()
    assert "a question about the same thing (“When is the socket opened, then?”)" in lr4.disclosure()[0]
    assert "pin_not_applied" not in lr4.meta(True) and len(lr4.disclosure()) == 1


def test_switch_cases_are_told_apart():
    from verity.learn.service import _same_place
    c = {"id": "c-0001", "target_fn": "f", "branch_line": 50, "side": "case", "target_line": 56, "seq": 1}
    lr = Learning(None, None, "q")
    lr.pins = [c]
    items = [("A", (51, 54)), ("B", (55, 58)), ("C", (59, 60))]
    loc = lambda it: Loc("f", "a.c", 50, "case", it[1])          # noqa: E731 — every case shares the switch's line
    assert [i[0] for i in lr.pin(items, loc)] == ["B", "A", "C"]
    whole = dict(c, target_line=50)                              # the switch line itself: any case
    lr2 = Learning(None, None, "q")
    lr2.pins = [whole]
    assert lr2.pin(items, loc)[0][0] == "A"
    tgt = {"target_fn": "f", "branch_line": 50, "side": "case", "target_line": 56}
    assert not _same_place({"fn": "f", "line": 50, "side": "case", "span": [51, 54]}, tgt)
    assert _same_place({"fn": "f", "line": 50, "side": "case", "span": [55, 58]}, tgt)


def _doctor(core, repo, aid, **learning):
    """Rewrite a stored answer's meta.learning (to reach paths that need particular candidates)."""
    fs = core.project(str(repo)).store
    ans = json.loads(fs.one("SELECT result FROM answer WHERE id=?", (aid,))["result"])
    ans["meta"]["learning"] = dict(ans["meta"].get("learning") or {}, **learning)
    fs.exec("UPDATE answer SET result=? WHERE id=?", (json.dumps(ans), aid))
    return ans


def test_picking_a_function_alternative_and_replacing_only_this_questions_pin(core, tmp_path):
    repo = fresh(core, tmp_path)
    fs = core.project(str(repo)).store
    key = fs.one("SELECT key FROM symbol WHERE name='hex_value' AND is_def=1")["key"]
    a = ask(core, repo, Q)
    first = lm(a)["candidates"][0]
    fn_alt = {"rank": 2, "fn": key, "file": None, "line": None, "side": None, "span": None, "label": "hex_value()"}
    _doctor(core, repo, a["id"], correctable=True, candidates=[first, fn_alt], alternatives=[fn_alt])
    c = core.call("answer.correct", {"path": str(repo), "answer_id": a["id"], "pick": 2})["correction"]
    assert c["target_fn"] == key and c["target_file"] == "gps/nmea_parser.c" and c["target_label"] == "hex_value()"
    # an answer to ANOTHER question that used c (matched by meaning or words) is corrected: c stays for its question
    b = ask(core, repo, "What happens when an NMEA sentence has no asterisk?")
    _doctor(core, repo, b["id"], correctable=True, used=[{"id": c["id"], "how": "pin"}])
    r = core.call("answer.correct", {"path": str(repo), "answer_id": b["id"], "target": TARGET})
    assert r["replaced"] == [] and core.project(str(repo)).learner().store.correction(c["id"])["status"] == "active"
    # the same question corrected again: the new correction replaces it
    a2 = ask(core, repo, Q)
    _doctor(core, repo, a2["id"], correctable=True, used=[{"id": c["id"], "how": "pin"}])
    r2 = core.call("answer.correct", {"path": str(repo), "answer_id": a2["id"], "target": {"file": "gps/nmea_parser.c",
                                                                                          "line": 19}})
    assert r2["replaced"] == [c["id"]]


def test_refused_when_the_code_changed_or_the_answer_has_no_single_place(core, tmp_path):
    repo = fresh(core, tmp_path)
    who = ask(core, repo, "Who calls CalculateHeading()?")
    with pytest.raises(EngineError) as e:
        core.call("answer.correct", {"path": str(repo), "answer_id": who["id"], "target": TARGET})
    assert "does not hinge on one place" in str(e.value)
    a = ask(core, repo, Q)
    f = repo / "gps" / "nmea_parser.c"
    f.write_text(f.read_text() + "\n/* edited after the index */\n")
    with pytest.raises(EngineError) as e:
        core.call("answer.correct", {"path": str(repo), "answer_id": a["id"], "target": TARGET})
    assert "changed since the project was last indexed" in str(e.value)
    reindex(core, repo)
    alts = lm(a).get("alternatives") or [dict(lm(a)["candidates"][0], rank=2)]
    _doctor(core, repo, a["id"], alternatives=[dict(alts[0], rank=2, file="gps/nmea_parser.c")],
            candidates=[lm(a)["candidates"][0], dict(alts[0], rank=2, file="gps/nmea_parser.c")])
    with pytest.raises(EngineError) as e:                       # an alternative from before the edit
        core.call("answer.correct", {"path": str(repo), "answer_id": a["id"], "pick": 2})
    assert "changed since this answer" in str(e.value)


def test_import_validates_everything_and_is_all_or_nothing(core, tmp_path, monkeypatch):
    repo = fresh(core, tmp_path)
    a = ask(core, repo, Q)
    core.call("answer.correct", {"path": str(repo), "answer_id": a["id"], "target": TARGET})
    core.call("glossary.add", {"path": str(repo), "term": "trailer", "identifiers": ["hex_value"]})
    core.call("answer.confirm", {"path": str(repo), "answer_id": ask(core, repo, "Who calls CalculateHeading()?")["id"]})
    data = core.call("corrections.export", {"path": str(repo)})["data"]
    good = data["corrections"][0]
    other = fresh(core, tmp_path, "other")
    core.call("glossary.add", {"path": str(other), "term": "trailer", "identifiers": ["nmea_checksum_ok"]})
    bad = [dict(good, id="c-0100", question=123), dict(good, id="c-0101", target_line="26"),
           dict(good, id="c-0102", target_file="../../etc/passwd", question="Another question?"), "nonsense"]
    payload = dict(data, corrections=[good] + bad,
                   glossary=[{"term": "trailer", "identifiers": ["x_y"], "status": "suggested"}])
    lr = core.project(str(other)).learner()
    orig = lr.store.put_glossary
    monkeypatch.setattr(lr.store, "put_glossary", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    with pytest.raises(Exception):
        core.call("corrections.import", {"path": str(other), "data": json.dumps(dict(payload, glossary=[
            {"term": "fresh", "identifiers": ["hex_value"], "status": "accepted"}]))})
    assert lr.store.corrections() == []                         # nothing half-imported
    monkeypatch.setattr(lr.store, "put_glossary", orig)
    r = core.call("corrections.import", {"path": str(other), "data": json.dumps(payload)})
    assert len(r["imported"]) == 1 and not r["stale"] and len(r["skipped"]) == 4 and r["confirmations"] == 1
    assert any("not part of this project" in s for s in r["skipped"])
    t = lr.store.glossary_term("trailer")
    assert t["status"] == "accepted" and t["identifiers"] == ["nmea_checksum_ok"]      # a local decision is kept
    assert lm(ask(core, other, Q))["used"]


def test_undo_clears_disagreement_and_a_new_embedding_model_re_embeds(core, tmp_path):
    repo = fresh(core, tmp_path)
    ls = core.project(str(repo)).learner().store
    ls.add_correction({"question": "q1", "target_fn": "f", "status": "active", "conflict": ["c-0002"]})
    ls.add_correction({"question": "q2", "target_fn": "f", "status": "active", "conflict": ["c-0001"], "qvec": [1, 0]})
    core.call("corrections.remove", {"path": str(repo), "id": "c-0002"})
    assert ls.correction("c-0001")["conflict"] == []
    a = ask(core, repo, Q)
    c = core.call("answer.correct", {"path": str(repo), "answer_id": a["id"], "target": TARGET})["correction"]
    ls.update_correction(c["id"], qvec=[1.0, 0.0])               # made with another embedding model

    class R:
        intent, topic = "explain", ["x"]
    core.project(str(repo)).learner().for_question(R, "q", qvec=[0.0, 1.0, 0.0], embed=lambda q: [0.0, 0.0, 1.0])
    assert ls.correction(c["id"])["qvec"] == [0.0, 0.0, 1.0]    # vectors from another model are made again
