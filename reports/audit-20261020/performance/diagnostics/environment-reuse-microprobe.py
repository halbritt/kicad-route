"""Bounded actual CLI-version diagnostics; never a routing qualification claim."""
import contextlib
import hashlib
import json
from pathlib import Path
import resource
import statistics
import time

from kicad_route import common


OUTPUT = Path(__file__).resolve().parent
EXPECTED_STATE = OUTPUT.parent / "agent497-bytecode-state"
assert common.STATE == EXPECTED_STATE
actual_run = common.subprocess.run
observed = []


def traced_run(argv, **kwargs):
    start = time.perf_counter()
    result = actual_run(argv, **kwargs)
    if list(argv) == ["kicad-cli", "version"]:
        observed.append({"argv": list(argv), "wall_s": time.perf_counter() - start,
                         "exit_code": result.returncode, "version": result.stdout.strip()})
    return result


def cpu():
    own = resource.getrusage(resource.RUSAGE_SELF)
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {"wrapper_cpu_s": own.ru_utime + own.ru_stime,
            "child_cpu_s": child.ru_utime + child.ru_stime}


common.subprocess.run = traced_run
samples = []
try:
    for repetition in range(1, 4):
        for mode in ("fresh_each_environment", "attempt_scoped_major"):
            common.set_out(OUTPUT / "environment-reuse-probe-out" / f"{mode}-{repetition}")
            observed.clear()
            before = cpu()
            start = time.perf_counter()
            manager = common.tool_environment_scope() if mode == "attempt_scoped_major" else contextlib.nullcontext()
            with manager:
                environments = [common.tool_env() for _ in range(9)]
            wall = time.perf_counter() - start
            after = cpu()
            assert len(observed) == (1 if mode == "attempt_scoped_major" else 9), observed
            assert all(probe["exit_code"] == 0 and probe["version"] for probe in observed)
            assert all(environment == environments[0] for environment in environments)
            samples.append({"mode": mode, "repetition": repetition, "environment_calls": 9,
                            "native_version_probes": len(observed), "wall_s": wall,
                            **{key: after[key] - before[key] for key in before}, "probes": list(observed)})
finally:
    common.subprocess.run = actual_run

result = {"schema": "kicad-route/environment-major-reuse-diagnostic/1",
          "scope": "Three alternating samples per mode, nine actual tool_env calls per sample; no routing/DRC.",
          "limitation": "Version-probe reduction is measured; no guaranteed route-matrix saving is claimed.",
          "common_source": common.__file__,
          "common_sha256": hashlib.sha256(Path(common.__file__).read_bytes()).hexdigest(),
          "selected_state": str(common.STATE), "samples": samples,
          "medians": {mode: {key: statistics.median(sample[key] for sample in samples if sample["mode"] == mode)
                             for key in ("wall_s", "wrapper_cpu_s", "child_cpu_s", "native_version_probes")}
                      for mode in ("fresh_each_environment", "attempt_scoped_major")}}
path = OUTPUT / "environment-reuse-microprobe.json"
path.write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({"receipt": str(path), "medians": result["medians"]}, indent=2))
