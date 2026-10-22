from pathlib import Path
import pcbnew,sys
b=pcbnew.BOARD();b.SetCopperLayerCount(4)
for x,span in [(0,(pcbnew.F_Cu,pcbnew.B_Cu)),(5,(pcbnew.In1_Cu,pcbnew.B_Cu))]:
 v=pcbnew.PCB_VIA(b);v.SetPosition(pcbnew.VECTOR2I(x*1000000,0));v.SetViaType(pcbnew.VIATYPE_THROUGH if x==0 else pcbnew.VIATYPE_BLIND);v.SetLayerPair(*span);v.SetWidth(600000);v.SetDrill(300000);v.Padstack().SetMode(pcbnew.PADSTACK.MODE_CUSTOM)
 for layer,size in [(pcbnew.F_Cu,600000),(pcbnew.In1_Cu,900000),(pcbnew.In2_Cu,700000),(pcbnew.B_Cu,800000)]:v.SetWidth(layer,size)
 b.Add(v)
pcbnew.SaveBoard(sys.argv[1],b)
