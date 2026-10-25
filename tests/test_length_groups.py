"""Final useful paths, not summed copper or backend completion, decide matching."""
import json
import shutil
import subprocess
import sys

import pytest

from helpers import ROOT, cli, run, tree_hash


def test_engine_whole_name_globs_and_public_literal_encoding(tmp_path):
    from kicad_route import cli as router, length_groups
    names = ['A[0]', 'A*', 'A.', '!RST', '\\!RST', 'A?', 'A0', '-P', '/x/A*', '/x/A0']
    groups = [['A[0]', 'A*'], ['!RST', '\\!RST'], ['A?', '-P'], ['/x/A*', 'A0']]
    inputs = []
    for group in groups:
        args = length_groups.backend_args(dict(groups=[group], tolerance_mm=.1, amplitude_mm=1, spacing_widths=2))
        inputs.append(dict(patterns=args[1:args.index('--length-match-tolerance')], expected=group))
    inputs += [dict(patterns=['A[0-9]'], expected=['A0']),
               dict(patterns=['/x/A*'], expected=['/x/A*', '/x/A0']),
               dict(patterns=['A?'], expected=['A*', 'A.', 'A?', 'A0']),
               dict(patterns=['a0'], expected=[])]
    request = tmp_path / 'request.json'; output = tmp_path / 'result.json'
    request.write_text(json.dumps(dict(names=names, inputs=inputs)))
    subprocess.run([str(router.route_python()), '-c', '''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from length_matching import find_nets_matching_patterns
r=json.loads(Path(sys.argv[2]).read_text())
Path(sys.argv[3]).write_text(json.dumps([find_nets_matching_patterns(r['names'],g['patterns']) for g in r['inputs']]))
''', str(router.KRT_DIR / 'py_router'), str(request), str(output)], check=True,
        capture_output=True, timeout=30)
    assert [sorted(row) for row in json.loads(output.read_text())] == [sorted(g['expected']) for g in inputs]

ARGS = ('--diff-pairs', 'auto', '--length-match-group', 'DQ0', 'DQ1', 'DQ2', 'DQ3',
        '--group-match-tolerance', '.1', '--group-meander-amplitude', '3',
        '--grid-step', '.1', '--budget', '180')


@pytest.fixture(scope='module')
def matched(tmp_path_factory):
    source = tmp_path_factory.mktemp('length-groups') / 'input'
    shutil.copytree(ROOT / 'fixtures/group_matching', source)
    before = tree_hash(source)
    done, directory, result = run('run', '--backend', 'builtin', '--dir', str(source), *ARGS)
    assert done.returncode == 0, done.stdout + done.stderr
    assert tree_hash(source) == before
    return directory, result


def test_public_group_matching_checks_every_saved_native_path(matched):
    directory, result = matched
    assert result['routing']['status'] == 'COMPLETE'
    assert result['validation']['status'] == 'PASS'
    measured = result['validation']['length_matching']
    assert measured['status'] == 'PASS' and measured['defects'] == []
    assert measured['board_sha256'] == result['validation']['candidate_board_sha256']
    group, = measured['groups']
    assert {p['net'] for p in group['paths']} == {'DQ0', 'DQ1', 'DQ2', 'DQ3'}
    assert len(group['measurements']) == 4 and group['skew_violations'] == []
    native = json.loads((directory / 'validate/candidate-drc.json').read_text())
    assert native['violations'] == [] and native['unconnected_items'] == []
    assert not list((directory / 'candidate').rglob('.kicad-config'))
    for suffix in ('.kicad_pro', '.kicad_dru'):
        assert next((directory / 'candidate').rglob('group' + suffix)).read_bytes() == (
            ROOT / 'fixtures/group_matching' / ('group' + suffix)).read_bytes()


def test_connected_loop_cannot_inflate_useful_path_length(matched, tmp_path):
    directory, _ = matched
    candidate = next((directory / 'candidate').rglob('group.kicad_pcb'))
    source = tmp_path / 'input'
    shutil.copytree(candidate.parent, source)
    board = source / candidate.name
    project = board.with_suffix('.kicad_pro').read_bytes()
    subprocess.run([sys.executable, '-c', '''import sys,pcbnew as k
b=k.LoadBoard(sys.argv[1]);tracks=list(b.GetTracks())
code=next(t.GetNetCode() for t in tracks if t.GetNetname()=='DQ1')
for t in tracks:
 if t.GetNetname()=='DQ1':b.Remove(t)
for a,z in [((40,50),(88,50)),((64,50),(64.75,50.661438)),((64.75,50.661438),(65.5,50))]:
 t=k.PCB_TRACK(b);t.SetStart(k.VECTOR2I(k.FromMM(a[0]),k.FromMM(a[1])))
 t.SetEnd(k.VECTOR2I(k.FromMM(z[0]),k.FromMM(z[1])))
 t.SetWidth(k.FromMM(.25));t.SetLayer(k.F_Cu);t.SetNetCode(code);b.Add(t)
k.SaveBoard(sys.argv[1],b)
''', str(board)], check=True, capture_output=True, timeout=30)
    board.with_suffix('.kicad_pro').write_bytes(project)
    before = tree_hash(source)
    done, rejected, result = run('run', '--backend', 'builtin', '--dir', str(source),
                                 '--protect', 'existing', *ARGS)
    assert tree_hash(source) == before
    assert done.returncode == 3, done.stdout + done.stderr
    assert result['process']['status'] == 'PASS'
    assert result['routing']['unconnected_total'] == 0
    native = json.loads((rejected / 'validate/candidate-drc.json').read_text())
    assert native['violations'] == [] and native['unconnected_items'] == []
    measured = result['validation']['length_matching']
    assert measured['status'] == 'FAIL'
    group, = measured['groups']
    assert len(group['measurements']) == 4
    assert group['status'] == 'FAIL' and len(group['skew_violations']) == 1
    assert 'actual 48.0000 mm' in group['skew_violations'][0]['description']


def test_insufficient_room_rejects_a_complete_route():
    args = list(ARGS)
    args[args.index('--group-meander-amplitude') + 1] = '.001'
    done, _, result = run('run', '--backend', 'builtin', '--dir',
                          str(ROOT / 'fixtures/group_matching'), *args)
    assert done.returncode == 3, done.stdout + done.stderr
    assert result['routing']['unconnected_total'] == 0
    assert result['validation']['length_matching']['status'] == 'FAIL'
    assert result['validation']['length_matching']['groups'][0]['skew_violations']


def test_ignored_native_skew_cannot_pass_group_verification(matched, tmp_path):
    directory, result = matched
    candidate = next((directory / 'candidate').rglob('group.kicad_pcb'))
    source = tmp_path / 'input'
    shutil.copytree(candidate.parent, source)
    project_path = source / 'group.kicad_pro'
    project = json.loads(project_path.read_text())
    project['board']['design_settings'].setdefault('rule_severities', {})['skew_out_of_range'] = 'ignore'
    project_path.write_text(json.dumps(project))
    request = tmp_path / 'request.json'
    request.write_text(json.dumps(result['settings']['length_match']))
    output = tmp_path / 'result.json'
    done = subprocess.run([sys.executable, str(ROOT / 'src/kicad_route/length_groups.py'),
        str(source / 'group.kicad_pcb'), str(request), str(output)], capture_output=True, text=True, timeout=60)
    measured = json.loads(output.read_text())
    assert done.returncode == 2 and measured['status'] == 'FAIL'
    assert any('ignored' in reason for reason in measured['defects'])


def test_overlapping_groups_have_separate_native_coverage(matched, tmp_path):
    directory, result = matched
    board = next((directory / 'candidate').rglob('group.kicad_pcb'))
    spec = {**result['settings']['length_match'], 'groups': [['DQ0', 'DQ1'], ['DQ1', 'DQ2', 'DQ3']]}
    request = tmp_path / 'request.json'
    request.write_text(json.dumps(spec))
    output = tmp_path / 'result.json'
    done = subprocess.run([sys.executable, str(ROOT / 'src/kicad_route/length_groups.py'),
        str(board), str(request), str(output)], capture_output=True, text=True, timeout=60)
    measured = json.loads(output.read_text())
    assert done.returncode == 0 and measured['status'] == 'PASS', measured
    assert [len(g['measurements']) for g in measured['groups']] == [2, 3]
    assert all(g['status'] == 'PASS' for g in measured['groups'])


def test_revalidation_reads_native_paths_again_without_overwriting_evidence(matched):
    directory, result = matched
    previous = tree_hash(directory / 'validate')
    done = cli('validate', str(directory))
    assert done.returncode == 0, done.stdout + done.stderr
    assert tree_hash(directory / 'validate') == previous
    measured = json.loads(next((directory / 'revalidate').glob('*/length-matching.json')).read_text())
    assert measured['status'] == 'PASS'
    assert measured['board_sha256'] == result['validation']['candidate_board_sha256']
    assert len(measured['groups'][0]['measurements']) == 4


def test_exact_group_names_preserve_active_low_and_literal_glob_characters(tmp_path):
    source = tmp_path / 'input'
    shutil.copytree(ROOT / 'fixtures/group_matching', source)
    mapping = tmp_path / 'rename.json'
    mapping.write_text(json.dumps({'DQ0': '!DQ0', 'DQ1': 'DQ*', 'DQ2': 'DQ.', 'DQ3': '/sheet/DQ*'}))
    project = (source / 'group.kicad_pro').read_bytes()
    subprocess.run([sys.executable, str(ROOT / 'src/kicad_route/kicad/rename_nets.py'),
                    str(source / 'group.kicad_pcb'), str(mapping)], check=True,
                    capture_output=True, timeout=30)
    (source / 'group.kicad_pro').write_bytes(project)
    (source / 'group.kicad_dru').write_text('(version 1)\n'
        '(rule "signal width floor" (constraint track_width (min 0.25mm)))\n'
        '(rule "required group skew" (condition "A.fromTo(\'J1-1\', \'J2-1\') || A.fromTo(\'J3-1\', \'J4-1\')") '
        '(constraint skew (max 0.1mm)))\n')
    before = tree_hash(source)
    done, directory, result = run('run', '--dir', str(source), '--diff-pairs', 'auto',
        '--length-match-group', '!DQ0', 'DQ*', '--group-match-tolerance', '.1',
        '--group-meander-amplitude', '3', '--grid-step', '.1', '--budget', '180')
    assert done.returncode == 0, done.stdout + done.stderr
    assert tree_hash(source) == before
    assert result['validation']['length_matching']['status'] == 'PASS'
    summary = json.loads(next((directory / 'work').rglob('krt-out/summary.json')).read_text())
    group, = summary['length_match_groups']
    assert group['members'] == ['!DQ0', 'DQ*']
    native = json.loads((directory / 'validate/candidate-drc.json').read_text())
    assert native['violations'] == [] and native['unconnected_items'] == []


def test_group_tail_preserves_pair_coupling_and_separate_matching_controls(tmp_path):
    source = tmp_path / 'input'
    shutil.copytree(ROOT / 'fixtures/group_matching', source)
    subprocess.run([sys.executable, '-c', '''import sys,pcbnew as k
b=k.LoadBoard(sys.argv[1]);nets={}
for name in ('DP_P','DP_N'):
 nets[name]=k.NETINFO_ITEM(b,name);b.Add(nets[name])
for ref,x in [('P1',40),('P2',90)]:
 f=k.FOOTPRINT(b);b.Add(f);f.SetReference(ref);f.SetLocked(True)
 f.SetPosition(k.VECTOR2I(k.FromMM(x),k.FromMM(75)))
 f.Reference().SetVisible(False);f.Value().SetVisible(False)
 for i,name in enumerate(nets):
  p=k.PAD(f);p.SetNumber(str(i+1));p.SetAttribute(k.PAD_ATTRIB_SMD);p.SetShape(k.PAD_SHAPE_RECT)
  p.SetSize(k.VECTOR2I(k.FromMM(.25),k.FromMM(.25)))
  layers=k.LSET();layers.AddLayer(k.F_Cu);p.SetLayerSet(layers)
  p.SetPosition(k.VECTOR2I(k.FromMM(x),k.FromMM(75+i*.6)));p.SetNet(nets[name]);f.Add(p)
k.SaveBoard(sys.argv[1],b)
''', str(source / 'group.kicad_pcb')], check=True, capture_output=True, timeout=30)
    path = source / 'group.kicad_pro'
    project = json.loads(path.read_text())
    project['net_settings']['classes'][0].update(diff_pair_width=.25, diff_pair_gap=.35)
    path.write_text(json.dumps(project))
    before = tree_hash(source)
    done, directory, result = run('run', '--backend', 'builtin', '--dir', str(source),
        *ARGS, '--diff-pair-intra-match', '--length-match-tolerance', '.2', '--meander-amplitude', '1')
    assert done.returncode == 0, done.stdout + done.stderr
    assert tree_hash(source) == before
    assert result['routing']['status'] == 'COMPLETE'
    assert result['validation']['length_matching']['status'] == 'PASS'
    assert result['pairs'][0]['status'] == 'ROUTED'
    assert result['pairs'][0]['backend_status'] == 'COUPLED'
    assert result['settings']['length_match']['tolerance_mm'] == .1
    commands = [row['argv'] for row in result['process']['commands']]
    pair = next(argv for argv in commands if any(str(a).endswith('route_diff.py') for a in argv))
    single = next(argv for argv in commands if any(str(a).endswith('/route.py') for a in argv))
    assert pair[pair.index('--length-match-tolerance') + 1] == '0.2'
    assert single[single.index('--length-match-tolerance') + 1] == '0.1'
    assert pair[pair.index('--meander-amplitude') + 1] == '1.0'
    assert single[single.index('--meander-amplitude') + 1] == '3.0'
    native = json.loads((directory / 'validate/candidate-drc.json').read_text())
    assert native['violations'] == [] and native['unconnected_items'] == []
    assert next((directory / 'candidate').rglob('group.kicad_pro')).read_bytes() == path.read_bytes()
    refused = cli('run', '--dir', str(source), '--diff-pairs', 'auto',
                  '--length-match-group', 'DP_P', 'DQ0')
    assert refused.returncode == 4 and 'coupled pair member' in refused.stdout + refused.stderr


@pytest.mark.parametrize('options', [
    ['--group-match-tolerance', '.1'],
    ['--length-match-group', 'DQ0'],
    ['--length-match-group', 'DQ0', 'DQ0'],
    ['--length-match-group', 'DQ0', 'MISSING'],
    ['--length-match-group', 'DQ0', 'DQ1', '--nets', 'DQ0'],
    ['--length-match-group', 'DQ0', 'DQ1', '--group-match-tolerance', 'nan'],
    ['--length-match-group', 'DQ0', 'DQ1', '--group-meander-amplitude', '0'],
    ['--length-match-group', 'DQ0', 'DQ1', '--group-meander-spacing', '.5'],
    ['--length-match-group', 'DQ0', 'DQ1', '--backend-arg=--time-matching'],
    ['--length-match-group', 'DQ0', 'DQ1', '--backend', 'freerouting'],
])
def test_invalid_or_missing_group_controls_refuse_before_routing(options):
    done = cli('run', '--dir', str(ROOT / 'fixtures/group_matching'), *options)
    assert done.returncode == 4, done.stdout + done.stderr
    assert '[route] result:' not in done.stdout
