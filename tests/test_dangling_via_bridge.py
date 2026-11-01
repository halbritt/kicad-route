"""A via that joins tracks on one layer is replaced by track, not left dangling."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/kicad_route/router/py_router'))
from kicad_parser import BoardInfo, Pad, PCBData, Segment, Via
from pcb_modification import sweep_dangling_via_branches
from check_connected import check_net_connectivity
from kicad_writer import replace_copper_in_content

NET = 5
OTHER_NET = 6
CLEARANCE = .15


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
        removed, dropped = sweep_dangling_via_branches(pcb, {NET}, added=added, clearance=CLEARANCE)
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
        self.assertEqual(sweep_dangling_via_branches(pcb, {NET}, added=added, clearance=CLEARANCE), ([], []))
        self.assertEqual((pcb.vias, added), ([via], []))

    def test_via_ending_a_dead_branch_is_still_removed_without_a_bridge(self):
        live, spur = track(0, 0, 10, 0), track(10, 0, 10, 2)
        via = Via(10, 2, .6, .3, ['F.Cu', 'B.Cu'], NET)
        pcb = board([live, spur], [via], [pad('A', 0, 0), pad('B', 10, 0)])
        added = []
        removed, dropped = sweep_dangling_via_branches(pcb, {NET}, added=added, clearance=CLEARANCE)
        self.assertEqual((removed, dropped, added), ([spur], [via], []))

    def test_bridge_that_would_crowd_another_net_keeps_the_via(self):
        # Review counterexample: a wide radial bridge leaves the via disk and the wide track.
        trunk = Segment(-4, 0, 0, 0, 1.2, 'F.Cu', NET)
        crossing = Segment(1.18, -4, 1.18, 4, 1.2, 'F.Cu', NET)
        foreign = Segment(.44, .56, .46, .56, .05, 'F.Cu', OTHER_NET)
        via = Via(0, 0, 1.2, .6, ['F.Cu', 'B.Cu'], NET)
        pcb = board([trunk, crossing, foreign], [via], [pad('A', -4, 0), pad('B', 1.18, 4)])
        added = []
        self.assertEqual(sweep_dangling_via_branches(pcb, {NET}, added=added, clearance=CLEARANCE), ([], []))
        self.assertEqual((pcb.vias, added), ([via], []))

    def test_without_a_clearance_the_sweep_never_lays_copper(self):
        trunk, branch = track(0, 0, 10, 0), track(10, .225, 10, 2)
        via = Via(10, 0, .6, .3, ['F.Cu', 'B.Cu'], NET)
        pcb = board([trunk, branch], [via], [pad('A', 0, 0), pad('B', 10, 2)])
        added = []
        self.assertEqual(sweep_dangling_via_branches(pcb, {NET}, added=added), ([], []))
        self.assertEqual((pcb.vias, added), ([via], []))

    def test_each_bridge_keeps_the_width_of_the_track_it_reaches(self):
        narrow = Segment(0, 0, 10, 0, .1, 'F.Cu', NET)
        wide = Segment(10, .225, 10, 2, .5, 'F.Cu', NET)
        via = Via(10, 0, .6, .3, ['F.Cu', 'B.Cu'], NET)
        pcb = board([narrow, wide], [via], [pad('A', 0, 0), pad('B', 10, 2)])
        added = []
        sweep_dangling_via_branches(pcb, {NET}, added=added, clearance=CLEARANCE)
        self.assertEqual([s.width for s in added], [.5])

    def test_bridge_removed_by_a_later_round_is_not_emitted(self):
        # Review counterexample: removing the second via carries the first via's bridge away.
        left, right = track(-2, 0, 0, 0), track(.225, 0, .225, 2)
        first, second = Via(0, 0, .6, .3, ['F.Cu', 'B.Cu'], NET), Via(-2, 0, .6, .3, ['F.Cu', 'B.Cu'], NET)
        pcb = board([left, right], [first, second], [])
        added = []
        removed, dropped = sweep_dangling_via_branches(pcb, {NET}, added=added, clearance=CLEARANCE)
        self.assertEqual(added, [])
        self.assertTrue(all(s in (left, right) for s in removed))
        self.assertEqual(pcb.segments, [])
        self.assertEqual(set(map(id, dropped)), {id(first), id(second)})


class LateCopperEditTests(unittest.TestCase):
    BOARD = ('(kicad_pcb (version {version})\n{table}'
             '(segment (start 0 0) (end 10 0) (width 0.2) (layer "F.Cu") (net {net}) (uuid "a"))\n'
             '(via (at 10 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net {net}) (uuid "b"))\n)')

    def edit(self, version, table, net):
        content = self.BOARD.format(version=version, table=table, net=net)
        via = Via(10, 0, .6, .3, ['F.Cu', 'B.Cu'], 1, uuid='b')
        return replace_copper_in_content(content, [], [via], [track(10, 0, 10, .225)], {5: 'N', 1: 'N'})

    def test_edit_follows_the_file_net_dialect_not_its_version_stamp(self):
        # Review finding: a 2024 version stamp over name-only nets is a supported input.
        for version, table, net, expected in ((20241229, '', '"N"', '(net "N")'),
                                              (20241229, '(net 0 "")\n(net 1 "N")\n', '1', '(net 5)'),
                                              (20260206, '', '"N"', '(net "N")')):
            with self.subTest(version=version, net=net):
                edited = self.edit(version, table, net)
                self.assertNotIn('(via ', edited)
                self.assertIn(expected, edited[edited.index('(segment', edited.index('(segment') + 1):])


if __name__ == '__main__':
    unittest.main()
