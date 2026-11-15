#!/usr/bin/env python3
"""Foreign tracks clear a plated pad's actual drill, under its scoped rules."""
import math
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

from shapely.geometry import LineString, Point
from design_rules import DesignRules, parse_dru
from kicad_parser import BoardInfo, Net, Pad, PCBData, pad_drill_capsule
from kicad_dru import install_track_clearances
from routing_config import GridCoord, GridRouteConfig
from obstacle_map import GridObstacleMap, _add_pad_obstacle, build_base_obstacle_map, pad_track_hole_cells
from obstacle_cache import (precompute_net_obstacles, add_net_obstacles_from_cache,
                            remove_net_obstacles_from_cache, precompute_all_net_obstacles,
                            build_working_obstacle_map)

LAYERS = ['F.Cu', 'B.Cu']
RULE_TEXT = '''(version 1)
  (rule "copper" (constraint clearance (min 0.1mm)))
  (rule "VIN hole" (layer "F.Cu")
    (condition "A.NetName == '/VIN' && B.Pad_Number == 'SH'")
    (constraint hole_clearance (min 0.28mm)))'''


def fixture(slot=False, angle=0, offset=False, routed=(2,)):
    pad = Pad('J1', 'SH', 10.0, 10.0, 0, 0, 0.9, 2.4 if slot else 0.9,
              'oval' if slot else 'circle', ['*.Cu'], 1, '/GND',
              drill=2.1 if slot else 0.6, drill_w=0.6 if slot else 0,
              drill_h=2.1 if slot else 0, pad_type='thru_hole',
              rotation=angle, rect_rotation=-angle,
              hole_x=10.04 if offset else None, hole_y=10.03 if offset else None)
    pcb = PCBData(BoardInfo({0: 'F.Cu', 31: 'B.Cu'}, LAYERS),
                  {1: Net(1, '/GND'), 2: Net(2, '/VIN'), 3: Net(3, '/OTHER')},
                  {}, [], [], {1: [pad]})
    rules, notes = parse_dru(RULE_TEXT)
    assert not notes, notes
    dr = DesignRules(board_min={'min_hole_clearance': 0.2}, rules=rules,
                     net_names={n: net.name for n, net in pcb.nets.items()},
                     copper_layers=LAYERS)
    cfg = GridRouteConfig(layers=LAYERS, grid_step=0.025, track_width=0.1,
                          clearance=0.1, via_size=0.45, via_drill=0.2, rules=dr)
    install_track_clearances(cfg, {}, '', pcb, routed_net_ids=routed)
    return pcb, pad, cfg


def maps(pcb, pad, cfg):
    direct = GridObstacleMap(2)
    _add_pad_obstacle(direct, pad, GridCoord(cfg.grid_step),
                      {'F.Cu': 0, 'B.Cu': 1}, cfg)
    cached = GridObstacleMap(2)
    data = precompute_net_obstacles(pcb, 1, cfg)
    add_net_obstacles_from_cache(cached, data)
    return direct, cached, data


class PadHoleRules(unittest.TestCase):
    def test_rule_resolution(self):
        _, pad, cfg = fixture()
        dr = cfg.rules
        a = dr.item_for_net(2, 'track', 'F.Cu')
        b = dr.item_for_pad(pad)
        self.assertAlmostEqual(dr.resolve('hole_clearance', a, b, 'F.Cu').min, .28)
        self.assertAlmostEqual(dr.resolve('hole_clearance', a, b, 'B.Cu').min, .2)

    def test_round_slot_rotated_and_offset_keepouts(self):
        for slot, angle, offset in [(False, 0, False), (True, 0, False),
                                     (True, 37, False), (True, 37, True)]:
            with self.subTest(slot=slot, angle=angle, offset=offset):
                pcb, pad, cfg = fixture(slot, angle, offset)
                direct, cached, _ = maps(pcb, pad, cfg)
                p1, p2, radius = pad_drill_capsule(pad)
                axis = Point(p1) if p1 == p2 else LineString([p1, p2])
                step = cfg.grid_step
                points = [(x, y) for x in range(325, 476) for y in range(325, 476)]
                for li, hole_gap in [(0, .28), (1, .2)]:
                    a = {(x, y) for x, y in points if direct.is_blocked(x, y, li)}
                    b = {(x, y) for x, y in points if cached.is_blocked(x, y, li)}
                    self.assertEqual(a, b)
                    # Every horizontal/vertical/diagonal step between free cells
                    # must clear the drill, including between the sampled vertices.
                    closest = math.inf
                    for x, y in points:
                        if (x, y) in a:
                            continue
                        if axis.distance(Point(x * step, y * step)) > 1.6:
                            continue
                        for dx, dy in [(1, 0), (0, 1), (1, 1), (1, -1)]:
                            if (x + dx, y + dy) not in a:
                                edge = LineString([(x * step, y * step),
                                                   ((x + dx) * step, (y + dy) * step)])
                                closest = min(closest, axis.distance(edge) - radius - .05)
                    self.assertGreaterEqual(closest, hole_gap - 1e-8)

    def test_scope_and_own_pad_reachability(self):
        pcb, pad, cfg = fixture()
        direct, cached, data = maps(pcb, pad, cfg)
        # .275 from the drill to track edge: legal copper, illegal VIN hole gap.
        self.assertTrue(direct.is_blocked(425, 400, 0))
        self.assertTrue(cached.is_blocked(425, 400, 0))
        self.assertFalse(cached.is_blocked(425, 400, 1))
        other_pcb, other_pad, other_cfg = fixture(routed=(3,))
        other, _, _ = maps(other_pcb, other_pad, other_cfg)
        self.assertFalse(other.is_blocked(425, 400, 0))
        # The drill keep-out belongs to the pad's cache, not to the permanent
        # map. Routing its own net removes it with the copper and leaves no leak.
        remove_net_obstacles_from_cache(cached, data)
        self.assertEqual(cached.get_stats()[:2], (0, 0))
        self.assertFalse(cached.is_blocked(400, 400, 0))
        base = build_base_obstacle_map(pcb, cfg, [1, 2])
        self.assertFalse(base.is_blocked(400, 400, 0))
        self.assertFalse(base.is_blocked(425, 400, 0))

    def test_board_minimum_and_undeclared_rule(self):
        pcb, pad, cfg = fixture()
        cfg.rules.rules = []
        cfg.rules.board_min['min_hole_clearance'] = .28
        direct, cached, _ = maps(pcb, pad, cfg)
        self.assertTrue(direct.is_blocked(425, 400, 0))
        self.assertTrue(cached.is_blocked(425, 400, 0))
        cfg.rules.board_min.clear()
        direct, cached, _ = maps(pcb, pad, cfg)
        self.assertFalse(direct.is_blocked(425, 400, 0))
        self.assertFalse(cached.is_blocked(425, 400, 0))

    def test_new_routing_call_rebuilds_cache_for_wider_net_set(self):
        pcb, pad, cfg = fixture(routed=(3,))
        # The route and diff fronts install the full call's set, then build
        # fresh caches. A later call may include a net with a larger gap.
        narrow = precompute_all_net_obstacles(pcb, [1], cfg)
        first = build_working_obstacle_map(GridObstacleMap(2), narrow)
        self.assertFalse(first.is_blocked(425, 400, 0))
        install_track_clearances(cfg, {}, '', pcb, routed_net_ids=[1, 2, 3])
        wide = precompute_all_net_obstacles(pcb, [1], cfg)
        second = build_working_obstacle_map(GridObstacleMap(2), wide)
        self.assertTrue(second.is_blocked(425, 400, 0))
        self.assertFalse(second.is_blocked(425, 400, 1))
        # Even under the wider set its own pad remains a usable terminal.
        remove_net_obstacles_from_cache(second, wide[1])
        self.assertEqual(second.get_stats()[:2], (0, 0))
        self.assertFalse(second.is_blocked(400, 400, 0))

    def test_no_hole_gap_skips_drill_geometry(self):
        _, pad, cfg = fixture(slot=True, angle=37, offset=True)
        cfg.rules.rules = []
        cfg.rules.board_min.clear()
        with patch('obstacle_map.pad_drill_capsule', side_effect=AssertionError('unused drill geometry')):
            self.assertEqual(list(pad_track_hole_cells(pad, GridCoord(cfg.grid_step),
                                                      {'F.Cu': 0, 'B.Cu': 1}, cfg)), [])

    def test_native_drc_agrees_at_straight_drill_wall(self):
        import pcbnew
        for slot in [False, True]:
            for x, violation in [(10.625, True), (10.65, False)]:
                with self.subTest(slot=slot, x=x), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / 'hole.kicad_pcb'
                    board = pcbnew.BOARD()
                    ground = pcbnew.NETINFO_ITEM(board, '/GND')
                    vin = pcbnew.NETINFO_ITEM(board, '/VIN')
                    board.Add(ground)
                    board.Add(vin)
                    fp = pcbnew.FOOTPRINT(board)
                    fp.SetReference('J1')
                    board.Add(fp)
                    p = pcbnew.PAD(fp)
                    p.SetNumber('SH')
                    p.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
                    p.SetShape(pcbnew.PAD_SHAPE_OVAL if slot else pcbnew.PAD_SHAPE_CIRCLE)
                    p.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(.9), pcbnew.FromMM(2.4 if slot else .9)))
                    p.SetDrillShape(pcbnew.PAD_DRILL_SHAPE_OBLONG if slot else pcbnew.PAD_DRILL_SHAPE_CIRCLE)
                    p.SetDrillSize(pcbnew.VECTOR2I(pcbnew.FromMM(.6), pcbnew.FromMM(2.1 if slot else .6)))
                    p.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(10)))
                    p.SetLayerSet(pcbnew.LSET.AllCuMask())
                    p.SetNet(ground)
                    fp.Add(p)
                    track = pcbnew.PCB_TRACK(board)
                    track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(9.9)))
                    track.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(10.1)))
                    track.SetWidth(pcbnew.FromMM(.1))
                    track.SetLayer(pcbnew.F_Cu)
                    track.SetNet(vin)
                    board.Add(track)
                    pcbnew.SaveBoard(str(path), board)
                    path.with_suffix('.kicad_pro').write_text(json.dumps({
                        'board': {'design_settings': {'rules': {
                            'min_hole_clearance': .2, 'min_clearance': .1}}}}))
                    path.with_suffix('.kicad_dru').write_text(RULE_TEXT)
                    report = path.with_suffix('.json')
                    run = subprocess.run(['kicad-cli', 'pcb', 'drc', '--format', 'json',
                                          '--output', str(report), str(path)],
                                         capture_output=True, text=True, timeout=60)
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    findings = json.loads(report.read_text())['violations']
                    self.assertFalse(any(v['type'] == 'clearance' for v in findings), findings)
                    holes = [v for v in findings if v['type'] == 'hole_clearance']
                    self.assertEqual(bool(holes), violation, findings)


if __name__ == '__main__':
    unittest.main()
