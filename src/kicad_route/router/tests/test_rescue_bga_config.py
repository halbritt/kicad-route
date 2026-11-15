"""BGA rescue prepares its configuration even when the net owns no plane."""
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'py_router'))
import net_rescue
import plane_pad_tap
from routing_config import GridRouteConfig


class RescueConfigTests(unittest.TestCase):
    def test_bga_rescue_without_a_plane_owner_reaches_the_search(self):
        for costs in (None, [1.0, 9.0, 9.0, 1.0]):
            with self.subTest(layer_costs=costs):
                config = GridRouteConfig(layers=['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'],
                    layer_costs=costs, bga_exclusion_zones=[(0.0, 0.0, 1.0, 1.0)],
                    track_width=0.25, clearance=0.2, via_size=0.6, via_drill=0.3)
                pcb = SimpleNamespace(zones=[], nets={})
                seen = []

                def rungs(candidate, *args):
                    seen.append(candidate)
                    return [candidate]

                with patch.object(plane_pad_tap, 'make_local_window', return_value=object()), \
                     patch.object(net_rescue, '_rescue_rungs', side_effect=rungs):
                    result = net_rescue._attempt_edge(pcb, 1, (1.0, 0.0, 0.0, 1.0, 0.0),
                        config, {}, cancel_check=lambda: True)
                self.assertEqual(result, (None, None))
                self.assertEqual(len(seen), 1)
                candidate = seen[0]
                self.assertEqual(candidate.bga_exclusion_zones, [])
                self.assertEqual((candidate.track_width, candidate.clearance, candidate.via_size, candidate.via_drill),
                                 (0.25, 0.2, 0.6, 0.3))
                self.assertEqual(candidate.layer_costs, costs)
                self.assertEqual(config.bga_exclusion_zones, [(0.0, 0.0, 1.0, 1.0)])


if __name__ == '__main__':
    unittest.main()
