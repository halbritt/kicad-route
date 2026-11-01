"""A via that joins tracks on one layer is replaced by track, not left dangling."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/kicad_route/router/py_router'))
from kicad_parser import BoardInfo, Pad, PCBData, Segment, Via
from pcb_modification import sweep_dangling_via_branches
from check_connected import check_net_connectivity

NET = 5


def pad(ref, x, y):
    return Pad(component_ref=ref, pad_number='1', global_x=x, global_y=y, local_x=0, local_y=0,
               size_x=.6, size_y=.6, shape='rect', layers=['F.Cu'], net_id=NET, net_name='/V3V3')


def track(ax, ay, bx, by, layer='F.Cu'):
    return Segment(ax, ay, bx, by, .2, layer, NET)


def board(segments, vias, pads):
    pcb = PCBData(BoardInfo({}, ['F.Cu', 'B.Cu']), {}, {}, [], segments, {})
    pcb.vias = vias
    pcb.pads_by_net = {NET: pads}
    pcb.zones = []
    return pcb


class ViaJoiningOneLayerTests(unittest.TestCase):
    def test_via_whose_copper_alone_joins_two_tracks_becomes_track(self):
        # Retained 2026-11-01 router trials left this shape: a trunk ends at the via, and the
        # next track ends 0.225 mm away, inside the via's copper. KiCad reports via_dangling.
        trunk, branch = track(0, 0, 10, 0), track(10, .225, 10, 2)
        via = Via(10, 0, .6, .3, ['F.Cu', 'B.Cu'], NET)
        pads = [pad('A', 0, 0), pad('B', 10, 2)]
        pcb = board([trunk, branch], [via], pads)
        added = []
        removed, dropped = sweep_dangling_via_branches(pcb, {NET}, added=added)
        self.assertEqual((removed, dropped), ([], [via]))
        self.assertEqual(pcb.vias, [])
        self.assertEqual([(s.start_x, s.start_y, s.end_x, s.end_y, s.layer) for s in added],
                         [(10, 0, 10, .225, 'F.Cu')])
        self.assertEqual(added[0].width, .2)
        self.assertIn(added[0], pcb.segments)
        connected = check_net_connectivity(NET, pcb.segments, pcb.vias, pads, [])
        self.assertFalse(connected.get('disconnected_pads'))

    def test_via_that_joins_two_layers_is_left_alone(self):
        front, back = track(0, 0, 10, 0), track(10, 0, 10, 2, 'B.Cu')
        via = Via(10, 0, .6, .3, ['F.Cu', 'B.Cu'], NET)
        pcb = board([front, back], [via], [pad('A', 0, 0), pad('B', 10, 2)])
        added = []
        self.assertEqual(sweep_dangling_via_branches(pcb, {NET}, added=added), ([], []))
        self.assertEqual((pcb.vias, added), ([via], []))

    def test_via_ending_a_dead_branch_is_still_removed_without_a_bridge(self):
        live, spur = track(0, 0, 10, 0), track(10, 0, 10, 2)
        via = Via(10, 2, .6, .3, ['F.Cu', 'B.Cu'], NET)
        pcb = board([live, spur], [via], [pad('A', 0, 0), pad('B', 10, 0)])
        added = []
        removed, dropped = sweep_dangling_via_branches(pcb, {NET}, added=added)
        self.assertEqual((removed, dropped, added), ([spur], [via], []))


if __name__ == '__main__':
    unittest.main()
