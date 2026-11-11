"""Areas closed to new copper on every run (kicad/closed.py, closed.py), on generated boards.

Solder-mask apertures that are not pad openings: a bar on F.Mask and a ring on B.Mask
lie across the straight line between two pads, over a pour on each side. A track
under either is a `solder_mask_bridge` for KiCad (the aperture then shows the pour
and the track). The plan must close both, leave a window open that a pad can only be
reached through, keep a pad beside a window reachable, and ignore a footprint that
allows mask bridges; reserve and strip must round-trip the board; and a routed run
must connect the pads without a new finding and without a leftover rule area.

Copper without a net: a land-pattern tab drawn over its own pad, a pad buried in such
copper, a marking, text. The plan must close each shape to every net, open only the
berth of a pad the shape lies over, and never a via; a routed run must reach the pad
under the tab without running along the tab or dropping a via on it.
"""
import importlib.util
import json
import shutil
import subprocess

import pcbnew
import pytest

from helpers import FIXTURE, ROOT, run, tree_hash
from kicad_route import cli as route
from kicad_route import common, spokes

SCRIPT = ROOT / "src" / "kicad_route" / "kicad" / "closed.py"
BOARD = "windows.kicad_pcb"
MM = 1_000_000
_spec = importlib.util.spec_from_file_location("kicad_closed_script", SCRIPT)
planner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(planner)


def script(*args):
    r = subprocess.run([str(common.PCBNEW_PYTHON), str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                       env=common.tool_env(), timeout=600)
    tag = next((json.loads(l.split(":", 1)[1]) for l in r.stdout.splitlines() if l.startswith("CLOSED_JSON:")), None)
    return r, tag


def plan_of(board, out):
    before = common.sha256(board)
    r, tag = script("plan", board, out)
    assert r.returncode == 0 and tag, r.stdout + r.stderr
    assert common.sha256(board) == before, "plan must never modify the board"
    return json.loads(out.read_text())


def xy(x, y):
    return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))


class Sketch:
    """An empty copy of the fixture board (its outline, layers and rules) to draw on."""

    def __init__(self):
        self.board = pcbnew.LoadBoard(str(FIXTURE / "route_demo.kicad_pcb"))
        for item in [*self.board.GetTracks(), *self.board.GetFootprints(), *self.board.Zones()]:
            self.board.Delete(item)
        for net in list(self.board.GetNetsByName().values()):
            if net.GetNetCode():
                self.board.Delete(net)
        self.nets = {}

    def net(self, name):
        if name not in self.nets:
            self.nets[name] = pcbnew.NETINFO_ITEM(self.board, name)
            self.board.Add(self.nets[name])
        return self.nets[name]

    def footprint(self, ref, x, y):
        fp = pcbnew.FOOTPRINT(self.board)
        fp.SetReference(ref)
        fp.SetPosition(xy(x, y))
        self.board.Add(fp)
        return fp

    def pad(self, fp, number, x, y, net, size=(1, 1), drill=None):
        pad = pcbnew.PAD(fp)
        pad.SetNumber(number)
        pad.SetShape(pcbnew.PAD_SHAPE_RECT)
        pad.SetSize(xy(*size))
        if drill:
            pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
            pad.SetDrillSize(xy(drill, drill))
            pad.SetLayerSet(pad.PTHMask())
        else:
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            layers = pcbnew.LSET()
            layers.AddLayer(pcbnew.F_Cu)
            pad.SetLayerSet(layers)
        pad.SetPosition(xy(x, y))
        if net:
            pad.SetNet(self.net(net))
        fp.Add(pad)
        return pad

    def shape(self, kind, layer, start, end, width=0.1, filled=True, owner=None):
        parent = owner or self.board
        item = pcbnew.PCB_SHAPE(parent)
        item.SetShape(kind)
        if kind == pcbnew.SHAPE_T_CIRCLE:
            item.SetCenter(xy(*start))
        else:
            item.SetStart(xy(*start))
        item.SetEnd(xy(*end))
        item.SetLayer(layer)
        item.SetWidth(pcbnew.FromMM(width))
        item.SetFilled(filled)
        parent.Add(item)
        return item

    def text(self, layer, x, y, content):
        """Text 1 mm high, centred on (x, y)."""
        item = pcbnew.PCB_TEXT(self.board)
        item.SetText(content)
        item.SetPosition(xy(x, y))
        item.SetTextSize(xy(1, 1))
        item.SetTextThickness(pcbnew.FromMM(.15))
        item.SetLayer(layer)
        self.board.Add(item)
        return item

    def pour(self, net, layer):
        zone = pcbnew.ZONE(self.board)
        zone.SetLayer(layer)
        zone.SetNet(self.net(net))
        outline = zone.Outline()
        outline.NewOutline()
        for x, y in [(100.5, 100.5), (149.5, 100.5), (149.5, 129.5), (100.5, 129.5)]:
            outline.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
        self.board.Add(zone)
        return zone

    def save(self, directory):
        directory.mkdir(parents=True)
        self.board.Save(str(directory / BOARD))
        shutil.copyfile(FIXTURE / "route_demo.kicad_pro", (directory / BOARD).with_suffix(".kicad_pro"))
        return directory


BAR = (124, 104, 126, 120)          # filled, on F.Mask
RING = (125, 112, 2.0, 0.2)         # centre, radius and stroke, on B.Mask


def crossing_board():
    """/SIG pads at (110,112) and (140,112); between them a bar on F.Mask and a ring on
    B.Mask; a /POUR zone on both layers, anchored by a through-hole pad."""
    sketch = Sketch()
    for ref, x in (("J1", 110), ("J2", 140)):
        sketch.pad(sketch.footprint(ref, x, 112), "1", x, 112, "/SIG")
    sketch.pad(sketch.footprint("J3", 105, 125), "1", 105, 125, "/POUR", size=(1.6, 1.6), drill=0.8)
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
        sketch.pour("/POUR", layer)
    sketch.shape(pcbnew.SHAPE_T_RECT, pcbnew.F_Mask, BAR[:2], BAR[2:])
    cx, cy, radius, stroke = RING
    sketch.shape(pcbnew.SHAPE_T_CIRCLE, pcbnew.B_Mask, (cx, cy), (cx + radius, cy), width=stroke, filled=False)
    return sketch


def plan_board():
    """One of every case the plan tells apart; nothing here needs routing."""
    sketch = Sketch()
    sketch.shape(pcbnew.SHAPE_T_RECT, pcbnew.F_Mask, (130, 105), (134, 107))                    # plain window
    sketch.shape(pcbnew.SHAPE_T_CIRCLE, pcbnew.B_Mask, (140, 110), (141, 110), width=.15, filled=False)  # ring
    sketch.text(pcbnew.F_Mask, 125, 120, "CC")                                                  # text
    tab = sketch.footprint("U1", 110, 110)                 # a pad that lies wholly under its own mask shape
    sketch.pad(tab, "1", 110, 110, "/A")
    sketch.shape(pcbnew.SHAPE_T_RECT, pcbnew.F_Mask, (109, 109), (111, 111), owner=tab)
    sketch.pad(sketch.footprint("R1", 120, 110), "1", 120, 110, "/B")   # a pad 0.1 mm beside a window
    sketch.shape(pcbnew.SHAPE_T_RECT, pcbnew.F_Mask, (120.65, 109), (123, 111), width=.1)
    jumper = sketch.footprint("JP1", 115, 120)             # KiCad does not check this footprint's apertures
    jumper.SetAllowSolderMaskBridges(True)
    sketch.pad(jumper, "1", 115, 120, "/A")
    sketch.shape(pcbnew.SHAPE_T_RECT, pcbnew.F_Mask, (114, 119), (116, 121), owner=jumper)
    copper = sketch.shape(pcbnew.SHAPE_T_RECT, pcbnew.F_Cu, (145, 105), (146, 106))   # copper with a mask opening
    copper.SetHasSolderMask(True)
    return sketch


@pytest.fixture(scope="module")
def plan_dir(tmp_path_factory):
    return plan_board().save(tmp_path_factory.mktemp("closed-plan") / "project")


def bbox_mm(points):
    xs, ys = [p[0] / MM for p in points], [p[1] / MM for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def test_geometry_helpers():
    square = [(0, 0), (4, 0), (4, 4), (0, 4)]
    assert set(planner.convex_hull(square + [(2, 2), (1, 3), (4, 0)])) == set(square)
    assert planner.convex_hull([(0, 0), (1, 1), (1, 1)]) == [(0, 0), (1, 1)]          # nothing to enclose
    assert planner.mask_grow(0, 0, 0, 0, vias_tented=True) == 0
    assert planner.mask_grow(50, 20, 100, 50, vias_tented=True) == 70                 # expansion + mask-to-copper
    assert planner.mask_grow(50, 20, 100, 50, vias_tented=False) == 250               # + web + 2 * expansion
    assert planner.mask_grow(50, 300, 100, 50, vias_tented=False) == 350              # the larger of the two
    assert planner.mask_grow(-80, 20, 0, 0, vias_tented=True) == 0                    # never shrinks an aperture
    assert planner.boxes_touch((0, 0, 2, 2), (2, 2, 3, 3)) and not planner.boxes_touch((0, 0, 2, 2), (3, 0, 4, 2))


def test_plan_closes_the_windows_that_cut_nothing_off(plan_dir, tmp_path):
    plan = plan_of(plan_dir / BOARD, tmp_path / "plan.json")
    mask = plan["mask"]
    assert (mask["aperture_count"], mask["ignored"]) == (5, 1), mask
    assert {(a["owner"], a["class"], a["mask_layer"], a["outlined_as"]) for a in mask["apertures"]} == {
        (None, "PCB_SHAPE", "F.Mask", "shape"), (None, "PCB_SHAPE", "B.Mask", "shape"),
        (None, "PCB_TEXT", "F.Mask", "hull"), ("U1", "PCB_SHAPE", "F.Mask", "shape")}
    assert all(a["grow"] == 0 and a["window"] is not None for a in mask["apertures"])
    assert (mask["window_count"], mask["closed"], mask["left_open"]) == (5, 4, 1), mask["windows"]
    # the tab: its pad can only be reached through the window, so the window stays open
    tab = next(w for w in mask["windows"] if w["enclosed"])
    assert tab["enclosed"] == [{"kind": "pad", "ref": "U1", "pad": "1", "net": "/A"}] and not tab["closed"]
    assert bbox_mm([tab["bbox"][:2], tab["bbox"][2:]]) == pytest.approx((108.95, 108.95, 111.05, 111.05), abs=.01)
    areas = {"F.Cu": [], "B.Cu": []}
    for a in plan["areas"]:
        if a["source"] == "mask":       # the copper with a mask opening is closed as copper, not as an aperture
            assert a["no_tracks"] and a["no_vias"]
            areas[a["layer"]].append(a)
    assert len(areas["F.Cu"]) == 3 and len(areas["B.Cu"]) == 1
    assert [a["source"] for a in plan["areas"]].count("copper") == plan["copper"]["shape_count"] == 1
    boxes = sorted(bbox_mm(a["outline"]) for a in areas["F.Cu"])
    # beside R1.1 (right edge at 120.5): the window starts at 120.6 and loses the pad's berth,
    # the pad grown by its class's clearance + track width (0.2 + 0.2)
    assert boxes[0] == pytest.approx((120.6, 108.95, 123.05, 111.05), abs=.01)
    beside = next(a for a in areas["F.Cu"] if bbox_mm(a["outline"])[0] < 121)
    facing = [x / MM for x, y in beside["outline"] if abs(y / MM - 110) < .6]       # the edge that faces the pad
    assert facing and min(facing) == pytest.approx(120.9, abs=.01)
    kept = next(w for w in mask["windows"] if w["kept_reachable"])
    assert kept["kept_reachable"] == [{"kind": "pad", "ref": "R1", "pad": "1", "net": "/B"}] and kept["closed"]
    assert boxes[2] == pytest.approx((129.95, 104.95, 134.05, 107.05), abs=.01)       # the plain window, stroke included
    text = boxes[1]
    assert 120 < text[0] < 125 < text[2] < 130 and 118 < text[1] < 120 < text[3] < 122, text
    ring = areas["B.Cu"][0]                                                             # a ring keeps its hole
    assert bbox_mm(ring["outline"]) == pytest.approx((138.925, 108.925, 141.075, 111.075), abs=.01)
    assert len(ring["holes"]) == 1
    assert bbox_mm(ring["holes"][0]) == pytest.approx((139.075, 109.075, 140.925, 110.925), abs=.01)


def test_untented_vias_and_board_values_grow_the_windows(plan_dir, tmp_path):
    project = tmp_path / "project"
    shutil.copytree(plan_dir, project)
    board = pcbnew.LoadBoard(str(project / BOARD))
    settings = board.GetDesignSettings()
    settings.m_TentViasFront = False
    settings.m_SolderMaskMinWidth = pcbnew.FromMM(.1)
    settings.m_SolderMaskExpansion = pcbnew.FromMM(.05)
    board.Save(str(project / BOARD))
    pro = project / "windows.kicad_pro"
    data = json.loads(pro.read_text())
    data["board"]["design_settings"]["rules"]["solder_mask_to_copper_clearance"] = .02
    pro.write_text(json.dumps(data))
    mask = plan_of(project / BOARD, tmp_path / "plan.json")["mask"]
    assert (mask["mask_to_copper_clearance"], mask["web_width"], mask["mask_expansion"]) == (20_000, 100_000, 50_000)
    assert mask["vias_tented"] == {"F.Mask": False, "B.Mask": True}
    grow = {(a["class"], a["mask_layer"]): (a["expansion"], a["grow"]) for a in mask["apertures"] if not a["owner"]}
    assert grow == {("PCB_SHAPE", "F.Mask"): (50_000, 250_000),    # expansion + web + 2 * board expansion
                    ("PCB_SHAPE", "B.Mask"): (50_000, 70_000),     # tented side: expansion + mask-to-copper
                    ("PCB_TEXT", "F.Mask"): (0, 200_000)}          # KiCad adds no expansion to text


def test_reserve_strip_round_trip(plan_dir, tmp_path):
    plan = plan_of(plan_dir / BOARD, tmp_path / "plan.json")
    project = tmp_path / "project"
    shutil.copytree(plan_dir, project)
    board = project / BOARD
    facts, rec = route.board_facts(board, tmp_path / "before.json")
    assert facts is not None, rec
    r, tag = script("reserve", board, tmp_path / "plan.json")
    assert r.returncode == 0 and tag == {"reserved": 5}, r.stdout + r.stderr
    reserved, rec = route.board_facts(board, tmp_path / "reserved.json")
    ours = [a for a in reserved["rule_areas"] if a["name"].startswith("kicad-route:closed:")]
    assert sorted(a["name"].rsplit(":", 1)[0] for a in ours) == ["kicad-route:closed:copper:F.Cu"] + \
        ["kicad-route:closed:mask:B.Cu"] + ["kicad-route:closed:mask:F.Cu"] * 3
    assert len(reserved["rule_areas"]) == len(facts["rule_areas"]) + 5
    for a in ours:
        assert len(a["layers"]) == 1 and a["no_tracks"] and a["no_vias"]
        assert not (a["no_pads"] or a["no_footprints"] or a["no_zone_fills"])
    ring = next(a for a in ours if a["layers"] == ["B.Cu"])
    assert len(ring["polygons"]) == 1 and len(ring["polygons"][0]["holes"]) == 1
    planned = next(a for a in plan["areas"] if a["layer"] == "B.Cu")
    assert {tuple(p) for p in planned["holes"][0]} == {tuple(p) for p in ring["polygons"][0]["holes"][0]}
    again, _ = script("reserve", board, tmp_path / "plan.json")
    assert again.returncode != 0 and "strip them first" in again.stdout + again.stderr
    r, tag = script("strip", board)
    assert r.returncode == 0 and tag == {"stripped": 5, "remaining": 0}, r.stdout + r.stderr
    stripped, rec = route.board_facts(board, tmp_path / "stripped.json")
    for key in ("rule_areas", "zones", "footprints", "copper_graphics", "tracks", "vias", "outline", "setup"):
        assert stripped[key] == facts[key], key
    untouched = board.read_bytes()
    r, tag = script("strip", board)
    assert tag == {"stripped": 0, "remaining": 0} and board.read_bytes() == untouched   # nothing to strip: no save


def solder_mask_bridges(report):
    return [v for v in report["violations"] if v["type"] == "solder_mask_bridge"]


def test_new_copper_stays_out_of_mask_windows(tmp_path):
    project = crossing_board().save(tmp_path / "project")
    before = tree_hash(project)

    # What a backend that does not see the windows does: the straight track is a bridge.
    naive = tmp_path / "naive"
    shutil.copytree(project, naive)
    board = pcbnew.LoadBoard(str(naive / BOARD))
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(xy(110, 112))
    track.SetEnd(xy(140, 112))
    track.SetWidth(pcbnew.FromMM(.2))
    track.SetLayer(pcbnew.F_Cu)
    track.SetNet(board.FindNet("/SIG"))
    board.Add(track)
    board.Save(str(naive / BOARD))
    report, info = route.kicad_drc(naive / BOARD, tmp_path / "naive-drc", "naive", refill_and_save=True)
    assert report is not None, info
    assert solder_mask_bridges(report), "the generated board no longer shows the defect"
    assert not report["unconnected_items"]

    process, run_dir, result = run("run", "--backend", "builtin", "--dir", str(project),
                                   "--budget", "60", "--label", "mask-windows")
    assert tree_hash(project) == before
    assert result["process"]["status"] == "PASS", (result["process"], process.stderr[-2000:])
    closed = result["process"]["closed_areas"]
    assert closed["mask"] == {"aperture_count": 2, "window_count": 2, "closed": 2, "left_open": 0, "ignored": 0}
    assert closed["areas"] == closed["reserved"] == closed["stripped"] == 2 and not closed["reasons"]
    assert result["routing"]["status"] == "COMPLETE", result["routing"]
    assert result["validation"]["status"] == "PASS", result["validation"]
    assert result["validation"]["drc"]["new_violations"]["count"] == 0, result["validation"]["drc"]
    assert result["validation"]["preservation"]["keepouts"] == "PASS"
    assert process.returncode == 0, (process.stdout[-2000:], process.stderr[-2000:])
    drc = json.loads((run_dir / "validate" / "candidate-drc.json").read_text())
    assert not solder_mask_bridges(drc)
    facts = json.loads((run_dir / "validate" / "candidate-facts.json").read_text())
    assert not [a for a in facts["rule_areas"] if a["name"].startswith("kicad-route:closed:")]
    # The work input carried the areas; no new copper of the candidate lies in one.
    plan = json.loads((run_dir / "work" / "closed-plan.json").read_text())
    work = json.loads((run_dir / "facts" / "reference-facts.json").read_text())
    layers = [layer["name"] for layer in work["copper_layers"]]
    tracks = [t for t in facts["tracks"] if t["net"] == "/SIG"]
    assert tracks
    for area in plan["areas"]:
        corridor = {"polygon": area["outline"]}
        hits = [item for item in tracks + facts["vias"] if spokes.copper_hits(corridor, area["layer"], layers, item)]
        assert not hits, (area["layer"], hits)
    assert any(t["start"][1] != 112 * MM or t["end"][1] != 112 * MM for t in tracks) or facts["vias"]


# ------------------------------------------------------------- copper without a net

TAB = (111.3, 111.5, 118, 112.5)    # drawn with a 0.1 mm stroke: copper from 111.25 to 118.05, 111.45 to 112.55
TAB_PAD = (112, 112, 1.5, .9)       # centre and size: its left edge is the tab's left edge


def tab_part(sketch, ref, x, y, net, tab_to):
    """A part whose land pattern draws its tab as copper without a net, over its pad:
    the pad is 1.5 x 0.9 at (x, y) and the tab runs from the pad's left edge to `tab_to`."""
    part = sketch.footprint(ref, x, y)
    sketch.pad(part, "2", x, y, net, size=(1.5, .9))
    sketch.shape(pcbnew.SHAPE_T_RECT, pcbnew.F_Cu, (x - .7, y - .5), (tab_to, y + .5), owner=part)
    return part


def copper_plan_board():
    """One of every case the plan tells apart on copper without a net."""
    sketch = Sketch()
    sketch.shape(pcbnew.SHAPE_T_RECT, pcbnew.F_Cu, (130, 115), (133, 117))            # a marking
    sketch.pad(sketch.footprint("R1", 135, 116), "1", 135, 116, "/B")                 # beside it, 1.45 mm away
    part = tab_part(sketch, "U1", 110, 110, "/TAB", 114)                              # a tab over its own pad
    part.Value().SetLayer(pcbnew.F_Cu)                                                 # a hidden field is not copper
    part.Value().SetVisible(False)
    buried = sketch.footprint("U2", 120, 120)                                         # a pad buried in its copper
    sketch.pad(buried, "1", 120, 120, "/DEEP")
    sketch.shape(pcbnew.SHAPE_T_RECT, pcbnew.F_Cu, (118, 118), (122, 122), owner=buried)
    sketch.shape(pcbnew.SHAPE_T_RECT, pcbnew.F_Cu, (140, 105), (141, 106)).SetNet(sketch.net("/TAB"))  # has a net
    sketch.text(pcbnew.B_Cu, 125, 125, "REV")
    return sketch


@pytest.fixture(scope="module")
def copper_dir(tmp_path_factory):
    return copper_plan_board().save(tmp_path_factory.mktemp("closed-copper") / "project")


def test_plan_closes_copper_without_a_net(copper_dir, tmp_path):
    plan = plan_of(copper_dir / BOARD, tmp_path / "plan.json")
    copper = plan["copper"]
    shapes = {(s["owner"], s["class"], s["layer"]): s for s in copper["shapes"]}
    assert set(shapes) == {(None, "PCB_SHAPE", "F.Cu"), ("U1", "PCB_SHAPE", "F.Cu"), ("U2", "PCB_SHAPE", "F.Cu"),
                           (None, "PCB_TEXT", "B.Cu")}, copper["shapes"]
    assert (copper["shape_count"], copper["closed"], copper["berths_open"], copper["left_open"]) == (4, 2, 1, 1)
    assert plan["mask"]["aperture_count"] == 0
    areas = [a for a in plan["areas"] if a["source"] == "copper"]
    assert len(areas) == plan["area_count"] == 5

    def area(x, y, layer="F.Cu"):
        found = [a for a in areas if a["layer"] == layer and
                 bbox_mm(a["outline"])[0] <= x <= bbox_mm(a["outline"])[2] and
                 bbox_mm(a["outline"])[1] <= y <= bbox_mm(a["outline"])[3]]
        return sorted(found, key=lambda a: a["no_tracks"])

    # a marking nothing touches: closed whole, at its outline (the pad beside it gets no berth)
    marking = shapes[(None, "PCB_SHAPE", "F.Cu")]
    assert marking["tracks"] == "closed" and marking["touching"] == []
    (closed,) = area(131.5, 116)
    assert closed["no_tracks"] and closed["no_vias"]
    assert bbox_mm(closed["outline"]) == pytest.approx((129.95, 114.95, 133.05, 117.05), abs=.01)
    # the tab: tracks may reach the pad in its berth (0.2 + 0.2 around the pad), vias nowhere on the tab
    tab = shapes[("U1", "PCB_SHAPE", "F.Cu")]
    assert tab["tracks"] == "berths open"
    assert tab["touching"] == [{"kind": "pad", "ref": "U1", "pad": "2", "net": "/TAB"}]
    (berth,) = area(110, 110)
    assert not berth["no_tracks"] and berth["no_vias"]
    assert bbox_mm(berth["outline"]) == pytest.approx((109.25, 109.45, 111.15, 110.55), abs=.01)
    (rest,) = area(113, 110)
    assert rest["no_tracks"] and rest["no_vias"]
    # the berth's corner is round: at the tab's edge, 0.1 beyond the pad's, it reaches 0.387 past the pad
    assert bbox_mm(rest["outline"]) == pytest.approx((111.137, 109.45, 114.05, 110.55), abs=.01)
    # the buried pad: no way to it except across its copper, so tracks stay open; vias do not
    buried = shapes[("U2", "PCB_SHAPE", "F.Cu")]
    assert buried["tracks"] == "open" and buried["touching"][0]["ref"] == "U2"
    (whole,) = area(120, 120)
    assert not whole["no_tracks"] and whole["no_vias"]
    assert bbox_mm(whole["outline"]) == pytest.approx((117.95, 117.95, 122.05, 122.05), abs=.01)
    # text: the hull of its outline, on its own layer
    (text,) = area(125, 125, "B.Cu")
    assert text["no_tracks"] and text["no_vias"] and shapes[(None, "PCB_TEXT", "B.Cu")]["outlined_as"] == "hull"


def tab_board():
    """U1's pad 2 (/TAB) lies under the tab its land pattern draws, between the long pads
    1 and 3 of the same part. The rest of /TAB is at J1, straight beyond the far end of
    the tab: the short way there runs along the tab. The clean way leaves the pad to the
    left, the one side where it reaches the edge of the tab."""
    sketch = Sketch()
    part = tab_part(sketch, "U1", TAB_PAD[0], TAB_PAD[1], "/TAB", TAB[2])
    sketch.pad(part, "1", 112.4, 110.5, "/UP", size=(2.4, .8))      # 0.55 mm from the tab: no track fits between
    sketch.pad(part, "3", 112.4, 113.5, "/DOWN", size=(2.4, .8))
    sketch.pad(sketch.footprint("J1", 125, 112), "1", 125, 112, "/TAB")
    sketch.pad(sketch.footprint("J2", 112.4, 105), "1", 112.4, 105, "/UP")
    sketch.pad(sketch.footprint("J3", 112.4, 119), "1", 112.4, 119, "/DOWN")
    return sketch


def test_new_copper_touches_a_tab_only_to_reach_the_pad_under_it(tmp_path):
    project = tab_board().save(tmp_path / "project")
    process, run_dir, result = run("run", "--backend", "builtin", "--dir", str(project),
                                   "--budget", "60", "--label", "netless-tab")
    assert result["process"]["status"] == "PASS", (result["process"], process.stderr[-2000:])
    closed = result["process"]["closed_areas"]
    assert closed["copper"] == {"shape_count": 1, "closed": 0, "berths_open": 1, "left_open": 0}
    assert closed["areas"] == closed["reserved"] == closed["stripped"] == 2 and not closed["reasons"]
    assert result["routing"]["status"] == "COMPLETE", result["routing"]
    assert result["validation"]["preservation"]["keepouts"] == "PASS"
    assert result["validation"]["preservation"]["copper_graphics"] == "PASS"

    # The unrouted board already has the pad against the tab. A track that reaches the pad
    # touches the tab too: that is all the new findings there may be.
    new = json.loads((run_dir / "validate" / "new-findings.json").read_text())["violations"]
    for finding in new:
        items = sorted(item["description"] for item in finding["items"])
        assert finding["type"] in ("clearance", "shorting_items"), finding
        assert len(items) == 2 and "[<no net>] of U1 on F.Cu" in items[0] and \
            items[1].startswith("Track [/TAB]"), finding
    assert len(new) <= 2, new

    # New copper on the tab stays in the pad's berth; none of it is a via.
    board = pcbnew.LoadBoard(str(run_dir / "candidate" / "project" / BOARD))
    part = board.FindFootprintByReference("U1")
    tab, berth = pcbnew.SHAPE_POLY_SET(), pcbnew.SHAPE_POLY_SET()
    next(iter(part.GraphicalItems())).TransformShapeToPolygon(tab, pcbnew.F_Cu, 0, 5000, pcbnew.ERROR_OUTSIDE)
    part.FindPadByNumber("2").TransformShapeToPolygon(berth, pcbnew.F_Cu, pcbnew.FromMM(.4) + 5000, 5000,
                                                      pcbnew.ERROR_OUTSIDE)
    far = pcbnew.SHAPE_POLY_SET(tab)
    far.BooleanSubtract(berth)
    tracks = [t for t in board.GetTracks() if t.IsOnLayer(pcbnew.F_Cu)]
    assert {t.GetNetname() for t in board.GetTracks()} == {"/TAB", "/UP", "/DOWN"}
    for track in tracks:
        copper = pcbnew.SHAPE_POLY_SET()
        track.TransformShapeToPolygon(copper, pcbnew.F_Cu, 0, 5000, pcbnew.ERROR_OUTSIDE)
        on_tab = pcbnew.SHAPE_POLY_SET(copper)
        on_tab.BooleanIntersection(tab)
        kind = "via" if isinstance(track, pcbnew.PCB_VIA) else "track"
        where = (track.GetNetname(), kind, pcbnew.ToMM(track.GetStart().x), pcbnew.ToMM(track.GetStart().y))
        if kind == "via" or track.GetNetname() != "/TAB":
            assert not (on_tab.OutlineCount() and on_tab.Area() > 0), where
        copper.BooleanIntersection(far)
        assert not (copper.OutlineCount() and copper.Area() > 0), where
