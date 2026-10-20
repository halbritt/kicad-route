"""Synthetic controls and selector tests only: no workers or systemd operations."""
import copy
from pathlib import Path
import runpy

import pytest

CAL = runpy.run_path(str(Path(__file__).with_name("calibrate-cpu-service.py")))


def workers():
    return [dict(cpu_s=3.0, affinity=list(range(12)), nice=0, scheduler=0) for _ in range(4)]


def receipts(passed):
    return [dict(status="PASS" if value else "INSUFFICIENT_SERVICE", verdict={"passed": value})
            for value in passed]


def test_two_cpu_service_threshold_is_explicit_rate_not_router_gate():
    assert CAL["verdict"](2, 6.66, workers())["passed"]
    assert not CAL["verdict"](2, 6.67, workers())["passed"]


def test_one_cpu_service_threshold():
    assert CAL["verdict"](1, 13.33, workers())["passed"]
    assert not CAL["verdict"](1, 13.34, workers())["passed"]


def test_all_three_trials_required_at_highest_cap():
    assert CAL["select"]({"2": receipts([True, True, True])}) == (2, "PASS")
    assert CAL["select"]({"2": receipts([True, False, True])}) == (None, "TRY_LOWER_CAP")


def test_one_cpu_only_selected_after_two_cpu_readiness_failure():
    observed = {"2": receipts([False, False, False]), "1": receipts([True, True, True])}
    assert CAL["select"](observed) == (1, "PASS")
    observed["1"] = receipts([True, False, True])
    assert CAL["select"](observed) == (None, "INSUFFICIENT_SERVICE")


def test_control_errors_do_not_become_capacity_fallback():
    observed = {"2": [{"status": "ERROR"}, *receipts([True, True])]}
    assert CAL["select"](observed) == (None, "CALIBRATION_ERROR")
    with pytest.raises(ValueError):
        CAL["select"]({"1": receipts([True, True, True])})


def test_incomplete_or_underworked_samples_are_rejected():
    with pytest.raises(ValueError):
        CAL["select"]({"2": receipts([True, True])})
    values = workers()
    values[0]["cpu_s"] = 2.99
    with pytest.raises(ValueError):
        CAL["verdict"](2, 6, values)
    with pytest.raises(ValueError):
        CAL["verdict"](2, float("nan"), workers())


def controls():
    return {"affinity": list(range(12)), "nice": 0, "scheduler": 0,
            "cgroup": "/user.slice/user-1000.slice/user@1000.service/app.slice/kicadroute-agent497-calibration-test.scope",
            "ancestry": [{"cpu.max": "200000 100000", "cpu.max.burst": "0", "cpu.weight": "100"},
                         {"cpu.max": "max 100000"}]}


def test_planned_normal_scope_controls_accept_but_changed_quota_rejects():
    actual = controls()
    CAL["validate_controls"](actual, 2)
    for field, bad in [("cpu.max", "200000 200000"), ("cpu.max.burst", "10000"), ("cpu.weight", "200")]:
        changed = copy.deepcopy(actual)
        changed["ancestry"][0][field] = bad
        with pytest.raises(RuntimeError):
            CAL["validate_controls"](changed, 2)


def test_ancestor_cap_and_scheduler_controls_are_checked():
    actual = controls()
    actual["ancestry"][1]["cpu.max"] = "100000 100000"
    with pytest.raises(RuntimeError):
        CAL["validate_controls"](actual, 2)
    actual = controls()
    actual["nice"] = 1
    with pytest.raises(RuntimeError):
        CAL["validate_controls"](actual, 2)


def test_too_fast_work_cannot_validate_an_unapplied_cap():
    for cap, wall in [(2, 3), (2, 5.89), (1, 11.89)]:
        with pytest.raises(ValueError, match="too fast"):
            CAL["verdict"](cap, wall, workers())
    assert CAL["verdict"](2, 5.9, workers())["passed"]
    assert CAL["verdict"](1, 11.9, workers())["passed"]


def test_ready_and_completed_worker_membership_and_controls_must_match():
    group = controls()["cgroup"]
    actual = [dict(pid=index + 100, cgroup=group, cgroup_after=group,
                   affinity=list(range(12)), affinity_after=list(range(12)),
                   nice=0, nice_after=0, scheduler=0, scheduler_after=0) for index in range(4)]
    CAL["validate_workers"](actual, group, finished=True)
    for field, bad in [("cgroup", "/other.scope"), ("cgroup_after", "/other.scope"),
                       ("nice_after", 1), ("scheduler_after", 3), ("affinity_after", [0])]:
        changed = copy.deepcopy(actual)
        changed[0][field] = bad
        with pytest.raises(RuntimeError):
            CAL["validate_workers"](changed, group, finished=True)


def test_cpu_accounting_must_cover_worker_work_and_respect_cap():
    CAL["validate_accounting"](12, 12.005, 2, 6)
    for cpu in [11.97, 12.23, float("nan")]:
        with pytest.raises(RuntimeError):
            CAL["validate_accounting"](12, cpu, 2, 6)
