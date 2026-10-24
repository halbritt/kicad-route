"""A route through a terminal pad must not leave copper past that pad."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py_router'))
from synth import make_net, make_pad, make_pcb, make_seg
from pcb_modification import trim_dangles_past_body_anchor


class PadBodyDangles(unittest.TestCase):
    def board(self, *, reverse=False, offset=0, layers=('F.Cu',), locked=False):
        ends = [(5, 4), (2.52, 4)]
        if reverse:
            ends.reverse()
        seg = make_seg(*ends[0], *ends[1], width=.25, locked=locked)
        pads = [make_pad(1, 5, 4, ref='J1', size_x=.25, size_y=.25),
                make_pad(1, 3, 4 + offset, ref='J2', size_x=.25, size_y=.25, layers=layers)]
        pcb = make_pcb(nets={1: make_net(1, 'signal')}, segments=[seg], pads_by_net={1: pads})
        return pcb, seg

    def test_short_tail_trims_to_pad_in_both_directions(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                pcb, seg = self.board(reverse=reverse)
                results = [{'new_segments': [seg]}]
                count, strips = trim_dangles_past_body_anchor(results, pcb, {1}, keep_input_copper=True)
                self.assertEqual(count, 1)
                self.assertEqual(strips, [])
                self.assertEqual(sorted([seg.start_x, seg.end_x]), [3, 5])

    def test_distant_and_other_layer_pads_are_not_trim_anchors(self):
        for kw in ({'offset': .3}, {'layers': ('B.Cu',)}):
            with self.subTest(kw=kw):
                pcb, seg = self.board(**kw)
                self.assertEqual(trim_dangles_past_body_anchor([{'new_segments': [seg]}], pcb, {1})[0], 0)
                self.assertEqual(seg.end_x, 2.52)

    def test_input_and_locked_copper_are_preserved(self):
        for locked in (False, True):
            with self.subTest(locked=locked):
                pcb, seg = self.board(locked=locked)
                results = [{'new_segments': [seg]}] if locked else []
                self.assertEqual(trim_dangles_past_body_anchor(results, pcb, {1}, keep_input_copper=True)[0], 0)
                self.assertEqual(seg.end_x, 2.52)

    def test_last_of_multiple_pads_keeps_all_terminal_connections(self):
        pcb, seg = self.board()
        pcb.pads_by_net[1].append(make_pad(1, 2.8, 4, ref='J3', size_x=.25, size_y=.25))
        count, _ = trim_dangles_past_body_anchor([{'new_segments': [seg]}], pcb, {1})
        self.assertEqual(count, 1)
        self.assertEqual(seg.end_x, 2.8)


if __name__ == '__main__':
    unittest.main()
