"""Independent symmetry/clearance assertions, run with the engine's interpreter."""
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/kicad_route/router/py_router'))
from diff_pair_routing import _process_via_positions
from routing_config import GridCoord, GridRouteConfig


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


config = GridRouteConfig(track_width=.25, diff_pair_gap=.35, clearance=.2,
                         via_size=.6, via_drill=.3, grid_step=.1)
coord = GridCoord(.1)
for dx, dy in [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]:
    norm = math.hypot(dx, dy)
    px, py = -dy / norm, dx / norm
    path = [(0, 0, 0), (100 * dx, 100 * dy, 0),
            (100 * dx, 100 * dy, 1), (200 * dx, 200 * dy, 1)]
    for sign in [-1, 1]:
        paths = [[(x * .1 + side * px * .3, y * .1 + side * py * .3, layer)
                  for x, y, layer in path] for side in [sign, -sign]]
        positive, negative = _process_via_positions(path, *paths, coord, config, sign, -sign, .3)
        # Approaches and exits stay centered; both members have equal travel.
        for i in [1, 4]:
            assert math.isclose((positive[i][0] + negative[i][0]) / 2, dx * 10, abs_tol=1e-9)
            assert math.isclose((positive[i][1] + negative[i][1]) / 2, dy * 10, abs_tol=1e-9)
            assert math.isclose(distance(positive[i], negative[i]), .6, abs_tol=1e-9)
        lengths = [sum(distance(a, b) for a, b in zip(points, points[1:]) if a[2] == b[2])
                   for points in [positive, negative]]
        assert math.isclose(*lengths, abs_tol=1e-9), lengths
        # Edge-to-edge via/member and via/via distances meet the native class.
        assert distance(positive[2], negative[2]) - .6 >= .2 - 1e-9
        assert distance(positive[2], negative[1]) - .3 - .125 >= .2 - 1e-9
        assert distance(negative[2], positive[1]) - .3 - .125 >= .2 - 1e-9
print('16 straight-transition directions/polarities preserve symmetry and clearance')
