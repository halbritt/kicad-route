"""Small owned-extension invariants; no pcbnew, fixture corpus or network."""
from collections import Counter
import heapq
import importlib.util
import os
from pathlib import Path
import random
import sys
import unittest

import numpy as np

# The gate builds this checkout; never import a possibly stale installed module.
spec = importlib.util.spec_from_file_location('grid_router', os.environ['KERNEL_EXTENSION'])
grid = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grid)
sys.modules['grid_router'] = grid
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py_router'))
DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]


def cells(values):
    return np.ascontiguousarray(values, dtype=np.int32).reshape((-1, 3))


class ObstacleStateTests(unittest.TestCase):
    def test_refcounts_merge_overlap_growth_and_overflow(self):
        maps = [grid.GridObstacleMap(2), grid.GridObstacleMap(2)]
        counts = [Counter(), Counter()]
        # Force left/up remapping, right/down growth, capped overflow and layers.
        sites = [(0, 0, 0), (-300, -280, 1), (600, 450, 0),
                 (1000000, -1000000, 1), (-1000000, 1000000, 0)]
        def check():
            for model, expected in zip(maps, counts):
                for x, y, layer in sites:
                    for dx, dy in ((0, 0), (1, 0), (-1, 0)):
                        self.assertEqual(model.is_blocked(x+dx, y+dy, layer),
                                         expected[x+dx, y+dy, layer] > 0)
        for cell in sites + sites[:2]:
            maps[0].add_blocked_cell(*cell)
            counts[0][cell] += 1
            check()
        maps[1].add_blocked_cells_batch(cells(sites[1:]))
        counts[1].update(sites[1:])
        maps[0].merge_blocked_from(maps[1])
        counts[0].update(counts[1])
        check()
        rng = random.Random(497)
        for _ in range(80):
            index = rng.randrange(2)
            cell = rng.choice(sites)
            if rng.randrange(2):
                maps[index].add_blocked_cells_batch(cells([cell, cell]))
                counts[index][cell] += 2
            else:
                maps[index].remove_blocked_cells_batch(cells([cell]))
                counts[index][cell] = max(0, counts[index][cell] - 1)
            check()
        for model, expected in zip(maps, counts):
            model.remove_blocked_cells_batch(cells(list(expected.elements())))
            expected.clear()
        check()

    def test_static_copper_survives_dynamic_removal_and_clone(self):
        model = grid.GridObstacleMap(2)
        model.add_static_blocked_cell(7, 9, 1)
        model.add_blocked_cell(7, 9, 1)
        clone = model.clone()
        model.remove_blocked_cells_batch(cells([(7, 9, 1)]))
        self.assertTrue(model.is_blocked(7, 9, 1))
        self.assertTrue(clone.is_blocked(7, 9, 1))
        self.assertTrue(model.is_blocked(0, 0, 2))


def tables(n=7, layers=2):
    band = np.ones((n, n), dtype=bool)
    bad = np.zeros((layers, n, n), dtype=bool)
    vbad = np.ones((4, n, n), dtype=bool)
    zero = np.zeros((n, n), dtype=float)
    dv = np.full((1, n, n), np.inf)
    return band, bad, vbad, zero, dv


def lane(t, lays=(0,), start=(0, 3), end=(6, 3)):
    band, bad, vbad, zero, dv = t
    return grid.lane_search(band, bad, vbad, zero, zero, dv, start, end, 0, 0,
        list(lays), [(-1., 1.)] * len(lays), -1., 1., 1., 1., 0., 0., 0., 0.,
        [1.] * 8, [True] * 8, [True] * 8, [True] * 8, [True] * 8, False)[0]


def pair(t, lays=(0,), starts=((0, 3, 0),), end=(6, 3, 0), rt=0, st=0, pr=0):
    band, bad, vbad, zero, dv = t
    return grid.pair_search(band, bad, vbad, zero, zero, None, None, dv,
        list(starts), [end], False, list(lays), [(-1., 1.)] * len(lays),
        [-1.] * 8, [0.] * 8, 1., 1., 1., 0., 0., 0., 0., 0., [1.] * 8,
        8, rt, st, pr, False, [(0, 0)] * 8, None)[0]


def oracle(t, paired, lays=(0,), rt=0, st=0, pr=0):
    """Exhaustive Dijkstra on the finite pose graph, with zero heuristic.

    This models the bounded unit-cost fixtures only, independently of the
    extension's heap, heuristic, pruning and numpy indexing implementation.
    """
    band, bad, vbad, _, dv = t
    start = (0, 3, 0, 0, pr if paired else 0, 0)
    cost, previous = {start: 0}, {}
    queue = [(0, start)]
    while queue:
        distance, state = heapq.heappop(queue)
        if cost[state] != distance:
            continue
        x, y, heading, stage, owed, taken = state
        if (x, y) == (6, 3) and stage == len(lays)-1 and (not paired or taken >= pr):
            path = [state]
            while path[-1] in previous:
                path.append(previous[path[-1]])
            return list(reversed(path))
        edges = []
        if stage < len(lays)-1 and not vbad[heading % 4, x, y] and not bad[lays[stage+1], x, y] and dv[stage, x, y] <= 1:
            if not paired or (owed == 0 and taken >= st):
                edges.append(((x, y, heading, stage+1, st if paired else 0, 0), dv[stage, x, y]))
        for direction, (dx, dy) in enumerate(DIRS):
            angle = min((heading-direction) % 8, (direction-heading) % 8)
            if angle > (1 if paired else 2) or (paired and angle and owed):
                continue
            nx, ny = x+dx, y+dy
            if not (0 <= nx < band.shape[0] and 0 <= ny < band.shape[1]) or not band[nx, ny]:
                continue
            if bad[lays[stage], nx, ny] and (paired or (nx, ny) != (6, 3)):
                continue
            if paired and (nx, ny) == (6, 3) and direction != 0:
                continue
            ns = (nx, ny, direction, stage,
                  (rt if angle else max(0, owed-1)) if paired else 0,
                  (1 if angle else min(8, taken+1)) if paired else 0)
            edges.append((ns, 1))
        for ns, weight in edges:
            nd = distance + weight
            if nd < cost.get(ns, float('inf')):
                cost[ns], previous[ns] = nd, state
                heapq.heappush(queue, (nd, ns))
    return None


class SearchTests(unittest.TestCase):
    def test_deterministic_obstacle_paths_match_exhaustive_pose_oracle(self):
        for paired, search in ((False, lane), (True, pair)):
            for hole in (1, 2, 4, 5):
                with self.subTest(paired=paired, hole=hole):
                    t = tables()
                    t[1][0, 3, :] = True
                    t[1][0, 3, hole] = False
                    expected = oracle(t, paired)
                    actual = search(t)
                    self.assertEqual(actual, None if expected is None else
                                     [s if paired else s[:4] for s in expected])
                    self.assertEqual(search(t), actual)
                    if actual:
                        self.assertTrue(all(not t[1][0, s[0], s[1]] for s in actual))

    def test_no_path_and_empty_search(self):
        t = tables()
        t[0][:] = False
        for search in (lane, pair):
            self.assertIsNone(search(t))
        self.assertIsNone(pair(tables(), starts=()))

    def test_required_layers_via_heading_and_straight_constraints(self):
        t = tables()
        t[2][:, 3, 3] = False
        t[4][0, 3, 3] = 0.
        for paired, search in ((False, lane), (True, pair)):
            kw = dict(rt=1, st=1, pr=1) if paired else {}
            path = search(t, lays=(0, 1), **kw)
            expected = oracle(t, paired, lays=(0, 1), **kw)
            self.assertEqual(path, [s if paired else s[:4] for s in expected])
            dives = [(a, b) for a, b in zip(path, path[1:]) if a[3] != b[3]]
            self.assertEqual(len(dives), 1)
            self.assertEqual(dives[0][0][:3], dives[0][1][:3])
            self.assertEqual({s[3] for s in path}, {0, 1})
            t[2][:] = True
            self.assertIsNone(search(t, lays=(0, 1), **kw))
            t[2][:, 3, 3] = False


if __name__ == '__main__':
    unittest.main()
