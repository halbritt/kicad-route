"""State manifest: STATE/manifest.json, what is installed under STATE right now.

`kicad-route update` writes it after installing, and `kicad-route state-manifest`
writes it for an existing install without downloading anything. It records the
identity of everything a routing run depends on (schema kicad-route/state/1):

    kicad_route   this checkout's describe, commit and dirty flag when written
    krt           owned source digest, repository commit, upstream provenance,
                  VERSION, grid_router version, the extension module's path
                  and sha256, source-build origin, requirements.txt's
                  sha256 and the venv's package list with its sha256
    freerouting   tag, asset, URL, sha256 (release.json and the jar on disk),
                  build revision, published_at, the Java release it needs
    java          the JVM find_java picks, its full version
    kicad         kicad-cli path, version and sha256; pcbnew's build version
    determinism   per backend, the latest repeat check (determinism.py) and the
                  build it was made for

Reading it (read(), verify(), doctor_checks()) needs no network or subprocesses and never writes under
STATE. verify() recomputes the recorded hashes on disk: a replaced jar or
extension module, a changed venv or a different kicad-cli is drift, which
`doctor` reports as a required FAIL.

`update` builds the owned routing source and acquires Freerouting's current
release. The manifest lets a caller (agent-ee) refuse a run whose
tool identity differs from the one it captured.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from . import common
from . import engine

SCHEMA = "kicad-route/state/1"
FILENAME = "manifest.json"
KRT_RELEASE_URL = "https://github.com/drandyhaas/KiCadRoutingTools/releases/download/{tag}/{asset}"


def manifest_path(state=None):
    return Path(state or common.STATE) / FILENAME


def layout(state, generation=None):
    state = Path(state)
    generation = Path(generation) if generation else common.router_generation(state)
    return {"generation": generation, "krt": generation / "router" if generation else engine.ROOT,
            "venv": generation / "venv" if generation else state / ".venv-krt",
            "freerouting": state / "vendor" / "freerouting"}


# ------------------------------------------------------------------ pieces

def _rel(path, state):
    try:
        return str(Path(path).relative_to(state))
    except ValueError:
        return str(path)


def _sha(path):
    try:
        return common.sha256(path)
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None


def extension_module(venv):
    """The owned extension installed into the routing environment."""
    found = sorted(Path(venv).glob("lib/python*/site-packages/grid_router*.so")) + \
        sorted(Path(venv).glob("lib/python*/site-packages/grid_router*.pyd"))
    return found[0] if found else None


def venv_packages(venv):
    """`name==version` for every distribution in the venv, read from its dist-info
    metadata (the same list `uv pip freeze` prints, without needing uv)."""
    out = {}
    for d in sorted(Path(venv).glob("lib/python*/site-packages/*.dist-info")):
        name = version = None
        try:
            for line in (d / "METADATA").read_text(errors="replace").splitlines():
                if not line.strip():
                    break
                if line.startswith("Name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("Version:"):
                    version = line.split(":", 1)[1].strip()
        except FileNotFoundError:
            continue
        if name:
            out[re.sub(r"[-_.]+", "-", name).lower()] = version
    return [f"{n}=={v}" for n, v in sorted(out.items())]


def packages_sha256(lines):
    import hashlib
    return hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest() if lines else None


def venv_python_version(venv):
    try:
        for line in (Path(venv) / "pyvenv.cfg").read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() in ("version_info", "version"):
                return v.strip()
    except FileNotFoundError:
        pass
    return None


def prebuilt_from_log(text):
    """The prebuilt grid_router asset build_router.py reports downloading, or None
    (built from source, or no log)."""
    if not text:
        return None
    asset = re.search(r"^Downloading (\S+) \(", text, re.M)
    release = re.search(r"Installed prebuilt binary from release (\S+?)\.?$", text, re.M)
    if not asset or not release:
        return {"asset": None, "url": None, "release": None, "from_source": "cargo" in text.lower()}
    return {"asset": asset.group(1), "release": release.group(1), "from_source": False,
            "url": KRT_RELEASE_URL.format(tag=release.group(1), asset=asset.group(1))}


def jar_java_major(jar):
    try:
        with zipfile.ZipFile(jar) as z:
            mf = z.read("META-INF/MANIFEST.MF").decode(errors="replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    m = re.search(r"Created-By:\s*(\d+)", mf) or re.search(r"Build-Jdk-Spec:\s*(\d+)", mf)
    return int(m.group(1)) if m else None


def java_full_version(java):
    """The `java -version` banner's version and runtime build, e.g. '25.0.4.1 (25.0.4.1+1-...)'."""
    if not java:
        return None
    try:
        r = subprocess.run([str(java), "-version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    v = re.search(r'version "([^"]+)"', r.stderr)
    b = re.search(r"Runtime Environment[^\n]*\(build ([^)]+)\)", r.stderr)
    return (v.group(1) + (f" (build {b.group(1)})" if b else "")) if v else None


def kicad_cli_identity():
    path = shutil.which("kicad-cli")
    real = Path(path).resolve() if path else None
    return {"path": str(real) if real else None, "version": common.kicad_version(),
            "sha256": _sha(real) if real else None}


def pcbnew_build_version():
    try:
        r = subprocess.run([str(common.PCBNEW_PYTHON), "-c", "import pcbnew; print(pcbnew.GetBuildVersion())"],
                           capture_output=True, text=True, env=common.tool_env(), timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    return lines[-1].strip() if r.returncode == 0 and lines else None


def kicad_route_identity():
    root = common.TOOL_ROOT
    status = common.git(root, "status", "--porcelain", "--untracked-files=no")
    return {"version": common.git_describe(root),  # describe --dirty would take the index lock
            "commit": common.git(root, "rev-parse", "HEAD"),
            "dirty": bool(status) if status is not None else None}


# -------------------------------------------------------------- collection

def collect_krt(state, build_router_log=None, generation=None):
    d = layout(state, generation)
    krt, venv = d["krt"], d["venv"]
    if not krt.exists():
        return None
    version = (krt / "VERSION").read_text().strip() if (krt / "VERSION").exists() else None
    module = extension_module(venv)
    grid = None
    py = venv / "bin" / "python"
    if py.exists():
        try:
            r = subprocess.run([str(py), "-c", "import sys; sys.path.insert(0, 'rust_router'); "
                                "import grid_router; print(grid_router.__version__)"],
                               cwd=krt, capture_output=True, text=True, timeout=120)
            grid = r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            grid = None
    pkgs = venv_packages(venv)
    identity = engine.identity(krt)
    return {
        "generation": _rel(d["generation"], state) if d["generation"] else None,
        "tag": None,
        "describe": identity["build"],
        "commit": identity["commit"],
        "source_sha256": identity["source_sha256"],
        "source_path": _rel(krt, state),
        "source": "https://github.com/halbritt/kicad-route",
        "clone_url": None,
        "upstream": identity["upstream"],
        "version": version,
        "grid_router_version": grid,
        "extension_module": {"path": _rel(module, state) if module else None,
                             "sha256": _sha(module) if module else None},
        "prebuilt": {"asset": None, "url": None, "release": None, "from_source": True},
        "requirements": {"path": _rel(krt / "requirements.txt", state),
                         "sha256": _sha(krt / "requirements.txt")},
        "venv": {"path": _rel(venv, state), "python": str((venv / "bin" / "python").resolve()) if py.exists() else None,
                 "python_version": venv_python_version(venv), "packages": pkgs,
                 "packages_sha256": packages_sha256(pkgs)},
    }


def collect_freerouting(state):
    cur = layout(state)["freerouting"] / "current"
    rel = common.load_json(cur / "release.json") if cur.exists() else None
    if not rel:
        return None
    jar = cur / rel["asset"]
    on_disk = _sha(jar)
    return {"tag": rel.get("tag"), "asset": rel.get("asset"), "url": rel.get("url"),
            "jar": {"path": _rel(jar, state), "sha256": on_disk},
            "release_sha256": rel.get("sha256"), "sha256_matches_release": on_disk == rel.get("sha256"),
            # update verifies the download against GitHub's asset digest when GitHub publishes one
            # (and refuses a mismatch); release.json does not record whether one was published.
            "github_digest_match": rel.get("github_digest_match"),
            "build_revision": rel.get("build_revision"), "published_at": rel.get("published_at"),
            "java_required": jar_java_major(jar)}


def collect_java(freerouting):
    need = (freerouting or {}).get("java_required")
    if freerouting is None:
        return None
    from . import cli  # find_java lives with the Freerouting adapter
    java = cli.find_java(need)
    return {"path": str(java[0]) if java else None, "major": java[1] if java else None,
            "version": java_full_version(java[0]) if java else None, "required": need}


def collect(state=None, build_router_log=None, written_by="state-manifest", previous=None, generation=None):
    """The manifest for what is installed under state now. Downloads nothing.
    A determinism record from `previous` is kept when its build is still the one installed."""
    state = Path(state or common.STATE).resolve()
    fr = collect_freerouting(state)
    m = {
        "schema": SCHEMA,
        "state_dir": str(state),
        "written_by": written_by,
        "updated_utc": common.now_utc().isoformat(),
        "pinned": False,  # update follows each backend's latest release (docs/routing.md)
        "kicad_route": kicad_route_identity(),
        "python": {"version": sys.version.split()[0], "executable": sys.executable},
        "krt": collect_krt(state, build_router_log, generation),
        "freerouting": fr,
        "java": collect_java(fr),
        "kicad": {"kicad_cli": kicad_cli_identity(), "pcbnew_build_version": pcbnew_build_version(),
                  "pcbnew_python": str(common.PCBNEW_PYTHON)},
        "determinism": {},
    }
    for backend, rec in ((previous or {}).get("determinism") or {}).items():
        if rec and rec.get("build_id") == build_id(m, backend):
            m["determinism"][backend] = rec
    return m


def write(manifest, state=None):
    """Atomic write of STATE/manifest.json (temporary file + rename)."""
    path = manifest_path(state)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(tmp, path)
    return path


def read(state=None):
    return common.load_json(manifest_path(state))


def file_sha256(state=None):
    p = manifest_path(state)
    return _sha(p) if p.exists() else None


# ---------------------------------------------------------------- checking

REQUIRED_KEYS = ("schema", "state_dir", "updated_utc", "pinned", "kicad_route", "krt", "freerouting",
                 "java", "kicad", "determinism")


def schema_problems(m):
    """Structural check of a manifest against kicad-route/state/1."""
    if not isinstance(m, dict):
        return ["not a JSON object"]
    problems = [f"missing {k}" for k in REQUIRED_KEYS if k not in m]
    if m.get("schema") != SCHEMA:
        problems.append(f"schema {m.get('schema')!r} is not {SCHEMA!r}")
    k = m.get("krt")
    if k is not None:
        for key in ("commit", "version", "extension_module", "requirements", "venv"):
            if key not in k:
                problems.append(f"krt.{key} missing")
    f = m.get("freerouting")
    if f is not None:
        for key in ("tag", "jar", "build_revision"):
            if key not in f:
                problems.append(f"freerouting.{key} missing")
    if not isinstance(m.get("determinism", {}), dict):
        problems.append("determinism is not an object")
    return problems


def current_identity(m, state):
    """What is on disk now for every hash the manifest records (no network, no writes)."""
    state = Path(state)
    cur = {}
    k = m.get("krt")
    if k is not None:
        source = k.get("source_path")
        if source is None and k.get("generation"):
            source = str(Path(k["generation"]) / "router")
        cur["krt.source_sha256"] = engine.source_sha256(state / source if source else engine.ROOT)
        mod = (k.get("extension_module") or {}).get("path")
        cur["krt.extension_module.sha256"] = _sha(state / mod) if mod else None
        req = (k.get("requirements") or {}).get("path")
        cur["krt.requirements.sha256"] = _sha(state / req) if req else None
        venv = (k.get("venv") or {}).get("path")
        cur["krt.venv.packages_sha256"] = packages_sha256(venv_packages(state / venv)) if venv else None
    f = m.get("freerouting")
    if f is not None:
        jar = (f.get("jar") or {}).get("path")
        cur["freerouting.jar.sha256"] = _sha(state / jar) if jar else None
    cli_path = ((m.get("kicad") or {}).get("kicad_cli") or {}).get("path")
    if cli_path:
        cur["kicad.kicad_cli.sha256"] = _sha(cli_path)
    return cur


def recorded(m, dotted):
    v = m
    for part in dotted.split("."):
        v = (v or {}).get(part) if isinstance(v, dict) else None
    return v


def verify(m, state=None):
    """Drift between the manifest and the files on disk: [{item, recorded, current}]."""
    state = Path(state or m.get("state_dir") or common.STATE)
    return [{"item": item, "recorded": recorded(m, item), "current": now}
            for item, now in current_identity(m, state).items() if recorded(m, item) != now]


def build_id(m, backend="krt"):
    """Identity of the build a determinism record holds for: the backend's code and
    environment plus the KiCad that refills and saves every candidate."""
    if backend == "krt":
        k = m.get("krt") or {}
        parts = {"source": k.get("source_sha256") or k.get("commit"), "extension_module": (k.get("extension_module") or {}).get("sha256"),
                 "venv": (k.get("venv") or {}).get("packages_sha256")}
    else:
        f = m.get(backend) or {}
        parts = {"jar": (f.get("jar") or {}).get("sha256"), "java": (m.get("java") or {}).get("version")}
    parts["kicad_cli"] = ((m.get("kicad") or {}).get("kicad_cli") or {}).get("sha256")
    return common.sha256_of({"backend": backend, **parts})


def execution_build_id(tools, backend="krt"):
    """The build actually executed, using the same content fields as build_id.

    Missing execution evidence cannot be replaced with the currently selected
    installation: a worker may still be running a prior generation.
    """
    tools = tools or {}
    executed = tools.get("backend") or {}
    native = (tools.get("kicad_cli") or {}).get("sha256")
    if backend == "krt":
        source = executed.get("source_sha256")
        module = executed.get("extension_module_sha256")
        packages = executed.get("venv_packages_sha256")
        if not all((source, module, packages, native)):
            return None
        manifest = {"krt": {"source_sha256": source, "extension_module": {"sha256": module},
                            "venv": {"packages_sha256": packages}}}
    else:
        jar, java = executed.get("jar_sha256"), executed.get("java_version")
        if not all((jar, java, native)):
            return None
        manifest = {backend: {"jar": {"sha256": jar}}, "java": {"version": java}}
    manifest["kicad"] = {"kicad_cli": {"sha256": native}}
    return build_id(manifest, backend)


def current_build_id(state=None, backend="krt"):
    """build_id of what is on disk now, from the manifest's own paths."""
    state = Path(state or common.STATE)
    m = read(state)
    if m is None:
        return None
    cur = current_identity(m, state)
    if backend == "krt":
        live = {"krt": {"commit": cur.get("krt.commit"),
                        "source_sha256": cur.get("krt.source_sha256"),
                        "extension_module": {"sha256": cur.get("krt.extension_module.sha256")},
                        "venv": {"packages_sha256": cur.get("krt.venv.packages_sha256")}}}
    else:
        live = {backend: {"jar": {"sha256": cur.get("freerouting.jar.sha256")}}, "java": m.get("java")}
    live["kicad"] = {"kicad_cli": {"sha256": cur.get("kicad.kicad_cli.sha256",
                                                    recorded(m, "kicad.kicad_cli.sha256"))}}
    return build_id(live, backend)


_UNSPECIFIED_GENERATION = object()


def reproducibility(backend, state=None, runtime_generation=_UNSPECIFIED_GENERATION):
    """backend.reproducible and the result's measurement flag for a run on the installed build.
    Freerouting's outcome may vary run to run: never reproducible, never a measurement.
    KRT: what the state manifest's determinism record says for the build on disk now."""
    if backend == "freerouting":
        return {"reproducible": False, "measurement": False,
                "basis": "Freerouting's outcome may vary between runs: this result is an attempt, not a measurement"}
    m = read(state)
    if runtime_generation is not _UNSPECIFIED_GENERATION:
        selected = common.router_generation(state)
        runtime = Path(runtime_generation) if runtime_generation else None
        if runtime != selected:
            return {"reproducible": None, "measurement": False,
                    "basis": "routing process uses a previous generation; the active manifest selects another build"}
    rec = ((m or {}).get("determinism") or {}).get(backend)
    if not rec:
        return {"reproducible": None, "measurement": False,
                "basis": "no determinism record for this build in the state manifest "
                         "(kicad-route determinism --record)"}
    if rec.get("build_id") != current_build_id(state, backend):
        return {"reproducible": None, "measurement": False,
                "basis": f"the determinism record ({rec.get('verified_utc')}) is for another {backend} build"}
    ok = rec.get("reproducible") is True
    return {"reproducible": ok, "measurement": ok,
            "basis": f"determinism check {rec.get('verified_utc')}: "
                     + ("identical copper geometry on repeat" if ok else
                        f"repeat differed ({rec.get('differs_at') or 'see the record'})")}


def attempt_qualification(repeat, process):
    """Keep installed-build repeat evidence separate from this attempt's stop."""
    from .determinism import wall_clock_stops
    stops = wall_clock_stops(process)
    reasons = [f"wall-clock stop: {flag}" for flag in stops]
    if not repeat.get("measurement"):
        reasons.append(repeat["basis"])
    return {"measurement": bool(repeat.get("measurement") and not stops),
            "build_reproducible": repeat.get("reproducible"), "reasons": reasons}


def record_determinism(rec, state=None):
    """Record proof only for its executed build, under the activation lock."""
    from .installation import update_lock
    state = Path(state or common.STATE)
    with update_lock(state):
        m = read(state)
        if m is None:
            raise RuntimeError(f"no state manifest at {manifest_path(state)}: run `kicad-route state-manifest` first")
        if rec.get("build_id") is None or rec["build_id"] != current_build_id(state, rec["backend"]):
            raise RuntimeError("executed repeat build differs from the active installation; rerun determinism")
        if verify(m, state):
            raise RuntimeError("state manifest drift: re-record it with `kicad-route state-manifest` first")
        m.setdefault("determinism", {})[rec["backend"]] = rec
        write(m, state)
        return m


def doctor_checks(state=None):
    """Checks `doctor` reports for the manifest: (name, status, detail, required)."""
    state = Path(state or common.STATE)
    path = manifest_path(state)
    m = read(state)
    if m is None:
        return [("state manifest", common.NOT_RUN,
                 f"{path} missing or unreadable: run `kicad-route state-manifest` (update writes it)", False)]
    out = []
    problems = schema_problems(m)
    out.append(("state manifest", common.PASS if not problems else common.FAIL,
                f"{path} ({m.get('updated_utc')}, by {m.get('written_by')})" if not problems
                else "; ".join(problems), True))
    drift = verify(m, state)
    out.append(("state manifest matches the files on disk", common.PASS if not drift else common.FAIL,
                "jar, extension module, venv packages, requirements, owned source, kicad-cli" if not drift else
                "drift: " + "; ".join(f"{d['item']} recorded {str(d['recorded'])[:16]} now {str(d['current'])[:16]}"
                                      for d in drift) + " (reinstall, or re-record with `kicad-route state-manifest`)",
                True))
    r = reproducibility("krt", state)
    out.append(("KRT determinism verified for this build",
                common.PASS if r["reproducible"] else (common.FAIL if r["reproducible"] is False else common.NOT_RUN),
                r["basis"], False))
    return out


# ---------------------------------------------------------------- command

def default_build_router_log():
    return common.out_dir() / "update" / "krt-build-router.log"


def refresh(written_by="state-manifest", build_router_log=None, state=None):
    """Collect and write under the same lock that activates router generations."""
    from .installation import update_lock
    state = Path(state or common.STATE)
    with update_lock(state):
        log = build_router_log or default_build_router_log()
        m = collect(state, log, written_by=written_by, previous=read(state))
        if m["krt"] and (m["krt"].get("prebuilt") is None):
            m["krt"]["prebuilt_note"] = f"no build_router log at {log}: prebuilt asset URL not recorded"
        return write(m, state), m


def cmd(args):
    path, m = refresh(build_router_log=getattr(args, "build_router_log", None))
    problems = schema_problems(m)
    drift = verify(m)
    k, f = m.get("krt") or {}, m.get("freerouting") or {}
    print(f"[state-manifest] krt {k.get('version')} ({(k.get('commit') or '')[:7]}), grid_router "
          f"{k.get('grid_router_version')}, module {((k.get('extension_module') or {}).get('sha256') or '')[:12]}",
          flush=True)
    print(f"[state-manifest] freerouting {f.get('tag')} jar {((f.get('jar') or {}).get('sha256') or '')[:12]}; "
          f"java {(m.get('java') or {}).get('version')}; kicad-cli {m['kicad']['kicad_cli']['version']}", flush=True)
    for backend, rec in m["determinism"].items():
        print(f"[state-manifest] determinism record kept for {backend} ({rec.get('verified_utc')})", flush=True)
    print(f"[state-manifest] {path}", flush=True)
    if problems or drift:
        print("[state-manifest] problems: " + "; ".join(problems + [d["item"] for d in drift]), flush=True)
        return 1
    return 0


def add_parser(sub):
    p = sub.add_parser("state-manifest", help="record what is installed under STATE in STATE/manifest.json "
                                              "(no downloads)")
    p.add_argument("--build-router-log", metavar="LOG",
                   help="KRT build_router.py output naming the prebuilt asset (default OUT/update/krt-build-router.log)")
    return p
