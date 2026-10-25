"""Native routing qualification article: two unequal coupled pair paths."""
from pathlib import Path
import json,sys,subprocess
import pcbnew as k
root=Path(__file__).resolve().parent
b=k.BOARD();b.SetCopperLayerCount(4)
point=lambda x,y:k.VECTOR2I(k.FromMM(x),k.FromMM(y))
for a,z in [((35,25),(110,25)),((110,25),(110,75)),((110,75),(35,75)),((35,75),(35,25))]:
 edge=k.PCB_SHAPE(b);edge.SetShape(k.SHAPE_T_SEGMENT);edge.SetLayer(k.Edge_Cuts);edge.SetStart(point(*a));edge.SetEnd(point(*z));edge.SetWidth(k.FromMM(.05));b.Add(edge)
nets={}
for name in ('A_P','A_N','B_P','B_N'):
 net=k.NETINFO_ITEM(b,name);b.Add(net);nets[name]=net
library=root/'PairQualification.pretty';library.mkdir(exist_ok=True)
for ref,x,y,prefix in [('J1',40,40,'A'),('J2',90,40,'A'),('J3',40,60,'B'),('J4',100,60,'B')]:
 fp=k.FOOTPRINT(b);fp.SetReference(ref);fp.SetValue('Qualification terminal pair');fp.SetFPID(k.LIB_ID('PairQualification','Terminal2'));fp.SetAttributes(k.FP_SMD);fp.SetPosition(point(x,y));fp.SetLocked(True);b.Add(fp);fp.Reference().SetPosition(point(x,y-2));fp.Value().SetVisible(False)
 for i,suffix in enumerate(('P','N')):
  pad=k.PAD(fp);pad.SetNumber(str(i+1));pad.SetAttribute(k.PAD_ATTRIB_SMD);pad.SetShape(k.PAD_SHAPE_RECT);pad.SetSize(point(.25,.25));layers=k.LSET()
  for layer in (k.F_Cu,k.F_Mask,k.F_Paste):layers.AddLayer(layer)
  pad.SetLayerSet(layers);pad.SetPosition(point(x,y+i*.6));pad.SetNet(nets[prefix+'_'+suffix]);fp.Add(pad)
 if ref=='J1':k.PCB_IO_MGR.FindPlugin(k.PCB_IO_MGR.KICAD_SEXP).FootprintSave(str(library),fp)
p=root/'pairs.kicad_pcb';k.SaveBoard(str(p),b)
source=root.parent/'pair-skew/board.kicad_pro'
project=json.loads(source.read_text());project['net_settings']['netclass_patterns']=[dict(netclass='CAN',pattern=name) for name in nets]
(root/'pairs.kicad_pro').write_text(json.dumps(project,indent=2)+'\n')
(root/'pairs.kicad_dru').write_text('''(version 1)
(rule "required pair width" (condition "A.NetClass == 'CAN'") (constraint track_width (min 0.25mm) (max 0.25mm)))
(rule "required coupled gap" (condition "A.NetClass == 'CAN'") (constraint diff_pair_gap (min 0.34mm) (max 0.36mm)))
(rule "required intra-pair skew" (condition "A.NetClass == 'CAN'") (constraint skew (max 0.1mm) (within_diff_pairs)))
(rule "required pair group skew" (condition "A.NetClass == 'CAN'") (constraint skew (max 0.15mm)))
(rule "bounded uncoupled extent" (condition "A.NetClass == 'CAN'") (constraint diff_pair_uncoupled (max 3mm)))
''')
(root/'fp-lib-table').write_text('(fp_lib_table (version 7) (lib (name "PairQualification")(type "KiCad")(uri "${KIPRJMOD}/PairQualification.pretty")(options "")(descr "qualification terminal pair")))\n')
subprocess.run([sys.executable,'-c','import pcbnew as k,sys;b=k.LoadBoard(sys.argv[1]);k.SaveBoard(sys.argv[1],b,True)',str(p)],check=True)
(root/'README.md').write_text('Agent-selected software qualification assumptions: four layers; fixed SMD terminal pairs with 50 and 60 mm direct paths, 0.25 mm width/0.35 mm gap, 0.15 mm group skew, 0.1 mm intra-pair skew, 3 mm maximum uncoupled extent (5 percent of the approximately 60 mm useful path). No impedance, circuit, assembly or manufacturing acceptance is claimed. Native group skew checks all four useful paths, a stated requirement, not an average of paired legs.\n')
print(root)
