"""An exact SMD diagonal can fit where a four-direction grid staircase cannot."""
import json

import pcbnew
import pytest

from tests.e2e_rust_router.kicad_verify import BoardBuilder, run_native_drc
from test_rust_engine_native import _built_rust_engine, bridge, guarded_cli as guarded_cli


def corridor(directory, offset, shift, layer):
    board = BoardBuilder(seed=127)
    side = 12_000_000
    board.set_rect_outline(0, 0, side, side)
    opposite = pcbnew.B_Cu if layer == pcbnew.F_Cu else pcbnew.F_Cu

    def contact(ref, net, xy, face):
        fp = board.add_footprint(ref, xy, side="F" if face == pcbnew.F_Cu else "B")
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        board.add_pad(fp, "1", net, xy, size_nm=(500_000, 500_000), layers=[face])

    a = (2_000_000, 2_000_000)
    b = (10_000_000, 10_000_000 + shift)
    contact("J1", "DATA", a, layer)
    contact("J2", "DATA", b, layer)
    p, q = (3_000_000, 3_000_000), (4_000_000, 4_000_000)
    contact("P1", "FIXED", p, opposite)
    contact("P2", "FIXED", q, opposite)
    locked = board.add_track("FIXED", p, q, 200_000, layer=opposite, locked=True)
    if offset is not None:
        # Block only the signal face so the locked control stays legal in all variants.
        # Absence of vias is part of the accepted positive result. Forbid vias on
        # both faces in the same two polygons so they cannot escape the channel.
        walls = [[(0, offset), (side-offset, side), (0, side)],
                 [(offset, 0), (side, side-offset), (side, 0)]]
        for ring in walls:
            board.add_keepout([layer], ring, no_tracks=True, no_vias=True)
            board.add_keepout([opposite], ring, no_tracks=False, no_vias=True)
    source = board.save(directory / "input").pcb_path
    locked_id = locked.m_Uuid.AsString()
    if shift:
        board.add_track("DATA", a, (b[0], a[1] + b[0] - a[0]), 200_000, layer=layer)
        board.add_track("DATA", (b[0], a[1] + b[0] - a[0]), b, 200_000, layer=layer)
    else:
        board.add_track("DATA", a, b, 200_000, layer=layer)
    witness = board.save(directory / "drawn").pcb_path
    return source, witness, locked_id, {a, b}


@pytest.mark.parametrize("offset,shift,layer,accept", [
    (160_000, 0, pcbnew.F_Cu, True), (145_000, 7_000, pcbnew.B_Cu, False),
    (160_000, 7_000, pcbnew.B_Cu, True), (145_000, 0, pcbnew.F_Cu, True),
    (141_421, 0, pcbnew.F_Cu, False), (126_000, 0, pcbnew.B_Cu, False),
    (None, 0, pcbnew.F_Cu, True),
])
def test_saved_octilinear_escape(tmp_path, guarded_cli, offset, shift, layer, accept):
    source, drawn, locked_id, centers = corridor(tmp_path, offset, shift, layer)
    original = source.read_bytes()
    initial = bridge.native_violations(source, tmp_path / "initial.json")
    witness = bridge.native_violations(drawn, tmp_path / "witness.json")
    assert len(initial["unconnected_items"]) == 1 and not initial["violations"]
    assert not witness["unconnected_items"]
    assert {v["type"] for v in witness["violations"]} == (set() if accept else {"items_not_allowed"})
    run, _ = guarded_cli
    engine = _built_rust_engine(run)

    def route(stem, *limits):
        result = tmp_path / (stem + ".json")
        output = tmp_path / (stem + ".kicad_pcb")
        proc = run([str(engine), "route-native", str(source), str(output),
                    "--operation-result", str(result), *limits], check=False)
        assert source.read_bytes() == original
        return proc.returncode, json.loads(result.read_bytes()), output

    code, op, output = route("saved")
    if not accept:
        assert (code, op["status"]) == (3, "unroutable"), op.get("reason")
        assert not output.exists() and op["routing"]["added_track_count"] == 0
        return
    assert (code, op["status"]) == (0, "complete"), op.get("reason")
    saved = pcbnew.LoadBoard(str(output))
    fixed = [t for t in saved.GetTracks() if t.GetNetname() == "FIXED"]
    assert len(fixed) == 1 and fixed[0].IsLocked() and fixed[0].m_Uuid.AsString() == locked_id
    drc = run_native_drc(output, sidecar_source=source).raw
    assert not drc["unconnected_items"] and not drc["violations"]
    tracks = [t for t in saved.GetTracks() if t.GetNetname() == "DATA"]
    assert all(not isinstance(t, pcbnew.PCB_VIA) and t.GetLayer() == layer and t.GetWidth() == 200_000 for t in tracks)
    ends = {(tuple(t.GetStart()), tuple(t.GetEnd())) for t in tracks}
    diagonal = [(a, b) for a, b in ends if a[0] != b[0] and a[1] != b[1]]
    if offset is None:
        assert not diagonal   # originally routable Manhattan path is retained
    else:
        assert len(diagonal) == 1 and len(tracks) == 1 + bool(shift)
        assert all(abs(a[0]-b[0]) == abs(a[1]-b[1]) for a,b in diagonal)
        assert centers <= {point for segment in ends for point in segment}
    if offset == 160_000 and not shift:
        for counter, flag, status, code in [("expanded_states", "--max-expansions", "budget_exhausted", 4),
                                             ("work_used", "--limits", "work_exhausted", 5)]:
            limit = op["routing"][counter] - 1
            value = str(limit)
            if flag == "--limits":
                file = tmp_path / "limit.json"
                file.write_text(json.dumps({"max_work": limit}))
                value = str(file)
            got, stopped, none = route(status, flag, value)
            assert (got, stopped["status"]) == (code, status)
            assert stopped["routing"][counter] == limit and not none.exists()
            assert stopped["routing"]["added_track_count"] == 0


@pytest.mark.parametrize("signal_layer", [pcbnew.F_Cu, pcbnew.B_Cu])
def test_multilayer_board_uses_legal_diagonal_into_surface_pad(tmp_path, guarded_cli, signal_layer):
    board = BoardBuilder(copper_layers=4, seed=303)
    board.set_rect_outline(0, 0, 40_000_000, 32_000_000)

    def surface_pair(net, points, layer, shape):
        for i, xy in enumerate(points):
            fp = board.add_footprint(net + str(i), xy, side="F" if layer == pcbnew.F_Cu else "B")
            fp.Reference().SetVisible(False)
            fp.Value().SetVisible(False)
            board.add_pad(fp, "1", net, xy, size_nm=(900_000, 950_000), shape=shape, layers=[layer])

    surface_pair("SENSE", [(3_000_000, 4_000_000), (13_000_000, 10_000_000)],
                 signal_layer, pcbnew.PAD_SHAPE_RECT)
    surface_pair("DRIVE", [(5_000_000, 20_000_000), (17_400_000, 28_200_000)],
                 pcbnew.F_Cu, pcbnew.PAD_SHAPE_ROUNDRECT)
    source = board.save(tmp_path / "input").pcb_path
    output = tmp_path / "routed.kicad_pcb"
    op = tmp_path / "routed.json"
    run, _ = guarded_cli
    engine = _built_rust_engine(run)
    run([str(engine), "route-native", str(source), str(output), "--operation-result", str(op)])
    assert json.loads(op.read_bytes())["status"] == "complete"
    drc = run_native_drc(output, sidecar_source=source).raw
    assert not drc["unconnected_items"] and not drc["violations"]
    saved = pcbnew.LoadBoard(str(output))
    tracks = [t for t in saved.GetTracks() if t.GetNetname() == "SENSE"]
    assert len(tracks) == 2 and all(not isinstance(t, pcbnew.PCB_VIA) for t in tracks)
    assert all(t.GetLayer() == signal_layer and t.GetWidth() == 200_000 for t in tracks)
    deltas = [(abs(t.GetStartX() - t.GetEndX()), abs(t.GetStartY() - t.GetEndY())) for t in tracks]
    assert any(dx == dy and dx > 0 for dx, dy in deltas)


@pytest.mark.parametrize("reverse,sealed", [(False, False), (True, False), (False, True)])
def test_opposite_surface_lands_with_a_remote_via(tmp_path, guarded_cli, reverse, sealed):
    board = BoardBuilder(copper_layers=4, seed=303)
    board.set_rect_outline(0, 0, 30_000_000, 22_000_000)
    launch, dest = (pcbnew.B_Cu, pcbnew.F_Cu) if reverse else (pcbnew.F_Cu, pcbnew.B_Cu)
    layers = [pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu]

    def xy(x, y):
        return (30_000_000 - x if reverse else x, y)

    def pad(ref, net, pos, layer):
        fp = board.add_footprint(ref, pos, side="F" if layer == pcbnew.F_Cu else "B")
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        board.add_pad(fp, "1", net, pos, size_nm=(900_000, 950_000),
                      shape=pcbnew.PAD_SHAPE_RECT, layers=[layer])

    a, b = xy(3_000_000, 3_000_000), xy(17_000_000, 10_000_000)
    pad("J1", "SENSE", a, launch)
    pad("J2", "SENSE", b, dest)
    pad("T1", "SHIFT", xy(4_000_000, 18_000_000), dest)
    pad("T2", "SHIFT", xy(15_000_000, 18_000_000), dest)
    for polygon in [[(0, 160_000), (21_840_000, 22_000_000), (0, 22_000_000)],
                    [(160_000, 0), (30_000_000, 0), (30_000_000, 22_000_000), (22_160_000, 22_000_000)]]:
        board.add_keepout([launch], [xy(*p) for p in polygon], no_tracks=True, no_vias=False)
    stop = 30_000_000 if sealed else 8_000_000
    via_keepout = [(0, 0), (stop, 0), (stop, 22_000_000), (0, 22_000_000)]
    board.add_keepout(layers, [xy(*p) for p in via_keepout], no_tracks=False, no_vias=True)
    source = board.save(tmp_path / "input").pcb_path
    output = tmp_path / "routed.kicad_pcb"
    op = tmp_path / "op.json"
    run, _ = guarded_cli
    engine = _built_rust_engine(run)
    run([str(engine), "route-native", str(source), str(output), "--operation-result", str(op)], check=False)
    result = json.loads(op.read_bytes())
    if sealed:
        assert result["status"] == "unroutable" and not output.exists()
        return
    assert result["status"] == "complete"
    drc = run_native_drc(output, sidecar_source=source).raw
    assert not drc["violations"] and not drc["unconnected_items"]
    saved = pcbnew.LoadBoard(str(output))
    copper = [t for t in saved.GetTracks() if t.GetNetname() == "SENSE"]
    vias = [t for t in copper if isinstance(t, pcbnew.PCB_VIA)]
    tracks = [t for t in copper if not isinstance(t, pcbnew.PCB_VIA)]
    assert len(vias) == 1 and vias[0].GetViaType() == pcbnew.VIATYPE_THROUGH
    via_position = tuple(vias[0].GetPosition())
    assert sorted(t.GetLayer() for t in tracks) == sorted([launch, dest])
    assert all(via_position in (tuple(t.GetStart()), tuple(t.GetEnd())) for t in tracks)


@pytest.mark.parametrize("transpose", [False, True])
def test_staggered_surface_keepouts_admit_a_shortest_trunk(tmp_path, guarded_cli, transpose):
    board = BoardBuilder(copper_layers=4, seed=805)
    board.set_rect_outline(0, 0, 30_000_000, 30_000_000)
    face = pcbnew.B_Cu if transpose else pcbnew.F_Cu
    other = pcbnew.F_Cu if transpose else pcbnew.B_Cu

    def pos(x, y):
        return (y, x) if transpose else (x, y)

    def pad(ref, net, x, y, layer):
        xy = pos(x, y)
        fp = board.add_footprint(ref, xy, side="F" if layer == pcbnew.F_Cu else "B")
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        board.add_pad(fp, "1", net, xy, size_nm=(900_000, 900_000),
                      shape=pcbnew.PAD_SHAPE_RECT, layers=[layer])

    pad("J1", "SIG", 3_000_000, 3_000_000, face)
    pad("J2", "SIG", 25_000_000, 15_000_000, face)
    pad("T1", "AUX", 3_000_000, 25_000_000, other)
    pad("T2", "AUX", 19_000_000, 25_000_000, other)
    # Only the left-side gap connects both terminal runs.
    for x0, y0, x1, y1 in [(0, 4_600_000, 7_400_000, 30_000_000),
                           (8_800_000, 0, 30_000_000, 13_400_000)]:
        ring = [pos(x0, y0), pos(x1, y0), pos(x1, y1), pos(x0, y1)]
        board.add_keepout([face], ring, no_tracks=True, no_vias=False)
    source = board.save(tmp_path / "input").pcb_path
    output, op = tmp_path / "routed.kicad_pcb", tmp_path / "operation.json"
    run, _ = guarded_cli
    engine = _built_rust_engine(run)
    run([str(engine), "route-native", str(source), str(output),
         "--operation-result", str(op), "--max-expansions", "2"])
    routing = json.loads(op.read_bytes())["routing"]
    assert (routing["status"], routing["expanded_states"], routing["added_via_count"]) == (
        "complete", 2, 0)
    drc = run_native_drc(output, sidecar_source=source).raw
    assert not drc["violations"] and not drc["unconnected_items"]
    saved = pcbnew.LoadBoard(str(output))
    segments = [t for t in saved.GetTracks() if t.GetNetname() == "SIG"]
    assert segments and all(t.GetLayer() == face and t.GetWidth() == 200_000 for t in segments)
    assert any(0 < abs(t.GetStartX() - t.GetEndX()) == abs(t.GetStartY() - t.GetEndY())
               for t in segments)


@pytest.mark.parametrize("bridge_width,blocked", [(0, False), (3_000_000, False), (3_000_000, True)])
def test_two_diagonal_pad_exits_meet_in_a_surface_channel(tmp_path, guarded_cli, bridge_width, blocked):
    board = BoardBuilder(copper_layers=4, seed=40757)
    side = 20_000_000
    board.set_rect_outline(0, 0, side, side)
    a = (3_000_000, 4_000_000)
    corner = (7_000_000, 8_000_000)
    z = (11_000_000 + bridge_width, 4_000_000)
    for reference, at in [("J1", a), ("J2", z)]:
        fp = board.add_footprint(reference, at)
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        board.add_pad(fp, "1", "DATA", at, size_nm=(500_000, 500_000), layers=[pcbnew.F_Cu])
    center = [(0, 1_000_000), corner]
    if bridge_width:
        center.append((corner[0] + bridge_width, corner[1]))
    center.append((side, z[1] - (side - z[0])))
    for off, outer_y in [(159_000, side), (-159_000, -side)]:
        wall = [(x, y + off) for x, y in center] + [(side, outer_y), (0, outer_y)]
        board.add_keepout([pcbnew.F_Cu], wall, no_tracks=True, no_vias=True)
    if blocked:
        # Both diagonal departures fit. Only the middle copper is obstructed.
        x = corner[0] + bridge_width // 2
        board.add_keepout([pcbnew.F_Cu], [(x, -1), (x + 100_000, -1),
                                         (x + 100_000, side + 1), (x, side + 1)])
    stack = [pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu]
    board.add_keepout(stack, [(-1, -1), (side + 1, -1), (side + 1, side + 1), (-1, side + 1)],
                      no_tracks=False, no_vias=True)
    source = board.save(tmp_path / "input").pcb_path
    before = run_native_drc(source).raw
    assert len(before["unconnected_items"]) == 1 and not before["violations"]
    output = tmp_path / "routed.kicad_pcb"
    run, _ = guarded_cli
    operation = tmp_path / "operation.json"
    proc = run([str(_built_rust_engine(run)), "route-native", str(source), str(output),
                "--operation-result", str(operation)], check=False)
    report = json.loads(operation.read_bytes())
    if blocked:
        assert (proc.returncode, report["status"]) == (3, "unroutable")
        assert not report["publication"]["published"] and not output.exists()
        return
    assert (proc.returncode, report["status"]) == (0, "complete")
    saved = pcbnew.LoadBoard(str(output))
    tracks = list(saved.GetTracks())
    assert len(tracks) == 2 + bool(bridge_width)
    assert all(t.GetNetname() == "DATA" and t.GetLayer() == pcbnew.F_Cu and
               not isinstance(t, pcbnew.PCB_VIA) for t in tracks)
    deltas = [(abs(t.GetEndX() - t.GetStartX()), abs(t.GetEndY() - t.GetStartY())) for t in tracks]
    assert sum(dx == dy and dx > 0 for dx, dy in deltas) == 2
    after = run_native_drc(output, sidecar_source=source).raw
    assert not after["unconnected_items"] and not after["violations"]
