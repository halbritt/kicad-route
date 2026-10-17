"""Shared harness for the kicad-route integration tests: the fixture, the output
location, and helpers that run the CLI and record evidence.

Runs go under $KICAD_ROUTE_TEST_OUT (set by `kicad-route smoke`; default
build/kicad-route/smoke in this checkout). Each test may write a small JSON record
there with record(); `kicad-route smoke --publish DIR` copies them for committing.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIXTURE = ROOT / "fixtures" / "route_demo"
OUT = Path(os.environ.get("KICAD_ROUTE_TEST_OUT") or ROOT / "build" / "kicad-route" / "smoke").resolve()
RUNS = OUT / "runs" / "route_demo"
CLI = [sys.executable, str(ROOT / "bin" / "kicad-route"), "--out", str(OUT)]
PROTECTED = {"tracks": 5, "vias": 2}  # the locked /SIG_B route in the fixture


def record(name, data):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(data, indent=2, default=str) + "\n")


def tree_hash(path):
    h = hashlib.sha256()
    for p in sorted(Path(path).rglob("*")):
        if p.is_file():
            h.update(p.relative_to(path).as_posix().encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


def cli(*args, timeout=900):
    OUT.mkdir(parents=True, exist_ok=True)
    return subprocess.run([*CLI, *args], capture_output=True, text=True, timeout=timeout, cwd=OUT)


def run(*args, timeout=900):
    """`kicad-route ARGS` for a run (on the fixture unless --dir is given) -> (process, run_dir, result)."""
    if "--dir" not in args:
        args = (*args, "--dir", str(FIXTURE))
    r = cli(*args, timeout=timeout)
    run_dir = None
    for line in r.stdout.splitlines():
        if line.startswith("[route] result: "):
            run_dir = OUT / line.split()[2].rsplit("/result.json", 1)[0]
    assert run_dir, f"kicad-route printed no result (exit {r.returncode}):\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}"
    result = json.loads((run_dir / "result.json").read_text())
    return r, run_dir, result


def summary(result):
    return {k: result[k] for k in ("backend", "settings", "scope", "unsupported", "routing", "exit_code")} | {
        "process": {k: result["process"].get(k) for k in ("status", "reasons", "exit_code", "runtime_s",
                                                         "backend_report", "bridge", "project_writeback")},
        "validation": result["validation"], "run_dir": result["run_dir"]}
