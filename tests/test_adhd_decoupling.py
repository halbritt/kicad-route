"""KICADROUTE-54, first step: the read-only decoupling-cell audit (`kicad-route decoupling`).

The route_demo fixture has no IC, so these tests build a small synthetic 4-layer
board with pcbnew (In1.Cu GND plane, In2.Cu +3V3 plane, SOIC-8 ICs with pin 7 = +3V3
and pin 8 = GND, 0402 capacitors beside them) and route each cell in one style:

    U1/C1  pin -> track -> cap pad -> track -> via behind the cap      respects
    U2/C2  each pin and each cap pad has its own via, no pin-cap track  bypassed
    U3/C3  power: pin -> cap track with a T-stub via before the cap    bypassed
           ground: pin -> cap pad centre, via-in-pad in the cap pad    respects
    U4/C4  power pin unrouted; ground pin -> cap, cap has no via       incomplete
    C5     +3V3/GND cap 15+ mm from any IC                              unpaired
    C6     /SIG/GND cap                                                 not a decoupling cap
"""
import json
import shutil

import pytest

from helpers import OUT, cli, record

FP = "/usr/share/kicad/footprints"
SOIC = ("Package_SO", "SOIC-8_3.9x4.9mm_P1.27mm")
C0402 = ("Capacitor_SMD", "C_0402_1005Metric")
NAME = "decoupling_cells"


def build(path, extra_pin_via=False):
    """Write the synthetic board. extra_pin_via adds a via in U1.7's pad: what an
    autorouter does when it drops a power via straight onto the IC pin."""
    import pcbnew
    mm = pcbnew.FromMM

    def pt(x, y):
        return pcbnew.VECTOR2I(mm(x), mm(y))

    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    nets = {}
    for n in ("GND", "+3V3", "/SIG"):
        nets[n] = pcbnew.NETINFO_ITEM(board, n)
        board.Add(nets[n])

    def place(lib_name, ref, x, y, conn, angle=0):
        lib, name = lib_name
        fp = pcbnew.FootprintLoad(f"{FP}/{lib}.pretty", name)
        fp.SetFPID(pcbnew.LIB_ID(lib, name))
        fp.SetReference(ref)
        fp.SetValue("100n" if ref.startswith("C") else "IC")
        board.Add(fp)
        fp.SetPosition(pt(x, y))
        fp.SetOrientationDegrees(angle)
        for num, net in conn.items():
            fp.FindPadByNumber(num).SetNet(nets[net])
        return fp

    def track(a, b, net, layer=pcbnew.F_Cu):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(a)
        t.SetEnd(b)
        t.SetWidth(mm(0.25))
        t.SetLayer(layer)
        t.SetNet(nets[net])
        board.Add(t)

    def via(p, net):
        v = pcbnew.PCB_VIA(board)
        v.SetPosition(p)
        v.SetWidth(mm(0.6))
        v.SetDrill(mm(0.3))
        v.SetNet(nets[net])
        board.Add(v)

    def pad(fp, n):
        return fp.FindPadByNumber(n).GetPosition()

    ic_nets = {"7": "+3V3", "8": "GND"}
    cap_nets = {"1": "+3V3", "2": "GND"}  # rotated 90: pad 1 beside pin 7, pad 2 beside pin 8

    # U1/C1: respects. Tracks meet at the cap pad centre: the junction runs through the pad.
    x, y = 110, 110
    u1 = place(SOIC, "U1", x, y, ic_nets | {"1": "/SIG"})
    c1 = place(C0402, "C1", x + 4.5, y - 1.27, cap_nets, 90)
    track(pad(u1, "7"), pad(c1, "1"), "+3V3")
    track(pad(c1, "1"), pt(x + 5.6, y - 0.4), "+3V3")
    via(pt(x + 5.6, y - 0.4), "+3V3")
    track(pad(u1, "8"), pad(c1, "2"), "GND")
    track(pad(c1, "2"), pt(x + 5.6, y - 2.2), "GND")
    via(pt(x + 5.6, y - 2.2), "GND")
    if extra_pin_via:
        via(pad(u1, "7"), "+3V3")

    # U2/C2: bypassed on both sides: pins and cap pads each drop to the planes on their own.
    x, y = 125, 110
    u2 = place(SOIC, "U2", x, y, ic_nets)
    c2 = place(C0402, "C2", x + 6, y - 1.27, cap_nets, 90)
    for n, net, vy in (("7", "+3V3", y - 0.635), ("8", "GND", y - 1.905)):
        track(pad(u2, n), pt(x + 3.9, vy), net)
        via(pt(x + 3.9, vy), net)
    track(pad(c2, "1"), pt(x + 7.1, y - 0.4), "+3V3")
    via(pt(x + 7.1, y - 0.4), "+3V3")
    track(pad(c2, "2"), pt(x + 7.1, y - 2.2), "GND")
    via(pt(x + 7.1, y - 2.2), "GND")

    # U3/C3: power side bypassed by a T-stub via; ground side via-in-pad on the cap.
    x, y = 140, 110
    u3 = place(SOIC, "U3", x, y, ic_nets)
    c3 = place(C0402, "C3", x + 4.5, y - 1.27, cap_nets, 90)
    j = pt(x + 3.8, y - 0.7)
    track(pad(u3, "7"), j, "+3V3")
    track(j, pad(c3, "1"), "+3V3")
    track(j, pt(x + 4.2, y + 0.6), "+3V3")
    via(pt(x + 4.2, y + 0.6), "+3V3")
    track(pad(u3, "8"), pad(c3, "2"), "GND")
    via(pad(c3, "2"), "GND")

    # U4/C4: power pin unrouted; ground pin reaches the cap, the cap never reaches the plane.
    x, y = 110, 122
    u4 = place(SOIC, "U4", x, y, ic_nets)
    c4 = place(C0402, "C4", x + 4.5, y - 1.27, cap_nets, 90)
    track(pad(u4, "8"), pad(c4, "2"), "GND")

    place(C0402, "C5", 140, 126, cap_nets)  # too far from every IC
    place(C0402, "C6", 128, 104, {"1": "/SIG", "2": "GND"})  # signal-to-ground: not decoupling

    corners = [(100, 100), (150, 100), (150, 130), (100, 130)]
    for a, b in zip(corners, corners[1:] + corners[:1]):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pt(*a))
        s.SetEnd(pt(*b))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(mm(0.1))
        board.Add(s)
    for layer, net in ((pcbnew.In1_Cu, "GND"), (pcbnew.In2_Cu, "+3V3")):
        z = pcbnew.ZONE(board)
        z.SetLayer(layer)
        z.SetNet(nets[net])
        z.SetZoneName(f"{net}_plane")
        o = z.Outline()
        o.NewOutline()
        for px, py in [(100.5, 100.5), (149.5, 100.5), (149.5, 129.5), (100.5, 129.5)]:
            o.Append(mm(px), mm(py))
        board.Add(z)
    path.parent.mkdir(parents=True, exist_ok=True)
    pcbnew.SaveBoard(str(path), board)  # unfilled zones: the analyzer must refill them itself
    return path


@pytest.fixture(scope="module")
def board(tmp_path_factory):
    return build(tmp_path_factory.mktemp("decoupling") / f"{NAME}.kicad_pcb")


def audit(board, *extra):
    r = cli("decoupling", "--board", str(board), *extra, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    out = OUT / "decoupling" / board.stem
    return json.loads((out / "decoupling.json").read_text()), (out / "decoupling.md").read_text()


def test_classifies_both_routing_styles(board):
    from kicad_route.common import sha256
    before = sha256(board)
    rep, md = audit(board)
    assert sha256(board) == before, "the analyzer wrote the board"
    cells = {c["ic"]: c for c in rep["cells"]}
    record("adhd_decoupling_board", {"counts": rep["counts"], "unpaired": [u["ref"] for u in rep["unpaired"]],
                                     "cells": {k: [c["capacitor"]["ref"], c["power"]["topology"],
                                                   c["ground"]["topology"], c["topology"]]
                                               for k, c in cells.items()}})
    assert rep["power_nets"] == ["+3V3"] and rep["ground_nets"] == ["GND"]
    assert rep["zones_refilled"] is True
    assert sorted(cells) == ["U1", "U2", "U3", "U4"]
    assert {k: c["capacitor"]["ref"] for k, c in cells.items()} == {"U1": "C1", "U2": "C2", "U3": "C3", "U4": "C4"}
    for c in cells.values():
        assert (c["power_pin"]["pad"], c["ground_pin"]["pad"]) == (f"{c['ic']}.7", f"{c['ic']}.8")
        assert not c["ambiguous"]

    u1 = cells["U1"]  # pin -> cap -> via, with the track junction inside the cap pad
    assert (u1["power"]["topology"], u1["ground"]["topology"], u1["topology"]) == ("respects",) * 3
    assert [e["kind"] for e in u1["power"]["capacitor_entries"]] == ["via"]
    assert u1["power"]["capacitor_entries"][0]["zone_layers"] == ["In2.Cu"]
    assert u1["ground"]["capacitor_entries"][0]["zone_layers"] == ["In1.Cu"]

    u2 = cells["U2"]  # every pin has its own plane via
    assert (u2["power"]["topology"], u2["ground"]["topology"], u2["topology"]) == ("bypassed",) * 3
    assert u2["power"]["pin_reaches_capacitor"] is False
    assert u2["power"]["direct_entries"][0]["kind"] == "via"

    u3 = cells["U3"]  # T-stub via on the power side; via-in-pad on the cap's ground pad
    assert (u3["power"]["topology"], u3["ground"]["topology"], u3["topology"]) == ("bypassed", "respects", "bypassed")
    assert u3["power"]["pin_reaches_capacitor"] is True
    assert [e["kind"] for e in u3["power"]["direct_entries"]] == ["via"]

    u4 = cells["U4"]
    assert (u4["power"]["topology"], u4["ground"]["topology"], u4["topology"]) == \
        ("unrouted", "cap_not_on_plane", "incomplete")

    assert [u["ref"] for u in rep["unpaired"]] == ["C5"]
    assert rep["capacitors"] == 5  # C6 (/SIG to GND) is not a decoupling capacitor
    assert rep["counts"] == {"respects": 1, "bypassed": 2, "incomplete": 1}
    assert "| U1 | U1.7 +3V3 | U1.8 GND |" in md and "**bypassed**" in md and "Unpaired capacitors: C5" in md


def test_radius_is_configurable(board):
    """U2's capacitor sits ~3.5 mm from its pins: inside the default 5 mm, outside 3 mm."""
    rep, _ = audit(board, "--radius", "3")
    assert sorted(c["ic"] for c in rep["cells"]) == ["U1", "U3", "U4"]
    assert sorted(u["ref"] for u in rep["unpaired"]) == ["C2", "C5"]


def test_net_patterns_replace_the_name_rules(board):
    rep, _ = audit(board, "--power", "/SIG", "--ground", "GND")
    assert rep["power_nets"] == ["/SIG"] and rep["cells"] == []  # no IC has a /SIG pin near C6


def test_run_dir_reports_cells_the_candidate_changed(board, tmp_path):
    """A candidate whose router dropped a via onto U1.7: U1 goes respects -> bypassed."""
    run = tmp_path / "run"
    for role, extra in (("reference", False), ("candidate", True)):
        build(run / role / "p" / f"{NAME}.kicad_pcb", extra_pin_via=extra)
    (run / "attempt.json").write_text(json.dumps({"reference_dir": "reference/p", "candidate_dir": "candidate/p",
                                                  "board": f"{NAME}.kicad_pcb"}))
    r = cli("decoupling", str(run), timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    rep = json.loads((run / "decoupling.json").read_text())
    assert [c["cell"] for c in rep["changed"]] == ["U1:U1.7/U1.8:C1"]
    ch = rep["changed"][0]
    assert (ch["reference"], ch["candidate"]) == ("respects", "bypassed")
    assert ch["sides"] == {"power": ["respects", "bypassed"], "ground": ["respects", "respects"]}
    u1 = next(c for c in rep["candidate"]["cells"] if c["ic"] == "U1")
    assert u1["power"]["direct_entries"][0]["kind"] == "via"
    assert "U1:U1.7/U1.8:C1`: respects -> bypassed" in (run / "decoupling.md").read_text()


def test_fails_closed(tmp_path):
    r = cli("decoupling", "--board", str(tmp_path / "missing.kicad_pcb"), timeout=60)
    assert r.returncode == 1 and "no such board" in r.stdout
    bad = tmp_path / "bad.kicad_pcb"
    bad.write_text("(kicad_pcb (version 1) garbage")
    r = cli("decoupling", "--board", str(bad), timeout=120)
    assert r.returncode == 1 and "analysis failed" in r.stdout
    r = cli("decoupling", str(tmp_path), timeout=60)
    assert r.returncode == 1 and "not a routing run" in r.stdout
    shutil.rmtree(OUT / "decoupling" / "bad", ignore_errors=True)
