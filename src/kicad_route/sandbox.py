"""Running kicad-route as a sandboxed writer (docs/sandbox.md).

Three things live here:

- state_status(): is STATE (the installed backends) present, and can this process
  write to it? `run` and `campaign` with --require-readonly-state (or
  KICAD_ROUTE_REQUIRE_READONLY_STATE=1) refuse with exit 4 (reason_class state)
  before routing when STATE is missing, holds no installed backend, is not the
  directory its state manifest describes (or has drifted from it), or is writable;
  `update` and `corpus add` write STATE, so they refuse under the flag unconditionally.
- facts(): what this process sees (network namespace, STATE, the flag, where the
  tools' per-user files go instead of HOME), recorded as
  result.json "sandbox" and in doctor's JSON.
- requirements(): what a sandbox has to provide for kicad-route to work: the
  read-only paths (resolved, at their host paths), the one writable OUT, the
  environment and the network each command needs. `doctor --json` reports it so a
  launcher (agent-ee's executor) can bind exactly that.

Nothing here opens a network connection: the interface list comes from /proc/net,
and the optional probe is a UDP connect() to a TEST-NET-1 address, which only asks
the kernel for a route and sends no packet.
"""
import errno
import os
import shutil
import socket
from pathlib import Path

from . import common

ENV_FLAG = "KICAD_ROUTE_REQUIRE_READONLY_STATE"
# STATE's installed parts (only those present are reported).
STATE_PARTS = ("vendor/krt", ".venv-krt", "vendor/freerouting", "vendor/skills", "corpus")
BACKEND_PARTS = ("vendor/krt", "vendor/freerouting")
PROBE = ("192.0.2.1", 9)  # TEST-NET-1 (RFC 5737): never a real host

UPDATE_HOSTS = ["api.github.com:443", "github.com:443", "objects.githubusercontent.com:443",
                "release-assets.githubusercontent.com:443", "pypi.org:443", "files.pythonhosted.org:443"]


def require_readonly_state(flag=False):
    """The flag or KICAD_ROUTE_REQUIRE_READONLY_STATE=1 (also true/yes/on)."""
    return bool(flag) or os.environ.get(ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def _readonly_mount(path):
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except OSError:
        return None


def _writable(path):
    # access(2) answers EROFS for a read-only mount as well as checking permissions.
    return os.access(path, os.W_OK, effective_ids=os.access in os.supports_effective_ids)


def state_status(state=None):
    state = Path(state or common.STATE)
    rec = {"path": str(state), "exists": state.is_dir(), "installed": [], "writable": None,
           "writable_parts": [], "readonly_mount": None}
    if not rec["exists"]:
        return rec
    rec["installed"] = [p for p in STATE_PARTS if (state / p).exists()]
    rec["readonly_mount"] = _readonly_mount(state)
    rec["writable"] = _writable(state)
    rec["writable_parts"] = [p for p in rec["installed"] if _writable(state / p)]
    return rec


def state_refusal(command="run", state=None):
    """None when STATE is present, installed and read-only to this process; else why not."""
    st = state_status(state)
    p = st["path"]
    if not st["exists"]:
        return f"--require-readonly-state: STATE {p} does not exist (set KICAD_ROUTE_STATE and mount it)"
    if not any(part in st["installed"] for part in BACKEND_PARTS):
        return (f"--require-readonly-state: STATE {p} holds no installed backend "
                f"({', '.join(BACKEND_PARTS)}); it is not a kicad-route state directory")
    manifest = manifest_problem(st["path"])
    if manifest:
        return f"--require-readonly-state: {manifest}"
    if st["writable"] or st["writable_parts"]:
        where = ", ".join(["."] * bool(st["writable"]) + st["writable_parts"])
        return (f"--require-readonly-state: STATE {p} is writable by this process ({where}); "
                f"mount it read-only before `{command}`")
    return None


def manifest_problem(state):
    """Why STATE is not the directory its state manifest (STATE/manifest.json,
    statemanifest.py) describes, or None: no manifest, a malformed one, one written
    for another directory, or drift between it and the files on disk."""
    from . import statemanifest
    state = Path(state)
    path = statemanifest.manifest_path(state)
    m = statemanifest.read(state)
    if m is None:
        return f"no state manifest at {path} (run `kicad-route state-manifest` where STATE is writable)"
    problems = statemanifest.schema_problems(m)
    if problems:
        return f"state manifest {path} is malformed: {'; '.join(problems[:3])}"
    described = Path(m.get("state_dir") or "").expanduser()
    if described.resolve() != state.resolve():
        return f"state manifest {path} describes {described}, not {state}"
    drift = statemanifest.verify(m, state)
    if drift:
        return (f"STATE {state} differs from its manifest: "
                + "; ".join(f"{d['item']} recorded {str(d['recorded'])[:16]} now {str(d['current'])[:16]}"
                            for d in drift[:3]))
    return None


def writer_refusal(command, flag=False):
    """`update` and `corpus add` write STATE: under the flag they never run."""
    if require_readonly_state(flag):
        return (f"--require-readonly-state / {ENV_FLAG}=1: `{command}` writes the state directory "
                f"{common.STATE}, which this environment requires to stay read-only")
    return None


# ------------------------------------------------------------------ network

def _proc_lines(path):
    try:
        return Path(path).read_text().splitlines()
    except OSError:
        return None


def network_facts(probe=True):
    """Interfaces and routes of this process's network namespace; with probe, a
    UDP connect() (route lookup only, no packet) to a TEST-NET-1 address."""
    dev = _proc_lines("/proc/net/dev")
    ifaces = sorted(l.split(":", 1)[0].strip() for l in (dev or [])[2:] if ":" in l)
    routes = [l.split()[0] for l in (_proc_lines("/proc/net/route") or [])[1:] if l.split()]
    routes += [l.split()[-1] for l in (_proc_lines("/proc/net/ipv6_route") or []) if l.split()]
    others = [i for i in ifaces if i != "lo"]
    rec = {"interfaces": len(ifaces) if dev is not None else None, "non_loopback_interfaces": len(others),
           "non_loopback_examples": others[:5], "non_loopback_routes": len([r for r in routes if r != "lo"])}
    try:
        rec["namespace"] = os.readlink("/proc/self/ns/net")
    except OSError:
        rec["namespace"] = None
    isolated = dev is not None and not others
    if probe:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(PROBE)
            result = "routable"
        except OSError as e:
            result = errno.errorcode.get(e.errno, str(e.errno))
        finally:
            s.close()
        rec["connect_probe"] = {"target": f"{PROBE[0]}:{PROBE[1]}/udp", "sends_packet": False,
                                "result": result}
        isolated = isolated and result != "routable"
    rec["isolated"] = isolated
    return rec


def facts(flag=False, probe=True):
    """What this process sees, for result.json "sandbox" and doctor."""
    return {"require_readonly_state": require_readonly_state(flag),
            "state": state_status(),
            "tool_root": {"path": str(common.TOOL_ROOT), "writable": _writable(common.TOOL_ROOT)},
            "out": {"path": str(common.out_dir())},
            "home": common.home_facts(),
            "network": network_facts(probe=probe)}


# ------------------------------------------------------------- requirements

def _within(path, root):
    try:
        Path(path).relative_to(root)
        return True
    except ValueError:
        return False


def _cover(paths):
    """The smallest set of listed paths whose binds include all of them."""
    out = []
    for p in sorted(set(paths), key=lambda p: (len(Path(p).parts), p)):
        if not any(_within(p, q) for q in out):
            out.append(p)
    return sorted(out)


def requirements(*, venv_python, pcbnew_python, pcbnew_module=None, java=None, offline=False, flag=False):
    """The mounts, environment and network a sandbox must provide (doctor --json)."""
    ro = []

    def add(path, purpose, host_path=True):
        if path:
            path = Path(path)
            ro.append({"path": str(path), "purpose": purpose, "exists": path.exists(),
                       "same_path_required": host_path})

    add("/usr", "system: KiCad (kicad-cli, pcbnew, /usr/share/kicad), Python, Java, git")
    add("/etc", "system configuration (ld.so cache, fonts, passwd)")
    add(common.TOOL_ROOT, "kicad-route checkout: code, KiCad scripts, the canary fixture")
    git_dir = common.git(common.TOOL_ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if git_dir and not _within(git_dir, common.TOOL_ROOT):
        add(git_dir, "the checkout's git metadata (a worktree's common dir): kicad-route's version")
    add(common.STATE, "STATE: vendor/krt, .venv-krt, vendor/freerouting; the venv stores absolute paths, "
                      "so bind it at the same path")
    venv = Path(venv_python)
    if venv.exists():
        add(venv.resolve(), "interpreter the KRT venv links to (.venv-krt/bin/python)")
    add(Path(pcbnew_python).resolve() if Path(pcbnew_python).exists() else pcbnew_python,
        "Python with KiCad's pcbnew bindings (KICAD_ROUTE_PCBNEW_PYTHON)")
    if pcbnew_module:
        add(Path(pcbnew_module).resolve().parent, "the pcbnew module")
    add(common.KICAD_SHARE, "KiCad stock libraries and templates (hermetic config, DRC)")
    for exe in ("kicad-cli", "git", "bwrap"):
        found = shutil.which(exe)
        add(Path(found).resolve() if found else None, f"{exe} executable")
    if java:
        add(Path(java).resolve().parents[1], "JVM for Freerouting (newest under /usr/lib/jvm meeting the jar)")
    out = common.out_dir()
    exe_dirs = [str(Path(shutil.which(e)).parent) for e in ("kicad-cli", "git", "bwrap") if shutil.which(e)]
    path_value = ":".join(dict.fromkeys([*exe_dirs, "/usr/local/bin", "/usr/bin", "/bin"]))
    env = [
        {"name": "KICAD_ROUTE_STATE", "value": str(common.STATE), "required": True,
         "purpose": "the installed backends (default would be the checkout)"},
        {"name": "HOME", "value": str(out / "home"), "required": False,
         "purpose": "optional, need not be writable: KiCad's config, cache and data, fontconfig's cache "
                    "and the Freerouting JVM's user.home are kept under OUT (OUT/.kicad-config, OUT/.home)"},
        {"name": "PATH", "value": path_value, "required": True, "purpose": "kicad-cli, git and bwrap (planes)"},
        {"name": "KICAD_ROUTE_PCBNEW_PYTHON", "value": str(pcbnew_python), "required": False,
         "purpose": "only when it is not /usr/bin/python3"},
        {"name": "KICAD_ROUTE_OUT", "value": str(out), "required": False, "purpose": "or pass --out"},
        {"name": ENV_FLAG, "value": "1", "required": False,
         "purpose": "refuse (exit 4) unless STATE is read-only; update and corpus add refuse"},
    ]
    return {
        "ro_paths": ro,
        "ro_binds": _cover([r["path"] for r in ro if r["exists"]]),
        "rw_paths": [{"path": str(out), "purpose": "--out: runs, baseline cache, hermetic KiCad config, "
                                                   "the tools' per-user files (OUT/.home), "
                                                   "doctor output, the ledger (unless --ledger)"}],
        "tmp": "/tmp may be a private tmpfs",
        "env": env,
        "network": {"run": [], "campaign": [], "validate": [], "blocked": [], "planes": [], "doctor --offline": [],
                    "doctor": ["api.github.com:443"], "update": UPDATE_HOSTS,
                    "note": "routing needs no network; Freerouting additionally gets a dead HTTPS proxy"},
        "writes_state": ["update", "corpus add"],
        "observed": facts(flag=flag, probe=not offline),
    }
