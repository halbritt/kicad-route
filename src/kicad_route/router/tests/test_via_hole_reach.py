#!/usr/bin/env python3
"""A new via's hole keeps the board's copper-to-hole minimum from a foreign pad.

A pad or footprint clearance override replaces the class clearance (KiCad's
order). At 0.05 mm it lets via copper sit 0.05 mm from the pad; with a 0.15 mm
annular ring the via's hole is then 0.2 mm from the pad, and KiCad grades that
as hole_clearance on a board whose minimum is 0.25 mm. The via keep-out around
a pad must cover both the via's copper and its hole.
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


def main():
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
