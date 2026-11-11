#!/usr/bin/env python3
"""A via's hole keeps the board's copper-to-hole minimum from foreign copper.

A pad or footprint clearance override replaces the class clearance (KiCad's
order). At 0.05 mm it lets via copper sit 0.05 mm from the pad; with a 0.15 mm
annular ring the via's hole is then 0.2 mm from the pad, and KiCad grades that
as hole_clearance on a board whose minimum is 0.25 mm. The via keep-out around
a pad must cover both the via's copper and its hole.

The same holds between a via and a foreign track or via whenever the annular
ring plus the clearance is less than the hole minimum (a 0.45/0.20 via at
0.10 mm clearance against 0.25 mm): the keep-outs the per-net obstacle cache
stamps around a track and a via must cover the hole term in every direction.
"""
import os
import sys
import types

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

from kicad_parser import Pad                                    # noqa: E402
from routing_config import GridRouteConfig, GridCoord           # noqa: E402
import obstacle_map                                             # noqa: E402

FAILS = []


def check(name, cond, detail=''):
    if not cond:
        FAILS.append(name)
    print(('  PASS ' if cond else '  FAIL ') + name + (f'  {detail}' if detail else ''))


def config(hole_minimum, grid=0.025, **kw):
    rules = types.SimpleNamespace(board_min={'min_hole_clearance': hole_minimum} if hole_minimum else {},
                                  rules=[])
    return GridRouteConfig(track_width=0.2, clearance=0.2, via_size=0.6, via_drill=0.3,
                           grid_step=grid, layers=['F.Cu', 'B.Cu'], rules=rules, **kw)


def pad(local_clearance):
    return Pad(component_ref='C1', pad_number='1', global_x=10.0, global_y=10.0, local_x=0.0, local_y=0.0,
               size_x=1.016, size_y=1.016, shape='rect', layers=['F.Cu', 'F.Mask'], net_id=7,
               net_name='/A', local_clearance=local_clearance)


def nearest_free_via_gap(cfg, the_pad):
    """Edge-to-edge distance from the pad to the nearest via HOLE the keep-out allows, east of the pad."""
    cells = obstacle_map._pad_via_keepout_cells(the_pad, GridCoord(cfg.grid_step), cfg)
    blocked = {(int(x), int(y)) for x, y in cells}
    gy = round(the_pad.global_y / cfg.grid_step)
    gx = round(the_pad.global_x / cfg.grid_step)
    while (gx, gy) in blocked:
        gx += 1
    centre = gx * cfg.grid_step
    return centre - (the_pad.global_x + the_pad.size_x / 2) - cfg.via_drill / 2


def small_ring(hole_minimum, grid):
    """A 0.45/0.20 via at 0.10 mm clearance: ring + clearance is 0.225 mm."""
    rules = types.SimpleNamespace(board_min={'min_hole_clearance': hole_minimum} if hole_minimum else {},
                                  rules=[])
    return GridRouteConfig(track_width=0.0889, clearance=0.1, via_size=0.45, via_drill=0.2,
                           grid_step=grid, layers=['F.Cu', 'B.Cu'], rules=rules)


def stamped(cfg, **copper):
    """The obstacle map with net 7's copper stamped from the per-net cache."""
    from grid_router import GridObstacleMap
    from kicad_parser import PCBData, BoardInfo, Net
    from obstacle_cache import precompute_net_obstacles, add_net_obstacles_from_cache
    pcb = PCBData(BoardInfo({0: 'F.Cu', 31: 'B.Cu'}, ['F.Cu', 'B.Cu']), {7: Net(7, '/A')}, {},
                  copper.get('vias', []), copper.get('segments', []), {})
    obstacles = GridObstacleMap(len(cfg.layers))
    add_net_obstacles_from_cache(obstacles, precompute_net_obstacles(pcb, 7, cfg))
    return obstacles


def nearest_free(blocked, cfg, origin, reach_cells=60):
    """The smallest centre distance (mm) from `origin` to a cell `blocked` leaves free."""
    gx0, gy0 = round(origin[0] / cfg.grid_step), round(origin[1] / cfg.grid_step)
    best = None
    for dx in range(-reach_cells, reach_cells + 1):
        for dy in range(-reach_cells, reach_cells + 1):
            if (dx or dy) and not blocked(gx0 + dx, gy0 + dy):
                d = (dx * dx + dy * dy) ** 0.5 * cfg.grid_step
                best = d if best is None else min(best, d)
    return best


def check_track_and_via_hole_terms():
    from kicad_parser import Segment, Via
    for grid in (0.025, 0.05, 0.1):
        cfg = small_ring(0.25, grid)
        via = Via(x=10.0, y=10.0, size=0.45, drill=0.2, layers=['F.Cu', 'B.Cu'], net_id=7)
        around_via = stamped(cfg, vias=[via])
        # a foreign track's centre: hole radius + hole minimum + half the track
        need = 0.1 + 0.25 + cfg.track_width / 2
        got = nearest_free(lambda x, y: around_via.is_blocked(x, y, 0), cfg, (10.0, 10.0))
        check(f'a track stays 0.25 mm from a foreign via hole at grid {grid}', got >= need - 1e-9,
              f'{got:.4f} >= {need:.4f}')
        # a foreign via's centre: one hole + hole minimum + the other via's copper
        need = 0.1 + 0.25 + 0.225
        got = nearest_free(around_via.is_via_blocked, cfg, (10.0, 10.0))
        check(f'two vias keep hole to copper 0.25 mm apart at grid {grid}', got >= need - 1e-9,
              f'{got:.4f} >= {need:.4f}')
        track = Segment(start_x=10.0, start_y=10.0, end_x=10.0, end_y=10.0 + 40 * grid, width=0.0889,
                        layer='F.Cu', net_id=7)
        around_track = stamped(cfg, segments=[track])
        need = 0.1 + 0.25 + 0.0889 / 2
        got = nearest_free(around_track.is_via_blocked, cfg, (10.0, 10.0 + 20 * grid))
        check(f'a new via hole stays 0.25 mm from a foreign track at grid {grid}', got >= need - 1e-9,
              f'{got:.4f} >= {need:.4f}')
    loose = small_ring(0, 0.025)
    got = nearest_free(stamped(loose, vias=[Via(x=10.0, y=10.0, size=0.45, drill=0.2, layers=['F.Cu', 'B.Cu'],
                                                net_id=7)]).is_via_blocked, loose, (10.0, 10.0))
    check('without a hole minimum the copper clearance alone spaces two vias', got < 0.1 + 0.25 + 0.225,
          f'{got:.4f}')
    ring = config(0.25)   # 0.6/0.3 via at 0.2 clearance: the copper term already covers the hole
    check('a ring that covers the hole minimum changes nothing',
          abs(ring.via_hole_floor(0.3 + 0.2 + 0.1, 0.1) - 0.6) < 1e-9
          and abs(ring.via_via_hole_floor(0.8, 0.6, 0.3) - 0.8) < 1e-9)


def main():
    check_track_and_via_hole_terms()
    plain = config(0.25)
    check('copper term governs at the class clearance',
          abs(plain.via_copper_reach(0.2) - 0.5) < 1e-9, f'{plain.via_copper_reach(0.2)}')
    check('hole term governs under a small pad override',
          abs(plain.via_copper_reach(0.0508) - 0.401) < 1e-9, f'{plain.via_copper_reach(0.0508)}')
    check('no declared hole minimum leaves the copper term',
          abs(config(0).via_copper_reach(0.0508) - 0.3508) < 1e-9)
    check('an explicit hole clearance outranks the board value',
          abs(config(0.25, hole_clearance=0.4).via_copper_reach(0.0508) - 0.551) < 1e-9)
    for grid in (0.025, 0.05, 0.1):
        gap = nearest_free_via_gap(config(0.25, grid=grid), pad(0.0508))
        check(f'via hole stays 0.25 mm from an overridden pad at grid {grid}', gap >= 0.25 - 1e-6, f'{gap:.4f}')
    gap = nearest_free_via_gap(config(0, grid=0.025), pad(0.0508))
    check('without a hole minimum the override alone sets the gap', gap < 0.25, f'{gap:.4f}')
    print('FAILED: ' + ', '.join(FAILS) if FAILS else 'ALL PASS')
    return 1 if FAILS else 0


if __name__ == '__main__':
    raise SystemExit(main())
