"""Areas a board closes to new copper without saying so in a rule area (every `run`).

KiCad's DRC rejects new copper in places a routing backend cannot read from the board
the way it reads a keep-out. kicad/closed.py (pcbnew) finds them:

- solder-mask apertures that are not pad openings: a shape or text drawn on F.Mask or
  B.Mask opens a window in the mask, and `solder_mask_bridge` lets a window show copper
  of one net only; a pour under it counts. A backend that does not know the windows
  routes through them, and every track that crosses one is a bridge with whatever the
  window already showed.
- copper without a net: the tab of a land pattern, a jumper bridge, an antenna, a
  logo, a marking. It is foreign copper to every net. A backend may not model it, or
  may hand it to the net of a pad it touches and route that net across it.

around_route(): the hook every `run` goes through. Plans on the work input board, adds
the planned areas as single-layer rule areas named `kicad-route:closed:...`, routes,
then strips them from the backend's output before it becomes the candidate. A board
without such areas is routed as it is. Grading is unchanged: the candidate is graded
against the input's own rules, and the `keepouts` preservation check (rule areas
identical to the input) fails if an area survives. Closing an area only takes room
away from the backend; it never relaxes a rule.

See docs/closed.md.
"""
import json
from pathlib import Path

from . import common

PASS, FAIL = common.PASS, common.FAIL
SCRIPT = Path(__file__).resolve().parent / "kicad" / "closed.py"


def log(msg):
    print(msg, flush=True)


def _script(args, cwd, log_path, timeout=900):
    rec = common.run_cmd([common.PCBNEW_PYTHON, SCRIPT, *args], cwd, log_path, timeout=timeout)
    tag = None
    for line in rec["output"].splitlines():
        if line.startswith("CLOSED_JSON:"):
            try:
                tag = json.loads(line[len("CLOSED_JSON:"):])
            except json.JSONDecodeError:
                pass
    return rec, tag


def _failed(rec, reason):
    rec["reasons"].append(reason)
    return {"status": FAIL, "reasons": rec["reasons"], "commands": rec["commands"], "exit_code": None,
            "output_board": None, "closed_areas": rec}


def around_route(work, board, route):
    """Run `route()` (a backend call returning its process record) with the planned
    areas of `board` (the work input) closed. The strip runs on the backend's output
    board, so the candidate carries no such area; a strip that fails leaves them in
    place, which the keepouts preservation check then fails. A plan or a reservation
    that fails is a process failure: nothing is routed."""
    rec = {"plan": common.rel(work / "closed-plan.json"), "commands": [], "reasons": []}
    pr, ptag = _script(["plan", board, work / "closed-plan.json"], work, work / "closed-plan.log")
    rec["commands"].append(common.public(pr))
    if pr["exit_code"] != 0 or ptag is None:
        return _failed(rec, f"could not plan the areas closed to new copper (see {pr['log']})")
    rec.update(areas=ptag["area_count"], warnings=ptag["warnings"], mask=ptag["mask"], copper=ptag["copper"])
    mask, copper = ptag["mask"], ptag["copper"]
    if mask["left_open"]:
        log(f"[route] closed: {mask['left_open']} solder-mask window(s) left open, copper of the input lies under them")
    if copper["left_open"]:
        log(f"[route] closed: {copper['left_open']} copper shape(s) without a net left open to tracks, "
            "copper of the input can only be reached across them")
    if not ptag["area_count"]:
        proc = route()
        proc["closed_areas"] = rec
        return proc
    rr, rtag = _script(["reserve", board, work / "closed-plan.json"], work, work / "closed-reserve.log")
    rec["commands"].append(common.public(rr))
    rec["reserved"] = (rtag or {}).get("reserved")
    if rr["exit_code"] != 0 or rtag is None or rtag["reserved"] != ptag["area_count"]:
        return _failed(rec, f"could not close the planned areas (see {rr['log']})")
    log(f"[route] closed: {rec['reserved']} areas closed to new copper "
        f"({mask['closed']} of {mask['window_count']} solder-mask windows, "
        f"{copper['shape_count']} copper shapes without a net)")
    proc = route()
    out = proc.get("output_board")
    if proc["status"] == PASS and out and Path(out).exists():
        sr, stag = _script(["strip", out], work, work / "closed-strip.log")
        proc.setdefault("commands", []).append(common.public(sr))
        rec["stripped"] = (stag or {}).get("stripped")
        if sr["exit_code"] != 0 or stag is None or stag.get("remaining"):
            rec["reasons"].append(f"could not strip the closed areas from the backend output (see {sr['log']}); "
                                  "the keepouts check grades what is left")
            proc["reasons"].append(rec["reasons"][-1])
        elif stag["stripped"] != rec["reserved"]:
            rec["reasons"].append(f"backend output carried {stag['stripped']} of {rec['reserved']} closed "
                                  "areas (stripped what was there)")
    proc["closed_areas"] = rec
    return proc
