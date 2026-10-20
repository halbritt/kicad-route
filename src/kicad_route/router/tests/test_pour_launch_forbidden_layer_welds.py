#!/usr/bin/env python3
"""POUR-LAUNCH must not weld on a layer the route may not place track on.

A pour-launch cell is keyed on the routing grid but used to carry the float of
the fill-raster sample it came from as its weld point, so the converter drew a
sub-cell bridge from the grid point to that sample on the zone's layer. On a
4-layer board routed `--layers F.Cu B.Cu` with a GND plane on In1.Cu, the route
reaches the plane only by a via at the grid point, and that bridge was a 20-56
um GND track on In1.Cu -- a layer outside --layers -- inside the via's own
copper. On a forbidden layer the cell's weld point is now the grid point (the
via there covers the fill sample a sub-cell away); on a routable layer it stays
the fill float, so the bridge still lands ON fill copper.

The board is built here: a 20x16 mm 4-layer outline, a THT GND test point, three
0402 resistors with a GND pad on F.Cu, and a GND zone on In1.Cu.

    python3 tests/test_pour_launch_forbidden_layer_welds.py
"""
import os
import re
import subprocess
import sys
import tempfile
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # #522
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # #522

ROUTED = ('F.Cu', 'B.Cu')
# route.py appends the copper layers left out of --layers at the forbidden cost.
ALL_LAYERS = ['F.Cu', 'B.Cu', 'In1.Cu', 'In2.Cu']


def _uuid(tag):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'krt-pour-weld/' + tag))


def _board_text():
    def smd(ref, x, y):
        pads = ''.join(
            f'\t\t(pad "{i + 1}" smd roundrect (at {dx} 0) (size 0.6 0.6) '
            f'(layers "F.Cu" "F.Mask") (roundrect_rratio 0.25) (net "{net}") '
            f'(uuid "{_uuid(ref + str(i))}"))\n'
            for i, (dx, net) in enumerate(((-0.5, 'GND'), (0.5, 'SIG'))))
        return (f'\t(footprint "test:R_0402" (layer "F.Cu") (uuid "{_uuid(ref)}") '
                f'(at {x} {y})\n'
                f'\t\t(property "Reference" "{ref}" (at 0 -1 0) (layer "F.Fab") '
                f'(uuid "{_uuid(ref + "r")}") (effects (font (size 0.5 0.5) '
                f'(thickness 0.1))))\n{pads}\t)\n')

    parts = [
        '(kicad_pcb\n\t(version 20260206)\n\t(generator "pcbnew")\n'
        '\t(generator_version "10.0")\n'
        '\t(general (thickness 1.6) (legacy_teardrops no))\n\t(paper "A4")\n'
        '\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(4 "In1.Cu" signal)\n'
        '\t\t(6 "In2.Cu" signal)\n\t\t(2 "B.Cu" signal)\n'
        '\t\t(1 "F.Mask" user)\n\t\t(3 "B.Mask" user)\n'
        '\t\t(25 "Edge.Cuts" user)\n\t)\n'
        '\t(setup (pad_to_mask_clearance 0))\n'
        '\t(net 0 "")\n\t(net 1 "GND")\n\t(net 2 "SIG")\n',
        f'\t(footprint "test:TP" (layer "F.Cu") (uuid "{_uuid("J1")}") (at 3 3)\n'
        f'\t\t(property "Reference" "J1" (at 0 -1.5 0) (layer "F.Fab") '
        f'(uuid "{_uuid("J1r")}") (effects (font (size 0.5 0.5) (thickness 0.1))))\n'
        f'\t\t(pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1) '
        f'(layers "*.Cu" "*.Mask") (remove_unused_layers no) (net "GND") '
        f'(uuid "{_uuid("J1p")}"))\n\t)\n',
    ]
    parts += [smd(f'R{k + 1}', x, y)
              for k, (x, y) in enumerate(((12, 6), (15, 11), (8, 12)))]
    parts.append(
        f'\t(gr_rect (start 0 0) (end 20 16) (stroke (width 0.05) (type default)) '
        f'(fill no) (layer "Edge.Cuts") (uuid "{_uuid("edge")}"))\n'
        f'\t(zone (net "GND") (layer "In1.Cu") (uuid "{_uuid("zone")}") '
        f'(hatch edge 0.5)\n'
        '\t\t(connect_pads yes (clearance 0.2)) (min_thickness 0.16)\n'
        '\t\t(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5) '
        '(island_removal_mode 0))\n'
        '\t\t(polygon (pts (xy 0.3 0.3) (xy 19.7 0.3) (xy 19.7 15.7) (xy 0.3 15.7)))\n'
        '\t)\n)\n')
    return ''.join(parts)


def _launch_cells(board, layer_costs):
    """_pour_launch_region_cells for GND under a config with these costs."""
    from kicad_parser import parse_kicad_pcb
    from routing_config import GridRouteConfig, GridCoord
    from connectivity import get_multipoint_net_pads, get_terminal_component_info
    from single_ended_routing import _pour_launch_region_cells

    pcb = parse_kicad_pcb(board)
    config = GridRouteConfig(layers=list(ALL_LAYERS), layer_costs=layer_costs)
    coord = GridCoord(config.grid_step)
    net_id = next(n for n, net in pcb.nets.items() if net.name == 'GND')
    pad_info = get_multipoint_net_pads(pcb, net_id, config)
    comps = get_terminal_component_info(pcb, net_id, pad_info)[0]
    cells = _pour_launch_region_cells(pcb, net_id, pad_info, comps,
                                      config.layers, coord, {}, config)
    return coord, [(k, v) for cmap in cells.values() for k, v in cmap.items()]


def _pair_anchor_rows(board, layer_costs):
    """_pour_launch_pair_anchors rows for R1.1 (near) -> J1.1 (on the plane)."""
    from kicad_parser import parse_kicad_pcb
    from routing_config import GridRouteConfig, GridCoord
    from single_ended_routing import _pour_launch_pair_anchors

    pcb = parse_kicad_pcb(board)
    config = GridRouteConfig(layers=list(ALL_LAYERS), layer_costs=layer_costs)
    coord = GridCoord(config.grid_step)
    net_id = next(n for n, net in pcb.nets.items() if net.name == 'GND')

    def row(x, y):
        g = coord.to_grid(x, y)
        return (g[0], g[1], 0, x, y)
    _, extra_t = _pour_launch_pair_anchors(
        pcb, net_id, [row(11.5, 6.0)], [row(3.0, 3.0)], config.layers, coord,
        config)
    return coord, extra_t


def _copper(path):
    """Uuid-free sorted (segments, vias) of a board, and its off-route tracks."""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    segs = sorted(re.findall(
        r'\(segment\s+\(start ([-\d.]+) ([-\d.]+)\)\s+\(end ([-\d.]+) ([-\d.]+)\)'
        r'\s+\(width ([\d.]+)\)\s+\(layer "([^"]+)"\)', text))
    vias = sorted(re.findall(
        r'\(via\s+\(at ([-\d.]+) ([-\d.]+)\)\s+\(size ([\d.]+)\)', text))
    return segs, vias, [s for s in segs if s[5] not in ROUTED]


def _route(board, out_dir):
    out = os.path.join(out_dir, 'out.kicad_pcb')
    r = subprocess.run(
        [sys.executable, os.path.join(ROOT, 'py_router', 'route.py'), board,
         '--output', out, '--nets', '*', '--layers', *ROUTED],
        cwd=ROOT, capture_output=True, text=True, timeout=600)
    return r, out


def scenarios(tmp):
    from routing_config import GridRouteConfig
    from single_ended_routing import _track_forbidden_layers

    results = []
    board = os.path.join(tmp, 'plane4.kicad_pcb')
    with open(board, 'w', encoding='utf-8') as f:
        f.write(_board_text())

    # 1. The forbidden set is exactly the negative-cost layers.
    cfg = GridRouteConfig(layers=list(ALL_LAYERS), layer_costs=[1, 1.5, -1, -1])
    results.append(("forbidden layers are the cost<0 ones",
                    _track_forbidden_layers(cfg, ALL_LAYERS) == {2, 3}))
    results.append(("no costs -> nothing forbidden",
                    _track_forbidden_layers(
                        GridRouteConfig(layers=list(ALL_LAYERS)),
                        ALL_LAYERS) == frozenset()))

    # 2. Forbidden In1.Cu: every launch cell welds at its own grid point.
    coord, cells = _launch_cells(board, [1, 1, -1, -1])
    in1 = [(k, v) for k, v in cells if k[2] == ALL_LAYERS.index('In1.Cu')]
    results.append(("pour-launch offers In1.Cu fill cells", len(in1) > 0))
    results.append(("forbidden-layer cells weld at their grid point",
                    all(v == coord.to_float(k[0], k[1]) for k, v in in1)))

    # 3. Routable In1.Cu: the weld point stays the fill sample (lands on fill).
    coord, cells = _launch_cells(board, [1, 1, 1, 1])
    in1 = [(k, v) for k, v in cells if k[2] == ALL_LAYERS.index('In1.Cu')]
    results.append(("routable-layer cells keep their fill float",
                    any(v != coord.to_float(k[0], k[1]) for k, v in in1)))

    # 3b. The 2-pad fill anchors follow the same rule.
    in1_idx = ALL_LAYERS.index('In1.Cu')
    coord, rows = _pair_anchor_rows(board, [1, 1, -1, -1])
    rows = [r for r in rows if r[2] == in1_idx]
    results.append(("pair anchors offer In1.Cu fill cells", len(rows) > 0))
    results.append(("forbidden-layer pair anchors weld at their grid point",
                    all((r[3], r[4]) == coord.to_float(r[0], r[1])
                        for r in rows)))
    coord, rows = _pair_anchor_rows(board, [1, 1, 1, 1])
    results.append(("routable-layer pair anchors keep their fill float",
                    any((r[3], r[4]) != coord.to_float(r[0], r[1])
                        for r in rows if r[2] == in1_idx)))

    # 4. Route the board twice: no copper outside --layers, all connected,
    #    identical copper both times.
    runs = []
    for i in (1, 2):
        d = os.path.join(tmp, f'run{i}')
        os.makedirs(d)
        r, out = _route(board, d)
        runs.append((r, out))
        results.append((f"run {i}: route.py exits 0", r.returncode == 0))
        if r.returncode != 0:
            print(r.stdout[-3000:], r.stderr[-3000:])
            return results
    (r1, out1), (r2, out2) = runs
    results.append(("pour-launch fired", 'POUR-LAUNCH v3' in r1.stdout))
    segs1, vias1, off1 = _copper(out1)
    segs2, vias2, off2 = _copper(out2)
    for s in off1:
        print(f"  track outside --layers: {s}")
    results.append(("route placed copper", len(segs1) > 0 and len(vias1) > 0))
    results.append(("no track on a layer outside --layers", off1 == []))
    c = subprocess.run([sys.executable,
                        os.path.join(ROOT, 'py_router', 'check_connected.py'),
                        out1], cwd=ROOT, capture_output=True, text=True,
                       timeout=300)
    conn = c.returncode == 0 and 'ALL NETS FULLY CONNECTED' in c.stdout
    results.append(("all nets connected", bool(conn)))
    results.append(("two identical runs give identical copper",
                    (segs1, vias1) == (segs2, vias2)))
    return results


def main():
    print("=" * 60)
    print("POUR-LAUNCH forbidden-layer weld regression test")
    print("=" * 60)
    with tempfile.TemporaryDirectory() as tmp:
        results = scenarios(tmp)
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    n_pass = sum(1 for _, ok in results if ok)
    print(f"\n{n_pass}/{len(results)} checks passed")
    print("=" * 60)
    return 0 if n_pass == len(results) else 1


def test_pour_launch_forbidden_layer_welds():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
