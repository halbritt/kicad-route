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


def test_a_run_records_what_it_removed():
    proc, run_dir, result = run("run", "--budget", "180", "--label", "dangling")
    swept = result["process"]["dangling_removed"]
    assert set(swept) == {"new_items", "removed_tracks", "removed_vias", "rounds", "refilled", "removed"}
    assert swept["new_items"] > 0 and len(swept["removed"]) == swept["removed_tracks"] + swept["removed_vias"]
    assert (run_dir / "work" / "before-routing" / BOARD).is_file()
    # what KiCad's DRC then finds on the candidate has no stub of this run in it
    new = json.loads((run_dir / "validate" / "new-findings.json").read_text())["violations"]
    assert not [v for v in new if v["type"] in ("track_dangling", "via_dangling")]
