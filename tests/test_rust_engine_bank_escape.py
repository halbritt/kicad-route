"""Competing multilayer nets share the original expansion and work limits."""
import json

import pcbnew
import pytest

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


def test_signal_tracks_detour_around_keyways_inside_a_local_drill_keepout(tmp_path, guarded_cli):
    board = BoardBuilder(copper_layers=4, seed=3589)

    def point(x, y):
        return pcbnew.FromMM(x), pcbnew.FromMM(y)

    def rectangle(left, top, right, bottom):
        return [point(left, top), point(right, top), point(right, bottom), point(left, bottom)]

    board.set_rect_outline(*point(0, 0), *point(46, 29))
    outer = [pcbnew.F_Cu, pcbnew.B_Cu]
    board.add_keepout([pcbnew.In1_Cu], rectangle(1, 2, 45, 27), no_tracks=False, no_vias=True)
    for channel, y in enumerate([6.2, 14.4, 22.1]):
        face = outer[channel % 2]
        net = f"IO{channel}"
        for ref, xy in [("J", point(3.25, y)), ("P", point(42.75, y + .7))]:
            fp = board.add_footprint(f"{ref}{channel + 1}", xy, side="F" if face == pcbnew.F_Cu else "B")
            fp.Reference().SetVisible(False)
            fp.Value().SetVisible(False)
            board.add_pad(fp, "1", net, xy, size_nm=point(.8, .7),
                          shape=pcbnew.PAD_SHAPE_RECT, layers=[face])
        for x, offset in [(12.4, -.6), (23.1, .65), (33.8, -.55)]:
            middle = y + offset
            board.add_keepout([face], rectangle(x, middle - 1.2, x + 1.5, middle + 1.2),
                              no_tracks=True, no_vias=False)
    board.set_project_rules()
    source = board.save(tmp_path / "input").pcb_path
    before = run_native_drc(source).raw
    assert len(before["unconnected_items"]) == 3 and not before["violations"]
    run, _ = guarded_cli
    saved = tmp_path / "signals.kicad_pcb"
    run([str(_built_rust_engine(run)), "route-native", str(source), str(saved)])
    grade = run_native_drc(saved, sidecar_source=source).raw
    assert not grade["unconnected_items"] and not grade["violations"]
    reopened = pcbnew.LoadBoard(str(saved))
    pieces = list(reopened.GetTracks())
    assert pieces and not any(isinstance(piece, pcbnew.PCB_VIA) for piece in pieces)


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


@pytest.mark.parametrize("diagonal", [False, True], ids=["axial", "diagonal"])
def test_shielded_contacts_reach_drill_windows_between_geometric_samples(tmp_path, guarded_cli, diagonal):
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

    windows = [(5.6, 10.5), (33.09, 16.81)] if diagonal else [(5.6, 8), (33.09, 13)]
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
    if diagonal:
        starts = {point(3.1, 8), point(36.9, 13)}
        ends = set(vias)
        def reaches_window(track):
            a = (track.GetStartX(), track.GetStartY())
            b = (track.GetEndX(), track.GetEndY())
            return ((a in starts and b in ends) or (b in starts and a in ends)) and (
                abs(a[0] - b[0]) == abs(a[1] - b[1])
            )
        assert any(reaches_window(track) for track in reopened.GetTracks()
                   if track.GetNetname() == "SIG" and track.GetLayer() == front
                   and not isinstance(track, pcbnew.PCB_VIA))
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


@pytest.mark.parametrize(("count", "rear", "keyways"), [(9, True, True), (12, False, False)])
def test_negotiated_routes_between_perpendicular_connector_banks(tmp_path, guarded_cli, count, rear, keyways):
    board = BoardBuilder(copper_layers=4, seed=4997)
    board.set_rect_outline(0, 0, 62_000_000, 52_000_000)

    def point(x, y):
        if rear:
            x, y = 62 - x, 52 - y
        return round(x * 1_000_000), round(y * 1_000_000)

    def keepout(layers, x, y, right, top, *, tracks=False, vias=False):
        polygon = [point(x, y), point(right, y), point(right, top), point(x, top)]
        board.add_keepout(layers, polygon, no_tracks=tracks, no_vias=vias)

    outer = [pcbnew.B_Cu, pcbnew.F_Cu]
    face, opposite = ("B", "F") if rear else ("F", "B")
    for index in range(count):
        y = 4.95 + index * 2.7
        x = 17.65 + ((index * 5 + 1) % count) * 3.4
        for bank, at, side in [("A", point(3.35, y), face),
                               ("B", point(x, 48.85), opposite if index % 3 == 0 else face)]:
            fp = board.add_footprint(f"{bank}{index}", at, side=side)
            fp.Reference().SetVisible(False)
            fp.Value().SetVisible(False)
            board.add_pad(fp, "1", f"IO{index}", at, size_nm=(700_000, 650_000),
                          shape=pcbnew.PAD_SHAPE_RECT,
                          layers=[pcbnew.B_Cu if side == "B" else pcbnew.F_Cu])
        if keyways:
            keepout([pcbnew.B_Cu if rear else pcbnew.F_Cu], 6.5, y - .35, 7.4, y + .35, tracks=True)
            keepout(outer, x - .45, 44.9, x + .45, 45.9, tracks=True)
    keepout(outer, -1, -1, 10.1, 37.5, vias=True)
    keepout(outer, 13.05, 42.7, 61, 53, vias=True)
    keepout(outer, 12.4, -1, 60.5, 40.8, tracks=True)
    board.set_project_rules()
    source = board.save(tmp_path / "input").pcb_path
    before = run_native_drc(source).raw
    assert len(before["unconnected_items"]) == count and not before["violations"]

    run, _ = guarded_cli
    saved, op = tmp_path / "routed.kicad_pcb", tmp_path / "operation.json"
    run([str(_built_rust_engine(run)), "route-native", str(source), str(saved),
         "--operation-result", str(op)])
    routing = json.loads(op.read_bytes())["routing"]
    assert routing["status"] == "complete" and 0 < routing["expanded_states"] <= 100_000
    after = run_native_drc(saved, sidecar_source=source).raw
    assert not after["unconnected_items"] and not after["violations"]
    reopened = pcbnew.LoadBoard(str(saved))
    assert reopened.GetCopperLayerCount() == 4 and len(list(reopened.GetPads())) == count * 2


def test_surface_branch_reaches_the_middle_of_a_locked_inner_trace(tmp_path, guarded_cli):
    board = BoardBuilder(copper_layers=4, seed=13142)
    board.set_rect_outline(0, 0, 64_000_000, 38_000_000)
    stack = [pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu]

    def point(x, y):
        return round((64 - x) * 1_000_000), round((38 - y) * 1_000_000)

    def block(layers, x, y, right, top, *, tracks=False, vias=False):
        board.add_keepout(layers, [point(x, y), point(right, y), point(right, top), point(x, top)],
                          no_tracks=tracks, no_vias=vias)

    a, b = point(3.25, 27.55), point(59.25, 34.55)
    for i, at in enumerate([a, b, point(31.25, 3.55), point(3, 3), point(9, 3)]):
        fp = board.add_footprint(f"J{i + 1}", at, side="B")
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        if i < 2:
            board.add_pad(fp, "1", "SIG", at, attrib=pcbnew.PAD_ATTRIB_PTH,
                          layers=[*stack, pcbnew.F_Mask, pcbnew.B_Mask], size_nm=(1_350_000,) * 2,
                          drill_size_nm=(600_000,) * 2, shape=pcbnew.PAD_SHAPE_CIRCLE)
        else:
            board.add_pad(fp, "1", "SIG" if i == 2 else "REF", at, layers=[pcbnew.B_Cu],
                          size_nm=(800_000, 650_000), shape=pcbnew.PAD_SHAPE_RECT)
    board.add_track("SIG", a, b, 200_000, layer=pcbnew.In2_Cu, locked=True)
    block([pcbnew.F_Cu, pcbnew.B_Cu], -1, 7.5, 65, 25, tracks=True)
    block(stack, -1, 7.5, 65, 39, vias=True)
    for y, left, right in [(11.5, 32.75, 34.25), (16.5, 36.35, 37.85), (21.5, 33.25, 34.75)]:
        block(stack[1:3], -1, y, left, y + .8, tracks=True)
        block(stack[1:3], right, y, 65, y + .8, tracks=True)
    board.set_project_rules()
    source = board.save(tmp_path / "input").pcb_path
    before = run_native_drc(source).raw
    assert len(before["unconnected_items"]) == 2 and not before["violations"]
    run, _ = guarded_cli
    saved = tmp_path / "routed.kicad_pcb"
    run([str(_built_rust_engine(run)), "route-native", str(source), str(saved)])
    after = run_native_drc(saved, sidecar_source=source).raw
    assert not after["unconnected_items"] and not after["violations"]
    reopened = pcbnew.LoadBoard(str(saved))
    tracks = list(reopened.GetTracks())
    assert len([item for item in tracks if isinstance(item, pcbnew.PCB_VIA)]) == 1
    original = [t for t in tracks if not isinstance(t, pcbnew.PCB_VIA) and t.IsLocked()]
    assert len(original) == 1 and {tuple(original[0].GetStart()), tuple(original[0].GetEnd())} == {a, b}
    contacts = [tuple(endpoint) for t in tracks if not isinstance(t, pcbnew.PCB_VIA)
                and not t.IsLocked() and t.GetLayer() == pcbnew.In2_Cu
                for endpoint in (t.GetStart(), t.GetEnd())]
    assert any(min(a[0], b[0]) < x < max(a[0], b[0]) and (x - a[0]) == 8 * (y - a[1])
               for x, y in contacts)


def test_plated_connector_accepts_the_inner_trunk_without_a_second_drill(tmp_path, guarded_cli):
    board = BoardBuilder(copper_layers=4, seed=4903)
    board.set_rect_outline(0, 0, 50_000_000, 32_000_000)
    stack = [pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu]

    def point(x, y):
        return round(x * 1_000_000), round(y * 1_000_000)

    for index in range(6):
        y = 4.75 + 3.5 * index
        net = f"IO{index:02}"
        source = point(3.25 + index % 2 * .45, y)
        target = point(46.65 - index % 2 * 1.3, y + (.85 if index % 2 else 1.35))
        face = pcbnew.B_Cu if index % 3 == 2 else pcbnew.F_Cu
        for end, at in enumerate((source, target)):
            fp = board.add_footprint(("J" if end else "U") + str(index + 1), at,
                                     side="F" if end or face == pcbnew.F_Cu else "B")
            fp.Value().SetVisible(False)
            fp.Reference().SetVisible(False)
            if end:
                pad = board.add_pad(fp, "1", net, at, attrib=pcbnew.PAD_ATTRIB_PTH,
                                    layers=[*stack, pcbnew.F_Mask, pcbnew.B_Mask],
                                    shape=pcbnew.PAD_SHAPE_CIRCLE, size_nm=point(1.45, 1.45),
                                    drill_size_nm=point(.65, .65))
            else:
                pad = board.add_pad(fp, "1", net, at, layers=[face], shape=pcbnew.PAD_SHAPE_RECT,
                                    size_nm=point(.8, .65))
            pad.SetUuid(pcbnew.KIID(f"76e6b93c-5b6d-4bf2-b3a6-{2*index+end+1:012x}"))
    for layers, x, right, vias in [([stack[0], stack[-1]], 10, 39, False), (stack, 9, 51, True)]:
        rectangle = [point(x, -1), point(right, -1), point(right, 33), point(x, 33)]
        board.add_keepout(layers, rectangle, no_tracks=not vias, no_vias=vias)
    board.set_project_rules()
    source = board.save(tmp_path / "input").pcb_path
    before = run_native_drc(source).raw
    assert len(before["unconnected_items"]) == 6 and not before["violations"]

    run, _ = guarded_cli
    saved, operation = tmp_path / "routed.kicad_pcb", tmp_path / "operation.json"
    run([str(_built_rust_engine(run)), "route-native", str(source), str(saved),
         "--operation-result", str(operation)])
    result = json.loads(operation.read_bytes())["routing"]
    assert result["status"] == "complete" and result["added_via_count"] == 6
    reopened = pcbnew.LoadBoard(str(saved))
    tracks = list(reopened.GetTracks())
    for pad in reopened.GetPads():
        if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
            copper = [t for t in tracks if t.GetNetCode() == pad.GetNetCode()
                      and not isinstance(t, pcbnew.PCB_VIA) and t.GetLayer() in stack[1:3]]
            assert any(pad.HitTest(t.GetStart()) or pad.HitTest(t.GetEnd()) for t in copper)
    verified = run_native_drc(saved, sidecar_source=source).raw
    assert not verified["unconnected_items"] and not verified["violations"]


def test_distributed_surface_supply_joins_the_header_and_all_local_loads(tmp_path, guarded_cli):
    board = BoardBuilder(copper_layers=4, seed=7372)
    board.set_rect_outline(0, 0, 45_000_000, 35_000_000)
    stack = [pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu]
    for i, (x, y) in enumerate([(5, 7), (17, 7.5), (28, 8), (6, 19), (18, 20),
                                (30, 19), (6, 28), (17, 27.5), (30, 28)]):
        coord = (pcbnew.FromMM(x), pcbnew.FromMM(y))
        fp = board.add_footprint(f"C{i + 1}", coord)
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        board.add_pad(fp, "1", "BIAS", coord, size_nm=(850_000, 650_000),
                      shape=pcbnew.PAD_SHAPE_RECT, layers=[pcbnew.F_Cu])
    for ref, net, x, y, plated in [("J1", "BIAS", 39, 18, True),
                                   ("R1", "REF", 4, 3, False), ("R2", "REF", 12, 3, False)]:
        at = (pcbnew.FromMM(x), pcbnew.FromMM(y))
        fp = board.add_footprint(ref, at)
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        if plated:
            board.add_pad(fp, "1", net, at, attrib=pcbnew.PAD_ATTRIB_PTH,
                          layers=[*stack, pcbnew.F_Mask, pcbnew.B_Mask], size_nm=(1_450_000, 1_450_000),
                          drill_size_nm=(650_000, 650_000), shape=pcbnew.PAD_SHAPE_CIRCLE)
        else:
            board.add_pad(fp, "1", net, at, size_nm=(650_000, 650_000),
                          layers=[pcbnew.F_Cu], shape=pcbnew.PAD_SHAPE_RECT)
    board.set_project_rules()
    source = board.save(tmp_path / "input").pcb_path
    before = run_native_drc(source).raw
    assert len(before["unconnected_items"]) == 10 and not before["violations"]
    run, _ = guarded_cli
    saved, operation = tmp_path / "routed.kicad_pcb", tmp_path / "operation.json"
    run([str(_built_rust_engine(run)), "route-native", str(source), str(saved),
         "--operation-result", str(operation)])
    result = json.loads(operation.read_bytes())["routing"]
    assert result["status"] == "complete" and result["added_via_count"] == 0
    reopened = pcbnew.LoadBoard(str(saved))
    copper = [t for t in reopened.GetTracks() if t.GetNetname() == "BIAS"]
    assert copper and all(t.GetLayer() == pcbnew.F_Cu for t in copper)
    ends = {tuple(p) for t in copper for p in (t.GetStart(), t.GetEnd())}
    assert (39_000_000, 18_000_000) in ends
    assert any(p.HitTest(pcbnew.VECTOR2I(*q)) and q != tuple(p.GetPosition())
               for p in reopened.GetPads() if p.GetNetname() == "BIAS" for q in ends)
    after = run_native_drc(saved, sidecar_source=source).raw
    assert not after["unconnected_items"] and not after["violations"]
