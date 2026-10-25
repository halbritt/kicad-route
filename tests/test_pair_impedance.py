"""Impedance target -> solved pair geometry -> independently measured native fill."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import ROOT, cli, run, tree_hash


ARGS = ('--diff-pairs', 'auto', '--pair-layers', 'F.Cu', '--pair-impedance', '100',
        '--impedance-tolerance', '10', '--impedance-reference', 'GND', '--impedance-uncoupled', '3',
        '--grid-step', '.025', '--budget', '180')


@pytest.fixture(scope='module')
def impedance(tmp_path_factory):
    source = tmp_path_factory.mktemp('impedance') / 'input'
    shutil.copytree(ROOT / 'fixtures/pair_impedance', source)
    before = tree_hash(source)
    done, directory, result = run('run', '--backend', 'builtin', '--dir', str(source), *ARGS)
    assert tree_hash(source) == before
    assert done.returncode == 0, done.stdout + done.stderr
    return directory, result


def test_public_impedance_route_preserves_native_rules_and_solves_geometry(impedance):
    directory, result = impedance
    measured = json.loads((directory / 'validate/impedance.json').read_text())
    assert measured['status'] == 'PASS', measured
    assert result['validation']['status'] == 'PASS'
    assert result['routing']['status'] == 'COMPLETE'
    code = """import json,sys,pcbnew as k
b=k.LoadBoard(sys.argv[1])
print(json.dumps([t.GetWidth() for t in b.GetTracks() if t.GetNetname() in ('CAN+','CAN-')]))
"""
    native = subprocess.run([sys.executable, '-c', code,
                             str(next((directory / 'candidate').rglob('board.kicad_pcb')))],
                            capture_output=True, text=True, timeout=30, check=True)
    assert set(json.loads(native.stdout)) == {325537}
    for item in measured['nets']:
        assert item['coupled_mm'] > 7
        assert 0 < item['uncoupled_mm'] < 3
        assert all(90 < value < 110 for value in item['impedance_ohm_range'])
    for suffix in ('.kicad_pro', '.kicad_dru'):
        assert next((directory / 'candidate').rglob('board' + suffix)).read_bytes() == (
            ROOT / 'fixtures/pair_impedance' / ('board' + suffix)).read_bytes()


@pytest.mark.parametrize('mutation', ['width', 'missing-reference', 'wrong-reference-net', 'reference-slot', 'midroute-neck', 'missing-coverage', 'escape-limit'])
def test_public_revalidation_rejects_wrong_saved_geometry_or_reference(impedance, tmp_path, mutation):
    directory, _ = impedance
    copied = tmp_path / mutation
    shutil.copytree(directory, copied)
    candidate = next((copied / 'candidate').rglob('board.kicad_pcb'))
    if mutation in ('escape-limit', 'missing-coverage'):
        path = copied / 'attempt.json'
        attempt = json.loads(path.read_text())
        if mutation == 'escape-limit':
            attempt['settings']['pair_impedance']['requirement']['max_uncoupled_mm'] = .5
        else:
            attempt['settings']['pair_impedance']['pairs'] = []
        path.write_text(json.dumps(attempt))
    else:
        code = """import sys,pcbnew as k
b=k.LoadBoard(sys.argv[1]);mutation=sys.argv[2]
if mutation=='width':
 for t in b.GetTracks():
  if t.GetNetname() in ('CAN+','CAN-'):t.SetWidth(250000)
elif mutation=='missing-reference':
 for z in list(b.Zones()):b.Remove(z)
elif mutation=='wrong-reference-net':
 for z in b.Zones():z.SetNet(b.FindNet('CAN+'))
elif mutation=='reference-slot':
 for z in b.Zones():
  p=z.Outline();hole=p.NewHole(0)
  for x,y in [(49,49.5),(51,49.5),(51,51.2),(49,51.2)]:p.Append(k.FromMM(x),k.FromMM(y),0,hole)
elif mutation=='midroute-neck':
 t=max((t for t in b.GetTracks() if t.GetNetname()=='CAN+'),key=lambda t:t.GetLength())
 a,e=t.GetStart(),t.GetEnd();assert a.y==e.y
 lo,hi=sorted([a.x,e.x]);y=int(a.y);mid=(lo+hi)//2;net=t.GetNetCode();width=t.GetWidth();layer=t.GetLayer()
 b.Remove(t)
 for start,end,w in [(lo,mid-50000,width),(mid-50000,mid+50000,250000),(mid+50000,hi,width)]:
  t=k.PCB_TRACK(b);t.SetNetCode(net);t.SetLayer(layer);t.SetWidth(w)
  t.SetStart(k.VECTOR2I(start,y));t.SetEnd(k.VECTOR2I(end,y));b.Add(t)
k.SaveBoard(sys.argv[1],b)
"""
        subprocess.run([sys.executable, '-c', code, str(candidate), mutation],
                       check=True, capture_output=True, timeout=30)
    done = cli('validate', str(copied), '--json')
    result = json.loads(done.stdout)
    assert done.returncode == 3, result
    assert any('impedance' in r for r in result['validation']['reasons']), result
    measured = json.loads(sorted((copied / 'revalidate').glob('*/impedance.json'))[-1].read_text())
    assert measured['status'] == 'FAIL' and measured['defects']
    if mutation == 'midroute-neck':
        assert result['routing']['unconnected_total'] == 0
        assert all(row['uncoupled_mm'] < 3 for row in measured['nets'])
        assert all('not a pad-connected terminal escape' in d for d in measured['defects'])
    if mutation == 'reference-slot':
        assert result['routing']['unconnected_total'] == 0
        assert any('not fully over' in d for d in measured['defects'])


@pytest.mark.parametrize('mutation', ['missing-er', 'unreachable', 'inner-layer', 'multiple-layers'])
def test_unproven_impedance_request_refuses_before_routing(tmp_path, mutation):
    source = tmp_path / 'input'
    shutil.copytree(ROOT / 'fixtures/pair_impedance', source)
    args = list(ARGS)
    if mutation == 'missing-er':
        path = source / 'board.kicad_pcb'
        raw = path.read_text()
        assert '(epsilon_r 4.2)' in raw
        path.write_text(raw.replace('(epsilon_r 4.2)', ''))
    elif mutation == 'unreachable':
        args[args.index('--pair-impedance') + 1] = '300'
    elif mutation == 'inner-layer':
        args[args.index('--pair-layers') + 1] = 'In1.Cu'
    else:
        args.insert(args.index('--pair-layers') + 2, 'B.Cu')
    done = cli('run', '--backend', 'builtin', '--dir', str(source), *args)
    assert done.returncode == 4, done.stdout + done.stderr
    assert 'impedance' in done.stdout + done.stderr
    assert '[route] result:' not in done.stdout
