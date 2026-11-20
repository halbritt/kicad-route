"""Native rounded-pad copper admission and real executable/native outcomes."""

from collections import Counter
import hashlib
import json
import math
import re
import subprocess
import sys
import uuid

import pcbnew
import pytest

from tests.e2e_rust_router.kicad_verify import run_native_drc

from test_rust_engine_native import (
    ROOT,
    RULES,
    _add_rect_outline,
    bridge,
    native_board as native_board,
    native_copper_key,
    native_copper_partitions,
)


def save(path, board):
    assert pcbnew.SaveBoard(str(path), board)


def rounded(pad, kind, size=(2000000, 1000000), angle=0, ratio=0.25):
    pad.SetShape(kind)
    pad.SetSize(pcbnew.VECTOR2I(*size))
    pad.SetOrientationDegrees(angle)
    if kind == pcbnew.PAD_SHAPE_ROUNDRECT:
        pad.SetRoundRectRadiusRatio(ratio)


def execute(path, data, tmp_path):
    request, result = tmp_path / "request.json", tmp_path / "result.json"
    bridge.write_request(data, request)
    done = subprocess.run(
        [
            "cargo",
            "run",
            "--locked",
            "--offline",
            "--manifest-path",
            str(ROOT / "rust-engine/Cargo.toml"),
            "--",
            str(request),
            str(result),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, (done.stdout, done.stderr, result.read_text())
    plan = json.loads(result.read_text())
    assert plan["status"] == "complete"
    assert plan["request_sha256"] == hashlib.sha256(request.read_bytes()).hexdigest()
    return request, plan


@pytest.mark.parametrize(
    "kind,angle,ratio",
    [
        (pcbnew.PAD_SHAPE_OVAL, 0, 0.25),
        (pcbnew.PAD_SHAPE_OVAL, 90, 0.25),
        (pcbnew.PAD_SHAPE_OVAL, 180, 0.25),
        (pcbnew.PAD_SHAPE_ROUNDRECT, 0, 0.25),
        (pcbnew.PAD_SHAPE_ROUNDRECT, 90, 0.25),
        (pcbnew.PAD_SHAPE_ROUNDRECT, 270, 0.25),
        (pcbnew.PAD_SHAPE_ROUNDRECT, 0, 0),
        (pcbnew.PAD_SHAPE_ROUNDRECT, 90, 0.5),
    ],
)
def test_native_rounded_signals_and_foreign_obstacles_route_preserve_and_validate(
    native_board, tmp_path, kind, angle, ratio
):
    board = pcbnew.LoadBoard(str(native_board))
    for pad in board.GetPads():
        rounded(pad, kind, angle=angle, ratio=ratio)
    # A foreign pad adds a real rounded copper obstacle, independent of the
    # existing locked obstacle track. Net0 intentionally has no electrical joins.
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("OB1")
    fp.SetPosition(pcbnew.VECTOR2I(10000000, 2000000))
    board.Add(fp)
    obstacle = pcbnew.PAD(fp)
    obstacle.SetNumber("1")
    obstacle.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    layers = pcbnew.LSET()
    layers.AddLayer(pcbnew.F_Cu)
    obstacle.SetLayerSet(layers)
    obstacle.SetPosition(fp.GetPosition())
    obstacle.SetNetCode(0)
    rounded(
        obstacle,
        pcbnew.PAD_SHAPE_ROUNDRECT
        if kind == pcbnew.PAD_SHAPE_OVAL
        else pcbnew.PAD_SHAPE_OVAL,
        size=(1500000, 1000000),
        angle=90,
    )
    fp.Add(obstacle)
    save(native_board, board)
    original = native_board.read_bytes()
    data = bridge.export_board(native_board, RULES, 100000)
    assert all(p["shape"]["kind"] == "round_rect" for p in data["pads"])
    before_pads = {
        p.GetParentFootprint().GetReference(): native_copper_key(p)
        for p in board.GetPads()
    }
    assert native_copper_partitions(native_board)["SIGNAL"] == {
        frozenset((before_pads["J1"],)),
        frozenset((before_pads["J2"],)),
    }
    request, plan = execute(native_board, data, tmp_path)
    assert plan["added_tracks"]
    output = tmp_path / "routed.kicad_pcb"
    bridge.apply_plan(native_board, plan, output, expected_request=request.read_bytes())
    actual = bridge.export_board(output, RULES, 100000)
    assert actual["pads"] == data["pads"] and actual["nets"] == data["nets"]
    assert {t["id"]: t for t in actual["tracks"]} == {
        t["id"]: t for t in data["tracks"] + plan["added_tracks"]
    }
    assert native_board.read_bytes() == original
    added_keys = {("track", t["id"]) for t in plan["added_tracks"]}
    assert native_copper_partitions(output)["SIGNAL"] == {
        frozenset((before_pads["J1"], before_pads["J2"])) | added_keys
    }
    saved = pcbnew.LoadBoard(str(output))
    saved.BuildConnectivity()
    assert saved.GetConnectivity().GetUnconnectedCount(False) == 0
    before = bridge.native_violations(native_board, tmp_path / "before.json")
    after = run_native_drc(output, sidecar_source=native_board).raw

    def key(violation):
        return json.dumps(violation, sort_keys=True)

    assert not after["unconnected_items"]
    assert not (
        Counter(map(key, after["violations"])) - Counter(map(key, before["violations"]))
    )
    # The original native shape, rotation and rounding metadata survive apply.
    for old in board.GetPads():
        new = next(
            p for p in saved.GetPads() if bridge.item_id(p) == bridge.item_id(old)
        )
        assert (
            new.GetShape(),
            bridge.point(new.GetSize()),
            new.GetOrientationDegrees(),
            new.GetRoundRectRadiusRatio(),
            new.IsLocked(),
        ) == (
            old.GetShape(),
            bridge.point(old.GetSize()),
            old.GetOrientationDegrees(),
            old.GetRoundRectRadiusRatio(),
            old.IsLocked(),
        )


@pytest.mark.parametrize("kind", [pcbnew.PAD_SHAPE_OVAL, pcbnew.PAD_SHAPE_ROUNDRECT])
@pytest.mark.parametrize(
    "feature",
    [
        "zero",
        "negative",
        "drill",
    ],
)
def test_native_rounded_unsupported_and_malformed_are_not_coerced(
    native_board, kind, feature
):
    board = pcbnew.LoadBoard(str(native_board))
    pad = next(iter(board.GetPads()))
    rounded(pad, kind)
    if feature == "zero":
        pad.SetSize(pcbnew.VECTOR2I(0, 1000000))
    elif feature == "negative":
        pad.SetSize(pcbnew.VECTOR2I(-2000000, 1000000))
    elif feature == "drill":
        pad.SetDrillSize(pcbnew.VECTOR2I(100000, 100000))
    save(native_board, board)
    if feature == "drill":
        # KiCad omits an SMD drill set via its API on save. Inject the source
        # feature so native parsing cannot erase it before admission.
        text = re.sub(
            r'(\(pad "1" smd (?:oval|roundrect))',
            r"\1 (drill 0.1)",
            native_board.read_text(),
            count=1,
        )
        native_board.write_text(text)
    original = native_board.read_bytes()
    with pytest.raises(ValueError):
        bridge.export_board(native_board, RULES, 100000)
    # Apply uses the same native admission, even if supplied a fabricated complete
    # edit list with the correct source/request identities.
    request = b"{}"
    plan = {
        "version": 3,
        "status": "complete",
        "request_sha256": hashlib.sha256(request).hexdigest(),
        "source_id": hashlib.sha256(original).hexdigest(),
        "added_tracks": [],
        "removed_track_ids": [],
        "expanded_states": 0,
        "work_used": 0,
    }
    output = native_board.with_name("refused.kicad_pcb")
    with pytest.raises(ValueError):
        bridge.apply_plan(native_board, plan, output, expected_request=request)
    assert not output.exists() and native_board.read_bytes() == original


def test_native_rounded_back_copper_smd_pad_and_clearance_export_and_route(native_board, tmp_path):
    board = pcbnew.LoadBoard(str(native_board))
    pads = list(board.GetPads())
    rounded(pads[1], pcbnew.PAD_SHAPE_ROUNDRECT)
    pads[1].SetLocalClearance(500000)
    layers = pcbnew.LSET()
    layers.AddLayer(pcbnew.B_Cu)
    pads[1].SetLayerSet(layers)
    save(native_board, board)
    data = bridge.export_board(native_board, RULES, 100000)
    b_pads = [p for p in data["pads"] if p.get("layers") == ["B.Cu"]]
    assert len(b_pads) == 1 and b_pads[0]["local_clearance_nm"] == 500000
    request, plan = execute(native_board, data, tmp_path)
    assert plan["status"] == "complete" and plan["added_vias"]
    output = tmp_path / "routed-back.kicad_pcb"
    bridge.apply_plan(native_board, plan, output, expected_request=request.read_bytes())
    assert output.is_file()


def _custom_j1(board, vertices, *, angle=0, back=False, anchor=pcbnew.PAD_SHAPE_CIRCLE, stroke=0):
    pad = next(p for p in board.GetPads() if p.GetParentFootprint().GetReference() == "J1")
    footprint = pad.GetParentFootprint()
    if back:
        footprint.Flip(footprint.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
    layer = pcbnew.B_Cu if back else pcbnew.F_Cu
    layers = pcbnew.LSET()
    layers.AddLayer(layer)
    layers.AddLayer(pcbnew.B_Mask if back else pcbnew.F_Mask)
    pad.SetLayerSet(layers)
    pad.SetShape(pcbnew.PAD_SHAPE_CUSTOM)
    pad.SetAnchorPadShape(layer, anchor)
    pad.SetSize(pcbnew.VECTOR2I(200000, 200000))
    pad.SetOrientationDegrees(angle)
    if vertices:
        pad.AddPrimitivePoly(layer, [pcbnew.VECTOR2I(x, y) for x, y in vertices], stroke, True)
    return pad


@pytest.mark.parametrize("back, angle, anchor", [
    (False, 30, pcbnew.PAD_SHAPE_RECT),
    (True, 90, pcbnew.PAD_SHAPE_CIRCLE),
])
def test_custom_convex_terminal_exact_copper_mask_rotation_and_native_route(
    native_board, tmp_path, back, angle, anchor
):
    board = pcbnew.LoadBoard(str(native_board))
    vertices = [(-500000, -400000), (300000, -400000), (900000, 0), (300000, 400000), (-500000, 400000)]
    pad = _custom_j1(board, vertices, angle=angle, back=back, anchor=anchor)
    pad.SetLocalClearance(350000)
    save(native_board, board)
    data, plan, output = _route_and_verify_drc(native_board, tmp_path)
    exported = next(p for p in data["pads"] if p["footprint_ref"] == "J1")
    assert exported["local_clearance_nm"] == 350000
    assert exported.get("layers", ["F.Cu"]) == (["B.Cu"] if back else ["F.Cu"])
    assert plan["added_tracks"]
    if back:
        assert plan["added_vias"]
    cos, sin = math.cos(math.radians(angle)), math.sin(math.radians(angle))

    def transform(x, y):
        return (round(x * cos + y * sin) + pad.GetPosition().x,
                round(-x * sin + y * cos) + pad.GetPosition().y)

    shape = exported["shape"]
    assert shape["kind"] == "convex_polygon" and shape["radius_nm"] == 0
    assert set(map(tuple, shape["vertices"])) == {transform(*v) for v in vertices}
    saved = pcbnew.LoadBoard(str(output))
    native = next(p for p in saved.GetPads() if p.GetParentFootprint().GetReference() == "J1")
    assert native.GetShape() == pcbnew.PAD_SHAPE_CUSTOM
    copper, mask = (pcbnew.B_Cu, pcbnew.B_Mask) if back else (pcbnew.F_Cu, pcbnew.F_Mask)
    assert set(native.GetLayerSet().Seq()) == {copper, mask}
    for layer in (copper, mask):
        effective = native.GetEffectiveShape(layer)
        for x, y, inside in [(0, 0, True), (700000, 0, True), (1100000, 0, False), (700000, 350000, False)]:
            assert bool(effective.Collide(pcbnew.VECTOR2I(*transform(x, y)))) == inside


@pytest.mark.parametrize("geometry, vertices, stroke, invalid", [
    ("concave", [(-300000, -300000), (600000, -300000), (600000, 0), (250000, 0),
                 (250000, 200000), (600000, 200000), (600000, 400000), (-300000, 400000)], 0, False),
    ("stroke", [(-300000, -300000), (600000, -300000), (600000, 300000), (-300000, 300000)], 40000, False),
    ("disjoint", [(400000, 0), (800000, 0), (800000, 300000), (400000, 300000)], 0, True),
])
def test_custom_copper_refusal_matches_native_padstack_drc(native_board, tmp_path, geometry, vertices, stroke, invalid):
    board = pcbnew.LoadBoard(str(native_board))
    _custom_j1(board, vertices, stroke=stroke)
    save(native_board, board)
    report = bridge.native_violations(native_board, tmp_path / "input-drc.json")
    split = [v for v in report["violations"] if v["type"] == "padstack_invalid" and "single polygon" in v["description"]]
    assert bool(split) == invalid
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps(RULES))
    request = tmp_path / "request.json"
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/rust_engine_kicad.py"), "export", str(native_board),
         str(request), "--rules", str(rules)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2 and not request.exists()
    failure = json.loads(result.stderr)
    assert failure["status"] == ("invalid" if invalid else "unsupported")
    reason = {"concave": "non-convex",
              "stroke": "compound copper", "disjoint": r"padstack_invalid.*single polygon.*2 outlines"}[geometry]
    assert re.search(rf"custom pad J1\.1: .*{reason}", failure["reason"])


def test_custom_anchor_only_circle_and_missing_mask(native_board):
    board = pcbnew.LoadBoard(str(native_board))
    pad = _custom_j1(board, [])
    save(native_board, board)
    data = bridge.export_board(native_board, RULES, 100000)
    assert next(p["shape"] for p in data["pads"] if p["footprint_ref"] == "J1") == {"kind": "circle", "radius_nm": 100000}
    layers = pcbnew.LSET()
    layers.AddLayer(pcbnew.F_Cu)
    pad.SetLayerSet(layers)
    save(native_board, board)
    with pytest.raises(bridge.UnsupportedBoard, match=r"custom pad J1\.1: a solder-mask opening on the copper face"):
        bridge.export_board(native_board, RULES, 100000)


def test_partial_polygon_keeps_the_one_nm_protruding_round_anchor(native_board):
    board = pcbnew.LoadBoard(str(native_board))
    _custom_j1(board, [(-99999, -200000), (500000, -200000), (500000, 200000), (-99999, 200000)])
    save(native_board, board)
    exported = bridge.export_board(native_board, RULES, 100000)
    pad = next(p for p in exported["pads"] if p["footprint_ref"] == "J1")
    assert pad["shape"]["kind"] == "union"
    circle, polygon = pad["shape"]["shapes"]
    assert circle == {"kind": "circle", "radius_nm": 100000}
    assert min(x for x, _ in polygon["vertices"]) == pad["at"][0] - 99999


def test_valid_edge_touching_custom_copper_reports_unsupported_through_route_native(native_board, tmp_path):
    board = pcbnew.LoadBoard(str(native_board))
    _custom_j1(board, [(100000, -200000), (500000, -200000), (500000, 200000), (100000, 200000)],
               anchor=pcbnew.PAD_SHAPE_RECT)
    save(native_board, board)
    before = bridge.native_violations(native_board, tmp_path / "before.json")
    assert not any(v["type"] == "padstack_invalid" for v in before["violations"])
    rules, result, output = tmp_path / "rules.json", tmp_path / "operation.json", tmp_path / "out.kicad_pcb"
    rules.write_text(json.dumps(RULES))
    done = subprocess.run(["cargo", "run", "--locked", "--offline", "--manifest-path",
                           str(ROOT / "rust-engine/Cargo.toml"), "--", "route-native", str(native_board),
                           str(output), "--rules", str(rules), "--operation-result", str(result)],
                          cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and not output.exists()
    failure = json.loads(result.read_text())
    assert failure["status"] == "unsupported" and "positive overlap" in failure["reason"]


def _filled_custom_circle(pad, layer, x, y, radius):
    circle = pcbnew.PCB_SHAPE(pad)
    circle.SetShape(pcbnew.SHAPE_T_CIRCLE)
    circle.SetStart(pcbnew.VECTOR2I(x, y))
    circle.SetEnd(pcbnew.VECTOR2I(x + radius, y))
    circle.SetWidth(0)
    circle.SetFilled(True)
    pad.AddPrimitiveShape(layer, circle)
    # KiCad now owns the native primitive; SWIG does not transfer ownership.
    circle.thisown = False


@pytest.mark.parametrize("side, board_offset", [(1, 0), (-1, 150000000)])
def test_native_compound_jumper_preserves_curves_corners_contact_and_centering(native_board, tmp_path, side, board_offset):
    board = pcbnew.LoadBoard(str(native_board))
    pad = _custom_j1(board, [], angle=180, back=True, anchor=pcbnew.PAD_SHAPE_RECT)
    pad.SetSize(pcbnew.VECTOR2I(1000000, 500000))
    pad.SetLocalClearance(350000)
    for y in (-250000, 250000):
        _filled_custom_circle(pad, pcbnew.B_Cu, 0, y, 500000)
    pad.AddPrimitivePoly(pcbnew.B_Cu, [pcbnew.VECTOR2I(side * x, y) for x, y in
                         ((0, 750000), (500000, 750000), (500000, -750000), (0, -750000))], 0, True)
    if board_offset:
        delta = pcbnew.VECTOR2I(board_offset, board_offset)
        for item in [*board.GetFootprints(), *board.GetTracks(), *board.GetDrawings()]:
            item.Move(delta)
    px, py = bridge.point(pad.GetPosition())
    copper = pad.GetEffectiveShape(pcbnew.B_Cu)
    # Contact the square corner, outside both circles and the anchor rectangle.
    tip = pcbnew.VECTOR2I(px - side * 450000, py + 700000)
    assert copper.Collide(tip)
    fixed = pcbnew.PCB_TRACK(board)
    fixed.SetStart(tip)
    fixed.SetEnd(pcbnew.VECTOR2I(tip.x, tip.y + 1500000))
    fixed.SetNetCode(pad.GetNetCode())
    fixed.SetLayer(pcbnew.B_Cu)
    fixed.SetWidth(250000)
    fixed.SetLocked(True)
    board.Add(fixed)
    save(native_board, board)
    data, _, output = _route_and_verify_drc(native_board, tmp_path)
    assert len(data["pads"]) == 2
    wire = next(p for p in data["pads"] if p["footprint_ref"] == "J1")
    assert wire["shape"]["kind"] == "union" and wire["layers"] == ["B.Cu"]
    assert wire["local_clearance_nm"] == 350000
    x, y = wire["at"]
    if board_offset:
        assert [x, y] != [px, py]
    anchor, top, bottom, square = wire["shape"]["shapes"]
    assert anchor == {"kind": "box_exact", "center_2x": [2 * x, 2 * y],
                      "half_size_2x": [1000000, 500000], "radius_2x": 0}
    for disc, offset in ((top, 250000), (bottom, -250000)):
        assert disc == {"kind": "box_exact", "center_2x": [2 * x, 2 * (y + offset)],
                        "half_size_2x": [1000000, 1000000], "radius_2x": 1000000}
    assert set(map(tuple, square["vertices"])) == {
        (x + dx, y + dy) for dx in (0, -side * 500000) for dy in (-750000, 750000)}
    saved = pcbnew.LoadBoard(str(output))
    actual = next(p for p in saved.GetPads() if p.GetParentFootprint().GetReference() == "J1")
    assert set(actual.GetLayerSet().Seq()) == {pcbnew.B_Cu, pcbnew.B_Mask}
    assert [px, py] == bridge.point(actual.GetPosition())
    for layer in (pcbnew.B_Cu, pcbnew.B_Mask):
        shape = actual.GetEffectiveShape(layer)
        for dx, dy, inside in ((-450000, 700000, True), (450000, 700000, False),
                               (300000, 620000, True), (400000, 620000, False)):
            assert bool(shape.Collide(pcbnew.VECTOR2I(px + side * dx, py + dy))) == inside
    assert len(native_copper_partitions(output)["SIGNAL"]) == 1


def test_native_compound_line_width_and_member_bound(native_board):
    board = pcbnew.LoadBoard(str(native_board))
    pad = _custom_j1(board, [])
    segment = pcbnew.PCB_SHAPE(pad)
    segment.SetShape(pcbnew.SHAPE_T_SEGMENT)
    segment.SetStart(pcbnew.VECTOR2I(0, 0))
    segment.SetEnd(pcbnew.VECTOR2I(300000, 250000))
    segment.SetWidth(100000)
    pad.AddPrimitiveShape(pcbnew.F_Cu, segment)
    segment.thisown = False
    save(native_board, board)
    data = bridge.export_board(native_board, RULES, 100000)
    exported = next(p for p in data["pads"] if p["footprint_ref"] == "J1")
    x, y = exported["at"]
    assert exported["shape"]["shapes"][1] == {
        "kind": "capsule", "start_nm": [x, y], "end_nm": [x + 300000, y + 250000], "width_nm": 100000}
    odd = pcbnew.PCB_SHAPE(pad)
    odd.SetShape(pcbnew.SHAPE_T_SEGMENT)
    odd.SetStart(pcbnew.VECTOR2I(0, 0))
    odd.SetEnd(pcbnew.VECTOR2I(300000, 250000))
    odd.SetWidth(100001)
    pad.AddPrimitiveShape(pcbnew.F_Cu, odd)
    odd.thisown = False
    save(native_board, board)
    with pytest.raises(bridge.UnsupportedBoard, match="positive even width"):
        bridge.export_board(native_board, RULES, 100000)
    for _ in range(15):
        _filled_custom_circle(pad, pcbnew.F_Cu, 0, 0, 100000)
    save(native_board, board)
    with pytest.raises(bridge.UnsupportedBoard, match="more than 16 copper shapes"):
        bridge.export_board(native_board, RULES, 100000)


def test_native_compound_zone_clearance_requires_the_real_outline(native_board):
    board = pcbnew.LoadBoard(str(native_board))
    pad = _custom_j1(board, [(-99999, -200000), (500000, -200000), (500000, 200000), (-99999, 200000)])
    pad.SetCustomShapeInZoneOpt(pcbnew.CUSTOM_SHAPE_ZONE_MODE_CONVEXHULL)
    zone = pcbnew.ZONE(board)
    zone.SetLayer(pcbnew.F_Cu)
    zone.SetNetCode(2)
    _add_rect_outline(zone.Outline(), 2000000, 2000000, 18000000, 18000000)
    board.Add(zone)
    save(native_board, board)
    with pytest.raises(bridge.UnsupportedBoard, match="convex-hull zone clearance"):
        bridge.export_board(native_board, RULES, 100000)
    pad.SetCustomShapeInZoneOpt(pcbnew.CUSTOM_SHAPE_ZONE_MODE_OUTLINE)
    save(native_board, board)
    data = bridge.export_board(native_board, RULES, 100000)
    assert next(p for p in data["pads"] if p["footprint_ref"] == "J1")["shape"]["kind"] == "union"


@pytest.mark.parametrize("primitive", ["gr_arc", "gr_vector"])
def test_custom_curve_and_thermal_proxy_keep_their_native_meaning(native_board, primitive):
    board = pcbnew.LoadBoard(str(native_board))
    pad = _custom_j1(board, [])
    shape = pcbnew.PCB_SHAPE(pad)
    if primitive == "gr_vector":
        shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
        shape.SetStart(pcbnew.VECTOR2I(0, 0))
        shape.SetEnd(pcbnew.VECTOR2I(200000, 300000))
        shape.SetIsProxyItem(True)
    else:
        shape.SetShape(pcbnew.SHAPE_T_ARC)
        shape.SetArcGeometry(pcbnew.VECTOR2I(0, 0), pcbnew.VECTOR2I(300000, -300000),
                             pcbnew.VECTOR2I(600000, 0))
        shape.SetWidth(100000)
    pad.AddPrimitiveShape(pcbnew.F_Cu, shape)
    shape.thisown = False
    save(native_board, board)
    with pytest.raises(bridge.UnsupportedBoard, match=rf"custom pad J1\.1: native {primitive} primitive"):
        bridge.export_board(native_board, RULES, 100000)


def test_foreign_custom_copper_tip_forces_clearance_on_saved_route(native_board, tmp_path):
    board = pcbnew.LoadBoard(str(native_board))
    for track in list(board.GetTracks()):
        board.Remove(track)
    # J1 becomes an obstacle. Its anchor misses the direct signal path by
    # 1.4 mm, while the actual polygon tip crosses the path.
    pad = _custom_j1(board, [(-300000, -1700000), (300000, -1700000),
                             (300000, 200000), (-300000, 200000)])
    pad.GetParentFootprint().SetPosition(pcbnew.VECTOR2I(10000000, 11500000))
    pad.SetNetCode(0)
    pad.SetLocalClearance(500000)
    j2 = next(p for p in board.GetPads() if p.GetParentFootprint().GetReference() == "J2")
    j3 = pcbnew.Cast_to_FOOTPRINT(j2.GetParentFootprint().Duplicate(False))
    j3.SetReference("J3")
    j3.SetPosition(pcbnew.VECTOR2I(3000000, 10000000))
    board.Add(j3)
    save(native_board, board)
    assert pad.GetEffectiveShape(pcbnew.F_Cu).Collide(pcbnew.VECTOR2I(10000000, 10000000))
    _, plan, output = _route_and_verify_drc(native_board, tmp_path)
    assert plan["added_tracks"]
    saved = pcbnew.LoadBoard(str(output))
    obstacle = next(p for p in saved.GetPads() if p.GetParentFootprint().GetReference() == "J1")
    copper = obstacle.GetEffectiveShape(pcbnew.F_Cu)
    for track in saved.GetTracks():
        if track.IsOnLayer(pcbnew.F_Cu):
            assert not copper.Collide(track.GetEffectiveShape(pcbnew.F_Cu), 499999)


@pytest.mark.parametrize(
    "size,ratio,expected",
    [
        ((1000000, 1000000), 0.5, {"kind": "circle", "radius_nm": 500000}),
        ((1000002, 1000000), 0.5, {"kind": "circle", "radius_nm": 500000}),
        ((1000198, 1000000), 0.5, {"kind": "circle", "radius_nm": 500000}),
        (
            (1000200, 1000000),
            0.5,
            {
                "kind": "round_rect",
                "half_size_nm": [500100, 500000],
                "radius_nm": 500000,
            },
        ),
        (
            (1000000, 1000000),
            0,
            {"kind": "round_rect", "half_size_nm": [500000, 500000], "radius_nm": 0},
        ),
    ],
)
def test_native_near_circle_effective_shape_is_exact_and_metadata_is_preserved(
    native_board, tmp_path, size, ratio, expected
):
    board = pcbnew.LoadBoard(str(native_board))
    for pad in board.GetPads():
        rounded(pad, pcbnew.PAD_SHAPE_ROUNDRECT, size=size, ratio=ratio)
    save(native_board, board)
    data = bridge.export_board(native_board, RULES, 100000)
    assert all(p["shape"] == expected for p in data["pads"])
    # Native effective BBox must match the emitted circle at the collapse,
    # including the one-nm nominal sliver KiCad deliberately suppresses.
    loaded = pcbnew.LoadBoard(str(native_board))
    for pad in loaded.GetPads():
        effective = pad.GetEffectiveShape(pcbnew.F_Cu)
        box = effective.BBox()
        if expected["kind"] == "circle":
            assert (box.GetWidth(), box.GetHeight()) == (2 * expected["radius_nm"],) * 2
            assert (
                box.GetX() + expected["radius_nm"],
                box.GetY() + expected["radius_nm"],
            ) == tuple(bridge.point(pad.GetPosition()))
    request, plan = execute(native_board, data, tmp_path)
    output = tmp_path / "circle-routed.kicad_pcb"
    bridge.apply_plan(native_board, plan, output, expected_request=request.read_bytes())
    saved = pcbnew.LoadBoard(str(output))
    saved.BuildConnectivity()
    assert saved.GetConnectivity().GetUnconnectedCount(False) == 0
    assert all(
        p.GetShape() == pcbnew.PAD_SHAPE_ROUNDRECT
        and bridge.point(p.GetSize()) == list(size)
        and p.GetRoundRectRadiusRatio() == ratio
        for p in saved.GetPads()
    )


@pytest.mark.parametrize("ratio", ["-0.01", "0.500001", "1", "nan", "inf"])
def test_native_source_radius_cannot_be_silently_clamped(native_board, ratio):
    board = pcbnew.LoadBoard(str(native_board))
    for pad in board.GetPads():
        rounded(pad, pcbnew.PAD_SHAPE_ROUNDRECT)
    save(native_board, board)
    text = native_board.read_text().replace(
        "(roundrect_rratio 0.25)", f"(roundrect_rratio {ratio})"
    )
    native_board.write_text(text)
    with pytest.raises(ValueError, match="radius"):
        bridge.export_board(native_board, RULES, 100000)


@pytest.mark.parametrize("kind", [pcbnew.PAD_SHAPE_OVAL, pcbnew.PAD_SHAPE_ROUNDRECT])
@pytest.mark.parametrize(
    "width,dx,expected_before_unconnected,expected_repaired",
    [
        (250000, 0, 1, True),   # exact 0.0 nm tangency
        (250000, 1, 0, False),  # +1.0 nm physical overlap
        (250001, -1, 1, True),  # -0.5 nm gap (strictly disconnected)
        (250001, 0, 0, True),   # +0.5 nm physical overlap (KiCad connected; Rust conservatively repairs)
        (250001, 1, 0, False),  # +1.5 nm physical overlap (1.0 nm after flooring half-width; reused)
    ],
)
def test_native_round_pad_tangency_is_repaired_and_one_nm_overlap_reuses_incumbent(
    native_board,
    tmp_path,
    kind,
    width,
    dx,
    expected_before_unconnected,
    expected_repaired,
):
    board = pcbnew.LoadBoard(str(native_board))
    for pad in board.GetPads():
        rounded(pad, kind)
    corners = [
        [3000000, 10000000],
        [3000000, 2000000],
        [15000000, 2000000],
        [15000000, 10000000],
        [15875000 + dx, 10000000],
    ]
    for a, b in zip(corners, corners[1:]):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(*a))
        t.SetEnd(pcbnew.VECTOR2I(*b))
        t.SetWidth(width)
        t.SetLayer(pcbnew.F_Cu)
        t.SetNetCode(1)
        t.SetLocked(True)
        board.Add(t)
    save(native_board, board)
    before = bridge.native_violations(native_board, tmp_path / "overlap-before.json")
    loaded = pcbnew.LoadBoard(str(native_board))
    loaded.BuildConnectivity()
    before_unconnected = loaded.GetConnectivity().GetUnconnectedCount(False)
    if (width, dx) == (250001, 0):
        # KiCad 10.0.8's connectivity engine (both kicad-cli DRC and in-process
        # BuildConnectivity()) is non-deterministic ({0, 1}) at the +0.5 nm
        # physical overlap boundary across fresh board UUIDs; Rust conservatively
        # repairs it so post-routing connectivity is deterministically 0.
        assert len(before["unconnected_items"]) in {0, 1}
        assert before_unconnected in {0, 1}
    else:
        assert len(before["unconnected_items"]) == expected_before_unconnected
        assert before_unconnected == expected_before_unconnected
    data = bridge.export_board(native_board, RULES, 100000)
    request, plan = execute(native_board, data, tmp_path)
    assert bool(plan["added_tracks"]) == expected_repaired
    output = tmp_path / "overlap-routed.kicad_pcb"
    bridge.apply_plan(native_board, plan, output, expected_request=request.read_bytes())
    saved = pcbnew.LoadBoard(str(output))
    saved.BuildConnectivity()
    assert saved.GetConnectivity().GetUnconnectedCount(False) == 0
    after = run_native_drc(output, sidecar_source=native_board).raw
    assert after["unconnected_items"] == []
    key = lambda violation: json.dumps(violation, sort_keys=True)
    assert not (
        Counter(map(key, after["violations"])) - Counter(map(key, before["violations"]))
    )
    assert {
        t["id"]: t for t in bridge.export_board(output, RULES, 100000)["tracks"]
    } == {t["id"]: t for t in data["tracks"] + plan["added_tracks"]}


@pytest.mark.parametrize(
    "dimensions",
    [
        "0 1",
        "-1 1",
        "0.0000001 1",
        "1.000000000000000000000000000001 1",
        "201 1",
        "nan 1",
        "inf 1",
    ],
)
def test_native_source_dimensions_are_not_rounded_or_clamped(native_board, dimensions):
    board = pcbnew.LoadBoard(str(native_board))
    for pad in board.GetPads():
        rounded(pad, pcbnew.PAD_SHAPE_OVAL)
    save(native_board, board)
    text = native_board.read_text().replace("(size 2 1)", f"(size {dimensions})", 1)
    native_board.write_text(text)
    with pytest.raises(ValueError, match="pad dimensions"):
        bridge.export_board(native_board, RULES, 100000)


def test_scalar_validation_ignores_quoted_pad_expressions():
    bridge.validate_native_pad_scalars(
        b'(kicad_pcb (property "note" "(pad x (size 0 1) (roundrect_rratio 9))"))'
    )
    bridge.validate_native_pad_scalars(
        b'(kicad_pcb (property "note" "escaped \\" (pad x (size 0 1))"))'
    )


def test_native_near_rounded_corner_clearance_rejects_false_complete_plan(
    native_board, tmp_path
):
    board = pcbnew.LoadBoard(str(native_board))
    for pad in board.GetPads():
        rounded(pad, pcbnew.PAD_SHAPE_ROUNDRECT)
    save(native_board, board)
    data = bridge.export_board(native_board, RULES, 100000)
    request, plan = execute(native_board, data, tmp_path)
    valid = tmp_path / "valid.kicad_pcb"
    bridge.apply_plan(native_board, plan, valid, expected_request=request.read_bytes())
    # J2's NE core corner is (17.75, 10.25) mm, radius 0.25 mm.
    # Vector (0.345, 0.460) mm has length 0.575 mm, matching KiCad's default
    # 0.2 mm netclass clearance + pad 0.25 mm + trace 0.125 mm. Move 10 um inward.
    obstacle_net = data["tracks"][0]["net"]
    additions = [
        {
            "id": str(uuid.uuid4()),
            "net": obstacle_net,
            "start": [10000000, 16000000],
            "end": [18085000, 16000000],
            "width_nm": 250000,
            "locked": False,
        },
        {
            "id": str(uuid.uuid4()),
            "net": obstacle_net,
            "start": [18085000, 16000000],
            "end": [18085000, 10710000],
            "width_nm": 250000,
            "locked": False,
        },
    ]
    unsafe = pcbnew.LoadBoard(str(valid))
    for addition in additions:
        track = pcbnew.PCB_TRACK(unsafe)
        track.SetStart(pcbnew.VECTOR2I(*addition["start"]))
        track.SetEnd(pcbnew.VECTOR2I(*addition["end"]))
        track.SetWidth(addition["width_nm"])
        track.SetLayer(pcbnew.F_Cu)
        track.SetNetCode(obstacle_net)
        track.SetUuid(pcbnew.KIID(addition["id"]))
        unsafe.Add(track)
    unsafe_path = tmp_path / "unsafe-witness.kicad_pcb"
    save(unsafe_path, unsafe)
    report = bridge.native_violations(unsafe_path, tmp_path / "unsafe-drc.json")
    assert not report["unconnected_items"]
    assert any(v["type"] == "clearance" for v in report["violations"])
    output = tmp_path / "refused-corner.kicad_pcb"
    with pytest.raises(bridge.NativeValidationError, match="DRC violations"):
        bridge.apply_plan(
            native_board,
            {**plan, "added_tracks": plan["added_tracks"] + additions},
            output,
            expected_request=request.read_bytes(),
        )
    assert not output.exists()


def test_chamfered_roundrect_is_rejected_in_memory_and_on_disk(native_board):
    board = pcbnew.LoadBoard(str(native_board))
    pad = next(iter(board.GetPads()))
    rounded(pad, pcbnew.PAD_SHAPE_ROUNDRECT)
    pad.SetChamferPositions(1)
    assert pad.GetShape() == pcbnew.PAD_SHAPE_ROUNDRECT
    with pytest.raises(bridge.UnsupportedBoard, match="chamfered"):
        bridge.pad_shape(pad)
    pad.SetShape(pcbnew.PAD_SHAPE_CHAMFERED_RECT)
    save(native_board, board)
    with pytest.raises(bridge.UnsupportedBoard, match="chamfered"):
        bridge.export_board(native_board, RULES, 100000)


@pytest.mark.parametrize(
    "mutation,exc_type,match",
    [
        ("padstack", bridge.UnsupportedBoard, "pad stack"),
        ("chamfer_sexpr", bridge.UnsupportedBoard, "chamfered"),
        ("missing_size", ValueError, "declared explicitly"),
        ("unterminated_quote", ValueError, "unterminated string"),
        ("underscore_scalar", ValueError, "numeric scalars"),
        ("non_utf8", UnicodeDecodeError, None),
    ],
)
def test_native_pad_scalar_validation_fails_closed(native_board, mutation, exc_type, match):
    board = pcbnew.LoadBoard(str(native_board))
    for pad in board.GetPads():
        rounded(pad, pcbnew.PAD_SHAPE_ROUNDRECT)
    save(native_board, board)
    if mutation == "non_utf8":
        raw = native_board.read_bytes() + b"\xff"
        native_board.write_bytes(raw)
    else:
        text = native_board.read_text()
        if mutation == "padstack":
            text = text.replace(
                '(pad "1" smd roundrect',
                '(pad "1" smd roundrect (padstack (mode normal))',
                1,
            )
        elif mutation == "chamfer_sexpr":
            text = text.replace(
                '(pad "1" smd roundrect',
                '(pad "1" smd roundrect (chamfer top_left)',
                1,
            )
        elif mutation == "missing_size":
            text = text.replace("(size 2 1)", "", 1)
        elif mutation == "unterminated_quote":
            text = text.replace('(property "note"', '(property "note', 1)
            if '(property "note' not in text:
                text = text.replace('(pad "1"', '(pad "1', 1)
        elif mutation == "underscore_scalar":
            text = text.replace("(size 2 1)", "(size 1_0 1)", 1)
        native_board.write_text(text)
    with pytest.raises(exc_type, match=match):
        bridge.export_board(native_board, RULES, 100000)


def test_validate_native_pad_scalars_scopes_size_to_direct_pad_child():
    # A nested (size 0 1) inside a non-direct child of (pad ...) must not trigger
    # pad-dimension failure when the pad itself has a valid direct (size 2 1).
    bridge.validate_native_pad_scalars(
        b'(kicad_pcb (footprint "FP" (pad "1" smd rect (size 2 1) (custom (size 0 1)))))'
    )
    # A nested (drill ...) or (padstack ...) at any depth under (pad ...) is still rejected.
    with pytest.raises(bridge.UnsupportedBoard, match="drilled"):
        bridge.validate_native_pad_scalars(
            b'(kicad_pcb (footprint "FP" (pad "1" smd rect (size 2 1) (nested (drill 0.1)))))'
        )
    with pytest.raises(bridge.UnsupportedBoard, match="pad stack"):
        bridge.validate_native_pad_scalars(
            b'(kicad_pcb (footprint "FP" (pad "1" smd rect (size 2 1) (nested (padstack)))))'
        )


def test_nonsquare_circle_pad_is_rejected(native_board):
    board = pcbnew.LoadBoard(str(native_board))
    pad = next(iter(board.GetPads()))
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetSize(pcbnew.VECTOR2I(1000000, 1000002))
    save(native_board, board)
    with pytest.raises(bridge.UnsupportedBoard, match="circular pads"):
        bridge.export_board(native_board, RULES, 100000)


def test_odd_circle_pad_routes_and_validates(native_board, tmp_path):
    board = pcbnew.LoadBoard(str(native_board))
    for pad in board.GetPads():
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(1000001, 1000001))
    save(native_board, board)
    data = bridge.export_board(native_board, RULES, 100000)
    assert all(p["shape"]["kind"] == "box_exact" for p in data["pads"])
    request, plan = execute(native_board, data, tmp_path)
    output = tmp_path / "odd-circle.kicad_pcb"
    bridge.apply_plan(native_board, plan, output, expected_request=request.read_bytes())
    saved = pcbnew.LoadBoard(str(output))
    saved.BuildConnectivity()
    assert saved.GetConnectivity().GetUnconnectedCount(False) == 0


def test_effective_shape_compound_none_fails_closed(native_board, monkeypatch):
    board = pcbnew.LoadBoard(str(native_board))
    for pad in board.GetPads():
        rounded(pad, pcbnew.PAD_SHAPE_ROUNDRECT)
    save(native_board, board)
    monkeypatch.setattr(bridge.pcbnew, "Cast_to_SHAPE_COMPOUND", lambda _raw: None)
    with pytest.raises(bridge.UnsupportedBoard, match="effective shape"):
        bridge.export_board(native_board, RULES, 100000)


def _route_and_verify_drc(board_path, tmp_path, *, rules=RULES, expected_opens_before=1):
    data = bridge.export_board(board_path, rules, 100000)
    request, plan = execute(board_path, data, tmp_path)
    output = tmp_path / "routed.kicad_pcb"
    bridge.apply_plan(board_path, plan, output, expected_request=request.read_bytes())
    saved = pcbnew.LoadBoard(str(output))
    saved.BuildConnectivity()
    assert saved.GetConnectivity().GetUnconnectedCount(False) == 0
    before = bridge.native_violations(board_path, tmp_path / "before-drc.json")
    after = run_native_drc(output, sidecar_source=board_path).raw
    assert len(before["unconnected_items"]) == expected_opens_before
    assert after["unconnected_items"] == []
    key = lambda violation: json.dumps(violation, sort_keys=True)
    assert not (
        Counter(map(key, after["violations"])) - Counter(map(key, before["violations"]))
    )
    return data, plan, output


def test_native_rotated_odd_nm_offset_and_trapezoid_pads_route_and_drc(native_board, tmp_path):
    board = pcbnew.LoadBoard(str(native_board))
    pads = sorted(board.GetPads(), key=lambda p: p.GetPosition().x)
    # J1: odd-nm Rect + SetOffset; J2: rotated 45 deg Oval inside 30 deg rotated footprint -> 45 deg board-abs.
    pads[0].SetShape(pcbnew.PAD_SHAPE_RECT)
    pads[0].SetSize(pcbnew.VECTOR2I(1000001, 500001))
    pads[0].SetOffset(pcbnew.VECTOR2I(500000, 0))
    fp2 = pads[1].GetParentFootprint()
    fp2.SetOrientationDegrees(30.0)
    pads[1].SetShape(pcbnew.PAD_SHAPE_OVAL)
    pads[1].SetSize(pcbnew.VECTOR2I(2000000, 1000000))
    pads[1].SetOrientationDegrees(45.55)
    # Odd-nm Oval obstacle at (10, 2) mm verifying KiCad's +1 nm width rounding in GetEffectiveShape.
    fp_obs = pcbnew.FOOTPRINT(board)
    fp_obs.SetReference("OB1")
    fp_obs.SetPosition(pcbnew.VECTOR2I(10000000, 2000000))
    board.Add(fp_obs)
    obs = pcbnew.PAD(fp_obs)
    obs.SetNumber("1")
    obs.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    layers = pcbnew.LSET()
    layers.AddLayer(pcbnew.F_Cu)
    obs.SetLayerSet(layers)
    obs.SetShape(pcbnew.PAD_SHAPE_OVAL)
    obs.SetSize(pcbnew.VECTOR2I(1000000, 500001))
    obs.SetPosition(fp_obs.GetPosition())
    obs.SetNetCode(0)
    fp_obs.Add(obs)
    # Rotated Trapezoid obstacle at (14, 18) mm.
    fp_trap = pcbnew.FOOTPRINT(board)
    fp_trap.SetReference("OB2")
    fp_trap.SetPosition(pcbnew.VECTOR2I(14000000, 18000000))
    board.Add(fp_trap)
    trap = pcbnew.PAD(fp_trap)
    trap.SetNumber("1")
    trap.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    trap.SetLayerSet(layers)
    trap.SetShape(pcbnew.PAD_SHAPE_TRAPEZOID)
    trap.SetSize(pcbnew.VECTOR2I(2000000, 1000000))
    trap.SetDelta(pcbnew.VECTOR2I(200000, 0))
    trap.SetOrientationDegrees(135.0)
    trap.SetPosition(fp_trap.GetPosition())
    trap.SetNetCode(0)
    fp_trap.Add(trap)
    save(native_board, board)

    data, plan, _ = _route_and_verify_drc(native_board, tmp_path)
    assert plan["added_tracks"]
    obs_exported = next(p for p in data["pads"] if p["net"] == 0 and p["shape"]["kind"] == "capsule")
    assert obs_exported["shape"]["width_nm"] == 500002


def test_native_pth_oblong_and_npth_holes_route_and_pass_drc(native_board, tmp_path):
    board = pcbnew.LoadBoard(str(native_board))
    pads = sorted(board.GetPads(), key=lambda p: p.GetPosition().x)
    all_cu = pcbnew.LSET.AllCuMask()
    # J1: circular PTH; J2: rotated oblong PTH.
    for pad, shape, size, drill_shape, drill_size, angle in [
        (pads[0], pcbnew.PAD_SHAPE_CIRCLE, (2000000, 2000000), pcbnew.PAD_DRILL_SHAPE_CIRCLE, (1000000, 1000000), 0.0),
        (pads[1], pcbnew.PAD_SHAPE_OVAL, (3000000, 1800000), pcbnew.PAD_DRILL_SHAPE_OBLONG, (1600000, 800000), 30.0),
    ]:
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetLayerSet(all_cu)
        pad.SetShape(shape)
        pad.SetSize(pcbnew.VECTOR2I(*size))
        pad.SetDrillShape(drill_shape)
        pad.SetDrillSize(pcbnew.VECTOR2I(*drill_size))
        pad.SetOrientationDegrees(angle)
    # NPTH mounting hole at (10, 10) mm.
    mh_fp = pcbnew.FOOTPRINT(board)
    mh_fp.SetReference("MH1")
    mh_fp.SetPosition(pcbnew.VECTOR2I(10000000, 10000000))
    board.Add(mh_fp)
    mh = pcbnew.PAD(mh_fp)
    mh.SetNumber("1")
    mh.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
    mh.SetLayerSet(pcbnew.LSET())
    mh.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    mh.SetSize(pcbnew.VECTOR2I(3000000, 3000000))
    mh.SetDrillShape(pcbnew.PAD_DRILL_SHAPE_CIRCLE)
    mh.SetDrillSize(pcbnew.VECTOR2I(3000000, 3000000))
    mh.SetPosition(mh_fp.GetPosition())
    mh.SetNetCode(0)
    mh_fp.Add(mh)
    save(native_board, board)

    data, plan, _ = _route_and_verify_drc(native_board, tmp_path)
    assert plan["added_tracks"]
    assert len(data["holes"]) == 1
    assert data["rules"]["hole_clearance_nm"] >= 250000
    assert all("drill" in p for p in data["pads"])


def test_native_nonrect_outline_footprint_slot_and_pcb_arc_route_and_drc(native_board, tmp_path):
    board = pcbnew.LoadBoard(str(native_board))
    for drawing in list(board.GetDrawings()):
        board.Remove(drawing)
    for track in list(board.GetTracks()):
        board.Remove(track)
    l_pts = [
        (0, 0), (30000000, 0), (30000000, 15000000),
        (15000000, 15000000), (15000000, 30000000), (0, 30000000),
    ]
    for a, b in zip(l_pts, l_pts[1:] + l_pts[:1]):
        edge = pcbnew.PCB_SHAPE(board)
        edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
        edge.SetLayer(pcbnew.Edge_Cuts)
        edge.SetStart(pcbnew.VECTOR2I(*a))
        edge.SetEnd(pcbnew.VECTOR2I(*b))
        edge.SetWidth(50000)
        board.Add(edge)
    pads = sorted(board.GetPads(), key=lambda p: p.GetPosition().x)
    for pad, pos in zip(pads, [(5000000, 5000000), (5000000, 25000000)]):
        pad.GetParentFootprint().SetPosition(pcbnew.VECTOR2I(*pos))
        pad.SetPosition(pcbnew.VECTOR2I(*pos))
    # Footprint closed Edge.Cuts slot at (9..11, 10..12) mm.
    slot_fp = pcbnew.FOOTPRINT(board)
    slot_fp.SetReference("U_SLOT")
    slot_fp.SetPosition(pcbnew.VECTOR2I(10000000, 11000000))
    board.Add(slot_fp)
    slot_pts = [(9000000, 10000000), (11000000, 10000000), (11000000, 12000000), (9000000, 12000000)]
    for a, b in zip(slot_pts, slot_pts[1:] + slot_pts[:1]):
        seg = pcbnew.PCB_SHAPE(slot_fp)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetStart(pcbnew.VECTOR2I(*a))
        seg.SetEnd(pcbnew.VECTOR2I(*b))
        seg.SetWidth(50000)
        slot_fp.Add(seg)
    # Locked PCB_ARC obstacle on net 2.
    arc = pcbnew.PCB_ARC(board)
    arc.SetLayer(pcbnew.F_Cu)
    arc.SetNetCode(2)
    arc.SetStart(pcbnew.VECTOR2I(1000000, 15000000))
    arc.SetMid(pcbnew.VECTOR2I(6000000, 13000000))
    arc.SetEnd(pcbnew.VECTOR2I(11000000, 15000000))
    arc.SetWidth(250000)
    arc.SetLocked(True)
    board.Add(arc)
    save(native_board, board)

    data, plan, output = _route_and_verify_drc(native_board, tmp_path)
    assert plan["added_tracks"]
    assert "board_outline" in data
    assert len(data["board_outline"]["polygons"][0]["holes"]) == 1
    assert any(g["shape"]["kind"] == "arc" for g in data["graphics"])
    saved = pcbnew.LoadBoard(str(output))
    assert any(type(t) is pcbnew.PCB_ARC for t in saved.GetTracks())


def test_native_board_solder_mask_aperture_and_bezier_refusal(native_board, tmp_path):
    board = pcbnew.LoadBoard(str(native_board))
    ds = board.GetDesignSettings()
    ds.m_SolderMaskMinWidth = 200000
    ds.m_SolderMaskToCopperClearance = 100000
    fp_obs = pcbnew.FOOTPRINT(board)
    fp_obs.SetReference("TP_OBS")
    fp_obs.SetPosition(pcbnew.VECTOR2I(10000000, 8000000))
    board.Add(fp_obs)
    obs = pcbnew.PAD(fp_obs)
    obs.SetNumber("1")
    obs.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    ls = pcbnew.LSET()
    ls.AddLayer(pcbnew.F_Cu)
    obs.SetLayerSet(ls)
    obs.SetShape(pcbnew.PAD_SHAPE_RECT)
    obs.SetSize(pcbnew.VECTOR2I(400000, 400000))
    obs.SetPosition(fp_obs.GetPosition())
    obs.SetNetCode(2)
    fp_obs.Add(obs)
    mask_seg = pcbnew.PCB_SHAPE(board)
    mask_seg.SetLayer(pcbnew.F_Mask)
    mask_seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
    mask_seg.SetStart(pcbnew.VECTOR2I(10000000, 8000000))
    mask_seg.SetEnd(pcbnew.VECTOR2I(10000000, 12000000))
    mask_seg.SetWidth(1000000)
    board.Add(mask_seg)
    save(native_board, board)

    data, plan, _ = _route_and_verify_drc(native_board, tmp_path)
    assert plan["added_tracks"]
    assert data["mask_apertures"]
    assert data["rules"]["solder_mask_min_width_nm"] == 200000

    # Bezier copper curve on F.Cu must fail closed (UnsupportedBoard).
    bz = pcbnew.PCB_SHAPE(board)
    bz.SetLayer(pcbnew.F_Cu)
    bz.SetShape(pcbnew.SHAPE_T_BEZIER)
    bz.SetStart(pcbnew.VECTOR2I(10000000, 6000000))
    bz.SetEnd(pcbnew.VECTOR2I(10000000, 14000000))
    bz.SetBezierC1(pcbnew.VECTOR2I(12000000, 8000000))
    bz.SetBezierC2(pcbnew.VECTOR2I(8000000, 12000000))
    bz.SetWidth(250000)
    board.Add(bz)
    bz_path = tmp_path / "bezier.kicad_pcb"
    save(bz_path, board)
    with pytest.raises(bridge.UnsupportedBoard, match="Bezier"):
        bridge.export_board(bz_path, RULES, 100000)


def test_native_ultra_large_major_pcb_arc_refused_without_panic(native_board, tmp_path):
    board = pcbnew.LoadBoard(str(native_board))
    arc = pcbnew.PCB_ARC(board)
    arc.SetLayer(pcbnew.F_Cu)
    arc.SetNetCode(0)
    arc.SetStart(pcbnew.VECTOR2I(10_000_000, 15_000_000))
    arc.SetMid(pcbnew.VECTOR2I(9_999_999, 15_000_000))
    arc.SetEnd(pcbnew.VECTOR2I(20_000_000, 15_000_100))
    arc.SetWidth(250_000)
    board.Add(arc)
    save(native_board, board)

    data = bridge.export_board(native_board, RULES, 100000)
    request, result = tmp_path / "arc-req.json", tmp_path / "arc-res.json"
    bridge.write_request(data, request)
    done = subprocess.run(
        [
            "cargo",
            "run",
            "--locked",
            "--offline",
            "--manifest-path",
            str(ROOT / "rust-engine/Cargo.toml"),
            "--",
            str(request),
            str(result),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 2, (done.stdout, done.stderr)
    out = json.loads(result.read_text())
    assert out["status"] == "unsupported"


def test_native_solder_mask_min_width_without_copper_clearance_routes_over_mask_and_drc(
    native_board, tmp_path
):
    board = pcbnew.LoadBoard(str(native_board))
    ds = board.GetDesignSettings()
    ds.m_SolderMaskMinWidth = 200000
    ds.m_SolderMaskToCopperClearance = 0
    board.Add(pcbnew.NETINFO_ITEM(board, "AUX", 3))
    # 2-net footprint with an F.Mask aperture directly covering Pad 1 (Secondary Reviewer Probe 2).
    fp_mask = pcbnew.FOOTPRINT(board)
    fp_mask.SetReference("U_MASK")
    fp_mask.SetPosition(pcbnew.VECTOR2I(5_000_000, 10_000_000))
    board.Add(fp_mask)
    ls = pcbnew.LSET()
    ls.AddLayer(pcbnew.F_Cu)
    for num, net_code, dy in [("1", 1, 0), ("2", 3, 2_000_000)]:
        p = pcbnew.PAD(fp_mask)
        p.SetNumber(num)
        p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        p.SetLayerSet(ls)
        p.SetShape(pcbnew.PAD_SHAPE_RECT)
        p.SetSize(pcbnew.VECTOR2I(500_000, 500_000))
        p.SetPosition(pcbnew.VECTOR2I(5_000_000, 10_000_000 + dy))
        p.SetNetCode(net_code)
        fp_mask.Add(p)
    mask_rect = pcbnew.PCB_SHAPE(fp_mask)
    mask_rect.SetLayer(pcbnew.F_Mask)
    mask_rect.SetShape(pcbnew.SHAPE_T_RECT)
    mask_rect.SetFilled(True)
    mask_rect.SetStart(pcbnew.VECTOR2I(4_600_000, 9_600_000))
    mask_rect.SetEnd(pcbnew.VECTOR2I(5_400_000, 10_400_000))
    mask_rect.SetWidth(0)
    fp_mask.Add(mask_rect)
    save(native_board, board)

    data, plan, _ = _route_and_verify_drc(native_board, tmp_path, expected_opens_before=2)
    assert plan["added_tracks"]
    assert data["rules"]["solder_mask_min_width_nm"] == 200000

    # Same U_MASK 2-net footprint with positive solder_mask_to_copper_clearance=100k also connects Pad 1 cleanly.
    pos_board = pcbnew.LoadBoard(str(native_board))
    pos_board.GetDesignSettings().m_SolderMaskToCopperClearance = 100_000
    pos_path = tmp_path / "umask-clr100k.kicad_pcb"
    save(pos_path, pos_board)
    pos_dir = tmp_path / "umask-clr100k-run"
    pos_dir.mkdir()
    _, pos_plan, pos_out = _route_and_verify_drc(pos_path, pos_dir, expected_opens_before=2)
    assert pos_plan["added_tracks"]
    pos_drc = bridge.native_violations(pos_out, pos_dir / "umask-drc.json")
    assert pos_drc["unconnected_items"] == []
    assert not any(v["type"] == "solder_mask_bridge" for v in pos_drc["violations"])

    # Verify load_supported (apply_plan) and export_board agree on F.Mask Bezier even when mask rules are 0.
    board2 = pcbnew.LoadBoard(str(native_board))
    ds2 = board2.GetDesignSettings()
    ds2.m_SolderMaskMinWidth = 0
    ds2.m_SolderMaskToCopperClearance = 0
    bz_mask = pcbnew.PCB_SHAPE(board2)
    bz_mask.SetLayer(pcbnew.F_Mask)
    bz_mask.SetShape(pcbnew.SHAPE_T_BEZIER)
    bz_mask.SetStart(pcbnew.VECTOR2I(6_000_000, 6_000_000))
    bz_mask.SetEnd(pcbnew.VECTOR2I(8_000_000, 6_000_000))
    bz_mask.SetBezierC1(pcbnew.VECTOR2I(6_500_000, 7_000_000))
    bz_mask.SetBezierC2(pcbnew.VECTOR2I(7_500_000, 5_000_000))
    bz_mask.SetWidth(100_000)
    board2.Add(bz_mask)
    bz_mask_path = tmp_path / "fmask-bezier.kicad_pcb"
    save(bz_mask_path, board2)
    with pytest.raises(bridge.UnsupportedBoard, match="Bezier"):
        bridge.load_supported(bz_mask_path)


def test_native_rotated_odd_circle_and_square_oval_preserve_1nm_diameter(native_board, tmp_path):
    board = pcbnew.LoadBoard(str(native_board))
    pads = sorted(board.GetPads(), key=lambda p: p.GetPosition().x)
    pads[0].SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pads[0].SetSize(pcbnew.VECTOR2I(1_000_001, 1_000_001))
    pads[0].SetOrientationDegrees(45.0)
    pads[1].SetShape(pcbnew.PAD_SHAPE_OVAL)
    pads[1].SetSize(pcbnew.VECTOR2I(1_000_001, 1_000_001))
    pads[1].SetOrientationDegrees(45.0)
    save(native_board, board)

    data, plan, _ = _route_and_verify_drc(native_board, tmp_path)
    assert plan["added_tracks"]
    for p in data["pads"]:
        assert p["shape"]["kind"] == "box_exact"
        assert p["shape"]["half_size_2x"] == [1_000_001, 1_000_001]
        assert p["shape"]["radius_2x"] == 1_000_001


@pytest.mark.parametrize("size", [(1_000_001, 500_001), (2_000_000, 1_000_001), (1_000_001, 1_000_001)])
@pytest.mark.parametrize("angle", [0.0, 45.0])
def test_native_odd_minor_roundrect_rratio_05_routes_and_drc(native_board, tmp_path, size, angle):
    board = pcbnew.LoadBoard(str(native_board))
    for pad in board.GetPads():
        rounded(pad, pcbnew.PAD_SHAPE_ROUNDRECT, size=size, angle=angle, ratio=0.5)
    save(native_board, board)

    _, plan, _ = _route_and_verify_drc(native_board, tmp_path)
    assert plan["added_tracks"]


def test_native_asymmetric_rotated_trapezoid_boundary_and_drc(native_board, tmp_path):
    board = pcbnew.LoadBoard(str(native_board))
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("OB_TRAP")
    fp.SetPosition(pcbnew.VECTOR2I(10_000_000, 18_000_000))
    board.Add(fp)
    trap = pcbnew.PAD(fp)
    trap.SetNumber("1")
    trap.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    ls = pcbnew.LSET()
    ls.AddLayer(pcbnew.F_Cu)
    trap.SetLayerSet(ls)
    trap.SetShape(pcbnew.PAD_SHAPE_TRAPEZOID)
    trap.SetSize(pcbnew.VECTOR2I(2_000_000, 1_000_000))
    trap.SetDelta(pcbnew.VECTOR2I(400_000, 0))
    trap.SetOrientationDegrees(30.0)
    trap.SetPosition(fp.GetPosition())
    trap.SetNetCode(0)
    fp.Add(trap)
    save(native_board, board)

    data, plan, _ = _route_and_verify_drc(native_board, tmp_path)
    assert plan["added_tracks"]
    obs = next(p for p in data["pads"] if p["net"] == 0)
    assert obs["shape"]["kind"] == "convex_polygon"
    verts = obs["shape"]["vertices"]
    # Verify exact KiCad effective shape orientation (not reflected -30 deg).
    assert any(x > 10_900_000 and y < 17_900_000 for x, y in verts)
    assert not any(x > 10_900_000 and y > 18_200_000 for x, y in verts)


def _add_poly_shape(parent, layer, net, filled, width, outer, holes=()):
    shape = pcbnew.PCB_SHAPE(parent)
    shape.SetLayer(layer)
    shape.SetShape(pcbnew.SHAPE_T_POLY)
    shape.SetFilled(filled)
    shape.SetWidth(width)
    if net is not None:
        shape.SetNetCode(net)
    poly_set = pcbnew.SHAPE_POLY_SET()
    out_idx = poly_set.NewOutline()
    for pt in outer:
        poly_set.Append(pt[0], pt[1], out_idx)
    for hole in holes:
        hole_idx = poly_set.NewHole(out_idx)
        for pt in hole:
            poly_set.Append(pt[0], pt[1], out_idx, hole_idx)
    shape.SetPolyShape(poly_set)
    parent.Add(shape)
    return shape


def test_native_saved_reloaded_holed_gr_poly_and_overlapping_unotch_merge(
    native_board, tmp_path
):
    board = pcbnew.LoadBoard(str(native_board))
    for track in list(board.GetTracks()):
        board.Remove(track)
    # 2-hole filled F.Cu gr_poly covering J1=(3M, 10M) up to x=15.5M.
    _add_poly_shape(
        board,
        pcbnew.F_Cu,
        1,
        True,
        0,
        [(2_000_000, 4_000_000), (15_500_000, 4_000_000),
         (15_500_000, 16_000_000), (2_000_000, 16_000_000)],
        holes=[
            [(6_000_000, 5_000_000), (8_000_000, 5_000_000),
             (8_000_000, 9_000_000), (6_000_000, 9_000_000)],
            [(10_000_000, 5_000_000), (12_000_000, 5_000_000),
             (12_000_000, 14_000_000), (10_000_000, 14_000_000)],
        ],
    )
    # Overlapping U-notch 1-hole filled F.Cu gr_poly covering J2=(17M, 10M).
    _add_poly_shape(
        board,
        pcbnew.F_Cu,
        1,
        True,
        0,
        [(15_000_000, 4_000_000), (18_000_000, 4_000_000),
         (18_000_000, 16_000_000), (15_000_000, 16_000_000),
         (15_000_000, 13_000_000), (16_000_000, 13_000_000),
         (16_000_000, 7_000_000), (15_000_000, 7_000_000)],
        holes=[
            [(16_500_000, 4_500_000), (17_500_000, 4_500_000),
             (17_500_000, 6_000_000), (16_500_000, 6_000_000)],
        ],
    )
    save(native_board, board)

    # Confirm KiCad SaveBoard/LoadBoard fractured the holes before export.
    reloaded = pcbnew.LoadBoard(str(native_board))
    fcu_polys = [
        d for d in reloaded.GetDrawings()
        if d.GetLayer() == pcbnew.F_Cu and d.GetShape() == pcbnew.SHAPE_T_POLY
    ]
    assert len(fcu_polys) == 2
    assert all(d.GetPolyShape().HoleCount(0) == 0 for d in fcu_polys)

    data = bridge.export_board(native_board, RULES, 100000)
    hole_counts = sorted(len(g["shape"].get("holes", [])) for g in data["graphics"])
    assert hole_counts == [1, 2]
    assert all(g["shape"]["filled"] is True for g in data["graphics"])

    request, plan = execute(native_board, data, tmp_path)
    assert plan["added_tracks"] == []
    output = tmp_path / "holed-merge.kicad_pcb"
    bridge.apply_plan(native_board, plan, output, expected_request=request.read_bytes())
    after = run_native_drc(output, sidecar_source=native_board).raw
    assert after["unconnected_items"] == []
    assert after["violations"] == []


def test_native_saved_reloaded_holed_fp_poly_and_multi_outline_route_and_drc(
    native_board, tmp_path
):
    board = pcbnew.LoadBoard(str(native_board))
    for track in list(board.GetTracks()):
        board.Remove(track)
    ds = board.GetDesignSettings()
    ds.m_SolderMaskMinWidth = 200_000
    ds.m_SolderMaskToCopperClearance = 100_000

    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("PO_HOLE")
    fp.Reference().SetVisible(False)
    fp.SetPosition(pcbnew.VECTOR2I(6_500_000, 10_000_000))
    board.Add(fp)
    _add_poly_shape(
        fp,
        pcbnew.F_Cu,
        None,
        True,
        0,
        [(5_500_000, 6_000_000), (7_500_000, 6_000_000),
         (7_500_000, 14_000_000), (5_500_000, 14_000_000)],
        holes=[
            [(6_000_000, 7_000_000), (7_000_000, 7_000_000),
             (7_000_000, 13_000_000), (6_000_000, 13_000_000)],
        ],
    )
    # Pinched filled gr_poly that Simplify() splits into 2 disjoint outlines.
    _add_poly_shape(
        board,
        pcbnew.F_Cu,
        0,
        True,
        0,
        [(12_500_000, 6_000_000), (13_200_000, 6_000_000),
         (13_200_000, 8_000_000), (13_800_000, 8_000_000),
         (13_800_000, 6_000_000), (14_500_000, 6_000_000),
         (14_500_000, 10_000_000), (13_800_000, 10_000_000),
         (13_800_000, 8_000_000), (13_200_000, 8_000_000),
         (13_200_000, 10_000_000), (12_500_000, 10_000_000)],
    )
    # F.Mask holed polygon aperture away from copper paths.
    _add_poly_shape(
        board,
        pcbnew.F_Mask,
        None,
        True,
        0,
        [(12_500_000, 14_000_000), (15_500_000, 14_000_000),
         (15_500_000, 18_000_000), (12_500_000, 18_000_000)],
        holes=[
            [(13_000_000, 15_000_000), (15_000_000, 15_000_000),
             (15_000_000, 17_000_000), (13_000_000, 17_000_000)],
        ],
    )
    save(native_board, board)

    data, plan, output = _route_and_verify_drc(native_board, tmp_path)
    assert plan["added_tracks"]
    assert len(data["graphics"]) == 3  # 1 fp_poly + 2 split outlines
    assert sorted(len(g["shape"].get("holes", [])) for g in data["graphics"]) == [0, 0, 1]
    assert len(data["mask_apertures"]) == 1
    assert len(data["mask_apertures"][0]["shape"]["holes"]) == 1
    after = run_native_drc(output, sidecar_source=native_board).raw
    assert after["unconnected_items"] == []
    assert after["violations"] == []


def test_native_unfilled_poly_positive_control_routes_and_drc(native_board, tmp_path):
    board = pcbnew.LoadBoard(str(native_board))
    for track in list(board.GetTracks()):
        board.Remove(track)
    _add_poly_shape(
        board,
        pcbnew.F_Cu,
        0,
        False,
        200_000,
        [(12_500_000, 6_000_000), (14_500_000, 6_000_000),
         (14_500_000, 10_000_000), (12_500_000, 10_000_000)],
    )
    save(native_board, board)
    data, plan, output = _route_and_verify_drc(native_board, tmp_path)
    assert plan["added_tracks"]
    assert len(data["graphics"]) == 1
    assert data["graphics"][0]["shape"]["filled"] is False
    assert "holes" not in data["graphics"][0]["shape"]
    assert len(data["graphics"][0]["shape"]["outer"]) == 4
    pos_drc = run_native_drc(output, sidecar_source=native_board).raw
    assert pos_drc["unconnected_items"] == []
    assert pos_drc["violations"] == []


def test_native_unfilled_figure8_poly_negative_control_is_not_unfractured(
    native_board, tmp_path
):
    board_neg = pcbnew.LoadBoard(str(native_board))
    _add_poly_shape(
        board_neg,
        pcbnew.F_Cu,
        0,
        False,
        200_000,
        [(12_500_000, 6_000_000), (14_500_000, 10_000_000),
         (14_500_000, 6_000_000), (12_500_000, 10_000_000)],
    )
    save(native_board, board_neg)
    neg_data = bridge.export_board(native_board, RULES, 100000)
    assert len(neg_data["graphics"]) == 1
    assert neg_data["graphics"][0]["shape"]["filled"] is False
    req, res = tmp_path / "neg-req.json", tmp_path / "neg-res.json"
    bridge.write_request(neg_data, req)
    done = subprocess.run(
        [
            "cargo", "run", "--locked", "--offline", "--manifest-path",
            str(ROOT / "rust-engine/Cargo.toml"), "--", str(req), str(res),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 2
    assert json.loads(res.read_text())["status"] == "invalid"


def test_native_rotated_holed_fp_poly_35deg_and_angles_route_and_drc(
    native_board, tmp_path
):
    board = pcbnew.LoadBoard(str(native_board))
    for track in list(board.GetTracks()):
        board.Remove(track)
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("F0")
    fp.Reference().SetVisible(False)
    fp.SetPosition(pcbnew.VECTOR2I(10_000_000, 10_000_000))
    _add_poly_shape(
        fp,
        pcbnew.F_Cu,
        None,
        True,
        0,
        [(8_000_000, 8_000_000), (12_000_000, 8_000_000),
         (12_000_000, 12_000_000), (8_000_000, 12_000_000)],
        holes=[
            [(9_000_000, 9_000_000), (11_000_000, 9_000_000),
             (11_000_000, 11_000_000), (9_000_000, 11_000_000)],
        ],
    )
    fp.SetOrientationDegrees(35.0)
    board.Add(fp)
    save(native_board, board)

    reloaded = pcbnew.LoadBoard(str(native_board))
    fp_polys = [
        d for f in reloaded.GetFootprints() for d in f.GraphicalItems()
        if d.GetShape() == pcbnew.SHAPE_T_POLY
    ]
    assert len(fp_polys) == 1
    assert fp_polys[0].GetPolyShape().HoleCount(0) == 0

    data, plan, output = _route_and_verify_drc(native_board, tmp_path)
    assert plan["added_tracks"]
    assert len(data["graphics"]) == 1
    assert data["graphics"][0]["shape"]["filled"] is True
    assert len(data["graphics"][0]["shape"]["holes"]) == 1
    after = run_native_drc(output, sidecar_source=native_board).raw
    assert after["unconnected_items"] == []
    assert after["violations"] == []

    for deg in (40, 60, 70):
        b_ang = pcbnew.LoadBoard(str(native_board))
        next(f for f in b_ang.GetFootprints() if f.GetReference() == "F0").SetOrientationDegrees(float(deg))
        save(native_board, b_ang)
        ang_data = bridge.export_board(native_board, RULES, 100000)
        assert len(ang_data["graphics"]) == 1, deg
        assert len(ang_data["graphics"][0]["shape"].get("holes", [])) == 1, deg
        run_dir = tmp_path / f"run{deg}"
        run_dir.mkdir()
        _, ang_plan = execute(native_board, ang_data, run_dir)
        assert ang_plan["added_tracks"], deg


def test_native_unfractured_shape_poly_set_preserves_holes_and_islands():
    board = pcbnew.CreateEmptyBoard()
    shape = pcbnew.PCB_SHAPE(board)
    shape.SetLayer(pcbnew.F_Cu)
    shape.SetShape(pcbnew.SHAPE_T_POLY)
    shape.SetFilled(True)
    shape.SetWidth(0)
    poly_set = shape.GetPolyShape()
    out0 = poly_set.NewOutline()
    for pt in [(4_000_000, 4_000_000), (16_000_000, 4_000_000),
               (16_000_000, 16_000_000), (4_000_000, 16_000_000)]:
        poly_set.Append(pt[0], pt[1], out0)
    hole0 = poly_set.NewHole(out0)
    for pt in [(6_000_000, 6_000_000), (14_000_000, 6_000_000),
               (14_000_000, 14_000_000), (6_000_000, 14_000_000)]:
        poly_set.Append(pt[0], pt[1], out0, hole0)
    assert shape.GetPolyShape().OutlineCount() == 1
    assert shape.GetPolyShape().HoleCount(0) == 1

    base_id = str(uuid.uuid4())
    extracted = bridge._extract_graphic_shapes(shape, base_id=base_id)
    assert len(extracted) == 1
    assert len(extracted[0][1]["holes"]) == 1
    assert shape.GetPolyShape().OutlineCount() == 1
    assert shape.GetPolyShape().HoleCount(0) == 1

    out1 = poly_set.NewOutline()
    for pt in [(8_000_000, 8_000_000), (12_000_000, 8_000_000),
               (12_000_000, 12_000_000), (8_000_000, 12_000_000)]:
        poly_set.Append(pt[0], pt[1], out1)
    extracted_island = bridge._extract_graphic_shapes(shape, base_id=base_id)
    assert sorted(len(s.get("holes", [])) for _, s in extracted_island) == [0, 1]
