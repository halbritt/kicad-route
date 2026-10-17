#!/usr/bin/env python3
"""Temporarily lock a board's existing copper for a routing backend, then restore.

    locks.py lock BOARD.kicad_pcb RECORD.json [--only KEYS.json]
                                                   lock unlocked tracks/vias (in place)
    locks.py restore BOARD.kicad_pcb RECORD.json   unlock exactly those items again (in place)
    locks.py keys BOARD.kicad_pcb OUT.json         write the key of every track/via

Backends treat locked copper as untouchable, but some of their recovery paths
rework unlocked input copper even when told to keep it (KRT's net rescue moves
stubs between layers). For `kicad-route run --protect existing` the wrapper locks the
input's unlocked tracks and vias in the work copy, routes, and then restores the
original lock flags on the backend's output. Items are matched by exact
geometry (layer(s), net, width/size, end points), so a backend that rewrites
UUIDs still gets its flags restored. Runs with KiCad's pcbnew module.

lock --only KEYS.json (a JSON list of keys as `keys` writes them): lock only the
unlocked items whose key is in the list, each listed key at most as many times
as it appears (multiset, like restore). `--protect human` uses it to lock the
design's own copper and leave copper an earlier kicad-route step placed free.

keys: {"layer_ids": {layer name: id}, "keys": [key, ...]} in board order, so
the wrapper can compute provenance in exactly the key space lock and restore
match in, and convert facts.py records into it.
"""
import json
import sys


def key(t, pcbnew):
    net = t.GetNetname()
    if isinstance(t, pcbnew.PCB_VIA):
        p = t.GetPosition()
        return ["via", net, int(p.x), int(p.y), int(t.GetWidth(pcbnew.F_Cu)), int(t.GetDrillValue()),
                int(t.TopLayer()), int(t.BottomLayer())]
    a, b = sorted([(int(t.GetStart().x), int(t.GetStart().y)), (int(t.GetEnd().x), int(t.GetEnd().y))])
    mid = []
    if isinstance(t, pcbnew.PCB_ARC):
        m = t.GetMid()
        mid = [int(m.x), int(m.y)]
    return ["track", net, int(t.GetLayer()), int(t.GetWidth()), *a, *b, *mid]


def multiset(keys):
    counts = {}
    for k in keys:
        counts[json.dumps(k)] = counts.get(json.dumps(k), 0) + 1
    return counts


def main(argv):
    import pcbnew
    only = None
    if len(argv) == 5 and argv[0] == "lock" and argv[3] == "--only":
        only = multiset(json.load(open(argv[4])))
        argv = argv[:3]
    if len(argv) != 3 or argv[0] not in ("lock", "restore", "keys"):
        sys.exit(__doc__)
    board_path, record = argv[1], argv[2]
    board = pcbnew.LoadBoard(board_path)
    if argv[0] == "keys":
        layer_ids = {board.GetLayerName(l): int(l) for l in board.GetEnabledLayers().CuStack()}
        keys = [key(t, pcbnew) for t in board.GetTracks()]
        with open(record, "w") as f:
            json.dump({"layer_ids": layer_ids, "keys": keys}, f)
        print("LOCKS_JSON: " + json.dumps({"keys": len(keys)}))
        return 0
    if argv[0] == "lock":
        keys = []
        allowed = sum(only.values()) if only is not None else None
        for t in board.GetTracks():
            if t.IsLocked():
                continue
            if only is not None:
                k = json.dumps(key(t, pcbnew))
                if not only.get(k):
                    continue
                only[k] -= 1
            t.SetLocked(True)
            keys.append(key(t, pcbnew))
        board.Save(board_path)
        with open(record, "w") as f:
            json.dump(keys, f)
        info = {"locked": len(keys)}
        if only is not None:
            info.update(allowlist=allowed, allowlist_not_found=sum(only.values()))
        print("LOCKS_JSON: " + json.dumps(info))
        return 0
    wanted = multiset(json.load(open(record)))
    restored = 0
    for t in board.GetTracks():
        k = json.dumps(key(t, pcbnew))
        if t.IsLocked() and wanted.get(k):
            t.SetLocked(False)
            wanted[k] -= 1
            restored += 1
    board.Save(board_path)
    print("LOCKS_JSON: " + json.dumps({"restored": restored, "not_found": sum(wanted.values())}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
