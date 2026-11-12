"""The builtin router's search grid follows the lattice the board's pads sit on (cli.resolve_grid)."""
from kicad_route import cli as route

MIL_5 = 127_000


def facts(positions, unconnected=()):
    pads = {str(i): {"position": list(p), "net": "/N"} for i, p in enumerate(positions)}
    pads.update({f"u{i}": {"position": list(p), "net": ""} for i, p in enumerate(unconnected)})
    return {"footprints": {"U1": {"pads": pads}}}


def test_pads_are_counted_on_each_lattice():
    board = facts([(101 * MIL_5, 203 * MIL_5), (101 * MIL_5 + 900, 203 * MIL_5 - 900),     # on 5 mil, to a micrometre
                   (1_000_000, 2_500_000),                                                 # on 0.1 mm
                   (12_700_000, 25_400_000),                                               # on both
                   (1_050_000, 2_000_000), (101 * MIL_5 + 2_000, 203 * MIL_5)],            # on neither
                  unconnected=[(3 * MIL_5, 4 * MIL_5)])
    assert route.pad_lattice(board) == {"pads": 6, "on_0.1": 2, "on_0.127": 3}
    assert route.pad_lattice({}) == {"pads": 0, "on_0.1": 0, "on_0.127": 0}


def test_a_board_drawn_in_mils_is_routed_on_the_5_mil_grid():
    dip = facts([(79 * 1_270_000 + k * 2_540_000, 60 * 1_270_000 + row * 7_620_000) for k in range(8) for row in (0, 1)])
    assert route.resolve_grid(None, dip) == (0.127, {"pads": 16, "on_0.1": 0, "on_0.127": 16})


def test_other_boards_keep_the_engines_grid():
    metric = facts([(100_000_000 + k * 500_000, 50_000_000) for k in range(20)])
    step, counts = route.resolve_grid(None, metric)
    assert step is None and counts["on_0.1"] == 20
    # a quarter of the pads on the 5 mil lattice (the connectors of a surface-mount board) is not most of them
    mixed = facts([(k * MIL_5 * 20, 0) for k in range(5)] + [(1_030_000 + k * 650_000, 7_770_000) for k in range(15)])
    step, counts = route.resolve_grid(None, mixed)
    assert step is None and counts == {"pads": 20, "on_0.1": 1, "on_0.127": 5}
    assert route.resolve_grid(None, {}) == (None, {"pads": 0, "on_0.1": 0, "on_0.127": 0})


def test_a_named_step_is_the_callers():
    dip = facts([(k * 2_540_000, 0) for k in range(8)])
    assert route.resolve_grid(0.1, dip) == (0.1, None)
    assert route.resolve_grid(0.05, {}) == (0.05, None)


def test_a_run_on_the_pad_lattice_reserves_thermal_spokes_unless_the_caller_chose():
    counts = {"pads": 16, "on_0.1": 0, "on_0.127": 16}
    assert route.spokes_with_grid(0.127, counts, None, []) == 0            # the board's minimum per pad
    assert route.spokes_with_grid(0.127, counts, 1, []) == 1               # the caller's count
    assert route.spokes_with_grid(0.127, counts, None, ["uuid@F.Cu"]) is None   # the caller's pads (set elsewhere)
    assert route.spokes_with_grid(None, counts, None, []) is None          # the engine's grid: nothing reserved
    assert route.spokes_with_grid(0.127, None, None, []) is None           # a grid the caller named
    assert route.spokes_with_grid(None, None, 0, []) == 0
