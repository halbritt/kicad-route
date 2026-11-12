"""Rule areas through the Specctra bridge.

KiCad's DSN export writes every rule area as a keep-out of some kind: one that
forbids vias only as (via_keepout ...), tracks only as (wire_keepout ...), and
any other as a full (keepout ...), which is also what it writes for an area
that forbids neither. The bridge leaves those out, so Freerouting may route
where the board lets it. Shared harness: helpers.py and conftest.py.
"""
import json
import re
import shutil
import subprocess

from helpers import FIXTURE, record
from kicad_route import common
from kicad_route import cli as route

ADD_AREAS = r'''
import json, sys
import pcbnew
board = pcbnew.LoadBoard(sys.argv[1])
box = board.GetBoardEdgesBoundingBox()
left, top = box.GetX() + box.GetWidth() // 4, box.GetY() + box.GetHeight() // 4
side = min(box.GetWidth(), box.GetHeight()) // 8
for index, (name, tracks, vias, pours, footprints) in enumerate(json.loads(sys.argv[2])):
    zone = pcbnew.ZONE(board)
    zone.SetIsRuleArea(True)
    zone.SetZoneName(name)
    zone.SetLayer(pcbnew.F_Cu)
    zone.SetDoNotAllowTracks(tracks)
    zone.SetDoNotAllowVias(vias)
    zone.SetDoNotAllowZoneFills(pours)
    zone.SetDoNotAllowFootprints(footprints)
    zone.SetDoNotAllowPads(False)
    x = left + index * (side + side // 4)
    outline = zone.Outline()
    outline.NewOutline()
    for dx, dy in ((0, 0), (side, 0), (side, side), (0, side)):
        outline.Append(x + dx, top + dy)
    board.Add(zone)
board.Save(sys.argv[1])
'''

# name, forbids tracks, vias, pours, footprints
AREAS = [("placement", False, False, False, False), ("no pour", False, False, True, False),
         ("no footprints", False, False, False, True), ("no tracks", True, False, False, False),
         ("no vias", False, True, False, False), ("no copper", True, True, True, False)]


def keepouts(dsn_text):
    return sorted(re.findall(r"\((keepout|via_keepout|wire_keepout) ", dsn_text))


def test_an_area_that_forbids_no_routing_is_no_keepout(tmp_path):
    proj = tmp_path / FIXTURE.name
    shutil.copytree(FIXTURE, proj, ignore=shutil.ignore_patterns("generate.py", "__pycache__"))
    board = proj / "route_demo.kicad_pcb"
    scripts = route.KICAD_SCRIPTS
    before = tmp_path / "before.dsn"
    r = subprocess.run([str(common.PCBNEW_PYTHON), str(scripts / "specctra.py"), "check", str(board), str(before)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    own = keepouts(before.read_text())  # what the fixture has of its own

    r = subprocess.run([str(common.PCBNEW_PYTHON), "-c", ADD_AREAS, str(board), json.dumps(AREAS)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]

    raw = tmp_path / "raw.dsn"  # `check` leaves KiCad's own export as it is
    r = subprocess.run([str(common.PCBNEW_PYTHON), str(scripts / "specctra.py"), "check", str(board), str(raw)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    kicad = keepouts(raw.read_text())
    # KiCad: the three areas that forbid no routing are full keepouts, like the one that forbids both
    assert kicad == sorted(own + ["keepout"] * 4 + ["via_keepout", "wire_keepout"]), kicad

    bridged = tmp_path / "bridged.dsn"
    r = subprocess.run([str(common.PCBNEW_PYTHON), str(scripts / "specctra.py"), "export", str(board), str(bridged)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    info = route.tagged_json(r.stdout, "BRIDGE_JSON")
    assert sorted(info["rule_areas_not_for_routing_left_out"]) == ["no footprints", "no pour", "placement"]
    assert keepouts(bridged.read_text()) == sorted(own + ["keepout", "via_keepout", "wire_keepout"])
    # the board on disk is the caller's: the areas are left out of the export only
    r = subprocess.run([str(common.PCBNEW_PYTHON), "-c",
                        "import sys, pcbnew; b = pcbnew.LoadBoard(sys.argv[1]); "
                        "print(sum(1 for z in b.Zones() if z.GetIsRuleArea()))", str(board)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and int(r.stdout.strip().splitlines()[-1]) >= len(AREAS), r.stdout + r.stderr[-500:]
    record("specctra_rule_areas", {"kicad": kicad, "bridged": keepouts(bridged.read_text()), "left_out": info})
