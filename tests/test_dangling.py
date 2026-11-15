"""Copper a routing run left ending in the air is removed before the board is graded (kicad/dangling.py).

Found on designer boards in the router lab: runs with a dozen connections open and no rule
broken came back as boards not to adopt, each for one or two `track_dangling` or `via_dangling`
findings on pieces of connections the engine had given up or been stopped in.
"""
import json
import shutil
import subprocess

import pytest

from helpers import FIXTURE, run
from kicad_route import cli as route

BOARD = "route_demo.kicad_pcb"

# Board surgery runs in KiCad's interpreter, as every pcbnew step of the wrapper does.
# BEFORE: the fixture with a stub of its own. ROUTED: that, plus a two-piece stub with a via at
# its end from a signal pad, a track that joins two pads, and a ground track that ends in the
# ground pour.
BUILD = """
import sys
import pcbnew
mm = pcbnew.FromMM


def pad(board, ref, number):
    return next(p for fp in board.GetFootprints() if fp.GetReference() == ref
                for p in fp.Pads() if p.GetNumber() == number)


def track(board, net, layer, start, end, width_mm=0.2):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(*start))
    t.SetEnd(pcbnew.VECTOR2I(*end))
    t.SetLayer(layer)
    t.SetWidth(mm(width_mm))
    t.SetNetCode(board.GetNetcodeFromNetname(net))
    board.Add(t)


for path, routed in ((sys.argv[1], False), (sys.argv[2], True)):
    board = pcbnew.LoadBoard(path)
    a = pad(board, "J1", "3").GetPosition()
    track(board, "/SIG_A", pcbnew.F_Cu, (a.x, a.y), (a.x - mm(1.7), a.y))      # the input's own stub
    if routed:
        track(board, "/SIG_A", pcbnew.F_Cu, (a.x, a.y), (a.x - mm(1.0), a.y + mm(1.0)))
        track(board, "/SIG_A", pcbnew.F_Cu, (a.x - mm(1.0), a.y + mm(1.0)), (a.x - mm(1.0), a.y + mm(2.0)))
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(a.x - mm(1.0), a.y + mm(2.0)))
        via.SetWidth(pcbnew.F_Cu, mm(0.6))
        via.SetDrill(mm(0.3))
        via.SetNetCode(board.GetNetcodeFromNetname("/SIG_A"))
        board.Add(via)
        # a whole connection: the two pads of /SIG_A_OUT (a surface-mount pad and a drilled one)
        p, q = pad(board, "R1", "2").GetPosition(), pad(board, "J2", "3").GetPosition()
        track(board, "/SIG_A_OUT", pcbnew.F_Cu, (p.x, p.y), (q.x, q.y))
        # from a ground pad across its thermal gap into the ground pour on the back
        g = pad(board, "J1", "2").GetPosition()
        track(board, "GND", pcbnew.B_Cu, (g.x, g.y), (g.x + mm(3.0), g.y), width_mm=0.5)
    board.Save(path)
"""

COPPER = """
import json
import sys
import pcbnew
board = pcbnew.LoadBoard(sys.argv[1])
print("COPPER_JSON: " + json.dumps(sorted([type(t).__name__, t.GetNetname(), board.GetLayerName(t.GetLayer())]
                                          for t in board.GetTracks())))
"""


def kicad(script, *paths):
    done = subprocess.run([route.pcbnew_python(), "-c", script, *map(str, paths)], capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


def sweep(before, routed):
    done = subprocess.run([route.pcbnew_python(), str(route.KICAD_SCRIPTS / "dangling.py"), "sweep", str(before), str(routed)],
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-2000:]
    return route.tagged_json(done.stdout, "DANGLING_JSON")


def copper(path):
    return [tuple(item) for item in route.tagged_json(kicad(COPPER, path), "COPPER_JSON")]


@pytest.fixture()
def boards(tmp_path):
    before, routed = tmp_path / "before" / BOARD, tmp_path / "routed" / BOARD
    for path in (before, routed):
        shutil.copytree(FIXTURE, path.parent, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    kicad(BUILD, before, routed)
    return before, routed


def test_sweep_removes_the_runs_own_stubs_and_nothing_else(boards):
    before, routed = boards
    had, got = copper(before), copper(routed)
    assert len(got) == len(had) + 5
    found = sweep(before, routed)
    assert found["new_items"] == 5 and found["removed_tracks"] == 2 and found["removed_vias"] == 1, found
    assert found["rounds"] >= 1 and found["refilled"] is True
    assert sorted((k[0], k[1]) for k in found["removed"]) == [("track", "/SIG_A"), ("track", "/SIG_A"), ("via", "/SIG_A")]
    # the input's own stub, the whole connection and the track into the pour are all still there
    assert copper(routed) == sorted(had + [("PCB_TRACK", "/SIG_A_OUT", "F.Cu"), ("PCB_TRACK", "GND", "B.Cu")])


def test_a_board_without_stubs_is_not_rewritten(boards):
    before, _ = boards
    same = before.parent.parent / "same" / BOARD
    shutil.copytree(before.parent, same.parent)
    stamp, data = same.stat().st_mtime_ns, same.read_bytes()
    found = sweep(before, same)
    assert found["removed_tracks"] == found["removed_vias"] == 0 and found["new_items"] == 0 and found["removed"] == []
    assert same.read_bytes() == data and same.stat().st_mtime_ns == stamp


BRANCHED = """
import sys
import pcbnew as k
def point(x, y):
    return k.VECTOR2I(k.FromMM(x), k.FromMM(y))
board = k.BOARD()
net = k.NETINFO_ITEM(board, "SIGNAL")
board.Add(net)
via_joint = sys.argv[3] == "via"
for number, (x, y) in enumerate([(10, 10), (20, 15)], 1):
    fp = k.FOOTPRINT(board)
    fp.SetReference("J" + str(number))
    fp.SetPosition(point(x, y))
    board.Add(fp)
    pad = k.PAD(fp)
    fp.Add(pad)
    pad.SetNumber("1")
    pad.SetAttribute(k.PAD_ATTRIB_SMD)
    pad.SetShape(k.PAD_SHAPE_RECT)
    layers = k.LSET()
    layers.AddLayer(k.B_Cu if via_joint and number == 2 else k.F_Cu)
    pad.SetLayerSet(layers)
    pad.SetSize(point(1, 1))
    pad.SetPosition(point(x, y))
    pad.SetNet(net)
for a, b in [((5, 5), (25, 5)), ((25, 5), (25, 25)),
             ((25, 25), (5, 25)), ((5, 25), (5, 5))]:
    edge = k.PCB_SHAPE(board, k.SHAPE_T_SEGMENT)
    edge.SetLayer(k.Edge_Cuts)
    edge.SetStart(point(*a))
    edge.SetEnd(point(*b))
    edge.SetWidth(k.FromMM(0.05))
    board.Add(edge)
k.SaveBoard(sys.argv[1], board)
for index, (a, b) in enumerate([((10, 10), (10, 20)), ((10, 15), (20, 15))]):
    track = k.PCB_TRACK(board)
    track.SetNet(net)
    track.SetLayer(k.B_Cu if via_joint and index == 1 else k.F_Cu)
    track.SetStart(point(*a))
    track.SetEnd(point(*b))
    track.SetWidth(k.FromMM(0.25))
    board.Add(track)
if via_joint:
    via = k.PCB_VIA(board)
    via.SetPosition(point(10, 15))
    via.SetWidth(k.F_Cu, k.FromMM(0.6))
    via.SetDrill(k.FromMM(0.3))
    via.SetNet(net)
    board.Add(via)
k.SaveBoard(sys.argv[2], board)
"""


@pytest.mark.parametrize("joint", ["track", "via"])
def test_a_dangling_end_does_not_remove_the_connected_branch(tmp_path, joint):
    before, routed = tmp_path / "before.kicad_pcb", tmp_path / "routed.kicad_pcb"
    kicad(BRANCHED, before, routed, joint)
    initial, info = route.kicad_drc(routed, tmp_path / "initial", "initial", False)
    assert initial is not None, info
    assert initial["unconnected_items"] == []
    assert any(v["type"] == "track_dangling" for v in initial["violations"])
    original = routed.read_bytes()
    found = sweep(before, routed)
    final, info = route.kicad_drc(routed, tmp_path / "final", "final", False)
    assert final is not None, info
    assert final["unconnected_items"] == []
    # Preserve the connection; this conservative repair does not trim the dead tail
    # or conceal the native warning that still needs to be resolved.
    assert found["removed_tracks"] == found["removed_vias"] == 0
    assert routed.read_bytes() == original
    assert any(v["type"] == "track_dangling" for v in final["violations"])


def test_a_run_records_what_it_removed():
    proc, run_dir, result = run("run", "--budget", "180", "--label", "dangling")
    swept = result["process"]["dangling_removed"]
    assert set(swept) == {"new_items", "removed_tracks", "removed_vias", "rounds", "refilled", "removed"}
    assert swept["new_items"] > 0 and len(swept["removed"]) == swept["removed_tracks"] + swept["removed_vias"]
    assert (run_dir / "work" / "before-routing" / BOARD).is_file()
    # what KiCad's DRC then finds on the candidate has no stub of this run in it
    new = json.loads((run_dir / "validate" / "new-findings.json").read_text())["violations"]
    assert not [v for v in new if v["type"] in ("track_dangling", "via_dangling")]
