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


def test_offset_gap_connects_two_surfaces_on_the_internal_layers(tmp_path, guarded_cli):
    board = BoardBuilder(copper_layers=4, seed=1399)
    board.set_rect_outline(0, 0, 48_000_000, 32_000_000)
    def point(x, y):
        return pcbnew.FromMM(x), pcbnew.FromMM(y)

    for ref, net, xy, face in [("J1", "SIGNAL", (4.48, 12.3), pcbnew.F_Cu),
                               ("J2", "SIGNAL", (43.2, 13.65), pcbnew.F_Cu),
                               ("R1", "AUX", (2, 3.2), pcbnew.B_Cu),
                               ("R2", "AUX", (8.4, 3.2), pcbnew.B_Cu)]:
        at = point(*xy)
        fp = board.add_footprint(ref, at, side="F" if face == pcbnew.F_Cu else "B")
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        board.add_pad(fp, "1", net, at, size_nm=point(.75, .65),
                      shape=pcbnew.PAD_SHAPE_ROUNDRECT, layers=[face])
    for layers, bounds in [([pcbnew.F_Cu, pcbnew.B_Cu], (10, -1, 38, 33)),
                           ([pcbnew.In1_Cu, pcbnew.In2_Cu], (23, -1, 24, 20))]:
        x, y, u, v = bounds
        board.add_keepout(layers, [point(x, y), point(u, y), point(u, v), point(x, v)],
                          no_tracks=True, no_vias=False)
    source = board.save(tmp_path / "input").pcb_path
    original = run_native_drc(source).raw
    assert len(original["unconnected_items"]) == 2 and not original["violations"]
    saved, op = tmp_path / "routed.kicad_pcb", tmp_path / "op.json"
    run, _ = guarded_cli
    run([str(_built_rust_engine(run)), "route-native", str(source), str(saved),
         "--operation-result", str(op)])
    assert json.loads(op.read_bytes())["routing"]["added_via_count"] == 2
    drc = run_native_drc(saved, sidecar_source=source).raw
    assert not drc["unconnected_items"] and not drc["violations"]
    reopened = pcbnew.LoadBoard(str(saved))
    inner = [p for p in reopened.GetTracks() if p.GetNetname() == "SIGNAL"
             and p.GetLayer() in (pcbnew.In1_Cu, pcbnew.In2_Cu)]
    assert inner and any(max(t.GetStartY(), t.GetEndY()) > pcbnew.FromMM(20) for t in inner)
