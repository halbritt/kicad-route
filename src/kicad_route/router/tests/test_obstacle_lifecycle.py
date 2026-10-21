"""KICADROUTE-86: a tap call lends its obstacle map, never owns it."""
import contextlib
import io
import os
import signal
import tempfile
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py_router'))
from kicad_parser import BoardInfo, Net, PCBData, Pad, Segment, Via
from obstacle_map import GridObstacleMap
from routing_config import GridRouteConfig
import single_ended_routing as routing


def board():
    return PCBData(BoardInfo({0: 'F.Cu', 31: 'B.Cu'}, ['F.Cu', 'B.Cu']),
                   {1: Net(1, 'N')}, {}, [], [], {1: []})


def config():
    return GridRouteConfig(layers=['F.Cu', 'B.Cu'], grid_step=0.1,
                           track_width=0.2, clearance=0.2,
                           via_size=0.6, via_drill=0.3)


def result():
    return {'iterations': 0, 'new_segments': [],
            'new_vias': [Via(2, 2, 0.6, 0.3, ['F.Cu', 'B.Cu'], 1)],
            'multipoint_pad_info': [], 'routed_pad_indices': set(), 'mst_edges': []}


def tap_case(blocked=False):
    pcb = board()
    cfg = config()
    cfg.max_iterations = 100
    pads = [Pad('U'+str(i), '1', x, y, 0, 0, .4, .4, 'circle', ['F.Cu'], 1, 'N')
            for i, (x, y) in enumerate(((1, 1), (3, 1), (3, 3)))]
    pcb.pads_by_net[1] = pcb.nets[1].pads = pads
    seg = Segment(1, 1, 3, 1, .2, 'F.Cu', 1)
    pcb.segments.append(seg)
    answer = result()
    answer.update(new_segments=[seg], new_vias=[Via(2, 1, .6, .3, ['F.Cu', 'B.Cu'], 1)],
                  multipoint_pad_info=[(round(p.global_x*10), round(p.global_y*10),
                                        0, p.global_x, p.global_y, p) for p in pads],
                  routed_pad_indices={0, 1}, mst_edges=[(0, 1, 2), (1, 2, 2)])
    obstacles = GridObstacleMap(2)
    if blocked:
        # A closed ring beyond the terminal exemption radius, on both layers.
        for x in range(20, 41):
            for y in range(20, 41):
                if x in (20, 40) or y in (20, 40):
                    for layer in (0, 1):
                        obstacles.add_static_blocked_cell(x, y, layer)
                    obstacles.add_static_blocked_via(x, y)
    return pcb, cfg, obstacles, answer


class TapMapLifecycleTests(unittest.TestCase):
    def test_stub_retry_rolls_back_real_switch_on_builder_or_route_failure(self):
        from phase3_routing import _phase3_stub_switch_retry
        from routing_state import RoutingState
        from obstacle_map import build_base_obstacle_map

        for failure in ('builder-error', 'builder-cancel', 'route-error', 'route-cancel', 'route-incomplete'):
            with self.subTest(failure=failure):
                pcb, cfg, _, main = tap_case()
                main['new_vias'] = []
                main['failed_pads_info'] = [dict(x=3, y=2.5)]
                pcb.segments.append(Segment(3, 3, 3, 2.5, .2, 'F.Cu', 1))
                state = RoutingState(pcb, cfg)
                before = [(id(s), dict(vars(s))) for s in pcb.segments]
                vias = [(id(v), dict(vars(v))) for v in pcb.vias]
                observed = []

                def build():
                    observed.append([s.layer for s in pcb.segments if s.net_id == 1])
                    self.assertIn('B.Cu', observed[-1], 'the real stub switch never ran')
                    if failure == 'builder-error':
                        raise RuntimeError('fresh obstacle build failed')
                    if failure == 'builder-cancel':
                        raise KeyboardInterrupt
                    obstacles = build_base_obstacle_map(pcb, cfg, [1], static_base=True)
                    if failure == 'route-incomplete':
                        # A closed, thick wall remains beyond both terminal
                        # exemptions; straight-line shortcuts cannot bypass it.
                        for x in range(10, 51):
                            for y in range(10, 51):
                                if x < 23 or x > 37 or y < 23 or y > 37:
                                    for layer in (0, 1):
                                        obstacles.add_static_blocked_cell(x, y, layer)
                                    obstacles.add_static_blocked_via(x, y)
                    return obstacles

                def cancel_route(frame, event, _arg):
                    if event == 'call' and frame.f_code is routing._route_multipoint_taps_impl.__code__:
                        signal.raise_signal(signal.SIGINT)

                if failure == 'route-error':
                    main['mst_edges'][1] = None  # Real tap engine raises after the switch.
                if failure == 'route-incomplete':
                    cfg.max_iterations = 1
                if failure == 'route-cancel':
                    sys.setprofile(cancel_route)
                error = KeyboardInterrupt if failure.endswith('cancel') else (RuntimeError if failure.startswith('builder') else TypeError)
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        if failure == 'route-incomplete':
                            self.assertIsNone(_phase3_stub_switch_retry(1, main, pcb, cfg, state, build, 0, 0, 0))
                        else:
                            with self.assertRaises(error):
                                _phase3_stub_switch_retry(1, main, pcb, cfg, state, build, 0, 0, 0)
                finally:
                    sys.setprofile(None)
                self.assertEqual(observed, [['F.Cu', 'B.Cu']])
                self.assertEqual([(id(s), dict(vars(s))) for s in pcb.segments], before)
                self.assertEqual([(id(v), dict(vars(v))) for v in pcb.vias], vias)
                self.assertEqual(state.all_segment_modifications, [])
                self.assertEqual(state.all_swap_vias, [])

    def test_success_removes_new_free_via_and_keeps_entry_barrel(self):
        pcb = board()
        pcb.vias.append(Via(1, 1, 0.6, 0.3, ['F.Cu', 'B.Cu'], 1))
        obstacles = GridObstacleMap(2)
        obstacles.add_free_via(10, 10)
        obstacles.add_blocked_cell(25, 25, 0)
        before = obstacles.get_stats()
        with contextlib.redirect_stdout(io.StringIO()):
            answer = routing.route_multipoint_taps(pcb, 1, config(), obstacles, result())
        self.assertIsNotNone(answer)
        self.assertTrue(obstacles.is_free_via(10, 10))
        self.assertFalse(obstacles.is_free_via(20, 20))
        self.assertEqual(obstacles.get_stats(), before)
        self.assertTrue(obstacles.is_blocked(25, 25, 0))

    def test_real_tap_success_and_failure_leave_no_search_exemptions(self):
        for blocked in (False, True):
            with self.subTest(blocked=blocked):
                pcb, cfg, obstacles, main = tap_case(blocked)
                before = obstacles.get_stats()
                with contextlib.redirect_stdout(io.StringIO()):
                    routed = routing.route_multipoint_taps(pcb, 1, cfg, obstacles, main)
                self.assertEqual(bool(routed.get('failed_pads_info')), blocked)
                self.assertEqual(obstacles.get_stats(), before)
                self.assertFalse(obstacles.is_free_via(20, 20))

    def test_unknown_entry_free_via_uses_copy_without_losing_barrels(self):
        pcb, cfg, obstacles, main = tap_case()
        obstacles.add_free_via(99, 99)  # no pad/via/main-result owns this barrel
        before = obstacles.get_stats()
        with contextlib.redirect_stdout(io.StringIO()):
            routed = routing.route_multipoint_taps(pcb, 1, cfg, obstacles, main)
        self.assertFalse(routed.get('failed_pads_info'))
        self.assertTrue(obstacles.is_free_via(99, 99))
        self.assertFalse(obstacles.is_free_via(20, 20))
        self.assertEqual(obstacles.get_stats(), before)

    def test_exception_after_via_stamp_restores_map_and_lift(self):
        pcb, cfg, obstacles, main = tap_case()
        main['mst_edges'][1] = None  # malformed edge fails AFTER via-ring stamps
        obstacles.add_blocked_cell(50, 50, 0)
        import numpy as np
        pcb._graphic_own_pad_lift = {1: np.array([[50, 50, 50, 0]], dtype=np.int32)}
        before = obstacles.get_stats()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(TypeError):
            routing.route_multipoint_taps(pcb, 1, cfg, obstacles, main)
        self.assertEqual(obstacles.get_stats(), before)
        self.assertFalse(obstacles.is_free_via(20, 20))
        self.assertTrue(obstacles.is_blocked(50, 50, 0))

    def test_bga_allowed_and_endpoint_overrides_do_not_escape(self):
        pcb = board()
        obstacles = GridObstacleMap(2)
        obstacles.set_bga_zone(50, 50, 55, 55)
        obstacles.add_allowed_cell(52, 52)
        obstacles.add_blocked_cell(60, 60, 0)
        obstacles.add_source_target_cell(60, 60, 0)
        obstacles.set_endpoint_exempt([(52, 52)], 1)
        self.assertFalse(obstacles.is_blocked(52, 52, 0))
        self.assertFalse(obstacles.is_blocked(60, 60, 0))
        self.assertFalse(obstacles.is_via_blocked(52, 52))
        with contextlib.redirect_stdout(io.StringIO()):
            routing.route_multipoint_taps(pcb, 1, config(), obstacles, result())
        self.assertTrue(obstacles.is_blocked(52, 52, 0))
        self.assertTrue(obstacles.is_blocked(60, 60, 0))
        self.assertTrue(obstacles.is_via_blocked(52, 52))

    def test_retry_rebuilds_after_mutation_and_avoids_native_clearance_collision(self):
        from phase3_routing import _phase3_stub_switch_retry
        from routing_state import RoutingState
        from obstacle_map import build_base_obstacle_map
        from output_writer import write_routed_output
        from native_lifecycle_support import write_input, native_grade
        from collections import Counter

        root = Path(os.environ.get('LIFECYCLE_EVIDENCE') or tempfile.mkdtemp(prefix='krt-lifecycle-'))
        root.mkdir(parents=True, exist_ok=True)
        grades = {}
        for fresh in (False, True):
            pcb, cfg, _, main = tap_case()
            cfg.max_iterations = 3000
            main['new_vias'] = []
            main['failed_pads_info'] = [dict(x=3, y=2.5)]
            pcb.segments.append(Segment(3, 3, 3, 2.5, .2, 'F.Cu', 1))
            stale = build_base_obstacle_map(pcb, cfg, [1], static_base=True)
            # A preceding rip/re-route moved this other net after the first map.
            for nid, layer in enumerate(cfg.layers, 2):
                pcb.nets[nid] = Net(nid, 'foreign'+str(nid))
                pcb.segments.append(Segment(3.3, 1.4, 3.3, 2.2, .2, layer, nid))
            source = root / ('fresh' if fresh else 'stale')
            source.mkdir(exist_ok=True)
            input_path = source / 'input.kicad_pcb'
            write_input(input_path, pcb, omit_segments=main['new_segments'])
            original_rules = input_path.with_suffix('.kicad_pro').read_bytes()
            baseline = native_grade(input_path)
            state = RoutingState(pcb, cfg)
            observed = []
            def build():
                observed.append([s.layer for s in pcb.segments if s.net_id == 1])
                return build_base_obstacle_map(pcb, cfg, [1], static_base=True) if fresh else stale
            with contextlib.redirect_stdout(io.StringIO()) as log:
                routed = _phase3_stub_switch_retry(1, main, pcb, cfg, state, build, 0, 0, 0)
                self.assertIsNotNone(routed)
                output = source / 'output.kicad_pcb'
                self.assertTrue(write_routed_output(str(input_path), str(output), [routed],
                    state.all_segment_modifications, state.all_swap_vias, [], [], [], pcb))
            (source / 'routing.log').write_text(log.getvalue())
            # Actual callback observes the switched stub and current other-net copper.
            self.assertEqual(observed, [['F.Cu', 'B.Cu']])
            self.assertEqual(input_path.with_suffix('.kicad_pro').read_bytes(), original_rules)
            self.assertEqual(output.with_suffix('.kicad_pro').read_bytes(), original_rules)
            grades[fresh] = native_grade(output)
            self.assertFalse(grades[fresh]['unconnected_items'])
            if fresh:
                counts = Counter(v['type'] for v in grades[fresh]['violations'])
                original = Counter(v['type'] for v in baseline['violations'])
                self.assertFalse(counts - original, counts - original)
        self.assertTrue(any(v['type'] == 'clearance' for v in grades[False]['violations']))
        self.assertFalse(any(v['type'] in {'clearance', 'shorting_items', 'tracks_crossing', 'track_width', 'hole_clearance'}
                             for v in grades[True]['violations']), grades[True]['violations'])

    def test_scaffold_observer_distinguishes_leaked_exemption_from_balanced_map(self):
        import obstacle_cache
        previous = obstacle_cache._LEDGER
        obstacle_cache._LEDGER = {}
        try:
            pcb = board()
            cfg = config()
            # A pad names the region probed by the observer.
            pcb.pads_by_net[1] = [Pad('J1', '1', 1, 1, 0, 0, .4, .4,
                                     'circle', ['F.Cu'], 1, 'N')]
            obstacles = GridObstacleMap(2)
            obstacles.add_blocked_cell(10, 10, 0)
            with contextlib.redirect_stdout(io.StringIO()) as log:
                token = obstacle_cache.ledger_scaffold_begin(obstacles)
                obstacle_cache.ledger_scaffold_end(token, obstacles, pcb, 1, cfg, {})
                token = obstacle_cache.ledger_scaffold_begin(obstacles)
                obstacles.add_source_target_cell(10, 10, 0)
                obstacle_cache.ledger_scaffold_end(token, obstacles, pcb, 1, cfg, {})
            self.assertEqual(obstacle_cache._LEDGER['scaffold'], [2, 1])
            self.assertIn('SCAFFOLD LEAK', log.getvalue())
            self.assertNotIn('skipped', log.getvalue())
        finally:
            obstacle_cache._LEDGER = previous


if __name__ == '__main__':
    unittest.main()
