"""Environment major-version reuse never replaces authoritative tool identity."""
import os
import time
from types import SimpleNamespace

import pytest

from kicad_route import common, statemanifest


@pytest.fixture
def probe(tmp_path, monkeypatch):
    executable = tmp_path / "kicad-cli"
    executable.write_text("#!/bin/sh\n# version 10\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    original_home_env = common.home_env
    monkeypatch.setattr(common, "home_env", lambda: {
        "XDG_CONFIG_HOME": os.environ.get("TEST_TOOL_HOME", "initial")})
    observed = SimpleNamespace(executable=executable, calls=[], version="10.0.7", exit_code=0,
                               original_home_env=original_home_env)

    def run(argv, **kwargs):
        assert argv == ["kicad-cli", "version"]
        observed.calls.append((argv, kwargs))
        return SimpleNamespace(stdout=observed.version, returncode=observed.exit_code)

    monkeypatch.setattr(common.subprocess, "run", run)
    return observed


def test_environment_probes_stay_fresh_outside_attempt(probe):
    common.tool_env()
    common.tool_env()
    assert len(probe.calls) == 2


def test_attempt_reuses_only_major_and_rebuilds_environment(probe, monkeypatch):
    with common.tool_environment_scope():
        first = common.tool_env()
        monkeypatch.setenv("TEST_TOOL_HOME", "changed")
        monkeypatch.setenv("HOME", "/changed/home")
        monkeypatch.setenv("VIRTUAL_ENV", "/inherited/venv")
        monkeypatch.setenv("PYTHONPATH", "/inherited/modules")
        second = common.tool_env()
        assert first["XDG_CONFIG_HOME"] == "initial"
        assert second["XDG_CONFIG_HOME"] == "changed" and second["HOME"] == "/changed/home"
        assert "VIRTUAL_ENV" not in second and "PYTHONPATH" not in second
        assert "KICAD10_SYMBOL_DIR" in second and second["PYTHONDONTWRITEBYTECODE"] == "1"
    assert len(probe.calls) == 1


def test_next_attempt_observes_fresh_version_even_with_unchanged_file(probe):
    with common.tool_environment_scope():
        assert "KICAD10_SYMBOL_DIR" in common.tool_env()
    probe.version = "11.0.0"
    with common.tool_environment_scope():
        assert "KICAD11_SYMBOL_DIR" in common.tool_env()
        common.tool_env()
    assert len(probe.calls) == 2


def test_out_paths_are_rebuilt_for_each_environment(probe, tmp_path, monkeypatch):
    monkeypatch.setattr(common, "home_env", probe.original_home_env)
    monkeypatch.setattr(common, "_out", None)
    with common.tool_environment_scope():
        common.set_out(tmp_path / "first")
        first = common.tool_env()
        common.set_out(tmp_path / "second")
        second = common.tool_env()
    assert first["KICAD_CONFIG_HOME"] == str(tmp_path / "first/.kicad-config")
    assert second["KICAD_CONFIG_HOME"] == str(tmp_path / "second/.kicad-config")
    assert first["XDG_CACHE_HOME"] != second["XDG_CACHE_HOME"]
    assert len(probe.calls) == 1


def test_same_path_executable_replacement_invalidates_major(probe):
    with common.tool_environment_scope():
        common.tool_env()
        replacement = probe.executable.with_name("replacement")
        replacement.write_text("#!/bin/sh\n# version 11\n")
        replacement.chmod(0o755)
        before = probe.executable.stat()
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(probe.executable)
        probe.version = "11.0.0"
        assert "KICAD11_SYMBOL_DIR" in common.tool_env()
    assert len(probe.calls) == 2


def test_same_inode_size_and_mtime_rewrite_invalidates_major(probe):
    with common.tool_environment_scope():
        common.tool_env()
        before = probe.executable.stat()
        time.sleep(0.01)  # Cross the filesystem's timestamp granularity before rewriting.
        probe.executable.write_text("#!/bin/sh\n# version 11\n")
        os.utime(probe.executable, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = probe.executable.stat()
        assert (before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_ino, after.st_size, after.st_mtime_ns)
        assert before.st_ctime_ns != after.st_ctime_ns
        probe.version = "11.0.0"
        assert "KICAD11_SYMBOL_DIR" in common.tool_env()
    assert len(probe.calls) == 2


def test_path_switch_to_another_executable_invalidates_major(probe, tmp_path, monkeypatch):
    with common.tool_environment_scope():
        common.tool_env()
        other = tmp_path / "other"
        other.mkdir()
        executable = other / "kicad-cli"
        executable.write_text("#!/bin/sh\n# other version\n")
        executable.chmod(0o755)
        monkeypatch.setenv("PATH", str(other))
        probe.version = "11.0.0"
        assert "KICAD11_SYMBOL_DIR" in common.tool_env()
    assert len(probe.calls) == 2


def test_failed_probe_is_not_cached(probe):
    probe.exit_code = 1
    with common.tool_environment_scope():
        assert "KICAD10_SYMBOL_DIR" in common.tool_env()
        probe.exit_code, probe.version = 0, "11.0.0"
        assert "KICAD11_SYMBOL_DIR" in common.tool_env()
        common.tool_env()
    assert len(probe.calls) == 2


def test_symlink_retarget_invalidates_major(probe, tmp_path, monkeypatch):
    directory = tmp_path / "aliases"
    directory.mkdir()
    alias = directory / "kicad-cli"
    alias.symlink_to(probe.executable)
    monkeypatch.setenv("PATH", str(directory))
    with common.tool_environment_scope():
        common.tool_env()
        other = tmp_path / "other-cli"
        other.write_text("#!/bin/sh\n# other version\n")
        other.chmod(0o755)
        alias.unlink()
        alias.symlink_to(other)
        probe.version = "11.0.0"
        assert "KICAD11_SYMBOL_DIR" in common.tool_env()
    assert len(probe.calls) == 2


def test_missing_executable_fingerprint_is_not_cached(probe, monkeypatch):
    monkeypatch.setattr(common.shutil, "which", lambda name: None)
    with common.tool_environment_scope():
        common.tool_env()
        common.tool_env()
    assert len(probe.calls) == 2


def test_exception_restores_fresh_probes_outside_attempt(probe):
    with pytest.raises(RuntimeError, match="attempt failed"):
        with common.tool_environment_scope():
            common.tool_env()
            raise RuntimeError("attempt failed")
    common.tool_env()
    common.tool_env()
    assert len(probe.calls) == 3


def test_nested_attempt_restores_outer_scope_and_leaves_no_global_cache(probe):
    with common.tool_environment_scope():
        assert "KICAD10_SYMBOL_DIR" in common.tool_env()
        probe.version = "11.0.0"
        with common.tool_environment_scope():
            assert "KICAD11_SYMBOL_DIR" in common.tool_env()
        assert "KICAD10_SYMBOL_DIR" in common.tool_env()
    assert "KICAD11_SYMBOL_DIR" in common.tool_env()
    assert len(probe.calls) == 3


def test_decorated_attempt_starts_fresh_for_every_invocation(probe):
    @common.tool_environment_scope()
    def attempt():
        common.tool_env()
        common.tool_env()

    attempt()
    attempt()
    assert len(probe.calls) == 2


def test_authoritative_version_and_tool_versions_remain_fresh(probe, monkeypatch):
    monkeypatch.setattr(common, "git_describe", lambda root: "test-source")
    with common.tool_environment_scope():
        common.tool_env()
        probe.version = "10.0.8"
        assert common.kicad_version() == "10.0.8"
        probe.version = "10.0.9"
        assert common.tool_versions()["kicad-cli"] == "10.0.9"
        common.tool_env()
    assert len(probe.calls) == 3


def test_full_cli_identity_remains_fresh_inside_attempt(probe):
    with common.tool_environment_scope():
        common.tool_env()
        first = statemanifest.kicad_cli_identity()
        probe.version = "10.0.8"
        second = statemanifest.kicad_cli_identity()
        common.tool_env()
    assert first["version"] == "10.0.7" and second["version"] == "10.0.8"
    assert first["sha256"] == second["sha256"] == common.sha256(probe.executable)
    assert len(probe.calls) == 3
