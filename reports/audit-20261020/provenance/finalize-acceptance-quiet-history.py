#!/usr/bin/env python3
"""Finalize retained acceptance after the terminal matrix and landed runtime.

No routing, builds, tracker writes, Git writes or source edits are performed.
Outputs stay under ignored build/ until the parent stages the durable report.

    python3 build/agent497-qualification/finalize-acceptance.py --check-draft
    python3 build/agent497-qualification/finalize-acceptance.py \
        --matrix-session 18111 --matrix-exit-code 0 \
        --runtime-receipt build/agent497-qualification/landed-main-.../receipt.json \
        --doctor build/agent497-qualification/landed-main-.../doctor/doctor/doctor.json
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
FINAL = ("diff-pair-quiet-admission", "four-layer-quiet-admission", "multipoint-quiet-admission", "route-demo-quiet-admission")
REPORT = "reports/audit-20261020"
MATRIX_SESSION = "18111"
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
    # Importing this stdlib-only script defines pure comparison functions; its
    # benchmark runner remains behind __main__ and is never invoked here.
    spec = importlib.util.spec_from_file_location("retained_router_comparison", ROOT / "scripts/compare_router.py")
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


def verify_raw_timing(sample):
    path = Path(sample["result"]).parent.parent / "time.json"
    timing = read(path)
    for field in ("wall_s", "user_s", "system_s", "max_rss_kib"):
        require(finite_number(timing.get(field)) and finite_number(sample.get(field)) and
                sample[field] == timing[field], f"summary differs from raw GNU time {field}: {path}")
    return artifact(path)


def verify_final_matrix(session, exit_code):
    require(session == MATRIX_SESSION and exit_code == 0,
            f"explicit observed terminal matrix session {MATRIX_SESSION} and exit0 required")
    protocol = quiet_profile()
    metadata = Path(protocol["metadata_output"])
    terminal = read(metadata / "matrix.json")
    require(terminal.get("status") == "PASS" and terminal.get("all_comparisons_passed") is True and
            terminal.get("completed_samples") == 48 and len(terminal["boards"]) == 4 and
            terminal["protocol_sha256"] == sha(metadata / "protocol.json") == QUIET_PROTOCOL_SHA and
            read(metadata / "protocol.json") == protocol, "quiet matrix lacks complete terminal PASS")
    admission = verify_admission_journal(protocol)
    compare = comparison_module()
    slots = []
    for board, row, name in zip(protocol["boards"], terminal["boards"], FINAL):
        path = Q / name / "comparison.json"
        report, raw = read(path), read(path.with_name("samples.json"))
        require(row["name"] == board["name"] and row["output"] == board["output"] and
                row["status"] == "PASS" and row["completed_samples"] == 12,
                f"quiet board population/status differs: {name}")
        require(report.get("schema") == "kicad-route/engine-comparison/2" and
                report.get("mode") == "migration-equivalence", f"unexpected comparison contract: {name}")
        require(all(report.get(k) == raw.get(k) for k in ("reference", "candidate", "warmup")),
                f"raw samples differ from comparison: {name}")
        require(len(raw["reference"]) == len(raw["candidate"]) == 5 and
                set(raw["warmup"]) == {"reference", "candidate"}, f"expected5+5+2 attempts: {name}")
        verdict = compare.compare(raw["reference"], raw["candidate"], raw["warmup"], report["mode"])
        require(verdict == report["verdict"] and verdict["passed"] is True and
                verdict == row["verdict"] and report["cpu_affinity"] == list(range(12)) and
                report["quiet_admission_protocol"] == str(metadata / "protocol.json") and
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
        slots.append({"board": board["name"], "status": "passed",
                      "report": artifact(path), "raw_samples": artifact(path.with_name("samples.json")),
                      "reference_measured": 5, "candidate_measured": 5, "separate_warmups": 2,
                      "all_12_attempts_native_completion_preservation_uncensored": True,
                      "raw_native_qualities_recomputed": True, "verdict": verdict,
                      "mode": report["mode"], "cpu_affinity": report.get("cpu_affinity"),
                      "reference_input_files": raw["reference"][0]["reference_files"],
                      "candidate_execution_identity": raw["candidate"][0]["backend"]["execution_identity"],
                      "durable_report": f"{REPORT}/performance/final/{name}/comparison.json"})
    return {"status": "passed", "gate_limit": 1.10, "mode": "migration-equivalence",
            "matrix_session": session, "observed_matrix_exit_code": exit_code,
            "matrix_terminal_receipt": artifact(metadata / "matrix.json"),
            "matrix_terminal_log": artifact(Q / "quiet-admission-matrix.log"),
            "preregistered_protocol": artifact(Q / "quiet-admission-protocol.json"),
            "admission_verification": admission,
            "measured_repetitions_per_side": 5, "separate_warmups_per_board": 2,
            "completed_boards": 4, "complete_qualified_attempts": 48, "slots": slots}


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
    sample = read(matrix["slots"][0]["raw_samples"]["path"])["candidate"][0]
    path = Path(sample["result"])
    result = read(path)
    baseline = result["evidence"]["baseline"]
    identity = baseline["identity"]

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()

    key = digest(identity)
    ledger_path = Path(result["ledger"]["path"])
    generation = ledger_path.parent / ".cache/baseline" / key / baseline["generation"]
    receipt = read(generation / "generation.json")
    require(receipt["identity"] == identity and receipt["generation"] == baseline["generation"],
            "current candidate baseline generation is misbound")
    require(set(receipt["files"]) == {"baseline-drc.json", "baseline-info.json", "baseline-pour.json"} and
            all(sha(generation / name) == value for name, value in receipt["files"].items()),
            "current candidate cache payloads differ from complete generation receipt")
    require(all(sha(ROOT / "src/kicad_route" / name) == value for name, value in identity["grading"].items()) and
            all(sha(ROOT / "schemas" / name) == value for name, value in identity["result_schemas"].items()),
            "current baseline grader/schema identity differs from landed source")
    current_native = read(ROOT / "manifest.json")["kicad"]
    require(identity["kicad_cli"] == current_native["kicad_cli"] and
            identity["pcbnew"] == current_native["pcbnew_build_version"],
            "native grader changed between final matrix and default runtime proof")
    normalized = {name: (sorted(value) if name in ("net_patterns", "netclasses") else value)
                  for name, value in sorted(result["settings"].items())
                  if not (value is None or value == {} and name == "backend_env")}
    attempt = digest({"input": result["input"]["fingerprint"], "backend": result["backend"]["name"],
                      "settings": normalized})
    fingerprint = digest({"attempt": attempt, "backend_version": {
        "execution_identity": result["backend"]["execution_identity"]}})
    rows = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    matches = [row for row in rows if row.get("fingerprint") == fingerprint]
    require(fingerprint == result["ledger"]["fingerprint"] and matches and
            all(row["attempt_key"] == attempt for row in matches), "current ledger reconstruction differs")
    cache = {"run_result": artifact(path), "logical_key_sha256": key, "identity": identity,
             "generation": baseline["generation"], "receipt": artifact(generation / "generation.json"),
             "identity_matches": True, "payload_hashes_verified": {name: True for name in receipt["files"]},
             "all_grading_hashes_match_current_primary": True, "all_schema_hashes_match_current_primary": True,
             "native_identity_matches_landed_default_runtime": True,
             "cache_population_expected_payloads": 3, "cache_population_observed_payloads": 3,
             "scope": "Current quiet-admission candidate; immutable cache bytes remain external to the retained report."}
    ledger = {"run_result": artifact(path), "ledger": artifact(ledger_path),
              "execution_identity": result["backend"]["execution_identity"], "normalized_settings": normalized,
              "computed_attempt_key": attempt, "computed_fingerprint": fingerprint,
              "recorded_fingerprint": result["ledger"]["fingerprint"], "matches": True,
              "matching_ledger_rows": len(matches),
              "human_protection_proof": {"nodeid": "tests/test_evidence_integrity.py::test_native_refill_and_human_ledger_reconstruction",
                                         "junit_run_indices": [0, 1], "requires_route_and_revalidation_exit0": True}}
    return cache, ledger


def finalize(draft, comments, matrix, runtime):
    report = copy.deepcopy(draft)
    report.update(schema="kicad-route/final-acceptance/1", status="acceptance_verified_pending_durable_report",
                  audited_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                  qualified_source_commit=draft["source_commit"], source_commit=runtime["landed_git_head"],
                  source_checkout=str(ROOT), source_clean_at_snapshot=True,
                  final_performance_matrix=matrix, landed_default_runtime=runtime)
    cache, ledger = observe_current_cache_and_ledger(matrix)
    report.update(observed_cache_generation=cache, observed_ledger_reconstruction=ledger)
    report["summary"] = {"tickets_met": 14, "tickets_pending_final_matrix": 0,
                         "clauses_met": 66, "clauses_pending_final_matrix": 0,
                         "pending": "Durable report staging/commit and tracker updates remain parent-owned."}
    for ticket in report["tickets"]:
        ticket["status"] = "met"
        for clause in ticket["criteria"]:
            clause["status"] = "met"
        if ticket["identifier"] == "KICADROUTE-73":
            clause = ticket["criteria"][4]
            clause["finding"] = ("All four predeclared boards pass five measured repetitions per side and separate "
                                 "warmups (48 native attempts), with COMPLETE routing, native validation, preservation, "
                                 "pair success and uncensored timing. Copper geometry agrees and wall/GNU RSS/" 
                                 "sampled process-tree RSS ratios pass the unchanged 1.10 limits. Prior failed runs remain retained.")
            clause["artifacts"] = [slot["report"] for slot in matrix["slots"]] + [matrix["matrix_terminal_log"]]
    report["remaining_integration_gates"] = ["Parent must stage/verify and commit/push the durable report before posting tracker closure comments."]
    added = [matrix["matrix_terminal_log"], matrix["matrix_terminal_receipt"], matrix["preregistered_protocol"],
             matrix["admission_verification"]["journal"], runtime["receipt"], runtime["doctor"],
             runtime["installed_manifest_snapshot"], cache["receipt"], cache["run_result"], ledger["ledger"]]
    added += matrix["admission_verification"]["sample_metadata"]
    added += [s[k] for s in matrix["slots"] for k in ("report", "raw_samples")]
    for entry in added:
        if entry["path"] not in {a["path"] for a in report["artifact_sha256"]}:
            report["artifact_sha256"].append(entry)
    verify_original_clauses(report)
    closure = copy.deepcopy(comments)
    closure.update(schema="kicad-route/closure-comments/1", source_commit=runtime["landed_git_head"],
                   finalization_required=["Verify staged durable proof paths, commit/push report, then parent may post comments."],
                   tracker_writes=False)
    for ident, comment in closure["comments"].items():
        comment.update(status="ready_after_durable_report_commit", draft_acceptance="met")
        text = comment["body"]
        if ident == "KICADROUTE-73":
            text = text.replace("FINAL PERFORMANCE MATRIX PENDING: do not close until all four current five-repeat comparisons pass the unchanged 1.10 wall and dual-memory gates.",
                                "All four final five-repeat comparisons pass the unchanged 1.10 wall and dual-memory gates, with native quality and copper equality across all 48 attempts.")
        text = text[:text.index("\n\nFinal trunk/runtime/report gates remain parent-owned.")]
        text += (f"\n\nLanded source: {runtime['landed_git_head']}. Selected default runtime was rebuilt, passed two native repeats, "
                 f"recorded its actual executed build and passed doctor. Runtime proof: {runtime['durable_receipt']}; "
                 f"{runtime['durable_doctor']}. Final report must be committed and pushed before this comment is posted.")
        comment["body"] = text
    rows = [f"| {t['identifier']} | Met | {len(t['criteria'])} |" for t in report["tickets"]]
    metrics = [f"| {s['board']} | " + " | ".join(f"{s['verdict']['metrics'][k]['ratio']:.8f}" for k in
               ("wall_s", "max_rss_kib", "process_tree_peak_rss_kib")) + " |" for s in matrix["slots"]]
    count = report["test_evidence"]["current_collected_cases"]
    md = (f"Acceptance verified for all 66 original clauses across KICADROUTE-68–81. Landed source `{runtime['landed_git_head']}` "
          f"matches the qualified source/test hashes. The selected default runtime passed two native repeats and doctor; "
          f"its receipt and actual build identities are retained in `final-acceptance.json`. Report publication and tracker updates remain parent-owned.\n\n"
          "The broad invocation ended **686 passed, 2 failed (688 cases)**. Corrective reports contain **193/193, 25/25, 24/24 and 113/113** passing cases. "
          f"`tests/coverage-union.json` accounts for all **{count} currently collected node IDs** through passing observations across invocations. "
          "This is not one green final full-suite invocation. The original failures remain retained.\n\n"
          "| Ticket | Acceptance | Original clauses |\n| --- | --- | --- |\n" + "\n".join(rows) + "\n\n"
          "All four final quiet-admission comparisons pass five measured repetitions per side plus separate warmups: **48 native attempts**, copper equality, "
          "native completion/preservation/pair success and uncensored timing. Each wall/GNU RSS/process-tree RSS gate remains **1.10**.\n\n"
          "| Board | Wall ratio | GNU RSS ratio | Process-tree RSS ratio |\n| --- | --- | --- | --- |\n" + "\n".join(metrics) + "\n\n"
          "Retained failures remain failures: original route-demo wall ratio 1.68257261, multipoint-stable 1.45826772 and cache-reuse 1.17098446. "
          "Prepared differential-pair 1.11040905 and environment-reuse differential-pair 1.23704867 also remain failed. "
          "The final matrix does not erase those observations. No claim is made that cache reuse alone explained the contention gap.\n\n"
          "The preregistered full 0–11 CPU profile admits each CLI only after three eligible five-second host intervals: "
          "at least 80% idle, CPU some avg10 at most 5%, and I/O full avg10 at most 1%. "
          "The shared 1800-second admission budget excludes timed CLI execution. Raw journal eligibility, fresh streaks, all 48 admission records "
          "and actual affinity are independently checked; completed attempts are never retried or dropped. Admission does not guarantee isolation "
          "during execution, and post-run pressure remains diagnostic.\n\n"
          "Publication verification retains the original full-scope failure alongside selected corrective evidence; all included record hashes resolve. "
          "The native bundle preserves source receipts/candidate. Final candidate digests, native changed-refill and human-ledger reconstruction, "
          "staged runtime identity, cache/ledger conflict rules and discovery populations are mapped clause by clause in the JSON. "
          "No global owner-board corpus or full weighted-search/churn coverage claim is made.\n")
    return report, closure, md


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-draft", action="store_true")
    parser.add_argument("--check-admission", action="store_true",
                        help="read-only partial journal diagnostic; never finalizes acceptance")
    parser.add_argument("--matrix-session")
    parser.add_argument("--matrix-exit-code", type=int)
    parser.add_argument("--runtime-receipt", type=Path)
    parser.add_argument("--doctor", type=Path)
    args = parser.parse_args()
    draft = read(Q / "final-acceptance-draft.json")
    verify_original_clauses(draft)
    if args.check_draft:
        print(json.dumps({"original_tickets": 14, "original_clauses": 66, "exact_original_strings_preserved": True}))
        return
    if args.check_admission:
        print(json.dumps(verify_admission_journal(quiet_profile(), complete=False), indent=2))
        return
    require(args.runtime_receipt is not None and args.doctor is not None, "runtime receipt and doctor paths required")
    matrix = verify_final_matrix(args.matrix_session, args.matrix_exit_code)
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
