"""Close the connections the built-in router left open, in the time the run has left.

The engine gives each search a fixed allowance of steps, takes its nets in an order of its own
and ends when it has been through them: with connections open and, on most boards, with most
of the budget unspent (a through-hole board of 164 connections: 12 open after 690 of 1800
seconds, 71 of its failed searches stopped by the allowance). Longer searches for every net
cost more than they gain while the board is still filling up, which is why the run's own pass
has the allowance.

close() spends what is left of the budget on the nets that are still open, and on those alone.
Each round has the engine route them once more on the board as it stands, with different
settings from the round before (STRATEGIES): first with searches that may run on, then also
with the authority to lift copper this run laid, and in the time those two leave with leave to
route a net that is still open below its class sizes. KiCad's DRC is asked about the board before
and after each round with the zones refilled, and a round is kept only when its board has
fewer open connections than the board it started from and no finding more. Otherwise the
board stays as it was. So the rounds can only take open connections away, whatever the order
of the engine's nets does to one of them, and never trade one for a rule violation. A
connection closed beside a thermal pad can starve it; the round's board gets the thermal repair
(thermals.py) before it is judged.
"""
import re
import shutil
from pathlib import Path

from . import common, stitch

MIN_S = 30          # a round is not started with less; the engine's start alone takes a third of it
# What a round does differently from the run's own pass: its name, engine environment and options.
# LIFT_*: the engine may lift copper this run laid when it is what closes a connection in, and
# puts it back when it cannot route the lifted net again. Copper of the input is locked for the
# routing and is never lifted (the engine's own rule); a round is judged by KiCad's report either way.
LIFT_ENV = {"KICAD_DYNAMIC_ITERATIONS": "1", "KICAD_RIP_PREEXISTING": "1", "KICAD_FINALIZE_RIP": "1"}
LIFT_ARGS = ("--rip-existing-nets", "*")
# NARROW_*: a net that is still open may be routed below the sizes of its class, down to the
# board's own minimums (Board Setup), which are what KiCad grades a track and a via by. Designers
# do this where a class track does not fit: a through-hole board of the lab has lanes of 0.84 mm
# between pads where its 0.4 mm class track needs 0.91, and its designer took 0.3 mm tracks
# through six of them. The engine tries a net at its class sizes first and goes down only for a
# net it could not connect (its rescue and its last escalation, switched off in every other
# pass). Such a round has the board's pour nets among its nets ("pours"): a new track that cuts
# a pour is then the engine's to mend, where otherwise the engine throws the whole round away
# for the pads the cut leaves open. It runs in the time the other rounds leave ("spare").
NARROW_ENV = {"KICAD_DYNAMIC_ITERATIONS": "1", "KICAD_TERMINAL_ESCALATION": "1"}
NARROW_ARGS = ("--escalation", "board")
STRATEGIES = (
    {"name": "searches may run on", "env": {"KICAD_DYNAMIC_ITERATIONS": "1"}, "args": ()},
    {"name": "searches may run on, and this run's copper may be lifted", "env": LIFT_ENV, "args": LIFT_ARGS},
    {"name": "a net still open may go down to the board's own minimum sizes", "env": NARROW_ENV,
     "args": NARROW_ARGS, "spare": True, "pours": True},
)
NET_OF_ITEM = re.compile(r"\[([^\]]+)\]")


def open_nets(report):
    """The nets KiCad's unconnected items name, sorted."""
    nets = set()
    for finding in (report or {}).get("unconnected_items") or []:
        for item in finding.get("items") or []:
            named = NET_OF_ITEM.search(item.get("description", ""))
            if named:
                nets.add(named.group(1))
    return sorted(nets)


def _counts(report):
    return len(report.get("unconnected_items") or []), len(report.get("violations") or [])


def better(before, after):
    """Whether a round left the better board: (open connections, findings) before and after it.

    Fewer open connections and no finding more. A board with its connections open is still a
    valid board; one new finding fails it, so two connections closed are no trade for one."""
    return after[0] < before[0] and after[1] <= before[1]


def close(work, board, before, output, route_again, seconds, api, in_scope=None, settle=None, pour_nets=()):
    """Route the open nets of `output` (in place) once more, in up to `seconds`.

    `board` is the work copy beside the working project, `before` the board as it was before
    routing. `route_again(name, board_file, nets, seconds, strategy)` routes those nets on that
    board with the run's own engine settings and the strategy's on top (one of STRATEGIES), and
    returns a record with "status" ("routed", with "output" and "used_s", or "failed", with
    "reason") and "command".
    `in_scope(net)` says whether a net is the run's to route; None takes every net.
    `pour_nets` are the run's nets that own a pour; a strategy marked "pours" routes them along.
    `settle(name, board_file, seconds)` may repair what a round's new copper did to the board (a
    starved thermal) before the round is judged, in up to `seconds` of engine time; it returns
    the commands it ran and the engine time it took, which comes out of the rounds' time.

    Returns the record; "status" is "none" (nothing open, or no time), "closed" (the board has
    fewer open connections now), "kept" (no round was the better: the board is as it was) or
    "failed" (a step could not run: the board is as it was)."""
    work, board, before, output = Path(work), Path(board), Path(before), Path(output)
    rec = {"status": "none", "rounds": [], "commands": [], "budget_s": int(seconds)}
    if seconds < MIN_S:
        return dict(rec, reason=f"under {MIN_S} s of the budget left")
    folder = work / "closing"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)

    def failed(reason):
        return dict(rec, status="failed", reason=reason)

    base = stitch._graded(api, work, board.parent, output, "before", folder="closing-drc")
    if base is None:
        return failed("KiCad's DRC of the routed board failed")
    rec["open_before"] = rec["open_after"] = len(base.get("unconnected_items") or [])
    current, report, left = output, base, float(seconds)
    for index, strategy in enumerate(STRATEGIES):
        nets = [net for net in open_nets(report) if in_scope is None or in_scope(net)]
        # the time is shared among the rounds that are not marked "spare"; a spare round has what they left
        sharing = sum(1 for later in STRATEGIES[index:] if not later.get("spare"))
        share = left if strategy.get("spare") else left / sharing
        if not nets:
            break
        if share < MIN_S:
            continue
        entry = {"round": index + 1, "strategy": strategy["name"], "open": _counts(report)[0], "nets": nets,
                 "budget_s": int(share)}
        along = [net for net in sorted(pour_nets) if net not in nets] if strategy.get("pours") else []
        if along:
            entry["pour_nets"] = along
        rec["rounds"].append(entry)
        api.copy_project(board.parent, folder / f"round-{index + 1}")
        trial = folder / f"round-{index + 1}" / board.name
        shutil.copyfile(current, trial)
        routed = route_again(f"close-{index + 1}", trial, nets + along, int(share), strategy)
        if routed.get("command"):
            rec["commands"].append(routed["command"])
        left -= routed.get("used_s") or share
        if routed["status"] != "routed":
            entry["result"] = f"the nets could not be routed again ({routed.get('reason')})"
            continue
        again = Path(routed["output"])
        swept = common.run_cmd([api.pcbnew_python(), api.KICAD_SCRIPTS / "dangling.py", "sweep", before, again],
                               work, folder / f"dangling-{index + 1}.log", timeout=900)
        rec["commands"].append(common.public(swept))
        if swept["exit_code"] != 0:
            return failed(f"could not look for dangling copper after a closing round (see {swept['log']})")
        if settle is not None:
            commands, settled_s = settle(f"close-{index + 1}", again, max(0.0, left))
            rec["commands"] += commands
            left -= settled_s
            if settled_s:
                entry["settle_s"] = round(settled_s, 1)
        after = stitch._graded(api, work, board.parent, again, f"after-{index + 1}", folder="closing-drc")
        if after is None:
            return failed("KiCad's DRC of the board after a closing round failed")
        (open_before, found_before), (open_after, found_after) = _counts(report), _counts(after)
        entry["drc_counts"] = {"before": [open_before, found_before], "after": [open_after, found_after]}
        if not better((open_before, found_before), (open_after, found_after)) or (
                set(open_nets(after)) - set(open_nets(report))):
            entry["result"] = "not the better for it; the board stays as it was"
            continue
        entry["result"] = "kept"
        current, report = again, after
    rec["used_s"] = round(float(seconds) - left, 1)
    if current == output:
        return dict(rec, status="kept" if rec["rounds"] else "none")
    shutil.copyfile(current, output)
    return dict(rec, status="closed", open_after=_counts(report)[0])
