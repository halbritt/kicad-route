"""A partial shield permits entry on only one endpoint's lattice."""
import json

import pcbnew
import pytest

from tests.e2e_rust_router.kicad_verify import BoardBuilder
from test_rust_engine_native import _built_rust_engine, bridge, guarded_cli as guarded_cli


@pytest.mark.parametrize("offset,closed", [(40_000, False), (140_000, False), (140_000, True)])
def test_native_terminal_slot_uses_reachable_phase(tmp_path, guarded_cli, offset, closed):
    board = BoardBuilder(copper_layers=4, seed=302)
    board.set_rect_outline(0, 0, 45_000_000, 35_000_000)
    y = 16_000_000 + offset
    pads = [("J1", (4_070_000, 15_040_000), (1_300_000, 900_000)),
            ("J2", (29_110_000, y), (450_000, 180_000))]
    for ref, at, size in pads:
        fp = board.add_footprint(ref, at)
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        board.add_pad(fp, "1", "SENSE", at, shape=pcbnew.PAD_SHAPE_RECT, size_nm=size, layers=[pcbnew.F_Cu])

    def keep_out(left, low, right, high):
        layers = [pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu]
        board.add_keepout(layers, [(left, low), (right, low), (right, high), (left, high)],
                          no_tracks=True, no_vias=True)
    keep_out(20_000_000, 10_000_000, 34_000_000, y - 150_000)
    keep_out(20_000_000, y + 150_000, 34_000_000, 23_000_000)
    if closed:
        keep_out(25_000_000, 14_000_000, 34_000_000, 18_000_000)
    source = board.save(tmp_path / "source").pcb_path
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps(dict(width_nm=250_000, grid_nm=250_000,
                                     clearance_nm=200_000, edge_clearance_nm=500_000)))
    a, b = (p[1] for p in pads)
    # The lateral correction fits in open board space to the left of the slot.
    turn = (a[0], b[1])
    board.add_track("SENSE", a, turn, 250_000)
    board.add_track("SENSE", turn, b, 250_000)
    witness = board.save(tmp_path / "witness").pcb_path
    initial = bridge.native_violations(source, tmp_path / "initial-drc.json")
    drawn = bridge.native_violations(witness, tmp_path / "witness-drc.json")
    assert len(initial["unconnected_items"]) == 1 and not initial["violations"]
    assert not drawn["unconnected_items"]
    assert {v["type"] for v in drawn["violations"]} == ({"items_not_allowed"} if closed else set())

    run, _ = guarded_cli
    engine = _built_rust_engine(run)
    def route(name, *args):
        output, result = tmp_path / (name + ".kicad_pcb"), tmp_path / (name + ".json")
        process = run([str(engine), "route-native", str(source), str(output), "--rules", str(rules),
                       "--operation-result", str(result), *args], check=False)
        return process.returncode, json.loads(result.read_bytes()), output

    code, op, output = route("saved")
    if closed:
        assert (code, op["status"]) == (3, "unroutable"), op.get("reason")
        assert not output.exists()
        return
    assert (code, op["status"]) == (0, "complete"), op.get("reason")
    verified = bridge.native_violations(output, tmp_path / "saved-drc.json")
    assert not verified["violations"] and not verified["unconnected_items"]
    assert op["routing"]["added_track_count"] == 2 and op["routing"]["added_via_count"] == 0
    if offset == 140_000:
        cap = op["routing"]["expanded_states"] - 1
        code, stopped, output = route("tight", "--max-expansions", str(cap))
        assert (code, stopped["status"]) == (4, "budget_exhausted") and not output.exists()
        assert stopped["routing"]["expanded_states"] == cap
