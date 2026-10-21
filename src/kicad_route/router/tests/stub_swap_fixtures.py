"""Synthetic native-route control adapted from upstream #1261."""
from synth import RoutingBoard


def rescue_board(lock=True):
    """NET_A: P1's locked F.Cu stub sits inside a locked B.Cu ring of NET_C
    that admits a via only at P1's exact (off-grid) centre -- where the
    rescue's own pad via goes, and where the router's grid cannot -- and the
    route is restricted to In1/In2, so the F.Cu stub must leave F.Cu."""
    b = RoutingBoard(layers=('F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'))
    px, py, ex = 10.05, 15.05, 14.05
    b.pad('P1', px, py, 'NET_A')
    b.pad('P2', 30, 15, 'NET_A')
    b.seg(px, py, ex, py, 'NET_A', locked=lock)
    b.seg(30, 15, 26, 15, 'NET_A')
    d = 0.25 + 0.25 + 0.15 + 0.01
    b.ring([(round(px - d, 4), round(py - d, 4)), (round(ex + d, 4), round(py - d, 4)),
            (round(ex + d, 4), round(py + d, 4)), (round(px - d, 4), round(py + d, 4))],
           'NET_C', w=0.3, layer='B.Cu', locked=True)
    b.pad('C1', 4, 15, 'NET_C', sx=1.6, sy=1.6, tht=True)
    b.pad('C2', 4, 25, 'NET_C', sx=1.6, sy=1.6, tht=True)
    b.seg(4, 15, round(px - d, 4), round(py - d, 4), 'NET_C', layer='B.Cu', locked=True)
    b.seg(4, 15, 4, 25, 'NET_C', layer='B.Cu', locked=True)
    return b.text(), ['--nets', '*', '--layers', 'In1.Cu', 'In2.Cu']
