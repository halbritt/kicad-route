"""Explicit single-ended length groups, graded on final native fromTo paths."""
import argparse
import json
import math
from pathlib import Path
import re
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kicad_route import common, netnames


def settings(args):
    groups = getattr(args, 'length_match_group', None)
    controls = {key: getattr(args, flag, None) for key, flag in (
        ('tolerance_mm', 'group_match_tolerance'),
        ('amplitude_mm', 'group_meander_amplitude'),
        ('spacing_widths', 'group_meander_spacing'))}
    if groups is None:
        if any(value is not None for value in controls.values()):
            raise ValueError('group matching controls require --length-match-group')
        return None
    if args.backend != 'builtin':
        raise ValueError('length groups require the builtin engine')
    defaults = dict(tolerance_mm=.1, amplitude_mm=1., spacing_widths=2.)
    controls = {key: defaults[key] if value is None else value for key, value in controls.items()}
    result = normalize(dict(groups=groups, **controls))
    reserved = {'--length-match-group', '--length-match-tolerance', '--meander-amplitude',
                '--meander-spacing', '--time-matching', '--time-match-tolerance'}
    if any(arg.split('=', 1)[0] in reserved for arg in args.backend_arg or []):
        raise ValueError('public length groups conflict with raw backend matching controls')
    return result


def normalize(spec):
    fields = {'groups', 'tolerance_mm', 'amplitude_mm', 'spacing_widths'}
    if not isinstance(spec, dict) or set(spec) != fields:
        raise ValueError('length matching requires groups, tolerance_mm, amplitude_mm and spacing_widths')
    groups = spec['groups']
    if not isinstance(groups, list) or not groups:
        raise ValueError('length matching requires at least one explicit group')
    for group in groups:
        if (not isinstance(group, list) or len(group) < 2
                or not all(isinstance(name, str) and name for name in group)
                or len(set(group)) != len(group)):
            raise ValueError('each length group needs at least two distinct exact net names')
    for key in fields - {'groups'}:
        value = spec[key]
        if type(value) not in (float, int) or not math.isfinite(value) or value < .000001:
            raise ValueError('length matching numeric controls must be finite and positive')
    if spec['spacing_widths'] < 1:
        raise ValueError('group meander spacing must be at least one track width')
    return {**spec, 'groups': [list(group) for group in groups]}


def paths(spec, facts, scope, pairs=()):
    """Resolve every requested name and terminal before invoking a heuristic."""
    normalize(spec)
    paired = {name for pair in pairs for name in (pair['p'], pair['n'])}
    result = []
    for group in spec['groups']:
        resolved = []
        for name in group:
            if name not in facts['nets'] or name not in scope:
                raise ValueError(f'length group member {name!r} is missing or outside routing scope')
            if name in paired:
                raise ValueError(f'length group member {name!r} is a coupled pair member; this operation is single-ended')
            if any(zone.get('net') == name for zone in facts.get('zones', [])):
                raise ValueError(f'length group member {name!r} has zone copper, outside the trace-path measurement model')
            if any(item.get('net') == name for item in facts.get('copper_graphics', [])):
                raise ValueError(f'length group member {name!r} has graphical copper, outside the trace-path measurement model')
            terminals = [(ref, number) for ref, fp in facts['footprints'].items()
                         for number, pad in fp['pads'].items() if pad['net'] == name]
            if len(terminals) != 2:
                raise ValueError(f'length group member {name!r} needs exactly two native terminals')
            if any(not re.fullmatch(r'[A-Za-z0-9_.]+', token) for pair in terminals for token in pair):
                raise ValueError(f'length group member {name!r} has a terminal name not expressible as an exact native fromTo path')
            resolved.append(dict(net=name, start='-'.join(terminals[0]), end='-'.join(terminals[1])))
        result.append(resolved)
    return result


def backend_args(spec):
    argv = []
    for group in spec['groups']:
        # Matching globs cover the whole name and have no ! exclusion syntax.
        patterns = [netnames.exact(name) for name in group]
        patterns = [pattern[1:] if name.startswith('!') else pattern for name, pattern in zip(group, patterns)]
        argv += ['--length-match-group', *patterns]
    return argv + ['--length-match-tolerance', str(spec['tolerance_mm']),
                   '--meander-amplitude', str(spec['amplitude_mm']),
                   '--meander-spacing', str(spec['spacing_widths'])]


def run(board, output, spec, api):
    request = output.with_suffix('.request.json')
    common.write_json(request, spec)
    rec = common.run_cmd([api.pcbnew_python(), '-B', Path(__file__), board, request, output],
                         board.parent, output.with_suffix('.log'), timeout=600)
    report = common.load_json(output)
    if rec['exit_code'] not in (0, 2) or not isinstance(report, dict):
        raise ValueError(f'native group matching check failed (see {rec["log"]})')
    return report


def check(board, output, spec):
    from kicad_route import cli
    spec = normalize(spec)
    board = board.resolve()
    root = output.with_suffix('.native').resolve()
    if root == board.parent or board.parent in root.parents:
        raise ValueError('native matching verification directory must be outside the input project')
    root.mkdir(parents=True)
    common.set_out(root / 'tools')
    board_sha = common.sha256(board)
    facts, info = cli.board_facts(board, root / 'facts.json')
    if facts is None:
        raise ValueError(f'native matching terminal inventory failed (see {info["log"]})')
    resolved = paths(spec, facts, set(facts['nets']))
    defects, groups = [], []
    for index, members in enumerate(resolved):
        first_defect = len(defects)
        work = root / f'group-{index}'
        work.mkdir()
        project = work / 'project'
        cli.copy_project(board.parent, project)
        copied = project / board.name
        rules = copied.with_suffix('.kicad_dru')
        original = rules.read_text() if rules.is_file() else '(version 1)\n'
        rule = f'kicad-route length group {index}'
        condition = ' || '.join(f"A.fromTo('{p['start']}', '{p['end']}')" for p in members)
        # A zero maximum deliberately emits one length measurement per path.
        # Those findings establish coverage; the native skew check uses full
        # precision and is the acceptance result. Original rules are unchanged.
        rules.write_text(original + '\n' + f'(rule "{rule}" (condition {json.dumps(condition)}) '
            f'(constraint length (max 0mm)) (constraint skew (max {spec["tolerance_mm"]}mm)))\n')
        native, info = cli.kicad_drc(copied, work, 'paths', False)
        entry = dict(paths=members, status='FAIL', measurements=[], skew_violations=[],
                     native=info, rule=rule)
        groups.append(entry)
        if native is None:
            defects.append(f'group {index}: native path check unavailable')
            continue
        if common.sha256(copied) != board_sha:
            defects.append(f'group {index}: native check changed board bytes')
            continue
        names = {p['net'] for p in members}
        observed = []
        for finding in native['violations']:
            if f"rule '{rule}'" not in finding.get('description', ''):
                continue
            if finding['type'] == 'length_out_of_range':
                identified = {name for item in finding.get('items', [])
                              for name in netnames.nets_in_description(item.get('description', ''), names)}
                entry['measurements'].append(finding)
                if len(identified) != 1:
                    defects.append(f'group {index}: ambiguous path measurement')
                observed.extend(identified)
            elif finding['type'] == 'skew_out_of_range':
                entry['skew_violations'].append(finding)
        ignored = {row.get('key') for row in native.get('ignored_checks', [])}
        if {'length_out_of_range', 'skew_out_of_range'} & ignored:
            defects.append(f'group {index}: a required native length/skew check is ignored')
        elif sorted(observed) != sorted(names):
            defects.append(f'group {index}: native path coverage differs from required members')
        elif native['unconnected_items']:
            defects.append(f'group {index}: native board has unconnected items')
        elif entry['skew_violations']:
            defects.append(f'group {index}: final native paths exceed {spec["tolerance_mm"]} mm skew')
        elif len(defects) == first_defect:
            entry['status'] = 'PASS'
    if common.sha256(board) != board_sha:
        defects.append('input board changed during native group verification')
    return dict(status='FAIL' if defects else 'PASS', defects=defects, board_sha256=board_sha,
                requirement=spec, groups=groups,
                measurement='KiCad fromTo paths; zero-length findings prove measurement coverage, native skew decides acceptance',
                limits='Explicit two-terminal single-ended groups; no delay, inter-pair or AC-composed matching claim')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('board', type=Path)
    parser.add_argument('request', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    try:
        result = check(args.board, args.output, common.load_json(args.request))
    except ValueError as exc:
        result = dict(status='FAIL', board_sha256=common.sha256(args.board), defects=[str(exc)])
    common.write_json(args.output, result)
    return 0 if result['status'] == 'PASS' else 2


if __name__ == '__main__':
    sys.exit(main())
