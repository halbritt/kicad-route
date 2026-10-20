"""Whole-environment preparation failures must not alter the active generation."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import venv

import pytest

from kicad_route import common, engine, evidence, installation, statemanifest


@pytest.fixture
def installer(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "VERSION").write_text("1.0")
    (source / "UPSTREAM.json").write_text('{}')
    (source / "requirements.txt").write_text('numpy==1.0\n')
    monkeypatch.setattr(common, "STATE", state)
    monkeypatch.setattr(engine, "SOURCE_ROOT", source)
    monkeypatch.setattr(engine, "ROOT", source)
    commands = []
    failure = [None]

    def run(argv, cwd, log):
        commands.append((argv, Path(cwd), Path(log)))
        if argv[1] == "venv":
            environment = Path(argv[2])
            # Substitute only external dependency/build commands. The venv,
            # imported version and collected manifest remain real.
            venv.EnvBuilder(with_pip=False).create(environment)
            site = next(environment.glob("lib/python*/site-packages"))
            version = (environment.parent / "router/VERSION").read_text()
            (site / "grid_router_probe.so").write_text("candidate module " + version)
            (site / "grid_router.py").write_text(f"__version__ = {version!r}\n")
            package = site / f"synthetic-{version}.dist-info"
            package.mkdir()
            (package / "METADATA").write_text(f"Name: synthetic\nVersion: {version}\n")
        failed = Path(log).name == failure[0]
        Path(log).write_text("failure" if failed else "ok")
        return {"exit_code": 1 if failed else 0, "argv": list(map(str, argv)), "log": str(log)}

    monkeypatch.setattr(common, "run_cmd", run)
    return state, source, commands, failure


@pytest.mark.parametrize("stage", ["venv.log", "krt-deps.log", "krt-build-router.log",
                                   "krt-startup-checks.log", "krt-route-help.log"])
def test_failed_preparation_preserves_active_manifest_and_generation(installer, stage):
    state, source, commands, failure = installer
    old, _ = installation.install([], state / "logs", sys.executable)
    manifest_bytes = (state / "manifest.json").read_bytes()
    prior_source = (old / "router/VERSION").read_bytes()
    source.joinpath("VERSION").write_text("2.0")
    failure[0] = stage
    with pytest.raises(RuntimeError, match="installation failed"):
        installation.install([], state / "logs", sys.executable)
    assert (state / "manifest.json").read_bytes() == manifest_bytes
    assert common.router_generation() == old
    assert (old / "router/VERSION").read_bytes() == prior_source
    assert subprocess.check_output([old / "venv/bin/python", "-c", "print('usable')"], text=True).strip() == "usable"
    failed = [json.loads(p.read_text()) for p in state.glob('.router-generations/*/preparation.json')]
    assert sorted(p["status"] for p in failed) == ["failed", "validated"]


def test_success_pins_existing_process_and_new_selection(installer):
    state, source, commands, failure = installer
    old, _ = installation.install([], state / "logs", sys.executable)
    # A live process retains absolute old-generation paths across activation.
    child = subprocess.Popen([old / "venv/bin/python", "-c",
                             "import pathlib,sys; print('ready',flush=True); input(); "
                             "print(pathlib.Path(sys.argv[1]).read_text())", old / "router/VERSION"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "ready"
    source.joinpath("VERSION").write_text("2.0")
    new, manifest = installation.install([], state / "logs", sys.executable)
    output, _ = child.communicate("continue\n", timeout=10)
    assert child.returncode == 0 and output.strip() == "1.0"
    assert common.router_generation() == new != old
    assert (new / "router/VERSION").read_text() == "2.0"
    assert manifest == json.loads((new / "manifest.json").read_text())
    assert (old / "venv/bin/python").exists()
    assert manifest["krt"]["source_sha256"] == engine.source_sha256(new / "router")
    # Recover the router selector while retaining current other-tool facts.
    current = statemanifest.read(state)
    current["kicad_route"]["version"] = "current wrapper metadata"
    current["krt"] = json.loads((old / "manifest.json").read_text())["krt"]
    current["determinism"].pop("krt", None)
    statemanifest.write(current, state)
    assert common.router_generation() == old
    restored = statemanifest.read(state)
    assert restored["kicad_route"]["version"] == "current wrapper metadata"
    assert statemanifest.verify(restored, state) == []


def test_cold_failure_and_interrupted_preparation_are_never_selected(installer):
    state, source, commands, failure = installer
    abandoned = state / ".router-generations/abandoned"
    abandoned.mkdir(parents=True)
    (abandoned / "preparation.json").write_text('{"status":"preparing"}')
    failure[0] = "krt-deps.log"
    with pytest.raises(RuntimeError):
        installation.install([], state / "logs", sys.executable)
    assert not (state / "manifest.json").exists()
    assert common.router_generation() is None
    failure[0] = None
    active, _ = installation.install([], state / "logs", sys.executable)
    assert active != abandoned and common.router_generation() == active
    assert (abandoned / "preparation.json").read_text() == '{"status":"preparing"}'


def test_updates_serialize_preparation_and_use_distinct_artifacts(installer, monkeypatch):
    state, source, commands, failure = installer
    original = common.run_cmd
    entered, release = threading.Event(), threading.Event()
    visits = []

    def paused(argv, cwd, log):
        if Path(log).name == "venv.log":
            visits.append(Path(log).parent.name)
            if len(visits) == 1:
                entered.set()
                assert release.wait(10)
        return original(argv, cwd, log)

    monkeypatch.setattr(common, "run_cmd", paused)
    results, errors = [], []
    def install():
        try:
            results.append(installation.install([], state / "logs", sys.executable))
        except BaseException as error:
            errors.append(error)
    first, second = threading.Thread(target=install), threading.Thread(target=install)
    first.start()
    assert entered.wait(10)
    second.start()
    time.sleep(0.05)
    assert len(visits) == 1
    release.set()
    first.join(10)
    second.join(10)
    assert not first.is_alive() and not second.is_alive() and errors == []
    assert len(set(visits)) == 2 and len(results) == 2
    assert common.router_generation() == results[-1][0]
    assert len({str(p) for _, _, p in commands}) == len(commands)


def test_late_extension_import_failure_preserves_previous_selection(installer, monkeypatch):
    state, source, commands, failure = installer
    active, _ = installation.install([], state / "logs", sys.executable)
    before = (state / "manifest.json").read_bytes()
    run = common.run_cmd
    def broken_import(argv, cwd, log):
        record = run(argv, cwd, log)
        if Path(log).name == "krt-route-help.log":
            site = next(Path(argv[0]).parents[1].glob("lib/python*/site-packages"))
            (site / "grid_router.py").write_text("raise ImportError('injected import failure')\n")
        return record
    monkeypatch.setattr(common, "run_cmd", broken_import)
    with pytest.raises(RuntimeError, match="import failed"):
        installation.install([], state / "logs", sys.executable)
    assert common.router_generation() == active
    assert (state / "manifest.json").read_bytes() == before


def test_cli_pins_source_and_interpreter_from_one_activation_snapshot(installer):
    state, source, commands, failure = installer
    old, before = installation.install([], state / "logs", sys.executable)
    source.joinpath("VERSION").write_text("2.0")
    new, after = installation.install([], state / "logs", sys.executable)
    statemanifest.write(before, state)
    pending = state / "next-manifest.json"
    common.write_json(pending, after)
    script = """
import json, os
from pathlib import Path
from kicad_route import common
read = common.load_json
def load(path):
    value = read(path)
    if Path(path) == common.STATE / 'manifest.json' and (common.STATE / 'next-manifest.json').exists():
        os.replace(common.STATE / 'next-manifest.json', path)
    return value
common.load_json = load
from kicad_route import cli
print(json.dumps({'source': str(cli.KRT_DIR.parent), 'venv': str(cli.KRT_VENV.parent)}))
"""
    import os
    env = dict(os.environ, KICAD_ROUTE_STATE=str(state),
               PYTHONPATH=str(Path(common.__file__).resolve().parents[1]), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.check_output([sys.executable, "-c", script], env=env, text=True)
    assert json.loads(result) == {"source": str(old), "venv": str(old)}
    assert common.router_generation() == new


def test_update_lock_excludes_other_cli_processes(installer):
    import os
    state, source, commands, failure = installer
    env = dict(os.environ, KICAD_ROUTE_STATE=str(state),
               PYTHONPATH=str(Path(common.__file__).resolve().parents[1]), PYTHONDONTWRITEBYTECODE="1")
    first_script = "from kicad_route.installation import update_lock\nwith update_lock():\n print('locked',flush=True)\n input()\n"
    second_script = "from kicad_route.installation import update_lock\nprint('ready',flush=True)\nwith update_lock():\n print('acquired',flush=True)\n"
    first = subprocess.Popen([sys.executable, "-c", first_script], env=env, text=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    second = None
    try:
        assert first.stdout.readline().strip() == "locked"
        second = subprocess.Popen([sys.executable, "-c", second_script], env=env, text=True,
                                  stdout=subprocess.PIPE)
        assert second.stdout.readline().strip() == "ready"
        import select
        assert select.select([second.stdout], [], [], 0.1)[0] == []
        first.communicate("release\n", timeout=10)
        output, _ = second.communicate(timeout=10)
        assert first.returncode == second.returncode == 0
        assert output.strip() == "acquired"
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)



def test_old_worker_evidence_stays_bound_to_its_executed_generation(installer):
    state, source, commands, failure = installer
    old, before = installation.install([], state / "logs", sys.executable)
    version = engine.identity(old / "router") | {"rust_router": "1.0", "runtime": {
        "source": str(old / "router"), "venv": str(old / "venv"), "generation": str(old)}}
    source.joinpath("VERSION").write_text("2.0")
    new, after = installation.install([], state / "logs", sys.executable)
    identity = evidence.backend_identity("krt", version, state)
    assert identity["source_sha256"] == before["krt"]["source_sha256"] != after["krt"]["source_sha256"]
    assert identity["extension_module_sha256"] == before["krt"]["extension_module"]["sha256"]
    assert identity["venv_packages_sha256"] == before["krt"]["venv"]["packages_sha256"]
    assert identity["venv_packages_sha256"] != after["krt"]["venv"]["packages_sha256"]
    assert statemanifest.verify(before, state) == []
    tools = evidence.tool_identity("krt", version, state=state)
    assert "krt.runtime_generation" in tools["state_manifest_drift"]
    assert tools["backend"] == identity
    proof = statemanifest.reproducibility("krt", state, runtime_generation=old)
    assert proof["measurement"] is False and "previous generation" in proof["basis"]


def test_update_reports_install_failure_without_refreshing_prior_manifest(installer, monkeypatch):
    from kicad_route import cli
    state, source, commands, failure = installer
    active, manifest = installation.install([], state / "logs", sys.executable)
    before = (state / "manifest.json").read_bytes()
    monkeypatch.setattr(common, "_out", state / "out")
    monkeypatch.setattr(cli, "KRT_DIR", active / "router")
    monkeypatch.setattr(cli, "KRT_VENV", active / "venv")
    # The other backend's network installation is outside this router failure case.
    monkeypatch.setattr(cli, "update_freerouting", lambda steps: "unchanged")
    failure[0] = "krt-deps.log"
    assert cli.update() == 1
    assert (state / "manifest.json").read_bytes() == before
    report = common.load_json(state / "out/update/update.json")
    assert any("router installation failed" in error for error in report["errors"])
    assert report["state_manifest"]["sha256"] == common.sha256(state / "manifest.json")
