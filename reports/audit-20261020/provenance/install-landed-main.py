#!/usr/bin/env python3
"""Install and qualify the landed main checkout's default KRT runtime.

Prepared artifact only: do not invoke before final qualification and landing.
From the landed checkout root, with its normal system Python:

    PYTHONPATH="$PWD/src" PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 \
        build/agent497-qualification/install-landed-main.py

This deliberately calls update_krt and skills.sync, never full update or a
Freerouting downloader. Installation prepares a fresh generation and atomically
activates it. Existing generations and the legacy environment are not modified.
A later proof failure leaves the validated installation active and exits nonzero;
there is no automatic rollback. All observed gates and install steps are retained
under build/agent497-qualification/landed-main-<UTC>-<pid>/.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback


ROOT = Path(__file__).resolve().parents[2]
QUALIFICATION = ROOT / "build/agent497-qualification"
QUALIFIED_STATE = ROOT / "build/agent497-bytecode-state"
QUALIFIED_GENERATION = ".router-generations/60310b2b2bc14f9e9efcddcb57e650e5"
SOURCE_SHA256 = "c6c09054f6845442867ed99141ecbcfdc78f39f622cda06ff401e8230bd9bab1"
PACKAGES_SHA256 = "0dc7c913210ed2d7f0afe996b4f936eaee539c19ca5e6e5d76949d13eaf304ae"
RUST_LOCK_SHA256 = "f14e7b0c06ae682e63f1b452862a72417cb69ee629f51ac49bac242a24d94203"
PYTHON_VERSION = "3.12.3"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    os.chdir(ROOT)
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    # This invocation always targets the checkout default, not an inherited test
    # STATE, OUT, or interpreter. None of these changes survive this process.
    for key in ("KICAD_ROUTE_STATE", "KICAD_ROUTE_OUT", "KICAD_ROUTE_KRT_PYTHON"):
        os.environ.pop(key, None)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = QUALIFICATION / f"landed-main-{stamp}-{os.getpid()}"
    destination.mkdir(parents=True, exist_ok=False)
    receipt_path = destination / "receipt.json"
    log_path = destination / "install-proof.log"
    receipt = {"status": "running", "started_utc": stamp, "checkout": str(ROOT),
               "qualified_state": str(QUALIFIED_STATE), "gates": [], "steps": [],
               "receipt": str(receipt_path), "log": str(log_path),
               "rollback_policy": "no automatic rollback after activation"}

    def save():
        receipt_path.write_text(json.dumps(receipt, indent=2, default=str) + "\n")

    def note(message):
        print(message, flush=True)
        with log_path.open("a") as stream:
            stream.write(message + "\n")

    def gate(name, condition, observed=None):
        passed = bool(condition)
        receipt["gates"].append({"name": name, "passed": passed, "observed": observed})
        save()
        note(f"{'PASS' if passed else 'FAIL'} {name}: {observed}")
        if not passed:
            raise RuntimeError(f"qualification gate failed: {name}")

    save()
    note(f"Receipt: {receipt_path}")
    try:
        branch = subprocess.check_output(["git", "branch", "--show-current"], text=True).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"],
                                        text=True).strip()
        receipt["git_head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        gate("clean main checkout", branch == "main" and not dirty,
             {"branch": branch, "tracked_changes": dirty, "head": receipt["git_head"]})

        qualified_path = QUALIFIED_STATE / "manifest.json"
        qualified_bytes = qualified_path.read_bytes()
        qualified = json.loads(qualified_bytes)
        reference = qualified["krt"]
        gate("qualified generation", reference["generation"] == QUALIFIED_GENERATION,
             reference["generation"])
        gate("qualified routing source", reference["source_sha256"] == SOURCE_SHA256,
             reference["source_sha256"])
        gate("qualified packages", reference["venv"]["packages_sha256"] == PACKAGES_SHA256,
             reference["venv"])
        gate("qualified Python", reference["venv"]["python_version"] == PYTHON_VERSION,
             reference["venv"]["python_version"])
        receipt["qualified_identity"] = reference
        (destination / "qualified-manifest.json").write_bytes(qualified_bytes)
        constraints = destination / "constraints.txt"
        constraints.write_text("\n".join(reference["venv"]["packages"]) + "\n")
        os.environ["UV_CONSTRAINT"] = str(constraints)
        os.environ["KICAD_ROUTE_KRT_PYTHON"] = reference["venv"]["python"]
        receipt["invocation_environment"] = {
            "UV_CONSTRAINT": str(constraints),
            "KICAD_ROUTE_KRT_PYTHON": os.environ["KICAD_ROUTE_KRT_PYTHON"],
            "PYTHONDONTWRITEBYTECODE": "1", "KICAD_ROUTE_STATE": "unset (checkout default)"}

        # Imports must resolve to the landed checkout specified by PYTHONPATH.
        from kicad_route import cli, common, determinism, engine, installation, skills, statemanifest

        common.set_out(destination / "out")
        receipt["out"] = str(common.out_dir())
        gate("landed checkout imports", Path(cli.__file__).resolve() == ROOT / "src/kicad_route/cli.py",
             cli.__file__)
        gate("default checkout STATE", common.STATE == ROOT, str(common.STATE))
        gate("landed routing source", engine.source_sha256(engine.SOURCE_ROOT) == SOURCE_SHA256,
             engine.source_sha256(engine.SOURCE_ROOT))
        lock = engine.SOURCE_ROOT / "rust_router/Cargo.lock"
        gate("landed Rust lock", sha256(lock) == RUST_LOCK_SHA256, sha256(lock))
        builder = subprocess.check_output([cli.krt_venv_python(), "-I", "-B", "-c",
                                           "import sys; print(sys.version.split()[0])"], text=True).strip()
        gate("build interpreter version", builder == PYTHON_VERSION, builder)

        # As in full update, the shared activation lock covers installation and
        # proof recording; another writer cannot replace the selected generation.
        with installation.update_lock():
            original_manifest = common.STATE / "manifest.json"
            receipt["previous_generation"] = str(engine.GENERATION) if engine.GENERATION else None
            if original_manifest.exists():
                (destination / "previous-manifest.json").write_bytes(original_manifest.read_bytes())
            receipt["phase"] = "preparing"
            save()
            note("Preparing KRT with update_krt; Freerouting is not updated.")
            description = cli.update_krt(receipt["steps"])
            receipt["phase"] = "activated"
            receipt["generation"] = str(engine.GENERATION)
            receipt["description"] = description
            save()
            gate("installation step exits", bool(receipt["steps"]) and
                 all(step.get("exit_code") == 0 for step in receipt["steps"]), receipt["steps"])
            gate("prepared dependency bytecode", any(
                Path(step["log"]).name == "krt-bytecode.log" and step.get("exit_code") == 0
                for step in receipt["steps"]), [step["log"] for step in receipt["steps"]])
            receipt["skills_sync"] = skills.sync(cli.KRT_DIR, cli.KRT_VENV)
            note(receipt["skills_sync"])
            manifest = statemanifest.read()
            installed = manifest["krt"]
            receipt["installed_identity"] = installed
            receipt["module_comparison"] = {
                "qualified_sha256": reference["extension_module"]["sha256"],
                "installed_sha256": installed["extension_module"]["sha256"],
                "equality_required": False,
                "basis": "fresh native rebuild; source, Rust lock and packages are gated separately"}
            save()
            gate("new coherent generation", engine.GENERATION is not None and
                 engine.GENERATION == common.router_generation() and
                 cli.KRT_DIR == engine.GENERATION / "router" and
                 cli.KRT_VENV == engine.GENERATION / "venv" and
                 str(engine.GENERATION) != receipt["previous_generation"],
                 {"generation": str(engine.GENERATION), "source": str(cli.KRT_DIR),
                  "venv": str(cli.KRT_VENV)})
            gate("installed routing source", installed["source_sha256"] == SOURCE_SHA256,
                 installed["source_sha256"])
            gate("installed Rust lock", sha256(cli.KRT_DIR / "rust_router/Cargo.lock") == RUST_LOCK_SHA256,
                 sha256(cli.KRT_DIR / "rust_router/Cargo.lock"))
            gate("installed packages", installed["venv"]["packages"] == reference["venv"]["packages"] and
                 installed["venv"]["packages_sha256"] == PACKAGES_SHA256, installed["venv"])
            gate("installed Python", installed["venv"]["python_version"] == PYTHON_VERSION,
                 installed["venv"]["python_version"])
            gate("installed manifest drift", statemanifest.verify(manifest, common.STATE) == [],
                 statemanifest.verify(manifest, common.STATE))
            receipt["phase"] = "native repeat proof"
            save()
            repeat, repeat_path = determinism.check(cli, backend="krt", runs=2, budget=180,
                                                     default_clearance=0.4, record=False)
            receipt["determinism_path"] = str(repeat_path)
            receipt["determinism"] = repeat
            save()
            gate("two native repeats", len(repeat["runs"]) == 2, len(repeat["runs"]))
            receipt["executed_runs"] = []
            for index, layer in enumerate(repeat["runs"], 1):
                result_path = Path(layer["run_dir"]) / "result.json"
                result = json.loads(result_path.read_text())
                process, routing, validation = (result[key] for key in ("process", "routing", "validation"))
                native = [command for command in process.get("commands", []) if
                          len(command.get("argv", [])) > 1 and
                          Path(command["argv"][1]) == cli.KRT_DIR / "py_router/route.py"]
                backend = result["tool_identity"]["backend"]
                receipt["executed_runs"].append({"result": str(result_path), "outcome": layer["outcome"],
                    "tool_identity": result["tool_identity"], "backend_runtime": result["backend"].get("runtime"),
                    "native_commands": native, "candidate": result.get("candidate")})
                gate(f"run {index} native command", bool(native) and all(
                    command.get("exit_code") == 0 and Path(command["argv"][0]) == cli.route_python()
                    for command in native), native)
                gate(f"run {index} process PASS / routing COMPLETE / validation PASS",
                     result.get("exit_code") == 0 and process["status"] == "PASS" and
                     process.get("exit_code") == 0 and routing["status"] == "COMPLETE" and
                     routing.get("unconnected_in_scope") == 0 and validation["status"] == "PASS" and
                     not determinism.wall_clock_stops(process), layer["outcome"])
                gate(f"run {index} executed build", backend["source_sha256"] == SOURCE_SHA256 and
                     backend["venv_packages_sha256"] == PACKAGES_SHA256 and
                     backend["venv_python_version"] == PYTHON_VERSION and
                     backend["extension_module_sha256"] == installed["extension_module"]["sha256"] and
                     result["backend"]["runtime"]["generation"] == str(engine.GENERATION) and
                     result["tool_identity"]["state_manifest_drift"] == [], backend)
            gate("repeat proof", repeat.get("reproducible") is True and
                 repeat.get("same", {}).get("execution_build") is True and
                 repeat.get("build_id") == statemanifest.current_build_id(), repeat)
            # Store only after actual native process, completeness and validation
            # gates pass: equal failed outcomes are insufficient qualification.
            statemanifest.record_determinism(repeat)
            repeat["recorded_in"] = str(statemanifest.manifest_path())
            common.write_json(repeat_path, repeat)
            receipt["phase"] = "proof recorded"
            final = statemanifest.read()
            gate("recorded executed build", final["determinism"]["krt"]["build_id"] == repeat["build_id"] and
                 statemanifest.reproducibility("krt", runtime_generation=engine.GENERATION)["measurement"] is True,
                 statemanifest.reproducibility("krt", runtime_generation=engine.GENERATION))
            gate("final manifest drift", statemanifest.verify(final, common.STATE) == [],
                 statemanifest.verify(final, common.STATE))
            (destination / "installed-manifest.json").write_text(json.dumps(final, indent=2) + "\n")
            gate("qualified state unchanged", qualified_path.read_bytes() == qualified_bytes,
                 sha256(qualified_path))
        receipt["status"] = "PASS"
        receipt["doctor_record_expected"] = str(destination / "doctor/doctor/doctor.json")
        receipt["doctor_followup"] = (
            'env -u KICAD_ROUTE_STATE PYTHONPATH="$PWD/src" PYTHONDONTWRITEBYTECODE=1 '
            f'bin/kicad-route --out "{destination / "doctor"}" doctor --offline --no-canary --json')
        note("PASS: installed default KRT runtime and recorded two successful native repeats.")
        return 0
    except BaseException as error:
        receipt["status"] = "FAIL"
        receipt["error"] = {"type": type(error).__name__, "message": str(error)}
        receipt["traceback"] = traceback.format_exc()
        note(receipt["traceback"])
        return 1
    finally:
        receipt["finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        save()


if __name__ == "__main__":
    raise SystemExit(main())
