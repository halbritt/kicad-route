"""A grid-cell tap must not add a branch across already joined centrelines."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/kicad_route/router/py_router'))
from kicad_parser import Segment
import single_ended_routing as routing


class SingletonTapCrossingTests(unittest.TestCase):
    def test_pending_route_already_crosses_the_target_track(self):
        # The target has slope 2 and crosses the pending horizontal at
        # (10.005, 10). Both terminals round to the same 0.025 mm grid cell.
        target=Segment(9.99,9.97,10.03,10.05,.25,'F.Cu',5)
        pending=Segment(9.5,10.,10.5,10.,.25,'F.Cu',5)
        target.locked=True
        pcb=SimpleNamespace(segments=[target])
        before=copy.deepcopy((pcb.segments,[pending]))
        start=(10.,10.,'F.Cu');end=(10.01,10.01,'F.Cu')
        self.assertFalse(routing._tap_terminals_already_cross(pcb,[],5,start,end))
        self.assertTrue(routing._tap_terminals_already_cross(pcb,[pending],5,start,end))
        self.assertEqual((pcb.segments,[pending]),before)

    def test_same_cell_and_copper_overlap_are_not_centreline_contact(self):
        source=Segment(9.5,10.,10.,10.,.25,'F.Cu',5)
        target=Segment(10.01,10.01,10.01,10.5,.25,'F.Cu',5)
        pcb=SimpleNamespace(segments=[target])
        self.assertFalse(routing._tap_terminals_already_cross(
            pcb,[source],5,(10.,10.,'F.Cu'),(10.01,10.01,'F.Cu')))

    def test_wrong_net_layer_or_missing_owner_cannot_suppress_a_weld(self):
        for mode in ('target_net','source_net','target_layer','different_terminal_layer',
                     'off_centreline','zone_only','graphic'):
            with self.subTest(mode=mode):
                target=Segment(9.99,9.97,10.03,10.05,.25,'F.Cu',5)
                source=Segment(9.5,10.,10.5,10.,.25,'F.Cu',5)
                pcb=SimpleNamespace(segments=[target],zones=[object()])
                start=(10.,10.,'F.Cu');end=(10.01,10.01,'F.Cu')
                if mode=='target_net':target.net_id=6
                elif mode=='source_net':source.net_id=6
                elif mode=='target_layer':target.layer='B.Cu'
                elif mode=='different_terminal_layer':end=(*end[:2],'B.Cu')
                elif mode=='off_centreline':start=(10.,10.000002,'F.Cu')
                elif mode=='zone_only':pcb.segments=[]
                else:target.graphic=True
                before=copy.deepcopy((pcb.segments,[source]))
                self.assertFalse(routing._tap_terminals_already_cross(pcb,[source],5,start,end))
                self.assertEqual((pcb.segments,[source]),before)

    def test_repeated_or_reversed_owner_tracks_do_not_change_the_proof(self):
        source=Segment(9.5,10.,10.5,10.,.25,'F.Cu',5)
        target=Segment(10.03,10.05,9.99,9.97,.25,'F.Cu',5)
        pcb=SimpleNamespace(segments=[source,target])
        self.assertTrue(routing._tap_terminals_already_cross(
            pcb,[source,target],5,(10.,10.,'F.Cu'),(10.01,10.01,'F.Cu')))

    def test_writer_rounding_does_not_close_a_one_nanometre_gap(self):
        # round(v*1e6) would incorrectly join these. The writer saves the
        # source at 10.000002 and the target at 10.000003 mm.
        source=Segment(9.,10.,10.000002,10.,.25,'F.Cu',5)
        target=Segment(10.0000025,10.,10.0000025,11.,.25,'F.Cu',5)
        self.assertFalse(routing._tap_terminals_already_cross(
            SimpleNamespace(segments=[target]),[source],5,
            (9.99,10.,'F.Cu'),(10.0000025,10.01,'F.Cu')))

    def test_population_limit_conservatively_keeps_the_weld(self):
        source=Segment(9.5,10.,10.5,10.,.25,'F.Cu',5)
        target=Segment(9.99,9.97,10.03,10.05,.25,'F.Cu',5)
        pcb=SimpleNamespace(segments=[target])
        pending=[source,source,source]
        before=copy.deepcopy((pcb.segments,pending))
        self.assertFalse(routing._tap_terminals_already_cross(
            pcb,pending,5,(10.,10.,'F.Cu'),(10.01,10.01,'F.Cu'),max_segments=3))
        self.assertEqual((pcb.segments,pending),before)

    def test_pair_limit_conservatively_keeps_a_late_crossing_weld(self):
        source=Segment(9.5,10.,10.5,10.,.25,'F.Cu',5)
        target=Segment(10.01,10.01,10.01,10.5,.25,'F.Cu',5)
        crossing=Segment(9.99,9.97,10.03,10.05,.25,'F.Cu',5)
        pcb=SimpleNamespace(segments=[target,crossing])
        before=copy.deepcopy((pcb.segments,[source]))
        args=(pcb,[source],5,(10.,10.,'F.Cu'),(10.01,10.01,'F.Cu'))
        self.assertFalse(routing._tap_terminals_already_cross(*args,max_pairs=1))
        self.assertTrue(routing._tap_terminals_already_cross(*args,max_pairs=2))
        self.assertEqual((pcb.segments,[source]),before)


if __name__=='__main__':
    unittest.main()
