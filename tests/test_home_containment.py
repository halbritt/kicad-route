"""A run writes nothing under HOME (docs/sandbox.md, "HOME"; KICADROUTE-60).

KiCad's config (OUT/.kicad-config), cache and data, fontconfig's cache and every
other XDG writer go under OUT/.home through common.tool_env(); the Freerouting JVM
takes user.home from the password database, not $HOME, so it gets -Duser.home and
-Djava.util.prefs.userRoot under OUT/.home/java (common.jvm_home_args).

The fixture's KRT and Freerouting round trips, and `doctor --json --offline`, run
with HOME set to an empty read-only directory. They must pass, and the directory must
be unchanged afterwards. When strace works here, the runs are traced (--seccomp-bpf,
file syscalls only), and no process may attempt a write (open for writing or
creating, mkdir, rename, unlink, link) under that HOME, under the user's real home
from the password database (where the JVM would otherwise write), or anywhere else
outside OUT and /tmp. Without strace the directory comparison alone is the check.
"""
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import FIXTURE, ROOT, record

STATE = Path(os.environ.get("KICAD_ROUTE_STATE") or ROOT).resolve()
KR = [sys.executable, str(ROOT / "bin" / "kicad-route")]
# The smoke round trips' settings (tests/test_routing.py): complete and valid on the fixture.
ROUTE_ARGS = {"krt": ["--budget", "180", "--name", "route_demo", "--default-clearance", "0.4"],
              "freerouting": ["--budget", "180", "--name", "route_demo", "--clearance-margin", "0.01",
                              "--passes", "100", "--backend-arg=--router.fanout.enabled=true"]}
WRITE_CALLS = ("open", "openat", "creat", "mkdir", "mkdirat", "rename", "renameat", "renameat2",
               "unlink", "unlinkat", "rmdir", "link", "linkat", "symlink", "symlinkat", "truncate")
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def _strace_works():
    if not shutil.which("strace"):
        return False
    r = subprocess.run(["strace", "--seccomp-bpf", "-f", "-qq", "-o", "/dev/null", "-e", "trace=openat", "true"],
                       capture_output=True)
    return r.returncode == 0


STRACE = _strace_works()


def readonly_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o555)  # r-x: nothing can be created in it
    return home


def home_snapshot(home):
    """Every entry under HOME with its type, size and mtime, plus HOME's own."""
    return {str(p.relative_to(home)): (p.lstat().st_mode, p.lstat().st_size, p.lstat().st_mtime_ns)
            for p in [home, *home.rglob("*")]}


def traced(argv, trace):
    if not STRACE:
        return argv
    return ["strace", "--seccomp-bpf", "-f", "-qq", "-o", str(trace), "-e", "trace=" + ",".join(WRITE_CALLS),
            *argv]


CALL = re.compile(r"^\d+\s+(\w+)\((.*)")


def write_attempts(trace, out, home):
    """Write-intent file syscalls (successful or not) on absolute paths under HOME,
    under the real home, or outside OUT and /tmp. An unfinished call's line still
    holds its path and flags."""
    out, home = Path(out).resolve(), Path(home)
    found = {}
    for line in Path(trace).read_text(errors="replace").splitlines():
        m = CALL.match(line)
        if not m or m.group(1) not in WRITE_CALLS:
            continue
        call, args = m.groups()
        if call in ("open", "openat") and not re.search(r"O_WRONLY|O_RDWR|O_CREAT|O_TRUNC", args):
            continue
        for path in re.findall(r'"((?:[^"\\]|\\.)*)"', args):
            if not path.startswith("/") or path.startswith(("/dev/", "/proc/")):
                continue  # relative paths resolve against the run's cwd, which is in OUT
            p = Path(path)
            in_home = p == home or home in p.parents
            in_real_home = (p == REAL_HOME or REAL_HOME in p.parents) and out not in p.parents and p != out
            outside = not (p == out or out in p.parents or path.startswith("/tmp/"))
            if in_home or in_real_home or outside:
                key = f"{call} {re.sub(r'[0-9]{5,}', 'N', path)}"
                found[key] = found.get(key, 0) + 1
    return found


def contained_env(home):
    env = {k: v for k, v in os.environ.items() if k not in ("KICAD_ROUTE_LEDGER", "KICAD_ROUTE_REQUIRE_READONLY_STATE")}
    env.update(KICAD_ROUTE_STATE=str(STATE), HOME=str(home))
    return env


def run_contained(tmp_path, *args, timeout=1200):
    """kicad-route --out OUT ARGS with an empty read-only HOME, traced when possible."""
    home = readonly_home(tmp_path)
    out = tmp_path / "out"
    trace = tmp_path / "strace.log"
    before = home_snapshot(home)
    r = subprocess.run(traced([*KR, "--out", str(out), *args], trace), capture_output=True, text=True,
                       timeout=timeout, env=contained_env(home), cwd=tmp_path)
    after = home_snapshot(home)
    attempts = write_attempts(trace, out, home) if STRACE else None
    return r, out, home, before, after, attempts


def result_of(stdout, base):
    for line in stdout.splitlines():
        if line.startswith("[route] result: "):
            p = Path(line.split()[2])
            return json.loads((p if p.is_absolute() else base / p).read_text())
    raise AssertionError(f"no result line in:\n{stdout[-2000:]}")


def assert_home_facts(facts, out, home):
    assert facts["HOME"] == str(home) and facts["required"] is False
    assert facts["tool_home"] == str(out / ".home")
    for name in ("KICAD_CONFIG_HOME", "KICAD_CACHE_HOME", "KICAD_DOCUMENTS_HOME",
                 "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        assert Path(facts["env"][name]).is_relative_to(out), (name, facts["env"][name])
    assert f"-Duser.home={out / '.home' / 'java'}" in facts["jvm"]
    assert f"-Djava.util.prefs.userRoot={out / '.home' / 'java' / 'prefs'}" in facts["jvm"]


@pytest.mark.parametrize("backend", ["krt", "freerouting"])
def test_round_trip_leaves_readonly_home_untouched(backend, tmp_path):
    r, out, home, before, after, attempts = run_contained(
        tmp_path, "run", "--backend", backend, "--dir", str(FIXTURE), *ROUTE_ARGS[backend])
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    result = result_of(r.stdout, tmp_path)
    assert result["exit_code"] == 0
    for k in ("process", "validation"):
        assert result[k]["status"] == "PASS", (k, result[k].get("reasons"))
    assert result["routing"]["status"] == "COMPLETE"
    assert before == after, "the run changed the read-only HOME"
    assert not stat.S_IMODE(home.stat().st_mode) & 0o222
    if STRACE:
        assert attempts == {}, attempts
    assert_home_facts(result["sandbox"]["home"], out, home)
    # KiCad's cache and data did land, under OUT
    assert (out / ".home" / "cache" / "kicad").is_dir() and (out / ".home" / "data" / "kicad").is_dir()
    if backend == "freerouting":
        java = next(c for c in result["process"]["commands"] if "-jar" in c["argv"])
        assert f"-Duser.home={out / '.home' / 'java'}" in java["argv"]
        assert f"-Djava.util.prefs.userRoot={out / '.home' / 'java' / 'prefs'}" in java["argv"]
    record(f"home_containment_{backend}", {
        "strace": STRACE, "exit_code": r.returncode, "home": str(home), "home_entries": len(before),
        "write_attempts_outside_out": attempts, "sandbox_home": result["sandbox"]["home"],
        "tool_home": sorted(str(p.relative_to(out)) for p in (out / ".home").rglob("*"))[:40]})


def test_doctor_offline_lists_home_optional_and_leaves_it_untouched(tmp_path):
    """doctor runs kicad-cli and `java -jar freerouting --help`, which wrote
    ~/.config/kicad, ~/.config/freerouting and ~/.local/state/freerouting before."""
    r, out, home, before, after, attempts = run_contained(tmp_path, "doctor", "--json", "--offline", "--no-canary",
                                                          timeout=600)
    rec = json.loads(r.stdout)
    assert r.returncode == 0, r.stderr[-3000:]
    assert rec["status"] == "PASS", rec["required_failed"]
    env = {e["name"]: e for e in rec["sandbox"]["env"]}
    assert env["HOME"]["required"] is False
    assert "OUT/.home" in env["HOME"]["purpose"]
    assert_home_facts(rec["sandbox"]["observed"]["home"], out, home)
    assert before == after, "doctor changed the read-only HOME"
    if STRACE:
        assert attempts == {}, attempts
    record("home_containment_doctor", {"strace": STRACE, "home_env": env["HOME"],
                                       "write_attempts_outside_out": attempts})
