#!/usr/bin/env python3
"""Read effective native pair settings and planar copper lengths, without saving."""
import json
import sys
from pathlib import Path


def main(board_path, output):
    import pcbnew

    board = pcbnew.LoadBoard(board_path)
    settings = board.GetDesignSettings().m_NetSettings
    nets = {}
    for name in sorted(str(n) for n in board.GetNetsByName() if str(n)):
        nc = settings.GetEffectiveNetClass(name)
        nets[name] = {
            "netclass": nc.GetName(),
            "width": int(nc.GetDiffPairWidth()) if nc.HasDiffPairWidth() else None,
            "gap": int(nc.GetDiffPairGap()) if nc.HasDiffPairGap() else None,
            "clearance": int(nc.GetClearance()) if nc.HasClearance() else None,
            "length": 0,
            "pad_count": 0,
        }
    for track in board.GetTracks():
        if not isinstance(track, pcbnew.PCB_VIA) and track.GetNetname() in nets:
            nets[track.GetNetname()]["length"] += round(track.GetLength())
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetname() in nets:
                nets[pad.GetNetname()]["pad_count"] += 1
    Path(output).write_text(json.dumps(nets, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main(*sys.argv[1:])
