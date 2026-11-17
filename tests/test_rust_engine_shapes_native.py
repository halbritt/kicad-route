"""Native rounded-pad copper admission and real executable/native outcomes."""

from collections import Counter
import hashlib
import json
import re
import subprocess
import uuid

import pcbnew
import pytest

from test_rust_engine_native import (
    ROOT,
    RULES,
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
    after = bridge.native_violations(output, tmp_path / "after.json")

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
        "rotation",
        "odd_x",
        "odd_y",
        "zero",
        "negative",
        "drill",
        "offset",
        "clearance",
        "back",
    ],
)
def test_native_rounded_unsupported_and_malformed_are_not_coerced(
    native_board, kind, feature
):
    board = pcbnew.LoadBoard(str(native_board))
    pad = next(iter(board.GetPads()))
    rounded(pad, kind)
    if feature == "rotation":
        pad.SetOrientationDegrees(45)
    elif feature == "odd_x":
        pad.SetSize(pcbnew.VECTOR2I(2000001, 1000000))
    elif feature == "odd_y":
        pad.SetSize(pcbnew.VECTOR2I(2000000, 1000001))
    elif feature == "zero":
        pad.SetSize(pcbnew.VECTOR2I(0, 1000000))
    elif feature == "negative":
        pad.SetSize(pcbnew.VECTOR2I(-2000000, 1000000))
    elif feature == "drill":
        pad.SetDrillSize(pcbnew.VECTOR2I(100000, 100000))
    elif feature == "offset":
        pad.SetOffset(pcbnew.VECTOR2I(1000, 0))
    elif feature == "clearance":
        pad.SetLocalClearance(2000000)
    elif feature == "back":
        layers = pcbnew.LSET()
        layers.AddLayer(pcbnew.B_Cu)
        pad.SetLayerSet(layers)
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


def test_custom_pad_still_rejected(native_board):
    board = pcbnew.LoadBoard(str(native_board))
    pad = next(iter(board.GetPads()))
    pad.SetShape(pcbnew.PAD_SHAPE_CUSTOM)
    save(native_board, board)
    with pytest.raises(bridge.UnsupportedBoard):
        bridge.export_board(native_board, RULES, 100000)


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
    after = bridge.native_violations(output, tmp_path / "overlap-after.json")
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


@pytest.mark.parametrize("size", [(1000001, 1000001), (1000000, 1000002)])
def test_odd_or_nonsquare_circle_pad_is_rejected(native_board, size):
    board = pcbnew.LoadBoard(str(native_board))
    pad = next(iter(board.GetPads()))
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetSize(pcbnew.VECTOR2I(*size))
    save(native_board, board)
    with pytest.raises(bridge.UnsupportedBoard, match="circular pads"):
        bridge.export_board(native_board, RULES, 100000)


def test_effective_shape_compound_none_fails_closed(native_board, monkeypatch):
    board = pcbnew.LoadBoard(str(native_board))
    for pad in board.GetPads():
        rounded(pad, pcbnew.PAD_SHAPE_ROUNDRECT)
    save(native_board, board)
    monkeypatch.setattr(bridge.pcbnew, "Cast_to_SHAPE_COMPOUND", lambda _raw: None)
    with pytest.raises(bridge.UnsupportedBoard, match="effective shape"):
        bridge.export_board(native_board, RULES, 100000)
