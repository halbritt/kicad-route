"""A pour piece that routing ringed off gets a via to its net (stitch.py, kicad/stitch.py).

Found on a designer's two-layer board with ground poured on both sides: two tracks ringed a
20 mm^2 piece of the back pour that took two of a capacitor pin's three spokes. KiCad counts
no spoke into a piece that only one pad ties to its net, so the pin was one spoke short:
"Thermal relief connection to zone incomplete (2 spokes connected to isolated island)" made a
complete routing unclean. It came and went with the order of the nets: clean in the engine's
own order, there with any of four other settings. Round a surface-mount pad the same ring
cuts the pad from its net altogether.
"""
import json
import re
import shutil
import subprocess

import pytest

from helpers import FIXTURE
from kicad_route import cli as route
from kicad_route import stitch

BOARD = "route_demo.kicad_pcb"

# Board surgery runs in KiCad's interpreter, as every pcbnew step of the wrapper does.
# argv: BOARD back(0|1) cut(none|pad|pocket|pin) [AREA]. The fixture pours ground on the back
# only; every board built here gets the same pour on the front too, and back=0 takes the one on
# the back away. An AREA of "solid" is no area: both pours then join their pads without thermal
# relief. cut draws new tracks:
#   pad     a closed loop on the front round the ground pad of C1 (an 0805: the loop's left
#           side runs between its two pads) and 21 mm^2 of the pour beside it. The pad has no
#           other copper: an open connection
#   pocket  on the back, from the first and the third pin of the header J2 to the edge of the
#           pour. The second pin is ground and has four spokes: two now run into the pocket
#           between the tracks, two into the rest of the pour
#   pin     a closed loop on the back round all of J2. All four spokes of its ground pin run
#           into the piece inside, and the pin reaches the front pour: KiCad has no finding
#   island  a small pour of a signal net on the back that keeps its islands, with nothing of
#           the net on it, and a track from a pad of that net that ends over it on the front:
#           the board as a router leaves a pour or a plane it did not need. island-before is
#           the same pour without the track
#   under   the same kind of pour under the signal's pad of R1 and nothing else of the net:
#           a plane under pads that the routing joined some other way
# AREA names a rule area against vias (and nothing else) over the whole inside of the pad loop.
BUILD = """
import sys
import pcbnew
mm = pcbnew.FromMM
path, back, cut = sys.argv[1], sys.argv[2] == "1", sys.argv[3]
area = sys.argv[4] if len(sys.argv) > 4 else None
board = pcbnew.LoadBoard(path)
ground = next(z for z in board.Zones() if z.GetNetname() == "GND" and not z.GetIsRuleArea())
zone = pcbnew.ZONE(board)
zone.SetLayer(pcbnew.F_Cu)
zone.SetNetCode(ground.GetNetCode())
zone.SetPadConnection(ground.GetPadConnection())
zone.SetThermalReliefGap(ground.GetThermalReliefGap())
zone.SetThermalReliefSpokeWidth(ground.GetThermalReliefSpokeWidth())
zone.SetMinThickness(ground.GetMinThickness())
zone.SetLocalClearance(ground.GetLocalClearance())
outline = zone.Outline()
outline.NewOutline()
box = ground.GetBoundingBox()
for x, y in ((box.GetLeft(), box.GetTop()), (box.GetRight(), box.GetTop()),
             (box.GetRight(), box.GetBottom()), (box.GetLeft(), box.GetBottom())):
    outline.Append(x, y)
board.Add(zone)
if area == "solid":
    area = None
    for pour in (zone, ground):
        pour.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
if not back:
    board.Remove(ground)
pin = {p.GetNumber(): p for fp in board.GetFootprints() if fp.GetReference() == "J2" for p in fp.Pads()}
signal = board.GetNetcodeFromNetname("/SIG_A")
loop = lambda l, r, t, b, layer: [((l, t), (r, t), layer, signal), ((r, t), (r, b), layer, signal),
                                  ((r, b), (l, b), layer, signal), ((l, b), (l, t), layer, signal)]
if cut.startswith("island"):
    isl = pcbnew.ZONE(board)
    isl.SetLayer(pcbnew.B_Cu)
    isl.SetNetCode(signal)
    isl.SetAssignedPriority(1)
    isl.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_NEVER)
    isl.SetLocalClearance(ground.GetLocalClearance())
    isl.SetMinThickness(ground.GetMinThickness())
    shape = isl.Outline()
    shape.NewOutline()
    for x, y in ((134.0, 110.0), (140.0, 110.0), (140.0, 116.0), (134.0, 116.0)):
        shape.Append(mm(x), mm(y))
    board.Add(isl)
if cut == "under":
    isl = pcbnew.ZONE(board)
    isl.SetLayer(pcbnew.B_Cu)
    isl.SetNetCode(signal)
    isl.SetAssignedPriority(1)
    isl.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_NEVER)
    isl.SetLocalClearance(ground.GetLocalClearance())
    isl.SetMinThickness(ground.GetMinThickness())
    shape = isl.Outline()
    shape.NewOutline()
    for x, y in ((120.5, 101.0), (124.6, 101.0), (124.6, 104.6), (120.5, 104.6)):
        shape.Append(mm(x), mm(y))
    board.Add(isl)
tracks = {"none": [], "island-before": [], "under": [],
          # from the signal's pad on R1 round the part and down over the pour
          "island": [((124.0875, 103.0), (124.0875, 101.2), pcbnew.F_Cu, signal), ((124.0875, 101.2), (137.0, 101.2), pcbnew.F_Cu, signal),
                     ((137.0, 101.2), (137.0, 113.0), pcbnew.F_Cu, signal)],
          "pad": loop(110.0, 116.0, 101.5, 106.5, pcbnew.F_Cu),
          "pin": loop(143.0, 148.5, 103.5, 116.12, pcbnew.B_Cu),
          "pocket": [((146.0, 106.0), (149.9, 106.0), pcbnew.B_Cu, pin["1"].GetNetCode()),
                     ((146.0, 111.08), (149.9, 111.08), pcbnew.B_Cu, pin["3"].GetNetCode())]}[cut]
for a, b, layer, net in tracks:
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1])))
    t.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
    t.SetLayer(layer)
    t.SetWidth(mm(0.15))
    t.SetNetCode(net)
    board.Add(t)
if area:
    keep = pcbnew.ZONE(board)
    keep.SetIsRuleArea(True)
    keep.SetZoneName(area)
    keep.SetLayer(pcbnew.F_Cu)
    keep.SetDoNotAllowVias(True)
    keep.SetDoNotAllowTracks(False)
    keep.SetDoNotAllowZoneFills(False)
    keep.SetDoNotAllowPads(False)
    keep.SetDoNotAllowFootprints(False)
    shape = keep.Outline()
    shape.NewOutline()
    for x, y in ((110.0, 101.5), (116.0, 101.5), (116.0, 106.5), (110.0, 106.5)):
        shape.Append(mm(x), mm(y))
    board.Add(keep)
pcbnew.SaveBoard(path, board, aSkipSettings=True)
"""

# where the copper that each cut takes off lies, in board millimetres: left, right, top, bottom
INSIDE = {"pad": (110.0, 116.0, 101.5, 106.5), "pin": (143.0, 148.5, 103.5, 116.12), "pocket": (146.0, 149.5, 106.0, 111.08),
          "island": (134.0, 140.0, 110.0, 116.0), "under": (120.5, 124.6, 101.0, 104.6)}


def build(directory, back, cut, area=None, spokes=2):
    """A copy of the fixture project with the board rebuilt; `spokes` is the board's minimum spoke
    count (KiCad counts spokes only for a board that asks for a number; the fixture does not)."""
    shutil.copytree(FIXTURE, directory, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    project = directory / "route_demo.kicad_pro"
    setup = json.loads(project.read_text())
    setup["board"]["design_settings"]["rules"]["min_resolved_spokes"] = spokes
    project.write_text(json.dumps(setup, indent=2))
    done = subprocess.run([route.pcbnew_python(), "-c", BUILD, str(directory / BOARD), str(int(back)), cut, *([area] if area else [])],
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-2000:]
    return directory / BOARD


def place(before, routed, nets, tmp_path):
    listed = tmp_path / "nets.json"
    listed.write_text(json.dumps(nets))
    done = subprocess.run([route.pcbnew_python(), str(route.KICAD_SCRIPTS / "stitch.py"), "place", str(before), str(routed), str(listed)],
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-2000:]
    return route.tagged_json(done.stdout, "STITCH_JSON")


def vias(path):
    return re.findall(r'\(via\b', path.read_text())


def uuids(path):
    return set(re.findall(r'\(uuid "([0-9a-f-]+)"\)', path.read_text()))


def inside(via, cut):
    left, right, top, bottom = INSIDE[cut]
    reach = via["diameter_mm"] / 2
    return left + reach < via["x_mm"] < right - reach and top + reach < via["y_mm"] < bottom - reach


@pytest.fixture()
def unrouted(tmp_path):
    """Ground poured on both sides and no new track: the board as it was before the routing."""
    return build(tmp_path / "before", True, "none")


def test_a_pad_cut_from_its_net_gets_one_via_inside_the_loop(unrouted, tmp_path):
    routed = build(tmp_path / "routed", True, "pad")
    had = len(vias(routed))
    found = place(unrouted, routed, ["GND", "/SIG_A"], tmp_path)
    assert found["pieces_cut_off"] == 1 and found["left"] == [], found
    (via,) = found["vias"]
    assert (via["net"], via["layer"], via["pads"], via["case"]) == ("GND", "F.Cu", ["C1.2"], "open")
    assert inside(via, "pad"), via
    assert via["diameter_mm"] > via["hole_mm"] > 0
    assert len(vias(routed)) == had + 1


def test_a_pad_joined_without_thermal_relief_is_cut_from_its_net_the_same_way(tmp_path):
    before, routed = build(tmp_path / "before", True, "none", "solid"), build(tmp_path / "routed", True, "pad", "solid")
    found = place(before, routed, ["GND"], tmp_path)
    assert found["pieces_cut_off"] == 1 and found["left"] == [], found
    (via,) = found["vias"]
    assert (via["layer"], via["pads"], via["case"]) == ("F.Cu", ["C1.2"], "open") and inside(via, "pad"), via


def test_a_pin_left_short_of_spokes_gets_one_via_in_the_piece_that_took_them(tmp_path):
    # this board asks for three spokes; the pocket takes two of the pin's four
    before, routed = build(tmp_path / "before", True, "none", spokes=3), build(tmp_path / "routed", True, "pocket", spokes=3)
    found = place(before, routed, ["GND"], tmp_path)
    assert found["pieces_cut_off"] == 1 and found["left"] == [], found
    (via,) = found["vias"]
    assert (via["net"], via["layer"], via["pads"], via["case"]) == ("GND", "B.Cu", ["J2.2"], "spokes")
    assert (via["spokes"], via["live_spokes"]) == (2, 2)
    assert inside(via, "pocket"), via


def test_no_via_where_kicad_has_no_finding(unrouted, tmp_path):
    # two spokes into live copper are enough for a board that asks for two
    enough = build(tmp_path / "enough", True, "pocket")
    # all four spokes in the piece inside the loop, and the pin reaches the pour on the front
    elsewhere = build(tmp_path / "elsewhere", True, "pin")
    for routed in (enough, elsewhere):
        data = routed.read_bytes()
        assert place(unrouted, routed, ["GND"], tmp_path) == {"pieces_cut_off": 0, "vias": [], "left": []}
        assert routed.read_bytes() == data                   # nothing placed: not rewritten


def test_a_pour_with_nothing_of_its_net_on_it_gets_a_via_onto_a_track_of_the_net(tmp_path):
    before, routed = build(tmp_path / "before", True, "island-before"), build(tmp_path / "routed", True, "island")
    data = routed.read_bytes()
    assert place(before, routed, ["GND"], tmp_path) == {"pieces_cut_off": 0, "vias": [], "left": []}    # not its net
    assert routed.read_bytes() == data
    found = place(before, routed, ["/SIG_A"], tmp_path)
    assert found["pieces_cut_off"] == 1 and found["left"] == [], found
    (via,) = found["vias"]
    assert (via["net"], via["layer"], via["pads"], via["case"], via["onto"]) == ("/SIG_A", "B.Cu", [], "isolated", "track")
    assert inside(via, "island") and via["x_mm"] == pytest.approx(137.0, abs=0.001), via      # on the track's centre line
    # without the track there is nothing of the net to join the pour to
    alone = place(before, before, ["/SIG_A"], tmp_path)
    assert alone["vias"] == [] and [row["why"] for row in alone["left"]] == [{"no copper of the net on another layer": alone["left"][0]["why"]["no copper of the net on another layer"]}]


def test_a_pour_under_a_pad_of_its_net_gets_a_via_beside_the_pad_and_a_track_to_it(tmp_path):
    board = build(tmp_path / "board", True, "under")
    had_tracks, had_vias = board.read_text().count("(segment"), len(vias(board))
    found = place(board, board, ["/SIG_A"], tmp_path)
    assert found["pieces_cut_off"] == 1 and found["left"] == [], found
    (via,) = found["vias"]
    assert (via["net"], via["layer"], via["case"], via["onto"]) == ("/SIG_A", "B.Cu", "isolated", "pad"), via
    left, right, top, bottom = INSIDE["under"]
    assert left < via["x_mm"] < right and top < via["y_mm"] < bottom, via      # its centre in the pour; its rim may reach past
    leg = via["leg"]
    assert (leg["pad"], leg["layer"], leg["from_mm"]) == ("R1.1", "F.Cu", [124.0875, 103.0]) and leg["width_mm"] > 0
    length = ((via["x_mm"] - 124.0875) ** 2 + (via["y_mm"] - 103.0) ** 2) ** 0.5
    assert via["diameter_mm"] / 2 < length <= 2.5
    assert len(vias(board)) == had_vias + 1 and board.read_text().count("(segment") == had_tracks + 1


def test_no_via_for_a_net_outside_the_scope_or_a_pad_the_input_had_cut_off_already(unrouted, tmp_path):
    routed = build(tmp_path / "routed", True, "pad")
    data = routed.read_bytes()
    assert place(unrouted, routed, ["/SIG_A", "/VIN"], tmp_path) == {"pieces_cut_off": 0, "vias": [], "left": []}
    # the same loop in the input: the pad was cut from its net before the run
    assert place(routed, routed, ["GND"], tmp_path)["pieces_cut_off"] == 0
    assert routed.read_bytes() == data


def test_a_rule_area_against_vias_is_kept_but_not_the_runs_own_spoke_corridor(unrouted, tmp_path):
    forbidden = build(tmp_path / "forbidden", True, "pad", "no vias here")
    data = forbidden.read_bytes()
    found = place(unrouted, forbidden, ["GND"], tmp_path)
    assert found["pieces_cut_off"] == 1 and found["vias"] == [], found
    (left,) = found["left"]
    assert left["pads"] == ["C1.2"] and list(left["why"]) == ["rule area"] and left["why"]["rule area"] > 10
    assert forbidden.read_bytes() == data
    # --reserve-spokes leaves its corridors on the board until after this step
    reserved = build(tmp_path / "reserved", True, "pad", "kicad-route:spoke:C1:2:F.Cu:0")
    found = place(unrouted, reserved, ["GND"], tmp_path)
    assert len(found["vias"]) == 1 and found["left"] == [] and inside(found["vias"][0], "pad"), found


def test_a_net_with_copper_on_one_layer_only_has_nowhere_to_stitch_to(tmp_path):
    before, routed = build(tmp_path / "before", False, "none"), build(tmp_path / "routed", False, "pad")
    data = routed.read_bytes()
    found = place(before, routed, ["GND"], tmp_path)
    assert found["pieces_cut_off"] == 1 and found["vias"] == []
    assert [(row["net"], row["layer"], row["pads"]) for row in found["left"]] == [("GND", "F.Cu", ["C1.2"])]
    assert list(found["left"][0]["why"]) == ["no copper of the net on another layer"]
    assert routed.read_bytes() == data


def staged(before, routed, tmp_path):
    """The run's work directory at the point where the wrapper stitches."""
    work = tmp_path / "work"
    shutil.copytree(before.parent, work / "input" / "route_demo")
    output = work / "krt-out" / BOARD
    output.parent.mkdir(parents=True)
    shutil.copyfile(routed, output)
    return work, work / "input" / "route_demo" / BOARD, output


def reports(work):
    return {label: json.loads((work / "stitch-drc" / label / "drc" / f"{label}-drc.json").read_text())
            for label in ("before", "after-0")}


def test_the_wrapper_keeps_the_via_that_gives_a_pad_its_net_back(unrouted, tmp_path):
    work, board, output = staged(unrouted, build(tmp_path / "routed", True, "pad"), tmp_path)
    had = len(vias(output))
    rec = stitch.apply(work, board, unrouted, output, ["GND", "/SIG_A"], route)
    assert rec["status"] == "stitched" and len(rec["vias"]) == 1 and rec["removed_after_drc"] == [], rec
    assert len(vias(output)) == had + 1
    # KiCad: the pour round the pad was no longer joined to the rest of the net; now it is
    open_ground = {label: [u for u in rep["unconnected_items"] if any("[GND]" in i["description"] for i in u["items"])]
                   for label, rep in reports(work).items()}
    assert len(open_ground["before"]) == 1 and open_ground["after-0"] == []
    kinds = {label: sorted(v["type"] for v in rep["violations"]) for label, rep in reports(work).items()}
    assert kinds["before"] == kinds["after-0"], kinds


def test_the_wrapper_keeps_the_via_that_makes_a_pins_spokes_count(tmp_path):
    before, routed = build(tmp_path / "before", True, "none", spokes=3), build(tmp_path / "routed", True, "pocket", spokes=3)
    work, board, output = staged(before, routed, tmp_path)
    rec = stitch.apply(work, board, before, output, ["GND"], route)
    assert rec["status"] == "stitched" and len(rec["vias"]) == 1 and rec["removed_after_drc"] == [], rec
    # KiCad counted two of the pin's spokes as leading nowhere before, and counts all four after
    starved = {label: [v for v in rep["violations"] if v["type"] == "starved_thermal"] for label, rep in reports(work).items()}
    assert len(starved["before"]) == 1 and "2 spokes connected to isolated island" in starved["before"][0]["description"]
    assert starved["after-0"] == []


def test_the_wrapper_keeps_the_via_that_ends_kicads_isolated_copper(tmp_path):
    before, routed = build(tmp_path / "before", True, "island-before"), build(tmp_path / "routed", True, "island")
    work, board, output = staged(before, routed, tmp_path)
    rec = stitch.apply(work, board, before, output, ["/SIG_A", "GND"], route)
    assert rec["status"] == "stitched" and [v["case"] for v in rec["vias"]] == ["isolated"] and rec["removed_after_drc"] == [], rec
    # KiCad calls a fill isolated while nothing on it leads to a pad: the track does, once the via joins them
    lone = {label: [v for v in rep["violations"] if v["type"] == "isolated_copper"] for label, rep in reports(work).items()}
    assert len(lone["before"]) == 1 and lone["after-0"] == []


def test_the_wrapper_keeps_a_via_and_its_track_together_or_takes_both_out(tmp_path, monkeypatch):
    start = build(tmp_path / "board", True, "under")
    work, board, output = staged(start, start, tmp_path)
    rec = stitch.apply(work, board, start, output, ["/SIG_A"], route)
    assert rec["status"] == "stitched" and rec["vias"][0]["leg"]["pad"] == "R1.1", rec
    lone = {label: [v for v in rep["violations"] if v["type"] == "isolated_copper"] for label, rep in reports(work).items()}
    assert len(lone["before"]) == 1 and lone["after-0"] == []
    leg_uuid = rec["vias"][0]["leg"]["uuid"]
    assert leg_uuid in uuids(output) and rec["vias"][0]["uuid"] in uuids(output)
    # a finding that names the track takes the via out with it
    again = tmp_path / "again"
    again.mkdir()
    work, board, output = staged(start, start, again)
    data, known = output.read_bytes(), uuids(output)

    def the_track_is_named(base, report, kind):
        named = [u for u in sorted(uuids(board) - known) if u in legs_seen]
        return ([{"type": "clearance", "items": [{"uuid": u} for u in named]}] if named else []), []
    legs_seen = set()
    helper = stitch._helper

    def spy(api, work_dir, log, *args, **kwargs):
        run, found = helper(api, work_dir, log, *args, **kwargs)
        if args and args[0] == "place" and found:
            legs_seen.update(v["leg"]["uuid"] for v in found["vias"] if v.get("leg"))
        return run, found
    monkeypatch.setattr(stitch, "_helper", spy)
    monkeypatch.setattr(route, "drc_delta", the_track_is_named)
    rec = stitch.apply(work, board, start, output, ["/SIG_A"], route)
    assert rec["status"] == "none" and len(rec["removed_after_drc"]) == 1 and len(legs_seen) == 1, rec
    assert output.read_bytes() == data and not (uuids(board) - known)       # via and track both gone again


def test_the_wrapper_takes_a_via_out_again_when_a_new_finding_names_it(unrouted, tmp_path, monkeypatch):
    work, board, output = staged(unrouted, build(tmp_path / "routed", True, "pad"), tmp_path)
    data, known = output.read_bytes(), uuids(output)

    def every_new_via_is_named(base, report, kind):
        fresh = uuids(board) - known
        return ([{"type": "clearance", "items": [{"uuid": uuid} for uuid in sorted(fresh)]}] if fresh else []), []
    monkeypatch.setattr(route, "drc_delta", every_new_via_is_named)
    rec = stitch.apply(work, board, unrouted, output, ["GND"], route)
    assert rec["status"] == "none" and rec["vias"] == [] and len(rec["removed_after_drc"]) == 1, rec
    assert rec["removed_after_drc"][0]["findings"] == ["clearance"]
    assert output.read_bytes() == data                       # the board is as the router left it


def test_the_wrapper_takes_the_vias_out_again_when_kicads_report_is_no_shorter_for_them(unrouted, tmp_path, monkeypatch):
    work, board, output = staged(unrouted, build(tmp_path / "routed", True, "pad"), tmp_path)
    data = output.read_bytes()
    graded = route.kicad_drc

    def one_more_open_connection(copy, out_dir, label, refill):
        report, info = graded(copy, out_dir, label, refill)
        if label.startswith("after"):                        # as if the via had cut another net's pour
            report["unconnected_items"] = report["unconnected_items"] + [{"description": "Missing connection between items", "items": []}]
        return report, info
    monkeypatch.setattr(route, "kicad_drc", one_more_open_connection)
    rec = stitch.apply(work, board, unrouted, output, ["GND"], route)
    assert rec["status"] == "none" and rec["vias"] == [] and len(rec["removed_no_gain"]) == 1, rec
    assert rec["drc_counts"]["after"][0] == rec["drc_counts"]["before"][0]
    assert output.read_bytes() == data


def test_a_via_in_the_runs_own_spoke_corridor_is_graded_without_the_corridor(unrouted, tmp_path):
    # --reserve-spokes: the corridor forbids vias and is still on the board when the wrapper stitches
    routed = build(tmp_path / "routed", True, "pad", "kicad-route:spoke:C1:2:F.Cu:0")
    work, board, output = staged(unrouted, routed, tmp_path)
    rec = stitch.apply(work, board, unrouted, output, ["GND"], route)
    assert rec["status"] == "stitched" and len(rec["vias"]) == 1 and rec["removed_after_drc"] == [], rec
    assert b"kicad-route:spoke:" in output.read_bytes()      # the run strips it later, as before
    assert b"kicad-route:spoke:" not in (work / "stitch-drc" / "after-0" / BOARD).read_bytes()
