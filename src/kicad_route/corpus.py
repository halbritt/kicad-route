"""Real-board update gate: a corpus of project snapshots with recorded attempts
and baseline outcomes, re-run against the currently installed backends.

    kicad-route corpus add --dir D [--board B] [--rev REV] --name NAME
                           [--backend krt|freerouting] [--nets ...] [--netclass ...] [--budget S] ...
    kicad-route corpus check [--name NAME ...]
    kicad-route corpus list

The corpus lives in STATE/corpus/<name>/ (STATE is the install state, the
checkout by default; override the location with --root or KICAD_ROUTE_CORPUS).
It is never committed. Each entry holds:

    reference/<proj>/   snapshot of the project (snapshot_input: sheets, rules,
                        library tables, local libraries)
    entry.json          the attempt (backend, scope, settings) and its baseline
                        outcome from one run at `add` time
    checks/<stamp>.json every later `check` of the entry

`check` routes each entry again (runs go to OUT/runs/<NAME>/corpus-<NAME>/)
and compares to the baseline. A regression is: process FAIL, more unconnected
items in scope, more new DRC violations or parity issues, or a preservation
check that now fails. `update` runs `check` after smoke when the corpus is not
empty and records the outcome in update.json; it never rolls back.
"""
import argparse
import json
import os
import re
import shutil
from pathlib import Path

from . import common
from . import cli as route

PASS, FAIL = common.PASS, common.FAIL
RUN_KEYS = ("backend", "nets", "netclass", "budget", "max_iterations", "passes", "default_clearance",
            "clearance_margin", "protect", "backend_arg", "backend_env", "stall")


def corpus_root(root=None):
    return Path(root or os.environ.get("KICAD_ROUTE_CORPUS") or common.STATE / "corpus").expanduser().resolve()


def entries(root=None):
    base = corpus_root(root)
    out = []
    for p in sorted(base.glob("*/entry.json")) if base.exists() else []:
        e = common.load_json(p)
        if e:
            e["_dir"] = p.parent
            out.append(e)
    return out


def outcome(result):
    """The parts of a result.json a later check is compared on."""
    if result.get("refused"):
        return {"refused": result["refused"], "exit_code": result["exit_code"]}
    drc = result["validation"].get("drc") or {}
    return {"exit_code": result["exit_code"], "process": result["process"]["status"],
            "process_reasons": result["process"].get("reasons", []),
            "routing": result["routing"]["status"],
            "unconnected_in_scope": result["routing"].get("unconnected_in_scope"),
            "unconnected_total": result["routing"].get("unconnected_total"),
            "validation": result["validation"]["status"],
            "new_violations": (drc.get("new_violations") or {}).get("count"),
            "new_parity": (drc.get("new_parity") or {}).get("count"),
            "preservation_fail": sorted(k for k, v in (result["validation"].get("preservation") or {}).items()
                                        if v != PASS),
            "backend": {k: result["backend"].get(k) for k in ("name", "version", "commit", "sha256")
                        if result["backend"].get(k) is not None},
            "fingerprint": (result.get("ledger") or {}).get("short"),
            "run_dir": str(Path(result["run_dir"]).resolve())}


def attempt_args(entry):
    """argparse.Namespace for cli.run_attempt from a corpus entry."""
    a = entry["attempt"]
    proj = entry["_dir"] / entry["reference_dir"]
    return argparse.Namespace(
        dir=str(proj), board=entry["board"], schematic=entry.get("schematic"), name=entry["name"],
        library_dir=None, rev=None, label=f"corpus-{entry['name']}",
        backend=a["backend"], nets=a.get("nets"), netclass=a.get("netclass"), budget=a.get("budget", 300),
        max_iterations=a.get("max_iterations"), passes=a.get("passes"),
        default_clearance=a.get("default_clearance"), clearance_margin=a.get("clearance_margin"),
        protect=a.get("protect", "existing"), backend_arg=a.get("backend_arg") or [],
        backend_env=a.get("backend_env") or [], stall=a.get("stall"),
        force=True,   # a corpus check repeats an attempt on purpose; the ledger records it as forced
        canary=False)


def add(args):
    root = corpus_root(args.root)
    if not args.name:
        raise SystemExit("corpus add needs --name NAME")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.name):
        raise SystemExit(f"corpus name {args.name!r}: use letters, digits, '.', '_' and '-'")
    d = root / args.name
    if d.exists():
        raise SystemExit(f"corpus entry {args.name} exists ({d}); remove that directory to re-add it")
    cfg = route.resolve_input(args)
    d.mkdir(parents=True)
    try:
        ref, manifest = route.snapshot_input(cfg, d)
        entry = {"name": args.name, "created_utc": common.now_utc().isoformat(),
                 "board": cfg["pcb"], "schematic": cfg.get("schematic"),
                 "reference_dir": str(ref.relative_to(d)),
                 "input_fingerprint": route.input_fingerprint(manifest),
                 "source": {k: manifest.get(k) for k in ("source_dir", "source", "source_git_head")},
                 "attempt": {k: getattr(args, k) for k in RUN_KEYS if getattr(args, k, None) not in (None, [])}}
        entry["_dir"] = d
        route.log(f"[corpus] {args.name}: baseline attempt ({entry['attempt']['backend']})")
        result = route.run_attempt(attempt_args(entry))
        entry["baseline"] = outcome(result)
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    entry.pop("_dir")
    common.write_json(d / "entry.json", entry)
    route.log(f"[corpus] added {args.name}: {summary_line(entry['baseline'])} -> {d}")
    return 0


def summary_line(o):
    if o.get("refused"):
        return f"refused ({o['refused']})"
    return (f"exit {o['exit_code']}, process {o['process']}, {o['unconnected_in_scope']} unconnected in scope, "
            f"{o['new_violations']} new violations, {o['new_parity']} new parity, validation {o['validation']}")


def compare(base, now):
    """(regressions, improvements) of a check outcome against the baseline."""
    reg, imp = [], []
    if now.get("refused"):
        return [f"attempt refused: {now['refused']}"], imp
    if now["process"] != PASS:
        reg.append("process FAIL" + (" (baseline also FAIL)" if base.get("process") != PASS else "")
                   + (f": {'; '.join(now['process_reasons'][:2])}" if now.get("process_reasons") else ""))
    elif base.get("process") != PASS:
        imp.append("process now PASS")

    def num(o, k):
        v = o.get(k)
        return v if isinstance(v, int) else None

    for key, label in (("unconnected_in_scope", "unconnected in scope"), ("new_violations", "new DRC violations"),
                       ("new_parity", "new parity issues")):
        b, n = num(base, key), num(now, key)
        if b is None or n is None:
            continue
        if n > b:
            reg.append(f"{label} {b} -> {n}")
        elif n < b:
            imp.append(f"{label} {b} -> {n}")
    newly = sorted(set(now.get("preservation_fail", [])) - set(base.get("preservation_fail", [])))
    if newly:
        reg.append(f"preservation now fails: {', '.join(newly)}")
    return reg, imp


def check(names=None, root=None):
    """Re-run every entry (or those named); returns the summary record."""
    stamp = common.now_utc().strftime("%Y%m%dT%H%M%S.%fZ")
    rows = []
    selected = [e for e in entries(root) if not names or e["name"] in names]
    missing = sorted(set(names or []) - {e["name"] for e in selected})
    for e in selected:
        route.log(f"[corpus] check {e['name']} ({e['attempt']['backend']})")
        try:
            result = route.run_attempt(attempt_args(e))
            now = outcome(result)
        except SystemExit as ex:  # e.g. the snapshot no longer loads
            now = {"refused": f"attempt could not start: {ex}", "exit_code": None}
        reg, imp = compare(e["baseline"], now)
        row = {"name": e["name"], "status": FAIL if reg else PASS, "regressions": reg, "improvements": imp,
               "baseline": e["baseline"], "now": now,
               "upstream_changed": e["baseline"].get("backend") != now.get("backend")}
        common.write_json(e["_dir"] / "checks" / f"{stamp}.json", row)
        rows.append(row)
        route.log(f"[corpus] {row['status']:<4} {e['name']}: {summary_line(now)}"
                  + (f"; REGRESSION: {'; '.join(reg)}" if reg else "")
                  + (f"; improved: {'; '.join(imp)}" if imp else ""))
    rec = {"kind": "corpus-check", "time_utc": common.now_utc().isoformat(), "root": str(corpus_root(root)),
           "status": FAIL if any(r["status"] == FAIL for r in rows) or missing else PASS,
           "entries": len(rows), "regressions": {r["name"]: r["regressions"] for r in rows if r["regressions"]},
           "missing": missing, "checks": rows}
    dest = route.out_path("corpus", f"check-{stamp}.json")
    common.write_json(dest, rec)
    rec["report"] = common.rel(dest)
    route.log(f"[corpus] check: {rec['status']} ({len(rows)} entries, {len(rec['regressions'])} regressed) -> "
              f"{rec['report']}")
    return rec


def after_update(update_json, smoke_rc, root=None):
    """`update` hook: with a non-empty corpus, check it and record the outcome in
    update.json. Regressions are reported (non-zero exit), never rolled back."""
    if not entries(root):
        return smoke_rc
    route.log("[update] corpus: re-running the real-board gate against the installed backends")
    rec = check(root=root)
    data = common.load_json(update_json) or {}
    data["corpus"] = {"status": rec["status"], "entries": rec["entries"], "regressions": rec["regressions"],
                      "missing": rec["missing"], "report": rec["report"]}
    common.write_json(update_json, data)
    if rec["regressions"]:
        route.log("[update] corpus REGRESSIONS (not rolled back):\n  " + "\n  ".join(
            f"{n}: {'; '.join(r)}" for n, r in rec["regressions"].items()))
    return smoke_rc or (0 if rec["status"] == PASS else 1)


def cmd_corpus(args):
    if args.corpus_cmd == "add":
        if any("=" not in e for e in args.library_dir or []):
            raise SystemExit("--library-dir takes NAME=PATH")
        return add(args)
    if args.corpus_cmd == "check":
        return 0 if check(args.names, args.root)["status"] == PASS else 1
    for e in entries(args.root):
        route.log(f"{e['name']:<24} {e['attempt']['backend']:<12} {summary_line(e['baseline'])}  ({e['_dir']})")
    return 0


def add_parser(sub, add_input_args):
    cp = sub.add_parser("corpus", help="real-board regression gate: snapshots with baseline outcomes")
    cs = cp.add_subparsers(dest="corpus_cmd", required=True)
    a = cs.add_parser("add", help="snapshot a project with one attempt and record its baseline outcome")
    add_input_args(a)  # its --name is the entry's name (required)
    a.add_argument("--backend", default="krt", choices=sorted(route.BACKENDS))
    a.add_argument("--nets", nargs="+", metavar="PATTERN")
    a.add_argument("--netclass", nargs="+", metavar="CLASS")
    a.add_argument("--budget", type=int, default=300)
    a.add_argument("--max-iterations", type=int)
    a.add_argument("--passes", type=int)
    a.add_argument("--default-clearance", type=float, metavar="MM")
    a.add_argument("--clearance-margin", type=float, metavar="MM")
    a.add_argument("--protect", choices=["existing", "locked"], default="existing")
    a.add_argument("--backend-arg", action="append", metavar="ARG")
    a.add_argument("--backend-env", action="append", metavar="KEY=VALUE")
    a.add_argument("--stall", type=int, metavar="SECONDS")
    c = cs.add_parser("check", help="re-run every entry with the installed backends and compare to its baseline")
    c.add_argument("--name", dest="names", action="append", metavar="NAME", help="only this entry (repeatable)")
    ls = cs.add_parser("list", help="list the corpus entries and their baselines")
    for p in (a, c, ls):
        p.add_argument("--root", help="corpus location (default STATE/corpus or $KICAD_ROUTE_CORPUS)")
    return cp
