#!/usr/bin/env python3
"""Inventory by default; explicitly stage terminal, passing final evidence later.

    /usr/bin/python3 build/agent497-qualification/stage-audit-evidence.py
    /usr/bin/python3 build/agent497-qualification/stage-audit-evidence.py \
        --stage --matrix-session 94696 --matrix-exit-code 0 \
        --destination /path/to/primary/reports/audit-20261020

No original receipts are rewritten. AUDIT-MANIFEST.json maps every original
absolute source path to retained relative bytes and SHA256. Verification requires
only Python and the retained report:

    python3 reports/audit-20261020/provenance/stage-audit-evidence.py \
        --verify reports/audit-20261020

After writing later report/tracker files inside the audit, extend its inventory:

    python3 build/agent497-qualification/stage-audit-evidence.py \
        --refresh-manifest --destination reports/audit-20261020

If runtime proof happened after staging, also pass --append-runtime followed by
the original build/agent497-qualification/landed-main-<stamp>-<pid> directory.
"""
import argparse
from collections import defaultdict
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid


ROOT = Path(__file__).resolve().parents[2]
Q = ROOT / "build/agent497-qualification"
FINAL = ("diff-pair-cpu-rate", "four-layer-cpu-rate", "multipoint-cpu-rate", "route-demo-cpu-rate")
QUIET_HISTORY = ("diff-pair-quiet-admission", "four-layer-quiet-admission", "multipoint-quiet-admission", "route-demo-quiet-admission")
CPU_RATE_SESSION = "94696"
CPU_RATE_PROTOCOL_SHA = "b830f37b0941b595ba2497e61d1d1c930663ce91044432eb9c91febf7936d225"
CPU_RATE_HARNESS_SHA = "1542b947ac50ee2cbe632f36bfed3a1ab735bb379aa10a8fdf389c8fa897ac2a"
CALIBRATOR_SHA = "50973ba0e9f32012e65cef2ac2888f20cd34f43b4c191e22c31a6c96b8da0bf9"
CALIBRATION_PLAN_SHA = "51cf52ccad99977ebeea11a22e6b033c34f9a311c46023e91c13e9b81fb80aa0"
PRE_ENV_REUSE = ("multipoint-prepared", "route-demo-prepared", "diff-pair-prepared")
FAILURES = ("route-demo", "multipoint-stable", "multipoint-cache-reuse")
GENERATION = ROOT / "build/agent497-bytecode-state/.router-generations/60310b2b2bc14f9e9efcddcb57e650e5"
SMOKE = ROOT / "build/agent497-release/smoke/20261020T192501-v7t7f9o3"
QUIET_PROTOCOL_SHA = "546568312807eb4f4bd230c74c61e1da6c61f628f82286841a483ec842ad7aef"
QUIET_HARNESS_SHA = "392263b38f1f0391b35e0e56c5cd5e1150916b7437369e428abe8e961ffd5a66"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def inventory():
    entries, missing = {}, []

    def add(source, target):
        source, target = Path(source), Path(target)
        if not source.is_file():
            missing.append(str(source))
            return
        name = str(target)
        if name in entries and entries[name]["source"] != str(source.absolute()):
            raise RuntimeError(f"destination collision: {name}")
        entries[name] = {"source": str(source.absolute()), "retained": name,
                         "bytes": source.stat().st_size}

    def tree(source, target):
        source = Path(source)
        if not source.is_dir():
            missing.append(str(source))
            return
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            # Deliberately exclude OUT homes/caches and any environments/builds.
            if any(part in {".home", ".kicad-config", "__pycache__", ".cache", "site-packages"}
                   for part in relative.parts):
                continue
            if path.is_file():
                add(path, Path(target) / relative)

    for name in FINAL:
        tree(Q / name, Path("performance/final") / name)
    tree(Q / "cpu-rate-metadata", "performance/cpu-rate/cpu-rate-metadata")
    add(Q / "cpu-rate-matrix.log", "performance/cpu-rate-matrix.log")
    tree(Q / "cpu-service-calibration", "performance/cpu-rate/calibration")
    tree(Q / "cpu-rate-prerequisite-snapshots", "provenance/cpu-rate-prerequisite-snapshots")
    for name in ("cpu-rate-protocol.json", "run-cpu-rate-matrix.py", "test_cpu_rate_matrix.py", "cpu-rate-tests.log",
                 "cpu-rate-profile-review.json", "cpu-service-calibration-root-review.json",
                 "calibrate-cpu-service.py", "cpu-service-calibration-plan.json",
                 "test_cpu_service_calibration.py", "cpu-service-calibration-tests.log"):
        add(Q / name, Path("provenance") / name)
    # Retain the zero-launch, expired quiet study as historical inconclusive evidence.
    for name in QUIET_HISTORY:
        if (Q / name).is_dir():
            tree(Q / name, Path("performance/history/quiet-admission") / name)
    tree(Q / "quiet-admission-metadata", "performance/history/quiet-admission/quiet-admission-metadata")
    add(Q / "quiet-admission-resource-intervals.jsonl", "performance/history/quiet-admission/quiet-admission-resource-intervals.jsonl")
    add(Q / "quiet-admission-matrix.log", "performance/history/quiet-admission/quiet-admission-matrix.log")
    for name in ("quiet-admission-protocol.json", "run-quiet-matrix.py", "test_quiet_matrix.py", "quiet-admission-tests.log"):
        add(Q / name, Path("provenance") / name)
    for name in ("finalizer-admission-tests.log", "test_finalize_acceptance.py", "install-helper-review.json",
                 "finalizer-independent-review.json", "cpu-rate-finalizer-readiness.json",
                 "test_finalize_cpu_rate.py", "finalizer-cpu-rate-tests.log", "finalizer-admission-tests-current.log",
                 "finalize-acceptance-quiet-history.py", "test_finalize_acceptance-quiet-history.py",
                 "finalizer-admission-tests-quiet-history.log"):
        if (Q / name).is_file():
            add(Q / name, Path("provenance") / name)
    if (Q / "resource-coordination-summary.json").is_file():
        add(Q / "resource-coordination-summary.json", "provenance/resource-coordination-summary.json")
    for name in FAILURES:
        tree(Q / name, Path("performance/failures") / name)
        add(Q / f"{name}.log", Path("performance/failures") / f"{name}.log")
    for name in PRE_ENV_REUSE:
        tree(Q / name, Path("performance/history/pre-env-reuse") / name)
        add(Q / f"{name}.log", Path("performance/history/pre-env-reuse") / f"{name}.log")
    add(Q / "prepared-bytecode-matrix.log", "performance/history/pre-env-reuse/prepared-bytecode-matrix.log")
    tree(Q / "diff-pair-env-reuse", "performance/history/env-reuse/diff-pair-env-reuse")
    add(Q / "diff-pair-env-reuse.log", "performance/history/env-reuse/diff-pair-env-reuse.log")
    add(Q / "env-reuse-matrix.log", "performance/history/env-reuse/env-reuse-matrix.log")
    for name in ("final-matrix.log", "stable-matrix.log",
                 "version-reuse-matrix.log"):
        add(Q / name, Path("performance") / name)
    for name in ("comparison-parity.json", "final-source-parity.json", "multipoint-diagnosis.json",
                 "observer-cost.json", "observer-cost-descendants.json", "performance-native-cache-identity.json",
                 "wrapper-profile-comparison.json", "environment-reuse-microprobe.json",
                 "environment-reuse-microprobe.log", "environment-reuse-microprobe.py"):
        add(Q / name, Path("performance/diagnostics") / name)
    tree(Q / "bytecode-import-probe", "performance/diagnostics/bytecode-import-probe")
    tree(Q / "wrapper-startup", "performance/diagnostics/wrapper-startup")
    for name in ("acceptance.json", "engine-acceptance.json", "evidence-acceptance.json",
                 "installation-acceptance.json"):
        add(Q / name, Path("acceptance") / name)
    # Parent finalizes these only after the terminal matrix/runtime proof. Drafts
    # stay explicitly labeled and optional artifacts do not create false misses.
    for name in ("final-acceptance.json", "final-acceptance.md"):
        if (Q / name).is_file():
            add(Q / name, name)
    for name in ("final-acceptance-draft.json", "final-acceptance-draft.md", "closure-comments-draft.json", "audit-readme-draft.md"):
        if (Q / name).is_file():
            add(Q / name, Path("acceptance/drafts") / name)
    if (Q / "closure-comments-final.json").is_file():
        add(Q / "closure-comments-final.json", "tracker/closure-comments-final.json")
    for pattern in ("*doctrine*", "*decision*", "*citation*"):
        for path in sorted(Q.glob(pattern)):
            if path.is_dir():
                tree(path, Path("provenance/doctrine") / path.name)
            elif path.is_file():
                add(path, Path("provenance/doctrine") / path.name)
    for runtime in sorted(Q.glob("landed-main-*")):
        if runtime.is_dir():
            tree(runtime, Path("runtime") / runtime.name)
    # Finalizer's selected-manifest artifact names the checkout's current file;
    # retain those exact post-install bytes as well as the runtime's snapshot.
    if any((directory / "receipt.json").is_file() for directory in Q.glob("landed-main-*") if directory.is_dir()):
        add(ROOT / "manifest.json", "runtime/selected-manifest.json")
    add(SMOKE / "junit.xml", "tests/original-smoke/junit.xml")
    add(SMOKE / "summary.json", "tests/original-smoke/summary.json")
    add(ROOT / "build/agent497-smoke.log", "tests/original-smoke/smoke.log")
    for name in ("final-focused.xml", "final-selector.xml", "final-merged-generation.xml", "final-env-reuse.xml",
                 "coverage-union.json", "coverage-union-before-env-reuse.json", "final-collection.log",
                 "bytecode-smoke-collection.log"):
        add(Q / name, Path("tests") / name)
    if (Q / "env-reuse-collection.log").is_file():
        add(Q / "env-reuse-collection.log", "tests/env-reuse-collection.log")
    for name in ("agent497-kernel.log", "agent497-kernel-mutants.log"):
        add(ROOT / "build" / name, Path("kernel") / name)
    for path in sorted(Q.glob("kernel-mutant-*.log")):
        add(path, Path("kernel") / path.name)
    for name in ("native-run", "smoke-publication-tests"):
        tree(ROOT / "build/agent497-publication" / name, Path("publication") / name)
    add(Q / "publication-verification.json", "publication/verification-original.json")
    # Preserve the source's candidate and reference bytes alongside the exact
    # publication. Original external locators remain intact and map via inventory.
    published_result = ROOT / "build/agent497-publication/native-run/result.json"
    if published_result.exists():
        tree(Path(read(published_result)["run_dir"]), "publication/native-source-run")
    for name in ("bytecode-native-build.json", "bytecode-preparation.json", "bytecode-constraints.txt",
                 "install-landed-main.py", "stage-audit-evidence.py"):
        add(Q / name, Path("provenance") / name)
    if (Q / "finalize-acceptance.py").is_file():
        add(Q / "finalize-acceptance.py", "provenance/finalize-acceptance.py")
    for name in ("manifest.json", "installation.json", "preparation.json"):
        add(GENERATION / name, Path("provenance/qualified-runtime") / name)
    for name in ("VERSION", "UPSTREAM.json", "requirements.txt", "rust_router/Cargo.lock"):
        add(GENERATION / "router" / name, Path("provenance/qualified-runtime/router") / name)
    preparation = GENERATION / "preparation.json"
    if preparation.exists():
        tree(Path(read(preparation)["logs"]), "provenance/qualified-runtime/step-logs")
    for name in ("analysis.md", "issues-draft.json"):
        add(ROOT / "build/deep-analysis-20261020-agent486" / name,
            Path("provenance/original-issues") / name)
    return sorted(entries.values(), key=lambda entry: entry["retained"]), missing


def check_rate_authority(base, source=False):
    base = Path(base)
    provenance = base if source else base / "provenance"
    metadata = base / "cpu-rate-metadata" if source else base / "performance/cpu-rate/cpu-rate-metadata"
    calibration = base / "cpu-service-calibration" if source else base / "performance/cpu-rate/calibration"
    protocol = provenance / "cpu-rate-protocol.json"
    if sha(protocol) != CPU_RATE_PROTOCOL_SHA or sha(provenance / "run-cpu-rate-matrix.py") != CPU_RATE_HARNESS_SHA:
        raise RuntimeError("CPU-rate protocol or harness differs from preregistered bytes")
    if (sha(provenance / "calibrate-cpu-service.py") != CALIBRATOR_SHA or
            sha(provenance / "cpu-service-calibration-plan.json") != CALIBRATION_PLAN_SHA):
        raise RuntimeError("CPU-service calibration selector differs from frozen bytes")
    selected = read(calibration / "selection.json")
    frozen = read(protocol)
    snapshots = base / "cpu-rate-prerequisite-snapshots" if source else provenance / "cpu-rate-prerequisite-snapshots"
    index = read(snapshots / "index.json")
    if (index.get("protocol_sha256") != CPU_RATE_PROTOCOL_SHA or
            len(index.get("files", [])) != len(frozen["hash_prerequisites"])):
        raise RuntimeError("frozen CPU-rate prerequisite inventory differs from protocol")
    for claim, captured in zip(frozen["hash_prerequisites"], index["files"]):
        path = snapshots / captured["snapshot"]
        if (path.resolve().parent != snapshots.resolve() or captured["original_path"] != claim["path"] or
                sha(path) != captured["sha256"] or captured["sha256"] != claim["sha256"]):
            raise RuntimeError(f"frozen benchmark prerequisite bytes differ: {claim['path']}")
    claims = frozen["calibration_selection"]["all_trial_and_source_hashes"]
    mapping = {} if source else read(base / "AUDIT-MANIFEST.json")["original_to_retained"]
    for claim in claims:
        original = claim["path"]
        path = Path(original) if source else base / mapping[original]
        if sha(path) != claim["sha256"]:
            raise RuntimeError(f"calibration source or trial bytes changed: {original}")
    if sha(calibration / "selection.json") != frozen["calibration_selection"]["sha256"]:
        raise RuntimeError("calibration selection differs from frozen protocol")
    matrix = read(metadata / "matrix.json")
    if not (selected.get("status") == "PASS" and selected.get("selected_cap_cpus") in (1, 2) and
            selected["selected_cap_cpus"] == frozen.get("cpu_cap") == matrix.get("cpu_cap") and
            selected.get("source_sha256") == CALIBRATOR_SHA):
        raise RuntimeError("CPU-rate authority lacks matching successful calibration")
    if not (matrix.get("status") == "PASS" and matrix.get("all_comparisons_passed") is True and
            matrix.get("completed_samples") == 48 and len(matrix.get("boards", [])) == 4 and
            matrix.get("protocol_sha256") == sha(metadata / "protocol.json") == CPU_RATE_PROTOCOL_SHA and
            not matrix.get("resource_violations") and read(metadata / "protocol.json") == frozen and
            len(list((metadata / "samples").glob("*.json"))) == 48):
        raise RuntimeError("CPU-rate matrix lacks terminal PASS with four boards and48 completed samples")
    for row, name in zip(matrix["boards"], FINAL):
        directory = base / name if source else base / "performance/final" / name
        report = read(directory / "comparison.json")
        if (report.get("verdict", {}).get("passed") is not True or row.get("status") != "PASS" or
                row.get("completed_samples") != 12 or row.get("name") != name.removesuffix("-cpu-rate") or
                row.get("verdict") != report["verdict"]):
            raise RuntimeError(f"final comparison is missing a passing terminal result: {name}")
        samples = read(directory / "samples.json")
        if any(len(samples.get(side, [])) != 5 for side in ("reference", "candidate")):
            raise RuntimeError(f"expected five measured repeats per side: {name}")
        if (report.get("candidate") != samples.get("candidate") or report.get("reference") != samples.get("reference") or
                report.get("warmup") != samples.get("warmup") or set(samples.get("warmup", {})) != {"reference", "candidate"}):
            raise RuntimeError(f"comparison and raw sample summaries disagree: {name}")
    return matrix


def check_final():
    return check_rate_authority(Q, source=True)


def verify(destination):
    destination = Path(destination)
    manifest = read(destination / "AUDIT-MANIFEST.json")
    mapping = manifest["original_to_retained"]
    for entry in manifest["files"]:
        relative = Path(entry["retained"])
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"unsafe inventory locator: {relative}")
        path = destination / relative
        if path.stat().st_size != entry["bytes"] or sha(path) != entry["sha256"]:
            raise RuntimeError(f"retained bytes differ: {relative}")
    terminal = check_rate_authority(destination)
    for name in ("native-run", "smoke-publication-tests"):
        base = destination / "publication" / name
        publication = read(base / "publication.json")
        for locator, digest in publication["files"].items():
            if sha(base / locator) != digest:
                raise RuntimeError(f"publication inventory differs: {name}/{locator}")
        for evidence in publication.get("evidence", {}).values():
            for claim in evidence.get("records", {}).values():
                if claim.get("availability") == "included" and sha(base / claim["path"]) != claim["sha256"]:
                    raise RuntimeError(f"publication included-record claim differs: {name}/{claim['path']}")
    for name in ("coverage-union.json", "coverage-union-before-env-reuse.json"):
        coverage = read(destination / "tests" / name)
        for run in coverage["runs"]:
            if run["path"] not in mapping or sha(destination / mapping[run["path"]]) != run["sha256"]:
                raise RuntimeError(f"coverage source missing or changed: {run['path']}")
    for name in FINAL:
        sample = read(destination / "performance/final" / name / "samples.json")
        for side in ("reference", "candidate"):
            for observation in [sample["warmup"][side], *sample[side]]:
                if observation["result"] not in mapping:
                    raise RuntimeError(f"raw sample result not retained: {observation['result']}")
                timing = str(Path(observation["result"]).parent.parent / "time.json")
                if timing not in mapping:
                    raise RuntimeError(f"raw sample timing not retained: {timing}")
                # Quality is embedded in the byte-preserved samples/comparison
                # records; the corresponding native result is retained above.
                if not isinstance(observation.get("quality"), dict):
                    raise RuntimeError(f"sample quality is absent: {observation['result']}")
    native = read(destination / "publication/native-run/result.json")
    candidate = native["candidate"]["board"]
    if candidate not in mapping or sha(destination / mapping[candidate]) != native["evidence"]["candidate_board_sha256"]:
        raise RuntimeError("published candidate's original external bytes are not retained")
    attempt = read(destination / "publication/native-run/attempt.json")
    for name, digest in native["evidence"]["reference_files"].items():
        original = str(Path(native["run_dir"]) / attempt["reference_dir"] / name)
        if original not in mapping or sha(destination / mapping[original]) != digest:
            raise RuntimeError(f"published input's original external bytes are not retained: {original}")
    scope = read(destination / "publication/smoke-publication-tests/scope.json")
    for key in ("full_suite_evidence", "corrective_evidence"):
        evidence = scope[key]
        if evidence["path"] not in mapping or sha(destination / mapping[evidence["path"]]) != evidence["sha256"]:
            raise RuntimeError(f"smoke publication external source missing or changed: {key}")
    runtime_receipts = sorted((destination / "runtime").glob("landed-main-*/receipt.json"))
    runtime_verdict = {"availability": "not retained yet"}
    if runtime_receipts:
        latest = runtime_receipts[-1]
        receipt = read(latest)
        if receipt.get("status") != "PASS" or not receipt.get("finished_utc"):
            raise RuntimeError("latest retained runtime proof is not a terminal pass")
        if not receipt.get("gates") or not all(gate.get("passed") is True for gate in receipt["gates"]):
            raise RuntimeError("retained runtime proof has missing/failed gates")
        selected = destination / "runtime/selected-manifest.json"
        installed = latest.parent / "installed-manifest.json"
        if not selected.is_file() or sha(selected) != sha(installed):
            raise RuntimeError("retained selected default manifest differs from runtime proof snapshot")
        doctors = sorted(latest.parent.rglob("doctor.json"))
        if not doctors or any(read(path).get("status") != "PASS" or read(path).get("exit_code") != 0
                              for path in doctors):
            raise RuntimeError("runtime proof lacks a retained passing doctor record")
        runtime_verdict = {"availability": "retained", "receipt": str(latest.relative_to(destination)),
                           "doctor_records": [str(path.relative_to(destination)) for path in doctors]}
    return {"files": len(manifest["files"]), "bytes": sum(entry["bytes"] for entry in manifest["files"]),
            "all_file_hashes": True, "publication_inventories": True,
            "coverage_sources": True, "raw_sample_results_and_timing": True,
            "inline_sample_quality": True, "external_candidate": True, "runtime_proof": runtime_verdict,
            "cpu_rate_matrix_authority": {"status": terminal["status"], "completed_samples": 48,
                                       "selected_cap_cpus": terminal["cpu_cap"],
                                       "protocol_sha256": CPU_RATE_PROTOCOL_SHA, "harness_sha256": CPU_RATE_HARNESS_SHA}}


def refresh_manifest(destination, runtime=None):
    """Extend a verified audit; retain its previous generated inventory verbatim."""
    destination = Path(destination).expanduser().absolute()
    verify(destination)  # Refuse changed original retained evidence before writes.
    manifest_path = destination / "AUDIT-MANIFEST.json"
    previous = manifest_path.read_bytes()
    manifest = json.loads(previous)
    entries = {entry["retained"]: entry for entry in manifest["files"]}
    added_sources = {}
    if runtime is not None:
        runtime = Path(runtime).expanduser().absolute()
        if runtime.parent != Q or not runtime.name.startswith("landed-main-") or not runtime.is_dir():
            raise RuntimeError("append source must be an original qualification/landed-main-* directory")
        receipt = read(runtime / "receipt.json")
        doctors = list(runtime.rglob("doctor.json"))
        if receipt.get("status") != "PASS" or not receipt.get("finished_utc") or not doctors or any(
                read(path).get("status") != "PASS" or read(path).get("exit_code") != 0 for path in doctors):
            raise RuntimeError("append source requires terminal passing install proof and doctor")
        target_root = destination / "runtime" / runtime.name
        if target_root.exists():
            raise RuntimeError("runtime directory is already retained")
        for source in sorted(runtime.rglob("*")):
            relative = source.relative_to(runtime)
            if any(part in {".home", ".kicad-config", "__pycache__", ".cache", "site-packages"}
                   for part in relative.parts) or not source.is_file():
                continue
            target = target_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = sha(source)
            shutil.copyfile(source, target)
            if sha(source) != digest or sha(target) != digest:
                raise RuntimeError(f"runtime evidence changed during copy: {source}")
            added_sources[str(target.relative_to(destination))] = str(source)
        # Optional first runtime extension also retains the exact selected file
        # named by the finalizer, preserving its original absolute-path mapping.
        selected_source = ROOT / "manifest.json"
        if sha(selected_source) != sha(runtime / "installed-manifest.json"):
            raise RuntimeError("current selected manifest differs from appended runtime proof")
        selected_target = destination / "runtime/selected-manifest.json"
        if selected_target.exists() and sha(selected_target) != sha(selected_source):
            raise RuntimeError("selected manifest already retained for a different runtime proof")
        shutil.copyfile(selected_source, selected_target)
        added_sources["runtime/selected-manifest.json"] = str(selected_source)
    previous_sha = hashlib.sha256(previous).hexdigest()
    history = destination / "provenance/inventory-history" / f"{previous_sha}.json"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_bytes(previous)
    for path in sorted(destination.rglob("*")):
        if not path.is_file() or path in (manifest_path, destination / "VERIFICATION.json"):
            continue
        relative = str(path.relative_to(destination))
        if relative not in entries:
            entries[relative] = {"source": added_sources.get(relative, str(path.absolute())),
                                 "retained": relative, "bytes": path.stat().st_size, "sha256": sha(path)}
    manifest["previous_inventory_sha256"] = previous_sha
    manifest["extended_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    manifest["files"] = sorted(entries.values(), key=lambda entry: entry["retained"])
    manifest["original_to_retained"] = {entry["source"]: entry["retained"] for entry in manifest["files"]}
    temporary = manifest_path.with_suffix(".json.new")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(temporary, manifest_path)
    result = verify(destination)
    (destination / "VERIFICATION.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", action="store_true")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--matrix-session")
    parser.add_argument("--matrix-exit-code", type=int)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--refresh-manifest", action="store_true")
    parser.add_argument("--append-runtime", type=Path)
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify(args.verify), indent=2))
        return
    if args.refresh_manifest:
        if args.destination is None:
            parser.error("--refresh-manifest requires --destination")
        print(json.dumps(refresh_manifest(args.destination, args.append_runtime), indent=2))
        return
    if args.append_runtime:
        parser.error("--append-runtime requires --refresh-manifest")
    entries, missing = inventory()
    totals = defaultdict(int)
    for entry in entries:
        totals[entry["retained"].split("/")[0]] += entry["bytes"]
    plan = {"proposed_destination": "reports/audit-20261020", "files": len(entries),
            "bytes": sum(totals.values()), "category_bytes": dict(totals), "missing": missing,
            "entries": entries, "excluded": ["OUT homes/caches", "site-packages", "native build trees", "whole runtime environments",
                                                "raw resource/session/infra coordination receipts (sanitized summary only)"],
            "original_receipts": "copied byte-for-byte; absolute locators map through AUDIT-MANIFEST.json",
            "final_gate": "explicit CPU-rate matrix terminal exit0; frozen protocol/harness/selector SHA; calibration-selected cap; terminal matrix.json PASS/four boards/48 samples; five measured runs per side"}
    if not args.stage:
        path = Q / "durable-evidence-plan.json"
        path.write_text(json.dumps(plan, indent=2) + "\n")
        print(json.dumps({key: value for key, value in plan.items() if key != "entries"}, indent=2))
        print(f"Full inventory: {path}")
        return
    if args.matrix_exit_code != 0 or args.matrix_session != CPU_RATE_SESSION or args.destination is None:
        parser.error("staging requires frozen CPU-rate --matrix-session, --matrix-exit-code 0 and --destination")
    if missing:
        raise RuntimeError(f"required evidence missing: {missing}")
    check_final()
    destination = args.destination.expanduser().absolute()
    if destination.name != "audit-20261020" or destination.parent.name != "reports" or destination.exists():
        raise RuntimeError("destination must be a new reports/audit-20261020 directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}-{uuid.uuid4().hex}")
    temporary.mkdir()
    try:
        for entry in entries:
            source = Path(entry["source"])
            target = temporary / entry["retained"]
            target.parent.mkdir(parents=True, exist_ok=True)
            before = sha(source)
            shutil.copyfile(source, target)
            if sha(target) != before or sha(source) != before:
                raise RuntimeError(f"evidence changed during staging: {source}")
            entry["sha256"] = before
            entry["bytes"] = target.stat().st_size
        manifest = {"schema": "kicad-route/audit-retained-evidence/1",
                    "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "source_checkout": str(ROOT), "matrix_session": args.matrix_session,
                    "observed_matrix_exit_code": args.matrix_exit_code,
                    "files": entries,
                    "original_to_retained": {entry["source"]: entry["retained"] for entry in entries},
                    "limitations": ["Absolute locators in original receipts are historical; resolve using original_to_retained.",
                                    "This is retained test evidence; no claim of a single all-green full-suite invocation.",
                                    "Whole runtime environments and OUT caches are deliberately omitted; selected hash-bound extension bytes may be retained as audit artifacts only. Runtime identities remain in receipts."]}
        (temporary / "AUDIT-MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
        result = verify(temporary)
        (temporary / "VERIFICATION.json").write_text(json.dumps(result, indent=2) + "\n")
        os.rename(temporary, destination)
        print(json.dumps({"destination": str(destination), "verification": result}, indent=2))
    except BaseException:
        shutil.rmtree(temporary)
        raise


if __name__ == "__main__":
    main()
