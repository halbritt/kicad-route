"""Inner pair routes require both filled references, including real PTH launches."""
import json
import shutil
import subprocess
import sys

import pytest

from helpers import ROOT, cli, run, tree_hash


ARGS = ('--diff-pairs', 'auto', '--pair-layers', 'In1.Cu', '--pair-impedance', '100',
        '--impedance-tolerance', '10', '--impedance-reference', 'GND', '--impedance-uncoupled', '4',
        '--grid-step', '.025', '--budget', '180')


@pytest.fixture(scope='module')
def stripline(tmp_path_factory):
    source = tmp_path_factory.mktemp('stripline') / 'input'
    shutil.copytree(ROOT / 'fixtures/pair_stripline', source)
    before = tree_hash(source)
    done, directory, result = run('run', '--backend', 'builtin', '--dir', str(source), *ARGS)
    assert tree_hash(source) == before
    assert done.returncode == 0, done.stdout + done.stderr
    return directory, result


def test_public_inner_pair_checks_both_planes_and_actual_terminal_antipads(stripline):
    directory, result = stripline
    measured = json.loads((directory / 'validate/impedance.json').read_text())
    assert measured['status'] == 'PASS', measured
    assert result['validation']['status'] == 'PASS'
    assert result['routing']['status'] == 'COMPLETE'
    drc = json.loads((directory / 'validate/candidate-drc.json').read_text())
    assert drc['violations'] == [] and drc['unconnected_items'] == []
    assert measured['plan']['reference_layers'] == ['F.Cu', 'In2.Cu']
    for row in measured['nets']:
        assert row['coupled_mm'] > 7
        assert 0 < row['uncoupled_mm'] < 4
        assert set(row['reference_opening_area_mm2']) == {'F.Cu', 'In2.Cu'}
        assert all(0 < area < 1 for area in row['reference_opening_area_mm2'].values())
        assert all(90 < value < 110 for value in row['impedance_ohm_range'])
        assert row['antipad_polygon_margin_mm'] == .006
    for suffix in ('.kicad_pro', '.kicad_dru'):
        assert next((directory / 'candidate').rglob('board' + suffix)).read_bytes() == (
            ROOT / 'fixtures/pair_stripline' / ('board' + suffix)).read_bytes()


@pytest.mark.parametrize('layer', ['F.Cu', 'In2.Cu'])
@pytest.mark.parametrize('mutation', ['missing', 'trunk-slot', 'launch-slot', 'between-traces-slot'])
def test_either_reference_and_oversized_terminal_opening_fail(stripline, tmp_path, layer, mutation):
    directory, _ = stripline
    copied = tmp_path / 'damaged'
    shutil.copytree(directory, copied)
    candidate = next((copied / 'candidate').rglob('board.kicad_pcb'))
    code = """import sys,pcbnew as k
b=k.LoadBoard(sys.argv[1]);layer=b.GetLayerID(sys.argv[2]);mutation=sys.argv[3]
for z in list(b.Zones()):
 if not z.IsOnLayer(layer):continue
 if mutation=='missing':b.Remove(z)
 else:
  points=([(49,48),(51,48),(51,52),(49,52)] if mutation=='trunk-slot' else
          [(45.9,48.4),(46.5,48.4),(46.5,50.7),(45.9,50.7)])
  if mutation=='between-traces-slot':points=[(49,49.9),(51,49.9),(51,50.1),(49,50.1)]
  p=z.Outline();hole=p.NewHole(0)
  for x,y in points:p.Append(k.FromMM(x),k.FromMM(y),0,hole)
k.SaveBoard(sys.argv[1],b)
"""
    subprocess.run([sys.executable, '-c', code, str(candidate), layer, mutation],
                   check=True, capture_output=True, timeout=30)
    done = cli('validate', str(copied), '--json')
    result = json.loads(done.stdout)
    assert done.returncode == 3, result
    assert result['routing']['unconnected_total'] == 0
    measured = json.loads(sorted((copied / 'revalidate').glob('*/impedance.json'))[-1].read_text())
    assert measured['status'] == 'FAIL'
    assert any(layer in defect and ('not fully over' in defect or 'missing' in defect or 'coupled corridor' in defect)
               for defect in measured['defects'])
    # Escape length cannot hide a reference discontinuity, even beside a pad.
    assert all(row['uncoupled_mm'] < 4 for row in measured['nets'])
    if mutation != 'missing':
        drc = json.loads(sorted((copied / 'revalidate').glob('*/candidate-drc.json'))[-1].read_text())
        assert drc['violations'] == [], drc


@pytest.mark.parametrize('mutation', ['missing-upper-er', 'missing-lower-er', 'asymmetric', 'unequal-er'])
def test_unknown_or_unqualified_inner_stackup_refuses_before_route(tmp_path, mutation):
    source = tmp_path / 'input'
    shutil.copytree(ROOT / 'fixtures/pair_stripline', source)
    board = source / 'board.kicad_pcb'
    text = board.read_text()
    if mutation.startswith('missing'):
        start = text.index('(epsilon_r 4.2)')
        if mutation == 'missing-lower-er':
            start = text.index('(epsilon_r 4.2)', start + 1)
        text = text[:start] + text[start:].replace('(epsilon_r 4.2)', '', 1)
    elif mutation == 'unequal-er':
        text = text.replace('(epsilon_r 4.2)', '(epsilon_r 4.8)', 1)
    else:
        text = text.replace('(thickness 0.486666)', '(thickness 0.4)', 1)
    assert text != board.read_text()
    board.write_text(text)
    done = cli('run', '--backend', 'builtin', '--dir', str(source), *ARGS)
    assert done.returncode == 4, done.stdout + done.stderr
    assert 'impedance' in done.stdout + done.stderr
    assert '[route] result:' not in done.stdout
