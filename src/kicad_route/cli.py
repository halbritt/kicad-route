#!/usr/bin/env python3
"""kicad-route: run a PCB routing backend (KRT or Freerouting) on an isolated copy
of a KiCad project and grade the resulting native KiCad candidate against the
input's unchanged constraints.

    kicad-route [--out DIR] [--json] doctor [--offline] [--no-canary] [--require-readonly-state]
                                          readiness and capabilities of each backend, and
                                          what a sandbox must provide (docs/sandbox.md)
    kicad-route run --backend krt|freerouting [--dir D] [--board B.kicad_pcb] [--rev REV]
                 [--nets PAT ...] [--netclass NAME ...] [--budget SECONDS]
                 [--protect existing|locked|human] [--label L | --run-dir PATH] [--backend-arg ARG ...]
    kicad-route validate RUN_DIR [--against ORIGIN_RUN]
                                          re-grade a candidate (or a --dir chain) against its input
    kicad-route campaign --backend krt|freerouting [run options] [--rounds N] [--race K] [--total-budget S]
                                          hard-nets-first campaign of runs; best chain graded vs the input
    kicad-route compare RUN_DIR ...       compare candidates routed from the same input
    kicad-route blocked RUN_DIR [--top N] which obstacles lie across the connections left open
    kicad-route blocked --board B.kicad_pcb [--json]
                                          the same receipts for a placed board, without routing it
    kicad-route audit --old REV --new REV [--dir D]
                                          grade a committed board change against its parent
    kicad-route floor [--dir D] [--since REV] [--write-declared]
                                          rule high-water mark of the history and the declared floor
    kicad-route spokes RUN_DIR | --board B.kicad_pcb
                                          thermal-spoke corridors: which new copper starved which pad
    kicad-route planes --board B.kicad_pcb --plan PLAN.json --run-dir PATH
                                          explicit native planes with refill and pour evidence
    kicad-route decoupling RUN_DIR | --board B.kicad_pcb [--radius MM]
                                          read-only: bypass capacitors between IC pins and the plane?
    kicad-route barrier RUN_DIR [--size MM]
                                          preview the collision keepouts `run --barrier-from RUN_DIR` injects
    kicad-route publish RUN_DIR [--to DIR]
                                          copy a run's structured evidence (not the board) for committing
    kicad-route update                    build the owned engine, acquire Freerouting, then smoke
    kicad-route state-manifest            record what is installed under STATE in STATE/manifest.json
    kicad-route determinism [--backend krt] [--runs N] [--record]
                                          route one input repeatedly and compare the candidates
    kicad-route smoke [--publish DIR]     integration tests on the bundled fixture board
    kicad-route canary [--backend B|all]  awkward net names through each backend's round trip
    kicad-route corpus add|check|list     real-board regression gate (`update` runs check)

--out DIR and --json go before or after the subcommand. --json (run, validate,
campaign, blocked, audit, floor, doctor, decoupling, barrier, planes) prints exactly one
JSON record on stdout and sends progress to stderr (docs/interface.md, schemas/).

Every attempt gets a fresh run directory OUT/runs/<project>/<label>/<stamp>/
(OUT defaults to ./build/kicad-route), or exactly --run-dir PATH:

    reference/<proj>/   untouched copy of the input project (board, .kicad_pro,
                        .kicad_dru, schematics, library tables, local libraries)
    candidate/<proj>/   the same project with the routed board; open it in KiCad
    work/               backend scratch: its own output files, DSN/SES, logs
    validate/           native DRC of the candidate, board facts, preservation diff
    result.json         everything below, machine-readable; RESULT.md for people

Three results are reported independently:

    process     did the backend run and hand back a board?         PASS | FAIL
    routing     are the in-scope connections complete?             COMPLETE | INCOMPLETE | NOT_RUN
    validation  does the candidate meet the unchanged constraints? PASS | FAIL | NOT_RUN

Validation always uses the input project's own .kicad_pro/.kicad_dru: whatever
the backend wrote to its project files is recorded and discarded. Zones are
refilled and saved into the candidate before native DRC (with schematic parity)
grades exactly the delivered file. Violations already present in the input
(graded the same way) are reported as pre-existing; only new ones fail.

Exit status: 0 complete and valid, 1 backend/process failure (no candidate),
2 candidate valid but routing incomplete, 3 candidate fails validation,
4 refused before routing: a usage or input error, the ledger shows this exact
attempt already failed, the --canary pre-flight failed, the board has no
.kicad_pro of its own name (so KiCad would load none of the input's rules), or
--require-readonly-state found the state directory writable, missing or not the
one its manifest describes (docs/sandbox.md); the
refusal record is OUT/refusals/refusal-latest.json and no run directory is left.
Contract: docs/interface.md. Run hygiene (stall, ledger, canary, corpus):
docs/hygiene.md.
"""
import argparse
import collections
import datetime as dt
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import barrier  # run --barrier-from / `kicad-route barrier` (docs/barrier.md)
from . import baselinecache
from . import blocked
from . import common
from . import contract
from . import evidence
from . import engine
from . import diff_pairs
from . import floor
from . import provenance
from . import sandbox  # --require-readonly-state, doctor's sandbox block (docs/sandbox.md)
from . import pour
from . import planes
from . import skills
from . import spokes  # --reserve-spokes / `kicad-route spokes` (docs/spokes.md)
from . import decoupling  # `kicad-route decoupling` (docs/decoupling.md)
from . import ledger
from . import netnames
from . import procgroup
from . import stall
from . import statemanifest
from .snapshot import KICAD_SUFFIXES, export_revision, make_snapshot, sibling_names

PASS, FAIL, NOT_RUN, UNSUPPORTED = common.PASS, common.FAIL, common.NOT_RUN, common.UNSUPPORTED
COMPLETE, INCOMPLETE = "COMPLETE", "INCOMPLETE"

TOOL_ROOT = common.TOOL_ROOT
KRT_VENV = common.STATE / ".venv-krt"
KRT_DIR = engine.ROOT
FREEROUTING_DIR = common.STATE / "vendor" / "freerouting"
FIXTURE = TOOL_ROOT / "fixtures" / "route_demo"
KICAD_SCRIPTS = Path(__file__).resolve().parent / "kicad"  # run with KiCad's Python (pcbnew)
NM = 1_000_000  # nanometres per millimetre


def log(msg):
    print(msg, flush=True)


def pcbnew_python():
    return common.PCBNEW_PYTHON


def route_python():
    return KRT_VENV / "bin" / "python"


load_json = common.load_json


def out_path(*parts):
    return common.out_dir().joinpath(*parts)


# ------------------------------------------------------------------- the input

def resolve_input(args):
    """The project to route, in the shape snapshot.make_snapshot expects."""
    d = Path(args.dir).expanduser().resolve()
    if not d.is_dir():
        contract.refuse("input", f"{d}: not a directory")
    boards = [args.board] if args.board else sorted(p.name for p in d.glob("*.kicad_pcb"))
    if len(boards) != 1:
        contract.refuse("input", f"{d}: pass --board (found {boards or 'no boards'})")
    board = boards[0]
    sch = args.schematic or Path(board).with_suffix(".kicad_sch").name
    if any("=" not in e for e in args.library_dir or []):
        contract.refuse("usage", "--library-dir takes NAME=PATH")
    libs = dict(e.split("=", 1) for e in args.library_dir or [])
    cfg = {"name": args.name or d.name, "path": d, "pcb": board,
           "schematic": sch if (d / sch).exists() else None, "library_dirs": libs}
    if args.rev:
        if not common.git(d, "rev-parse", "--show-toplevel"):
            contract.refuse("input", f"--rev needs {d} to be inside a git repository")
        cfg["rev"] = args.rev
    return cfg


def snapshot_input(cfg, run_dir):
    """reference/<proj>: the project snapshot (sheets, rules, library tables, linked libraries)."""
    if cfg.get("schematic"):
        dst, manifest = make_snapshot(cfg, run_dir)
        ref_root = run_dir / "reference"
        (run_dir / "snapshot").rename(ref_root)
        return ref_root / dst.name, manifest
    # No schematic: copy the board directory's KiCad files only (parity becomes UNSUPPORTED).
    src, commit, _ = (export_revision(cfg, run_dir) if cfg.get("rev") else (cfg["path"], None, None))
    dst = run_dir / "reference" / cfg["path"].name
    dst.mkdir(parents=True)
    for f in src.iterdir():
        if f.is_file() and (f.suffix in KICAD_SUFFIXES or f.name.endswith("-lib-table")):
            shutil.copy2(f, dst / f.name)
    manifest = {"project": cfg["name"], "source_dir": str(cfg["path"]),
                "source": f"git revision {cfg['rev']} ({commit})" if commit else "working tree",
                "source_git_head": commit or common.git(cfg["path"], "rev-parse", "HEAD"),
                "files": {p.name: common.sha256(p) for p in sorted(dst.iterdir()) if p.is_file()}}
    common.write_json(run_dir / "snapshot.json", manifest)  # the reference hashes, as make_snapshot records them
    return dst, manifest


def input_fingerprint(manifest):
    """One hash over every input file, so candidates can be compared only within one input."""
    import hashlib
    h = hashlib.sha256()
    for name, digest in sorted(manifest.get("files", {}).items()):
        h.update(f"{name}\0{digest}\n".encode())
    return h.hexdigest()


def copy_project(src, dst):
    """Copy a project directory, plus links beside it to the sibling directories its
    library tables reach as ${KIPRJMOD}/../<dir> (the snapshot keeps those next to
    reference/<proj>), so the copy resolves the same libraries."""
    shutil.copytree(src, dst, symlinks=True)
    for stale in list(dst.glob("*.kicad_prl")) + list(dst.glob("fp-info-cache")):
        stale.unlink()
    tables = [(src / t).read_text(errors="replace") for t in ("sym-lib-table", "fp-lib-table") if (src / t).is_file()]
    for name in sibling_names(tables, src.name):
        here, there = src.parent / name, dst.parent / name
        if here.exists() and not (there.exists() or there.is_symlink()):
            there.symlink_to(here.resolve())


# --------------------------------------------------------------- nets and scope

def board_facts(board, out):
    """Engineering facts of a board via KiCad's own loader (route_facts.py)."""
    rec = common.run_cmd([pcbnew_python(), KICAD_SCRIPTS / "facts.py", board, out], board.parent,
                      out.with_suffix(".log"), timeout=1800)
    facts = load_json(out) if rec["exit_code"] == 0 else None
    return facts, common.public(rec)


class NetSelectionError(ValueError):
    """A --nets selection that would not mean what it says; refused before routing."""


def parse_net_pattern(pat):
    r"""(excludes, fnmatch pattern) of one --nets pattern. `!PAT` excludes PAT; a
    leading `\!` is a literal '!' (`\!RESET` selects the net `!RESET`, `!\!RESET`
    excludes it), as netnames.exact() writes such names for KRT."""
    excludes = pat.startswith("!")
    body = pat[1:] if excludes else pat
    return excludes, body[1:] if body.startswith("\\!") else body


def select_nets(facts, patterns, netclasses):
    r"""Net names in scope. Patterns match net names (fnmatch, `!pat` excludes, `\!`
    is a literal leading '!'); net classes match the effective class KiCad resolved
    for each net. Exclusions alone (`--nets '!GND'`) start from every net.

    Raises NetSelectionError for an exclusion that excludes nothing while a net is
    literally named like it (`--nets '!RESET'` on a board with the net `!RESET`):
    that reads as "route !RESET" but would route everything else."""
    names = sorted(facts["nets"])
    if not patterns and not netclasses:
        return names, {"mode": "all"}
    parsed = [parse_net_pattern(p) for p in patterns or []]
    for pat, (excludes, body) in zip(patterns or [], parsed):
        if excludes and not any(fnmatch.fnmatchcase(n, body) for n in names):
            literal = [n for n in names if fnmatch.fnmatchcase(n, pat)]
            if literal:
                raise NetSelectionError(
                    f"--nets {pat!r} is an exclusion (of nets matching {body!r}, of which there are none), "
                    f"but the board has a net named {literal[0]!r}: write '\\{pat}' to route that net, "
                    f"or '!\\{pat}' to exclude it")
    only_excludes = not netclasses and all(excludes for excludes, _ in parsed)
    chosen = set(names) if only_excludes else set()
    for excludes, body in parsed:
        if not excludes:
            chosen |= {n for n in names if fnmatch.fnmatchcase(n, body)}
    for cls in netclasses or []:
        chosen |= {n for n in names if facts["nets"][n]["netclass"] == cls}
    for excludes, body in parsed:
        if excludes:
            chosen -= {n for n in names if fnmatch.fnmatchcase(n, body)}
    return sorted(chosen), {"mode": "selected", "net_patterns": patterns or [], "netclasses": netclasses or []}


# ----------------------------------------------------------------- native DRC

def kicad_drc(board, out_dir, label, refill_and_save):
    """Refill+save (optional) then the grading DRC. Returns (report, info)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    info = {"commands": [], "reasons": []}
    if refill_and_save:
        before = common.sha256(board)
        rec = common.run_cmd(["kicad-cli", "pcb", "drc", "--refill-zones", "--save-board", "--format", "json",
                           "-o", out_dir / f"{label}-refill.json", board], board.parent,
                          out_dir / f"{label}-refill.log")
        info["commands"].append(common.public(rec))
        if rec["exit_code"] != 0:
            info["reasons"].append(f"zone refill failed (exit {rec['exit_code']})")
            return None, info
        info["refill"] = {"board_sha256_before": before, "board_sha256_after": common.sha256(board)}
    out = out_dir / f"{label}-drc.json"
    info["board_sha256_graded"] = common.sha256(board)  # the file the grading DRC reads
    rec = common.run_cmd(["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "--schematic-parity",
                       "--units", "mm", "-o", out, board], board.parent, out_dir / f"{label}-drc.log")
    info["commands"].append(common.public(rec))
    info["board_sha256_checked"] = common.sha256(board)
    if rec["exit_code"] != 0:
        info["reasons"].append(f"kicad-cli pcb drc exited {rec['exit_code']}")
        return None, info
    report = load_json(out)
    if report is None or not all(isinstance(report.get(k), list)
                                 for k in ("violations", "unconnected_items", "schematic_parity")):
        info["reasons"].append("DRC report missing or malformed")
        return None, info
    m = re.search(r"(?i)failed to fetch schematic netlist[^\n]*|parity tests require[^\n]*", rec["output"])
    info["parity_performed"] = m is None
    if m:
        info["parity_skip_message"] = m.group(0).strip()
    info["report"] = common.rel(out)
    return report, info


def item_key(v):
    """Identity of a DRC finding that survives unrelated edits elsewhere on the board."""
    items = tuple(sorted((i.get("description", ""), round(i.get("pos", {}).get("x", 0), 3),
                          round(i.get("pos", {}).get("y", 0), 3)) for i in v.get("items", [])))
    return (v.get("type"), v.get("severity"), items)


def drc_delta(base, cand, kind):
    b = collections.Counter(item_key(v) for v in base[kind])
    new, resolved = [], []
    remaining = collections.Counter(b)
    for v in cand[kind]:
        k = item_key(v)
        if remaining[k] > 0:
            remaining[k] -= 1
        else:
            new.append(v)
    c = collections.Counter(item_key(v) for v in cand[kind])
    for v in base[kind]:
        k = item_key(v)
        if c[k] > 0:
            c[k] -= 1
        else:
            resolved.append(v)
    return new, resolved


def item_nets(v, known=None):
    """Net names of a DRC finding's items (netnames.nets_in_description): `/BUS[0]` stays
    whole; with `known` (the board's nets) a name is matched exactly, brackets and all."""
    return {n for i in v.get("items", []) for n in netnames.nets_in_description(i.get("description", ""), known)}


def summarize_findings(items):
    return {"count": len(items), "by_type": common.count_by(items, "type"),
            "by_severity": common.count_by(items, "severity")}


def baseline_drc(reference, board_name, fingerprint):
    """Native DRC of the untouched input, graded exactly like candidates (refill first).
    Cached by input, selected board and native grading content identity."""
    return baselinecache.get(reference, board_name, fingerprint,
                             out_path(".cache", "baseline"), kicad_drc, copy_project)


# ---------------------------------------------------------------- preservation

GEOMETRY_TOLERANCE_NM = 1000  # 1 um: Specctra round trips quantise coordinates


def geometry_points(t):
    return [t[k] for k in ("start", "mid", "end", "position") if k in t]


def same_copper(a, b, tol):
    """Same conductor within tol: kind, layer(s), net, via type, size and geometry.
    A track's start and end may come back swapped (an arc keeps its mid point), so
    both orientations are accepted; facts.py also writes them in canonical order."""
    keys = ("kind", "layer", "net") if "layer" in a else ("net", "layers", "via_type")
    if any(a.get(k) != b.get(k) for k in keys):
        return False
    for k in ("width", "diameter", "drill"):
        if k in a and abs(a[k] - b.get(k, -10**12)) > tol:
            return False
    pa, pb = geometry_points(a), geometry_points(b)
    if len(pa) != len(pb):
        return False

    def close(pts):
        return all(abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol for p, q in zip(pa, pts))
    return close(pb) or ("start" in b and "end" in b and close(pb[::-1]))


def match_copper(ref_items, cand_items, tol=GEOMETRY_TOLERANCE_NM):
    """Pair reference copper with candidate copper by geometry (UUIDs do not survive
    every backend). Returns (matched pairs, unmatched reference, unmatched candidate).
    Items may carry `locked`: among equal geometric matches one with the same lock
    state is preferred, so identical copper does not pair a locked item with an unlocked one."""
    pool = collections.defaultdict(list)
    for i, c in enumerate(cand_items):
        pool[(c.get("net"), c.get("layer") or tuple(c.get("layers", [])))].append(i)
    used, pairs, missing = set(), [], []
    for r in ref_items:
        hit = None
        order = sorted(pool[(r.get("net"), r.get("layer") or tuple(r.get("layers", [])))],
                       key=lambda i: cand_items[i].get("locked") != r.get("locked"))
        for t in (0, tol):
            hit = next((i for i in order if i not in used and same_copper(r, cand_items[i], t)), None)
            if hit is not None:
                break
        if hit is None:
            missing.append(r)
        else:
            used.add(hit)
            pairs.append((r, cand_items[hit]))
    extra = [c for i, c in enumerate(cand_items) if i not in used]
    return pairs, missing, extra


def max_deviation(pairs):
    dev = 0
    for a, b in pairs:
        pa, pb = geometry_points(a), geometry_points(b)
        # in the orientation same_copper matched them (a track may come back reversed)
        dev = max(dev, min(max((max(abs(p[0] - q[0]), abs(p[1] - q[1])) for p, q in zip(pa, pts)), default=0)
                           for pts in (pb, pb[::-1])))
    return dev


def strip(rec, *keys):
    return {k: v for k, v in rec.items() if k not in keys}


def zone_definitions(facts):
    """Copper zone definitions without the fill state, in a canonical order. facts.py
    sorts zones by their whole record, `filled` included, so an input zone that was
    never filled sorts differently once validation has refilled it."""
    return sorted((strip(z, "filled") for z in facts["zones"]), key=lambda z: json.dumps(z, sort_keys=True))


def dict_diff(a, b, limit=20):
    out = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            out.append({"key": k, "reference": a.get(k), "candidate": b.get(k)})
            if len(out) >= limit:
                break
    return out


def project_file_hashes(project_dir, board_name):
    out = {}
    for p in sorted(project_dir.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        r = str(p.relative_to(project_dir))
        if r == board_name or p.suffix == ".kicad_prl" or p.name == "fp-info-cache" or "-backups" in r:
            continue
        out[r] = common.sha256(p)
    return out


PLACEMENT_KEYS = ("position", "orientation", "layer")


def footprint_change(ref_name, a, b):
    """One footprints-check difference: which attributes changed (by name), the
    placement on both sides plus every changed attribute's values, and per pad
    what changed. A footprint missing on one side has None there."""
    rec = {"footprint": ref_name}
    if a is None or b is None:
        rec.update(changed=["present"], reference=None if a is None else {k: a.get(k) for k in PLACEMENT_KEYS},
                   candidate=None if b is None else {k: b.get(k) for k in PLACEMENT_KEYS})
        return rec
    changed = [k for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    shown = [k for k in dict.fromkeys((*PLACEMENT_KEYS, *changed)) if k != "pads"]
    rec.update(changed=changed, reference={k: a.get(k) for k in shown}, candidate={k: b.get(k) for k in shown})
    if "pads" in changed:
        pa, pb = a.get("pads") or {}, b.get("pads") or {}
        pads = []
        for p in sorted(set(pa) | set(pb)):
            x, y = pa.get(p), pb.get(p)
            if x == y:
                continue
            keys = ["present"] if x is None or y is None else [k for k in sorted(set(x) | set(y))
                                                               if x.get(k) != y.get(k)]
            whole = keys == ["present"]
            pads.append({"pad": p, "changed": keys, "reference": x if whole else {k: x.get(k) for k in keys},
                         "candidate": y if whole else {k: y.get(k) for k in keys}})
        rec["pads"] = pads[:10]
        rec["pads_changed"] = len(pads)
    return rec


def preservation(ref, cand, ref_dir, cand_dir, board_name, protect, scope, restored=(), human=None, pair_widths=None):
    checks = []

    def check(name, ok, detail="", differences=None, status=None):
        checks.append({"check": name, "status": status or (PASS if ok else FAIL), "detail": detail,
                       "differences": differences or []})

    rf, cf = project_file_hashes(ref_dir, board_name), project_file_hashes(cand_dir, board_name)
    diffs = dict_diff(rf, cf)
    check("project_files", not diffs and not restored,
          "rules (.kicad_pro/.kicad_dru), schematics, library tables and local libraries are byte-identical "
          "to the input" + (f"; restored before grading: {', '.join(restored)}" if restored else ""),
          diffs + [{"key": r, "restored": True} for r in restored])
    check("board_outline", ref["outline"] == cand["outline"], f"{len(ref['outline'])} Edge.Cuts shapes",
          [] if ref["outline"] == cand["outline"] else [{"reference": len(ref["outline"]), "candidate": len(cand["outline"])}])
    layer_keys = ("copper_layer_count", "enabled_layers", "copper_layers")
    lr, lc = {k: ref[k] for k in layer_keys}, {k: cand[k] for k in layer_keys}
    check("layers", lr == lc, f"{ref['copper_layer_count']} copper layers", dict_diff(lr, lc))
    # facts.py sets "setup" to None when it cannot read the (setup) block; the stackup lives
    # in that block, so neither can be graded then (fail closed, never None == None).
    unread = [side for side, f in (("reference", ref), ("candidate", cand)) if f.get("setup") is None]
    if unread:
        why = f"the (setup) block of the {' and '.join(unread)} board could not be read: cannot be graded"
        check("stackup", False, why)
        check("board_setup", False, why)
    else:
        check("stackup", ref["stackup"] == cand["stackup"],
              "explicit stackup" if ref["stackup"] else "no explicit stackup in the input (KiCad default)")
        check("board_setup", ref["setup"] == cand["setup"], "board-file (setup) settings other than the stackup")
    nets_r = {n: v["netclass"] for n, v in ref["nets"].items()}
    nets_c = {n: v["netclass"] for n, v in cand["nets"].items()}
    check("nets_and_netclasses", nets_r == nets_c, f"{len(nets_r)} nets with their effective net classes",
          dict_diff(nets_r, nets_c))
    pads_r = {f"{r}.{p}": v["net"] for r, fp in ref["footprints"].items() for p, v in fp["pads"].items()}
    pads_c = {f"{r}.{p}": v["net"] for r, fp in cand["footprints"].items() for p, v in fp["pads"].items()}
    check("pad_connectivity", pads_r == pads_c, f"{len(pads_r)} pad-to-net assignments", dict_diff(pads_r, pads_c))
    fps_r = {r: strip(v, "value") for r, v in ref["footprints"].items()}
    fps_c = {r: strip(v, "value") for r, v in cand["footprints"].items()}

    moved = [footprint_change(r, fps_r.get(r), fps_c.get(r))
             for r in sorted(set(fps_r) | set(fps_c)) if fps_r.get(r) != fps_c.get(r)]
    check("footprints", not moved, f"{len(fps_r)} footprints: library id, position, orientation, side, "
          "lock state and pad geometry" + (f"; {len(moved)} differ" if moved else ""), moved[:20])
    zr, zc = zone_definitions(ref), zone_definitions(cand)
    check("zones", zr == zc, f"{len(zr)} copper zone definitions (outline, net, layers, priority, clearance, "
          "thermal and fill settings; fill polygons are regenerated)",
          [] if zr == zc else [{"reference": len(zr), "candidate": len(zc)}])
    check("keepouts", ref["rule_areas"] == cand["rule_areas"], f"{len(ref['rule_areas'])} rule areas"
          + (f" (candidate: {len(cand['rule_areas'])})" if len(cand["rule_areas"]) != len(ref["rule_areas"]) else ""),
          [] if ref["rule_areas"] == cand["rule_areas"] else
          [r for r in cand["rule_areas"] if r not in ref["rule_areas"]][:10])
    cg_r, cg_c = ref["copper_graphics"], cand["copper_graphics"]
    check("copper_graphics", cg_r == cg_c,
          f"{len(cg_r)} non-track copper graphics (shapes; text and other items with content and position)"
          + (f" (candidate: {len(cg_c)})" if len(cg_c) != len(cg_r) else ""),
          [] if cg_r == cg_c else
          [{"only_in": "reference", **g} for g in cg_r if g not in cg_c][:10]
          + [{"only_in": "candidate", **g} for g in cg_c if g not in cg_r][:10])

    # Protected copper: locked items always; every pre-existing item with --protect existing;
    # with --protect human the items provenance.designation() names (uuids in `human`).
    def designated(item):
        return item["locked"] or protect == "existing" or (human is not None and item["uuid"] in human)

    # A locked reference item must come back present AND locked: a dropped lock flag is
    # a FAIL (the next pass, or the designer's next push-and-shove, could move it).
    out = {}
    for kind in ("tracks", "vias"):
        rp = [t for t in ref[kind] if designated(t)]
        ru = [t for t in ref[kind] if not designated(t)]
        pairs, missing, rest = match_copper(rp, cand[kind])
        pairs_u, missing_u, extra = match_copper(ru, rest)
        unlocked = [strip(c, "locked") | {"reference_uuid": r.get("uuid"), "lock_flag": "dropped"}
                    for r, c in pairs if r["locked"] and not c["locked"]]
        out[kind] = {"protected": len(rp), "protected_missing": [strip(t, "locked") for t in missing],
                     "protected_max_deviation_nm": max_deviation(pairs), "unprotected_existing": len(ru),
                     "unprotected_removed_or_changed": len(missing_u),
                     "new": [strip(t, "uuid", "locked") for t in extra],
                     "lock_flags_lost": unlocked}
    missing = out["tracks"]["protected_missing"] + out["vias"]["protected_missing"]
    lost_locks = out["tracks"]["lock_flags_lost"] + out["vias"]["lock_flags_lost"]
    designated_what = {"locked": "locked items", "human": "locked items and human copper, see provenance.json"
                       }.get(protect, "all pre-existing copper")
    check("protected_copper", not missing and not lost_locks,
          f"{out['tracks']['protected']} tracks and {out['vias']['protected']} vias designated "
          f"({designated_what}); max deviation "
          f"{max(out['tracks']['protected_max_deviation_nm'], out['vias']['protected_max_deviation_nm'])} nm"
          + (f"; {len(lost_locks)} lock flags dropped" if lost_locks else "")
          + (f"; {len(missing)} missing or altered" if missing else ""), (missing + lost_locks)[:20])
    if protect in ("locked", "human"):
        what = "unlocked" if protect == "locked" else "agent (earlier-step)"
        check("existing_unlocked_copper", True,
              f"{out['tracks']['unprotected_removed_or_changed']} of {out['tracks']['unprotected_existing']} "
              f"{what} tracks and {out['vias']['unprotected_removed_or_changed']} of "
              f"{out['vias']['unprotected_existing']} {what} vias removed or changed (allowed: not protected)",
              status=PASS)

    # New copper must meet the net class of its net and stay inside the routing scope;
    # copper with no net belongs to no in-scope net, so it always fails routing_scope.
    under, outside = [], []
    nets = cand["nets"]
    tol = GEOMETRY_TOLERANCE_NM  # Specctra/KiCad quantisation, e.g. a 0.29972 mm drill read back as 0.299
    for t in out["tracks"]["new"]:
        nc = nets.get(t["net"], {})
        required_width = (pair_widths or {}).get(t["net"], nc.get("track_width"))
        if required_width and t["width"] < required_width - tol:
            under.append({"item": "track", "net": t["net"], "netclass": nc["netclass"], "layer": t["layer"],
                          "width_mm": t["width"] / NM, "required_mm": required_width / NM,
                          "start_mm": [c / NM for c in t["start"]]})
        if not t["net"] or t["net"] not in scope:  # net-less copper is never in scope
            outside.append({"item": "track", "net": t["net"] or netnames.NO_NET, "layer": t["layer"],
                            "start_mm": [c / NM for c in t["start"]]})
    for v in out["vias"]["new"]:
        nc = nets.get(v["net"], {})
        # a microvia is sized by the class's microvia values; through, blind and buried
        # vias by its via values (KiCad's own net class semantics)
        dia_key, drill_key = ("uvia_diameter", "uvia_drill") if v.get("via_type") == "micro" else \
            ("via_diameter", "via_drill")
        need_dia, need_drill = nc.get(dia_key), nc.get(drill_key)
        if (need_dia and v["diameter"] < need_dia - tol) or (need_drill and v["drill"] < need_drill - tol):
            under.append({"item": "via", "via_type": v.get("via_type"), "net": v["net"],
                          "netclass": nc.get("netclass"),
                          "diameter_mm": v["diameter"] / NM, "drill_mm": v["drill"] / NM,
                          "required_mm": [(need_dia or 0) / NM, (need_drill or 0) / NM],
                          "position_mm": [c / NM for c in v["position"]]})
        if not v["net"] or v["net"] not in scope:
            outside.append({"item": "via", "net": v["net"] or netnames.NO_NET,
                            "position_mm": [c / NM for c in v["position"]]})
    check("new_copper_netclass_geometry", not under,
          f"{len(out['tracks']['new'])} new tracks and {len(out['vias']['new'])} new vias checked against "
          f"their net class width / via (microvia) diameter / drill (1 um quantisation tolerance)"
          + (f"; {len(under)} below" if under else ""), under[:20])
    outside_nets = sorted({o["net"] for o in outside})
    check("routing_scope", not outside, f"new copper only on the {len(scope)} in-scope nets"
          + (f"; outside scope: {', '.join(outside_nets[:10])}" if outside else ""), outside[:20])

    stats = route_stats(out["tracks"]["new"], out["vias"]["new"])
    stats["removed_existing_tracks"] = out["tracks"]["unprotected_removed_or_changed"] + len(out["tracks"]["protected_missing"])
    stats["removed_existing_vias"] = out["vias"]["unprotected_removed_or_changed"] + len(out["vias"]["protected_missing"])
    return checks, stats


def route_stats(tracks, vias):
    length = collections.Counter()
    for t in tracks:
        (x1, y1), (x2, y2) = t["start"], t["end"]
        length[t["layer"]] += ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 / NM
    return {"new_tracks": len(tracks), "new_vias": len(vias),
            "new_track_length_mm": round(sum(length.values()), 3),
            "new_track_length_mm_by_layer": {k: round(v, 3) for k, v in sorted(length.items())},
            "new_copper_nets": len({t["net"] for t in tracks} | {v["net"] for v in vias})}


# ------------------------------------------------------------------ validation

def previous_step(attempt):
    """(run_dir, attempt) of the run whose candidate is this attempt's input, or None."""
    src = Path(attempt["source"].get("source_dir") or "/").resolve()
    prev = src.parent.parent
    prev_attempt = load_json(prev / "attempt.json")
    if prev_attempt is None or (prev / prev_attempt["candidate_dir"]).resolve() != src:
        return None
    return prev, prev_attempt


def resolve_origin(run_dir, attempt, origin=None):
    """The --dir chain from run_dir back to its origin, following each step's recorded
    input (attempt.json source.source_dir) to the run whose candidate it was:
    [(run_dir, attempt), ..., (origin, origin_attempt)]. With origin, the chain must
    reach that run; without, it ends at the first run whose input is not a candidate."""
    steps = [(run_dir, attempt)]
    seen = {run_dir.resolve()}
    while origin is None or steps[-1][0].resolve() != origin.resolve():
        prev = previous_step(steps[-1][1])
        if prev is None or prev[0].resolve() in seen:
            if origin is None:
                break
            contract.refuse("input", f"{common.rel(steps[-1][0])}: input is not a routing candidate, so it "
                            f"does not descend from {common.rel(origin)}")
        if prev[1]["board"] != attempt["board"]:
            contract.refuse("input", f"{common.rel(prev[0])}: board {prev[1]['board']} is not {attempt['board']}")
        seen.add(prev[0].resolve())
        steps.append(prev)
    return steps


def rules_files(project_dir, board_name):
    """The project and custom-rule files KiCad grades board_name with. KiCad finds a
    board's project (net classes and their assignments, DRC constraints, severities,
    exclusions) only as the .kicad_pro of the board's own name, and the custom rules
    only as the .kicad_dru of that project's name; a project or rule file under any
    other name is not loaded at all. `refusal` is set when the board has no project
    of its name: KiCad would then grade (and the backends route) with its built-in
    defaults instead of the input's rules."""
    board = Path(project_dir) / board_name
    pro, dru = board.with_suffix(".kicad_pro"), board.with_suffix(".kicad_dru")
    rec = {"project": pro.name, "custom_rules": dru.name if dru.exists() else None,
           "not_loaded": sorted(p.name for p in board.parent.iterdir()
                                if p.suffix in (".kicad_pro", ".kicad_dru") and p.name not in (pro.name, dru.name)),
           "refusal": None}
    if not pro.exists():
        rec["refusal"] = (f"{board_name} has no {pro.name} beside it: KiCad loads a board's rules (net classes, "
                          f"DRC constraints and severities, {dru.name} custom rules) only from the project of "
                          "the board's own name, so it would route and grade with its built-in defaults"
                          + (f"; {', '.join(rec['not_loaded'])} would not be loaded (copy or rename the "
                             f"project to {pro.name})" if rec["not_loaded"] else ""))
    return rec


def validate_dir(run_dir, origin=None):
    """A fresh directory for one grading of run_dir: validate/ the first time (run_attempt),
    then revalidate/<stamp>/ (revalidate-origin/<stamp>/ against an origin)."""
    if origin is None and not (run_dir / "validate").exists():
        return run_dir / "validate"
    parent = run_dir / ("revalidate-origin" if origin is not None else "revalidate")
    stamp = common.now_utc().strftime("%Y%m%dT%H%M%S.%fZ")
    out, n = parent / stamp, 1
    while out.exists():
        n += 1
        out = parent / f"{stamp}-{n}"
    return out


def validate(run_dir, out_dir=None, origin=None, pour_limits=None):
    """Grade run_dir/candidate against run_dir/reference. Re-runnable: an agent may
    edit the candidate and validate it again into a fresh directory. With origin (an
    earlier run whose candidate this run's input descends from through --dir steps),
    grade against the origin's input instead: its rules, baseline, facts and
    protection, with the union of every step's net scope, so a chain of attempts is
    judged against the original design."""
    attempt = load_json(run_dir / "attempt.json")
    if attempt is None:
        contract.refuse("input", f"{run_dir}: not a routing run (attempt.json missing)")
    cand_dir = run_dir / attempt["candidate_dir"]
    scope = set(attempt["scope"]["nets"])
    out_dir = out_dir or validate_dir(run_dir, origin)  # always under the run being graded
    chain = []
    if origin is not None:
        steps = resolve_origin(run_dir, attempt, origin)
        chain = [common.rel(d) for d, _ in steps]
        for _, a in steps[1:]:
            scope |= set(a["scope"]["nets"])
        run_dir, attempt = steps[-1]
        pair_plans, pair_outcomes = {}, {}
        for _, step in reversed(steps):
            pair_plans.update({p["p"]: p for p in step.get("pairs", [])})
            pair_outcomes.update(step.get("pair_routing", {}))
        if any("pairs" in step for _, step in steps):
            attempt = {**attempt, "pairs": [pair_plans[p] for p in sorted(pair_plans)],
                       "pair_routing": pair_outcomes}
    out_dir.mkdir(parents=True, exist_ok=False)
    board_name = attempt["board"]
    ref_dir = run_dir / attempt["reference_dir"]
    board = cand_dir / board_name
    v = {"status": None, "reasons": [], "dir": common.rel(out_dir)}
    floor.apply_preflight(attempt.get("floor"), v)  # input below its declared rule floor fails
    if chain:
        v["chain"] = chain  # this run back to the origin; scope is the union of the steps' scopes
    if not board.exists():
        v.update(status=NOT_RUN, reasons=["no candidate board"])
        return v, {"status": NOT_RUN, "reasons": ["no candidate board"]}
    rules = rules_files(ref_dir, board_name)
    v["rules_files"] = {k: rules[k] for k in ("project", "custom_rules", "not_loaded")}
    if rules["refusal"]:  # fail closed: KiCad would grade with its defaults, not the input's rules
        v.update(status=FAIL, reasons=v["reasons"] + [f"cannot grade against the input's rules: {rules['refusal']}"])
        return v, {"status": NOT_RUN, "reasons": ["the input's rules cannot be loaded"]}

    base = load_json(run_dir / attempt["baseline"]["report"]) if attempt["baseline"].get("report") else None
    # Grade against the input's own rules: project files that differ from the
    # reference are restored first, and the difference itself fails validation.
    # The candidate's own versions are kept under delivered-project-files/ first.
    restored, kept = [], out_dir / "delivered-project-files"
    delivered_files = []
    ref_files, cand_files = project_file_hashes(ref_dir, board_name), project_file_hashes(cand_dir, board_name)
    for rel_path in sorted(set(ref_files) | set(cand_files)):
        if ref_files.get(rel_path) != cand_files.get(rel_path):
            restored.append(rel_path)
            target = cand_dir / rel_path
            entry = {"path": rel_path, "reference_sha256": ref_files.get(rel_path),
                     "delivered_sha256": cand_files.get(rel_path),
                     "action": "restored" if rel_path in ref_files else "removed"}
            if rel_path in cand_files:
                (kept / rel_path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, kept / rel_path)
                entry["kept"] = common.rel(kept / rel_path)
            delivered_files.append(entry)
            if rel_path in ref_files:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ref_dir / rel_path, target)
            else:
                target.unlink()
    if restored:
        v["reasons"].append(f"candidate project files differed from the input and were restored before "
                            f"grading (the delivered versions are kept in {common.rel(kept)}): "
                            f"{', '.join(restored)}")
    v["restored_project_files"] = restored
    v["delivered_project_files"] = delivered_files
    # Board hashes: as delivered (before the refill), as graded (after the refill and
    # save, what the grading DRC read) and as left in candidate/ after grading.
    v["delivered_board_sha256"] = common.sha256(board)
    cand, info = kicad_drc(board, out_dir, "candidate", refill_and_save=True)
    v["drc_commands"] = info["commands"]
    v["validated_board_sha256"] = info.get("board_sha256_graded")
    v["candidate_board_sha256"] = common.sha256(board)
    v["refill_modified_board"] = (v["validated_board_sha256"] != v["delivered_board_sha256"]
                                  if v["validated_board_sha256"] else None)
    if cand is None:
        v.update(status=FAIL, reasons=v["reasons"] + info["reasons"])
        return v, {"status": NOT_RUN, "reasons": ["candidate DRC did not run"]}
    if v["candidate_board_sha256"] != v["validated_board_sha256"]:
        v["reasons"].append("the candidate board changed after DRC graded it")
    if not info["parity_performed"] and attempt.get("schematic"):
        v["reasons"].append(f"schematic parity was not performed: {info.get('parity_skip_message')}")
    v["parity_performed"] = info["parity_performed"] if attempt.get("schematic") else None

    ref_facts = load_json(run_dir / attempt["reference_facts"])
    known = set(ref_facts["nets"]) if ref_facts else None

    def nets_of(u):
        return item_nets(u, known)

    unconnected = cand["unconnected_items"]
    # Fail closed: an unconnected item none of whose bracketed names is a board net
    # cannot be placed outside the scope, so it counts as in scope.
    unidentified = [u for u in unconnected if known is not None and not nets_of(u) & known]
    in_scope = [u for u in unconnected if nets_of(u) & scope or (known is not None and not nets_of(u) & known)]
    routing = {"status": COMPLETE if not in_scope else INCOMPLETE,
               "unconnected_in_scope": len(in_scope), "unconnected_total": len(unconnected),
               "baseline_unconnected_total": len(base["unconnected_items"]) if base else None,
               "unrouted_nets": sorted({n for u in in_scope for n in nets_of(u) & scope})}
    if unidentified:
        routing["unconnected_unidentified"] = [[i.get("description") for i in u.get("items", [])]
                                               for u in unidentified][:20]

    drc = {"violations_total": len(cand["violations"]), "parity_total": len(cand["schematic_parity"])}
    if base is not None:
        new_v, fixed_v = drc_delta(base, cand, "violations")
        new_p, fixed_p = drc_delta(base, cand, "schematic_parity")
        drc.update(new_violations=summarize_findings(new_v), preexisting_violations=len(cand["violations"]) - len(new_v),
                   resolved_violations=len(fixed_v), new_parity=summarize_findings(new_p),
                   resolved_parity=len(fixed_p))
        common.write_json(out_dir / "new-findings.json", {"violations": new_v, "schematic_parity": new_p,
                                                        "resolved_violations": fixed_v})
        if new_v:
            v["reasons"].append(f"{len(new_v)} new DRC violations: " +
                                ", ".join(f"{k} {n}" for k, n in common.count_by(new_v, 'type').items()))
        if new_p:
            v["reasons"].append(f"{len(new_p)} new schematic parity issues")
    else:
        v["reasons"].append("no baseline DRC: cannot separate new from pre-existing findings")
    v["drc"] = drc

    cand_facts, rec = board_facts(board, out_dir / "candidate-facts.json")
    if cand_facts is None or ref_facts is None:
        v["reasons"].append("board facts could not be extracted")
        v["status"] = FAIL
        return v, routing
    # --protect human (provenance.py): only human copper is designated; nets that carried
    # agent copper may be ripped and rerouted, so new copper on them is not out of scope.
    protect = attempt["settings"]["protect"]
    human = provenance.designation(run_dir, attempt, ref_facts) if protect == "human" else None
    checks, stats = preservation(ref_facts, cand_facts, ref_dir, cand_dir, board_name, protect,
                                 (scope | human["agent_nets"]) if human else scope, restored,
                                 human=human["uuids"] if human else None,
                                 pair_widths={name: round(pair["width_mm"] * NM)
                                              for pair in attempt.get("pairs", []) for name in (pair["p"], pair["n"])})
    if "pairs" in attempt:
        native, rec = diff_pairs.probe(board, out_dir / "pair-facts.json", sys.modules[__name__])
        if native is None:
            v["reasons"].append(f"native differential-pair measurements failed (see {rec['log']})")
        pair_records = diff_pairs.grade(attempt["pairs"], attempt.get("pair_routing", {}), native, cand)
        common.write_json(out_dir / "pairs.json", pair_records)
        diff_pairs.apply_routing(routing, pair_records)
    ratchet = provenance.net_completion(base, cand, ref_facts, nets_of)  # every protect mode
    checks.append(ratchet)
    v["net_completion"] = {k: ratchet[k] for k in ("status", "nets_connected_in_input", "nets_lost")}
    checks.append(pour_check(run_dir, attempt, ref_dir, board, out_dir, pour_limits))
    common.write_json(out_dir / "preservation.json", checks)
    v["preservation"] = {c["check"]: c["status"] for c in checks}
    for c in checks:
        if c["status"] == FAIL:
            v["reasons"].append(f"preservation {c['check']}: {c['detail']}")
    v["status"] = FAIL if v["reasons"] else PASS
    routing["stats"] = stats
    if routing["status"] == INCOMPLETE:  # diagnosis only: what lies across the open connections
        boards = {"input_sha256": common.sha256(ref_dir / board_name), "candidate_sha256": v["candidate_board_sha256"],  # the file as it is now (refilled)
                  "graded_sha256": v["validated_board_sha256"]}
        routing["blocked_by"] = blocked.write_report(out_dir, cand, cand_facts, ref_facts, scope, rel=common.rel,
                                                     boards=boards)
    return v, routing


def pour_check(run_dir, attempt, ref_dir, board, out_dir, limits=None):
    """pour_integrity (pour.py): the refilled input's pours against the refilled candidate's.
    Limits (--pour-area-loss, --pour-min-spokes) come from the attempt, overridden by validate's."""
    overrides = {k: v for k, v in (limits or {}).items() if v is not None}
    limits = {k: attempt["settings"].get(k) for k in ("pour_area_loss_pct", "pour_min_spokes")} | overrides
    base = attempt["baseline"].get("report")
    ref, src = pour.reference_facts(run_dir / base if base else None, ref_dir, board.name,
                                    out_dir / "baseline-pour", kicad_drc)
    cand, rec = pour.extract(board, out_dir / "candidate-pour.json")
    if ref is None or cand is None:
        return pour.missing_check("reference" if ref is None else f"candidate ({rec['log']})")
    check, delta = pour.compare(ref, cand, limits.get("pour_area_loss_pct"), limits.get("pour_min_spokes"))
    common.write_json(out_dir / "pour-integrity.json", {**delta, "reference_source": src})
    return check


def overall_exit(process, routing, validation):
    if process["status"] != PASS:
        return 1
    if validation["status"] != PASS:
        return 3
    if routing["status"] != COMPLETE:
        return 2
    return 0


# ------------------------------------------------------------------- attempts

def make_run_dir(args, project):
    """--run-dir PATH (must not exist; no `latest` link), else a fresh
    OUT/runs/<project>/<label>/<stamp>/."""
    if getattr(args, "run_dir", None):
        run_dir = Path(args.run_dir).expanduser().resolve()
        if run_dir.exists() or run_dir.is_symlink():
            contract.refuse("state", f"--run-dir {run_dir} already exists; pass a path that does not exist")
        try:
            run_dir.mkdir(parents=True)
        except FileExistsError:  # a concurrent caller took it between the check and here
            contract.refuse("state", f"--run-dir {run_dir} already exists; pass a path that does not exist")
        return run_dir
    return common.new_run_dir(f"{project}/{args.label or args.backend}")


def discard_run_dir(args, run_dir):
    """A run refused before routing leaves nothing behind: an explicit --run-dir is
    removed (its parents stay), a default one goes through ledger.discard_run_dir."""
    if getattr(args, "run_dir", None):
        shutil.rmtree(run_dir, ignore_errors=True)
    else:
        ledger.discard_run_dir(run_dir)


def log_tail(path, lines=3):
    try:
        text = [l.strip() for l in Path(path).read_text(errors="replace").splitlines() if l.strip()]
    except OSError:
        return ""
    return " | ".join(text[-lines:])[:600]


def ledger_path(args):
    """--ledger PATH (a ledger that outlives a per-stage OUT) or OUT/ledger.jsonl."""
    given = getattr(args, "ledger", None)
    return Path(given).expanduser().resolve() if given else out_path("ledger.jsonl")


def run_attempt(args):
    readonly = sandbox.require_readonly_state(getattr(args, "require_readonly_state", False))
    if readonly:  # the very first step: nothing is written unless STATE is present and read-only
        refusal = sandbox.state_refusal("run")
        if refusal:
            contract.refuse("state", refusal)
    sandbox_facts = sandbox.facts(flag=readonly)
    common.ensure_kicad_config()
    backend = BACKENDS[args.backend]
    cfg = resolve_input(args)
    project = cfg["name"]
    if any("=" not in e for e in args.backend_env or []):
        contract.refuse("usage", "--backend-env takes KEY=VALUE")
    if getattr(args, "diff_pairs", None) and (args.backend != "krt" or args.backend_arg):
        contract.refuse("usage", "--diff-pairs auto requires KRT and does not accept --backend-arg")
    run_dir = make_run_dir(args, project)
    started = time.monotonic()
    fingerprint = None

    def refuse(reason_class, message):  # before routing: the run directory is discarded
        discard_run_dir(args, run_dir)
        contract.refuse(reason_class, message, input_fingerprint=fingerprint, discarded_run_dir=common.rel(run_dir))

    log(f"[route] {args.backend} on {project} -> {common.rel(run_dir)}")
    try:
        ref_dir, manifest = snapshot_input(cfg, run_dir)
    except contract.Refusal as r:  # e.g. --rev is not a commit
        refuse(r.reason_class, r.message)
    board_name = cfg["pcb"]
    rules = rules_files(ref_dir, board_name)
    if rules["refusal"]:  # KiCad would route and grade with its defaults: never a faithful grade
        refuse("input", rules["refusal"])
    if rules["not_loaded"]:
        log(f"[route] NOTE: KiCad grades {board_name} with {rules['project']}"
            + (f" and {rules['custom_rules']}" if rules["custom_rules"] else " (no custom rules file)")
            + f"; not loaded: {', '.join(rules['not_loaded'])}")
    fingerprint = input_fingerprint(manifest)
    (run_dir / "validate").mkdir()
    ref_facts, rec = board_facts(ref_dir / board_name, run_dir / "validate" / "reference-facts.json")
    (run_dir / "validate").rename(run_dir / "facts")
    if ref_facts is None:
        refuse("input", f"could not read the input board {board_name} (facts.py exit {rec['exit_code']}): "
                        f"{log_tail(run_dir / 'facts' / 'reference-facts.log')}")
    try:
        scope, scope_info = select_nets(ref_facts, args.nets, args.netclass)
    except NetSelectionError as e:
        refuse("input", str(e))
    if not scope:
        refuse("input", "the net selection matches no nets")
    pairs = None
    if getattr(args, "diff_pairs", None):
        native, rec = diff_pairs.probe(ref_dir / board_name, run_dir / "facts" / "pair-facts.json",
                                       sys.modules[__name__])
        if native is None:
            refuse("input", f"could not read native differential-pair settings (see {rec['log']})")
        try:
            pairs = diff_pairs.detect(native, set(scope))
        except ValueError as e:
            refuse("input", str(e))
        if args.default_clearance and any(args.default_clearance > p["gap_mm"] for p in pairs):
            refuse("input", "--default-clearance exceeds a native pair gap; KRT would silently raise that gap")
        if not (KRT_DIR / "py_router" / "route_diff.py").is_file():
            refuse("state", "KRT route_diff.py is not installed")
    if backend.get("preflight"):  # what the backend's bridge cannot carry, before it starts
        msg = backend["preflight"](ref_dir / board_name, ref_facts, run_dir)
        if msg:
            refuse("input", msg)
    settings = {"backend": args.backend, "budget_s": args.budget, "protect": args.protect,
                "max_iterations": args.max_iterations, "passes": args.passes,
                "default_clearance_mm": args.default_clearance, "clearance_margin_mm": args.clearance_margin,
                "pour_area_loss_pct": args.pour_area_loss, "pour_min_spokes": args.pour_min_spokes,
                "backend_args": args.backend_arg or [], "backend_env": dict(e.split("=", 1) for e in args.backend_env or []),
                "reserve_spokes": args.reserve_spokes,
                "barrier": None,
                "stall_s": getattr(args, "stall", None), **scope_info}
    if pairs is not None:
        settings.update(diff_pairs="auto", pairs=[dict(p) for p in pairs])
    log(f"[route] scope: {scope_info['mode']} ({len(scope)} of {len(ref_facts['nets'])} nets)")
    floor_rec = floor.preflight(cfg, ref_dir, board_name, with_history=args.floor_history)
    if floor_rec and floor_rec["floor_violated"]:
        log(f"[route] rule floor: {len(floor_rec['floor_violated'])} setting(s) below the declared floor; "
            "validation will fail")
    for note in (floor_rec or {}).get("notes", []):
        log(f"[route] rule floor NOTE: {note}")

    # The declared floor is not a KiCad project file, so it is outside the input fingerprint;
    # it changes the verdict, so the ledger must see it.
    settings["rule_floor_sha256"] = (common.sha256_of(floor_rec["declared"])
                                     if floor_rec and floor_rec.get("declared") else None)
    # --barrier-from: keepouts where an earlier run's new copper collided (barrier.py); planned
    # before the ledger gate, because the barrier positions change the attempt
    barrier_rec = barrier.prepare(getattr(args, "barrier_from", None), getattr(args, "barrier_size", None),
                                  run_dir, fingerprint)
    settings["barrier"] = barrier.settings_of(barrier_rec)
    if args.protect == "human":
        protection_attempt = {"board": board_name, "settings": settings,
                              "reference_dir": str(ref_dir.relative_to(run_dir)),
                              "reference_facts": "facts/reference-facts.json",
                              "source": {k: manifest.get(k) for k in
                                         ("source_dir", "source", "source_git_head", "source_dirty_files")}}
        prov = provenance.prepare(run_dir, protection_attempt, resolve_origin(run_dir, protection_attempt))
        if settings.get("protect_only"):
            settings["protect_only_sha256"] = common.sha256(run_dir / settings["protect_only"])
        log(f"[route] provenance: {prov.get('detail') or prov['reason']}")
    backend_version = backend["version"]()
    backend_version["execution_identity"] = evidence.backend_identity(args.backend, backend_version)
    backend_version["execution_identity"].pop("commit", None)
    gate = ledger.check(ledger_path(args), fingerprint, args.backend, backend_version, settings,
                        force=getattr(args, "force", False))
    if gate["refuse"]:
        refuse("ledger", gate["message"])
    if getattr(args, "canary", False):
        from . import canary
        c = canary.run_canary(args.backend)
        if c["status"] != PASS:
            refuse("canary", f"canary pre-flight failed: {'; '.join(c['reasons'][:3])}")
    for note in gate["notes"]:
        log(f"[route] ledger: {note}")

    log("[route] baseline: native DRC of the untouched input (refilled)")
    base, base_info = baseline_drc(ref_dir, board_name, fingerprint)
    if pairs is not None and base is not None:
        opened = diff_pairs.open_nets(base, ref_facts["nets"])
        for pair in pairs:
            pair["initially_connected"] = not {pair["p"], pair["n"]} & opened
    attempt = {
        "schema": contract.schema_id("attempt", 2 if pairs is not None else 1),
        "backend": args.backend, "project": project, "board": board_name,
        "schematic": cfg.get("schematic"), "input_fingerprint": fingerprint,
        "source": {k: manifest.get(k) for k in ("source_dir", "source", "source_git_head", "source_dirty_files")},
        "reference_dir": str(ref_dir.relative_to(run_dir)),
        "candidate_dir": str((run_dir / "candidate" / ref_dir.name).relative_to(run_dir)),
        "reference_facts": "facts/reference-facts.json",
        "rules_files": {k: rules[k] for k in ("project", "custom_rules", "not_loaded")},
        "baseline": {"report": os.path.relpath(base_info["report"], run_dir) if base else None,
                     "cached": base_info.get("cached"), "reasons": base_info["reasons"],
                     "identity": base_info.get("identity"), "generation": base_info.get("generation")},
        "scope": {"nets": scope, **scope_info},
        "settings": settings,
        "floor": floor_rec,
    }
    if pairs is not None:
        attempt["pairs"] = pairs
    common.write_json(run_dir / "attempt.json", attempt)

    unsupported = backend["unsupported"](ref_facts, ref_dir, settings)
    work = run_dir / "work"
    copy_project(ref_dir, work / "input" / ref_dir.name)
    log(f"[route] routing with {args.backend} (budget {args.budget}s)")
    t0 = time.monotonic()
    work_board = work / "input" / ref_dir.name / board_name
    routing_settings = {**settings, "pairs": pairs} if pairs is not None else settings
    # --reserve-spokes: corridors reserved on the work input, stripped from the output (spokes.py)
    # --barrier-from: collision keepouts injected the same way, outside the spoke corridors
    proc = barrier.around_route(barrier_rec, work, work_board, lambda: spokes.around_route(
        args.reserve_spokes, work, work_board,
        lambda: backend["route"](work, work_board, scope, ref_facts, routing_settings)))
    proc["runtime_s"] = round(time.monotonic() - t0, 3)
    if pairs is not None:
        attempt["pair_routing"] = proc.pop("pair_routing", {})
        common.write_json(run_dir / "attempt.json", attempt)
    stall.annotate(proc)
    unsupported += [f"backend reported: {u}" for u in proc.pop("unsupported_reported", [])]
    for u in (proc.get("bridge") or {}).get("export", {}).get("dru_untranslated", []):
        unsupported.append(f"custom rule '{u['rule']}' not given to the backend: {u['reason']}")
    log(f"[route] process: {proc['status']}" + (f" ({'; '.join(proc['reasons'])})" if proc["reasons"] else ""))

    cand_dir = run_dir / attempt["candidate_dir"]
    backend_output_sha = common.sha256(proc["output_board"]) if proc["status"] == PASS else None
    if proc["status"] == PASS:
        copy_project(ref_dir, cand_dir)
        shutil.copyfile(proc["output_board"], cand_dir / board_name)
        log("[route] validating the candidate (refill, DRC + parity, preservation)")
        validation, routing = validate(run_dir)
        if barrier.outcome(barrier_rec, run_dir, attempt):
            o = barrier_rec["outcome"]
            log(f"[route] barrier: {o['entered']} entered by new copper, {o['recurred']} collision(s) recurred")
    else:
        validation = {"status": NOT_RUN, "reasons": ["backend produced no candidate"]}
        routing = {"status": NOT_RUN, "reasons": ["backend produced no candidate"]}
    repro = statemanifest.reproducibility(args.backend)  # KICADROUTE-7: attempt or measurement
    qualification = statemanifest.attempt_qualification(repro, proc)
    proc["measurement_qualification"] = qualification
    result = {
        "schema": contract.schema_id("route", 2) if pairs is not None else contract.schema_id("route"),
        "kind": "route",
        "backend": {"name": args.backend, **backend_version, "reproducible": repro["reproducible"],
                    "reproducible_basis": repro["basis"]},
        "measurement": qualification["measurement"],
        "input": {"project": project, "board": board_name, "fingerprint": fingerprint,
                  **attempt["source"], "baseline": attempt["baseline"], "rules_files": attempt["rules_files"]},
        "settings": settings,
        "scope": {"mode": scope_info["mode"], "nets": len(scope), "nets_list": scope if len(scope) <= 50 else None},
        "unsupported": unsupported,
        "floor": floor_rec,
        "process": {k: v for k, v in proc.items() if k not in ("output_board",)},
        "routing": routing,
        "validation": validation,
        "candidate": {"dir": common.rel(cand_dir), "board": common.rel(cand_dir / board_name)} if proc["status"] == PASS else None,
        "run_dir": common.rel(run_dir),
        "runtime_s": round(time.monotonic() - started, 3),
        "tool_versions": {"kicad-cli": common.tool_versions()["kicad-cli"]},
        # KICADROUTE-4: hashes of what was graded and of the tools that graded it
        "evidence": evidence.block(run_dir, attempt, run_dir / "validate", validation, backend_output_sha),
        "tool_identity": evidence.tool_identity(args.backend, backend_version, ref_facts.get("kicad_version")),
        "finished_utc": common.now_utc().isoformat(),
    }
    if pairs is not None:
        result["pairs"] = load_json(run_dir / "validate" / "pairs.json")
        if result["pairs"] is None:
            result["pairs"] = diff_pairs.grade(pairs, attempt.get("pair_routing", {}), None, None)
    result["exit_code"] = overall_exit(proc, routing, validation)
    result["ledger"] = {k: gate.get(k) for k in ("fingerprint", "short", "forced", "forced_over",
                                                 "upstream_changed_since", "previous_runs", "notes")}
    result["ledger"]["path"] = str(ledger_path(args))
    result["sandbox"] = sandbox_facts
    common.write_json(run_dir / "result.json", result)
    ledger.append(ledger_path(args), gate, result)
    (run_dir / "RESULT.md").write_text(render_result(result))
    print_result(result)
    return result


def print_result(r):
    rt = r["routing"]
    log(f"[route] routing: {rt['status']}" + (f" ({rt.get('unconnected_in_scope')} unconnected in scope)"
                                               if rt["status"] == INCOMPLETE else ""))
    val = r["validation"]
    log(f"[route] validation: {val['status']}" + (f" ({'; '.join(val['reasons'][:4])})" if val["reasons"] else ""))
    if r.get("candidate"):
        log(f"[route] candidate: {r['candidate']['board']}")
    log(f"[route] result: {r['run_dir']}/result.json  (exit {r['exit_code']})")


def render_result(r):
    lines = [f"# Routing attempt: {r['backend']['name']} on {r['input']['project']}", "",
             f"- Backend: {r['backend']['name']} {r['backend'].get('version')} ({r['backend'].get('build', '')})",
             f"- Input: `{r['input']['board']}` from {r['input'].get('source')} @ {r['input'].get('source_git_head')}"
             f" (fingerprint {r['input']['fingerprint'][:16]})",
             f"- Scope: {r['scope']['mode']}, {r['scope']['nets']} nets; protect: {r['settings']['protect']};"
             f" budget {r['settings']['budget_s']} s",
             f"- Candidate: `{r['candidate']['board']}`" if r.get("candidate") else "- Candidate: none",
             "", "| Result | Status | Detail |", "|---|---|---|",
             f"| process | {r['process']['status']} | exit {r['process'].get('exit_code')}, "
             f"{r['process'].get('runtime_s')} s{'; ' + '; '.join(r['process']['reasons']) if r['process']['reasons'] else ''} |",
             f"| routing | {r['routing']['status']} | {r['routing'].get('unconnected_in_scope', '-')} unconnected in scope, "
             f"{r['routing'].get('unconnected_total', '-')} total (input: {r['routing'].get('baseline_unconnected_total', '-')}) |",
             f"| validation | {r['validation']['status']} | {'; '.join(r['validation'].get('reasons', [])) or 'no new findings'} |",
             ""]
    if r["validation"].get("preservation"):
        lines += ["| Preservation check | Status |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in r["validation"]["preservation"].items()]
        lines.append("")
    if "pairs" in r:
        lines += ["Differential pairs (lengths and skew in mm; native planar copper):", "",
                  "| P | N | Status | Width / gap | P length | N length | Skew | Gap violations |",
                  "|---|---|---|---|---|---|---|---|"]
        lines += [f"| {p['p']} | {p['n']} | {p['status']} | {p['width_mm']} / {p['gap_mm']} | "
                  f"{p['length_p']} | {p['length_n']} | {p['skew']} | {p['gap_violations']} |" for p in r["pairs"]]
        lines.append("")
    if r["validation"].get("drc"):
        lines += ["Native DRC (original rules and severities):", "",
                  "```json", json.dumps(r["validation"]["drc"], indent=1), "```", ""]
    if r["unsupported"]:
        lines += ["Unsupported or approximated by this backend:", ""] + [f"- {u}" for u in r["unsupported"]] + [""]
    lines += blocked.render_md(r["routing"].get("blocked_by"))
    if r["routing"].get("stats"):
        lines += ["Route statistics:", "", "```json", json.dumps(r["routing"]["stats"], indent=1), "```"]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ processes

def run_budgeted(argv, cwd, log_path, budget, env_extra=None, stop_file=None, grace=None, monitor=None):
    """Run a backend with a wall-clock budget. At the budget the stop file (if the
    backend supports one) asks for a clean stop; after a grace period it is killed.
    A stall.Monitor tails the log for heartbeats and, with a stall limit, treats
    no progress for that long like the budget (with a grace of at most the limit).
    The backend leads its own process group: terminate/kill signal the whole group,
    and whatever it leaves running in the group after it exits is killed too
    (procgroup.py), so no backend child outlives the call."""
    env = common.tool_env()
    env.update(env_extra or {})
    if monitor is not None:
        env.setdefault("PYTHONUNBUFFERED", "1")  # line-timely log for the heartbeat tail
    grace = grace if grace is not None else max(30, budget // 5)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"argv": [str(a) for a in argv], "cwd": common.rel(cwd), "log": common.rel(log_path),
           "env": {k: v for k, v in (env_extra or {}).items()}, "budget_s": budget}
    start = time.monotonic()
    with open(log_path, "wb") as out:
        try:
            proc = subprocess.Popen([str(a) for a in argv], cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT,
                                    **procgroup.POPEN_KW)
        except FileNotFoundError as e:
            rec.update(exit_code=None, error="executable_not_found", detail=str(e), duration_s=0)
            return rec
        try:
            try:
                (monitor.wait(proc, budget) if monitor is not None else proc.wait(timeout=budget))
            except subprocess.TimeoutExpired:
                if monitor is not None and monitor.stalled:
                    rec["stalled"] = True
                    grace = min(grace, max(15, monitor.stall_s))
                else:
                    rec["budget_reached"] = True
                if stop_file:
                    Path(stop_file).touch()
                    rec["stop_requested"] = True
                rec["stop_s"] = round(time.monotonic() - start, 3)
                try:
                    proc.wait(timeout=grace if stop_file else 5)
                except subprocess.TimeoutExpired:
                    procgroup.terminate(proc)
                    rec["killed"] = True
        except BaseException:
            # Ctrl-C or any other error here: the new session no longer gets the
            # terminal's SIGINT, so the group must not be left running.
            procgroup.terminate(proc, term_timeout=5)
            procgroup.reap_leftovers(proc, term_timeout=2)
            raise
        left = procgroup.reap_leftovers(proc)
        if left:
            rec["leftover_children_killed"] = left
    if monitor is not None:
        monitor.finish()
        rec.update(monitor.summary())
    rec["exit_code"] = proc.returncode
    rec["duration_s"] = round(time.monotonic() - start, 3)
    if "stop_s" in rec:
        rec["stop_to_exit_s"] = round(rec["duration_s"] - rec["stop_s"], 3)   # the overrun past the stop
    rec["output_tail"] = log_path.read_text(errors="replace")[-4000:]
    return rec


def tagged_json(text, tag):
    """Last `TAG: {json}` line a backend printed."""
    found = None
    for line in text.splitlines():
        if line.startswith(tag + ":"):
            try:
                found = json.loads(line[len(tag) + 1:])
            except json.JSONDecodeError:
                pass
    return found


# ------------------------------------------------------------- KRT backend

# Every relaxation, fallback and writeback KRT offers is switched off: a failed
# connection must stay failed rather than be delivered below the input's rules.
KRT_FLAGS = [
    "--escalation", "off",          # no fab/board escalation ladders, net rescue or terminal escalation
    "--fab-tier", "standard",       # the fab floor is a hard floor
    "--strict-sizes",               # exit 3 if anything it tracked shipped below its requirement
    "--no-power-tap-neckdown",      # wide nets do not neck down into pads
    "--no-fix-drc-settings",        # no .kicad_pro writeback (the wrapper restores the originals anyway)
]
KRT_PROTECT_EXISTING_FLAGS = ["--keep-input-copper", "--no-stub-layer-swap"]
KRT_ENV = {
    "KICAD_INRUN_FLOOR_SYNC": "0",     # no mid-run floor writes to the output .kicad_pro
    "KICAD_TERMINAL_ESCALATION": "0",
    "KICAD_IMPEDANCE_NECKDOWN": "0",
    "KICAD_RESCUE_CAP_MOVE": "0",      # never move passives to rescue a net
    "KICAD_DYNAMIC_ITERATIONS": "0",   # --max-iterations is a hard cap
    "KICAD_CLI": "/usr/bin/kicad-cli",
}
KRT_PROTECT_EXISTING_ENV = {"KICAD_RIP_PREEXISTING": "0", "KICAD_FINALIZE_RIP": "0"}
# KRT checks KICAD_STOP_FILE only between nets of its main single-ended pass. The
# passes after it (reroute, Phase 3, net rescue, terminal escalation, plane
# finalize, final reconciliation) never look at it, so a stop requested there is
# honoured only by the kill after the grace period. Their kill switches are the
# caller's choice (--backend-env), never forced: switching a pass off changes the
# attempt, and neither knob touches a rule (docs/hygiene.md, "Budget overrun").
KRT_OVERRUN_KNOBS = {"net rescue": "KICAD_NET_RESCUE=0", "plane finalize": "KICAD_PLANE_FINALIZE=0",
                     "oracle recheck": "KICAD_PLANE_FINALIZE=0", "final reconciliation": "KICAD_PLANE_FINALIZE=0"}


def krt_overrun_hint(phase, stalled=False):
    """What to change when KRT ignored the stop file in `phase` and was killed."""
    more = "a larger --stall" if stalled else "a larger --budget"
    knob = KRT_OVERRUN_KNOBS.get(phase)
    if knob:
        return f"retry with {more}, or --backend-env {knob} (KRT's {phase} ignores the stop file)"
    return (f"retry with {more}; KRT checks the stop file only between nets of its main pass, and "
            "--backend-env KICAD_NET_RESCUE=0 / KICAD_PLANE_FINALIZE=0 switch off its longest later passes")


KRT_REQUIRED_FLAGS = KRT_FLAGS[::2] + KRT_PROTECT_EXISTING_FLAGS + ["--json-out", "--nets", "--output",
                                                                     "--max-iterations"]


def krt_version():
    v = engine.identity()
    v["rust_router"] = None
    if route_python().exists():
        r = subprocess.run([str(route_python()), "-c", "import sys; sys.path.insert(0, 'rust_router'); "
                            "import grid_router; print(grid_router.__version__)"],
                           cwd=KRT_DIR, capture_output=True, text=True, env=common.tool_env())
        v["rust_router"] = r.stdout.strip() if r.returncode == 0 else None
    return v


def krt_unsupported(facts, ref_dir, settings):
    notes = []
    if (ref_dir / Path(facts["board"]).with_suffix(".kicad_dru").name).exists():
        notes.append("KRT reads only a subset of .kicad_dru custom rules; every rule is still graded by KiCad DRC")
    if any(v["via_type"] != "through" for v in facts["vias"]):
        notes.append("blind/buried/micro vias are present; KRT routes with through vias")
    if any(t["kind"] == "arc" for t in facts["tracks"]):
        notes.append("existing arc tracks are kept as obstacles; KRT routes octilinear segments")
    if settings.get("clearance_margin_mm"):
        notes.append("--clearance-margin is not supported by KRT and was not applied (use --default-clearance)")
    notes.append("KRT's cleanup may neck wide new segments near foreign pads without a flag; the "
                 "new_copper_netclass_geometry check reports any new copper below its net class")
    return notes


# KRT announces each custom rule it does not apply on its own log line. The wording
# drifts between releases (v0.23: ".kicad_dru: rule 'X': ... -- skipped, KiCad will
# still enforce it", ".kicad_dru rule 'X': NOT honoured (...)", ".kicad_dru rule 'X':
# parsed, but ... has no consumer in this tool yet", "rule 'X': unknown constraint ...
# not evaluated"), so match a rule subject plus any not-applied verdict rather than
# one exact phrase. Over-reporting is harmless (informational; KiCad DRC grades every
# rule); under-reporting hides an unapplied rule.
KRT_RULE_SUBJECT = re.compile(r"\.kicad_dru\b|\brule\s+['\"`]", re.I)
KRT_RULE_NOT_APPLIED = re.compile(
    r"\bskip(?:ped|ping|s)?\b|\bnot\s+(?:honou?red|evaluated|enforced|applied|supported|modell?ed|translated)\b"
    r"|\bunsupported\b|\bno\s+consumer\b|\bignored\b|\bunknown\s+constraint\b", re.I)


def krt_preflight(board, facts, run_dir):
    """Refuse unmodelled copper before the owned router starts."""
    from .router.py_router.sexpr import unsupported_copper
    try:
        return unsupported_copper(Path(board).read_text())
    except ValueError as error:
        return str(error)


def krt_artwork_environment(work, board, facts):
    from .router.py_router.sexpr import copper_text_blocks, copper_text_signature
    content = Path(board).read_text()
    if not copper_text_blocks(content):
        return {}
    texts = facts.get('routing_copper_text')
    if texts is None:
        raise ValueError('Native facts lack copper-text bounding-box evidence')
    path = Path(work) / 'copper-text.json'
    common.write_json(path, {'signature': copper_text_signature(content), 'texts': texts})
    return {'KICAD_ROUTE_COPPER_TEXT': str(path.resolve())}


def krt_unsupported_rules(text):
    """The KRT log lines that say a design rule was not applied, deduplicated and sorted."""
    return sorted({line.strip() for line in text.splitlines()
                   if KRT_RULE_SUBJECT.search(line) and KRT_RULE_NOT_APPLIED.search(line)})


def krt_route(work, board, scope, facts, settings):
    if settings.get("diff_pairs"):
        return diff_pairs.route(work, board, scope, facts, settings, sys.modules[__name__])
    out_dir = work / "krt-out"
    out_dir.mkdir(parents=True)
    output = out_dir / board.name
    summary = out_dir / "summary.json"
    stop = work / "STOP"
    argv = [route_python(), KRT_DIR / "py_router" / "route.py", board, "--output", output,
            *KRT_FLAGS, "--json-out", summary]
    env = dict(KRT_ENV, KICAD_STOP_FILE=str(stop))
    if settings["protect"] == "existing":
        argv += KRT_PROTECT_EXISTING_FLAGS
        env.update(KRT_PROTECT_EXISTING_ENV)
    if settings.get("max_iterations"):
        argv += ["--max-iterations", str(settings["max_iterations"])]
    if settings.get("default_clearance_mm"):
        default = max((n["clearance"] for n in facts["nets"].values() if n["netclass"] == "Default"
                       and n["clearance"]), default=0) / NM
        # KRT's --clearance sets the Default class; never below the input's own value
        argv += ["--clearance", f"{max(settings['default_clearance_mm'], default):g}"]
    if settings["mode"] == "all":
        argv += ["--nets", "*"]
    else:
        argv += ["--nets", *netnames.krt_patterns(scope, facts["nets"])]  # exact, argparse-safe
    argv += settings["backend_args"]
    env.update(settings.get("backend_env") or {})
    locks = None
    if settings["protect"] in ("existing", "human"):
        # KRT never touches locked copper, but its rescue path reworks unlocked input
        # copper despite --keep-input-copper: lock it for the run, restore flags after.
        # --protect human locks only the human copper (provenance.py) and leaves KRT
        # free to rip and reroute agent copper.
        only = ["--only", work.parent / settings["protect_only"]] if settings.get("protect_only") else []
        lr = common.run_cmd([pcbnew_python(), KICAD_SCRIPTS / "locks.py", "lock", board, work / "locks.json", *only],
                         work, work / "lock-existing.log", timeout=600)
        locks = {"lock": tagged_json(lr["output"], "LOCKS_JSON"), "lock_exit": lr["exit_code"]}
        if lr["exit_code"] != 0:
            return {"status": FAIL, "reasons": [f"could not lock existing copper (see {lr['log']})"],
                    "commands": [common.public(lr)], "exit_code": None, "output_board": None}
    env.update(krt_artwork_environment(work, board, facts))
    rec = run_budgeted(argv, work, work / "krt.log", settings["budget_s"], env, stop_file=stop,
                       monitor=stall.Monitor("krt", work / "krt.log", settings.get("stall_s")))
    text = Path(work / "krt.log").read_text(errors="replace")
    report = load_json(summary) or tagged_json(text, "JSON_SUMMARY")
    proc = {"status": PASS, "reasons": [], "exit_code": rec.get("exit_code"), "commands": [rec],
            "backend_report": None, "output_board": output,
            # rules KRT says it did not apply; KiCad DRC still grades them
            "unsupported_reported": krt_unsupported_rules(text)}
    if report:
        dr = report.get("design_rules", {})
        proc["backend_report"] = {k: report.get(k) for k in (
            "successful", "failed", "failed_single", "open_single", "failed_multipoint", "multipoint_deficit",
            "pad_pairs_connected", "pad_pairs_total", "total_vias", "total_iterations", "total_time",
            "protected_skipped", "scope")}
        proc["backend_report"]["relaxations"] = {k: dr.get(k) for k in (
            "escalation_policy", "fab_tier", "narrowed", "fab_tier_escalations", "min_delivered", "unsupported_rules")}
        proc["backend_report"]["improvement_gate"] = tagged_json(text, "JSON_IMPROVEMENT_GATE")
        proc["backend_report"]["project_writes"] = tagged_json(text, "PROJECT_WRITES_JSON")
    if rec.get("error"):
        proc.update(status=FAIL, reasons=[f"KRT could not start: {rec.get('detail')}"])
    elif rec.get("killed") and rec.get("stalled"):
        proc.update(status=FAIL, reasons=[f"KRT {stall.stall_reason(rec)} (--stall {settings['stall_s']}), did not "
                                          "stop at the stop file, and was killed; "
                                          + krt_overrun_hint(rec["stall"]["phase"], stalled=True)])
    elif rec.get("killed"):
        last = [l for l in text.splitlines() if l.strip()][-1:] or [""]
        proc.update(status=FAIL, reasons=[f"KRT exceeded the {settings['budget_s']} s budget, did not stop at the "
                                          f"stop file, and was killed (last output: {last[0].strip()[:120]!r}); "
                                          + krt_overrun_hint(rec.get("last_phase"))])
    elif rec["exit_code"] == 3:
        proc["reasons"].append("KRT --strict-sizes: it reports delivering geometry below a requirement "
                               "(candidate kept for grading)")
    elif rec["exit_code"] != 0:
        proc.update(status=FAIL, reasons=[f"KRT exited {rec['exit_code']}"])
    if rec.get("stop_requested") and not rec.get("killed"):
        proc["reasons"].append(f"KRT {stall.stall_reason(rec)}: stopped early and wrote the board as it stood"
                               if rec.get("stalled") else
                               "budget reached: KRT stopped early and wrote the board as it stood")
    if proc["status"] == PASS and not output.exists():
        proc.update(status=FAIL, reasons=proc["reasons"] + ["KRT wrote no output board"])
    if locks is not None and proc["status"] == PASS:
        rr = common.run_cmd([pcbnew_python(), KICAD_SCRIPTS / "locks.py", "restore", output, work / "locks.json"],
                         work, work / "restore-locks.log", timeout=600)
        proc["commands"].append(common.public(rr))
        locks["restore"] = tagged_json(rr["output"], "LOCKS_JSON")
        if rr["exit_code"] != 0:
            proc.update(status=FAIL, reasons=proc["reasons"] + [f"could not restore lock flags (see {rr['log']})"])
    proc["existing_copper_locks"] = locks
    # What KRT wrote beside its output is recorded, never used.
    writeback = []
    for sib in sorted(out_dir.glob("*")):
        if sib.suffix in (".kicad_pro", ".kicad_dru", ".kicad_prl"):
            orig = board.with_suffix(sib.suffix)
            same = orig.exists() and json_or_text(orig) == json_or_text(sib)
            writeback.append({"file": sib.name, "identical_to_input": same})
    proc["project_writeback"] = writeback
    return proc


def json_or_text(path):
    try:
        data = json.loads(path.read_text())
        data.get("meta", {}).pop("filename", None)
        return data
    except (json.JSONDecodeError, AttributeError):
        return path.read_text()


# ------------------------------------------------------ Freerouting backend

FREEROUTING_REPO = "freerouting/freerouting"
# Freerouting checks GitHub for new versions on every start and has no switch for
# it; a dead proxy keeps routing runs offline. Analytics are disabled with -da.
OFFLINE_ENV = {"HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
               "FREEROUTING__USAGE_AND_DIAGNOSTIC_DATA__DISABLE_ANALYTICS": "true"}


def freerouting_installed():
    cur = FREEROUTING_DIR / "current"
    rel = load_json(cur / "release.json") if cur.exists() else None
    if not rel:
        return None
    rel["jar"] = cur / rel["asset"]
    return rel if rel["jar"].exists() else None


def jar_java_major(jar):
    """Java feature release the jar was built for (its manifest's Created-By)."""
    import zipfile
    with zipfile.ZipFile(jar) as z:
        mf = z.read("META-INF/MANIFEST.MF").decode(errors="replace")
    m = re.search(r"Created-By:\s*(\d+)", mf) or re.search(r"Build-Jdk-Spec:\s*(\d+)", mf)
    return int(m.group(1)) if m else None


def find_java(min_major):
    """Newest installed JVM that satisfies min_major (the system default is left alone)."""
    best = None
    for java in sorted(Path("/usr/lib/jvm").glob("*/bin/java")) + [Path(shutil.which("java") or "/nonexistent")]:
        if not java.exists():
            continue
        r = subprocess.run([str(java), "-version"], capture_output=True, text=True)
        m = re.search(r'version "(\d+)', r.stderr)
        if m and int(m.group(1)) >= (min_major or 0) and (best is None or int(m.group(1)) > best[1]):
            best = (java.resolve(), int(m.group(1)))
    return best


def freerouting_version():
    rel = freerouting_installed()
    if not rel:
        return {"version": None, "build": "not installed"}
    need = jar_java_major(rel["jar"])
    java = find_java(need)
    return {"version": rel["tag"], "build": f"{rel['asset']} {rel.get('build_revision', '')}".strip(),
            "sha256": rel["sha256"], "java": f"{java[0]} ({java[1]})" if java else None,
            "java_required": need, "source": f"https://github.com/{FREEROUTING_REPO}"}


def freerouting_unsupported(facts, ref_dir, settings):
    notes = ["Freerouting never rips up pre-existing copper (KiCad exports it as fixed/protected); "
             "--protect locked behaves like --protect existing",
             "Specctra carries per-class width/clearance/via only: custom .kicad_dru rules, diff pairs and "
             "length tuning are not exported; KiCad DRC grades them",
             "rule areas export only their no-tracks/no-vias restrictions; zones export as plane outlines"]
    if settings.get("protect") == "human":
        notes.append("--protect human: agent copper is exported unlocked ((type route)) and rebuilt from the "
                     "session, but Freerouting returns it as (type protect) and never rips it up; use KRT to "
                     "reroute agent copper")
    dru = ref_dir / Path(facts["board"]).with_suffix(".kicad_dru").name
    if dru.exists() and "(rule" in dru.read_text():
        notes.append(f"{dru.name}: net-class-to-net-class clearance rules are translated to Specctra "
                     "class_class rules; any other custom rule is listed as untranslated below")
    if any(t["kind"] == "arc" for t in facts["tracks"]):
        notes.append("existing arc tracks export as straight chords; unlocked arcs come back as segments")
    if any(v["via_type"] != "through" for v in facts["vias"]):
        notes.append("blind/buried/micro vias are present; Freerouting places through vias only")
    return notes


def freerouting_preflight(board, facts, run_dir):
    """A refusal message (exit 4, before Freerouting starts) when the Specctra bridge
    cannot write every net name one way (KICADROUTE-56), else None. Only boards with
    a `"` in a net name pay for it: two names KiCad writes the same are refused from
    the facts, then `specctra.py check` reads KiCad's own export of the board."""
    from .kicad import specctra
    names = [n for n in facts["nets"] if n]
    if not any('"' in n for n in names):
        return None
    same = collections.defaultdict(list)
    for n in names:
        same[specctra.dsn_net_token(n)].append(n)
    clash = sorted((sorted(ns), tok) for tok, ns in same.items() if len(ns) > 1)
    if clash:
        return ("Freerouting cannot tell these nets apart: KiCad's DSN export writes `\"` as `''`, so "
                + "; ".join(" and ".join(map(repr, ns)) + f" are each written {tok}" for ns, tok in clash)
                + ". Rename one of each, or route with --backend krt")
    d = run_dir / "facts"
    rec = common.run_cmd([pcbnew_python(), KICAD_SCRIPTS / "specctra.py", "check", board, d / "names-check.dsn"],
                         d, d / "names-check.log", timeout=600)
    (d / "names-check.dsn").unlink(missing_ok=True)
    refusal = tagged_json(rec["output"], "BRIDGE_REFUSAL")
    if rec["exit_code"] == 4 and refusal:
        return (f"the Specctra bridge cannot write a net name one way for Freerouting: {refusal['message']}. "
                "Rename the net, or route with --backend krt")
    return None  # anything else the export itself reports when the attempt runs


def freerouting_route(work, board, scope, facts, settings):
    proc = {"status": PASS, "reasons": [], "commands": [], "output_board": None, "exit_code": None}
    rel = freerouting_installed()
    if not rel:
        proc.update(status=FAIL, reasons=["Freerouting is not installed: run `kicad-route update`"])
        return proc
    need = jar_java_major(rel["jar"])
    java = find_java(need)
    if not java:
        proc.update(status=FAIL, reasons=[f"Freerouting {rel['tag']} needs Java {need}+; none installed "
                                          f"(apt install openjdk-{need}-jre-headless)"])
        return proc
    dsn, ses, res = work / "board.dsn", work / "board.ses", work / "freerouting-result.json"
    export = [pcbnew_python(), KICAD_SCRIPTS / "specctra.py", "export", board, dsn]
    if settings["mode"] != "all":
        skip = work / "skip-nets.json"
        skip.write_text(json.dumps(sorted(set(facts["nets"]) - set(scope))))
        export += ["--skip-nets", skip]
    dru = board.with_suffix(".kicad_dru")
    if dru.exists():
        export += ["--dru", dru]
    if settings.get("clearance_margin_mm"):
        export += ["--clearance-margin", f"{settings['clearance_margin_mm'] * 1000:g}"]
    if settings.get("default_clearance_mm"):
        export += ["--default-clearance", f"{settings['default_clearance_mm'] * 1000:g}"]
    only = ["--only", work.parent / settings["protect_only"]] if settings.get("protect_only") else []
    export += only  # --protect human: agent copper is not locked for the round trip
    rec = common.run_cmd(export, work, work / "export-dsn.log", timeout=600)
    proc["commands"].append(common.public(rec))
    bridge = tagged_json(rec["output"], "BRIDGE_JSON") if rec["exit_code"] == 0 else None
    if not bridge or not dsn.exists():
        refusal = tagged_json(rec["output"], "BRIDGE_REFUSAL") or {}  # `run` refuses these before routing
        proc.update(status=FAIL, reasons=[f"DSN export failed (exit {rec['exit_code']}"
                                          + (f": {refusal['message']}" if refusal.get("message") else "")
                                          + f"); see {rec['log']}"])
        return proc
    budget = settings["budget_s"]
    argv = [java[0], "-Djava.awt.headless=true", *common.jvm_home_args(), "-jar", rel["jar"], "-de", dsn, "-do", ses,
            "--gui.enabled=false", "-da", "--api_server.enabled=false",
            f"--user_data_path={work / 'freerouting-userdata'}", f"--router.result_json={res}",
            # no narrowing of traces into pins: deliver the net class width or nothing.
            # The fanout stage necks down to 3/4, 3/5 or 1/2 of the class width
            # regardless of automatic_neckdown (FoundConnectionInserter), so it is off too.
            "--router.automatic_neckdown=false", "--router.fanout.enabled=false",
            f"--router.job_timeout={budget // 3600:02d}:{budget % 3600 // 60:02d}:{budget % 60:02d}"]
    if settings.get("passes"):
        argv.append(f"--router.autorouter.max_passes={settings['passes']}")
    if bridge["ignored_classes"]:
        argv.append("--router.autorouter.ignore_net_classes=" + ",".join(bridge["ignored_classes"]))
    argv += settings["backend_args"]
    rec = run_budgeted(argv, work, work / "freerouting.log", budget + 60,
                       dict(OFFLINE_ENV, **(settings.get("backend_env") or {})), grace=30,
                       monitor=stall.Monitor("freerouting", work / "freerouting.log", settings.get("stall_s")))
    proc["commands"].append(rec)
    proc["exit_code"] = rec.get("exit_code")
    report = load_json(res)
    if report:
        stats = report.get("board_statistics") or {}
        proc["backend_report"] = {
            "final_state": report.get("final_state"), "exit_code": report.get("exit_code"),
            "output_written": report.get("output_written"),
            "incomplete_connections": (stats.get("connections") or {}).get("incomplete_count"),
            "clearance_violations": (stats.get("clearance_violations") or {}).get("total_count")}
    unrouted = re.findall(r"The following connections could not be routed[^\n]*(?:\n[ \t]+[^\n]*)*",
                          Path(work / "freerouting.log").read_text(errors="replace"))
    if unrouted:  # the last report is the final state
        proc.setdefault("backend_report", {})["unrouted_log"] = unrouted[-1][:2000]
    if rec.get("error"):
        proc.update(status=FAIL, reasons=[f"Freerouting could not start: {rec.get('detail')}"])
        return proc
    log_text = Path(work / "freerouting.log").read_text(errors="replace")
    saved = f"Successfully saved output file '{ses}'" in log_text and ses.exists()
    if rec.get("killed"):
        # At --router.job_timeout Freerouting saves its session, but it can keep
        # running afterwards. A session it reported saved before the kill is complete
        # and is graded like any other; without one there is no candidate.
        if not saved:
            proc.update(status=FAIL, reasons=[f"Freerouting {stall.stall_reason(rec)} and was killed"
                                              if rec.get("stalled") else
                                              "Freerouting exceeded the budget and was killed"])
            return proc
        proc["reasons"] = ["Freerouting saved its session at the job timeout but did not exit; "
                           "killed after the grace period (session graded)"]
    elif rec["exit_code"] != 0 or not ses.exists():
        proc.update(status=FAIL, reasons=[f"Freerouting exited {rec['exit_code']} without a session file"])
        return proc
    out = work / "freerouting-out" / board.name
    out.parent.mkdir()
    rec = common.run_cmd([pcbnew_python(), KICAD_SCRIPTS / "specctra.py", "import", board, ses, out, *only], work,
                      work / "import-ses.log", timeout=600)
    proc["commands"].append(common.public(rec))
    if rec["exit_code"] != 0 or not out.exists():
        proc.update(status=FAIL, reasons=[f"SES import failed (exit {rec['exit_code']}); see {rec['log']}"])
        return proc
    proc["bridge"] = {"export": {k: v for k, v in bridge.items() if k != "dsn"},
                      "import": tagged_json(rec["output"], "BRIDGE_JSON")}
    proc["output_board"] = out
    proc["project_writeback"] = []
    return proc


# --------------------------------------------------------- install / update

def github_headers(env=None):
    """Request headers for the GitHub API: a token from GITHUB_TOKEN (else GH_TOKEN),
    when set, lifts the unauthenticated 60 requests/hour limit shared CI hits."""
    env = os.environ if env is None else env
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "kicad-route"}
    token = (env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_latest(repo):
    """The repo's latest release: GitHub's /releases/latest, which is the newest
    non-draft, non-prerelease release, so a stable release always wins."""
    import urllib.request
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest", headers=github_headers())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def krt_venv_python(env=None):
    """The interpreter the KRT venv is built on. KRT is plain CPython (its Rust module
    is abi3, Python >= 3.9) and never imports pcbnew: the venv does not see system
    site-packages. So the Python running kicad-route (>= 3.12) is the default, not a
    Debian path; KICAD_ROUTE_KRT_PYTHON overrides it."""
    env = os.environ if env is None else env
    return env.get("KICAD_ROUTE_KRT_PYTHON") or sys.executable


def update_krt(steps):
    """Install dependencies and build the routing source shipped by kicad-route."""
    def checked(argv, cwd, log_name):
        record = common.run_cmd(argv, cwd, out_path("update") / log_name)
        steps.append(record)
        if record.get("exit_code") != 0:
            raise RuntimeError(f"router installation failed (see {record['log']})")

    common.STATE.mkdir(parents=True, exist_ok=True)
    if not route_python().exists():
        checked(["uv", "venv", KRT_VENV, "--python", krt_venv_python()], common.STATE, "venv.log")
    env_cmd = ["env", f"VIRTUAL_ENV={KRT_VENV}", "uv", "pip", "install", "--upgrade", "-r", KRT_DIR / "requirements.txt"]
    checked(env_cmd, common.STATE, "krt-deps.log")
    checked([route_python(), KRT_DIR / "build_router.py", "--target-dir",
             common.STATE / "build" / "router-native"], common.STATE, "krt-build-router.log")
    checked([route_python(), "py_router/startup_checks.py"], KRT_DIR, "krt-startup-checks.log")
    return engine.identity()["build"]


# The executable jar of a Freerouting release: freerouting-<version>.jar, optionally
# with a pre-release suffix (freerouting-2.5.0-alpha.jar, -rc1, -beta.2, -SNAPSHOT).
# Classifier jars (-sources, -javadoc, ...) are not executable and do not match.
FREEROUTING_JAR = re.compile(
    r"freerouting-(?P<version>\d+(?:\.\d+)*)"
    r"(?:-(?P<pre>(?:alpha|beta|rc|pre|preview|snapshot|ea|dev)(?:[.-]?[0-9A-Za-z]+)*|m\d+))?\.jar", re.I)


def freerouting_jar_asset(assets):
    """The release asset to install: the stable jar when the release has one, else
    its first pre-release jar; None when it has neither."""
    jars = [a for a in assets if FREEROUTING_JAR.fullmatch(a["name"])]
    jars.sort(key=lambda a: FREEROUTING_JAR.fullmatch(a["name"])["pre"] is not None)  # stable first, stable sort
    return jars[0] if jars else None


def check_asset_digest(asset, sha256_hex):
    """Verify a downloaded asset against the "sha256:<hex>" digest GitHub publishes
    for each release asset. True when it matches; False when GitHub gave no digest
    (assets uploaded before GitHub computed them); raises on a mismatch or on a
    digest in an algorithm this cannot check."""
    want = asset.get("digest")
    if not want:
        return False
    algo, _, value = want.partition(":")
    if algo.lower() != "sha256" or not value:
        raise RuntimeError(f"{asset['name']}: unrecognised GitHub digest {want!r}")
    if value.lower() != sha256_hex.lower():
        raise RuntimeError(f"{asset['name']}: sha256 {sha256_hex} does not match GitHub's digest {value}")
    return True


def update_freerouting(steps):
    """Freerouting: the latest stable release's executable jar, verified against
    GitHub's per-asset sha256 digest (recorded as digest_verified in release.json)."""
    import hashlib
    import urllib.request
    rel = github_latest(FREEROUTING_REPO)
    tag = rel["tag_name"]
    asset = freerouting_jar_asset(rel["assets"])
    if asset is None:
        raise RuntimeError(f"release {tag} has no executable jar asset")
    dest_dir = FREEROUTING_DIR / tag
    jar = dest_dir / asset["name"]
    verified = None  # an already-installed jar is not re-downloaded; see its release.json
    if not jar.exists():
        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp = jar.with_suffix(".part")
        with urllib.request.urlopen(asset["browser_download_url"], timeout=300) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        try:
            verified = check_asset_digest(asset, hashlib.sha256(tmp.read_bytes()).hexdigest())
        except RuntimeError:
            tmp.unlink()
            raise
        if not verified:
            log(f"[update] WARNING: GitHub published no digest for {asset['name']}; it is installed unverified")
        tmp.rename(jar)
    else:
        verified = (load_json(dest_dir / "release.json") or {}).get("digest_verified")
    import zipfile
    with zipfile.ZipFile(jar) as z:
        mf = z.read("META-INF/MANIFEST.MF").decode(errors="replace")
    build = re.search(r"Build-Revision:\s*(\S+)", mf)
    common.write_json(dest_dir / "release.json", {
        "tag": tag, "asset": asset["name"], "sha256": common.sha256(jar), "published_at": rel.get("published_at"),
        "build_revision": build.group(1) if build else None, "url": asset["browser_download_url"],
        "digest": asset.get("digest"), "digest_verified": verified})
    cur = FREEROUTING_DIR / "current"
    if cur.is_symlink() or cur.exists():
        cur.unlink()
    cur.symlink_to(tag)
    for old in FREEROUTING_DIR.iterdir():  # keep the current release only
        if old.is_dir() and not old.is_symlink() and old.name != tag:
            shutil.rmtree(old)
    return tag


def update():
    out_path("update").mkdir(parents=True, exist_ok=True)
    before = {"krt": krt_version() if KRT_DIR.exists() else None, "freerouting": freerouting_version()}
    steps, errors = [], []
    for name, fn in (("krt", update_krt), ("freerouting", update_freerouting)):
        try:
            log(f"[update] {name}: {fn(steps)}")
        except Exception as e:  # report and continue with the other backend
            errors.append(f"{name}: {e}")
            log(f"[update] {name}: FAILED: {e}")
    failed = [s for s in steps if s.get("exit_code") != 0]
    for s in failed:
        errors.append(f"command failed: {' '.join(s['argv'])} (exit {s.get('exit_code')}, log {s['log']})")
    after = {"krt": krt_version(), "freerouting": freerouting_version()}
    if after["freerouting"].get("version") and not after["freerouting"].get("java"):
        errors.append(f"Freerouting needs Java {after['freerouting']['java_required']}+: "
                      f"sudo apt install openjdk-{after['freerouting']['java_required']}-jre-headless")
    # STATE/manifest.json: what is installed now, hashed (statemanifest.py, KICADROUTE-3)
    manifest_path, manifest = statemanifest.refresh("update", out_path("update") / "krt-build-router.log")
    record = {"when": common.now_utc().isoformat(), "before": before, "after": after,
              "commands": [common.public(s) for s in steps], "errors": errors,
              "state_manifest": {"path": str(manifest_path), "sha256": statemanifest.file_sha256()}}
    common.write_json(out_path("update") / "update.json", record)
    log(f"[update] state manifest: {manifest_path}")
    for name in ("krt", "freerouting"):
        b, a = (before[name] or {}).get("version"), after[name].get("version")
        log(f"[update] {name}: {b} -> {a}")
    if errors:
        log("[update] errors:\n  " + "\n  ".join(errors))
        return 1
    log(f"[update] {skills.sync(KRT_DIR, KRT_VENV)}")  # krt-* skills follow the installed KRT
    log("[update] re-running the routing integration checks")
    from . import corpus  # real-board gate after smoke; regressions reported in update.json, never rolled back
    rc = corpus.after_update(out_path("update") / "update.json", smoke())
    from . import determinism  # repeat check for the new KRT build, recorded in the state manifest
    det, det_path = determinism.check(sys.modules[__name__], "krt", record=True)
    log(f"[update] KRT determinism: {'reproducible' if det['reproducible'] else 'NOT reproducible'}"
        + (f" ({'; '.join(det['reasons'])})" if det["reasons"] else "") + f" -> {common.rel(det_path)}")
    record = load_json(out_path("update") / "update.json")
    record["determinism"] = {**det, "record_path": str(det_path)}
    record["state_manifest"] = {"path": str(statemanifest.manifest_path()),
                                "sha256": statemanifest.file_sha256()}
    record["exit_code"] = rc or (1 if det.get("record_error") else
                                3 if not det["reproducible"] else 0)
    common.write_json(out_path("update") / "update.json", record)
    return record["exit_code"]


# ------------------------------------------------------------------- doctor

def doctor(offline=False, canary_check=True, require_readonly=False):
    """Readiness checks; returns the kicad-route/doctor/1 record. offline: no
    latest-release lookups (NOT_RUN, not required) and no socket; canary_check=False
    skips the canary; require_readonly makes "state read-only" required."""
    checks = []
    require_readonly = sandbox.require_readonly_state(require_readonly)

    def add(name, status, detail="", required=True):
        checks.append({"check": name, "status": status, "detail": detail, "required": required})

    kv = common.tool_versions()["kicad-cli"]
    add("kicad-cli", PASS if kv else FAIL, kv or "not found")
    r = subprocess.run(["kicad-cli", "pcb", "drc", "--help"], capture_output=True, text=True, env=common.tool_env())
    for flag in ("--refill-zones", "--save-board", "--schematic-parity", "--severity-all", "--format"):
        add(f"kicad-cli pcb drc {flag}", PASS if flag in r.stdout else FAIL, "native grading")
    r = subprocess.run(["kicad-cli", "pcb", "export", "--help"], capture_output=True, text=True, env=common.tool_env())
    add("kicad-cli DSN export", PASS if "dsn" in r.stdout.lower() else UNSUPPORTED,
        "not in kicad-cli: the Freerouting bridge uses pcbnew SWIG instead", required=False)
    r = subprocess.run([str(pcbnew_python()), "-c", "import pcbnew; print(pcbnew.GetBuildVersion()); "
                        "print(callable(pcbnew.ExportSpecctraDSN) and callable(pcbnew.ImportSpecctraSES))"],
                       capture_output=True, text=True, env=common.tool_env())
    lines = r.stdout.split()
    add("pcbnew SWIG (facts, Specctra bridge)", PASS if r.returncode == 0 and lines[-1:] == ["True"] else FAIL,
        " ".join(lines[:1]) or r.stderr[-200:])

    planes_bwrap = planes.bwrap_check(out_path("doctor", "planes-bwrap"))
    checks.append(planes_bwrap)

    # KRT
    v = krt_version() if KRT_DIR.exists() else {}
    add("owned routing source", PASS if v.get("version") else FAIL, f"{v.get('version')} {v.get('build')}")
    add("KRT routing venv", PASS if route_python().exists() else FAIL, str(KRT_VENV))
    add("KRT Rust router module", PASS if v.get("rust_router") and v.get("rust_router") == v.get("version") else FAIL,
        f"grid_router {v.get('rust_router')} (checkout {v.get('version')})")
    if route_python().exists() and KRT_DIR.exists():
        r = subprocess.run([str(route_python()), "py_router/startup_checks.py"], cwd=KRT_DIR,
                           capture_output=True, text=True, env=common.tool_env())
        add("KRT startup checks", PASS if r.returncode == 0 else FAIL, (r.stdout + r.stderr).strip().splitlines()[-1:])
        r = subprocess.run([str(route_python()), "py_router/route.py", "--help"], cwd=KRT_DIR,
                           capture_output=True, text=True, env=common.tool_env())
        missing = [f for f in KRT_REQUIRED_FLAGS if f not in r.stdout]
        add("KRT controls used by the adapter", PASS if not missing else FAIL,
            "all present" if not missing else f"missing: {missing} (upstream changed; update the adapter)")
        add("KRT latest release", NOT_RUN, "owned source; upstream is provenance only", required=False)

    # Freerouting
    fv = freerouting_version()
    add("Freerouting jar", PASS if fv.get("version") else FAIL, f"{fv.get('version')} {fv.get('build')}")
    add("Java for Freerouting", PASS if fv.get("java") else FAIL,
        f"{fv.get('java')} (jar built for Java {fv.get('java_required')})")
    if fv.get("java"):
        rel = freerouting_installed()
        java = fv["java"].split(" (")[0]
        r = subprocess.run([java, "-Djava.awt.headless=true", *common.jvm_home_args(), "-jar", str(rel["jar"]), "--help"],
                           capture_output=True, text=True, env=dict(common.tool_env(), **OFFLINE_ENV), timeout=120)
        out = r.stdout + r.stderr
        add("Freerouting headless CLI", PASS if r.returncode == 0 and "-de" in out and "-do" in out else FAIL,
            re.search(r"Freerouting v\S+[^\n]*", out).group(0) if re.search(r"Freerouting v\S+", out) else out[-200:])
        if offline:
            add("Freerouting latest release", NOT_RUN, "--offline: not looked up", required=False)
        else:
            try:
                latest = github_latest(FREEROUTING_REPO)["tag_name"]
                add("Freerouting latest release", PASS if latest == fv["version"] else NOT_RUN,
                    f"latest {latest}, installed {fv['version']}" +
                    ("" if latest == fv["version"] else ": run `kicad-route update`"), required=False)
            except Exception as e:
                add("Freerouting latest release", NOT_RUN, f"GitHub unreachable: {e}", required=False)

    # STATE/manifest.json: read offline, hashes recomputed on disk; drift is a required FAIL
    for name, status, detail, required in statemanifest.doctor_checks():
        add(name, status, detail, required=required)

    if canary_check:
        from . import canary
        shutil.rmtree(out_path("doctor", "canary"), ignore_errors=True)  # a second doctor in one OUT starts fresh
        can = canary.run_canary(quick=True, dest=out_path("doctor", "canary"))
        for c in can["checks"]:
            add(f"canary {c['check']} (awkward net names)", c["status"],
                "; ".join(c.get("problems", [])[:3]) or "Specctra class split and DSN export/parse round-trip")
    else:
        add("canary (awkward net names)", NOT_RUN, "--no-canary", required=False)

    state_refusal = sandbox.state_refusal("doctor")  # what run --require-readonly-state would decide
    add("state read-only", PASS if not state_refusal else (FAIL if require_readonly else NOT_RUN),
        state_refusal or f"{common.STATE} is read-only to this process and matches its manifest",
        required=require_readonly)

    capabilities = {
        "planes": {"interface": "native pcbnew, agent-ee.plane-plan/1",
                   "bwrap_ready": planes_bwrap["status"] == PASS},
        "krt": {"interface": "CLI (py_router/route.py), headless, no KiCad editor needed",
                "net_selection": "names (fnmatch, !exclude); classes expanded by the wrapper",
                "budget": "wall clock via KICAD_STOP_FILE + kill; --max-iterations",
                "relaxation_controls": "--escalation off --fab-tier standard --strict-sizes --no-power-tap-neckdown "
                                       "plus KICAD_* knobs (see KRT_FLAGS/KRT_ENV in kicad_route/cli.py)",
                "project_writeback": "disabled (--no-fix-drc-settings, KICAD_INRUN_FLOOR_SYNC=0); "
                                     "originals restored anyway",
                "existing_copper": "locked copper never touched; with --protect existing the wrapper locks all "
                                           "input copper for the run and restores the flags afterwards"},
        "freerouting": {"interface": "executable jar, headless (-de/-do), via KiCad pcbnew Specctra bridge",
                        "net_selection": "by DSN class: out-of-scope nets moved to ignored copies of their class",
                        "budget": "--router.job_timeout + kill; --router.autorouter.max_passes",
                        "relaxation_controls": "--router.automatic_neckdown=false",
                        "project_writeback": "none (Specctra session only)",
                        "existing_copper": "all pre-existing copper frozen; SES import keeps locked copper and "
                                           "re-creates unlocked copper from the session"},
    }
    required_fail = [c for c in checks if c["required"] and c["status"] != PASS]
    status = FAIL if required_fail else PASS
    out = out_path("doctor", "doctor.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    pcbnew_module = None
    if lines[:1]:
        r = subprocess.run([str(pcbnew_python()), "-c", "import pcbnew; print(pcbnew.__file__)"],
                           capture_output=True, text=True, env=common.tool_env())
        pcbnew_module = r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else None
    rec = {"schema": contract.schema_id("doctor"), "kind": "doctor", "status": status, "checks": checks,
           "offline": offline, "canary": canary_check,
           "required_failed": [c["check"] for c in required_fail],
           "capabilities": capabilities, "versions": {"krt": v, "freerouting": fv},
           "state_manifest": {"path": str(statemanifest.manifest_path()), "sha256": statemanifest.file_sha256()},
           "state": sandbox.state_status(),
           "sandbox": sandbox.requirements(venv_python=route_python(), pcbnew_python=pcbnew_python(),
                                           pcbnew_module=pcbnew_module,
                                           java=fv["java"].split(" (")[0] if fv.get("java") else None,
                                           offline=offline, flag=require_readonly),
           "exit_code": 0 if status == PASS else 1, "record": common.rel(out)}
    common.write_json(out, rec)
    for c in checks:
        log(f"{c['status']:<12} {c['check']:<44} {c['detail'] if isinstance(c['detail'], str) else ' '.join(c['detail'])}")
    log(f"\nroute doctor: {status} ({len(checks)} checks) -> {common.rel(out)}")
    return rec


# ------------------------------------------------------------------- compare

def compare(run_dirs):
    rows, fps = [], set()
    for d in run_dirs:
        r = load_json(Path(d) / "result.json")
        if r is None:
            contract.refuse("input", f"{d}: no result.json")
        fps.add(r["input"]["fingerprint"])
        st = r["routing"].get("stats", {})
        rows.append({"run": r["run_dir"], "fp": ledger.from_result(r)[0][:12],
                     "backend": f"{r['backend']['name']} {r['backend'].get('version')}",
                     "exit": r["exit_code"], "process": r["process"]["status"], "routing": r["routing"]["status"],
                     "unconnected": r["routing"].get("unconnected_in_scope"),
                     "validation": r["validation"]["status"],
                     "new_violations": (r["validation"].get("drc", {}).get("new_violations") or {}).get("count"),
                     "new_parity": (r["validation"].get("drc", {}).get("new_parity") or {}).get("count"),
                     "preservation_fail": [k for k, v in (r["validation"].get("preservation") or {}).items() if v != PASS],
                     "new_vias": st.get("new_vias"), "new_length_mm": st.get("new_track_length_mm"),
                     "backend_s": r["process"].get("runtime_s"), "scope": r["scope"]["mode"],
                     "args": r["settings"].get("backend_args")})
    same = len(fps) == 1
    out = {"same_input": same, "input_fingerprints": sorted(fps), "candidates": rows}
    dest = out_path("compare", common.now_utc().strftime("%Y%m%dT%H%M%S.%fZ"))
    dest.mkdir(parents=True)
    common.write_json(dest / "compare.json", out)
    hdr = ["fp", "backend", "exit", "routing", "unconnected", "validation", "new_violations", "new_vias",
           "new_length_mm", "backend_s"]
    log("| " + " | ".join(hdr) + " | run |")
    log("|" + "---|" * (len(hdr) + 1))
    for row in rows:
        log("| " + " | ".join(str(row[h]) for h in hdr) + f" | {row['run']} |")
    if not same:
        log("WARNING: these candidates come from different inputs; the comparison is not like for like")
    log(f"[compare] {common.rel(dest)}/compare.json")
    return 0 if same else 1


# --------------------------------------------------------------------- smoke

# What `publish` copies: kicad-route's structured receipts (JSON, plus the RESULT.md
# summary) and never a board, project file, log or the backend's work/ tree.
# The run root and validate/ are published flat (the long-standing layout:
# DEST/result.json, DEST/preservation.json, ...); facts/ and spokes/ keep their
# directory; the latest revalidate*/<stamp> is published as a subdirectory.
PUBLISH_FLAT = ("", "validate")
PUBLISH_NESTED = ("facts", "spokes")
PUBLISH_LATEST = {"revalidate-origin": "against-origin", "revalidate": "revalidate"}


def receipts(d):
    """The structured receipts directly in d: JSON files and RESULT.md, nothing else."""
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir()
                  if p.is_file() and not p.is_symlink() and (p.suffix == ".json" or p.name == "RESULT.md"))


def publish_run(run_dir, dest):
    from . import publication
    publication.publish_run(run_dir, dest)


def smoke(publish=None):
    """The integration tests on the bundled fixture, run under OUT/smoke. With
    publish, the structured evidence (not the boards) is copied there for committing."""
    from . import publication
    root = out_path("smoke")
    protected = (common.out_dir().resolve(), FIXTURE.resolve())
    dest = publication.destination(publish, 'smoke', protected) if publish else None
    if any(p.is_symlink() for p in (root, *root.parents)):
        contract.refuse("input", f"smoke workspace has a symlink: {root}")
    root.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix=common.now_utc().strftime("%Y%m%dT%H%M%S-"), dir=root))
    junit = out / "junit.xml"
    env = dict(common.tool_env(), KICAD_ROUTE_TEST_OUT=str(out), KICAD_ROUTE_STATE=str(common.STATE))
    r = subprocess.run([str(pcbnew_python()), "-m", "pytest", "-v", "-p", "no:cacheprovider",
                        f"--junitxml={junit}", str(TOOL_ROOT / "tests")],
                       cwd=out, env=env)
    status = PASS if r.returncode == 0 else FAIL
    records = {p.stem: load_json(p) for p in sorted(out.glob("*.json")) if p.name != "summary.json"}
    summary = {"kind": "route-smoke", "status": status, "pytest_exit_code": r.returncode,
               "time_utc": common.now_utc().isoformat(),
               "versions": {"krt": krt_version(), "freerouting": freerouting_version(),
                            **common.tool_versions()},
               "junit": junit.name, "tests": sorted(records)}
    common.write_json(out / "summary.json", summary)
    if dest and status == PASS:
        with publication.staging(dest, 'smoke', protected) as stage:
            for p in [*out.glob("*.json"), junit]:
                if p.is_file() and not p.is_symlink():
                    (stage / p.name).write_bytes(p.read_bytes())
            for rec in records.values():
                run_dir = (rec or {}).get("run_dir")
                if run_dir:
                    target = stage / "runs" / Path(run_dir).parent.name
                    target.mkdir(parents=True, exist_ok=True)
                    publication.copy_run(out / run_dir, target)
            publication.seal(stage, 'smoke')
    log(f"kicad-route smoke: {status} -> {common.rel(Path(publish) if publish else out)}")
    return 0 if status == PASS else 1


# --------------------------------------------------------------------- audit

RULE_KEYS = ("rules", "rule_severities", "drc_exclusions")


def project_rule_changes(old_dir, new_dir, board_name):
    """Rule-relevant differences between two project snapshots: DRC constraints,
    severities, exclusions, net classes and the custom rule file. `kind` is
    "changed" when a value differs, "added"/"removed" when a key exists on one side
    only (KiCad writes its defaults out when it upgrades or re-saves a project, so
    those are usually not edits; grading under the parent's rules shows if one is)."""
    def pro(d):
        return load_json(d / Path(board_name).with_suffix(".kicad_pro").name) or {}

    a, b = pro(old_dir), pro(new_dir)
    ds_a, ds_b = a.get("board", {}).get("design_settings", {}), b.get("board", {}).get("design_settings", {})
    changes = []

    def change(setting, old, new, present_old=True, present_new=True):
        kind = "changed" if present_old and present_new else ("added" if present_new else "removed")
        changes.append({"setting": setting, "kind": kind, "old": old, "new": new})

    for key in RULE_KEYS:
        va, vb = ds_a.get(key), ds_b.get(key)
        if isinstance(va, dict) and isinstance(vb, dict):
            for d in dict_diff(va, vb, limit=500):
                change(f"{key}.{d['key']}", d["reference"], d["candidate"], d["key"] in va, d["key"] in vb)
        elif va != vb:
            change(key, va, vb, key in ds_a, key in ds_b)
    na, nb = a.get("net_settings", {}), b.get("net_settings", {})
    ca = {c.get("name"): c for c in na.get("classes") or []}
    cb = {c.get("name"): c for c in nb.get("classes") or []}
    for name in sorted(set(ca) | set(cb), key=str):
        if name not in ca or name not in cb:
            change(f"net_settings.classes.{name}", ca.get(name), cb.get(name), name in ca, name in cb)
            continue
        for d in dict_diff(ca[name], cb[name], limit=100):
            change(f"net_settings.classes.{name}.{d['key']}", d["reference"], d["candidate"],
                   d["key"] in ca[name], d["key"] in cb[name])
    for key in ("netclass_patterns", "netclass_assignments"):
        if (na.get(key) or None) != (nb.get(key) or None):  # null and empty mean the same
            change(f"net_settings.{key}", na.get(key), nb.get(key), bool(na.get(key)), bool(nb.get(key)))
    dru = Path(board_name).with_suffix(".kicad_dru").name
    ta = (old_dir / dru).read_text() if (old_dir / dru).exists() else None
    tb = (new_dir / dru).read_text() if (new_dir / dru).exists() else None
    if ta != tb:
        change(dru, ta, tb, ta is not None, tb is not None)
    return changes


def audit(args):
    """Grade a committed board change (--new) against its parent (--old) with native
    KiCad checks: DRC and parity of both, findings new in --new, --new graded under
    the parent's rules (what a rule change hides), and the preservation diff.
    Design changes in the diff are reported, not failed: a commit may move parts."""
    common.ensure_kicad_config()
    base = resolve_input(argparse.Namespace(**{**vars(args), "rev": None}))
    board = base["pcb"]
    root = out_path("audit", base["name"], f"{args.old}..{args.new}".replace("/", "_"))
    shutil.rmtree(root, ignore_errors=True)
    side = {}
    for label, rev in (("old", args.old), ("new", args.new)):
        d = root / label
        d.mkdir(parents=True)
        ref, manifest = snapshot_input(dict(base, rev=rev), d)
        refusal = rules_files(ref, board)["refusal"]
        if refusal:  # KiCad would grade this side with its defaults, hiding every rule change
            shutil.rmtree(root, ignore_errors=True)
            contract.refuse("input", f"{label} = {rev}: {refusal}")
        work = d / "graded" / ref.name
        copy_project(ref, work)
        log(f"[audit] {label} = {rev} ({manifest.get('source_git_head')}): native DRC (refilled)")
        rep, info = kicad_drc(work / board, d / "drc", label, refill_and_save=True)
        facts, rec = board_facts(ref / board, d / "facts.json")
        if rep is None or facts is None:
            why = '; '.join(info['reasons']) or rec['log']
            log(f"[audit] {label}: could not grade ({why})")
            return {"schema": contract.schema_id("audit"), "kind": "audit", "project": base["name"], "board": board,
                    "error": f"{label} ({rev}) could not be graded: {why}", "dir": common.rel(root), "exit_code": 1}
        side[label] = {"rev": rev, "commit": manifest.get("source_git_head"), "ref": ref, "rep": rep,
                       "facts": facts, "parity_performed": info["parity_performed"]}
    old, new = side["old"], side["new"]
    rules = project_rule_changes(old["ref"], new["ref"], board)
    under = None
    if rules:  # grade the new board with the parent's project rule files
        d = root / "new-under-old-rules" / new["ref"].name
        copy_project(new["ref"], d)
        for f in old["ref"].iterdir():
            if f.suffix in (".kicad_pro", ".kicad_dru"):
                shutil.copy2(f, d / f.name)
        log("[audit] rule settings changed: grading the new board under the parent's rules")
        under, _ = kicad_drc(d / board, root / "new-under-old-rules" / "drc", "new-old-rules", refill_and_save=True)
    fl = floor.audit_floor(base["path"], board, old["commit"], new["commit"], new["ref"], root / "new-under-floor")
    under_floor = None
    if fl["project_dir"]:  # some floor value is stricter than the new revision's
        log("[audit] rule floor stricter than the new revision: grading the new board under the floor")
        under_floor, _ = kicad_drc(fl["project_dir"] / board, root / "new-under-floor" / "drc", "new-floor",
                                   refill_and_save=True)

    def totals(rep):
        return {"unconnected": len(rep["unconnected_items"]), "violations": len(rep["violations"]),
                "errors": sum(1 for v in rep["violations"] if v.get("severity") == "error"),
                "parity": len(rep["schematic_parity"]), "by_type": common.count_by(rep["violations"], "type")}

    new_v, fixed_v = drc_delta(old["rep"], new["rep"], "violations")
    hidden = drc_delta(new["rep"], under, "violations")[0] if under else []
    checks, stats = preservation(old["facts"], new["facts"], old["ref"], new["ref"], board, "existing",
                                 set(old["facts"]["nets"]))
    result = {
        "schema": contract.schema_id("audit"), "kind": "audit", "project": base["name"], "board": board,
        "old": {"rev": old["rev"], "commit": old["commit"], **totals(old["rep"])},
        "new": {"rev": new["rev"], "commit": new["commit"], **totals(new["rep"])},
        "new_under_old_rules": totals(under) if under else None,
        "rule_changes": rules,
        "hidden_by_rule_changes": {"count": len(hidden), "by_type": common.count_by(hidden, "type"),
                                   "items": hidden[:200]},
        "new_violations": {"count": len(new_v), "by_type": common.count_by(new_v, "type")},
        "resolved_violations": len(fixed_v),
        "changes": [{k: c[k] for k in ("check", "status", "detail")} | {"differences": c["differences"][:20]}
                    for c in checks],
        "copper": stats,
        "dir": common.rel(root),
    }
    result["floor"], floor_findings, floor_notes = floor.audit_report(
        fl, drc_delta(new["rep"], under_floor, "violations")[0] if under_floor else [])
    findings = []
    edited = [r for r in rules if r["kind"] != "added" or r["setting"].endswith(".kicad_dru")]
    if edited:
        findings.append(f"{len(edited)} rule setting(s) changed: "
                        + ", ".join(f"{r['setting']} {r['old']!r} -> {r['new']!r}" if r["kind"] == "changed"
                                    else f"{r['setting']} {r['kind']}" for r in edited[:6]))
    if hidden:
        findings.append(f"{len(hidden)} finding(s) appear only under the parent's rules: "
                        + ", ".join(f"{k} {n}" for k, n in common.count_by(hidden, "type").items()))
    if result["new"]["unconnected"] or result["new"]["errors"] or result["new"]["parity"]:
        findings.append(f"new board: {result['new']['unconnected']} unconnected, {result['new']['errors']} DRC "
                        f"errors, {result['new']['parity']} parity issues")
    findings += floor_findings
    result["findings"] = findings
    result["exit_code"] = 3 if findings else 0
    common.write_json(root / "audit.json", result)
    for label in ("old", "new"):
        t = result[label]
        log(f"[audit] {label} {t['rev']}: {t['unconnected']} unconnected, {t['errors']} errors, "
            f"{t['violations']} violations, {t['parity']} parity")
    for c in checks:
        if c["status"] != PASS:
            log(f"[audit] changed: {c['check']}: {c['detail']}")
    for n in floor_notes:
        log(f"[audit] NOTE: {n}")
    for f in findings:
        log(f"[audit] FINDING: {f}")
    log(f"[audit] {common.rel(root)}/audit.json  (exit {result['exit_code']})")
    return result


# ------------------------------------------------------------------------ CLI

BACKENDS = {
    "krt": {"route": krt_route, "version": krt_version, "unsupported": krt_unsupported,
            "preflight": krt_preflight},
    "freerouting": {"route": freerouting_route, "version": freerouting_version,
                    "unsupported": freerouting_unsupported, "preflight": freerouting_preflight},
}


def cmd_validate(args):
    """`kicad-route validate`: the exit code (validate_record has the record)."""
    return validate_record(args)["exit_code"]


def validate_record(args):
    """Re-grade a run into revalidate*/<stamp>/ and return the route-validate record."""
    run_dir = Path(args.run_dir).resolve()
    origin = Path(args.against).resolve() if args.against else None
    out = run_dir / ("revalidate-origin" if origin else "revalidate") / common.now_utc().strftime("%Y%m%dT%H%M%S.%fZ")
    validation, routing = validate(run_dir, out, origin, {"pour_area_loss_pct": args.pour_area_loss,
                                                          "pour_min_spokes": args.pour_min_spokes})
    result = load_json(run_dir / "result.json") or {}
    proc = {k: result.get("process", {}).get(k) for k in ("status", "reasons")} if result.get("process") else \
        {"status": PASS, "reasons": []}
    if validation["status"] == NOT_RUN and proc["status"] == PASS:  # nothing to grade is a process failure
        proc = {"status": FAIL, "reasons": validation["reasons"]}
    build = result.get("backend") or {}
    proc["measurement_qualification"] = statemanifest.attempt_qualification(
        {"reproducible": build.get("reproducible"), "measurement": result.get("measurement", False),
         "basis": build.get("reproducible_basis", "historical attempt has no repeat evidence")},
        result.get("process") or {})
    graded = origin or run_dir  # whose input the candidate was graded against
    graded_attempt = load_json(graded / "attempt.json")
    rec = {"schema": contract.schema_id("route-validate"), "kind": "route-validate",
           "run_dir": common.rel(run_dir), "against": common.rel(origin) if origin else None,
           "dir": common.rel(out), "process": proc, "routing": routing, "validation": validation,
           "evidence": evidence.block(graded, graded_attempt, out, validation,
                                      (result.get("evidence") or {}).get("backend_output_board_sha256"),
                                      records_base=out),
           # the grading tools now; the backend that produced the candidate as its run recorded it
           "tool_identity": {**evidence.tool_identity(None, {}, (load_json(out / "candidate-facts.json") or {})
                                                       .get("kicad_version")),
                             "backend": (result.get("tool_identity") or {}).get("backend")},
           "exit_code": overall_exit(proc, routing, validation)}
    pairs = load_json(out / "pairs.json")
    if pairs is None and "pairs" in graded_attempt:
        pairs = diff_pairs.grade(graded_attempt["pairs"], graded_attempt.get("pair_routing", {}), None, None)
    if pairs is not None:
        rec["schema"] = contract.schema_id("route-validate", 2)
        rec["pairs"] = pairs
    common.write_json(out / "result.json", rec)
    log(f"[validate] routing: {routing['status']}  validation: {validation['status']}"
        + (f" ({'; '.join(validation['reasons'][:4])})" if validation.get("reasons") else ""))
    log(f"[validate] {common.rel(out)}/result.json  (exit {rec['exit_code']})")
    return rec


def blocked_board(board_path, top=10, as_json=False):
    """`kicad-route blocked --board B`: receipts for a placed, unrouted or partly routed board,
    with no backend run. The board and its same-stem .kicad_pro/.kicad_dru (and library
    tables) are copied; the copy is refilled and graded by KiCad's DRC, and every unconnected
    item is analysed. The source files are never written; a source that changes while it is
    analysed fails the command."""
    board = Path(board_path).expanduser().resolve()
    if not board.is_file() or board.suffix != ".kicad_pcb":
        contract.refuse("input", f"{board}: not a .kicad_pcb file")
    dest = out_path("blocked", board.stem, common.now_utc().strftime("%Y%m%dT%H%M%S.%fZ"))
    work = dest / "board"
    work.mkdir(parents=True)
    for f in board.parent.iterdir():
        if f.is_file() and (f == board or f.name.endswith("-lib-table")
                            or (f.stem == board.stem and f.suffix in (".kicad_pro", ".kicad_dru"))):
            shutil.copy2(f, work / f.name)
    say = log   # in --json mode main() sends every progress line to stderr

    def failed(why):
        say(f"[blocked] {why}")
        return 1, {"schema": contract.schema_id("route-blocked"), "kind": "route-blocked", "board": str(board),
                   "error": why, "dir": common.rel(dest), "exit_code": 1}
    input_sha = common.sha256(board)
    t0 = time.monotonic()
    drc, info = kicad_drc(work / board.name, dest, "board", refill_and_save=True)
    if drc is None:
        return failed(f"DRC did not run: {'; '.join(info['reasons'])} (logs in {common.rel(dest)})")
    facts, rec = board_facts(work / board.name, dest / "board-facts.json")
    if facts is None:
        return failed(f"board facts could not be extracted ({rec.get('log')})")
    if common.sha256(board) != input_sha:
        return failed(f"{board} changed while it was analysed: no receipts")
    boards = {"input_sha256": input_sha, "candidate_sha256": None, "graded_sha256": info.get("board_sha256_checked")}
    report = blocked.analyze(drc, facts, facts, None, top, boards=boards, source="board")
    report["board"] = str(board)
    report["runtime_s"] = round(time.monotonic() - t0, 2)
    path = dest / "blocked-by.json"
    path.write_text(json.dumps(report, indent=1) + "\n")
    if as_json:   # the record --json prints is the report itself (schema kicad-route/blocked/2)
        return 0, report
    log(f"[blocked] {report['connections_analyzed']} unconnected items on {board.name}"
        f" ({len(report['footprints'])} footprints involved, {report['runtime_s']} s)")
    if report["connections_analyzed"]:
        log("\n".join(blocked.render_md(blocked.summary(report, common.rel(path)))).rstrip())
    log(f"[blocked] {common.rel(path)}")
    return 0, report


def cmd_blocked(args):
    """(exit code, record): with --board the blocked-by report (kicad-route/blocked/2),
    for a run a route-blocked record carrying its summary."""
    if args.board:
        if args.run_dir:
            contract.refuse("usage", "blocked: pass a run directory or --board, not both")
        return blocked_board(args.board, args.top, args.json)
    if not args.run_dir:
        contract.refuse("usage", "blocked: pass a run directory or --board BOARD.kicad_pcb")
    s = blocked.recompute(args.run_dir, args.top, rel=common.rel, render=render_result)
    rc = 0
    if s is None:
        log("[blocked] no unconnected items in scope: nothing is blocked")
    elif s.get("error"):
        log(f"[blocked] diagnosis failed: {s['error']}")
        rc = 1
    else:
        log("\n".join(blocked.render_md(s)).rstrip())
    return rc, {"schema": contract.schema_id("route-blocked"), "kind": "route-blocked",
                "path": common.rel(Path(args.run_dir)), "blocked_by": s, "exit_code": rc}


def add_pour_args(p):
    p.add_argument("--pour-area-loss", type=float, metavar="PCT",
                   help="pour_integrity: fail when a zone net/layer loses more than PCT %% of its fill area "
                        "(default: area loss is reported, not failed)")
    p.add_argument("--pour-min-spokes", type=int, metavar="N",
                   help="pour_integrity: raise (never lower) the project's min_resolved_spokes for pads whose "
                        "spokes the candidate reduced")


def add_input_args(p, rev=True):
    p.add_argument("--dir", default=".", help="KiCad project directory (default: the current directory)")
    p.add_argument("--board", help="board file name inside --dir (default: its only .kicad_pcb)")
    p.add_argument("--schematic", help="root schematic inside --dir (default: the board's name, .kicad_sch)")
    p.add_argument("--name", help="project name for run directories (default: the directory name)")
    p.add_argument("--library-dir", action="append", metavar="NAME=PATH",
                   help="map an empty project subdirectory (e.g. an uninitialised library submodule) to PATH")
    if rev:
        p.add_argument("--rev", help="route a committed git revision of --dir instead of its working tree")


SCOPE_DESTS = ("nets", "netclass")  # a campaign chooses each step's scope itself


def add_route_args(p):
    """The routing options of one attempt. `run`, `campaign` (passed to every step,
    route_argv) and `corpus add` (recorded in the entry, replayed by `check`) all
    take exactly this set, so a new option reaches all three by being added here."""
    p.add_argument("--nets", nargs="+", metavar="PATTERN",
                   help="net-NAME selection: fnmatch patterns, !PATTERN excludes, a leading \\! is a literal '!' "
                        "(\\!RESET routes the net !RESET) (default: all nets)")
    p.add_argument("--netclass", nargs="+", metavar="CLASS",
                   help="net-CLASS selection: nets whose effective KiCad net class is CLASS")
    p.add_argument("--budget", type=int, default=300, help="wall-clock seconds for the backend (default 300)")
    p.add_argument("--diff-pairs", choices=["auto"], help="KRT: detect native differential pairs, route pairs first, "
                   "and exclude all pair members from single-ended routing (default off)")
    p.add_argument("--max-iterations", type=int, help="KRT A* iteration cap per search")
    p.add_argument("--passes", type=int, help="Freerouting maximum routing passes")
    p.add_argument("--default-clearance", type=float, metavar="MM",
                   help="route the Default net class with at least MM clearance (tighten only; same meaning "
                        "for both backends). Use when the input has custom rules a backend cannot read")
    p.add_argument("--clearance-margin", type=float, metavar="MM",
                   help="Freerouting only: add MM to every clearance it plans with (tighten only)")
    p.add_argument("--protect", choices=["existing", "locked", "human"], default="existing",
                   help="existing (default): all input copper must survive; locked: only locked copper, "
                        "the backend may rip up unlocked copper; human: on a kicad-route candidate, only "
                        "the origin run's copper (see docs/provenance.md), else like existing")
    p.add_argument("--stall", type=int, metavar="SECONDS",
                   help="stop the backend when its log shows no progress for SECONDS (default off)")
    p.add_argument("--force", action="store_true",
                   help="run even if the ledger shows this exact attempt already failed (recorded as forced)")
    p.add_argument("--canary", action="store_true",
                   help="first run the awkward-net-name canary for this backend; refuse (exit 4) if it fails")
    add_pour_args(p)
    p.add_argument("--backend-arg", action="append", metavar="ARG",
                   help="extra argument passed to the backend verbatim (recorded; grading is unaffected)")
    p.add_argument("--backend-env", action="append", metavar="KEY=VALUE",
                   help="extra environment variable for the backend, e.g. a KRT KICAD_* knob (recorded)")
    p.add_argument("--floor-history", action="store_true",
                   help="also compare the input's rules with their git high-water mark (notes only)")
    spokes.add_run_option(p)


ENVIRONMENT_DESTS = ("require_readonly_state", "ledger")


def add_environment_args(p):
    """The environment of a run, not its attempt (docs/sandbox.md). `run` and `campaign`
    take them (campaign forwards both to every step, route_argv); `corpus add` and
    `corpus check` take them for their own runs and never record them in an entry."""
    p.add_argument("--require-readonly-state", action="store_true",
                   help="refuse (exit 4) unless the state directory is present, read-only and the one its "
                        f"manifest describes (also {sandbox.ENV_FLAG}=1)")
    p.add_argument("--ledger", metavar="PATH", type=lambda s: str(Path(s).expanduser().resolve()),
                   help="run ledger file (default OUT/ledger.jsonl); a persistent path keeps exit-4 repeat "
                        "refusal across fresh OUT directories")


def _route_actions(adder=add_route_args):
    probe = argparse.ArgumentParser(add_help=False)
    adder(probe)
    return [a for a in probe._actions if a.option_strings]


def route_option_dests():
    """Namespace names of every add_route_args option (the attempt; not the environment)."""
    return [a.dest for a in _route_actions()]


def route_argv(args, skip=SCOPE_DESTS):
    """The `run` command line that reproduces args' routing and environment options
    (campaign steps). Derived from add_route_args and add_environment_args, so every
    one of them is forwarded."""
    argv = []
    for act in [*_route_actions(), *_route_actions(add_environment_args)]:
        if act.dest in skip:
            continue
        v = getattr(args, act.dest, None)
        flag = act.option_strings[0]
        if v is None or v is False or v == []:
            continue
        if isinstance(act, argparse._StoreTrueAction):
            argv.append(flag)
        elif isinstance(act, argparse._AppendAction):
            argv += [f"{flag}={x}" for x in v]       # =: values may start with '-'
        elif act.nargs == "?":
            argv += [flag] if v == act.const else [flag, str(v)]
        elif act.nargs in ("+", "*"):
            argv += [flag, *map(str, v)]
        else:
            argv += [flag, str(v)]
    return argv


JSON_COMMANDS = ("run", "validate", "campaign", "blocked", "audit", "floor", "doctor", "decoupling", "barrier", "planes")


def build_parser():
    ap = contract.Parser(prog="kicad-route", description=__doc__, root=True,
                         formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)  # subparsers are contract.Parser too
    dp = sub.add_parser("doctor", help="routing readiness and capabilities")
    dp.add_argument("--offline", action="store_true",
                    help="skip the latest-release lookups (NOT_RUN, not required); open no network socket")
    dp.add_argument("--no-canary", dest="canary", action="store_false", help="skip the awkward-net-name canary")
    dp.add_argument("--require-readonly-state", action="store_true",
                    help=f"make the 'state read-only' check required (also {sandbox.ENV_FLAG}=1)")
    rp = sub.add_parser("run", help="route an isolated copy and grade the candidate")
    rp.add_argument("--backend", required=True, choices=sorted(BACKENDS))
    add_input_args(rp)
    add_route_args(rp)
    add_environment_args(rp)
    barrier.add_run_option(rp)   # run only: a barrier is planned from one earlier run of this input
    rp.add_argument("--label", help="run directory name under OUT/runs/<project>/")
    rp.add_argument("--run-dir", metavar="PATH",
                    help="use PATH as the run directory instead of OUT/runs/<project>/<label>/<stamp>/ "
                         "(must not exist; no `latest` link is made)")
    vp = sub.add_parser("validate", help="re-grade a run's candidate against its reference")
    vp.add_argument("run_dir")
    vp.add_argument("--against", metavar="ORIGIN_RUN",
                    help="grade against an earlier run's input (the start of a --dir chain) instead of this run's")
    bp = sub.add_parser("blocked", help="(re)compute the blocked-by receipts of an incomplete run, or of a placed board")
    bp.add_argument("run_dir", nargs="?",
                    help="a run directory, or one of its validate/ or revalidate*/<stamp>/ directories")
    bp.add_argument("--board", metavar="BOARD.kicad_pcb",
                    help="receipts for a placed (unrouted or partly routed) board, without a routing run")
    bp.add_argument("--top", type=int, default=10, help="blockers listed in the summary (default 10)")
    add_pour_args(vp)
    cp = sub.add_parser("compare", help="compare candidates routed from the same input")
    cp.add_argument("run_dirs", nargs="+")
    au = sub.add_parser("audit", help="grade a committed board change against its parent revision")
    add_input_args(au, rev=False)
    au.add_argument("--old", required=True, help="parent revision (e.g. HEAD~1)")
    au.add_argument("--new", default="HEAD", help="revision to audit (default HEAD)")
    fp = sub.add_parser("floor", help="rule high-water mark of the board's history and its declared floor")
    add_input_args(fp, rev=False)
    fp.add_argument("--since", metavar="REV", help="start the history at REV (its state counts)")
    fp.add_argument("--write-declared", action="store_true",
                    help="write a draft <board>.rules-floor.json (the high-water mark) under OUT for review")
    fp.add_argument("--netclass-sizes", action="store_true",
                    help="also ratchet net-class track/via/diff-pair sizes (router defaults, not DRC minimums)")
    pp = sub.add_parser("publish", help="copy a run's structured evidence (not the board) for committing")
    pp.add_argument("run_dir")
    pp.add_argument("--to", default="reports/routing", help="destination root (default reports/routing); "
                                                            "evidence goes to <to>/<project>/<label>/")
    spokes.add_parser(sub)
    decoupling.add_parser(sub)
    planes.add_parser(sub)
    barrier.add_parser(sub)
    sub.add_parser("update", help="build the owned engine, acquire Freerouting, then smoke")
    statemanifest.add_parser(sub)
    from . import determinism
    determinism.add_parser(sub)
    cn = sub.add_parser("canary", help="awkward-net-name round trip through each backend on the fixture")
    cn.add_argument("--backend", choices=["all", *sorted(BACKENDS)], default="all")
    from . import corpus
    corpus.add_parser(sub, sys.modules[__name__])
    sm = sub.add_parser("smoke", help="routing integration tests on the bundled fixture")
    sm.add_argument("--publish", metavar="DIR", help="also copy the structured evidence to DIR")
    from . import campaign
    campaign.add_parser(sub, sys.modules[__name__])
    return ap


def run_namespace(backend, **values):
    """A `run` Namespace with every option at its CLI default, then `values`: for
    callers that drive run_attempt directly (corpus), so new run options never break them."""
    a = build_parser().parse_args(["run", "--backend", backend])
    for k, v in values.items():
        if not hasattr(a, k):
            raise TypeError(f"run has no option {k!r}")
        setattr(a, k, v)
    a.backend_arg = a.backend_arg or []   # as dispatch() normalises them for a CLI run
    a.backend_env = a.backend_env or []
    return a


def dispatch(a):
    """Run the parsed command: (exit code, its schema-carrying record or None)."""
    from . import campaign, corpus  # imported lazily, as build_parser does
    if getattr(a, "backend_env", None) and any("=" not in e for e in a.backend_env):
        contract.refuse("usage", "--backend-env takes KEY=VALUE")
    if getattr(a, "library_dir", None) and any("=" not in e for e in a.library_dir):
        contract.refuse("usage", "--library-dir takes NAME=PATH")
    if a.json and a.cmd not in JSON_COMMANDS:
        contract.refuse("usage", f"--json is not supported by `{a.cmd}` (it is by: {', '.join(JSON_COMMANDS)})")
    if a.cmd == "update" or (a.cmd == "corpus" and a.corpus_cmd == "add"):
        cmd = "update" if a.cmd == "update" else "corpus add"
        refusal = sandbox.writer_refusal(cmd, getattr(a, "require_readonly_state", False))
        if refusal:  # these two write the state directory
            contract.refuse("state", refusal)
    if a.cmd == "campaign" and sandbox.require_readonly_state(a.require_readonly_state):
        refusal = sandbox.state_refusal("campaign")
        if refusal:
            contract.refuse("state", refusal)
    rec = None
    if a.cmd == "run":
        a.backend_arg = a.backend_arg or []
        rec = run_attempt(a)
    elif a.cmd == "validate":
        rec = validate_record(a)
    elif a.cmd == "campaign":
        rec = campaign.main(a, sys.modules[__name__])
    elif a.cmd == "audit":
        rec = audit(a)
    elif a.cmd == "floor":
        cfg = resolve_input(argparse.Namespace(**{**vars(a), "rev": None}))
        rec = floor.floor_command(cfg, a.since, a.write_declared, a.netclass_sizes)
    elif a.cmd == "blocked":
        return cmd_blocked(a)
    elif a.cmd == "doctor":
        rec = doctor(a.offline, a.canary, a.require_readonly_state)
    elif a.cmd == "spokes":
        return spokes.cmd(a), None
    elif a.cmd == "planes":
        return planes.cmd(a)
    elif a.cmd == "decoupling":
        return decoupling.cmd(a)
    elif a.cmd == "barrier":
        return barrier.cmd(a)
    elif a.cmd == "canary":
        from . import canary
        return canary.cmd_canary(a), None
    elif a.cmd == "corpus":
        return corpus.cmd_corpus(a), None
    elif a.cmd == "state-manifest":
        return statemanifest.cmd(a), None
    elif a.cmd == "determinism":
        from . import determinism
        return determinism.cmd(a, sys.modules[__name__]), None
    elif a.cmd == "publish":
        run_dir = Path(a.run_dir).resolve()
        r = load_json(run_dir / "result.json")
        if r is None:
            contract.refuse("input", f"{run_dir}: no result.json")
        dest = Path(a.to) / r["input"]["project"] / run_dir.parent.name
        shutil.rmtree(dest, ignore_errors=True)
        publish_run(run_dir, dest)
        log(f"[publish] {common.rel(dest)} (candidate stays in {r['run_dir']})")
        return 0, None
    else:
        return {"compare": lambda: compare(a.run_dirs), "update": lambda: update(),
                "smoke": lambda: smoke(a.publish)}[a.cmd](), None
    return rec["exit_code"], rec


def command_named(argv):
    """The subcommand in a command line that did not parse (for the refusal record)."""
    names = set(next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)).choices)
    return next((t for t in argv if t in names), None)


def main(argv=None):
    """Exit codes and records: docs/interface.md. Every failure before routing,
    usage errors included, is a refusal: exit 4 and OUT/refusals/<stamp>.json."""
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode, out = contract.prescan(argv)
    a = None
    with contract.machine_output(json_mode) as emit:
        try:
            if out:
                common.set_out(out)
            a = build_parser().parse_args(argv)
            if a.out:
                common.set_out(a.out)
            rc, rec = dispatch(a)
        except contract.Refusal as r:
            rec = contract.write_refusal(r, argv, getattr(a, "cmd", None) or command_named(argv))
            contract.report_refusal(r, rec, json_mode)
            rc = contract.REFUSED_EXIT
        emit(rec)
    return rc


if __name__ == "__main__":
    sys.exit(main())
