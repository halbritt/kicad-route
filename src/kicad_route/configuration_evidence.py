"""Qualify owned-backend configuration evidence without granting replay authority."""
from hashlib import sha256
import json
import math
from pathlib import Path

from . import common, engine

SCHEMA = 'kicad-route/effective-configuration/1'
ENVIRONMENT_NAMES = (
    'KICAD_INRUN_FLOOR_SYNC', 'KICAD_TERMINAL_ESCALATION',
    'KICAD_IMPEDANCE_NECKDOWN', 'KICAD_RESCUE_CAP_MOVE',
    'KICAD_DYNAMIC_ITERATIONS', 'KICAD_RIP_PREEXISTING', 'KICAD_FINALIZE_RIP',
    'KICAD_HISTORY_COST', 'KICAD_VIA_RUNG', 'KICAD_CLI',
    'KICAD_NET_RESCUE', 'KICAD_PLANE_FINALIZE', 'KICAD_GLOBAL_PLAN',
    'KICAD_POUR_GUARD', 'KICAD_PACK_INLINE')
MANIFEST = 'effective-configuration.json'
REQUEST = 'effective-configuration-request.json'
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
EXCLUDED_STAGES = ['per_net_search_clones', 'later_subruns', 'derived_runtime_maps', 'native_validation']
UNITS = {'geometry': 'mm', 'grid_coordinates': 'cells', 'time': 'seconds',
         'costs': 'GridRouteConfig field-specific units; not normalized'}
KNOB_NAMES = {'INRUN_FLOOR_SYNC', 'TERMINAL_ESCALATION', 'RESCUE_CAP_MOVE',
              'DYNAMIC_ITERATIONS', 'DYNAMIC_ITERATIONS_CLAMP', 'VIA_RUNG_2',
              'HISTORY', 'NET_RESCUE', 'GLOBAL_PLAN', 'PACK_INLINE', 'PACKAGE_PROXIMITY'}


def _sha(path):
    try:
        return common.sha256(path) if Path(path).is_file() else None
    except OSError:
        return None


def request(board, output, argv, environment, source_root, settings=None, *, scope=None, facts=None):
    source_root, board = Path(source_root), Path(board)
    inventory = common.load_json(source_root / 'py_router/effective_config_fields.json')
    return {'schema': 'kicad-route/effective-configuration-request/1',
            'source_sha256': engine.source_sha256(source_root),
            'source_root': str(source_root.resolve()),
            'producer_sha256': _sha(source_root / 'py_router/effective_config_evidence.py'),
            'field_names': inventory,
            'scope': sorted(scope) if scope is not None else None,
            'named_nets': sorted(n for n in facts['nets'] if n) if facts is not None else None,
            'input': {'board_sha256': _sha(board), 'rules': {
                suffix: _sha(board.with_suffix(suffix)) for suffix in ('.kicad_pro', '.kicad_dru')}},
            'output': str(Path(output).resolve()), 'invocation': [str(a) for a in argv],
            'selected_environment': {name: environment.get(name) for name in ENVIRONMENT_NAMES},
            'settings_sha256': sha256(json.dumps(settings or {}, sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode()).hexdigest()}


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON object key')
        result[key] = value
    return result


def inspect(manifest, expected, output):
    manifest = Path(manifest)
    record = {'status': 'missing', 'reasons': [], 'replay_authority': False,
              'record': common.rel(manifest), 'sha256': None,
              'coverage': 'resolved_outer_batch_only',
              'output_stage': 'backend_output_before_wrapper_lock_restore_and_native_refill'}
    if not manifest.is_file():
        record['reasons'] = ['backend emitted no resolved configuration evidence (legacy or unsupported path)']
        return record
    try:
        with manifest.open('rb') as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            record.update(status='unsupported', reasons=['configuration evidence exceeds size limit'])
            return record
        record['sha256'] = sha256(raw).hexdigest()
        doc = json.loads(raw, object_pairs_hook=_json_object, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (OSError, ValueError, RecursionError) as error:
        record.update(status='mismatch', reasons=[f'malformed configuration evidence: {error}'])
        return record
    if not isinstance(doc, dict) or doc.get('schema') != SCHEMA:
        record.update(status='unsupported', reasons=['unsupported configuration evidence schema'])
        return record
    reasons = []
    try:
        if engine.source_sha256(Path(expected['source_root'])) != expected['source_sha256']:
            reasons.append('owned source changed during execution')
    except OSError:
        reasons.append('owned source unavailable after execution')
    checks = {'source_sha256': expected['source_sha256'],
              'producer_sha256': expected['producer_sha256'], 'field_names': expected['field_names'],
              'input': expected['input'], 'invocation': expected['invocation'],
              'selected_environment': expected['selected_environment'],
              'requested_output': expected['output'], 'replay_authority': False,
              'coverage': 'resolved_outer_batch_only',
              'not_captured': EXCLUDED_STAGES, 'units': UNITS,
              'stage': 'after_rule_width_pour_resolution_before_normal_pass'}
    for key, value in checks.items():
        if doc.get(key) != value:
            reasons.append(f'{key} binding mismatch')
    if doc.get('request_sha256') != expected.get('request_sha256'):
        reasons.append('request/settings binding mismatch')
    output_record = doc.get('output')
    if not isinstance(output_record, dict):
        output_record = {}
        reasons.append('malformed backend output binding')
    if output_record.get('board_sha256') != _sha(output) or not _sha(output):
        reasons.append('backend output hash mismatch')
    if output_record.get('stage') != record['output_stage']:
        reasons.append('backend output stage mismatch')
    knobs = doc.get('effective_environment')
    if not isinstance(knobs, dict) or set(knobs) != KNOB_NAMES:
        reasons.append('missing or unsupported effective environment coverage')
    config = doc.get('configuration')
    wanted = set(expected['field_names'] or []) - {'rules'}
    if not isinstance(config, dict) or set(config) != wanted:
        reasons.append('configuration field coverage mismatch')
    if 'rules' not in doc or not isinstance(doc.get('runtime_policy'), dict):
        reasons.append('missing rules or runtime policy evidence')
    if doc.get('rules') is not None and (not isinstance(doc['rules'], dict)
            or not {'table', 'net_names', 'groups', 'copper_layers', 'notes'} <= doc['rules'].keys()):
        if doc.get('capture_status') == 'complete':
            reasons.append('incomplete design rules evidence')
    runtime = doc.get('runtime_policy')
    presence = doc.get('runtime_field_presence')
    known_runtime = {'_keep_input_copper', 'ordering_strategy', 'final_reconcile'}
    if (not isinstance(presence, dict) or set(presence) != known_runtime
            or any(type(v) is not bool for v in presence.values())):
        reasons.append('missing runtime field coverage')
    elif isinstance(runtime, dict) and any((key in runtime) != present for key, present in presence.items()):
        reasons.append('runtime field coverage mismatch')
    if not isinstance(runtime, dict) or not {'fab_tier', 'escalation'} <= runtime.keys():
        reasons.append('missing effective process policy')
    inventory = doc.get('net_inventory')
    valid_inventory = not (not isinstance(inventory, list) or any(not isinstance(v, list) or len(v) != 2
            or type(v[0]) is not int or not isinstance(v[1], str) for v in inventory)
            or len({v[0] for v in inventory}) != len(inventory))
    if not valid_inventory:
        reasons.append('invalid net inventory')
    elif expected.get('named_nets') is not None and sorted(v[1] for v in inventory if v[1]) != expected['named_nets']:
        reasons.append('named net inventory differs from native input facts')
    scope = doc.get('scope')
    if not isinstance(scope, list) or any(not isinstance(v, str) for v in scope):
        reasons.append('invalid captured scope')
    elif expected.get('scope') is not None and scope != expected['scope']:
        reasons.append('captured scope differs from wrapper scope')
    issues = doc.get('issues')
    if doc.get('capture_status') not in ('complete', 'incomplete') or not isinstance(issues, list) or any(not isinstance(v, str) for v in issues):
        reasons.append('invalid capture qualification')
    if doc.get('capture_status') == 'complete' and issues:
        reasons.append('complete capture contains unsupported values')
    # Structural qualification only; do not decode this into executable config.
    remaining = 100000
    def check_values(value, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 64:
            raise ValueError('configuration value structure exceeds limit')
        if type(value) is float and not math.isfinite(value):
            raise ValueError('nonfinite configuration value')
        if isinstance(value, dict):
            if 'unsupported_type' in value and doc.get('capture_status') == 'complete':
                raise ValueError('complete capture contains unsupported_type marker')
            if set(value) == {'type', 'items'}:
                if value['type'] not in ('map', 'tuple', 'set', 'frozenset') or not isinstance(value['items'], list):
                    raise ValueError('invalid typed container')
                if value['type'] == 'map':
                    rows = value['items']
                    if any(not isinstance(row, list) or len(row) != 2 or type(row[0]) is not int for row in rows):
                        raise ValueError('invalid typed integer map')
                    if len({row[0] for row in rows}) != len(rows):
                        raise ValueError('duplicate integer map key')
            for entry in value.values():
                check_values(entry, depth + 1)
        elif isinstance(value, list):
            for entry in value:
                check_values(entry, depth + 1)
    try:
        check_values(doc)
    except ValueError as error:
        reasons.append(str(error))
    status = 'mismatch' if reasons else ('bound' if doc['capture_status'] == 'complete' else 'unsupported')
    record.update(status=status, reasons=reasons or issues,
                  named_inventory_binding='native_input_facts' if expected.get('named_nets') is not None else 'shape_only',
                  numeric_net_ids='captured_only_not_independently_validated',
                  capture_status=doc.get('capture_status'),
                  captured_input_sha256=doc['input'].get('board_sha256') if isinstance(doc.get('input'), dict) else None,
                  backend_output_sha256=output_record.get('board_sha256'),
                  request_sha256=expected.get('request_sha256'),
                  settings_sha256=expected['settings_sha256'])
    return record
