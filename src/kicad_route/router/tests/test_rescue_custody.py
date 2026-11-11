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

if __name__ == '__main__':
    unittest.main()
