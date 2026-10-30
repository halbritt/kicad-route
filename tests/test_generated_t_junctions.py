"""Retain explicit T vertices when a tiny tap weld overlaps both neighbours."""
import copy
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/kicad_route/router/py_router'))
from kicad_parser import BoardInfo, PCBData, Segment
import pcb_modification as modification
from check_weird import _check_kicad_dangling


def segment(a, b, **kw):
    return Segment(*a, *b, width=.25, layer='F.Cu', net_id=5, **kw)


def fixture():
    # Exact native coordinates from the acquisition failure. The source seed
    # is read-only; the long route and its tiny weld belong to this run.
    seed = segment((105.05, 121.8), (106.46869, 120.549203), locked=True)
    route = segment((106.525, 120.5), (104.375, 122.65))
    weld = segment((106.4, 120.625), (106.394022, 120.615034))
    pcb = PCBData(BoardInfo(layers={}, copper_layers=['F.Cu', 'B.Cu']), {}, {}, [],
                  [seed, route, weld], {})
    return pcb, [{'new_segments': [route, weld], 'new_vias': []}], seed, route, weld


def dangling(pcb):
    findings = []
    _check_kicad_dangling(5, 'GND', pcb.segments, [], [], [], ['F.Cu', 'B.Cu'], findings)
    return findings


class GeneratedJunctionTests(unittest.TestCase):
    def test_overlapping_weld_gets_a_shared_vertex_without_changing_copper(self):
        pcb, results, seed, route, weld = fixture()
        original_seed = copy.deepcopy(seed)
        self.assertEqual([(f['x'], f['y']) for f in dangling(pcb)], [(106.4, 120.625)])
        self.assertEqual(modification.split_generated_t_junctions(results, pcb, {5}), 1)
        self.assertEqual(dangling(pcb), [])
        self.assertEqual(seed, original_seed)
        self.assertIs(pcb.segments[0], seed)
        emitted = results[0]['new_segments']
        self.assertEqual({id(s) for s in emitted}, {id(s) for s in pcb.segments if s is not seed})
        pieces = [s for s in emitted if s is not weld]
        self.assertEqual(len(pieces), 2)
        self.assertEqual([(s.start_x, s.start_y, s.end_x, s.end_y) for s in pieces],
                         [(106.525, 120.5, 106.4, 120.625), (106.4, 120.625, 104.375, 122.65)])
        self.assertTrue(all((s.width, s.layer, s.net_id) == (.25, 'F.Cu', 5) for s in pieces))
        self.assertAlmostEqual(sum(math.hypot(s.end_x-s.start_x, s.end_y-s.start_y) for s in pieces),
                               math.hypot(route.end_x-route.start_x, route.end_y-route.start_y), places=12)
        before = copy.deepcopy(pcb.segments)
        self.assertEqual(modification.split_generated_t_junctions(results, pcb, {5}), 0)
        self.assertEqual(pcb.segments, before)

    def test_junction_survives_collinear_merge_and_writer_precision(self):
        from kicad_writer import generate_segment_sexpr
        pcb, results, seed, route, weld = fixture()
        modification.split_generated_t_junctions(results, pcb, {5})
        modification.merge_collinear_segments(results, pcb, {5}, keep_input_copper=True)
        self.assertEqual(dangling(pcb), [])
        joints = [(s, end) for s in pcb.segments for end in
                  ((s.start_x, s.start_y), (s.end_x, s.end_y)) if end == (106.4, 120.625)]
        self.assertEqual(len(joints), 3)
        for s, _ in joints:
            text = generate_segment_sexpr((s.start_x, s.start_y), (s.end_x, s.end_y),
                                          s.width, s.layer, s.net_id)
            self.assertIn('106.400000 120.625000', text)

    def test_input_locked_and_graphic_anchors_are_immutable(self):
        for mode in ('input', 'locked', 'graphic'):
            with self.subTest(mode=mode):
                pcb, results, seed, route, weld = fixture()
                if mode == 'input':
                    results[0]['new_segments'].remove(route)
                else:
                    setattr(route, mode, True)
                before = copy.deepcopy(pcb.segments)
                self.assertEqual(modification.split_generated_t_junctions(results, pcb, {5}), 0)
                self.assertEqual(pcb.segments, before)

    def test_nearby_or_foreign_endpoints_are_not_projected(self):
        for mode in ('nearby', 'layer', 'net', 'scope'):
            with self.subTest(mode=mode):
                pcb, results, seed, route, weld = fixture()
                if mode == 'nearby':
                    weld.start_y += .000002
                elif mode == 'layer':
                    weld.layer = 'B.Cu'
                elif mode == 'net':
                    weld.net_id = 6
                before = copy.deepcopy(pcb.segments)
                self.assertEqual(modification.split_generated_t_junctions(
                    results, pcb, {6} if mode == 'scope' else {5, 6}), 0)
                self.assertEqual(pcb.segments, before)

    def test_repeated_branch_points_make_one_nonzero_split(self):
        pcb, results, seed, route, weld = fixture()
        branch = segment((106.4, 120.625), (106.4, 121.5))
        pcb.segments.append(branch)
        results.append({'new_segments': [branch], 'new_vias': []})
        self.assertEqual(modification.split_generated_t_junctions(results, pcb, {5}), 1)
        self.assertTrue(all((s.start_x, s.start_y) != (s.end_x, s.end_y) for s in pcb.segments))
        self.assertIs(results[1]['new_segments'][0], branch)

    def test_vertical_reversed_and_two_split_points(self):
        route = segment((2, 4), (2, 0))
        branches = [segment((2, y), (3, y)) for y in (1, 3)]
        pcb = PCBData(BoardInfo(layers={}, copper_layers=['F.Cu', 'B.Cu']), {}, {}, [], [route] + branches, {})
        results = [{'new_segments': [route] + branches}]
        self.assertEqual(modification.split_generated_t_junctions(results, pcb), 2)
        pieces = results[0]['new_segments'][:3]
        self.assertEqual([(s.start_y, s.end_y) for s in pieces], [(4, 3), (3, 1), (1, 0)])

    def test_expansion_or_comparison_limit_preserves_the_whole_pass(self):
        # Nested collinear tracks have N squared output pieces if unrestricted.
        # A later limit must also discard an earlier, otherwise valid split.
        for limits, kind in (({'max_comparisons': 10}, 'comparisons'),
                             ({'max_replacement_pieces': 4}, 'replacement_pieces')):
            with self.subTest(kind=kind):
                pcb, results, seed, route, weld = fixture()
                overlapping = [segment((i, 0), (8-i, 0)) for i in range(4)]
                pcb.segments.extend(overlapping)
                results.append({'new_segments': overlapping})
                original_board_list = pcb.segments
                original_result_lists = [r['new_segments'] for r in results]
                original = copy.deepcopy(pcb.segments)
                stats = {}
                self.assertEqual(modification.split_generated_t_junctions(
                    results, pcb, {5}, stats=stats, **limits), 0)
                self.assertIs(pcb.segments, original_board_list)
                self.assertEqual(pcb.segments, original)
                for r, prior in zip(results, original_result_lists):
                    self.assertIs(r['new_segments'], prior)
                self.assertEqual(stats['limit_reached'], kind)
                self.assertEqual([(f['x'], f['y']) for f in dangling(pcb)
                                  if (f['x'], f['y']) == (106.4, 120.625)],
                                 [(106.4, 120.625)])

    def test_native_three_track_reproducer(self):
        import pcbnew as k
        pcb, results, seed, route, weld = fixture()
        weld.uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, 'kicad-route/generated-junction-weld'))
        seed.uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, 'kicad-route/generated-junction-seed'))
        with tempfile.TemporaryDirectory(prefix='native-t-junction-') as directory:
            root = Path(directory)
            def observe(name):
                board = k.BOARD();board.SetCopperLayerCount(2)
                net = k.NETINFO_ITEM(board, 'GND');board.Add(net)
                for s in pcb.segments:
                    item = k.PCB_TRACK(board)
                    item.SetStart(k.VECTOR2I(round(s.start_x*1e6), round(s.start_y*1e6)))
                    item.SetEnd(k.VECTOR2I(round(s.end_x*1e6), round(s.end_y*1e6)))
                    item.SetWidth(round(s.width*1e6));item.SetLayer(k.F_Cu);item.SetNet(net)
                    item.SetLocked(s.locked)
                    if s.uuid:item.SetUuid(k.KIID(s.uuid))
                    board.Add(item)
                path = root/(name+'.kicad_pcb');k.SaveBoard(str(path), board)
                report = root/(name+'.json')
                env = dict(os.environ, XDG_CONFIG_HOME=str(root/'config'),
                           XDG_CACHE_HOME=str(root/'cache'), XDG_DATA_HOME=str(root/'data'))
                run = subprocess.run(['kicad-cli', 'pcb', 'drc', '--format', 'json', '--severity-all',
                                      '--output', str(report), str(path)], env=env,
                                     capture_output=True, timeout=30)
                self.assertEqual(run.returncode, 0, run.stderr.decode(errors='replace'))
                data = json.loads(report.read_bytes())
                self.assertEqual(data['unconnected_items'], [])
                return {item['uuid'] for finding in data['violations'] if finding['type']=='track_dangling'
                        for item in finding['items']}
            self.assertIn(weld.uuid, observe('before'))
            modification.split_generated_t_junctions(results, pcb, {5})
            modification.merge_collinear_segments(results, pcb, {5}, keep_input_copper=True)
            self.assertNotIn(weld.uuid, observe('after'))


if __name__ == '__main__':
    unittest.main()
