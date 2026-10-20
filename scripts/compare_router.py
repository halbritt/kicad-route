#!/usr/bin/env python3
"""Compare a frozen reference CLI/state with the owned router on one project.

Runs both public CLIs, grades every candidate natively, retains raw samples and
enforces the owner's 10% time/RSS limits. Warmups count toward correctness only.
GNU-time RSS and sampled concurrent process-tree summed RSS are separate metrics.
"""
import argparse
import json
import math
import signal
import time
import hashlib
import os
from pathlib import Path
import statistics
import subprocess
import sys


def _positive(number):
    return type(number) in (int, float) and math.isfinite(number) and number > 0


def _quality_errors(quality):
    errors = []
    required = ("process", "routing", "validation", "unconnected", "open_nets", "preservation",
                "new_violations", "input_fingerprint", "copper", "pairs", "wall_clock_stop")
    if not isinstance(quality, dict) or any(quality.get(k) is None for k in required):
        return ["missing quality evidence"]
    if quality["process"] != "PASS":
        errors.append("routing process failed")
    if quality["validation"] != "PASS":
        errors.append("native validation failed")
    if quality["routing"] not in ("COMPLETE", "INCOMPLETE"):
        errors.append("missing routing outcome")
    if type(quality["unconnected"]) is not int or quality["unconnected"] < 0:
        errors.append("invalid connection evidence")
    if not isinstance(quality["open_nets"], list):
        errors.append("invalid open-net evidence")
    if type(quality["new_violations"]) is not int or quality["new_violations"] != 0:
        errors.append("new native violations")
    if not isinstance(quality["preservation"], dict) or not quality["preservation"] or any(
            v != "PASS" for v in quality["preservation"].values()):
        errors.append("preservation failed")
    if not quality["input_fingerprint"] or not quality["copper"]:
        errors.append("missing input/copper identity")
    if not isinstance(quality["pairs"], list) or any(
            not isinstance(p, dict) or p.get("routed") is not True or p.get("status") != "ROUTED"
            for p in quality["pairs"]):
        errors.append("pair routing failed")
    if quality["wall_clock_stop"] is not False:
        errors.append("wall-clock-censored attempt")
    return errors


def compare(reference, candidate, warmup=None, mode="migration-equivalence"):
    failures = []
    if mode not in ("migration-equivalence", "algorithm-change"):
        raise ValueError(f"unknown comparison mode: {mode}")
    if len(reference) < 3 or len(candidate) < 3:
        failures.append("at least three measured repetitions are required")
    if not warmup or set(warmup) != {"reference", "candidate"}:
        failures.append("separate warmups are required")
    all_samples = [*reference, *candidate, *(warmup or {}).values()]
    for sample in all_samples:
        failures.extend(_quality_errors(sample.get("quality")))
    qualities = [sample.get("quality", {}) for sample in all_samples]
    valid_quality = bool(reference and candidate and qualities) and not any(_quality_errors(q) for q in qualities)
    copper_equal = False
    if valid_quality:
        baseline = reference[0]["quality"]
        reference_quality = [s["quality"] for s in reference]
        if warmup and 'reference' in warmup:
            reference_quality.append(warmup['reference']["quality"])
        minimum_open = min(q["unconnected"] for q in reference_quality)
        allowed_open_nets = set.intersection(*(set(q["open_nets"]) for q in reference_quality))
        requires_complete = any(q["routing"] == "COMPLETE" for q in reference_quality)
        for quality in qualities:
            if quality["input_fingerprint"] != baseline["input_fingerprint"]:
                failures.append("inputs differ")
            if (quality["unconnected"] > minimum_open or set(quality["open_nets"]) - allowed_open_nets or
                    (requires_complete and quality["routing"] != "COMPLETE")):
                failures.append("routing completion regressed")
            if set(quality["preservation"]) != set(baseline["preservation"]):
                failures.append("preservation checks differ")
        copper_equal = all(q["copper"] == baseline["copper"] for q in qualities)
        if mode == "migration-equivalence" and not copper_equal:
            failures.append("copper geometry differs")
    metrics = {}
    for key in ("wall_s", "max_rss_kib", "process_tree_peak_rss_kib"):
        if not reference or not candidate or any(not _positive(s.get(key)) for s in [*reference, *candidate]):
            failures.append(f"missing or invalid {key} metrics")
            continue
        before = statistics.median(s[key] for s in reference)
        after = statistics.median(s[key] for s in candidate)
        ratio = after / before
        metrics[key] = {"reference_median": before, "candidate_median": after,
                        "ratio": ratio, "limit": 1.1}
        if after > before * 1.1:
            failures.append(f"{key} exceeds 10% regression limit")
    return {"passed": not failures, "failures": sorted(set(failures)), "metrics": metrics,
            "copper_geometry_equal": copper_equal, "mode": mode}


def quality(result):
    process, routing, validation = (result.get(k) or {} for k in ("process", "routing", "validation"))
    pairs = result.get("pairs", [])
    if result.get("schema", "").endswith('/2') and "pairs" not in result:
        pairs = None
    return {
        "process": process.get("status"), "routing": routing.get("status"),
        "validation": validation.get("status"), "unconnected": routing.get("unconnected_total"),
        "open_nets": routing.get("unrouted_nets"), "preservation": validation.get("preservation"),
        "new_violations": ((validation.get("drc") or {}).get("new_violations") or {}).get("count"),
        "input_fingerprint": (result.get("input") or {}).get("fingerprint"),
        "copper": ((result.get("evidence") or {}).get("candidate_copper_signature") or {}).get("sha256"),
        "pairs": pairs, "wall_clock_stop": any(
            command.get(flag) for command in [process, *(process.get("commands") or [])]
            for flag in ("budget_reached", "stalled", "killed")),
    }


def process_tree_rss(root_pid, tracked):
    """Sample summed RSS of descendants, retaining reparented children by birth ID."""
    processes = {}
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():
            continue
        try:
            fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
            processes[int(directory.name)] = (int(fields[1]), int(fields[19]), int(fields[21]))
        except (FileNotFoundError, ProcessLookupError):
            continue
    members = {pid for pid, (_, birth, _) in processes.items()
               if pid == root_pid or tracked.get(pid) == birth}
    while True:
        children = {pid for pid, (parent, _, _) in processes.items() if parent in members}
        if children <= members:
            break
        members |= children
    tracked.update({pid: processes[pid][1] for pid in members})
    return sum(processes[pid][2] for pid in members) * os.sysconf('SC_PAGE_SIZE') // 1024


def run_sample(config, side, index):
    directory = config.output / f"{side}-{index}"
    directory.mkdir()
    cli = getattr(config, side + "_cli")
    state = getattr(config, side + "_state")
    command = ["/usr/bin/time", "-q", "-f",
               '{"wall_s":%e,"max_rss_kib":%M}', "-o", str(directory / "time.json"),
               sys.executable, str(cli), "--out", str(config.output / (side + "-out")),
               "run", "--backend", "krt", "--dir", str(config.project),
               "--run-dir", str(directory / "run"), "--json", *config.arg]
    env = dict(os.environ, KICAD_ROUTE_STATE=str(state), KICAD_ROUTE_LEDGER="off")
    with (directory / "stdout.log").open("w") as stdout, (directory / "stderr.log").open("w") as stderr:
        with subprocess.Popen(command, env=env, stdout=stdout, stderr=stderr,
                              start_new_session=True) as process:
            tracked, peak = {}, 0
            deadline = time.monotonic() + config.timeout
            try:
                while True:
                    peak = max(peak, process_tree_rss(process.pid, tracked))
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(command, config.timeout)
                    try:
                        process.wait(timeout=0.05)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if process.poll() is None:
                    # The CLI starts backend sessions; kill tracked descendants too.
                    for pid, birth in tracked.items():
                        try:
                            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
                            if int(fields[19]) == birth:
                                os.kill(pid, signal.SIGKILL)
                        except (FileNotFoundError, ProcessLookupError):
                            pass
                    process.kill()
                    process.wait()
    result = json.loads((directory / "run" / "result.json").read_text())
    if process.returncode != result["exit_code"]:
        raise RuntimeError(f"{side} CLI/result exit mismatch in {directory}")
    return {**json.loads((directory / "time.json").read_text()),
            "process_tree_peak_rss_kib": peak, "memory_sample_interval_s": 0.05,
            "cli_sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
            "reference_files": result["evidence"]["reference_files"],
            "exit_code": process.returncode, "quality": quality(result),
            "backend": result["backend"], "tool_identity": result["tool_identity"],
            "result": str(directory / "run" / "result.json"),
            "backend_runtime_s": result["process"]["runtime_s"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("reference-cli", "reference-state", "candidate-cli", "candidate-state",
                 "project", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--arg", action="append", default=[])
    parser.add_argument("--mode", choices=["migration-equivalence", "algorithm-change"],
                        default="migration-equivalence")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    if args.repeats < 3:
        parser.error("at least three measured repetitions are required")
    for name in ("reference_cli", "reference_state", "candidate_cli", "candidate_state",
                 "project", "output"):
        setattr(args, name, getattr(args, name).resolve())
    args.output.mkdir(parents=True, exist_ok=False)
    reference, candidate, warmup = [], [], {}
    report = {"schema": "kicad-route/engine-comparison/2", "project": str(args.project),
              "args": args.arg, "reference": reference, "candidate": candidate, "warmup": warmup,
              "mode": args.mode,
              "measurement": "medians: full-CLI wall time, GNU-time largest individual-process RSS, "
                             "and sampled concurrent process-tree summed RSS (50 ms interval); "
                             "all three gates enforce 1.10. Sampled RSS is a lower bound on peak."}
    for index in range(args.repeats + 1):
        for side in (("reference", "candidate") if index % 2 == 0 else ("candidate", "reference")):
            print(f"{side}: {'warmup' if index == 0 else index}", flush=True)
            sample = run_sample(args, side, index)
            if index == 0:
                warmup[side] = sample
            else:
                (reference if side == "reference" else candidate).append(sample)
            (args.output / "samples.json").write_text(json.dumps(report, indent=2) + "\n")
    report["verdict"] = compare(reference, candidate, warmup, args.mode)
    (args.output / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["verdict"], indent=2))
    return 0 if report["verdict"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
