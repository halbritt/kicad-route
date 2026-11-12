#!/usr/bin/env python3
"""Two checks pass over what cannot matter, and give the answers they gave.

A ripped net is put back only when its saved copper keeps its distance from the
copper that is on the board now. The check took every foreign segment inside the
saved net's bounding box and measured it against every saved segment. For a net
that crosses the board that is nearly every segment against every segment: 2.6
million pairs and 42 s of a 385 s run on a four-layer board, for 52 restores.

A footprint's own copper leaves via sites open on its pads. The check asked for
every cell of every keep-out whether it lies on one of the pads, by measuring
the pad's outline: 2.7 million measurements and 121 s of a 436 s run on a board
whose footprints have polygon pads.

Both now pass over a pair whose bounding boxes are too far apart to matter
before they measure. Here each is run on random copper beside the plain form it
had, and has to give the same answer, in the same order.

    python3 tests/test_restore_speed.py
"""
import math
import os
import random
import sys
import types

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

import numpy as np                                              # noqa: E402

from kicad_parser import Pad, Segment, Via                      # noqa: E402
from routing_config import GridRouteConfig, GridCoord           # noqa: E402
from check_drc import point_to_pad_distance                     # noqa: E402
import obstacle_map                                             # noqa: E402
import rip_up_reroute as rur                                    # noqa: E402

FAILS = []
LAYERS = ['F.Cu', 'B.Cu']
OWN = 1


def check(name, cond, detail=''):
    if not cond:
        FAILS.append(name)
    print(('  PASS ' if cond else '  FAIL ') + name + (f'  {detail}' if detail else ''))


# ------------------------------------------------------------ the restore check

def copper(rng, net, count, span):
    segs, vias = [], []
    for _ in range(count):
        x, y = rng.uniform(0, span), rng.uniform(0, span)
        angle, length = rng.choice([0, 45, 90, 135]) * math.pi / 180, rng.uniform(0.2, 6.0)
        segs.append(Segment(start_x=x, start_y=y, end_x=x + length * math.cos(angle),
                            end_y=y + length * math.sin(angle), width=rng.choice([0.2, 0.4, 1.0]),
                            layer=rng.choice(LAYERS), net_id=net))
    for _ in range(max(1, count // 4)):
        vias.append(Via(x=rng.uniform(0, span), y=rng.uniform(0, span), size=rng.choice([0.6, 0.8]),
                        drill=0.3, layers=list(LAYERS), net_id=net))
    return segs, vias


def plain(saved, board, clearance, config):
    """The check as it was: every saved item against every foreign item."""
    pairwise = config is not None and not config.pair_clearance_inert()

    def clr(own_net, other, layer, kind):
        if not pairwise:
            return clearance
        return config.pair_clearance(own_net, other.net_id, layer, kind=kind, base=clearance)

    h2h = (getattr(config, 'hole_to_hole_clearance', 0.0) or 0.0) if config else 0.0
    hits, seen = [], set()

    def hit(kind, obj):
        if id(obj) not in seen:
            seen.add(id(obj))
            hits.append((kind, obj))

    others = [s for s in board.segments if s.net_id not in (OWN, 0)]
    other_vias = [v for v in board.vias if v.net_id not in (OWN, 0)]
    for s in saved['new_segments']:
        hw = s.width / 2.0
        for o in others:
            if o.layer != s.layer:
                continue
            thr = hw + o.width / 2.0 + clr(s.net_id, o, s.layer, 'track')
            if rur._seg_seg_dist_sq(s.start_x, s.start_y, s.end_x, s.end_y,
                                    o.start_x, o.start_y, o.end_x, o.end_y) < thr * thr:
                hit('segment', o)
        for v in other_vias:
            thr = hw + v.size / 2.0 + clr(s.net_id, v, s.layer, 'layer')
            if rur._pt_seg_dist_sq(v.x, v.y, s.start_x, s.start_y, s.end_x, s.end_y) < thr * thr:
                hit('via', v)
    for vv in saved['new_vias']:
        vr = vv.size / 2.0
        for v in other_vias:
            thr = vr + v.size / 2.0 + clr(vv.net_id, v, None, 'stack')
            if h2h > 0 and vv.drill > 0 and v.drill > 0:
                thr = max(thr, vv.drill / 2.0 + v.drill / 2.0 + h2h)
            if (vv.x - v.x) ** 2 + (vv.y - v.y) ** 2 < thr * thr:
                hit('via', v)
        for o in others:
            thr = vr + o.width / 2.0 + clr(vv.net_id, o, o.layer, 'layer')
            if rur._pt_seg_dist_sq(vv.x, vv.y, o.start_x, o.start_y, o.end_x, o.end_y) < thr * thr:
                hit('segment', o)
    return hits


def restore_check():
    for name, config, span in (
            ('one clearance for all', None, 30.0),
            ('a clearance for each pair of nets', GridRouteConfig(
                track_width=0.2, clearance=0.15, via_size=0.6, via_drill=0.3, grid_step=0.1,
                layers=list(LAYERS), net_clearances={2: 0.5, 3: 0.3}), 30.0),
            ('a crowded board', None, 8.0)):
        same, found, pairs = True, 0, 0
        for seed in range(12):
            rng = random.Random(seed)
            own_segs, own_vias = copper(rng, OWN, 30, span)
            segs, vias = [], []
            for net in (2, 3, 4):
                more_segs, more_vias = copper(rng, net, 60, span)
                segs += more_segs
                vias += more_vias
            board = types.SimpleNamespace(segments=segs, vias=vias)
            saved = {'new_segments': own_segs, 'new_vias': own_vias}
            want = plain(saved, board, 0.2, config)
            got = rur._saved_route_colliders(saved, board, [OWN], 0.2, config=config)
            first = rur._saved_route_colliders(saved, board, [OWN], 0.2, first_only=True, config=config)
            same = (same and [(k, id(o)) for k, o in got] == [(k, id(o)) for k, o in want]
                    and bool(first) == bool(want)
                    and (not want or (first[0][0], id(first[0][1])) == (want[0][0], id(want[0][1]))))
            found += len(want)
            pairs += len(own_segs) * len(segs)
        check(f'restore check, {name}: the same colliders in the same order', same,
              f'{found} colliders among {pairs} segment pairs')
        check(f'restore check, {name}: the boards do have colliders to find', found > 0)


# ------------------------------------------------------------ via sites on a footprint's own pads

def pads():
    square = [(10.0, 10.0), (11.2, 10.0), (11.2, 10.9), (10.6, 11.4), (10.0, 10.9)]
    tie = Pad(component_ref='NT1', pad_number='1', global_x=10.6, global_y=10.6, local_x=0.0, local_y=0.0,
              size_x=0.5, size_y=0.5, shape='custom', layers=['F.Cu'], net_id=7, net_name='/A')
    tie.polygons = [square]
    turned = Pad(component_ref='NT1', pad_number='2', global_x=13.0, global_y=10.5, local_x=0.0, local_y=0.0,
                 size_x=1.6, size_y=0.4, shape='rect', layers=['F.Cu'], net_id=7, net_name='/A')
    turned.rect_rotation = 30.0
    other = Pad(component_ref='NT1', pad_number='3', global_x=12.0, global_y=12.5, local_x=0.0, local_y=0.0,
                size_x=1.0, size_y=1.0, shape='circle', layers=['F.Cu'], net_id=9, net_name='/B')
    return [tie, turned, other]


def plain_sites(spans, the_pads, coord):
    """The check as it was: every cell against every pad's outline."""
    cells, dropped = [], False
    for gx, lo, hi in spans.tolist():
        for gy in range(lo, hi + 1):
            x, y = coord.to_float(gx, gy)
            if any(point_to_pad_distance(x, y, p) <= 0.0 for p in the_pads):
                dropped = True
            else:
                cells.append((gx, gy, gy))
    return spans if not dropped else np.array(cells, dtype=np.int32).reshape(-1, 3)


def via_sites():
    for grid in (0.1, 0.05):
        coord = GridCoord(grid)
        the_pads = pads()
        board = types.SimpleNamespace(footprints={'NT1': types.SimpleNamespace(pads=the_pads)})
        lo_x, hi_x = coord.to_grid(9.0, 0)[0], coord.to_grid(15.0, 0)[0]
        lo_y, hi_y = coord.to_grid(0, 9.0)[1], coord.to_grid(0, 13.5)[1]
        spans = np.array([(gx, lo_y, hi_y) for gx in range(lo_x, hi_x + 1)], dtype=np.int32)
        mine = [p for p in the_pads if p.net_id == 7]
        want = plain_sites(spans, mine, coord)
        got = obstacle_map._drop_own_pad_via_sites(spans, board, 'NT1', {7}, coord)
        total = sum(hi - lo + 1 for _gx, lo, hi in spans.tolist())
        check(f'via sites at {grid} mm: the same cells are left', np.array_equal(want, got),
              f'{total - len(want)} of {total} cells lie on a pad')
        check(f'via sites at {grid} mm: some cells do lie on the pads', 0 < total - len(want) < total)
        far = np.array([(coord.to_grid(40.0, 0)[0], lo_y, hi_y)], dtype=np.int32)
        check(f'via sites at {grid} mm: a keep-out that touches no pad comes back as it is',
              obstacle_map._drop_own_pad_via_sites(far, board, 'NT1', {7}, coord) is far)


def main():
    restore_check()
    via_sites()
    print('FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
