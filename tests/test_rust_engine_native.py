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



@pytest.fixture
def expected_request(native_board):
    """Retain request bytes for handcrafted native-validation plans.

    These tests intentionally fabricate edits; the hash is a consistency check,
    not proof that the Rust process produced them. Real CLI cases retain their
    own actual request, including any modified rules or source geometry.
    """
    path = native_board.with_name('expected-request.json')
    bridge.write_request(bridge.export_board(native_board, RULES, 100000), path)
    return path


def test_native_export_preserves_identity_rules_and_fixed_copper(native_board):
    original = native_board.read_bytes()
    data = bridge.export_board(native_board, RULES, 100000)
    assert data['version'] == 1
    assert data['bounds'] == {'min': [0, 0], 'max': [20000000, 20000000]}
    assert data['rules'] == RULES
    assert 'limits' not in data  # Existing inputs continue to use Rust defaults.
    assert len(data['pads']) == 2
    assert {tuple(p['at']) for p in data['pads']} == {(3000000, 10000000), (17000000, 10000000)}
    assert data['tracks'][0]['locked'] is True
    assert data['tracks'][0]['start'] == [10000000, 4000000]
    assert len({p['id'] for p in data['pads']} | {t['id'] for t in data['tracks']}) == 3
    assert native_board.read_bytes() == original


def multiterminal_board(path, variant):
    """Extend the native fixture with distinct terminals and fixed copper groups."""
    board = pcbnew.LoadBoard(str(path))
    signal_net = next(pad.GetNetCode() for pad in board.GetPads() if pad.GetNetname() == 'SIGNAL')
    positions = [(signal_net, (3000000, 3000000))]
    if variant in ('four', 'six', 'preconnected'):
        positions.append((signal_net, (17000000, 17000000)))
    if variant in ('six', 'preconnected'):
        positions.extend([(signal_net, (3000000, 17000000)), (signal_net, (17000000, 3000000))])
    if variant == 'multiple_nets':
        for edge in board.GetDrawings():
            for getter, setter in ((edge.GetStart, edge.SetStart), (edge.GetEnd, edge.SetEnd)):
                point = getter()
                if point.x == 20000000:
                    setter(pcbnew.VECTOR2I(40000000, point.y))
        board.Add(pcbnew.NETINFO_ITEM(board, 'SECOND_SIGNAL', 3))
        positions.extend((3, point) for point in (
            (23000000, 3000000), (37000000, 3000000),
            (23000000, 17000000), (37000000, 17000000)))
    for index, (net, position) in enumerate(positions, 3):
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
    if variant == 'preconnected':
        for start, end in (
                ((3000000, 3000000), (3000000, 10000000)),
                ((17000000, 10000000), (17000000, 17000000)),
                ((6000000, 17000000), (8000000, 17000000))):
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(*start))
            track.SetEnd(pcbnew.VECTOR2I(*end))
            track.SetLayer(pcbnew.F_Cu)
            track.SetWidth(RULES['width_nm'])
            track.SetNetCode(signal_net)
            track.SetLocked(True)
            board.Add(track)
    assert pcbnew.SaveBoard(str(path), board)


def native_copper_key(item):
    if isinstance(item, pcbnew.PAD):
        return ('pad', item.GetParentFootprint().GetReference(), item.GetNumber(), bridge.item_id(item))
    return ('track', bridge.item_id(item))


def native_copper_partitions(path):
    """Traverse KiCad's actual adjacency, independently of Rust contact code.

    GetConnectedPads/Tracks expose direct neighbors, so walk tracks as well as
    pads. Net labels alone and aggregate airwire counts do not establish these
    terminal partitions. Include every pad and track UUID: footprint labels can
    repeat, and disconnected track-only islands are real copper components.
    """
    board = pcbnew.LoadBoard(str(path))
    board.BuildConnectivity()
    connectivity = board.GetConnectivity()
    copper = list(board.GetPads()) + list(board.GetTracks())
    seen = set()
    partitions = {}
    for seed in copper:
        ident = bridge.item_id(seed)
        if ident in seen:
            continue
        pending, members = [seed], set()
        while pending:
            item = pending.pop()
            item_ident = bridge.item_id(item)
            if item_ident in seen:
                continue
            seen.add(item_ident)
            assert item.GetNetCode() == seed.GetNetCode()
            members.add(native_copper_key(item))
            pending.extend(connectivity.GetConnectedPads(item))
            pending.extend(connectivity.GetConnectedTracks(item))
        # KiCad can renumber integer net codes when saving a new net. Stable
        # names let this independent expectation detect actual net reassignment.
        partitions.setdefault(seed.GetNetname(), set()).add(frozenset(members))
    return partitions


@pytest.mark.parametrize('variant', ['three', 'four', 'six', 'preconnected', 'multiple_nets'])
def test_multiterminal_native_connectivity_preservation_and_drc(native_board, guarded_cli, variant):
    run, audit_log = guarded_cli
    multiterminal_board(native_board, variant)
    original = native_board.read_bytes()
    data = bridge.export_board(native_board, RULES, 100000)
    by_net = {'SIGNAL': {'J1', 'J2', 'J3'}}
    if variant in ('four', 'six', 'preconnected'):
        by_net['SIGNAL'].add('J4')
    if variant in ('six', 'preconnected'):
        by_net['SIGNAL'].update(('J5', 'J6'))
    if variant == 'multiple_nets':
        by_net['SECOND_SIGNAL'] = {'J4', 'J5', 'J6', 'J7'}
    board = pcbnew.LoadBoard(str(native_board))
    pads = {pad.GetParentFootprint().GetReference(): native_copper_key(pad) for pad in board.GetPads()}
    before_partitions = {net: {frozenset((pads[ref],)) for ref in refs} for net, refs in by_net.items()}
    if variant == 'preconnected':
        before_partitions['SIGNAL'] = {frozenset(pads[ref] for ref in group) for group in (
            ('J1', 'J3'), ('J2', 'J4'), ('J5',), ('J6',))}
    for track in board.GetTracks():
        groups = before_partitions.setdefault(track.GetNetname(), set())
        # The fixture has exactly two attached vertical incumbents; its third
        # SIGNAL incumbent is a separate island with no pad.
        attached = {3000000: ('J1', 'J3'), 17000000: ('J2', 'J4')}
        refs = attached.get(track.GetStart().x) if track.GetNetname() == 'SIGNAL' else None
        members = frozenset(pads[ref] for ref in refs) if refs else frozenset()
        if members:
            groups.remove(members)
        groups.add(members | {native_copper_key(track)})
    assert native_copper_partitions(native_board) == before_partitions
    request, plan_path = native_board.with_name('multi-request.json'), native_board.with_name('multi-plan.json')
    bridge.write_request(data, request)
    command = ['cargo', 'run', '--locked', '--offline', '--manifest-path',
               str(ROOT / 'rust-engine/Cargo.toml'), '--', str(request), str(plan_path)]
    execution = run(command, check=False)
    plan = json.loads(plan_path.read_bytes())
    assert execution.returncode == 0, (execution.stderr, plan)
    assert plan['version'] == 3 and plan['status'] == 'complete'
    assert plan['request_sha256'] == hashlib.sha256(request.read_bytes()).hexdigest()
    assert plan['added_tracks'] and plan['removed_track_ids'] == []
    assert 0 < plan['expanded_states'] <= data['max_expansions']
    assert 0 < plan['work_used'] <= 100000000
    # A second real execution must produce exactly the same edits and counters.
    repeated = native_board.with_name('multi-repeat.json')
    run([*command[:-1], str(repeated)])
    assert repeated.read_bytes() == plan_path.read_bytes()
    output = native_board.with_name('multi-routed.kicad_pcb')
    run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'), 'apply',
         str(native_board), str(plan_path), str(output), '--request', str(request)])
    expected = {net: {frozenset(members for group in groups for members in group)}
                for net, groups in before_partitions.items()}
    names = {net['id']: net['name'] for net in data['nets']}
    for track in plan['added_tracks']:
        net = names[track['net']]
        members, = expected[net]
        expected[net] = {members | {('track', track['id'])}}
    assert native_copper_partitions(output) == expected
    actual = bridge.export_board(output, RULES, 100000)
    assert actual['pads'] == data['pads'] and actual['nets'] == data['nets']
    assert {track['id']: track for track in actual['tracks']} == {
        track['id']: track for track in data['tracks'] + plan['added_tracks']}
    before = bridge.native_violations(native_board, native_board.with_name('multi-before-drc.json'))
    after = bridge.native_violations(output, native_board.with_name('multi-after-drc.json'))
    assert before['unconnected_items'] and after['unconnected_items'] == []
    key = lambda violation: json.dumps(violation, sort_keys=True)
    assert not (Counter(map(key, after['violations'])) - Counter(map(key, before['violations'])))
    assert native_board.read_bytes() == original
    observations = [json.loads(line) for line in audit_log.read_text().splitlines()]
    assert any(row.get('allowed', False) for row in observations)
    assert all(row.get('allowed', True) for row in observations)


def test_multiterminal_multipin_and_isolated_incumbent_components(native_board, guarded_cli):
    """Same-footprint pads and a padless island must each remain observable."""
    run, _ = guarded_cli
    board = pcbnew.LoadBoard(str(native_board))
    signal = next(pad.GetNetCode() for pad in board.GetPads() if pad.GetNetname() == 'SIGNAL')
    guard = next(track.GetNetCode() for track in board.GetTracks())
    for item in list(board.GetFootprints()) + list(board.GetTracks()):
        board.Remove(item)
    for edge in board.GetDrawings():
        for getter, setter in ((edge.GetStart, edge.SetStart), (edge.GetEnd, edge.SetEnd)):
            point = getter()
            if point.x == 20000000:
                setter(pcbnew.VECTOR2I(25000000, point.y))
    footprint = pcbnew.FOOTPRINT(board)
    footprint.SetReference('U1')
    board.Add(footprint)
    for number, position in enumerate(((4000000, 4000000), (4000000, 16000000),
                                       (20000000, 4000000), (20000000, 16000000)), 1):
        pad = pcbnew.PAD(footprint)
        pad.SetNumber(str(number))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.PAD_SHAPE_RECT if number <= 2 else pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(1200000 if number <= 2 else 1000000, 1000000))
        pad.SetPosition(pcbnew.VECTOR2I(*position))
        layers = pcbnew.LSET()
        layers.AddLayer(pcbnew.F_Cu)
        pad.SetLayerSet(layers)
        pad.SetNetCode(signal)
        footprint.Add(pad)
    for net, start, end, locked in (
            (signal, (10000000, 8000000), (13000000, 11000000), False),
            (guard, (12000000, 2000000), (12000000, 6000000), True)):
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(*start))
        track.SetEnd(pcbnew.VECTOR2I(*end))
        track.SetWidth(RULES['width_nm'])
        track.SetLayer(pcbnew.F_Cu)
        track.SetNetCode(net)
        track.SetLocked(locked)
        board.Add(track)
    assert pcbnew.SaveBoard(str(native_board), board)
    original = native_board.read_bytes()
    pads = {native_copper_key(pad) for pad in board.GetPads()}
    assert len(pads) == 4 and {key[1] for key in pads} == {'U1'}
    incumbent = next(track for track in board.GetTracks() if track.GetNetCode() == signal)
    obstacle = next(track for track in board.GetTracks() if track.GetNetCode() == guard)
    signal_members = pads | {native_copper_key(incumbent)}
    guard_members = frozenset((native_copper_key(obstacle),))
    expected_before = {'SIGNAL': {frozenset((key,)) for key in signal_members},
                       'OBSTACLE': {guard_members}}
    assert len(expected_before['SIGNAL']) == 5
    assert native_copper_partitions(native_board) == expected_before
    data = bridge.export_board(native_board, RULES, 100000)
    request, plan_path = native_board.with_name('multipin.json'), native_board.with_name('multipin-plan.json')
    bridge.write_request(data, request)
    run(['cargo', 'run', '--locked', '--offline', '--manifest-path',
         str(ROOT / 'rust-engine/Cargo.toml'), '--', str(request), str(plan_path)])
    plan = json.loads(plan_path.read_bytes())
    assert plan['status'] == 'complete' and plan['added_tracks']
    assert plan['removed_track_ids'] == []
    output = native_board.with_name('multipin-routed.kicad_pcb')
    run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'), 'apply',
         str(native_board), str(plan_path), str(output), '--request', str(request)])
    expected_after = {'SIGNAL': {frozenset(signal_members | {
        ('track', track['id']) for track in plan['added_tracks']})}, 'OBSTACLE': {guard_members}}
    assert native_copper_partitions(output) == expected_after
    assert expected_after != expected_before
    actual = bridge.export_board(output, RULES, 100000)
    assert actual['pads'] == data['pads'] and actual['nets'] == data['nets']
    assert {track['id']: track for track in actual['tracks']} == {
        track['id']: track for track in data['tracks'] + plan['added_tracks']}
    before = bridge.native_violations(native_board, native_board.with_name('multipin-before-drc.json'))
    after = bridge.native_violations(output, native_board.with_name('multipin-after-drc.json'))
    assert before['unconnected_items'] and after['unconnected_items'] == []
    key = lambda violation: json.dumps(violation, sort_keys=True)
    assert not (Counter(map(key, after['violations'])) - Counter(map(key, before['violations'])))
    assert native_board.read_bytes() == original


@pytest.mark.parametrize('budget,status,exit_code', [
    ('max_expansions', 'budget_exhausted', 4), ('max_work', 'work_exhausted', 5),
])
def test_multiterminal_shared_budget_failure_cannot_publish_partial_board(
        native_board, guarded_cli, budget, status, exit_code):
    run, _ = guarded_cli
    multiterminal_board(native_board, 'six')
    original = native_board.read_bytes()
    data = bridge.export_board(native_board, RULES, 100000)
    request, plan_path = native_board.with_name('multi-control.json'), native_board.with_name('multi-control-plan.json')
    bridge.write_request(data, request)
    run(['cargo', 'run', '--locked', '--offline', '--manifest-path',
         str(ROOT / 'rust-engine/Cargo.toml'), '--', str(request), str(plan_path)])
    control = json.loads(plan_path.read_bytes())
    assert control['status'] == 'complete' and control['added_tracks']
    counter = 'expanded_states' if budget == 'max_expansions' else 'work_used'
    limit = control[counter] - 1
    assert limit > 0
    if budget == 'max_expansions':
        data[budget] = limit
    else:
        data['limits'] = {budget: limit}
    limited_request = native_board.with_name('multi-limited.json')
    limited_plan = native_board.with_name('multi-limited-plan.json')
    bridge.write_request(data, limited_request)
    engine = ROOT / 'rust-engine/target/debug/kicad-route-engine'
    failed = run([str(engine), str(limited_request), str(limited_plan)], check=False)
    assert failed.returncode == exit_code, failed.stderr
    result = json.loads(limited_plan.read_bytes())
    assert result['status'] == status and result['reason']
    assert result['added_tracks'] == [] and result['removed_track_ids'] == []
    assert 0 < result[counter] <= limit
    output = native_board.with_name('multi-partial.kicad_pcb')
    applied = run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'), 'apply',
                   str(native_board), str(limited_plan), str(output), '--request', str(limited_request)], check=False)
    assert applied.returncode == 2
    assert json.loads(applied.stderr.splitlines()[-1])['status'] == 'invalid'
    assert not output.exists() and not list(native_board.parent.glob('.multi-partial-*'))
    assert native_board.read_bytes() == original


def test_multiterminal_native_apply_rejects_ignored_terminal(native_board, guarded_cli):
    run, _ = guarded_cli
    multiterminal_board(native_board, 'three')
    board = pcbnew.LoadBoard(str(native_board))
    for track in list(board.GetTracks()):
        board.Remove(track)
    for footprint in board.GetFootprints():
        if footprint.GetReference() == 'J3':
            footprint.SetPosition(pcbnew.VECTOR2I(10000000, 17000000))
    assert pcbnew.SaveBoard(str(native_board), board)
    original = native_board.read_bytes()
    data = bridge.export_board(native_board, RULES, 100000)
    request, partial_request = native_board.with_name('all-terminals.json'), native_board.with_name('ignored-terminal.json')
    bridge.write_request(data, request)
    ignored = next(pad['id'] for pad in data['pads'] if pad['at'] == [10000000, 17000000])
    bridge.write_request({**data, 'pads': [pad for pad in data['pads'] if pad['id'] != ignored]}, partial_request)
    partial_plan = native_board.with_name('ignored-terminal-plan.json')
    run(['cargo', 'run', '--locked', '--offline', '--manifest-path',
         str(ROOT / 'rust-engine/Cargo.toml'), '--', str(partial_request), str(partial_plan)])
    plan = json.loads(partial_plan.read_bytes())
    assert plan['status'] == 'complete'
    # Deliberately forge consistency metadata to exercise native validation.
    # A request digest is not authentication; it must not hide an omitted pad.
    plan['request_sha256'] = hashlib.sha256(request.read_bytes()).hexdigest()
    rejected = native_board.with_name('ignored-terminal-output.kicad_pcb')
    with pytest.raises(bridge.NativeValidationError, match='open connections'):
        bridge.apply_plan(native_board, plan, rejected, expected_request=request.read_bytes())
    assert not rejected.exists() and not list(native_board.parent.glob('.ignored-terminal-output-*'))
    complete_plan = native_board.with_name('all-terminals-plan.json')
    run([str(ROOT / 'rust-engine/target/debug/kicad-route-engine'), str(request), str(complete_plan)])
    accepted = native_board.with_name('all-terminals-output.kicad_pcb')
    bridge.apply_plan(native_board, json.loads(complete_plan.read_bytes()), accepted,
                      expected_request=request.read_bytes())
    source = pcbnew.LoadBoard(str(native_board))
    members = {native_copper_key(pad) for pad in source.GetPads()}
    members.update(('track', track['id']) for track in json.loads(complete_plan.read_bytes())['added_tracks'])
    assert native_copper_partitions(accepted) == {'SIGNAL': {frozenset(members)}}
    assert native_board.read_bytes() == original


def test_native_adapter_applies_exact_edit_list(native_board, expected_request):
    original = native_board.read_bytes()
    exported = bridge.export_board(native_board, RULES, 100000)
    added = connection_tracks()
    plan = {'version': 3, 'request_sha256': hashlib.sha256(expected_request.read_bytes()).hexdigest(), 'source_id': exported['source_id'], 'status': 'complete',
            'added_tracks': added, 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
    output = native_board.with_name('output.kicad_pcb')
    bridge.apply_plan(native_board, plan, output, expected_request=expected_request.read_bytes())
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
    assert plan['version'] == 3
    assert plan['request_sha256'] == hashlib.sha256(request.read_bytes()).hexdigest()
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
         str(native_board), str(plan_path), str(output), '--request', str(request)])
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
def test_native_apply_rejects_false_complete_plans_before_publication(native_board, expected_request, tamper, reason):
    original = native_board.read_bytes()
    added = connection_tracks()
    if tamper == 'empty':
        added = []
    elif tamper == 'crossing':
        added = [{**added[0], 'end': [17000000, 10000000]}]
    else:
        added[0]['width_nm'] = 1
    plan = {'version': 3, 'request_sha256': hashlib.sha256(expected_request.read_bytes()).hexdigest(), 'status': 'complete',
            'source_id': hashlib.sha256(original).hexdigest(),
            'added_tracks': added, 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
    output = native_board.with_name('rejected.kicad_pcb')
    with pytest.raises(bridge.NativeValidationError, match=reason):
        bridge.apply_plan(native_board, plan, output, expected_request=expected_request.read_bytes())
    assert not output.exists()
    assert not list(native_board.parent.glob('.rejected-*'))
    assert native_board.read_bytes() == original


def test_native_apply_cli_reports_typed_validation_failure(native_board, expected_request, guarded_cli):
    run, _ = guarded_cli
    plan = native_board.with_name('empty-plan.json')
    plan.write_text(json.dumps({'version': 3, 'request_sha256': hashlib.sha256(expected_request.read_bytes()).hexdigest(), 'status': 'complete',
        'source_id': hashlib.sha256(native_board.read_bytes()).hexdigest(),
        'added_tracks': [], 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}))
    output = native_board.with_name('rejected.kicad_pcb')
    result = run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'),
                  'apply', str(native_board), str(plan), str(output), '--request', str(expected_request)], check=False)
    assert result.returncode == 2
    assert json.loads(result.stderr.splitlines()[-1])['status'] == 'native_validation_failed'
    assert not output.exists()


def test_missing_native_drc_tool_cannot_publish_a_candidate(native_board, expected_request, monkeypatch, tmp_path):
    empty_path = tmp_path / 'no-native-tool'
    empty_path.mkdir()
    monkeypatch.setenv('PATH', str(empty_path))
    plan = {'version': 3, 'request_sha256': hashlib.sha256(expected_request.read_bytes()).hexdigest(), 'status': 'complete',
            'source_id': hashlib.sha256(native_board.read_bytes()).hexdigest(),
            'added_tracks': connection_tracks(), 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
    output = native_board.with_name('no-drc-output.kicad_pcb')
    with pytest.raises(bridge.NativeValidationError, match='native DRC did not produce'):
        bridge.apply_plan(native_board, plan, output, expected_request=expected_request.read_bytes())
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
    assert plan['version'] == 3
    assert plan['status'] == 'complete'
    assert plan['added_tracks'], 'KiCad still has an open: tangent contact cannot skip routing'
    output = native_board.with_name('routed.kicad_pcb')
    bridge.apply_plan(native_board, plan, output, expected_request=request.read_bytes())
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
def test_native_adapter_rejects_unrepresented_native_geometry_and_rules(native_board, expected_request, feature):
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
    plan = {'version': 3, 'request_sha256': hashlib.sha256(expected_request.read_bytes()).hexdigest(), 'status': 'complete',
            'source_id': hashlib.sha256(native_board.read_bytes()).hexdigest(),
            'added_tracks': connection_tracks(), 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
    with pytest.raises(bridge.UnsupportedBoard):
        bridge.apply_plan(native_board, plan, output, expected_request=expected_request.read_bytes())
    assert not output.exists()


@pytest.mark.parametrize('location', ['board', 'footprint'])
@pytest.mark.parametrize('layer', [pcbnew.Margin, pcbnew.Dwgs_User])
def test_native_margin_geometry_is_rejected_but_documentation_graphics_are_allowed(
        native_board, expected_request, location, layer):
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
    plan = {'version': 3, 'request_sha256': hashlib.sha256(expected_request.read_bytes()).hexdigest(), 'status': 'complete',
            'source_id': hashlib.sha256(native_board.read_bytes()).hexdigest(),
            'added_tracks': connection_tracks(), 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
    if layer == pcbnew.Margin:
        assert edge_errors, 'native DRC must demonstrate the unrepresented rule'
        with pytest.raises(bridge.UnsupportedBoard, match='Margin'):
            bridge.export_board(native_board, RULES, 100000)
        with pytest.raises(bridge.UnsupportedBoard, match='Margin'):
            bridge.apply_plan(native_board, plan, output, expected_request=expected_request.read_bytes())
        assert not output.exists()
    else:
        assert not edge_errors
        bridge.export_board(native_board, RULES, 100000)
        bridge.apply_plan(native_board, plan, output, expected_request=expected_request.read_bytes())
        assert output.exists()


@pytest.mark.parametrize('status,source', [('unroutable', None), ('internal_error', None),
                                        ('complete', 'stale')])
def test_native_adapter_does_not_write_failed_or_stale_plan(native_board, expected_request, status, source):
    original = native_board.read_bytes()
    data = bridge.export_board(native_board, RULES, 100000)
    plan = {'version': 3, 'request_sha256': hashlib.sha256(expected_request.read_bytes()).hexdigest(), 'status': status, 'source_id': source or data['source_id'],
            'added_tracks': [], 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
    output = native_board.with_name('output.kicad_pcb')
    with pytest.raises(ValueError):
        bridge.apply_plan(native_board, plan, output, expected_request=expected_request.read_bytes())
    assert not output.exists()
    assert native_board.read_bytes() == original


@pytest.mark.parametrize('operation', ['export', 'apply'])
def test_native_operations_pin_one_snapshot_without_source_sidecars(native_board, expected_request, monkeypatch, operation):
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
    read_bounded = bridge.read_bounded
    reads = []

    def concurrent_read(path):
        value = read_bounded(path)
        if path == native_board:
            reads.append(path)
            native_board.write_bytes(changed_bytes)
        return value

    monkeypatch.setattr(bridge, 'read_bounded', concurrent_read)
    output = native_board.with_name('snapshot-output.kicad_pcb')
    if operation == 'export':
        result = bridge.export_board(native_board, RULES, 100000)
        assert result == expected
        expected_names = before_names
    else:
        plan = {'version': 3, 'request_sha256': hashlib.sha256(expected_request.read_bytes()).hexdigest(), 'status': 'complete',
                'source_id': hashlib.sha256(original).hexdigest(),
                'added_tracks': connection_tracks(), 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
        bridge.apply_plan(native_board, plan, output, expected_request=expected_request.read_bytes())
        assert bridge.export_board(output, RULES, 100000)['pads'] == expected['pads']
        expected_names = before_names | {output}
    assert len(reads) == 1
    assert read_bytes(native_board) == changed_bytes
    assert set(native_board.parent.iterdir()) == expected_names


def test_native_publication_preserves_a_destination_created_during_validation(native_board, expected_request, monkeypatch):
    plan = {'version': 3, 'request_sha256': hashlib.sha256(expected_request.read_bytes()).hexdigest(), 'status': 'complete',
            'source_id': hashlib.sha256(native_board.read_bytes()).hexdigest(),
            'added_tracks': connection_tracks(), 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
    output = native_board.with_name('concurrent-output.kicad_pcb')
    real_link = bridge.os.link

    def race(source, destination):
        destination.write_bytes(b'concurrent owner output')
        return real_link(source, destination)

    monkeypatch.setattr(bridge.os, 'link', race)
    with pytest.raises(FileExistsError):
        bridge.apply_plan(native_board, plan, output, expected_request=expected_request.read_bytes())
    assert output.read_bytes() == b'concurrent owner output'
    assert not list(native_board.parent.glob('.concurrent-output-*'))


def test_native_drc_timeout_refuses_publication(native_board, expected_request, monkeypatch):
    plan = {'version': 3, 'request_sha256': hashlib.sha256(expected_request.read_bytes()).hexdigest(), 'status': 'complete',
            'source_id': hashlib.sha256(native_board.read_bytes()).hexdigest(),
            'added_tracks': connection_tracks(), 'removed_track_ids': [], 'expanded_states': 0, 'work_used': 0}
    output = native_board.with_name('timeout-output.kicad_pcb')

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])

    monkeypatch.setattr(bridge.subprocess, 'run', timeout)
    with pytest.raises(bridge.NativeValidationError, match='timed out'):
        bridge.apply_plan(native_board, plan, output, expected_request=expected_request.read_bytes())
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
def test_apply_cli_reports_input_failures_without_publication(native_board, expected_request, guarded_cli, case, status):
    run, _ = guarded_cli
    original = native_board.read_bytes()
    additions = connection_tracks()
    plan = {'version': 3, 'request_sha256': hashlib.sha256(expected_request.read_bytes()).hexdigest(), 'status': 'complete',
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
        text = text.replace('"version": 3', '"version": 3, "version": 3')
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
                  'apply', str(native_board), str(plan_path), str(output), '--request', str(expected_request)], check=False)
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
def test_native_apply_rejects_invalid_work_counters(native_board, expected_request, guarded_cli, counter, value):
    run, _ = guarded_cli
    original = native_board.read_bytes()
    plan = {'version': 3, 'request_sha256': hashlib.sha256(expected_request.read_bytes()).hexdigest(), 'status': 'complete',
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
                  'apply', str(native_board), str(plan_path), str(output), '--request', str(expected_request)], check=False)
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
    assert plan['version'] == 3
    assert plan['status'] == status and plan['reason']
    assert plan['added_tracks'] == [] and plan['removed_track_ids'] == []
    assert type(plan['work_used']) is int and plan['work_used'] >= 0
    if 'max_work' in limits:
        assert plan['work_used'] <= limits['max_work']
    output = native_board.with_name('limited-output.kicad_pcb')
    applied = run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'),
                   'apply', str(native_board), str(plan_path), str(output), '--request', str(request)], check=False)
    assert applied.returncode == 2
    assert json.loads(applied.stderr.splitlines()[-1])['status'] == 'invalid'
    assert not output.exists() and not list(native_board.parent.glob('.limited-output-*'))
    assert native_board.read_bytes() == original
    observations = [json.loads(line) for line in audit_log.read_text().splitlines()]
    assert sum(row.get('guard_started', False) for row in observations) >= 2
    assert all(row.get('allowed', True) for row in observations)


@pytest.mark.parametrize('case', ['missing_version', 'old_with_counters', 'old_without_counters', 'old_version2',
    'future_version', 'boolean_version', 'float_version', 'string_version',
    'missing_removals', 'null_removals', 'object_removals', 'string_removals',
    'nonstring_removal', 'supported_shape_removal'])
def test_native_result_schema_refusals(native_board, expected_request, guarded_cli, case):
    run, _ = guarded_cli
    original = native_board.read_bytes()
    plan = {'version': 3, 'request_sha256': hashlib.sha256(expected_request.read_bytes()).hexdigest(), 'status': 'complete',
            'source_id': hashlib.sha256(original).hexdigest(),
            'added_tracks': connection_tracks(), 'removed_track_ids': [],
            'expanded_states': 0, 'work_used': 0}
    if case == 'missing_version':
        del plan['version']
    elif case.startswith('old_'):
        plan['version'] = 2 if case == 'old_version2' else 1
        if case == 'old_without_counters':
            del plan['expanded_states'], plan['work_used']
    elif case.endswith('_version'):
        plan['version'] = {'future_version': 4, 'boolean_version': True,
                           'float_version': 3.0, 'string_version': '3'}[case]
    elif case == 'missing_removals':
        del plan['removed_track_ids']
    else:
        plan['removed_track_ids'] = {'null_removals': None, 'object_removals': {},
            'string_removals': '', 'nonstring_removal': [1],
            'supported_shape_removal': [str(uuid.uuid4())]}[case]
    plan_path = native_board.with_name('schema-plan.json')
    plan_path.write_text(json.dumps(plan))
    output = native_board.with_name('schema-output.kicad_pcb')
    result = run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'),
                  'apply', str(native_board), str(plan_path), str(output), '--request', str(expected_request)], check=False)
    assert result.returncode == 2
    failure = json.loads(result.stderr.splitlines()[-1])
    expected = 'unsupported' if case == 'supported_shape_removal' else 'invalid'
    assert failure['status'] == expected
    field = 'removed_track_ids' if 'removal' in case and expected == 'invalid' else None
    if field:
        assert field in failure['reason']
    elif expected == 'invalid':
        assert 'version3' in failure['reason']
    assert 'Traceback' not in result.stderr
    assert not output.exists() and native_board.read_bytes() == original


@pytest.mark.parametrize('document', ['board', 'plan', 'rules', 'limits', 'request'])
def test_native_cli_refuses_oversized_documents(native_board, expected_request, guarded_cli, document):
    run, _ = guarded_cli
    original = native_board.read_bytes()
    source = native_board.with_name('oversized.kicad_pcb')
    source.write_bytes(original)
    rules = native_board.with_name('rules.json')
    rules.write_text(json.dumps(RULES))
    limits = native_board.with_name('limits.json')
    limits.write_text('{}')
    plan = native_board.with_name('plan.json')
    plan.write_text('{}')
    oversized = {'board': source, 'plan': plan, 'rules': rules, 'limits': limits,
                 'request': expected_request}[document]
    with oversized.open('ab') as stream:
        stream.write(b' ' * (bridge.MAX_DOCUMENT_BYTES + 1 - oversized.stat().st_size))
    output = native_board.with_name('oversized-output')
    if document in ('plan', 'request'):
        command = ['apply', str(source), str(plan), str(output), '--request', str(expected_request)]
    else:
        command = ['export', str(source), str(output), '--rules', str(rules), '--limits', str(limits)]
    result = run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'), *command], check=False)
    assert result.returncode == 2
    failure = json.loads(result.stderr.splitlines()[-1])
    assert failure['status'] == 'invalid'
    assert f'exceeds {bridge.MAX_DOCUMENT_BYTES} bytes' in failure['reason']
    assert 'Traceback' not in result.stderr
    assert not output.exists() and not list(native_board.parent.glob('.oversized-output-*'))
    assert native_board.read_bytes() == original


def test_native_exact_document_boundary_accepts_board_and_json(native_board, guarded_cli):
    run, _ = guarded_cli
    source_bytes = native_board.read_bytes()
    native_board.write_bytes(source_bytes + b' ' * (bridge.MAX_DOCUMENT_BYTES - len(source_bytes)))
    rules = native_board.with_name('rules.json')
    rule_bytes = json.dumps(RULES).encode()
    rules.write_bytes(rule_bytes + b' ' * (bridge.MAX_DOCUMENT_BYTES - len(rule_bytes)))
    output = native_board.with_name('boundary-request.json')
    result = run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'),
                  'export', str(native_board), str(output), '--rules', str(rules)], check=False)
    assert result.returncode == 0, result.stderr
    exported = json.loads(output.read_text())
    assert exported['version'] == 1 and exported['rules'] == RULES
    assert exported['source_id'] == hashlib.sha256(native_board.read_bytes()).hexdigest()


@pytest.mark.parametrize('depth', [2000, 500000])
def test_deep_invalid_json_has_typed_cli_failure(native_board, expected_request, guarded_cli, depth):
    run, _ = guarded_cli
    plan = native_board.with_name('deep-plan.json')
    plan.write_text('{"nested":' + '[' * depth + '0' + ']' * depth + '}')
    output = native_board.with_name('deep-output.kicad_pcb')
    result = run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'),
                  'apply', str(native_board), str(plan), str(output), '--request', str(expected_request)], check=False)
    assert result.returncode == 2
    failure = json.loads(result.stderr.splitlines()[-1])
    assert failure['status'] == 'invalid'
    if depth == 500000:
        assert failure['reason'] == 'JSON input exceeds parser nesting capacity'
    else:
        # Python 3.14's decoder accepts this depth; older decoders can refuse it.
        assert ('nesting capacity' in failure['reason'] or
                failure['reason'] == 'only a complete version3 Rust result can be written')
    assert 'Traceback' not in result.stderr and not output.exists()


def test_export_refuses_serialization_above_engine_input_limit(tmp_path):
    output = tmp_path / 'request.json'
    # Includes JSON syntax/indentation, so this payload is just above the bound.
    with pytest.raises(ValueError, match='exported JSON exceeds'):
        bridge.write_request({'padding': 'x' * bridge.MAX_DOCUMENT_BYTES}, output)
    assert not list(tmp_path.iterdir())


def test_export_serialization_has_no_nesting_indentation_growth(tmp_path):
    depth = 2000
    nested = 0
    for _ in range(depth):
        nested = [nested]
    output = tmp_path / 'request.json'
    bridge.write_request({'limits': {'nested': nested}}, output)
    assert output.stat().st_size < depth * 3 + 100
    # The representation is still the same object; the native seam does not
    # pre-empt Rust's validation of limits or select resource policy.
    loaded = json.loads(output.read_bytes())['limits']['nested']
    for _ in range(depth):
        assert isinstance(loaded, list) and len(loaded) == 1
        loaded = loaded[0]
    assert loaded == 0


def test_export_encoder_recursion_is_typed_and_leaves_no_output(tmp_path):
    nested = 0
    for _ in range(100000):
        nested = [nested]
    output = tmp_path / 'request.json'
    with pytest.raises(ValueError, match='exported JSON exceeds encoder nesting capacity'):
        bridge.write_request({'limits': {'nested': nested}}, output)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('extra', [0, 1])
def test_export_serialization_exact_byte_boundary(tmp_path, extra):
    output = tmp_path / 'request.json'
    payload = 'x' * (bridge.MAX_DOCUMENT_BYTES - len(b'{"padding":""}\n') + extra)
    if extra:
        with pytest.raises(ValueError, match='exported JSON exceeds'):
            bridge.write_request({'padding': payload}, output)
        assert not list(tmp_path.iterdir())
    else:
        bridge.write_request({'padding': payload}, output)
        assert output.stat().st_size == bridge.MAX_DOCUMENT_BYTES
        assert json.loads(output.read_bytes()) == {'padding': payload}


def test_oversized_native_report_is_a_validation_failure(tmp_path, monkeypatch):
    report = tmp_path / 'drc.json'

    def large_report(command, **kwargs):
        report.write_bytes(b' ' * (bridge.MAX_DOCUMENT_BYTES + 1))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bridge.subprocess, 'run', large_report)
    with pytest.raises(bridge.NativeValidationError, match='exceeds'):
        bridge.native_violations(tmp_path / 'board.kicad_pcb', report)


def test_deep_native_report_has_typed_decoder_failure(tmp_path, monkeypatch):
    report = tmp_path / 'drc.json'

    def deep_report(command, **kwargs):
        report.write_text('{"nested":' + '[' * 500000 + '0' + ']' * 500000 + '}')
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bridge.subprocess, 'run', deep_report)
    with pytest.raises(bridge.NativeValidationError) as failure:
        bridge.native_violations(tmp_path / 'board.kicad_pcb', report)
    assert isinstance(failure.value.__cause__, RecursionError)


@pytest.mark.parametrize('identity', ['missing', None, True, 1, [], {}, '', '0' * 64,
                                    'A' * 64, 'uppercase_correct', 'trailing_space'])
def test_native_apply_requires_matching_request_identity(native_board, expected_request, guarded_cli, identity):
    run, _ = guarded_cli
    original = native_board.read_bytes()
    plan = {'version': 3, 'status': 'complete',
            'source_id': hashlib.sha256(original).hexdigest(),
            'added_tracks': connection_tracks(), 'removed_track_ids': [],
            'expanded_states': 0, 'work_used': 0}
    if identity != 'missing':
        plan['request_sha256'] = identity
    digest = hashlib.sha256(expected_request.read_bytes()).hexdigest()
    if identity == 'uppercase_correct':
        plan['request_sha256'] = digest.upper()
        assert plan['request_sha256'] != digest
    elif identity == 'trailing_space':
        plan['request_sha256'] = digest + ' '
    plan_path = native_board.with_name('unbound-plan.json')
    plan_path.write_text(json.dumps(plan))
    output = native_board.with_name('unbound-output.kicad_pcb')
    result = run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'),
                  'apply', str(native_board), str(plan_path), str(output),
                  '--request', str(expected_request)], check=False)
    assert result.returncode == 2
    assert json.loads(result.stderr.splitlines()[-1]) == {
        'status': 'invalid',
        'reason': 'Rust plan request_sha256 does not match the expected request'}
    assert not output.exists() and not list(native_board.parent.glob('.unbound-output-*'))
    assert native_board.read_bytes() == original


@pytest.mark.parametrize('mode', ['omitted', 'missing_file'])
def test_native_apply_requires_request_document(native_board, expected_request, guarded_cli, mode):
    run, _ = guarded_cli
    plan = native_board.with_name('plan.json')
    plan.write_text('{}')
    output = native_board.with_name('missing-request-output.kicad_pcb')
    command = ['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'),
               'apply', str(native_board), str(plan), str(output)]
    if mode == 'missing_file':
        command.extend(['--request', str(expected_request.with_name('absent.json'))])
    result = run(command, check=False)
    assert result.returncode == 2
    if mode == 'omitted':
        assert '--request' in result.stderr and 'required' in result.stderr
    else:
        assert json.loads(result.stderr.splitlines()[-1])['status'] == 'io_error'
    assert not output.exists() and 'Traceback' not in result.stderr


@pytest.mark.parametrize('change', ['width', 'limits', 'expansions', 'whitespace'])
def test_native_apply_rejects_same_board_result_for_different_request(
        native_board, expected_request, guarded_cli, change):
    run, _ = guarded_cli
    original = native_board.read_bytes()
    request_a = expected_request.read_bytes()
    plan_path = native_board.with_name('plan-a.json')
    run(['cargo', 'run', '--locked', '--offline', '--manifest-path',
         str(ROOT / 'rust-engine/Cargo.toml'), '--', str(expected_request), str(plan_path)])
    plan = json.loads(plan_path.read_bytes())
    assert plan['status'] == 'complete'
    assert plan['request_sha256'] == hashlib.sha256(request_a).hexdigest()
    data = json.loads(request_a)
    if change == 'width':
        data['rules']['width_nm'] = 500000
    elif change == 'limits':
        data['limits'] = {'max_work': 1}
    elif change == 'expansions':
        data['max_expansions'] = 1
    request_b = native_board.with_name('request-b.json')
    request_b.write_bytes(request_a + b' ' if change == 'whitespace' else json.dumps(data).encode())
    assert json.loads(request_b.read_bytes())['source_id'] == plan['source_id']
    output = native_board.with_name('stale-output.kicad_pcb')
    result = run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'),
                  'apply', str(native_board), str(plan_path), str(output),
                  '--request', str(request_b)], check=False)
    assert result.returncode == 2
    assert 'request_sha256' in json.loads(result.stderr.splitlines()[-1])['reason']
    assert not output.exists() and native_board.read_bytes() == original
    assert expected_request.read_bytes() == request_a


def test_failed_publication_cannot_apply_old_result_as_new_request(native_board, expected_request, guarded_cli):
    run, _ = guarded_cli
    original = native_board.read_bytes()
    plan_path = native_board.with_name('retained-plan.json')
    run(['cargo', 'run', '--locked', '--offline', '--manifest-path',
         str(ROOT / 'rust-engine/Cargo.toml'), '--', str(expected_request), str(plan_path)])
    old_bytes = plan_path.read_bytes()
    plan_a = json.loads(old_bytes)
    assert plan_a['status'] == 'complete'
    request_b = native_board.with_name('request-b.json')
    changed = json.loads(expected_request.read_bytes())
    changed['rules']['width_nm'] = 500000
    bridge.write_request(changed, request_b)
    engine = ROOT / 'rust-engine/target/debug/kicad-route-engine'
    fresh_path = native_board.with_name('fresh-b.json')
    run([str(engine), str(request_b), str(fresh_path)])
    plan_b = json.loads(fresh_path.read_bytes())
    assert plan_b['status'] == 'complete'
    assert plan_b['source_id'] == plan_a['source_id']
    assert {t['width_nm'] for t in plan_b['added_tracks']} == {500000}
    assert plan_b['request_sha256'] != plan_a['request_sha256']
    # The exec preserves the child's PID, so a foreign adjacent temporary file
    # provokes a real publication failure over the actual older result.
    collision = '''import os, pathlib, sys
target = pathlib.Path(sys.argv[3])
target.with_name(f".{target.name}.{os.getpid()}.tmp").write_bytes(b"foreign temporary")
os.execv(sys.argv[1], sys.argv[1:])
'''
    failed = run(['/usr/bin/python3', '-c', collision, str(engine), str(request_b), str(plan_path)], check=False)
    assert failed.returncode == 8 and 'no fresh result established' in failed.stderr
    assert plan_path.read_bytes() == old_bytes
    foreign = list(native_board.parent.glob('.retained-plan.json.*.tmp'))
    assert len(foreign) == 1 and foreign[0].read_bytes() == b'foreign temporary'
    output = native_board.with_name('mismatched-output.kicad_pcb')
    rejected = run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'),
                    'apply', str(native_board), str(plan_path), str(output),
                    '--request', str(request_b)], check=False)
    assert rejected.returncode == 2
    assert 'request_sha256' in json.loads(rejected.stderr.splitlines()[-1])['reason']
    assert not output.exists() and not list(native_board.parent.glob('.mismatched-output-*'))
    # Each matching pair remains usable; native apply itself checks DRC/opens.
    for label, result_path, request_path in [('a', plan_path, expected_request), ('b', fresh_path, request_b)]:
        accepted = native_board.with_name(f'matching-{label}.kicad_pcb')
        run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'),
             'apply', str(native_board), str(result_path), str(accepted),
             '--request', str(request_path)])
        assert accepted.exists()
    assert native_board.read_bytes() == original and plan_path.read_bytes() == old_bytes
    assert foreign[0].read_bytes() == b'foreign temporary'


def test_native_apply_pins_one_expected_request_read(native_board, expected_request, monkeypatch):
    request_a = expected_request.read_bytes()
    plan = {'version': 3, 'status': 'complete',
            'request_sha256': hashlib.sha256(request_a).hexdigest(),
            'source_id': hashlib.sha256(native_board.read_bytes()).hexdigest(),
            'added_tracks': connection_tracks(), 'removed_track_ids': [],
            'expanded_states': 0, 'work_used': 0}
    plan_path = native_board.with_name('pinned-plan.json')
    plan_path.write_text(json.dumps(plan))
    output = native_board.with_name('pinned-request-output.kicad_pcb')
    read_bounded = bridge.read_bounded
    reads = []

    def change_after_read(path):
        value = read_bounded(path)
        if path == expected_request:
            reads.append(path)
            expected_request.write_bytes(request_a + b' ')
        return value

    monkeypatch.setattr(bridge, 'read_bounded', change_after_read)
    monkeypatch.setattr('sys.argv', ['rust_engine_kicad.py', 'apply', str(native_board),
                                   str(plan_path), str(output), '--request', str(expected_request)])
    assert bridge.main() == 0
    assert reads == [expected_request] and output.exists()
    assert expected_request.read_bytes() == request_a + b' '


def test_exact_request_byte_limit_routes_and_applies(native_board, expected_request, guarded_cli):
    run, _ = guarded_cli
    original = native_board.read_bytes()
    body = expected_request.read_bytes()
    expected_request.write_bytes(body + b' ' * (bridge.MAX_DOCUMENT_BYTES - len(body)))
    plan_path = native_board.with_name('boundary-plan.json')
    run(['cargo', 'run', '--locked', '--offline', '--manifest-path',
         str(ROOT / 'rust-engine/Cargo.toml'), '--', str(expected_request), str(plan_path)])
    plan = json.loads(plan_path.read_bytes())
    assert plan['status'] == 'complete'
    assert plan['request_sha256'] == hashlib.sha256(expected_request.read_bytes()).hexdigest()
    output = native_board.with_name('boundary-output.kicad_pcb')
    run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'), 'apply',
         str(native_board), str(plan_path), str(output), '--request', str(expected_request)])
    assert output.exists() and native_board.read_bytes() == original


@pytest.mark.parametrize('kind', ['none', 'text', 'bytearray', 'memoryview', 'dict', 'path', 'oversized'])
@pytest.mark.parametrize('entrypoint', ['apply_plan', 'apply_snapshot'])
def test_native_request_api_admission(native_board, expected_request, entrypoint, kind):
    original = native_board.read_bytes()
    body = expected_request.read_bytes()
    if kind == 'oversized':
        # Match the digest of the entire oversized value so a missing size check
        # cannot be hidden by a later hash mismatch. The native plan is valid.
        body += b' ' * (bridge.MAX_DOCUMENT_BYTES + 1 - len(body))
        argument = body
        reason = f'expected request exceeds {bridge.MAX_DOCUMENT_BYTES} bytes'
    else:
        argument = {'none': None, 'text': body.decode(), 'bytearray': bytearray(body),
                    'memoryview': memoryview(body), 'dict': json.loads(body),
                    'path': expected_request}[kind]
        reason = 'expected request must be the exact request bytes'
    plan = {'version': 3, 'status': 'complete',
            'request_sha256': hashlib.sha256(body).hexdigest(),
            'source_id': hashlib.sha256(original).hexdigest(),
            'added_tracks': connection_tracks(), 'removed_track_ids': [],
            'expanded_states': 0, 'work_used': 0}
    output = native_board.with_name('api-refused.kicad_pcb')
    with pytest.raises(ValueError, match=reason):
        getattr(bridge, entrypoint)(native_board, plan, output, expected_request=argument)
    assert not output.exists() and not list(native_board.parent.glob('.api-refused-*'))
    assert native_board.read_bytes() == original


@pytest.mark.parametrize(
    'uuid_order',
    [
        None,       # Fresh random KiCad UUIDs on save
        (1, 2, 3),  # J1 (12M, 10M) smallest UUID
        (2, 1, 3),  # J2 (4M, 10M) smallest UUID
        (3, 2, 1),  # J3 (8M, 16M) smallest UUID
    ],
)
def test_multiterminal_t_corridor_normalizes_collinear_overlaps(native_board, guarded_cli, uuid_order):
    run, _ = guarded_cli
    board = pcbnew.LoadBoard(str(native_board))
    signal = next(pad.GetNetCode() for pad in board.GetPads() if pad.GetNetname() == 'SIGNAL')
    for item in list(board.GetFootprints()) + list(board.GetTracks()):
        board.Remove(item)
    for idx, pos in enumerate(((12000000, 10000000), (4000000, 10000000), (8000000, 16000000)), 1):
        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference(f'J{idx}')
        fp.SetPosition(pcbnew.VECTOR2I(*pos))
        if uuid_order is not None:
            rank = uuid_order[idx - 1]
            fp.SetUuid(pcbnew.KIID(f'00000000-0000-4000-8000-{rank:012x}'))
        board.Add(fp)
        pad = pcbnew.PAD(fp)
        pad.SetNumber('1')
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(1000000, 1000000))
        pad.SetPosition(pcbnew.VECTOR2I(*pos))
        layers = pcbnew.LSET()
        layers.AddLayer(pcbnew.F_Cu)
        pad.SetLayerSet(layers)
        pad.SetNetCode(signal)
        if uuid_order is not None:
            rank = uuid_order[idx - 1]
            pad.SetUuid(pcbnew.KIID(f'00000000-0000-4000-8000-{100 + rank:012x}'))
        fp.Add(pad)
    for idx, (start, end) in enumerate((
            ((2300000, 8300000), (6750000, 8300000)),
            ((2300000, 11700000), (6750000, 11700000)),
            ((2300000, 8300000), (2300000, 11700000)),
            ((6750000, 11250000), (6750000, 15000000)),
            ((9250000, 8300000), (13700000, 8300000)),
            ((9250000, 11700000), (13700000, 11700000)),
            ((13700000, 8300000), (13700000, 11700000)),
            ((9250000, 11250000), (9250000, 15000000))), 1):
        t = pcbnew.PCB_TRACK(board)
        t.SetLayer(pcbnew.F_Cu)
        t.SetNetCode(0)
        t.SetStart(pcbnew.VECTOR2I(*start))
        t.SetEnd(pcbnew.VECTOR2I(*end))
        t.SetWidth(RULES['width_nm'])
        t.SetLocked(True)
        if uuid_order is not None:
            t.SetUuid(pcbnew.KIID(f'00000000-0000-4000-8000-{200 + idx:012x}'))
        board.Add(t)
    assert pcbnew.SaveBoard(str(native_board), board)
    data = bridge.export_board(native_board, {**RULES, 'grid_nm': 500000}, 100000)
    assert [p['at'] for p in data['pads']] == [
        [4000000, 10000000],
        [8000000, 16000000],
        [12000000, 10000000],
    ]
    assert data['tracks'] == sorted(
        data['tracks'],
        key=lambda t: (t['net'], t['start'], t['end'], t['width_nm'], t['locked'], t['id']),
    )
    request, plan_path = native_board.with_name('t-corridor.json'), native_board.with_name('t-plan.json')
    bridge.write_request(data, request)
    run(['cargo', 'run', '--locked', '--offline', '--manifest-path',
         str(ROOT / 'rust-engine/Cargo.toml'), '--', str(request), str(plan_path)])
    plan = json.loads(plan_path.read_bytes())
    assert plan['status'] == 'complete'
    tracks = plan['added_tracks']
    assert len(tracks) == 2
    assert sum(abs(t['end'][0] - t['start'][0]) + abs(t['end'][1] - t['start'][1]) for t in tracks) == 12750002
    for i, a in enumerate(tracks):
        for b in tracks[i + 1:]:
            for axis in (0, 1):
                ortho = 1 - axis
                if a['start'][ortho] == a['end'][ortho] == b['start'][ortho] == b['end'][ortho]:
                    alo, ahi = sorted((a['start'][axis], a['end'][axis]))
                    blo, bhi = sorted((b['start'][axis], b['end'][axis]))
                    assert min(ahi, bhi) <= max(alo, blo), (a, b)
    output = native_board.with_name('t-routed.kicad_pcb')
    run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'), 'apply',
         str(native_board), str(plan_path), str(output), '--request', str(request)])
    before = bridge.native_violations(native_board, native_board.with_name('t-before-drc.json'))
    after = bridge.native_violations(output, native_board.with_name('t-after-drc.json'))
    assert before['unconnected_items'] and after['unconnected_items'] == []
    key = lambda violation: json.dumps(violation, sort_keys=True)
    assert not (Counter(map(key, after['violations'])) - Counter(map(key, before['violations'])))


def test_multiterminal_t_corridor_partial_overlap_trims_cleanly(native_board, guarded_cli):
    run, _ = guarded_cli
    board = pcbnew.LoadBoard(str(native_board))
    signal = next(pad.GetNetCode() for pad in board.GetPads() if pad.GetNetname() == 'SIGNAL')
    for item in list(board.GetFootprints()) + list(board.GetTracks()):
        board.Remove(item)
    for idx, pos in enumerate(((4000000, 10000000), (12000000, 10000000), (9000000, 16000000)), 1):
        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference(f'J{idx}')
        fp.SetPosition(pcbnew.VECTOR2I(*pos))
        board.Add(fp)
        pad = pcbnew.PAD(fp)
        pad.SetNumber('1')
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(1000000, 1000000))
        pad.SetPosition(pcbnew.VECTOR2I(*pos))
        layers = pcbnew.LSET()
        layers.AddLayer(pcbnew.F_Cu)
        pad.SetLayerSet(layers)
        pad.SetNetCode(signal)
        fp.Add(pad)
    for start, end in (
            ((2300000, 8300000), (14700000, 8300000)),
            ((2300000, 11700000), (7750000, 11700000)),
            ((2300000, 8300000), (2300000, 11700000)),
            ((7750000, 11700000), (7750000, 14500000)),
            ((10250000, 11700000), (14700000, 11700000)),
            ((14700000, 8300000), (14700000, 11700000)),
            ((10250000, 11700000), (10250000, 14500000))):
        t = pcbnew.PCB_TRACK(board)
        t.SetLayer(pcbnew.F_Cu)
        t.SetNetCode(0)
        t.SetStart(pcbnew.VECTOR2I(*start))
        t.SetEnd(pcbnew.VECTOR2I(*end))
        t.SetWidth(RULES['width_nm'])
        t.SetLocked(True)
        board.Add(t)
    assert pcbnew.SaveBoard(str(native_board), board)
    data = bridge.export_board(native_board, {**RULES, 'grid_nm': 500000}, 100000)
    request, plan_path = native_board.with_name('t-partial.json'), native_board.with_name('t-partial-plan.json')
    bridge.write_request(data, request)
    run(['cargo', 'run', '--locked', '--offline', '--manifest-path',
         str(ROOT / 'rust-engine/Cargo.toml'), '--', str(request), str(plan_path)])
    plan = json.loads(plan_path.read_bytes())
    assert plan['status'] == 'complete'
    tracks = plan['added_tracks']
    assert len(tracks) == 3
    assert sum(abs(t['end'][0] - t['start'][0]) + abs(t['end'][1] - t['start'][1]) for t in tracks) == 13375001
    for i, a in enumerate(tracks):
        for b in tracks[i + 1:]:
            for axis in (0, 1):
                ortho = 1 - axis
                if a['start'][ortho] == a['end'][ortho] == b['start'][ortho] == b['end'][ortho]:
                    alo, ahi = sorted((a['start'][axis], a['end'][axis]))
                    blo, bhi = sorted((b['start'][axis], b['end'][axis]))
                    assert min(ahi, bhi) <= max(alo, blo), (a, b)
    output = native_board.with_name('t-partial-routed.kicad_pcb')
    run(['/usr/bin/python3', str(ROOT / 'tools/rust_engine_kicad.py'), 'apply',
         str(native_board), str(plan_path), str(output), '--request', str(request)])
    before = bridge.native_violations(native_board, native_board.with_name('t-partial-before-drc.json'))
    after = bridge.native_violations(output, native_board.with_name('t-partial-after-drc.json'))
    assert before['unconnected_items'] and after['unconnected_items'] == []
    key = lambda violation: json.dumps(violation, sort_keys=True)
    assert not (Counter(map(key, after['violations'])) - Counter(map(key, before['violations'])))


def _built_rust_engine(run):
    run(['cargo', 'build', '--locked', '--offline', '--manifest-path',
         str(ROOT / 'rust-engine/Cargo.toml')])
    return ROOT / 'rust-engine/target/debug/kicad-route-engine'


def test_route_native_end_to_end_under_guarded_cli(native_board, guarded_cli):
    run, audit_log = guarded_cli
    engine = _built_rust_engine(run)
    original = native_board.read_bytes()
    rules_path = native_board.with_name('rules.json')
    rules_path.write_text(json.dumps(RULES))

    output_a = native_board.with_name('native-routed-a.kicad_pcb')
    op_result_a = native_board.with_name('native-op-a.json')
    saved_req_a = native_board.with_name('native-req-a.json')
    res_a = run([
        str(engine), 'route-native', str(native_board), str(output_a),
        '--rules', str(rules_path),
        '--operation-result', str(op_result_a),
        '--save-request', str(saved_req_a),
    ], check=False)
    assert res_a.returncode == 0, res_a.stderr

    op_a = json.loads(res_a.stdout)
    assert op_result_a.read_text() == res_a.stdout
    assert op_a['schema'] == 'kicad-route/native-operation/1'
    assert op_a['version'] == 1
    assert op_a['status'] == 'complete' and op_a['exit_code'] == 0
    assert op_a['binary']['name'] == 'kicad-route-engine'
    assert op_a['binary']['binary_sha256'] == hashlib.sha256(engine.read_bytes()).hexdigest()
    assert op_a['source']['source_sha256'] == hashlib.sha256(original).hexdigest()
    assert op_a['source']['snapshot_sha256'] == op_a['source']['source_sha256']
    assert op_a['source']['sidecar_check']['passed'] is True
    assert op_a['request']['request_sha256'] == hashlib.sha256(saved_req_a.read_bytes()).hexdigest()
    assert op_a['request']['request_json'].encode('utf-8') == saved_req_a.read_bytes()
    assert op_a['routing']['status'] == 'complete'
    assert op_a['routing']['added_track_count'] > 0
    assert op_a['staging']['cleaned_up'] is True
    assert not Path(op_a['staging']['dir']).exists()
    assert op_a['publication']['commit_point_reached'] is True
    assert op_a['publication']['published'] is True
    assert op_a['publication']['ambiguous_termination'] is False
    assert op_a['publication']['output_sha256'] == hashlib.sha256(output_a.read_bytes()).hexdigest()

    # Verify native KiCad connectivity, DRC, and copper preservation.
    assert native_board.read_bytes() == original
    board = pcbnew.LoadBoard(str(output_a))
    board.BuildConnectivity()
    assert board.GetConnectivity().GetUnconnectedCount(False) == 0
    before = bridge.native_violations(native_board, native_board.with_name('native-before-drc.json'))
    after = bridge.native_violations(output_a, native_board.with_name('native-after-drc.json'))
    assert before['unconnected_items'] and after['unconnected_items'] == []
    key = lambda violation: json.dumps(violation, sort_keys=True)
    assert not (Counter(map(key, after['violations'])) - Counter(map(key, before['violations'])))

    # A second run on identical inputs must have a distinct operation_id nonce.
    output_b = native_board.with_name('native-routed-b.kicad_pcb')
    res_b = run([
        str(engine), 'route-native', str(native_board), str(output_b),
        '--rules', str(rules_path),
    ], check=False)
    assert res_b.returncode == 0, res_b.stderr
    op_b = json.loads(res_b.stdout)
    assert op_b['operation_id'] != op_a['operation_id']
    assert op_b['request']['request_sha256'] == op_a['request']['request_sha256']
    assert op_b['publication']['output_sha256'] == op_a['publication']['output_sha256']

    observed = [json.loads(line) for line in audit_log.read_text().splitlines()]
    assert sum(row.get('guard_started', False) for row in observed) >= 4
    assert sum(row.get('allowed', False) for row in observed) >= 4
    assert all(row.get('allowed', True) for row in observed)


@pytest.mark.parametrize('mode', ['pro', 'dru', 'dangling_pro', 'symlinked_board_with_dru'])
def test_route_native_rejects_original_and_symlink_sidecars_before_python(
        native_board, guarded_cli, tmp_path, mode):
    run, audit_log = guarded_cli
    engine = _built_rust_engine(run)
    rules_path = native_board.with_name('rules.json')
    rules_path.write_text(json.dumps(RULES))

    source = native_board
    if mode == 'pro':
        native_board.with_suffix('.kicad_pro').write_text('{}')
    elif mode == 'dru':
        native_board.with_suffix('.kicad_dru').write_text('(version 1)')
    elif mode == 'dangling_pro':
        native_board.with_suffix('.kicad_pro').symlink_to(tmp_path / 'absent.kicad_pro')
    else:
        native_board.with_suffix('.kicad_dru').write_text('(version 1)')
        clean_dir = tmp_path / 'clean-dir'
        clean_dir.mkdir()
        source = clean_dir / 'alias.kicad_pcb'
        source.symlink_to(native_board)

    output = tmp_path / 'rejected-sidecar.kicad_pcb'
    res = run([
        str(engine), 'route-native', str(source), str(output),
        '--rules', str(rules_path),
    ], check=False)
    assert res.returncode == 2
    op = json.loads(res.stdout)
    assert op['status'] == 'unsupported'
    assert op['source']['sidecar_check']['passed'] is False
    assert op['staging']['dir'] is None
    assert not output.exists()
    assert not audit_log.exists() or audit_log.read_text() == ''


@pytest.mark.parametrize('mode', ['chained_pro', 'chained_dru', 'chained_parent_dir_pro'])
def test_route_native_rejects_chained_symlink_sidecars_before_python(
        native_board, guarded_cli, tmp_path, mode):
    run, audit_log = guarded_cli
    engine = _built_rust_engine(run)
    rules_path = native_board.with_name('rules.json')
    rules_path.write_text(json.dumps(RULES))

    d1 = tmp_path / 'd1'
    d2 = tmp_path / 'd2'
    d3 = tmp_path / 'd3'
    for d in (d1, d2, d3):
        d.mkdir()
    real = d3 / 'real.kicad_pcb'
    real.write_bytes(native_board.read_bytes())

    mid = d2 / 'mid.kicad_pcb'
    mid.symlink_to(Path('..') / 'd3' / 'real.kicad_pcb')

    if mode == 'chained_pro':
        (d2 / 'mid.kicad_pro').write_text('{}')
        top = d1 / 'top.kicad_pcb'
        top.symlink_to(Path('..') / 'd2' / 'mid.kicad_pcb')
    elif mode == 'chained_dru':
        (d2 / 'mid.kicad_dru').write_text('(version 1)')
        top = d1 / 'top.kicad_pcb'
        top.symlink_to(Path('..') / 'd2' / 'mid.kicad_pcb')
    else:
        link_d2 = d1 / 'link_d2'
        link_d2.symlink_to(Path('..') / 'd2')
        (d2 / 'mid.kicad_pro').write_text('{}')
        top = d1 / 'top.kicad_pcb'
        top.symlink_to(Path('link_d2') / 'mid.kicad_pcb')

    output = tmp_path / 'rejected-chained.kicad_pcb'
    res = run([
        str(engine), 'route-native', str(top), str(output),
        '--rules', str(rules_path),
    ], check=False)
    assert res.returncode == 2
    op = json.loads(res.stdout)
    assert op['status'] == 'unsupported'
    assert op['source']['sidecar_check']['passed'] is False
    assert op['staging']['dir'] is None
    assert not output.exists()
    assert not audit_log.exists() or audit_log.read_text() == ''


def test_native_adapter_rejects_chained_symlink_sidecars(
        native_board, expected_request, tmp_path, monkeypatch):
    d1 = tmp_path / 'd1'
    d2 = tmp_path / 'd2'
    d3 = tmp_path / 'd3'
    for d in (d1, d2, d3):
        d.mkdir()
    real = d3 / 'real.kicad_pcb'
    real.write_bytes(native_board.read_bytes())
    mid = d2 / 'mid.kicad_pcb'
    mid.symlink_to(Path('..') / 'd3' / 'real.kicad_pcb')
    top = d1 / 'top.kicad_pcb'
    top.symlink_to(Path('..') / 'd2' / 'mid.kicad_pcb')

    # Clean 2-hop symlink chain succeeds on export_board
    exported = bridge.export_board(top, RULES, 100000)
    assert exported['source_id'] == hashlib.sha256(real.read_bytes()).hexdigest()

    # Intermediate .kicad_pro and .kicad_dru sidecars rejected in both export_board and apply_plan
    plan = {
        'version': 3,
        'status': 'complete',
        'request_sha256': hashlib.sha256(expected_request.read_bytes()).hexdigest(),
        'source_id': hashlib.sha256(real.read_bytes()).hexdigest(),
        'added_tracks': connection_tracks(),
        'removed_track_ids': [],
        'expanded_states': 0,
        'work_used': 0,
    }
    for suffix, content in (('.kicad_pro', '{}'), ('.kicad_dru', '(version 1)')):
        sidecar = d2 / f'mid{suffix}'
        sidecar.write_text(content)
        with pytest.raises(bridge.UnsupportedBoard, match='project/custom-rule'):
            bridge.export_board(top, RULES, 100000)
        with pytest.raises(bridge.UnsupportedBoard, match='project/custom-rule'):
            bridge.apply_plan(
                top,
                plan,
                tmp_path / f'out{suffix}.kicad_pcb',
                expected_request=expected_request.read_bytes(),
            )
        sidecar.unlink()

    # Post-read sidecar creation caught by source_snapshot re-check
    orig_read_bounded = bridge.read_bounded

    def add_sidecar_during_read(path):
        data = orig_read_bounded(path)
        if path == top:
            (d2 / 'mid.kicad_pro').write_text('{}')
        return data

    monkeypatch.setattr(bridge, 'read_bounded', add_sidecar_during_read)
    with pytest.raises(bridge.UnsupportedBoard, match='project/custom-rule'):
        with bridge.source_snapshot(top):
            pass


def test_route_native_pins_snapshot_under_source_mutation(native_board, guarded_cli, tmp_path):
    run, audit_log = guarded_cli
    engine = _built_rust_engine(run)
    rules_path = native_board.with_name('rules.json')
    rules_path.write_text(json.dumps(RULES))
    t0_expected = bridge.export_board(native_board, RULES, 100000)

    changed = pcbnew.LoadBoard(str(native_board))
    for footprint in changed.GetFootprints():
        if footprint.GetReference() == 'J2':
            footprint.SetPosition(pcbnew.VECTOR2I(17000000, 12000000))
    mutated_tmp = tmp_path / 'mutated.kicad_pcb'
    assert pcbnew.SaveBoard(str(mutated_tmp), changed)
    mutated_bytes = mutated_tmp.read_bytes()

    wrapper = tmp_path / 'mutating_bridge.py'
    wrapper.write_text(f'''import pathlib, runpy, sys
real_bridge = {str(ROOT / 'tools/rust_engine_kicad.py')!r}
source_path = pathlib.Path({str(native_board)!r})
mutated = pathlib.Path({str(mutated_tmp)!r}).read_bytes()
sys.argv[0] = real_bridge
try:
    runpy.run_path(real_bridge, run_name='__main__')
finally:
    if sys.argv[1] == 'export':
        source_path.write_bytes(mutated)
''')
    output = tmp_path / 'snapshot-pinned.kicad_pcb'
    res = run([
        str(engine), 'route-native', str(native_board), str(output),
        '--rules', str(rules_path), '--native-bridge', str(wrapper),
    ], check=False)
    assert res.returncode == 0, res.stderr
    assert native_board.read_bytes() == mutated_bytes
    assert bridge.export_board(output, RULES, 100000)['pads'] == t0_expected['pads']
    observed = [json.loads(line) for line in audit_log.read_text().splitlines()]
    assert all(row.get('allowed', True) for row in observed)


def test_route_native_preserves_existing_destination_and_concurrent_writer(
        native_board, guarded_cli, tmp_path):
    run, audit_log = guarded_cli
    engine = _built_rust_engine(run)
    rules_path = native_board.with_name('rules.json')
    rules_path.write_text(json.dumps(RULES))

    # 1) Pre-existing destination refused before Python
    existing = tmp_path / 'existing.kicad_pcb'
    existing.write_bytes(b'incumbent board')
    res_ex = run([
        str(engine), 'route-native', str(native_board), str(existing),
        '--rules', str(rules_path),
    ], check=False)
    assert res_ex.returncode == 8
    assert existing.read_bytes() == b'incumbent board'
    assert json.loads(res_ex.stdout)['status'] == 'destination_exists'
    assert not audit_log.exists() or audit_log.read_text() == ''

    # 2) Concurrent writer creates destination during apply before hard_link
    raced = tmp_path / 'raced.kicad_pcb'
    wrapper = tmp_path / 'race_bridge.py'
    wrapper.write_text(f'''import pathlib, runpy, sys
real_bridge = {str(ROOT / 'tools/rust_engine_kicad.py')!r}
dest = pathlib.Path({str(raced)!r})
sys.argv[0] = real_bridge
try:
    runpy.run_path(real_bridge, run_name='__main__')
except SystemExit as exc:
    if exc.code not in (None, 0):
        raise
if sys.argv[1] == 'apply':
    dest.write_bytes(b'concurrent writer board')
''')
    res_race = run([
        str(engine), 'route-native', str(native_board), str(raced),
        '--rules', str(rules_path), '--native-bridge', str(wrapper),
    ], check=False)
    assert res_race.returncode == 8
    assert raced.read_bytes() == b'concurrent writer board'
    op_race = json.loads(res_race.stdout)
    assert op_race['status'] == 'destination_exists'
    assert op_race['publication']['published'] is False
    assert op_race['staging']['cleaned_up'] is True
    assert not list(tmp_path.glob('.kicad-route-pub-*'))


@pytest.mark.parametrize('kind,expected_exit,expected_status', [
    ('max_expansions', 4, 'budget_exhausted'),
    ('max_work', 5, 'work_exhausted'),
])
def test_route_native_budget_exhaustion_and_residue_immunity(
        native_board, guarded_cli, tmp_path, kind, expected_exit, expected_status):
    run, audit_log = guarded_cli
    engine = _built_rust_engine(run)
    rules_path = native_board.with_name('rules.json')
    rules_path.write_text(json.dumps(RULES))

    out = tmp_path / f'{kind}.kicad_pcb'
    cmd = [
        str(engine), 'route-native', str(native_board), str(out),
        '--rules', str(rules_path),
    ]
    if kind == 'max_expansions':
        cmd.extend(['--max-expansions', '1'])
    else:
        limits_path = native_board.with_name('limits-zero.json')
        limits_path.write_text(json.dumps({'max_work': 0}))
        cmd.extend(['--limits', str(limits_path)])

    res = run(cmd, check=False)
    assert res.returncode == expected_exit, res.stderr
    op = json.loads(res.stdout)
    assert op['status'] == expected_status
    assert op['publication']['published'] is False
    assert op['staging']['cleaned_up'] is True
    assert not out.exists()

    # F-M5-N4 residue immunity: foreign PID-named temp files never block route-native
    clean_out = tmp_path / 'residue-clean.kicad_pcb'
    for pid_guess in (os.getpid(), os.getpid() + 1):
        (tmp_path / f'.{clean_out.name}.{pid_guess}.tmp').write_bytes(b'foreign')
    res_ok = run([
        str(engine), 'route-native', str(native_board), str(clean_out),
        '--rules', str(rules_path),
    ], check=False)
    assert res_ok.returncode == 0, res_ok.stderr
    assert clean_out.exists()
    observed = [json.loads(line) for line in audit_log.read_text().splitlines()]
    assert all(row.get('allowed', True) for row in observed)
