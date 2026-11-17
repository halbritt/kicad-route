"""Native I/O and executable vertical-slice checks for the Rust engine."""
import importlib.util
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

import pcbnew
import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('rust_engine_kicad', ROOT / 'tools/rust_engine_kicad.py')
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)
RULES = {'width_nm': 250000, 'clearance_nm': 1000000,
         'grid_nm': 250000, 'edge_clearance_nm': 500000}


def connection_tracks():
    corners = [[3000000, 10000000], [3000000, 2000000],
               [17000000, 2000000], [17000000, 10000000]]
    return [{'id': str(uuid.uuid4()), 'net': 1, 'start': start, 'end': end,
             'width_nm': 250000, 'locked': False}
            for start, end in zip(corners, corners[1:])]


@pytest.fixture
def guarded_cli(tmp_path):
    """Exercise real CLIs with project Python policy execution forbidden.

    Keep the normal host environment. A disposable sitecustomize audit hook
    permits the exact native-I/O exception and rejects other project Python.
    This is a regression probe, not a hostile-code security boundary.
    """
    guard = tmp_path / 'boundary-guard'
    guard.mkdir()
    log = guard / 'audit.jsonl'
    (guard / 'sitecustomize.py').write_text(f'''
import json, pathlib, sys
root = pathlib.Path({str(ROOT)!r}).resolve()
allowed = (root / 'tools/rust_engine_kicad.py').resolve()
log = pathlib.Path({str(log)!r})
def record(row):
    with log.open('a') as out:
        out.write(json.dumps(row) + '\\n')
def check(event, args):
    if event != 'exec':
        return
    filename = args[0].co_filename
    if filename.startswith('<'):
        return
    path = pathlib.Path(filename).resolve()
    if path.is_relative_to(root):
        record({{'path': str(path), 'allowed': path == allowed}})
        if path != allowed:
            raise RuntimeError('ARCHITECTURE_BOUNDARY: project Python policy is forbidden: ' + str(path))
record({{'guard_started': True}})
sys.addaudithook(check)
''')
    env = dict(os.environ)
    env['PYTHONPATH'] = str(guard) + os.pathsep + env.get('PYTHONPATH', '')
    env['PYTHONDONTWRITEBYTECODE'] = '1'

    def run(command, *, check=True):
        return subprocess.run(command, cwd=ROOT, env=env, check=check,
                              capture_output=True, text=True, timeout=60)
    return run, log


@pytest.fixture
def native_board(tmp_path):
    board = pcbnew.BOARD()
    for code, name in ((1, 'SIGNAL'), (2, 'OBSTACLE')):
        board.Add(pcbnew.NETINFO_ITEM(board, name, code))
    corners = [(0, 0), (20000000, 0), (20000000, 20000000), (0, 20000000)]
    for a, b in zip(corners, corners[1:] + corners[:1]):
        edge = pcbnew.PCB_SHAPE(board)
        edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
        edge.SetLayer(pcbnew.Edge_Cuts)
        edge.SetStart(pcbnew.VECTOR2I(*a))
        edge.SetEnd(pcbnew.VECTOR2I(*b))
        edge.SetWidth(50000)
        board.Add(edge)
    for index, x in enumerate((3000000, 17000000), 1):
        footprint = pcbnew.FOOTPRINT(board)
        footprint.SetReference(f'J{index}')
        footprint.SetPosition(pcbnew.VECTOR2I(x, 10000000))
        board.Add(footprint)
        pad = pcbnew.PAD(footprint)
        pad.SetNumber('1')
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(1000000, 1000000))
        pad.SetPosition(pcbnew.VECTOR2I(x, 10000000))
        layers = pcbnew.LSET()
        layers.AddLayer(pcbnew.F_Cu)
        pad.SetLayerSet(layers)
        pad.SetNetCode(1)
        footprint.Add(pad)
    obstacle = pcbnew.PCB_TRACK(board)
    obstacle.SetStart(pcbnew.VECTOR2I(10000000, 4000000))
    obstacle.SetEnd(pcbnew.VECTOR2I(10000000, 16000000))
    obstacle.SetWidth(250000)
    obstacle.SetLayer(pcbnew.F_Cu)
    obstacle.SetNetCode(2)
    obstacle.SetLocked(True)
    board.Add(obstacle)
    path = tmp_path / 'board.kicad_pcb'
    assert pcbnew.SaveBoard(str(path), board)
    return path


def test_native_export_preserves_identity_rules_and_fixed_copper(native_board):
    original = native_board.read_bytes()
    data = bridge.export_board(native_board, RULES, 100000)
    assert data['bounds'] == {'min': [0, 0], 'max': [20000000, 20000000]}
    assert data['rules'] == RULES
    assert 'limits' not in data  # Existing inputs continue to use Rust defaults.
    assert len(data['pads']) == 2
    assert {tuple(p['at']) for p in data['pads']} == {(3000000, 10000000), (17000000, 10000000)}
    assert data['tracks'][0]['locked'] is True
    assert data['tracks'][0]['start'] == [10000000, 4000000]
    assert len({p['id'] for p in data['pads']} | {t['id'] for t in data['tracks']}) == 3
    assert native_board.read_bytes() == original


def test_native_adapter_applies_exact_edit_list(native_board):
    original = native_board.read_bytes()
    exported = bridge.export_board(native_board, RULES, 100000)
    added = connection_tracks()
    plan = {'version': 1, 'source_id': exported['source_id'], 'status': 'complete',
            'added_tracks': added, 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
    output = native_board.with_name('output.kicad_pcb')
    bridge.apply_plan(native_board, plan, output)
    actual = bridge.export_board(output, RULES, 100000)
    assert {t['id']: t for t in actual['tracks']} == {
        t['id']: t for t in exported['tracks'] + added}
    assert actual['pads'] == exported['pads']
    assert native_board.read_bytes() == original


@pytest.mark.parametrize('variant', ['circle', 'rect_90', 'net_zero_pad', 'foreign_pad',
    'two_signal_nets', 'joined_pads_stray_track', 'track_components', 'clipped_goal',
    'first_contact', 'edge_stroke_boundary'])
def test_rust_route_preserves_copper_and_passes_native_connectivity_drc(native_board, guarded_cli, variant):
    run, audit_log = guarded_cli
    rules = RULES
    if variant != 'circle':
        board = pcbnew.LoadBoard(str(native_board))
        if variant == 'rect_90':
            for pad in board.GetPads():
                pad.SetShape(pcbnew.PAD_SHAPE_RECT)
                pad.SetSize(pcbnew.VECTOR2I(2000000, 1000000))
                pad.SetOrientationDegrees(90)
        elif variant == 'edge_stroke_boundary':
            # Carbon's native probe: clearance is measured from the outline
            # centerline, including when its visible stroke is 0.2 mm wide.
            rules = {**RULES, 'grid_nm': 125000}
            for edge in board.GetDrawings():
                edge.SetWidth(200000)
            for footprint in board.GetFootprints():
                footprint.SetPosition(pcbnew.VECTOR2I(footprint.GetPosition().x, 625000))
                for pad in footprint.Pads():
                    pad.SetSize(pcbnew.VECTOR2I(250000, 250000))
        elif variant == 'first_contact':
            # Fable F1, scaled uniformly by five to meet the exporter's 1 mm
            # native clearance floor. The original integer Rust witness stays
            # unchanged. The target centerline is clearance-shadowed; its near
            # copper edge admits a legal endpoint between lattice vertices.
            rules = {'width_nm': 1500000, 'clearance_nm': 1000000,
                     'grid_nm': 2500000, 'edge_clearance_nm': 1000000}
            for edge in board.GetDrawings():
                a, b = edge.GetStart(), edge.GetEnd()
                edge.SetStart(pcbnew.VECTOR2I(a.x * 5 // 2, a.y * 5 // 2))
                edge.SetEnd(pcbnew.VECTOR2I(b.x * 5 // 2, b.y * 5 // 2))
            for footprint in list(board.GetFootprints()):
                if footprint.GetReference() == 'J2':
                    board.Remove(footprint)
                else:
                    footprint.SetPosition(pcbnew.VECTOR2I(25000000, 10000000))
                    for pad in footprint.Pads():
                        pad.SetSize(pcbnew.VECTOR2I(3000000, 3000000))
            wall = next(iter(board.GetTracks()))
            wall.SetStart(pcbnew.VECTOR2I(10000000, 31500000))
            wall.SetEnd(pcbnew.VECTOR2I(40000000, 31500000))
            wall.SetWidth(500000)
            target = pcbnew.PCB_TRACK(board)
            target.SetLayer(pcbnew.F_Cu)
            target.SetNetCode(1)
            target.SetStart(pcbnew.VECTOR2I(10000000, 30000000))
            target.SetEnd(pcbnew.VECTOR2I(40000000, 30000000))
            target.SetWidth(500000)
            target.SetLocked(True)
            board.Add(target)
        elif variant == 'clipped_goal':
            rules = {**RULES, 'grid_nm': 10000000, 'width_nm': 2000000}
            for footprint in list(board.GetFootprints()):
                if footprint.GetReference() == 'J2':
                    board.Remove(footprint)
                else:
                    footprint.SetPosition(pcbnew.VECTOR2I(10000000, 10000000))
                    for pad in footprint.Pads():
                        pad.SetSize(pcbnew.VECTOR2I(2000000, 2000000))
            wall = next(iter(board.GetTracks()))
            wall.SetStart(pcbnew.VECTOR2I(18000000, 1000000))
            wall.SetEnd(pcbnew.VECTOR2I(18000000, 19000000))
            wall.SetWidth(2000000)
            target = pcbnew.PCB_TRACK(board)
            target.SetLayer(pcbnew.F_Cu)
            target.SetNetCode(1)
            target.SetStart(pcbnew.VECTOR2I(15000000, 5000000))
            target.SetEnd(pcbnew.VECTOR2I(15000000, 15000000))
            target.SetWidth(2000000)
            target.SetLocked(True)
            board.Add(target)
        elif variant in ('joined_pads_stray_track', 'track_components'):
            additions = connection_tracks() if variant == 'joined_pads_stray_track' else []
            additions.append({
                'net': 1 if variant == 'joined_pads_stray_track' else 2,
                'start': [12000000, 16500137] if variant == 'joined_pads_stray_track' else [14000000, 4000000],
                'end': [14000000, 17500137] if variant == 'joined_pads_stray_track' else [14000000, 8000000],
                'width_nm': 250000, 'locked': True,
            })
            for item in additions:
                track = pcbnew.PCB_TRACK(board)
                track.SetLayer(pcbnew.F_Cu)
                track.SetNetCode(item['net'])
                track.SetStart(pcbnew.VECTOR2I(*item['start']))
                track.SetEnd(pcbnew.VECTOR2I(*item['end']))
                track.SetWidth(item['width_nm'])
                track.SetLocked(item['locked'])
                board.Add(track)
        else:
            net = 0 if variant == 'net_zero_pad' else 2
            positions = [(10000000, 2000000)]
            if variant == 'two_signal_nets':
                net = 3
                board.Add(pcbnew.NETINFO_ITEM(board, 'SECOND_SIGNAL', net))
                positions = [(3000000, 5000000), (17000000, 5000000)]
            for index, position in enumerate(positions, 3):
                footprint = pcbnew.FOOTPRINT(board)
                footprint.SetReference(f'J{index}')
                footprint.SetPosition(pcbnew.VECTOR2I(*position))
                board.Add(footprint)
                pad = pcbnew.PAD(footprint)
                pad.SetNumber('1')
                pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
                pad.SetSize(pcbnew.VECTOR2I(1000000, 1000000))
                pad.SetPosition(pcbnew.VECTOR2I(*position))
                layers = pcbnew.LSET()
                layers.AddLayer(pcbnew.F_Cu)
                pad.SetLayerSet(layers)
                pad.SetNetCode(net)
                footprint.Add(pad)
        assert pcbnew.SaveBoard(str(native_board), board)
    original = native_board.read_bytes()
    request = native_board.with_name('request.json')
    plan_path = native_board.with_name('plan.json')
    rules_path = native_board.with_name('rules.json')
    rules_path.write_text(json.dumps(rules))
    export_command = ['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'), 'export',
                      str(native_board), str(request), '--rules', str(rules_path)]
    if variant == 'circle':
        limits_path = native_board.with_name('limits.json')
        limits_path.write_text(json.dumps({'max_work': 100000000}))
        export_command.extend(['--limits', str(limits_path)])
    run(export_command)
    exported = json.loads(request.read_text())
    if variant == 'circle':
        assert exported['limits'] == {'max_work': 100000000}
    if variant == 'rect_90':
        assert all(p['shape'] == {'kind': 'rect', 'half_size_nm': [500000, 1000000]}
                   for p in exported['pads'])
    run(
        ['cargo', 'run', '--locked', '--offline', '--manifest-path',
         str(ROOT / 'rust-engine/Cargo.toml'), '--', str(request), str(plan_path)],
    )
    plan = json.loads(plan_path.read_text())
    assert plan['status'] == 'complete'
    assert plan['added_tracks']
    assert 0 < plan['expanded_states'] <= exported['max_expansions']
    assert type(plan['work_used']) is int and 0 < plan['work_used'] <= 100000000
    if variant == 'first_contact':
        assert any(29000001 <= endpoint[1] <= 29500000
                   for track in plan['added_tracks']
                   for endpoint in (track['start'], track['end']))
    elif variant == 'edge_stroke_boundary':
        assert all(track['start'][1] == track['end'][1] == 625000
                   for track in plan['added_tracks'])
    output = native_board.with_name('routed.kicad_pcb')
    run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'), 'apply',
         str(native_board), str(plan_path), str(output)])
    observed = [json.loads(line) for line in audit_log.read_text().splitlines()]
    assert sum(row.get('guard_started', False) for row in observed) >= 2
    assert sum(row.get('allowed', False) for row in observed) >= 2
    assert all(row.get('allowed', True) for row in observed)
    actual = bridge.export_board(output, rules, 100000)
    assert actual['rules'] == exported['rules']
    assert actual['pads'] == exported['pads']
    assert actual['nets'] == exported['nets']
    assert {t['id']: t for t in actual['tracks']} == {
        t['id']: t for t in exported['tracks'] + plan['added_tracks']}
    assert native_board.read_bytes() == original

    reports = []
    before_opens = 2 if variant in ('two_signal_nets', 'foreign_pad', 'track_components') else 1
    for label, path, expected_opens in [('before', native_board, before_opens), ('after', output, 0)]:
        board = pcbnew.LoadBoard(str(path))
        board.BuildConnectivity()
        assert board.GetConnectivity().GetUnconnectedCount(False) == expected_opens
        report_path = native_board.with_name(f'{label}-drc.json')
        subprocess.run(
            ['kicad-cli', 'pcb', 'drc', '--format', 'json', '--severity-all', '--all-track-errors',
             '--output', str(report_path), str(path)],
            check=True, capture_output=True, text=True, timeout=30,
        )
        report = json.loads(report_path.read_text())
        assert len(report['unconnected_items']) == expected_opens
        reports.append(report)
    # The intentionally isolated protected track has a pre-existing dangling
    # warning. No new warning or error is allowed, including new copper items.
    key = lambda violation: json.dumps(violation, sort_keys=True)
    assert not (Counter(map(key, reports[1]['violations']))
                - Counter(map(key, reports[0]['violations'])))


@pytest.mark.parametrize('tamper,reason', [
    ('empty', 'open connections'), ('crossing', 'DRC violations'),
    ('thin', 'native minimum'),
])
def test_native_apply_rejects_false_complete_plans_before_publication(native_board, tamper, reason):
    original = native_board.read_bytes()
    added = connection_tracks()
    if tamper == 'empty':
        added = []
    elif tamper == 'crossing':
        added = [{**added[0], 'end': [17000000, 10000000]}]
    else:
        added[0]['width_nm'] = 1
    plan = {'version': 1, 'status': 'complete',
            'source_id': hashlib.sha256(original).hexdigest(),
            'added_tracks': added, 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
    output = native_board.with_name('rejected.kicad_pcb')
    with pytest.raises(bridge.NativeValidationError, match=reason):
        bridge.apply_plan(native_board, plan, output)
    assert not output.exists()
    assert not list(native_board.parent.glob('.rejected-*'))
    assert native_board.read_bytes() == original


def test_native_apply_cli_reports_typed_validation_failure(native_board, guarded_cli):
    run, _ = guarded_cli
    plan = native_board.with_name('empty-plan.json')
    plan.write_text(json.dumps({'version': 1, 'status': 'complete',
        'source_id': hashlib.sha256(native_board.read_bytes()).hexdigest(),
        'added_tracks': [], 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}))
    output = native_board.with_name('rejected.kicad_pcb')
    result = run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'),
                  'apply', str(native_board), str(plan), str(output)], check=False)
    assert result.returncode == 2
    assert json.loads(result.stderr.splitlines()[-1])['status'] == 'native_validation_failed'
    assert not output.exists()


def test_missing_native_drc_tool_cannot_publish_a_candidate(native_board, monkeypatch, tmp_path):
    empty_path = tmp_path / 'no-native-tool'
    empty_path.mkdir()
    monkeypatch.setenv('PATH', str(empty_path))
    plan = {'version': 1, 'status': 'complete',
            'source_id': hashlib.sha256(native_board.read_bytes()).hexdigest(),
            'added_tracks': connection_tracks(), 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
    output = native_board.with_name('no-drc-output.kicad_pcb')
    with pytest.raises(bridge.NativeValidationError, match='native DRC did not produce'):
        bridge.apply_plan(native_board, plan, output)
    assert not output.exists()
    assert not list(native_board.parent.glob('.no-drc-output-*'))


@pytest.mark.parametrize('path', ['src/kicad_route/cli.py',
                                  'src/kicad_route/router/py_router/route.py'])
def test_actual_legacy_entrypoints_cannot_pass_the_candidate_boundary(guarded_cli, path):
    run, audit_log = guarded_cli
    result = run(['/usr/bin/python3', str(ROOT / path), '--help'], check=False)
    assert result.returncode != 0
    assert 'ARCHITECTURE_BOUNDARY' in result.stderr
    observed = [json.loads(line) for line in audit_log.read_text().splitlines()]
    assert {'path': str(ROOT / path), 'allowed': False} in observed


@pytest.mark.parametrize('contact', ['axis_tangent', 'diagonal_sub_nm'])
def test_rust_does_not_report_tangent_copper_as_connected(native_board, contact):
    board = pcbnew.LoadBoard(str(native_board))
    board.Remove(next(iter(board.GetTracks())))
    rules = RULES
    segments = [((3000000, 10000000), (16400000, 10000000))]
    if contact == 'diagonal_sub_nm':
        # Retained native/Rust counterexample: 0.185 nm ideal overlap still
        # leaves a native open. The shifted second terminal fits a 0.1 mm grid.
        positions = {'J1': (1000000, 4000000), 'J2': (2400000, 5400000)}
        for footprint in board.GetFootprints():
            footprint.SetPosition(pcbnew.VECTOR2I(*positions[footprint.GetReference()]))
            for pad in footprint.Pads():
                pad.SetSize(pcbnew.VECTOR2I(200000, 200000))
        segments = [((1000000, 4000000), (3142620, 3416878)),
                    ((2050220, 3921453), (2444123, 5368809))]
        rules = {**RULES, 'grid_nm': 100000}
    for start, end in segments:
        track = pcbnew.PCB_TRACK(board)
        track.SetLayer(pcbnew.F_Cu)
        track.SetNetCode(1)
        track.SetStart(pcbnew.VECTOR2I(*start))
        track.SetEnd(pcbnew.VECTOR2I(*end))
        track.SetWidth(200000)
        board.Add(track)
    assert pcbnew.SaveBoard(str(native_board), board)
    board = pcbnew.LoadBoard(str(native_board))
    board.BuildConnectivity()
    assert board.GetConnectivity().GetUnconnectedCount(False) == 1
    original = native_board.read_bytes()
    exported = bridge.export_board(native_board, rules, 100000)
    request = native_board.with_name('request.json')
    plan_path = native_board.with_name('plan.json')
    request.write_text(json.dumps(exported))
    subprocess.run(
        ['cargo', 'run', '--locked', '--offline', '--manifest-path',
         str(ROOT / 'rust-engine/Cargo.toml'), '--', str(request), str(plan_path)],
        cwd=ROOT, check=True, capture_output=True, text=True, timeout=60,
    )
    plan = json.loads(plan_path.read_text())
    assert plan['status'] == 'complete'
    assert plan['added_tracks'], 'KiCad still has an open: tangent contact cannot skip routing'
    output = native_board.with_name('routed.kicad_pcb')
    bridge.apply_plan(native_board, plan, output)
    board = pcbnew.LoadBoard(str(output))
    board.BuildConnectivity()
    assert board.GetConnectivity().GetUnconnectedCount(False) == 0
    actual = bridge.export_board(output, rules, 100000)
    assert {t['id']: t for t in actual['tracks']} == {
        t['id']: t for t in exported['tracks'] + plan['added_tracks']}
    assert native_board.read_bytes() == original


@pytest.mark.parametrize('suffix', ['.kicad_pro', '.kicad_dru'])
def test_native_adapter_rejects_unimplemented_rule_import(native_board, suffix):
    native_board.with_suffix(suffix).write_text('{}')
    with pytest.raises(bridge.UnsupportedBoard, match='project/custom-rule'):
        bridge.export_board(native_board, RULES, 100000)


def test_native_adapter_rejects_below_native_floor(native_board):
    with pytest.raises(ValueError, match='below native floor'):
        bridge.export_board(native_board, {**RULES, 'width_nm': 100000}, 100000)


@pytest.mark.parametrize('feature', [
    'pad_offset', 'pad_clearance', 'footprint_clearance', 'copper_field',
    'footprint_edge', 'footprint_zone', 'net_tie', 'pad_mask', 'footprint_mask', 'board_mask',
])
def test_native_adapter_rejects_unrepresented_native_geometry_and_rules(native_board, feature):
    board = pcbnew.LoadBoard(str(native_board))
    footprint = next(iter(board.GetFootprints()))
    pad = next(iter(footprint.Pads()))
    if feature == 'pad_offset':
        pad.SetOffset(pcbnew.VECTOR2I(1000000, 0))
    elif feature == 'pad_clearance':
        pad.SetLocalClearance(2000000)
    elif feature == 'footprint_clearance':
        footprint.SetLocalClearance(2000000)
    elif feature == 'pad_mask':
        pad.SetLocalSolderMaskMargin(2000000)
    elif feature == 'footprint_mask':
        footprint.SetLocalSolderMaskMargin(2000000)
    elif feature == 'board_mask':
        board.GetDesignSettings().m_SolderMaskExpansion = 2000000
    elif feature == 'copper_field':
        footprint.Reference().SetLayer(pcbnew.F_Cu)
        footprint.Reference().SetVisible(True)
    elif feature == 'footprint_edge':
        edge = pcbnew.PCB_SHAPE(footprint)
        edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
        edge.SetLayer(pcbnew.Edge_Cuts)
        edge.SetStart(pcbnew.VECTOR2I(5000000, 5000000))
        edge.SetEnd(pcbnew.VECTOR2I(15000000, 5000000))
        footprint.Add(edge)
    elif feature == 'footprint_zone':
        zone = pcbnew.ZONE(footprint)
        zone.SetLayer(pcbnew.F_Cu)
        zone.SetIsRuleArea(True)
        zone.SetDoNotAllowTracks(True)
        polygon = zone.Outline()
        polygon.NewOutline()
        for x, y in [(5000000, 5000000), (15000000, 5000000), (15000000, 15000000)]:
            polygon.Append(x, y)
        footprint.Add(zone)
    elif feature == 'net_tie':
        footprint.AddNetTiePadGroup('1,2')
    assert pcbnew.SaveBoard(str(native_board), board)
    with pytest.raises(bridge.UnsupportedBoard):
        bridge.export_board(native_board, RULES, 100000)
    output = native_board.with_name('unsupported-output.kicad_pcb')
    plan = {'version': 1, 'status': 'complete',
            'source_id': hashlib.sha256(native_board.read_bytes()).hexdigest(),
            'added_tracks': connection_tracks(), 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
    with pytest.raises(bridge.UnsupportedBoard):
        bridge.apply_plan(native_board, plan, output)
    assert not output.exists()


@pytest.mark.parametrize('location', ['board', 'footprint'])
@pytest.mark.parametrize('layer', [pcbnew.Margin, pcbnew.Dwgs_User])
def test_native_margin_geometry_is_rejected_but_documentation_graphics_are_allowed(
        native_board, location, layer):
    board = pcbnew.LoadBoard(str(native_board))
    parent = board if location == 'board' else next(iter(board.GetFootprints()))
    shape = pcbnew.PCB_SHAPE(parent)
    shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
    shape.SetLayer(layer)
    shape.SetStart(pcbnew.VECTOR2I(10000000, 5000000))
    shape.SetEnd(pcbnew.VECTOR2I(10000000, 15000000))
    shape.SetWidth(50000)
    parent.Add(shape)
    assert pcbnew.SaveBoard(str(native_board), board)
    # Save/reload and native DRC establish why otherwise identical drawings
    # have different acceptance outcomes; this is not only a layer-name check.
    report_path = native_board.with_name('margin-drc.json')
    subprocess.run(
        ['kicad-cli', 'pcb', 'drc', '--format', 'json', '--severity-all',
         '--output', str(report_path), str(native_board)],
        check=True, capture_output=True, text=True, timeout=30,
    )
    report = json.loads(report_path.read_text())
    edge_errors = [v for v in report['violations'] if v['type'] == 'copper_edge_clearance']
    output = native_board.with_name('margin-output.kicad_pcb')
    plan = {'version': 1, 'status': 'complete',
            'source_id': hashlib.sha256(native_board.read_bytes()).hexdigest(),
            'added_tracks': connection_tracks(), 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
    if layer == pcbnew.Margin:
        assert edge_errors, 'native DRC must demonstrate the unrepresented rule'
        with pytest.raises(bridge.UnsupportedBoard, match='Margin'):
            bridge.export_board(native_board, RULES, 100000)
        with pytest.raises(bridge.UnsupportedBoard, match='Margin'):
            bridge.apply_plan(native_board, plan, output)
        assert not output.exists()
    else:
        assert not edge_errors
        bridge.export_board(native_board, RULES, 100000)
        bridge.apply_plan(native_board, plan, output)
        assert output.exists()


@pytest.mark.parametrize('status,source', [('unroutable', None), ('complete', 'stale')])
def test_native_adapter_does_not_write_failed_or_stale_plan(native_board, status, source):
    original = native_board.read_bytes()
    data = bridge.export_board(native_board, RULES, 100000)
    plan = {'version': 1, 'status': status, 'source_id': source or data['source_id'],
            'added_tracks': [], 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
    output = native_board.with_name('output.kicad_pcb')
    with pytest.raises(ValueError):
        bridge.apply_plan(native_board, plan, output)
    assert not output.exists()
    assert native_board.read_bytes() == original


@pytest.mark.parametrize('operation', ['export', 'apply'])
def test_native_operations_pin_one_snapshot_without_source_sidecars(native_board, monkeypatch, operation):
    original = native_board.read_bytes()
    expected = bridge.export_board(native_board, RULES, 100000)
    changed = pcbnew.LoadBoard(str(native_board))
    for footprint in changed.GetFootprints():
        if footprint.GetReference() == 'J2':
            footprint.SetPosition(pcbnew.VECTOR2I(17000000, 12000000))
    staging = native_board.with_name('concurrent.kicad_pcb')
    assert pcbnew.SaveBoard(str(staging), changed)
    changed_bytes = staging.read_bytes()
    staging.unlink()
    before_names = set(native_board.parent.iterdir())
    read_bytes = Path.read_bytes
    reads = []

    def concurrent_read(path):
        value = read_bytes(path)
        if path == native_board:
            reads.append(path)
            native_board.write_bytes(changed_bytes)
        return value

    monkeypatch.setattr(Path, 'read_bytes', concurrent_read)
    output = native_board.with_name('snapshot-output.kicad_pcb')
    if operation == 'export':
        result = bridge.export_board(native_board, RULES, 100000)
        assert result == expected
        expected_names = before_names
    else:
        plan = {'version': 1, 'status': 'complete',
                'source_id': hashlib.sha256(original).hexdigest(),
                'added_tracks': connection_tracks(), 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
        bridge.apply_plan(native_board, plan, output)
        assert bridge.export_board(output, RULES, 100000)['pads'] == expected['pads']
        expected_names = before_names | {output}
    assert len(reads) == 1
    assert read_bytes(native_board) == changed_bytes
    assert set(native_board.parent.iterdir()) == expected_names


def test_native_publication_preserves_a_destination_created_during_validation(native_board, monkeypatch):
    plan = {'version': 1, 'status': 'complete',
            'source_id': hashlib.sha256(native_board.read_bytes()).hexdigest(),
            'added_tracks': connection_tracks(), 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
    output = native_board.with_name('concurrent-output.kicad_pcb')
    real_link = bridge.os.link

    def race(source, destination):
        destination.write_bytes(b'concurrent owner output')
        return real_link(source, destination)

    monkeypatch.setattr(bridge.os, 'link', race)
    with pytest.raises(FileExistsError):
        bridge.apply_plan(native_board, plan, output)
    assert output.read_bytes() == b'concurrent owner output'
    assert not list(native_board.parent.glob('.concurrent-output-*'))


def test_native_drc_timeout_refuses_publication(native_board, monkeypatch):
    plan = {'version': 1, 'status': 'complete',
            'source_id': hashlib.sha256(native_board.read_bytes()).hexdigest(),
            'added_tracks': connection_tracks(), 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
    output = native_board.with_name('timeout-output.kicad_pcb')

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])

    monkeypatch.setattr(bridge.subprocess, 'run', timeout)
    with pytest.raises(bridge.NativeValidationError, match='timed out'):
        bridge.apply_plan(native_board, plan, output)
    assert not output.exists()
    assert not list(native_board.parent.glob('.timeout-output-*'))


@pytest.mark.parametrize('case,status', [
    ('stale', 'invalid'), ('failed_plan', 'invalid'), ('missing_field', 'invalid'),
    ('boolean_net', 'invalid'), ('lock_string', 'invalid'), ('huge_width', 'invalid'),
    ('bad_coordinate', 'invalid'), ('zero_length', 'invalid'), ('duplicate_id', 'invalid'),
    ('duplicate_json', 'invalid'), ('invalid_json', 'invalid'), ('root_list', 'invalid'),
    ('missing_file', 'io_error'), ('existing_output', 'output_exists'),
    ('unsupported_rules', 'unsupported'),
])
def test_apply_cli_reports_input_failures_without_publication(native_board, guarded_cli, case, status):
    run, _ = guarded_cli
    original = native_board.read_bytes()
    additions = connection_tracks()
    plan = {'version': 1, 'status': 'complete',
            'source_id': hashlib.sha256(original).hexdigest(),
            'added_tracks': additions, 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
    if case == 'stale':
        plan['source_id'] = '0' * 64
    elif case == 'failed_plan':
        plan['status'] = 'budget_exhausted'
    elif case == 'missing_field':
        del additions[0]['start']
    elif case == 'boolean_net':
        additions[0]['net'] = True
    elif case == 'lock_string':
        additions[0]['locked'] = 'false'
    elif case == 'huge_width':
        additions[0]['width_nm'] = 2 ** 32
    elif case == 'bad_coordinate':
        additions[0]['start'] = 42
    elif case == 'zero_length':
        additions.append({**additions[0], 'id': str(uuid.uuid4()), 'end': additions[0]['start']})
    elif case == 'duplicate_id':
        additions[1]['id'] = additions[0]['id']
    elif case == 'unsupported_rules':
        native_board.with_suffix('.kicad_dru').write_text('(version 1)')
    plan_path = native_board.with_name('invalid-plan.json')
    text = json.dumps(plan)
    if case == 'duplicate_json':
        text = text.replace('"version": 1', '"version": 1, "version": 1')
    elif case == 'invalid_json':
        text = '{'
    elif case == 'root_list':
        text = '[]'
    if case != 'missing_file':
        plan_path.write_text(text)
    output = native_board.with_name('invalid-output.kicad_pcb')
    if case == 'existing_output':
        output.write_bytes(b'existing owner output')
    result = run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'),
                  'apply', str(native_board), str(plan_path), str(output)], check=False)
    assert result.returncode == 2
    outcome = json.loads(result.stderr.splitlines()[-1])
    assert outcome['status'] == status
    assert outcome['reason']
    assert 'Traceback' not in result.stderr
    if case == 'existing_output':
        assert output.read_bytes() == b'existing owner output'
    else:
        assert not output.exists()
    assert not list(native_board.parent.glob('.invalid-output-*'))
    assert native_board.read_bytes() == original


@pytest.mark.parametrize('case,status', [('rules_list', 'invalid'),
    ('native_floor', 'invalid'), ('missing_source', 'io_error'),
    ('malformed_board', 'invalid'),
    ('project_rules', 'unsupported'), ('existing_output', 'output_exists')])
def test_export_cli_reports_input_failures_without_publication(native_board, guarded_cli, case, status):
    run, _ = guarded_cli
    original = native_board.read_bytes()
    rules = [] if case == 'rules_list' else dict(RULES)
    if case == 'native_floor':
        rules['width_nm'] = 1
    if case == 'project_rules':
        native_board.with_suffix('.kicad_pro').write_text('{}')
    rules_path = native_board.with_name('rules.json')
    rules_path.write_text(json.dumps(rules))
    source = native_board.with_name('missing.kicad_pcb') if case == 'missing_source' else native_board
    if case == 'malformed_board':
        source = native_board.with_name('malformed.kicad_pcb')
        source.write_text('not a KiCad board\n')
    output = native_board.with_name('failed-request.json')
    if case == 'existing_output':
        output.write_bytes(b'existing owner output')
    result = run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'),
                  'export', str(source), str(output), '--rules', str(rules_path)], check=False)
    assert result.returncode == 2
    assert json.loads(result.stderr.splitlines()[-1])['status'] == status
    assert 'Traceback' not in result.stderr
    if case == 'existing_output':
        assert output.read_bytes() == b'existing owner output'
    else:
        assert not output.exists()
    assert not list(native_board.parent.glob('.failed-request-*'))
    assert native_board.read_bytes() == original


def test_export_write_failure_does_not_publish_partial_json(tmp_path, monkeypatch):
    output = tmp_path / 'request.json'
    write = Path.write_text

    def fail(path, text, **kwargs):
        write(path, text[:4], **kwargs)
        raise OSError('injected short write')

    monkeypatch.setattr(Path, 'write_text', fail)
    with pytest.raises(OSError, match='short write'):
        bridge.write_request({'version': 1}, output)
    assert not output.exists()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('report', [[], {'violations': [], 'unconnected_items': {}}, {}])
def test_invalid_native_report_is_a_typed_validation_failure(tmp_path, monkeypatch, report):
    output = tmp_path / 'drc.json'

    def invalid_report(command, **kwargs):
        output.write_text(json.dumps(report))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bridge.subprocess, 'run', invalid_report)
    with pytest.raises(bridge.NativeValidationError, match='invalid violation/connectivity'):
        bridge.native_violations(tmp_path / 'board.kicad_pcb', output)


@pytest.mark.parametrize('counter', ['work_used', 'expanded_states'])
@pytest.mark.parametrize('value', ['missing', None, True, -1, 1.5, '1', 2**64])
def test_native_apply_rejects_invalid_work_counters(native_board, guarded_cli, counter, value):
    run, _ = guarded_cli
    original = native_board.read_bytes()
    plan = {'version': 1, 'status': 'complete',
            'source_id': hashlib.sha256(original).hexdigest(),
            'added_tracks': connection_tracks(), 'removed_track_ids': [],
            'expanded_states': 0, 'work_used': 0}
    if value == 'missing':
        del plan[counter]
    else:
        plan[counter] = value
    plan_path = native_board.with_name('counter-plan.json')
    plan_path.write_text(json.dumps(plan))
    output = native_board.with_name('counter-output.kicad_pcb')
    result = run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'),
                  'apply', str(native_board), str(plan_path), str(output)], check=False)
    assert result.returncode == 2
    failure = json.loads(result.stderr.splitlines()[-1])
    assert failure['status'] == 'invalid' and counter in failure['reason']
    assert not output.exists()
    assert native_board.read_bytes() == original


@pytest.mark.parametrize('limits_text,valid', [
    ('{}', True), ('{"max_work": 0, "max_depth": 1}', True),
    ('{"future_engine_policy": 7}', True),  # Rust owns schema/policy rejection.
    ('[]', False), ('null', False), ('{', False),
    ('{"max_work": 1, "max_work": 2}', False),
])
def test_export_budget_limits_json(native_board, guarded_cli, limits_text, valid):
    run, _ = guarded_cli
    original = native_board.read_bytes()
    rules_path = native_board.with_name('rules.json')
    rules_path.write_text(json.dumps(RULES))
    limits_path = native_board.with_name('limits.json')
    limits_path.write_text(limits_text)
    output = native_board.with_name('limited-request.json')
    result = run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'),
                  'export', str(native_board), str(output), '--rules', str(rules_path),
                  '--limits', str(limits_path)], check=False)
    if valid:
        assert result.returncode == 0, result.stderr
        assert json.loads(output.read_text())['limits'] == json.loads(limits_text)
    else:
        assert result.returncode == 2
        assert json.loads(result.stderr.splitlines()[-1])['status'] == 'invalid'
        assert not output.exists()
    assert native_board.read_bytes() == original


@pytest.mark.parametrize('limits,status,exit_code', [
    ({'max_work': 0}, 'work_exhausted', 5),
    ({'max_items': 0}, 'admission_exceeded', 6),
    ({'max_search_entries': 0}, 'admission_exceeded', 6),
    ({'max_journal_entries': 0}, 'journal_exhausted', 6),
    ({'max_depth': 0}, 'nesting_exhausted', 6),
])
def test_rust_budget_exhaustion_cannot_publish_native_output(
        native_board, guarded_cli, limits, status, exit_code):
    run, audit_log = guarded_cli
    original = native_board.read_bytes()
    rules_path = native_board.with_name('rules.json')
    rules_path.write_text(json.dumps(RULES))
    limits_path = native_board.with_name('limits.json')
    limits_path.write_text(json.dumps(limits))
    request = native_board.with_name('limited-request.json')
    plan_path = native_board.with_name('limited-plan.json')
    run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'), 'export',
         str(native_board), str(request), '--rules', str(rules_path),
         '--limits', str(limits_path)])
    result = run(['cargo', 'run', '--locked', '--offline', '--manifest-path',
                  str(ROOT / 'rust-engine/Cargo.toml'), '--', str(request), str(plan_path)],
                 check=False)
    assert result.returncode == exit_code, result.stderr
    plan = json.loads(plan_path.read_text())
    assert plan['status'] == status and plan['reason']
    assert plan['added_tracks'] == [] and plan['removed_track_ids'] == []
    assert type(plan['work_used']) is int and plan['work_used'] >= 0
    if 'max_work' in limits:
        assert plan['work_used'] <= limits['max_work']
    output = native_board.with_name('limited-output.kicad_pcb')
    applied = run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'),
                   'apply', str(native_board), str(plan_path), str(output)], check=False)
    assert applied.returncode == 2
    assert json.loads(applied.stderr.splitlines()[-1])['status'] == 'invalid'
    assert not output.exists() and not list(native_board.parent.glob('.limited-output-*'))
    assert native_board.read_bytes() == original
    observations = [json.loads(line) for line in audit_log.read_text().splitlines()]
    assert sum(row.get('guard_started', False) for row in observations) >= 2
    assert all(row.get('allowed', True) for row in observations)
