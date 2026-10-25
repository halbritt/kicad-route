"""Native capacitor-chain matching must grade the whole saved useful path."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from helpers import OUT, ROOT, cli, run, tree_hash

FIXTURE=ROOT/'fixtures/ac_matching'
PATHS={'DPA_P':('J1-1','C1-1'),'DPA_N':('J1-2','C2-1'),
       'DPB_P':('C1-2','J2-1'),'DPB_N':('C2-2','J2-2')}
ARGS = ['--diff-pairs', 'auto', '--pair-layers', 'F.Cu',
        '--ac-couple-chain-json', '["C1","C2"]', '--ac-match-tolerance', '.1',
        '--ac-meander-amplitude', '3', '--budget', '180']


@pytest.fixture(scope='module')
def matched(tmp_path_factory):
    source=tmp_path_factory.mktemp('ac-chain')/'input';shutil.copytree(FIXTURE,source)
    before=tree_hash(source)
    result=run('run','--dir',str(source),*ARGS)
    assert tree_hash(source)==before
    return result


def test_public_chain_matches_saved_paths_and_revalidates(matched):
    done,directory,result=matched
    assert done.returncode==0,done.stdout+done.stderr
    assert result['validation']['status']=='PASS'
    measured=result['validation']['ac_matching']
    assert measured['status']=='PASS'
    chain,=measured['chains']
    assert chain['skew_mm']+chain['rounding_bound_mm']<=.1
    assert set(chain['measurements_mm'])==set(PATHS)
    native_result=json.loads((directory/'validate/candidate-drc.json').read_text())
    assert native_result['violations']==native_result['unconnected_items']==[]
    assert len(result['pairs'])==2 and all(p['routed'] for p in result['pairs'])
    board=next((directory/'candidate').rglob('chain.kicad_pcb'))
    for suffix in ('.kicad_pro','.kicad_dru'):
        assert board.with_suffix(suffix).read_bytes()==(FIXTURE/('chain'+suffix)).read_bytes()
    checked=cli('validate',str(directory),'--json')
    assert checked.returncode==0,checked.stdout+checked.stderr
    assert json.loads(checked.stdout)['validation']['ac_matching']['status']=='PASS'


@pytest.fixture(scope='module')
def unmatched(tmp_path_factory):
    source=tmp_path_factory.mktemp('ac-unmatched')/'input';shutil.copytree(FIXTURE,source)
    args=list(ARGS);args[args.index('--ac-meander-amplitude')+1]='.001'
    return run('run','--dir',str(source),*args)


def test_public_connected_unmatched_chain_is_rejected(unmatched):
    done,directory,result=unmatched
    assert done.returncode!=0
    native_result=json.loads((directory/'validate/candidate-drc.json').read_text())
    assert native_result['violations']==native_result['unconnected_items']==[]
    measured=result['validation']['ac_matching']
    assert measured['status']=='FAIL' and measured['chains'][0]['skew_mm']>.1


@pytest.mark.parametrize('defect,message', [('dnp','DNP'),('polarity','opposite polarities'),
    ('missing','missing'),('duplicate','duplicate parallel bridges'),('continuation','unlisted two-pad')])
def test_public_chain_refuses_invalid_topology(tmp_path,defect,message):
    import pcbnew as k
    source=tmp_path/'input';shutil.copytree(FIXTURE,source)
    path=source/'chain.kicad_pcb';project=path.with_suffix('.kicad_pro').read_bytes()
    board=k.LoadBoard(str(path));fps={fp.GetReference():fp for fp in board.GetFootprints()}
    args=list(ARGS)
    if defect=='dnp':fps['C1'].SetAttributes(fps['C1'].GetAttributes()|k.FP_DNP)
    elif defect=='polarity':
        next(p for p in fps['C1'].Pads() if p.GetNumber()=='2').SetNet(board.FindNet('DPB_N'))
        next(p for p in fps['C2'].Pads() if p.GetNumber()=='2').SetNet(board.FindNet('DPB_P'))
    elif defect=='missing':args[args.index('--ac-couple-chain-json')+1]='["C1","C99"]'
    elif defect in ('duplicate','continuation'):
        extra=k.Cast_to_FOOTPRINT(fps['C1'].Duplicate(False));extra.SetReference('C3');board.Add(extra)
        if defect=='duplicate':
            args[args.index('--ac-couple-chain-json')+1]='["C1","C3"]'
    k.SaveBoard(str(path),board,True);path.with_suffix('.kicad_pro').write_bytes(project)
    before=tree_hash(source)
    done=cli('run','--dir',str(source),*args)
    assert done.returncode==4 and message in done.stdout+done.stderr,done.stdout+done.stderr
    assert tree_hash(source)==before


def test_ignored_native_length_check_cannot_pass(tmp_path,matched):
    from kicad_route import ac_matching
    _,directory,_=matched
    source=tmp_path/'input'
    shutil.copytree(next((directory/'candidate').rglob('chain.kicad_pcb')).parent,source)
    project=source/'chain.kicad_pro';data=json.loads(project.read_text())
    data['board']['design_settings'].setdefault('rule_severities',{})['length_out_of_range']='ignore'
    project.write_text(json.dumps(data))
    report=ac_matching.check(source/'chain.kicad_pcb',tmp_path/'ignored.json',
        dict(chains=[['C1','C2']],tolerance_mm=.1,amplitude_mm=3),None)
    assert report['status']=='FAIL' and any('ignored' in d for d in report['defects'])


def test_existing_chain_is_preserved_and_remeasured(tmp_path,matched):
    _,directory,_=matched
    source=tmp_path/'input';shutil.copytree(next((directory/'candidate').rglob('chain.kicad_pcb')).parent,source)
    before=tree_hash(source)
    done,out,result=run('run','--dir',str(source),*ARGS)
    assert done.returncode==0,done.stdout+done.stderr
    assert tree_hash(source)==before
    assert result['validation']['ac_matching']['status']=='PASS'
    assert all(p['backend_status']=='ALREADY_CONNECTED' for p in result['pairs'])
    # Native preservation checks must retain every original conductor.
    assert result['validation']['status']=='PASS'


def test_existing_segment_contributes_to_chain_target(tmp_path,unmatched):
    import pcbnew as k
    _,directory,_=unmatched
    source=tmp_path/'input';shutil.copytree(next((directory/'candidate').rglob('chain.kicad_pcb')).parent,source)
    path=source/'chain.kicad_pcb';project=path.with_suffix('.kicad_pro').read_bytes()
    board=k.LoadBoard(str(path))
    for track in list(board.GetTracks()):
        if track.GetNetname().startswith('DPB_'):board.Delete(track)
    k.SaveBoard(str(path),board,True);path.with_suffix('.kicad_pro').write_bytes(project)
    before=tree_hash(source)
    done,out,result=run('run','--dir',str(source),*ARGS)
    assert done.returncode==0,done.stdout+done.stderr
    assert tree_hash(source)==before
    assert result['validation']['ac_matching']['status']=='PASS'
    assert result['pairs'][0]['backend_status']=='ALREADY_CONNECTED'
    assert result['pairs'][1]['backend_status']=='COUPLED'


def test_chain_controls_replay_and_conflicts(tmp_path):
    from kicad_route import cli as api
    parser=api.build_parser();args=parser.parse_args(['run',*ARGS])
    replay=parser.parse_args(['run',*api.route_argv(args)])
    assert replay.ac_couple_chain==[['C1','C2']]
    source=tmp_path/'input';shutil.copytree(FIXTURE,source)
    done=cli('run','--dir',str(source),*ARGS,'--diff-pair-intra-match','--meander-amplitude','2')
    assert done.returncode==4 and 'declare identical controls' in done.stdout+done.stderr


def test_chain_retains_each_segments_native_class(tmp_path):
    source=tmp_path/'input';shutil.copytree(FIXTURE,source)
    path=source/'chain.kicad_pro';project=json.loads(path.read_text())
    base=next(c for c in project['net_settings']['classes'] if c['name']=='CAN')
    project['net_settings']['classes'].append({**base,'name':'Wide','track_width':.2,
        'diff_pair_width':.2,'diff_pair_gap':.3})
    for pattern in project['net_settings']['netclass_patterns']:
        if pattern['pattern'].startswith('DPB_'):pattern['netclass']='Wide'
    path.write_text(json.dumps(project))
    rules=source/'chain.kicad_dru'
    rules.write_text(rules.read_text()+'''
(rule "wide width" (condition "A.NetClass == 'Wide'") (constraint track_width (min 0.2mm) (max 0.2mm)))
(rule "wide gap" (condition "A.NetClass == 'Wide'") (constraint diff_pair_gap (min 0.29mm) (max 0.31mm)))
(rule "wide local skew" (condition "A.NetClass == 'Wide'") (constraint skew (max 0.2mm) (within_diff_pairs)))
(rule "wide terminal escape" (condition "A.NetClass == 'Wide'") (constraint diff_pair_uncoupled (max 4mm)))
''')
    done,directory,result=run('run','--dir',str(source),*ARGS)
    assert done.returncode==0,done.stdout+done.stderr
    assert result['validation']['ac_matching']['status']=='PASS'
    native_result=json.loads((directory/'validate/candidate-drc.json').read_text())
    assert native_result['violations']==native_result['unconnected_items']==[]


def native(board,out):
    cmd=['kicad-cli','pcb','drc','--format','json','--severity-all','--refill-zones',
         '--units','mm','-o',str(out),str(board)]
    done=subprocess.run(cmd,capture_output=True,text=True,timeout=60)
    out.with_suffix('.command.json').write_text(json.dumps(dict(argv=cmd,exit_code=done.returncode,
        stdout=done.stdout,stderr=done.stderr),indent=2)+'\n')
    assert done.returncode==0,done.stdout+done.stderr
    return json.loads(out.read_text())


@pytest.mark.parametrize('amplitude,expected', [('3', True), ('.001', False)])
def test_small_composed_skew_uses_legal_tuning_geometry(amplitude,expected):
    root=OUT/('ac-small-skew-'+amplitude);root.mkdir(parents=True)
    inputs=root/'input';shutil.copytree(FIXTURE,inputs)
    before=tree_hash(inputs);out=root/'candidate';shutil.copytree(inputs,out)
    cmd=[str(ROOT/'.venv-krt/bin/python'),str(ROOT/'src/kicad_route/router/py_router/route_diff.py'),
         str(inputs/'chain.kicad_pcb'),'--output',str(out/'chain.kicad_pcb'),
         '--layers','F.Cu','--track-width','.15','--diff-pair-gap','.25',
         '--diff-pair-intra-match','--ac-couple-match','--length-match-tolerance','.1',
         '--meander-amplitude',amplitude,'--no-fix-drc-settings','--keep-input-copper',
         '--no-stub-layer-swap','--no-gnd-vias','--nets',*PATHS]
    done=subprocess.run(cmd,capture_output=True,text=True,timeout=180)
    (root/'route.log').write_text(done.stdout+done.stderr)
    assert done.returncode==0,done.stdout+done.stderr
    assert tree_hash(inputs)==before
    report=native(out/'chain.kicad_pcb',root/'native.json')
    assert report['violations']==report['unconnected_items']==[],report
    probe=root/'measurement';shutil.copytree(out,probe)
    rules=probe/'chain.kicad_dru'
    rules.write_text(rules.read_text()+'\n'+''.join(
        f'''(rule "measure {net}" (condition "A.fromTo('{a}', '{b}')") (constraint length (max 0mm)))\n'''
        for net,(a,b) in PATHS.items()))
    report=native(probe/'chain.kicad_pcb',root/'measurement.json')
    lengths={}
    for finding in report['violations']:
        match=re.search(r"rule 'measure (.+?)'.*actual ([\d.]+) mm",finding['description'])
        if match:
            assert match[1] not in lengths
            lengths[match[1]]=float(match[2])
    assert set(lengths)==set(PATHS),lengths
    skew=abs(sum(v for n,v in lengths.items() if n.endswith('_P'))-
             sum(v for n,v in lengths.items() if n.endswith('_N')))
    # Conservative sum of four printed rounding bounds.
    assert (skew+.0002<=.1) is expected,lengths
