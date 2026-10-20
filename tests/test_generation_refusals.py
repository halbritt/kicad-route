"""Invalid generation selections refuse before routing instead of failing at import."""
import json
import os
import subprocess
import sys

import pytest

from helpers import FIXTURE, ROOT


@pytest.mark.parametrize("case,message", [
    ("broken_json", "malformed"), ("non_object", "JSON object"), ("null", "JSON object"),
    ("numeric_state_dir", "invalid state_dir"), ("empty_state_dir", "invalid state_dir"),
    ("non_object_krt", "non-object krt"), ("numeric_generation", "path string"),
    ("empty_generation", "path string"), ("absolute_escape", "escapes"),
    ("relative_escape", "escapes"), ("symlink_escape", "escapes"),
    ("missing_generation", "incomplete"), ("receipt_only", "incomplete"),
    ("missing_interpreter", "incomplete"), ("other_manifest", "describes"),
])
def test_invalid_selection_has_json_refusal_and_never_uses_legacy(tmp_path, case, message):
    state, out = tmp_path / "state", tmp_path / "out"
    legacy = state / ".venv-krt/bin"
    legacy.mkdir(parents=True)
    (legacy / "python").write_text("unused legacy interpreter sentinel")
    generation = state / ".router-generations/selected"
    manifest = {"state_dir": str(state), "krt": {"generation": str(generation.relative_to(state))}}
    if case == "non_object":
        manifest = []
    elif case == "null":
        manifest = None
    elif case == "numeric_state_dir":
        manifest["state_dir"] = 42
    elif case == "empty_state_dir":
        manifest["state_dir"] = ""
    elif case == "non_object_krt":
        manifest["krt"] = []
    elif case == "numeric_generation":
        manifest["krt"]["generation"] = 42
    elif case == "empty_generation":
        manifest["krt"]["generation"] = ""
    elif case == "absolute_escape":
        manifest["krt"]["generation"] = str(tmp_path / "outside")
    elif case == "relative_escape":
        manifest["krt"]["generation"] = "../outside"
    elif case == "symlink_escape":
        generation.parent.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        generation.symlink_to(outside, target_is_directory=True)
    elif case in ("receipt_only", "missing_interpreter"):
        generation.mkdir(parents=True)
        (generation / "installation.json").write_text("{}")
        if case == "missing_interpreter":
            (generation / "router").mkdir()
            (generation / "router/VERSION").write_text("1.0")
            (generation / "router/UPSTREAM.json").write_text("{}")
    elif case == "other_manifest":
        manifest["state_dir"] = str(tmp_path / "another-state")
    path = state / "manifest.json"
    path.write_text("{" if case == "broken_json" else json.dumps(manifest))
    before = {str(p.relative_to(state)): p.read_bytes() for p in state.rglob("*") if p.is_file()}
    env = dict(os.environ, KICAD_ROUTE_STATE=str(state), PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.run([sys.executable, ROOT / "bin/kicad-route", "--out", out,
                              "run", "--backend", "krt", "--dir", FIXTURE, "--json"],
                             env=env, capture_output=True, text=True, timeout=30)
    assert process.returncode == 4, process.stdout + process.stderr
    result = json.loads(process.stdout)
    assert result["kind"] == "refusal" and result["reason_class"] == "state"
    assert result["exit_code"] == 4 and result["command"] == "run"
    assert message in result["message"]
    assert json.loads((out / "refusals/refusal-latest.json").read_text()) == result
    assert not (out / "runs").exists() and "Traceback" not in process.stderr
    assert before == {str(p.relative_to(state)): p.read_bytes() for p in state.rglob("*") if p.is_file()}


@pytest.mark.parametrize("legacy_manifest", [False, True])
def test_absent_generation_keeps_explicit_legacy_layout(tmp_path, legacy_manifest):
    state = tmp_path / "state"
    state.mkdir()
    if legacy_manifest:
        (state / "manifest.json").write_text(json.dumps({"state_dir": str(state), "krt": {"generation": None}}))
    env = dict(os.environ, KICAD_ROUTE_STATE=str(state), PYTHONPATH=str(ROOT / "src"),
               PYTHONDONTWRITEBYTECODE="1")
    code = "from kicad_route import cli; print(cli.route_python())"
    process = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == str(state / ".venv-krt/bin/python")
