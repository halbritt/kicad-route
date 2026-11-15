#!/usr/bin/env python3
"""Remove the copper a routing run left ending in the air.

    dangling.py sweep BEFORE.kicad_pcb ROUTED.kicad_pcb    (ROUTED in place)

A router that runs out of time, or gives a connection up, can leave the piece it had laid:
a track from a pad to nowhere, a via with a track on one side only. KiCad's DRC reports each
as `track_dangling` or `via_dangling`, kicad-route counts every new finding against the
candidate, and a board with a dozen connections open and nothing else wrong came back as one
not to adopt because of one 0.2 mm stub.

`sweep` removes tracks and vias of ROUTED that BEFORE does not have and that KiCad's
own test calls dangling, unless removing the whole item would disconnect its copper neighbours.
A dangling end can belong to a track with a connected branch in its middle: that track stays,
with its native warning, until a separate repair trims the tail. The sweep then removes what
dangles once the safe removals are gone, until nothing more can be removed. It asks
on a copy held in memory with the zones refilled, as the candidate is graded, so a track that
ends in a pour of its net is connected and stays. Copper of BEFORE is never touched: it is
matched by exact geometry, as locks.py matches it, so a backend that rewrites UUIDs is no
obstacle. ROUTED is saved only when something was removed, and without the refill.
Runs with KiCad's pcbnew module.
"""
import json
import sys

import locks

REPORTED = 200      # removed items listed in the record; the counts are of all of them


def new_items(board, had, pcbnew):
    """The tracks and vias of `board` that the multiset `had` (of locks.py keys) does not cover."""
    left, new = dict(had), []
    for item in board.GetTracks():
        key = json.dumps(locks.key(item, pcbnew))
        if left.get(key, 0) > 0:
            left[key] -= 1
        else:
            new.append(item)
    return new


def copper_graph(board, connectivity):
    """Direct native contacts between tracks, vias and pads, without merging zone islands."""
    items = list(board.GetTracks()) + [p for fp in board.GetFootprints() for p in fp.Pads()]
    graph = {item.m_Uuid.AsString(): set() for item in items}
    for item in items:
        key = item.m_Uuid.AsString()
        neighbours = list(connectivity.GetConnectedTracks(item)) + list(connectivity.GetConnectedPads(item))
        for other in neighbours:
            other_key = other.m_Uuid.AsString()
            if other_key != key and other_key in graph and other.GetNetCode() == item.GetNetCode():
                graph[key].add(other_key)
                graph[other_key].add(key)
    return graph


def removable(graph, key):
    """The item's remaining neighbours must still reach each other without it."""
    neighbours = graph[key]
    if len(neighbours) < 2:
        return True
    pending = [next(iter(neighbours))]
    seen = {key}
    needed = set(neighbours)
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        needed.discard(node)
        if not needed:
            return True
        pending.extend(graph[node] - seen)
    return False


def sweep(before_path, routed_path, pcbnew):
    had = locks.multiset(locks.key(item, pcbnew) for item in pcbnew.LoadBoard(before_path).GetTracks())
    judged = pcbnew.LoadBoard(routed_path)
    refilled = bool(pcbnew.ZONE_FILLER(judged).Fill(judged.Zones())) if judged.Zones() else True
    new = new_items(judged, had, pcbnew)
    result = {"new_items": len(new), "removed_tracks": 0, "removed_vias": 0, "rounds": 0, "refilled": refilled}
    doomed, removed = set(), []
    while new:
        judged.BuildConnectivity()
        connectivity = judged.GetConnectivity()
        # True: a track that lies wholly in a pad is no stub, as in KiCad's own DRC
        dangling = [item for item in new if connectivity.TestTrackEndpointDangling(item, True)]
        if not dangling:
            break
        graph = copper_graph(judged, connectivity)
        removed_this_round = 0
        for item in dangling:
            key = item.m_Uuid.AsString()
            if not removable(graph, key):
                continue
            # Update after each removal: two individually redundant tracks need
            # not be redundant when removed together.
            for neighbour in graph.pop(key):
                graph[neighbour].discard(key)
            result["removed_vias" if isinstance(item, pcbnew.PCB_VIA) else "removed_tracks"] += 1
            doomed.add(key)
            removed.append(locks.key(item, pcbnew))
            judged.RemoveNative(item)
            new.remove(item)
            removed_this_round += 1
        if not removed_this_round:
            break
        result["rounds"] += 1
    if doomed:
        board = pcbnew.LoadBoard(routed_path)      # as the backend wrote it: no refill is saved
        for item in list(board.GetTracks()):
            if item.m_Uuid.AsString() in doomed:
                board.RemoveNative(item)
        board.Save(routed_path)
    result["removed"] = removed[:REPORTED]      # locks.py keys: kind, net, layer or size, end points (nm)
    return result


def main(argv):
    import pcbnew
    if len(argv) != 3 or argv[0] != "sweep":
        sys.exit(__doc__)
    print("DANGLING_JSON: " + json.dumps(sweep(argv[1], argv[2], pcbnew)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
