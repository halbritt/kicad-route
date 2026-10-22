"""Relaxed blocker search against the checkout's native grid kernel."""
import importlib.util
import os
from pathlib import Path
import sys
import unittest
import pickle
from copy import deepcopy
from unittest.mock import patch

import numpy as np

spec = importlib.util.spec_from_file_location('grid_router', os.environ['KERNEL_EXTENSION'])
grid = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grid)
sys.modules['grid_router'] = grid
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py_router'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from blocking_analysis import BlockingInfo, mincut_probe_order
from obstacle_cache import precompute_net_obstacles, add_net_obstacles_from_cache
from routing_config import GridRouteConfig
from synth import make_pad, make_seg, make_pcb, make_net


class BlockerResourceTests(unittest.TestCase):
    def fixture(self):
        cfg = GridRouteConfig(ripup_blocker_select='mincut', layers=['F.Cu'], grid_step=1., track_width=.1,
                              clearance=.1, via_size=.3, via_drill=.15,
                              max_iterations=1000, max_probe_iterations=1000)
        pcb = make_pcb(nets={1: make_net(1, 'target'), 2: make_net(2, 'wall')},
            pads_by_net={1: [make_pad(1, 1, 3), make_pad(1, 7, 3)]},
            segments=[make_seg(4, 0, 4, 6, net_id=2, width=.1)])
        data = precompute_net_obstacles(pcb, 2, cfg)
        board = grid.GridObstacleMap(1)
        for x in range(-1, 10):
            for y in (-1, 7):
                board.add_static_blocked_cell(x, y, 0)
        for y in range(0, 7):
            for x in (-1, 9):
                board.add_static_blocked_cell(x, y, 0)
        add_net_obstacles_from_cache(board, data)
        blocker = BlockingInfo(2, 'wall', 1, 1, 0, 1, 0, 0, '')
        return pcb, cfg, board, {2: data}, [blocker]

    def test_span_only_wall_is_crossed_by_real_search(self):
        pcb, cfg, board, cache, blockers = self.fixture()
        self.assertEqual(len(cache[2].blocked_cells), 0)
        self.assertGreater(len(cache[2].blocked_cell_spans), 0)
        before = board.get_stats()
        self.assertEqual(mincut_probe_order(pcb, cfg, board, 1, blockers, cache,
            routed_results={2: {"new_segments": pcb.segments[:], "new_vias": []}}), ([2], True))
        self.assertEqual(board.get_stats(), before)
        self.assertTrue(board.is_blocked(4, 3, 0))

    def run_probe(self, fixture, **kwargs):
        pcb, cfg, board, cache, blockers = fixture
        routes = kwargs.pop('routed_results',
            {2: {'new_segments': pcb.segments[:], 'new_vias': pcb.vias[:]}})
        before = pickle.dumps((pcb, cfg, cache, routes))
        stats = board.get_stats()
        from obstacle_cache import remove_net_obstacles_from_cache

        def membership(model):
            return [(model.is_blocked(x, y, li), model.is_via_blocked_rung(x, y, 0))
                    for li in range(len(cfg.layers)) for x in range(-2, 11) for y in range(-2, 9)]

        def depleted():
            clone = board.clone_fresh()
            for contribution in cache.values():
                remove_net_obstacles_from_cache(clone, contribution)
            return clone.get_stats(), membership(clone)

        hard_before, depleted_before = membership(board), depleted()
        diagnostics = {}
        result = mincut_probe_order(pcb, cfg, board, 1, blockers, cache,
                    routed_results=routes, diagnostics=diagnostics, **kwargs)
        self.assertEqual(pickle.dumps((pcb, cfg, cache, routes)), before)
        self.assertEqual(board.get_stats(), stats)
        self.assertEqual(membership(board), hard_before)
        self.assertEqual(depleted(), depleted_before)
        return result, diagnostics

    def test_explicit_cells_match_span_search(self):
        fixture = self.fixture()
        from obstacle_cache import _cache_cells_and_vias
        data = fixture[3][2]
        cells, vias = _cache_cells_and_vias(data)
        data.blocked_cells = cells.astype(np.int32)
        data.blocked_vias = vias.astype(np.int32)
        data.blocked_cell_spans = np.empty((0, 4), dtype=np.int32)
        data.blocked_via_spans = np.empty((0, 3), dtype=np.int32)
        self.assertEqual(self.run_probe(fixture)[0], ([2], True))

    def test_documented_point_one_grid_span_reproduction(self):
        cfg = GridRouteConfig(ripup_blocker_select='mincut', layers=['F.Cu', 'B.Cu'], grid_step=.1,
                              track_width=.2, clearance=.2, via_size=.6, via_drill=.3)
        pcb = make_pcb(nets={1: make_net(1, 'target'), 2: make_net(2, 'wall')},
            pads_by_net={1: [make_pad(1, 2, 1), make_pad(1, 2, 3)]},
            segments=[make_seg(1, 2, 3, 2, width=.2, net_id=2)])
        data = precompute_net_obstacles(pcb, 2, cfg)
        from obstacle_cache import _cache_cells_and_vias
        self.assertEqual(len(data.blocked_cells), 0)
        self.assertEqual(len(data.blocked_cell_spans), 27)
        self.assertEqual(len(_cache_cells_and_vias(data)[0]), 185)
        board = grid.GridObstacleMap(2)
        for li in range(2):
            for x in range(9, 32):
                for y in range(9, 32):
                    if li == 1 or x in (9, 31) or y in (9, 31):
                        board.add_static_blocked_cell(x, y, li)
        add_net_obstacles_from_cache(board, data)
        blocker = BlockingInfo(2, 'wall', 1, 1, 0, 1, 0, 0, '')
        self.assertEqual(mincut_probe_order(pcb, cfg, board, 1, [blocker], {2: data},
            routed_results={2: {'new_segments': pcb.segments[:], 'new_vias': []}}), ([2], True))

    def test_fixed_pad_wall_survives_victim_lift(self):
        fixture = self.fixture()
        pcb, cfg, board, cache, _ = fixture
        pcb.pads_by_net[2] = [make_pad(2, 4, 3, size_x=.1, size_y=8)]
        from obstacle_cache import remove_net_obstacles_from_cache
        remove_net_obstacles_from_cache(board, cache[2])
        cache[2] = precompute_net_obstacles(pcb, 2, cfg)
        add_net_obstacles_from_cache(board, cache[2])
        result, diag = self.run_probe(fixture)
        self.assertEqual(result, ([], False))
        self.assertEqual(diag['reason'], 'no_path_or_budget')

    def test_unscheduled_same_net_branch_stays_hard(self):
        fixture = self.fixture()
        pcb, cfg, board, cache, _ = fixture
        original = pcb.segments[0]
        pcb.segments.append(make_seg(4, 0, 4, 6, net_id=2, width=.1))
        from obstacle_cache import remove_net_obstacles_from_cache
        remove_net_obstacles_from_cache(board, cache[2])
        cache[2] = precompute_net_obstacles(pcb, 2, cfg)
        add_net_obstacles_from_cache(board, cache[2])
        self.assertEqual(self.run_probe(fixture, routed_results={2: {
            'new_segments': [original], 'new_vias': []}})[0], ([], False))

    def test_each_decoded_view_is_reused_without_losing_duplicate_contributions(self):
        from collections import Counter
        import obstacle_cache
        fixture = self.fixture()
        pcb, cfg, board, cache, _ = fixture
        scheduled = pcb.segments[0]
        obstacle_cache.remove_net_obstacles_from_cache(board, cache[2])
        pcb.segments.append(deepcopy(scheduled))
        cache[2] = precompute_net_obstacles(pcb, 2, cfg)
        add_net_obstacles_from_cache(board, cache[2])
        route = {2: {'new_segments': [scheduled], 'new_vias': []}}
        decoded = []
        expand = obstacle_cache._cache_cells_and_vias

        def track_decode(data):
            decoded.append(data)
            return expand(data)

        with patch.object(obstacle_cache, '_cache_cells_and_vias', side_effect=track_decode):
            result, diagnostics = self.run_probe(fixture, routed_results=route)
        self.assertEqual(result, ([], False))
        self.assertEqual(diagnostics['reason'], 'no_path_or_budget')
        self.assertEqual(Counter(map(id, decoded)), {id(data): 1 for data in decoded})
        self.assertLessEqual(len(decoded), 3)  # original, rebuilt, survivor

        # The same unique footprint with one missing contribution is stale:
        # validating unique ownership instead of the multiset must not pass.
        cache[2].blocked_cell_spans = cache[2].blocked_cell_spans[:
            len(cache[2].blocked_cell_spans) // 2].copy()
        self.assertEqual(self.run_probe(fixture, routed_results=route)[1]['reason'],
                         'cache_geometry_mismatch')

    def test_diagonal_offgrid_shared_branch_remains_hard(self):
        fixture = self.fixture()
        pcb, cfg, board, cache, _ = fixture
        from obstacle_cache import remove_net_obstacles_from_cache
        remove_net_obstacles_from_cache(board, cache[2])
        scheduled = make_seg(2.13, -1.2, 6.27, 7.2, net_id=2, width=.2)
        pcb.segments = [scheduled, deepcopy(scheduled)]
        cache[2] = precompute_net_obstacles(pcb, 2, cfg)
        add_net_obstacles_from_cache(board, cache[2])
        self.assertEqual(self.run_probe(fixture, routed_results={2: {
            'new_segments': [scheduled], 'new_vias': []}})[0], ([], False))

    def test_overlapping_owners_are_both_attributed(self):
        fixture = self.fixture()
        pcb, cfg, board, cache, blockers = fixture
        twin = make_seg(4, 0, 4, 6, net_id=3, width=.1)
        pcb.segments.append(twin)
        pcb.nets[3] = make_net(3, 'other')
        cache[3] = precompute_net_obstacles(pcb, 3, cfg)
        add_net_obstacles_from_cache(board, cache[3])
        blockers.append(BlockingInfo(3, 'other', 1, 1, 0, 1, 0, 0, ''))
        routes = {nid: {'new_segments': [seg], 'new_vias': []}
                  for nid, seg in zip((2, 3), pcb.segments)}
        self.assertEqual(self.run_probe(fixture, routed_results=routes)[0], ([2, 3], True))

    def test_base_wall_and_blocked_source_remain_hard(self):
        for cell in ((4, 3, 0), (1, 3, 0)):
            with self.subTest(cell=cell):
                fixture = self.fixture()
                if cell[0] == 4:
                    for y in range(7):
                        fixture[2].add_static_blocked_cell(4, y, 0)
                else:
                    fixture[2].add_static_blocked_cell(*cell)
                self.assertEqual(self.run_probe(fixture)[0], ([], False))

    def test_locked_victim_is_never_relaxed(self):
        fixture = self.fixture()
        fixture[0].segments[0].locked = True
        self.assertEqual(self.run_probe(fixture)[0], ([], False))

    def test_missing_recipe_and_stale_geometry_are_unknown(self):
        fixture = self.fixture()
        del fixture[3][2]._probe_recipe
        self.assertEqual(self.run_probe(fixture)[1]['reason'], 'unbound_stamp_recipe')
        fixture = self.fixture()
        fixture[0].segments[0].width = 3.
        self.assertEqual(self.run_probe(fixture)[1]['reason'], 'cache_geometry_mismatch')

    def test_budget_and_native_exception_preserve_sources(self):
        fixture = self.fixture()
        fixture[1].max_probe_iterations = 1
        # Refresh recipe after an intentional configuration change.
        fixture[3][2] = precompute_net_obstacles(fixture[0], 2, fixture[1])
        self.assertEqual(self.run_probe(fixture)[1]['reason'], 'no_path_or_budget')
        fixture = self.fixture()
        with patch.object(grid, 'GridRouter', side_effect=RuntimeError('injected native failure')):
            diag = self.run_probe(fixture)[1]
            self.assertEqual(diag['reason'], 'probe_error')
            self.assertEqual(diag['error_type'], 'RuntimeError')
            self.assertIn('injected native failure', diag['error'])

    def test_pair_live_provider_and_missing_membership_are_unknown(self):
        fixture = self.fixture()
        self.assertEqual(self.run_probe(fixture, diff_pair_by_net_id={2: object()})[1]['reason'],
                         'unsupported_repair_unit')
        fixture = self.fixture()
        fixture[0].exact_fill_provider = 'live-provider'
        self.assertEqual(self.run_probe(fixture)[1]['reason'], 'live_board_provider')
        fixture = self.fixture()
        self.assertEqual(self.run_probe(fixture, routed_results=None)[1]['reason'],
                         'missing_route_membership')

    def test_via_only_owner_is_found_by_multilayer_search(self):
        cfg = GridRouteConfig(ripup_blocker_select='mincut', layers=['F.Cu', 'B.Cu'],
            grid_step=1., track_width=.1, clearance=.1, via_size=.3, via_drill=.15,
            via_proximity_cost=0, max_iterations=1000, max_probe_iterations=1000)
        pcb = make_pcb(nets={1: make_net(1, 'target'), 2: make_net(2, 'via wall')},
            pads_by_net={1: [make_pad(1, 1, 3), make_pad(1, 7, 3, layers=['B.Cu'])]},
            segments=[make_seg(4, 0, 4, 6, layer='In1.Cu', net_id=2, width=.1)])
        board = grid.GridObstacleMap(2)
        for li in range(2):
            for x in range(-1, 10):
                for y in range(-1, 8):
                    allowed = y == 3 and ((li == 0 and 1 <= x <= 4)
                                         or (li == 1 and 4 <= x <= 7))
                    if not allowed:
                        board.add_static_blocked_cell(x, y, li)
        data = precompute_net_obstacles(pcb, 2, cfg)
        self.assertEqual(len(data.blocked_cells) + len(data.blocked_cell_spans), 0)
        add_net_obstacles_from_cache(board, data)
        blockers = [BlockingInfo(2, 'via wall', 1, 0, 1, 1, 0, 0, '')]
        self.assertEqual(self.run_probe((pcb, cfg, board, {2: data}, blockers))[0], ([2], True))

    def test_human_protection_and_wide_tracks_are_unknown(self):
        fixture = self.fixture()
        fixture[0]._protection_map_memo = {'wall': 'human'}
        self.assertEqual(self.run_probe(fixture)[0], ([], False))
        fixture = self.fixture()
        fixture[1].net_track_widths = {1: 2.}
        self.assertEqual(self.run_probe(fixture)[1]['reason'], 'swept_track_margin')

    def test_resource_limit_and_stale_own_cache_are_unknown(self):
        fixture = self.fixture()
        fixture[3][2].blocked_cell_spans = np.array([[4, -2147483648, 2147483647, 0]], dtype=np.int32)
        pcb, cfg, board, cache, blockers = fixture
        diagnostics = {}
        self.assertEqual(mincut_probe_order(pcb, cfg, board, 1, blockers, cache,
            routed_results={2: {'new_segments': pcb.segments[:], 'new_vias': []}},
            diagnostics=diagnostics), ([], False))
        self.assertEqual(diagnostics['reason'], 'resource_expansion_limit')
        fixture = self.fixture()
        pcb, cfg, board, cache, _ = fixture
        cache[1] = precompute_net_obstacles(pcb, 1, cfg)
        add_net_obstacles_from_cache(board, cache[1])
        pcb.pads_by_net[1][0].global_x = 2.
        self.assertEqual(self.run_probe(fixture)[1]['reason'], 'cache_geometry_mismatch')

    def test_via_only_prices_work_when_proximity_multiplier_is_zero(self):
        board = grid.GridObstacleMap(2)
        rows = np.array([[x, 0, 100000] for x in range(4)], dtype=np.int32)
        board.set_probe_via_costs(0, rows)
        router = grid.GridRouter(via_cost=10, h_weight=1., via_proximity_cost=0)
        path, _, _ = router.route_multi(board, [(0, 0, 0)], [(4, 0, 1)], 1000)
        jumps = [p for a, p in zip(path, path[1:]) if a[2] != p[2]]
        self.assertEqual(jumps, [(4, 0, 1)])
        self.assertEqual(board.get_probe_via_cost(0, 0, 1), 0)
        board.set_probe_via_costs(0, rows)
        self.assertEqual(board.get_probe_via_cost(0, 0, 0), 100000)
        board.set_probe_via_costs(1, np.array([[x, 0, 100000] for x in range(1, 5)], dtype=np.int32))
        path, _, _ = router.route_multi(board, [(0, 0, 0)], [(4, 0, 1)], 1000, via_rung=1)
        self.assertEqual([p for a, p in zip(path, path[1:]) if a[2] != p[2]], [(0, 0, 1)])
        board.add_free_via(0, 0)
        path, _, _ = router.route_multi(board, [(0, 0, 0)], [(4, 0, 1)], 1000)
        self.assertEqual([p for a, p in zip(path, path[1:]) if a[2] != p[2]], [(0, 0, 1)])

    def test_empty_selected_rung_does_not_fall_back_after_lift(self):
        board = grid.GridObstacleMap(2)
        board.add_blocked_vias_batch(np.array([[4, 0]], dtype=np.int32))
        board.add_blocked_vias_rung_batch(1, np.array([[2, 0]], dtype=np.int32))
        board.remove_blocked_vias_rung_batch(1, np.array([[2, 0]], dtype=np.int32))
        self.assertTrue(board.is_via_blocked_rung(4, 0, 1))
        board.set_probe_via_costs(1, np.empty((0, 3), dtype=np.int32))
        self.assertFalse(board.is_via_blocked_rung(4, 0, 1))
        self.assertTrue(board.is_via_blocked_rung(4, 0, 0))



if __name__ == '__main__':
    unittest.main()
