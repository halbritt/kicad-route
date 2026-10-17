"""KICADROUTE-52 first concrete step: the spoke-corridor geometry as a standalone
calculation, checked against KiCad's own zone filler.

`run --reserve-spokes` (tests/test_spokes.py) reserves one corridor per spoke and
claims that a foreign track kept outside it cannot erode the refilled pour at the
spoke end below min_thickness. Here that claim is tested directly: on the fixture
with an F.Cu GND pour (C1 is the two-pin part: C1.1 /VIN, C1.2 GND in the pour),
foreign tracks are put against each corridor's edge, the zone is refilled by
KiCad, and the spoke must survive with a min_thickness anchor; moved 10 um inside,
it must not (the corridor length is the exact threshold). kicad-cli DRC confirms
the result natively. Board files are never modified except throwaway copies.
"""
import importlib.util
import json
import math
import shutil
import subprocess

import pytest

from helpers import FIXTURE, OUT, ROOT, cli, record
from kicad_route import common, spokes

SCRIPT = ROOT / "src" / "kicad_route" / "kicad" / "spokes.py"
BOARD = "route_demo.kicad_pcb"
_spec = importlib.util.spec_from_file_location("kicad_spokes_script", SCRIPT)
ks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ks)


def script(*args):
    r = subprocess.run([str(common.PCBNEW_PYTHON), str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                       env=common.tool_env(), timeout=600)
    tag = next((json.loads(l.split(":", 1)[1]) for l in r.stdout.splitlines() if l.startswith("SPOKES_JSON:")), None)
    assert r.returncode == 0 and tag is not None, r.stdout + r.stderr
    return tag


@pytest.fixture(scope="module")
def fzone(tmp_path_factory):
    """The fixture plus an F.Cu GND pour (as in test_spokes) and its corridor plan."""
    import pcbnew
    d = tmp_path_factory.mktemp("adhd_fzone") / "route_demo"
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
    plan_path = d.parent / "plan.json"
    script("plan", d / BOARD, plan_path)
    return d, plan_path, json.loads(plan_path.read_text())


def verify(fz, tmp_path, name, *extra):
    d, plan_path, _ = fz
    before = common.sha256(d / BOARD)
    tag = script("verify", d / BOARD, plan_path, tmp_path / f"{name}.json", *extra)
    assert common.sha256(d / BOARD) == before, "verify must never modify the board"
    return tag, json.loads((tmp_path / f"{name}.json").read_text())


def c1_2(plan):
    return next(p for p in plan["pads"] if (p["ref"], p["pad"], p["layer"]) == ("C1", "2", "F.Cu"))


# ------------------------------------------------------------ pure geometry

def test_corridor_geometry_square_pad():
    """A 1 x 1 mm square pad at the origin, stock 90 degree spokes, fixture-like values."""
    sq = [[(-500_000, -500_000), (500_000, -500_000), (500_000, 500_000), (-500_000, 500_000)]]
    gap, sw, mt, cl, me = 500_000, 500_000, 250_000, 400_000, 5_000
    g = ks.corridor_geometry((0, 0), sq, 0, 90, gap, sw, mt, cl, me)
    assert [round(c["angle"]) for c in g] == [90, 180, 270, 360]
    up = g[0]                                   # 90 degrees: up on screen, -y in KiCad coordinates
    assert up["d"] == pytest.approx((0, -1), abs=1e-9)
    assert up["reach"] == pytest.approx(500_000) and up["start"] == pytest.approx(500_000)
    assert up["end"] == pytest.approx(500_000 + gap + mt + cl + me)
    assert up["half_width"] == sw / 2 + cl
    assert up["test_point"] == pytest.approx((0, -(500_000 + gap + me + mt / 2)), abs=1e-6)
    xs, ys = zip(*up["polygon"])
    assert (min(xs), max(xs)) == (-650_000, 650_000) and (min(ys), max(ys)) == (-1_655_000, -500_000)
    # 45 degree spokes on the same square: they leave through the corners
    diag = ks.corridor_geometry((0, 0), sq, 0, 45, gap, sw, mt, cl, me)[0]
    assert diag["reach"] == pytest.approx(500_000 * math.sqrt(2))
    assert diag["start"] == pytest.approx(500_000 * math.sqrt(2))


def test_adversaries_touch_the_corridor_edge():
    """The adversary tracks' copper edge lies exactly on the corridor boundary, and
    `intrude` moves it inside by that much."""
    sq = [[(-500_000, -500_000), (500_000, -500_000), (500_000, 500_000), (-500_000, 500_000)]]
    for spoke_angle in (90, 45):
        for g in ks.corridor_geometry((1_000_000, 2_000_000), sq, 30, spoke_angle, 500_000, 500_000, 250_000,
                                      400_000, 5_000):
            cr = {"direction_deg": g["angle"] % 360, "start": g["start"], "end": g["end"],
                  "half_width": g["half_width"]}
            for mode, count in (("end", 1), ("sides", 2), ("u", 3)):
                for intrude in (0, 10_000, 50_000):
                    segs = ks.adversary_segments((1_000_000, 2_000_000), cr, mode, 200_000, intrude)
                    assert len(segs) == count
                    for a, b in segs:
                        dist = spokes.seg_poly_distance(a, b, g["polygon"])
                        assert dist == pytest.approx(100_000 - intrude, abs=2), (mode, intrude)


def test_plan_carries_the_geometry(fzone):
    """plan's corridors are corridor_geometry's, with the reach and zone the verifier needs."""
    _, _, plan = fzone
    c1 = c1_2(plan)
    assert c1["zone"] == "GND_F" and c1["zone_uuid"]
    for cr in c1["corridors"]:
        assert cr["end"] - cr["reach"] == c1["gap"] + c1["min_thickness"] + plan["clearance"] + plan["max_error"]


# ------------------------------------------------------------ KiCad's zone filler

def test_tracks_at_the_corridor_edge_leave_the_spoke_whole(fzone, tmp_path):
    """Every live corridor (19 on the variant), one net per net class, a track across the
    far end and tracks along both sides: KiCad keeps the spoke, and the pour beyond the
    gap still covers a min_thickness x spoke_width strip."""
    tag, rep = verify(fzone, tmp_path, "edge", "--all-live", "1")
    record("adhd_spoke_keepout_edge", {k: rep[k] for k in ("corridors_checked", "corridors_held", "failed",
                                                          "netclasses", "modes")})
    assert rep["netclasses"] == {"Default": "/SIG_A", "Power": "/VIN"}
    assert rep["corridors_checked"] == fzone[2]["corridors_live"] == 19
    assert rep["corridors_held"] == 19 and rep["failed"] == [] and rep["errors"] == 0
    checks = [ch for p in rep["pads"] for c in p["corridors"] for ch in c["checks"]]
    assert len(checks) == 19 * 2 * 2                                   # modes x net classes
    assert all(ch["kept"] and ch["anchor_coverage"] >= ks.ANCHOR_COVERAGE for ch in checks)


def test_ten_microns_inside_the_corridor_starves_the_spoke(fzone, tmp_path):
    """The corridor length is the exact threshold: a /SIG_A track 10 um inside the far end
    (the 0.4 mm Power-to-signal rule) makes KiCad drop the spoke. A /VIN track there (Power
    to Power: 0.3 mm) does not, so the verifier must try every net class, not one net."""
    _, rep = verify(fzone, tmp_path, "in10", "--intrude", "10000", "--all-live", "1")
    assert rep["corridors_held"] == 0 and len(rep["failed"]) == 19
    for p in rep["pads"]:
        for c in p["corridors"]:
            by = {(ch["mode"], ch["netclass"]): ch for ch in c["checks"]}
            assert c["baseline"]["kept"]
            assert not by[("end", "Default")]["kept"]                  # spoke dropped by KiCad
            assert by[("end", "Power")]["held"]
            # from the sides the half-width is conservative: KiCad still keeps the spoke,
            # only the spoke-wide anchor strip is nibbled
            assert by[("sides", "Default")]["kept"]
            assert by[("sides", "Default")]["anchor_coverage"] < ks.ANCHOR_COVERAGE
    _, rep50 = verify(fzone, tmp_path, "in50", "--intrude", "50000")
    for p in rep50["pads"]:
        for c in p["corridors"]:
            by = {(ch["mode"], ch["netclass"]): ch for ch in c["checks"]}
            assert by[("end", "Default")]["anchor_coverage"] == 0 and by[("sides", "Default")]["kept"]


def test_trench_around_the_corridor_is_not_held(fzone, tmp_path):
    """Tracks hugging the end and both sides at once (all outside the corridor) leave
    KiCad's spoke in place, but it lands on a pocket cut off from the pour. Corridors
    alone do not prevent this; the verifier reports it as not held."""
    _, rep = verify(fzone, tmp_path, "trench", "--modes", "u")
    assert rep["corridors_checked"] == fzone[2]["corridors_selected"] == 10 and rep["corridors_held"] == 0
    for p in rep["pads"]:
        for c in p["corridors"]:
            sig = next(ch for ch in c["checks"] if ch["netclass"] == "Default")
            assert sig["kept"] and 0 < sig["body_area"] < c["baseline"]["body_area"] / 1000


# ------------------------------------------------------------ native DRC agrees

def test_native_drc_agrees(fzone, tmp_path):
    """kicad-cli DRC with --refill-zones on copies with /SIG_A tracks around C1.2's three
    live corridors: at the edge, no starved_thermal; 50 um inside two of them, C1.2 is
    starved (1 spoke of 2); a trench around all three keeps the spoke count (no
    starved_thermal) but grading still fails it: DRC reports the pad's pour piece as
    unconnected and the pour facts put C1.2 off the main pour (isolated_island)."""
    import pcbnew
    d, _, plan = fzone
    c1 = c1_2(plan)
    live = [cr for cr in c1["corridors"] if cr["live"]]
    assert len(live) == 3

    def drc(name, mode, intrude, corridors):
        b_dir = tmp_path / name
        shutil.copytree(d, b_dir)
        board = pcbnew.LoadBoard(str(b_dir / BOARD))
        for cr in corridors:
            for a, b in ks.adversary_segments(c1["position"], cr, mode, 200_000, intrude):
                t = pcbnew.PCB_TRACK(board)
                t.SetStart(pcbnew.VECTOR2I(*a))
                t.SetEnd(pcbnew.VECTOR2I(*b))
                t.SetWidth(200_000)
                t.SetLayer(pcbnew.F_Cu)
                t.SetNet(board.FindNet("/SIG_A"))
                board.Add(t)
        board.Save(str(b_dir / BOARD))
        out = b_dir / "drc.json"
        r = subprocess.run(["kicad-cli", "pcb", "drc", "--refill-zones", "--save-board", "--format", "json",
                            "--severity-all",
                            "--units", "mm", "-o", str(out), str(b_dir / BOARD)], capture_output=True, text=True,
                           timeout=600)
        assert r.returncode == 0, r.stdout + r.stderr
        rep = json.loads(out.read_text())
        starved = [v for v in rep["violations"] if v["type"] == "starved_thermal"
                   and any("of C1 " in i["description"] for i in v["items"])]
        unconnected = [[i["description"] for i in v["items"]] for v in rep["unconnected_items"]]
        return starved, unconnected

    edge, edge_unc = drc("edge", "end", 0, live)
    inside, _ = drc("inside", "end", 50_000, live[:2])
    trench, trench_unc = drc("trench", "u", 0, live)
    record("adhd_spoke_keepout_drc", {"edge": edge, "inside": inside, "trench": trench,
                                      "trench_unconnected": trench_unc})
    assert edge == [] and not any("Pad 2 [GND] of C1" in i for u in edge_unc for i in u)
    assert len(inside) == 1 and "actual 1" in inside[0]["description"]
    assert trench == []
    assert any(any("Zone 'GND_F'" in i for i in u) for u in trench_unc)
    # pour_integrity's facts (on the refilled copy) see C1.2 off the main pour: isolated_island
    facts = tmp_path / "trench-pour.json"
    r = subprocess.run([str(common.PCBNEW_PYTHON), str(ROOT / "src" / "kicad_route" / "kicad" / "pour.py"),
                        str(tmp_path / "trench" / BOARD), str(facts)], capture_output=True, text=True,
                       env=common.tool_env(), timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    pad = json.loads(facts.read_text())["pads"]["C1.2@F.Cu"]
    assert pad["spokes"] == 3 and pad["touches_fill"] and pad["in_main"] is False


def test_cli_board_verify(fzone):
    d, _, _ = fzone
    r = cli("spokes", "--board", str(d / BOARD), "--verify")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "verify: 10 of 10 selected corridors hold" in r.stdout
    rep = json.loads((OUT / "spokes" / "route_demo" / "verify.json").read_text())
    assert rep["corridors_checked"] == rep["corridors_held"] == 10 and rep["intrude"] == 0
