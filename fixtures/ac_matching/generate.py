"""Author a native capacitor-chain routing qualification article."""
from pathlib import Path
import json
import pcbnew as k

root=Path(__file__).resolve().parent
board=k.BOARD();board.SetCopperLayerCount(4)
point=lambda x,y:k.VECTOR2I(k.FromMM(x),k.FromMM(y))
for a,b in [((35,35),(110,35)),((110,35),(110,55)),((110,55),(35,55)),((35,55),(35,35))]:
    edge=k.PCB_SHAPE(board);edge.SetShape(k.SHAPE_T_SEGMENT);edge.SetLayer(k.Edge_Cuts)
    edge.SetStart(point(*a));edge.SetEnd(point(*b));edge.SetWidth(k.FromMM(.05));board.Add(edge)
nets={}
for name in ('DPA_P','DPA_N','DPB_P','DPB_N'):
    net=k.NETINFO_ITEM(board,name);board.Add(net);nets[name]=net
library=root/'AcQualification.pretty';library.mkdir(exist_ok=True)
parts=[('J1','Connector_PinHeader_2.54mm','PinHeader_1x02_P2.54mm_Vertical','Terminal2',40,45,['DPA_P','DPA_N']),
       ('J2','Connector_PinHeader_2.54mm','PinHeader_1x02_P2.54mm_Vertical','Terminal2',100,45,['DPB_P','DPB_N']),
       ('C1','Capacitor_SMD','C_0402_1005Metric','C0402',70,43,['DPA_P','DPB_P']),
       ('C2','Capacitor_SMD','C_0402_1005Metric','C0402',70,47,['DPA_N','DPB_N'])]
saved=set()
for ref,lib,footprint,local,x,y,net_names in parts:
    fp=k.FootprintLoad('/usr/share/kicad/footprints/'+lib+'.pretty',footprint)
    assert fp is not None,(lib,footprint)
    fp.SetReference(ref);fp.SetValue('100nF' if ref.startswith('C') else 'Qualification terminal')
    fp.SetFPID(k.LIB_ID('AcQualification',local));board.Add(fp);fp.SetPosition(point(x,y));fp.SetLocked(True)
    fp.Reference().SetPosition(point(x,y-2));fp.Value().SetVisible(False)
    for pad in fp.Pads():pad.SetNet(nets[net_names[int(pad.GetNumber())-1]])
    if local not in saved:k.PCB_IO_MGR.FindPlugin(k.PCB_IO_MGR.KICAD_SEXP).FootprintSave(str(library),fp);saved.add(local)
path=root/'chain.kicad_pcb';k.SaveBoard(str(path),board)
project=json.loads((root.parent/'pair-skew/board.kicad_pro').read_text())
project['net_settings']['netclass_patterns']=[dict(netclass='CAN',pattern=n) for n in nets]
cls=next(c for c in project['net_settings']['classes'] if c['name']=='CAN')
cls.update(track_width=.15,diff_pair_width=.15,diff_pair_gap=.25)
project['board'].setdefault('design_settings',{}).setdefault('rules',{})['min_track_width']=.1
(root/'chain.kicad_pro').write_text(json.dumps(project,indent=2)+'\n')
(root/'chain.kicad_dru').write_text('''(version 1)
(rule "required pair width" (condition "A.NetClass == 'CAN'") (constraint track_width (min 0.15mm) (max 0.15mm)))
(rule "required coupled gap" (condition "A.NetClass == 'CAN'") (constraint diff_pair_gap (min 0.24mm) (max 0.26mm)))
(rule "local skew budget" (condition "A.NetClass == 'CAN'") (constraint skew (max 0.2mm) (within_diff_pairs)))
(rule "terminal escape budget" (condition "A.NetClass == 'CAN'") (constraint diff_pair_uncoupled (max 4mm)))
''')
(root/'fp-lib-table').write_text('(fp_lib_table (version 7) (lib (name "AcQualification")(type "KiCad")(uri "${KIPRJMOD}/AcQualification.pretty")(options "")(descr "capacitor-chain qualification footprints")))\n')
(root/'README.md').write_text('''Software qualification article: four layers, two fixed 2.54mm terminal pairs and two populated 0402 100nF series capacitors. C1 bridges P and C2 bridges N between DPA and DPB. Each segment spans approximately30mm; capacitor pad separation is0.96mm and the P/N capacitor pitch is4mm. The native width/gap are0.15/0.25mm, local skew budget0.2mm, and terminal escape budget4mm persegment. The composed terminal-to-terminal copper P/N skew requirement is0.1mm. All are explicit agent-selected software qualification assumptions; no physical protocol, capacitor internal delay, impedance or manufacturing acceptance is asserted. Actual native useful-path contributions across both named segments must be measured after routing; backend success/skew flags cannot establish acceptance. Initial generation derives footprint geometry from installed KiCad libraries and retains the exact local dependencies.\n''')
print(path)
