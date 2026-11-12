#!/usr/bin/env python3
"""The corner allowance of a round-ended pad is what a move round its arc needs.

A cell of the obstacle map is open when a track centred on it clears the pad.
A track runs from an open cell to one of its eight neighbours. Along a straight
side that piece is as far from the pad as its ends. Round a corner it cuts
inside the arc its ends sit on, so the keep-out there is drawn a little larger.

Upstream draws it half a grid step larger at every corner. For a circle or an
oval every cell beside the arc is "corner", also in a lane that runs straight
past the pad. At a round 1.6 mm through-hole pad with a 0.4 mm track at
0.254 mm clearance that is 0.05 mm (0.0635 mm on a 0.127 mm grid) on each side
of a lane that has 0.016 mm to spare, so no track passed between the pins of a
DIP part: 115 such lanes on the KiCad demo board interf_u, 48 of which its
designer used.

For round-ended pads the allowance is now the geometric bound
(`routing_utils.pad_corner_buffer`); rectangles and rounded rectangles keep
upstream's. This test holds it to both ends:

1. No move between two open cells brings the track closer to the pad than the
   margin: every pad shape, at several sizes, margins, grids, rotations and
   positions off the grid.
2. Without any allowance such moves exist, so the test can fail.
3. The DIP lane is open with the pads on the grid, and stays closed where the
   track does not fit.
4. Rectangles and rounded rectangles have upstream's cells, and
   `KICAD_PAD_CORNER=legacy` gives upstream's cells for round pads too.

    python3 tests/test_pad_corner_allowance.py
"""
import math
import os
import random
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), 'py_router'))

import numpy as np                                              # noqa: E402

import env_knobs                                                # noqa: E402
import routing_utils                                            # noqa: E402
from routing_utils import iter_pad_blocked_cells, pad_blocked_cells_array, pad_corner_buffer  # noqa: E402

FAILS = []
MOVES = [(1, 0), (1, 1), (0, 1), (-1, 1)]       # each move once; the other four are these reversed
SAMPLES = 9                                     # points checked along one move


def check(name, cond, detail=''):
    if not cond:
        FAILS.append(name)
    print(('  PASS ' if cond else '  FAIL ') + name + (f'  {detail}' if detail else ''))


def pad_distance(x, y, half_w, half_h, radius, rotation):
    """Distance (mm) from points to the pad's copper, 0 inside. The points are
    relative to the pad centre, in board axes."""
    if rotation:
        rad = math.radians(rotation)
        c, s = math.cos(rad), math.sin(rad)
        x, y = x * c + y * s, -x * s + y * c
    qx = np.abs(x) - (half_w - radius)
    qy = np.abs(y) - (half_h - radius)
    outside = np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0))
    return np.maximum(outside - radius, 0.0)


def closest_move(half_w, half_h, radius, margin, grid, off, rotation, corner_buffer=None):
    """The smallest distance from the pad that any move between two open cells
    reaches, less the margin (mm). Negative: a track there breaks the clearance."""
    cells = pad_blocked_cells_array(0, 0, half_w, half_h, margin, grid, radius, corner_buffer,
                                    off[0], off[1], rotation_deg=rotation)
    reach = int(math.ceil((math.hypot(half_w, half_h) + margin) / grid)) + 4
    size = 2 * reach + 1
    blocked = np.zeros((size, size), dtype=bool)
    blocked[cells[:, 0] + reach, cells[:, 1] + reach] = True
    worst = math.inf
    steps = np.linspace(0.0, 1.0, SAMPLES)
    index = np.arange(-reach, reach + 1)
    gx, gy = np.meshgrid(index, index, indexing='ij')
    for dx, dy in MOVES:
        to_x, to_y = gx + dx, gy + dy
        inside = (np.abs(to_x) <= reach) & (np.abs(to_y) <= reach)
        ax, ay = gx[inside], gy[inside]
        bx, by = to_x[inside], to_y[inside]
        both_open = ~blocked[ax + reach, ay + reach] & ~blocked[bx + reach, by + reach]
        ax, ay, bx, by = ax[both_open], ay[both_open], bx[both_open], by[both_open]
        if not len(ax):
            continue
        px = (ax[:, None] + (bx - ax)[:, None] * steps[None, :]) * grid - off[0]
        py = (ay[:, None] + (by - ay)[:, None] * steps[None, :]) * grid - off[1]
        worst = min(worst, float(pad_distance(px, py, half_w, half_h, radius, rotation).min()) - margin)
    return worst


def shapes(rng):
    """(name, half width, half height, corner radius) of the pad shapes the engine draws."""
    side = rng.choice([0.25, 0.3, 0.6, 1.0, 1.5])
    long = side * rng.choice([1.0, 1.6, 4.0])
    return [
        ('rectangle', long / 2, side / 2, 0.0),
        ('rounded rectangle', long / 2, side / 2, 0.25 * side),
        ('circle', side / 2, side / 2, side / 2),
        ('oval', long / 2, side / 2, side / 2),
    ]


def main():
    rng = random.Random(20261112)

    print('1. no move between two open cells comes inside the margin')
    worst = {}
    cases = 0
    for _ in range(60):
        grid = rng.choice([0.05, 0.0635, 0.1, 0.127])
        margin = rng.choice([0.2, 0.25, 0.325, 0.454, 0.6])
        off = (rng.uniform(-grid / 2, grid / 2), rng.uniform(-grid / 2, grid / 2))
        rotation = rng.choice([0.0, 0.0, 30.0, 45.0, -60.0])
        for name, half_w, half_h, radius in shapes(rng):
            if name == 'circle':
                rotation_here = 0.0
            else:
                rotation_here = rotation
            got = closest_move(half_w, half_h, radius, margin, grid, off, rotation_here)
            worst[name] = min(worst.get(name, math.inf), got)
            cases += 1
    for name, got in sorted(worst.items()):
        check(f'{name}: the closest move keeps the margin', got >= -1e-9, f'{got * 1000:+.2f} um over {cases // 4} pads')

    print('2. with no allowance at all a move does come inside it')
    least = min(closest_move(0.8, 0.8, 0.8, 0.454, 0.127, (0.031, -0.047), 0.0, corner_buffer=0.0),
                closest_move(0.5, 0.15, 0.0, 0.2, 0.1, (0.02, 0.04), 0.0, corner_buffer=0.0))
    check('the test finds the cut corner', least < -1e-4, f'{least * 1000:+.2f} um')

    print('3. the lane between two pins of a DIP part')
    grid, pitch, half = 0.127, 2.54, 0.8

    def lane(margin, corner_radius=0.8):
        first = {tuple(c) for c in pad_blocked_cells_array(0, 0, half, half, margin, grid, corner_radius).tolist()}
        second = {tuple(c) for c in pad_blocked_cells_array(round(pitch / grid), 0, half, half, margin, grid,
                                                            corner_radius).tolist()}
        mid = round(pitch / grid) // 2
        return [gy for gy in range(-12, 13) if (mid, gy) in first or (mid, gy) in second]

    closed = lane(0.2 + 0.254)
    check('a 0.4 mm track at 0.254 mm passes between 1.6 mm round pads at 2.54 mm', not closed,
          f'{len(closed)} cell(s) of the lane blocked')
    closed = lane(0.25 + 0.254)
    check('a 0.5 mm track does not', bool(closed))
    allowance = pad_corner_buffer(0.454, grid, 0.8, 0.8, 0.8)
    check('the allowance there is the bound, under upstream\'s', 0.012 < allowance < 0.014 < grid / 2,
          f'{allowance * 1000:.1f} um against {grid / 2 * 1000:.1f} um')
    small = pad_corner_buffer(0.02, 0.1, 0.05, 0.05, 0.05)
    check('it is never more than upstream\'s', small == 0.05, f'{small}')

    print('4. other shapes are upstream\'s; both rasterisers agree; KICAD_PAD_CORNER=legacy is upstream\'s')
    for name, half_w, half_h, radius in (('rectangle', 0.5, 0.15, 0.0), ('rounded rectangle', 0.5, 0.15, 0.075),
                                         ('rounded rectangle, square', 0.4, 0.4, 0.2)):
        ours = pad_blocked_cells_array(0, 0, half_w, half_h, 0.25, 0.1, radius, None, 0.02, 0.04)
        theirs = pad_blocked_cells_array(0, 0, half_w, half_h, 0.25, 0.1, radius, 0.05, 0.02, 0.04)
        check(f'a {name} has upstream\'s cells', sorted(map(tuple, ours.tolist())) == sorted(map(tuple, theirs.tolist())),
              f'{len(ours)} and {len(theirs)}')
    oval = pad_blocked_cells_array(0, 0, 1.2, 0.8, 0.454, 0.127, 0.8)
    oval_upstream = pad_blocked_cells_array(0, 0, 1.2, 0.8, 0.454, 0.127, 0.8, 0.127 / 2)
    check('an oval has the bound at its ends', len(oval) < len(oval_upstream), f'{len(oval)} against {len(oval_upstream)}')
    args = (3, -2, 0.8, 0.8, 0.454, 0.127, 0.8)
    now = pad_blocked_cells_array(*args)
    twin = np.array(sorted(iter_pad_blocked_cells(*args)), dtype=np.int64)
    check('the reference rasteriser draws the same cells',
          sorted(map(tuple, now.tolist())) == sorted(map(tuple, twin.tolist())), f'{len(now)} and {len(twin)}')
    upstream = pad_blocked_cells_array(*args, corner_buffer=0.127 / 2)
    env_knobs.PAD_CORNER_LEGACY = True
    routing_utils._PAD_OFFSETS_CACHE.clear()
    try:
        legacy = pad_blocked_cells_array(*args)
    finally:
        env_knobs.PAD_CORNER_LEGACY = False
        routing_utils._PAD_OFFSETS_CACHE.clear()
    check('legacy is upstream\'s half grid step', sorted(map(tuple, legacy.tolist())) == sorted(map(tuple, upstream.tolist())),
          f'{len(legacy)} and {len(upstream)}')
    check('and blocks more than the bound', len(legacy) > len(now), f'{len(legacy)} against {len(now)}')

    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'ALL PASS'}")
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
