#!/usr/bin/env python3
"""The stop file ends a run after the single-ended loop too, and the board is written.

A caller with a time budget touches KICAD_STOP_FILE when the time is up. The
single-ended loop looked for it before each net. The passes after that loop did
not: a run that finished the loop in time went on through Phase 3, the rescue
and the plane repair however long they took, the caller killed it, and no board
came back. Seen on three real boards of 160 to 480 connections at a 300 s
budget.

Here 24 three-pad nets give Phase 3 24 tap connections. The stop file appears
when the Phase 3 banner is printed. The run must stop between two nets of that
pass, say so, and write the board with what it has; without the stop file the
same board is routed completely.

The plane repair is the last of those passes and the longest: it taps every pad
of a plane net that has no way to its plane, one after the other. Upstream calls
it without a cancel hook, so on a board with fifty such pads it ran on for more
than a minute after the request and was killed while it printed its summary,
with 29 pads tapped and no board. The third part runs the repair alone
(KICAD_FINALIZE_ONLY=1 --skip-routing, kicad-route's last pass) on a four-layer
board with 20 ground pads over a ground plane, and puts the stop file there when
the fifth pad is done: the repair must stop between two pads, and the board must
hold the copper it had made.

    python3 tests/test_stop_file_later_passes.py
"""
import os
import re
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), 'py_router'))

from regression_utils import tool      # noqa: E402
from synth import RoutingBoard         # noqa: E402

FAILS = []
NETS = 24
FLAGS = ['--escalation', 'off', '--fab-tier', 'standard', '--strict-sizes', '--no-power-tap-neckdown',
         '--no-fix-drc-settings', '--keep-input-copper', '--no-stub-layer-swap']


def check(name, cond, detail=''):
    if not cond:
        FAILS.append(name)
    print(('  PASS ' if cond else '  FAIL ') + name + (f'  {detail}' if detail else ''))


def board():
    """NETS nets of three pads each, one row per net, nothing in the way."""
    b = RoutingBoard(w=44, h=3 * NETS + 20)
    for i in range(NETS):
        y = 10 + 3 * i
        for name, x in (('A', 10), ('B', 20), ('C', 34)):
            b.pad(f'{name}{i}', x, y, f'N{i}')
    return b.text()


def route(tmp, tag, stop_at_phase3):
    inp = os.path.join(tmp, f'{tag}.kicad_pcb')
    out = os.path.join(tmp, f'{tag}_out.kicad_pcb')
    stop = os.path.join(tmp, f'{tag}.STOP')
    with open(inp, 'w', encoding='utf-8') as fh:
        fh.write(board())
    env = dict(os.environ, KICAD_PLANE_FINALIZE='0', KICAD_STOP_FILE=stop, PYTHONUNBUFFERED='1')
    proc = subprocess.Popen([sys.executable, '-X', 'utf8', '-u', tool('route.py'), inp, out, '--nets', '*', *FLAGS],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    lines = []
    for line in proc.stdout:
        lines.append(line)
        if stop_at_phase3 and 'Multi-point Phase 3' in line and not os.path.exists(stop):
            open(stop, 'w').close()
    code = proc.wait(timeout=300)
    log = ''.join(lines)
    assert 'Traceback (most recent call last)' not in log, log[-6000:]
    return log, code, out


PLANE_PADS = 20


def plane_board():
    """A four-layer board: PLANE_PADS surface pads of GND on the front, a GND plane on In1.Cu and
    one through-hole pad that is on the plane."""
    import uuid
    uid = lambda tag: str(uuid.uuid5(uuid.NAMESPACE_URL, 'krt-stop-plane/' + tag))
    pads = ''
    for k in range(PLANE_PADS):
        x, y = 6 + 4 * (k % 5), 6 + 4 * (k // 5)
        pads += (f'\t(footprint "test:P" (layer "F.Cu") (uuid "{uid(f"R{k}")}") (at {x} {y})\n'
                 f'\t\t(property "Reference" "R{k}" (at 0 -1 0) (layer "F.Fab") (uuid "{uid(f"R{k}r")}") '
                 f'(effects (font (size 0.5 0.5) (thickness 0.1))))\n'
                 f'\t\t(pad "1" smd roundrect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask") '
                 f'(roundrect_rratio 0.25) (net "GND") (uuid "{uid(f"R{k}p")}"))\n\t)\n')
    return ('(kicad_pcb\n\t(version 20260206)\n\t(generator "pcbnew")\n\t(generator_version "10.0")\n'
            '\t(general (thickness 1.6) (legacy_teardrops no))\n\t(paper "A4")\n'
            '\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(4 "In1.Cu" signal)\n\t\t(6 "In2.Cu" signal)\n'
            '\t\t(2 "B.Cu" signal)\n\t\t(1 "F.Mask" user)\n\t\t(3 "B.Mask" user)\n\t\t(25 "Edge.Cuts" user)\n\t)\n'
            '\t(setup (pad_to_mask_clearance 0))\n\t(net 0 "")\n\t(net 1 "GND")\n'
            f'\t(footprint "test:TP" (layer "F.Cu") (uuid "{uid("J1")}") (at 3 3)\n'
            f'\t\t(property "Reference" "J1" (at 0 -1.5 0) (layer "F.Fab") (uuid "{uid("J1r")}") '
            f'(effects (font (size 0.5 0.5) (thickness 0.1))))\n'
            f'\t\t(pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask") '
            f'(remove_unused_layers no) (net "GND") (uuid "{uid("J1p")}"))\n\t)\n'
            + pads +
            f'\t(gr_rect (start 0 0) (end 28 24) (stroke (width 0.05) (type default)) (fill no) '
            f'(layer "Edge.Cuts") (uuid "{uid("edge")}"))\n'
            f'\t(zone (net "GND") (layer "In1.Cu") (uuid "{uid("zone")}") (hatch edge 0.5)\n'
            '\t\t(connect_pads yes (clearance 0.2)) (min_thickness 0.16)\n'
            '\t\t(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5) (island_removal_mode 0))\n'
            '\t\t(polygon (pts (xy 0.3 0.3) (xy 27.7 0.3) (xy 27.7 23.7) (xy 0.3 23.7)))\n\t)\n)\n')


def repair(tmp, tag, stop_at_fifth_pad):
    """The plane repair alone on the plane board: (log, exit code, output board)."""
    inp = os.path.join(tmp, f'{tag}.kicad_pcb')
    out = os.path.join(tmp, f'{tag}_out.kicad_pcb')
    stop = os.path.join(tmp, f'{tag}.STOP')
    with open(inp, 'w', encoding='utf-8') as fh:
        fh.write(plane_board())
    env = dict(os.environ, KICAD_FINALIZE_ONLY='1', KICAD_STOP_FILE=stop, PYTHONUNBUFFERED='1')
    proc = subprocess.Popen([sys.executable, '-X', 'utf8', '-u', tool('route.py'), inp, out, '--nets', 'GND',
                             '--skip-routing', *FLAGS],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    lines = []
    for line in proc.stdout:
        lines.append(line)
        if stop_at_fifth_pad and 'Pad R4.1' in line and not os.path.exists(stop):
            open(stop, 'w').close()            # the fifth pad is done: fifteen are left
    code = proc.wait(timeout=600)
    log = ''.join(lines)
    assert 'Traceback (most recent call last)' not in log, log[-6000:]
    return log, code, out


def reconnected(log):
    """(pads the repair reconnected, pads that had no way to the plane), from its summary line."""
    found = re.findall(r'Pad repair: (\d+)/(\d+) unconnected pad', log)
    return tuple(int(n) for n in found[-1]) if found else None


def copper(path):
    with open(path, encoding='utf-8') as fh:
        text = fh.read()
    return text.count('(segment') + text.count('(via')


def pads_connected(log):
    found = re.findall(r'Multi-point:\s+(\d+)/(\d+) pads connected', log)
    return tuple(int(n) for n in found[-1]) if found else None


def main():
    with tempfile.TemporaryDirectory() as tmp:
        print('1. no stop file: the board is routed completely')
        log, code, out = route(tmp, 'free', stop_at_phase3=False)
        check('the run ends in order and writes the board', code == 0 and os.path.isfile(out), f'exit {code}')
        check('every pad is connected', pads_connected(log) == (3 * NETS, 3 * NETS), f'{pads_connected(log)}')
        check('no stop is reported', 'STOP requested' not in log)

        print('2. the stop file appears when Phase 3 starts')
        log, code, out = route(tmp, 'stopped', stop_at_phase3=True)
        check('the engine says it was asked to stop', 'STOP requested (KICAD_STOP_FILE)' in log)
        check('Phase 3 stops between two nets', 'Phase 3 cancelled' in log)
        check('the board is written', os.path.isfile(out), f'exit {code}')
        done = pads_connected(log)
        check('the taps that were not reached are reported as not connected',
              done is not None and 2 * NETS <= done[0] < done[1] == 3 * NETS, f'{done}')
        with open(out, encoding='utf-8') as fh:
            routed = fh.read()
        check('what was routed before the stop is on the board', routed.count('(segment') >= NETS,
              f"{routed.count('(segment')} segments")

        print('3. the plane repair alone: no stop file, then the stop file when its fifth pad is done')
        log, code, whole = repair(tmp, 'plane', stop_at_fifth_pad=False)
        check('the repair runs without the routing', 'Plane finalize (#562)' in log and os.path.isfile(whole), f'exit {code}')
        check('every pad is reconnected', reconnected(log) == (PLANE_PADS, PLANE_PADS), f'{reconnected(log)}')
        check('no stop is reported', 'STOP requested' not in log)
        log, code, part = repair(tmp, 'plane_stopped', stop_at_fifth_pad=True)
        check('the engine says it was asked to stop', 'STOP requested (KICAD_STOP_FILE)' in log)
        check('the pad repair stops between two pads', '(cancelled)' in log)
        done = reconnected(log)
        check('the pads not reached stay unconnected and are counted',
              done is not None and 0 < done[0] < done[1] == PLANE_PADS, f'{done}')
        check('the board is written with the copper made before the stop',
              os.path.isfile(part) and 0 < copper(part) < copper(whole),
              f'exit {code}, {copper(part) if os.path.isfile(part) else None} of {copper(whole)} tracks and vias')

    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'ALL PASS'}")
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
