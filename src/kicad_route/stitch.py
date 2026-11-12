"""Stitch pour pieces that routing ringed off, after the built-in router (kicad/stitch.py).

New tracks around a pad can ring the pour it sits in off from the rest: a piece of copper
whose only tie to its net is that pad's thermal spokes. KiCad counts no spoke into such a
piece. Either the pad is then short of spokes ("Thermal relief connection to zone incomplete
(N spokes connected to isolated island)"), or it has no other copper and is cut from its net.
A pour or a whole plane that the routing never touched is the third case: nothing of its net
on it, KiCad's "Isolated copper fill".
On a two-layer board with ground poured on both sides the first alone decided whether a
complete routing was clean, and it came and went with the order the nets happened to take:
the board is clean in the engine's own order and has the finding with spoke corridors
reserved, with a finer grid, with the pour nets first and with the engine's planning pass.

apply() gives each such piece a via to the same net's copper on another layer, as a designer
would, when there is a place for one (kicad/stitch.py says which pieces, where and at what
size). KiCad's DRC is then asked about the board before and after, with the zones refilled,
and a via that any new finding names is taken out again; so are all of them when KiCad's
report is not the shorter for them. A piece with no place for a via, or on a net with copper
on one layer only, stays as it is and is listed with the reasons; KiCad's finding then stands.
"""
import shutil
from pathlib import Path

from . import common

ROUNDS = 2
SPOKE_AREAS = b"kicad-route:spoke:"      # kicad/spokes.py PREFIX, as it stands in a board file
_REPORTS = {}                            # (project directory, board sha256) -> (KiCad's report, the refilled copy)


def _helper(api, work, log, *args, timeout=900):
    run = common.run_cmd([api.pcbnew_python(), api.KICAD_SCRIPTS / "stitch.py", *args], work, Path(work) / log,
                         timeout=timeout)
    return run, (api.tagged_json(run["output"], "STITCH_JSON") if run["exit_code"] == 0 else None)


def _refilled(api, work, project_dir, board_file, label, folder="stitch-drc"):
    """(KiCad's DRC report of `board_file` with the zones refilled, the refilled copy it is of), or
    (None, None). The board is put into a copy of the project for it.
    The corridors of --reserve-spokes are still on the board at this point and come off before it
    is delivered, so they come off the graded copy: a via of the pad's own net in one is no finding
    of the board that the run delivers."""
    # The steps after the engine (this one, closing.py, thermals.py) each ask about the board as
    # it stands, and a step that changes nothing leaves the next one the same board to ask about.
    # A report is remembered with the copy it is of, until another board takes the copy's place.
    asked = (str(Path(project_dir).resolve()), common.sha256(board_file))
    if asked in _REPORTS:
        return _REPORTS[asked]
    directory = Path(work) / folder / label
    for known, (_, kept) in list(_REPORTS.items()):
        if kept.parent == directory:
            del _REPORTS[known]
    shutil.rmtree(directory, ignore_errors=True)
    api.copy_project(project_dir, directory)
    copy = directory / Path(board_file).name
    shutil.copyfile(board_file, copy)
    if SPOKE_AREAS in copy.read_bytes():
        bare = common.run_cmd([api.pcbnew_python(), api.KICAD_SCRIPTS / "spokes.py", "strip", copy], directory,
                              directory / "strip-corridors.log", timeout=600)
        if bare["exit_code"] != 0:
            return None, None
    report, _ = api.kicad_drc(copy, directory / "drc", label, True)
    if report is None:
        return None, None
    _REPORTS[asked] = (report, copy)
    return report, copy


def _graded(api, work, project_dir, board_file, label, folder="stitch-drc"):
    """KiCad's DRC report (zones refilled) of `board_file`, or None (see _refilled)."""
    return _refilled(api, work, project_dir, board_file, label, folder)[0]


def apply(work, board, before, output, scope, api):
    """Stitch the pieces of `output` (in place) that this run cut off. `board` is the work copy
    beside the working project, whose classes are what the engine routed with; `before` the
    board as it was before routing. Returns the record; "status" is "none", "stitched" or
    "failed" (the board is then as the router left it)."""
    work, board, output = Path(work), Path(board), Path(output)
    rec = {"status": "none", "vias": [], "left": [], "removed_after_drc": [], "commands": []}
    nets = work / "stitch-nets.json"
    common.write_json(nets, sorted(scope))
    shutil.copyfile(output, board)
    placed, found = _helper(api, work, "stitch-place.log", "place", before, board, nets)
    rec["commands"].append(common.public(placed))
    if found is None:
        return dict(rec, status="failed", reason=f"could not look for cut-off pour pieces (see {placed['log']})")
    rec.update(pieces_cut_off=found["pieces_cut_off"], left=found["left"])
    vias = {v["uuid"]: v for v in found["vias"]}
    if not vias:
        return rec
    legs = {v["leg"]["uuid"]: v["uuid"] for v in found["vias"] if v.get("leg")}      # a via's track goes with it
    with_legs = lambda uuids: sorted(set(uuids) | {leg for leg, via in legs.items() if via in set(uuids)})
    base = _graded(api, work, board.parent, output, "before")
    if base is None:
        return dict(rec, status="failed", reason="KiCad's DRC of the board before stitching failed")
    for index in range(ROUNDS + 1):
        report = _graded(api, work, board.parent, board, f"after-{index}")
        if report is None:
            return dict(rec, status="failed", reason="KiCad's DRC of the stitched board failed")
        new, _ = api.drc_delta(base, report, "violations")
        against = {}                         # via -> the kinds of new finding that name it
        for finding in new:
            for item in finding.get("items", []):
                via = item.get("uuid") if item.get("uuid") in vias else legs.get(item.get("uuid"))
                if via in vias:
                    against.setdefault(via, set()).add(finding.get("type"))
        named = sorted(against)
        if not named:
            break
        if index == ROUNDS:
            return dict(rec, status="failed", reason="stitching vias still break a rule after removal")
        listed = work / f"stitch-remove-{index}.json"
        common.write_json(listed, with_legs(named))
        removed, counted = _helper(api, work, f"stitch-remove-{index}.log", "remove", board, listed)
        rec["commands"].append(common.public(removed))
        if counted is None or counted["removed"] != len(with_legs(named)):
            return dict(rec, status="failed", reason=f"could not remove a stitching via KiCad's DRC names (see {removed['log']})")
        rec["removed_after_drc"] += [dict(vias.pop(uuid), findings=sorted(against[uuid])) for uuid in named]
        if not vias:
            return rec
    # A via is there to take a finding or an open connection away. KiCad's report has to be the
    # shorter for the vias that are left, with no more open connections than before (a via pushes
    # the pours of other nets back by their clearance, which no finding would name it for).
    count = lambda rep: (len(rep.get("unconnected_items") or []), len(rep.get("violations") or []))
    (open_before, found_before), (open_after, found_after) = count(base), count(report)
    if open_after > open_before or open_after + found_after >= open_before + found_before:
        listed = work / "stitch-remove-all.json"
        common.write_json(listed, with_legs(vias))
        removed, counted = _helper(api, work, "stitch-remove-all.log", "remove", board, listed)
        rec["commands"].append(common.public(removed))
        if counted is None or counted["removed"] != len(with_legs(vias)):
            return dict(rec, status="failed", reason=f"could not remove stitching vias that gained nothing (see {removed['log']})")
        rec["removed_no_gain"] = sorted(vias.values(), key=lambda v: (v["net"], v["x_mm"], v["y_mm"]))
        rec["drc_counts"] = {"before": [open_before, found_before], "after": [open_after, found_after]}
        return rec
    shutil.copyfile(board, output)
    return dict(rec, status="stitched", vias=sorted(vias.values(), key=lambda v: (v["net"], v["x_mm"], v["y_mm"])))
