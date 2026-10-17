"""Blocked-by receipts (src/kicad_route/blocked.py, docs/blocked.md).

Unit tests of the corridor geometry and the ranking on synthetic board facts, then
integration checks on the fixture: the keepout-wall variant comes back INCOMPLETE
and the receipts name the wall as what blocks every open connection; a complete
run has no receipts.
"""
import json
import math

import pytest

from helpers import OUT, cli, record, run, tree_hash
from kicad_route import blocked
from kicad_route import cli as route

MM = blocked.NM


def p(x, y):
    return (x * MM, y * MM)


# ------------------------------------------------------------------ geometry

def test_segment_geometry():
    assert blocked.segments_intersect(p(0, 0), p(2, 2), p(0, 2), p(2, 0))
    assert blocked.segments_intersect(p(0, 0), p(1, 0), p(1, 0), p(1, 5))      # shared endpoint
    assert not blocked.segments_intersect(p(0, 0), p(1, 0), p(0, 1), p(1, 1))  # parallel
    assert blocked.segment_distance(p(0, 0), p(1, 0), p(0, 1), p(1, 1)) == pytest.approx(MM)
    assert blocked.segment_distance(p(0, 0), p(2, 2), p(0, 2), p(2, 0)) == 0
    assert blocked.point_segment_distance(p(5, 3), p(0, 0), p(10, 0)) == pytest.approx(3 * MM)
    assert blocked.point_segment_distance(p(-3, 4), p(0, 0), p(10, 0)) == pytest.approx(5 * MM)


def test_polygon_geometry():
    concave = [p(0, 0), p(10, 0), p(10, 10), p(5, 5), p(0, 10)]  # notch from the top
    assert blocked.point_in_polygon(p(2, 2), concave)
    assert not blocked.point_in_polygon(p(5, 8), concave)
    square = [p(4, -1), p(6, -1), p(6, 1), p(4, 1)]
    # a segment straight through the square has no endpoint inside it
    assert blocked.polyline_polygon_distance([p(0, 0), p(10, 0)], square) == 0
    assert blocked.polyline_polygon_distance([p(0, 3), p(10, 3)], square) == pytest.approx(2 * MM)
    assert blocked.polyline_polygon_distance([p(5, 0)], square) == 0          # a point inside


def test_arc_and_rectangle_helpers():
    pts = blocked.arc_points(p(10, 0), p(10 / math.sqrt(2), 10 / math.sqrt(2)), p(0, 10))
    assert pts[0] == pytest.approx(p(10, 0)) and pts[-1] == pytest.approx(p(0, 10))
    assert all(math.hypot(*q) == pytest.approx(10 * MM, abs=1) for q in pts)
    assert any(q[0] > 0 and q[1] > 0 for q in pts)                            # the short way round
    assert blocked.arc_points(p(0, 0), p(1, 0), p(2, 0)) == [p(0, 0), p(1, 0), p(2, 0)]
    r = blocked.rect_polygon(p(0, 0), (4 * MM, 2 * MM), 90)
    xs, ys = [q[0] for q in r], [q[1] for q in r]
    assert max(xs) - min(xs) == pytest.approx(2 * MM) and max(ys) - min(ys) == pytest.approx(4 * MM)


def test_corridor_paths_include_both_manhattan_routes():
    paths = blocked.corridor_paths(p(0, 0), p(4, 3))
    assert paths["straight"] == [p(0, 0), p(4, 3)]
    assert paths["L_horizontal_first"][1] == p(4, 0)
    assert paths["L_vertical_first"][1] == p(0, 3)
    assert list(blocked.corridor_paths(p(0, 0), p(4, 0))) == ["straight"]   # already Manhattan


# ------------------------------------------------- synthetic board analysis

LAYERS = [{"name": "F.Cu", "type": 0}, {"name": "B.Cu", "type": 0}]
NETS = {n: {"netclass": "Default", "track_width": 200_000, "clearance": 200_000,
            "via_diameter": 600_000, "via_drill": 300_000} for n in ("A", "B", "C", "D", "GND")}


def pad(x, y, net, layers=("F.Cu",), size=(1, 1)):
    return {"position": [x * MM, y * MM], "net": net, "size": [size[0] * MM, size[1] * MM],
            "drill": [0, 0], "attribute": 1, "layers": list(layers)}


def track(uuid, net, layer, a, b, locked=False, width=0.2):
    return {"kind": "segment", "uuid": uuid, "net": net, "layer": layer, "locked": locked,
            "width": int(width * MM), "start": [a[0] * MM, a[1] * MM], "end": [b[0] * MM, b[1] * MM]}


def board(**over):
    facts = {"copper_layers": LAYERS, "nets": NETS, "tracks": [], "vias": [], "zones": [], "rule_areas": [],
             "footprints": {}, "outline": [{"shape": "Rect", "layer": "Edge.Cuts", "width": 100_000,
                                            "start": [-5 * MM, -10 * MM], "end": [35 * MM, 10 * MM]}]}
    facts.update(over)
    return facts


def unconnected(net, a, b, da="Pad 1 [{n}] of U1 on F.Cu", db="Pad 1 [{n}] of U2 on F.Cu"):
    return {"type": "unconnected_items", "severity": "error", "description": "Missing connection between items",
            "items": [{"description": da.format(n=net), "pos": {"x": a[0], "y": a[1]}, "uuid": "u-a"},
                      {"description": db.format(n=net), "pos": {"x": b[0], "y": b[1]}, "uuid": "u-b"}]}


def test_analysis_lists_blockers_per_layer_with_origin_and_lock():
    """A--A from (0,0) to (20,0). F.Cu: a keepout across the board. B.Cu: a locked
    input track of B, a new track of C, a same-net track (never a blocker) and a
    zone of D (soft). F.Cu has fewer hard blockers, so it is the best layer."""
    locked_b = track("in-b", "B", "B.Cu", (15, -9), (15, 9), locked=True)
    ref = board(tracks=[locked_b])
    facts = board(
        tracks=[locked_b, track("new-c", "C", "B.Cu", (5, -9), (5, 9)), track("same-a", "A", "B.Cu", (8, -9), (8, 9))],
        rule_areas=[{"name": "wall", "layers": ["F.Cu"], "no_tracks": True, "no_vias": True, "no_pads": False,
                     "no_footprints": False, "no_zone_fills": True,
                     "outline": [[9 * MM, -11 * MM], [11 * MM, -11 * MM], [11 * MM, 11 * MM], [9 * MM, 11 * MM]]}],
        zones=[{"name": "plane", "net": "D", "layers": ["B.Cu"], "priority": 0,
                "outline": [[0, -2 * MM], [30 * MM, -2 * MM], [30 * MM, 2 * MM], [0, 2 * MM]]}],
        footprints={"U1": {"orientation": 0, "locked": False, "pads": {"1": pad(0, 0, "A")}},
                    "U2": {"orientation": 0, "locked": False, "pads": {"1": pad(20, 0, "A")}}})
    rep = blocked.analyze({"unconnected_items": [unconnected("A", (0, 0), (20, 0))]}, facts, ref)
    (c,) = rep["connections"]
    assert c["corridor_width_mm"] == pytest.approx(0.6)
    fcu, bcu = c["layers"]["F.Cu"], c["layers"]["B.Cu"]
    assert [b["id"] for b in fcu["hard"]] == ["rule_area:wall"]
    by_id = {b["id"]: b for b in bcu["hard"]}
    assert set(by_id) == {"in-b", "new-c"}                                     # same net never blocks
    assert by_id["in-b"]["origin"] == "input" and by_id["in-b"]["locked"] is True
    assert by_id["new-c"]["origin"] == "new" and by_id["new-c"]["category"] == "new_copper"
    assert [b["id"] for b in bcu["soft"]] == ["zone:plane:D"]
    assert c["best_layer"] == "F.Cu" and c["best_layer_hard_count"] == 1
    assert rep["top"][0]["id"] == "rule_area:wall" and rep["top"][0]["blocks"] == 1
    # copper on the other layer is still listed, as blocking on that layer only
    inb = next(e for e in rep["ranking"]["input_copper"] if e["id"] == "in-b")
    assert inb["blocks"] == 0 and inb["blocks_any_layer"] == 1
    assert rep["ranking"]["input_copper_by_net"][0] == {"net": "B", "items": 1, "locked": 1, "blocks": 0}


def test_manhattan_corridor_avoids_a_straight_line_blocker():
    via = {"uuid": "v-b", "net": "B", "position": [10 * MM, int(2.5 * MM)], "diameter": 600_000, "drill": 300_000,
           "via_type": "through", "layers": ["F.Cu", "B.Cu"], "locked": False}
    facts = board(vias=[via])
    rep = blocked.analyze({"unconnected_items": [unconnected("A", (0, 0), (20, 5))]}, facts, facts)
    lay = rep["connections"][0]["layers"]["F.Cu"]
    assert lay["corridors"]["straight"]["hard"] == 1
    assert lay["hard_count"] == 0 and lay["corridor"].startswith("L_")
    assert rep["unobstructed_connections"] == 1 and rep["top"] == []           # nothing to rip


def test_footprint_pads_rank_by_connections_blocked():
    """Pads of other nets on one footprint block two connections: the footprint ranks
    with count 2, its pads listed. Out-of-scope nets are not analysed."""
    facts = board(footprints={"U3": {"orientation": 90, "locked": True, "pads": {
        "1": pad(10, 0, "C", ("F.Cu", "B.Cu")), "2": pad(10, 5, "", ("F.Cu", "B.Cu"))}}})
    drc = {"unconnected_items": [unconnected("A", (0, 0), (20, 0)), unconnected("B", (0, 5), (20, 5)),
                                 unconnected("D", (0, -8), (20, -8))]}
    rep = blocked.analyze(drc, facts, facts, scope={"A", "B"})
    assert rep["connections_analyzed"] == 2
    top = rep["top"][0]
    assert top["id"] == "footprint:U3" and top["blocks"] == 2 and top["nets_blocked"] == ["A", "B"]
    fp = rep["ranking"]["footprint"][0]
    assert fp["pads"] == ["1", "2"] and fp["nets"] == ["C"] and fp["locked"] is True


def test_zone_endpoint_lands_off_the_pads_own_island():
    """KiCad reports a pad left unconnected to a zone with the zone's anchor point. The
    corridor goes to the nearest same-net pad inside the zone that is not on the pad's
    own copper island instead (here not U5.1, which a track already joins)."""
    facts = board(
        tracks=[track("t", "GND", "F.Cu", (30, 0), (28, 0))],
        zones=[{"name": "GND_B", "net": "GND", "layers": ["F.Cu", "B.Cu"], "priority": 0,
                "outline": [[-4 * MM, -9 * MM], [34 * MM, -9 * MM], [34 * MM, 9 * MM], [-4 * MM, 9 * MM]]}],
        footprints={"J1": {"orientation": 0, "locked": False, "pads": {"2": pad(30, 0, "GND", ("F.Cu", "B.Cu"))}},
                    "U5": {"orientation": 0, "locked": False, "pads": {"1": pad(28, 0, "GND")}},
                    "U6": {"orientation": 0, "locked": False, "pads": {"1": pad(5, 1, "GND")}}})
    drc = {"unconnected_items": [unconnected("GND", (30, 0), (-4, -9), da="PTH pad 2 [{n}] of J1",
                                             db="Zone 'GND_B' [{n}] on B.Cu, priority 0")]}
    rep = blocked.analyze(drc, facts, facts)
    end = rep["connections"][0]["endpoints"][1]
    assert end["kind"] == "zone" and end["position_mm"] == [5.0, 1.0] and "island" in end["note"]


def test_render_and_errors():
    assert blocked.render_md(None) == []
    assert "failed" in blocked.render_md({"error": "KeyError: x"})[0]
    s = blocked.write_report("/nonexistent-dir", {"unconnected_items": []}, board(), None, None)
    assert s["error"].startswith("FileNotFoundError")     # recorded, never raised into the verdict


# --------------------------------------------------------------- integration

def test_blocked_receipts_name_the_wall(blocked_dir):
    """The keepout wall cuts J2 off: every open connection is blocked by it, on every
    layer, and it is the top-ranked blocker. Recomputing with `kicad-route blocked`
    and revalidating give the same answer; neither touches the candidate."""
    # --default-clearance 0.4: without it KRT also breaks the Power-to-signal rule it
    # cannot read (exit 3), and the exit code would not show that an incomplete but
    # otherwise valid candidate is reported as exit 2
    r, run_dir, result = run("run", "--backend", "krt", "--budget", "180", "--dir", str(blocked_dir),
                             "--backend-env", "KICAD_NET_RESCUE=0", "--default-clearance", "0.4",
                             "--label", "blocked-receipts-krt")
    rt = result["routing"]
    record("blocked_receipts", {"routing": rt, "exit_code": result["exit_code"]})
    assert result["validation"]["status"] == route.PASS, result["validation"]["reasons"]
    assert rt["status"] == route.INCOMPLETE and r.returncode == result["exit_code"] == 2
    bb = rt["blocked_by"]
    assert "error" not in bb, bb
    report = json.loads((run_dir / "validate" / "blocked-by.json").read_text())
    assert report["connections_analyzed"] == bb["connections_analyzed"] == rt["unconnected_in_scope"]
    top = bb["top"][0]
    assert top["id"] == "rule_area:wall" and top["category"] == "keepout"
    assert top["blocks"] == top["blocks_any_layer"] == report["connections_analyzed"]
    assert set(top["nets_blocked"]) == set(rt["unrouted_nets"])
    for c in report["connections"]:
        assert all(any(b["id"] == "rule_area:wall" for b in lay["hard"]) for lay in c["layers"].values()), c
    assert "keepout 'wall'" in (run_dir / "RESULT.md").read_text()

    board_file = OUT / result["candidate"]["board"]
    before = tree_hash(board_file.parent)
    again = cli("blocked", str(run_dir))
    assert again.returncode == 0, again.stdout + again.stderr
    assert "keepout 'wall'" in again.stdout
    assert json.loads((run_dir / "result.json").read_text())["routing"]["blocked_by"]["top"][0]["id"] == top["id"]
    assert tree_hash(board_file.parent) == before

    v = cli("validate", str(run_dir), timeout=600)
    reval = sorted((run_dir / "revalidate").iterdir())[-1]
    res = json.loads((reval / "result.json").read_text())
    assert v.returncode == res["exit_code"] == 2 and res["routing"]["blocked_by"]["top"][0]["id"] == "rule_area:wall"
    assert (reval / "blocked-by.json").exists()
    assert cli("blocked", str(reval)).returncode == 0


def test_complete_run_has_no_blocked_by():
    r, run_dir, result = run("run", "--backend", "krt", "--budget", "180", "--nets", "/SIG_A*",
                             "--default-clearance", "0.4", "--label", "blocked-complete-krt")
    assert result["routing"]["status"] == route.COMPLETE, result["routing"]
    assert "blocked_by" not in result["routing"]
    assert not (run_dir / "validate" / "blocked-by.json").exists()
    again = cli("blocked", str(run_dir))
    assert again.returncode == 0 and "nothing is blocked" in again.stdout
    assert not (run_dir / "validate" / "blocked-by.json").exists()
