"""Owned routing must work without the historical vendor/krt checkout."""
import json
import os
import subprocess

from helpers import CLI, FIXTURE, ROOT


def test_route_uses_owned_source_with_no_external_checkout(tmp_path):
    # This state contains only the routing environment. No KRT source or git
    # metadata is available under it, and updates cannot repair a missing import.
    state = tmp_path / "state"
    state.mkdir()
    (state / ".venv-krt").symlink_to(ROOT / ".venv-krt", target_is_directory=True)
    directory = tmp_path / "attempt"
    env = dict(os.environ, KICAD_ROUTE_STATE=str(state), KICAD_ROUTE_LEDGER="off")
    process = subprocess.run([
        *CLI, "run", "--backend", "krt", "--dir", str(FIXTURE),
        "--default-clearance", "0.4", "--run-dir", str(directory), "--json",
    ], env=env, capture_output=True, text=True, timeout=300)
    assert process.returncode == 0, process.stderr[-5000:]
    result = json.loads(process.stdout)
    assert result["validation"]["status"] == "PASS"
    assert result["routing"]["status"] == "COMPLETE"
    assert result["backend"]["source"] == "https://github.com/halbritt/kicad-route"
    assert not (state / "vendor" / "krt").exists()
    assert all("vendor/krt" not in str(command["argv"])
               for command in result["process"]["commands"])


def test_owned_source_starts_without_git_metadata(tmp_path, monkeypatch):
    from kicad_route import common, engine
    monkeypatch.setattr(common, "TOOL_ROOT", tmp_path)
    version = engine.identity()
    assert len(version["source_sha256"]) == 64
    assert version["commit"] is None
    assert version["upstream"]["fork_commit"] == "0494a05e30063b849c9cf1b9ac02098073d82216"
