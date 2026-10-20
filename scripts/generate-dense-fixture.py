#!/usr/bin/env python3
"""Generate a public synthetic multipoint crossing workload; no external libraries.

Run with the system KiCad Python and an empty destination. Twelve shuffled
connections share two copper layers, three also have a central testpoint, and a
track/via keepout forces paths around a wall. This is a benchmark workload, not
proof that every run enters a rescue pass; the run report establishes that.
"""
import argparse
import json
from pathlib import Path
import re
import uuid

import pcbnew

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = uuid.UUID('cd4f3b04-7828-4b1f-a93f-9fb6423c0849')


def generate(destination):
    destination.mkdir(parents=True)
    fixture = ROOT / 'fixtures/route_demo'
    board = pcbnew.LoadBoard(str(fixture / 'route_demo.kicad_pcb'))
    for item in [*board.GetTracks(), *board.GetFootprints(), *board.Zones()]:
        board.Delete(item)
    for net in list(board.GetNetsByName().values()):
        if net.GetNetCode():
            board.Delete(net)
    def xy(x, y):
        return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
    nets = []
    for index in range(12):
        net = pcbnew.NETINFO_ITEM(board, f'/D{index:02}')
        board.Add(net)
        nets.append(net)
    permutation = [8, 1, 10, 3, 6, 11, 0, 9, 4, 7, 2, 5]
    for ref, x, order in (('J1', 107, list(range(12))), ('J2', 143, permutation)):
        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference(ref)
        fp.SetPosition(xy(x, 105))
        board.Add(fp)
        for index, net_index in enumerate(order):
            pad = pcbnew.PAD(fp)
            pad.SetNumber(str(index+1))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(xy(.6, .6))
            layers = pcbnew.LSET()
            layers.AddLayer(pcbnew.F_Cu)
            pad.SetLayerSet(layers)
            pad.SetPosition(xy(x, 105 + index*1.5))
            pad.SetNet(nets[net_index])
            fp.Add(pad)
    for index in (1, 5, 9):
        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference(f'TP{index}')
        fp.SetPosition(xy(120 if index != 5 else 130, 104 + index*1.8))
        board.Add(fp)
        pad = pcbnew.PAD(fp)
        pad.SetNumber('1')
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(xy(.7, .7))
        layers = pcbnew.LSET()
        layers.AddLayer(pcbnew.F_Cu)
        pad.SetLayerSet(layers)
        pad.SetPosition(fp.GetPosition())
        pad.SetNet(nets[index])
        fp.Add(pad)
    wall = pcbnew.ZONE(board)
    wall.SetIsRuleArea(True)
    layers = pcbnew.LSET()
    layers.AddLayer(pcbnew.F_Cu)
    layers.AddLayer(pcbnew.B_Cu)
    wall.SetLayerSet(layers)
    wall.SetDoNotAllowTracks(True)
    wall.SetDoNotAllowVias(True)
    wall.SetDoNotAllowZoneFills(True)
    wall.SetZoneName('synthetic-crossing-wall')
    outline = wall.Outline()
    outline.NewOutline()
    for x, y in ((124, 109), (126, 109), (126, 120), (124, 120)):
        outline.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
    board.Add(wall)
    path = destination / 'dense.kicad_pcb'
    board.Save(str(path))
    # Every generated identity is stable; rerunning yields identical source.
    count = iter(range(10000))
    text = re.sub(r'\(uuid "[^"]+"\)',
                  lambda _: f'(uuid "{uuid.uuid5(NAMESPACE, str(next(count)))}")', path.read_text())
    path.write_text(text)
    project = json.loads((fixture / 'route_demo.kicad_pro').read_text())
    project['net_settings']['netclass_patterns'] = []
    (destination / 'dense.kicad_pro').write_text(json.dumps(project, indent=2) + '\n')
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    print(generate(parser.parse_args().destination))
