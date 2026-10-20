#!/usr/bin/env python3
"""KICADROUTE-64: grid taps must not create off-grid T-junction weld stubs.

Run with python3 tests/test_krt64_tap_grid.py. Needs the normal routing
runtime, pcbnew and kicad-cli; missing tools are failures, never passes.
The tiny board is authored here without external footprint libraries.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'py_router'))
from kicad_parser import Segment, Via, parse_kicad_pcb
from routing_config import GridCoord
from single_ended_routing import get_all_segment_tap_points


class TapGridTests(unittest.TestCase):
    def test_axial_and_diagonal_cover_each_grid_cell(self):
        for step in (0.1, 0.05, 0.025):
            coord = GridCoord(step)
            for start, end in (((79.3, 22.4), (80.6, 22.4)),
                               ((3, 5), (9, 5)), ((3, 3), (9, 9)),
                               ((9, 3), (3, 9)), ((-3, -3), (-3, 3))):
                for a, b in ((start, end), (end, start)):
                    with self.subTest(step=step, start=a, end=b):
                        points = get_all_segment_tap_points(
                            [Segment(*a, *b, 0.2, 'F.Cu', 1)], coord, ['F.Cu', 'B.Cu'])
                        ga, gb = coord.to_grid(*a), coord.to_grid(*b)
                        expected = max(abs(gb[0]-ga[0]), abs(gb[1]-ga[1])) + 1
                        self.assertEqual(len(points), expected)
                        for gx, gy, layer, x, y in points:
                            self.assertEqual(layer, 0)
                            self.assertAlmostEqual(x, gx * step, places=9)
                            self.assertAlmostEqual(y, gy * step, places=9)
                        self.assertEqual(points, sorted(points))

    def test_off_grid_copper_keeps_physical_endpoints_and_collinearity(self):
        coord = GridCoord(0.1)
        for a, b in (((1.023, 2.017), (1.027, 2.021)),
                     ((1.023, 2.017), (3.139, 3.841))):
            with self.subTest(start=a, end=b):
                points = get_all_segment_tap_points(
                    [Segment(*a, *b, 0.2, 'F.Cu', 1)], coord, ['F.Cu'])
                physical = [(p[3], p[4]) for p in points]
                self.assertIn(a, physical)
                # A sub-cell segment deduplicates to its first endpoint.
                if coord.to_grid(*a) != coord.to_grid(*b):
                    self.assertTrue(any(abs(x-b[0]) < 1e-9 and abs(y-b[1]) < 1e-9
                                        for x, y in physical))
                for x, y in physical:
                    self.assertAlmostEqual((x-a[0])*(b[1]-a[1]) - (y-a[1])*(b[0]-a[0]), 0)

    def test_diff_pair_terminal_island_uses_grid_taps(self):
        from diff_pair_routing import _terminal_island_taps
        coord = GridCoord(0.1)
        diagonal = Segment(3, 3, 9, 9, 0.2, 'F.Cu', 1)
        unrelated = Segment(12, 12, 13, 13, 0.2, 'F.Cu', 1)
        points = _terminal_island_taps((3, 3), [diagonal, unrelated], [], coord, ['F.Cu'])
        self.assertEqual(len(points), 61)
        for gx, gy, layer, x, y in points:
            self.assertEqual(gx, gy)
            self.assertEqual(layer, 0)
            self.assertAlmostEqual(x, gx * 0.1, places=9)
            self.assertAlmostEqual(y, gy * 0.1, places=9)

    def test_point_segment_and_empty_input(self):
        coord = GridCoord(0.1)
        self.assertEqual(get_all_segment_tap_points([], coord, ['F.Cu']), [])
        for end in ((1.023, 2.017), (1.0231, 2.0171)):
            self.assertEqual(get_all_segment_tap_points(
                [Segment(1.023, 2.017, *end, 0.2, 'F.Cu', 1)], coord, ['F.Cu']),
                [(10, 20, 0, 1.023, 2.017)])

    def test_layer_dedup_and_via_physical_coordinates(self):
        coord = GridCoord(0.1)
        seg = Segment(3, 3, 3.7, 3, 0.2, 'B.Cu', 1)
        via = Via(3.223, 3.017, 0.6, 0.3, ['F.Cu', 'B.Cu'], 1)
        points = get_all_segment_tap_points([seg, seg], coord, ['F.Cu', 'B.Cu'], [via])
        self.assertEqual(len(points), 9)
        self.assertIn((32, 30, 0, via.x, via.y), points)
        self.assertIn((32, 30, 1, 3.2, 3.0), points)  # existing segment wins


PADS = [(3, 3), (9, 9), (7.3, 5.5), (5.2, 7.9), (10.5, 4)]
TRACK = (3, 3, 9, 9)


def variant_namespace(n):
    return uuid.uuid5(uuid.NAMESPACE_URL, f'kicad-route/KICADROUTE-64/{n}')


def owned_board(path, variant):
    """20 x 15 mm, two layers, five one-pad footprints authored here (no library parts)."""
    import pcbnew
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    net = pcbnew.NETINFO_ITEM(board, "/T")
    board.Add(net)
    mm = pcbnew.FromMM
    ids = (pcbnew.KIID(str(uuid.uuid5(variant_namespace(variant), str(n)))) for n in range(1 << 16))

    def add(parent, item):
        item.SetUuid(next(ids))
        for field in item.GetFields() if isinstance(item, pcbnew.FOOTPRINT) else ():
            field.SetUuid(next(ids))
        parent.Add(item)

    def xy(x, y):
        return pcbnew.VECTOR2I(mm(x), mm(y))
    for x0, y0, x1, y1 in ((0, 0, 20, 0), (20, 0, 20, 15), (20, 15, 0, 15), (0, 15, 0, 0)):
        edge = pcbnew.PCB_SHAPE(board)
        edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
        edge.SetLayer(pcbnew.Edge_Cuts)
        edge.SetStart(xy(x0, y0))
        edge.SetEnd(xy(x1, y1))
        edge.SetWidth(mm(0.1))
        add(board, edge)
    for i, (x, y) in enumerate(PADS, 1):
        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference(f"TP{i}")
        fp.Reference().SetVisible(False)
        fp.SetFPID(pcbnew.LIB_ID("kr64", "Pad"))
        fp.SetPosition(xy(x, y))
        pad = pcbnew.PAD(fp)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.PAD_SHAPE_RECT)
        pad.SetSize(xy(0.4, 0.4))
        pad.SetNumber("1")
        layers = pcbnew.LSET()
        for layer in (pcbnew.F_Cu, pcbnew.F_Mask, pcbnew.F_Paste):
            layers.AddLayer(layer)
        pad.SetLayerSet(layers)
        pad.SetPosition(xy(x, y))
        pad.SetNet(net)
        add(fp, pad)
        add(board, fp)
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(xy(*TRACK[:2]))
    track.SetEnd(xy(*TRACK[2:]))
    track.SetWidth(mm(0.2))
    track.SetLayer(pcbnew.F_Cu)
    track.SetNet(net)
    add(board, track)
    pcbnew.SaveBoard(str(path), board)


def copper(path):
    pcb = parse_kicad_pcb(str(path))
    return (sorted((s.start_x, s.start_y, s.end_x, s.end_y, s.width, s.layer, s.net_id)
                   for s in pcb.segments),
            sorted((v.x, v.y, v.size, v.drill, v.net_id) for v in pcb.vias))


class OwnedBoardTests(unittest.TestCase):
    def test_native_drc_and_repeatability(self):
        with tempfile.TemporaryDirectory(prefix='krt64-') as scratch:
            work = Path(scratch)
            # Fixed UUID orders 2 and 3 both reproduced track_dangling before the fix.
            for variant in (2, 3):
                board = work / f'v{variant}.kicad_pcb'
                owned_board(board, variant)
                geometries, findings = [], []
                for repeat in range(2):
                    out = work / f'v{variant}-run{repeat}'
                    out.mkdir()
                    output = out / board.name
                    summary = out / 'summary.json'
                    env = dict(os.environ, KICAD_CACHE_HOME=str(out / '.cache'),
                               KICAD_INRUN_FLOOR_SYNC='0', KICAD_TERMINAL_ESCALATION='0',
                               KICAD_IMPEDANCE_NECKDOWN='0', KICAD_RESCUE_CAP_MOVE='0',
                               KICAD_DYNAMIC_ITERATIONS='0', KICAD_RIP_PREEXISTING='0',
                               KICAD_FINALIZE_RIP='0')
                    r = subprocess.run([sys.executable, str(ROOT / 'py_router/route.py'), str(board),
                                        '--output', str(output), '--escalation', 'off',
                                        '--fab-tier', 'standard', '--strict-sizes',
                                        '--no-power-tap-neckdown', '--no-fix-drc-settings',
                                        '--keep-input-copper', '--no-stub-layer-swap',
                                        '--json-out', str(summary), '--nets', '*'],
                                       cwd=out, env=env, capture_output=True, text=True, timeout=600)
                    self.assertEqual(r.returncode, 0, r.stdout[-5000:] + r.stderr[-3000:])
                    stats = json.loads(summary.read_text())
                    self.assertEqual(stats['failed_single'], [])
                    self.assertEqual((stats['pad_pairs_connected'], stats['pad_pairs_total']), (4, 4))
                    drc = out / 'drc.json'
                    d = subprocess.run(['kicad-cli', 'pcb', 'drc', '--format', 'json',
                                        '--severity-all', '--refill-zones', '-o', str(drc), str(output)],
                                       capture_output=True, text=True, timeout=120)
                    self.assertEqual(d.returncode, 0, d.stdout + d.stderr)
                    report = json.loads(drc.read_text())
                    self.assertEqual(report['unconnected_items'], [])
                    self.assertFalse([v for v in report['violations'] if v['type'] == 'track_dangling'])
                    # The only expected warnings concern the original, library-free footprints.
                    self.assertTrue(all(v['type'] in ('lib_footprint_issues', 'lib_footprint_mismatch')
                                        for v in report['violations']), report['violations'])
                    geometries.append(copper(output))
                    findings.append(sorted(v['type'] for v in report['violations']))
                self.assertEqual(geometries[0], geometries[1])
                self.assertEqual(findings[0], findings[1])
                print(f'variant {variant}: 4/4 pairs, no dangling/unconnected, identical copper and DRC twice')


if __name__ == '__main__':
    unittest.main(verbosity=2)
