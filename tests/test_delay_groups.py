"""Nominal timing uses native profiles and final useful-path delay coverage."""
import json
import shutil
from pathlib import Path

import pytest

from helpers import ROOT, cli, run, tree_hash


ARGS = ('--length-match-group-json', '["DQ0","DQ1"]', '--group-delay-tolerance', '1',
        '--group-meander-amplitude', '3', '--grid-step', '.1', '--budget', '180')
FIXTURE = ROOT / 'fixtures/delay_matching'


def test_public_delay_command_replays_constraints():
    from kicad_route import cli as router, length_groups
    parser = router.build_parser()
    args = parser.parse_args(['run', *ARGS])
    replay = parser.parse_args(['run', *router.route_argv(args)])
    assert length_groups.settings(replay) == dict(groups=[['DQ0', 'DQ1']], tolerance_ps=1,
                                                 amplitude_mm=3, spacing_widths=2)


@pytest.fixture(scope='module')
def matched():
    before = tree_hash(FIXTURE)
    done, directory, result = run('run', '--dir', str(FIXTURE), *ARGS)
    assert tree_hash(FIXTURE) == before
    assert done.returncode == 0, done.stdout + done.stderr
    return directory, result


def test_layered_delays_match_with_existing_inner_route_preserved(matched):
    directory, result = matched
    assert result['routing']['status'] == 'COMPLETE'
    measured = result['validation']['length_matching']
    assert measured['status'] == 'PASS'
    assert measured['requirement']['tolerance_ps'] == 1
    assert measured['board_sha256'] == result['validation']['candidate_board_sha256']
    group, = measured['groups']
    assert len(group['measurements']) == 2
    assert all(' ps' in f['description'] for f in group['measurements'])
    assert group['skew_violations'] == []
    assert all(v == 'PASS' for v in result['validation']['preservation'].values())
    native = json.loads((directory / 'validate/candidate-drc.json').read_text())
    assert native['violations'] == [] and native['unconnected_items'] == []
    summary = json.loads((directory / 'work/krt-out/summary.json').read_text())
    assert summary['length_match_groups'][0]['members'] == ['DQ0', 'DQ1']
    board = next((directory / 'candidate').rglob('delay.kicad_pcb'))
    for suffix in ('.kicad_pro', '.kicad_dru'):
        assert board.with_suffix(suffix).read_bytes() == (FIXTURE / ('delay'+suffix)).read_bytes()


@pytest.mark.parametrize('change,reason', [
    ('missing_profile', 'effective native tuning profile'),
    ('old_project_schema', 'effective native tuning profile'),
    ('disabled', 'enabled single-ended'),
    ('zero_delay', 'positive native width and delay'),
    ('missing_inner', 'saved track layer/width'),
    ('missing_er', 'epsilon_r'),
    ('missing_sheet_er', 'epsilon_r'),
    ('ignore_via_height', 'via-height'),
])
def test_missing_delay_inputs_refuse_before_routing(tmp_path, change, reason):
    source = tmp_path / 'input'; shutil.copytree(FIXTURE, source)
    path = source / 'delay.kicad_pro'; doc = json.loads(path.read_text())
    profile = doc['tuning_profiles']['tuning_profiles_impedance_geometric'][0]
    if change == 'missing_profile':
        del doc['tuning_profiles']
    elif change == 'old_project_schema':
        doc['net_settings']['meta']['version'] = 3
    elif change == 'disabled':
        profile['enable_time_domain_tuning'] = False
    elif change == 'zero_delay':
        profile['layer_entries'][0]['delay'] = 0
    elif change == 'missing_inner':
        profile['layer_entries'] = [e for e in profile['layer_entries'] if e['signal_layer'] != 'In2.Cu']
    elif change == 'missing_er':
        board = source / 'delay.kicad_pcb'
        board.write_text(board.read_text().replace('(epsilon_r 4)', '', 1))
    elif change == 'missing_sheet_er':
        board = source / 'delay.kicad_pcb'
        board.write_text(board.read_text().replace('(epsilon_r 4)', '(epsilon_r 4) addsublayer (thickness 0.1)', 1))
    elif change == 'ignore_via_height':
        doc['board']['design_settings']['rules']['use_height_for_length_calcs'] = False
    path.write_text(json.dumps(doc))
    before = tree_hash(source)
    done = cli('run', '--dir', str(source), *ARGS)
    assert done.returncode == 4, done.stdout + done.stderr
    assert reason in done.stdout + done.stderr
    assert tree_hash(source) == before


def test_insufficient_delay_meander_rejects_a_connected_route():
    args = list(ARGS); args[args.index('--group-meander-amplitude')+1] = '.001'
    done, _, result = run('run', '--dir', str(FIXTURE), *args)
    assert done.returncode == 3
    assert result['routing']['unconnected_total'] == 0
    check = result['validation']['length_matching']
    assert check['status'] == 'FAIL'
    assert len(check['groups'][0]['measurements']) == 2
    assert check['groups'][0]['skew_violations']


def test_delay_and_length_tolerances_cannot_silently_override_each_other():
    done = cli('run', '--dir', str(FIXTURE), *ARGS, '--group-match-tolerance', '.1')
    assert done.returncode == 4
    assert 'either mm or ps' in done.stdout + done.stderr
