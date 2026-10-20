"""Writer regressions run without a native KiCad installation."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py_router'))
from kicad_writer import add_tracks_to_pcb, add_tracks_and_vias_to_pcb


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
