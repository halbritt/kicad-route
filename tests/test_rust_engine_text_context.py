"""Native text rendering under the source, private and published PCB names."""
import hashlib
import json
import shutil

import pcbnew
import pytest

from tests.e2e_rust_router.kicad_verify import run_native_drc
from test_rust_engine_native import (
    ROOT, RULES, _built_rust_engine, _make_rect_board, bridge, connection_tracks,
    guarded_cli as guarded_cli, native_board as native_board,
)


def add_text(owner, layer, content, x=106.5, y=108.4):
    drawing = pcbnew.PCB_TEXT(owner)
    drawing.SetLayer(layer)
    drawing.SetText(content)
    drawing.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
    drawing.SetTextSize(pcbnew.VECTOR2I(1_600_000, 1_600_000))
    drawing.SetTextThickness(250_000)
    drawing.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_LEFT)
    owner.Add(drawing)
    return drawing.m_Uuid.AsString()


def copy_project(tmp_path, stem):
    directory = tmp_path / 'input'
    directory.mkdir()
    source = directory / (stem + '.kicad_pcb')
    for suffix in ('.kicad_pcb', '.kicad_pro', '.kicad_dru'):
        shutil.copy2(ROOT / 'fixtures/route_demo' / ('route_demo' + suffix), source.with_suffix(suffix))
    for table in ('fp-lib-table', 'sym-lib-table'):
        shutil.copy2(ROOT / 'fixtures/route_demo' / table, directory / table)
    return source


@pytest.mark.parametrize('dynamic', [True, False], ids=['variable', 'literal'])
@pytest.mark.parametrize('rename', [False, True], ids=['same-name', 'new-name'])
def test_public_route_project_named_copper(tmp_path, guarded_cli, dynamic, rename):
    run, _ = guarded_cli
    engine = _built_rust_engine(run)
    source = copy_project(tmp_path, 'my_long_project_name_rev_b')
    board = pcbnew.LoadBoard(str(source))
    content = '${PROJECTNAME}' if dynamic else source.stem
    text_id = add_text(board, pcbnew.F_Cu, content)
    assert pcbnew.SaveBoard(str(source), board)
    inputs = {p.name: p.read_bytes() for p in source.parent.iterdir()}
    output_dir = tmp_path / 'output'
    output_dir.mkdir()
    output = output_dir / ('renamed.kicad_pcb' if rename else source.name)
    operation = tmp_path / 'operation.json'
    before = run_native_drc(source, refill_zones=True)
    assert len(before.unconnected_items) == 6 and not before.violations

    result = run([str(engine), 'route-native', str(source), str(output),
                  '--operation-result', str(operation)], check=False)
    report = json.loads(operation.read_bytes())
    assert {p.name: p.read_bytes() for p in source.parent.iterdir()} == inputs
    if dynamic:
        assert (result.returncode, report['status']) == (2, 'unsupported')
        assert not report['publication']['commit_point_reached'] and list(output_dir.iterdir()) == []
        reason = next(p['detail'] for p in report['phases'] if p['phase'] == 'native_export')
        assert all(s in reason for s in (text_id, '${PROJECTNAME}', 'F.Cu'))
    else:
        assert (result.returncode, report['status']) == (0, 'complete'), result.stderr
        assert report['publication']['commit_point_reached']
        companions = {output.with_suffix(ext): inputs[source.with_suffix(ext).name]
                      for ext in ('.kicad_pro', '.kicad_dru')}
        assert set(output_dir.iterdir()) == {output, *companions}
        assert {p: p.read_bytes() for p in companions} == companions
        saved = pcbnew.LoadBoard(str(output))
        assert next(d for d in saved.GetDrawings() if d.m_Uuid.AsString() == text_id).GetText() == content
        after = run_native_drc(output, sidecar_source=source, refill_zones=True)
        assert not after.unconnected_items and not after.violations

        # Reopen the actual output without supplying the source's library tables.
        # Give unrouted copper the same basename and directory-local library context.
        baseline = tmp_path / 'baseline' / output.name
        baseline.parent.mkdir()
        shutil.copyfile(source, baseline)
        for companion, data in companions.items():
            baseline.with_suffix(companion.suffix).write_bytes(data)

        def drc_at_path(path):
            report_path = tmp_path / (path.parent.name + '-drc.json')
            run(['kicad-cli', 'pcb', 'drc', '--format', 'json', '--severity-all', '--all-track-errors',
                 '--refill-zones', '--units', 'mm', '-o', str(report_path), str(path)])
            return json.loads(report_path.read_bytes())

        baseline_drc = drc_at_path(baseline)
        published_drc = drc_at_path(output)
        assert len(baseline_drc['unconnected_items']) == 6 and not published_drc['unconnected_items']
        assert all((v['type'], v['severity']) == ('lib_footprint_issues', 'warning')
                   for v in baseline_drc['violations'])
        assert sorted(json.dumps(v, sort_keys=True) for v in published_drc['violations']) == sorted(
            json.dumps(v, sort_keys=True) for v in baseline_drc['violations'])
        assert {p: p.read_bytes() for p in companions} == companions


@pytest.mark.parametrize(('owner', 'layer', 'content', 'mask_bridges'), [
    ('board', pcbnew.B_Cu, '${FILENAME}', False),
    ('board', pcbnew.F_Mask, '${ALIAS}', False),
    ('footprint', pcbnew.F_Cu, '${VALUE}', False),
    ('footprint', pcbnew.B_Mask, '${ALIAS}', True),
    ('field', pcbnew.B_Mask, '${ALIAS}', True),
])
def test_native_export_and_apply_refuse_contextual_artwork(
        native_board, owner, layer, content, mask_bridges):
    request = bridge.export_board(native_board, RULES, 100_000)
    # An older producer could have exported a snapshot for exactly these source
    # bytes. The native apply boundary checks the board again before using edits.
    board = pcbnew.LoadBoard(str(native_board))
    fp = next(iter(board.GetFootprints()))
    fp.SetAllowSolderMaskBridges(mask_bridges)
    if owner == 'field':
        field = fp.Value()
        field.SetLayer(layer)
        field.SetVisible(True)
        field.SetText(content)
        text_id = field.m_Uuid.AsString()
    else:
        fp.SetValue('${ALIAS}')
        text_id = add_text(board if owner == 'board' else fp, layer, content, 6, 4)
    assert pcbnew.SaveBoard(str(native_board), board)
    project = json.loads((ROOT / 'fixtures/route_demo/route_demo.kicad_pro').read_bytes())
    project['text_variables'] = {'ALIAS': '${INNER}', 'INNER': '${PROJECTNAME}'}
    native_board.with_suffix('.kicad_pro').write_text(json.dumps(project))
    request['source_id'] = hashlib.sha256(native_board.read_bytes()).hexdigest()
    retained = json.dumps(request).encode()
    plan = {
        'version': 3, 'source_id': request['source_id'], 'status': 'complete',
        'request_sha256': hashlib.sha256(retained).hexdigest(), 'expanded_states': 0, 'work_used': 0,
        'added_tracks': connection_tracks(), 'added_vias': [], 'removed_track_ids': [],
    }
    with pytest.raises(bridge.UnsupportedBoard, match=text_id):
        bridge.export_board(native_board, RULES, 100_000)
    destination = native_board.with_name('published.kicad_pcb')
    with pytest.raises(bridge.UnsupportedBoard, match=text_id):
        bridge.apply_plan(native_board, plan, destination, expected_request=retained)
    assert not destination.exists()


def test_native_assembly_variant_can_override_a_literal_reference(native_board):
    board = pcbnew.LoadBoard(str(native_board))
    fp = next(iter(board.GetFootprints()))
    board.AddVariant('Prototype')
    variant = fp.AddVariant('Prototype')
    variant.SetFieldValue('Reference', '${PROJECTNAME}')
    fp.SetVariant(variant)
    text_id = add_text(fp, pcbnew.F_Cu, '${REFERENCE}', 2, 2)
    assert pcbnew.SaveBoard(str(native_board), board)
    with pytest.raises(bridge.UnsupportedBoard, match=text_id):
        bridge.export_board(native_board, RULES, 100_000)


def test_native_fixed_footprint_labels_and_variable_silkscreen_route(tmp_path, guarded_cli):
    run, _ = guarded_cli
    engine = _built_rust_engine(run)
    native_board = _make_rect_board(tmp_path / 'board.kicad_pcb', 0, 0, 30_000_000, 20_000_000)
    board = pcbnew.LoadBoard(str(native_board))
    fp = next(iter(board.GetFootprints()))
    fp.SetValue('47k')
    graphic = add_text(fp, pcbnew.F_Cu, '${VALUE}', 2, 2)
    add_text(fp, pcbnew.F_Cu, '${REFERENCE}', 2, 5)
    add_text(board, pcbnew.F_Cu, '${LAYER}', 2, 8)
    silk = add_text(board, pcbnew.F_SilkS, '${PROJECTNAME}', 2, 17)
    assert pcbnew.SaveBoard(str(native_board), board)
    output = native_board.with_name('published.kicad_pcb')
    run([str(engine), 'route-native', str(native_board), str(output)])
    saved = pcbnew.LoadBoard(str(output))
    texts = {d.m_Uuid.AsString(): d for owner in saved.GetFootprints() for d in owner.GraphicalItems()}
    assert texts[graphic].GetText() == '${VALUE}' and texts[graphic].GetShownText(False) == '47k'
    legend = next(d for d in saved.GetDrawings() if d.m_Uuid.AsString() == silk)
    assert legend.GetText() == '${PROJECTNAME}' and legend.GetShownText(False) == output.stem
    after = run_native_drc(output, sidecar_source=native_board)
    assert not after.unconnected_items and not after.violations


@pytest.mark.parametrize('operand', ['${PROJECTNAME}', '${RULE}', 'SIG_A_OUT'])
def test_public_route_variable_in_native_rule_condition(tmp_path, guarded_cli, operand):
    run, _ = guarded_cli
    engine = _built_rust_engine(run)
    source = copy_project(tmp_path, 'SIG_A_OUT')
    rules = source.with_suffix('.kicad_dru')
    name = 'SIG_A_OUT' if operand == '${RULE}' else operand
    rule = f'''(rule "conditional width" (condition "A.NetName == '/{name}'")
               (constraint track_width (min 0.45mm)))'''
    with rules.open('a') as file:
        if operand == '${RULE}':
            project_path = source.with_suffix('.kicad_pro')
            project = json.loads(project_path.read_bytes())
            project['text_variables'] = {'RULE': '\n' + rule}
            project_path.write_text(json.dumps(project))
            file.write('\n# Conditional width example: ${RULE}\n')
        else:
            file.write('\n' + rule + '\n')
    source_files = {p.name: p.read_bytes() for p in source.parent.iterdir()}
    output_dir = tmp_path / 'output'
    output_dir.mkdir()
    output = output_dir / source.name
    operation = tmp_path / 'operation.json'
    result = run([str(engine), 'route-native', str(source), str(output),
                  '--operation-result', str(operation)], check=False)
    report = json.loads(operation.read_bytes())
    assert {p.name: p.read_bytes() for p in source.parent.iterdir()} == source_files
    if operand.startswith('${'):
        assert (result.returncode, report['status']) == (2, 'unsupported')
        detail = next(p['detail'] for p in report['phases'] if p['phase'] == 'native_export')
        assert '.kicad_dru' in detail and operand in detail and not output.exists()
    else:
        assert (result.returncode, report['status']) == (0, 'complete'), result.stderr
        saved = pcbnew.LoadBoard(str(output))
        widths = [t.GetWidth() for t in saved.GetTracks() if type(t) is pcbnew.PCB_TRACK
                  and t.GetNetname() == '/SIG_A_OUT']
        assert widths and min(widths) >= 450_000
        after = run_native_drc(output, sidecar_source=source, refill_zones=True)
        assert not after.violations and not after.unconnected_items


@pytest.mark.parametrize(('source_name', 'output_name', 'new_warnings', 'use_alias'), [
    ('a', 'published_under_another_name', True, False),
    ('published_under_another_name', 'published_under_another_name', False, False),
    ('published_under_another_name', 'a', False, False),
    ('published_under_another_name', 'published_under_another_name', False, True),
])
def test_public_drc_renders_silk_with_admitted_project_and_real_names(
        tmp_path, guarded_cli, source_name, output_name, new_warnings, use_alias):
    run, _ = guarded_cli
    source = copy_project(tmp_path, source_name)
    project_path = source.with_suffix('.kicad_pro')
    project = json.loads(project_path.read_bytes())
    project['text_variables'] = {'LEGEND': '${PROJECTNAME}'}
    project_path.write_text(json.dumps(project))
    board = pcbnew.LoadBoard(str(source))
    text_id = add_text(board, pcbnew.F_SilkS, '${LEGEND}', 111.5, 103)
    text = next(t for t in board.GetDrawings() if t.m_Uuid.AsString() == text_id)
    text.SetTextSize(pcbnew.VECTOR2I(1_000_000, 1_000_000))
    text.SetTextThickness(200_000)
    assert pcbnew.SaveBoard(str(source), board)
    output_dir = tmp_path / 'output'
    output_dir.mkdir()
    output = output_dir / (output_name + '.kicad_pcb')
    for ext in ('.kicad_pro', '.kicad_dru'):
        shutil.copy2(source.with_suffix(ext), output.with_suffix(ext))
    for table in ('fp-lib-table', 'sym-lib-table'):
        shutil.copy2(source.parent / table, output_dir / table)
    before = run_native_drc(source, refill_zones=True)
    source_findings = [] if source_name == 'a' else ['silk_over_copper'] * 2
    assert [v['type'] for v in before.violations] == source_findings
    assert len(before.unconnected_items) == 6
    operation = tmp_path / 'result.json'
    route_source = source
    if use_alias:
        route_source = tmp_path / 'a.kicad_pcb'
        route_source.symlink_to(source)
    result = run([str(_built_rust_engine(run)), 'route-native', str(route_source), str(output),
                  '--operation-result', str(operation)], check=False)
    report = json.loads(operation.read_bytes())
    if new_warnings:
        assert (result.returncode, report['status']) == (2, 'native_validation_failed')
        failure = next(p for p in report['phases'] if p['phase'] == 'native_apply_validate')
        assert 'silk_over_copper (2)' in failure['detail']
        assert not output.exists() and not report['publication']['commit_point_reached']
    else:
        assert (result.returncode, report['status']) == (0, 'complete'), result.stderr
        after = run_native_drc(output, refill_zones=True)
        assert [v['type'] for v in after.violations] == ([] if output_name == 'a' else source_findings)
        assert not after.unconnected_items
    for ext in ('.kicad_pro', '.kicad_dru'):
        assert output.with_suffix(ext).read_bytes() == source.with_suffix(ext).read_bytes()


def test_native_detects_file_path_through_project_variable_on_silk(tmp_path):
    source = copy_project(tmp_path, 'plain_name')
    project = source.with_suffix('.kicad_pro')
    data = json.loads(project.read_bytes())
    data['text_variables'] = {'LEGEND': '${FILEPATH}'}
    project.write_text(json.dumps(data))
    board = pcbnew.LoadBoard(str(source))
    text_id = add_text(board, pcbnew.F_SilkS, '${LEGEND}')
    assert pcbnew.SaveBoard(str(source), board)
    with pytest.raises(bridge.UnsupportedBoard, match=text_id + '.*absolute PCB path'):
        bridge.export_board(source)
