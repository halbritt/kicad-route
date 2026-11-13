"""Give pads their pour back, after the built-in router (kicad/spokes.py clear, reserve, strip).

A pad joined to a pour of its net by thermal relief reaches the pour through its spokes. A
track or via of another net close to the pad pushes the pour back by its clearance and a spoke
ends in nothing. With fewer spokes left than the board asks for, KiCad reports
`starved_thermal`; with none left it reports nothing, because it sees no thermal connection to
be short of, and the pad hangs on a track alone. The engine does not see spokes: it meets the
rule only when it happens to stay away. On a board whose through-hole ground pins sit in a
pour a complete routing came back with two to seven starved thermals, depending on the order
its nets took, and on another, clean by KiCad's DRC, one ground pin had lost its pour.

repair() does what a designer does with such a pad. The pads are the ones the pour check names
when it compares the routed board with the board before routing (pour.py: spokes below the
minimum, fill contact lost). For them it plans the spoke corridors on the board as it was
before routing, takes this run's own copper out of as many corridors as each pad is short of
(the board asks `min_resolved_spokes`; the corridors the fewest nets cross go first), reserves
the corridors the pad then has as rule areas, and has the engine route the nets it broke once
more. The corridors come off again, the boards are compared again with the zones refilled,
and the repair is kept only when fewer pads are short of their pour, no more connections are
open and KiCad's report is no longer. Otherwise the board stays as the router left it and the
findings stand. Copper the input had is never touched.
All short pads are tried together first; when that is not kept, up to six of them are tried
one by one, so that a net which cannot be routed again costs its own pad's repair only.
"""
import re
import shutil
from pathlib import Path

from . import common, pour, spokes, stitch

ROUNDS = 2          # a repair can starve a neighbour; one more round takes that, then it stops
SINGLE_PADS = 6     # pads tried one by one when the repair of all of them together was not kept
PASS_S = 120        # seconds the engine has for the nets of one round
MIN_PASS_S = 30     # a pass is not started with less of the repair's time left
DRC_PAD_RE = re.compile(r"(?:PTH )?[Pp]ad (\S+) \[[^\]]*\] of (\S+)")


def starved(report):
    """The starved_thermal errors of a DRC report."""
    return [v for v in (report or {}).get("violations") or []
            if v.get("type") == "starved_thermal" and v.get("severity") == "error"]


def _counts(report):
    return len(report.get("unconnected_items") or []), len(report.get("violations") or [])


# What the repair is for, as the pour check names it (pour.py): a thermal pad with fewer spokes
# than the board asks, and a pad the fill no longer touches at all. KiCad reports the first as
# starved_thermal and the second not at all: with every spoke gone it sees no thermal connection
# to be short of. A pad on a pour piece that is cut off is the stitch step's (stitch.py).
KINDS = ("spokes_below_minimum", "lost_fill_contact")


def _pour_facts(copy):
    """The pour facts of a refilled board copy, kept beside it."""
    out = Path(copy).parent / "pour-facts.json"
    facts = common.load_json(out) if out.is_file() else None
    if facts is None or facts.get("schema") != pour.SCHEMA:
        facts, _ = pour.extract(copy, out)
    return facts


def short_pads(api, work, project_dir, before, board_file, label, folder):
    """(KiCad's report of `board_file`, the pads that the copper laid since `before` left short
    of their pour, as "REF.PAD@layer"), or (None, None) when a step could not run."""
    ref_report, reference = stitch._refilled(api, work, project_dir, before, "reference", folder)
    report, copy = stitch._refilled(api, work, project_dir, board_file, label, folder)
    if ref_report is None or reference is None or report is None or copy is None:
        return None, None
    then, now = _pour_facts(reference), _pour_facts(copy)
    if then is None or now is None:
        return None, None
    _, delta = pour.compare(then, now)
    found = {failure["pad"] for failure in delta["failures"] if failure["kind"] in KINDS}
    new_v, _ = api.drc_delta(ref_report, report, "violations")
    for v in new_v:
        if v.get("type") != "starved_thermal" or v.get("severity") != "error":
            continue
        desc = v.get("description", "")
        layer = desc.split("layer ", 1)[1].split(";", 1)[0].strip() if "layer " in desc else None
        if not layer:
            continue
        for it in v.get("items", []):
            m = DRC_PAD_RE.match(it.get("description", ""))
            if m:
                found.add(f"{m.group(2)}.{m.group(1)}@{layer}")
    return report, sorted(found)


def repair(work, board, before, output, route_again, api, label="", seconds=PASS_S):
    """Repair the starved thermals of `output` (in place) that this run's copper causes.
    `label` keeps the work files and pass names of one call apart from another's.
    `seconds` is the engine time the whole repair may take, all its passes together: the time of
    one pass unless the caller says otherwise. On a large board every pass takes its full time,
    and five of them had put ten minutes on a run whose budget was already spent. The record's
    "used_s" is what the passes took of it.

    `board` is the work copy beside the working project, `before` the board as it was before
    routing. `route_again(name, board_file, nets, seconds)` routes those nets on that board with
    the run's own engine settings and returns a record with "status" ("routed", with "output",
    or "failed", with "reason") and "command".

    Returns the record; "status" is "none" (no starved thermal, or none of this run's making),
    "repaired", "kept" (KiCad's report was not the better for the repair: the board is as it
    was) or "failed" (a step could not run: the board is as it was)."""
    work, board, before, output = Path(work), Path(board), Path(before), Path(output)
    rec = {"status": "none", "rounds": [], "commands": []}
    folder = work / ("thermal-repair" + (f"-{label}" if label else ""))
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)

    def failed(reason):
        return dict(rec, status="failed", reason=reason)

    def helper(log_name, *args):
        run, tag = spokes._script(list(args), work, folder / log_name)
        rec["commands"].append(common.public(run))
        return run, (tag if run["exit_code"] == 0 else None)

    base, short = short_pads(api, work, board.parent, before, output, "before", folder.name + "-drc")
    if base is None:
        return failed("KiCad's DRC or the pour facts of the routed board failed")
    rec["starved_before"] = rec["starved_after"] = len(short)
    if not short:
        return rec
    if stitch.SPOKE_AREAS in output.read_bytes():
        # --reserve-spokes / --reserve-spoke-pad: the caller chose the corridors of this run, they
        # are still on the board, and a second set cannot be reserved beside them
        return dict(rec, note="the run reserved spoke corridors itself; its starved thermals are left to the caller")
    # the corridors as the board had them before any of this run's copper was there
    api.copy_project(board.parent, folder / "plan")
    plan_board = folder / "plan" / board.name
    shutil.copyfile(before, plan_board)
    planned, tag = helper("plan.log", "plan", plan_board, folder / "plan-all.json", "--include-solid", "1")
    plan = common.load_json(folder / "plan-all.json") if tag is not None else None
    if plan is None:
        return failed(f"could not plan the spoke corridors (see {planned['log']})")
    in_plan = {f"{p['ref']}.{p['pad']}@{p['layer']}": f"{p['uuid']}@{p['layer']}" for p in plan["pads"]}
    current, report = output, base
    # First every starved pad at once. When that is not kept, each pad on its own: one net that
    # cannot be routed again then costs its own pad's repair and not the others'.
    queue, together, index, left = [None], 0, 0, max(0.0, float(seconds))
    rec["budget_s"], allowed = int(left), left
    while queue:
        min_s = 15 if queue[0] is not None else MIN_PASS_S
        if left < min_s:
            rec["out_of_time"] = f"{len(queue)} more attempt(s) not started: under {min_s} s of the repair's time left"
            break
        wanted = queue.pop(0)
        index += 1
        named = {in_plan[pad] for pad in short if pad in in_plan}
        keys = sorted(named) if wanted is None else [key for key in wanted if key in named]
        entry = {"round": index, "starved": len(short), "pads": len(keys)}
        rec["rounds"].append(entry)
        if not keys:
            entry["result"] = "no starved pad has a spoke corridor in the plan"
            continue
        if wanted is None:
            together += 1
        plan_path = folder / f"plan-{index}.json"
        common.write_json(plan_path, spokes.select_pad_layers(plan, keys))
        api.copy_project(board.parent, folder / f"round-{index}")
        trial = folder / f"round-{index}" / board.name
        shutil.copyfile(current, trial)
        cleared, tag = helper(f"clear-{index}.log", "clear", before, trial, plan_path)
        if tag is None:
            return failed(f"could not clear the spoke corridors (see {cleared['log']})")
        entry.update(removed_tracks=tag["removed_tracks"], removed_vias=tag["removed_vias"], nets=tag["nets"],
                     cut_by=tag["pads"], left_alone=tag["left_alone"])
        if not tag["nets"]:
            entry["result"] = "none of this run's copper lies in the corridors the starved pads need"
            continue
        reserved, tag = helper(f"reserve-{index}.log", "reserve", trial, plan_path)
        if tag is None:
            return failed(f"could not reserve the spoke corridors (see {reserved['log']})")
        entry["reserved"] = tag["reserved"]
        if wanted is None and len(keys) > 1 and left >= 2 * MIN_PASS_S:
            share = int(min(PASS_S, max(MIN_PASS_S, left * 0.35)))
        elif wanted is not None and queue and left >= 30:
            share = int(min(PASS_S, max(15, left / (len(queue) + 1))))
        else:
            share = int(min(PASS_S, left))
        routed = route_again(f"thermal-{label + '-' if label else ''}{index}", trial, entry["nets"], share)
        left -= routed.get("used_s") or share
        if routed.get("command"):
            rec["commands"].append(routed["command"])
        kept = False
        if routed["status"] != "routed":
            entry["result"] = f"the nets could not be routed again ({routed.get('reason')}); the board stays as it was"
        else:
            again = Path(routed["output"])
            stripped, tag = helper(f"strip-{index}.log", "strip", again)
            if tag is None or tag.get("remaining"):
                return failed(f"could not take the spoke corridors off again (see {stripped['log']})")
            swept = common.run_cmd([api.pcbnew_python(), api.KICAD_SCRIPTS / "dangling.py", "sweep", before, again],
                                   work, folder / f"dangling-{index}.log", timeout=900)
            rec["commands"].append(common.public(swept))
            if swept["exit_code"] != 0:
                return failed(f"could not look for dangling copper after the repair (see {swept['log']})")
            after, short_after = short_pads(api, work, board.parent, before, again, f"after-{index}",
                                            folder.name + "-drc")
            if after is None:
                return failed("KiCad's DRC or the pour facts of the repaired board failed")
            (open_before, found_before), (open_after, found_after) = _counts(report), _counts(after)
            non_starved_before = found_before - len(starved(report))
            non_starved_after = found_after - len(starved(after))
            entry["drc_counts"] = {"before": [open_before, found_before], "after": [open_after, found_after]}
            entry["starved_after"] = len(short_after)
            kept = not (open_after > open_before or len(short_after) >= len(short)
                        or found_after > found_before or non_starved_after > non_starved_before)
            entry["result"] = "kept" if kept else "the board was not the better for it and stays as it was"
        if kept:
            current, report, short = again, after, short_after
            if wanted is None and short and together < ROUNDS:
                queue.append(None)          # a repair can starve a neighbour: once more for what is left
        elif wanted is None and len(keys) > 1:
            zone_pad_counts = {}
            for p in plan.get("pads") or []:
                zn = p.get("net")
                if zn:
                    zone_pad_counts[zn] = zone_pad_counts.get(zn, 0) + 1
            cut_by = entry.get("cut_by") or {}
            key_to_pad = {f"{p['uuid']}@{p['layer']}": f"{p['ref']}.{p['pad']} on {p['layer']}"
                          for p in plan.get("pads") or []}
            def _pad_order(k):
                nets_k = cut_by.get(key_to_pad.get(k, ""), [])
                max_zp = max((zone_pad_counts.get(n, 0) for n in nets_k), default=0)
                return (max_zp, len(nets_k), k)
            queue = [[key] for key in sorted(keys, key=_pad_order)[:SINGLE_PADS]]
    rec["used_s"] = round(allowed - left, 1)
    if current == output:
        tried = any("nets" in entry and entry["nets"] for entry in rec["rounds"])
        return dict(rec, status="kept" if tried else "none")
    shutil.copyfile(current, output)
    return dict(rec, status="repaired", starved_after=len(short))
