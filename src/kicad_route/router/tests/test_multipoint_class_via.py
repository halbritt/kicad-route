#!/usr/bin/env python3
"""A net with its own class via gets that via or none (AGENTEE-650).

A net class may ask for a larger via than the Default class. The engine reads
that size per net (config.net_via_sizes). A two-pad net is searched with it. A
net with more than two pads is searched with the run's via, the Default
class's, and each via is enlarged afterwards only where the class via happens
to fit; the others are delivered below their class and reported as a descent.
On a real two-layer board that left 15 to 25 vias of a power class at the
Default size. The rescue's pad escape draws the run's via as well.

KICAD_CLASS_VIA_ONLY=1, which kicad-route sets, searches such a net with its
own via and gives it no pad escape. This test routes one three-pad net of a
class with a 1.0 mm via, with the flags and environment kicad-route passes,
on a board where pad A can leave its pocket only through a via:

  narrow pocket   a 0.6 mm via fits beside pad A, a 1.0 mm via does not
  wide pocket     both fit

Off, the narrow pocket gets a 0.6 mm via. On, the net gets no via there and
pad A is left open, which a caller can see and act on. On the wide pocket the
net is complete with 1.0 mm vias.

    python3 tests/test_multipoint_class_via.py
"""
import json
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
CLASS_VIA, RUN_VIA = 1.0, 0.6
# What kicad-route passes on every run with --protect existing (KRT_FLAGS, KRT_ENV in kicad_route/cli.py),
# without the knob under test.
WRAPPER_FLAGS = ['--escalation', 'off', '--fab-tier', 'standard', '--strict-sizes', '--no-power-tap-neckdown',
                 '--no-fix-drc-settings', '--keep-input-copper', '--no-stub-layer-swap']
WRAPPER_ENV = {'KICAD_INRUN_FLOOR_SYNC': '0', 'KICAD_TERMINAL_ESCALATION': '0', 'KICAD_IMPEDANCE_NECKDOWN': '0',
               'KICAD_RESCUE_CAP_MOVE': '0', 'KICAD_DYNAMIC_ITERATIONS': '0', 'KICAD_RIP_PREEXISTING': '0',
               'KICAD_FINALIZE_RIP': '0'}
AX, AY = 10.0, 15.0                    # pad A, in the middle of its pocket


def check(name, cond, detail=''):
    if not cond:
        FAILS.append(name)
    print(('  PASS ' if cond else '  FAIL ') + name + (f'  {detail}' if detail else ''))


def board(half_height):
    """Pad A of net W inside a closed F.Cu ring of net X; pads B and C of W outside it.

    The ring's centre lines are half_height above and below pad A. Its copper is
    0.2 mm wide, so with a 0.2 mm clearance a via of diameter d fits on the
    pocket's axis when half_height >= d / 2 + 0.3."""
    b = RoutingBoard()
    b.pad('A', AX, AY, 'W', sx=0.6, sy=0.6)
    b.pad('B', 30, 10, 'W')
    b.pad('C', 30, 20, 'W')
    b.ring([(AX - 1.4, AY - half_height), (AX + 3.4, AY - half_height),
            (AX + 3.4, AY + half_height), (AX - 1.4, AY + half_height)], 'X', w=0.2, locked=True)
    b.pad('X1', 4, 4, 'X', sx=1.6, sy=1.6, tht=True)
    b.seg(4, 4, AX - 1.4, AY - half_height, 'X', w=0.2, locked=True)
    return b.text()


PROJECT = {
    'board': {'design_settings': {'rules': {
        'min_clearance': 0.2, 'min_hole_clearance': 0.2, 'min_hole_to_hole': 0.25,
        'min_via_diameter': 0.4, 'min_through_hole_diameter': 0.2, 'min_via_annular_width': 0.1,
        'min_track_width': 0.2, 'min_copper_edge_clearance': 0.3}}},
    'net_settings': {
        'classes': [
            {'name': 'Default', 'clearance': 0.2, 'track_width': 0.2, 'via_diameter': RUN_VIA, 'via_drill': 0.3},
            {'name': 'BigVia', 'clearance': 0.2, 'track_width': 0.2, 'via_diameter': CLASS_VIA, 'via_drill': 0.5}],
        'netclass_patterns': [{'netclass': 'BigVia', 'pattern': 'W'}]}}


def route(tmp, tag, text, own_via):
    """Route the board; returns (the vias of net W as (x, y, diameter), the engine's output)."""
    inp = os.path.join(tmp, f'{tag}.kicad_pcb')
    out = os.path.join(tmp, f'{tag}_out.kicad_pcb')
    with open(inp, 'w', encoding='utf-8') as fh:
        fh.write(text)
    with open(inp.replace('.kicad_pcb', '.kicad_pro'), 'w', encoding='utf-8') as fh:
        json.dump(PROJECT, fh)
    env = dict(os.environ, KICAD_PLANE_FINALIZE='0', KICAD_CLASS_VIA_ONLY='1' if own_via else '0', **WRAPPER_ENV)
    done = subprocess.run([sys.executable, '-X', 'utf8', tool('route.py'), inp, out, '--nets', 'W', *WRAPPER_FLAGS],
                          capture_output=True, text=True, env=env, timeout=300)
    log = done.stdout + done.stderr
    assert 'Traceback (most recent call last)' not in log, log[-6000:]
    # --strict-sizes exits 3 when it delivered something below its requirement; the board is still written
    assert done.returncode in (0, 3) and os.path.isfile(out), log[-6000:]
    with open(out, encoding='utf-8') as fh:
        routed = fh.read()
    net = re.search(r'\(net (\d+) "W"\)', routed).group(1)
    vias = [(float(x), float(y), float(size)) for x, y, size, n in re.findall(
        r'\(via\s+\(at ([-\d.]+) ([-\d.]+)\)\s+\(size ([-\d.]+)\).*?\(net (\d+)\)', routed, re.S) if n == net]
    return vias, log, done.returncode


def in_pocket(via, half_height):
    return AX - 1.4 < via[0] < AX + 3.4 and abs(via[1] - AY) < half_height


def pads_connected(log):
    """(connected, total) pads of the multi-pad net, from the engine's last summary."""
    found = re.findall(r'Multi-point:\s+(\d+)/(\d+) pads connected', log)
    return tuple(int(n) for n in found[-1]) if found else None


def main():
    narrow, wide = 0.7, 1.0
    with tempfile.TemporaryDirectory() as tmp:
        print('1. narrow pocket, the engine default: the net searched at the run via')
        vias, log, code = route(tmp, 'narrow_off', board(narrow), own_via=False)
        inside = [v for v in vias if in_pocket(v, narrow)]
        check('pad A leaves its pocket through a via', len(inside) >= 1, f'{vias}')
        check('that via is below its class', bool(inside) and all(abs(v[2] - RUN_VIA) < 1e-6 for v in inside),
              f'{inside}')
        check('the engine reports the descent', 'below the requested size' in log and code == 3, f'exit {code}')
        check('the net is complete', pads_connected(log) == (3, 3), f'{pads_connected(log)}')

        print('2. narrow pocket, KICAD_CLASS_VIA_ONLY=1')
        vias, log, code = route(tmp, 'narrow_on', board(narrow), own_via=True)
        check('no via is below its class', all(v[2] >= CLASS_VIA - 1e-6 for v in vias), f'{vias}')
        check('no via stands in the pocket', not [v for v in vias if in_pocket(v, narrow)], f'{vias}')
        check('pad A is reported open', pads_connected(log) == (2, 3), f'{pads_connected(log)}')
        check('nothing is reported below its size', 'below the requested size' not in log and code == 0,
              f'exit {code}')
        check('the rescue gave the net no pad escape', 'bare-ball escape skipped' in log)

        print('3. wide pocket, KICAD_CLASS_VIA_ONLY=1')
        vias, log, code = route(tmp, 'wide_on', board(wide), own_via=True)
        inside = [v for v in vias if in_pocket(v, wide)]
        check('pad A leaves its pocket through a via', len(inside) >= 1, f'{vias}')
        check('every via has the class size', bool(vias) and all(abs(v[2] - CLASS_VIA) < 1e-6 for v in vias),
              f'{vias}')
        check('the net is complete', pads_connected(log) == (3, 3) and code == 0, f'{pads_connected(log)} exit {code}')

    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'ALL PASS'}")
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
