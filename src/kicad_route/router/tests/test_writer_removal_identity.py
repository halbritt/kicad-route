"""Identity-safe production text removal, without native KiCad or routing."""
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

    def test_uuid_mismatch_never_falls_back_to_other_geometry_twin(self):
        for block, parse, remove, changes in (
                (segment, extract_segments, remove_segments_from_content,
                 ({'width': .4}, {'layer': 'B.Cu'}, {'net_id': 2}, {'start_x': 2}, {'start_x': 1.0000004})),
                (via, extract_vias, remove_vias_from_content,
                 ({'size': .8}, {'drill': .4}, {'layers': ['F.Cu', 'In1.Cu']}, {'net_id': 2}, {'x': 4}, {'x': 2.0000004}))):
            for delta in changes:
                with self.subTest(kind=block.__name__, delta=delta):
                    content = board(block(A), block(B))
                    target = parse(content)[0]
                    for changed in (replace(target, uuid=C), replace(target, **delta)):
                        left = []
                        self.assertEqual(remove(content, [changed], unmatched_out=left), (content, 0))
                        self.assertEqual(len(left), 1)
                        self.assertIs(left[0], changed)

    def test_uuidless_full_geometry_and_counted_multiplicity(self):
        for block, parse, remove, different in (
                (segment, extract_segments, remove_segments_from_content, segment(C, width='.4')),
                (via, extract_vias, remove_vias_from_content, via(C, drill='.4'))):
            content = board(different, block(A, lock='locked'), block(B), block(C))
            target = replace(next(t for t in parse(content) if t.uuid == B), uuid='')
            left = []
            out, count = remove(content, [target], unmatched_out=left)
            self.assertEqual(count, 1)
            self.assertEqual(out, content.replace(block(B), ''))
            self.assertEqual(left, [])
            out, count = remove(board(block(B)), [target, target], unmatched_out=left)
            self.assertEqual(count, 1)
            self.assertEqual(len(left), 1)
            self.assertIs(left[0], target)

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

    def test_uuidless_via_matches_size_drill_and_layer_span(self):
        for variant in (via(A, size='.8'), via(A, drill='.4'),
                        via(A, layers='"F.Cu" "In1.Cu"')):
            content = board(variant, via(B))
            target = replace(extract_vias(board(via(B)))[0], uuid='')
            out, count = remove_vias_from_content(content, [target])
            self.assertEqual((out, count), (content.replace(via(B), ''), 1))

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

    def test_unlocked_twin_uuid_and_explicit_target_precedes_generic(self):
        for block, parse, remove in ((segment, extract_segments, remove_segments_from_content),
                                     (via, extract_vias, remove_vias_from_content)):
            for reverse in (False, True):
                blocks = [block(A), block(B)]
                content = board(*(reversed(blocks) if reverse else blocks))
                target = next(t for t in parse(content) if t.uuid == B)
                out, count = remove(content, [target])
                self.assertEqual((out, count), (content.replace(block(B), ''), 1))
            explicit = parse(board(block(A)))[0]
            generic = replace(explicit, uuid='')
            left = []
            out, count = remove(board(block(A), block(B)), [generic, explicit], unmatched_out=left)
            self.assertEqual(count, 2)
            self.assertEqual(left, [])

    def test_known_uuid_geometry_mismatch_does_not_remove_matching_other_twin(self):
        for text, parse, remove, delta in (
                (board(segment(A), segment(B, width='.4')), extract_segments,
                 remove_segments_from_content, {'width': .4}),
                (board(via(A), via(B, size='.8')), extract_vias,
                 remove_vias_from_content, {'size': .8})):
            target = replace(parse(text)[0], **delta)
            left = []
            self.assertEqual(remove(text, [target], unmatched_out=left), (text, 0))
            self.assertIs(left[0], target)

    def test_same_bucket_residual_is_actual_missing_uuid(self):
        for block, parse, remove in ((segment, extract_segments, remove_segments_from_content),
                                     (via, extract_vias, remove_vias_from_content)):
            first, second = parse(board(block(A), block(B)))
            left = []
            out, count = remove(board(block(B)), [first, second], unmatched_out=left)
            self.assertEqual(count, 1)
            self.assertEqual(len(left), 1)
            self.assertIs(left[0], first)
            self.assertEqual(remove(out, left), (out, 0))

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

    def test_unsupported_uuidless_vias_stay_unmatched(self):
        target = replace(extract_vias(board(via()))[0], uuid='')
        for text in (via(lock='blind'), via(lock='micro'), via(extra='(padstack (mode custom))')):
            content = board(text)
            left = []
            self.assertEqual(remove_vias_from_content(content, [target], unmatched_out=left), (content, 0))
            self.assertIs(left[0], target)

    def test_malformed_input_refuses_before_removing_anything(self):
        for parse, remove, text in ((extract_segments, remove_segments_from_content, segment()),
                                     (extract_vias, remove_vias_from_content, via())):
            target = parse(board(text))[0]
            with self.assertRaisesRegex(ValueError, 'Truncated|parenthesis'):
                remove(board(text)[:-1], [target])


if __name__ == '__main__':
    unittest.main()
