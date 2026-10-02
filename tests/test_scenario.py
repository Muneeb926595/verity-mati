"""Scenario execution must reproduce the independent reference harness (bench/gold)."""
from conftest import ROOT, ask, needs_sandbox

GOLD = (ROOT / "bench" / "gold" / "reference_output.txt").read_text()


def test_gold_file_is_the_reference():
    assert "A.currentHeading=337.5000 mode=1" in GOLD and "B.mode=2 consecutive_invalid=5" in GOLD


@needs_sandbox
def test_heading_270_matches_gold(core, c_indexed):
    ans = ask(core, c_indexed, "GPS heading = 270, speed = 80, valid = true. Which branch executes?")
    run = ans["run"]["runs"][0]
    assert run["state"]["currentHeading"] == "337.5000"          # gold A.currentHeading=337.5000
    assert run["state"]["g_nav.mode"].startswith("1")              # gold mode=1 (TRACKING)
    assert "motor_set_steering(50)" in run["hw_calls"]             # gold last_steer=50
    assert ans["status"] == "OBSERVED"


@needs_sandbox
def test_invalid_fixes_degrade(core, c_indexed):
    ans = ask(core, c_indexed, "What happens if GPS data is invalid?")
    last = ans["run"]["runs"][-1]
    assert last["state"]["g_nav.mode"].startswith("2") and last["state"]["g_nav.consecutive_invalid"] == "5"  # gold B
    assert not any(h.startswith("motor_set_steering") for h in last["hw_calls"])


@needs_sandbox
def test_gps_loss_holds(core, c_indexed):
    ans = ask(core, c_indexed, "What happens if GPS becomes unavailable?")
    run = ans["run"]["runs"][-1]
    assert run["state"]["g_nav.mode"].startswith("3")                                  # gold C mode=3 (HOLD)
    assert run["hw_calls"][-2:] == ["motor_set_steering(0)", "led_set_status(0)"]      # gold last_steer=0 led=0


@needs_sandbox
def test_runs_are_sandboxed_and_leave_repo_untouched(core, c_indexed):
    before = {p: p.stat().st_mtime for p in c_indexed.rglob("*") if p.is_file()}
    ans = ask(core, c_indexed, "What happens when GPS sends heading 90, speed 50, valid true?")
    assert ans["run"]["runs"][0]["ok"]
    after = {p: p.stat().st_mtime for p in c_indexed.rglob("*") if p.is_file()}
    assert before == after
    sb = open(ans["run"]["runs"][0]["run_dir"] + "/sandbox.sb").read()
    assert "(deny network*)" in sb and "(deny default)" in sb
