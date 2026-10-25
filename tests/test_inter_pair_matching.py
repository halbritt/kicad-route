"""All coupled paths must survive grouped tuning and native final grading."""
import json
from pathlib import Path
import shutil

import pytest
from helpers import cli, run, tree_hash

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/inter_pair_matching'
GROUP = ['A_P', 'A_N', 'B_P', 'B_N']
ARGS = ['--diff-pairs', 'auto', '--pair-layers', 'F.Cu', '--grid-step', '.025',
        '--length-match-group-json', json.dumps(GROUP), '--group-match-tolerance', '.1',
        '--group-meander-amplitude', '8', '--budget', '180']


@pytest.fixture(scope='module')
def matched(tmp_path_factory):
    source = tmp_path_factory.mktemp('inter-pair')/'input'
    shutil.copytree(FIXTURE, source)
    before = tree_hash(source)
    value = run('run', '--dir', str(source), *ARGS)
    assert tree_hash(source) == before
    return value


def test_public_group_preserves_all_pairs_and_native_rules(matched):
    done, directory, result = matched
    assert done.returncode == 0, done.stdout+done.stderr
    assert result['validation']['status'] == 'PASS'
    measured = result['validation']['length_matching']
    assert measured['status'] == 'PASS'
    assert len(measured['groups'][0]['measurements']) == 4
    assert measured['groups'][0]['skew_violations'] == []
    assert all(p['routed'] and p['backend_status'] == 'COUPLED' for p in result['pairs'])
    assert len(result['pairs']) == 2
    argv = [r['argv'] for r in result['process']['commands']
            if any(str(a).endswith('/route_diff.py') for a in r['argv'])]
    assert len(argv) == 1
    assert set(argv[0][argv[0].index('--nets')+1:]) == set(GROUP)
    native = json.loads((directory/'validate/candidate-drc.json').read_text())
    assert native['violations'] == native['unconnected_items'] == []
    for suffix in ('.kicad_pro','.kicad_dru'):
        assert next((directory/'candidate').rglob('pairs'+suffix)).read_bytes() == (FIXTURE/('pairs'+suffix)).read_bytes()
    checked = cli('validate', str(directory), '--json')
    assert checked.returncode == 0, checked.stdout+checked.stderr
    assert json.loads(checked.stdout)['validation']['length_matching']['status'] == 'PASS'


def test_connected_insufficient_group_meander_is_rejected(tmp_path):
    source=tmp_path/'input';shutil.copytree(FIXTURE,source)
    args=list(ARGS);args[args.index('--group-meander-amplitude')+1]='.001'
    done,directory,result=run('run','--dir',str(source),*args)
    assert done.returncode != 0
    native=json.loads((directory/'validate/candidate-drc.json').read_text())
    assert native['unconnected_items']==[]
    assert result['validation']['length_matching']['status']=='FAIL'
    assert any(v['type']=='skew_out_of_range' for v in native['violations'])


def test_stricter_authored_coupling_limit_still_rejects(tmp_path):
    source=tmp_path/'input';shutil.copytree(FIXTURE,source)
    rules=source/'pairs.kicad_dru';rules.write_text(rules.read_text().replace('(max 3mm)','(max 1mm)'))
    before=tree_hash(source)
    done,directory,result=run('run','--dir',str(source),*ARGS)
    assert done.returncode != 0
    assert tree_hash(source)==before
    native=json.loads((directory/'validate/candidate-drc.json').read_text())
    assert native['unconnected_items']==[]
    assert result['validation']['length_matching']['status']=='PASS'
    assert any(v['type']=='diff_pair_uncoupled_length_too_long' for v in native['violations'])


@pytest.mark.parametrize('group', [['A_P','B_P'],['A_P','A_N']])
def test_partial_pair_or_single_pair_group_refuses(tmp_path,group):
    source=tmp_path/'input';shutil.copytree(FIXTURE,source)
    args=list(ARGS);args[args.index('--length-match-group-json')+1]=json.dumps(group)
    done=cli('run','--dir',str(source),*args)
    assert done.returncode==4 and 'both members of at least two pairs' in done.stdout+done.stderr


def test_conflicting_intra_group_controls_refuse(tmp_path):
    source=tmp_path/'input';shutil.copytree(FIXTURE,source)
    done=cli('run','--dir',str(source),*ARGS,'--diff-pair-intra-match','--meander-amplitude','2')
    assert done.returncode==4 and 'declare identical controls' in done.stdout+done.stderr


def test_failure_of_one_pair_discards_the_group(tmp_path):
    import pcbnew as k
    source=tmp_path/'input';shutil.copytree(FIXTURE,source)
    path=source/'pairs.kicad_pcb';project=path.with_suffix('.kicad_pro').read_bytes()
    board=k.LoadBoard(str(path));wall=k.ZONE(board);wall.SetIsRuleArea(True)
    layers=k.LSET();layers.AddLayer(k.F_Cu);wall.SetLayerSet(layers)
    wall.SetDoNotAllowTracks(True);wall.SetDoNotAllowVias(True);wall.SetDoNotAllowPads(False)
    wall.SetDoNotAllowFootprints(False);wall.SetZoneName('blocked B terminal')
    wall.Outline().NewOutline()
    for x,y in [(99,59),(101,59),(101,62),(99,62)]:wall.Outline().Append(k.FromMM(x),k.FromMM(y))
    board.Add(wall);k.SaveBoard(str(path),board,True);path.with_suffix('.kicad_pro').write_bytes(project)
    done,directory,result=run('run','--dir',str(source),*ARGS)
    assert done.returncode != 0
    assert all(p['backend_status']=='REJECTED' for p in result['pairs']),result['pairs']
    candidate=k.LoadBoard(str(next((directory/'candidate').rglob('pairs.kicad_pcb'))))
    assert len(list(candidate.GetTracks()))==0


def test_existing_long_pair_sets_target_without_copper_changes(tmp_path, matched):
    import pcbnew as k
    _, earlier, _ = matched
    source=tmp_path/'input'
    shutil.copytree(next((earlier/'candidate').rglob('pairs.kicad_pcb')).parent,source)
    path=source/'pairs.kicad_pcb';project=path.with_suffix('.kicad_pro').read_bytes()
    board=k.LoadBoard(str(path))
    def geometry(board):
        return sorted((t.GetNetname(),t.GetLayerName(),t.GetStart().x,t.GetStart().y,
                       t.GetEnd().x,t.GetEnd().y,t.GetWidth(),t.IsLocked())
                      for t in board.GetTracks() if t.GetNetname().startswith('B_'))
    kept=geometry(board)
    for track in list(board.GetTracks()):
        if track.GetNetname().startswith('A_'):board.Delete(track)
    k.SaveBoard(str(path),board,True);path.with_suffix('.kicad_pro').write_bytes(project)
    before=tree_hash(source)
    done,directory,result=run('run','--dir',str(source),*ARGS,'--protect','existing')
    assert done.returncode==0,done.stdout+done.stderr
    assert tree_hash(source)==before
    assert result['validation']['length_matching']['status']=='PASS'
    assert next(p for p in result['pairs'] if p['p']=='B_P')['backend_status']=='ALREADY_CONNECTED'
    final=k.LoadBoard(str(next((directory/'candidate').rglob('pairs.kicad_pcb'))))
    assert geometry(final)==kept


def test_native_pair_classes_keep_distinct_geometry_during_group_tuning(tmp_path):
    source=tmp_path/'input';shutil.copytree(FIXTURE,source)
    path=source/'pairs.kicad_pro';project=json.loads(path.read_text())
    other={**project['net_settings']['classes'][1], 'name':'WidePair',
           'diff_pair_width':.3, 'diff_pair_gap':.3, 'track_width':.3}
    project['net_settings']['classes'].append(other)
    for pattern in project['net_settings']['netclass_patterns']:
        if pattern['pattern'].startswith('B_'):pattern['netclass']='WidePair'
    path.write_text(json.dumps(project,indent=2)+'\n')
    rules=source/'pairs.kicad_dru'
    rules.write_text(rules.read_text()+'''
(rule "wide pair width" (condition "A.NetClass == 'WidePair'") (constraint track_width (min 0.3mm) (max 0.3mm)))
(rule "wide pair gap" (condition "A.NetClass == 'WidePair'") (constraint diff_pair_gap (min 0.29mm) (max 0.31mm)))
''')
    done,directory,result=run('run','--dir',str(source),*ARGS)
    assert done.returncode==0,done.stdout+done.stderr
    assert result['validation']['length_matching']['status']=='PASS'
    native=json.loads((directory/'validate/candidate-drc.json').read_text())
    assert native['violations']==native['unconnected_items']==[]
