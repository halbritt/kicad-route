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

    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'ALL PASS'}")
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
