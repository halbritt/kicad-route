#!/usr/bin/env python3
"""Inventory by default; explicitly stage terminal, passing final evidence later.

    /usr/bin/python3 build/agent497-qualification/stage-audit-evidence.py
    /usr/bin/python3 build/agent497-qualification/stage-audit-evidence.py \
        --stage --matrix-session 8896 --matrix-exit-code 0 \
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
import importlib.util
import sys
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid


ROOT = Path(__file__).resolve().parents[2]
Q = ROOT / "build/agent497-qualification"
FINAL = ("diff-pair-archon", "four-layer-archon", "multipoint-archon", "route-demo-archon")
LOCAL_CPU_HISTORY = ("diff-pair-cpu-rate", "four-layer-cpu-rate", "multipoint-cpu-rate", "route-demo-cpu-rate")
ARCHON_SESSION = "8896"
ARCHON_INDEX_SHA = "19b5fca94da3fdf939bd13259fa28829573906acbae5fd694441e1b49669eb4e"
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

    def add(source, target, original=None):
        source, target = Path(source), Path(target)
        if not source.is_file():
            missing.append(str(source))
            return
        name = str(target)
        if name in entries and entries[name]["source"] != str(source.absolute()):
            raise RuntimeError(f"destination collision: {name}")
        aliases = set(entries.get(name, {}).get("original_sources", []))
        if original is not None:
            aliases.add(original)
        entries[name] = {"source": str(source.absolute()), "retained": name,
                         "bytes": source.stat().st_size, "original_sources": sorted(aliases)}

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

    imported = Q / "archon-import"
    index_path = imported / "archon-evidence-index.json"
    if index_path.is_file():
        index = read(index_path)
        add(index_path, "performance/archon/archon-evidence-index.json",
            original="/evidence/archon-filtered-export/archon-evidence-index.json")
        for row in index["files"]:
            relative = Path(row["retained"])
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("unsafe Archon import locator")
            add(imported / relative, Path("performance/archon") / relative, original=row["source"])
    else:
        missing.append(str(index_path))
    for name in LOCAL_CPU_HISTORY:
        if (Q / name).is_dir():
            tree(Q / name, Path("performance/history/cpu-rate") / name)
    tree(Q / "cpu-rate-metadata", "performance/history/cpu-rate/cpu-rate-metadata")
    add(Q / "cpu-rate-matrix.log", "performance/history/cpu-rate/cpu-rate-matrix.log")
    tree(Q / "cpu-service-calibration", "performance/history/cpu-rate/calibration")
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
    for name in ("acceptance_profile.py", "archon_acceptance.py", "test_acceptance_profile.py",
                 "acceptance-profile-tests.log", "test_archon_acceptance.py", "archon-adapter-tests.log", "test_archon_acceptance.py",
                 "archon-finalizer-readiness.json", "stage-audit-evidence-before-archon.py",
                 "test_finalizer_archon.py", "finalizer-archon-tests.log",
                 "archon-final-verification.json", "archon-import-receipt.json", "archon-export-receipt.json",
                 "archon-task-ownership.json", "archon-setup-readiness.json", "archon-target-bindings.json",
                 "archon-verifier-independent-review.json", "stage-audit-checks.log",
                 "land-integration.json", "post-readiness-integration-provenance.json",
                 "landed-runtime-observation.json", "landed-main-install.log", "landed-main-doctor.log",
                 "final-output-review.json", "final-output-review-before-doctor-count-correction.json",
                 "final-output-review-doctor-count-erratum.json",
                 "archon-transport-selected-decision.json", "capture-archon-host-after.py"):
        if (Q / name).is_file():
            add(Q / name, Path("provenance") / name)
    if (Q / "local-cpu-rate-verifier-history").is_dir():
        tree(Q / "local-cpu-rate-verifier-history", "provenance/local-cpu-rate-verifier-history")
    for name in ("Dockerfile", "kicad.sources", "image-build-ssh.log", "source-checkouts-ssh.log",
                 "container-start-ssh.log", "runtime-preparation-ssh.log", "host-controls.log"):
        if (Q / "archon-setup" / name).is_file():
            add(Q / "archon-setup" / name, Path("provenance/archon-setup") / name)
    if (Q / "prepare-archon-runtime.py").is_file():
        add(Q / "prepare-archon-runtime.py", "provenance/archon-setup/prepare-archon-runtime.py")
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
    doctrine = Q / "doctrine-preparation.json"
    if doctrine.is_file():
        for record in read(doctrine).get("records", []):
            path = Path(record["path"])
            if path.is_file() and sha(path) != record["sha256"]:
                raise RuntimeError(f"typed doctrine evidence differs: {path}")
            add(path, Path("provenance/doctrine/typed") / path.name)
    for path in (Path("/tmp/kicad-route-agent497-doctrine.json"), Path("/tmp/kicad-route-agent497-doctrine-final.json"),
                 Path("/tmp/kicad-route-agent497-doctrine-second-final.json")):
        if path.is_file():
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


def check_archon_authority(base, source=False):
    base = Path(base)
    provenance = Q if source else base / "provenance"
    index = Q / "archon-import/archon-evidence-index.json" if source else base / "performance/archon/archon-evidence-index.json"
    sys.path.insert(0, str(provenance))
    try:
        spec = importlib.util.spec_from_file_location("retained_archon_acceptance", provenance / "archon_acceptance.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.verify_archon(module.EvidenceStore(index, ARCHON_INDEX_SHA), ARCHON_SESSION, 0)
    finally:
        sys.path.pop(0)


def check_final():
    result = check_archon_authority(Q, source=True)
    history = read(Q / "cpu-rate-metadata/matrix.json")
    if history.get("status") == "RUNNING":
        raise RuntimeError("local CPU-rate history is still mutable; wait for its terminal result before staging")
    return result


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
    terminal = check_archon_authority(destination)
    remote = read(destination / "performance/archon/archon-evidence-index.json")
    for row in remote["files"]:
        if mapping.get(row["source"]) != "performance/archon/" + row["retained"]:
            raise RuntimeError(f"remote original locator lacks durable mapping: {row['source']}")
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
            "archon_matrix_authority": {"status": terminal["status"], "completed_samples": 48,
                                       "matrix_session": ARCHON_SESSION, "index_sha256": ARCHON_INDEX_SHA,
                                       "full_independent_raw_and_resource_verification": True}}


def locator_mapping(entries):
    result = {}
    for entry in entries:
        for source in (entry["source"], *entry.get("original_sources", [])):
            # Some provenance is intentionally retained in two contexts; use
            # the first stable locator only when both copies have equal bytes.
            if source in result:
                previous = next(row for row in entries if row["retained"] == result[source])
                if previous.get("sha256") != entry.get("sha256"):
                    raise RuntimeError(f"conflicting original source bytes: {source}")
            else:
                result[source] = entry["retained"]
    return result


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
    manifest["original_to_retained"] = locator_mapping(manifest["files"])
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
            "entries": entries, "excluded": ["task HOME/download caches; local supporting OUT caches (remote indexed baseline cache retained)", "site-packages", "native build trees", "whole runtime environments",
                                                "raw resource/session/infra coordination receipts (sanitized summary only)"],
            "original_receipts": "copied byte-for-byte; absolute locators map through AUDIT-MANIFEST.json",
            "final_gate": "explicit Archon8896 terminal exit0; externally observed filtered index SHA; full independent raw/native/resource verification; four boards/48 samples and five measured runs per side; local94696 retained as terminal history"}
    if not args.stage:
        path = Q / "durable-evidence-plan.json"
        path.write_text(json.dumps(plan, indent=2) + "\n")
        print(json.dumps({key: value for key, value in plan.items() if key != "entries"}, indent=2))
        print(f"Full inventory: {path}")
        return
    if args.matrix_exit_code != 0 or args.matrix_session != ARCHON_SESSION or args.destination is None:
        parser.error("staging requires frozen Archon --matrix-session, --matrix-exit-code 0 and --destination")
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
                    "original_to_retained": locator_mapping(entries),
                    "limitations": ["Absolute locators in original receipts are historical; resolve using original_to_retained.",
                                    "This is retained test evidence; no claim of a single all-green full-suite invocation.",
                                    "Whole runtime environments and task HOME/download caches are deliberately omitted; indexed baseline grading cache and ledger evidence remains. Selected hash-bound extension bytes are retained as audit artifacts only. Runtime identities remain in receipts."]}
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
