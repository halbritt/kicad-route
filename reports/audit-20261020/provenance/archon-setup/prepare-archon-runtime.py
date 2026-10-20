#!/usr/bin/env python3
"""Prepare one fresh, target-local reference/candidate runtime; no routing.

Draft only until root verifies Archon capabilities and applicable instructions.
Run with the target's verified Python3.12.3. Never copy a preexisting virtualenv.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback

COMMITS = {'reference': 'f0c09ac3b8d39a6674a734d3caaa9d7e3ae9633d',
           'candidate': 'f0f73da86835ec583f2a2c9128e6893fa59c03b7'}
SOURCES = {'reference': 'c2ac657d5617e6e0441ea42d1db52ce95d6fa23a512652e835c5064db440a9f7',
           'candidate': 'c6c09054f6845442867ed99141ecbcfdc78f39f622cda06ff401e8230bd9bab1'}
PACKAGES = ['numpy==2.5.4', 'pillow==13.0.0', 'scipy==1.18.1', 'shapely==2.2.0']
LOCK = 'f14e7b0c06ae682e63f1b452862a72417cb69ee629f51ac49bac242a24d94203'
COMPILE = ('import compileall,sys,sysconfig; '
           "paths=dict.fromkeys(sysconfig.get_paths()[key] for key in ('purelib','platlib')); "
           'sys.exit(not all(compileall.compile_dir(p,quiet=1,workers=1) for p in paths))')
sys.dont_write_bytecode = True


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def caches(root):
    return {str(path.relative_to(root)): sha(path) for path in sorted(root.rglob('*.pyc')) if path.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--side', required=True, choices=COMMITS)
    for key in ['checkout', 'state', 'output']:
        parser.add_argument('--'+key, required=True, type=Path)
    args = parser.parse_args()
    checkout, state, output = (getattr(args, key).expanduser().resolve() for key in ['checkout', 'state', 'output'])
    if state.exists() or output.exists() or state == output or state.is_relative_to(checkout) or output.is_relative_to(state):
        parser.error('STATE and output must be new independent paths; STATE must be outside checkout')
    if sys.version.split()[0] != '3.12.3':
        parser.error('preparation requires verified system Python3.12.3 with native pcbnew')
    head = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(checkout), 'status', '--porcelain'], text=True).strip()
    if head != COMMITS[args.side] or dirty:
        parser.error('source checkout is dirty or differs from exact frozen side commit')
    output.mkdir(parents=True)
    receipt = {'schema': 'kicad-route/archon-runtime-preparation/1', 'status': 'RUNNING', 'side': args.side,
               'checkout': str(checkout), 'state': str(state), 'source_commit': head, 'steps': [], 'gates': [],
               'started_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
               'helper_sha256': sha(__file__), 'host': os.uname()._asdict() if hasattr(os.uname(), '_asdict') else list(os.uname())}

    def save():
        (output/'preparation.json').write_text(json.dumps(receipt, indent=2, default=str)+'\n')

    def gate(name, condition, observed):
        receipt['gates'].append({'name': name, 'passed': bool(condition), 'observed': observed})
        save()
        if not condition:
            raise RuntimeError('preparation gate failed: '+name)

    def checked(argv, name, cwd=checkout, env=None):
        with (output/name).open('w') as stream:
            result = subprocess.run([str(value) for value in argv], cwd=cwd, env=env,
                                    stdout=stream, stderr=subprocess.STDOUT)
        record = {'argv': [str(value) for value in argv], 'exit_code': result.returncode, 'log': str(output/name)}
        receipt['steps'].append(record)
        save()
        if result.returncode:
            raise RuntimeError('preparation command failed: '+name)
        return (output/name).read_text()

    save()
    try:
        for key in ['VIRTUAL_ENV', 'PYTHONPATH', 'PYTHONPYCACHEPREFIX', 'KICAD_ROUTE_OUT', 'KICAD_ROUTE_PCBNEW_PYTHON']:
            os.environ.pop(key, None)
        os.environ.update(KICAD_ROUTE_STATE=str(state), KICAD_ROUTE_KRT_PYTHON=sys.executable,
                          PYTHONDONTWRITEBYTECODE='1', KICAD_ROUTE_LEDGER='off')
        constraints = output/'constraints.txt'
        constraints.write_text('\n'.join(PACKAGES)+'\n')
        os.environ['UV_CONSTRAINT'] = str(constraints)
        sys.path.insert(0, str(checkout/'src'))
        from kicad_route import cli, common, engine, statemanifest
        common.set_out(output/'out')
        gate('selected source checkout and STATE', Path(cli.__file__).resolve() == checkout/'src/kicad_route/cli.py' and
             common.STATE == state, {'module': cli.__file__, 'STATE': str(common.STATE)})
        gate('owned routing source', engine.source_sha256() == SOURCES[args.side], engine.source_sha256())
        gate('Rust lock', sha(engine.ROOT/'rust_router/Cargo.lock') == LOCK, sha(engine.ROOT/'rust_router/Cargo.lock'))
        native = checked(['kicad-cli', '--version'], 'native-cli.log').strip()
        gate('native KiCad10.0.7', native == '10.0.7', native)
        pcb = checked([common.PCBNEW_PYTHON, '-B', '-c',
                       'import json,pcbnew,sys; print(json.dumps({"python":sys.version.split()[0],"pcbnew":pcbnew.GetBuildVersion()}))'],
                      'native-pcbnew.log')
        pcb = json.loads(pcb.strip().splitlines()[-1])
        gate('Python3.12.3 and pcbnew10.0.7', pcb['python'] == '3.12.3' and pcb['pcbnew'].split('-')[0] == '10.0.7', pcb)
        for name, argv in [('rustc', ['rustc', '-vV']), ('cargo', ['cargo', '--version']), ('uv', ['uv', '--version']),
                           ('gcc', ['gcc', '--version']), ('glibc', ['ldd', '--version'])]:
            checked(argv, name+'.log')
        # Each checkout uses its actual lifecycle. Candidate activation remains atomic.
        cli.update_krt(receipt['steps'])
        save()
        python = cli.route_python()
        if args.side == 'reference':
            checked([python, '-I', '-B', '-c', COMPILE], 'reference-dependency-bytecode.log', cwd=state)
        checked([python, 'py_router/startup_checks.py'], 'bounded-startup.log', cwd=cli.KRT_DIR, env=common.tool_env())
        checked([python, 'py_router/route.py', '--help'], 'bounded-route-help.log', cwd=cli.KRT_DIR, env=common.tool_env())
        if args.side == 'reference':
            build_log = next(row['log'] for row in receipt['steps'] if Path(row['log']).name == 'krt-build-router.log')
            _, manifest = statemanifest.refresh(written_by='archon-preparation', build_router_log=build_log)
        else:
            manifest = statemanifest.read()
        gate('installation steps passed', bool(receipt['steps']) and all(step.get('exit_code') == 0 for step in receipt['steps']),
             [step.get('exit_code') for step in receipt['steps']])
        gate('manifest drift', statemanifest.verify(manifest) == [], statemanifest.verify(manifest))
        packages = statemanifest.venv_packages(cli.KRT_VENV)
        gate('exact complete numerical package list', packages == PACKAGES, packages)
        before_manifest = (state/'manifest.json').read_bytes()
        before_cache = caches(cli.KRT_VENV)
        gate('prepared dependency bytecode exists', bool(before_cache) and any('/numpy/' in '/'+path for path in before_cache) and
             any('/scipy/' in '/'+path for path in before_cache), {'count': len(before_cache)})
        checked([python, '-I', '-B', '-c', 'import numpy,scipy.optimize,scipy.spatial,shapely.geometry,PIL,grid_router; print("native+lazy numerical imports PASS")'],
                'bounded-lazy-import.log', cwd=state)
        gate('bounded imports preserve immutable caches and manifest', caches(cli.KRT_VENV) == before_cache and
             (state/'manifest.json').read_bytes() == before_manifest, {'pyc_count': len(before_cache)})
        gate('owned source has no incidental bytecode', not list(cli.KRT_DIR.rglob('*.pyc')), str(cli.KRT_DIR))
        (output/'manifest.json').write_bytes(before_manifest)
        receipt.update(runtime={'source': engine.identity(), 'venv': str(cli.KRT_VENV), 'python': str(python),
                                'generation': str(getattr(engine, 'GENERATION', None)),
                                'module': str(statemanifest.extension_module(cli.KRT_VENV)),
                                'module_sha256': sha(statemanifest.extension_module(cli.KRT_VENV)),
                                'packages': packages, 'packages_sha256': statemanifest.packages_sha256(packages),
                                'manifest_sha256': hashlib.sha256(before_manifest).hexdigest(),
                                'dependency_pyc_count': len(before_cache)}, status='PASS')
        return 0
    except BaseException as error:
        receipt.update(status='FAIL', error=f'{type(error).__name__}: {error}', traceback=traceback.format_exc())
        return 1
    finally:
        receipt['finished_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
        save()
        print(json.dumps({'status': receipt['status'], 'receipt': str(output/'preparation.json')}))


if __name__ == '__main__':
    raise SystemExit(main())
