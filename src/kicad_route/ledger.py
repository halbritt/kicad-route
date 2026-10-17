"""Run ledger: OUT/ledger.jsonl, one line per finished attempt.

An attempt's fingerprint covers what decides its outcome:

    attempt key   input fingerprint + backend name + normalized settings (scope,
                  budget, protection, tightening controls, backend args/env, stall)
    fingerprint   attempt key + backend version identity (version, commit,
                  Rust router, jar sha256)

Before routing, an attempt whose fingerprint already finished with a failing
outcome class (process FAIL, validation FAIL, routing INCOMPLETE) is refused
(exit 4) with the earlier run named: repeating it changes nothing. `--force`
runs it anyway and records the forced rerun. The same attempt key under a
different backend version is allowed, noted "upstream changed since <run>".

The fingerprint is derived from a result.json alone, so `compare` can show it
for runs that predate the ledger.
"""
import hashlib
import json
import os
import shutil
from pathlib import Path

from . import common

VERSION_KEYS = ("version", "commit", "rust_router", "sha256")
FAILING = ("process_fail", "validation_fail", "incomplete")
REFUSED_EXIT = 4


def _h(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def normalize_settings(settings):
    """Settings that can change an outcome, in a canonical form. None values are
    dropped (a new optional setting left unset keeps old fingerprints valid);
    net patterns and classes are order-free, backend args are not."""
    out = {}
    for k, v in sorted(settings.items()):
        if v is None or v == {} and k == "backend_env":
            continue
        if k in ("net_patterns", "netclasses"):
            v = sorted(v)
        out[k] = v
    return out


def backend_identity(version):
    return {k: version.get(k) for k in VERSION_KEYS if version.get(k) is not None}


def attempt_key(input_fp, backend, settings):
    return _h({"input": input_fp, "backend": backend, "settings": normalize_settings(settings)})


def fingerprint(input_fp, backend, version, settings):
    return _h({"attempt": attempt_key(input_fp, backend, settings), "backend_version": backend_identity(version)})


def from_result(result):
    """(fingerprint, attempt key) of a finished run's result.json."""
    b = result["backend"]
    return (fingerprint(result["input"]["fingerprint"], b["name"], b, result["settings"]),
            attempt_key(result["input"]["fingerprint"], b["name"], result["settings"]))


def outcome_class(result):
    if result["process"]["status"] != common.PASS:
        return "process_fail"
    if result["validation"]["status"] != common.PASS:
        return "validation_fail"
    if result["routing"]["status"] != "COMPLETE":
        return "incomplete"
    return "pass"


def entries(path):
    out = []
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return out
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn line from an interrupted write
    return out


def check(path, input_fp, backend, version, settings, force=False):
    """Gate before routing. Returns a dict: refuse (bool), message, and the
    fields recorded in result.json and the ledger line."""
    fp = fingerprint(input_fp, backend, version, settings)
    key = attempt_key(input_fp, backend, settings)
    same = [e for e in entries(path) if e.get("fingerprint") == fp]
    failed = [e for e in same if e.get("outcome") in FAILING]
    upstream = [e for e in entries(path) if e.get("attempt_key") == key and e.get("fingerprint") != fp]
    gate = {"fingerprint": fp, "short": fp[:12], "attempt_key": key, "refuse": False, "forced": False,
            "previous_runs": [e["run_dir"] for e in same][-5:], "notes": []}
    if failed and os.environ.get("KICAD_ROUTE_LEDGER") == "off":
        gate["notes"].append(f"ledger refusal disabled (KICAD_ROUTE_LEDGER=off); repeats {failed[-1]['run_dir']}")
        failed = []
    if failed:
        last = failed[-1]
        msg = (f"identical attempt (fingerprint {fp[:12]}) already finished {last['outcome']} "
               f"(exit {last.get('exit_code')}) in {last['run_dir']}")
        if force:
            gate.update(forced=True, forced_over=last["run_dir"])
            gate["notes"].append(f"forced rerun: {msg}")
        else:
            gate.update(refuse=True, message=msg + ": rerunning it changes nothing; change the input, scope or "
                        "settings, or pass --force to repeat it anyway")
    if upstream:
        last = upstream[-1]
        old = last.get("backend_version", {})
        gate["upstream_changed_since"] = last["run_dir"]
        gate["notes"].append(f"upstream changed since {last['run_dir']} ({backend} "
                             f"{old.get('version')} -> {version.get('version')}): same attempt, new backend build")
    return gate


def append(path, gate, result):
    rec = {"fingerprint": gate["fingerprint"], "attempt_key": gate["attempt_key"],
           "run_dir": str(Path(result["run_dir"]).resolve()), "backend": result["backend"]["name"],
           "backend_version": backend_identity(result["backend"]),
           "input_fingerprint": result["input"]["fingerprint"], "project": result["input"]["project"],
           "outcome": outcome_class(result), "exit_code": result["exit_code"],
           "unconnected_in_scope": result["routing"].get("unconnected_in_scope"),
           "forced": gate.get("forced", False), "finished_utc": result.get("finished_utc")}
    if gate.get("upstream_changed_since"):
        rec["upstream_changed_since"] = gate["upstream_changed_since"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:  # one write per line: concurrent appends do not interleave
        f.write(json.dumps(rec, sort_keys=True) + "\n")
    return rec


def discard_run_dir(run_dir):
    """Remove a run directory that was refused before routing, and point
    `latest` back at the newest remaining run (or drop it)."""
    run_dir = Path(run_dir)
    parent = run_dir.parent
    shutil.rmtree(run_dir, ignore_errors=True)
    latest = parent / "latest"
    if latest.is_symlink() and not latest.exists():
        latest.unlink()
        remaining = sorted(p.name for p in parent.iterdir() if p.is_dir() and not p.is_symlink())
        if remaining:
            latest.symlink_to(remaining[-1])
    if parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
