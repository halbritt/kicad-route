"""Process hygiene around backend runs (docs/hygiene.md, "Process groups" and
"Budget overrun"): run_budgeted kills the backend's whole process group, so a
backend that ignores the stop file and SIGTERM is still killed and nothing it
spawned survives; the `latest` run link is replaced atomically. Mock backends
are small Python scripts written to tmp_path; no router is needed."""
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from helpers import record
from kicad_route import cli as route
from kicad_route import common, ledger, procgroup, stall


def alive(pid):
    """Running (not gone and not a zombie)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rindex(")") + 2] != "Z"


def gone_within(pids, timeout=5):
    deadline = time.monotonic() + timeout
    while any(alive(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    return [p for p in pids if alive(p)]


# A backend that starts a child, which starts a grandchild (a JVM helper, a shell
# wrapper, a kicad-cli refill), then keeps "searching" forever. IGNORE_TERM makes
# a process ignore SIGTERM; nobody looks at the stop file.
BACKEND = r'''
import os, signal, subprocess, sys, time
pids, role = sys.argv[1], sys.argv[2]
ignore = set(os.environ.get("IGNORE_TERM", "").split(","))
if role in ignore:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
nxt = {"backend": "child", "child": "grandchild"}.get(role)
if nxt:
    subprocess.Popen([sys.executable, __file__, pids, nxt])
with open(os.path.join(pids, role), "w") as f:
    f.write(str(os.getpid()))
print(f"{role} {os.getpid()} up", flush=True)
if role == "backend" and os.environ.get("EXIT_AFTER"):
    time.sleep(float(os.environ["EXIT_AFTER"]))   # exits, leaving its children running
    sys.exit(0)
while True:
    print("No route found after 1000 iterations (forward), trying backward...", flush=True)
    time.sleep(0.2)
'''


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setattr(procgroup, "TERM_S", 1)
    monkeypatch.setattr(procgroup, "LEFTOVER_S", 1)
    script = tmp_path / "backend.py"
    script.write_text(BACKEND)
    pids = tmp_path / "pids"
    pids.mkdir()
    spawned = []

    def start(budget, env=None, **kw):
        stop = tmp_path / "STOP"
        rec = route.run_budgeted([sys.executable, script, pids, "backend"], tmp_path, tmp_path / "backend.log",
                                 budget, dict(env or {}, KICAD_STOP_FILE=str(stop)), stop_file=stop, **kw)
        found = {p.name: int(p.read_text()) for p in pids.iterdir()}
        spawned.extend(found.values())
        return rec, found

    yield start
    for pid in spawned:   # never leave a mock behind, whatever the test did
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


@pytest.mark.parametrize("ignore", ["", "backend", "backend,child,grandchild"])
def test_uncooperative_backend_and_its_children_are_killed(backend, ignore):
    """KICADROUTE-25/42: the backend ignores the stop file (and, in turn, SIGTERM):
    it is terminated, then killed, and its child and grandchild die with it."""
    t0 = time.monotonic()
    rec, pids = backend(2, {"IGNORE_TERM": ignore}, grace=1)
    took = time.monotonic() - t0
    record(f"process_hygiene_uncooperative_{ignore.replace(',', '_') or 'none'}",
           {k: rec.get(k) for k in ("budget_reached", "stop_requested", "killed", "exit_code", "duration_s",
                                    "stop_to_exit_s", "leftover_children_killed")} | {"pids": pids})
    assert set(pids) == {"backend", "child", "grandchild"}
    assert rec["budget_reached"] and rec["stop_requested"] and rec["killed"]
    # SIGTERM reached the leader unless it ignored it; then SIGKILL did
    assert rec["exit_code"] == (-signal.SIGKILL if "backend" in ignore else -signal.SIGTERM)
    assert not gone_within(pids.values()), "a backend process survived the kill"
    assert "leftover_children_killed" not in rec      # the group kill took them, not the sweep
    assert 1 <= rec["stop_to_exit_s"] < 5 and took < 10


def test_children_left_behind_by_an_exiting_backend_are_killed(backend):
    """The backend exits on its own (as a cooperative stop does) but leaves a child
    and grandchild running, one of them deaf to SIGTERM: both are killed."""
    rec, pids = backend(30, {"EXIT_AFTER": "1", "IGNORE_TERM": "grandchild"})
    assert rec["exit_code"] == 0 and not rec.get("killed") and not rec.get("budget_reached")
    assert sorted(rec["leftover_children_killed"]) == sorted([pids["child"], pids["grandchild"]])
    assert not gone_within(pids.values())


def test_stalled_uncooperative_backend_is_killed(backend, tmp_path):
    """With a stall limit the same uncooperative backend (search chatter only) is
    stopped as stalled, then killed, with no survivors."""
    log = tmp_path / "backend.log"
    rec, pids = backend(60, {"IGNORE_TERM": "backend"}, grace=1, monitor=stall.Monitor("krt", log, 1))
    assert rec["stalled"] and rec["stop_requested"] and rec["killed"] and not rec.get("budget_reached")
    assert rec["exit_code"] == -signal.SIGKILL
    assert rec["duration_s"] < 15
    assert not gone_within(pids.values())


def test_interrupt_leaves_no_survivors(tmp_path, monkeypatch):
    """Ctrl-C while waiting: the backend's new session no longer gets the terminal's
    SIGINT, so run_budgeted kills the group itself before re-raising."""
    monkeypatch.setattr(procgroup, "TERM_S", 1)
    monkeypatch.setattr(procgroup, "LEFTOVER_S", 1)
    script = tmp_path / "backend.py"
    script.write_text(BACKEND)
    pids = tmp_path / "pids"
    pids.mkdir()

    class Interrupted:
        stall_s = None

        def wait(self, proc, budget):
            deadline = time.monotonic() + 10
            while len(list(pids.iterdir())) < 3 and time.monotonic() < deadline:
                time.sleep(0.05)
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        route.run_budgeted([sys.executable, script, pids, "backend"], tmp_path, tmp_path / "b.log", 30,
                           {"IGNORE_TERM": "backend,child"}, monitor=Interrupted())
    found = [int(p.read_text()) for p in pids.iterdir()]
    assert len(found) == 3
    assert not gone_within(found)


# ------------------------------------------------------------ KICADROUTE-37

def test_krt_overrun_hint_names_the_pass_that_ignores_the_stop_file():
    assert "KICAD_NET_RESCUE=0" in route.krt_overrun_hint("net rescue")
    assert "KICAD_PLANE_FINALIZE=0" in route.krt_overrun_hint("plane finalize")
    assert "KICAD_PLANE_FINALIZE=0" in route.krt_overrun_hint("final reconciliation")
    assert "--budget" in route.krt_overrun_hint("routing single-ended net(s)")
    stalled = route.krt_overrun_hint("net rescue", stalled=True)
    assert "--stall" in stalled and "budget" not in stalled


def test_krt_overrun_knobs_are_never_forced():
    """The kill switches stay the caller's choice: krt_route sets neither."""
    assert "KICAD_NET_RESCUE" not in route.KRT_ENV and "KICAD_PLANE_FINALIZE" not in route.KRT_ENV


# ------------------------------------------------------------ KICADROUTE-36

def test_latest_link_survives_concurrent_runs(tmp_path, monkeypatch):
    """Many runs sharing one label at once: no FileExistsError (neither from the
    `latest` link nor from a shared stamp), every run gets its own directory, and
    `latest` always points at one of them."""
    monkeypatch.setattr(common, "_out", tmp_path)
    made, errors = [], []
    seen_missing, seen_latest = [], threading.Event()

    def worker():
        try:
            for _ in range(40):
                made.append(common.new_run_dir("proj/label"))
        except Exception as e:      # noqa: BLE001 - the test reports any failure
            errors.append(repr(e))

    parent = tmp_path / "runs" / "proj" / "label"
    stop = threading.Event()

    def watcher():
        while not stop.is_set():
            if (parent / "latest").is_symlink():
                seen_latest.set()
            elif seen_latest.is_set():
                seen_missing.append(time.monotonic())
            time.sleep(0)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    w = threading.Thread(target=watcher)
    w.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    w.join()
    assert not errors, errors[:3]
    assert len(made) == len(set(made)) == 12 * 40
    latest = parent / "latest"
    assert latest.is_symlink() and latest.resolve() in {p.resolve() for p in made}
    assert not [p for p in parent.iterdir() if p.name.endswith(".tmp")]
    # once `latest` exists it never disappears in between
    assert seen_latest.is_set() and not seen_missing, f"latest missing {len(seen_missing)} times"


def test_discard_run_dir_repoints_latest(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "_out", tmp_path)
    a, b = common.new_run_dir("p/x"), common.new_run_dir("p/x")
    ledger.discard_run_dir(b)
    assert (a.parent / "latest").resolve() == a.resolve()
    ledger.discard_run_dir(a)
    assert not (tmp_path / "runs" / "p" / "x").exists()
