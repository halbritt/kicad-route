"""Backend process groups (docs/hygiene.md, "Process groups").

run_budgeted starts every backend as the leader of its own session and process
group, so the KRT interpreter, the Freerouting JVM and anything they spawn
(kicad-cli refills, helpers, shell wrappers) can be signalled together. A stop,
a stall or a budget kill signals the whole group; when the leader has exited,
whatever it left behind in the group is terminated as well. Nothing a backend
started outlives its run_budgeted call.
"""
import os
import signal
import subprocess
import time
from pathlib import Path

POPEN_KW = {"start_new_session": True}   # leader of a new session and process group
TERM_S = 10      # SIGTERM -> SIGKILL wait for the leader
LEFTOVER_S = 5   # SIGTERM -> SIGKILL wait for what the leader left behind


def signal_group(proc, sig):
    """Send `sig` to the backend's whole process group. False if the group is gone."""
    try:
        os.killpg(proc.pid, sig)
        return True
    except ProcessLookupError:
        return False


def members(pgid):
    """Live (non-zombie) processes in group `pgid`, from /proc."""
    found = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
        except OSError:
            continue
        # pid (comm) state ppid pgrp ...; comm may contain spaces and parentheses
        fields = stat[stat.rindex(")") + 2:].split()
        if fields[0] != "Z" and int(fields[2]) == pgid:
            found.append(int(d.name))
    return found


def _wait_group(pgid, timeout):
    deadline = time.monotonic() + timeout
    while True:
        left = members(pgid)
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(0.05)


def terminate(proc, term_timeout=None):
    """SIGTERM the group, wait up to `term_timeout` s (TERM_S) for the leader, then SIGKILL
    the group. Returns once the leader is reaped."""
    signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=TERM_S if term_timeout is None else term_timeout)
    except subprocess.TimeoutExpired:
        signal_group(proc, signal.SIGKILL)
        proc.wait()


def reap_leftovers(proc, term_timeout=None):
    """After the leader has exited: terminate, then kill, whatever is still in its
    group. Returns the pids that were left behind (empty when the group was clean)."""
    left = members(proc.pid)
    if not left:
        return []
    signal_group(proc, signal.SIGTERM)
    if _wait_group(proc.pid, LEFTOVER_S if term_timeout is None else term_timeout):
        signal_group(proc, signal.SIGKILL)
        _wait_group(proc.pid, 2)
    return left
