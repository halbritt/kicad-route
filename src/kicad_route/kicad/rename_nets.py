#!/usr/bin/env python3
"""Rename nets of a board through KiCad's own loader (for the canary).

    rename_nets.py BOARD.kicad_pcb MAP.json

MAP.json is {"old name": "new name"}. The board is saved in place. Prints
RENAME_JSON: {"renamed": {...}, "missing": [...]}.
"""
import json
import sys


def main(board_path, map_path):
    import pcbnew
    mapping = json.loads(open(map_path).read())
    board = pcbnew.LoadBoard(board_path)
    renamed, missing = {}, []
    for old, new in mapping.items():
        net = board.FindNet(old)
        if net is None:
            missing.append(old)
            continue
        net.SetNetname(new)
        renamed[old] = new
    board.Save(board_path)
    print("RENAME_JSON: " + json.dumps({"renamed": renamed, "missing": missing}))
    return 1 if missing else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(*sys.argv[1:]))
