"""A starved thermal of the run's own making is repaired (thermals.py, kicad/spokes.py clear).

The board is the route_demo fixture with a GND pour on F.Cu, where the surface-mount pad
C1.2 joins the pour through thermal relief: three live spokes, of which the board asks two.
The "routing" is laid by hand: /SIG_A from J1.3 to R1.1 round C1.2, below it and to its right,
the way real routes were seen to hug such a pad. KiCad then counts one spoke and reports
`starved_thermal`.

The engine's part (routing the cleared net once more) is played by a stand-in that lays a known
route, so the test holds what the repair itself does: which copper it takes out, what it
reserves, that it keeps a repair only when KiCad's report is the better for it, and that the
input's own copper and the caller's board are left alone otherwise. The engine's own pass is
krt_plane_pass, which the plane passes of every run use.
Shared harness: helpers.py and conftest.py.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from helpers import FIXTURE, ROOT, record
from kicad_route import cli as route
from kicad_route import common, thermals

SCRIPT = ROOT / "src" / "kicad_route" / "kicad" / "spokes.py"
BOARD = "route_demo.kicad_pcb"

# /SIG_A from J1.3 to R1.1: below C1.2 (y 105.4) and up its right side (x 112.0)
HUGGING = [(104.0, 111.08), (107.0, 111.08), (107.0, 105.4), (112.0, 105.4), (112.0, 101.5), (124.09, 101.5),
           (124.09, 103.0)]
# the same connection well clear of the pad
CLEAR = [(104.0, 111.08), (110.0, 111.08), (117.08, 104.0), (118.08, 103.0), (124.09, 103.0)]

EDIT = r'''
import json, sys
import pcbnew
board = pcbnew.LoadBoard(sys.argv[1])
job = json.loads(sys.argv[3])
if job.get("pour"):
    zone = pcbnew.ZONE(board)
    zone.SetLayer(pcbnew.F_Cu)
    zone.SetNet(board.FindNet("GND"))
    zone.SetZoneName("GND_F")
    zone.SetLocalClearance(pcbnew.FromMM(0.3))
    zone.SetMinThickness(pcbnew.FromMM(0.25))
    outline = zone.Outline()
    outline.NewOutline()
    for x, y in [(100.5, 100.5), (149.5, 100.5), (149.5, 129.5), (100.5, 129.5)]:
        outline.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
    board.Add(zone)
if job.get("drop_unlocked"):
    for item in [t for t in board.GetTracks() if t.GetNetname() == job["drop_unlocked"] and not t.IsLocked()]:
        board.RemoveNative(item)
points = job.get("track") or []
for a, b in zip(points, points[1:]):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]), pcbnew.FromMM(a[1])))
    t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(b[0]), pcbnew.FromMM(b[1])))
    t.SetWidth(pcbnew.FromMM(0.2))
    t.SetLayer(pcbnew.F_Cu)
    t.SetNet(board.FindNet("/SIG_A"))
    board.Add(t)
board.Save(sys.argv[2])
print("TRACKS_JSON: " + json.dumps(sorted(
    [t.GetNetname(), t.GetClass(), bool(t.IsLocked())] for t in pcbnew.LoadBoard(sys.argv[2]).GetTracks())))
'''


def edit(source, target, **job):
    r = subprocess.run([str(common.PCBNEW_PYTHON), "-c", EDIT, str(source), str(target), json.dumps(job)],
                       capture_output=True, text=True, env=common.tool_env(), timeout=300)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    return route.tagged_json(r.stdout, "TRACKS_JSON")


def spoke_script(*args):
    r = subprocess.run([str(common.PCBNEW_PYTHON), str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                       env=common.tool_env(), timeout=600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    return route.tagged_json(r.stdout, "SPOKES_JSON")


@pytest.fixture(scope="module")
def pour_dir(tmp_path_factory):
    """The fixture project with a GND pour over F.Cu; its board is the board before routing."""
    d = tmp_path_factory.mktemp("thermal") / "route_demo"
    shutil.copytree(FIXTURE, d, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    edit(d / BOARD, d / BOARD, pour=True)
    return d


def stage(pour_dir, tmp_path, track):
    """A run's work directory at the point the repair is called: the work copy beside the working
    project, the board before routing, and the routed board with `track` as the run's new copper."""
    work = tmp_path / "work"
    project = work / "input" / "route_demo"
    shutil.copytree(pour_dir, project)
    before = work / "before-routing" / BOARD
    before.parent.mkdir(parents=True)
    shutil.copyfile(pour_dir / BOARD, before)
    output = work / "krt-out" / BOARD
    output.parent.mkdir(parents=True)
    edit(pour_dir / BOARD, output, track=track)
    return work, project / BOARD, before, output


def starved_of(work, board, output, label):
    report = thermals.stitch._graded(route, work, board.parent, output, label, folder="test-drc")
    assert report is not None
    return len(thermals.starved(report)), len(report["unconnected_items"])


def test_clear_takes_only_the_runs_copper_out_of_the_needed_corridors(pour_dir, tmp_path):
    work, board, before, output = stage(pour_dir, tmp_path, HUGGING)
    plan_path = tmp_path / "plan.json"
    spoke_script("plan", before, plan_path)
    plan = json.loads(plan_path.read_text())
    c1 = next(p for p in plan["pads"] if (p["ref"], p["pad"], p["layer"]) == ("C1", "2", "F.Cu"))
    # three live spokes; the two the pad needs are up and down, the track lies below and to the right
    assert c1["live_spokes"] == 3 and sorted(c["direction_deg"] for c in c1["corridors"] if c["selected"]) == [90, 270]
    only_c1 = thermals.spokes.select_pad_layers(plan, [f"{c1['uuid']}@F.Cu"])
    plan_path.write_text(json.dumps(only_c1))
    had = edit(before, tmp_path / "copy-before.kicad_pcb")
    assert len(edit(output, tmp_path / "copy-routed.kicad_pcb")) == len(had) + len(HUGGING) - 1
    got = spoke_script("clear", before, output, plan_path)
    # the piece below the pad goes; the piece to its right is in a corridor the pad does not need
    assert (got["removed_tracks"], got["removed_vias"], got["nets"]) == (1, 0, ["/SIG_A"]), got
    assert got["pads"] == {"C1.2 on F.Cu": ["/SIG_A"]} and got["new_items"] == len(HUGGING) - 1
    left = edit(output, tmp_path / "copy-cleared.kicad_pcb")
    assert len(left) == len(had) + len(HUGGING) - 2
    assert [t for t in left if t[2]] == [t for t in had if t[2]]      # the input's locked copper is all there
    # nothing of the run's in the corridors: a second clear finds nothing and leaves the file alone
    stamp = common.sha256(output)
    again = spoke_script("clear", before, output, plan_path)
    assert again["nets"] == [] and common.sha256(output) == stamp
    record("thermal_clear", got)


def test_a_starved_thermal_is_repaired(pour_dir, tmp_path):
    work, board, before, output = stage(pour_dir, tmp_path, HUGGING)
    assert starved_of(work, board, output, "routed") == (1, 4)       # C1.2; the other nets are not routed here
    calls = []

    def route_again(name, board_file, nets, seconds):
        """The engine's stand-in: what is left of the net goes, and a route clear of the pad is laid."""
        calls.append((name, nets, seconds, thermals.spokes.SCRIPT.name))
        text = Path(board_file).read_text()
        assert text.count("kicad-route:spoke:C1:2:F.Cu:") == 2       # the two corridors the pad needs are reserved
        out = work / f"krt-{name}" / BOARD
        out.parent.mkdir(parents=True)
        edit(board_file, out, drop_unlocked="/SIG_A", track=CLEAR)
        return {"status": "routed", "output": out, "command": None}

    rec = thermals.repair(work, board, before, output, route_again, route)
    record("thermal_repair", {k: v for k, v in rec.items() if k != "commands"})
    assert rec["status"] == "repaired" and (rec["starved_before"], rec["starved_after"]) == (1, 0), rec
    assert calls == [("thermal-1", ["/SIG_A"], thermals.PASS_S, "spokes.py")]
    only = rec["rounds"]
    assert len(only) == 1 and only[0]["result"] == "kept" and only[0]["nets"] == ["/SIG_A"]
    assert only[0]["removed_tracks"] == 1 and only[0]["reserved"] == 2 and only[0]["cut_by"] == {"C1.2 on F.Cu": ["/SIG_A"]}
    assert "kicad-route:spoke:" not in output.read_text()            # the corridors came off again
    assert starved_of(work, board, output, "repaired") == (0, 4)
    had = edit(before, tmp_path / "copy-before.kicad_pcb")
    now = edit(output, tmp_path / "copy-output.kicad_pcb")
    assert [t for t in now if t[2]] == [t for t in had if t[2]] and len(now) == len(had) + len(CLEAR) - 1


@pytest.mark.parametrize("outcome", ["left open", "not routed"])
def test_a_repair_that_gains_nothing_is_not_kept(pour_dir, tmp_path, outcome):
    work, board, before, output = stage(pour_dir, tmp_path, HUGGING)
    original = output.read_bytes()

    def route_again(name, board_file, nets, seconds):
        if outcome == "not routed":
            return {"status": "failed", "reason": "it wrote no board", "command": None}
        out = work / f"krt-{name}" / BOARD                           # the net stays broken: one more open connection
        out.parent.mkdir(parents=True)
        shutil.copyfile(board_file, out)
        return {"status": "routed", "output": out, "command": None}

    rec = thermals.repair(work, board, before, output, route_again, route)
    assert rec["status"] == "kept" and output.read_bytes() == original, rec
    assert (rec["starved_before"], rec["starved_after"]) == (1, 1)
    result = rec["rounds"][-1]["result"]
    if outcome == "not routed":
        assert "could not be routed again (it wrote no board)" in result
    else:
        assert rec["rounds"][-1]["drc_counts"]["after"][0] == rec["rounds"][-1]["drc_counts"]["before"][0] + 1
        assert "not the better for it" in result


def test_no_starved_thermal_no_repair(pour_dir, tmp_path):
    work, board, before, output = stage(pour_dir, tmp_path, CLEAR)
    original = output.read_bytes()

    def route_again(*args):
        raise AssertionError("nothing to route again")

    rec = thermals.repair(work, board, before, output, route_again, route)
    assert rec["status"] == "none" and rec["rounds"] == [] and rec["starved_before"] == 0
    assert output.read_bytes() == original


def test_a_starved_thermal_of_the_inputs_own_copper_is_left_alone(pour_dir, tmp_path):
    """The hugging track is in the board before routing: it is the input's, and the run's to keep."""
    work, board, before, output = stage(pour_dir, tmp_path, HUGGING)
    shutil.copyfile(output, before)
    original = output.read_bytes()

    def route_again(*args):
        raise AssertionError("the input's copper is not this run's to route again")

    rec = thermals.repair(work, board, before, output, route_again, route)
    assert rec["status"] == "none" and output.read_bytes() == original, rec
    # the pad is as short of spokes after routing as it was before: nothing of the run's making
    assert rec["starved_before"] == 0 and rec["rounds"] == []
    assert starved_of(work, board, output, "as-it-came")[0] == 1          # KiCad's finding stands


def test_a_labelled_repair_keeps_its_files_and_passes_apart(pour_dir, tmp_path):
    """The closing rounds call the repair on their own boards, each under its own label."""
    work, board, before, output = stage(pour_dir, tmp_path, HUGGING)
    names = []

    def route_again(name, board_file, nets, seconds):
        names.append(name)
        out = work / f"krt-{name}" / BOARD
        out.parent.mkdir(parents=True)
        edit(board_file, out, drop_unlocked="/SIG_A", track=CLEAR)
        return {"status": "routed", "output": out, "command": None}

    rec = thermals.repair(work, board, before, output, route_again, route, label="close-1")
    assert rec["status"] == "repaired" and names == ["thermal-close-1-1"]
    assert (work / "thermal-repair-close-1").is_dir() and not (work / "thermal-repair").exists()


def test_a_run_that_reserved_corridors_itself_is_left_to_its_caller(pour_dir, tmp_path):
    """Under --reserve-spokes the corridors the caller chose are still on the board at this point."""
    work, board, before, output = stage(pour_dir, tmp_path, HUGGING)
    plan_path = tmp_path / "plan.json"
    spoke_script("plan", before, plan_path)
    plan = json.loads(plan_path.read_text())
    j1 = next(p for p in plan["pads"] if (p["ref"], p["pad"], p["layer"]) == ("J1", "2", "F.Cu"))
    plan_path.write_text(json.dumps(thermals.spokes.select_pad_layers(plan, [f"{j1['uuid']}@F.Cu"])))
    spoke_script("reserve", output, plan_path)
    original = output.read_bytes()

    def route_again(*args):
        raise AssertionError("the caller's corridors are not ours to replace")

    rec = thermals.repair(work, board, before, output, route_again, route)
    assert rec["status"] == "none" and rec["starved_before"] == 1 and "reserved spoke corridors itself" in rec["note"]
    assert output.read_bytes() == original


def test_clear_restores_only_what_the_pad_is_short_of(pour_dir, tmp_path):
    """Tracks above and below C1.2 cross two of its three corridors: the one to its right is
    left, so one more is enough, and the one that a single track crosses goes before the one
    that two cross. The plan is rewritten to the corridors the pad then has."""
    work, board, before, output = stage(pour_dir, tmp_path, [(107.0, 105.4), (110.0, 105.4), (112.4, 105.4)])
    edit(output, output, track=[(109.5, 102.6), (112.4, 102.6)])
    assert starved_of(work, board, output, "routed")[0] == 1
    plan_path = tmp_path / "plan.json"
    spoke_script("plan", before, plan_path)
    plan = json.loads(plan_path.read_text())
    c1 = next(p for p in plan["pads"] if (p["ref"], p["pad"], p["layer"]) == ("C1", "2", "F.Cu"))
    plan_path.write_text(json.dumps(thermals.spokes.select_pad_layers(plan, [f"{c1['uuid']}@F.Cu"])))
    got = spoke_script("clear", before, output, plan_path)
    assert (got["removed_tracks"], got["removed_vias"], got["left_alone"]) == (1, 0, []), got
    after = json.loads(plan_path.read_text())["pads"][0]
    assert sorted(c["direction_deg"] for c in after["corridors"] if c["selected"]) == [0, 90]   # right, and up cleared
    assert len(edit(output, tmp_path / "copy.kicad_pcb")) == len(edit(before, tmp_path / "copy-before.kicad_pcb")) + 2
    assert starved_of(work, board, output, "cleared")[0] == 0


# /SIG_A round three sides of C1.2 (C1.1 is on the fourth): no spoke of the pad is left
ENCLOSING = [(104.0, 111.08), (107.0, 111.08), (107.0, 105.4), (112.0, 105.4), (112.0, 102.6), (108.0, 102.6),
             (108.0, 101.5), (124.09, 101.5), (124.09, 103.0)]


def test_a_pad_that_lost_its_pour_altogether_is_repaired(pour_dir, tmp_path):
    """KiCad reports a pad with one spoke of two, and nothing for a pad with none. The pour check
    names both, and the repair takes both."""
    work, board, before, output = stage(pour_dir, tmp_path, ENCLOSING)
    report, short = thermals.short_pads(route, work, board.parent, before, output, "routed", "test-pour")
    assert short == ["C1.2@F.Cu"], short
    drc_names_it = len(thermals.starved(report))

    def route_again(name, board_file, nets, seconds):
        out = work / f"krt-{name}" / BOARD
        out.parent.mkdir(parents=True)
        edit(board_file, out, drop_unlocked="/SIG_A", track=CLEAR)
        return {"status": "routed", "output": out, "command": None}

    rec = thermals.repair(work, board, before, output, route_again, route)
    record("thermal_repair_lost_contact", {"kicad_starved_thermal_findings": drc_names_it,
                                           **{k: v for k, v in rec.items() if k != "commands"}})
    assert rec["status"] == "repaired" and (rec["starved_before"], rec["starved_after"]) == (1, 0), rec
    assert thermals.short_pads(route, work, board.parent, before, output, "repaired", "test-pour")[1] == []
