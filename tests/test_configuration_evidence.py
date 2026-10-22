"""Effective-config evidence binds the actual invocation and saved output."""
import json
import sys
from pathlib import Path

import pytest

from helpers import ROOT
from kicad_route import common, configuration_evidence

sys.path.insert(0, str(ROOT / 'src/kicad_route/router/py_router'))
import effective_config_evidence as producer
from routing_config import GridRouteConfig
from design_rules import DesignRules
from kicad_parser import PCBData, BoardInfo, Net


def capture(tmp_path, *, environment=None):
    board = tmp_path / 'board.kicad_pcb'
    board.write_text('input board')
    output = tmp_path / 'out.kicad_pcb'
    output.write_text('actual saved output')
    board.with_suffix('.kicad_pro').write_text('{}')
    argv = ['route.py', str(board), '--output', str(output)]
    environment = environment if environment is not None else {'KICAD_TERMINAL_ESCALATION': '0'}
    request = configuration_evidence.request(board, output, argv, environment,
                                             ROOT / 'src/kicad_route/router')
    request_path = tmp_path / 'request.json'
    common.write_json(request_path, request)
    request["request_sha256"] = common.sha256(request_path)
    cfg = GridRouteConfig(track_width=.27, layers=['F.Cu', 'B.Cu'],
            layer_clearances={'F.Cu': .31, 'B.Cu': .43}, net_via_sizes={7: (.72, .35)},
            net_track_widths={7: .42}, coplanar_net_ids={7})
    cfg.rules = DesignRules(board_min={'clearance': .21}, net_names={7: '/power'},
        copper_layers=['F.Cu', 'B.Cu'], groups={'U1': frozenset({'power'})},
        cli={'track_width': .27})
    pcb = PCBData(BoardInfo({0: 'F.Cu', 31: 'B.Cu'}, ['F.Cu', 'B.Cu']),
                  {7: Net(7, '/power')}, {}, [], [], {})
    producer.begin_execution()
    producer.begin_batch(outer=True)
    doc = producer.capture(cfg, pcb, str(board), ['power'], argv=argv,
        environment=environment, request_path=request_path)
    manifest = tmp_path / 'effective-configuration.json'
    producer.finish(doc, str(output), manifest)
    return request, manifest, output, cfg


def test_manifest_binds_saved_output_and_nondefault_effective_rules(tmp_path):
    request, manifest, output, _ = capture(tmp_path)
    record = configuration_evidence.inspect(manifest, request, output)
    assert record['status'] == 'bound'
    assert record['replay_authority'] is False
    doc = json.loads(manifest.read_text())
    assert doc['configuration']['track_width'] == .27
    assert doc['configuration']['net_via_sizes'] == {
        'type': 'map', 'items': [[7, {'type': 'tuple', 'items': [.72, .35]}]]}
    assert doc['configuration']['coplanar_net_ids'] == {'type': 'set', 'items': [7]}
    assert doc['rules']['table']['board_min']['clearance'] == .21
    assert doc['rules']['groups']['U1'] == {'type': 'frozenset', 'items': ['power']}
    output.write_text('replaced after emission')
    assert configuration_evidence.inspect(manifest, request, output)['status'] == 'mismatch'


def test_settings_source_input_and_field_bindings_fail_closed(tmp_path):
    request, manifest, output, _ = capture(tmp_path)
    original = json.loads(manifest.read_text())
    for change, reason in (({'source_sha256': '0' * 64}, 'source_sha256'),
                           ({'request_sha256': '0' * 64}, 'request/settings'),
                           ({'selected_environment': {}}, 'selected_environment'),
                           ({'invocation': ['different']}, 'invocation'),
                           ({'input': {'board_sha256': '0' * 64}}, 'input'),
                           ({'configuration': {}}, 'configuration field')):
        doc = {**original, **change}
        manifest.write_text(json.dumps(doc))
        rec = configuration_evidence.inspect(manifest, request, output)
        assert rec['status'] == 'mismatch'
        assert any(reason in v for v in rec['reasons'])
    manifest.unlink()
    assert configuration_evidence.inspect(manifest, request, output)['status'] == 'missing'
    manifest.write_text('{not-json')
    assert configuration_evidence.inspect(manifest, request, output)['status'] == 'mismatch'
    manifest.write_text(json.dumps({'schema': 'future'}))
    assert configuration_evidence.inspect(manifest, request, output)['status'] == 'unsupported'


def test_unknown_objects_and_dynamic_fields_are_explicitly_incomplete(tmp_path):
    request, manifest, output, cfg = capture(tmp_path)
    cfg.track_width = object()
    cfg.future_policy = {'cannot_silently_drop': True}
    board = tmp_path / 'board.kicad_pcb'
    pcb = PCBData(None, {}, {}, [], [], {})
    # Use the exact original request rather than inventing supported defaults.
    doc = producer.capture(cfg, pcb, str(board), [], argv=request['invocation'],
        environment=request['selected_environment'], request_path=tmp_path / 'request.json')
    producer.finish(doc, str(output), manifest)
    rec = configuration_evidence.inspect(manifest, request, output)
    assert rec['status'] == 'unsupported'
    assert doc['configuration']['track_width']['unsupported_type'] == 'builtins.object'
    assert doc['runtime_policy']['future_policy'] == {'cannot_silently_drop': True}
    assert any('future_policy' in v for v in rec['reasons'])


def test_canonical_capture_preserves_types_and_rule_context(tmp_path):
    request, manifest, output, cfg = capture(tmp_path)
    board = tmp_path / 'board.kicad_pcb'
    pcb = PCBData(None, {7: Net(7, '/power')}, {}, [], [], {})
    first = producer.capture(cfg, pcb, str(board), ['power'], argv=request['invocation'],
        environment=request['selected_environment'], request_path=tmp_path / 'request.json')
    cfg.layer_clearances = dict(reversed(list(cfg.layer_clearances.items())))
    second = producer.capture(cfg, pcb, str(board), ['power'], argv=request['invocation'],
        environment=request['selected_environment'], request_path=tmp_path / 'request.json')
    assert producer.canonical(first) == producer.canonical(second)
    assert first['rules']['net_names'] == {'type': 'map', 'items': [[7, '/power']]}
    assert first['rules']['copper_layers'] == ['F.Cu', 'B.Cu']
    assert set(first['effective_environment']) >= {'HISTORY', 'DYNAMIC_ITERATIONS', 'VIA_RUNG_2'}
    assert first['not_captured'] == ['per_net_search_clones', 'later_subruns',
                                    'derived_runtime_maps', 'native_validation']


def test_wrapper_accepts_legacy_missing_emitter_without_changing_process_validity(tmp_path, monkeypatch):
    from kicad_route import cli
    work = tmp_path / 'work'
    work.mkdir()
    board = work / 'board.kicad_pcb'
    board.write_text('input')
    def legacy(argv, cwd, log_path, budget, env_extra=None, **kwargs):
        destination = Path(argv[argv.index('--output') + 1])
        destination.write_text('output')
        log_path.write_text('legacy backend, no config evidence')
        return {'exit_code': 0}
    monkeypatch.setattr(cli, 'run_budgeted', legacy)
    monkeypatch.setattr(cli, 'krt_artwork_environment', lambda *args: {})
    settings = {'protect': 'locked', 'mode': 'all', 'backend_args': [], 'budget_s': 2}
    result = cli.krt_route(work, board, [], {'nets': {}}, settings)
    assert result['status'] == 'PASS'
    assert result['effective_configuration']['status'] == 'missing'


def test_malformed_nested_shapes_are_evidence_mismatches(tmp_path):
    request, manifest, output, _ = capture(tmp_path)
    original = json.loads(manifest.read_text())
    for update in ({'output': ['wrong']}, {'output': 'wrong'}, {'input': ['wrong']},
                   {'issues': [None]}, {'issues': 'wrong'}, {'net_inventory': 'wrong'},
                   {'net_inventory': [[7]]}, {'scope': 3}, {'runtime_policy': []},
                   {'output': {**original['output'], 'stage': 'post-refill'}}):
        manifest.write_text(json.dumps({**original, **update}))
        assert configuration_evidence.inspect(manifest, request, output)['status'] == 'mismatch'


def test_source_hash_algorithm_matches_owned_identity():
    from kicad_route import engine
    assert producer.source_sha() == engine.source_sha256(ROOT / 'src/kicad_route/router')


def test_real_owned_backend_emitter_is_consumed(tmp_path, monkeypatch):
    import shutil
    import os
    import pytest
    from kicad_route import cli
    selected_python = Path(os.environ.get('KERNEL_PYTHON') or cli.route_python())
    if not selected_python.is_file():
        pytest.skip('requires the installed owned routing interpreter')
    monkeypatch.setattr(cli, 'KRT_DIR', ROOT / 'src/kicad_route/router')
    monkeypatch.setattr(cli, 'KRT_VENV', selected_python.parents[1])
    work = tmp_path / 'work'
    project = work / 'input'
    shutil.copytree(ROOT / 'fixtures/route_demo', project)
    board = project / 'route_demo.kicad_pcb'
    facts, _ = cli.board_facts(board, work / 'native-input-facts.json')
    assert facts is not None
    settings = {'protect': 'locked', 'mode': 'scope', 'backend_args': [], 'budget_s': 20,
                'max_iterations': 5000, 'backend_env': {
                    'KICAD_NET_RESCUE': '0', 'KICAD_PLANE_FINALIZE': '0',
                    'KICAD_GLOBAL_PLAN': '0'}}
    result = cli.krt_route(work, board, ['/SIG_A'], facts, settings)
    assert result['status'] == 'PASS', result
    common.write_json(work / 'wrapper-process.json', {
        **common.public(result), 'output_board': str(result['output_board'])})
    config_record = result['effective_configuration']
    assert config_record['status'] == 'bound', config_record
    doc = json.loads((work / configuration_evidence.MANIFEST).read_text())
    assert doc['configuration']['max_iterations'] == 5000
    assert doc['runtime_policy']['escalation']['items'][0] == 'off'
    assert doc['runtime_policy']['fab_tier']['items'][0] == 'standard'
    assert doc['effective_environment']['DYNAMIC_ITERATIONS'] is False
    assert doc['scope'] == ['/SIG_A']
    assert config_record['backend_output_sha256'] == common.sha256(result['output_board'])


def test_required_coverage_metadata_and_unsupported_markers_cannot_be_bound(tmp_path):
    request, manifest, output, _ = capture(tmp_path)
    original = json.loads(manifest.read_text())
    for key in ('effective_environment', 'not_captured', 'units'):
        doc = dict(original)
        del doc[key]
        manifest.write_text(json.dumps(doc))
        assert configuration_evidence.inspect(manifest, request, output)['status'] == 'mismatch'
    original['configuration']['track_width'] = {'unsupported_type': 'builtins.object'}
    manifest.write_text(json.dumps(original))
    assert configuration_evidence.inspect(manifest, request, output)['status'] == 'mismatch'


def test_native_named_inventory_and_requested_scope_are_qualified(tmp_path):
    request, manifest, output, _ = capture(tmp_path)
    request['named_nets'] = ['/power']
    request['scope'] = ['power']
    assert configuration_evidence.inspect(manifest, request, output)['status'] == 'bound'
    doc = json.loads(manifest.read_text())
    doc['net_inventory'] = [[7, '/other']]
    manifest.write_text(json.dumps(doc))
    assert configuration_evidence.inspect(manifest, request, output)['status'] == 'mismatch'


def test_nonfinite_scalars_and_missing_process_policy_are_mismatches(tmp_path):
    request, manifest, output, _ = capture(tmp_path)
    original = manifest.read_text()
    assert '"track_width":0.27' in original
    manifest.write_text(original.replace('"track_width":0.27', '"track_width":1e999'))
    assert configuration_evidence.inspect(manifest, request, output)['status'] == 'mismatch'
    doc = json.loads(original)
    doc['runtime_policy'] = {}
    manifest.write_text(json.dumps(doc))
    assert configuration_evidence.inspect(manifest, request, output)['status'] == 'mismatch'


def test_complete_manifest_matches_versioned_schema(tmp_path):
    import jsonschema
    _, manifest, _, _ = capture(tmp_path)
    schema = json.loads((ROOT / 'schemas/effective-configuration-1.json').read_text())
    jsonschema.validate(json.loads(manifest.read_text()), schema)


def test_ambient_routing_controls_are_bound_not_only_caller_overrides(tmp_path):
    environment = {'KICAD_DIRECT_FIRST': '0', 'KICAD_PLANE_FRAGILITY_COST': '2.0'}
    request, manifest, output, _ = capture(tmp_path, environment=environment)
    assert configuration_evidence.inspect(manifest, request, output)['status'] == 'bound'
    changed = configuration_evidence.request(tmp_path / 'board.kicad_pcb', output,
        request['invocation'], {**environment, 'KICAD_DIRECT_FIRST': '1'},
        ROOT / 'src/kicad_route/router')
    assert changed != {k: v for k, v in request.items() if k != 'request_sha256'}
    changed['request_sha256'] = request['request_sha256']
    assert configuration_evidence.inspect(manifest, changed, output)['status'] == 'mismatch'
    doc = json.loads(manifest.read_text())
    doc['routing_environment']['KICAD_PLANE_FRAGILITY_COST'] = '0'
    manifest.write_text(json.dumps(doc))
    assert configuration_evidence.inspect(manifest, request, output)['status'] == 'mismatch'


def test_later_batch_execution_cannot_claim_single_batch_configuration(tmp_path):
    producer.begin_batch(outer=True)
    request, manifest, output, _ = capture(tmp_path)
    assert configuration_evidence.inspect(manifest, request, output)['status'] == 'bound'
    doc = json.loads(manifest.read_text())
    producer.begin_batch(outer=False)
    producer.finish(doc, str(output), manifest)
    rec = configuration_evidence.inspect(manifest, request, output)
    assert rec['status'] == 'unsupported'
    assert any('later' in reason for reason in rec['reasons'])
    producer.begin_batch(outer=True)


@pytest.mark.parametrize('execution', [
    {'outer_batches': 1, 'later_subruns': 0},
    {'outer_batches': 2, 'later_subruns': 0},
    {'outer_batches': 1, 'later_subruns': 1},
], ids=['single', 'multiple', 'later'])
@pytest.mark.parametrize('update', [
    {}, {'issues': None}, {'issues': 0}, {'issues': False}, {'issues': 'wrong'},
    {'issues': {}}, {'issues': [None]}, {'issues': ['known issue', 1]},
], ids=['missing', 'null', 'number', 'boolean', 'string', 'object',
        'null-element', 'mixed-elements'])
def test_malformed_issues_remain_mismatch_for_all_batch_counts(tmp_path, execution, update):
    request, manifest, output, _ = capture(tmp_path)
    doc = json.loads(manifest.read_text())
    del doc['issues']
    doc.update(update, execution=execution)
    manifest.write_text(json.dumps(doc))
    result = configuration_evidence.inspect(manifest, request, output)
    assert result['status'] == 'mismatch'
    assert 'invalid capture qualification' in result['reasons']


@pytest.mark.parametrize('execution,status', [
    ({'outer_batches': 1, 'later_subruns': 0}, 'bound'),
    ({'outer_batches': 2, 'later_subruns': 0}, 'unsupported'),
    ({'outer_batches': 1, 'later_subruns': 1}, 'unsupported'),
])
def test_valid_issues_preserve_batch_coverage_status(tmp_path, execution, status):
    request, manifest, output, _ = capture(tmp_path)
    doc = json.loads(manifest.read_text())
    doc['execution'] = execution
    manifest.write_text(json.dumps(doc))
    assert configuration_evidence.inspect(manifest, request, output)['status'] == status
