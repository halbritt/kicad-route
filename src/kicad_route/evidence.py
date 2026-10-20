"""The evidence and tool_identity blocks of result.json, and the copper geometry signature.

evidence (what was graded, by hash):

    input_board_sha256       the input board as snapshotted into reference/
    reference_files          every reference file's sha256 (snapshot.json `files`)
    rules_files              the .kicad_pro / .kicad_dru among them
    rule_floor_sha256        the declared rule floor (settings.rule_floor_sha256)
    backend_output_board_sha256   the board the backend handed back (work/krt-out or
                             work/freerouting-out), before KiCad refilled and saved it
    candidate_board_sha256   the delivered candidate (validation.delivered_board_sha256)
    candidate_copper_signature    copper_signature() of the candidate's facts
    records                  sha256 of attempt.json and the grading records

tool_identity (what graded it): kicad-route, kicad-cli, pcbnew, Python, the backend
build and the state manifest's sha256 with any drift from it.

Two runs of one input under one backend build report the same reference hashes and
tool_identity; their candidate hashes differ only when their candidates do. Bytes
can differ when geometry does not (KiCad writes fresh UUIDs and its own item order),
so routing determinism is judged by the copper signature, not by file hashes.
"""
import json
import os
import sys
from pathlib import Path

from . import common
from . import statemanifest

RECORD_FILES = ("attempt.json", "preservation.json", "new-findings.json", "candidate-drc.json",
                "candidate-facts.json", "pour-integrity.json", "pair-facts.json", "pairs.json")


def _canonical_item(kind, item):
    rec = {k: v for k, v in item.items() if k != "uuid"}
    if rec.get("kind") == "arc" and tuple(rec["end"]) < tuple(rec["start"]):
        rec["start"], rec["end"] = rec["end"], rec["start"]  # an arc reversed is the same arc
    elif rec.get("kind") == "segment":
        rec["start"], rec["end"] = sorted([rec["start"], rec["end"]])
    return json.dumps({"item": kind, **rec}, sort_keys=True)


def copper_signature(facts):
    """Order- and UUID-free digest of a board's tracks, arcs and vias (from facts.py):
    equal exactly when the copper geometry (layer, net, width, endpoints, via size,
    drill, layers, lock state) is equal."""
    if not facts:
        return None
    lines = sorted([_canonical_item("track", t) for t in facts.get("tracks", [])] +
                   [_canonical_item("via", v) for v in facts.get("vias", [])])
    import hashlib
    return {"sha256": hashlib.sha256("\n".join(lines).encode()).hexdigest(),
            "tracks": len(facts.get("tracks", [])), "vias": len(facts.get("vias", []))}


def _sha(path):
    try:
        return common.sha256(path)
    except (FileNotFoundError, IsADirectoryError):
        return None


def reference_files(run_dir, attempt):
    """The reference files' sha256 map: snapshot.json when the run has one, else hashed now."""
    snap = common.load_json(Path(run_dir) / "snapshot.json")
    if snap and snap.get("files"):
        return dict(sorted(snap["files"].items()))
    ref = Path(run_dir) / attempt["reference_dir"]
    return {str(p.relative_to(ref)): common.sha256(p) for p in sorted(ref.rglob("*"))
            if p.is_file() and not p.is_symlink()}


def block(run_dir, attempt, grade_dir, validation, backend_output_sha256=None, records_base=None):
    """The evidence block for a candidate graded into grade_dir against run_dir's input.
    Record files are keyed by their path relative to records_base (the directory
    holding the result.json; default run_dir)."""
    run_dir, grade_dir = Path(run_dir), Path(grade_dir)
    base = Path(records_base or run_dir)
    files = reference_files(run_dir, attempt)
    facts = common.load_json(grade_dir / "candidate-facts.json")
    records = {}
    if (run_dir / "attempt.json").exists():
        records["attempt.json"] = _sha(run_dir / "attempt.json")
    for name in RECORD_FILES[1:]:
        if (grade_dir / name).exists():
            records[os.path.relpath(grade_dir / name, base)] = _sha(grade_dir / name)
    return {
        "input_board_sha256": files.get(attempt["board"]),
        "input_fingerprint": attempt.get("input_fingerprint"),
        "reference_files": files,
        "rules_files": {k: v for k, v in files.items() if k.endswith((".kicad_pro", ".kicad_dru"))},
        "rule_floor_sha256": (attempt.get("settings") or {}).get("rule_floor_sha256"),
        "backend_output_board_sha256": backend_output_sha256,
        "candidate_board_sha256": (validation or {}).get("delivered_board_sha256"),
        "candidate_copper_signature": copper_signature(facts),
        "records": records,
    }


def backend_identity(name, version, state=None):
    """The backend part of tool_identity, from its version record and the state on disk."""
    state = Path(state or common.STATE)
    if name == "krt":
        module = statemanifest.extension_module(statemanifest.layout(state)["venv"])
        return {"name": "krt", "tag": version.get("build"), "version": version.get("version"),
                "commit": version.get("commit"), "grid_router_version": version.get("rust_router"),
                "source_sha256": version.get("source_sha256"),
                "extension_module_sha256": _sha(module) if module else None,
                "venv_packages_sha256": statemanifest.packages_sha256(
                    statemanifest.venv_packages(statemanifest.layout(state)["venv"]))}
    java = (version.get("java") or "").split(" (")[0] or None
    installed = statemanifest.collect_freerouting(state) or {}
    return {"name": name, "tag": version.get("version"), "jar_sha256": version.get("sha256"),
            "build_revision": installed.get("build_revision"),
            "java_path": java, "java_version": statemanifest.java_full_version(java)}


def tool_identity(name, version, pcbnew_build=None, state=None):
    """tool_identity for a run with backend `name` (version: the backend's version record).
    Holds no timestamps: equal for runs on the same tools."""
    m = statemanifest.read(state)
    return {
        "kicad_route": statemanifest.kicad_route_identity(),
        "kicad_cli": statemanifest.kicad_cli_identity(),
        "pcbnew": {"build_version": pcbnew_build, "python": str(common.PCBNEW_PYTHON)},
        "python": {"version": sys.version.split()[0], "executable": sys.executable},
        "backend": backend_identity(name, version, state) if name else None,
        "state_manifest_sha256": statemanifest.file_sha256(state),
        "state_manifest_drift": [d["item"] for d in statemanifest.verify(m, state or common.STATE)]
        if m else None,
    }
