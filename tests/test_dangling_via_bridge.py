"""A via that joins tracks on one layer is replaced by track, not left dangling."""
import math
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/kicad_route/router/py_router'))
from kicad_parser import BoardInfo, extract_segments, Pad, PCBData, Segment, Via, Zone
from pcb_modification import sweep_dangling_via_branches, trim_tracks_past_their_pads
from check_connected import check_net_connectivity
from kicad_writer import replace_copper_in_content

NET = 5
OTHER_NET = 6


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

    def test_via_stays_when_no_bridge_fits_inside_existing_copper(self):
        # Review counterexamples: a bridge reaching beyond the via's disk could crowd another
        # net, a hole or the board edge. Via, then the two tracks it joins.
        cases = {
            'wide tracks': (Via(0, 0, 1.2, .6, ['F.Cu', 'B.Cu'], NET),
                            Segment(-4, 0, 0, 0, 1.2, 'F.Cu', NET), Segment(1.18, -4, 1.18, 4, 1.2, 'F.Cu', NET)),
            'track wider than the via': (Via(0, 0, .6, .3, ['F.Cu', 'B.Cu'], NET),
                                         Segment(0, -4, 0, 0, .2, 'F.Cu', NET), Segment(.85, -4, .85, 4, 1.2, 'F.Cu', NET)),
            'far end at the rim': (Via(0, 0, .6, .3, ['F.Cu', 'B.Cu'], NET),
                                   Segment(0, -4, 0, 0, .5, 'F.Cu', NET), Segment(.29, 0, .29, 4, .5, 'F.Cu', NET)),
        }
        for name, (via, first, second) in cases.items():
            with self.subTest(name):
                far = (second.end_x, second.end_y)
                pcb = board([first, second], [via], [pad('A', first.start_x, first.start_y), pad('B', *far)])
                added = []
                self.assertEqual(sweep_dangling_via_branches(pcb, {NET}, added=added), ([], []))
                self.assertEqual((pcb.vias, added), ([via], []))

    def test_bridge_copper_stays_inside_the_via_disk_and_its_track(self):
        narrow = Segment(0, 0, 10, 0, .1, 'F.Cu', NET)
        wide = Segment(10, .225, 10, 2, .5, 'F.Cu', NET)
        via = Via(10, 0, .6, .3, ['F.Cu', 'B.Cu'], NET)
        pcb = board([narrow, wide], [via], [pad('A', 0, 0), pad('B', 10, 2)])
        added = []
        sweep_dangling_via_branches(pcb, {NET}, added=added)
        self.assertEqual(len(added), 1)
        self.assertGreater(added[0].width, narrow.width)
        self.assertLessEqual(math.hypot(.225, added[0].width / 2), .3 + 1e-9)

    def test_pad_touching_the_via_copper_keeps_the_via(self):
        # Review counterexamples: a same-net pad reaches the via's disk, whether or not it also
        # reaches a track, and removing the disk would narrow or break its connection.
        for x in (-.34, -.29):
            with self.subTest(x=x):
                trunk, branch = track(0, -3, 0, 0), Segment(.225, 0, .225, 3, .5, 'F.Cu', NET)
                via = Via(0, 0, .6, .3, ['F.Cu', 'B.Cu'], NET)
                beside = pad('C', x, 0)
                beside.size_x = beside.size_y = .2
                beside.shape = 'circle'
                pcb = board([trunk, branch], [via], [pad('A', 0, -3), pad('B', .225, 3), beside])
                added = []
                self.assertEqual(sweep_dangling_via_branches(pcb, {NET}, added=added), ([], []))
                self.assertEqual((pcb.vias, added), ([via], []))

    def test_bridge_removed_by_a_later_round_is_not_emitted(self):
        # Review counterexample: removing the second via carries the first via's bridge away.
        left, right = track(-2, 0, 0, 0), track(.225, 0, .225, 2)
        first, second = Via(0, 0, .6, .3, ['F.Cu', 'B.Cu'], NET), Via(-2, 0, .6, .3, ['F.Cu', 'B.Cu'], NET)
        pcb = board([left, right], [first, second], [])
        added = []
        removed, dropped = sweep_dangling_via_branches(pcb, {NET}, added=added)
        self.assertEqual(added, [])
        self.assertTrue(all(s in (left, right) for s in removed))
        self.assertEqual(pcb.segments, [])
        self.assertEqual(set(map(id, dropped)), {id(first), id(second)})


class ViaBesidePadTests(unittest.TestCase):
    def test_pad_with_its_own_track_does_not_keep_the_via(self):
        # Retained I2C trial 2026-11-02: U2.3 has a stub from its centre to just beside a via
        # whose disk also touches the pad. The next track starts elsewhere on the disk.
        beside = pad('U', 0, .475)
        beside.size_x, beside.size_y = .5, .35
        stub, onward = track(0, .475, 0, .15), track(.1, -.1, 3, -.1)
        via = Via(0, 0, .6, .3, ['F.Cu', 'B.Cu'], NET)
        pcb = board([stub, onward], [via], [beside, pad('R', 3, -.1)])
        added = []
        removed, dropped = sweep_dangling_via_branches(pcb, {NET}, added=added)
        self.assertEqual((removed, dropped), ([], [via]))
        self.assertEqual(len(added), 2)
        connected = check_net_connectivity(NET, pcb.segments, pcb.vias, pcb.pads_by_net[NET], [])
        self.assertFalse(connected.get('disconnected_pads'))


    def test_pad_whose_only_track_misses_the_via_keeps_the_via(self):
        # Review counterexample: a stub inside the pad that reaches neither the via nor a bridge.
        beside = pad('C', -.29, 0)
        beside.size_x = beside.size_y = .2
        beside.shape = 'circle'
        stub = Segment(-.36, 0, -.36, .01, .1, 'F.Cu', NET)
        trunk, branch = track(0, -3, 0, 0), Segment(.225, 0, .225, 3, .5, 'F.Cu', NET)
        via = Via(0, 0, .6, .3, ['F.Cu', 'B.Cu'], NET)
        pcb = board([trunk, branch, stub], [via], [pad('A', 0, -3), pad('B', .225, 3), beside])
        added = []
        self.assertEqual(sweep_dangling_via_branches(pcb, {NET}, added=added), ([], []))
        self.assertEqual((pcb.vias, added), ([via], []))

    def test_net_with_a_plane_keeps_the_via(self):
        # Review counterexample: a plane overlaps the via's rim without containing its centre.
        beside = pad('U', 0, .465)
        beside.size_x, beside.size_y = .5, .35
        stub, onward = track(0, .465, 0, .15), track(.1, -.1, 3, -.1)
        via = Via(0, 0, .6, .3, ['F.Cu', 'B.Cu'], NET)
        pcb = board([stub, onward], [via], [beside, pad('R', 3, -.1)])
        pcb.zones = [SimpleNamespace(net_id=NET, layer='F.Cu', polygon=[(-2, -2), (-.26, -2), (-.26, 2), (-2, 2)])]
        added = []
        self.assertEqual(sweep_dangling_via_branches(pcb, {NET}, added=added), ([], []))
        self.assertEqual((pcb.vias, added), ([via], []))


class TrackPastItsPadTests(unittest.TestCase):
    def board_with_overshoot(self, *others):
        # Retained I2C trial 2026-11-02: SDA runs through U2.3 and stops 0.1 mm beyond its edge.
        through = track(130.7, 115.375, 129.475, 115.375)
        far = track(133.75, 115.375, 130.7, 115.375)
        u2 = pad('U2', 129.825, 115.375)
        u2.size_x, u2.size_y = .5, .35
        return through, board([far, through, *others], [], [u2, pad('J2', 133.75, 115.375)])

    def test_track_past_its_pad_ends_at_the_pad_centre(self):
        through, pcb = self.board_with_overshoot()
        removed, shortened = trim_tracks_past_their_pads(pcb, {NET})
        self.assertEqual(removed, [through])
        self.assertEqual([(s.start_x, s.start_y, s.end_x, s.end_y, s.width) for s in shortened],
                         [(130.7, 115.375, 129.825, 115.375, .2)])
        self.assertFalse(check_net_connectivity(NET, pcb.segments, pcb.vias, pcb.pads_by_net[NET], []).get('disconnected_pads'))
        self.assertNotIn(through, pcb.segments)

    def test_both_overruns_of_one_track_are_trimmed_into_one_edit(self):
        # Sol review 2026-11-01: one invocation left the second overrun dangling.
        through = track(0, 0, 10, 0)
        pcb = board([through], [], [pad('A', 4, 0), pad('B', 6, 0)])
        removed, shortened = trim_tracks_past_their_pads(pcb, {NET})
        self.assertEqual(removed, [through])
        self.assertEqual([(s.start_x, s.end_x) for s in shortened], [(4, 6)])

    def assert_left_alone(self, pcb, kept):
        self.assertEqual(trim_tracks_past_their_pads(pcb, {NET}), ([], []))
        self.assertIn(kept, pcb.segments)

    def test_second_pad_overlapping_the_tail_keeps_the_track(self):
        # Sol review: KiCad lost this pad's only contact, while the point test credited it.
        source, offset = pad('A', 0, 0), pad('B', 8.09, .09)
        target = pad('C', 8, 0)
        for small in (target, offset):
            small.size_x = small.size_y = .1
            small.shape = 'circle'
        through = Segment(0, 0, 10, 0, .1, 'F.Cu', NET)
        self.assert_left_alone(board([through], [], [source, target, offset]), through)

    def test_drilled_pad_is_not_cut_to_its_copper_centre(self):
        # Sol review: an offset hole carried the connection, and a cut at the copper centre lost it.
        through, pcb = self.board_with_overshoot()
        pcb.pads_by_net[NET][0].drill = .3
        self.assert_left_alone(pcb, through)

    def test_zone_on_the_tracks_layer_keeps_the_track(self):
        # Sol review: the outline credited the zone, while the tail held the only fill contact.
        through, pcb = self.board_with_overshoot()
        pcb.zones = [Zone(NET, '/V3V3', 'F.Cu', [(129, 118), (130, 118), (130, 119), (129, 119)])]
        self.assert_left_alone(pcb, through)

    def test_zone_on_another_layer_does_not_stop_the_trim(self):
        through, pcb = self.board_with_overshoot()
        pcb.zones = [Zone(NET, '/V3V3', 'B.Cu', [(120, 110), (140, 110), (140, 120), (120, 120)])]
        self.assertEqual(trim_tracks_past_their_pads(pcb, {NET})[0], [through])

    def test_track_whose_end_cap_touches_another_tracks_middle_keeps_its_tail(self):
        # Sol review: a locked loop's only contact was the end cap of the "free" track.
        beside = track(129.3, 113, 129.3, 118)
        through, pcb = self.board_with_overshoot(beside)
        pcb.pads_by_net[NET].append(pad('R1', 129.3, 118))
        self.assert_left_alone(pcb, through)

    def test_arc_chord_is_left_alone(self):
        # Sol review: the writer removes (segment) blocks only, so the arc stayed and a track was added.
        content = ('(kicad_pcb (version 20241229)\n(net 0 "")\n(net 5 "N")\n(arc (start 130.7 115.375) '
                   '(mid 130.1 115.4) (end 129.475 115.375) (width 0.2) (layer "F.Cu") (net 5) (uuid "a"))\n)')
        chords = extract_segments(content)
        self.assertTrue(chords and all(s.from_arc for s in chords))
        u2 = pad('U2', 129.825, 115.39)
        pcb = board(chords, [], [u2, pad('J2', 130.7, 115.375)])
        self.assertEqual(trim_tracks_past_their_pads(pcb, {NET}), ([], []))

    def test_track_ending_inside_its_pad_is_unchanged(self):
        inside = track(130.7, 115.375, 129.7, 115.375)
        u2 = pad('U2', 129.825, 115.375)
        u2.size_x, u2.size_y = .5, .35
        pcb = board([inside], [], [u2, pad('J2', 130.7, 115.375)])
        self.assertEqual(trim_tracks_past_their_pads(pcb, {NET}), ([], []))

    def test_tail_that_another_track_reaches_is_kept(self):
        joined_tail = track(129.5, 115.375, 129.5, 117)
        through, pcb = self.board_with_overshoot(joined_tail)
        pcb.pads_by_net[NET].append(pad('R1', 129.5, 117))
        self.assertEqual(trim_tracks_past_their_pads(pcb, {NET}), ([], []))
        self.assertIn(through, pcb.segments)

    def test_via_on_the_tail_keeps_the_track(self):
        through, pcb = self.board_with_overshoot()
        pcb.vias.append(Via(129.5, 115.375, .6, .3, ['F.Cu', 'B.Cu'], NET))
        self.assertEqual(trim_tracks_past_their_pads(pcb, {NET}), ([], []))
        self.assertIn(through, pcb.segments)

    def test_locked_track_is_kept(self):
        through, pcb = self.board_with_overshoot()
        through.locked = True
        self.assertEqual(trim_tracks_past_their_pads(pcb, {NET}), ([], []))


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
