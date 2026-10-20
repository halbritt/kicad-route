"""Paths, process, hashing and KiCad-environment helpers shared by kicad-route.

Two locations matter:

- TOOL_ROOT, this checkout or the installed package. Runtime resources are
  under RESOURCE_ROOT; writable STATE defaults to the checkout for source
  installs and XDG_STATE_HOME/kicad-route for wheels (KICAD_ROUTE_STATE overrides).
  It contains backend generations, manifests and vendor/freerouting.
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

PACKAGE_ROOT = Path(__file__).resolve().parent
_source_root = PACKAGE_ROOT.parents[1]
CHECKOUT_ROOT = _source_root if (_source_root / "pyproject.toml").is_file() and \
    (_source_root / "bin" / "kicad-route").is_file() else None
TOOL_ROOT = CHECKOUT_ROOT or PACKAGE_ROOT
RESOURCE_ROOT = CHECKOUT_ROOT or PACKAGE_ROOT / "resources"
_default_state = CHECKOUT_ROOT or Path(os.environ.get("XDG_STATE_HOME") or
                                     Path.home() / ".local" / "state") / "kicad-route"
STATE = Path(os.environ.get("KICAD_ROUTE_STATE") or _default_state).expanduser().resolve()
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

class RouterSelectionError(RuntimeError):
    """The selected state cannot identify a usable routing generation."""


def state_manifest(state=None):
    """Read selection metadata without treating malformed state as a cold install."""
    state = Path(state or STATE).resolve()
    path = state / "manifest.json"
    try:
        manifest = load_json(path)
        if manifest is None and not path.exists():
            return None
    except (OSError, UnicodeError) as error:
        raise RouterSelectionError(f"cannot read state manifest {path}: {error}") from error
    if not isinstance(manifest, dict):
        raise RouterSelectionError(f"state manifest {path} is malformed or is not a JSON object")
    described = manifest.get("state_dir")
    if described is not None:
        if not isinstance(described, str) or not described:
            raise RouterSelectionError(f"state manifest {path} has an invalid state_dir")
        try:
            owner = Path(described).expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise RouterSelectionError(f"state manifest {path} has an invalid state_dir: {error}") from error
        if owner != state:
            raise RouterSelectionError(f"state manifest {path} describes {owner}, not {state}")
    if manifest.get("krt") is not None and not isinstance(manifest["krt"], dict):
        raise RouterSelectionError(f"state manifest {path} has a non-object krt record")
    return manifest


def router_generation(state=None):
    """Resolve one complete selection; invalid selections never select legacy paths."""
    state = Path(state or STATE).resolve()
    manifest = state_manifest(state) or {}
    generation = (manifest.get("krt") or {}).get("generation")
    if generation is None:
        return None
    if not isinstance(generation, str) or not generation:
        raise RouterSelectionError("krt.generation must be a nonempty path string")
    try:
        path = (state / generation).resolve()
        container = (state / ".router-generations").resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise RouterSelectionError(f"invalid router generation path: {error}") from error
    if path == container or not path.is_relative_to(container):
        raise RouterSelectionError("router generation escapes STATE/.router-generations")
    required = (path / "installation.json", path / "router" / "VERSION",
                path / "router" / "UPSTREAM.json", path / "venv" / "bin" / "python")
    if not all(item.is_file() for item in required):
        raise RouterSelectionError(f"selected router generation is incomplete: {path}")
    return path


def kicad_version():
    # even `kicad-cli version` writes its config, cache and data directories: contained too
    r = subprocess.run(["kicad-cli", "version"], capture_output=True, text=True, env=dict(os.environ, **home_env()))
    return r.stdout.strip() if r.returncode == 0 else None


def kicad_config_home():
    return out_dir() / ".kicad-config"


def tool_home():
    """OUT/.home: where the per-user files of the tools kicad-route runs land instead
    of the user's home, so a run writes nothing outside OUT (and /tmp) whatever HOME
    is (docs/sandbox.md, "HOME"; KICADROUTE-60)."""
    return out_dir() / ".home"


def home_env():
    """KiCad's config (OUT/.kicad-config), cache and data (KiCad 10 creates
    ~/.cache/kicad/10.0 and ~/.local/share/kicad/10.0/{symbols,footprints,...} on
    every start), fontconfig's cache, and every other XDG writer, all under OUT."""
    home = tool_home()
    return {"KICAD_CONFIG_HOME": str(kicad_config_home()),
            "KICAD_CACHE_HOME": str(home / "cache"), "KICAD_DOCUMENTS_HOME": str(home / "data"),
            "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_DATA_HOME": str(home / "data"), "XDG_STATE_HOME": str(home / "state")}


def jvm_home_args():
    """The JVM takes user.home from the password database, not $HOME, and Freerouting
    keeps its settings (~/.config/freerouting) and log (~/.local/state/freerouting)
    there; java.util.prefs defaults to ~/.java. Both go under OUT/.home/java."""
    java_home = tool_home() / "java"
    return [f"-Duser.home={java_home}", f"-Djava.util.prefs.userRoot={java_home / 'prefs'}"]


def home_facts():
    """result.json / doctor "sandbox.home": the caller's HOME and where the writes go instead."""
    return {"HOME": os.environ.get("HOME"), "required": False, "tool_home": str(tool_home()),
            "env": home_env(), "jvm": jvm_home_args()}


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
    env.update(home_env())  # nothing under HOME: config, cache and data stay in OUT
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
    parent = out_dir() / "runs" / name
    parent.mkdir(parents=True, exist_ok=True)
    stamp = now_utc().strftime("%Y%m%dT%H%M%S.%fZ")
    for n in range(1000):
        run_dir = parent / (stamp if n == 0 else f"{stamp}-{n}")
        try:
            run_dir.mkdir()  # fresh: never reuse a previous (or a concurrent) run's outputs
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError(f"no fresh run directory under {parent} for stamp {stamp}")
    point_latest(parent / "latest", run_dir.name)
    return run_dir


def point_latest(link, target):
    """Point the symlink `link` at `target`, atomically: a new link is created
    beside it and renamed over it, so concurrent runs sharing a label never see
    `latest` missing and never fail with FileExistsError (the last rename wins)."""
    link = Path(link)
    tmp = link.with_name(f".{link.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    tmp.symlink_to(target)
    try:
        os.replace(tmp, link)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
