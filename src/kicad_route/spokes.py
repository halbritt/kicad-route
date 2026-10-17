"""Thermal-spoke corridor reservation (`run --reserve-spokes`) and spoke-cut attribution
(`kicad-route spokes`).

New signal copper routed close to a pad that joins a pour through thermal relief can
cut the pour back from the spoke ends; KiCad then drops those spokes and DRC reports
`starved_thermal`, but only after routing. kicad/spokes.py (pcbnew) plans one
corridor per spoke on the refilled board -- the region the spoke needs to stay
connected -- and reserves the best N per pad as single-layer no-track/no-via rule
areas named `kicad-route:spoke:...`.

- around_route(): the `run --reserve-spokes [N]` hook. Plans and reserves on the work
  input board, routes, then strips the areas from the backend's output before it
  becomes the candidate. Grading is unchanged: the candidate is graded against the
  input's own rules, and the `keepouts` preservation check (rule areas identical to
  the input) fails if a corridor survives. Reserving only tightens what the backend
  may do; it never relaxes a rule.
- evaluate(): `kicad-route spokes RUN_DIR`. Plans corridors on the run's reference
  board and intersects them with the candidate's NEW copper (validate facts vs
  reference facts) and with the candidate DRC's starved_thermal pads: which new
  track or via crossed which spoke corridor of which starved pad. Writes
  RUN_DIR/spokes.json; exit 0 without new starved_thermal findings, 3 with them.
- plan_only(): `kicad-route spokes --board B.kicad_pcb`: preview the corridors.

Corridor geometry is a model of KiCad's zone filler, not the filler itself; see
docs/spokes.md for its limits.
"""
import json
import math
import sys
from pathlib import Path

from . import common

PASS, FAIL = common.PASS, common.FAIL
SCRIPT = Path(__file__).resolve().parent / "kicad" / "spokes.py"
NM = 1_000_000


def log(msg):
    print(msg, flush=True)


def _script(args, cwd, log_path, timeout=900):
    rec = common.run_cmd([common.PCBNEW_PYTHON, SCRIPT, *args], cwd, log_path, timeout=timeout)
    tag = None
    for line in rec["output"].splitlines():
        if line.startswith("SPOKES_JSON:"):
            try:
                tag = json.loads(line[len("SPOKES_JSON:"):])
            except json.JSONDecodeError:
                pass
    return rec, tag


# ------------------------------------------------------------- run hook

def around_route(per_pad, work, board, route):
    """Run `route()` (a backend call returning its process record) with spoke corridors
    reserved on `board` (the work input). per_pad None: no reservation, just route;
    0: the board's min_resolved_spokes; N: N corridors per pad. The strip runs on the
    backend's output board, so the candidate carries no corridor; a strip that fails
    leaves them in place, which the keepouts preservation check then fails."""
    if per_pad is None:
        return route()
    rec = {"per_pad_requested": per_pad or "min_resolved_spokes", "plan": common.rel(work / "spokes-plan.json"),
           "commands": [], "reasons": []}
    plan_args = ["plan", board, work / "spokes-plan.json"] + (["--per-pad", str(per_pad)] if per_pad else [])
    pr, ptag = _script(plan_args, work, work / "spokes-plan.log")
    rec["commands"].append(common.public(pr))
    if pr["exit_code"] == 0 and ptag:
        rec.update(per_pad=ptag["per_pad"], min_resolved_spokes=ptag["min_resolved_spokes"],
                   clearance_mm=ptag["clearance"] / NM, pads=ptag["pad_count"],
                   corridors_live=ptag["corridors_live"], warnings=ptag["warnings"])
        rr, rtag = _script(["reserve", board, work / "spokes-plan.json"], work, work / "spokes-reserve.log")
        rec["commands"].append(common.public(rr))
        rec["reserved"] = (rtag or {}).get("reserved")
        if rr["exit_code"] != 0 or rtag is None:
            rec["reasons"].append(f"could not reserve spoke corridors (see {rr['log']})")
    else:
        rec["reasons"].append(f"could not plan spoke corridors (see {pr['log']})")
    if rec["reasons"]:
        return {"status": FAIL, "reasons": rec["reasons"], "commands": rec["commands"], "exit_code": None,
                "output_board": None, "spoke_reservation": rec}
    log(f"[route] spokes: reserved {rec['reserved']} corridors on {rec['pads']} thermal pads "
        f"({rec['per_pad']} per pad)")
    proc = route()
    out = proc.get("output_board")
    if proc["status"] == PASS and out and Path(out).exists():
        sr, stag = _script(["strip", out], work, work / "spokes-strip.log")
        proc.setdefault("commands", []).append(common.public(sr))
        rec["stripped"] = (stag or {}).get("stripped")
        if sr["exit_code"] != 0 or stag is None or stag.get("remaining"):
            rec["reasons"].append(f"could not strip the spoke corridors from the backend output (see {sr['log']}); "
                                  "the keepouts check grades what is left")
            proc["reasons"].append(rec["reasons"][-1])
        elif stag["stripped"] != rec["reserved"]:
            rec["reasons"].append(f"backend output carried {stag['stripped']} of {rec['reserved']} reserved "
                                  "corridors (stripped what was there)")
    proc["spoke_reservation"] = rec
    return proc


# ------------------------------------------------------------- geometry

def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _seg_seg_intersect(p1, p2, q1, q2):
    d1, d2 = _cross(q1, q2, p1), _cross(q1, q2, p2)
    d3, d4 = _cross(p1, p2, q1), _cross(p1, p2, q2)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _point_seg(p, a, b):
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)


def _inside(p, poly):
    """Point in polygon (even-odd)."""
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > p[1]) != (y2 > p[1]) and p[0] < (x2 - x1) * (p[1] - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def seg_poly_distance(a, b, poly):
    """Distance between segment a-b and polygon (0 when they touch or overlap)."""
    if _inside(a, poly) or _inside(b, poly):
        return 0.0
    edges = list(zip(poly, poly[1:] + poly[:1]))
    if any(_seg_seg_intersect(a, b, e1, e2) for e1, e2 in edges):
        return 0.0
    return min(min(_point_seg(e1, a, b), _point_seg(a, e1, e2), _point_seg(b, e1, e2)) for e1, e2 in edges)


def arc_points(start, mid, end, n=12):
    """Polyline through an arc given by three points (falls back to the chord)."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9:
        return [start, end]
    ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1) + (x3 ** 2 + y3 ** 2) * (y1 - y2)) / d
    uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3) + (x3 ** 2 + y3 ** 2) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1, a2, a3 = (math.atan2(y - uy, x - ux) for x, y in (start, mid, end))
    sweep = (a3 - a1) % (2 * math.pi)
    if (a2 - a1) % (2 * math.pi) > sweep:  # mid not on the ccw sweep: go the other way
        sweep -= 2 * math.pi
    return [(ux + r * math.cos(a1 + sweep * i / n), uy + r * math.sin(a1 + sweep * i / n)) for i in range(n + 1)]


def copper_hits(corridor, layer, layer_order, item):
    """True when a facts track/arc/via's copper overlaps the corridor polygon on layer."""
    poly = [tuple(p) for p in corridor["polygon"]]
    if "position" in item:  # via: on every copper layer between its two ends
        idx = [layer_order.index(n) for n in item["layers"] if n in layer_order]
        if layer not in layer_order or len(idx) != 2 or not min(idx) <= layer_order.index(layer) <= max(idx):
            return False
        p = tuple(item["position"])
        return _inside(p, poly) or min(_point_seg(p, e1, e2) for e1, e2 in zip(poly, poly[1:] + poly[:1])) \
            < item["diameter"] / 2
    if item["layer"] != layer:
        return False
    pts = arc_points(item["start"], item["mid"], item["end"]) if "mid" in item else [item["start"], item["end"]]
    return any(seg_poly_distance(tuple(a), tuple(b), poly) < item["width"] / 2 for a, b in zip(pts, pts[1:]))


# ------------------------------------------------------------- evaluation

def new_copper(ref_facts, cand_facts):
    """Candidate tracks/vias not matching any reference item by geometry (cli.match_copper)."""
    from . import cli as route
    out = []
    for kind in ("tracks", "vias"):
        ref = [route.strip(t, "uuid", "locked") for t in ref_facts[kind]]
        cand = [route.strip(t, "uuid", "locked") for t in cand_facts[kind]]
        _, _, extra = route.match_copper(ref, cand)
        ids = {id(c): orig for c, orig in zip(cand, cand_facts[kind])}
        out += [ids[id(c)] for c in extra]
    return out


def _describe(item):
    rec = {"kind": "via" if "position" in item else item["kind"], "uuid": item.get("uuid"), "net": item["net"]}
    if "position" in item:
        rec.update(layers=item["layers"], position_mm=[c / NM for c in item["position"]])
    else:
        rec.update(layer=item["layer"], width_mm=item["width"] / NM,
                   start_mm=[c / NM for c in item["start"]], end_mm=[c / NM for c in item["end"]])
    return rec


def starved_pads(drc, new_findings):
    """starved_thermal findings -> [{uuid, layer, ref/pad text, position, new, description}]"""
    from . import cli as route
    new_keys = {route.item_key(v) for v in (new_findings or {}).get("violations", [])}
    out = []
    for v in (drc or {}).get("violations", []):
        if v.get("type") != "starved_thermal":
            continue
        desc = v.get("description", "")
        layer = desc.split("layer ", 1)[1].split(";", 1)[0].strip() if "layer " in desc else None
        for it in v.get("items", []):
            if it.get("description", "").startswith("Pad "):
                out.append({"pad_item": it.get("description"), "uuid": it.get("uuid"), "layer": layer,
                            "position_mm": [it.get("pos", {}).get("x"), it.get("pos", {}).get("y")],
                            "new": route.item_key(v) in new_keys if new_findings is not None else None,
                            "description": desc})
    return out


def attribute(plan, new_items, starved, layer_order, reserved=None):
    """Intersect every live corridor with new copper; attribute starved pads."""
    crossings, by_pad = [], {}
    for p in plan["pads"]:
        for cr in p["corridors"]:
            if not cr["live"]:
                continue
            hits = [_describe(t) for t in new_items if copper_hits(cr, p["layer"], layer_order, t)]
            if hits:
                c = {"ref": p["ref"], "pad": p["pad"], "layer": p["layer"], "k": cr["k"],
                     "direction_deg": cr["direction_deg"], "by": hits}
                crossings.append(c)
                by_pad.setdefault((p["uuid"], p["layer"]), []).append(c)
    pads = {(p["uuid"], p["layer"]): p for p in plan["pads"]}
    report = []
    for s in starved:
        p = pads.get((s["uuid"], s["layer"]))
        if p is None:  # fall back to position (UUIDs may not survive every tool)
            pos = [round(c * NM) for c in s["position_mm"]] if None not in s["position_mm"] else None
            p = next((q for q in plan["pads"] if q["layer"] == s["layer"] and pos and
                      all(abs(a - b) <= 1000 for a, b in zip(q["position"], pos))), None)
        rec = dict(s)
        if p is None:
            rec.update(planned=False, attributed=False,
                       note="pad not in the plan (no thermal connection modelled for it)")
        else:
            cut = by_pad.get((p["uuid"], p["layer"]), [])
            rec.update(planned=True, ref=p["ref"], pad=p["pad"], zone=p["zone"], live_spokes_in_input=p["live_spokes"],
                       crossed_corridors=cut, attributed=bool(cut),
                       cutting_items=sorted({h["uuid"] for c in cut for h in c["by"]}))
        report.append(rec)
    out = {"corridors_live": sum(p["live_spokes"] for p in plan["pads"]), "corridors_crossed": len(crossings),
           "pads_with_crossed_corridors": len(by_pad), "crossings": crossings, "starved_pads": report}
    if reserved is not None:  # the run reserved corridors: new copper in one means the backend ignored a keepout
        hit = []
        for p in reserved["pads"]:
            for cr in p["corridors"]:
                if cr["selected"]:
                    by = [_describe(t) for t in new_items if copper_hits(cr, p["layer"], layer_order, t)]
                    if by:
                        hit.append({"ref": p["ref"], "pad": p["pad"], "layer": p["layer"], "k": cr["k"], "by": by})
        out["reserved"] = {"corridors": reserved["corridors_selected"], "crossed_by_new_copper": hit}
    return out


def evaluate(run_dir, validation_dir=None):
    run_dir = Path(run_dir).resolve()
    attempt = common.load_json(run_dir / "attempt.json")
    if attempt is None:
        sys.exit(f"{run_dir}: not a routing run (attempt.json missing)")
    vdir = Path(validation_dir).resolve() if validation_dir else run_dir / "validate"
    ref_board = run_dir / attempt["reference_dir"] / attempt["board"]
    ref_facts = common.load_json(run_dir / attempt["reference_facts"])
    cand_facts = common.load_json(vdir / "candidate-facts.json")
    drc = common.load_json(vdir / "candidate-drc.json")
    if cand_facts is None or drc is None or ref_facts is None:
        sys.exit(f"{common.rel(vdir)}: candidate facts or DRC missing (validate the run first)")
    sdir = run_dir / "spokes"
    sdir.mkdir(exist_ok=True)
    rec, tag = _script(["plan", ref_board, sdir / "reference-plan.json"], run_dir, sdir / "plan.log")
    plan = common.load_json(sdir / "reference-plan.json") if rec["exit_code"] == 0 else None
    if plan is None:
        log(f"[spokes] could not plan corridors on {common.rel(ref_board)} (see {rec['log']})")
        return 1
    result = common.load_json(run_dir / "result.json") or {}
    reserved = None
    if (result.get("settings") or {}).get("reserve_spokes") is not None:
        reserved = common.load_json(run_dir / "work" / "spokes-plan.json")
    layer_order = [l["name"] for l in ref_facts["copper_layers"]]
    starved = starved_pads(drc, common.load_json(vdir / "new-findings.json"))
    new_items = new_copper(ref_facts, cand_facts)
    report = {"kind": "spokes", "run_dir": common.rel(run_dir), "validation_dir": common.rel(vdir),
              "reference_board": common.rel(ref_board), "plan": common.rel(sdir / "reference-plan.json"),
              "plan_command": common.public(rec), "clearance_mm": plan["clearance"] / NM,
              "min_resolved_spokes": plan["min_resolved_spokes"], "warnings": plan["warnings"],
              "thermal_pads": plan["pad_count"], "new_items": len(new_items),
              **attribute(plan, new_items, starved, layer_order, reserved)}
    new_starved = [s for s in report["starved_pads"] if s["new"] is not False]
    report["new_starved"] = len(new_starved)
    report["new_starved_attributed"] = sum(1 for s in new_starved if s["attributed"])
    report["exit_code"] = 3 if new_starved else 0
    common.write_json(run_dir / "spokes.json", report)
    log(f"[spokes] {report['thermal_pads']} thermal pads, {report['corridors_live']} live spoke corridors; "
        f"{report['corridors_crossed']} crossed by new copper on {report['pads_with_crossed_corridors']} pads")
    log(f"[spokes] starved_thermal: {len(report['starved_pads'])} ({report['new_starved']} new, "
        f"{report['new_starved_attributed']} attributed to new copper)")
    if reserved is not None:
        log(f"[spokes] reserved corridors crossed by new copper: {len(report['reserved']['crossed_by_new_copper'])}")
    log(f"[spokes] {common.rel(run_dir / 'spokes.json')}  (exit {report['exit_code']})")
    return report["exit_code"]


def plan_only(board, per_pad=None):
    board = Path(board).resolve()
    out = common.out_dir() / "spokes" / board.stem
    out.mkdir(parents=True, exist_ok=True)
    args = ["plan", board, out / "plan.json"] + (["--per-pad", str(per_pad)] if per_pad else [])
    rec, tag = _script(args, board.parent, out / "plan.log")
    if rec["exit_code"] != 0 or tag is None:
        log(f"[spokes] plan failed (see {rec['log']})")
        return 1
    log(f"[spokes] {tag['pad_count']} thermal pads, {tag['corridors_live']} live spokes, "
        f"{tag['corridors_selected']} corridors selected ({tag['per_pad']} per pad, clearance "
        f"{tag['clearance'] / NM:g} mm)" + (f"; warnings: {'; '.join(tag['warnings'])}" if tag["warnings"] else ""))
    log(f"[spokes] {common.rel(out / 'plan.json')}")
    return 0


# ------------------------------------------------------------- CLI

def _count(text):
    n = int(text)
    if n < 0 or n > 4:
        raise ValueError(text)
    return n


def add_run_option(rp):
    rp.add_argument("--reserve-spokes", nargs="?", type=_count, const=0, default=None, metavar="N",
                    help="keep new tracks/vias out of N thermal-spoke corridors per zone-connected pad "
                         "(default N: the board's min_resolved_spokes) while routing; the corridors are "
                         "removed before grading (docs/spokes.md)")


def add_parser(sub):
    sp = sub.add_parser("spokes", help="spoke-cut attribution for a run, or plan corridors for a board")
    sp.add_argument("run_dir", nargs="?", help="routing run to evaluate (writes RUN_DIR/spokes.json)")
    sp.add_argument("--validation", metavar="DIR", help="validation directory to read (default RUN_DIR/validate; "
                                                        "e.g. RUN_DIR/revalidate/<stamp>)")
    sp.add_argument("--board", help="instead of a run: plan corridors for this board (OUT/spokes/<board>/plan.json)")
    sp.add_argument("--per-pad", type=int, help="with --board: corridors selected per pad")
    return sp


def cmd(args):
    if args.board:
        return plan_only(args.board, args.per_pad)
    if not args.run_dir:
        sys.exit("kicad-route spokes: pass RUN_DIR or --board")
    return evaluate(args.run_dir, args.validation)
