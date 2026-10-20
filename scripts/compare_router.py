#!/usr/bin/env python3
"""Compare a frozen reference CLI/state with the owned router on one project.

Runs both public CLIs, grades every candidate natively, retains raw samples and
enforces the owner's 10% time/RSS limits. One warmup per implementation is excluded.
GNU time reports the largest process RSS, not aggregate process-tree memory.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys


def compare(reference, candidate):
    failures = []
    baseline = reference[0]["quality"]
    minimum_open = min(s["quality"]["unconnected"] for s in reference)
    allowed_open_nets = set.intersection(*[
        set(s["quality"]["open_nets"]) for s in reference])
    for sample in [*reference, *candidate]:
        quality = sample["quality"]
        if quality["input_fingerprint"] != baseline["input_fingerprint"]:
            failures.append("inputs differ")
        if quality["process"] != "PASS":
            failures.append("routing process failed")
        if quality["validation"] != "PASS":
            failures.append("native validation failed")
        if quality["unconnected"] > minimum_open or set(quality["open_nets"]) - allowed_open_nets:
            failures.append("routing completion regressed")
        if quality["new_violations"]:
            failures.append("new native violations")
        if set(quality["preservation"]) != set(baseline["preservation"]):
            failures.append("preservation checks differ")
        if any(v != "PASS" for v in quality["preservation"].values()):
            failures.append("preservation failed")
    metrics = {}
    for key in ("wall_s", "max_rss_kib"):
        before = statistics.median(s[key] for s in reference)
        after = statistics.median(s[key] for s in candidate)
        ratio = after / before
        metrics[key] = {"reference_median": before, "candidate_median": after,
                        "ratio": ratio, "limit": 1.1}
        if ratio > 1.1:
            failures.append(f"{key} exceeds 10% regression limit")
    return {"passed": not failures, "failures": sorted(set(failures)), "metrics": metrics,
            "copper_geometry_equal": all(s["quality"]["copper"] == baseline["copper"]
                                         for s in [*reference, *candidate])}


def quality(result):
    return {
        "process": result["process"]["status"],
        "validation": result["validation"]["status"],
        "unconnected": result["routing"]["unconnected_total"],
        "open_nets": result["routing"]["unrouted_nets"],
        "preservation": result["validation"]["preservation"],
        "new_violations": result["validation"]["drc"]["new_violations"]["count"],
        "input_fingerprint": result["input"]["fingerprint"],
        "copper": result["evidence"]["candidate_copper_signature"]["sha256"],
    }


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
        process = subprocess.run(command, env=env, stdout=stdout, stderr=stderr,
                                 timeout=config.timeout, check=False)
    result = json.loads((directory / "run" / "result.json").read_text())
    if process.returncode != result["exit_code"]:
        raise RuntimeError(f"{side} CLI/result exit mismatch in {directory}")
    return {**json.loads((directory / "time.json").read_text()),
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
    report = {"schema": "kicad-route/engine-comparison/1", "project": str(args.project),
              "args": args.arg, "reference": reference, "candidate": candidate, "warmup": warmup,
              "measurement": "median full-CLI wall time and maximum individual-process RSS"}
    for index in range(args.repeats + 1):
        for side in (("reference", "candidate") if index % 2 == 0 else ("candidate", "reference")):
            print(f"{side}: {'warmup' if index == 0 else index}", flush=True)
            sample = run_sample(args, side, index)
            if index == 0:
                warmup[side] = sample
            else:
                (reference if side == "reference" else candidate).append(sample)
            (args.output / "samples.json").write_text(json.dumps(report, indent=2) + "\n")
    report["verdict"] = compare(reference, candidate)
    (args.output / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["verdict"], indent=2))
    return 0 if report["verdict"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
