"""Competing multilayer nets share the original expansion and work limits."""
import json

import pcbnew

from test_rust_engine_native import _built_rust_engine, guarded_cli as guarded_cli
from tests.e2e_rust_router.kicad_verify import BoardBuilder, run_native_drc


def test_interleaved_surface_bank_routes_every_contact(tmp_path, guarded_cli):
    board = BoardBuilder(copper_layers=4, seed=6103)
    board.set_rect_outline(0, 0, 40_000_000, 30_000_000)
    for index in range(12):
        net = f"S{index:02}"
        destination = (index * 5) % 12
        for suffix, x, pin, side in [("A", 3_100_000, index, "F"),
                                     ("B", 36_700_000, destination, "B" if index % 2 else "F")]:
            point = (x, 4_000_000 + pin * 2_000_000)
            component = board.add_footprint(net + suffix, point, side=side)
            component.Reference().SetVisible(False)
            component.Value().SetVisible(False)
            board.add_pad(component, "1", net, point, size_nm=(900_000, 700_000),
                          shape=pcbnew.PAD_SHAPE_ROUNDRECT,
                          layers=[pcbnew.F_Cu if side == "F" else pcbnew.B_Cu])
    for x, low, high in [(12_000_000, 7_000_000, 27_000_000),
                         (27_000_000, 2_000_000, 22_000_000)]:
        ring = [(x, low), (x + 1_000_000, low), (x + 1_000_000, high), (x, high)]
        board.add_keepout([pcbnew.F_Cu], ring, no_tracks=True, no_vias=False)
    board.set_project_rules(
        rules=dict(min_track_width=.2, min_clearance=.15,
                   min_copper_edge_clearance=.4, min_via_diameter=.6),
        classes=[dict(name="Default", priority=0, clearance=.15,
                      track_width=.2, via_diameter=.6, via_drill=.3)],
    )
    source = board.save(tmp_path / "input").pcb_path
    original = run_native_drc(source).raw
    assert len(original["unconnected_items"]) == 12 and not original["violations"]
    run, _ = guarded_cli
    engine = _built_rust_engine(run)
    saved, op = tmp_path / "routed.kicad_pcb", tmp_path / "op.json"
    run([str(engine), "route-native", str(source), str(saved), "--operation-result", str(op)])
    routing = json.loads(op.read_bytes())["routing"]
    assert routing["status"] == "complete" and 0 < routing["expanded_states"] <= 100_000
    drc = run_native_drc(saved, sidecar_source=source).raw
    assert not drc["unconnected_items"] and not drc["violations"]
    reopened = pcbnew.LoadBoard(str(saved))
    assert reopened.GetCopperLayerCount() == 4 and len(list(reopened.GetPads())) == 24
    for piece in reopened.GetTracks():
        if isinstance(piece, pcbnew.PCB_VIA):
            assert piece.GetWidth() >= 600_000 and piece.GetDrillValue() >= 300_000
        else:
            assert piece.GetWidth() >= 200_000

    # A bounded failure with twelve original opens must not publish any copper.
    limited, denied = tmp_path / "limited.kicad_pcb", tmp_path / "denied.json"
    result = run([str(engine), "route-native", str(source), str(limited), "--max-expansions", "1",
                  "--operation-result", str(denied)], check=False)
    failure = json.loads(denied.read_bytes())
    assert result.returncode == 4 and failure["routing"]["expanded_states"] == 1
    assert failure["status"] == "budget_exhausted" and not limited.exists()
