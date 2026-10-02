"""C extraction + derive + graph facts on the sample, checked against hand-verified truths."""
import json

from verity.analysis.graph import CodeGraph


def g_of(core, repo):
    return core.project(str(repo)).answerer(core.runtime(), None).graph()


def test_call_edges_and_indirect_resolution(core, c_indexed):
    st = core.project(str(c_indexed)).store
    q = lambda a, b: st.one("SELECT res, via FROM call WHERE caller=? AND callee=?", (a, b))
    assert q("c:@F@ProcessGpsData", "c:@F@CalculateHeading")["res"] == "exact"
    cb = q("c:gps_receiver.c@F@handle_rmc", "c:@F@ProcessGpsData")          # callback through g_fix_callback
    assert cb and cb["res"] == "flow" and cb["via"] == "c:gps_receiver.c@g_fix_callback"
    for h in ("c:gps_receiver.c@F@handle_rmc", "c:gps_receiver.c@F@handle_gga"):     # dispatch table
        assert q("c:@F@gps_receiver_process_line", h)["res"] == "table"


def test_guards_include_early_exit(core, c_indexed):
    st = core.project(str(c_indexed)).store
    r = st.one("SELECT guards FROM call WHERE caller='c:@F@ProcessGpsData' AND callee='c:@F@CalculateHeading'")
    gd = json.loads(r["guards"])
    assert any(x["cond"] == "status != GPS_OK" and x["polarity"] is False and x["kind"] == "early_exit" for x in gd)


def test_writers_of_current_heading(core, c_indexed):
    g = g_of(core, c_indexed)
    ws = {(o["fn"], o["line"]) for o in g.writers("c:@currentHeading")}
    assert ws == {("c:@F@navigation_init", 18), ("c:@F@ProcessGpsData", 59)}


def test_entries_io_and_mmio(core, c_indexed):
    g = g_of(core, c_indexed)
    assert set(g.entries) >= {"c:@F@main", "c:@F@USART2_IRQHandler"}
    assert any(io["res"] == "mmio" and io["sym"].endswith("@FI@DR") for io in g.io)
    assert any(io["sym"] == "c:@F@motor_set_steering" and io["dir"] == "sink" for io in g.io)


def test_context_sensitive_backward_slice(core, c_indexed):
    g = g_of(core, c_indexed)
    sl = g.slice("c:@currentHeading", "backward", max_nodes=2000, max_depth=200)
    # wrap360() is shared by CalculateHeading() and bearing_deg(); a context-insensitive slice would leak bearing's inputs
    assert not any(n.startswith("c:@F@bearing_deg#param") for n in sl["nodes"])
    assert sl["terminals"] == ["c:@S@__vr_regs_USART2@FI@DR"]


def test_latitude_origin_path(core, c_indexed):
    g = g_of(core, c_indexed)
    p = g.flow_path("c:@SA@nav_state_t@FI@latitude", lambda n: n.split("#")[0] in g.io_by_sym, "backward")
    names = [g.name(e.dst) for e in p]
    assert "nmea_parse_coord() result" in names and names[-1] == "nav_state_t.latitude"
    assert p[0].src.endswith("@FI@DR")


def test_free_macro_variant(core, c_indexed):
    st = core.project(str(c_indexed)).store
    assert st.one("SELECT cfg FROM call WHERE callee='c:@F@VehicleController_Hold'")["cfg"] == "-ENABLE_DEAD_RECKONING"
    assert st.one("SELECT 1 FROM occ WHERE cfg='+ENABLE_DEAD_RECKONING' AND sym='c:@EA@nav_mode_t@NAV_MODE_DEAD_RECKONING'")


def test_crosscheck_agreement(core, c_indexed):
    xc = core.project(str(c_indexed)).store.meta_get("crosscheck")
    if xc is None:
        import pytest
        pytest.skip("cppcheck not installed")
    assert xc["function_agreement"] == 1.0 and xc["call_agreement"] >= 0.95


def test_csharp_facts(core, cs_indexed):
    st = core.project(str(cs_indexed)).store
    callers = {r["caller"] for r in st.q("SELECT caller FROM call WHERE callee='M:Nav.NavigationController.ProcessGpsData(Nav.GpsFix)'")}
    assert callers == {"M:Nav.Services.GpsService.OnFix(System.Object,Nav.GpsFixEventArgs)",
                       "M:Nav.Program.RunSelfTest(Nav.NavigationController)"}
    impl = {r["callee"] for r in st.q("SELECT callee FROM call WHERE derived=1 AND res IN ('cha','rta') AND line=67")}
    assert "M:Nav.ComplementaryHeadingFilter.Update(System.Double,System.Double)" in impl
    ev = st.one("SELECT callee FROM call WHERE derived=1 AND kind='event'")
    assert ev["callee"] == "M:Nav.Services.GpsService.OnFix(System.Object,Nav.GpsFixEventArgs)"
