#!/usr/bin/env python3
"""Acceptance finalizer preparation for the user-selected Archon final host.

Finalization requires the actual independently verified Archon protocol,
transferred original-to-retained inventory and host/container/resource proofs.
Local94696 remains supporting history; it cannot close the performance clause.
The exact earlier local verifier and proof bindings are SHA-indexed under
local-cpu-rate-verifier-history/ before these preparation changes.

No routing, builds, remote/tracker/Git writes or source edits are performed.
Outputs stay under ignored build/ until the parent stages the durable report.
--check-draft validates the exact14/66 original requirements only.
--check-resources diagnoses supporting local telemetry only.
"""
import argparse
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
Q = ROOT / "build/agent497-qualification"
FINAL = ("diff-pair-cpu-rate", "four-layer-cpu-rate", "multipoint-cpu-rate", "route-demo-cpu-rate")
REPORT = "reports/audit-20261020"
ACTIVE_FINAL_HOST = "archon"  # User-selected final host; local94696 is supporting history only.
MATRIX_SESSION = "8896"
CPU_PROTOCOL_SHA = "b830f37b0941b595ba2497e61d1d1c930663ce91044432eb9c91febf7936d225"
CPU_HARNESS_SHA = "1542b947ac50ee2cbe632f36bfed3a1ab735bb379aa10a8fdf389c8fa897ac2a"
QUIET_PROTOCOL_SHA = "546568312807eb4f4bd230c74c61e1da6c61f628f82286841a483ec842ad7aef"
QUIET_HARNESS_SHA = "392263b38f1f0391b35e0e56c5cd5e1150916b7437369e428abe8e961ffd5a66"
sys.dont_write_bytecode = True


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def artifact(path):
    return {"path": str(Path(path).resolve()), "sha256": sha(path)}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def verify_original_clauses(report):
    original = read(ROOT / "build/deep-analysis-20261020-agent486/issues-draft.json")
    require(len(original) == len(report["tickets"]) == 14, "expected exactly 14 original tickets")
    for number, (ticket, issue) in enumerate(zip(report["tickets"], original), 68):
        require(ticket["identifier"] == f"KICADROUTE-{number}", "ticket order/identity changed")
        require(ticket["name"] == issue["name"], f"ticket name changed: {number}")
        require([c["original_acceptance"] for c in ticket["criteria"]] == issue["acceptance"],
                f"original acceptance clauses changed: {number}")
    require(sum(len(t["criteria"]) for t in report["tickets"]) == 66, "expected exactly 66 original clauses")
    require(sha(ROOT / "build/deep-analysis-20261020-agent486/issues-draft.json") ==
            report["requirements"]["sha256"], "original issues bytes changed")


def comparison_module():
    # Judge retained samples with the exact preregistered comparison bytes,
    # independently of later landing or worktree removal. No runner is invoked.
    protocol = cpu_rate_profile()
    directory = Q/"cpu-rate-prerequisite-snapshots"
    rows = read(directory/"index.json")["files"]
    rows = [row for row in rows if row["original_path"] == protocol["driver"]]
    require(len(rows) == 1, "frozen comparison snapshot missing/ambiguous")
    path = directory/rows[0]["snapshot"]
    require(sha(path) == protocol["driver_sha256"], "frozen comparison bytes differ")
    spec = importlib.util.spec_from_file_location("retained_router_comparison", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def quiet_profile():
    path = Q / "quiet-admission-protocol.json"
    require(sha(path) == QUIET_PROTOCOL_SHA, "preregistered quiet protocol bytes changed")
    protocol = read(path)
    require(sha(protocol["harness"]) == protocol["harness_sha256"] == QUIET_HARNESS_SHA,
            "frozen quiet-admission harness changed")
    require(protocol["admission"] == {
        "interval_s": 5, "consecutive_intervals": 3, "minimum_idle_fraction": 0.8,
        "maximum_cpu_some_avg10_percent": 5, "maximum_io_full_avg10_percent": 1,
        "global_wait_budget_s": 1800}, "quiet admission policy differs from reviewed protocol")
    require(protocol["cpu_affinity"] == list(range(12)) and protocol["measured_repetitions"] == 5 and
            protocol["warmups_per_side"] == 1 and
            [Path(board["output"]).name for board in protocol["boards"]] == list(FINAL),
            "reviewed full-affinity four-board profile changed")
    return protocol


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def admission_assessment(before, after, interval, cpus):
    """Independently derive eligibility from raw counters, not reported booleans."""
    def cpu_rows(snapshot):
        rows = {}
        for line in snapshot["sources"]["/proc/stat"].splitlines():
            fields = line.split()
            if not fields or fields[0] not in {f"cpu{cpu}" for cpu in cpus}:
                continue
            cpu = int(fields[0][3:])
            if cpu in rows or len(fields) < 9:
                raise ValueError("duplicate/malformed selected CPU row")
            values = tuple(int(value) for value in fields[1:9])
            if any(value < 0 for value in values):
                raise ValueError("negative CPU counter")
            rows[cpu] = values
        if set(rows) != set(cpus):
            raise ValueError("missing selected CPU row")
        return rows

    def pressure(snapshot, source, kind):
        rows = [line.split() for line in snapshot["sources"][source].splitlines()
                if line.split() and line.split()[0] == kind]
        if len(rows) != 1:
            raise ValueError("missing/duplicate pressure row")
        fields = {}
        for part in rows[0][1:]:
            key, value = part.split("=", 1)
            if key in fields:
                raise ValueError("duplicate pressure field")
            fields[key] = value
        averages = [float(fields[key]) for key in ("avg10", "avg60", "avg300")]
        total = int(fields["total"])
        if total < 0 or any(not math.isfinite(value) or not 0 <= value <= 100 for value in averages):
            raise ValueError("invalid pressure value")
        return averages[0], total

    try:
        first, last = cpu_rows(before), cpu_rows(after)
        deltas = [tuple(b - a for a, b in zip(first[cpu], last[cpu])) for cpu in cpus]
        if any(any(value < 0 for value in row) or sum(row) <= 0 for row in deltas):
            raise ValueError("CPU accounting reset/nonpositive interval")
        idle = sum(row[3] for row in deltas) / sum(sum(row) for row in deltas)
        old_cpu, old_cpu_total = pressure(before, "/proc/pressure/cpu", "some")
        cpu, cpu_total = pressure(after, "/proc/pressure/cpu", "some")
        old_io, old_io_total = pressure(before, "/proc/pressure/io", "full")
        io, io_total = pressure(after, "/proc/pressure/io", "full")
        if cpu_total < old_cpu_total or io_total < old_io_total:
            raise ValueError("pressure accounting reset")
        return {"eligible": interval >= 5 and idle >= 0.8 and cpu <= 5 and io <= 1,
                "cpu_idle_fraction": idle, "cpu_some_avg10": cpu, "io_full_avg10": io}
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        # Unusable telemetry is legitimate waiting evidence, never admission.
        return {"eligible": False, "unusable_telemetry": True}


def verify_admission_journal(protocol, complete=True):
    root = Path(protocol["metadata_output"])
    expected = [f"{board['name']}/{side}-{index}" for board in protocol["boards"]
                for index in range(6)
                for side in (("reference", "candidate") if index % 2 == 0 else ("candidate", "reference"))]
    journal = Path(protocol["resource_interval_log"])
    content = journal.read_text()
    require(not content or content.endswith("\n"), "resource journal has an incomplete trailing event")
    events = [json.loads(line) for line in content.splitlines()]
    groups = {}
    previous_wait = 0.0
    for event in events:
        require(event.get("event") == "resource-interval", "journal contains expiry/unknown event; batch not qualified")
        label = event["label"]
        if label not in groups:
            require(len(groups) < len(expected) and label == expected[len(groups)], "missing/extra/reordered admission group")
            groups[label] = []
        require(label == next(reversed(groups)), "a previous admission group was reused")
        interval = event["actual_interval_s"]
        require(finite_number(interval) and interval > 0, "invalid actual admission interval")
        for snapshot in (event["before"], event["after"]):
            require(snapshot["actual_cpu_affinity"] == list(range(12)) and
                    finite_number(snapshot["monotonic_s"]), "actual admission affinity/clock differs from profile")
        require(event["after"]["monotonic_s"] > event["before"]["monotonic_s"], "nonpositive resource snapshot interval")
        require(finite_number(event["wait_used_s"]) and previous_wait <= event["wait_used_s"] <= 1800,
                "journal resets/exceeds global waiting budget")
        previous_wait = event["wait_used_s"]
        groups[label].append(event)
    if complete:
        require(list(groups) == expected, "journal does not cover exactly all48 scheduled admissions")
    metadata_paths = {label: root / "samples" / (label.replace("/", "-") + ".json") for label in expected}
    actual_metadata = {path.name for path in (root / "samples").glob("*.json")}
    require(actual_metadata <= {path.name for path in metadata_paths.values()}, "extra sample admission metadata")
    if complete:
        require(actual_metadata == {path.name for path in metadata_paths.values()}, "missing one or more of48 admission records")
    admitted = completed = unusable = after_errors = 0
    previous_total = 0.0
    previous_after = None
    for label, group in groups.items():
        consecutive = 0
        for index, event in enumerate(group):
            if index:
                require(event["before"] == group[index - 1]["after"], "resource intervals are not continuous within admission")
            elif previous_after is not None:
                require(event["before"]["monotonic_s"] >= previous_after,
                        "next admission overlaps the preceding CLI")
            assessment = admission_assessment(event["before"], event["after"], event["actual_interval_s"], list(range(12)))
            if event["after"]["monotonic_s"] - event["before"]["monotonic_s"] < 5:
                assessment["eligible"] = False
            unusable += bool(assessment.get("unusable_telemetry"))
            require(event.get("eligible") is assessment["eligible"], "reported admission differs from raw telemetry")
            consecutive = consecutive + 1 if assessment["eligible"] else 0
            require(event["consecutive_eligible_intervals"] == consecutive, "incorrect fresh/reset consecutive interval count")
            require(consecutive < 3 or index == len(group) - 1, "completed admission was continued/reused")
        path = metadata_paths[label]
        if not path.exists():
            require(not complete and label == next(reversed(groups)), "admitted sample metadata missing")
            continue
        require(consecutive == 3, "CLI launched without three fresh qualifying intervals")
        sample = read(path)
        require(sample["label"] == label and sample["before_launch"] == group[-1]["after"],
                "sample not bound to its exact terminal admission snapshot")
        total = sample["admission_wait_total_s"]
        require(finite_number(total) and previous_total <= total <= 1800 and total >= group[-1]["wait_used_s"],
                "sample admission budget differs from interval journal")
        require(total - previous_total + 1e-6 >= sum(event["actual_interval_s"] for event in group),
                "admission budget omits observed waiting intervals")
        raw_span = group[-1]["after"]["monotonic_s"] - group[0]["before"]["monotonic_s"]
        require(total - previous_total + 1e-6 >= raw_span,
                "admission budget omits raw snapshot elapsed waiting time")
        previous_total = total
        require(sample["actual_cpu_affinity_before"] == list(range(12)), "CLI launch affinity changed")
        admitted += 1
        if "driver_call_wall_s" not in sample:
            require(not complete and label == next(reversed(groups)), "missing completed CLI telemetry")
            continue
        require(finite_number(sample["driver_call_wall_s"]) and sample["driver_call_wall_s"] > 0 and
                sample["actual_cpu_affinity_after"] == list(range(12)), "invalid completed CLI telemetry/affinity")
        after = sample.get("after_completion")
        if after is None:
            require(bool(sample.get("after_observation_error")), "missing post-CLI observation/error")
            after_errors += 1
            previous_after = None
        else:
            require(after["actual_cpu_affinity"] == list(range(12)) and
                    after["monotonic_s"] >= sample["before_launch"]["monotonic_s"], "post-CLI resource control changed")
            previous_after = after["monotonic_s"]
        completed += 1
    if complete:
        require(admitted == completed == 48, "admission journal/sample population is incomplete")
        terminal = read(root / "matrix.json")
        require(math.isclose(terminal["admission_wait_s"], previous_total, abs_tol=1e-6) and
                720 <= previous_total <= 1800, "terminal waiting budget is misbound")
    return {"status": "verified" if complete else "partial_diagnostic_only", "journal": artifact(journal),
            "interval_events": len(events), "admitted_samples": admitted, "completed_sample_records": completed,
            "unusable_intervals_not_admitted": unusable, "post_observation_errors_diagnostic_only": after_errors,
            "last_admission_wait_s": previous_total, "last_observed_wait_s": previous_wait,
            "sample_metadata": [artifact(path) for path in metadata_paths.values() if path.exists()],
            "raw_eligibility_independently_recomputed": True,
            "fresh_streak_and_sequence_verified": True, "all_actual_affinities": list(range(12))}


def cpu_rate_profile():
    path = Q / "cpu-rate-protocol.json"
    require(sha(path) == CPU_PROTOCOL_SHA, "preregistered CPU-rate protocol bytes changed")
    protocol = read(path)
    require(sha(protocol["harness"]) == protocol["harness_sha256"] == CPU_HARNESS_SHA,
            "frozen CPU-rate harness changed")
    require(protocol["cpu_cap"] == 1 and protocol["calibration_selected"] is True and
            protocol["cpu_affinity"] == list(range(12)) and protocol["measured_repetitions"] == 5 and
            protocol["warmups_per_side"] == 1 and
            [Path(board["output"]).name for board in protocol["boards"]] == list(FINAL),
            "reviewed selected1CPU/full-affinity/four-board profile changed")
    directory = Q/"cpu-rate-prerequisite-snapshots"
    index = read(directory/"index.json")
    require(index["protocol_sha256"] == CPU_PROTOCOL_SHA and len(index["files"]) == len(protocol["hash_prerequisites"]),
            "frozen prerequisite snapshot index differs")
    for item, captured in zip(protocol["hash_prerequisites"], index["files"]):
        target = directory/captured["snapshot"]
        require(target.resolve().parent == directory.resolve() and captured["original_path"] == item["path"] and
                captured["sha256"] == item["sha256"] == sha(target),
                "frozen prerequisite snapshot differs: " + item["path"])
    return protocol


def parsed_cpuset(text):
    values = set()
    for field in text.strip().split(","):
        ends = field.split("-")
        require(len(ends) in (1, 2), "invalid effective cpuset")
        first, last = int(ends[0]), int(ends[-1])
        require(0 <= first <= last and last < 100000, "invalid effective cpuset range")
        for value in range(first, last + 1):
            require(value not in values, "duplicate effective cpuset CPU")
            values.add(value)
    return values


def raw_cpu_stat(text):
    values = {}
    for line in text.splitlines():
        fields = line.split()
        require(len(fields) == 2 and fields[0] not in values, "invalid/duplicate raw CPU accounting field")
        value = int(fields[1])
        require(value >= 0, "negative CPU accounting")
        values[fields[0]] = value
    require("usage_usec" in values, "missing CPU usage accounting")
    return values


def calibration_trial(trial, cap):
    require(trial.get("cap_cpus") == cap and trial.get("launcher_exit_code") == 0,
            "calibration cap/launcher mismatch")
    require(trial.get("status") in ("PASS", "INSUFFICIENT_SERVICE"), "calibration control/accounting error")
    controls = []
    for name in ("before", "work_before", "after"):
        snapshot = trial[name]
        group = snapshot["cgroup"]
        require(Path(group).name.startswith("kicadroute-agent497-calibration-") and
                snapshot["affinity"] == list(range(12)) and snapshot["nice"] == snapshot["scheduler"] == 0,
                "calibration execution controls differ")
        ancestry = snapshot["ancestry"]
        require(ancestry and Path(ancestry[0]["path"]) == Path("/sys/fs/cgroup") / group.lstrip("/"),
                "calibration membership/leaf mismatch")
        own = ancestry[0]
        require(own["cpu.max"] == f"{cap*100000} 100000" and own["cpu.max.burst"] == "0" and
                own["cpu.weight"] == "100", "calibration actual quota differs")
        for index, ancestor in enumerate(ancestry):
            if ancestor.get("cpuset.cpus.effective") is not None:
                require(parsed_cpuset(ancestor["cpuset.cpus.effective"]) == set(range(12)),
                        "calibration effective CPU set differs")
            if index:
                require(Path(ancestry[index-1]["path"]).parent == Path(ancestor["path"]),
                        "calibration ancestor chain is incomplete")
            quota = ancestor.get("cpu.max")
            if quota is not None:
                maximum, period = quota.split()
                require(int(period) > 0 and (maximum == "max" or int(maximum)/int(period) >= cap),
                        "calibration ancestor quota below chosen cap")
        require(ancestry[-1]["path"] == "/sys/fs/cgroup", "calibration ancestors omit root")
        controls.append((group, [{key: row.get(key) for key in
            ("path", "cpu.max", "cpu.max.burst", "cpu.weight", "cpuset.cpus.effective")} for row in ancestry]))
    require(controls[0] == controls[1] == controls[2], "calibration controls changed during trial")
    group = trial["before"]["cgroup"]
    ready, workers = trial["ready"], trial["workers"]
    pids = {worker["pid"] for worker in workers}
    require(len(ready) == len(workers) == len(pids) == 4 and {worker["pid"] for worker in ready} == pids,
            "calibration worker population changed")
    require(trial["parent_observed_worker_membership"] == {str(pid): group for pid in pids},
            "calibration parent observed escaped worker")
    for worker in ready + workers:
        require(worker["cgroup"] == group and worker["affinity"] == list(range(12)) and
                worker["nice"] == worker["scheduler"] == 0, "calibration worker escaped planned controls")
    for worker in workers:
        require(worker["cgroup_after"] == group and worker["affinity_after"] == list(range(12)) and
                worker["nice_after"] == worker["scheduler_after"] == 0 and
                finite_number(worker["cpu_s"]) and worker["cpu_s"] >= 3,
                "calibration completed worker lacks work/controls")
    wall = trial["wall_s"]
    require(finite_number(wall) and wall > 0, "invalid calibration wall time")
    delivered = sum(worker["cpu_s"] for worker in workers)
    require(wall + 1e-6 >= delivered/cap - .1, "calibration work too fast for actual cap")
    before_cpu = trial["work_before"]["ancestry"][0]["cpu.stat"]["usage_usec"]
    after_cpu = trial["after"]["ancestry"][0]["cpu.stat"]["usage_usec"]
    delta = after_cpu - before_cpu
    require(trial["cgroup_work_cpu_usec"] == delta and trial["accounting_allowance_s"] == .02 and
            delivered-.02 <= delta/1000000 <= cap*(wall+.1)+.02,
            "calibration cgroup accounting disagrees with delivered work or cap")
    fraction = delivered/(cap*wall)
    passed = fraction >= .90
    expected = {"passed": passed, "worker_cpu_s": delivered, "ideal_wall_s": delivered/cap,
                "measured_wall_s": wall, "minimum_cap_consistent_wall_s": delivered/cap-.1,
                "one_period_startup_allowance_s": .1, "service_fraction": fraction,
                "service_deficit_fraction": max(0., 1-fraction), "limit": .10}
    require(trial["verdict"] == expected and trial["status"] == ("PASS" if passed else "INSUFFICIENT_SERVICE"),
            "calibration verdict differs from independently computed service")
    return {"passed": passed, "service_fraction": fraction, "delivered_cpu_s": delivered, "wall_s": wall}


def verify_calibration(protocol):
    binding = protocol["calibration_selection"]
    require(sha(binding["path"]) == binding["sha256"], "selected calibration receipt changed")
    proof = read(binding["path"])
    require(proof["status"] == "PASS" and proof["selected_cap_cpus"] == protocol["cpu_cap"] == 1,
            "calibration did not select actual1CPU profile")
    artifacts = []
    for item in binding["all_trial_and_source_hashes"]:
        require(sha(item["path"]) == item["sha256"], "raw calibration/source hash mismatch")
        artifacts.append(artifact(item["path"]))
    require(set(proof["caps"]) == {"2", "1"}, "selected1CPU calibration lacks exact2then1 populations")
    trials = {}
    base = Path(binding["path"]).parent
    for cap in (2, 1):
        rows = proof["caps"][str(cap)]
        require(len(rows) == 3, "calibration lacks exactly3trials per cap")
        trials[str(cap)] = []
        for index, trial in enumerate(rows, 1):
            raw = read(base / f"{cap}cpu-trial{index}/trial.json")
            require(raw == {key: value for key, value in trial.items() if key != "launcher_exit_code"},
                    "selected calibration differs from raw trial receipt")
            trials[str(cap)].append(calibration_trial(trial, cap))
    require(not all(row["passed"] for row in trials["2"]) and all(row["passed"] for row in trials["1"]),
            "highest-adequate2then1 cap selection failed")
    return {"selection": artifact(binding["path"]), "selected_cap_cpus": 1, "trials": trials,
            "independent_work_membership_accounting_and_selection": True, "artifacts": artifacts,
            "claim": "Calibration-only service check; not a native sample admission/exclusion rule or future isolation guarantee"}


def rate_snapshot(snapshot, protocol):
    group = snapshot["group"]
    require(Path(group).name == protocol["scope_unit"] and snapshot["cpu_affinity"] == list(range(12)) and
            snapshot["nice"] == snapshot["scheduler"] == 0, "actual CPU-rate membership/priority/affinity differs")
    require(snapshot["proc_self_cgroup"].splitlines() == ["0::"+group], "raw driver membership differs from summary")
    require(finite_number(snapshot["monotonic_s"]), "invalid resource snapshot clock")
    leaf, mount = Path(snapshot["leaf"]), Path(snapshot["mount"])
    require(leaf.is_relative_to(mount) and str(leaf.relative_to(mount)) == group.lstrip("/"),
            "actual scope mount/leaf differs from raw membership")
    ancestry = snapshot["ancestors"]
    require(ancestry and ancestry[0]["path"] == str(leaf) and ancestry[-1]["path"] == str(mount),
            "actual scope ancestor population incomplete")
    for index, row in enumerate(ancestry):
        if index:
            require(Path(ancestry[index-1]["path"]).parent == Path(row["path"]), "scope ancestor chain omitted a parent")
        controls = row["controls"]
        effective = controls["cpuset.cpus.effective"]
        if "text" in effective:
            require(parsed_cpuset(effective["text"]) == set(range(12)), "scope/ancestor effective CPU set differs")
        else:
            require("No such file or directory" in effective.get("error", ""),
                    "unusable effective cpuset observation")
        quota = controls["cpu.max"].get("text")
        if quota is not None:
            maximum, period = quota.split()
            require(int(period) > 0 and (maximum == "max" or int(maximum)/int(period) >= protocol["cpu_cap"]),
                    "scope ancestor quota below selected cap")
        else:
            require(index == len(ancestry)-1 and "error" in controls["cpu.max"],
                    "nonroot quota observation missing")
        raw_cpu_stat(row["observations"]["cpu.stat"]["text"])
    own = ancestry[0]["controls"]
    require(own["cpu.max"]["text"].split() == ["100000", "100000"] and
            own["cpu.max.burst"]["text"].strip() == "0" and own["cpu.weight"]["text"].strip() == "100" and
            own["cpu.idle"]["text"].strip() == "0", "actual selected scope cap/period/burst/weight differs")
    for name in ("/proc/stat", "/proc/pressure/cpu", "/proc/pressure/io", "/proc/pressure/memory"):
        require(bool(snapshot["host_resources"][name]["text"]), "raw host resource observation missing")
    return {"group": group, "cpu_affinity": snapshot["cpu_affinity"], "nice": snapshot["nice"],
            "scheduler": snapshot["scheduler"],
            "ancestors": [{"path": row["path"], "controls": row["controls"]} for row in ancestry]}


def verify_membership(monitor, group, cli, memory_samples=None):
    rows = monitor["members"]
    require(type(monitor["sampler_calls"]) is int and monitor["sampler_calls"] > 0 and
            monitor["observed_births"] == len(rows) and rows, "missing actual descendant membership observations")
    require(memory_samples is None or monitor["sampler_calls"] == memory_samples,
            "membership sampler call count differs from raw RSS sample count")
    keys = {(row["pid"], row["birth_ticks"]) for row in rows}
    require(len(keys) == len(rows) and all(type(pid) is int and pid > 0 and type(birth) is int and birth > 0
            for pid, birth in keys), "duplicate/malformed descendant birth identities")
    roots = {row["gnu_time_root_pid"] for row in rows}
    require(len(roots) == 1, "membership observations mix native attempt roots")
    allowed = {"observed", "exited-before-membership-verification", "pid-reused-before-membership-verification"}
    for row in rows:
        require(row["status"] in allowed and finite_number(row["observed_monotonic_s"]),
                "membership read error or malformed status")
        if row["status"] == "observed":
            inside = row["group"] == group or row["group"].startswith(group+"/")
            require(inside and row.get("in_scope") is inside, "observed descendant escaped actual scope")
    observed = [row for row in rows if row["status"] == "observed"]
    require(any(row["pid"] in roots for row in observed), "GNU-time root membership not observed")
    require(any(str(cli) in row.get("argv", []) for row in observed), "actual CLI membership not observed")
    require(monitor["outside_scope"] == [] and monitor["read_errors"] == [] and
            monitor["exit_races"] == [row for row in rows if row["status"] != "observed"],
            "membership summary differs from raw birth observations")
    return {"observed_births": len(rows), "observed_in_scope": len(observed), "explicit_races": len(rows)-len(observed)}


def verify_cpu_rate_resources(protocol, complete=True):
    root = Path(protocol["metadata_output"])
    calibration = verify_calibration(protocol)
    initial = read(root/"initial-resources.json")
    expected = rate_snapshot(initial, protocol)
    metadata = list((root/"samples").glob("*.json"))
    labels = [(f"{board['name']}-{side}-{index}", board, side, index) for board in protocol["boards"]
              for index in range(6) for side in (("reference", "candidate") if index%2 == 0 else ("candidate", "reference"))]
    require({path.stem for path in metadata} <= {label for label, *_ in labels}, "extra resource sample record")
    if complete:
        require(len(metadata) == 48, "missing one or more of48resource records")
    previous = initial["monotonic_s"]
    observed = races = completed = 0
    proofs = []
    for label, board, side, index in labels:
        path = root/"samples"/(label+".json")
        if not path.exists():
            require(not complete, "missing raw sample resource record")
            continue
        sample = read(path)
        before = sample["before"]
        require(sample["label"] == label and rate_snapshot(before, protocol) == expected and
                before["monotonic_s"] >= previous, "resource sample population/parity/order differs")
        if "driver_call_wall_s" not in sample:
            require(not complete, "incomplete resource sample")
            continue
        after = sample["after"]
        require(rate_snapshot(after, protocol) == expected and after["monotonic_s"] > before["monotonic_s"] and
                finite_number(sample["driver_call_wall_s"]) and sample["driver_call_wall_s"] > 0 and
                after["monotonic_s"]-before["monotonic_s"] >= sample["driver_call_wall_s"] and
                sample["resource_violations"] == [], "resource controls changed or completion clock differs")
        previous = after["monotonic_s"]
        for first, last in zip(before["ancestors"], after["ancestors"]):
            old = raw_cpu_stat(first["observations"]["cpu.stat"]["text"])
            new = raw_cpu_stat(last["observations"]["cpu.stat"]["text"])
            require(set(old) == set(new) and all(new[key] >= old[key] for key in old),
                    "scope/ancestor CPU accounting reset during native attempt")
        raw = read(Path(board["output"])/"samples.json")
        native = raw["warmup"][side] if index == 0 else raw[side][index-1]
        membership_proof = verify_membership(sample["membership_monitor"], initial["group"], protocol[side+"_cli"],
                                              native["memory_sampling"]["samples"])
        for row in sample["membership_monitor"]["members"]:
            require(before["monotonic_s"] <= row["observed_monotonic_s"] <= after["monotonic_s"],
                    "descendant birth observation outside native attempt interval")
        observed += membership_proof["observed_in_scope"]
        races += membership_proof["explicit_races"]
        completed += 1
        proofs.append(artifact(path))
    require(not complete or completed == 48, "incomplete qualified resource population")
    quiet = Q/"quiet-admission-metadata/matrix.json"
    history = read(quiet)
    require(history["status"] == "INCONCLUSIVE_ADMISSION_EXPIRED" and history["completed_samples"] == 0,
            "quiet-host history changed or mischaracterized")
    return {"status": "verified" if complete else "partial_diagnostic_only", "calibration": calibration,
            "initial_resources": artifact(root/"initial-resources.json"), "sample_metadata": proofs,
            "completed_samples": completed, "observed_in_scope_births": observed, "explicit_birth_races": races,
            "actual_scope_control_parity_independently_verified": True,
            "actual_cli_and_gnu_time_root_membership_verified": True,
            "membership_limitation": "Once per frozenRSS tracked birth; reap/reuse races explicit; continuous migration tracing not claimed",
            "quiet_expiry": artifact(quiet), "quiet_expiry_status": history["status"],
            "native_utilization_and_pressure_are_diagnostic_only": True}


def verify_raw_timing(sample):
    path = Path(sample["result"]).parent.parent / "time.json"
    timing = read(path)
    for field in ("wall_s", "user_s", "system_s", "max_rss_kib"):
        require(finite_number(timing.get(field)) and finite_number(sample.get(field)) and
                sample[field] == timing[field], f"summary differs from raw GNU time {field}: {path}")
    return artifact(path)


def verify_sample_binding(sample, result, protocol, board, side):
    expected = protocol["retained_tool_versions"][side]["tool_identity"]
    require(sample["tool_identity"] == result["tool_identity"] == expected,
            "native actual tool/build identity differs from frozen profile")
    require(sample["backend"] == result["backend"] and sample["cli_sha256"] == next(item["sha256"] for item in protocol["hash_prerequisites"]
                if item["path"] == protocol[side+"_cli"]),
            "native backend/CLI summary differs from executed result or frozen source")
    if side == "candidate":
        execution = {key: value for key, value in expected["backend"].items() if key != "commit"}
        require(result["backend"]["execution_identity"] == execution,
                "native candidate execution identity differs from qualified runtime")
    project = Path(board["project"])
    # These four frozen input directories have no external/local library trees.
    # Route-demo also contains generate.py and a GUI .kicad_prl; those remain
    # prerequisite-pinned but are deliberately outside the native snapshot.
    relevant = {".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_wks",
                ".kicad_sym", ".lib", ".dcm", ".mod"}
    files = {str(Path(item["path"]).relative_to(project)): item["sha256"]
             for item in protocol["hash_prerequisites"] if Path(item["path"]).is_relative_to(project) and
             (Path(item["path"]).suffix in relevant or Path(item["path"]).name in ("sym-lib-table", "fp-lib-table"))}
    require(files and sample["reference_files"] == result["evidence"]["reference_files"] == files,
            "native input file population/hashes differ from frozen board")


def verify_local_cpu_rate_history(session, exit_code):
    require(session == MATRIX_SESSION and exit_code == 0,
            f"explicit observed terminal matrix session {MATRIX_SESSION} and exit0 required")
    protocol = cpu_rate_profile()
    metadata = Path(protocol["metadata_output"])
    terminal = read(metadata / "matrix.json")
    require(terminal.get("status") == "PASS" and terminal.get("all_comparisons_passed") is True and
            terminal.get("completed_samples") == 48 and len(terminal["boards"]) == 4 and
            terminal.get("cpu_cap") == 1 and terminal.get("resource_violations") == [] and
            terminal["protocol_sha256"] == sha(metadata / "protocol.json") == CPU_PROTOCOL_SHA and
            read(metadata / "protocol.json") == protocol, "CPU-rate matrix lacks complete terminal PASS")
    resources = verify_cpu_rate_resources(protocol)
    compare = comparison_module()
    slots = []
    for board, row, name in zip(protocol["boards"], terminal["boards"], FINAL):
        path = Q / name / "comparison.json"
        report, raw = read(path), read(path.with_name("samples.json"))
        require(row["name"] == board["name"] and row["output"] == board["output"] and
                row["status"] == "PASS" and row["completed_samples"] == 12,
                f"CPU-rate board population/status differs: {name}")
        require(report.get("schema") == "kicad-route/engine-comparison/2" and
                report.get("mode") == "migration-equivalence" and
                report["project"] == board["project"] and report["args"] == board["args"],
                f"unexpected comparison contract/project/arguments: {name}")
        require(all(report.get(k) == raw.get(k) for k in ("reference", "candidate", "warmup")),
                f"raw samples differ from comparison: {name}")
        require(len(raw["reference"]) == len(raw["candidate"]) == 5 and
                set(raw["warmup"]) == {"reference", "candidate"}, f"expected5+5+2 attempts: {name}")
        verdict = compare.compare(raw["reference"], raw["candidate"], raw["warmup"], report["mode"])
        require(verdict == report["verdict"] and verdict["passed"] is True and
                verdict == row["verdict"] and report["cpu_affinity"] == list(range(12)) and
                report["cpu_rate_protocol"] == str(metadata / "protocol.json") and
                verdict["copper_geometry_equal"] is True and
                all(v["limit"] == 1.10 for v in verdict["metrics"].values()),
                f"final unchanged quality/resource gates do not pass: {name}")
        samples = raw["reference"] + raw["candidate"] + list(raw["warmup"].values())
        expected_results = {str((path.parent / f"{side}-{index}" / "run/result.json").resolve())
                            for side in ("reference", "candidate") for index in range(6)}
        require({sample["result"] for sample in samples} == expected_results and
                {str(p.resolve()) for p in path.parent.glob("*/run/result.json")} == expected_results,
                f"missing/extra/dropped raw native attempt: {name}")
        for sample in samples:
            result = read(sample["result"])
            require(sample.get("exit_code") == 0 and compare.quality(result) == sample["quality"],
                    f"raw native result/summary mismatch: {sample['result']}")
            require(result.get("exit_code") == 0 and sample["quality"]["routing"] == "COMPLETE" and
                    sample["quality"]["unconnected"] == 0 and sample["quality"]["open_nets"] == [],
                    f"expected completed native result: {sample['result']}")
            verify_raw_timing(sample)
            side = Path(sample["result"]).parent.parent.name.rsplit("-", 1)[0]
            verify_sample_binding(sample, result, protocol, board, side)
        slots.append({"board": board["name"], "status": "passed",
                      "report": artifact(path), "raw_samples": artifact(path.with_name("samples.json")),
                      "reference_measured": 5, "candidate_measured": 5, "separate_warmups": 2,
                      "all_12_attempts_native_completion_preservation_uncensored": True,
                      "raw_native_qualities_recomputed": True, "raw_tool_build_and_input_identity_verified": True, "verdict": verdict,
                      "mode": report["mode"], "cpu_affinity": report.get("cpu_affinity"),
                      "reference_input_files": raw["reference"][0]["reference_files"],
                      "candidate_execution_identity": raw["candidate"][0]["backend"]["execution_identity"],
                      "durable_report": f"{REPORT}/performance/final/{name}/comparison.json"})
    return {"status": "passed", "gate_limit": 1.10, "mode": "migration-equivalence",
            "matrix_session": session, "observed_matrix_exit_code": exit_code,
            "matrix_terminal_receipt": artifact(metadata / "matrix.json"),
            "frozen_prerequisite_index": artifact(Q/"cpu-rate-prerequisite-snapshots/index.json"),
            "frozen_prerequisite_snapshots": [artifact(path) for path in sorted((Q/"cpu-rate-prerequisite-snapshots").iterdir()) if path.name != "index.json"],
            "matrix_terminal_log": artifact(Q / "cpu-rate-matrix.log"),
            "preregistered_protocol": artifact(Q / "cpu-rate-protocol.json"),
            "cpu_rate_verification": resources,
            "measurement_scope": "Selected aggregate1CPU quota, full affinity0..11, normal priority; conditional resource-profile qualification only, no unrestricted performance claim",
            "measured_repetitions_per_side": 5, "separate_warmups_per_board": 2,
            "completed_boards": 4, "complete_qualified_attempts": 48, "slots": slots}


def retained_artifact(store, row):
    path=store.resolve(row['source'])
    return {'path':str(path),'sha256':row['sha256'],'original_source':row['source'],
            'durable_path':f"{REPORT}/performance/archon/{row['retained']}"}


def verify_final_matrix(session,exit_code,index_path,index_sha256,verification_path,verification_sha256):
    sys.path.insert(0,str(Q))
    import archon_acceptance
    from acceptance_profile import EvidenceStore
    require(sha(Q/'archon_acceptance.py')=='1a6af758a3995ef47c38d203dd1eb922f1dab022a3331ba18a1d19c244aa992f' and
        sha(Q/'acceptance_profile.py')=='99b1078290d27f13401adbac060ac1b6758bcfc60dcd1f2fe68f7acd4feef3e7',
        'independently reviewed Archon verifier bytes changed')
    require(index_sha256=='19b5fca94da3fdf939bd13259fa28829573906acbae5fd694441e1b49669eb4e' and
        verification_sha256=='8c5e7b8fa6983546441cdd21ac005f9feccda84d1a2fec6b2e4db365668a82b3' and
        sha(verification_path)==verification_sha256,'actual independently observed Archon index/report binding differs')
    store=EvidenceStore(index_path,index_sha256)
    observed=read(verification_path);recomputed=archon_acceptance.verify_archon(store,session,exit_code)
    # Relocation changes only the local index pathname, never bytes or remote
    # identities. Keep the original verifier receipt intact in the report.
    normalized=copy.deepcopy(recomputed);normalized['evidence_index']['path']=observed['evidence_index']['path']
    require(normalized==observed,'Archon proof differs from independently revalidated retained evidence')
    protocol=store.read(archon_acceptance.PROTOCOL)
    matrix=copy.deepcopy(observed)
    matrix.update(status='passed',matrix_session=session,observed_matrix_exit_code=exit_code,
        independent_verification=artifact(verification_path),archon_index=artifact(index_path),
        matrix_terminal_receipt=retained_artifact(store,observed['matrix_terminal']),
        matrix_terminal_log=retained_artifact(store,store.artifact(protocol['outer_log'])),
        preregistered_protocol=retained_artifact(store,observed['protocol']),
        measurement_scope=observed['scope'])
    for slot in matrix['slots']:
        for key in ('report','raw_samples'):slot[key]=retained_artifact(store,slot[key])
        slot.update(reference_measured=5,candidate_measured=5,separate_warmups=2,
            all_12_attempts_native_completion_preservation_uncensored=True,raw_native_qualities_recomputed=True,
            cpu_affinity=protocol['cpu_affinity'],mode='migration-equivalence',durable_report=slot['report']['durable_path'])
    matrix['archon_resource_artifacts']=[retained_artifact(store,row) for row in
        [observed['resources']['host_before'],observed['resources']['host_after'],observed['resources']['initial'],
         *observed['resources']['sample_metadata'],*observed['transport']['artifacts'].values()]]
    return matrix


def verify_landed_runtime(receipt_path, doctor_path, draft):
    receipt_path, doctor_path = receipt_path.resolve(), doctor_path.resolve()
    receipt, doctor = read(receipt_path), read(doctor_path)
    require(receipt.get("status") == "PASS" and receipt.get("gates") and
            all(g.get("passed") is True for g in receipt["gates"]), "runtime receipt is not all-gates PASS")
    require(Path(receipt["checkout"]).resolve() == ROOT and
            git("branch", "--show-current") == "main", "runtime/finalizer must target landed main")
    require(not git("status", "--porcelain", "--untracked-files=no"), "landed tracked source is dirty")
    head = git("rev-parse", "HEAD")
    require(subprocess.run(["git", "merge-base", "--is-ancestor", receipt["git_head"], head],
                           cwd=ROOT, capture_output=True).returncode == 0,
            "runtime was not proved from this landed main history")
    require(all(sha(ROOT / name) == digest for name, digest in draft["source_file_sha256"].items()),
            "landed source/test bytes differ from qualified acceptance bindings")
    manifest_path = ROOT / "manifest.json"
    manifest = read(manifest_path)
    require(manifest == read(receipt_path.parent / "installed-manifest.json"),
            "default manifest changed after native installation proof")
    installed = receipt["installed_identity"]
    require(manifest["krt"] == installed, "selected generation differs from runtime receipt")
    generation = (ROOT / installed["generation"]).resolve()
    require(generation.is_relative_to(ROOT / ".router-generations") and
            generation == Path(receipt["generation"]).resolve(), "receipt is not checkout-default generation")
    require(sha(ROOT / installed["extension_module"]["path"]) == installed["extension_module"]["sha256"],
            "executed extension bytes changed after installation proof")
    repeat = receipt["determinism"]
    require(repeat.get("reproducible") is True and repeat.get("same", {}).get("execution_build") is True and
            repeat.get("build_id") == manifest["determinism"]["krt"]["build_id"] and
            len(receipt["executed_runs"]) == 2, "fresh default runtime lacks recorded successful repeat proof")
    qualified = draft["observed_prepared_runtime"]
    require(installed["source_sha256"] == qualified["source_sha256"] and
            installed["venv"]["python_version"] == qualified["venv_python_version"] and
            installed["venv"]["packages_sha256"] == qualified["packages_sha256"],
            "default source/interpreter/dependencies differ from qualified candidate")
    require(doctor.get("kind") == "doctor" and doctor.get("status") == "PASS" and
            doctor.get("exit_code") == 0 and doctor.get("required_failed") == [], "doctor is not PASS")
    require(Path(doctor["state_manifest"]["path"]).resolve() == manifest_path and
            doctor["state_manifest"]["sha256"] == sha(manifest_path), "doctor is not bound to default manifest")
    checks = {c["check"]: c["status"] for c in doctor["checks"]}
    for name in ("KRT routing venv", "KRT Rust router module", "KRT startup checks",
                 "KRT controls used by the adapter", "state manifest",
                 "state manifest matches the files on disk", "KRT determinism verified for this build"):
        require(checks.get(name) == "PASS", f"required default runtime doctor proof missing: {name}")
    require(doctor_path.is_relative_to(receipt_path.parent), "doctor must be retained inside landed runtime evidence")
    prefix = f"{REPORT}/runtime/{receipt_path.parent.name}"
    return {"status": "PASS", "landed_git_head": head, "runtime_proved_git_head": receipt["git_head"],
            "source_and_test_hash_parity": True, "receipt": artifact(receipt_path),
            "doctor": artifact(doctor_path), "selected_manifest": artifact(manifest_path),
            "installed_manifest_snapshot": artifact(receipt_path.parent / "installed-manifest.json"),
            "installed_identity": installed, "fresh_native_repeats": 2,
            "recorded_build_id": repeat["build_id"], "doctor_matches_default_manifest": True,
            "durable_receipt": prefix + "/receipt.json",
            "durable_doctor": prefix + "/" + str(doctor_path.relative_to(receipt_path.parent))}


def observe_current_cache_and_ledger(matrix):
    sys.path.insert(0,str(Q))
    from acceptance_profile import EvidenceStore
    from pathlib import PurePosixPath
    store=EvidenceStore(matrix['archon_index']['path'],matrix['archon_index']['sha256'])
    slot=read(matrix['slots'][0]['raw_samples']['path'])
    sample=slot['candidate'][0];result=store.read(sample['result']);baseline=result['evidence']['baseline'];identity=baseline['identity']
    def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()
    key=digest(identity);ledger=PurePosixPath(result['ledger']['path'])
    generation=ledger.parent/'.cache/baseline'/key/baseline['generation'];receipt_path=str(generation/'generation.json');receipt=store.read(receipt_path)
    require(receipt['identity']==identity and receipt['generation']==baseline['generation'] and
        set(receipt['files'])=={'baseline-drc.json','baseline-info.json','baseline-pour.json'},'Archon baseline generation/identity population differs')
    require(all(store.artifact(str(generation/name))['sha256']==value for name,value in receipt['files'].items()),
        'Archon baseline payload bytes differ from complete generation receipt')
    require(all(sha(ROOT/'src/kicad_route'/name)==value for name,value in identity['grading'].items()) and
        all(sha(ROOT/'schemas'/name)==value for name,value in identity['result_schemas'].items()),
        'Archon grader/schema identities differ from landed qualified source')
    protocol=store.read('/evidence/archon-protocol.json');native=store.read(protocol['candidate_state']+'/manifest.json')['kicad']
    require(identity['kicad_cli']==native['kicad_cli'] and identity['pcbnew']==native['pcbnew_build_version'],
        'Archon baseline native identity differs from its own captured grading manifest')
    normalized={name:(sorted(value) if name in ('net_patterns','netclasses') else value)
        for name,value in sorted(result['settings'].items()) if not(value is None or value=={} and name=='backend_env')}
    attempt=digest({'input':result['input']['fingerprint'],'backend':result['backend']['name'],'settings':normalized})
    fingerprint=digest({'attempt':attempt,'backend_version':{'execution_identity':result['backend']['execution_identity']}})
    rows=[json.loads(line) for line in store.resolve(str(ledger)).read_text().splitlines()]
    matches=[row for row in rows if row.get('fingerprint')==fingerprint]
    require(fingerprint==result['ledger']['fingerprint'] and matches and all(row['attempt_key']==attempt for row in matches),
        'Archon executed-build ledger reconstruction differs')
    def item(source):return retained_artifact(store,store.artifact(source))
    cache={'run_result':item(sample['result']),'logical_key_sha256':key,'identity':identity,'generation':baseline['generation'],
        'receipt':item(receipt_path),'identity_matches':True,'payload_hashes_verified':{name:True for name in receipt['files']},
        'all_grading_hashes_match_landed_source':True,'all_schema_hashes_match_landed_source':True,
        'native_identity_matches_captured_archon_grading_manifest':True,'cache_population_expected_payloads':3,'cache_population_observed_payloads':3,
        'scope':'Current user-selected Archon candidate; complete native baseline cache proof retained through strict original-to-retained inventory.'}
    ledger_proof={'run_result':item(sample['result']),'ledger':item(str(ledger)),'execution_identity':result['backend']['execution_identity'],
        'normalized_settings':normalized,'computed_attempt_key':attempt,'computed_fingerprint':fingerprint,
        'recorded_fingerprint':result['ledger']['fingerprint'],'matches':True,'matching_ledger_rows':len(matches),
        'human_protection_proof':{'nodeid':'tests/test_evidence_integrity.py::test_native_refill_and_human_ledger_reconstruction',
            'junit_run_indices':[0,1],'requires_route_and_revalidation_exit0':True}}
    return cache,ledger_proof



def final_comments(comments,matrix,runtime,test_count):
    closure=copy.deepcopy(comments)
    closure.update(schema='kicad-route/closure-comments/1',source_commit=runtime['landed_git_head'],
        finalization_required=['Commit and push the verified durable report before posting.'],tracker_writes=False)
    for ident,comment in closure['comments'].items():
        implementation=comment['body'].split('\n\n',1)[0].removeprefix('Implemented: ')
        implementation=implementation.split(' FINAL PERFORMANCE MATRIX PENDING:',1)[0].strip()
        implementation=implementation.replace('at1.10','at 1.10').replace('Nativebundle','Native bundle')
        if ident=='KICADROUTE-73':
            implementation=('Preserves text, netless/net-tied graphics and footprint copper, respects native clearance in the narrow passage, '
                'and explicitly refuses unsupported shapes. On user-selected Archon, all four boards and 48 native attempts pass copper/native quality '
                'and the unchanged 1.10 full-CLI wall and dual-memory limits.')
        paths=[]
        for path in comment['intended_proof_paths']:
            for board in ('diff-pair','four-layer','multipoint','route-demo'):
                for profile in ('quiet-admission','cpu-rate','env-reuse','prepared'):
                    path=path.replace(f'performance/final/{board}-{profile}/comparison.json',
                        f'performance/archon/{board}-archon/comparison.json')
            if path not in paths:paths.append(path)
        if ident in ('KICADROUTE-73','KICADROUTE-81'):
            proof=f'{REPORT}/provenance/archon-final-verification.json'
            if proof not in paths:paths.append(proof)
        if ident in ('KICADROUTE-75','KICADROUTE-76','KICADROUTE-79'):
            paths += [runtime['durable_receipt'],runtime['durable_doctor']]
            implementation+=' The landed default runtime passed two native repeats with its executed build recorded, followed by doctor.'
        paths=list(dict.fromkeys([*paths,f'{REPORT}/final-acceptance.json']))
        comment.update(status='ready_after_durable_report_commit',acceptance='met',intended_proof_paths=paths)
        comment.pop('draft_acceptance',None)
        comment['body']=(implementation+'\n\nProof: '+', '.join(paths)+'.\n\n'
            f'Source: {runtime["landed_git_head"]}. Passing observations cover all {test_count} current test nodes across five invocations. '
            'The original broad invocation had 686 passed and 2 failed; corrective receipts passed 193/193, 25/25, 24/24 and 113/113. '
            'This is not one green final full-suite invocation.')
        require(not any(marker in comment['body'].lower() for marker in ('pending','draft','parent-owned','trunk/runtime/report gates','quiet-admission','cpu-rate')),
            f'obsolete closure wording remains: {ident}')
    return closure


def finalize(draft, comments, matrix, runtime):
    require(matrix.get("host") == ACTIVE_FINAL_HOST == "archon",
            "User-selected Archon final performance proof is required; local studies cannot close73")
    report = copy.deepcopy(draft)
    report.update(schema="kicad-route/final-acceptance/1", status="acceptance_verified_pending_durable_report",
                  audited_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                  qualified_source_commit=draft["source_commit"], source_commit=runtime["landed_git_head"],
                  source_checkout=str(ROOT), source_clean_at_snapshot=True,
                  final_performance_matrix=matrix, landed_default_runtime=runtime)
    cache, ledger = observe_current_cache_and_ledger(matrix)
    report.update(observed_cache_generation=cache, observed_ledger_reconstruction=ledger,
                  qualified_performance_runtime=matrix["tool_identity_verification"]["prepared_runtimes"]["candidate"])
    local_history=read(Q/"cpu-rate-metadata/matrix.json")
    report["superseded_local_cpu_rate_final_matrix"].update(status=local_history["status"],
        complete_qualified_attempts=local_history["completed_samples"],completed_boards=len(local_history["boards"]),
        qualification_role="Supporting local profile only; user-selected Archon is the final performance authority.")
    report["summary"] = {"tickets_met": 14, "tickets_pending_final_matrix": 0,
                         "clauses_met": 66, "clauses_pending_final_matrix": 0,
                         "pending": "Durable report staging/commit and tracker updates remain parent-owned."}
    for ticket in report["tickets"]:
        ticket["status"] = "met"
        for clause in ticket["criteria"]:
            clause["status"] = "met"
        if ticket["identifier"] == "KICADROUTE-73":
            clause = ticket["criteria"][4]
            clause["finding"] = ("On the user-selected Archon same-container full16CPU profile, all four predeclared boards pass five measured repetitions per side and separate "
                                 "warmups (48 native attempts), with COMPLETE routing, native validation, preservation, "
                                 "pair success and uncensored timing. Copper geometry agrees and wall/GNU RSS/" 
                                 "sampled process-tree RSS ratios pass the unchanged 1.10 limits. Prior failed runs remain retained.")
            clause["artifacts"] = [slot["report"] for slot in matrix["slots"]] + [matrix["matrix_terminal_log"]]
    report["remaining_integration_gates"] = ["Parent must stage/verify and commit/push the durable report before posting tracker closure comments."]
    added = [matrix["matrix_terminal_log"], matrix["matrix_terminal_receipt"], matrix["preregistered_protocol"],
             matrix["independent_verification"], matrix["archon_index"], runtime["receipt"], runtime["doctor"],
             runtime["installed_manifest_snapshot"], cache["receipt"], cache["run_result"], ledger["ledger"]]
    added += matrix["archon_resource_artifacts"]
    added += [s[k] for s in matrix["slots"] for k in ("report", "raw_samples")]
    for entry in added:
        if entry["path"] not in {a["path"] for a in report["artifact_sha256"]}:
            report["artifact_sha256"].append(entry)
    verify_original_clauses(report)
    closure=final_comments(comments,matrix,runtime,report["test_evidence"]["current_collected_cases"])
    rows = [f"| {t['identifier']} | Met | {len(t['criteria'])} |" for t in report["tickets"]]
    metrics = [f"| {s['board']} | " + " | ".join(f"{s['verdict']['metrics'][k]['ratio']:.8f}" for k in
               ("wall_s", "max_rss_kib", "process_tree_peak_rss_kib")) + " |" for s in matrix["slots"]]
    count = report["test_evidence"]["current_collected_cases"]
    md = (f"Acceptance verified for all 66 original clauses across KICADROUTE-68–81. Landed source `{runtime['landed_git_head']}` "
          f"matches the qualified source/test hashes. The selected default runtime passed two native repeats and doctor; "
          f"its receipt and actual build identities are retained in `final-acceptance.json`. The complete retained report binds source, native runtime and item-specific evidence.\n\n"
          "The broad invocation ended **686 passed, 2 failed (688 cases)**. Corrective reports contain **193/193, 25/25, 24/24 and 113/113** passing cases. "
          f"`tests/coverage-union.json` accounts for all **{count} currently collected node IDs** through passing observations across invocations. "
          "This is not one green final full-suite invocation. The original failures remain retained.\n\n"
          "| Ticket | Acceptance | Original clauses |\n| --- | --- | --- |\n" + "\n".join(rows) + "\n\n"
          "All four user-selected Archon comparisons pass five measured repetitions per side plus separate warmups: **48 native attempts**, copper equality, "
          "native completion/preservation/pair success and uncensored timing. Each wall/GNU RSS/process-tree RSS gate remains **1.10**.\n\n"
          "| Board | Wall ratio | GNU RSS ratio | Process-tree RSS ratio |\n| --- | --- | --- | --- |\n" + "\n".join(metrics) + "\n\n"
          "Retained failures remain failures: original route-demo wall ratio 1.68257261, multipoint-stable 1.45826772 and cache-reuse 1.17098446. "
          "Prepared differential-pair 1.11040905 and environment-reuse differential-pair 1.23704867 also remain failed. "
          "The final matrix does not erase those observations. No claim is made that cache reuse alone explained the contention gap.\n\n"
          "The final profile runs both frozen source/runtime builds in the same Archon container, with all16available CPUs, normal priority and no task-imposed quota/admission. "
          "Actual container/image/initPID, hidden host ancestors and per-attempt container controls/membership are independently verified before/after. "
          "Native tools match KiCad10.0.7/Python3.12.3; both scientific environments contain identical pinned packages and1619prepared bytecode files. "
          "Original remote locators resolve through the externally hash-bound retained inventory. Exact HOME/cache omissions are explicit transport metadata; all required native proof bytes remain retained. "
          "No completed attempt is retried, dropped or excluded for pressure/utilization. This is an Archon qualification, with no cross-host binary-equality claim. "
          "Earlier local outcomes remain supporting history, including the complete local one-CPU study and inconclusive quiet-host expiry.\n\n"
          "Publication verification retains the original full-scope failure alongside selected corrective evidence; all included record hashes resolve. "
          "The native bundle preserves source receipts/candidate. Final candidate digests, native changed-refill and human-ledger reconstruction, "
          "staged runtime identity, cache/ledger conflict rules and discovery populations are mapped clause by clause in the JSON. "
          "No global owner-board corpus or full weighted-search/churn coverage claim is made.\n")
    return report, closure, md


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-draft", action="store_true")
    parser.add_argument("--check-resources", action="store_true",
                        help="read-only current CPU-rate telemetry diagnostic; never finalizes acceptance")
    parser.add_argument("--matrix-session")
    parser.add_argument("--matrix-exit-code", type=int)
    parser.add_argument("--archon-index", type=Path)
    parser.add_argument("--archon-index-sha256")
    parser.add_argument("--archon-verification", type=Path)
    parser.add_argument("--archon-verification-sha256")
    parser.add_argument("--runtime-receipt", type=Path)
    parser.add_argument("--doctor", type=Path)
    args = parser.parse_args()
    draft = read(Q / "final-acceptance-draft.json")
    verify_original_clauses(draft)
    if args.check_draft:
        print(json.dumps({"original_tickets": 14, "original_clauses": 66, "exact_original_strings_preserved": True}))
        return
    if args.check_resources:
        print(json.dumps(verify_cpu_rate_resources(cpu_rate_profile(), complete=False), indent=2))
        return
    require(all((args.archon_index,args.archon_index_sha256,args.archon_verification,args.archon_verification_sha256)),
            "actual independently verified Archon index/report bindings required")
    require(args.runtime_receipt is not None and args.doctor is not None, "runtime receipt and doctor paths required")
    matrix = verify_final_matrix(args.matrix_session,args.matrix_exit_code,args.archon_index,args.archon_index_sha256,
                                 args.archon_verification,args.archon_verification_sha256)
    runtime = verify_landed_runtime(args.runtime_receipt, args.doctor, draft)
    report, comments, prose = finalize(draft, read(Q / "closure-comments-draft.json"), matrix, runtime)
    for name, value in (("final-acceptance.json", report), ("closure-comments-final.json", comments)):
        (Q / name).write_text(json.dumps(value, indent=2) + "\n")
    (Q / "final-acceptance.md").write_text(prose)
    print(json.dumps({"acceptance": artifact(Q / "final-acceptance.json"),
                      "report": artifact(Q / "final-acceptance.md"),
                      "comments": artifact(Q / "closure-comments-final.json"),
                      "tickets_met": 14, "original_clauses_met": 66, "matrix_attempts": 48,
                      "landed_git_head": runtime["landed_git_head"], "tracker_writes": False}, indent=2))


if __name__ == "__main__":
    main()
