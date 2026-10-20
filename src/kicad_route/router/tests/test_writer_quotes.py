"""Writer regressions run without a native KiCad installation."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py_router'))
from kicad_writer import add_tracks_to_pcb, add_tracks_and_vias_to_pcb
from sexpr import copper_text_signature


class WriterQuoteTests(unittest.TestCase):
    def test_both_writers_preserve_quoted_contents(self):
        values = ['((', ')', r'quote \" and slash \\', '(gr_text fake']
        for writer in (add_tracks_to_pcb, add_tracks_and_vias_to_pcb):
            for layer in ('F.Cu', 'B.Cu', 'F.SilkS'):
                for value in values:
                    with self.subTest(writer=writer.__name__, layer=layer, value=value):
                        text = f'(gr_text "{value}" (at 1 2) (layer "{layer}") (effects (font (size 1 1) (thickness 0.1))))'
                        board = '(kicad_pcb (version 20241229) (generator "pcbnew") ' + text + ')'
                        with tempfile.TemporaryDirectory() as tmp:
                            source, dest = Path(tmp) / 'in.kicad_pcb', Path(tmp) / 'out.kicad_pcb'
                            source.write_text(board)
                            self.assertTrue(writer(str(source), str(dest), []))
                            self.assertIn(f'"{value}"', dest.read_text())
                            self.assertEqual(dest.read_text().count('(gr_text'), board.count('(gr_text'))

    def test_native_text_signature_binds_footprint_pose(self):
        board = ('(kicad_pcb (footprint "local" (layer "F.Cu") (at 4 5 90) '
                 '(fp_text user "literal ((" (at 1 2) (layer "F.Cu")) '
                 '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))))')
        signature = copper_text_signature(board)
        self.assertEqual(copper_text_signature(board.replace(') (', ')\n  (')), signature)
        self.assertNotEqual(copper_text_signature(board.replace('(at 4 5 90)', '(at 5 5 90)')), signature)
        self.assertNotEqual(copper_text_signature(board.replace('(layer "F.Cu") (at 4', '(layer "B.Cu") (at 4')), signature)

    def test_both_writers_reject_malformed_boards_without_partial_output(self):
        for writer in (add_tracks_to_pcb, add_tracks_and_vias_to_pcb):
            for board in ('(kicad_pcb (gr_text "unterminated', '(kicad_pcb (gr_line (start 1 2))', ')'):
                with self.subTest(writer=writer.__name__, board=board), tempfile.TemporaryDirectory() as tmp:
                    source, dest = Path(tmp) / 'in.kicad_pcb', Path(tmp) / 'out.kicad_pcb'
                    source.write_text(board)
                    with self.assertRaisesRegex(ValueError, 'parenthesis|Truncated'):
                        writer(str(source), str(dest), [])
                    self.assertFalse(dest.exists())


if __name__ == '__main__':
    unittest.main()
