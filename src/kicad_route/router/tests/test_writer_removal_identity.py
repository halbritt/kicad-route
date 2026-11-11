"""Locked copper survives the writer's text removal, without native KiCad or routing.

The removal functions are upstream's (matched by span, layer and net, the listed
width preferred). kicad-route adds one guarantee: a locked block is never removed and
a locked target never searched for, so a locked track cannot go with an unlocked twin.
"""
from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py_router'))
from kicad_parser import extract_segments, extract_vias, kiid_from_tstamp
from kicad_writer import (remove_segments_from_content, remove_vias_from_content,
                          generate_segment_sexpr, generate_via_sexpr)

A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'
C = '33333333-3333-4333-8333-333333333333'


def segment(uid=A, width='.2', lock='', layer='F.Cu', net='1'):
    return (f'(segment {lock} (start 1 3) (end 7 3) (width {width}) '
            f'(layer "{layer}") (net {net}) (uuid "{uid}"))')


def via(uid=A, size='.6', drill='.3', layers='"F.Cu" "B.Cu"', lock='', net='1', extra=''):
    return (f'(via {lock} (at 2 3) (size {size}) (drill {drill}) '
            f'(layers {layers}) (net {net}) (uuid "{uid}") {extra})')


def board(*blocks):
    return '(kicad_pcb (net 1 "N") ' + ' '.join(blocks) + ')'


class RemovalIdentityTests(unittest.TestCase):
    def test_uuid_selects_unlocked_twin_in_both_orders(self):
        for block, parse, remove in ((segment, extract_segments, remove_segments_from_content),
                                     (via, extract_vias, remove_vias_from_content)):
            for lock in ('locked', '(locked)', '(locked yes)', '(locked true)'):
                for reverse in (False, True):
                    with self.subTest(kind=block.__name__, lock=lock, reverse=reverse):
                        blocks = [block(A, lock=lock), block(B)]
                        content = board(*(reversed(blocks) if reverse else blocks))
                        target = next(t for t in parse(content) if t.uuid == B)
                        out, count = remove(content, [target])
                        self.assertEqual(count, 1)
                        self.assertEqual(out, content.replace(block(B), ''))
                        locked = next(t for t in parse(content) if t.uuid == A)
                        self.assertEqual(remove(content, [locked]), (content, 0))

    def test_writer_minted_uuid_matches_generated_uuidless_target(self):
        target_s = replace(extract_segments(board(segment()))[0], uuid='', start_x=1.1234564)
        target_v = replace(extract_vias(board(via()))[0], uuid='', x=2.1234564)
        generated_s = generate_segment_sexpr((target_s.start_x, target_s.start_y),
                                            (target_s.end_x, target_s.end_y),
                                            target_s.width, target_s.layer, target_s.net_id)
        generated_v = generate_via_sexpr(target_v.x, target_v.y, target_v.size,
                                        target_v.drill, target_v.layers, target_v.net_id)
        for generated, target, remove in ((generated_s, target_s, remove_segments_from_content),
                                           (generated_v, target_v, remove_vias_from_content)):
            content = board(generated.strip())
            out, count = remove(content, [target])
            self.assertEqual((out, count), (board(''), 1))

    def test_exact_unmatched_identity_survives_transform_retry(self):
        for block, parse, remove, delta in (
                (segment, extract_segments, remove_segments_from_content, {'layer': 'B.Cu', 'net_id': 2}),
                (via, extract_vias, remove_vias_from_content, {'net_id': 2})):
            content = board(block(B), block(A))
            # Same geometry bucket: first target cannot match yet; second can.
            original = parse(board(block(A), block(B)))
            first = replace(original[0], **delta)
            second = original[1]
            left = []
            out, count = remove(content, [first, second], unmatched_out=left)
            self.assertEqual(count, 1)
            self.assertEqual(len(left), 1)
            self.assertIs(left[0], first)
            out = out.replace('(net 1)', '(net 2)')
            if block is segment:
                out = out.replace('(layer "F.Cu")', '(layer "B.Cu")')
            out, count = remove(out, left)
            self.assertEqual(count, 1)
            self.assertNotIn('(uuid', out)

    def test_mixed_net_dialects_escaped_names_and_quoted_fake_blocks(self):
        raw = r'Net-\"Q\\(x)'
        name = 'Net-"Q\\(x)'
        for block, parse, remove in ((segment, extract_segments, remove_segments_from_content),
                                     (via, extract_vias, remove_vias_from_content)):
            content = board(block(A, net='1'), block(B, net=f'"{raw}"'))
            targets = parse(content, {raw: 1})
            self.assertEqual(len(targets), 2)
            out, count = remove(content, targets, {1: name})
            self.assertEqual(count, 2)
            self.assertNotIn('(uuid', out)
            fake = '(kicad_pcb (gr_text "literal (segment and (via ))" (at 1 2)))'
            self.assertEqual(remove(fake, targets, {1: name}), (fake, 0))

    def test_legacy_tstamp_identity_and_mask_exposed_segment(self):
        text = segment().replace(f'(uuid "{A}")', '(tstamp 12345678)')
        target = replace(extract_segments(board(segment()))[0], uuid=kiid_from_tstamp('12345678'))
        self.assertEqual(remove_segments_from_content(board(text), [target])[1], 1)
        text = segment().replace('(layer "F.Cu")', '(layers "F.Cu" "F.Mask")')
        self.assertEqual(remove_segments_from_content(board(text), [replace(target, uuid=A)])[1], 1)

    def test_lock_token_inside_net_name_cannot_hide_a_real_lock(self):
        for block, parse, remove in ((segment, extract_segments, remove_segments_from_content),
                                     (via, extract_vias, remove_vias_from_content)):
            text = block(A, net='"(locked no)"').replace('(uuid', '(locked yes) (uuid')
            content = board(text)
            target = replace(parse(board(block(A)))[0], locked=False)
            self.assertEqual(remove(content, [target], {1: '(locked no)'}), (content, 0))

if __name__ == '__main__':
    unittest.main()
