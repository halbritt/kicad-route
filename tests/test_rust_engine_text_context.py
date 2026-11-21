"""Native text rendering under the source, private and published PCB names."""
import hashlib
import json
import os
from pathlib import Path
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
        companions[output.with_name('fp-lib-table')] = inputs['fp-lib-table']
        assert set(output_dir.iterdir()) == {output, *companions}
        assert {p: p.read_bytes() for p in companions} == companions
        saved = pcbnew.LoadBoard(str(output))
        assert next(d for d in saved.GetDrawings() if d.m_Uuid.AsString() == text_id).GetText() == content
        after = run_native_drc(output, sidecar_source=source, refill_zones=True)
        assert not after.unconnected_items and not after.violations

        # Grade source and publication in their actual project directories.
        def drc_at_path(path):
            report_path = tmp_path / (path.parent.name + '-drc.json')
            run(['kicad-cli', 'pcb', 'drc', '--format', 'json', '--severity-all', '--all-track-errors',
                 '--refill-zones', '--units', 'mm', '-o', str(report_path), str(path)])
            return json.loads(report_path.read_bytes())

        baseline_drc = drc_at_path(source)
        published_drc = drc_at_path(output)
        assert len(baseline_drc['unconnected_items']) == 6 and not published_drc['unconnected_items']
        assert baseline_drc['violations'] == published_drc['violations'] == []
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

@pytest.mark.parametrize('case', ['fresh', 'installed-parentheses', 'relative-in-place', 'relative-shared', 'relative-missing',
                                  'relative-different', 'relative-conflict', 'conflict', 'stray-table'])
def test_footprint_libraries_at_actual_publication(tmp_path, guarded_cli, case):
    run, _ = guarded_cli
    engine = _built_rust_engine(run)
    source = copy_project(tmp_path, 'source')
    project = source.with_suffix('.kicad_pro')
    settings = json.loads(project.read_bytes())
    settings['board']['design_settings']['rule_severities'] = {'lib_footprint_issues': 'error'}
    project.write_text(json.dumps(settings))
    table = source.with_name('fp-lib-table')
    if case == 'installed-parentheses':
        table.write_text(table.read_text().replace('${KICAD10_FOOTPRINT_DIR}', '$(KICAD10_FOOTPRINT_DIR)'))
    if case.startswith('relative'):
        table.write_text(table.read_text().replace('${KICAD10_FOOTPRINT_DIR}', '${KIPRJMOD}/my footprints'))
        (source.parent / 'my footprints').symlink_to(
            Path(os.environ.get('KICAD10_FOOTPRINT_DIR', '/usr/share/kicad/footprints')), target_is_directory=True)
    directory = source.parent if case == 'relative-in-place' else tmp_path / 'output'
    directory.mkdir(exist_ok=True)
    output = directory / 'routed.kicad_pcb'
    destination_table = directory / 'fp-lib-table'
    if case == 'relative-shared':
        (directory / 'my footprints').symlink_to(source.parent / 'my footprints', target_is_directory=True)
    elif case in ('relative-different', 'relative-conflict'):
        for name in ('Capacitor_SMD', 'Resistor_SMD', 'Connector_PinHeader_2.54mm'):
            (directory / 'my footprints' / (name + '.pretty')).mkdir(parents=True)
        if case == 'relative-conflict':
            destination_table.write_bytes(table.read_bytes())
    elif case in ('conflict', 'stray-table'):
        destination_table.write_bytes(b'(fp_lib_table (version 7))')
    if case == 'stray-table':
        table.unlink()
    original_bytes = table.read_bytes() if table.exists() else None
    incumbent = destination_table.read_bytes() if destination_table.exists() else None
    result_path = tmp_path / 'operation.json'
    result = run([str(engine), 'route-native', str(source), str(output),
                  '--operation-result', str(result_path)], check=False)
    report = json.loads(result_path.read_bytes())
    if case in ('conflict', 'stray-table', 'relative-conflict'):
        assert result.returncode and not report['publication']['commit_point_reached']
        assert not output.exists() and not output.with_suffix('.kicad_pro').exists()
        reason = report['reason']
        assert report['status'] == 'publication_refused' and 'fp-lib-table' in reason
        assert (destination_table.read_bytes() if destination_table.exists() else None) == incumbent
    else:
        assert (result.returncode, report['status']) == (0, 'complete'), result.stderr
        if case in ('relative-missing', 'relative-different'):
            assert b'${KIPRJMOD}' not in destination_table.read_bytes()
            assert str(source.parent / 'my footprints') in destination_table.read_text()
        else:
            assert destination_table.read_bytes() == original_bytes
        for board, expected_opens in ((source, 6), (output, 0)):
            drc = bridge.native_violations(board, tmp_path / (board.stem + '-drc.json'),
                                           refill_zones=True)
            assert len(drc['unconnected_items']) == expected_opens
            assert drc['violations'] == []
    assert (table.read_bytes() if table.exists() else None) == original_bytes


def test_private_apply_drc_uses_project_footprint_table(tmp_path, guarded_cli):
    run, _ = guarded_cli
    engine = _built_rust_engine(run)
    source = copy_project(tmp_path, 'grade')
    project = source.with_suffix('.kicad_pro')
    settings = json.loads(project.read_bytes())
    settings['board']['design_settings']['rule_severities'] = {'lib_footprint_issues': 'error'}
    project.write_text(json.dumps(settings))
    request = tmp_path / 'request.json'
    bridge.write_request(bridge.export_board(source), request)
    plan = tmp_path / 'plan.json'
    run([str(engine), str(request), str(plan)])
    validation = tmp_path / 'grade.json'
    bridge.apply_plan(source, json.loads(plan.read_bytes()), tmp_path / 'result.kicad_pcb',
                      expected_request=request.read_bytes(), validation_report=validation)
    grade = json.loads(validation.read_bytes())
    assert grade['before_violation_count'] == grade['after_violation_count'] == 0

    # A bad prepared destination table must be visible to candidate DRC even
    # though the original library and source-side DRC are still correct.
    empty = tmp_path / 'empty.pretty'
    empty.mkdir()
    wrong_table = tmp_path / 'wrong-table'
    wrong_table.write_text(source.with_name('fp-lib-table').read_text().replace(
        '${KICAD10_FOOTPRINT_DIR}/Resistor_SMD.pretty', str(empty)))
    wrong_output = tmp_path / 'wrong.kicad_pcb'
    with pytest.raises(bridge.NativeValidationError, match='lib_footprint_issues'):
        bridge.apply_plan(source, json.loads(plan.read_bytes()), wrong_output,
                          expected_request=request.read_bytes(),
                          footprint_publication=(source.parent, tmp_path, wrong_table))
    assert not wrong_output.exists()

@pytest.mark.parametrize(('preexisting_library', 'root'), [(False, '${KIPRJMOD}'), (True, '${KIPRJMOD}'),
                                                          (False, '$(KIPRJMOD)')])
def test_ac_matching_relative_footprints_in_fresh_and_populated_destinations(tmp_path, guarded_cli, preexisting_library, root):
    run, _ = guarded_cli
    directory = tmp_path / 'original'
    shutil.copytree(ROOT / 'fixtures/ac_matching', directory)
    source = directory / 'chain.kicad_pcb'
    source_table = directory / 'fp-lib-table'
    source_table.write_bytes(source_table.read_bytes().replace(b'${KIPRJMOD}', root.encode()))
    original_bytes = source_table.read_bytes()
    saved = tmp_path / 'saved'
    saved.mkdir()
    dest_table = saved / 'fp-lib-table'
    if preexisting_library:
        shutil.copytree(directory / 'AcQualification.pretty', saved / 'equivalent.pretty')
        # Keep the locally named, byte-equivalent library and its table.
        incumbent = original_bytes.replace(b'AcQualification.pretty', b'equivalent.pretty') + b'\n'
        dest_table.write_bytes(incumbent)
    output = saved / source.name
    run([str(_built_rust_engine(run)), 'route-native', str(source), str(output),
         '--diff-pairs', 'auto', '--pair-layers', 'F.Cu', '--ac-couple-chain-json', '["C1","C2"]',
         '--ac-match-tolerance', '0.1', '--ac-meander-amplitude', '3'])
    if preexisting_library:
        assert dest_table.read_bytes() == incumbent
    else:
        assert str(directory / 'AcQualification.pretty') in dest_table.read_text()
    assert source_table.read_bytes() == original_bytes
    drc = bridge.native_violations(output, tmp_path / 'drc.json', refill_zones=True)
    assert not drc['unconnected_items']
    assert not [v for v in drc['violations'] if v['type'] == 'lib_footprint_issues']
