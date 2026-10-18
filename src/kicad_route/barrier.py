"""Collision barriers (`run --barrier-from RUN_DIR`, `kicad-route barrier RUN_DIR`).

The first step of KICADROUTE-53 (rewind with coordinate barriers on DRC collision
frames): an earlier run's candidate failed DRC because new copper ran too close to
something. Rewind to that run's input (the state before the routing decision), put a
small keepout -- a barrier -- where the offending copper ran, and route again; the
backend must go around it. The barrier is removed before grading.

- plan(): reads a run's DRC findings (validate/new-findings.json: only what the
  routing introduced; candidate-drc.json when there is no baseline), keeps the
  collision types in BARRIER_TYPES, finds the NEW copper item in each (a track, arc
  or via of the candidate that matches nothing in the reference, as `spokes` does),
  and has kicad/barrier.py locate the point on that item closest to the other
  item's copper, on their shared layer. Positions are KiCad's millimetres; the plan
  is integer nanometres.
- around_route(): the run hook. Injects each barrier into the work input board (a
  copy; the reference is never touched) as a single-layer no-track/no-via rule area
  `kicad-route:barrier:<x>:<y>`, routes, then strips every barrier from the
  backend's output before it becomes the candidate. A barrier left behind fails the
  `keepouts` preservation check; a strip that fails is a process reason. Barriers
  only take room away from the backend; no rule is relaxed and grading uses the
  input's own project files.
- outcome(): after grading, whether new copper still entered each barrier and
  whether the same collision (type and other item) came back.
- preview(): `kicad-route barrier RUN_DIR` plans and injects into a copy of the
  run's reference project (RUN_DIR/barrier-preview/<project>/) without routing.
  Exit 0 a barrier was injected; 1 the analysis ran and failed (locating the
  collisions failed, or every barrier lies on existing copper), with the record in
  barrier-preview/barrier-plan.json; 4 refused before the preview was made
  (contract.refuse): input when RUN_DIR is not a graded run, KiCad cannot load its
  candidate, or it has no collision to barrier; state when the barriers cannot be
  injected into the preview copy.

See docs/barrier.md.
"""
import json
from pathlib import Path

from . import common
from . import contract

PASS, FAIL = common.PASS, common.FAIL
SCRIPT = Path(__file__).resolve().parent / "kicad" / "barrier.py"
NM = 1_000_000
LOAD_ERROR = "BARRIER_LOAD_ERROR:"  # kicad/barrier.py: the board did not load (exit 3)
DEFAULT_SIZE_MM = 0.05
# DRC findings that are a collision between two copper items, one of which the routing placed.
BARRIER_TYPES = ("clearance", "tracks_crossing", "shorting_items", "hole_clearance", "copper_edge_clearance")


def log(msg):
    print(msg, flush=True)


def _script(args, cwd, log_path, timeout=900):
    rec = common.run_cmd([common.PCBNEW_PYTHON, SCRIPT, *args], cwd, log_path, timeout=timeout)
    tag = None
    for line in rec["output"].splitlines():
        if line.startswith("BARRIER_JSON:"):
            try:
                tag = json.loads(line[len("BARRIER_JSON:"):])
            except json.JSONDecodeError:
                pass
    return rec, tag


# ------------------------------------------------------------- planning

def findings_of(vdir):
    """(violations, source file, note): the routing's own findings when a baseline was graded."""
    new = common.load_json(vdir / "new-findings.json")
    if new is not None:
        return new.get("violations", []), vdir / "new-findings.json", None
    drc = common.load_json(vdir / "candidate-drc.json")
    if drc is not None:
        return drc.get("violations", []), vdir / "candidate-drc.json", \
            "no new-findings.json (no baseline): barriers planned from every candidate DRC violation"
    return None, None, None


def select(violations, new_uuids):
    """Collision findings with a new copper item -> (requests, skipped)."""
    requests, skipped = [], []
    for i, v in enumerate(violations):
        uuids = [it.get("uuid") for it in v.get("items", []) if it.get("uuid")]
        if v.get("type") not in BARRIER_TYPES:
            skipped.append({"index": i, "type": v.get("type"), "reason": "not a copper collision type"})
            continue
        new = [u for u in uuids if u in new_uuids]
        if not new:
            skipped.append({"index": i, "type": v.get("type"),
                            "reason": "no new copper among its items (the routing did not place them)"})
            continue
        others = [u for u in uuids if u != new[0]]
        if not others:
            skipped.append({"index": i, "type": v.get("type"), "reason": "a single item: no collision partner"})
            continue
        requests.append({"index": i, "type": v["type"], "description": v.get("description"),
                         "new_uuid": new[0], "other_uuids": others})
    return requests, skipped


def plan(src_run, out_dir, size_mm=DEFAULT_SIZE_MM):
    """Barriers from src_run's graded candidate -> plan dict (also written to out_dir/barrier-plan.json).
    plan["reasons"] lists why no plan could be made."""
    from . import spokes
    src_run = Path(src_run).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = {"from": common.rel(src_run), "size_mm": size_mm, "size": round(size_mm * NM), "barriers": [],
           "skipped": [], "notes": [], "reasons": [], "commands": []}
    attempt = common.load_json(src_run / "attempt.json")
    if not isinstance(attempt, dict):
        rec["reasons"].append(f"{common.rel(src_run)}: not a routing run (attempt.json missing)")
        return rec
    vdir = src_run / "validate"
    violations, source, note = findings_of(vdir)
    ref_facts = common.load_json(src_run / attempt["reference_facts"]) if attempt.get("reference_facts") else None
    cand_facts = common.load_json(vdir / "candidate-facts.json")
    board = src_run / attempt.get("candidate_dir", "candidate") / attempt.get("board", "")
    if violations is None or ref_facts is None or cand_facts is None or not board.is_file():
        rec["reasons"].append(f"{common.rel(src_run)}: no graded candidate (candidate board, DRC report and "
                              "facts are needed)")
        return rec
    rec.update(source=common.rel(source), input_fingerprint=attempt.get("input_fingerprint"))
    if note:
        rec["notes"].append(note)
    new_uuids = {t["uuid"] for t in spokes.new_copper(ref_facts, cand_facts)}
    requests, skipped = select(violations, new_uuids)
    rec["skipped"] += skipped
    if not requests:
        rec["reasons"].append(f"no copper collision made by new copper in {common.rel(source)} "
                              f"({len(violations)} findings)")
        return rec
    req_path, out = out_dir / "barrier-request.json", out_dir / "barrier-located.json"
    common.write_json(req_path, {"findings": requests})
    r, tag = _script(["locate", board, req_path, out], out_dir, out_dir / "barrier-locate.log")
    rec["commands"].append(common.public(r))
    located = common.load_json(out) if r["exit_code"] == 0 and tag else None
    if located is None:
        if r["exit_code"] == 3 and LOAD_ERROR in r["output"]:
            rec["reasons"].append(f"KiCad could not load the candidate board {common.rel(board)} (see {r['log']})")
        else:  # the analysis itself failed: `kicad-route barrier` exits 1, not a refusal
            rec["locate_failed"] = True
            rec["reasons"].append(f"could not locate the collisions (see {r['log']})")
        return rec
    rec["skipped"] += located["skipped"]
    descs = {q["index"]: q["description"] for q in requests}
    rec["barriers"] = [dict(b, description=descs.get(b["index"])) for b in located["barriers"]]
    if not rec["barriers"]:
        rec["reasons"].append("no collision could be located on the candidate board")
    common.write_json(out_dir / "barrier-plan.json", rec)
    return rec


def settings_of(rec):
    """What the ledger must see: which input the barriers were found on and where they are (not
    the run directory's path, which depends on the working directory)."""
    if rec is None:
        return None
    return {"source_input": rec.get("input_fingerprint"), "size_mm": rec["size_mm"],
            "points": sorted([b["x"], b["y"], b["layer"]] for b in rec["barriers"])}


# ------------------------------------------------------------- run hook

def prepare(src_run, size_mm, run_dir, input_fingerprint):
    """`run --barrier-from`: plan into run_dir/barrier/. None when the option is off."""
    if src_run is None:
        return None
    rec = plan(src_run, run_dir / "barrier", size_mm)
    fp = rec.get("input_fingerprint")
    if fp and fp != input_fingerprint:
        rec["notes"].append(f"{rec['from']} routed a different input (fingerprint {fp[:12]}); the barriers keep "
                            "their board coordinates")
    for n in rec["notes"]:
        log(f"[route] barrier NOTE: {n}")
    return rec


def around_route(rec, work, board, route):
    """Run `route()` with the planned barriers injected into `board` (the work input) and
    stripped from the backend's output board. rec None: just route."""
    if rec is None:
        return route()
    if not rec["reasons"]:
        common.write_json(work / "barrier-plan.json", rec)
        ir, itag = _script(["inject", board, work / "barrier-plan.json"], work, work / "barrier-inject.log")
        rec["commands"].append(common.public(ir))
        if ir["exit_code"] != 0 or itag is None:
            rec["reasons"].append(f"could not inject the barriers (see {ir['log']})")
        else:
            rec["injected"] = itag["injected"]
            rec["injected_names"] = [b["name"] for b in itag["barriers"]]
            rec["skipped"] += itag["skipped"]
            if not itag["injected"]:
                rec["reasons"].append("every barrier overlaps existing copper; nothing was injected")
    if rec["reasons"]:
        return {"status": FAIL, "reasons": [f"barrier: {r}" for r in rec["reasons"]], "commands": rec["commands"],
                "exit_code": None, "output_board": None, "barrier": rec}
    log(f"[route] barrier: injected {rec['injected']} keepout(s) from {rec['from']} "
        f"({rec['size_mm']:g} mm square)")
    proc = route()
    out = proc.get("output_board")
    if proc["status"] == PASS and out and Path(out).exists():
        sr, stag = _script(["strip", out], work, work / "barrier-strip.log")
        proc.setdefault("commands", []).append(common.public(sr))
        rec["stripped"] = (stag or {}).get("stripped")
        rec["remaining"] = (stag or {}).get("remaining")
        if sr["exit_code"] != 0 or stag is None or stag.get("remaining"):
            msg = (f"could not strip the barriers from the backend output (see {sr['log']}); "
                   "the keepouts check grades what is left")
            rec["reasons"].append(msg)
            proc["reasons"].append(msg)
        elif stag["stripped"] != rec["injected"]:
            rec["notes"].append(f"backend output carried {stag['stripped']} of {rec['injected']} barriers "
                                "(stripped what was there)")
    proc["barrier"] = rec
    return proc


def outcome(rec, run_dir, attempt):
    """After validation: did new copper still enter a barrier, did its collision come back?"""
    from . import spokes
    vdir = run_dir / "validate"
    cand_facts = common.load_json(vdir / "candidate-facts.json")
    ref_facts = common.load_json(run_dir / attempt["reference_facts"])
    if rec is None or not rec.get("injected") or cand_facts is None or ref_facts is None:
        return None
    from .kicad import barrier as geo  # pure geometry; pcbnew is imported only by its main()
    new_items = spokes.new_copper(ref_facts, cand_facts)
    layer_order = [l["name"] for l in ref_facts["copper_layers"]]
    violations, _, _ = findings_of(vdir)
    out = []
    for b in rec["barriers"]:
        if b["name"] not in rec.get("injected_names", []):
            continue
        corridor = {"polygon": geo.square((b["x"], b["y"]), rec["size"])}
        hits = [t.get("uuid") for t in new_items if spokes.copper_hits(corridor, b["layer"], layer_order, t)]
        others = set(b["other_uuids"])
        again = [v.get("description") for v in violations or [] if v.get("type") == b["type"] and
                 others <= {it.get("uuid") for it in v.get("items", [])}]
        out.append({"name": b["name"], "layer": b["layer"], "type": b["type"],
                    "entered_by_new_copper": hits, "collision_recurred": bool(again), "recurred_as": again[:3]})
    summary = {"barriers": out, "entered": sum(1 for o in out if o["entered_by_new_copper"]),
               "recurred": sum(1 for o in out if o["collision_recurred"])}
    rec["outcome"] = summary
    return summary


# ------------------------------------------------------------- preview

def _preview_record(rec, bdir, rc):
    """The `kicad-route barrier` record (--json prints it), in barrier-preview/barrier-plan.json."""
    rec = {"schema": contract.schema_id("barrier-preview"), "kind": "barrier-preview", "exit_code": rc, **rec}
    common.write_json(bdir / "barrier-plan.json", rec)
    return rc, rec


def preview(src_run, size_mm=DEFAULT_SIZE_MM):
    """Plan barriers from RUN_DIR and inject them into a copy of its reference project
    -> (exit code, record). Every failure before the preview is a refusal (exit 4)."""
    import shutil
    src_run = Path(src_run).resolve()
    attempt = common.load_json(src_run / "attempt.json")
    if not isinstance(attempt, dict) or not all(attempt.get(k) for k in ("reference_dir", "candidate_dir", "board")):
        contract.refuse("input", f"{common.rel(src_run)}: not a routing run (attempt.json missing or incomplete)")
    ref = src_run / attempt["reference_dir"]
    if not (ref / attempt["board"]).is_file():
        contract.refuse("input", f"{common.rel(src_run)}: no reference board ({common.rel(ref / attempt['board'])})")
    bdir = src_run / "barrier-preview"  # not barrier/: a --barrier-from run keeps its own plan there
    shutil.rmtree(bdir, ignore_errors=True)  # an earlier preview's board or plan never sits beside a refusal
    rec = plan(src_run, bdir, size_mm)
    if rec.get("locate_failed"):
        for r in rec["reasons"]:
            log(f"[barrier] {r}")
        return _preview_record(rec, bdir, 1)
    if rec["reasons"]:
        contract.refuse("input", f"{common.rel(src_run)}: nothing to barrier: {'; '.join(rec['reasons'])}")
    dst = bdir / ref.name
    shutil.copytree(ref, dst, symlinks=True)
    ir, itag = _script(["inject", dst / attempt["board"], bdir / "barrier-plan.json"], bdir, bdir / "barrier-inject.log")
    rec["commands"].append(common.public(ir))
    if ir["exit_code"] != 0 or itag is None:
        shutil.rmtree(dst, ignore_errors=True)
        contract.refuse("state", f"could not inject the barriers into the preview copy of "
                                 f"{common.rel(ref / attempt['board'])} (see {ir['log']})")
    rec.update(injected=itag["injected"], injected_names=[b["name"] for b in itag["barriers"]],
               preview_board=common.rel(dst / attempt["board"]))
    rec["skipped"] += itag["skipped"]
    for b in rec["barriers"]:
        state = "injected" if b["name"] in rec["injected_names"] else "skipped"
        log(f"[barrier] {b['name']} on {b['layer']} ({b['type']}, new {b['new_item']['kind']} "
            f"{b['new_item']['net']}): {state}")
    log(f"[barrier] {itag['injected']} barrier(s) in {common.rel(dst / attempt['board'])}; "
        f"plan {common.rel(bdir / 'barrier-plan.json')}")
    if not itag["injected"]:
        rec["reasons"].append("every barrier overlaps existing copper; nothing was injected")
    return _preview_record(rec, bdir, 0 if itag["injected"] else 1)


# ------------------------------------------------------------- CLI

def _size(text):
    v = float(text)
    if not 0 < v <= 5:
        raise ValueError(text)
    return v


def add_run_option(rp):
    rp.add_argument("--barrier-from", metavar="RUN_DIR",
                    help="rewind: route this input with a small keepout where RUN_DIR's new copper collided "
                         "(its new DRC findings); the keepouts are removed before grading (docs/barrier.md)")
    rp.add_argument("--barrier-size", type=_size, default=DEFAULT_SIZE_MM, metavar="MM",
                    help=f"side of each square barrier (default {DEFAULT_SIZE_MM:g} mm)")


def add_parser(sub):
    bp = sub.add_parser("barrier", help="preview the collision barriers `run --barrier-from` would inject")
    bp.add_argument("run_dir", help="graded routing run (writes RUN_DIR/barrier-preview/)")
    bp.add_argument("--size", type=_size, default=DEFAULT_SIZE_MM, metavar="MM",
                    help=f"side of each square barrier (default {DEFAULT_SIZE_MM:g} mm)")
    return bp


def cmd(args):
    """-> (exit code, record)."""
    return preview(args.run_dir, args.size)
