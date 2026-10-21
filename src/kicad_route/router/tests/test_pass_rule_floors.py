#!/usr/bin/env python3
"""Headless rule-floor regressions adapted from upstream fd59a050 (#1290).

Use the owned router source and its real obstacle maps/plane engines. Parser
and GUI coverage are deliberately outside this headless rule-floor suite.
"""
import contextlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, TESTS_DIR)

def evidence(path, label):
    assert os.path.isfile(path), label
    return path

FAILS = []


def check(name, cond, detail=''):
    if not cond:
        FAILS.append(name)
    print(('  PASS ' if cond else '  FAIL ') + name + (f'  {detail}' if detail else ''))


def quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


# ------------------------------------------------------------------ boards
_N = [0]


def _uid():
    _N[0] += 1
    return '00000000-0000-4000-8000-%012d' % _N[0]


_HEAD = ('(kicad_pcb\n\t(version 20260206)\n\t(generator "test_1290")\n'
         '\t(generator_version "10.0")\n\t(general\n\t\t(thickness 1.6)\n\t)\n'
         '\t(paper "A4")\n\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(2 "B.Cu" signal)\n'
         '\t\t(1 "F.Mask" user)\n\t\t(3 "B.Mask" user)\n'
         '\t\t(5 "F.SilkS" user "F.Silkscreen")\n\t\t(25 "Edge.Cuts" user)\n\t)\n'
         '\t(setup\n\t\t(pad_to_mask_clearance 0)\n\t)\n')


def _pad(net, size=1.0, num='1', old=False, net_spelling='name', pinfunction=None):
    """One SMD pad, quoted (KiCad 10) or in the older unquoted spelling."""
    if old:
        head = '(pad %s smd rect' % num
        layers = '(layers F.Cu F.Mask)'
    else:
        head = '(pad "%s" smd rect' % num
        layers = '(layers "F.Cu" "F.Mask")'
    if net_spelling == 'name':
        node = '(net "%s")' % net
    elif net_spelling == 'bare':
        node = '(net %s)' % net
    else:                       # ('id', n) / ('bare_id', n)
        kind, nid = net_spelling
        node = ('(net %d "%s")' if kind == 'id' else '(net %d %s)') % (nid, net)
    pf = ''
    if pinfunction:
        pf = ('\t\t\t(pinfunction %s)\n' % pinfunction if old
              else '\t\t\t(pinfunction "%s")\n' % pinfunction)
    return ('\t\t%s\n\t\t\t(at 0 0)\n\t\t\t(size %s %s)\n\t\t\t%s\n\t\t\t%s\n%s'
            '\t\t\t(uuid "%s")\n\t\t)\n' % (head, size, size, layers, node, pf, _uid()))


def _fp(ref, x, y, pads):
    return ('\t(footprint "TestPoint:TP"\n\t\t(layer "F.Cu")\n\t\t(uuid "%s")\n'
            '\t\t(at %s %s)\n\t\t(property "Reference" "%s"\n\t\t\t(at 0 -2 0)\n'
            '\t\t\t(layer "F.SilkS")\n\t\t\t(uuid "%s")\n\t\t)\n%s\t)\n'
            % (_uid(), x, y, ref, _uid(), ''.join(pads)))


def _seg(x1, y1, x2, y2, net, w=0.2):
    return ('\t(segment\n\t\t(start %s %s)\n\t\t(end %s %s)\n\t\t(width %s)\n'
            '\t\t(layer "F.Cu")\n\t\t(net "%s")\n\t\t(uuid "%s")\n\t)\n'
            % (x1, y1, x2, y2, w, net, _uid()))


def _outline(pts):
    return ''.join('\t(gr_line\n\t\t(start %s %s)\n\t\t(end %s %s)\n\t\t(stroke\n'
                   '\t\t\t(width 0.05)\n\t\t\t(type default)\n\t\t)\n'
                   '\t\t(layer "Edge.Cuts")\n\t\t(uuid "%s")\n\t)\n'
                   % (a[0], a[1], b[0], b[1], _uid())
                   for a, b in zip(pts, pts[1:] + pts[:1]))


TRAPEZOID = [(100, 140), (150, 140), (140, 110), (110, 110)]   # 71.6-degree sides


def _cls(name, clr, tw):
    return {'name': name, 'clearance': clr, 'track_width': tw,
            'via_diameter': 0.6, 'via_drill': 0.3, 'diff_pair_gap': 0.25,
            'diff_pair_width': 0.2}


def project(name):
    return {'board': {'design_settings': {'rules': {
                'min_clearance': 0.0, 'min_track_width': 0.15,
                'min_via_diameter': 0.5, 'min_through_hole_diameter': 0.3,
                'min_hole_to_hole': 0.25, 'min_hole_clearance': 0.25,
                'min_copper_edge_clearance': 0.5}, 'rule_severities': {}}},
            'meta': {'filename': name + '.kicad_pro', 'version': 3},
            'net_settings': {'classes': [_cls('Default', 0.15, 0.2),
                                         _cls('Power_Bulk', 0.25, 0.75)],
                             'netclass_patterns': [{'netclass': 'Power_Bulk',
                                                    'pattern': 'GND'}],
                             'meta': {'version': 4}}}


def stage(d, name, text):
    pcb = os.path.join(d, name + '.kicad_pcb')
    with open(pcb, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    with open(os.path.join(d, name + '.kicad_pro'), 'w', encoding='utf-8') as f:
        json.dump(project(name), f, indent=2)
    return evidence(pcb, 'staged board')


# ------------------------------------------------------- A. the predicate
def t_predicate():
    print('A. _is_rectangular_outline')
    from plane_obstacle_builder import _is_rectangular_outline as rect
    bb = (0, 0, 10, 6)
    check('a rectangle is rectangular', rect([(0, 0), (10, 0), (10, 6), (0, 6)], bb))
    check('a rectangle with a collinear vertex is rectangular',
          rect([(0, 0), (5, 0), (10, 0), (10, 6), (0, 6)], bb))
    check('a trapezoid is not', not rect([(0, 6), (10, 6), (8, 0), (2, 0)], bb))
    check('a chamfered rectangle is not',
          not rect([(1, 0), (9, 0), (10, 1), (10, 5), (9, 6), (1, 6), (0, 5), (0, 1)], bb))
    check('a flat-top hexagon is not',
          not rect([(2, 0), (8, 0), (10, 3), (8, 6), (2, 6), (0, 3)], bb))
    check('a diamond is not', not rect([(5, 0), (10, 3), (5, 6), (0, 3)], bb))


# ------------------------------------------- B. the slanted edge is stamped
def _left_edge_row(y):
    """(cell gx, signed copper-free distance of the cell centre from the left
    slanted edge -- negative off the board) along grid row y, 0.1 mm grid."""
    (x0, y0), (x1, y1) = TRAPEZOID[0], TRAPEZOID[3]
    L = math.hypot(x1 - x0, y1 - y0)
    out = []
    for gx in range(1000, 1090):
        px = gx * 0.1
        # positive on the board side of the edge (+x)
        out.append((gx, ((x1 - x0) * (y - y0) - (y1 - y0) * (px - x0)) / L))
    return out


def t_slanted_edge():
    print('B. a trapezoid\'s slanted edge reaches the plane and window maps')
    from grid_router import GridObstacleMap
    from kicad_parser import parse_kicad_pcb
    from obstacle_map import build_base_obstacle_map
    from plane_obstacle_builder import _add_board_edge_track_obstacles
    from plane_pad_tap import make_local_window
    from routing_config import GridRouteConfig

    d = tempfile.mkdtemp(prefix='t1290_')
    try:
        pcb_path = stage(d, 'trap', _HEAD + _fp('TP1', 125, 125, [_pad('N1')])
                         + _outline(TRAPEZOID) + ')\n')
        pcb = quiet(parse_kicad_pcb, pcb_path)
        check('the trapezoid parses as its four-vertex outline',
              len(pcb.board_info.board_outline or []) == 4,
              str(pcb.board_info.board_outline))
        cfg = GridRouteConfig(track_width=0.2, clearance=0.15, via_size=0.6,
                              via_drill=0.3, grid_step=0.1, layers=['F.Cu', 'B.Cu'],
                              board_edge_clearance=0.5)
        row_y = 125.0
        gy = int(round(row_y / 0.1))
        band = 0.5 + 0.2 / 2               # the track keep-out, centre to edge

        def row_ok(obs, label, layer=0, x_range=(0.0, 1e9)):
            bad_in, bad_out, bad_free = [], [], []
            for gx, dist in _left_edge_row(row_y):
                if not x_range[0] <= gx * 0.1 <= x_range[1]:
                    continue
                blocked = obs.is_blocked(gx, gy, layer)
                if dist < 0 and not blocked:
                    bad_out.append(round(gx * 0.1, 2))
                elif 0 <= dist < band - 0.05 and not blocked:
                    bad_in.append(round(gx * 0.1, 2))
                elif band + 0.15 < dist < 3.0 and blocked:
                    bad_free.append(round(gx * 0.1, 2))
            check(f'{label}: cells past the slanted edge are blocked',
                  not bad_out, f'free off-board at x={bad_out[:5]}')
            check(f'{label}: cells inside the 0.5 mm band are blocked',
                  not bad_in, f'free in the band at x={bad_in[:5]}')
            check(f'{label}: cells clear of the band stay free',
                  not bad_free, f'blocked at x={bad_free[:5]}')

        plane = GridObstacleMap(1)
        _add_board_edge_track_obstacles(plane, pcb, cfg, 0)
        row_ok(plane, 'plane builder track band')

        window = make_local_window(pcb, 105.5, row_y, 4.0)
        check('a make_local_window copy of the trapezoid keeps its outline',
              len(window.board_info.board_outline or []) == 4)
        nid = next(n for n, net in pcb.nets.items() if net.name == 'N1')
        wmap = quiet(build_base_obstacle_map, window, cfg, [nid])
        # A window map models its own bounds; the rescue fences the rest.
        wb = window.board_info.board_bounds
        row_ok(wmap, 'rescue window map', x_range=(wb[0], wb[2]))
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------- C. the plane net's own class
CX, CY, RING = 108.0, 110.0, 1.0
EDGE = 0.7      # above the project's 0.5: only a pass that takes the FLAG holds it
SNPC = 0.25
FLAGS = ['--escalation', 'off', '--no-fix-drc-settings', '--same-net-pad-clearance',
         str(SNPC), '--board-edge-clearance', str(EDGE), '--hole-to-hole-clearance',
         '0.25', '--via-size', '0.6', '--via-drill', '0.3', '--clearance', '0.15']


def ring_board():
    """TP1 (GND) inside a ring of SIGA track 0.5 mm off its copper, open on the
    right where the ring's ends stop 0.4 mm either side of the pad's axis:
    0.6 mm copper to copper, so a 0.2 mm tap passes at 0.15 (0.2 a side) and
    not at the GND/Default pair's 0.25 -- measured on this layout before the
    fix, the repair engine's tap via landed 0.24 mm from SIGA. U1 is an
    exposed-pad part (a GND EP beside a SIGB pin), the one pad the plane step
    still lays copper for (its thermal via array)."""
    r = RING
    ring = [(CX + r, CY - 0.4), (CX + r, CY - r), (CX - r, CY - r),
            (CX - r, CY + r), (CX + r, CY + r), (CX + r, CY + 0.4)]
    u1 = (_pad('GND', size=2.4)
          + _pad('SIGB', size=0.5, num='2').replace('(at 0 0)', '(at 1.75 0)'))
    return (_HEAD
            + _fp('TP1', CX, CY, [_pad('GND')])
            + _fp('TP2', 104, 104, [_pad('SIGA')])
            + _fp('TP3', 120, 116, [_pad('GND')])
            + _fp('TP4', 120, 104, [_pad('SIGB')])
            + _fp('U1', 115, 106, [u1]).replace('"TestPoint:TP"',
                                                '"Package_DFN_QFN:QFN-16-1EP_3x3mm"')
            + ''.join(_seg(a[0], a[1], b[0], b[1], 'SIGA')
                      for a, b in zip(ring, ring[1:]))
            + _seg(104, 104, CX - r, CY - r, 'SIGA')
            + _seg(116.75, 106, 120, 104, 'SIGB')
            + _outline([(100, 100), (124, 100), (124, 120), (100, 120)])
            + ')\n')


def _cfg_of(a, kw):
    """The GridRouteConfig among a call's arguments."""
    cfg = kw.get('config')
    if cfg is None:
        cfg = next((x for x in a if hasattr(x, 'obstacle_clearance')), None)
    return cfg


@contextlib.contextmanager
def _spies(*targets):
    """Wrap each (module, name, sink, pick) while the block runs; every call
    appends pick(args, kwargs) to its sink, then runs the real function."""
    saved = []
    for mod, name, sink, pick in targets:
        real = getattr(mod, name)

        def spy(*a, _real=real, _sink=sink, _pick=pick, **kw):
            _sink.append(_pick(a, kw))
            return _real(*a, **kw)
        saved.append((mod, name, real))
        setattr(mod, name, spy)
    try:
        yield
    finally:
        for mod, name, real in saved:
            setattr(mod, name, real)


def _run(fn, argv):
    """A CLI's main in-process under `argv`; its output, or AssertionError
    with the tail when it exits non-zero."""
    old_argv, buf = sys.argv, io.StringIO()
    sys.argv = [str(a) for a in argv]
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                fn()
            except SystemExit as e:
                if e.code not in (0, None):
                    raise AssertionError(f'{argv[0]} exited {e.code}:\n'
                                         f'{buf.getvalue()[-1500:]}')
    finally:
        sys.argv = old_argv
    return buf.getvalue()


def _edge_snpc(cfgs):
    return [(round(c.board_edge_clearance, 4), c.same_net_pad_clearance)
            for c in cfgs]


def t_plane_class_floor():
    print('C. the plane engines price foreign copper at the plane net\'s own class,')
    print('   and every finalize / repair sub-pass holds the run\'s edge and via rules')
    import runpy
    from kicad_parser import parse_kicad_pcb
    import kicad_oracle
    import repair_planes
    import route
    import route_planes
    d = tempfile.mkdtemp(prefix='t1290_')
    try:
        pcb = stage(d, 'ring', ring_board())
        board = quiet(parse_kicad_pcb, pcb)
        ids = {net.name: n for n, net in board.nets.items()}

        maps = []
        with _spies((route_planes, 'build_via_obstacle_map', maps, _cfg_of)):
            _run(route_planes.main, ['route_planes.py', pcb, '-O', '--nets', 'GND',
                                     '--plane-layers', 'B.Cu'] + FLAGS)
        evidence(pcb, 'route_planes output')
        shutil.copy(pcb, os.path.join(d, 'poured.kicad_pcb'))
        maps = [c for c in maps if c is not None]
        check('route_planes built a via map for the thermal array (the path under test)',
              bool(maps))
        for name in ('SIGA', 'SIGB'):
            check(f'route_planes prices {name} (Default) at GND\'s 0.25 for the array',
                  all(c.obstacle_clearance(ids[name]) >= 0.25 - 1e-9 for c in maps),
                  str([round(c.obstacle_clearance(ids[name]), 4) for c in maps]))

        # The standalone repair engine, at an explicit edge above the board's.
        taps, subruns, oracles = [], [], []
        with _spies((repair_planes, 'tap_pad_with_escalation', taps, _cfg_of),
                    (route, 'batch_route', subruns,
                     lambda a, kw: (kw.get('board_edge_clearance'),
                                    kw.get('same_net_pad_clearance'))),
                    (kicad_oracle, 'oracle_reconnect', oracles, _cfg_of)):
            _run(repair_planes.main, ['repair_planes.py', pcb, '-O', '--nets', 'GND',
                                      '--plane-layers', 'B.Cu'] + FLAGS)
        taps = [c for c in taps if c is not None]
        check('repair_planes tapped a pad (the path under test)', bool(taps))
        check('repair_planes prices SIGA (Default) at GND\'s 0.25 for the tap',
              all(c.obstacle_clearance(ids['SIGA']) >= 0.25 - 1e-9 for c in taps),
              str([round(c.obstacle_clearance(ids['SIGA']), 4) for c in taps]))
        check('repair_planes ran a reconnect / join sub-run (the path under test)',
              bool(subruns))
        check(f'its sub-runs hold --board-edge-clearance {EDGE} and the #581 '
              f'via clearance', all(e is not None and e >= EDGE - 1e-9 and s == SNPC
                                    for e, s in subruns), str(subruns))
        oracles = [c for c in oracles if c is not None]
        check('repair_planes main ran its oracle recheck (the path under test)',
              bool(oracles))
        check(f'its oracle holds --board-edge-clearance {EDGE} and the #581 via '
              f'clearance', all(e >= EDGE - 1e-9 and s == SNPC
                                for e, s in _edge_snpc(oracles)),
              str(_edge_snpc(oracles)))

        # route.py's in-run plane finalize: its oracle leg.
        fin = os.path.join(d, 'fin.kicad_pcb')
        shutil.copy(os.path.join(d, 'poured.kicad_pcb'), fin)
        shutil.copy(os.path.join(d, 'ring.kicad_pro'), os.path.join(d, 'fin.kicad_pro'))
        legs = []
        with _spies((kicad_oracle, 'oracle_reconnect', legs, _cfg_of)):
            out = _run(lambda: runpy.run_path(os.path.join(ROOT, 'py_router', 'route.py'),
                                              run_name='__main__'),
                       ['route.py', fin, '-O', '--nets', 'GND', '--track-width', '0.2',
                        '--power-nets', 'GND', '--power-nets-widths', '0.75',
                        '--layers', 'F.Cu', 'B.Cu'] + FLAGS)
        legs = [c for c in legs if c is not None]
        check('route.py\'s plane finalize ran its oracle leg (the path under test)',
              bool(legs) and 'Plane finalize' in out)
        check(f'the finalize oracle holds --board-edge-clearance {EDGE} and the '
              f'#581 via clearance', all(e >= EDGE - 1e-9 and s == SNPC
                                         for e, s in _edge_snpc(legs)),
              str(_edge_snpc(legs)))
    finally:
        shutil.rmtree(d, ignore_errors=True)



def t_reweld_spec():
    from types import SimpleNamespace
    from net_rescue import cap_move_reweld_spec
    from routing_config import GridRouteConfig
    cfg = GridRouteConfig(board_edge_clearance=EDGE, same_net_pad_clearance=SNPC)
    spec = cap_move_reweld_spec(cfg, SimpleNamespace(nets={}, pads_by_net={}, footprints={}), [])
    check("cap re-weld keeps explicit edge and same-net pad floors",
          spec.get("board_edge_clearance") == EDGE
          and spec.get("same_net_pad_clearance") == SNPC, str(spec))


if __name__ == "__main__":
    sections = {"outline": t_predicate, "edge-map": t_slanted_edge,
                "plane-passes": t_plane_class_floor, "reweld": t_reweld_spec}
    for section in sys.argv[1:] or sections:
        sections[section]()
    if FAILS:
        raise SystemExit("FAILED: " + "; ".join(FAILS))
    print("ALL PASS")
