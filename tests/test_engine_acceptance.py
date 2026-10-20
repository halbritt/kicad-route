"""Performance gains cannot mask a routing or native-validation regression."""
import importlib.util
from pathlib import Path


def test_fast_candidate_with_one_lost_connection_is_rejected():
    script = Path(__file__).resolve().parents[1] / "scripts" / "compare_router.py"
    spec = importlib.util.spec_from_file_location("compare_router", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    baseline = [{"wall_s": 10, "max_rss_kib": 100, "quality": {
        "process": "PASS", "validation": "PASS", "unconnected": 0, "open_nets": [],
        "preservation": {"protected_copper": "PASS"}, "new_violations": 0,
        "input_fingerprint": "same-input", "copper": "original-geometry",
    }}] * 3
    candidate = [{**baseline[0], "wall_s": 1, "quality": {
        **baseline[0]["quality"], "unconnected": 1,
    }}] * 3
    verdict = module.compare(baseline, candidate)
    assert not verdict["passed"]
    assert "routing completion regressed" in verdict["failures"]
