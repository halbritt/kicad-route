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


def test_displaced_slots_on_inner_layers_connect_all_terminals(tmp_path, guarded_cli):
    board = BoardBuilder(copper_layers=4, seed=29701)
    board.set_rect_outline(0, 0, 48_000_000, 32_000_000)

    def point(x, y):
        return pcbnew.FromMM(x), pcbnew.FromMM(y)

    def wall(layers, left, bottom, right, top):
        ring = [point(left, bottom), point(right, bottom), point(right, top), point(left, top)]
        board.add_keepout(layers, ring, no_tracks=True, no_vias=False)

    for ref, net, xy, face in [("A", "DATA", (4.1, 8.35), pcbnew.F_Cu),
                               ("Z", "DATA", (43.55, 21.1), pcbnew.F_Cu),
                               ("C", "CLK", (2, 3), pcbnew.B_Cu),
                               ("D", "CLK", (7, 3), pcbnew.B_Cu)]:
        at = point(*xy)
        fp = board.add_footprint(ref, at, side="F" if face == pcbnew.F_Cu else "B")
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        board.add_pad(fp, "1", net, at, size_nm=point(.7, .6),
                      shape=pcbnew.PAD_SHAPE_RECT, layers=[face])
    wall([pcbnew.F_Cu, pcbnew.B_Cu], 9, -1, 39, 33)
    inner = [pcbnew.In1_Cu, pcbnew.In2_Cu]
    for x, y in [(12, 12.35), (23, 17.65), (32, 13.95)]:
        wall(inner, x, -1, x + 2, y - .28)
        wall(inner, x, y + .28, x + 2, 33)
    board.set_project_rules()
    source = board.save(tmp_path / "source").pcb_path
    before = run_native_drc(source).raw
    assert len(before["unconnected_items"]) == 2 and not before["violations"]

    run, _ = guarded_cli
    engine = _built_rust_engine(run)
    saved, op = tmp_path / "routed.kicad_pcb", tmp_path / "op.json"
    run([str(engine), "route-native", str(source), str(saved), "--operation-result", str(op)])
    routing = json.loads(op.read_bytes())["routing"]
    assert routing["status"] == "complete" and routing["expanded_states"] <= 100_000
    drc = run_native_drc(saved, sidecar_source=source).raw
    assert not drc["unconnected_items"] and not drc["violations"]
    reopened = pcbnew.LoadBoard(str(saved))
    assert sum(isinstance(piece, pcbnew.PCB_VIA) for piece in reopened.GetTracks()) == 2


def test_clock_tree_joins_all_receivers_through_an_inner_channel(tmp_path, guarded_cli):
    board = BoardBuilder(copper_layers=4, seed=8023)
    board.set_rect_outline(0, 0, 54_000_000, 34_000_000)

    def point(x, y):
        return pcbnew.FromMM(x), pcbnew.FromMM(y)

    pads = [(1, "CLK", (4.45, 9.35), pcbnew.F_Cu)]
    pads.extend((i + 2, "CLK", (48.45, 4.05 + 2.25 * i), pcbnew.F_Cu) for i in range(12))
    pads.extend([(20, "REF", (3, 31), pcbnew.B_Cu), (21, "REF", (9, 31), pcbnew.B_Cu)])
    for index, net, xy, side in pads:
        pos = point(*xy)
        fp = board.add_footprint(f"J{index}", pos, side="F" if side == pcbnew.F_Cu else "B")
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        pad = board.add_pad(fp, "1", net, pos, size_nm=point(.76, .6),
                            shape=pcbnew.PAD_SHAPE_RECT, layers=[side])
        pad.SetUuid(pcbnew.KIID(f"16bf72c0-a980-478d-a236-{index:012x}"))

    inner = [pcbnew.In1_Cu, pcbnew.In2_Cu]
    for layers, x, bottom, right, top in [
        ([pcbnew.F_Cu, pcbnew.B_Cu], 10, -1, 41, 35),
        (inner, 16, -1, 17.4, 17), (inner, 26.5, 11, 27.9, 35), (inner, 37, -1, 38.4, 19),
    ]:
        corners = [point(x, bottom), point(right, bottom), point(right, top), point(x, top)]
        board.add_keepout(layers, corners, no_tracks=True, no_vias=False)
    board.set_project_rules()
    source = board.save(tmp_path / "source").pcb_path
    before = run_native_drc(source).raw
    assert len(before["unconnected_items"]) == 13 and not before["violations"]
    run, _ = guarded_cli
    saved, op = tmp_path / "routed.kicad_pcb", tmp_path / "route.json"
    run([str(_built_rust_engine(run)), "route-native", str(source), str(saved),
         "--operation-result", str(op)])
    result = json.loads(op.read_bytes())["routing"]
    assert result["status"] == "complete" and result["expanded_states"] <= 100_000
    reopened = pcbnew.LoadBoard(str(saved))
    assert len(list(reopened.GetPads())) == 15
    assert any(piece.GetLayer() in inner for piece in reopened.GetTracks())
    after = run_native_drc(saved, sidecar_source=source).raw
    assert not after["unconnected_items"] and not after["violations"]


def test_inner_land_grid_pads_escape_to_drills_outside_the_package(tmp_path, guarded_cli):
    board = BoardBuilder(copper_layers=4, seed=36018)
    board.set_rect_outline(0, 0, 50_000_000, 32_000_000)
    stack = [pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu]

    def point(x, y):
        return pcbnew.FromMM(x), pcbnew.FromMM(y)

    def keepout(layers, x, y, right, top, *, tracks, vias):
        ring = [point(x, y), point(right, y), point(right, top), point(x, top)]
        board.add_keepout(layers, ring, no_tracks=tracks, no_vias=vias)

    for ref, x, y, layer in [("U1", 9, 10, pcbnew.F_Cu), ("U2", 41, 20, pcbnew.B_Cu)]:
        chip = board.add_footprint(ref, point(x, y), side="F" if layer == pcbnew.F_Cu else "B")
        chip.Value().SetVisible(False)
        chip.Reference().SetVisible(False)
        for col in range(-2, 3):
            for row in range(-2, 3):
                board.add_pad(chip, f"{chr(67 + row)}{col + 3}", "CLK" if (col, row) == (0, 0) else "",
                              point(x + col, y + row), size_nm=point(.45, .45),
                              shape=pcbnew.PAD_SHAPE_CIRCLE, layers=[layer])
        keepout(stack, x - 3.25, y - 3.25, x + 3.25, y + 3.25, tracks=False, vias=True)
    keepout([pcbnew.F_Cu, pcbnew.B_Cu], 15, -1, 35, 33, tracks=True, vias=False)
    for ref, x in [("R1", 4), ("R2", 10)]:
        part = board.add_footprint(ref, point(x, 28))
        part.Reference().SetVisible(False)
        part.Value().SetVisible(False)
        board.add_pad(part, "1", "REF", point(x, 28), size_nm=point(.7, .6),
                      shape=pcbnew.PAD_SHAPE_RECT, layers=[pcbnew.F_Cu])
    board.set_project_rules(
        rules=dict(min_track_width=.2, min_clearance=.15, min_copper_edge_clearance=.4,
                   min_via_diameter=.6),
        classes=[dict(name="Default", priority=0, clearance=.15, track_width=.2,
                      via_diameter=.6, via_drill=.3)],
    )
    source = board.save(tmp_path / "source").pcb_path
    before = run_native_drc(source).raw
    assert len(before["unconnected_items"]) == 2 and not before["violations"]
    run, _ = guarded_cli
    saved, op = tmp_path / "routed.kicad_pcb", tmp_path / "route.json"
    run([str(_built_rust_engine(run)), "route-native", str(source), str(saved),
         "--operation-result", str(op)])
    result = json.loads(op.read_bytes())["routing"]
    assert result["status"] == "complete" and result["added_via_count"] == 2
    reopened = pcbnew.LoadBoard(str(saved))
    assert len(list(reopened.GetPads())) == 52
    tracks = [t for t in reopened.GetTracks() if t.GetNetname() == "CLK"
              and not isinstance(t, pcbnew.PCB_VIA)]
    for face in (pcbnew.F_Cu, pcbnew.B_Cu):
        assert any(t.GetLayer() == face and t.GetStartX() != t.GetEndX()
                   and t.GetStartY() != t.GetEndY() for t in tracks)
    native = run_native_drc(saved, sidecar_source=source).raw
    assert not native["unconnected_items"] and not native["violations"]


def test_shielded_contacts_reach_drill_windows_between_geometric_samples(tmp_path, guarded_cli):
    board = BoardBuilder(copper_layers=4, seed=10382)
    board.set_rect_outline(0, 0, 40_000_000, 24_000_000)
    front, back = pcbnew.F_Cu, pcbnew.B_Cu
    stack = [front, pcbnew.In1_Cu, pcbnew.In2_Cu, back]

    def point(x, y):
        return pcbnew.FromMM(x), pcbnew.FromMM(y)

    def block(layers, x, y, u, v, *, tracks=False, vias=False):
        ring = [point(x, y), point(u, y), point(u, v), point(x, v)]
        board.add_keepout(layers, ring, no_tracks=tracks, no_vias=vias)

    for ref, net, x, y, layer in [("J1", "SIG", 3.1, 8, front), ("J2", "SIG", 36.9, 13, front),
                                  ("R1", "REF", 3, 2, back), ("R2", "REF", 8, 2, back)]:
        pos = point(x, y)
        part = board.add_footprint(ref, pos, side="F" if layer == front else "B")
        part.Reference().SetVisible(False)
        part.Value().SetVisible(False)
        board.add_pad(part, "1", net, pos, size_nm=point(.7, .6),
                      shape=pcbnew.PAD_SHAPE_RECT, layers=[layer])

    windows = [(5.6, 8), (33.09, 13)]
    for (x, y), (lo, hi) in zip(windows, [(-1, 20), (20, 41)]):
        block(stack, lo, -1, x - .55, 25, vias=True)
        block(stack, x + .55, -1, hi, 25, vias=True)
        block(stack, x - .55, -1, x + .55, y - .65, vias=True)
        block(stack, x - .55, y + .65, x + .55, 25, vias=True)
    block([front, back], 14, -1, 26, 25, tracks=True)
    board.set_project_rules()
    source = board.save(tmp_path / "input").pcb_path
    initial = run_native_drc(source).raw
    assert len(initial["unconnected_items"]) == 2 and not initial["violations"]

    run, _ = guarded_cli
    saved, operation = tmp_path / "routed.kicad_pcb", tmp_path / "operation.json"
    run([str(_built_rust_engine(run)), "route-native", str(source), str(saved),
         "--operation-result", str(operation)])
    assert json.loads(operation.read_bytes())["routing"]["status"] == "complete"
    reopened = pcbnew.LoadBoard(str(saved))
    vias = sorted((via.GetStartX(), via.GetStartY()) for via in reopened.GetTracks()
                  if isinstance(via, pcbnew.PCB_VIA) and via.GetNetname() == "SIG")
    assert len(vias) == 2
    for (vx, vy), (cx, cy) in zip(vias, windows):
        x, y = point(cx, cy)
        assert abs(vx - x) < pcbnew.FromMM(.55) and abs(vy - y) < pcbnew.FromMM(.65)
    verified = run_native_drc(saved, sidecar_source=source).raw
    assert not verified["unconnected_items"] and not verified["violations"]


def test_eight_backplane_channels_share_the_board_search_budget(tmp_path, guarded_cli):
    board = BoardBuilder(copper_layers=4, seed=9831)

    def point(x, y):
        return round(x * 1_000_000), round(y * 1_000_000)

    def barrier(layers, x, y, right, top):
        ring = [point(x, y), point(right, y), point(right, top), point(x, top)]
        board.add_keepout(layers, ring, no_tracks=True, no_vias=False)

    board.set_rect_outline(0, 0, *point(68, 44))
    channels = range(8)
    for index in channels:
        y = 4.5 + index * 5
        for ref, at, side in [("A", point(3.35, y), "F"),
                              ("B", point(64.65, y + .25), "B" if index % 2 else "F")]:
            fp = board.add_footprint(f"{ref}{index + 1}", at, side=side)
            fp.Reference().SetVisible(False)
            fp.Value().SetVisible(False)
            board.add_pad(fp, "1", f"BUS{index}", at, size_nm=point(.85, .65),
                          shape=pcbnew.PAD_SHAPE_RECT,
                          layers=[pcbnew.F_Cu if side == "F" else pcbnew.B_Cu])
    barrier([pcbnew.F_Cu, pcbnew.B_Cu], 12, -1, 56, 45)
    inner = [pcbnew.In1_Cu, pcbnew.In2_Cu]
    for x, offset in [(17, 1.55), (29, -1.55), (40, 1.45), (51, -1.45)]:
        bottom = -1
        for index in channels:
            center = 4.5 + index * 5 + offset
            barrier(inner, x, bottom, x + .85, center - .65)
            bottom = center + .65
        barrier(inner, x, bottom, x + .85, 45)
    board.set_project_rules()
    source = board.save(tmp_path / "source").pcb_path
    before = run_native_drc(source).raw
    assert len(before["unconnected_items"]) == 8 and not before["violations"]

    run, _ = guarded_cli
    saved, op = tmp_path / "routed.kicad_pcb", tmp_path / "route.json"
    run([str(_built_rust_engine(run)), "route-native", str(source), str(saved),
         "--operation-result", str(op)])
    routing = json.loads(op.read_bytes())["routing"]
    assert routing["status"] == "complete" and 0 < routing["expanded_states"] <= 100_000
    after = run_native_drc(saved, sidecar_source=source).raw
    assert not after["unconnected_items"] and not after["violations"]
    reopened = pcbnew.LoadBoard(str(saved))
    pieces = list(reopened.GetTracks())
    for index in channels:
        net = [p for p in pieces if p.GetNetname() == f"BUS{index}"]
        assert sum(isinstance(p, pcbnew.PCB_VIA) for p in net) == 2
        assert any(p.GetLayer() in inner for p in net)


def test_three_surface_contacts_share_the_same_physical_junction(tmp_path, guarded_cli):
    board = BoardBuilder(copper_layers=4, seed=7280)
    board.set_rect_outline(0, 0, 40_000_000, 40_000_000)
    pins = [(5_200_000, 5_600_000), (21_950_000, 11_850_000), (16_700_000, 31_800_000)]
    for i, at in enumerate([*pins, (3_000_000, 34_000_000), (11_000_000, 34_000_000)]):
        fp = board.add_footprint(f"J{i + 1}", at, side="B")
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        board.add_pad(fp, "1", "CLK" if i < 3 else "REF", at, size_nm=(700_000, 700_000),
                      shape=pcbnew.PAD_SHAPE_CIRCLE, layers=[pcbnew.B_Cu])
    board.set_project_rules()
    source = board.save(tmp_path / "input").pcb_path
    original = run_native_drc(source).raw
    assert len(original["unconnected_items"]) == 3 and not original["violations"]

    run, _ = guarded_cli
    saved, op = tmp_path / "routed.kicad_pcb", tmp_path / "operation.json"
    run([str(_built_rust_engine(run)), "route-native", str(source), str(saved),
         "--operation-result", str(op)])
    assert json.loads(op.read_bytes())["status"] == "complete"
    board = pcbnew.LoadBoard(str(saved))
    joint = (16_700_000, 17_100_000)
    arms = [t for t in board.GetTracks() if t.GetNetname() == "CLK"]
    assert all(not isinstance(t, pcbnew.PCB_VIA) and t.GetLayer() == pcbnew.B_Cu for t in arms)
    assert len(arms) == 3
    assert {frozenset((tuple(t.GetStart()), tuple(t.GetEnd()))) for t in arms} == {
        frozenset((joint, p)) for p in pins
    }
    routed = run_native_drc(saved, sidecar_source=source).raw
    assert not routed["unconnected_items"] and not routed["violations"]
