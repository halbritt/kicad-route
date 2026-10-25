"""Spoke corridor reservation (`run --reserve-spokes`) and spoke-cut attribution
(`kicad-route spokes`), on the route_demo fixture and a variant with an F.Cu GND pour.

The variant adds GND_F (F.Cu, same settings as the fixture's B.Cu GND_B), so the SMD
pad C1.2 joins a top-layer pour through thermal relief -- the situation where new F.Cu
signal tracks cut spokes on real boards. The plan must model C1.2's spokes, reserve
and strip must round-trip the board, a reserved attempt must still pass every
preservation check (keepouts included), and a hand-placed track that starves C1.2
must be attributed to that track.
"""
import json
import shutil
import subprocess

import pytest

from helpers import FIXTURE, OUT, ROOT, cli, record, run, summary
from kicad_route import cli as route
from kicad_route import common, spokes

SCRIPT = ROOT / "src" / "kicad_route" / "kicad" / "spokes.py"
BOARD = "route_demo.kicad_pcb"
MM = 1_000_000


def script(*args):
    r = subprocess.run([str(common.PCBNEW_PYTHON), str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                       env=common.tool_env(), timeout=600)
    tag = next((json.loads(l.split(":", 1)[1]) for l in r.stdout.splitlines() if l.startswith("SPOKES_JSON:")), None)
    return r, tag


def board_facts(board, out):
    facts, rec = route.board_facts(board, out)
    assert facts is not None, rec
    return facts


@pytest.fixture(scope="module")
def fzone_dir(tmp_path_factory):
    """The fixture plus an F.Cu GND pour over the whole board (C1.2 gets thermal spokes on F.Cu)."""
    import pcbnew
    d = tmp_path_factory.mktemp("fzone") / "route_demo"
    shutil.copytree(FIXTURE, d, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    board = pcbnew.LoadBoard(str(d / BOARD))
    zone = pcbnew.ZONE(board)
    zone.SetLayer(pcbnew.F_Cu)
    zone.SetNet(board.FindNet("GND"))
    zone.SetZoneName("GND_F")
    zone.SetLocalClearance(pcbnew.FromMM(0.3))
    zone.SetMinThickness(pcbnew.FromMM(0.25))
    o = zone.Outline()
    o.NewOutline()
    for x, y in [(100.5, 100.5), (149.5, 100.5), (149.5, 129.5), (100.5, 129.5)]:
        o.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
    board.Add(zone)
    board.Save(str(d / BOARD))
    return d


def plan_of(board, out, *extra):
    before = common.sha256(board)
    r, tag = script("plan", board, out, *extra)
    assert r.returncode == 0 and tag, r.stdout + r.stderr
    assert common.sha256(board) == before, "plan must never modify the board"
    return json.loads(out.read_text())


def test_geometry_helpers():
    sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert spokes.seg_poly_distance((-5, 5), (15, 5), sq) == 0          # crosses
    assert spokes.seg_poly_distance((2, 2), (3, 3), sq) == 0            # inside
    assert spokes.seg_poly_distance((-5, 12), (15, 12), sq) == pytest.approx(2)
    cr = {"polygon": sq}
    track = {"kind": "segment", "layer": "F.Cu", "width": 6, "start": [-5, 12], "end": [15, 12]}
    assert spokes.copper_hits(cr, "F.Cu", ["F.Cu", "B.Cu"], track)       # 2 away, half-width 3: overlaps
    assert not spokes.copper_hits(cr, "B.Cu", ["F.Cu", "B.Cu"], track)   # other layer
    assert not spokes.copper_hits(cr, "F.Cu", ["F.Cu", "B.Cu"], dict(track, width=4))  # edge touch only
    via = {"position": [5, 13], "diameter": 8, "layers": ["F.Cu", "B.Cu"]}
    assert spokes.copper_hits(cr, "B.Cu", ["F.Cu", "B.Cu"], via)
    pts = spokes.arc_points([0, 0], [5, 5], [10, 0])
    assert pts[0] == pytest.approx((0, 0)) and pts[-1] == pytest.approx((10, 0))
    assert max(y for _, y in pts) == pytest.approx(5)


def test_plan_models_thermal_spokes(fzone_dir, tmp_path):
    plan = plan_of(fzone_dir / BOARD, tmp_path / "plan.json")
    record("spokes_plan", {k: plan[k] for k in ("per_pad", "min_resolved_spokes", "clearance", "refilled",
                                                "pad_count", "corridors_live", "corridors_selected", "warnings")})
    assert plan["refilled"] and plan["per_pad"] == plan["min_resolved_spokes"] == 2
    assert plan["clearance"] == 400_000  # the .kicad_dru Power-to-signal rule, above every class and zone clearance
    pads = {(p["ref"], p["pad"], p["layer"]): p for p in plan["pads"]}
    # the GND pads in a pour: C1.2 (SMD) on F.Cu, J1.2/J2.2 (THT) on both layers
    assert set(pads) == {("C1", "2", "F.Cu"), ("J1", "2", "F.Cu"), ("J1", "2", "B.Cu"),
                         ("J2", "2", "F.Cu"), ("J2", "2", "B.Cu")}
    c1 = pads[("C1", "2", "F.Cu")]
    assert (c1["gap"], c1["spoke_width"], c1["min_thickness"]) == (500_000, 500_000, 250_000)
    cors = {round(c["direction_deg"]): c for c in c1["corridors"]}
    assert set(cors) == {0, 90, 180, 270}                         # roundrect pad: spoke angle 90
    toward_c1_1 = cors[180]                                       # 0.9 mm to the VIN pad: no pour there
    assert not toward_c1_1["live"] and not toward_c1_1["eligible"] and not toward_c1_1["selected"]
    assert toward_c1_1["overlaps_existing"][0][0] == "pad"
    assert all(cors[a]["live"] and cors[a]["eligible"] for a in (0, 90, 270))
    assert sum(c["selected"] for c in c1["corridors"]) == 2 == c1["selected"]
    length = c1["gap"] + c1["min_thickness"] + plan["clearance"] + plan["max_error"]
    half = c1["spoke_width"] / 2 + plan["clearance"]
    cx, cy = c1["position"]
    for ang, extent in ((0, 500_000), (90, 725_000), (270, 725_000)):  # 0805 pad: 1.0 x 1.45 mm
        c = cors[ang]
        assert c["start"] == pytest.approx(extent, abs=6_000)          # starts at the pad edge
        assert c["end"] - extent == pytest.approx(length, abs=6_000)
        assert c["half_width"] == half
        xs, ys = [p[0] for p in c["polygon"]], [p[1] for p in c["polygon"]]
        if ang == 0:     # +x
            assert min(xs) == pytest.approx(cx + c["start"], abs=2) and max(ys) - min(ys) == 2 * half
        if ang == 90:    # up on screen: -y in KiCad coordinates
            assert max(ys) == pytest.approx(cy - c["start"], abs=2) and max(xs) - min(xs) == 2 * half
    j1 = pads[("J1", "2", "F.Cu")]                                 # circular THT pad: diagonal spokes
    assert sorted(round(c["direction_deg"]) for c in j1["corridors"]) == [45, 135, 225, 315]
    assert j1["live_spokes"] == 4 and j1["selected"] == 2
    one = plan_of(fzone_dir / BOARD, tmp_path / "plan1.json", "--per-pad", "1")
    assert one["corridors_selected"] == len(one["pads"]) == 5


def test_reserve_strip_round_trip(fzone_dir, tmp_path):
    plan = plan_of(fzone_dir / BOARD, tmp_path / "plan.json")
    board = tmp_path / "r" / BOARD
    shutil.copytree(fzone_dir, board.parent)
    original = board.read_bytes()
    before = board_facts(board, tmp_path / "before.json")
    r, tag = script("reserve", board, tmp_path / "plan.json")
    assert r.returncode == 0 and tag == {"reserved": plan["corridors_selected"], "pads": 5}, r.stdout + r.stderr
    reserved = board_facts(board, tmp_path / "reserved.json")
    ours = [a for a in reserved["rule_areas"] if a["name"].startswith("kicad-route:spoke:")]
    assert len(ours) == plan["corridors_selected"] == 10
    assert len(reserved["rule_areas"]) == len(before["rule_areas"]) + 10
    for a in ours:
        assert len(a["layers"]) == 1 and a["no_tracks"] and a["no_vias"]
        assert not (a["no_pads"] or a["no_footprints"] or a["no_zone_fills"])
    assert {a["name"] for a in ours if a["name"].startswith("kicad-route:spoke:C1:2:F.Cu:")} == \
        {"kicad-route:spoke:C1:2:F.Cu:0", "kicad-route:spoke:C1:2:F.Cu:2"}
    assert reserved["zones"] == before["zones"] and reserved["tracks"] == before["tracks"]
    # Freerouting sees them: KiCad exports each as a single-layer Specctra keepout
    dsn = tmp_path / "r.dsn"
    e = subprocess.run([str(common.PCBNEW_PYTHON), str(route.KICAD_SCRIPTS / "specctra.py"), "export", board, dsn],
                       capture_output=True, text=True, env=common.tool_env(), timeout=600)
    assert e.returncode == 0, e.stdout + e.stderr
    text = dsn.read_text()
    assert text.count('(keepout "" (polygon F.Cu') == 1 + 6 and text.count('(keepout "" (polygon B.Cu') == 1 + 4
    # a second reservation on top would make strip ambiguous: refused
    again, _ = script("reserve", board, tmp_path / "plan.json")
    assert again.returncode != 0 and "strip them first" in again.stderr
    r, tag = script("strip", board)
    assert r.returncode == 0 and tag == {"stripped": 10, "remaining": 0, "rule_areas_left": 1}
    after = board_facts(board, tmp_path / "after.json")
    assert after["rule_areas"] == before["rule_areas"]
    assert board.read_bytes() == original
    r, tag = script("strip", board)                               # nothing to strip: file untouched
    assert tag["stripped"] == 0 and board.read_bytes() == original


def test_targeted_spokes_native_round_trip(fzone_dir, tmp_path):
    """A selected SMD pad retains its two corridors without reserving other pads."""
    plan = plan_of(fzone_dir / BOARD, tmp_path / "all.json")
    pad = next(p for p in plan["pads"] if p["ref"] == "C1")
    key = f"{pad['uuid']}@{pad['layer']}"
    before = json.dumps(plan, sort_keys=True)
    selected = spokes.select_pad_layers(plan, [key])
    assert json.dumps(plan, sort_keys=True) == before
    assert selected["pads"] == [pad]
    board = tmp_path / "target" / BOARD
    shutil.copytree(fzone_dir, board.parent)
    original = board.read_bytes()
    common.write_json(tmp_path / "selected.json", selected)
    r, tag = script("reserve", board, tmp_path / "selected.json")
    assert r.returncode == 0 and tag["reserved"] == 2
    facts = board_facts(board, tmp_path / "selected-facts.json")
    reserved = [a for a in facts["rule_areas"] if a["name"].startswith("kicad-route:spoke:")]
    assert len(reserved) == 2 and all(":C1:2:F.Cu:" in a["name"] for a in reserved)
    r, tag = script("strip", board)
    assert r.returncode == 0 and tag["stripped"] == 2 and board.read_bytes() == original
    # A stale identity cannot silently become an untargeted or empty reservation.
    with pytest.raises(ValueError):
        spokes.select_pad_layers(plan, [key, "missing@F.Cu"])
    with pytest.raises(ValueError):
        spokes.select_pad_layers(plan, [f"{pad['uuid']}@B.Cu"])
    with pytest.raises(ValueError):
        spokes.select_pad_layers(plan, [])


def test_targeted_spokes_wrapper(fzone_dir, tmp_path):
    """The real wrapper injects/strips only the target; unknown identity never routes."""
    plan = plan_of(fzone_dir / BOARD, tmp_path / "all.json")
    pad = next(p for p in plan["pads"] if p["ref"] == "C1")
    key = f"{pad['uuid']}@{pad['layer']}"
    work = tmp_path / "work"
    shutil.copytree(fzone_dir, work)
    board = work / BOARD
    original = board.read_bytes()
    calls = []

    def backend():
        calls.append(1)
        facts = board_facts(board, tmp_path / "callback-facts.json")
        ours = [a for a in facts["rule_areas"] if a["name"].startswith("kicad-route:spoke:")]
        assert len(ours) == 2 and all(":C1:2:F.Cu:" in a["name"] for a in ours)
        return {"status": "PASS", "reasons": [], "output_board": board}

    result = spokes.around_route(None, work, board, backend, pad_layers=[key])
    assert result["status"] == "PASS" and len(calls) == 1 and board.read_bytes() == original
    assert result["spoke_reservation"]["reserved"] == result["spoke_reservation"]["stripped"] == 2
    result = spokes.around_route(None, work, board, backend, pad_layers=["missing@F.Cu"])
    assert result["status"] == "FAIL" and len(calls) == 1 and board.read_bytes() == original


@pytest.mark.parametrize("backend", ["krt", "freerouting"])
def test_run_with_reserved_spokes(backend):
    """A reserved attempt on the fixture (J1.2/J2.2 spokes in the B.Cu pour): the backend
    routes around the corridors, the wrapper strips them, and the candidate passes every
    preservation check -- keepouts included -- under the input's own rules."""
    extra = (["--default-clearance", "0.4"] if backend == "krt" else
             ["--clearance-margin", "0.01", "--passes", "100", "--backend-arg=--router.fanout.enabled=true"])
    r, run_dir, result = run("run", "--backend", backend, "--budget", "180", "--reserve-spokes", *extra,
                             "--label", f"smoke-spokes-{backend}")
    res = result["process"]["spoke_reservation"]
    record(f"spokes_run_{backend}", summary(result) | {"spoke_reservation": res})
    assert result["settings"]["reserve_spokes"] == 0
    assert res["per_pad"] == 2 and res["pads"] == 2 and res["reserved"] == 4 == res["stripped"]
    assert not res["reasons"]
    assert result["process"]["status"] == route.PASS
    assert result["validation"]["preservation"]["keepouts"] == route.PASS
    assert all(v == route.PASS for v in result["validation"]["preservation"].values()), result["validation"]
    assert result["routing"]["status"] == route.COMPLETE and result["exit_code"] == 0, result["validation"]
    assert "kicad-route:spoke:" not in (OUT / result["candidate"]["board"]).read_text()
    assert "kicad-route:spoke:" in (run_dir / "work" / "input" / "route_demo" / BOARD).read_text()
    s = cli("spokes", str(run_dir), timeout=600)
    rep = json.loads((run_dir / "spokes.json").read_text())
    assert s.returncode == 0, s.stdout
    assert rep["reserved"]["corridors"] == 4 and rep["reserved"]["crossed_by_new_copper"] == []
    assert rep["new_starved"] == 0


@pytest.mark.parametrize("backend", ["krt", "freerouting"])
def test_reserved_corridors_hold_under_pressure(backend, fzone_dir):
    """On the F.Cu-pour variant new F.Cu routes pass close to the GND pads (unreserved
    attempts were seen crossing corridors). With reservation no new track or via may
    enter a reserved corridor, and the stripped candidate's rule areas equal the input's.

    The candidate is graded in full, but two outcomes are backend limits on this
    variant (with or without reservation), so they are pinned, not asserted away:
    KRT completes the routing but ships one /VIN via below the Power class size
    (0.6/0.3 mm for 0.8/0.4 mm), so new_copper_netclass_geometry is its only
    failing check (exit 3); Freerouting leaves /VIN open but its candidate passes
    every check (exit 2). Every other check, and DRC, must pass for both."""
    extra = (["--default-clearance", "0.4"] if backend == "krt" else
             ["--clearance-margin", "0.01", "--passes", "100", "--backend-arg=--router.fanout.enabled=true"])
    r, run_dir, result = run("run", "--backend", backend, "--budget", "180", "--dir", str(fzone_dir),
                             "--reserve-spokes", *extra, "--name", "route_demo_fzone",
                             "--label", f"smoke-spokes-fzone-{backend}")
    res = result["process"]["spoke_reservation"]
    s = cli("spokes", str(run_dir), timeout=600)
    rep = json.loads((run_dir / "spokes.json").read_text())
    record(f"spokes_fzone_{backend}", summary(result) | {"spoke_reservation": res, "spokes": {
        k: rep[k] for k in ("corridors_live", "corridors_crossed", "new_starved", "reserved")}})
    assert result["process"]["status"] == route.PASS
    assert res["pads"] == 5 and res["reserved"] == 10 == res["stripped"]
    assert result["validation"]["preservation"]["keepouts"] == route.PASS
    assert rep["reserved"]["corridors"] == 10 and rep["reserved"]["crossed_by_new_copper"] == [], rep["reserved"]
    assert rep["new_starved"] == 0 and s.returncode == 0
    v = result["validation"]
    assert v["parity_performed"] is True
    assert v["drc"]["new_violations"]["count"] == 0 and v["drc"]["new_parity"]["count"] == 0, v["reasons"]
    failing = {k for k, st in v["preservation"].items() if st != route.PASS}
    if backend == "krt":
        assert result["routing"]["status"] == route.COMPLETE, result["routing"]
        assert failing == {"new_copper_netclass_geometry"}, v["reasons"]
        below = next(c for c in json.loads((run_dir / "validate" / "preservation.json").read_text())
                     if c["check"] == "new_copper_netclass_geometry")["differences"]
        assert [(d["item"], d["net"], d["netclass"]) for d in below] == [("via", "/VIN", "Power")], below
        assert result["exit_code"] == r.returncode == 3
    else:
        assert failing == set() and v["status"] == route.PASS, v["reasons"]
        assert result["routing"]["status"] == route.INCOMPLETE and result["routing"]["unrouted_nets"] == ["/VIN"]
        assert result["exit_code"] == r.returncode == 2


def test_spoke_cut_attribution(fzone_dir):
    """Route a scope far from C1 on the F.Cu-pour variant, then hand-place a /SIG_A track
    hugging C1.2 the way the real-board routes did. Re-validated, the candidate has a new
    starved_thermal on C1.2, and `kicad-route spokes` names that track as the cut."""
    import pcbnew
    r, run_dir, result = run("run", "--backend", "freerouting", "--budget", "180", "--dir", str(fzone_dir),
                             "--nets", "/SIG_B_OUT", "--clearance-margin", "0.01",
                             "--backend-arg=--router.fanout.enabled=true", "--name", "route_demo_fzone",
                             "--label", "smoke-spokes-cut")
    assert result["process"]["status"] == route.PASS, result["process"]
    clean = cli("spokes", str(run_dir), timeout=600)
    assert clean.returncode == 0 and json.loads((run_dir / "spokes.json").read_text())["new_starved"] == 0
    board_path = run_dir / json.loads((run_dir / "attempt.json").read_text())["candidate_dir"] / BOARD
    board = pcbnew.LoadBoard(str(board_path))
    mm = pcbnew.FromMM
    added = []
    for a, b in (((112.0, 101.5), (112.0, 105.4)), ((112.0, 105.4), (109.0, 105.4))):  # right of / below C1.2
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
        t.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
        t.SetWidth(mm(0.2))
        t.SetLayer(pcbnew.F_Cu)
        t.SetNet(board.FindNet("/SIG_A"))
        board.Add(t)
        added.append(t.m_Uuid.AsString())
    board.Save(str(board_path))
    v = cli("validate", str(run_dir), timeout=600)
    vdir = sorted((run_dir / "revalidate").iterdir())[-1]
    s = cli("spokes", str(run_dir), "--validation", str(vdir), timeout=600)
    rep = json.loads((run_dir / "spokes.json").read_text())
    record("spokes_attribution", {k: rep[k] for k in ("new_starved", "new_starved_attributed", "corridors_live",
                                                      "corridors_crossed", "starved_pads", "crossings")})
    assert v.returncode == 3 and s.returncode == 3, s.stdout
    c1 = [p for p in rep["starved_pads"] if p.get("ref") == "C1"]
    assert len(c1) == 1 and c1[0]["new"] and c1[0]["attributed"] and c1[0]["layer"] == "F.Cu"
    assert set(c1[0]["cutting_items"]) == set(added)
    crossed = {round(c["direction_deg"]) for c in c1[0]["crossed_corridors"]}
    assert crossed == {0, 270}                                    # right and down; up survives (1 spoke)
    assert all(h["net"] == "/SIG_A" and h["layer"] == "F.Cu" for c in c1[0]["crossed_corridors"] for h in c["by"])
    assert rep["new_starved"] == rep["new_starved_attributed"] == 1


def test_spokes_board_preview(fzone_dir):
    r = cli("spokes", "--board", str(fzone_dir / BOARD), "--per-pad", "1")
    plan = json.loads((OUT / "spokes" / "route_demo" / "plan.json").read_text())
    assert r.returncode == 0 and "5 thermal pads" in r.stdout
    assert plan["corridors_selected"] == 5
