"""Canonical builtin spelling and compatibility with persisted legacy KRT state."""
import copy
import hashlib
import json
import os
import subprocess
import sys

import pytest

from helpers import ROOT
from kicad_route import cli, common, engine, evidence, ledger, statemanifest


COMMANDS = [
    pytest.param(["run"], id="run"),
    pytest.param(["campaign"], id="campaign"),
    pytest.param(["corpus", "add", "--name", "example"], id="corpus-add"),
    pytest.param(["determinism"], id="determinism"),
    pytest.param(["canary"], id="canary"),
]


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("spelling", ["builtin", "krt"])
def test_explicit_backend_is_canonical_and_only_legacy_cli_spelling_warns(command, spelling, capsys):
    args = cli.build_parser().parse_args([*command, "--backend", spelling])
    assert args.backend == "builtin"
    captured = capsys.readouterr()
    assert captured.out == ""
    if spelling == "krt":
        assert "deprecat" in captured.err.lower()
        assert "krt" in captured.err and "builtin" in captured.err
    else:
        assert captured.err == ""


@pytest.mark.parametrize("command", COMMANDS[:-1])
def test_default_owned_backend_is_builtin_without_deprecation(command, capsys):
    assert cli.build_parser().parse_args(command).backend == "builtin"
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("spelling", [None, "builtin", "krt"])
def test_cli_alias_warning_does_not_corrupt_json_refusal(tmp_path, spelling):
    argv = [sys.executable, str(ROOT / "bin/kicad-route"), "--out", str(tmp_path / "out"),
            "run", "--dir", str(tmp_path / "missing-project"), "--json"]
    if spelling is not None:
        argv += ["--backend", spelling]
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1",
               KICAD_ROUTE_STATE=str(tmp_path / "unused-state"))
    completed = subprocess.run(argv, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert completed.returncode == 4, (completed.stdout, completed.stderr)
    result = json.loads(completed.stdout)
    assert result["command"] == "run" and result["reason_class"] == "input"
    assert "deprecat" not in completed.stdout.lower()
    assert ("deprecat" in completed.stderr.lower()) == (spelling == "krt")
    if spelling == "krt":
        assert "krt" in completed.stderr and "builtin" in completed.stderr


@pytest.fixture
def legacy_state(tmp_path):
    """Real temporary files and an old-format krt-keyed repeat proof; no native process."""
    state = tmp_path / "state"
    source = state / "vendor/krt"
    source.mkdir(parents=True)
    (source / "VERSION").write_text("1.0\n")
    (source / "UPSTREAM.json").write_text("{}\n")
    requirements = source / "requirements.txt"
    requirements.write_text("numpy==2.5.4\n")
    (source / "router.py").write_text("# synthetic source identity\n")
    venv = state / ".venv-krt"
    site = venv / "lib/python3.12/site-packages"
    metadata = site / "numpy-2.5.4.dist-info/METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Name: numpy\nVersion: 2.5.4\n\n")
    module = site / "grid_router.so"
    module.write_bytes(b"synthetic native module identity")
    (venv / "pyvenv.cfg").write_text("version = 3.12.3\n")
    native = tmp_path / "native-grader"
    native.write_bytes(b"synthetic native grader identity")
    packages = statemanifest.packages_sha256(["numpy==2.5.4"])
    source_hash = engine.source_sha256(source)
    module_hash = common.sha256(module)
    grader_hash = common.sha256(native)
    # The pre-rename algorithm includes the storage name krt in its hash input.
    old_id = hashlib.sha256(json.dumps({"backend": "krt", "source": source_hash,
        "extension_module": module_hash, "venv": packages, "python": "3.12.3",
        "kicad_cli": grader_hash}, sort_keys=True).encode()).hexdigest()
    manifest = {"krt": {"source_path": "vendor/krt", "source_sha256": source_hash,
        "version": "1.0", "extension_module": {"path": str(module.relative_to(state)), "sha256": module_hash},
        "requirements": {"path": "vendor/krt/requirements.txt", "sha256": common.sha256(requirements)},
        "venv": {"path": ".venv-krt", "packages_sha256": packages, "python_version": "3.12.3"}},
        "kicad": {"kicad_cli": {"path": str(native), "sha256": grader_hash}},
        "determinism": {"krt": {"backend": "krt", "build_id": old_id, "reproducible": True,
                                 "verified_utc": "2026-10-20T00:00:00Z"}}}
    statemanifest.write(manifest, state)
    return state, manifest, old_id


@pytest.mark.parametrize("name", ["builtin", "krt"])
def test_backend_rename_preserves_legacy_build_id_and_repeat_proof(legacy_state, name, capsys):
    state, manifest, old_id = legacy_state
    assert statemanifest.build_id(manifest, name) == old_id
    assert statemanifest.current_build_id(state, backend=name) == old_id
    old_tools = {"backend": {"name": "krt", "source_sha256": manifest["krt"]["source_sha256"],
        "extension_module_sha256": manifest["krt"]["extension_module"]["sha256"],
        "venv_packages_sha256": manifest["krt"]["venv"]["packages_sha256"],
        "venv_python_version": "3.12.3"}, "kicad_cli": manifest["kicad"]["kicad_cli"]}
    assert statemanifest.execution_build_id(old_tools, backend=name) == old_id
    proof = statemanifest.reproducibility(name, state)
    assert proof["reproducible"] is True and proof["measurement"] is True
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("name", ["builtin", "krt"])
def test_evidence_uses_canonical_backend_for_both_internal_spellings(legacy_state, name, capsys):
    state, manifest, _ = legacy_state
    version = {"build": "owned-example", "version": "1.0", "rust_router": "1.0",
               "source_sha256": manifest["krt"]["source_sha256"], "runtime": {"venv": str(state / ".venv-krt")}}
    identity = evidence.backend_identity(name, version, state=state)
    assert identity["name"] == "builtin"
    assert identity["extension_module_sha256"] == manifest["krt"]["extension_module"]["sha256"]
    assert identity["venv_packages_sha256"] == manifest["krt"]["venv"]["packages_sha256"]
    assert identity["venv_python_version"] == "3.12.3"
    assert capsys.readouterr() == ("", "")


def test_new_builtin_repeat_record_reuses_old_storage_without_losing_proof(legacy_state):
    state, manifest, old_id = legacy_state
    new_proof = copy.deepcopy(manifest["determinism"]["krt"])
    new_proof.update(backend="builtin", verified_utc="2026-10-21T00:00:00Z")
    statemanifest.record_determinism(new_proof, state)
    recorded = statemanifest.read(state)
    assert set(recorded["determinism"]) == {"krt"}
    assert recorded["determinism"]["krt"] == new_proof
    assert statemanifest.reproducibility("builtin", state)["measurement"] is True
    assert statemanifest.build_id(recorded, "builtin") == old_id


def test_internal_run_namespace_normalizes_old_alias_without_cli_warning(capsys):
    assert cli.run_namespace("krt").backend == "builtin"
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("spelling", ["builtin", "krt"])
@pytest.mark.parametrize("identity_kind", ["content", "legacy-version"])
def test_backend_rename_keeps_old_failed_ledger_attempt_blocked(tmp_path, monkeypatch, spelling, identity_kind):
    monkeypatch.delenv("KICAD_ROUTE_LEDGER", raising=False)
    # Captured from the pre-rename implementation, before any normalization edits.
    old_attempt = "853c3c6bf82296c22ea97fe7402a3d801de422c3c5433f14f367c2fbd75ee326"
    settings = {"backend": spelling, "budget_s": 180, "net_patterns": ["/B", "/A"]}
    if identity_kind == "content":
        old_fingerprint = "1d629034d1dabe5c5970c02511e31c8de64792ea1571270cdc67dbb4030dbbf5"
        version = {"execution_identity": {"name": spelling, "source_sha256": "owned-source",
            "extension_module_sha256": "extension", "venv_packages_sha256": "packages",
            "venv_python_version": "3.12.3"}}
    else:
        version = {"version": "1.2", "commit": "historical-revision", "rust_router": "0.23.1", "sha256": "module"}
        # Frozen legacy VERSION_KEYS fingerprint shape, independent of new helpers.
        old_fingerprint = hashlib.sha256(json.dumps({"attempt": old_attempt, "backend_version": version},
                                                     sort_keys=True).encode()).hexdigest()
    original_settings, original_version = copy.deepcopy(settings), copy.deepcopy(version)
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps({"attempt_key": old_attempt, "fingerprint": old_fingerprint,
        "backend": "krt", "outcome": "validation_fail", "exit_code": 1,
        "run_dir": "legacy-failed-run"}) + "\n")
    gate = ledger.check(path, "input", spelling, version, settings)
    assert gate["attempt_key"] == old_attempt and gate["fingerprint"] == old_fingerprint
    assert gate["refuse"] is True and gate["previous_runs"] == ["legacy-failed-run"]
    assert "legacy-failed-run" in gate["message"]
    result = {"backend": {"name": spelling, **version}, "input": {"fingerprint": "input", "project": "fixture"},
        "settings": settings, "run_dir": str(tmp_path / "forced-rerun"), "exit_code": 1,
        "process": {"status": "PASS"}, "routing": {"status": "COMPLETE", "unconnected_in_scope": 0},
        "validation": {"status": "FAIL"}}
    original_result = copy.deepcopy(result)
    assert ledger.from_result(result) == (old_fingerprint, old_attempt)
    assert result == original_result
    unknown_settings = dict(settings, backend="future-router")
    unknown_version = copy.deepcopy(version)
    if identity_kind == "content":
        unknown_version["execution_identity"]["name"] = "future-router"
    unknown_before = copy.deepcopy(unknown_version)
    unknown = ledger.check(path, "input", "future-router", unknown_version, unknown_settings)
    assert ledger.normalize_settings(unknown_settings)["backend"] == "future-router"
    assert unknown["attempt_key"] != old_attempt and unknown["fingerprint"] != old_fingerprint
    assert unknown["refuse"] is False and unknown_version == unknown_before
    # A new canonical result still records its public name while using old keys.
    result["backend"]["name"] = "builtin"
    forced = ledger.check(path, "input", "builtin", version, settings, force=True)
    appended = ledger.append(path, forced, result)
    assert appended["backend"] == ledger.entries(path)[-1]["backend"] == "builtin"
    assert appended["fingerprint"] == old_fingerprint and appended["attempt_key"] == old_attempt
    assert settings == original_settings and version == original_version
