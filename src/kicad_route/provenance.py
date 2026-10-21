"""Copper provenance for `kicad-route run --protect human`, and the completion ratchet.

A `--dir` chain routes from an earlier run's candidate. With `--protect existing`
every track and via on a step's input is locked, including the copper earlier
kicad-route steps placed, so a late step cannot make room. `--protect human`
locks only the design's own copper:

    human   every track/via of the ORIGIN run's input (the start of the chain), plus,
            failing closed, any input copper that no step can be shown to have placed
            (for example a trace the designer edited between steps)
    agent   input copper that is not in the origin's input and that some step of the
            chain delivered as new copper (its candidate minus its own input)

No tag is written to the board. Items are keyed exactly as kicad/locks.py keys
them (net, layer ids, width/size, end points), multiset-aware: two identical
segments are two items. The record goes to <run>/provenance.json and the lock
allowlist to <run>/facts/human-keys.json; grading designates the same human items
as protected copper. When the input is not a kicad-route candidate, `human`
behaves exactly like `existing` (all input copper is the design's).

The completion ratchet (`net_completion`, every protect mode): a net that was fully
connected in the graded input (no unconnected item names it in the baseline DRC)
must still be connected in the candidate.
"""
import collections
import json
from pathlib import Path

from . import common

KICAD_SCRIPTS = Path(__file__).resolve().parent / "kicad"
PROVENANCE = "provenance.json"
HUMAN_KEYS = "facts/human-keys.json"


def board_keys(board, out, log):
    """`locks.py keys`: {"layer_ids": {...}, "keys": [...]} of a board, or None."""
    rec = common.run_cmd([common.PCBNEW_PYTHON, KICAD_SCRIPTS / "locks.py", "keys", board, out], board.parent,
                         log, timeout=600)
    return common.load_json(out) if rec["exit_code"] == 0 else None


def facts_key(item, layer_ids):
    """The locks.py key of a facts.py track/via record (layer names mapped to the board's ids)."""
    if "position" in item:
        return ["via", item["net"], *item["position"], item["diameter"], item["drill"],
                *(layer_ids.get(n) for n in item["layers"])]
    a, b = sorted([tuple(item["start"]), tuple(item["end"])])
    return ["track", item["net"], layer_ids.get(item["layer"]), item["width"], *a, *b, *item.get("mid", [])]


def facts_keys(facts, layer_ids):
    return [facts_key(t, layer_ids) for t in facts["tracks"] + facts["vias"]]


def counter(keys):
    return collections.Counter(json.dumps(k) for k in keys)


def summary(keys, with_keys=True):
    rec = {"tracks": sum(1 for k in keys if k[0] == "track"), "vias": sum(1 for k in keys if k[0] == "via")}
    rec["nets"] = sorted({k[1] for k in keys if k[1]})
    if with_keys:
        rec["keys"] = keys
    return rec


def prepare(run_dir, attempt, steps):
    """Classify the input copper of run_dir for --protect human and set the effective
    protection in attempt["settings"]. steps: the --dir chain from this run back to its
    origin (cli.resolve_origin), steps[0] being this run. Writes provenance.json."""
    settings = attempt["settings"]
    settings["protect_requested"] = "human"
    board = attempt["board"]
    origin_dir, origin_attempt = steps[-1]
    rec = {"kind": "copper-provenance", "requested_protect": "human",
           "chain": [common.rel(d) for d, _ in steps], "origin_run": common.rel(origin_dir)}

    def as_existing(reason):
        settings["protect"] = "existing"
        rec.update(effective_protect="existing", reason=reason)
        common.write_json(run_dir / PROVENANCE, rec)
        return rec

    rec["input_is_candidate"] = len(steps) > 1
    if len(steps) == 1:
        rec["origin_run"] = None
        return as_existing("the input is not a kicad-route candidate, so all of its copper is the design's: "
                           "--protect human behaves exactly like --protect existing")
    facts_dir = run_dir / "facts"
    inp = board_keys(run_dir / attempt["reference_dir"] / board, facts_dir / "input-keys.json",
                     facts_dir / "input-keys.log")
    org = board_keys(origin_dir / origin_attempt["reference_dir"] / board, facts_dir / "origin-keys.json",
                     facts_dir / "origin-keys.log")
    if inp is None or org is None:
        return as_existing("could not read the copper keys of the input or the origin board (see facts/*-keys.log); "
                           "all input copper locked")
    layer_ids = inp["layer_ids"]
    ref_facts = common.load_json(run_dir / attempt["reference_facts"])
    if ref_facts is None or counter(facts_keys(ref_facts, layer_ids)) != counter(inp["keys"]):
        return as_existing("board facts and lock keys of the input disagree, so grading could not designate "
                           "the same items that are locked; all input copper locked")

    # Copper each earlier step delivered: its graded candidate minus its own input.
    produced, unknown = collections.Counter(), []
    for d, a in steps[1:]:
        ref = common.load_json(d / a["reference_facts"])
        cand = common.load_json(d / "validate" / "candidate-facts.json")
        if ref is None or cand is None:
            unknown.append(common.rel(d))
            continue
        produced += counter(facts_keys(cand, layer_ids)) - counter(facts_keys(ref, layer_ids))

    origin_left, produced_left = counter(org["keys"]), collections.Counter(produced)
    origin_copper, agent, unattributed = [], [], []
    for k in inp["keys"]:
        s = json.dumps(k)
        if origin_left[s] > 0:
            origin_left[s] -= 1
            origin_copper.append(k)
        elif produced_left[s] > 0:
            produced_left[s] -= 1
            agent.append(k)
        else:
            unattributed.append(k)
    human = origin_copper + unattributed
    common.write_json(run_dir / HUMAN_KEYS, human)
    settings["protect_only"] = HUMAN_KEYS
    settings["provenance"] = PROVENANCE
    rec.update(effective_protect="human", layer_ids=layer_ids,
               input={"tracks": sum(1 for k in inp["keys"] if k[0] == "track"),
                      "vias": sum(1 for k in inp["keys"] if k[0] == "via")},
               human=summary(human), agent=summary(agent),
               origin_copper=summary(origin_copper, with_keys=False),
               unattributed=summary(unattributed),
               origin_copper_missing_from_input=sum(origin_left.values()),
               steps_without_candidate_facts=unknown)
    rec["detail"] = (f"{len(human)} human items locked ({len(origin_copper)} from the origin's input, "
                     f"{len(unattributed)} not traceable to any step), {len(agent)} agent items left unlocked")
    common.write_json(run_dir / PROVENANCE, rec)
    return rec


def designation(run_dir, attempt, ref_facts):
    """For grading a --protect human run: the uuids of the reference items that are human
    copper, and the nets that carried agent copper (the backend may rip and reroute those).
    Fails closed: without a usable provenance record every input item is designated."""
    prov = common.load_json(run_dir / attempt["settings"].get("provenance", PROVENANCE))
    items = ref_facts["tracks"] + ref_facts["vias"]
    if not prov or prov.get("effective_protect") != "human":
        return {"uuids": {t["uuid"] for t in items}, "agent_nets": set(),
                "note": "provenance.json missing or not human: all input copper designated"}
    left = counter(prov["human"]["keys"])
    uuids = set()
    for t in items:
        s = json.dumps(facts_key(t, prov["layer_ids"]))
        if left[s] > 0:
            left[s] -= 1
            uuids.add(t["uuid"])
    return {"uuids": uuids, "agent_nets": set(prov["agent"]["nets"]),
            "unmatched_human_keys": sum(left.values())}


def net_completion(reference, candidate):
    """Preserve every original native terminal equivalence, including partial nets."""
    from . import native_components
    check = {"check": "net_completion", "differences": [], "split_components": [], "nets_lost": []}
    if not native_components.compatible(reference, candidate):
        check.update(status=common.NOT_RUN, nets_connected_in_input=None,
                     detail="complete source-bound native terminal coverage is missing, stale or incompatible")
        return check
    connected = {net for net, groups in reference["nets"].items() if len(groups) == 1}
    splits = []
    for net, groups in reference["nets"].items():
        after = [set(g) for g in candidate["nets"][net]]
        for group in groups:
            before = set(group)
            if not any(before <= other for other in after):
                splits.append({"net": net, "original_terminals": group,
                               "candidate_components": [sorted(other) for other in after if before & other]})
    lost = sorted(connected & {s["net"] for s in splits})
    check.update(status=common.FAIL if splits else common.PASS,
                 detail=(f"{len(splits)} original terminal components split; {len(lost)} of "
                         f"{len(connected)} originally complete nets lost connectivity" if splits else
                         f"every original terminal equivalence preserved; {len(connected)} originally complete nets"),
                 nets_connected_in_input=len(connected), nets_lost=lost,
                 split_components=splits, differences=splits)
    return check
