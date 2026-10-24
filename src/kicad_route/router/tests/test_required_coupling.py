"""Required pair coupling must override the backend's short-leg heuristic."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py_router'))
from kicad_parser import Pad
from routing_config import GridRouteConfig
from diff_pair_multipoint import leg_electrically_short
from synth import make_net, make_pad, make_pcb
from native_lifecycle_support import write_input, native_grade


def terminal(x):
    return tuple(Pad('J', str(i + 1), x, i * 0.45, 0, i * 0.45,
                     .2, .2, 'rect', ['F.Cu'], i + 1, '/D_' + polarity)
                 for i, polarity in enumerate(('P', 'N')))


class RequiredCouplingTests(unittest.TestCase):
    def test_explicit_coupling_overrides_length_heuristic(self):
        default = GridRouteConfig(track_width=.2, diff_pair_gap=.25)
        required = GridRouteConfig(track_width=.2, diff_pair_gap=.25, defer_short_pairs=False)
        for distance in (1.0, 2.0, 4.0):
            with self.subTest(distance=distance):
                self.assertTrue(leg_electrically_short(terminal(0), terminal(distance), default))
                self.assertFalse(leg_electrically_short(terminal(0), terminal(distance), required))
        self.assertFalse(leg_electrically_short(terminal(0), terminal(10), default))

    def test_native_short_pair_is_attempted_and_connects_when_deferral_disabled(self):
        with tempfile.TemporaryDirectory(prefix='required-coupling-') as directory:
            root = Path(directory)
            pads = {i: [make_pad(i, x, 4 + (i - 1) * .45, ref=f'J{i}{j}',
                                     size_x=.2, size_y=.2, net_name=name)
                        for j, x in enumerate((3, 5))]
                    for i, name in ((1, '/D_P'), (2, '/D_N'))}
            pcb = make_pcb(nets={i: make_net(i, p[0].net_name) for i, p in pads.items()}, pads_by_net=pads)
            original = root / 'pair.kicad_pcb'
            write_input(original, pcb)
            source_bytes = original.read_bytes()
            results = {}
            for label, flags in (('default', []), ('required', ['--no-short-pair-deferral'])):
                work = root / label
                work.mkdir()
                output = work / 'pair.kicad_pcb'
                argv = [sys.executable, str(Path(__file__).resolve().parents[1] / 'py_router/route_diff.py'),
                        str(original), '--output', str(output), '--track-width', '.2', '--diff-pair-gap', '.25',
                        '--no-fix-drc-settings', '--keep-input-copper', '--no-stub-layer-swap', '--no-gnd-vias',
                        *flags, '--nets', '/D_P', '/D_N']
                env = {**os.environ, 'KICAD_CACHE_HOME': str(work / 'cache')}
                done = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                summaries = [json.loads(s.split('JSON_SUMMARY: ', 1)[1]) for s in done.stdout.splitlines()
                             if s.startswith('JSON_SUMMARY: ')]
                self.assertEqual(len(summaries), 1, done.stdout)
                results[label] = summaries[0]['pair_reports'][0]
                if label == 'required':
                    shutil.copyfile(original.with_suffix('.kicad_pro'), output.with_suffix('.kicad_pro'))
                    native = native_grade(output)
                    self.assertEqual(native['unconnected_items'], [])
                    self.assertEqual([v for v in native['violations'] if v['severity'] == 'error'], [])
                    self.assertEqual([v for v in native['violations'] if v['type'] == 'track_dangling'], [])
            self.assertEqual(original.read_bytes(), source_bytes)
            self.assertEqual(results['default']['outcome'], 'deferred')
            self.assertEqual(results['required']['outcome'], 'coupled')
            self.assertNotEqual(results['required'].get('escape'), 'hybrid')
            self.assertIsNot(results['required'].get('coupled_terminals'), False)


if __name__ == '__main__':
    unittest.main()
