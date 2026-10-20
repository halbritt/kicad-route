"""Required repeat gates and current-attempt wall-clock qualification."""
from types import SimpleNamespace

import pytest
from kicad_route import cli, common, corpus, determinism, statemanifest


@pytest.mark.parametrize('reproducible,record_error,earlier,want', [
    (False, None, 0, 3), (True, 'cannot record', 0, 1), (True, None, 0, 0),
    (True, None, 3, 3), (False, None, 1, 1)])
def test_update_propagates_repeat_and_preserves_earlier_failure(
        tmp_path, monkeypatch, reproducible, record_error, earlier, want):
    monkeypatch.setattr(cli, 'out_path', lambda *p: tmp_path.joinpath(*p))
    monkeypatch.setattr(cli, 'krt_version', lambda: {'version': 'owned'})
    monkeypatch.setattr(cli, 'freerouting_version', lambda: {})
    monkeypatch.setattr(cli, 'update_krt', lambda steps: 'ok')
    monkeypatch.setattr(cli, 'update_freerouting', lambda steps: 'ok')
    monkeypatch.setattr(statemanifest, 'refresh', lambda *a: (tmp_path / 'manifest', {}))
    monkeypatch.setattr(statemanifest, 'manifest_path', lambda: tmp_path / 'manifest')
    monkeypatch.setattr(statemanifest, 'file_sha256', lambda: 'final-manifest-hash')
    monkeypatch.setattr(cli.skills, 'sync', lambda *a: 'ok')
    monkeypatch.setattr(cli, 'smoke', lambda: earlier)
    monkeypatch.setattr(corpus, 'after_update', lambda path, rc: rc)
    outcome = {'reproducible': reproducible, 'reasons': [] if reproducible else ['geometry differs']}
    if record_error:
        outcome['record_error'] = record_error
    monkeypatch.setattr(determinism, 'check', lambda *a, **kw: (outcome, tmp_path / 'repeat.json'))
    assert cli.update() == want
    record = common.load_json(tmp_path / 'update' / 'update.json')
    assert record['exit_code'] == want
    assert record['determinism']['reproducible'] == reproducible
    assert record['determinism'].get('record_error') == record_error
    assert record['determinism']['record_path'] == str(tmp_path / 'repeat.json')
    assert record['state_manifest']['sha256'] == 'final-manifest-hash'


@pytest.mark.parametrize('flag', ['budget_reached', 'stalled', 'killed'])
@pytest.mark.parametrize('pass_index', [0, 1, 3])
def test_repeat_evidence_cannot_qualify_wall_clock_stopped_attempt(flag, pass_index):
    repeat = {'reproducible': True, 'measurement': True, 'basis': 'build repeat passed'}
    commands = [{} for _ in range(4)]
    commands[pass_index][flag] = True
    process = {'status': 'PASS', 'commands': commands}
    qualified = statemanifest.attempt_qualification(repeat, process)
    assert qualified['measurement'] is False
    assert qualified['build_reproducible'] is True
    assert flag in qualified['reasons'][0]
    assert process['status'] == 'PASS'
    assert determinism.layer_record({'process': process})['wall_clock_stop'] is True


def test_unstopped_repeat_and_freerouting_qualification():
    assert statemanifest.attempt_qualification(
        {'reproducible': True, 'measurement': True, 'basis': 'repeat passed'},
        {'commands': [{}]})['measurement'] is True
    assert statemanifest.attempt_qualification(
        statemanifest.reproducibility('freerouting'), {'commands': [{}]})['measurement'] is False
