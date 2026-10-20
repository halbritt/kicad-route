"""Prepare complete router generations; manifest replacement is activation.

Generation directories are never reused or pruned by update. Their absolute paths
keep virtualenv scripts and processes launched before an update usable.
"""
import contextlib
import fcntl
from pathlib import Path
import shutil
import uuid
import threading

from . import common, engine, statemanifest


_UPDATE_MUTEX = threading.RLock()
_locked_state = None


@contextlib.contextmanager
def update_lock(state=None):
    global _locked_state
    state = Path(state or common.STATE).resolve()
    with _UPDATE_MUTEX:
        if _locked_state == state:
            yield
            return
        if _locked_state is not None:
            raise RuntimeError("cannot nest updates for different state directories")
        state.mkdir(parents=True, exist_ok=True)
        with (state / ".router-update.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            _locked_state = state
            try:
                yield
            finally:
                _locked_state = None


def install(steps, log_dir, python):
    """Return the activated generation and manifest, or preserve the old selection.

    Failed/interrupted preparations remain unselected for diagnosis. A later install
    always uses a fresh directory; it never trusts or resumes partial dependencies.
    """
    state, log_dir = common.STATE, Path(log_dir)
    with update_lock(state):
        generation = state / ".router-generations" / uuid.uuid4().hex
        generation.mkdir(parents=True)
        logs = log_dir / generation.name
        logs.mkdir(parents=True)
        preparation = generation / "preparation.json"
        common.write_json(preparation, {"status": "preparing", "logs": str(logs)})

        def checked(argv, cwd, name):
            record = common.run_cmd(argv, cwd, logs / name)
            steps.append(record)
            if record.get("exit_code") != 0:
                raise RuntimeError(f"router installation failed (see {record['log']})")

        try:
            source = generation / "router"
            expected_source = engine.source_sha256(engine.SOURCE_ROOT)
            shutil.copytree(engine.SOURCE_ROOT, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            if engine.source_sha256(source) != expected_source:
                raise RuntimeError("routing source changed while preparing the generation; retry update")
            venv = generation / "venv"
            route_python = venv / "bin" / "python"
            checked(["uv", "venv", venv, "--python", python], state, "venv.log")
            checked(["uv", "pip", "install", "--python", route_python, "-r", source / "requirements.txt"],
                    state, "krt-deps.log")
            # Build local caches before activation: routing keeps STATE read-only.
            # Isolation ignores an inherited PYTHONPYCACHEPREFIX; -B prevents
            # incidental writes while compileall explicitly compiles this venv.
            checked([route_python, "-I", "-B", "-c",
                     "import compileall,sys,sysconfig; "
                     "paths=dict.fromkeys(sysconfig.get_paths()[key] for key in ('purelib','platlib')); "
                     "sys.exit(not all(compileall.compile_dir(path,quiet=1,workers=1) for path in paths))"],
                    state, "krt-bytecode.log")
            checked([route_python, source / "build_router.py", "--target-dir", generation / "build"],
                    state, "krt-build-router.log")
            checked([route_python, "py_router/startup_checks.py"], source, "krt-startup-checks.log")
            checked([route_python, "py_router/route.py", "--help"], source, "krt-route-help.log")
            module = statemanifest.extension_module(venv)
            if module is None:
                raise RuntimeError("validated routing environment has no grid_router extension")
            common.write_json(generation / "installation.json", {
                "source": engine.identity(source), "extension_sha256": common.sha256(module),
                "packages": statemanifest.venv_packages(venv), "validated_utc": common.now_utc().isoformat(),
            })
            manifest = statemanifest.collect(state, logs / "krt-build-router.log", written_by="update",
                                             previous=statemanifest.read(state), generation=generation)
            if not manifest["krt"]["grid_router_version"]:
                raise RuntimeError("grid_router import failed while collecting the candidate manifest")
            common.write_json(generation / "manifest.json", manifest)
            common.write_json(preparation, {"status": "validated", "logs": str(logs)})
            # One atomic file selects source, interpreter, dependencies and module.
            statemanifest.write(manifest, state)
        except BaseException as error:
            common.write_json(preparation, {"status": "failed", "error": str(error), "logs": str(logs)})
            raise
        return generation, manifest
