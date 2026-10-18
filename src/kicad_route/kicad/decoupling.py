#!/usr/bin/env python3
"""Decoupling cells: which bypass capacitors serve which IC pins, and does the copper
run pin -> capacitor -> plane?

    decoupling.py BOARD.kicad_pcb OUT.json [OPTIONS_JSON]

Exit 0 OUT.json written; 3 KiCad could not load the board (`DECOUPLING_LOAD_ERROR:`
line); anything else, the analysis failed.

Read-only: the board is loaded with KiCad's loader, its zones are refilled IN MEMORY
(so the planes are what DRC would grade) and nothing is ever saved.

OPTIONS_JSON (all optional): {"radius_mm": 5, "ic": ["U*", "IC*"], "cap": ["C*"],
"power": [fnmatch patterns], "ground": [fnmatch patterns]}. `power` / `ground` replace
the built-in name rules below when given.

Nets. A net is ground-like when its name (last path component, case-insensitive)
matches GROUND_RE. A net is a power net when it is not ground-like and either its
name matches POWER_RE or one of its pads has the schematic pin type power_in or
power_out.

Cells. A capacitor is a footprint whose reference matches `cap` with exactly two
pads that carry a net, one on a power net and one on a ground-like net. An IC is a
footprint whose reference matches `ic`. For each capacitor, each IC that has a pad
on the capacitor's power net within `radius_mm` of the capacitor's power pad AND a
pad on its ground net within `radius_mm` of the capacitor's ground pad is a
candidate (nearest such pins per IC; pad centre to pad centre). The candidate with
the smallest summed distance becomes the cell; the others are listed under
`alternatives` (a capacitor with alternatives is `ambiguous`). Capacitors with no
candidate are listed under `unpaired`.

Topology. Per net, a graph of the net's copper is built from KiCad's own
connectivity (CONNECTIVITY_DATA.GetConnectedTracks / GetConnectedPads: directly
touching items). Two tracks/vias that meet inside a pad both touch, and this script
routes that junction THROUGH the pad (the direct track-track edge is dropped when
both items touch a common pad). Every same-net filled zone (refilled; rule areas
and teardrops excluded) is one PLANE node; an item touches it when the zone's fill
on one of the item's copper layers collides with the item's effective shape there.
For each side (power, ground) of a cell, with pin = the IC pad and cap = the
capacitor pad:

    bypassed           the pin reaches PLANE without passing through cap (a via or
                       pour contact of its own); `direct_entries` names those items
    respects           the pin reaches cap without the plane, does not reach the
                       plane except through cap, and cap reaches PLANE without the pin
    cap_not_on_plane   the pin reaches cap (and not the plane), but cap has no
                       plane entry except through the pin (or the net has no plane)
    incomplete         the pin has copper, but it reaches neither cap nor PLANE
    unrouted           the pin has no track, via or zone contact at all

The cell's topology is `respects` when both sides respect, `bypassed` when either
side is bypassed, else `incomplete`. Prints `DECOUPLING_JSON: {summary}`. Lengths
in the JSON are millimetres.
"""
import fnmatch
import json
import math
import re
import sys
from pathlib import Path

GROUND_RE = re.compile(r"^([ADPSC]|SIG)?_?(GND|VSS)\w*$|^(0V|EARTH|CHASSIS|PE)$", re.I)
POWER_RE = re.compile(r"^[+-]|^\d+V\d*\w*$|^V(CC|DD|EE|BAT|BUS|SYS|REF|IO|CORE|PP)\w*$|^VIN(_?\d+V?\d*)?$"
                      r"|^(AVCC|AVDD|DVCC|DVDD|PVDD|PVCC|PWR)\w*$", re.I)
POWER_PIN_TYPES = {"power_in", "power_out"}
NM = 1_000_000
PLANE = "PLANE"


def leaf(net):
    return net.rsplit("/", 1)[-1]


def match_any(name, patterns):
    return any(fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(leaf(name), p) for p in patterns)


def mm(v):
    return round(v / NM, 4)


def pos_mm(p):
    return [mm(p.x), mm(p.y)]


def pad_name(pad):
    return f"{pad.GetParentFootprint().GetReference()}.{pad.GetNumber()}"


def classify_nets(board, opts):
    pads_by_net = {}
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetCode() > 0:
                pads_by_net.setdefault(pad.GetNetname(), []).append(pad)
    ground, power = set(), set()
    for name, pads in pads_by_net.items():
        if opts.get("ground"):
            is_gnd = match_any(name, opts["ground"])
        else:
            is_gnd = bool(GROUND_RE.match(leaf(name)))
        if is_gnd:
            ground.add(name)
            continue
        if opts.get("power"):
            is_pwr = match_any(name, opts["power"])
        else:
            is_pwr = bool(POWER_RE.match(leaf(name))) or any(p.GetPinType() in POWER_PIN_TYPES for p in pads)
        if is_pwr:
            power.add(name)
    return power, ground


def dist(a, b):
    return math.hypot(a.x - b.x, a.y - b.y)


class NetGraph:
    """Copper of one net: pads, tracks, arcs, vias as nodes; PLANE for its zones."""

    def __init__(self, pcbnew, board, conn, netcode):
        self.items = {}
        self.adj = {}
        self.kind = {}
        tracks = [t for t in board.GetTracks() if t.GetNetCode() == netcode]
        pads = [p for fp in board.GetFootprints() for p in fp.Pads() if p.GetNetCode() == netcode]
        zones = [z for z in board.Zones() if z.GetNetCode() == netcode and not z.GetIsRuleArea()
                 and not z.IsTeardropArea()]
        self.has_plane = any(z.HasFilledPolysForLayer(L) and z.GetFilledPolysList(L).OutlineCount()
                             for z in zones for L in z.GetLayerSet().CuStack())
        for it in tracks + pads:
            key = it.m_Uuid.AsString()
            self.items[key] = it
            self.adj.setdefault(key, set())
            self.kind[key] = "pad" if it.GetClass() == "PAD" else (
                "via" if it.GetClass() == "PCB_VIA" else ("arc" if it.GetClass() == "PCB_ARC" else "track"))
        self.adj[PLANE] = set()
        self.zone_contact = {}
        for key, it in self.items.items():
            if self.kind[key] == "pad":
                continue
            touching_pads = {p.m_Uuid.AsString() for p in conn.GetConnectedPads(it)} & self.items.keys()
            for p in touching_pads:
                self._edge(key, p)
            for other in conn.GetConnectedTracks(it):
                ok = other.m_Uuid.AsString()
                if ok == key or ok not in self.items:
                    continue
                other_pads = {p.m_Uuid.AsString() for p in conn.GetConnectedPads(other)}
                if touching_pads & other_pads:
                    continue  # they meet inside a pad: the junction goes through that pad
                self._edge(key, ok)
        for key, it in self.items.items():
            layers = []
            for L in it.GetLayerSet().CuStack():
                for z in zones:
                    if (z.IsOnLayer(L) and z.HasFilledPolysForLayer(L)
                            and z.GetFilledPolysList(L).Collide(it.GetEffectiveShape(L))):
                        layers.append(board.GetLayerName(L))
                        break
            if layers:
                self.zone_contact[key] = layers
                self._edge(key, PLANE)

    def _edge(self, a, b):
        self.adj.setdefault(a, set()).add(b)
        self.adj.setdefault(b, set()).add(a)

    def reach(self, start, blocked=()):
        """Nodes reachable from start without entering any node in blocked (start itself excluded from blocked)."""
        seen, todo = {start}, [start]
        blocked = set(blocked) - {start}
        while todo:
            n = todo.pop()
            for m in self.adj.get(n, ()):
                if m not in seen and m not in blocked:
                    seen.add(m)
                    if m != PLANE:  # the plane joins everything; never walk across it
                        todo.append(m)
        return seen

    def describe(self, key):
        it = self.items[key]
        k = self.kind[key]
        rec = {"kind": k, "uuid": key}
        if k == "via":
            rec.update(position_mm=pos_mm(it.GetPosition()),
                       layers=[it.GetBoard().GetLayerName(L) for L in it.GetLayerSet().CuStack()])
        elif k == "pad":
            rec.update(pad=pad_name(it), position_mm=pos_mm(it.GetPosition()))
        else:
            rec.update(layer=it.GetLayerName(), start_mm=pos_mm(it.GetStart()), end_mm=pos_mm(it.GetEnd()))
        rec["zone_layers"] = self.zone_contact.get(key, [])
        return rec


def side(graph, pin, cap):
    """Topology of one IC pin -> capacitor pad connection (see the module docstring)."""
    p, c = pin.m_Uuid.AsString(), cap.m_Uuid.AsString()
    rec = {"pin": pad_name(pin), "capacitor_pad": pad_name(cap), "net": pin.GetNetname(),
           "distance_mm": mm(dist(pin.GetPosition(), cap.GetPosition())), "net_has_plane": graph.has_plane}
    if not graph.adj.get(p):
        rec["topology"] = "unrouted"
        return rec
    from_pin = graph.reach(p, blocked={c})  # everything the pin reaches without passing the capacitor pad
    to_cap = c in graph.reach(p, blocked={PLANE})
    if PLANE in from_pin:
        direct = sorted((k for k in from_pin if k in graph.zone_contact),
                        key=lambda k: dist(graph.items[k].GetPosition(), pin.GetPosition()))
        rec.update(topology="bypassed", pin_reaches_capacitor=to_cap,
                   direct_entries=[graph.describe(k) for k in direct])
        return rec
    if not to_cap:
        rec.update(topology="incomplete")
        return rec
    from_cap = graph.reach(c, blocked={p})
    entries = sorted((k for k in from_cap if k in graph.zone_contact and k != p),
                     key=lambda k: dist(graph.items[k].GetPosition(), cap.GetPosition()))
    if PLANE in from_cap:
        rec.update(topology="respects", capacitor_entries=[graph.describe(k) for k in entries])
    else:
        rec.update(topology="cap_not_on_plane")
    return rec


LOAD_ERROR = "DECOUPLING_LOAD_ERROR:"  # the board did not load: the host refuses (input), exit 3


class LoadError(Exception):
    pass


def load(pcbnew, board_path):
    try:
        board = pcbnew.LoadBoard(str(board_path))
    except Exception as e:  # older pcbnew raises IO_ERROR, KiCad 10 returns None
        raise LoadError(f"{board_path}: KiCad could not load the board ({e})") from e
    if board is None:
        raise LoadError(f"{board_path}: KiCad could not load the board")
    return board


def analyze(pcbnew, board_path, opts):
    board = load(pcbnew, board_path)
    zones = [z for z in board.Zones() if not z.GetIsRuleArea()]
    filled = pcbnew.ZONE_FILLER(board).Fill(board.Zones()) if zones else True
    board.BuildConnectivity()
    conn = board.GetConnectivity()
    radius = round(float(opts.get("radius_mm", 5.0)) * NM)
    ic_pats, cap_pats = opts.get("ic") or ["U*", "IC*"], opts.get("cap") or ["C*"]
    power, ground = classify_nets(board, opts)
    ics = [fp for fp in board.GetFootprints() if match_any(fp.GetReference(), ic_pats)]
    caps, unpaired, cells = [], [], []
    for fp in board.GetFootprints():
        if not match_any(fp.GetReference(), cap_pats) or fp in ics:
            continue
        netted = [p for p in fp.Pads() if p.GetNetCode() > 0]
        if len(netted) != 2:
            continue
        pw = [p for p in netted if p.GetNetname() in power]
        gn = [p for p in netted if p.GetNetname() in ground]
        if len(pw) == 1 and len(gn) == 1:
            caps.append((fp, pw[0], gn[0]))
    graphs = {}

    def graph(net):
        if net not in graphs:
            graphs[net] = NetGraph(pcbnew, board, conn, board.FindNet(net).GetNetCode())
        return graphs[net]

    for fp, cp, cg in caps:
        cands = []
        for ic in ics:
            best = []
            for cap_pad, net in ((cp, cp.GetNetname()), (cg, cg.GetNetname())):
                near = [(dist(q.GetPosition(), cap_pad.GetPosition()), q) for q in ic.Pads()
                        if q.GetNetname() == net]
                near = [x for x in near if x[0] <= radius]
                best.append(min(near, key=lambda x: x[0]) if near else None)
            if all(best):
                cands.append((best[0][0] + best[1][0], ic, best[0][1], best[1][1]))
        cap_rec = {"ref": fp.GetReference(), "value": fp.GetValue(), "footprint": fp.GetFPIDAsString(),
                   "power_net": cp.GetNetname(), "ground_net": cg.GetNetname(),
                   "position_mm": pos_mm(fp.GetPosition()), "layer": fp.GetLayerName()}
        if not cands:
            unpaired.append(cap_rec | {"reason": f"no IC pad on both nets within {mm(radius):g} mm"})
            continue
        cands.sort(key=lambda c: (c[0], c[1].GetReference()))
        _, ic, pin_p, pin_g = cands[0]
        cell = {"ic": ic.GetReference(), "capacitor": cap_rec,
                "power_pin": {"pad": pad_name(pin_p), "function": pin_p.GetPinFunction(),
                              "pin_type": pin_p.GetPinType(), "position_mm": pos_mm(pin_p.GetPosition())},
                "ground_pin": {"pad": pad_name(pin_g), "function": pin_g.GetPinFunction(),
                               "pin_type": pin_g.GetPinType(), "position_mm": pos_mm(pin_g.GetPosition())},
                "power": side(graph(cp.GetNetname()), pin_p, cp),
                "ground": side(graph(cg.GetNetname()), pin_g, cg),
                "alternatives": [{"ic": c[1].GetReference(), "power_pin": pad_name(c[2]),
                                  "ground_pin": pad_name(c[3]), "distance_sum_mm": mm(c[0])} for c in cands[1:]]}
        cell["ambiguous"] = bool(cell["alternatives"])
        tops = {cell["power"]["topology"], cell["ground"]["topology"]}
        cell["topology"] = ("respects" if tops == {"respects"} else "bypassed" if "bypassed" in tops
                            else "incomplete")
        cells.append(cell)
    cells.sort(key=lambda c: (c["ic"], c["capacitor"]["ref"]))
    for cell in cells:  # capacitors sharing one pin pair (e.g. 100n beside 10u)
        key = (cell["power_pin"]["pad"], cell["ground_pin"]["pad"])
        cell["shared_with"] = [c["capacitor"]["ref"] for c in cells if c is not cell and
                               (c["power_pin"]["pad"], c["ground_pin"]["pad"]) == key]
    counts = {}
    for c in cells:
        counts[c["topology"]] = counts.get(c["topology"], 0) + 1
    return {"kind": "decoupling", "board": str(board_path), "zones_refilled": bool(filled),
            "options": {"radius_mm": mm(radius), "ic": ic_pats, "cap": cap_pats,
                        "power": opts.get("power"), "ground": opts.get("ground")},
            "power_nets": sorted(power), "ground_nets": sorted(ground),
            "ics": sorted(fp.GetReference() for fp in ics), "capacitors": len(caps),
            "cells": cells, "unpaired": unpaired, "counts": counts}


def main(argv):
    if len(argv) not in (2, 3):
        print(__doc__)
        return 2
    import pcbnew
    opts = json.loads(argv[2]) if len(argv) == 3 else {}
    try:
        report = analyze(pcbnew, Path(argv[0]), opts)
    except LoadError as e:
        print(LOAD_ERROR + " " + json.dumps(str(e)))
        return 3
    Path(argv[1]).write_text(json.dumps(report, indent=1) + "\n")
    print("DECOUPLING_JSON: " + json.dumps({"cells": len(report["cells"]), "counts": report["counts"],
                                             "unpaired": len(report["unpaired"]),
                                             "capacitors": report["capacitors"],
                                             "zones_refilled": report["zones_refilled"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
