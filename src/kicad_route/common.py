"""Paths, process, hashing and KiCad-environment helpers shared by kicad-route.

Two locations matter:

- TOOL_ROOT, this checkout. Installed backends live under STATE (default: the
  checkout itself; override with KICAD_ROUTE_STATE): STATE/vendor/krt,
  STATE/vendor/freerouting, STATE/.venv-krt and the generated STATE/vendor/skills.
- OUT, where a project's attempts go (default ./build/kicad-route; set with
  `kicad-route --out DIR` or KICAD_ROUTE_OUT). It holds runs/, the baseline cache
  and a hermetic KiCad configuration, and never touches the routed project.
"""
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PASS, FAIL, NOT_RUN, UNSUPPORTED = "PASS", "FAIL", "NOT_RUN", "UNSUPPORTED"

TOOL_ROOT = Path(__file__).resolve().parents[2]
STATE = Path(os.environ.get("KICAD_ROUTE_STATE") or TOOL_ROOT).expanduser().resolve()
KICAD_SHARE = Path("/usr/share/kicad")
# KiCad's Python bindings ship for the system interpreter.
PCBNEW_PYTHON = Path(os.environ.get("KICAD_ROUTE_PCBNEW_PYTHON") or "/usr/bin/python3")

# KiCad's SWIG module prints this assert on import; it is noise.
NOISE = re.compile(r'property\.h\(\d+\): assert "m_choices\.GetCount\(\) > 0"')

_out = None


def set_out(path):
    global _out
    _out = Path(path).expanduser().resolve()


def out_dir():
    if _out is None:
        set_out(os.environ.get("KICAD_ROUTE_OUT") or "build/kicad-route")
    return _out


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sha256_of(obj):
    """Digest of a JSON-serialisable value (canonical key order)."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def rel(path):
    """Path as recorded in results: relative to the working directory when inside it."""
    try:
        return str(Path(path).resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")


def load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def git(repo, *args):
    # --no-optional-locks: `describe --dirty` must not rewrite the index of a read-only checkout
    r = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def git_describe(repo):
    """`git describe --tags --always` plus "-dirty" for tracked changes, without writing
    to the repository: `describe --dirty` takes the index lock even with
    --no-optional-locks, which touches a read-only state directory (docs/sandbox.md)."""
    desc = git(repo, "describe", "--tags", "--always")
    if desc is None:
        return None
    dirty = git(repo, "status", "--porcelain", "--untracked-files=no")
    return desc + ("-dirty" if dirty else "")


def count_by(items, key):
    out = {}
    for it in items:
        out[it.get(key, "?")] = out.get(it.get(key, "?"), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------- env

def kicad_version():
    r = subprocess.run(["kicad-cli", "version"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def kicad_config_home():
    return out_dir() / ".kicad-config"


def ensure_kicad_config():
    """Hermetic KiCad config: stock global library tables, nothing from the GUI."""
    version = kicad_version() or "10.0"
    cfg = kicad_config_home() / ".".join(version.split(".")[:2])
    cfg.mkdir(parents=True, exist_ok=True)
    for table in ("sym-lib-table", "fp-lib-table"):
        src = KICAD_SHARE / "template" / table
        dst = cfg / table
        if src.exists() and (not dst.exists() or dst.read_bytes() != src.read_bytes()):
            shutil.copyfile(src, dst)
    return cfg


def tool_env():
    env = dict(os.environ)
    major = (kicad_version() or "10").split(".")[0]
    env["KICAD_CONFIG_HOME"] = str(kicad_config_home())
    env[f"KICAD{major}_SYMBOL_DIR"] = str(KICAD_SHARE / "symbols")
    env[f"KICAD{major}_FOOTPRINT_DIR"] = str(KICAD_SHARE / "footprints")
    env[f"KICAD{major}_TEMPLATE_DIR"] = str(KICAD_SHARE / "template")
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONPATH", None)
    # Backends and KiCad scripts run from STATE and the checkout: never write
    # __pycache__ there (STATE stays untouched outside `update`; docs/sandbox.md).
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_cmd(argv, cwd, log_path, timeout=1800):
    """Run one command, capture its combined output to a log, return a record."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    record = {"argv": [str(a) for a in argv], "cwd": rel(cwd), "log": rel(log_path)}
    try:
        proc = subprocess.run([str(a) for a in argv], cwd=cwd, env=tool_env(),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout)
        output = proc.stdout.decode("utf-8", "replace")
        record["exit_code"] = proc.returncode
    except FileNotFoundError as e:
        output = f"executable not found: {e}\n"
        record["exit_code"] = None
        record["error"] = "executable_not_found"
    except subprocess.TimeoutExpired as e:
        output = (e.stdout or b"").decode("utf-8", "replace") + f"\nTIMEOUT after {timeout}s\n"
        record["exit_code"] = None
        record["error"] = "timeout"
    record["duration_s"] = round(time.monotonic() - start, 3)
    log_path.write_text(output)
    record["output"] = "\n".join(l for l in output.splitlines() if not NOISE.search(l))
    return record


def public(record):
    return {k: v for k, v in record.items() if k != "output"}


def tool_versions():
    return {"kicad-cli": kicad_version(), "python": sys.version.split()[0],
            "kicad-route": git_describe(TOOL_ROOT)}


def new_run_dir(name):
    """OUT/runs/<name>/<stamp>, fresh every time, with a `latest` link beside it."""
    stamp = now_utc().strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = out_dir() / "runs" / name / stamp
    run_dir.mkdir(parents=True)  # fresh: never reuse a previous run's outputs
    latest = run_dir.parent / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(run_dir.name)
    return run_dir
