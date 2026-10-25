"""Regenerate the layered nominal-delay article with native KiCad 10."""
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys

import pcbnew as k

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / 'group_matching'
prepared = ROOT / 'prepared.kicad_pcb'
# KiCad 10.0.7 cannot safely walk a board again after Remove(); isolate stripping.
subprocess.run([sys.executable, '-c', """import sys,pcbnew as k
b=k.LoadBoard(sys.argv[1])
for f in list(b.GetFootprints()):
 if f.GetReference() in ('J6','J7','J8'): b.Remove(f)
k.SaveBoard(sys.argv[2],b,True)
""", str(SOURCE / 'group.kicad_pcb'), str(prepared)], check=True)
board = k.LoadBoard(str(prepared))
board.SetCopperLayerCount(6)
terminals = []
for fp in list(board.GetFootprints()):
    if fp.GetReference() in ('J3', 'J4'):
        pad = next(iter(fp.Pads()))
        pad.SetAttribute(k.PAD_ATTRIB_SMD)
        pad.SetDrillSize(k.VECTOR2I(0, 0))
        layers = k.LSET()
        for layer in (k.F_Cu, k.F_Mask, k.F_Paste):
            layers.AddLayer(layer)
        pad.SetLayerSet(layers)
        fp.SetAttributes(k.FP_SMD)
        fp.SetFPID(k.LIB_ID('Delay', 'Terminal'))
        (ROOT / 'Delay.pretty').mkdir(exist_ok=True)
        terminals.append(fp)
ground = k.NETINFO_ITEM(board, 'GND'); board.Add(ground)
next(p for f in board.GetFootprints() if f.GetReference() == 'J5' for p in f.Pads()).SetNet(ground)
net = board.GetNetsByName()['DQ1']
for start, end, layer in [((40, 50), (42, 50), k.F_Cu),
                           ((42, 50), (86, 50), k.In2_Cu),
                           ((86, 50), (88, 50), k.F_Cu)]:
    track = k.PCB_TRACK(board)
    track.SetStart(k.VECTOR2I(*(k.FromMM(v) for v in start)))
    track.SetEnd(k.VECTOR2I(*(k.FromMM(v) for v in end)))
    track.SetWidth(k.FromMM(.25)); track.SetLayer(layer); track.SetNet(net)
    board.Add(track)
for x in (42, 86):
    via = k.PCB_VIA(board); via.SetPosition(k.VECTOR2I(k.FromMM(x), k.FromMM(50)))
    via.SetWidth(k.FromMM(.6)); via.SetDrill(k.FromMM(.3))
    via.SetLayerPair(k.F_Cu, k.B_Cu); via.SetNet(net); board.Add(via)
for layer in (k.In1_Cu, k.In3_Cu):
    zone = k.ZONE(board); zone.SetLayer(layer); zone.SetNet(ground)
    zone.SetLocalClearance(k.FromMM(.2)); zone.SetPadConnection(k.ZONE_CONNECTION_FULL)
    zone.SetMinThickness(k.FromMM(.15)); zone.Outline().NewOutline()
    for x, y in ((31,31),(109,31),(109,79),(31,79)):
        zone.Outline().Append(k.FromMM(x), k.FromMM(y))
    board.Add(zone)
path = ROOT / 'delay.kicad_pcb'; k.SaveBoard(str(path), board)
# Explicit native stackup; equal Er gives stripline a geometry-independent
# nominal velocity. Outer delay uses the width-dependent microstrip formula.
layers = ['F.Cu', 'In1.Cu', 'In2.Cu', 'In3.Cu', 'In4.Cu', 'B.Cu']
heights = [.2, .2, .59, .2, .2]
rows = []
for i, layer in enumerate(layers):
    rows.append(f'(layer "{layer}" (type "copper") (thickness 0.035))')
    if i < 5:
        rows.append(f'(layer "dielectric {i+1}" (type "core") (thickness {heights[i]}) '
                    '(material "FR4 qualification assumption") (epsilon_r 4) (loss_tangent 0.02))')
text = path.read_text()
text = text.replace('(setup', '(setup\n(stackup\n' + '\n'.join(rows) + '\n)', 1)
path.write_text(text)
project = json.loads((SOURCE / 'group.kicad_pro').read_text())
project['net_settings']['meta']['version'] = 5
default = project['net_settings']['classes'][0]
default.update(priority=2147483647, tuning_profile='nominal-qualification')
outer_er = (4+1)/2 + (4-1)/2 / math.sqrt(1+12/(.25/.2))
outer = math.sqrt(outer_er) / .299792458
inner = math.sqrt(4) / .299792458
entries = []
for layer in ('F.Cu', 'In2.Cu', 'B.Cu'):
    entries.append(dict(signal_layer=layer, top_reference_layer='In1.Cu' if layer=='In2.Cu' else 'UNDEFINED',
        bottom_reference_layer='In3.Cu' if layer=='In2.Cu' else 'In1.Cu' if layer=='F.Cu' else 'In4.Cu',
        width=250000, diff_pair_gap=0, delay=round((inner if layer=='In2.Cu' else outer)*1e6)))
project['tuning_profiles'] = dict(meta=dict(version=0), tuning_profiles_impedance_geometric=[
    dict(profile_name='nominal-qualification', type=0, target_impedance=0,
         enable_time_domain_tuning=True, layer_entries=entries, via_prop_delay=round(inner*1e6), via_overrides=[])])
(ROOT / 'delay.kicad_pro').write_text(json.dumps(project, indent=2)+'\n')
(ROOT / 'delay.kicad_dru').write_text('(version 1)\n'
    '(rule "signal width floor" (constraint track_width (min 0.25mm)))\n'
    '(rule "required nominal delay skew" (condition "A.NetName == \'DQ0\' || A.NetName == \'DQ1\'") '
    '(constraint skew (max 1ps)))\n')
shutil.copytree(SOURCE / 'Connector_PinHeader_2.54mm.pretty', ROOT / 'Connector_PinHeader_2.54mm.pretty', dirs_exist_ok=True)
(ROOT / 'fp-lib-table').write_text((SOURCE / 'fp-lib-table').read_text().rstrip()[:-1] +
    '\n(lib (name "Delay")(type "KiCad")(uri "${KIPRJMOD}/Delay.pretty")(options "")(descr "qualification terminal"))\n)\n')
print(dict(outer_ps_per_mm=outer, inner_ps_per_mm=inner))

io = k.PCB_IO_MGR.FindPlugin(k.PCB_IO_MGR.KICAD_SEXP)
io.FootprintSave(str(ROOT / 'Delay.pretty'), terminals[0])

prepared.unlink()

# Normalize native defaults once at authoring, before preservation is graded.
subprocess.run([sys.executable, '-c', """import sys,pcbnew as k
b=k.LoadBoard(sys.argv[1]);k.SaveBoard(sys.argv[1],b,True)
""", str(path)], check=True)
