"""Explicit plane-plan production, isolated from trace routing (docs/planes.md)."""
import argparse
from pathlib import Path
import shutil
import sys
import uuid

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = 'kicad_route'

from . import common, contract, pour
from .plane_plan import load_plan, zone_ids

NATIVE = Path(__file__).resolve().parent / 'kicad' / 'planes.py'
SIBLINGS = ('.kicad_pro', '.kicad_dru', '.kicad_prl')


def sandbox_command(work, argv):
    """All host paths read-only; only this attempt's work and private tmp are writable."""
    return ['bwrap', '--die-with-parent', '--unshare-all', '--ro-bind', '/', '/',
            '--tmpfs', '/tmp', '--proc', '/proc', '--dev', '/dev',
            '--ro-bind', str(work.parent), str(work.parent), '--bind', str(work), str(work),
            '--chdir', str(work), '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin',
            '--setenv', 'HOME', str(work / 'home'), '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
            '--setenv', 'KICAD_ROUTE_OUT', str(work / 'tools'), *map(str, argv)]


def bwrap_check(work):
    """Probe planes namespace support without staging a private filesystem."""
    work.mkdir(parents=True, exist_ok=True)
    argv = ['bwrap', '--die-with-parent', '--unshare-all', '--ro-bind', '/', '/',
            '--clearenv', '--', '/usr/bin/true']
    rec = common.run_cmd(argv, work, work / 'bwrap.log', timeout=10)
    ready = rec['exit_code'] == 0
    return dict(check='bwrap (planes)', status=common.PASS if ready else common.FAIL,
                required=False, detail=f'planes namespaces {"ready" if ready else "unavailable"}; see {rec["log"]}')


def _isolated_copper_errors(report, always_ids):
    """Incomplete native evidence cannot establish an isolated-copper pass."""
    if not isinstance(report, dict):
        raise ValueError('native plane DRC report is malformed')
    severities = report.get('included_severities')
    if (not isinstance(severities, list) or
            any(s not in ('error', 'warning', 'exclusion') for s in severities) or
            set(severities) != {'error', 'warning', 'exclusion'}):
        raise ValueError('native plane DRC report lacks complete severity coverage')
    violations = report.get('violations')
    if not isinstance(violations, list):
        raise ValueError('native plane DRC report is malformed')
    errors = []
    for violation in violations:
        if (not isinstance(violation, dict) or
                violation.get('severity') not in ('error', 'warning', 'exclusion') or
                not isinstance(violation.get('type'), str) or not violation['type'].strip() or
                not isinstance(violation.get('items'), list)):
            raise ValueError('native plane DRC violation metadata is malformed')
        items = violation['items']
        isolated_always = violation['type'] == 'isolated_copper' and bool(always_ids)
        if (violation['severity'] == 'error' or isolated_always) and not items:
            raise ValueError('native plane DRC violation has no item attribution')
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get('uuid'), str):
                raise ValueError('native plane DRC item attribution is malformed')
            try:
                valid = str(uuid.UUID(item['uuid'])) == item['uuid']
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('native plane DRC item attribution is malformed')
        if isolated_always and any(item['uuid'] in always_ids for item in items):
            errors.append(violation)
    return errors


def _native(command, board, plan, output):
    rec = common.run_cmd([common.PCBNEW_PYTHON, NATIVE, command, board, plan, output],
                         board.parent, output.with_suffix('.log'), timeout=180)
    if rec['exit_code'] != 0 or not output.is_file():
        raise ValueError(f'native plane {command} failed; see {rec["log"]}')
    return common.load_json(output), common.public(rec)


def _facts(board, directory):
    from . import cli
    facts, rec = cli.board_facts(board, directory / 'facts.json')
    fill, fill_rec = pour.extract(board, directory / 'pour.json')
    if facts is None or fill is None:
        raise ValueError('native board/pour facts unavailable')
    return facts, fill, [rec, fill_rec]


def _grade(board, directory):
    from . import cli
    report, rec = cli.kicad_drc(board, directory, 'planes', refill_and_save=True)
    if report is None:
        raise ValueError('native refill/DRC failed: ' + '; '.join(rec['reasons']))
    return report, rec


def _restore_siblings(reference, board):
    restored = []
    for suffix in SIBLINGS:
        source = (reference / board.name).with_suffix(suffix)
        target = board.with_suffix(suffix)
        if source.is_file():
            if not target.is_file() or common.sha256(source) != common.sha256(target):
                restored.append(target.name)
                shutil.copyfile(source, target)
        elif target.exists():
            restored.append(target.name)
            target.unlink()
    return restored


def worker(reference, plan_path, work):
    """Executed in bwrap. Missing evidence propagates as a failed process."""
    from . import cli
    plan = load_plan(plan_path)
    boards = {}
    for role in ('before', 'candidate'):
        project = work / role
        shutil.copytree(reference, project)
        boards[role] = next(project.glob('*.kicad_pcb'))
    before_drc, before_grade = _grade(boards['before'], work / 'before-drc')
    before, before_pour, before_commands = _facts(boards['before'], work / 'before-facts')
    applied, apply_command = _native('apply', boards['candidate'], plan_path, work / 'applied.json')
    # KiCad may rewrite project sidecars. Restore captured rules before grading.
    restored = set(_restore_siblings(reference, boards['candidate']))
    after_drc, after_grade = _grade(boards['candidate'], work / 'candidate-drc')
    inspected, inspect_command = _native('inspect', boards['candidate'], plan_path, work / 'inspected.json')
    after, after_pour, after_commands = _facts(boards['candidate'], work / 'candidate-facts')
    _restore_siblings(reference, boards['before'])
    restored.update(_restore_siblings(reference, boards['candidate']))
    # Reuse existing preservation checks; zone inventory and settings are checked
    # against the explicit plan above.
    checks, _ = cli.preservation(before, after, boards['before'].parent, boards['candidate'].parent,
                                 boards['candidate'].name, 'existing', sorted(before['nets']))
    checks = [c for c in checks if c['check'] != 'zones']
    unchanged = all(sorted(before[k], key=lambda v: v['uuid']) == sorted(after[k], key=lambda v: v['uuid'])
                    for k in ('tracks', 'vias'))
    checks.append(dict(check='plane_copper_inventory', status=common.PASS if unchanged else common.FAIL,
                       detail='plane-plan/1 permits no new or altered tracks/vias', differences=[]))
    integrity, delta = pour.compare(before_pour, after_pour)
    checks.append(integrity)
    # Validate attribution before the permissive delta identity helper reads it.
    _isolated_copper_errors(before_drc, set())
    always_ids = {uid for uid, spec in zip(zone_ids(plan), plan['zones']) if spec['island_removal'] == 'always'}
    isolated = _isolated_copper_errors(after_drc, always_ids)
    new, resolved = cli.drc_delta(before_drc, after_drc, 'violations')
    errors = [v for v in new if v['severity'] == 'error']
    errors += [v for v in isolated if v not in errors]
    checks.append(dict(check='native_drc', status=common.FAIL if errors else common.PASS,
                       detail='new errors and planned always-remove isolated copper', differences=errors))
    valid = all(c['status'] == common.PASS for c in checks)
    report = dict(exit_code=0 if valid else 3, process=dict(status=common.PASS),
                  validation=dict(status=common.PASS if valid else common.FAIL, checks=checks,
                                  restored_project_files=sorted(restored)),
                  planes=dict(zones_created=applied['zones_created'], zones=inspected['zones'],
                              pour_integrity=dict(check=integrity, before=before_pour, after=after_pour, delta=delta)),
                  evidence=dict(before_drc=before_grade, after_drc=after_grade,
                                new_violations=new, resolved_violations=resolved,
                                commands=[*before_commands, apply_command, inspect_command, *after_commands]),
                  candidate=str(boards['candidate']))
    common.write_json(work / 'report.json', report)


def produce(board, plan_path, run_dir):
    board, plan_path, run_dir = Path(board).resolve(), Path(plan_path).resolve(), Path(run_dir).absolute()
    try:
        load_plan(plan_path)
        if board.suffix != '.kicad_pcb' or not board.is_file() or not board.with_suffix('.kicad_pro').is_file():
            raise ValueError('a native board and same-name .kicad_pro are required')
        if run_dir.exists() or run_dir.is_symlink():
            contract.refuse('state', 'plane --run-dir must not exist')
        if board.parent == run_dir.resolve() or board.parent in run_dir.resolve().parents:
            raise ValueError('plane output must be outside the source project')
    except (OSError, ValueError) as exc:
        contract.refuse('input', str(exc))
    run_dir.mkdir(parents=True)
    reference, work = run_dir / 'reference', run_dir / 'work'
    reference.mkdir()
    work.mkdir()
    for source in [board, *(board.with_suffix(s) for s in SIBLINGS)]:
        if source.is_file():
            shutil.copyfile(source, reference / source.name)
    shutil.copyfile(plan_path, run_dir / 'plan.json')
    captured = {p.name: common.sha256(p) for p in reference.iterdir()}
    record = dict(schema=contract.schema_id('planes'), kind='planes', exit_code=1,
                  run_dir=str(run_dir), input=dict(files=captured, plan_sha256=common.sha256(run_dir / 'plan.json')),
                  process=dict(status=common.FAIL), validation=dict(status=common.NOT_RUN, restored_project_files=[]),
                  planes=dict(zones_created=None, zones=[], pour_integrity=None), candidate=None,
                  limitation='Plane production only; full post-routing DRC, parity and connectivity acceptance remain required.')
    argv = [common.PCBNEW_PYTHON, '-B', Path(__file__).resolve(), reference, run_dir / 'plan.json', work]
    execution = common.run_cmd(sandbox_command(work, argv), work, run_dir / 'sandbox.log', timeout=600)
    record['sandbox'] = common.public(execution)
    produced = common.load_json(work / 'report.json') if execution['exit_code'] == 0 else None
    if produced is not None and produced.get('exit_code') in (0, 3):
        record.update(produced)
        candidate = run_dir / 'candidate'
        shutil.copytree(work / 'candidate', candidate)
        record['candidate'] = str(candidate / board.name)
    else:
        record['process']['reason'] = 'sandbox/native production failed or report missing; see sandbox.log'
    if captured != {p.name: common.sha256(p) for p in reference.iterdir()}:
        record.update(exit_code=1, process=dict(status=common.FAIL, reason='immutable reference changed'),
                      validation=dict(status=common.NOT_RUN, restored_project_files=[]), candidate=None)
    common.write_json(run_dir / 'result.json', record)
    print(f'[planes] result: {run_dir / "result.json"}')
    return record['exit_code'], record


def add_parser(sub):
    parser = sub.add_parser('planes', help='create/refill exactly an agent-ee.plane-plan/1 (docs/planes.md)')
    parser.add_argument('--board', required=True)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--run-dir', required=True, help='new directory outside the input project')


def cmd(args):
    return produce(args.board, args.plan, args.run_dir)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('reference', type=Path)
    parser.add_argument('plan', type=Path)
    parser.add_argument('work', type=Path)
    args = parser.parse_args()
    common.set_out(args.work / 'tools')
    worker(args.reference, args.plan, args.work)
