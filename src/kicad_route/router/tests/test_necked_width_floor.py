#!/usr/bin/env python3
"""A track at the board's minimum width is never written narrower than that.

A net that could not be connected at its class width may be routed at the
board's own minimum (`--escalation board`). On a board whose minimum is 7.8 mil
(0.19812 mm) such tracks came out 0.1981 mm wide, 20 nm under it, and KiCad
reported every one (track_width): seven on one net of a four-layer board,
which cost a closing round its two connections. Two things rounded a width to
0.1 um and wrote the rounded number:

1. The smoothing and the collinear merge group a net's tracks by rounded
   width and re-drew each group at the group's key. They now re-draw it at the
   width the tracks have (`routing_utils.group_width`).
2. A track necked at a pad ("terminal neck") was rounded wherever it landed,
   also on its floor. `routing_utils.necked_width` keeps the 0.1 um raster
   above the floor and the floor itself exact.

This test merges two collinear tracks at 0.19812 mm and reads the width back,
holds the two helpers to their rule, and holds the places that used the bare
rounding to the helpers, so that a newer upstream file shows up here.

    python3 tests/test_necked_width_floor.py
"""
import os
import re
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(os.path.dirname(TESTS_DIR), 'py_router')
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, ENGINE)

from types import SimpleNamespace                                # noqa: E402

from kicad_parser import Segment                                 # noqa: E402
from pcb_modification import merge_collinear_segments            # noqa: E402
from routing_utils import group_width, necked_width              # noqa: E402

FAILS = []


def check(name, cond, detail=''):
    if not cond:
        FAILS.append(name)
    print(('  PASS ' if cond else '  FAIL ') + name + (f'  {detail}' if detail else ''))


def main():
    print('necked_width')
    mils = 7.8 * 0.0254                         # 0.19812 mm
    check('a neck on the floor is the floor', necked_width(mils, mils) == mils, repr(necked_width(mils, mils)))
    check('bare rounding went below it', round(mils, 4) < mils, repr(round(mils, 4)))
    check('a neck just above the floor does not round below it', necked_width(mils + 2e-5, mils) >= mils)
    check('a neck below the floor is the floor', necked_width(0.15, 0.2) == 0.2)
    check('above the floor the 0.1 um raster stays', necked_width(0.250049, 0.2) == 0.25
          and necked_width(0.33333, 0.2) == 0.3333)
    for floor in (0.0889, 0.1, 0.127, 0.15, 0.19812, 0.2, 0.2032, 0.254):
        for step in range(0, 400):
            width = floor - 0.01 + step * 0.0001237
            got = necked_width(width, floor)
            if got < floor or abs(got - max(floor, width)) > 5.1e-5:
                check(f'floor {floor} width {width}', False, repr(got))
                break
        else:
            check(f'floor {floor}: never below it, never more than half a raster step off', True)

    print('the collinear merge')
    for width in (mils, 0.2, 0.25004):
        pieces = [Segment(start_x=10.0, start_y=20.0, end_x=12.0, end_y=20.0, width=width, layer='F.Cu', net_id=7),
                  Segment(start_x=12.0, start_y=20.0, end_x=15.5, end_y=20.0, width=width, layer='F.Cu', net_id=7)]
        board = SimpleNamespace(segments=list(pieces), vias=[], pads_by_net={})
        results = [{'new_segments': list(pieces), 'new_vias': []}]
        merged, *_ = merge_collinear_segments(results, board)
        widths = sorted({segment.width for segment in board.segments})
        check(f'two tracks of {width!r} mm become one of that width',
              merged >= 1 and len(board.segments) == 1 and widths == [width], f'{len(board.segments)} track(s), {widths}')
    check('a group is re-drawn at the width of its tracks',
          group_width([SimpleNamespace(width=mils), SimpleNamespace(width=mils)]) == mils)

    print('the places that neck a track')
    for name in ('single_ended_routing.py', 'pcb_modification.py', 'diff_pair_routing.py'):
        source = open(os.path.join(ENGINE, name), encoding='utf-8').read()
        bare = re.findall(r'^\s*\w+\.width\s*=\s*round\(.*$', source, re.M)
        check(f'{name} assigns no bare rounded width', not bare, '; '.join(line.strip() for line in bare))
        check(f'{name} necks through necked_width', len(re.findall(r'\.width = necked_width\(', source)) == 1)
    source = open(os.path.join(ENGINE, 'pcb_modification.py'), encoding='utf-8').read()
    grouped = len(re.findall(r'groups\[\(s\.layer, round\(s\.width, 4\)\)\]', source))
    check('every pass that groups tracks by rounded width re-draws at group_width',
          grouped == 2 and len(re.findall(r'^\s*w = group_width\(gsegs\)$', source, re.M)) == grouped, f'{grouped} grouping(s)')

    print()
    if FAILS:
        print(f'FAILED: {len(FAILS)} check(s): {FAILS}')
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
