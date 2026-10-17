"""kicad-route as a sandboxed writer (docs/sandbox.md; KICADROUTE-5, KICADROUTE-6).

- `doctor --json --offline` runs with no network (an unshared network namespace)
  and its stdout is one JSON record whose sandbox block lists the mounts, the
  environment and the network a sandbox must provide.
- Runs inside agent-ee's Bubblewrap sandbox shape (--unshare-net, STATE and the
  project read-only, one writable --out), with only the doctor's ro_binds added,
  give the same verdicts and candidates as unsandboxed runs, and record what they
  saw in result.json "sandbox".
- --require-readonly-state / KICAD_ROUTE_REQUIRE_READONLY_STATE=1 refuses (exit 4)
  when STATE is writable, missing or holds no backend; `update` and `corpus add`
  refuse under it.
- No run writes under STATE: a tree snapshot (every entry's type, size and mtime)
  is taken before and after each fixture run, sandboxed or not.
- --ledger PATH keeps the ledger (and its exit-4 repeat refusal) across fresh OUTs.

Sandboxed tests skip cleanly when bwrap is missing or cannot create namespaces.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from helpers import FIXTURE, ROOT, record

STATE = Path(os.environ.get("KICAD_ROUTE_STATE") or ROOT).resolve()
KR = [sys.executable, str(ROOT / "bin" / "kicad-route")]
AGENT_EE_SRC = Path(os.environ.get("AGENT_EE_SRC") or ROOT.parent / "agent-ee" / "src")
# When STATE is a dedicated directory the whole tree is snapshotted. When it is also a
# source checkout (the default, STATE = the checkout), only its installed parts are:
# the sources, git metadata and build output change under other processes' hands
# (merges, commits from worktrees, a checkout's own test runs).
INSTALLED = ("vendor", ".venv-krt", "corpus")
# The smoke round trips' settings (tests/test_routing.py): complete and valid on the fixture.
ROUTE_ARGS = {"krt": ["--budget", "180", "--name", "route_demo", "--default-clearance", "0.4"],
              "freerouting": ["--budget", "180", "--name", "route_demo", "--clearance-margin", "0.01",
                              "--passes", "100", "--backend-arg=--router.fanout.enabled=true"]}


def _bwrap_works():
    if not shutil.which("bwrap"):
        return False
    r = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--unshare-net", "--", "true"], capture_output=True)
    return r.returncode == 0


BWRAP = _bwrap_works()
needs_bwrap = pytest.mark.skipif(not BWRAP, reason="bwrap missing or unable to create namespaces")


def _mirror_sandbox_argv(inputs, work, argv, *, network=False, extra_ro=()):
    """agent-ee's executor.sandbox_argv (src/agent_ee/executor.py), for hosts without agent-ee."""
    command = ["bwrap", "--die-with-parent", "--new-session", "--unshare-pid",
               "--unshare-ipc", "--unshare-uts", "--ro-bind", "/usr", "/usr"]
    for path in ("/lib", "/lib64", "/bin", "/sbin"):
        if Path(path).is_symlink():
            command += ["--symlink", os.readlink(path), path]
        elif Path(path).exists():
            command += ["--ro-bind", path, path]
    command += ["--ro-bind", "/etc", "/etc", "--proc", "/proc", "--dev", "/dev",
                "--tmpfs", "/tmp", "--dir", "/home", "--dir", "/run",
                "--ro-bind", str(Path(inputs).resolve()), "/candidate",
                "--bind", str(Path(work).resolve()), "/work", "--chdir", "/work",
                "--clearenv", "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
                "--setenv", "HOME", "/work/home", "--setenv", "LANG", "C.UTF-8"]
    if not network:
        command += ["--unshare-net"]
    for source, dest in extra_ro:
        command += ["--ro-bind", str(Path(source).resolve()), dest]
    return command + ["--", *map(str, argv)]


def _sandbox_argv():
    if (AGENT_EE_SRC / "agent_ee" / "executor.py").exists():
        sys.path.insert(0, str(AGENT_EE_SRC))
        try:
            from agent_ee.executor import sandbox_argv
            return sandbox_argv, "agent_ee.executor.sandbox_argv"
        except Exception:
            pass
        finally:
            sys.path.remove(str(AGENT_EE_SRC))
    return _mirror_sandbox_argv, "mirror of agent_ee.executor.sandbox_argv"


def state_snapshot(root=STATE):
    """{relative path: (type, size, mtime_ns)} for every entry under STATE (or its
    installed parts, see INSTALLED): a created, removed or rewritten file changes its
    own entry or its directory's mtime."""
    root = Path(root)
    tops = [root / t for t in INSTALLED if (root / t).exists()] if (root / ".git").exists() else [root]
    snap = {}
    for top in tops:
        for p in [top, *top.rglob("*")]:
            st = p.lstat()
            snap[str(p.relative_to(root))] = (st.st_mode >> 12, st.st_size if not p.is_dir() else None,
                                              st.st_mtime_ns)
    return snap


def snapshot_diff(before, after):
    changed = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
    return {"created": sorted(after.keys() - before.keys())[:20], "removed": sorted(before.keys() - after.keys())[:20],
            "changed": changed[:20]}


COPPER_ITEM = re.compile(r"\n\t\((segment|via|arc)\b.*?\n\t\)", re.S)


def board_digest(path):
    """sha256 of a board with its track/via/arc items order-free and without their
    UUIDs (KiCad gives every new item a random UUID, so no two runs are byte-identical),
    plus a digest of everything else in the file, byte for byte."""
    text = Path(path).read_text()
    items = sorted(" ".join(re.sub(r'\(uuid "[^"]*"\)', "", m.group(0)).split()) for m in COPPER_ITEM.finditer(text))
    return {"copper_items": len(items), "copper": hashlib.sha256("\n".join(items).encode()).hexdigest(),
            "rest": hashlib.sha256(COPPER_ITEM.sub("", text).encode()).hexdigest()}


def plain_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ("KICAD_ROUTE_LEDGER", "KICAD_ROUTE_REQUIRE_READONLY_STATE")}
    env["KICAD_ROUTE_STATE"] = str(STATE)
    env.update(extra)
    return env


def kr(out, *args, env=None, timeout=900):
    return subprocess.run([*KR, "--out", str(out), *args], capture_output=True, text=True, timeout=timeout,
                          env=env or plain_env(), cwd=out.parent)


def result_of(stdout, base):
    """The run's result.json from its `[route] result:` line (path relative to the run's cwd)."""
    for line in stdout.splitlines():
        if line.startswith("[route] result: "):
            p = Path(line.split()[2])
            return json.loads((p if p.is_absolute() else base / p).read_text())
    raise AssertionError(f"no result line in:\n{stdout[-2000:]}")


# ------------------------------------------------------------------ doctor

@pytest.fixture(scope="module")
def doctor_record(tmp_path_factory):
    out = tmp_path_factory.mktemp("doctor") / "out"
    out.parent.mkdir(exist_ok=True)
    r = kr(out, "doctor", "--json", "--offline", "--no-canary", timeout=600)
    rec = json.loads(r.stdout)  # stdout holds the record and nothing else
    assert "route doctor:" in r.stderr and "route doctor:" not in r.stdout
    return r, rec, out


def test_doctor_json_offline_record(doctor_record):
    r, rec, out = doctor_record
    assert r.returncode == 0, r.stderr[-2000:]
    assert rec["schema"] == "kicad-route/doctor/1" and rec["status"] == "PASS" and rec["offline"] is True
    assert json.loads((out / "doctor" / "doctor.json").read_text()) == rec
    checks = {c["check"]: c for c in rec["checks"]}
    for name in ("KRT latest release", "Freerouting latest release", "canary (awkward net names)"):
        assert checks[name]["status"] == "NOT_RUN" and not checks[name]["required"], checks[name]
    sb = rec["sandbox"]
    ro = {e["path"] for e in sb["ro_paths"]}
    java = rec["versions"]["freerouting"]["java"].split(" (")[0]
    for need in (str(STATE), str(ROOT), str(Path(java).resolve().parents[1]),
                 str((STATE / ".venv-krt" / "bin" / "python").resolve()), "/usr/share/kicad"):
        assert need in ro, need
    assert all(any(p == b or p.startswith(b.rstrip("/") + "/") for b in sb["ro_binds"]) for p in ro)
    assert sb["rw_paths"][0]["path"] == str(out)
    env = {e["name"]: e for e in sb["env"]}
    assert env["KICAD_ROUTE_STATE"]["value"] == str(STATE) and env["HOME"]["required"]
    assert sb["network"]["run"] == [] and sb["network"]["doctor --offline"] == []
    assert any("github" in h for h in sb["network"]["update"]) and any("pypi" in h for h in sb["network"]["update"])
    assert "connect_probe" not in sb["observed"]["network"]  # --offline opens no socket at all
    record("doctor_offline", {"exit_code": r.returncode, "status": rec["status"], "sandbox": sb})


def _netns_runner(tmp_path, binds):
    """argv prefix that runs a command with no network: agent-ee's bwrap shape, else unshare -rn."""
    if BWRAP:
        sandbox_argv, which = _sandbox_argv()
        work = tmp_path / "work"
        work.mkdir()
        wrap = lambda argv: sandbox_argv(FIXTURE, work, argv, network=False, extra_ro=[(b, b) for b in binds])
        return wrap, work, which
    if shutil.which("unshare") and subprocess.run(["unshare", "-rn", "true"], capture_output=True).returncode == 0:
        return (lambda argv: ["unshare", "-rn", *argv]), None, "unshare -rn"
    pytest.skip("neither bwrap nor unshare -rn can create a network namespace here")


def test_doctor_json_offline_without_network(doctor_record, tmp_path):
    """The acceptance run: full doctor (canary included) in a network namespace with
    only loopback finishes without network timeouts and prints parseable JSON."""
    binds = [b for b in doctor_record[1]["sandbox"]["ro_binds"] if b not in ("/usr", "/etc")]
    wrap, work, which = _netns_runner(tmp_path, binds)
    out = "/work/out" if work else str(tmp_path / "out")
    inner = ["/usr/bin/env", f"KICAD_ROUTE_STATE={STATE}", *KR, "--out", out, "doctor", "--json", "--offline"]
    t0 = time.monotonic()
    r = subprocess.run(wrap(inner), capture_output=True, text=True, timeout=600)
    wall = time.monotonic() - t0
    rec = json.loads(r.stdout)
    assert r.returncode == 0, r.stderr[-3000:]
    assert rec["status"] == "PASS" and rec["required_failed"] == []
    assert rec["sandbox"]["observed"]["network"]["isolated"] is True
    assert rec["sandbox"]["observed"]["network"]["non_loopback_interfaces"] == 0
    assert "GitHub unreachable" not in r.stdout + r.stderr
    assert wall < 300, f"doctor took {wall:.0f}s without network"
    if work:  # bwrap: STATE bound read-only
        assert {c["check"]: c for c in rec["checks"]}["state read-only"]["status"] == "PASS"
    record("doctor_offline_netns", {"runner": which, "wall_s": round(wall, 1), "status": rec["status"],
                                    "observed": rec["sandbox"]["observed"]})


# ----------------------------------------------------------- state refusal

@pytest.mark.parametrize("case", ["writable", "missing", "no_backend", "env_var"])
def test_require_readonly_state_refuses(case, tmp_path):
    out = tmp_path / "out"
    flag = ["--require-readonly-state"]
    env = plain_env()
    if case == "writable" or case == "env_var":
        if not os.access(STATE, os.W_OK):
            pytest.skip("STATE is read-only here")
    if case == "missing":
        env["KICAD_ROUTE_STATE"] = str(tmp_path / "absent")
    if case == "no_backend":
        (tmp_path / "empty").mkdir()
        env["KICAD_ROUTE_STATE"] = str(tmp_path / "empty")
    if case == "env_var":
        flag = []
        env["KICAD_ROUTE_REQUIRE_READONLY_STATE"] = "1"
    r = kr(out, "run", "--backend", "krt", "--dir", str(FIXTURE), *flag, env=env, timeout=120)
    assert r.returncode == 4, r.stdout[-2000:] + r.stderr[-2000:]
    assert "[route] refused: --require-readonly-state: STATE" in r.stdout
    assert {"writable": "is writable", "env_var": "is writable", "missing": "does not exist",
            "no_backend": "holds no installed backend"}[case] in r.stdout
    assert not (out / "runs").exists(), "a refused run must not leave a run directory"


def test_campaign_refuses_writable_state(tmp_path):
    if not os.access(STATE, os.W_OK):
        pytest.skip("STATE is read-only here")
    r = kr(tmp_path / "out", "campaign", "--backend", "krt", "--dir", str(FIXTURE), "--require-readonly-state",
           timeout=120)
    assert r.returncode == 4 and "[campaign] refused: --require-readonly-state" in r.stdout
    assert not (tmp_path / "out" / "campaigns").exists()


def test_campaign_passes_sandbox_flags_to_runs(tmp_path):
    from kicad_route import campaign, cli
    c = campaign.Campaign.__new__(campaign.Campaign)
    c.args = cli.build_parser().parse_args(["campaign", "--backend", "krt", "--require-readonly-state",
                                           "--ledger", str(tmp_path / "l.jsonl")])
    argv = c.common_argv()
    assert "--require-readonly-state" in argv and argv[argv.index("--ledger") + 1] == str(tmp_path / "l.jsonl")
    c.args = cli.build_parser().parse_args(["campaign", "--backend", "krt"])
    assert "--require-readonly-state" not in c.common_argv() and "--ledger" not in c.common_argv()


def test_without_flag_state_is_not_checked(tmp_path):
    """No flag: a missing STATE is not a refusal; the run proceeds and fails as before."""
    out = tmp_path / "out"
    env = plain_env(KICAD_ROUTE_STATE=str(tmp_path / "absent"))
    r = kr(out, "run", "--backend", "krt", "--dir", str(FIXTURE), "--budget", "5", env=env, timeout=600)
    assert r.returncode == 1, r.stdout[-2000:]
    result = result_of(r.stdout, out.parent)
    assert result["process"]["status"] == "FAIL"
    assert result["sandbox"]["require_readonly_state"] is False
    assert result["sandbox"]["state"]["exists"] is False


def test_update_and_corpus_add_refuse_under_flag(monkeypatch, tmp_path):
    """Checked in-process with the writers replaced: the real `update` must never run here."""
    from kicad_route import cli, corpus
    called = []
    monkeypatch.setattr(cli, "update", lambda: called.append("update") or 0)
    monkeypatch.setattr(corpus, "cmd_corpus", lambda a: called.append(f"corpus {a.corpus_cmd}") or 0)
    monkeypatch.setenv("KICAD_ROUTE_REQUIRE_READONLY_STATE", "1")
    out = str(tmp_path / "out")
    assert cli.main(["--out", out, "update"]) == 4
    assert cli.main(["--out", out, "corpus", "add", "--dir", str(FIXTURE), "--backend", "krt"]) == 4
    assert called == []
    assert cli.main(["--out", out, "corpus", "list"]) == 0 and called == ["corpus list"]  # reading is fine
    monkeypatch.delenv("KICAD_ROUTE_REQUIRE_READONLY_STATE")
    assert cli.main(["--out", out, "update"]) == 0 and called[-1] == "update"


# ------------------------------------------------- fixture runs and STATE

_plain = {}


def plain_run(backend, tmp_path_factory):
    """One unsandboxed fixture run per backend (STATE writable), with a persistent
    --ledger, bracketed by STATE snapshots."""
    if backend not in _plain:
        base = tmp_path_factory.mktemp(f"plain-{backend}")
        ledger = base / "ledger.jsonl"
        before = state_snapshot()
        r = kr(base / "out", "run", "--backend", backend, "--dir", str(FIXTURE), *ROUTE_ARGS[backend],
               "--ledger", str(ledger))
        after = state_snapshot()
        _plain[backend] = {"proc": r, "result": result_of(r.stdout, base), "base": base, "ledger": ledger,
                           "state_diff": snapshot_diff(before, after), "state_entries": len(before)}
    return _plain[backend]


def test_run_writes_nothing_under_state(tmp_path_factory):
    p = plain_run("krt", tmp_path_factory)
    assert p["proc"].returncode == 0, p["proc"].stdout[-2000:]
    assert p["state_diff"] == {"created": [], "removed": [], "changed": []}, p["state_diff"]
    sb = p["result"]["sandbox"]
    assert sb["require_readonly_state"] is False and sb["state"]["path"] == str(STATE)
    record("state_untouched_plain_krt", {"state": str(STATE), "entries": p["state_entries"], "diff": p["state_diff"]})


def test_persistent_ledger(tmp_path_factory, tmp_path):
    """--ledger PATH outlives a fresh OUT: an identical failed attempt recorded there is refused."""
    p = plain_run("krt", tmp_path_factory)
    ledger = p["ledger"]
    lines = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert len(lines) == 1 and p["result"]["ledger"]["path"] == str(ledger)
    assert not (p["base"] / "out" / "ledger.jsonl").exists()
    failed = dict(lines[0], outcome="incomplete", exit_code=2)  # as if this attempt had failed
    shared = tmp_path / "ledger.jsonl"
    shared.write_text(json.dumps(failed) + "\n")
    r = kr(tmp_path / "fresh-out", "run", "--backend", "krt", "--dir", str(FIXTURE), *ROUTE_ARGS["krt"],
           "--ledger", str(shared), timeout=600)
    assert r.returncode == 4, r.stdout[-2000:]
    assert "[route] refused: identical attempt" in r.stdout
    assert not (tmp_path / "fresh-out" / "ledger.jsonl").exists()


@needs_bwrap
@pytest.mark.parametrize("backend", ["krt", "freerouting"])
def test_sandboxed_round_trip_matches_unsandboxed(backend, doctor_record, tmp_path_factory):
    plain = plain_run(backend, tmp_path_factory)
    assert plain["state_diff"] == {"created": [], "removed": [], "changed": []}, plain["state_diff"]
    sandbox_argv, which = _sandbox_argv()
    binds = [b for b in doctor_record[1]["sandbox"]["ro_binds"] if b not in ("/usr", "/etc")]
    work = tmp_path_factory.mktemp(f"sbx-{backend}")
    inner = ["/usr/bin/env", f"KICAD_ROUTE_STATE={STATE}", *KR, "--out", "/work/out", "run", "--backend", backend,
             "--dir", "/candidate", *ROUTE_ARGS[backend], "--require-readonly-state", "--ledger", "/work/ledger.jsonl"]
    argv = sandbox_argv(FIXTURE, work, inner, network=False, extra_ro=[(b, b) for b in binds])
    before = state_snapshot()
    r = subprocess.run(argv, capture_output=True, text=True, timeout=900)
    diff = snapshot_diff(before, state_snapshot())
    assert "[route] refused" not in r.stdout, r.stdout[-2000:]
    sbx = result_of(r.stdout, work)
    ref = plain["result"]
    assert r.returncode == plain["proc"].returncode == sbx["exit_code"], r.stdout[-2000:] + r.stderr[-2000:]
    for k in ("process", "routing", "validation"):
        assert sbx[k]["status"] == ref[k]["status"], k
    assert sbx["input"]["fingerprint"] == ref["input"]["fingerprint"]
    # same candidate: identical copper and identical rest of the file; only new items' UUIDs differ
    digest = board_digest(work / sbx["candidate"]["board"])
    assert digest == board_digest(plain["base"] / ref["candidate"]["board"])
    assert sbx["ledger"]["fingerprint"] == ref["ledger"]["fingerprint"]
    facts = sbx["sandbox"]
    assert facts["require_readonly_state"] is True
    assert facts["state"]["writable"] is False and facts["state"]["readonly_mount"] is True
    assert facts["network"]["isolated"] is True
    assert facts["network"]["connect_probe"]["result"] != "routable"
    assert diff == {"created": [], "removed": [], "changed": []}, diff
    record(f"sandboxed_{backend}", {
        "sandbox_argv": which, "binds": binds, "exit_code": r.returncode,
        "verdicts": {k: sbx[k]["status"] for k in ("process", "routing", "validation")},
        "candidate_digest": digest,
        "candidate_sha256": sbx["validation"]["delivered_board_sha256"],
        "unsandboxed_candidate_sha256": ref["validation"]["delivered_board_sha256"],
        "sandbox": facts, "state_diff": diff})
