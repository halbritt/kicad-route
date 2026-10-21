"""KICADROUTE-88: parsing must not retain whole-board text between calls."""
import gc
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
from threading import Barrier
import tracemalloc
import unittest
import weakref

ROUTER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROUTER / 'py_router'))
import kicad_parser as parser

FIXTURE = ROUTER.parents[2] / 'fixtures' / 'route_demo' / 'route_demo.kicad_pcb'

CUSTOM = '''(footprint "cache:custom" (layer "F.Cu") (at 30 30 45)
 (property "Reference" "U_CACHE" (at 0 0) (layer "F.SilkS"))
 (pad "1" smd custom (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask")
  (net "/VIN") (options (clearance outline) (anchor rect))
  (primitives (gr_poly (pts (xy -1 -1) (xy 1 -1) (xy 1 1) (xy -1 1))
   (width 0) (fill yes))))
 (fp_line (start -2 0) (end 2 0) (stroke (width 0.2) (type default)) (layer "F.Cu"))
 (fp_circle (center 0 0) (end 1 0) (stroke (width 0.1) (type default)) (fill none) (layer "F.Paste")))'''


def custom_board(directory):
    path = Path(directory) / 'custom.kicad_pcb'
    text = FIXTURE.read_text()
    path.write_text(text[:text.rfind(')')] + CUSTOM + '\n)')
    return path


def geometry(board):
    return {key: repr(value) for key, value in vars(board).items() if not callable(value)}


class ParserCacheTests(unittest.TestCase):
    def test_shared_and_unshared_custom_pad_geometry_match_across_repeated_parses(self):
        with tempfile.TemporaryDirectory() as directory:
            path = custom_board(directory)
            unshared = parser._parse_kicad_pcb(path, 'User.1', 'User.2')
            for _ in range(3):
                shared = parser.parse_kicad_pcb(path)
                self.assertEqual(geometry(shared), geometry(unshared))
                pad = shared.footprints['U_CACHE'].pads[0]
                self.assertEqual((pad.global_x, pad.global_y), (30, 30))
                self.assertTrue(pad.polygons)
                self.assertIsNone(parser._PARSE_MEMO.get())

    def test_nested_parse_and_failure_restore_outer_geometry_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = custom_board(directory)
            expected = geometry(parser.parse_kicad_pcb(path))
            bad = Path(directory) / 'broken.kicad_pcb'
            bad.write_text('(kicad_pcb (version 20260206)')
            nested = []

            def enter(frame, event, _arg):
                if event == 'call' and frame.f_code is parser.extract_segments.__code__ and not nested:
                    nested.append(geometry(parser.parse_kicad_pcb(FIXTURE)))
                    with self.assertRaises(ValueError):
                        parser.parse_kicad_pcb(bad)

            sys.setprofile(enter)
            try:
                actual = parser.parse_kicad_pcb(path)
            finally:
                sys.setprofile(None)
            self.assertEqual(geometry(actual), expected)
            self.assertEqual(nested, [geometry(parser.parse_kicad_pcb(FIXTURE))])
            self.assertIsNone(parser._PARSE_MEMO.get())

    def test_standalone_extractors_do_not_retain_the_callers_text(self):
        class BoardText(str):
            pass
        with tempfile.TemporaryDirectory() as directory:
            path = custom_board(directory)
            text = BoardText(path.read_text())
            held = weakref.ref(text)
            segments = parser.extract_segments(text, {'/VIN': 1})
            self.assertTrue(segments)
            del text
            gc.collect()
            self.assertIsNone(held(), 'standalone extraction retained its input string')

    def test_overlapping_threads_parse_different_boards_without_shared_lifetime(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [FIXTURE, custom_board(directory)]
            expected = [geometry(parser.parse_kicad_pcb(path)) for path in paths]
            barrier = Barrier(2)

            def parse(path):
                def overlap(frame, event, _arg):
                    if event == 'call' and frame.f_code is parser.extract_segments.__code__:
                        barrier.wait(timeout=10)
                sys.setprofile(overlap)
                try:
                    result = geometry(parser.parse_kicad_pcb(path))
                finally:
                    sys.setprofile(None)
                self.assertIsNone(parser._PARSE_MEMO.get())
                return result

            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(parse, paths)), expected)
            self.assertIsNone(parser._PARSE_MEMO.get())

    def test_one_parse_shares_repeated_footprint_shape_scans(self):
        with tempfile.TemporaryDirectory() as directory:
            board = custom_board(directory)
            scans = Counter()

            def count(frame, event, _arg):
                if event == 'call' and frame.f_code is parser.find_matching_paren.__code__:
                    text, start = frame.f_locals['content'], frame.f_locals['open_idx']
                    if text[start:].startswith(('(fp_line ', '(fp_circle ')):
                        scans[text, start] += 1

            sys.setprofile(count)
            try:
                result = parser.parse_kicad_pcb(board)
            finally:
                sys.setprofile(None)
            self.assertTrue(result.footprints['U_CACHE'].pads)
            self.assertTrue(scans)
            self.assertEqual(max(scans.values()), 1, 'one parse scanned a footprint shape twice')

    def test_completed_parse_releases_large_unused_board_text(self):
        parser.parse_kicad_pcb(FIXTURE)  # Warm imports and regex compilation.
        with tempfile.TemporaryDirectory() as directory:
            board = Path(directory) / 'large.kicad_pcb'
            board.write_text(FIXTURE.read_text().replace(
                '(general', '(property "unused" "' + 'x' * (512 * 1024) + '")\n(general', 1))
            gc.collect()
            tracemalloc.start()
            try:
                result = parser.parse_kicad_pcb(board)
                self.assertTrue(result.footprints)
                gc.collect()
                retained, _peak = tracemalloc.get_traced_memory()
                self.assertLess(retained, 256 * 1024,
                                'a completed parse retained the unused 512 KiB board property')
            finally:
                tracemalloc.stop()

    def test_failed_parse_releases_board_text_and_next_parse_recovers(self):
        parser.parse_kicad_pcb(FIXTURE)
        with tempfile.TemporaryDirectory() as directory:
            path = custom_board(directory)
            text = path.read_text().replace('(width 0.2)', '(width --1)', 1)
            path.write_text(text.replace('(general', '(property "unused" "' + 'x' * (512 * 1024) + '")\n(general', 1))
            gc.collect()
            tracemalloc.start()
            try:
                with self.assertRaisesRegex(ValueError, 'float'):
                    parser.parse_kicad_pcb(path)
                gc.collect()
                retained, _peak = tracemalloc.get_traced_memory()
                self.assertLess(retained, 256 * 1024)
                self.assertIsNone(parser._PARSE_MEMO.get())
            finally:
                tracemalloc.stop()
            self.assertTrue(parser.parse_kicad_pcb(FIXTURE).footprints)


if __name__ == '__main__':
    unittest.main()
