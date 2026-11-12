#!/usr/bin/env python3
"""Router lab: route real boards and list what the router got wrong.

    lab.py add NAME BOARD.kicad_pcb [--note TEXT]
    lab.py run TAG [NAME ...] [--engine CHECKOUT] [--budget S] [--jobs N] [-- kicad-route run options]
    lab.py run TAG [NAME ...] --stock KRT_CLONE [--budget S] [--jobs N] [-- route.py options]
    lab.py show TAG [TAG ...] [--best]
    lab.py issues TAG [NAME ...]
    lab.py classes TAG [TAG ...]
    lab.py opens TAG [TAG ...] [--board NAME]
    lab.py why TAG [TAG ...] [--board NAME] [--list]
    lab.py passes TAG [NAME ...]
    lab.py diff TAG_A TAG_B

A corpus entry is a designer's board with its own rule files: the board as designed
(the yardstick), and the same board with its tracks and vias removed (the input).
`run` routes every input with the engine of one checkout through `kicad-route run`,
the way a caller does, and records each thing that went wrong as one issue: a
connection left open, a DRC finding the input did not have, a grading check that
failed, a process failure. `diff` puts two runs side by side, issue by issue.

`--stock` routes with an unmodified KiCadRoutingTools clone instead (its own
`route.py`, the options after `--`), and scores its board the same way: the
connections left open and the DRC errors the input did not have, under the
board's own rule files. That is the opponent: what upstream delivers today.
It gets the same wall time as our backend, the budget plus the grace before a
kill. It has no stop request, so a run that needs longer delivers no board.

The corpus and the runs live under $KICAD_ROUTE_LAB (default
~/.local/share/kicad-route-lab) and are never committed: the boards are other
people's designs.
"""
import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

LAB = Path(os.environ.get('KICAD_ROUTE_LAB') or Path.home() / '.local/share/kicad-route-lab')
HERE = Path(__file__).resolve().parents[1]
RULE_SUFFIXES = ('.kicad_pro', '.kicad_dru')


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=1, sort_keys=True) + '\n')


def read_json(path):
    return json.loads(Path(path).read_text())


def drc(board, out):
    """Native DRC of a board beside its own rule files -> (report or None, error text)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(['kicad-cli', 'pcb', 'drc', '--format', 'json', '--severity-all', '--refill-zones',
                           '--output', str(out), str(board)], capture_output=True, text=True)
    if not out.is_file():
        return None, (proc.stdout + proc.stderr)[-1500:]
    return read_json(out), ''


def tally(report):
    errors, warnings = {}, {}
    for row in report.get('violations', []):
        bucket = errors if row.get('severity') == 'error' else warnings
        bucket[row['type']] = bucket.get(row['type'], 0) + 1
    return {'unconnected': len(report.get('unconnected_items', [])), 'errors': sum(errors.values()),
            'errors_by_type': dict(sorted(errors.items())), 'warnings': sum(warnings.values())}


def copper(board_path):
    """Vias, track segments and track length of a board file (arcs by their chord)."""
    text = Path(board_path).read_text(errors='replace')
    length, segments, by_layer = 0.0, 0, {}
    for kind in ('segment', 'arc'):
        for block in re.findall(r'\n\t\(' + kind + r'\b(.*?)\n\t\)', text, re.S):
            start = re.search(r'\(start ([-\d.]+) ([-\d.]+)\)', block)
            end = re.search(r'\(end ([-\d.]+) ([-\d.]+)\)', block)
            layer = re.search(r'\(layer "([^"]+)"\)', block)
            if start and end:
                segments += 1
                mm = math.dist(tuple(map(float, start.groups())), tuple(map(float, end.groups())))
                length += mm
                if layer:
                    by_layer[layer.group(1)] = by_layer.get(layer.group(1), 0.0) + mm
    return {'vias': len(re.findall(r'\n\t\(via\b', text)), 'segments': segments, 'track_mm': round(length, 1),
            'track_mm_by_layer': {k: round(v, 1) for k, v in sorted(by_layer.items())}}


PLANE_SHARE = 0.02


def layer_issues(entry, routed_board):
    """Track on an inner layer the designer kept for planes.

    The designer's own board is the yardstick, as it is for DRC: an inner layer that carries
    under 2 % of their track length is one they kept for planes, and a router that puts more
    than that share of its own tracks there has cut up a plane the designer left whole. Found
    on a four-layer board with two ground planes (designer: 1 and 2 mm of 7744 mm on them;
    the router: 1212 and 1298 mm of 5039 mm), which no DRC rule and no grading check reports."""
    reference = entry['_dir'] / 'reference' / (entry['name'] + '.kicad_pcb')
    if not reference.is_file() or not Path(routed_board).is_file():
        return []
    designed, ours = copper(reference), copper(routed_board)
    if not designed['track_mm'] or not ours['track_mm']:
        return []
    issues = []
    for layer in sorted(set(designed['track_mm_by_layer']) | set(ours['track_mm_by_layer'])):
        if layer in ('F.Cu', 'B.Cu'):
            continue
        theirs, mine = designed['track_mm_by_layer'].get(layer, 0.0), ours['track_mm_by_layer'].get(layer, 0.0)
        if theirs <= PLANE_SHARE * designed['track_mm'] and mine > PLANE_SHARE * ours['track_mm']:
            issues.append({'kind': 'quality', 'type': 'plane_layer_tracks',
                           'detail': f"{mine:.0f} mm of track on {layer}, {100 * mine / ours['track_mm']:.0f} % of the "
                                     f"routed length; the designer has {theirs:.0f} mm there and kept the layer for planes"})
    return issues


# ---------------------------------------------------------------- corpus

def entries(names=()):
    found = [read_json(p) | {'_dir': p.parent} for p in sorted((LAB / 'corpus').glob('*/entry.json'))]
    unknown = sorted(set(names) - {e['name'] for e in found})
    if unknown:
        sys.exit(f'not in the corpus: {", ".join(unknown)}')
    return [e for e in found if not names or e['name'] in names]


def add(args):
    import pcbnew
    source = Path(args.board).resolve()
    root = LAB / 'corpus' / args.name
    if root.exists():
        sys.exit(f'{root} exists; remove it to add the board again')
    board = pcbnew.LoadBoard(str(source))
    if board is None:
        sys.exit(f'{source}: KiCad could not load it')
    entry = {'name': args.name, 'source': str(source), 'source_sha256': sha256(source), 'note': args.note or '',
             'layers': board.GetCopperLayerCount(), 'footprints': len(board.GetFootprints()),
             'pads': sum(len(f.Pads()) for f in board.GetFootprints()),
             'rules': [s for s in RULE_SUFFIXES if source.with_suffix(s).is_file()]}
    file_name = args.name + '.kicad_pcb'
    for part, strip in (('reference', False), ('input', True)):
        target = root / part / file_name
        target.parent.mkdir(parents=True)
        board = pcbnew.LoadBoard(str(source))
        if strip:        # tracks, vias and the teardrop zones that belong to them; pours stay
            for item in list(board.GetTracks()) + [z for z in board.Zones() if z.IsTeardropArea()]:
                board.RemoveNative(item)
        pcbnew.SaveBoard(str(target), board)
        for suffix in entry['rules']:
            shutil.copyfile(source.with_suffix(suffix), target.with_suffix(suffix))
        report, error = drc(target, root / part / 'drc.json')
        if report is None:
            shutil.rmtree(root)
            sys.exit(f'{args.name}: native DRC of the {part} board failed: {error}')
        entry['designed' if part == 'reference' else 'unrouted'] = tally(report) | copper(target)
    write_json(root / 'entry.json', entry)
    d, u = entry['designed'], entry['unrouted']
    print(f"{args.name}: {entry['layers']} layers, {entry['footprints']} parts, {u['unconnected']} connections to route; "
          f"as designed {d['unconnected']} open / {d['errors']} errors {d['errors_by_type']}, {d['vias']} vias, {d['track_mm']} mm; "
          f"rules {entry['rules'] or 'KiCad defaults'}")


# ---------------------------------------------------------------- run

def git(checkout, *argv):
    return subprocess.run(['git', '-C', str(checkout), *argv], capture_output=True, text=True).stdout


def engine_identity(checkout):
    dirty = git(checkout, 'status', '--porcelain', '--untracked-files=no')
    version = checkout / 'src/kicad_route/router/VERSION'
    if not version.is_file():
        version = checkout / 'VERSION'          # an upstream clone
    built = sorted(checkout.glob('.venv-*/lib/python*/site-packages/grid_router*.so'))
    return {'checkout': str(checkout), 'commit': git(checkout, 'rev-parse', 'HEAD').strip(),
            'dirty_files': len(dirty.splitlines()),
            'diff_sha256': hashlib.sha256(git(checkout, 'diff', 'HEAD').encode()).hexdigest() if dirty else None,
            'engine_version': version.read_text().strip() if version.is_file() else None,
            'built_extension_sha256': sha256(built[0]) if built else None}


def frozen(checkout):
    """A copy of the checkout's commit, which the run routes with instead of the checkout.

    A run takes minutes to hours, and kicad-route binds what it grades to the hashes of its own
    source files. An edit in the checkout while a run is going made the reference evidence of
    that run stale: three boards showed a failed completion check the router had no part in.
    So a run is of one commit: the committed files are copied out and the checkout is free to
    change. Uncommitted changes to tracked files are refused, they would not be in the copy."""
    dirty = git(checkout, 'status', '--porcelain', '--untracked-files=no')
    if dirty:
        sys.exit(f'{checkout} has uncommitted changes; a run is of one commit, so commit them first:\n{dirty}')
    commit = git(checkout, 'rev-parse', 'HEAD').strip()
    if not commit:
        sys.exit(f'{checkout} is not a git checkout')
    snaps = LAB / 'snap'
    snaps.mkdir(parents=True, exist_ok=True)
    for old in snaps.glob('frozen-*'):                       # copies no run of the last week used
        if time.time() - old.stat().st_mtime > 7 * 86400:
            shutil.rmtree(old, ignore_errors=True)
    dest = snaps / f'frozen-{commit[:12]}'
    if not dest.is_dir():
        tmp = snaps / f'.frozen-{commit[:12]}-{os.getpid()}'
        tmp.mkdir()
        archive = subprocess.Popen(['git', '-C', str(checkout), 'archive', '--format=tar', commit], stdout=subprocess.PIPE)
        subprocess.run(['tar', '-x', '-C', str(tmp)], stdin=archive.stdout, check=True)
        if archive.wait():
            sys.exit(f'git archive of {commit} failed')
        try:
            tmp.rename(dest)
        except OSError:                                      # another run of this commit got there first
            shutil.rmtree(tmp, ignore_errors=True)
    # The built engine environment is not in git: the copy uses the checkout's. A checkout installed
    # with `kicad-route update` routes with the environment of the router generation its state
    # manifest names; a .venv-krt left beside it by an older install has no engine in it (twelve
    # boards failed at start on "numpy: not installed" when the copy linked that one).
    venvs = {v.name: v for v in checkout.glob('.venv-*') if v.is_dir()}
    try:
        generation = json.loads((checkout / 'manifest.json').read_text())['krt']['generation']
    except (OSError, ValueError, KeyError, TypeError):
        generation = None
    if generation and (checkout / generation / 'venv').is_dir():
        venvs['.venv-krt'] = checkout / generation / 'venv'
    for name, venv in venvs.items():
        link = dest / name
        if not link.exists():
            try:
                link.symlink_to(venv.resolve(), target_is_directory=True)
            except FileExistsError:
                pass
    # Nor is the other backend: `kicad-route update` downloads Freerouting beside the source.
    jar_home = checkout / 'vendor' / 'freerouting'
    if jar_home.exists() and not (dest / 'vendor' / 'freerouting').exists():
        (dest / 'vendor').mkdir(exist_ok=True)
        try:
            (dest / 'vendor' / 'freerouting').symlink_to(jar_home.resolve(), target_is_directory=True)
        except FileExistsError:
            pass
    os.utime(dest)
    return dest


def describe(item):
    pos = item.get('pos') or {}
    return {'what': item.get('description'), 'x': pos.get('x'), 'y': pos.get('y')}


def collect(entry, run_root, record):
    """The issues of one routed board, each one line a person can act on."""
    issues = []
    result = record.get('result')
    if result is None:
        return [{'kind': 'process', 'type': 'no_result', 'detail': record.get('error', '')[-400:]}]
    run_dir = None
    for candidate in sorted((run_root / 'kr').glob('runs/*/*/*/result.json')):
        run_dir = candidate.parent
    process = result.get('process') or {}
    if process.get('status') != 'PASS':
        issues.append({'kind': 'process', 'type': 'backend_failed', 'detail': '; '.join(process.get('reasons') or [])[:400]})
    for reason in process.get('reasons') or []:
        if process.get('status') == 'PASS':
            issues.append({'kind': 'process', 'type': 'reported', 'detail': reason[:300]})
    validation = result.get('validation') or {}
    if run_dir is not None and (run_dir / 'validate/candidate-drc.json').is_file():
        report = read_json(run_dir / 'validate/candidate-drc.json')
        for row in report.get('unconnected_items', []):
            items = [describe(i) for i in row.get('items', [])]
            issues.append({'kind': 'open', 'type': 'unconnected', 'items': items})
    elif (result.get('routing') or {}).get('unconnected_total'):
        issues.append({'kind': 'open', 'type': 'unconnected', 'count': result['routing']['unconnected_total'], 'items': []})
    if run_dir is not None and (run_dir / 'validate/new-findings.json').is_file():
        findings = read_json(run_dir / 'validate/new-findings.json')
        for row in findings.get('violations', []):
            issues.append({'kind': 'drc', 'type': row.get('type'), 'severity': row.get('severity'),
                           'detail': row.get('description'), 'items': [describe(i) for i in row.get('items', [])]})
        inherited = findings.get('inherited_from_input') or []
        if inherited:
            # the footprint's own net-less copper on a pad: no route to that pad avoids it (reading-results.md)
            issues.append({'kind': 'advisory', 'type': 'inherited_from_input', 'count': len(inherited),
                           'detail': f"{len(inherited)} finding(s) against net-less footprint copper the input already "
                                     'joins to the pad: ' + '; '.join(sorted({i.get('description', '') for row in inherited
                                                                              for i in row.get('items', [])
                                                                              if '<no net>' in i.get('description', '')})[:3])})
    reasons = validation.get('reasons') or []
    for check, status in sorted((validation.get('preservation') or {}).items()):
        if status != 'PASS':
            text = next((r for r in reasons if check in r), '')
            issues.append({'kind': 'grading', 'type': check, 'detail': text[:400]})
    if run_dir is not None:
        for board in sorted((run_dir / 'candidate').glob('*/' + entry['name'] + '.kicad_pcb')):
            issues += layer_issues(entry, board)
    below = ((result.get('routing') or {}).get('stats') or {}).get('below_class') or {}
    if below.get('tracks') or below.get('vias'):
        # reported, never counted against a board: KiCad's DRC enforces the board's minimums, not class sizes
        issues.append({'kind': 'advisory', 'type': 'below_class',
                       'detail': f"{below.get('tracks', 0)} new tracks and {below.get('vias', 0)} new vias below their "
                                 f"net class, on {len(below.get('nets') or [])} nets: " + ', '.join((below.get('nets') or [])[:8])})
    return issues


def finding_key(row):
    return (row.get('type'), tuple(sorted(str(i.get('description')) for i in row.get('items', []))))


WALL_FACTOR = 6     # kicad-route's own cap: with a processor-time budget a run ends at 6 x budget on the wall clock


def run_timed(argv, cwd, limit, log_path, clock='cpu'):
    """Run to the limit (processor seconds of the process, or wall seconds), then kill the whole
    process group. -> (exit code or None, processor seconds)."""
    tick = os.sysconf('SC_CLK_TCK')
    cpu = 0.0
    with open(log_path, 'wb') as out:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        start = time.monotonic()
        while True:
            try:    # user and system time of the process and of the children it waited for
                fields = Path(f'/proc/{proc.pid}/stat').read_text().rsplit(')', 1)[1].split()
                cpu = sum(int(v) for v in fields[11:15]) / tick
            except (OSError, ValueError, IndexError):
                pass
            try:
                proc.wait(timeout=1)
                return proc.returncode, round(cpu, 1)
            except subprocess.TimeoutExpired:
                wall = time.monotonic() - start
                if (cpu >= limit or wall >= WALL_FACTOR * limit) if clock == 'cpu' else wall >= limit:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
                    return None, round(cpu, 1)


def stock_one(entry, tag, clone, budget, extra, clock='cpu'):
    """Unmodified upstream route.py on the input; issues from native DRC against the input's own."""
    run_root = LAB / 'runs' / tag / entry['name']
    if run_root.exists():
        shutil.rmtree(run_root)
    file_name = entry['name'] + '.kicad_pcb'
    staged = run_root / 'in' / file_name
    staged.parent.mkdir(parents=True)
    for path in (entry['_dir'] / 'input').glob(entry['name'] + '.kicad_*'):
        shutil.copyfile(path, staged.parent / path.name)
    routed = run_root / 'routed' / file_name
    routed.parent.mkdir()
    argv = [str(clone / '.venv-bench/bin/python'), '-X', 'utf8', 'py_router/route.py', str(staged), str(routed), *extra]
    # The same time kicad-route gives its backend, on the same clock: the budget, then the grace before
    # it kills (cli.run_budgeted). Stock route.py takes no stop request, so a run that needs longer has no board.
    limit = budget + max(30, budget // 5)
    start = time.monotonic()
    issues = []
    exit_code, cpu = run_timed(argv, clone, limit, run_root / 'route.log', clock)
    if exit_code is None:
        issues.append({'kind': 'process', 'type': 'no_board_in_time',
                       'detail': f"not finished after {limit} {'processor' if clock == 'cpu' else 'wall'} seconds"})
    seconds = round(time.monotonic() - start, 1)
    stats = {}
    if routed.is_file():
        for suffix in entry['rules']:       # the board's own rules, whatever the tool wrote beside its output
            shutil.copyfile(entry['_dir'] / 'input' / (entry['name'] + suffix), routed.with_suffix(suffix))
        report, error = drc(routed, run_root / 'routed' / 'drc.json')
        if report is None:
            issues.append({'kind': 'process', 'type': 'drc_failed', 'detail': error[-300:]})
        else:
            before = {}
            for row in read_json(entry['_dir'] / 'input' / 'drc.json').get('violations', []):
                before[finding_key(row)] = before.get(finding_key(row), 0) + 1
            for row in report.get('unconnected_items', []):
                issues.append({'kind': 'open', 'type': 'unconnected', 'items': [describe(i) for i in row.get('items', [])]})
            for row in report.get('violations', []):
                if before.get(finding_key(row), 0) > 0:
                    before[finding_key(row)] -= 1
                    continue
                issues.append({'kind': 'drc', 'type': row.get('type'), 'severity': row.get('severity'),
                               'detail': row.get('description'), 'items': [describe(i) for i in row.get('items', [])]})
            c = copper(routed)
            stats = {'new_vias': c['vias'], 'new_track_length_mm': c['track_mm']}
            issues += layer_issues(entry, routed)
    elif not issues:
        issues.append({'kind': 'process', 'type': 'no_board', 'detail': f'route.py exit {exit_code}'})
    stats['engine_cpu_s'] = cpu
    return finish(entry, run_root, issues, stats, seconds, exit_code, True, {'argv': argv})


def finish(entry, run_root, issues, stats, seconds, exit_code, have_result, record):
    opens = [i for i in issues if i['kind'] == 'open']
    errors = [i for i in issues if i['kind'] == 'drc' and i.get('severity') == 'error']
    summary = {'name': entry['name'], 'seconds': seconds, 'exit_code': exit_code,
               'open': sum(i.get('count', 1) for i in opens), 'errors': len(errors),
               'errors_by_type': {t: sum(1 for i in errors if i['type'] == t) for t in sorted({i['type'] for i in errors})},
               'warnings': sum(1 for i in issues if i['kind'] == 'drc' and i.get('severity') != 'error'),
               'grading': sorted(i['type'] for i in issues if i['kind'] == 'grading'),
               'quality': sorted({i['type'] for i in issues if i['kind'] == 'quality'}),
               'process': sorted({i['type'] for i in issues if i['kind'] == 'process' and i['type'] != 'reported'}),
               'vias': stats.get('new_vias'), 'track_mm': stats.get('new_track_length_mm'),
               'engine_cpu_s': stats.get('engine_cpu_s'),
               # the step of the engine's procedure the budget stopped the run's own pass in, if it did
               'cut_off_in': stats.get('cut_off_in'),
               # dangling pieces removed, ringed-off pour pieces stitched and left (kicad-route runs only)
               'after_engine': stats.get('after_engine'),
               # budgets are wall time: a loaded host routes less in the same budget
               'host_load': round(os.getloadavg()[0], 1), 'host_cpus': os.cpu_count(),
               'clean': have_result and not opens and not errors
                        and not any(i['kind'] in ('grading', 'quality', 'process') and i['type'] != 'reported'
                                    for i in issues)}
    write_json(run_root / 'issues.json', issues)
    write_json(run_root / 'summary.json', summary)
    write_json(run_root / 'record.json', record)
    return summary


def route_one(entry, tag, engine, budget, extra, clock='cpu'):
    if (engine / 'py_router/route.py').is_file():
        return stock_one(entry, tag, engine, budget, extra, clock)
    run_root = LAB / 'runs' / tag / entry['name']
    if run_root.exists():
        shutil.rmtree(run_root)
    run_root.mkdir(parents=True)
    argv = [sys.executable, '-B', str(engine / 'bin/kicad-route'), '--out', str(run_root / 'kr'), '--json', 'run',
            '--backend', 'builtin', '--dir', str(entry['_dir'] / 'input'), '--board', entry['name'] + '.kicad_pcb',
            '--name', entry['name'], '--label', 'lab', '--protect', 'existing', '--budget', str(budget),
            '--budget-clock', clock, *extra]
    env = dict(os.environ, KICAD_ROUTE_LEDGER='off', PYTHONDONTWRITEBYTECODE='1')
    env.pop('KICAD_ROUTE_STATE', None)
    start = time.monotonic()
    record = {'argv': argv}
    try:
        # the run's own limits end it; this only catches a hang (grading may take minutes on a big board)
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=run_root,
                              timeout=budget * (WALL_FACTOR + 1 if clock == 'cpu' else 2) + 1800)
        record['exit_code'] = proc.returncode
        (run_root / 'stderr.log').write_text(proc.stderr)
        try:
            record['result'] = json.loads(proc.stdout)
        except ValueError:
            record['error'] = (proc.stdout[-600:] + proc.stderr[-1200:])
    except subprocess.TimeoutExpired:
        record['error'] = 'timeout'
    seconds = round(time.monotonic() - start, 1)
    issues = collect(entry, run_root, record)
    result = record.get('result') or {}
    stats = dict((result.get('routing') or {}).get('stats') or {})
    # the engine's own processor time: what the wall-time budget bought on this host
    engine = [c.get('cpu_s') for c in (result.get('process') or {}).get('commands') or []
              if any(str(a).endswith(('route.py', 'route_diff.py')) for a in c.get('argv') or [])]
    stats['engine_cpu_s'] = round(sum(c for c in engine if c is not None), 1) if any(c is not None for c in engine) else None
    run_dir = run_dir_of(tag, entry['name'])
    own = [row for row in engine_passes(run_dir, result) if row['log'] == 'krt.log'] if run_dir and result else []
    stats['cut_off_in'] = own[0]['stopped_in'] if own else None
    # what the wrapper cleared up, or could not, after the engine: each is something the engine left
    swept, stitched = [(result.get('process') or {}).get(key) or {} for key in ('dangling_removed', 'stitched')]
    stats['after_engine'] = {'stubs_removed': (swept.get('removed_tracks') or 0) + (swept.get('removed_vias') or 0),
                             'pieces_stitched': len(stitched.get('vias') or []),
                             'pieces_left': len(stitched.get('left') or [])}
    return finish(entry, run_root, issues, stats, seconds, record.get('exit_code'), bool(result),
                  {k: v for k, v in record.items() if k != 'result'})


def run(args):
    engine = Path(args.stock or args.engine).resolve()
    chosen = entries(args.names)
    root = LAB / 'runs' / args.tag
    root.mkdir(parents=True, exist_ok=True)
    identity = engine_identity(engine)
    if not args.stock:
        engine = frozen(engine)
        identity['routed_from'] = str(engine)
    write_json(root / 'run.json', {'tag': args.tag, 'engine': identity, 'budget_s': args.budget, 'clock': args.clock,
                                   'extra': args.extra, 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(route_one, e, args.tag, engine, args.budget, args.extra, args.clock): e for e in chosen}
        for future in concurrent.futures.as_completed(futures):
            s = future.result()
            print(f"  {s['name']}: {'clean' if s['clean'] else 'ISSUES'} open {s['open']} errors {s['errors']} "
                  f"{s['errors_by_type'] or ''} {s['grading'] or ''} {s['process'] or ''} {s['seconds']} s", flush=True)
    show(argparse.Namespace(tags=[args.tag]))


# ---------------------------------------------------------------- reading

def summaries(tag):
    return {p.parent.name: read_json(p) for p in sorted((LAB / 'runs' / tag).glob('*/summary.json'))}


def after_engine(s):
    """What the wrapper cleared up after the engine, for a table cell: only what is not zero."""
    a = (s or {}).get('after_engine') or {}
    parts = [f"{a[key]} {text}" for key, text in (('stubs_removed', 'dangling removed'), ('pieces_stitched', 'pour stitched'),
                                                   ('pieces_left', 'pour left cut off')) if a.get(key)]
    return ', ' + ', '.join(parts) if parts else ''


def cell(s):
    if s is None:
        return 'not run'
    if s.get('process'):
        return 'FAILED ' + ','.join(s['process'])
    extra = ''.join(f' +{c}' for c in s['grading'] + s.get('quality', []))
    text = f"{s['open']} / {s['errors']}{extra}"
    return f'**{text}**' if s['clean'] else text


def rank(s):
    """Lower is better: a board first, then clean, new errors, open connections, failed checks, vias,
    copper. An error is worse than an open connection: the one breaks a rule, the other is reported."""
    if s is None or s.get('process'):
        return (2,)
    return (0 if s['clean'] else 1, s['errors'], s['open'], len(s['grading']) + len(s.get('quality', [])),
            s['vias'] or 0, s['track_mm'] or 0)


def designer(entry):
    """The designer's own board graded as a routed candidate is: the connections it leaves open
    and the DRC errors the unrouted input does not have. None for an entry that came unrouted.

    The table used to put the designer's total error count beside a router's NEW errors. A
    designer's board carries its courtyard and library findings with it, and four of ten
    designers' boards of the corpus break a routing rule of their own (clearance, shorts,
    starved thermals, drill sizes): a board its designer did not get clean under its rules is
    not the same evidence against a router as one the designer did."""
    d = entry.get('designed') or {}
    reference, unrouted = entry['_dir'] / 'reference/drc.json', entry['_dir'] / 'input/drc.json'
    if not d.get('track_mm') or not reference.is_file() or not unrouted.is_file():
        return None
    before = {}
    for row in read_json(unrouted).get('violations', []):
        before[finding_key(row)] = before.get(finding_key(row), 0) + 1
    new = []
    report = read_json(reference)
    for row in report.get('violations', []):
        if before.get(finding_key(row), 0) > 0:
            before[finding_key(row)] -= 1
        elif row.get('severity') == 'error':
            new.append(row.get('type'))
    return {'open': len(report.get('unconnected_items', [])), 'errors': len(new),
            'by_type': {t: new.count(t) for t in sorted(set(new))},
            'clean': not new and not report.get('unconnected_items')}


def show(args):
    corpus = {e['name']: e for e in entries()}
    runs = {tag: summaries(tag) for tag in args.tags}
    if getattr(args, 'best', False) and len(args.tags) > 1:
        # what a portfolio of these runs would deliver: per board, the best of them
        best = {}
        for name in {n for r in runs.values() for n in r}:
            tag = min((t for t in args.tags if name in runs[t]), key=lambda t: rank(runs[t][name]))
            best[name] = dict(runs[tag][name], best_of=tag)
        runs['best'] = best
        args = argparse.Namespace(tags=[*args.tags, 'best'])
    names = sorted({n for r in runs.values() for n in r} | set(corpus if len(args.tags) > 1 else ()))
    print('| Board | Layers | To route | As designed | ' + ' | '.join(args.tags) + ' |')
    print('|---|---|---|---|' + '---|' * len(args.tags))
    for name in [n for n in names if any(n in r for r in runs.values())]:
        e = corpus.get(name) or {}
        d = e.get('designed') or {}
        own = designer(e) if e else None
        if own is None:
            designed = 'not routed' if d else ''
        else:
            kinds = ', '.join(f'{t} {n}' for t, n in own['by_type'].items())
            designed = (f"**{own['open']} / {own['errors']}**" if own['clean'] else f"{own['open']} / {own['errors']}"
                        + (f' ({kinds})' if kinds else '')) + f", {d.get('vias')} vias, {d.get('track_mm')} mm"
        cells = []
        for tag in args.tags:
            s = runs[tag].get(name)
            cells.append(cell(s) + (f", {s['vias']} vias, {s['track_mm']} mm, {s['seconds']:.0f} s" if s and not s.get('process') else '')
                         + (f" (engine {s['engine_cpu_s']:.0f} CPU s)" if s and s.get('engine_cpu_s') is not None else '')
                         + (f", cut off in {s['cut_off_in']}" if s and s.get('cut_off_in') else '')
                         + after_engine(s)
                         + (f" ({s['best_of']})" if s and s.get('best_of') else ''))
        print(f"| {name} | {e.get('layers', '')} | {(e.get('unrouted') or {}).get('unconnected', '')} | {designed} | " + ' | '.join(cells) + ' |')
    for tag in args.tags:
        done = runs[tag]
        if tag == 'best' and not (LAB / 'runs' / 'best').is_dir():
            print(f"\nbest: {sum(1 for s in done.values() if s['clean'])} of {len(done)} clean (the best run of each board)")
            continue
        info = read_json(LAB / 'runs' / tag / 'run.json') if (LAB / 'runs' / tag / 'run.json').is_file() else {}
        engine = info.get('engine') or {}
        print(f"\n{tag}: {sum(1 for s in done.values() if s['clean'])} of {len(done)} clean; engine {engine.get('engine_version')} "
              f"{(engine.get('commit') or '')[:8]}{' +' + str(engine['dirty_files']) + ' dirty' if engine.get('dirty_files') else ''}; "
              f"budget {info.get('budget_s')} {'processor' if info.get('clock') == 'cpu' else 'wall'} s "
              f"{' '.join(info.get('extra') or [])}" + host_load(done))
    graded = {name: designer(e) for name, e in corpus.items()}
    routed = {name for name, own in graded.items() if own is not None}
    theirs = {name for name in routed if graded[name]['clean']}
    if routed:
        print(f"\nThe designers' own boards, graded the same way: {len(theirs)} of {len(routed)} clean "
              f"({len(corpus) - len(routed)} of the corpus came unrouted).")
        for tag in args.tags:
            done = runs[tag]
            ours = {name for name, summary in done.items() if summary['clean']}
            print(f"  {tag}: clean on {len(ours & theirs)} of the {len(theirs & set(done))} its designer got clean, "
                  f"on {len(ours & (routed - theirs))} of the {len((routed - theirs) & set(done))} its designer did not, "
                  f"on {len(ours - routed)} of the {len(set(done) - routed)} that came unrouted")
    print('\nCells are open connections / new DRC errors (+ failed grading checks); the designer\'s column counts the errors '
          'the unrouted board does not have. Bold is clean. "cut off in": the budget stopped the routing in that step of '
          'the engine, so the count is the budget\'s, not the router\'s.')


def host_load(done):
    loads = [s['host_load'] for s in done.values() if s.get('host_load') is not None]
    if not loads:
        return ''
    cpus = next(s['host_cpus'] for s in done.values() if s.get('host_load') is not None)
    return f"; host load {min(loads)} to {max(loads)} on {cpus} CPUs"


def key(issue):
    """Stable across runs: what kind of thing, on what, never where exactly."""
    whats = tuple(sorted(str(i.get('what')) for i in issue.get('items', [])))
    return (issue['kind'], issue['type'], whats or (issue.get('detail', '')[:80],))


def line(issue):
    items = '; '.join(f"{i.get('what')} @({i.get('x')}, {i.get('y')})" for i in issue.get('items', []))
    detail = issue.get('detail') or ''
    return f"{issue['kind']:8} {issue['type']:28} {detail[:110]}{' | ' if detail and items else ''}{items[:260]}"


def issues(args):
    for path in sorted((LAB / 'runs' / args.tag).glob('*/issues.json')):
        if args.names and path.parent.name not in args.names:
            continue
        rows = [i for i in read_json(path) if not (i['kind'] == 'drc' and i.get('severity') != 'error' and not args.warnings)]
        print(f"== {path.parent.name}: {len(rows)} issue(s)   {path.parent}")
        for issue in rows:
            print('  ' + line(issue))


def classes(args):
    """Every kind of issue of a run, with the boards that have it: where to look first."""
    for tag in args.tags:
        count, boards = {}, {}
        for path in sorted((LAB / 'runs' / tag).glob('*/issues.json')):
            for issue in read_json(path):
                if issue['kind'] == 'drc' and issue.get('severity') != 'error':
                    continue
                if issue['kind'] == 'process' and issue['type'] == 'reported':
                    continue
                k = (issue['kind'], issue['type'])
                count[k] = count.get(k, 0) + issue.get('count', 1)
                boards.setdefault(k, {}).setdefault(path.parent.name, 0)
                boards[k][path.parent.name] += issue.get('count', 1)
        print(f'== {tag}')
        for k in sorted(count, key=lambda k: (-len(boards[k]), -count[k])):
            where = ', '.join(f'{b} {n}' for b, n in sorted(boards[k].items(), key=lambda x: -x[1]))
            print(f"  {k[0]:8} {k[1]:30} {count[k]:4} on {len(boards[k])} board(s): {where}")


def run_dir_of(tag, name):
    """The kicad-route run directory of one lab cell, or None (a stock cell has none)."""
    found = sorted((LAB / 'runs' / tag / name / 'kr').glob('runs/*/*/*/result.json'))
    return found[-1].parent if found else None


def net_roles(run_dir):
    """{net: 'plane' | 'pour'} for the nets that own a zone: on an inner layer, or on outer layers only."""
    facts = read_json(run_dir / 'facts/reference-facts.json') if run_dir else None
    if not facts:
        return {}
    layers = [layer['name'] if isinstance(layer, dict) else layer for layer in facts.get('copper_layers') or []]
    inner, roles = set(layers[1:-1]), {}
    for zone in facts.get('zones') or []:
        if zone.get('net'):
            plane = bool(inner & set(zone.get('layers') or []))
            roles[zone['net']] = 'plane' if plane or roles.get(zone['net']) == 'plane' else 'pour'
    return roles


def opens(args):
    """What is left open, by the kind of net: the question that found the boxed-in plane pads.

    A count of open connections says how far a board is from done, not why. On a four-layer
    board 50 of 56 were pads of the two plane nets, which no cell of `show` could say."""
    for tag in args.tags:
        print(f'== {tag}')
        for path in sorted((LAB / 'runs' / tag).glob('*/issues.json')):
            name = path.parent.name
            if args.names and name not in args.names:
                continue
            rows = [i for i in read_json(path) if i['kind'] == 'open']
            if not rows:
                continue
            roles = net_roles(run_dir_of(tag, name))
            by_net, ends, by_role = {}, {}, {'plane': 0, 'pour': 0, 'other': 0}
            for issue in rows:
                items = issue.get('items') or []
                found = re.search(r'\[(.*)\]', items[0]['what']) if items and items[0].get('what') else None
                net = found.group(1) if found else '?'
                count = issue.get('count', 1)
                by_net[net] = by_net.get(net, 0) + count
                by_role[roles.get(net, 'other')] += count
                kind = ' to '.join(sorted((i.get('what') or '?').split(' [')[0].split(' ')[0].lower() for i in items)) or '?'
                ends[kind] = ends.get(kind, 0) + count
            total = sum(by_net.values())
            top = ', '.join(f'{n} {c}' for n, c in sorted(by_net.items(), key=lambda x: (-x[1], x[0]))[:8])
            kinds = ', '.join(f'{k} {c}' for k, c in sorted(ends.items(), key=lambda x: -x[1]))
            print(f"  {name}: {total} open on {len(by_net)} nets; {by_role['plane']} on plane nets, "
                  f"{by_role['pour']} on pour nets, {by_role['other']} on others")
            print(f'      nets: {top}')
            print(f'      ends: {kinds}')


PAD_END = re.compile(r'[Pp]ad (\S+) \[.*\] of (\S+)')
WHY_ORDER = ('routing', 'passable', 'no throat', 'placement', 'not measured')
WHY_TEXT = {'routing': 'caged by copper this run laid', 'passable': 'still reachable on the routed board',
            'no throat': "with nothing in the way inside the check's view (no answer)",
            'placement': 'caged by the placement', 'not measured': 'not measured'}
# The check looks at 8 mm round the pad. A widest path of that order met no foreign copper at all:
# the pad's own net was reached at once (a through-hole pad reaches it on its other layer), which
# says nothing about the connection that is open.
WHY_VIEW_UM = 15000


_ROUTING_PYTHON = {}


def routing_python(engine):
    """The interpreter of a checkout's routing environment, as kicad-route itself finds it: an
    installed checkout keeps the environment under .router-generations, a lab copy under .venv-krt."""
    if engine not in _ROUTING_PYTHON:
        found = subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, "src"); '
                                'from kicad_route.cli import route_python; print(route_python())'],
                               cwd=engine, capture_output=True, text=True)
        path = found.stdout.strip().splitlines()[-1] if found.returncode == 0 and found.stdout.strip() else ''
        _ROUTING_PYTHON[engine] = path if path and Path(path).exists() else str(engine / '.venv-krt/bin/python')
    return _ROUTING_PYTHON[engine]


def reach(engine, board, end, run_dir=None):
    """The engine's reachability check from one end of an open connection: the check's JSON, or
    {'error': ...}. `end` is an issue item: its description and position. A board with text on
    copper is read with the bounds the run measured for that text (work/copper-text.json), as the
    engine was when it routed."""
    what = end.get('what') or ''
    pad, net = PAD_END.search(what), re.search(r'\[(.*)\]', what)
    argv = [routing_python(engine), str(engine / 'src/kicad_route/router/py_tools/check_reachability.py'),
            str(board), '--json']
    if pad:
        argv += ['--pad', f'{pad.group(2)}.{pad.group(1)}']
    elif net and end.get('x') is not None:
        argv += ['--net', net.group(1), '--at', f"{end['x']},{end['y']}"]
    else:
        return {'error': 'no pad and no position to start from'}
    env = dict(os.environ)
    if run_dir is not None and (run_dir / 'work/copper-text.json').is_file():
        env['KICAD_ROUTE_COPPER_TEXT'] = str((run_dir / 'work/copper-text.json').resolve())
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=1800, env=env)
        return json.loads(done.stdout[done.stdout.index('{'):done.stdout.rindex('}') + 1])
    except subprocess.TimeoutExpired:
        return {'error': 'timeout'}
    except ValueError:
        said = [line for line in (done.stderr or done.stdout).splitlines() if line.strip()]
        return {'error': said[-1].strip()[:300] if said else f'exit {done.returncode}'}


def cut_off_pads(run_dir, board):
    """One pad of every group of pads that the routed board leaves off the rest of its net:
    [{'what': 'pad N [NET] of REF', 'net', 'pads' (in the group)}], from the run's own record of
    which pads are joined (validate/candidate-terminal-components.json). KiCad's list of open
    connections names a zone or a track as often as a pad; the check needs a pad to start from."""
    record = read_json(run_dir / 'validate/candidate-terminal-components.json') if run_dir else None
    if not record or not board.is_file():
        return None
    refs = {}
    for chunk in board.read_text(errors='replace').split('\n\t(footprint ')[1:]:
        uuid, ref = re.search(r'\(uuid "([^"]+)"\)', chunk), re.search(r'\(property "Reference" "([^"]*)"', chunk)
        if uuid and ref:
            refs[uuid.group(1)] = ref.group(1)
    ends = []
    for net, groups in sorted((record.get('nets') or {}).items()):
        for group in sorted(groups, key=len, reverse=True)[1:]:
            named = sorted((refs.get(record['inventory'][pad]['footprint'], '?'), str(record['inventory'][pad]['number']))
                           for pad in group if pad in record.get('inventory', {}))
            if named and named[0][0] != '?':
                ends.append({'what': f'pad {named[0][1]} [{net}] of {named[0][0]}', 'net': net, 'pads': len(group)})
    return ends


def why_class(data):
    """'placement': no track of the net's width reaches the rest of the net from here, whatever is
    routed (the board's, not the router's). 'routing': one does on the board as placed, and the
    copper this run laid closes it off. 'passable': one still does on the routed board."""
    if data.get('error') or data.get('measured') is False:
        return 'not measured'
    if data.get('verdict') == 'CAGED':
        return 'placement'
    routed = data.get('routed_copper') or {}
    if routed.get('cages_the_pad') or routed.get('verdict') == 'CAGED':
        return 'routing'
    margin = routed.get('margin_um')
    return 'no throat' if margin is None or margin >= WHY_VIEW_UM else 'passable'


def why(args):
    """Whose doing each open connection is: the board's or the router's.

    An open connection at a pin of a fine-pitch part reads the same whether the placement left
    no way out or the router closed the way with its own copper. The engine carries a check that
    measures it (py_tools/check_reachability.py: the widest track that reaches the rest of the
    net from a pad, on the placement alone and with the routing in place). This runs it from one
    end of every open connection of a cell, a pad where there is one, and keeps the answers in
    the cell's why.json. Some ten seconds a connection."""
    engine = Path(args.engine).resolve()
    for tag in args.tags:
        print(f'== {tag}')
        for path in sorted((LAB / 'runs' / tag).glob('*/issues.json')):
            name, run_dir = path.parent.name, run_dir_of(tag, path.parent.name)
            if args.names and name not in args.names:
                continue
            rows = [i for i in read_json(path) if i['kind'] == 'open' and i.get('items')]
            board = run_dir / 'candidate' / 'input' / f'{name}.kicad_pcb' if run_dir else None
            if not rows or board is None or not board.is_file():
                continue
            saved = path.parent / 'why.json'
            answers = read_json(saved) if saved.is_file() and not args.again else None
            if answers is None:
                ends = cut_off_pads(run_dir, board)
                if ends is None:             # no record of the joined pads: the ends KiCad names
                    ends = [next((i for i in row['items'] if PAD_END.search(i.get('what') or '')), row['items'][0])
                            for row in rows]
                ends = ends[:args.limit]
                with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
                    measured = list(pool.map(lambda end: reach(engine, board, end, run_dir), ends))
                answers = []
                for end, data in zip(ends, measured):
                    routed = data.get('routed_copper') or {}
                    near = routed.get('near') or data.get('near') or []
                    answers.append({'from': end.get('what'), 'net': data.get('net'), 'class': why_class(data),
                                    'margin_um': data.get('margin_um'), 'routed_margin_um': routed.get('margin_um'),
                                    'throat': routed.get('throat') or data.get('throat'),
                                    'between': sorted({b.get('pad') or b.get('net') or b.get('kind') for b in near} - {None}),
                                    'error': data.get('error')})
                write_json(saved, answers)
            for a in answers:                # answers kept before the class 'no throat' existed
                if a['class'] == 'passable' and (a.get('routed_margin_um') is None or a['routed_margin_um'] >= WHY_VIEW_UM):
                    a['class'] = 'no throat'
            count = {k: sum(1 for a in answers if a['class'] == k) for k in WHY_ORDER}
            more = f' ({len(answers)} measured)' if len(answers) != len(rows) else ''
            print(f"  {name}: {len(rows)} open{more}: " + ', '.join(f'{count[k]} {WHY_TEXT[k]}' for k in WHY_ORDER if count[k]))
            closers = {}
            for a in answers:
                if a['class'] == 'routing':
                    for other in a['between']:
                        closers[other] = closers.get(other, 0) + 1
            if closers:
                print('      closed off by: ' + ', '.join(f'{n} {c}' for n, c in sorted(closers.items(), key=lambda x: (-x[1], x[0]))[:10]))
            slack = sorted(a['routed_margin_um'] for a in answers if a['class'] == 'passable')
            if slack:
                # how much wider than the net's track the way still is: under the routing grid's step, the grid decides
                tight = sum(1 for m in slack if m < 100)
                print(f"      still reachable with {slack[0]:.0f} to {slack[-1]:.0f} um to spare (median {slack[len(slack) // 2]:.0f}); "
                      f"{tight} of {len(slack)} with under 100 um, less than the engine's 0.1 mm grid step")
            if args.list:
                for a in sorted(answers, key=lambda a: (WHY_ORDER.index(a['class']), a['from'] or '')):
                    spot = a.get('throat') or {}
                    at = f" at {spot.get('x')}, {spot.get('y')} on {spot.get('layer')}" if spot else ''
                    detail = a['error'] or (f"between {', '.join(a['between'])}{at}" if a['between'] else '')
                    if a['class'] == 'passable':
                        detail = f"{a['routed_margin_um']:.0f} um to spare; " + detail
                    print(f"      {a['class']:12} {(a['from'] or '?')[:58]:58} {detail}")


# What the engine prints when it enters a part of its procedure, in the order of the procedure.
ENGINE_STEPS = (('main loop', r'Routing \d+ single-ended net'), ('taps', r'Multi-point Phase 3: Routing'),
                ('net rescue', r'Per-net fine-parameter rescue'), ('plane repair', r'Plane finalize \(#562\)'),
                ('KiCad check', r'JSON_ORACLE:'), ('reconciliation', r'Final reconciliation: retrying'),
                ('regrade', r'JSON_REGRADE'))


def engine_passes(run_dir, result):
    """One row per engine pass of a kicad-route run: its log, nets, budget, processor seconds,
    whether the budget stopped it, the step it was stopped in and the steps it reached."""
    rows = []
    for command in (result.get('process') or {}).get('commands') or []:
        argv = [str(a) for a in command.get('argv') or []]
        if not any(a.endswith(('route.py', 'route_diff.py')) for a in argv):
            continue
        nets = argv[argv.index('--nets') + 1:] if '--nets' in argv else []
        nets = nets[:next((k for k, a in enumerate(nets) if a.startswith('--')), len(nets))]
        log_path = run_dir / 'work' / Path(command.get('log') or '').name
        text = log_path.read_text(errors='replace') if log_path.is_file() else ''
        stop = text.find('STOP requested')
        before = [step for step, mark in ENGINE_STEPS
                  if stop >= 0 and (found := re.search(mark, text)) and found.start() < stop]
        stopped = bool(command.get('stop_requested'))
        rows.append({'log': log_path.name, 'nets': nets, 'budget_s': command.get('budget_s'), 'cpu_s': command.get('cpu_s'),
                     'stopped': stopped, 'killed': bool(command.get('killed')),
                     'stopped_in': None if not stopped else (before[-1] if before else 'its start') if stop >= 0 else 'its finish',
                     'reached': [step for step, mark in ENGINE_STEPS if re.search(mark, text)]})
    return rows


def passes(args):
    """Each engine pass of a cell: its nets, its budget, the processor time it used, whether the
    budget stopped it, and how far through the engine's procedure it got.

    A run that is stopped in its taps has not rescued a net or repaired a plane, and a few
    seconds of budget move that point: two runs of one board whose own pass was stopped 4 s
    apart came out at 83 and 123 open connections. Without this view the difference looks
    like the router's."""
    for path in sorted((LAB / 'runs' / args.tag).glob('*/summary.json')):
        name = path.parent.name
        if args.names and name not in args.names:
            continue
        run_dir = run_dir_of(args.tag, name)
        result = read_json(run_dir / 'result.json') if run_dir else None
        if not result:
            continue
        print(f'== {name}: {cell(read_json(path))}')
        for row in engine_passes(run_dir, result):
            nets = row['nets']
            scope = 'all nets' if nets == ['*'] else f"{len(nets)} net(s): {', '.join(nets[:5])}{', ...' if len(nets) > 5 else ''}"
            print(f"  {row['log']:15} {scope}; budget {row['budget_s']} s, used "
                  f"{row['cpu_s'] if row['cpu_s'] is None else round(row['cpu_s'])} processor s"
                  + (f"; stopped by the budget in {row['stopped_in']}" if row['stopped'] else '; ended on its own')
                  + (', then killed' if row['killed'] else ''))
            print(f"      reached: {', '.join(row['reached']) or 'nothing'}")


def diff(args):
    a, b = (LAB / 'runs' / t for t in (args.tag_a, args.tag_b))
    names = sorted({p.parent.name for p in a.glob('*/issues.json')} & {p.parent.name for p in b.glob('*/issues.json')})
    for name in names:
        ia, ib = ({key(i): i for i in read_json(r / name / 'issues.json')
                   if i['kind'] != 'drc' or i.get('severity') == 'error'} for r in (a, b))
        gone, new = [ia[k] for k in ia if k not in ib], [ib[k] for k in ib if k not in ia]
        sa, sb = read_json(a / name / 'summary.json'), read_json(b / name / 'summary.json')
        print(f"== {name}: {cell(sa)} -> {cell(sb)}   ({len(gone)} gone, {len(new)} new, {len(ia) - len(gone)} unchanged)")
        for issue in gone:
            print('  gone ' + line(issue))
        for issue in new:
            print('  NEW  ' + line(issue))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('add')
    p.add_argument('name')
    p.add_argument('board')
    p.add_argument('--note')
    p.set_defaults(fn=add)
    p = sub.add_parser('run')
    p.add_argument('tag')
    p.add_argument('names', nargs='*')
    p.add_argument('--engine', default=str(HERE))
    p.add_argument('--stock')
    p.add_argument('--budget', type=int, default=300)
    p.add_argument('--clock', choices=['cpu', 'wall'], default='cpu',
                   help='what the budget counts: the engine\'s processor seconds (default) or wall seconds')
    p.add_argument('--jobs', type=int, default=3)
    p.set_defaults(fn=run, extra=[])
    p = sub.add_parser('show')
    p.add_argument('tags', nargs='+')
    p.add_argument('--best', action='store_true', help='add a column with the best run of each board')
    p.set_defaults(fn=show)
    p = sub.add_parser('issues')
    p.add_argument('tag')
    p.add_argument('names', nargs='*')
    p.add_argument('--warnings', action='store_true')
    p.set_defaults(fn=issues)
    p = sub.add_parser('classes')
    p.add_argument('tags', nargs='+')
    p.set_defaults(fn=classes)
    p = sub.add_parser('opens', help='what is left open, by kind of net (plane, pour, other), by net and by kind of end')
    p.add_argument('tags', nargs='+')
    p.add_argument('--board', dest='names', action='append', help='only this board (repeatable)')
    p.set_defaults(fn=opens)
    p = sub.add_parser('why', help="whose doing each open connection is: caged by the placement, caged by this run's "
                                   'copper, or still reachable on the routed board')
    p.add_argument('tags', nargs='+')
    p.add_argument('--board', dest='names', action='append', default=[])
    p.add_argument('--engine', default=str(HERE), help='checkout whose engine carries the check (default: this one)')
    p.add_argument('--jobs', type=int, default=4)
    p.add_argument('--limit', type=int, default=40, help='connections measured per board (default 40)')
    p.add_argument('--list', action='store_true', help='one line per connection')
    p.add_argument('--again', action='store_true', help='measure again instead of reading why.json')
    p.set_defaults(fn=why)
    p = sub.add_parser('passes', help='each engine pass of a cell: nets, budget, time used, where the budget stopped it')
    p.add_argument('tag')
    p.add_argument('names', nargs='*')
    p.set_defaults(fn=passes)
    p = sub.add_parser('diff')
    p.add_argument('tag_a')
    p.add_argument('tag_b')
    p.set_defaults(fn=diff)
    argv = sys.argv[1:]
    extra = []
    if '--' in argv:
        split = argv.index('--')
        argv, extra = argv[:split], argv[split + 1:]
    args = parser.parse_args(argv)
    if args.command == 'run':
        args.extra = extra
    args.fn(args)


if __name__ == '__main__':
    main()
