"""Every attempt protects correctness; measured medians enforce owner limits."""
import importlib.util
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

script = Path(__file__).resolve().parents[1] / "scripts" / "compare_router.py"
spec = importlib.util.spec_from_file_location("compare_router", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('side,backend', [('reference', 'krt'), ('candidate', 'builtin')])
def test_sample_records_selected_backend_before_launch(tmp_path, monkeypatch, side, backend):
    config = SimpleNamespace(
        output=tmp_path, project=tmp_path / 'project', arg=['--budget', '180'],
        reference_cli=tmp_path / 'old-cli.py', candidate_cli=tmp_path / 'new-cli.py',
        reference_state=tmp_path / 'old-state', candidate_state=tmp_path / 'new-state',
        reference_backend='krt', candidate_backend='builtin', timeout=30,
    )
    captured = {}

    class LaunchStopped(Exception):
        pass

    def stop_launch(command, **kwargs):
        captured.update(command=command, env=kwargs['env'])
        raise LaunchStopped

    monkeypatch.setattr(module.subprocess, 'Popen', stop_launch)
    with pytest.raises(LaunchStopped):
        module.run_sample(config, side, 0)

    recorded = json.loads((tmp_path / f'{side}-0' / 'command.json').read_text())
    assert recorded == captured['command']
    assert recorded[recorded.index('--backend') + 1] == backend
    assert recorded[recorded.index('run') - 3] == str(getattr(config, side + '_cli'))
    assert captured['env']['KICAD_ROUTE_STATE'] == str(getattr(config, side + '_state'))
    assert captured['env']['KICAD_ROUTE_LEDGER'] == 'off'


def sample():
    return {"wall_s": 10, "max_rss_kib": 100, "process_tree_peak_rss_kib": 200, "quality": {
        "process": "PASS", "routing": "COMPLETE", "validation": "PASS", "unconnected": 0,
        "open_nets": [], "preservation": {"protected_copper": "PASS"}, "new_violations": 0,
        "input_fingerprint": "same-input", "copper": "original-geometry", "pairs": [],
        "wall_clock_stop": False}}


def compare(changed=None, warmup=False, mode='migration-equivalence'):
    reference, candidate = [sample() for _ in range(3)], [sample() for _ in range(3)]
    warms = {'reference': sample(), 'candidate': sample()}
    target = warms['candidate'] if warmup else candidate[0]
    if changed:
        changed(target)
    return module.compare(reference, candidate, warms, mode)


@pytest.mark.parametrize('key', ['wall_s', 'max_rss_kib', 'process_tree_peak_rss_kib'])
@pytest.mark.parametrize('ratio,passed', [(1.10, True), (1.1001, False)])
def test_each_resource_metric_enforces_exact_owner_boundary(key, ratio, passed):
    reference, candidate = [sample() for _ in range(3)], [sample() for _ in range(3)]
    for s in candidate:
        s[key] *= ratio
    verdict = module.compare(reference, candidate, {'reference': sample(), 'candidate': sample()})
    assert verdict['passed'] is passed


@pytest.mark.parametrize('key', ['wall_s', 'max_rss_kib', 'process_tree_peak_rss_kib'])
@pytest.mark.parametrize('invalid', [None, 0, -1, float('nan'), float('inf'), '12', True])
def test_invalid_metrics_cannot_qualify(key, invalid):
    assert not compare(lambda s: s.update({key: invalid}))['passed']


@pytest.mark.parametrize('field,value,message', [
    ('process', 'FAIL', 'routing process failed'),
    ('validation', 'FAIL', 'native validation failed'),
    ('unconnected', 1, 'routing completion regressed'),
    ('open_nets', ['/lost'], 'routing completion regressed'),
    ('routing', 'INCOMPLETE', 'routing completion regressed'),
    ('pairs', [{'routed': False, 'status': 'INCOMPLETE'}], 'pair routing failed'),
    ('wall_clock_stop', True, 'wall-clock-censored attempt'),
    ('copper', 'changed', 'copper geometry differs'),
    ('preservation', {}, 'preservation failed'),
    ('new_violations', 1, 'new native violations'),
    ('input_fingerprint', 'different', 'inputs differ'),
])
@pytest.mark.parametrize('warmup', [False, True])
def test_measured_and_warmup_quality_gates(field, value, message, warmup):
    verdict = compare(lambda s: s['quality'].update({field: value}), warmup)
    assert not verdict['passed'] and message in verdict['failures']


@pytest.mark.parametrize('field', list(sample()['quality']))
def test_missing_quality_evidence_fails_closed(field):
    verdict = compare(lambda s: s['quality'].pop(field))
    assert not verdict['passed'] and 'missing quality evidence' in verdict['failures']


def test_algorithm_change_mode_keeps_independent_native_and_completion_gates():
    assert compare(lambda s: s['quality'].update(copper='new-algorithm'), mode='algorithm-change')['passed']
    verdict = compare(lambda s: s['quality'].update(copper='new-algorithm', unconnected=1), mode='algorithm-change')
    assert not verdict['passed']


def test_algorithm_change_rejects_omitted_reference_pair_population():
    reference, candidate = [sample() for _ in range(3)], [sample() for _ in range(3)]
    for row in reference:
        row['quality']['pairs'] = [{'p': '/CAN+', 'n': '/CAN-',
                                    'routed': True, 'status': 'ROUTED'}]
    verdict = module.compare(reference, candidate,
                             {'reference': reference[0], 'candidate': candidate[0]},
                             'algorithm-change')
    assert not verdict['passed']
    assert 'pair identities differ' in verdict['failures']


def paired_comparison():
    reference, candidate = [sample() for _ in range(3)], [sample() for _ in range(3)]
    warmup = {'reference': sample(), 'candidate': sample()}
    for row in [*reference, *candidate, *warmup.values()]:
        row['quality']['pairs'] = [{'p': '/CAN+', 'n': '/CAN-', 'routed': True, 'status': 'ROUTED'},
                                   {'p': '/USB+', 'n': '/USB-', 'routed': True, 'status': 'ROUTED'}]
    return reference, candidate, warmup


@pytest.mark.parametrize('where', ['candidate', 'candidate-warmup', 'reference', 'reference-warmup'])
@pytest.mark.parametrize('change', ['missing', 'changed', 'swapped'])
def test_every_measured_and_warmup_attempt_preserves_pair_population(where, change):
    reference, candidate, warmup = paired_comparison()
    rows = {'candidate': candidate[1], 'candidate-warmup': warmup['candidate'],
            'reference': reference[1], 'reference-warmup': warmup['reference']}
    pairs = rows[where]['quality']['pairs']
    if change == 'missing':
        pairs.pop()
    elif change == 'changed':
        pairs[1]['n'] = '/OTHER-'
    else:
        pairs[1]['p'], pairs[1]['n'] = pairs[1]['n'], pairs[1]['p']
    verdict = module.compare(reference, candidate, warmup, 'algorithm-change')
    assert not verdict['passed'] and 'pair identities differ' in verdict['failures']


@pytest.mark.parametrize('bad_pairs', [
    [{'routed': True, 'status': 'ROUTED'}],
    [{'p': '', 'n': '/N', 'routed': True, 'status': 'ROUTED'}],
    [{'p': '/P', 'n': True, 'routed': True, 'status': 'ROUTED'}],
    [{'p': '/P', 'n': '/P', 'routed': True, 'status': 'ROUTED'}],
    [{'p': '/P', 'n': '/N', 'routed': True, 'status': 'ROUTED'}] * 2,
])
def test_malformed_or_duplicate_pair_records_cannot_qualify(bad_pairs):
    reference, candidate, warmup = paired_comparison()
    candidate[0]['quality']['pairs'] = copy.deepcopy(bad_pairs)
    verdict = module.compare(reference, candidate, warmup, 'algorithm-change')
    assert not verdict['passed'] and 'invalid pair identities' in verdict['failures']


def test_pair_record_order_and_new_algorithm_geometry_can_change_together():
    reference, candidate, warmup = paired_comparison()
    for row in [*candidate, warmup['candidate']]:
        row['quality']['pairs'].reverse()
        row['quality']['copper'] = 'new-algorithm-geometry'
    assert module.compare(reference, candidate, warmup, 'algorithm-change')['passed']
    assert not module.compare(reference, candidate, warmup, 'migration-equivalence')['passed']


def test_minimum_repeats_and_warmup_are_required():
    assert not module.compare([sample()], [sample()])['passed']
    assert not module.compare([], [])['passed']


def test_quality_retains_pair_routing_and_any_intermediate_wall_clock_stop():
    result = {'schema': 'kicad-route/route/2', 'process': {'status': 'PASS', 'commands': [{}, {'stalled': True}]},
              'routing': {'status': 'COMPLETE'}, 'pairs': [{'routed': False, 'status': 'INCOMPLETE'}]}
    quality = module.quality(result)
    assert quality['wall_clock_stop'] is True
    assert quality['routing'] == 'COMPLETE'
    assert quality['pairs'][0]['routed'] is False
    del result['pairs']
    assert module.quality(result)['pairs'] is None


@pytest.mark.parametrize('threaded', [False, True])
def test_process_tree_memory_counts_concurrent_child_in_separate_session(tmp_path, threaded):
    ready = tmp_path / 'ready'
    code = 'import pathlib,time; allocation=bytearray(24*1024*1024); pathlib.Path(%r).touch(); time.sleep(30)' % str(ready)
    parent_code = 'import subprocess,sys,time; allocation=bytearray(24*1024*1024); p=subprocess.Popen([sys.executable,"-c",%r],start_new_session=True); time.sleep(30)' % code
    if threaded:
        parent_code = 'import threading,time; t=threading.Thread(target=exec,args=(%r,)); t.start(); time.sleep(30)' % parent_code
    parent = subprocess.Popen([sys.executable, '-c', parent_code], start_new_session=True)
    tracked = {}
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        rss = module.process_tree_rss(parent.pid, tracked)
        assert len(tracked) >= 2
        assert rss > 48 * 1024
    finally:
        module.process_tree_rss(parent.pid, tracked)
        for pid in tracked:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        if parent.poll() is None:
            parent.kill()
        parent.wait()
