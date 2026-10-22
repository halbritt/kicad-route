"""Evidence of one resolved batch config; never a replay or grading authority."""
from dataclasses import fields
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sys

SCHEMA = 'kicad-route/effective-configuration/1'
REQUEST_ENV = 'KICAD_EFFECTIVE_CONFIG_REQUEST'
ENVIRONMENT_NAMES = (
    'KICAD_INRUN_FLOOR_SYNC', 'KICAD_TERMINAL_ESCALATION',
    'KICAD_IMPEDANCE_NECKDOWN', 'KICAD_RESCUE_CAP_MOVE',
    'KICAD_DYNAMIC_ITERATIONS', 'KICAD_RIP_PREEXISTING', 'KICAD_FINALIZE_RIP',
    'KICAD_HISTORY_COST', 'KICAD_VIA_RUNG', 'KICAD_CLI',
    'KICAD_NET_RESCUE', 'KICAD_PLANE_FINALIZE', 'KICAD_GLOBAL_PLAN',
    'KICAD_POUR_GUARD', 'KICAD_PACK_INLINE')
RUNTIME_FIELDS = ('_keep_input_copper', 'ordering_strategy', 'final_reconcile')
SOURCE_ROOT = Path(__file__).resolve().parents[1]


_BATCHES = {'outer_batches': 0, 'later_subruns': 0}


def begin_execution():
    """Start the one CLI invocation whose output will carry this receipt."""
    _BATCHES.update(outer_batches=0, later_subruns=0)


def begin_batch(*, outer):
    _BATCHES['outer_batches' if outer else 'later_subruns'] += 1


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def file_sha(path):
    path = Path(path)
    return sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def source_sha(root=SOURCE_ROOT):
    digest = sha256()
    for path in sorted(Path(root).rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts:
            digest.update(path.relative_to(root).as_posix().encode() + b'\0')
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _typed(value, location, issues):
    # Only closed plain-data types used by GridRouteConfig, not arbitrary objects.
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [_typed(v, f'{location}[{i}]', issues) for i, v in enumerate(value)]
    if type(value) in (tuple, set, frozenset):
        items = [_typed(v, location, issues) for v in value]
        if type(value) in (set, frozenset):
            items.sort(key=canonical)
        return {'type': type(value).__name__, 'items': items}
    if type(value) is dict:
        if all(type(k) is str for k in value):
            return {k: _typed(value[k], f'{location}.{k}', issues) for k in sorted(value)}
        if all(type(k) is int for k in value):
            return {'type': 'map', 'items': [[k, _typed(value[k], location, issues)] for k in sorted(value)]}
    issues.append(f'unsupported value at {location}: {type(value).__module__}.{type(value).__name__}')
    return {'unsupported_type': f'{type(value).__module__}.{type(value).__name__}'}


def capture(config, pcb_data, input_file, scope, *, argv=None, environment=None, request_path=None):
    from routing_config import GridRouteConfig
    from design_rules import DesignRules
    import env_knobs
    from fab_tiers import get_default_fab_tier, get_escalation_policy
    environment = os.environ if environment is None else environment
    request_path = request_path or environment.get(REQUEST_ENV)
    request = json.loads(Path(request_path).read_text()) if request_path else None
    if request is not None and not isinstance(request, dict):
        raise ValueError('configuration evidence request must be an object')
    issues = []
    supported = json.loads(Path(__file__).with_name('effective_config_fields.json').read_text())
    actual = [f.name for f in fields(config)]
    if type(config) is not GridRouteConfig or actual != supported:
        issues.append('unsupported configuration field inventory')
    configuration = {name: _typed(getattr(config, name), f'configuration.{name}', issues)
                     for name in actual if name != 'rules'}
    rules = config.rules
    if rules is None:
        rules_record = None
    elif type(rules) is DesignRules:
        rules_record = {'table': _typed(rules.table(), 'rules.table', issues),
                        'net_names': _typed(rules.net_names, 'rules.net_names', issues),
                        'groups': _typed(rules.groups, 'rules.groups', issues),
                        'copper_layers': _typed(rules.copper_layers, 'rules.copper_layers', issues),
                        'notes': _typed(rules.notes, 'rules.notes', issues)}
    else:
        rules_record = _typed(rules, 'rules', issues)
    runtime = {name: _typed(getattr(config, name), f'runtime_policy.{name}', issues)
               for name in RUNTIME_FIELDS if hasattr(config, name)}
    extras = sorted(set(vars(config)) - set(actual) - set(RUNTIME_FIELDS))
    for name in extras:
        # Keep unknown configuration values visible; never claim complete by omission.
        runtime[name] = _typed(getattr(config, name), f'runtime_policy.{name}', issues)
        issues.append(f'unsupported dynamic configuration field: {name}')
    runtime['fab_tier'] = _typed(get_default_fab_tier(), 'runtime_policy.fab_tier', issues)
    runtime['escalation'] = _typed(get_escalation_policy(), 'runtime_policy.escalation', issues)
    effective_knobs = {name: _typed(getattr(env_knobs, name), f'effective_environment.{name}', issues)
        for name in ('INRUN_FLOOR_SYNC', 'TERMINAL_ESCALATION', 'RESCUE_CAP_MOVE',
                     'DYNAMIC_ITERATIONS', 'DYNAMIC_ITERATIONS_CLAMP', 'VIA_RUNG_2', 'HISTORY',
                     'NET_RESCUE', 'GLOBAL_PLAN', 'PACK_INLINE', 'PACKAGE_PROXIMITY')}
    board = Path(input_file)
    return {'schema': SCHEMA, 'capture_status': 'incomplete' if issues else 'complete',
            'issues': issues, 'replay_authority': False,
            'coverage': 'resolved_outer_batch_only',
            'not_captured': ['per_net_search_clones', 'later_subruns', 'derived_runtime_maps', 'native_validation'],
            'stage': 'after_rule_width_pour_resolution_before_normal_pass',
            'units': {'geometry': 'mm', 'grid_coordinates': 'cells', 'time': 'seconds',
                      'costs': 'GridRouteConfig field-specific units; not normalized'},
            'source_sha256': source_sha(), 'producer_sha256': file_sha(__file__),
            'request_sha256': file_sha(request_path) if request_path else None,
            'invocation': list(sys.argv if argv is None else argv),
            'selected_environment': {name: environment.get(name) for name in ENVIRONMENT_NAMES},
            'routing_environment': {key: value for key, value in sorted(environment.items())
                if key.startswith('KICAD_') and key != 'KICAD_EFFECTIVE_CONFIG_REQUEST' and value is not None},
            'effective_environment': effective_knobs,
            'input': {'board_sha256': file_sha(board), 'rules': {
                suffix: file_sha(board.with_suffix(suffix)) for suffix in ('.kicad_pro', '.kicad_dru')}},
            'net_inventory': [[int(nid), net.name] for nid, net in sorted(pcb_data.nets.items())],
            'scope': list(scope), 'field_names': actual,
            'configuration': configuration, 'rules': rules_record, 'runtime_policy': runtime,
            'runtime_field_presence': {name: hasattr(config, name) for name in RUNTIME_FIELDS},
            'requested_output': (request or {}).get('output')}


def finish(record, output_file, destination):
    destination = Path(destination)
    doc = dict(record, execution=dict(_BATCHES), output={'board_sha256': file_sha(output_file),
                             'stage': 'backend_output_before_wrapper_lock_restore_and_native_refill'})
    if doc['output']['board_sha256'] is None:
        doc['capture_status'] = 'incomplete'
        doc['issues'] = [*doc['issues'], 'backend output missing']
    temporary = destination.with_name(destination.name + '.partial')
    temporary.write_text(canonical(doc) + '\n')
    os.replace(temporary, destination)
