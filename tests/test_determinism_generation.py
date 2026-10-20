"""Repeat proofs and manifest writers must stay bound to the executed generation."""
import copy
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from kicad_route import common, determinism, engine, installation, statemanifest


@pytest.fixture
def builds(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(common, "STATE", state)
    monkeypatch.setattr(common, "_out", tmp_path / "out")
    monkeypatch.setattr(common, "ensure_kicad_config", lambda: None)
    native = tmp_path / "kicad-cli"
    native.write_bytes(b"native grader")
    manifests, tools = {}, {}
    for name in ("A", "B"):
        generation = state / ".router-generations" / name
        source = generation / "router"
        source.mkdir(parents=True)
        (generation / "installation.json").write_text("{}")
        (source / "implementation.py").write_text(name)
        site = generation / "venv/lib/python3.12/site-packages"
        site.mkdir(parents=True)
        module = site / "grid_router.so"
        module.write_text("extension " + name)
        metadata = site / "numerical.dist-info/METADATA"
        metadata.parent.mkdir()
        metadata.write_text(f"Name: numerical\nVersion: {name}\n")
        packages = statemanifest.packages_sha256(statemanifest.venv_packages(generation / "venv"))
        manifest = {"krt": {"generation": str(generation.relative_to(state)),
            "source_path": str(source.relative_to(state)), "source_sha256": engine.source_sha256(source),
            "extension_module": {"path": str(module.relative_to(state)), "sha256": common.sha256(module)},
            "venv": {"path": str((generation / "venv").relative_to(state)), "packages_sha256": packages}},
            "kicad": {"kicad_cli": {"path": str(native), "sha256": common.sha256(native)}},
            "determinism": {}}
        tools[name] = {"backend": {"name": "krt", "source_sha256": manifest["krt"]["source_sha256"],
            "extension_module_sha256": common.sha256(module), "venv_packages_sha256": packages},
            "kicad_cli": {"version": "test", "sha256": common.sha256(native)}}
        manifests[name] = manifest
    statemanifest.write(manifests["A"], state)
    return state, manifests, tools


def result(tools):
    return {"run_dir": "executed-run", "exit_code": 0, "measurement": False,
        "process": {"status": "PASS", "commands": []},
        "routing": {"status": "COMPLETE", "unconnected_in_scope": 0},
        "validation": {"status": "PASS", "candidate_board_sha256": "final"},
        "evidence": {"candidate_copper_signature": {"sha256": "same geometry", "tracks": 1, "vias": 0}},
        "tool_identity": tools, "input": {"fingerprint": "same input"}}


def check(rows, after_last=None):
    remaining = iter(rows)
    def run(args):
        row = next(remaining)
        if row is rows[-1] and after_last:
            after_last()
        return row
    cli = SimpleNamespace(FIXTURE=Path("fixture"), run_namespace=lambda *a, **kw: None, run_attempt=run)
    return determinism.check(cli, runs=len(rows), record=True)[0]


def test_old_worker_proof_cannot_qualify_newly_activated_generation(builds):
    state, manifests, tools = builds
    old_id = statemanifest.build_id(manifests["A"])
    rec = check([result(tools["A"]), result(tools["A"])],
                after_last=lambda: statemanifest.write(manifests["B"], state))
    assert rec["reproducible"] is True and rec["build_id"] == old_id
    assert rec["build_id"] != statemanifest.build_id(manifests["B"])
    assert "active installation" in rec["record_error"]
    assert "recorded_in" not in rec
    assert statemanifest.read(state) == manifests["B"]


def test_equal_geometry_from_different_builds_is_not_a_repeat_proof(builds):
    state, manifests, tools = builds
    statemanifest.write(manifests["B"], state)
    rec = check([result(tools["A"]), result(tools["B"])])
    assert rec["reproducible"] is False
    assert rec["same"]["copper_geometry"] is True and rec["same"]["execution_build"] is False
    assert rec["build_id"] is None and rec["differs_at"] == "execution_build"
    assert "executed builds differ between repeats" in rec["reasons"]
    assert "record_error" in rec
    assert statemanifest.read(state)["determinism"] == {}


@pytest.mark.parametrize("block,key", [("backend", "source_sha256"),
    ("backend", "extension_module_sha256"), ("backend", "venv_packages_sha256"), ("kicad_cli", "sha256")])
def test_missing_execution_identity_never_uses_current_installation(builds, block, key):
    state, manifests, tools = builds
    incomplete = copy.deepcopy(tools["A"])
    del incomplete[block][key]
    rec = check([result(incomplete), result(incomplete)])
    assert rec["reproducible"] is False and rec["build_id"] is None
    assert "missing execution build identity" in rec["reasons"]
    assert statemanifest.read(state)["determinism"] == {}


def test_first_valid_repeat_can_qualify_without_prior_measurement_proof(builds):
    state, manifests, tools = builds
    rec = check([result(tools["A"]), result(tools["A"])])
    assert rec["reproducible"] is True and rec["same"]["execution_build"] is True
    assert rec["build_id"] == statemanifest.build_id(manifests["A"])
    assert "record_error" not in rec
    assert statemanifest.read(state)["determinism"]["krt"]["build_id"] == rec["build_id"]


def test_direct_repeat_write_rejects_another_build_without_changing_manifest(builds):
    state, manifests, tools = builds
    statemanifest.write(manifests["B"], state)
    before = statemanifest.manifest_path(state).read_bytes()
    with pytest.raises(RuntimeError, match="active installation"):
        statemanifest.record_determinism({"backend": "krt", "build_id": statemanifest.build_id(manifests["A"])}, state)
    assert statemanifest.manifest_path(state).read_bytes() == before


@pytest.mark.parametrize("operation", ["refresh", "record"])
def test_manifest_read_modify_write_serializes_with_activation(builds, monkeypatch, operation):
    state, manifests, tools = builds
    captured, release, activated = threading.Event(), threading.Event(), threading.Event()
    errors = []
    read = statemanifest.read
    if operation == "refresh":
        def collect(*args, **kwargs):
            old = read(state)
            captured.set()
            assert release.wait(5)
            return old
        monkeypatch.setattr(statemanifest, "collect", collect)
        write_record = lambda: statemanifest.refresh(state=state)
    else:
        def paused_read(*args, **kwargs):
            old = read(*args, **kwargs)
            if threading.current_thread().name == "manifest-writer" and not captured.is_set():
                captured.set()
                assert release.wait(5)
            return old
        monkeypatch.setattr(statemanifest, "read", paused_read)
        rec = {"backend": "krt", "build_id": statemanifest.build_id(manifests["A"])}
        write_record = lambda: statemanifest.record_determinism(rec, state)
    def writer():
        try:
            write_record()
        except BaseException as error:
            errors.append(error)
    def activation():
        try:
            with installation.update_lock(state):
                statemanifest.write(manifests["B"], state)
                activated.set()
        except BaseException as error:
            errors.append(error)
    first = threading.Thread(target=writer, name="manifest-writer")
    second = threading.Thread(target=activation)
    first.start()
    try:
        assert captured.wait(5)
        second.start()
        assert not activated.wait(.05), "activation must wait for the complete manifest transaction"
    finally:
        release.set()
        first.join(5)
        if second.ident:
            second.join(5)
    assert not first.is_alive() and not second.is_alive() and errors == []
    assert activated.is_set() and statemanifest.read(state) == manifests["B"]


def test_manifest_updates_are_reentrant_within_update_lock(builds, monkeypatch):
    state, manifests, tools = builds
    monkeypatch.setattr(statemanifest, "collect", lambda *a, **kw: copy.deepcopy(manifests["A"]))
    with installation.update_lock(state):
        statemanifest.refresh(state=state)
        statemanifest.record_determinism({"backend": "krt", "build_id": statemanifest.build_id(manifests["A"])}, state)
    assert "krt" in statemanifest.read(state)["determinism"]


@pytest.mark.parametrize("block,key", [("backend", "source_sha256"),
    ("backend", "extension_module_sha256"), ("backend", "venv_packages_sha256"), ("kicad_cli", "sha256")])
def test_each_execution_identity_field_changes_the_build(builds, block, key):
    state, manifests, tools = builds
    expected = statemanifest.build_id(manifests["A"])
    assert statemanifest.execution_build_id(tools["A"]) == expected
    changed = copy.deepcopy(tools["A"])
    changed[block][key] += "-different"
    assert statemanifest.execution_build_id(changed) != expected


def test_freerouting_execution_identity_matches_jar_runtime_and_native_build():
    tools = {"backend": {"jar_sha256": "jar", "java_version": "java"}, "kicad_cli": {"sha256": "native"}}
    manifest = {"freerouting": {"jar": {"sha256": "jar"}}, "java": {"version": "java"},
                "kicad": {"kicad_cli": {"sha256": "native"}}}
    assert statemanifest.execution_build_id(tools, "freerouting") == statemanifest.build_id(manifest, "freerouting")
    del tools["backend"]["java_version"]
    assert statemanifest.execution_build_id(tools, "freerouting") is None


def test_manifest_write_failure_is_retained_as_record_error(builds, monkeypatch):
    state, manifests, tools = builds
    def fail(*args, **kwargs):
        raise PermissionError("read-only state")
    monkeypatch.setattr(statemanifest, "record_determinism", fail)
    rec = check([result(tools["A"]), result(tools["A"])])
    assert rec["reproducible"] is True and rec["record_error"] == "read-only state"
    assert "recorded_in" not in rec
