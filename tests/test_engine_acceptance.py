"""Every attempt protects correctness; measured medians enforce owner limits."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

script = Path(__file__).resolve().parents[1] / "scripts" / "compare_router.py"
spec = importlib.util.spec_from_file_location("compare_router", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


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
