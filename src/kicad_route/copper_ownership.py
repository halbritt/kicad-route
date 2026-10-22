"""Bound saved-board custody and exact disposable removal; no routing or adoption."""
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from . import common

SCHEMA = 'kicad-route/copper-ownership/1'
MANIFEST = 'validate/copper-ownership.json'


class OwnershipError(ValueError):
    """Evidence or selection cannot authorize an exact disposable removal."""


def _json(path):
    def finite(text):
        value = float(text)
        if not math.isfinite(value):
            raise OwnershipError('nonfinite JSON')
        return value
    def unique(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise OwnershipError('duplicate JSON key')
            out[k] = v
        return out
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            raise OwnershipError('linked or oversized evidence')
        data = json.loads(path.read_text(), object_pairs_hook=unique, parse_float=finite,
                          parse_constant=lambda _: (_ for _ in ()).throw(OwnershipError('nonfinite JSON')))
        pending = [(data, 0)]
        count = 0
        while pending:
            value, depth = pending.pop()
            count += 1
            if depth > 64 or count > 100000:
                raise OwnershipError('evidence structure exceeds limit')
            if isinstance(value, (list, dict)):
                pending.extend((v, depth + 1) for v in (value.values() if isinstance(value, dict) else value))
        return data
    except (OSError, json.JSONDecodeError, UnicodeError, RecursionError) as e:
        raise OwnershipError('missing or malformed evidence JSON') from e


def _digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _path(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise OwnershipError('expected a run-relative path')
    p = root / relative
    if '..' in Path(relative).parts or any(x.is_symlink() for x in [p, *p.parents] if x != root.parent):
        raise OwnershipError('symlink or escaping evidence path')
    if not p.resolve().is_relative_to(root):
        raise OwnershipError('escaping evidence path')
    return p


def inventory(facts):
    """Validate exact native records, retaining multiplicity through unique UUIDs."""
    if not isinstance(facts, dict) or not isinstance(facts.get('sha256'), str):
        raise OwnershipError('missing source-bound native facts')
    out = {}
    for kind in ('tracks', 'vias'):
        if not isinstance(facts.get(kind), list):
            raise OwnershipError('missing complete copper inventory')
        for item in facts[kind]:
            if not isinstance(item, dict):
                raise OwnershipError('malformed copper record')
            base = {'uuid', 'net', 'locked'}
            fields = (base | {'position', 'diameter', 'drill', 'layers', 'via_type',
                              'annulus_geometry', 'layer_diameters'} if kind == 'vias'
                      else base | {'start', 'end', 'width', 'layer', 'kind'} | ({'mid'} if item.get('kind') == 'arc' else set()))
            if set(item) != fields or not isinstance(item.get('uuid'), str) or not item['uuid'] or item['uuid'] in out:
                raise OwnershipError('unknown copper fields or duplicate/missing UUID')
            if not isinstance(item['net'], str) or type(item['locked']) is not bool:
                raise OwnershipError('malformed copper identity')
            points = ['position'] if kind == 'vias' else ['start', 'end'] + (['mid'] if 'mid' in item else [])
            scalars = ['diameter', 'drill'] if kind == 'vias' else ['width']
            if any(not isinstance(item[p], list) or len(item[p]) != 2 or any(type(n) is not int for n in item[p]) for p in points):
                raise OwnershipError('noninteger native geometry')
            if any(type(item[p]) is not int or item[p] <= 0 for p in scalars):
                raise OwnershipError('invalid native dimensions')
            if kind == 'vias':
                if not isinstance(item['layers'], list) or len(item['layers']) != 2 or any(not isinstance(x, str) for x in item['layers']) or not isinstance(item['via_type'], str):
                    raise OwnershipError('invalid via layers/type')
                _via_layer_geometry(facts, item)
            elif item['kind'] not in ('segment', 'arc') or not isinstance(item['layer'], str):
                raise OwnershipError('invalid track kind/layer')
            out[item['uuid']] = {'collection': kind, **item}
    return out


def _via_layer_geometry(facts, via):
    """Require the producer's complete, ordered circular annulus inventory."""
    layers = facts.get('copper_layers')
    if (not isinstance(layers, list) or not layers
            or any(not isinstance(layer, dict) or not isinstance(layer.get('name'), str)
                   or not layer['name'] for layer in layers)):
        raise OwnershipError('missing physical copper stack')
    stack = [layer['name'] for layer in layers]
    span = via['layers']
    if (len(set(stack)) != len(stack) or any(layer not in stack for layer in span)
            or stack.index(span[0]) >= stack.index(span[1])):
        raise OwnershipError('invalid native via span')
    physical = stack[stack.index(span[0]):stack.index(span[1]) + 1]
    sizes = via['layer_diameters']
    if (via['annulus_geometry'] != 'circular-layer-discs'
            or not isinstance(sizes, list) or len(sizes) != len(physical)
            or any(not isinstance(size, dict) or set(size) != {'layer', 'diameter'}
                   or type(size['diameter']) is not int or size['diameter'] <= 0 for size in sizes)
            or [size['layer'] for size in sizes] != physical
            or any(size['layer'] == 'F.Cu' and size['diameter'] != via['diameter'] for size in sizes)):
        raise OwnershipError('unsupported or incomplete native via annulus geometry')


def _classification(reference, candidate, scope):
    a, b = inventory(reference), inventory(candidate)
    if not isinstance(scope, list) or not scope or any(not isinstance(n, str) or not n for n in scope) or len(set(scope)) != len(scope):
        raise OwnershipError('missing exact scope')
    # Without writer lineage, a vanished input primitive could be reminted
    # onto any other net. No newly delivered item is removable in that case.
    input_unchanged = all(b.get(u) == x for u, x in a.items())
    nets = {}
    for net in sorted({x['net'] for x in b.values()}):
        new = sorted(u for u, x in b.items() if u not in a and x['net'] == net)
        reasons = []
        if net not in scope:
            reasons.append('outside_scope')
        if not input_unchanged:
            reasons.append('input_lineage_unknown')
        if not new:
            reasons.append('no_generated_copper')
        if any(b[u]['locked'] for u in new):
            reasons.append('generated_copper_locked')
        nets[net] = {'generated_uuids': new, 'eligible': not reasons, 'reasons': reasons}
    return {'inventory': [b[u] for u in sorted(b)], 'nets': nets}


def _rules(board):
    if any(p.is_symlink() for p in (board.with_suffix('.kicad_pro'), board.with_suffix('.kicad_dru'))):
        raise OwnershipError('linked rules are unsupported')
    return {p.name: common.sha256(p) if p.exists() else None
            for p in (board.with_suffix('.kicad_pro'), board.with_suffix('.kicad_dru'))}


def _bound_inputs(run, attempt, result):
    if not isinstance(attempt, dict) or not {'candidate_dir', 'reference_dir', 'board', 'reference_facts', 'scope'} <= attempt.keys() or not isinstance(attempt['scope'], dict) or 'nets' not in attempt['scope']:
        raise OwnershipError('malformed attempt context')
    if not isinstance(result, dict) or any(not isinstance(result.get(k), dict) for k in ('backend', 'process')):
        raise OwnershipError('malformed owned result context')
    if not isinstance(result, dict) or result.get('kind') != 'route' or result.get('backend', {}).get('name') != 'builtin' or attempt.get('backend') != 'builtin' or result.get('process', {}).get('status') != 'PASS':
        raise OwnershipError('custody requires an owned builtin candidate')
    grade = result.get('validation')
    if not isinstance(grade, dict) or grade.get('status') not in ('PASS', 'FAIL'):
        raise OwnershipError('missing native grade')
    # Validate the basename too; it must not escape the verified directories.
    if not isinstance(attempt['board'], str) or Path(attempt['board']).name != attempt['board']:
        raise OwnershipError('invalid board basename')
    board = _path(run, attempt['candidate_dir']) / attempt['board']
    reference = _path(run, attempt['reference_dir']) / attempt['board']
    for p in (board, reference):
        if p.is_symlink() or not p.is_file():
            raise OwnershipError('missing or linked board')
    a_path = _path(run, attempt['reference_facts'])
    b_path = _path(run, 'validate/candidate-facts.json')
    a, b = _json(a_path), _json(b_path)
    inventory(a); inventory(b)
    sha = common.sha256(board)
    if sha != b['sha256'] or sha != grade.get('validated_board_sha256') or sha != grade.get('candidate_board_sha256') or common.sha256(reference) != a['sha256']:
        raise OwnershipError('board/facts/native-grade hash mismatch')
    ev = result.get('evidence')
    if not isinstance(ev, dict) or ev.get('candidate_board_sha256') != sha or ev.get('input_board_sha256') != a['sha256']:
        raise OwnershipError('result board evidence mismatch')
    if not isinstance(ev.get('records'), dict) or not isinstance(ev.get('reference_files'), dict):
        raise OwnershipError('missing evidence records')
    for rel in ('attempt.json', 'validate/candidate-facts.json'):
        if ev['records'].get(rel) != common.sha256(_path(run, rel)):
            raise OwnershipError('record binding mismatch: ' + rel)
    records = {rel: checksum for rel, checksum in ev['records'].items() if rel != MANIFEST}
    for rel, checksum in records.items():
        if common.sha256(_path(run, rel)) != checksum:
            raise OwnershipError('record binding mismatch: ' + rel)
    for rel, checksum in ev['reference_files'].items():
        if common.sha256(_path(_path(run, attempt['reference_dir']), rel)) != checksum:
            raise OwnershipError('reference file binding mismatch')
    rules = _rules(board)
    if rules != _rules(reference):
        raise OwnershipError('candidate rules differ from reference')
    expected_rules = {k: v for k, v in ev['reference_files'].items() if k.endswith(('.kicad_pro', '.kicad_dru'))}
    if ev.get('rules_files') != expected_rules:
        raise OwnershipError('rules evidence mismatch')
    tool = result.get('tool_identity')
    if not isinstance(tool, dict) or not isinstance(tool.get('backend'), dict) or tool['backend'].get('name') != 'builtin' or not tool['backend'].get('source_sha256'):
        raise OwnershipError('missing owned backend source identity')
    pairs = attempt.get('pairs', [])
    if not isinstance(pairs, list) or any(not isinstance(p, dict) or not isinstance(p.get('p'), str) or not isinstance(p.get('n'), str) or p['p'] == p['n'] for p in pairs):
        raise OwnershipError('unknown pair policy')
    return board, reference, a, b, {
        'board_sha256': sha, 'reference_board_sha256': a['sha256'],
        'reference_facts_sha256': common.sha256(a_path), 'candidate_facts_sha256': common.sha256(b_path),
        'attempt_sha256': common.sha256(run / 'attempt.json'), 'grade_sha256': _digest(grade),
        'tool_identity_sha256': _digest(tool), 'rules_files': rules, 'records': records,
        'scope': attempt['scope']['nets'], 'pairs': pairs}


def emit(run_dir, attempt, result):
    """Called only by the owned run path after grading; no validity changes."""
    run = Path(run_dir).resolve()
    _, reference, a, b, binding = _bound_inputs(run, attempt, result)
    binding['baseline'] = _baseline_binding(run, attempt, reference, retain=True)
    doc = {'schema': SCHEMA, 'stage': 'final_native_candidate',
           'coverage': 'current_run_only_input_fixed', 'replay_authority': False,
           'binding': binding, **_classification(a, b, binding['scope'])}
    common.write_json(run / MANIFEST, doc)
    return {'status': 'bound', 'record': MANIFEST, 'sha256': common.sha256(run / MANIFEST),
            'coverage': doc['coverage'], 'replay_authority': False}


def _baseline_binding(run, attempt, reference, retain=False):
    """Retain/reuse the existing qualified baseline generation, including refill."""
    from . import baselinecache
    baseline = attempt.get('baseline')
    if not isinstance(baseline, dict) or not isinstance(baseline.get('report'), str) or not isinstance(baseline.get('identity'), dict):
        raise OwnershipError('missing qualified baseline generation')
    identity = baseline['identity']
    if identity.get('input') != attempt.get('input_fingerprint') or identity.get('board') != attempt['board']:
        raise OwnershipError('baseline input identity mismatch')
    generation = Path(baseline['report']).parent.name
    if not generation.startswith('generation-'):
        raise OwnershipError('unsupported baseline generation name')
    relative = 'validate/custody-baseline/' + generation
    stored = _path(run, relative)
    source = run / baseline['report']
    expected = _json(run / 'validate/reference-terminal-components.json')
    names = (*baselinecache.FILES, 'generation.json')
    if retain and not stored.exists():
        qualified = baselinecache.terminal_facts(source, identity, reference)
        if qualified is None or qualified != expected:
            raise OwnershipError('reference partition differs from qualified baseline generation')
        stored.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='.baseline-', dir=stored.parent))
        try:
            for name in names:
                p = source.parent / name
                if p.is_symlink() or not p.is_file():
                    raise OwnershipError('linked or missing baseline artifact')
                shutil.copyfile(p, stage / name)
            stage.rename(stored)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    qualified = baselinecache.terminal_facts(stored / 'baseline-drc.json', identity, reference)
    if qualified is None or qualified != expected:
        raise OwnershipError('reference partition differs from retained qualified baseline')
    return {'report': relative + '/baseline-drc.json',
            'files': {relative + '/' + name: common.sha256(_path(run, relative + '/' + name)) for name in names}}


def _eligible_grade(run, result, binding):
    from . import campaign, native_components, provenance
    required = {'validate/' + name for name in (
        'candidate-drc.json', 'candidate-refill.json', 'preservation.json',
        'new-findings.json', 'reference-terminal-components.json', 'candidate-terminal-components.json',
        'pour-integrity.json')}
    if not required <= binding['records'].keys():
        raise OwnershipError('missing required native-grade records')
    grade = {'exit_code': result.get('exit_code'), 'routing': result.get('routing', {}),
             'validation': campaign.validation_evidence(result['validation']),
             'result': str(run / 'result.json'), 'candidate': str(run / 'candidate'), 'against': str(run)}
    qualified = campaign.eligibility({'grade': grade})
    if qualified['status'] != 'eligible':
        raise OwnershipError('ineligible native grade: ' + ','.join(qualified['reasons']))
    checks = _json(run / 'validate/preservation.json')
    if not isinstance(checks, list) or any(not isinstance(c, dict) or not isinstance(c.get('check'), str) or c.get('status') != 'PASS' for c in checks):
        raise OwnershipError('invalid preservation record')
    check_map = {c['check']: c['status'] for c in checks}
    if len(check_map) != len(checks) or check_map != result['validation']['preservation']:
        raise OwnershipError('preservation record disagrees with grade')
    findings = _json(run / 'validate/new-findings.json')
    if not isinstance(findings, dict) or findings.get('violations') != [] or findings.get('schematic_parity') != []:
        raise OwnershipError('new native findings are not empty')
    for name in ('candidate-drc.json', 'candidate-refill.json'):
        drc = _json(run / 'validate' / name)
        if not isinstance(drc, dict) or any(not isinstance(drc.get(k), list) for k in ('violations', 'unconnected_items', 'schematic_parity')):
            raise OwnershipError('malformed native DRC record')
    reference = _json(run / 'validate/reference-terminal-components.json')
    candidate = _json(run / 'validate/candidate-terminal-components.json')
    if (not native_components.compatible(reference, candidate)
            or candidate.get('identity', {}).get('board_sha256') != binding['board_sha256']
            or candidate['identity'].get('rules') != binding['rules_files']
            or reference['identity'].get('rules') != binding['rules_files']):
        raise OwnershipError('missing or stale complete native terminal coverage')
    if provenance.net_completion(reference, candidate)['status'] != 'PASS':
        raise OwnershipError('native input terminal partition was not preserved')


def prepare_disposable(run_dir, selected_nets, destination, timeout_s):
    """Remove complete generated units on a new scratch directory, never adopt it."""
    run = Path(run_dir).resolve()
    dest = Path(destination).absolute()
    if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise OwnershipError('invalid timeout')
    if '..' in dest.parts or dest.exists() or dest.is_symlink() or any(p.is_symlink() for p in dest.parents) or dest.is_relative_to(run):
        raise OwnershipError('destination must be new and outside the incumbent run')
    if not isinstance(selected_nets, list) or not selected_nets or any(not isinstance(n, str) or not n for n in selected_nets) or len(set(selected_nets)) != len(selected_nets):
        raise OwnershipError('select complete named net units')
    try:
        attempt, result = _json(run / 'attempt.json'), _json(run / 'result.json')
        board, reference, a, b, binding = _bound_inputs(run, attempt, result)
        binding['baseline'] = _baseline_binding(run, attempt, reference)
        if result['validation']['status'] != 'PASS':
            raise OwnershipError('native candidate grade is not PASS')
        _eligible_grade(run, result, binding)
        doc = _json(_path(run, MANIFEST))
        rec = result.get('copper_ownership')
        checksum = common.sha256(run / MANIFEST)
        if not isinstance(rec, dict) or rec.get('status') != 'bound' or rec.get('record') != MANIFEST or rec.get('sha256') != checksum or result['evidence']['records'].get(MANIFEST) != checksum:
            raise OwnershipError('missing result/evidence custody binding')
        expected = {'schema': SCHEMA, 'stage': 'final_native_candidate',
                    'coverage': 'current_run_only_input_fixed', 'replay_authority': False,
                    'binding': binding, **_classification(a, b, binding['scope'])}
        if doc != expected:
            raise OwnershipError('custody record disagrees with bound native input/output')
        selected = set(selected_nets)
        for pair in binding['pairs']:
            members = {pair['p'], pair['n']}
            if selected & members and not members <= selected:
                raise OwnershipError('pair split selection')
        uuids = []
        for net in sorted(selected):
            unit = doc['nets'].get(net)
            if unit is None or not unit['eligible']:
                raise OwnershipError('ineligible net: ' + net + ' ' + str((unit or {}).get('reasons')))
            uuids.extend(unit['generated_uuids'])
    except (KeyError, TypeError, AttributeError) as e:
        raise OwnershipError('malformed custody context') from e
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.copper-ownership-', dir=dest.parent))
    deadline = time.monotonic() + timeout_s
    try:
        rule_bytes = {}
        for source in (board, board.with_suffix('.kicad_pro'), board.with_suffix('.kicad_dru')):
            if source.exists():
                shutil.copyfile(source, staging / source.name)
                if source != board:
                    rule_bytes[source.name] = (staging / source.name).read_bytes()
        request = {'board_sha256': binding['board_sha256'], 'facts': b,
                   'selected_nets': sorted(selected), 'removed_uuids': sorted(uuids)}
        common.write_json(staging / 'request.json', request)
        command = [str(common.PCBNEW_PYTHON), str(Path(__file__).parent / 'kicad/remove_owned.py'),
                   str(staging / board.name), str(staging / 'request.json'), str(staging / 'native-receipt.json')]
        try:
            proc = subprocess.run(command, cwd=staging, capture_output=True, text=True,
                                  timeout=timeout_s, env=common.tool_env())
        except subprocess.TimeoutExpired as e:
            raise OwnershipError('native disposable removal timed out') from e
        if proc.returncode:
            raise OwnershipError('native disposable removal failed: ' + (proc.stderr or proc.stdout)[-1000:])
        receipt = _json(staging / 'native-receipt.json')
        if receipt.get('status') != 'prepared' or receipt.get('removed_uuids') != sorted(uuids):
            raise OwnershipError('invalid native removal receipt')
        # BOARD.Save may materialize project defaults as a side effect. Keep
        # the bound project bytes, then independently reread under those rules.
        rules_restored = _rules(staging / board.name) != binding['rules_files']
        for name, data in rule_bytes.items():
            (staging / name).write_bytes(data)
        for suffix in ('.kicad_pro', '.kicad_dru'):
            path = (staging / board.name).with_suffix(suffix)
            if path.name not in rule_bytes and path.exists():
                path.unlink()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OwnershipError('native disposable removal timed out')
        verify = staging / 'rules-restored-facts.json'
        try:
            checked = subprocess.run([str(common.PCBNEW_PYTHON), str(Path(__file__).parent / 'kicad/facts.py'),
                                      str(staging / board.name), str(verify)], cwd=staging,
                                     capture_output=True, text=True, timeout=remaining, env=common.tool_env())
        except subprocess.TimeoutExpired as e:
            raise OwnershipError('native survivor verification timed out') from e
        if checked.returncode or _json(verify) != _json(staging / 'survivor-facts.json'):
            raise OwnershipError('survivor facts differ under bound project rules')
        if common.sha256(staging / board.name) != receipt.get('output_board_sha256') or _rules(staging / board.name) != binding['rules_files']:
            raise OwnershipError('prepared output or rules mismatch')
        # Detect incumbent/rules/evidence drift during the operation too.
        _, _, _, _, after_binding = _bound_inputs(run, _json(run / 'attempt.json'), _json(run / 'result.json'))
        after_binding['baseline'] = _baseline_binding(run, attempt, reference)
        if binding != after_binding or common.sha256(run / MANIFEST) != checksum:
            raise OwnershipError('incumbent evidence changed during preparation')
        receipt.update(custody_sha256=checksum, selected_nets=sorted(selected),
                       authority='owned_run_bound_custody', replay_authority=False,
                       native_grade='not_run_on_prepared_board',
                       scratch_project_writeback_discarded=rules_restored,
                       inherited_fill='stale_after_copper_removal',
                       requires_before_promotion=['native_refill', 'connectivity_regeneration', 'native_grade'])
        common.write_json(staging / 'receipt.json', receipt)
        # mkdir is exclusive: a racing existing/symlink destination is never replaced.
        dest.mkdir()
        publication_inode = dest.stat().st_ino
        try:
            # Receipt is the completion marker and is always published last.
            for p in sorted(staging.iterdir(), key=lambda p: p.name == 'receipt.json'):
                shutil.move(str(p), dest / p.name)
        except BaseException:
            if dest.is_dir() and not dest.is_symlink() and dest.stat().st_ino == publication_inode:
                shutil.rmtree(dest)
            raise
        return receipt
    finally:
        shutil.rmtree(staging)
