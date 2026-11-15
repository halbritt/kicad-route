"""Retained retries need pad progress; reset retries keep their full-pass metric."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
from contextlib import ExitStack
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py_router'))
import phase3_routing as phase3
from blocking_analysis import BlockingInfo
from routing_config import GridRouteConfig
from routing_state import RoutingState


class RetryProgressTests(unittest.TestCase):
    def test_retained_retry_requires_pad_gain_and_reset_retry_keeps_full_pass_metric(self):
        # Outcomes: fewer searches only, pad gain only, both, neither.
        outcomes = ((False, 1), (True, 4), (True, 0), (False, 4))
        # The 25/26 boundary and plane ownership independently select retention.
        cases = (
            (False, 3, False, (True, False, True, False)),
            (False, 26, False, (True, False, True, False)),
            (True, 3, False, (True, True, True, False)),
            (True, 25, False, (True, True, True, False)),
            (True, 26, True, (False, True, True, False)),
            (True, 30, True, (False, True, True, False)),
        )
        for planes, edges, retained, accepted in cases:
            for outcome, expected in zip(outcomes, accepted):
                with self.subTest(planes=planes, edges=edges, outcome=outcome):
                    self.check_retry_progress(planes, edges,
                                              (*outcome, retained, expected))

    def check_retry_progress(self, planes, edges, case):
        connects_pad, retry_failed, retained, accepted = case
        layers = ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'] if planes else ['F.Cu', 'B.Cu']
        zones = [SimpleNamespace(layers=['In1.Cu', 'In2.Cu'],
                                 polygon=[(0, 0), (10, 0), (10, 10), (0, 10)], net_id=9)] if planes else []
        pcb = SimpleNamespace(nets={1: SimpleNamespace(name='tree', pads=[None] * (edges + 1)),
                                   2: SimpleNamespace(name='blocker', pads=[None, None])},
                              zones=zones, segments=[], vias=[],
                              board_info=SimpleNamespace(board_bounds=(0, 0, 10, 10)))
        config = GridRouteConfig(layers=layers, max_rip_up_count=1, max_iterations=120_000)
        state = RoutingState(pcb, config)
        trunk, branch = object(), object()
        original = dict(new_segments=[trunk, branch], new_vias=[], routed_pad_indices=set(range(edges)),
                        mst_edges=[(i, i + 1, 1.0) for i in range(edges)],
                        tap_edges_routed=edges - 1, tap_edges_failed=4, failed_pads_info=[{'pad_idx': edges}],
                        failed_edge_blocking={(2, 3): ([(0, 0, 0)], (3, 0))})
        # A fresh pass can make fewer unsuccessful searches without connecting a pad.
        retried = dict(original, tap_edges_failed=retry_failed,
                       failed_pads_info=[] if connects_pad else [{'pad_idx': edges}],
                       routed_pad_indices=set(range(edges + 1 if connects_pad else edges)))
        saved = dict(new_segments=[object()], new_vias=[])
        blocker = BlockingInfo(2, 'blocker', 1, 1, 0, 1, 1, 0, '')
        with ExitStack() as stack:
            stack.enter_context(patch.object(phase3, 'analyze_frontier_blocking', return_value=[blocker]))
            stack.enter_context(patch.object(phase3, 'LEG_RIP_ENABLED', False))
            stack.enter_context(patch.object(phase3, 'rip_up_net', return_value=(saved, [2], True)))
            stack.enter_context(patch.object(phase3, 'build_single_ended_obstacles', return_value=(None, None)))
            taps = stack.enter_context(patch.object(phase3, 'route_multipoint_taps', return_value=retried))
            restore = stack.enter_context(patch.object(phase3, 'restore_net'))
            reroute = stack.enter_context(patch.object(phase3, '_reroute_phase3_ripped_nets', return_value=[]))
            stack.enter_context(patch.object(phase3, '_phase3_abandon_retry', return_value=(False, '')))
            result = phase3.try_phase3_ripup(
                1, original, original['failed_edge_blocking'], [trunk], [],
                pcb, config, state, [1, 2], [], [], {}, {2: saved}, {}, [], {}, {}, None, None, [])
        taps.assert_called_once()
        retry_config, retry_input = taps.call_args.args[2], taps.call_args.args[4]
        self.assertEqual(retry_input['new_segments'], [trunk, branch] if retained else [trunk])
        self.assertIsNot(retry_input['new_segments'], original['new_segments'])
        self.assertEqual(retry_input['routed_pad_indices'],
                         original['routed_pad_indices'] if retained else {0, 1})
        self.assertEqual(retry_input['tap_edges_routed'], edges - 1 if retained else 1)
        self.assertEqual(retry_input['tap_edges_failed'], 0)
        self.assertEqual(retry_config.max_iterations, 40_000 if retained else 120_000)
        self.assertEqual(config.max_iterations, 120_000)
        if accepted:
            self.assertIs(result, retried)
            reroute.assert_called_once()
            restore.assert_not_called()
        else:
            self.assertIsNone(result)
            reroute.assert_not_called()
            restore.assert_called_once()
            self.assertEqual(restore.call_args.args[:4], (2, saved, [2], True))


if __name__ == '__main__':
    unittest.main()
