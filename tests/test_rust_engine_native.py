"""Native I/O and executable vertical-slice checks for the Rust engine."""
import importlib.util
import hashlib
import json
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
    assert len(data['pads']) == 2
    assert {tuple(p['at']) for p in data['pads']} == {(3000000, 10000000), (17000000, 10000000)}
    assert data['tracks'][0]['locked'] is True
    assert data['tracks'][0]['start'] == [10000000, 4000000]
    assert len({p['id'] for p in data['pads']} | {t['id'] for t in data['tracks']}) == 3
    assert native_board.read_bytes() == original


def test_native_adapter_applies_exact_edit_list(native_board):
    original = native_board.read_bytes()
    exported = bridge.export_board(native_board, RULES, 100000)
    ident = str(uuid.uuid4())
    added = {'id': ident, 'net': 1, 'start': [3000000, 10000000],
             'end': [5000000, 10000000], 'width_nm': 250000, 'locked': False}
    plan = {'version': 1, 'source_id': exported['source_id'], 'status': 'complete',
            'added_tracks': [added], 'removed_track_ids': [], 'expanded_states': 0}
    output = native_board.with_name('output.kicad_pcb')
    bridge.apply_plan(native_board, plan, output)
    actual = bridge.export_board(output, RULES, 100000)
    assert {t['id']: t for t in actual['tracks']} == {
        exported['tracks'][0]['id']: exported['tracks'][0], ident: added}
    assert actual['pads'] == exported['pads']
    assert native_board.read_bytes() == original


def test_rust_route_preserves_copper_and_passes_native_connectivity_drc(native_board):
    original = native_board.read_bytes()
    exported = bridge.export_board(native_board, RULES, 100000)
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
    assert plan['added_tracks']
    assert 0 < plan['expanded_states'] <= exported['max_expansions']
    output = native_board.with_name('routed.kicad_pcb')
    bridge.apply_plan(native_board, plan, output)
    actual = bridge.export_board(output, RULES, 100000)
    assert actual['rules'] == exported['rules']
    assert actual['pads'] == exported['pads']
    assert actual['nets'] == exported['nets']
    assert {t['id']: t for t in actual['tracks']} == {
        t['id']: t for t in exported['tracks'] + plan['added_tracks']}
    assert native_board.read_bytes() == original

    reports = []
    for label, path, expected_opens in [('before', native_board, 1), ('after', output, 0)]:
        board = pcbnew.LoadBoard(str(path))
        board.BuildConnectivity()
        assert board.GetConnectivity().GetUnconnectedCount(False) == expected_opens
        report_path = native_board.with_name(f'{label}-drc.json')
        subprocess.run(
            ['kicad-cli', 'pcb', 'drc', '--format', 'json', '--severity-all',
             '--output', str(report_path), str(path)],
            check=True, capture_output=True, text=True, timeout=30,
        )
        report = json.loads(report_path.read_text())
        assert len(report['unconnected_items']) == expected_opens
        reports.append(report)
    # The intentionally isolated protected track has a pre-existing dangling
    # warning. No new warning or error is allowed, including new copper items.
    assert reports[1]['violations'] == reports[0]['violations']


def test_rust_does_not_report_tangent_copper_as_connected(native_board):
    board = pcbnew.LoadBoard(str(native_board))
    board.Remove(next(iter(board.GetTracks())))
    track = pcbnew.PCB_TRACK(board)
    track.SetLayer(pcbnew.F_Cu)
    track.SetNetCode(1)
    track.SetStart(pcbnew.VECTOR2I(3000000, 10000000))
    track.SetEnd(pcbnew.VECTOR2I(16400000, 10000000))
    track.SetWidth(200000)
    board.Add(track)
    assert pcbnew.SaveBoard(str(native_board), board)
    board = pcbnew.LoadBoard(str(native_board))
    board.BuildConnectivity()
    assert board.GetConnectivity().GetUnconnectedCount(False) == 1
    original = native_board.read_bytes()
    exported = bridge.export_board(native_board, RULES, 100000)
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
    actual = bridge.export_board(output, RULES, 100000)
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
            'added_tracks': [], 'removed_track_ids': []}
    with pytest.raises(bridge.UnsupportedBoard):
        bridge.apply_plan(native_board, plan, output)
    assert not output.exists()


@pytest.mark.parametrize('status,source', [('unroutable', None), ('complete', 'stale')])
def test_native_adapter_does_not_write_failed_or_stale_plan(native_board, status, source):
    original = native_board.read_bytes()
    data = bridge.export_board(native_board, RULES, 100000)
    plan = {'version': 1, 'status': status, 'source_id': source or data['source_id'],
            'added_tracks': [], 'removed_track_ids': []}
    output = native_board.with_name('output.kicad_pcb')
    with pytest.raises(ValueError):
        bridge.apply_plan(native_board, plan, output)
    assert not output.exists()
    assert native_board.read_bytes() == original
