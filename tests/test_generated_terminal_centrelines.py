"""The short acquisition supply termination needs its strict attachment."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/kicad_route/router/py_router'))
from kicad_parser import BoardInfo, PCBData, Pad, Segment, Net
from routing_config import GridRouteConfig
from check_weird import _check_kicad_dangling
import pcb_modification as modification


def fixture():
    seed = Segment(105.05, 119.9, 106.95, 119.1625, .25, 'F.Cu', 1)
    parent = Segment(105.825, 119.125, 106.125, 119.425, .25, 'F.Cu', 1)
    stub = Segment(106.125, 119.425, 106.125, 119.475, .25, 'F.Cu', 1)
    pads = [Pad(str(i), '1', x, y, 0, 0, .2, .2, 'circle', ['F.Cu'], 1,
                '/V3V3', pad_type='smd') for i, (x, y) in enumerate(
                    [(105.05, 119.9), (105.825, 119.125), (106.95, 119.1625)])]
    pcb = PCBData(BoardInfo({}, ['F.Cu', 'B.Cu'], (100, 100, 140, 130)),
                  {1: Net(1, '/V3V3', pads)}, {}, [], [seed, parent, stub], {1: pads})
    return pcb, [{'new_segments': [parent, stub], 'new_vias': []}], seed, parent, stub


def model(pcb):
    return modification.StrictRemovalModel(1, pcb.segments, pcb.vias, pcb.pads_by_net[1])


def dangling(pcb):
    result = []
    _check_kicad_dangling(1, '/V3V3', pcb.segments, pcb.vias, pcb.pads_by_net[1], [],
                          ['F.Cu', 'B.Cu'], result)
    return result


class TerminalCentrelineTests(unittest.TestCase):
    def test_cleanup_preserves_declared_net_geometry_and_unfinished_nets(self):
        import cleanup_pipeline as cleanup
        import protected_nets as protections
        declarations = [(protections.PRO_KEY, reason) for reason in
                        ('length-matched', 'time-matched', 'diff-pair')]
        declarations += [(protections.IMPEDANCE_KEY, {'ohms': 50})]
        cases = [('none', None, None), ('locked_input', None, None),
                 ('unfinished', None, None)]
        cases += [(source, key, value) for source in ('live', 'pending')
                  for key, value in declarations]
        for source, key, value in cases:
            with self.subTest(source=source, key=key, value=value):
                pcb, results, seed, parent, stub = fixture()
                if source == 'locked_input':
                    seed.locked = True
                records = {key: {'/V3V3': value}} if key else {}
                if source == 'live':
                    pcb.live_records_provider = lambda: records
                before = copy.deepcopy(pcb.segments)
                pending = records if source == 'pending' else {}
                with mock.patch.dict(protections._notes, pending, clear=True):
                    outcome = cleanup.run_post_route_cleanup(
                        results, pcb, {1}, GridRouteConfig(clearance=.2),
                        protect_net_ids={1} if source == 'unfinished' else None,
                        snap=False, phantom=False, graze=False, octolinear=False,
                        via_nudge=False, cycles=False, neck=False, smooth=False,
                        microshift_max_shift=0, merge_collinear=False,
                        strict_collapse=False, keep_input_copper=True)
                    self.assertEqual(protections._notes, pending)
                should_repair = source in ('none', 'locked_input')
                self.assertEqual(outcome.counts['terminal_centrelines_repaired'],
                                 int(should_repair))
                self.assertEqual(seed, before[0])
                self.assertEqual(outcome.input_strip_segments, [])
                if not should_repair:
                    self.assertEqual(pcb.segments, before)

    def test_native_dangling_termination_keeps_strict_connection_to_input_trace(self):
        pcb, results, seed, parent, stub = fixture()
        before = copy.deepcopy(seed)
        self.assertEqual([(f['x'], f['y']) for f in dangling(pcb)], [(106.125, 119.475)])
        graph = model(pcb)
        self.assertTrue(graph.valid)
        self.assertFalse(graph.trial((), (), [2])[0], 'deletion alone loses strict attachment')
        self.assertEqual(modification.repair_generated_terminal_centrelines(
            results, pcb, {1}, GridRouteConfig(clearance=.2)), 1)
        self.assertEqual(seed, before)
        self.assertIs(pcb.segments[0], seed)
        self.assertEqual(len(pcb.segments), 2)
        replacement = pcb.segments[1]
        self.assertIs(results[0]['new_segments'][0], replacement)
        self.assertEqual(len(results[0]['new_segments']), 1)
        self.assertEqual((replacement.start_x, replacement.start_y), (105.825, 119.125))
        self.assertEqual((replacement.end_x, replacement.end_y), (106.166588, 119.466588))
        self.assertEqual((replacement.width, replacement.layer, replacement.net_id), (.25, 'F.Cu', 1))
        self.assertEqual(dangling(pcb), [])
        self.assertTrue(model(pcb).valid)

    def test_immutable_or_branched_terminals_are_left_unchanged(self):
        for mode in ('input_parent', 'locked_parent', 'locked_stub', 'branch', 'via', 'protected_net'):
            with self.subTest(mode=mode):
                pcb, results, seed, parent, stub = fixture()
                if mode == 'input_parent':
                    results[0]['new_segments'].remove(parent)
                elif mode == 'locked_parent':
                    parent.locked = True
                elif mode == 'locked_stub':
                    stub.locked = True
                elif mode == 'branch':
                    pcb.segments.append(Segment(106.125, 119.475, 106.125, 120, .25, 'F.Cu', 1))
                elif mode == 'via':
                    from kicad_parser import Via
                    pcb.vias.append(Via(106.125, 119.475, .6, .3, ['F.Cu', 'B.Cu'], 1))
                before = copy.deepcopy(pcb.segments)
                old_list = results[0]['new_segments']
                self.assertEqual(modification.repair_generated_terminal_centrelines(
                    results, pcb, {1}, GridRouteConfig(clearance=.2),
                    protect_net_ids={1} if mode == 'protected_net' else None), 0)
                self.assertEqual(pcb.segments, before)
                self.assertIs(results[0]['new_segments'], old_list)

    def test_clearance_keepout_edge_and_budget_refusals_are_atomic(self):
        for mode in ('foreign_track', 'keepout', 'edge', 'comparisons', 'repairs', 'parallel_target'):
            with self.subTest(mode=mode):
                pcb, results, seed, parent, stub = fixture()
                kwargs = {}
                if mode == 'foreign_track':
                    pcb.segments.append(Segment(106.15, 119.45, 106.2, 119.5, .25, 'F.Cu', 2))
                elif mode == 'keepout':
                    pcb.board_info.keepouts = [dict(layers={'F.Cu'}, tracks_allowed=False,
                        polygon=[(106.15,119.45),(106.2,119.45),(106.2,119.5),(106.15,119.5)])]
                elif mode == 'edge':
                    pcb.board_info.board_bounds = (100,100,106.18,130)
                elif mode == 'parallel_target':
                    seed.start_x, seed.start_y, seed.end_x, seed.end_y = 105,118.35,108,121.35
                else:
                    kwargs['max_'+mode] = 0
                before = copy.deepcopy(pcb.segments)
                old_list = results[0]['new_segments']
                self.assertEqual(modification.repair_generated_terminal_centrelines(
                    results, pcb, {1}, GridRouteConfig(clearance=.2), **kwargs), 0)
                self.assertEqual(pcb.segments, before)
                self.assertIs(results[0]['new_segments'], old_list)

    def test_native_drc_accepts_the_generated_centreline_termination(self):
        import pcbnew as k
        pcb, results, seed, parent, stub = fixture()
        with tempfile.TemporaryDirectory(prefix='native-terminal-') as directory:
            root = Path(directory)
            def observe(name):
                board = k.BOARD(); board.SetCopperLayerCount(2)
                net = k.NETINFO_ITEM(board, '/V3V3'); board.Add(net)
                for p in pcb.pads_by_net[1]:
                    fp = k.FOOTPRINT(board); fp.SetReference(p.component_ref); board.Add(fp)
                    pad = k.PAD(fp); pad.SetNumber(p.pad_number)
                    pad.SetAttribute(k.PAD_ATTRIB_SMD); pad.SetShape(k.PAD_SHAPE_CIRCLE)
                    pad.SetLayerSet(k.LSET.FrontMask()); pad.SetSize(k.VECTOR2I(200000,200000))
                    fp.Add(pad); pad.SetPosition(k.VECTOR2I(round(p.global_x*1e6),round(p.global_y*1e6)))
                    pad.SetNet(net)
                for s in pcb.segments:
                    item = k.PCB_TRACK(board)
                    item.SetStart(k.VECTOR2I(round(s.start_x*1e6),round(s.start_y*1e6)))
                    item.SetEnd(k.VECTOR2I(round(s.end_x*1e6),round(s.end_y*1e6)))
                    item.SetWidth(round(s.width*1e6)); item.SetLayer(k.F_Cu); item.SetNet(net)
                    board.Add(item)
                path = root/(name+'.kicad_pcb'); k.SaveBoard(str(path),board)
                report = root/(name+'.json')
                env = dict(os.environ,XDG_CONFIG_HOME=str(root/'config'),
                           XDG_CACHE_HOME=str(root/'cache'),XDG_DATA_HOME=str(root/'data'))
                run = subprocess.run(['kicad-cli','pcb','drc','--format','json','--severity-all',
                                      '--output',str(report),str(path)],env=env,capture_output=True,timeout=30)
                self.assertEqual(run.returncode,0,run.stderr.decode(errors='replace'))
                data = json.loads(report.read_bytes())
                self.assertEqual(data['unconnected_items'],[])
                return [v for v in data['violations'] if v['type']=='track_dangling']
            self.assertEqual(len(observe('before')),1)
            self.assertEqual(modification.repair_generated_terminal_centrelines(
                results,pcb,{1},GridRouteConfig(clearance=.2)),1)
            self.assertEqual(observe('after'),[])

    def test_unsupported_rule_channels_leave_the_proposal_unmodified(self):
        for field, value in (('layer_clearances', {'F.Cu': .3}),
                             ('track_clearances', {2: .4}), ('keepout_enabled', True),
                             ('pour_barriers', [{'net': 2, 'layer': 'F.Cu'}])):
            with self.subTest(field=field):
                pcb, results, seed, parent, stub = fixture()
                before = copy.deepcopy(pcb.segments)
                config = GridRouteConfig(clearance=.2)
                setattr(config, field, value)
                stats = {}
                self.assertEqual(modification.repair_generated_terminal_centrelines(
                    results, pcb, {1}, config, stats=stats), 0)
                self.assertEqual(pcb.segments, before)
                self.assertIn(field, stats['unsupported_channels'])

    def test_declared_and_local_hole_clearance_can_refuse_an_otherwise_legal_extension(self):
        for mode in ('base', 'declared', 'local'):
            with self.subTest(mode=mode):
                pcb, results, seed, parent, stub = fixture()
                hole = Pad('H1','1',106.7,119.466588,0,0,.2,.2,'circle',['*.Cu'],0,'',
                           drill=.2,pad_type='np_thru_hole',local_clearance=.4 if mode=='local' else 0)
                pcb.pads_by_net[0] = [hole]
                if mode == 'declared':
                    pcb.live_rules_provider = lambda: {'min_hole_clearance': .4}
                before = copy.deepcopy(pcb.segments)
                changed = modification.repair_generated_terminal_centrelines(
                    results,pcb,{1},GridRouteConfig(clearance=.2))
                self.assertEqual(changed,1 if mode=='base' else 0)
                if mode != 'base':
                    self.assertEqual(pcb.segments,before)


if __name__ == '__main__':
    unittest.main()
