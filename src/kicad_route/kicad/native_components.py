#!/usr/bin/env python3
"""Read full terminal equivalences from the saved board's native connectivity graph.

Only terminal UUIDs identify a component: a zone parent may contain disconnected
fill islands. Query every electrical pad so asymmetric or incomplete associations
fail closed, including UUID-distinct pads with the same printed number.
"""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from facts import find, sexpr

SCHEMA = 1


def native_identity():
    import pcbnew
    import _pcbnew
    paths = {"module": _pcbnew.__file__, "binding": pcbnew.__file__, "python": sys.executable}
    return {"version": pcbnew.GetBuildVersion(),
            **{key: {"path": str(Path(path).resolve()),
                     "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()} for key, path in paths.items()}}


class QuotedAtom(str):
    """Keep an inline name like (net "0") distinct from legacy numeric net 0."""


def serialized_inventory(path):
    root = sexpr(path.read_text(), quoted=lambda token: QuotedAtom(json.loads(token)))[0]
    net_names = {n[1]: n[-1] for n in root if isinstance(n, list) and n and n[0] == "net"}
    inventory, copper_nets, jumpers = {}, {}, {}

    def net_name(node):
        net = find(node, "net")
        if net is None:
            return ""
        if isinstance(net[1], QuotedAtom):
            return str(net[1])
        if len(net) >= 3:
            return str(net[-1])
        if net[1] == "0":
            return ""
        if net[1] not in net_names:
            raise ValueError("serialized numeric net has no name binding")
        return str(net_names[net[1]])

    for node in root:
        if not isinstance(node, list) or not node:
            continue
        if node[0] == "footprint":
            fp_uuid = find(node, "uuid")[1]
            jumpers[fp_uuid] = {"duplicate_numbers": find(node, "duplicate_pad_numbers_are_jumpers"),
                                "groups": sorted(sorted(str(p) for p in group) for group in (find(node, "jumper_pad_groups") or [])[1:])}
            for pad in node:
                if isinstance(pad, list) and pad and pad[0] == "pad":
                    uid = find(pad, "uuid")[1]
                    if uid in inventory:
                        raise ValueError("duplicate serialized pad UUID")
                    inventory[uid] = {"net": net_name(pad), "number": str(pad[1]), "footprint": fp_uuid}
        elif node[0] in ("segment", "arc", "via", "zone"):
            copper_nets[find(node, "uuid")[1]] = net_name(node)
    return inventory, copper_nets, jumpers


def read(path):
    import pcbnew
    serialized, copper_nets, jumpers = serialized_inventory(path)
    board = pcbnew.LoadBoard(str(path))
    if board is None or not board.BuildConnectivity():
        raise ValueError("native board connectivity could not be built")
    pads = {p.m_Uuid.AsString(): p for fp in board.GetFootprints() for p in fp.Pads()}
    inventory = {uid: {"net": p.GetNetname(), "number": p.GetNumber(),
                       "footprint": p.GetParentFootprint().m_Uuid.AsString()} for uid, p in pads.items()}
    if inventory != serialized:
        raise ValueError("native pad inventory differs from serialized UUID/net inventory")
    native_copper = {it.m_Uuid.AsString(): it.GetNetname() for it in [*board.GetTracks(), *board.Zones()]}
    if native_copper != copper_nets:
        raise ValueError("native copper net assignments differ from the saved board")
    # SWIG exposes JumperPadGroups as an opaque pointer in KiCad 10.0.7.
    # Native serialization provides its effective value without reconstructing it.
    with tempfile.TemporaryDirectory(prefix="krt-native-jumpers-") as directory:
        saved = Path(directory) / path.name
        pcbnew.SaveBoard(str(saved), board, aSkipSettings=True)
        _, _, native_jumpers = serialized_inventory(saved)
    for fp in board.GetFootprints():
        uid = fp.m_Uuid.AsString()
        setting = jumpers[uid]
        duplicate = setting["duplicate_numbers"]
        if duplicate is not None and (duplicate[1] == "yes") != fp.GetDuplicatePadNumbersAreJumpers():
            raise ValueError("native jumper settings differ from serialized board")
        if setting["groups"] != native_jumpers[uid]["groups"]:
            raise ValueError("native jumper groups differ from serialized board")
        setting["duplicate_numbers"] = fp.GetDuplicatePadNumbersAreJumpers()
    conn, memberships = board.GetConnectivity(), {}
    for uid, pad in pads.items():
        if pad.GetNetCode() <= 0:
            continue  # native connectivity explicitly excludes non-electrical (net 0) pads
        connected = {it.m_Uuid.AsString() for it in conn.GetConnectedItems(pad, 0) if it.GetClass() == "PAD"}
        if uid not in connected or any(other not in pads or pads[other].GetNetCode() != pad.GetNetCode()
                                       for other in connected):
            raise ValueError(f"incomplete or foreign-net native association for pad {uid}")
        memberships[uid] = connected
    for uid, group in memberships.items():
        if any(memberships.get(other) != group for other in group):
            raise ValueError(f"asymmetric native association for pad {uid}")
    nets = {}
    for uid, group in memberships.items():
        net = inventory[uid]["net"]
        nets.setdefault(net, set()).add(tuple(sorted(group)))
    return {"schema": SCHEMA, "status": "PASS", "reasons": [],
            "pcbnew": pcbnew.GetBuildVersion(), "inventory": inventory, "jumpers": jumpers,
            "jumpers_sha256": hashlib.sha256(json.dumps(jumpers, sort_keys=True).encode()).hexdigest(),
            "method": {"components": "GetConnectedItems(pad, flags=0), every electrical pad",
                       "jumpers": "native SaveBoard(aSkipSettings=True) in isolated temporary directory"},
            "inventory_sha256": hashlib.sha256(json.dumps(serialized, sort_keys=True).encode()).hexdigest(),
            "nets": {n: [list(c) for c in sorted(groups)] for n, groups in sorted(nets.items())}}


def main(board, out):
    path = Path(board)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    tool = native_identity()
    try:
        facts = read(path)
    except (ValueError, RuntimeError, KeyError, IndexError) as exc:
        facts = {"schema": SCHEMA, "status": "NOT_RUN", "reasons": [str(exc)]}
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    if native_identity() != tool:
        facts.update(status="NOT_RUN", reasons=["native tool identity changed during extraction"])
    facts.update(board_sha256_before=before, board_sha256_after=after, native_identity=tool)
    Path(out).write_text(json.dumps(facts, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    if sys.argv[1:] == ["--identity"]:
        print(json.dumps(native_identity()))
    else:
        main(*sys.argv[1:])
