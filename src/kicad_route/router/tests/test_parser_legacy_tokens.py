"""KICADROUTE-83: KiCad's older bare pad/net tokens remain real copper."""
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROUTER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROUTER / 'py_router'))
import kicad_parser as parser
import kicad_writer as writer

FIXTURE = ROUTER.parents[2] / 'fixtures' / 'route_demo' / 'route_demo.kicad_pcb'


def legacy_text(dialect):
    original = FIXTURE.read_text()
    ids = {net.name: ident for ident, net in parser.parse_kicad_pcb(FIXTURE).nets.items()}
    text = re.sub(r'\(pad "(\d+)"', r'(pad \1', original)
    if dialect == 9:
        text = text.replace('(version 20260206)', '(version 20241229)')
        table = '\n'.join(f'(net {ident} "{name}")' for name, ident in ids.items())
        text = text.replace('(general', table + '\n(general', 1)
    return re.sub(r'\(net "([^"]+)"\)',
                  lambda m: f'(net {ids[m[1]]} {m[1]})' if dialect == 9 else f'(net {m[1]})', text)


def pads(board):
    return sorted((ref, pad.pad_number, pad.net_name, pad.net_id,
                   pad.global_x, pad.global_y, pad.size_x, pad.size_y)
                  for ref, footprint in board.footprints.items() for pad in footprint.pads)


class LegacyTokenTests(unittest.TestCase):
    def test_pad_net_swap_handles_bare_tokens_in_both_dialects(self):
        with tempfile.TemporaryDirectory() as directory:
            for dialect in (9, 10):
                with self.subTest(dialect=dialect):
                    path = Path(directory) / 'swap.kicad_pcb'
                    text = legacy_text(dialect)
                    path.write_text(text)
                    before = parser.parse_kicad_pcb(path)
                    pair = before.footprints['J1'].pads[:2]
                    self.assertNotEqual(pair[0].net_name, pair[1].net_name)
                    changed = writer.swap_pad_nets_in_content(text, *pair)
                    path.write_text(changed)
                    after = parser.parse_kicad_pcb(path).footprints['J1'].pads[:2]
                    self.assertEqual([p.net_name for p in after], [p.net_name for p in pair][::-1])
                    self.assertEqual(writer.move_copper_text_to_silkscreen(changed), changed)

    def test_quoted_parentheses_in_net_name_do_not_cut_short_the_pad_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'quoted.kicad_pcb'
            text = FIXTURE.read_text().replace('/VIN', '/VIN)')
            text = text.replace('(net "/VIN)")', '(pinfunction ") ) (net GHOST)")\n(net "/VIN)")', 1)
            path.write_text(text)
            before = parser.parse_kicad_pcb(path).footprints['J1'].pads[:2]
            path.write_text(writer.swap_pad_nets_in_content(text, *before))
            after = parser.parse_kicad_pcb(path).footprints['J1'].pads[:2]
            self.assertEqual([p.net_name for p in after], [p.net_name for p in before][::-1])

    def test_bare_net_discovery_ignores_quoted_tokens_and_numeric_references(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'mixed.kicad_pcb'
            quoted = FIXTURE.read_text()
            original_ids = parser.extract_nets(quoted)[1]
            mixed = quoted.replace('(general', '(property "comment" "literal (net GHOST)")\n(general', 1)
            mixed = mixed[:mixed.rfind(')')] + '(net 999 )\n(net NEW_NET)\n)'
            path.write_text(mixed)
            actual = parser.parse_kicad_pcb(path)
            ids = {n.name: ident for ident, n in actual.nets.items()}
            for name, ident in original_ids.items():
                if name:
                    self.assertEqual(ids.get(name), ident)
            self.assertNotIn('GHOST', ids)
            self.assertNotIn('999', ids)
            self.assertEqual(ids['NEW_NET'], max(original_ids.values()) + 1)

    def test_bare_tokens_match_native_kicad_pad_geometry_and_net_names(self):
        import pcbnew
        with tempfile.TemporaryDirectory() as directory:
            for dialect in (9, 10):
                with self.subTest(dialect=dialect):
                    path = Path(directory) / f'native-{dialect}.kicad_pcb'
                    version, back, silk = (20241229, 31, 37) if dialect == 9 else (20260206, 2, 5)
                    table = '(net 0 "") (net 1 GND) (net 2 VCC)' if dialect == 9 else ''
                    net1, net2 = ('1 GND', '2 VCC') if dialect == 9 else ('GND', 'VCC')
                    path.write_text(f'''(kicad_pcb (version {version}) (generator "pcbnew")
 (general (thickness 1.6)) (paper "A4")
 (layers (0 "F.Cu" signal) ({back} "B.Cu" signal) ({silk} "F.SilkS" user))
 (setup (pad_to_mask_clearance 0)) {table}
 (footprint "test:bare" (layer "F.Cu") (at 10 20)
  (fp_text reference "J1" (at 0 -2) (layer "F.SilkS")
   (effects (font (size 1 1) (thickness 0.15))))
  (pad 1 smd rect (at 0 0) (size 1 2) (layers "F.Cu") (net {net1})
   (pinfunction GND) (pintype passive))
  (pad 2 smd rect (at 3 0) (size 1 2) (layers "F.Cu") (net {net2}))))''')
                    parsed = parser.parse_kicad_pcb(path)
                    board = pcbnew.LoadBoard(str(path))
                    self.assertIsNotNone(board, f'KiCad refused the dialect-{dialect} fixture')
                    native = sorted((fp.GetReference(), p.GetNumber(), p.GetNetname(),
                                     pcbnew.ToMM(p.GetPosition().x), pcbnew.ToMM(p.GetPosition().y),
                                     pcbnew.ToMM(p.GetSize().x), pcbnew.ToMM(p.GetSize().y))
                                    for fp in board.GetFootprints() for p in fp.Pads())
                    actual = sorted((ref, number, name, x, y, sx, sy)
                                    for ref, number, name, _id, x, y, sx, sy in pads(parsed))
                    self.assertEqual(actual, native)
                    pad = parsed.footprints['J1'].pads[0]
                    self.assertEqual((pad.pinfunction, pad.pintype), ('GND', 'passive'))

    def test_bare_net_names_in_both_dialects_preserve_pad_geometry_and_net_ids(self):
        quoted = parser.parse_kicad_pcb(FIXTURE)
        with tempfile.TemporaryDirectory() as directory:
            for dialect in (9, 10):
                with self.subTest(dialect=dialect):
                    path = Path(directory) / f'bare-{dialect}.kicad_pcb'
                    path.write_text(legacy_text(dialect))
                    actual = parser.parse_kicad_pcb(path)
                    self.assertEqual(pads(actual), pads(quoted))
                    self.assertEqual(actual.net_id_to_name, quoted.net_id_to_name)

    def test_bare_pad_numbers_have_the_same_geometry_and_nets_as_quoted_numbers(self):
        quoted = parser.parse_kicad_pcb(FIXTURE)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bare.kicad_pcb'
            path.write_text(re.sub(r'\(pad "(\d+)"', r'(pad \1', FIXTURE.read_text()))
            bare = parser.parse_kicad_pcb(path)
            self.assertTrue(pads(quoted))
            self.assertEqual(pads(bare), pads(quoted))


if __name__ == '__main__':
    unittest.main()
