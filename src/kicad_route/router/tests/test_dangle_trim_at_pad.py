#!/usr/bin/env python3
"""A track that runs over its own pad and ends beyond it is cut back to the pad.

The dangle trim shortens a dead-end track to the last anchor on its body. Its
anchors were a via on the track and another track's end. A pad was not one.
The pair router leaves exactly that: a pair whose pads are closer than its two
setbacks (2 mm apart here) is routed as a loop of 19 mm that passes over both
pads, the cycle prune and the dead-end sweep remove the loop, and what is left
of each member runs 0.5 to 0.7 mm past the far pad into free space. KiCad
reports each such end as `track_dangling`, whatever its length, so a pad
anchor has no minimum tail (the via and track anchors keep theirs, three track
widths).

    python3 tests/test_dangle_trim_at_pad.py
"""
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), 'py_router'))

from synth import make_pad, make_seg, make_net, make_pcb        # noqa: E402
import pcb_modification                                         # noqa: E402

FAILS = []
W = 0.25


def check(name, cond, detail=''):
    if not cond:
        FAILS.append(name)
    print(('  PASS ' if cond else '  FAIL ') + name + (f'  {detail}' if detail else ''))


def board(run_out, *, far_pad=0.25, far_layers=('F.Cu',), far_net=1, new=True):
    """Pad A at x=0 and pad B at x=2 on one net, and one track from A over B to x = 2 + run_out."""
    pads = [make_pad(1, 0.0, 0.0, ref='J1', size_x=0.25, size_y=0.25, net_name='/A'),
            make_pad(far_net, 2.0, 0.0, ref='J2', size_x=far_pad, size_y=far_pad, layers=far_layers,
                     net_name='/A' if far_net == 1 else '/B')]
    track = make_seg(0.0, 0.0, 2.0 + run_out, 0.0, width=W)
    by_net = {}
    for p in pads:
        by_net.setdefault(p.net_id, []).append(p)
    pcb = make_pcb(nets={1: make_net(1, '/A'), 2: make_net(2, '/B')}, segments=[track], pads_by_net=by_net)
    results = [{'new_segments': [track], 'new_vias': []}] if new else []
    return pcb, results, track


def trim(pcb, results, **kw):
    count, stripped = pcb_modification.trim_dangles_past_body_anchor(results, pcb, {1}, **kw)
    seg, = pcb.segments
    return count, stripped, (round(seg.start_x, 4), round(seg.end_x, 4))


def main():
    print('1. a run-out longer than three track widths')
    pcb, results, _ = board(0.9)
    count, stripped, ends = trim(pcb, results)
    check('the track ends on the pad it ran over', (count, ends) == (1, (0.0, 2.0)), f'{count} {ends}')
    check('a track of this run is shortened in place', stripped == [])

    print('2. the run-outs the pair router leaves (0.476 and 0.724 mm, under three track widths)')
    for run_out in (0.475736, 0.724264):
        pcb, results, _ = board(run_out)
        count, _, ends = trim(pcb, results)
        check(f'{run_out} mm is cut', (count, ends) == (1, (0.0, 2.0)), f'{count} {ends}')

    print('3. what is not a run-out stays')
    pcb, results, _ = board(0.3, far_pad=1.0)
    count, _, ends = trim(pcb, results)
    check('an end on the pad\'s own copper', (count, ends) == (0, (0.0, 2.3)), f'{count} {ends}')
    pcb, results, _ = board(0.9, far_layers=('B.Cu',))
    count, _, ends = trim(pcb, results)
    check('a pad of another layer under the track is no anchor', (count, ends) == (0, (0.0, 2.9)), f'{count} {ends}')
    pcb, results, _ = board(0.9, far_net=2)
    count, _, ends = trim(pcb, results)
    check('a pad of another net is no anchor', (count, ends) == (0, (0.0, 2.9)), f'{count} {ends}')

    print('4. input copper under --keep-input-copper is not touched')
    pcb, results, _ = board(0.9, new=False)
    count, stripped, ends = trim(pcb, results, keep_input_copper=True)
    check('the input track keeps its run-out', (count, stripped, ends) == (0, [], (0.0, 2.9)), f'{count} {ends}')

    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'ALL PASS'}")
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
