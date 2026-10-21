"""KICADROUTE-87: restored copper and newly rescued copper share real custody."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py_router'))
from kicad_parser import BoardInfo, Net, PCBData, Pad, Segment, parse_kicad_pcb
from routing_config import GridRouteConfig
from routing_state import RoutingState
import fab_tiers
import env_knobs
import net_rescue


def broken_restoration():
    pads = [Pad('J'+str(i), '1', x, 2, 0, 0, .4, .4, 'circle', ['F.Cu'], 1, 'N')
            for i, x in enumerate((2, 5))]
    old = Segment(2, 2, 3, 2, .2, 'F.Cu', 1)
    pcb = PCBData(BoardInfo({0: 'F.Cu', 31: 'B.Cu'}, ['F.Cu', 'B.Cu'],
                           board_bounds=(0, 0, 10, 8)),
                   {1: Net(1, 'N', pads)}, {}, [], [old], {1: pads})
    cfg = GridRouteConfig(layers=['F.Cu', 'B.Cu'], grid_step=.1,
                          track_width=.2, clearance=.2, via_size=.6, via_drill=.3)
    reg = {'net_name': 'N', 'net_id': 1, 'is_existing_route': True,
           'new_segments': [old], 'new_vias': [],
           'failed_pads_info': [{'component_ref': 'J1', 'pad_number': '1'}]}
    state = RoutingState(pcb, cfg, routed_net_ids=[1], routed_results={1: reg})
    return state, reg, old


class RescueCustodyTests(unittest.TestCase):
    def setUp(self):
        self.policy = fab_tiers.get_escalation_policy()
        self.flags = {name: getattr(env_knobs, name) for name in
                      ('BARE_PAD_ESCAPE', 'NET_RESCUE', 'TERMINAL_ESCALATION')}
        env_knobs.BARE_PAD_ESCAPE = False
        env_knobs.NET_RESCUE = env_knobs.TERMINAL_ESCALATION = True
        fab_tiers.set_escalation_policy('off')

    def tearDown(self):
        for name, value in self.flags.items():
            setattr(env_knobs, name, value)
        fab_tiers.set_escalation_policy(*self.policy)

    def test_real_rescue_of_restored_registration_reaches_write_list(self):
        state, registration, old = broken_restoration()
        # These are routing policies, not substitutes for route or state objects.
        fab_tiers.set_escalation_policy('off')
        with contextlib.redirect_stdout(io.StringIO()):
            summary = net_rescue.rescue_failed_nets(state, [('N', 1)])
        self.assertEqual(summary['recovered'], ['N'])
        self.assertIs(state.routed_results[1], registration)
        self.assertGreater(len(registration['new_segments']), 1)
        self.assertTrue(state.results, 'rescued copper must reach the writer, not only the registration')
        emitted = [seg for result in state.results for seg in result['new_segments']]
        self.assertNotIn(old, emitted)
        self.assertEqual({id(s) for s in emitted},
                         {id(s) for s in registration['new_segments'] if s is not old})

    def test_actual_rescue_and_escalation_emit_reconnection_once_with_unchanged_rules(self):
        from route import _prune_superseded_results
        from output_writer import write_routed_output
        from native_lifecycle_support import write_input, native_grade
        from design_rules import DesignRules
        from collections import Counter

        root = Path(os.environ.get('CUSTODY_EVIDENCE') or tempfile.mkdtemp(prefix='krt-custody-'))
        for producer in ('rescue', 'escalation'):
            with self.subTest(producer=producer):
                directory = root / producer
                directory.mkdir(parents=True, exist_ok=True)
                state, reg, old = broken_restoration()
                source = directory / 'input.kicad_pcb'
                write_input(source, state.pcb_data)
                original_rules = source.with_suffix('.kicad_pro').read_bytes()
                before = native_grade(source)
                self.assertTrue(before['unconnected_items'])
                if producer == 'escalation':
                    # Tightened .4 request may descend only to the original .2 rule.
                    fab_tiers.set_escalation_policy('board', {'track_width': .2,
                        'clearance': .2, 'via_diameter': .6, 'via_drill': .3})
                    state.config.track_width = .4
                    state.config.rules = DesignRules(board_min={'min_track_width': .2,
                        'min_clearance': .2, 'min_via_diameter': .6,
                        'min_through_hole_diameter': .3})
                    run = net_rescue.terminal_geometry_escalation
                else:
                    fab_tiers.set_escalation_policy('off')
                    run = net_rescue.rescue_failed_nets
                with contextlib.redirect_stdout(io.StringIO()) as log:
                    answer = run(state, [('N', 1)])
                    self.assertIsNotNone(answer)
                    self.assertTrue(state.results)
                    self.assertEqual(_prune_superseded_results(state.results, state.routed_results), 0)
                    dest = directory / 'output.kicad_pcb'
                    self.assertTrue(write_routed_output(str(source), str(dest), state.results,
                        [], [], [], [], [], state.pcb_data))
                (directory / 'routing.log').write_text(log.getvalue())
                self.assertEqual(source.with_suffix('.kicad_pro').read_bytes(), original_rules)
                self.assertEqual(dest.with_suffix('.kicad_pro').read_bytes(), original_rules)
                after = native_grade(dest)
                self.assertEqual(dest.with_suffix('.kicad_pro').read_bytes(), original_rules)
                self.assertFalse(after['unconnected_items'])
                self.assertFalse(any(v['type'] in {'clearance', 'shorting_items',
                    'track_width', 'tracks_crossing', 'via_diameter', 'hole_clearance'}
                    for v in after['violations']), after['violations'])
                emitted = parse_kicad_pcb(str(dest)).segments
                signature = lambda seg: (seg.start_x, seg.start_y, seg.end_x,
                                         seg.end_y, seg.width, seg.layer, seg.net_id)
                counts = Counter(signature(seg) for seg in emitted)
                self.assertTrue(counts)
                self.assertTrue(all(n == 1 for n in counts.values()), counts)
                self.assertEqual(counts[signature(old)], 1)

    def test_partial_then_repeated_rescue_keeps_additions_once_and_later_rip_discards_riders(self):
        from route import _prune_superseded_results
        from rip_up_reroute import rip_up_net
        state, reg, old = broken_restoration()
        extra = Pad('J2', '1', 8, 2, 0, 0, .4, .4, 'circle', ['F.Cu'], 1, 'N')
        state.pcb_data.pads_by_net[1].append(extra)
        reg['failed_pads_info'].append({'component_ref': 'J2', 'pad_number': '1'})
        with contextlib.redirect_stdout(io.StringIO()):
            partial = net_rescue.rescue_failed_nets(state, [('N', 1)],
                cancel_check=lambda: len(state.pcb_data.segments) > 1)
            self.assertEqual(partial['improved'], ['N'])
            finished = net_rescue.rescue_failed_nets(state, [('N', 1)])
            self.assertEqual(finished['recovered'], ['N'])
            emitted = [seg for result in state.results for seg in result['new_segments']]
            self.assertNotIn(old, emitted)
            self.assertEqual(len(emitted), len({id(seg) for seg in emitted}))
            self.assertEqual({id(seg) for seg in emitted},
                             {id(seg) for seg in reg['new_segments'] if seg is not old})
            self.assertIsNone(net_rescue.rescue_failed_nets(state, [('N', 1)]))
            self.assertEqual(_prune_superseded_results(state.results, state.routed_results), 0)
            saved, ids, was_written = rip_up_net(1, state.pcb_data, state.routed_net_ids,
                state.routed_net_paths, state.routed_results, state.diff_pair_by_net_id,
                state.remaining_net_ids, state.results, state.config, history_conflict=False)
            self.assertIs(saved, reg)
            self.assertEqual(ids, [1])
            self.assertFalse(state.pcb_data.segments)
            self.assertGreater(_prune_superseded_results(state.results, state.routed_results), 0)
            self.assertEqual(state.results, [])

    def test_ordinary_result_has_no_rider_and_superseded_registration_loses_its_rider(self):
        from route import _prune_superseded_results
        for ordinary in (False, True):
            with self.subTest(ordinary=ordinary):
                state, reg, old = broken_restoration()
                if ordinary:
                    reg.pop('is_existing_route')
                    state.results.append(reg)
                with contextlib.redirect_stdout(io.StringIO()):
                    net_rescue.rescue_failed_nets(state, [('N', 1)])
                self.assertEqual(len(state.results), 1)
                self.assertEqual(state.results[0] is reg, ordinary)
                replacement = {'net_id': 1, 'new_segments': [], 'new_vias': []}
                state.routed_results[1] = replacement
                state.results.append(replacement)
                self.assertEqual(_prune_superseded_results(state.results, state.routed_results), 1)
                self.assertEqual(state.results, [replacement])


if __name__ == '__main__':
    unittest.main()
