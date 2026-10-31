"""Generated pair connectors must not double back over their terminal path."""
import copy
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/kicad_route/router/py_router'))
from diff_pair_routing import _float_path_to_geometry
from routing_config import GridRouteConfig


LAYERS = ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']


def nm(point):
    return tuple(int(Decimal(format(v, '.6f')) * 1_000_000) for v in point)


def edges(segments):
    return [(nm((s.start_x, s.start_y)), nm((s.end_x, s.end_y))) for s in segments]


class PairTerminalRetraceTests(unittest.TestCase):
    def emit(self, path, *, start=None, end=None, net=1, sign=1,
             src_extension=0, tgt_extension=0, widths=None, omit=False,
             src_dir=(0, -1), tgt_dir=(0, 1)):
        before = copy.deepcopy(path)
        config = GridRouteConfig(track_width=.25, via_size=.6, via_drill=.3,
                                 debug_lines=True, layer_widths=widths or {})
        result = _float_path_to_geometry(
            path, net, start, end, sign, src_dir, tgt_dir,
            src_extension, tgt_extension, config, LAYERS,
            omit_connectors=omit)
        self.assertEqual(path, before, 'caller-owned path must not change')
        return result

    def test_observed_matching_and_transition_retraces_keep_both_terminal_directions(self):
        # Matching constructor trace and transition native DRC, in mm.
        cases = [((75., 69.90625), (75., 68.8), (75., 68.89926406871193),
                  (53.75073593128807, 47.65)),
                 ((75.6, 69.90625), (75.6, 68.8), (75.6, 68.824264),
                  (76.3, 68.124264))]
        for terminal, overshoot, junction, far in cases:
            for reverse in (False, True):
                for net, sign in ((1, -1), (2, 1)):
                    with self.subTest(terminal=terminal, reverse=reverse, net=net):
                        path = [(*p, 0) for p in (overshoot, junction, far)]
                        expected = [(nm(terminal), nm(junction)), (nm(junction), nm(far))]
                        options = dict(start=terminal)
                        if reverse:
                            path.reverse()
                            expected = [(b, a) for a, b in reversed(expected)]
                            options = dict(end=terminal)
                        segments, vias, connectors = self.emit(path, net=net, sign=sign, **options)
                        self.assertEqual(edges(segments), expected)
                        self.assertEqual(vias, [])
                        self.assertEqual({(s.width, s.layer, s.net_id) for s in segments},
                                         {(.25, 'F.Cu', net)})
                        expected_connector = expected[-1] if reverse else expected[0]
                        self.assertEqual([(nm(a), nm(b)) for a, b in connectors],
                                         [expected_connector])

    def test_retracing_prefix_can_contain_more_than_one_step(self):
        path = [(4., 0., 0), (3., 0., 0), (2., 0., 0), (2., 2., 0)]
        segments, vias, _ = self.emit(path, start=(0., 0.))
        self.assertEqual(edges(segments), [((0, 0), (2000000, 0)),
                                          ((2000000, 0), (2000000, 2000000))])
        self.assertEqual(vias, [])

    def test_monotonic_and_one_nanometre_off_line_paths_are_preserved(self):
        for path in [[(2., 0., 0), (4., 0., 0), (4., 2., 0)],
                     [(4., 0., 0), (3., .000001, 0), (3., 2., 0)]]:
            with self.subTest(path=path):
                segments, _, _ = self.emit(path, start=(0., 0.))
                points = [(0., 0.)] + [p[:2] for p in path]
                self.assertEqual(edges(segments), [(nm(a), nm(b)) for a, b in zip(points, points[1:])])

    def test_missing_and_omitted_connectors_leave_the_coupled_path_alone(self):
        path = [(4., 0., 0), (3., 0., 0), (3., 2., 0)]
        for options in ({}, dict(start=(0., 0.), omit=True)):
            with self.subTest(options=options):
                segments, vias, connectors = self.emit(path, **options)
                self.assertEqual(edges(segments), [((4000000, 0), (3000000, 0)),
                                                  ((3000000, 0), (3000000, 2000000))])
                self.assertEqual(vias, [])
                self.assertEqual(connectors, [])

    def test_distinct_connector_and_path_widths_are_preserved(self):
        path = [(4., 0., 0), (3., 0., 0), (3., 2., 0)]
        segments, _, _ = self.emit(path, start=(0., 0.), widths={'F.Cu': .3})
        self.assertEqual(edges(segments), [((0, 0), (4000000, 0)),
                                          ((4000000, 0), (3000000, 0)),
                                          ((3000000, 0), (3000000, 2000000))])
        self.assertEqual([s.width for s in segments], [.25, .3, .3])

    def test_explicit_extensions_are_preserved(self):
        path = [(4., 0., 0), (3., 0., 0), (3., 2., 0)]
        segments, _, _ = self.emit(path, start=(0., 0.), src_extension=1, src_dir=(1, 0))
        self.assertEqual(edges(segments), [((0, 0), (1000000, 0)),
                                          ((1000000, 0), (4000000, 0)),
                                          ((4000000, 0), (3000000, 0)),
                                          ((3000000, 0), (3000000, 2000000))])

    def test_layer_transition_at_the_terminal_is_not_moved(self):
        path = [(4., 0., 0), (4., 0., 1), (3., 0., 1), (3., 2., 1)]
        segments, vias, _ = self.emit(path, start=(0., 0.))
        self.assertEqual(edges(segments), [((0, 0), (4000000, 0)),
                                          ((4000000, 0), (3000000, 0)),
                                          ((3000000, 0), (3000000, 2000000))])
        self.assertEqual([s.layer for s in segments], ['F.Cu', 'In1.Cu', 'In1.Cu'])
        self.assertEqual([(v.x, v.y, v.size, v.drill, v.net_id) for v in vias],
                         [(4., 0., .6, .3, 1)])

    def test_via_after_retracing_stays_at_its_original_position(self):
        path = [(4., 0., 0), (2., 0., 0), (2., 0., 1), (2., 2., 1)]
        segments, vias, _ = self.emit(path, start=(0., 0.))
        self.assertEqual(edges(segments), [((0, 0), (2000000, 0)),
                                          ((2000000, 0), (2000000, 2000000))])
        self.assertEqual([s.layer for s in segments], ['F.Cu', 'In1.Cu'])
        self.assertEqual([(v.x, v.y, v.size, v.drill, v.net_id) for v in vias],
                         [(2., 0., .6, .3, 1)])


if __name__ == '__main__':
    unittest.main()
