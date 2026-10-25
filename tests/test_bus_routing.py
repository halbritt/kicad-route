"""Opt-in bus grouping on native copper through paired and single-ended orchestration."""
import json
import shutil
import subprocess
import sys

import pytest

from helpers import ROOT, cli, run, tree_hash

ARGS = ('--bus', '--bus-detection-radius', '20', '--bus-min-nets', '3',
        '--grid-step', '.1', '--budget', '180')


@pytest.fixture(scope='module')
def bus_route(tmp_path_factory):
    source = tmp_path_factory.mktemp('bus') / 'input'
    shutil.copytree(ROOT / 'fixtures/bus_routing', source)
    before = tree_hash(source)
    done, directory, result = run('run', '--backend', 'builtin', '--dir', str(source),
                                  '--diff-pairs', 'auto', *ARGS)
    assert tree_hash(source) == before
    assert done.returncode == 0, done.stdout + done.stderr
    return directory, result


def assert_native_bus(directory, result):
    assert result['routing']['status'] == 'COMPLETE'
    assert result['validation']['status'] == 'PASS'
    native = json.loads((directory / 'validate/candidate-drc.json').read_text())
    assert native['violations'] == [] and native['unconnected_items'] == []
    groups = result['process']['bus_routing']['groups']
    assert len(groups) == 1
    group = next(iter(groups.values()))
    assert group['members_in_physical_order'] == [f'D{i}' for i in range(8)]
    assert group['corridor_planned'] and group['corridor_points'] > 1
    story_path = next((directory / 'work').rglob('bus_netstory.json'))
    story = json.loads(story_path.read_text())
    assert 'bus' not in story['nets']['AUX']
    assert story['phase3_order'] == ['D3']
    for suffix in ('.kicad_pro', '.kicad_dru'):
        assert next((directory / 'candidate').rglob('bus' + suffix)).read_bytes() == (
            ROOT / 'fixtures/bus_routing' / ('bus' + suffix)).read_bytes()
    return story


def test_public_bus_with_tap_and_unrelated_net(bus_route):
    directory, result = bus_route
    assert_native_bus(directory, result)
    assert result['settings']['bus'] == dict(detection_radius=20., attraction_radius=5.,
                                             attraction_bonus=5000, min_nets=3)
    raw = json.loads((directory / 'work/single/krt-out/summary.json').read_text())
    assert raw['pad_pairs_connected'] == raw['pad_pairs_total'] == 10
    assert raw['multipoint_pads_connected'] == raw['multipoint_pads_total'] == 3


def test_native_revalidation_rejects_a_missing_bus_lane(bus_route, tmp_path):
    directory, _ = bus_route
    copied = tmp_path / 'missing-lane'; shutil.copytree(directory, copied)
    board = next((copied / 'candidate').rglob('bus.kicad_pcb'))
    subprocess.run([sys.executable, '-c', '''import sys,pcbnew as k
b=k.LoadBoard(sys.argv[1])
for t in list(b.GetTracks()):
 if t.GetNetname()=='D7':b.Remove(t)
k.SaveBoard(sys.argv[1],b)
''', str(board)], check=True, capture_output=True, timeout=30)
    done = cli('validate', str(copied), '--json')
    result = json.loads(done.stdout)
    assert done.returncode != 0
    assert result['routing']['status'] == 'INCOMPLETE'
    assert result['routing']['unconnected_total'] > 0


def test_bus_single_ended_tail_preserves_a_coupled_pair(tmp_path):
    source = tmp_path / 'input'; shutil.copytree(ROOT / 'fixtures/bus_routing', source)
    subprocess.run([sys.executable, '-c', '''import sys,pcbnew as k
p=sys.argv[1];b=k.LoadBoard(p)
nets={}
for name in ('DP_P','DP_N'):
 nets[name]=k.NETINFO_ITEM(b,name);b.Add(nets[name])
for ref,x in [('P1',40),('P2',90)]:
 f=k.FOOTPRINT(b);b.Add(f);f.SetReference(ref);f.SetLocked(True)
 f.SetPosition(k.VECTOR2I(k.FromMM(x),k.FromMM(91)))
 f.Reference().SetVisible(False);f.Value().SetVisible(False)
 for i,name in enumerate(nets):
  p=k.PAD(f);p.SetNumber(str(i+1));p.SetAttribute(k.PAD_ATTRIB_SMD);p.SetShape(k.PAD_SHAPE_RECT)
  p.SetSize(k.VECTOR2I(k.FromMM(.25),k.FromMM(.25)))
  layers=k.LSET();layers.AddLayer(k.F_Cu);p.SetLayerSet(layers)
  p.SetPosition(k.VECTOR2I(k.FromMM(x),k.FromMM(91+i*.6)));p.SetNet(nets[name]);f.Add(p)
k.SaveBoard(sys.argv[1],b)
''', str(source / 'bus.kicad_pcb')], check=True, capture_output=True, timeout=30)
    project = json.loads((source / 'bus.kicad_pro').read_text())
    project['net_settings']['classes'][0].update(diff_pair_width=.25, diff_pair_gap=.35)
    (source / 'bus.kicad_pro').write_text(json.dumps(project))
    before = tree_hash(source)
    done, directory, result = run('run', '--backend', 'builtin', '--dir', str(source),
                                  '--diff-pairs', 'auto', *ARGS)
    assert done.returncode == 0, done.stdout + done.stderr
    assert tree_hash(source) == before
    assert result['routing']['status'] == 'COMPLETE'
    native = json.loads((directory / 'validate/candidate-drc.json').read_text())
    assert native['violations'] == [] and native['unconnected_items'] == []
    assert result['pairs'][0]['p'] == 'DP_P'
    assert result['pairs'][0]['n'] == 'DP_N'
    story = json.loads(next((directory / 'work').rglob('bus_netstory.json')).read_text())
    assert {'DP_P', 'DP_N'}.isdisjoint(story['nets'])
    assert next(iter(story['bus_groups'].values()))['members_in_physical_order'] == [f'D{i}' for i in range(8)]
    # The original project still controls both passes; no backend writeback adopted.
    assert next((directory / 'candidate').rglob('bus.kicad_pro')).read_bytes() == (source / 'bus.kicad_pro').read_bytes()


@pytest.mark.parametrize('options', [
    ['--bus-detection-radius', '20'],
    ['--bus', '--bus-detection-radius', 'nan'],
    ['--bus', '--bus-attraction-radius', '0'],
    ['--bus', '--bus-attraction-bonus', '-1'],
    ['--bus', '--bus-min-nets', '1'],
    ['--bus', '--backend-arg=--bus'],
    ['--bus', '--backend', 'freerouting'],
])
def test_invalid_bus_controls_refuse_before_routing(options):
    done = cli('run', '--dir', str(ROOT / 'fixtures/bus_routing'), *options)
    assert done.returncode == 4, done.stdout + done.stderr
    assert 'bus' in (done.stdout + done.stderr).lower()
    assert '[route] result:' not in done.stdout
