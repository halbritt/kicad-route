"""A real inner-layer paired route, and refusal of inaccessible or invalid layers."""
import json
import shutil
import subprocess

import pytest

from helpers import ROOT, cli, run, tree_hash
from kicad_route import cli as routing


LAYERS = ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']


def test_straight_via_geometry_preserves_both_member_lengths_and_clearances():
    done = subprocess.run([routing.route_python(), ROOT / 'tests/pair_transition_geometry.py'],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stdout + done.stderr
    assert '16 straight-transition directions/polarities' in done.stdout


def article(path):
    shutil.copytree(ROOT / 'fixtures/pair_transition', path)
    return path


@pytest.fixture(scope='module')
def transition(tmp_path_factory):
    source = article(tmp_path_factory.mktemp('pair-transition') / 'input')
    before = tree_hash(source)
    done, directory, result = run('run', '--backend', 'builtin', '--dir', str(source),
        '--diff-pairs', 'auto', '--pair-layers', *LAYERS, '--grid-step', '.1',
        '--diff-pair-intra-match', '--length-match-tolerance', '.1', '--meander-amplitude', '2',
        '--budget', '120', '--label', 'inner-pair')
    assert tree_hash(source) == before
    return done, directory, result


def test_public_pair_crosses_inner_copper_and_preserves_native_requirements(transition):
    done, directory, result = transition
    assert done.returncode == 0, result
    assert result['validation']['status'] == 'PASS', result
    assert all(v == 'PASS' for v in result['validation']['preservation'].values())
    assert result['settings']['pair_layers'] == LAYERS
    pair, = result['pairs']
    assert pair['backend_status'] == 'COUPLED'
    assert pair['matching']['status'] == 'PASS'
    assert pair['skew'] < .1
    native = json.loads((directory / 'validate/candidate-drc.json').read_text())
    assert not native['violations'] and not native['unconnected_items']
    facts = json.loads((directory / 'validate/candidate-facts.json').read_text())
    assert len(facts['vias']) == 4
    assert {t['layer'] for t in facts['tracks']} == {'F.Cu', 'In1.Cu'}
    assert len(facts['rule_areas']) == 1
    command = next(c['argv'] for c in result['process']['commands']
                   if any(str(a).endswith('route_diff.py') for a in c['argv']))
    start = command.index('--layers') + 1
    assert command[start:start + 4] == LAYERS


def test_outer_layers_cannot_cross_the_same_native_barrier(tmp_path):
    source = article(tmp_path / 'outer-only')
    done, _, result = run('run', '--backend', 'builtin', '--dir', str(source),
        '--diff-pairs', 'auto', '--pair-layers', 'F.Cu', 'B.Cu', '--grid-step', '.1',
        '--budget', '120', '--label', 'outer-pair')
    assert done.returncode != 0, result
    assert result['validation']['status'] != 'PASS' or result['routing']['status'] != 'COMPLETE'
    assert not all(p['routed'] for p in result['pairs'])


def test_revalidation_rejects_copper_outside_requested_layers(transition, tmp_path):
    _, directory, _ = transition
    copied = tmp_path / 'restricted-revalidation'
    shutil.copytree(directory, copied)
    path = copied / 'attempt.json'
    attempt = json.loads(path.read_text())
    attempt['settings']['pair_layers'] = ['F.Cu', 'B.Cu']
    path.write_text(json.dumps(attempt))
    done = cli('validate', str(copied), '--json')
    result = json.loads(done.stdout)
    assert done.returncode == 3, result
    assert any('new pair tracks outside permitted pair layers' in reason
               for reason in result['validation']['reasons'])
    assert result['routing']['status'] == 'COMPLETE'


@pytest.mark.parametrize('layers', [['F.Cu', 'F.Cu'], ['In3.Cu'], ['F.SilkS']])
def test_invalid_pair_layers_refuse_before_routing(tmp_path, layers):
    source = article(tmp_path / 'invalid')
    done = cli('run', '--backend', 'builtin', '--dir', str(source), '--diff-pairs', 'auto',
               '--pair-layers', *layers)
    assert done.returncode == 4, done.stdout + done.stderr
    assert 'pair-layers' in done.stdout + done.stderr
    assert '[route] result:' not in done.stdout


def test_pair_layer_policy_requires_pair_routing(tmp_path):
    source = article(tmp_path / 'no-pairs')
    done = cli('run', '--backend', 'builtin', '--dir', str(source), '--pair-layers', *LAYERS)
    assert done.returncode == 4
    assert '--pair-layers requires --diff-pairs auto' in done.stdout + done.stderr
