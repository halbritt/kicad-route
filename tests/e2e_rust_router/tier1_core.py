"""Tier 1 isolated happy-path E2E tests for Features 1-6 (31 tests)."""

from __future__ import annotations

from typing import Any

from tests.e2e_rust_router.core_helpers import (
    STD_RULES,
    assert_clean_e2e,
    assert_no_crash,
    build_input1_json,
    build_std_2pad_board,
    run_m3_board_or_pending,
    run_wire_or_pending,
    snapshot_source_dir,
    write_json_file,
)
from tests.e2e_rust_router.drivers import sha256_bytes, sha256_file
from tests.e2e_rust_router.kicad_verify import (
    BoardBuilder,
    _pcbnew,
    read_native_connectivity,
    run_native_drc,
    verify_dru_loaded,
)
from tests.e2e_rust_router.runner import DriverContext, PendingCapability, register_test


# ============================================================================
# Feature 1 — Lane 1: M7 Multi-Terminal (T1-F1-01 .. T1-F1-06)
# ============================================================================


@register_test(
    test_id="T1-F1-01",
    tier=1,
    feature="F1",
    min_stage="M1",
    name="Three-terminal single-net daisy/branch route",
)
def t1_f1_01(ctx: DriverContext) -> dict[str, Any]:
    b = BoardBuilder(seed=101)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    for idx, pos in enumerate([(3_000_000, 10_000_000), (10_000_000, 10_000_000), (17_000_000, 5_000_000)], 1):
        fp = b.add_footprint(f"J{idx}", pos)
        b.add_pad(fp, "1", "SIGNAL", pos, size_nm=(1_000_000, 1_000_000))
    b.add_track("OBSTACLE", (14_000_000, 2_000_000), (14_000_000, 14_000_000), 250_000, locked=True)
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)

    res, before_hashes = run_wire_or_pending(ctx, bundle.pcb_path, out_pcb, STD_RULES)
    assert res.stage_failed is None, f"route failed at {res.stage_failed}"
    assert res.engine_res is not None and res.engine_res.output_json is not None
    plan = res.engine_res.output_json
    assert len(plan["added_tracks"]) > 0
    assert res.export_res.output_bytes is not None
    metrics = assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        plan_json=plan,
        request_bytes=res.export_res.output_bytes,
    )
    conn = read_native_connectivity(out_pcb)
    assert len(conn.pad_partitions_by_net["SIGNAL"]) == 1
    assert len(conn.pad_partitions_by_net["SIGNAL"][0]) == 3
    return metrics


@register_test(
    test_id="T1-F1-02",
    tier=1,
    feature="F1",
    min_stage="M1",
    name="Six-terminal star/hub connectivity (>=3 branches)",
)
def t1_f1_02(ctx: DriverContext) -> dict[str, Any]:
    b = BoardBuilder(seed=102)
    b.set_rect_outline(0, 0, 30_000_000, 30_000_000)
    coords = [
        (15_000_000, 15_000_000),
        (5_000_000, 15_000_000),
        (25_000_000, 15_000_000),
        (15_000_000, 5_000_000),
        (15_000_000, 25_000_000),
        (25_000_000, 25_000_000),
    ]
    for idx, pos in enumerate(coords, 1):
        fp = b.add_footprint(f"J{idx}", pos)
        b.add_pad(fp, "1", "SIGNAL", pos, size_nm=(1_000_000, 1_000_000))
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)

    res, before_hashes = run_wire_or_pending(ctx, bundle.pcb_path, out_pcb, STD_RULES)
    assert res.stage_failed is None, f"route failed at {res.stage_failed}"
    assert res.engine_res is not None and res.engine_res.output_json is not None
    assert res.export_res.output_bytes is not None
    metrics = assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        plan_json=res.engine_res.output_json,
        request_bytes=res.export_res.output_bytes,
    )
    conn = read_native_connectivity(out_pcb)
    assert len(conn.pad_partitions_by_net["SIGNAL"]) == 1
    assert len(conn.pad_partitions_by_net["SIGNAL"][0]) == 6
    return metrics


@register_test(
    test_id="T1-F1-03",
    tier=1,
    feature="F1",
    min_stage="M1",
    name="Mixed preconnected pad pairs + disconnected track component",
)
def t1_f1_03(ctx: DriverContext) -> dict[str, Any]:
    b = BoardBuilder(seed=103)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    coords = [(3_000_000, 16_000_000), (7_000_000, 16_000_000), (13_000_000, 16_000_000), (17_000_000, 16_000_000)]
    for idx, pos in enumerate(coords, 1):
        fp = b.add_footprint(f"J{idx}", pos)
        b.add_pad(fp, "1", "SIGNAL", pos, size_nm=(1_000_000, 1_000_000))
    b.add_track("SIGNAL", (3_000_000, 16_000_000), (7_000_000, 16_000_000), 250_000, locked=True)
    b.add_track("SIGNAL", (13_000_000, 16_000_000), (17_000_000, 16_000_000), 250_000, locked=True)
    iso = b.add_track("SIGNAL", (10_000_000, 4_000_000), (10_000_000, 8_000_000), 250_000, locked=True)
    iso_uuid = iso.m_Uuid.AsString()
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)

    res, before_hashes = run_wire_or_pending(ctx, bundle.pcb_path, out_pcb, STD_RULES)
    assert res.stage_failed is None, f"route failed at {res.stage_failed}"
    assert res.engine_res is not None and res.engine_res.output_json is not None
    assert res.export_res.output_bytes is not None
    metrics = assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        plan_json=res.engine_res.output_json,
        request_bytes=res.export_res.output_bytes,
    )
    pn = _pcbnew()
    routed_board = pn.LoadBoard(str(out_pcb))
    assert routed_board.BuildConnectivity()
    first_pad = next(p for p in routed_board.GetPads() if p.GetNetname() == "SIGNAL")
    connected_ids = {it.m_Uuid.AsString() for it in routed_board.GetConnectivity().GetConnectedItems(first_pad, 0)}
    assert iso_uuid in connected_ids, "isolated incumbent track not unified into SIGNAL component"
    return metrics


@register_test(
    test_id="T1-F1-04",
    tier=1,
    feature="F1",
    min_stage="M1",
    name="Already-connected 6-terminal incumbent tree (idempotent)",
)
def t1_f1_04(ctx: DriverContext) -> dict[str, Any]:
    b = BoardBuilder(seed=104)
    b.set_rect_outline(0, 0, 24_000_000, 20_000_000)
    coords = [
        (3_000_000, 4_000_000),
        (12_000_000, 4_000_000),
        (21_000_000, 4_000_000),
        (3_000_000, 16_000_000),
        (12_000_000, 16_000_000),
        (21_000_000, 16_000_000),
    ]
    for idx, pos in enumerate(coords, 1):
        fp = b.add_footprint(f"J{idx}", pos)
        b.add_pad(fp, "1", "SIGNAL", pos, size_nm=(1_000_000, 1_000_000))
    b.add_track("SIGNAL", (3_000_000, 4_000_000), (21_000_000, 4_000_000), 250_000, locked=True)
    b.add_track("SIGNAL", (3_000_000, 16_000_000), (21_000_000, 16_000_000), 250_000, locked=True)
    b.add_track("SIGNAL", (12_000_000, 4_000_000), (12_000_000, 16_000_000), 250_000, locked=True)
    bundle = b.save(ctx.work_dir / "src")
    assert read_native_connectivity(bundle.pcb_path).unconnected_count == 0

    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    res, before_hashes = run_wire_or_pending(ctx, bundle.pcb_path, out_pcb, STD_RULES)
    assert res.stage_failed is None, f"route failed at {res.stage_failed}"
    assert res.engine_res is not None and res.engine_res.output_json is not None
    plan = res.engine_res.output_json
    assert plan["added_tracks"] == []
    assert plan["removed_track_ids"] == []
    assert plan["expanded_states"] == 0
    assert res.export_res.output_bytes is not None
    return assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        plan_json=plan,
        request_bytes=res.export_res.output_bytes,
    )


@register_test(
    test_id="T1-F1-05",
    tier=1,
    feature="F1",
    min_stage="M1",
    name="Multi-terminal first-contact shadowed center + nearest target",
)
def t1_f1_05(ctx: DriverContext) -> dict[str, Any]:
    rules = {
        "width_nm": 1_500_000,
        "clearance_nm": 1_000_000,
        "grid_nm": 2_500_000,
        "edge_clearance_nm": 1_000_000,
    }
    b = BoardBuilder(seed=105)
    b.set_rect_outline(0, 0, 50_000_000, 50_000_000)
    for idx, pos in enumerate([(25_000_000, 10_000_000), (45_000_000, 10_000_000), (5_000_000, 10_000_000)], 1):
        fp = b.add_footprint(f"J{idx}", pos)
        b.add_pad(fp, "1", "SIGNAL", pos, size_nm=(3_000_000, 3_000_000))
    b.add_track("SIGNAL", (10_000_000, 30_000_000), (40_000_000, 30_000_000), 500_000, locked=True)
    b.add_track("OBSTACLE", (10_000_000, 31_500_000), (40_000_000, 31_500_000), 500_000, locked=True)
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)

    res, before_hashes = run_wire_or_pending(ctx, bundle.pcb_path, out_pcb, rules)
    assert res.stage_failed is None, f"route failed at {res.stage_failed}"
    assert res.engine_res is not None and res.engine_res.output_json is not None
    plan = res.engine_res.output_json
    assert any(
        29_000_001 <= ep[1] <= 29_500_000
        for tr in plan["added_tracks"]
        for ep in (tr["start"], tr["end"])
    ), f"expected first-contact endpoint in [29000001..29500000], got {plan['added_tracks']}"
    assert res.export_res.output_bytes is not None
    return assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        plan_json=plan,
        request_bytes=res.export_res.output_bytes,
    )


@register_test(
    test_id="T1-F1-06",
    tier=1,
    feature="F1",
    min_stage="M1",
    name="Multi-net multi-terminal deterministic repeat (2x 3-pad nets)",
)
def t1_f1_06(ctx: DriverContext) -> dict[str, Any]:
    b = BoardBuilder(seed=106)
    b.set_rect_outline(0, 0, 24_000_000, 24_000_000)
    for idx, pos in enumerate([(3_000_000, 6_000_000), (12_000_000, 6_000_000), (21_000_000, 6_000_000)], 1):
        fp = b.add_footprint(f"A{idx}", pos)
        b.add_pad(fp, "1", "SIG1", pos, size_nm=(1_000_000, 1_000_000))
    for idx, pos in enumerate([(3_000_000, 18_000_000), (12_000_000, 18_000_000), (21_000_000, 18_000_000)], 1):
        fp = b.add_footprint(f"B{idx}", pos)
        b.add_pad(fp, "1", "SIG2", pos, size_nm=(1_000_000, 1_000_000))
    b.add_track("OBSTACLE", (12_000_000, 10_000_000), (12_000_000, 14_000_000), 250_000, locked=True)
    bundle = b.save(ctx.work_dir / "src")
    before_hashes = snapshot_source_dir(bundle.directory)
    rules_path = write_json_file(ctx.work_dir / "rules.json", STD_RULES)
    req_path = ctx.work_dir / "request.json"
    plan_a = ctx.work_dir / "plan_a.json"
    plan_b = ctx.work_dir / "plan_b.json"

    exp = ctx.json_driver.export_board(bundle.pcb_path, req_path, rules_path)
    assert_no_crash(exp.proc, "export")
    if exp.proc.returncode == 2 and (exp.stderr_json or {}).get("status") == "unsupported":
        raise PendingCapability(f"export unsupported: {exp.stderr_json}")
    assert exp.proc.returncode == 0 and exp.output_bytes is not None

    eng_a = ctx.json_driver.run_engine(req_path, plan_a)
    assert_no_crash(eng_a.proc, "engine_a")
    if eng_a.proc.returncode == 2 and (eng_a.output_json or {}).get("status") == "unsupported":
        raise PendingCapability(f"engine unsupported: {eng_a.output_json}")
    eng_b = ctx.json_driver.run_engine(req_path, plan_b)
    assert_no_crash(eng_b.proc, "engine_b")

    assert eng_a.proc.returncode == 0 and eng_b.proc.returncode == 0
    assert eng_a.output_bytes == eng_b.output_bytes, "identical inputs produced non-deterministic plans"

    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    app = ctx.json_driver.apply_plan(bundle.pcb_path, plan_a, out_pcb, req_path)
    assert_no_crash(app.proc, "apply")
    assert app.proc.returncode == 0, f"apply failed: {app.proc.stderr}"
    return assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        plan_json=eng_a.output_json,
        request_bytes=exp.output_bytes,
    )


# ============================================================================
# Feature 2 — Lane 3: Native Oval/RoundRect (T1-F2-01 .. T1-F2-05)
# ============================================================================


@register_test(
    test_id="T1-F2-01",
    tier=1,
    feature="F2",
    min_stage="M1",
    name="Oval SMD pads at 0, 90, 180 deg routing and preservation",
)
def t1_f2_01(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    for angle in (0.0, 90.0, 180.0):
        sub = ctx.work_dir / f"ang_{int(angle)}"
        b = BoardBuilder(seed=201 + int(angle))
        b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
        for idx, x in enumerate((3_000_000, 17_000_000), 1):
            fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
            b.add_pad(
                fp,
                "1",
                "SIGNAL",
                (x, 10_000_000),
                shape=pn.PAD_SHAPE_OVAL,
                size_nm=(2_000_000, 1_000_000),
                angle_deg=angle,
            )
        b.add_track("OBSTACLE", (10_000_000, 4_000_000), (10_000_000, 16_000_000), 250_000, locked=True)
        bundle = b.save(sub / "src")
        out_pcb = sub / "out" / "routed.kicad_pcb"
        out_pcb.parent.mkdir(parents=True, exist_ok=True)
        res, before_hashes = run_wire_or_pending(
            ctx, bundle.pcb_path, out_pcb, STD_RULES, tag=f"ang_{int(angle)}"
        )
        assert res.stage_failed is None, f"oval {angle} failed at {res.stage_failed}"
        assert res.export_res.output_json is not None
        expected_half = [500_000, 1_000_000] if angle == 90.0 else [1_000_000, 500_000]
        for p in res.export_res.output_json["pads"]:
            assert p["shape"] == {
                "kind": "round_rect",
                "half_size_nm": expected_half,
                "radius_nm": 500_000,
            }
        assert res.engine_res is not None and res.engine_res.output_json is not None
        assert res.export_res.output_bytes is not None
        assert_clean_e2e(
            bundle.pcb_path,
            out_pcb,
            before_hashes=before_hashes,
            source_dir=bundle.directory,
            plan_json=res.engine_res.output_json,
            request_bytes=res.export_res.output_bytes,
        )
    return {"angles_verified": [0, 90, 180]}


@register_test(
    test_id="T1-F2-02",
    tier=1,
    feature="F2",
    min_stage="M1",
    name="RoundRect SMD pads (ratio 0.25, 0.0, 0.5) at 0/90/270 deg",
)
def t1_f2_02(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    cases = [(0.0, 0.25), (90.0, 0.25), (270.0, 0.25), (0.0, 0.0), (90.0, 0.5)]
    for idx_case, (angle, ratio) in enumerate(cases):
        sub = ctx.work_dir / f"case_{idx_case}"
        b = BoardBuilder(seed=210 + idx_case)
        b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
        for idx, x in enumerate((3_000_000, 17_000_000), 1):
            fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
            b.add_pad(
                fp,
                "1",
                "SIGNAL",
                (x, 10_000_000),
                shape=pn.PAD_SHAPE_ROUNDRECT,
                size_nm=(2_000_000, 1_000_000),
                angle_deg=angle,
                roundrect_ratio=ratio,
            )
        b.add_track("OBSTACLE", (10_000_000, 4_000_000), (10_000_000, 16_000_000), 250_000, locked=True)
        bundle = b.save(sub / "src")
        out_pcb = sub / "out" / "routed.kicad_pcb"
        out_pcb.parent.mkdir(parents=True, exist_ok=True)
        res, before_hashes = run_wire_or_pending(
            ctx, bundle.pcb_path, out_pcb, STD_RULES, tag=f"rr_{idx_case}"
        )
        assert res.stage_failed is None, f"roundrect {(angle, ratio)} failed at {res.stage_failed}"
        assert res.export_res.output_json is not None
        expected_half = [500_000, 1_000_000] if angle in (90.0, 270.0) else [1_000_000, 500_000]
        expected_r = int(round(1_000_000 * ratio))
        for p in res.export_res.output_json["pads"]:
            assert p["shape"] == {
                "kind": "round_rect",
                "half_size_nm": expected_half,
                "radius_nm": expected_r,
            }
        assert res.engine_res is not None and res.engine_res.output_json is not None
        assert res.export_res.output_bytes is not None
        assert_clean_e2e(
            bundle.pcb_path,
            out_pcb,
            before_hashes=before_hashes,
            source_dir=bundle.directory,
            plan_json=res.engine_res.output_json,
            request_bytes=res.export_res.output_bytes,
        )
    return {"cases_verified": len(cases)}


@register_test(
    test_id="T1-F2-03",
    tier=1,
    feature="F2",
    min_stage="M1",
    name="Near-circle collapse boundary (1.000198 mm vs 1.000200 mm, ratio=0.5)",
)
def t1_f2_03(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    cases = [
        ((1_000_000, 1_000_000), 0.5, {"kind": "circle", "radius_nm": 500_000}),
        ((1_000_002, 1_000_000), 0.5, {"kind": "circle", "radius_nm": 500_000}),
        ((1_000_198, 1_000_000), 0.5, {"kind": "circle", "radius_nm": 500_000}),
        (
            (1_000_200, 1_000_000),
            0.5,
            {"kind": "round_rect", "half_size_nm": [500_100, 500_000], "radius_nm": 500_000},
        ),
        (
            (1_000_000, 1_000_000),
            0.0,
            {"kind": "round_rect", "half_size_nm": [500_000, 500_000], "radius_nm": 0},
        ),
    ]
    for idx_case, (size_nm, ratio, expected_shape) in enumerate(cases):
        sub = ctx.work_dir / f"collapse_{idx_case}"
        b = BoardBuilder(seed=220 + idx_case)
        b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
        for idx, x in enumerate((3_000_000, 17_000_000), 1):
            fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
            b.add_pad(
                fp,
                "1",
                "SIGNAL",
                (x, 10_000_000),
                shape=pn.PAD_SHAPE_ROUNDRECT,
                size_nm=size_nm,
                roundrect_ratio=ratio,
            )
        b.add_track("OBSTACLE", (10_000_000, 4_000_000), (10_000_000, 16_000_000), 250_000, locked=True)
        bundle = b.save(sub / "src")
        out_pcb = sub / "out" / "routed.kicad_pcb"
        out_pcb.parent.mkdir(parents=True, exist_ok=True)
        res, before_hashes = run_wire_or_pending(
            ctx, bundle.pcb_path, out_pcb, STD_RULES, tag=f"collapse_{idx_case}"
        )
        assert res.stage_failed is None, f"collapse case {idx_case} failed at {res.stage_failed}"
        assert res.export_res.output_json is not None
        for p in res.export_res.output_json["pads"]:
            assert p["shape"] == expected_shape, f"got {p['shape']}, expected {expected_shape}"
        assert res.engine_res is not None and res.engine_res.output_json is not None
        assert res.export_res.output_bytes is not None
        assert_clean_e2e(
            bundle.pcb_path,
            out_pcb,
            before_hashes=before_hashes,
            source_dir=bundle.directory,
            plan_json=res.engine_res.output_json,
            request_bytes=res.export_res.output_bytes,
        )
    return {"cases_verified": len(cases)}


@register_test(
    test_id="T1-F2-04",
    tier=1,
    feature="F2",
    min_stage="M1",
    name="Foreign net 0 rounded pad obstacle avoidance",
)
def t1_f2_04(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(seed=230)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    for idx, x in enumerate((3_000_000, 17_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
        b.add_pad(
            fp,
            "1",
            "SIGNAL",
            (x, 10_000_000),
            shape=pn.PAD_SHAPE_OVAL,
            size_nm=(2_000_000, 1_000_000),
        )
    ob = b.add_footprint("OB1", (10_000_000, 10_000_000))
    b.add_pad(
        ob,
        "1",
        "",
        (10_000_000, 10_000_000),
        shape=pn.PAD_SHAPE_ROUNDRECT,
        size_nm=(4_000_000, 6_000_000),
        roundrect_ratio=0.25,
    )
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    res, before_hashes = run_wire_or_pending(ctx, bundle.pcb_path, out_pcb, STD_RULES)
    assert res.stage_failed is None, f"route failed at {res.stage_failed}"
    assert res.engine_res is not None and res.engine_res.output_json is not None
    assert res.export_res.output_bytes is not None
    return assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        plan_json=res.engine_res.output_json,
        request_bytes=res.export_res.output_bytes,
    )


@register_test(
    test_id="T1-F2-05",
    tier=1,
    feature="F2",
    min_stage="M1",
    name="Round-pad 1 nm overlap incumbent reuse vs 0 nm tangency repair",
)
def t1_f2_05(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    for shape in (pn.PAD_SHAPE_OVAL, pn.PAD_SHAPE_ROUNDRECT):
        for overlap in (0, 1):
            tag = f"s{shape}_ov{overlap}"
            sub = ctx.work_dir / tag
            b = BoardBuilder(seed=240 + shape * 10 + overlap)
            b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
            for idx, x in enumerate((3_000_000, 17_000_000), 1):
                fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
                b.add_pad(
                    fp,
                    "1",
                    "SIGNAL",
                    (x, 10_000_000),
                    shape=shape,
                    size_nm=(2_000_000, 1_000_000),
                    roundrect_ratio=0.25,
                )
            pts = [
                (3_000_000, 10_000_000),
                (3_000_000, 2_000_000),
                (15_000_000, 2_000_000),
                (15_000_000, 10_000_000),
                (15_875_000 + overlap, 10_000_000),
            ]
            for a, c in zip(pts, pts[1:]):
                b.add_track("SIGNAL", a, c, 250_000, locked=True)
            bundle = b.save(sub / "src")
            before_opens = read_native_connectivity(bundle.pcb_path).unconnected_count
            assert before_opens == (1 if overlap == 0 else 0)
            out_pcb = sub / "out" / "routed.kicad_pcb"
            out_pcb.parent.mkdir(parents=True, exist_ok=True)
            res, before_hashes = run_wire_or_pending(
                ctx, bundle.pcb_path, out_pcb, STD_RULES, tag=tag
            )
            assert res.stage_failed is None, f"{tag} failed at {res.stage_failed}"
            assert res.engine_res is not None and res.engine_res.output_json is not None
            plan = res.engine_res.output_json
            if overlap == 0:
                assert len(plan["added_tracks"]) > 0
            else:
                assert plan["added_tracks"] == []
            assert res.export_res.output_bytes is not None
            assert_clean_e2e(
                bundle.pcb_path,
                out_pcb,
                before_hashes=before_hashes,
                source_dir=bundle.directory,
                plan_json=plan,
                request_bytes=res.export_res.output_bytes,
            )
    return {"checked": ["oval_0", "oval_1", "roundrect_0", "roundrect_1"]}


# ============================================================================
# Feature 3 — Lane 2: Spatial Index (T1-F3-01 .. T1-F3-05)
# ============================================================================


@register_test(
    test_id="T1-F3-01",
    tier=1,
    feature="F3",
    min_stage="M2",
    name="Sparse obstacle board routes with deterministic result and clearance",
)
def t1_f3_01(ctx: DriverContext) -> dict[str, Any]:
    b = BoardBuilder(seed=301)
    b.set_rect_outline(-100_000_000, -100_000_000, 100_000_000, 100_000_000)
    for k in range(4):
        y = -60_000_000 + 40_000_000 * k
        for suffix, x in (("A", -80_000_000), ("B", 80_000_000)):
            fp = b.add_footprint(f"J{k}_{suffix}", (x, y))
            b.add_pad(fp, "1", f"SIG{k + 1}", (x, y), size_nm=(1_000_000, 1_000_000))
        b.add_track("", (0, y - 5_000_000), (0, y + 5_000_000), 250_000, locked=True)
    # 46 additional scattered obstacle pads outside signal bands
    for i in range(46):
        ox = -90_000_000 + (i % 10) * 20_000_000
        oy = -85_000_000 + (i // 10) * 40_000_000
        fp = b.add_footprint(f"OB{i}", (ox, oy))
        b.add_pad(fp, "1", "", (ox, oy), size_nm=(500_000, 500_000))
    bundle = b.save(ctx.work_dir / "src")
    before_hashes = snapshot_source_dir(bundle.directory)
    rules_path = write_json_file(ctx.work_dir / "rules.json", {**STD_RULES, "grid_nm": 1_000_000})
    req_path = ctx.work_dir / "request.json"
    exp = ctx.json_driver.export_board(bundle.pcb_path, req_path, rules_path, max_expansions=500_000)
    assert_no_crash(exp.proc, "export")
    assert exp.proc.returncode == 0 and exp.output_bytes is not None

    plan_a = ctx.work_dir / "plan_a.json"
    plan_b = ctx.work_dir / "plan_b.json"
    eng_a = ctx.json_driver.run_engine(req_path, plan_a)
    eng_b = ctx.json_driver.run_engine(req_path, plan_b)
    assert_no_crash(eng_a.proc, "engine_a")
    assert_no_crash(eng_b.proc, "engine_b")
    assert eng_a.proc.returncode == 0 and eng_b.proc.returncode == 0
    assert eng_a.output_bytes == eng_b.output_bytes

    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    app = ctx.json_driver.apply_plan(bundle.pcb_path, plan_a, out_pcb, req_path)
    assert_no_crash(app.proc, "apply")
    assert app.proc.returncode == 0
    metrics = assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        plan_json=eng_a.output_json,
        request_bytes=exp.output_bytes,
    )
    if not ctx.stage_at_least("M2"):
        raise PendingCapability("M2 spatial index milestone pending", evidence=metrics)
    return metrics


@register_test(
    test_id="T1-F3-02",
    tier=1,
    feature="F3",
    min_stage="M2",
    name="Dense cluster neighborhood (550 items) routes legal corridor",
)
def t1_f3_02(ctx: DriverContext) -> dict[str, Any]:
    b = BoardBuilder(seed=302)
    b.set_rect_outline(0, 0, 60_000_000, 60_000_000)
    rows = [
        6_000_000 + 2_000_000 * r for r in range(11)
    ] + [28_500_000, 31_500_000] + [34_000_000 + 2_000_000 * r for r in range(9)]
    assert len(rows) == 22
    for ix in range(25):
        ox = 6_000_000 + ix * 2_000_000
        for iy, oy in enumerate(rows):
            fp = b.add_footprint(f"D_{ix}_{iy}", (ox, oy))
            b.add_pad(fp, "1", "", (ox, oy), size_nm=(500_000, 500_000))
    for idx, x in enumerate((2_000_000, 58_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 30_000_000))
        b.add_pad(fp, "1", "SIGNAL", (x, 30_000_000), size_nm=(1_000_000, 1_000_000))
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    res, before_hashes = run_wire_or_pending(ctx, bundle.pcb_path, out_pcb, STD_RULES)
    assert res.stage_failed is None, f"dense corridor failed at {res.stage_failed}"
    assert res.engine_res is not None and res.engine_res.output_json is not None
    assert res.export_res.output_bytes is not None
    metrics = assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        plan_json=res.engine_res.output_json,
        request_bytes=res.export_res.output_bytes,
    )
    if not ctx.stage_at_least("M2"):
        raise PendingCapability("M2 spatial index milestone pending", evidence=metrics)
    return metrics


@register_test(
    test_id="T1-F3-03",
    tier=1,
    feature="F3",
    min_stage="M2",
    name="RE-08 sparse and dense CPU/wall/RSS attribution benchmark",
)
def t1_f3_03(ctx: DriverContext) -> dict[str, Any]:
    from tests.e2e_rust_router.core_helpers import canonical_uuid

    def make_pads_fixture(count: int, x_start: int, cols: int) -> dict[str, Any]:
        pads = [
            {"id": canonical_uuid(1), "net": 1, "at": [-20_000_000, 0], "shape": {"kind": "circle", "radius_nm": 500_000}},
            {"id": canonical_uuid(2), "net": 1, "at": [0, 0], "shape": {"kind": "circle", "radius_nm": 500_000}},
        ]
        for i in range(count):
            col = i % cols
            row = i // cols
            pads.append({
                "id": canonical_uuid(100 + i),
                "net": 0,
                "at": [x_start + col * 2_000_000, -50_000_000 + row * 2_000_000],
                "shape": {"kind": "circle", "radius_nm": 250_000},
            })
        return build_input1_json(
            bounds_min=(-100_000_000, -100_000_000),
            bounds_max=(100_000_000, 100_000_000),
            pads=pads,
        )

    fixtures = {
        "sparse_50": make_pads_fixture(50, 20_000_000, 10),
        "dense_2000": make_pads_fixture(2000, 10_000_000, 40),
    }
    summary: dict[str, Any] = {"binary_sha256": ctx.provenance.binary_sha256}
    for label, doc in fixtures.items():
        req_path = write_json_file(ctx.work_dir / f"{label}.json", doc)
        # 1 excluded warmup + 3 measured runs
        warmup = ctx.json_driver.run_engine(req_path, ctx.work_dir / f"{label}_warmup.plan.json", metrics_name=f"{label}_w")
        assert_no_crash(warmup.proc, f"{label}:warmup")
        assert warmup.proc.returncode == 0
        runs = []
        for idx in range(3):
            res = ctx.json_driver.run_engine(req_path, ctx.work_dir / f"{label}_{idx}.plan.json", metrics_name=f"{label}_{idx}")
            assert_no_crash(res.proc, f"{label}:{idx}")
            assert res.proc.returncode == 0
            assert res.output_json is not None and res.proc.metrics is not None
            runs.append({
                "wall_s": res.proc.metrics.wall_s,
                "cpu_s": res.proc.metrics.cpu_s,
                "max_rss_kib": res.proc.metrics.max_rss_kib,
                "work_used": res.output_json["work_used"],
                "expanded_states": res.output_json["expanded_states"],
            })
        assert len({r["work_used"] for r in runs}) == 1
        assert len({r["expanded_states"] for r in runs}) == 1
        summary[label] = runs

    if not ctx.stage_at_least("M2"):
        raise PendingCapability("M2 spatial index RE-08 scaling gate pending", evidence=summary)
    sparse_work = summary["sparse_50"][0]["work_used"]
    dense_work = summary["dense_2000"][0]["work_used"]
    assert dense_work <= max(sparse_work * 8, 50_000), f"dense_2000 work_used regressed: {dense_work} vs sparse_50 {sparse_work}"
    for r in summary["dense_2000"]:
        assert r["wall_s"] < 10.0
        assert r["max_rss_kib"] < 524_288
    return summary


@register_test(
    test_id="T1-F3-04",
    tier=1,
    feature="F3",
    min_stage="M2",
    name="Arbitrary-angle diagonal track obstacles indexed accurately",
)
def t1_f3_04(ctx: DriverContext) -> dict[str, Any]:
    b = BoardBuilder(seed=304)
    b.set_rect_outline(0, 0, 40_000_000, 40_000_000)
    for k in range(10):
        b.add_track("OBSTACLE", (12_000_000 + k * 500_000, 10_000_000), (18_000_000 + k * 500_000, 28_000_000), 250_000, locked=True)
        b.add_track("OBSTACLE", (10_000_000, 12_000_000 + k * 500_000), (28_000_000, 18_000_000 + k * 500_000), 250_000, locked=True)
    for idx, x in enumerate((4_000_000, 36_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 20_000_000))
        b.add_pad(fp, "1", "SIGNAL", (x, 20_000_000), size_nm=(1_000_000, 1_000_000))
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    res, before_hashes = run_wire_or_pending(ctx, bundle.pcb_path, out_pcb, STD_RULES)
    assert res.stage_failed is None, f"diagonal obstacle route failed at {res.stage_failed}"
    assert res.engine_res is not None and res.engine_res.output_json is not None
    assert res.export_res.output_bytes is not None
    metrics = assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        plan_json=res.engine_res.output_json,
        request_bytes=res.export_res.output_bytes,
    )
    if not ctx.stage_at_least("M2"):
        raise PendingCapability("M2 spatial index milestone pending", evidence=metrics)
    return metrics


@register_test(
    test_id="T1-F3-05",
    tier=1,
    feature="F3",
    min_stage="M2",
    name="Multi-net incremental copper insertion visibility (8 nets)",
)
def t1_f3_05(ctx: DriverContext) -> dict[str, Any]:
    b = BoardBuilder(seed=305)
    b.set_rect_outline(0, 0, 40_000_000, 50_000_000)
    for i in range(1, 9):
        fp_a = b.add_footprint(f"A{i}", (4_000_000, 4_000_000 * i))
        b.add_pad(fp_a, "1", f"SIG{i}", (4_000_000, 4_000_000 * i), size_nm=(1_000_000, 1_000_000))
        fp_b = b.add_footprint(f"B{i}", (36_000_000, 4_000_000 * i + 2_000_000))
        b.add_pad(fp_b, "1", f"SIG{i}", (36_000_000, 4_000_000 * i + 2_000_000), size_nm=(1_000_000, 1_000_000))
    b.add_track("", (20_000_000, 14_000_000), (20_000_000, 26_000_000), 250_000, locked=True)
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    res, before_hashes = run_wire_or_pending(
        ctx, bundle.pcb_path, out_pcb, {**STD_RULES, "grid_nm": 500_000}, max_expansions=500_000
    )
    assert res.stage_failed is None, f"8-net route failed at {res.stage_failed}"
    assert res.engine_res is not None and res.engine_res.output_json is not None
    assert res.export_res.output_bytes is not None
    metrics = assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        plan_json=res.engine_res.output_json,
        request_bytes=res.export_res.output_bytes,
    )
    if not ctx.stage_at_least("M2"):
        raise PendingCapability("M2 spatial index milestone pending", evidence=metrics)
    return metrics


# ============================================================================
# Feature 4 — Lane 4: Native Orchestration (T1-F4-01 .. T1-F4-05)
# ============================================================================


@register_test(
    test_id="T1-F4-01",
    tier=1,
    feature="F4",
    min_stage="M2",
    name="Single-command native Rust orchestration end-to-end",
)
def t1_f4_01(ctx: DriverContext) -> dict[str, Any]:
    bundle = build_std_2pad_board(ctx.work_dir / "src", seed=401)
    before_hashes = snapshot_source_dir(bundle.directory)
    rules_path = write_json_file(ctx.work_dir / "rules.json", STD_RULES)
    out_dir = ctx.work_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_pcb = out_dir / "routed.kicad_pcb"

    proc = ctx.native_driver.run_native(
        bundle.pcb_path, out_pcb, rules_path=rules_path, max_expansions=100_000
    )
    assert_no_crash(proc, "native")
    assert proc.returncode == 0, f"native orchestration failed: {proc.stderr}"
    assert [p.name for p in out_dir.iterdir()] == ["routed.kicad_pcb"], (
        f"unexpected residue in out_dir: {list(out_dir.iterdir())}"
    )
    return assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
    )


@register_test(
    test_id="T1-F4-02",
    tier=1,
    feature="F4",
    min_stage="BASELINE",
    name="Exact request-identity and source-hash binding (input/1 -> result/3)",
)
def t1_f4_02(ctx: DriverContext) -> dict[str, Any]:
    bundle = build_std_2pad_board(ctx.work_dir / "src", seed=402)
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    res, before_hashes = run_wire_or_pending(
        ctx, bundle.pcb_path, out_pcb, STD_RULES, allow_unsupported_engine=False
    )
    assert res.stage_failed is None, f"failed at {res.stage_failed}"
    assert res.export_res.output_json is not None and res.export_res.output_bytes is not None
    assert res.engine_res is not None and res.engine_res.output_json is not None
    req = res.export_res.output_json
    plan = res.engine_res.output_json
    board_sha = sha256_file(bundle.pcb_path)
    assert req["version"] == 1
    assert req["source_id"] == board_sha
    assert plan["version"] == 3
    assert plan["source_id"] == board_sha
    assert plan["request_sha256"] == sha256_bytes(res.export_res.output_bytes)
    assert 0 < plan["expanded_states"] <= 100_000
    assert plan["work_used"] > 0
    return assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        plan_json=plan,
        request_bytes=res.export_res.output_bytes,
    )


@register_test(
    test_id="T1-F4-03",
    tier=1,
    feature="F4",
    min_stage="BASELINE",
    name="Exact 16 MiB boundary board, rules, and request admission",
)
def t1_f4_03(ctx: DriverContext) -> dict[str, Any]:
    max_bytes = 16 * 1024 * 1024
    bundle = build_std_2pad_board(ctx.work_dir / "src", seed=403)
    raw_pcb = bundle.pcb_path.read_bytes()
    bundle.pcb_path.write_bytes(raw_pcb + b" " * (max_bytes - len(raw_pcb)))
    assert bundle.pcb_path.stat().st_size == max_bytes
    before_hashes = snapshot_source_dir(bundle.directory)

    rules_path = write_json_file(ctx.work_dir / "rules.json", STD_RULES)
    raw_rules = rules_path.read_bytes()
    rules_path.write_bytes(raw_rules + b" " * (max_bytes - len(raw_rules)))
    assert rules_path.stat().st_size == max_bytes

    req_path = ctx.work_dir / "request.json"
    exp = ctx.json_driver.export_board(bundle.pcb_path, req_path, rules_path)
    assert_no_crash(exp.proc, "export_16m")
    assert exp.proc.returncode == 0 and exp.output_bytes is not None
    assert exp.output_json is not None and exp.output_json["source_id"] == sha256_file(bundle.pcb_path)

    padded_req = ctx.work_dir / "request_16m.json"
    padded_bytes = exp.output_bytes + b" " * (max_bytes - len(exp.output_bytes))
    padded_req.write_bytes(padded_bytes)
    assert padded_req.stat().st_size == max_bytes

    plan_path = ctx.work_dir / "plan.json"
    eng = ctx.json_driver.run_engine(padded_req, plan_path)
    assert_no_crash(eng.proc, "engine_16m")
    assert eng.proc.returncode == 0 and eng.output_json is not None
    assert eng.output_json["request_sha256"] == sha256_bytes(padded_bytes)

    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    app = ctx.json_driver.apply_plan(bundle.pcb_path, plan_path, out_pcb, padded_req)
    assert_no_crash(app.proc, "apply_16m")
    assert app.proc.returncode == 0
    return assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        plan_json=eng.output_json,
        request_bytes=padded_bytes,
    )


@register_test(
    test_id="T1-F4-04",
    tier=1,
    feature="F4",
    min_stage="BASELINE",
    name="Private source snapshot & read-only source-dir sidecar isolation",
)
def t1_f4_04(ctx: DriverContext) -> dict[str, Any]:
    import os

    src_dir = ctx.work_dir / "readonly_src"
    bundle = build_std_2pad_board(src_dir, seed=404)
    before_hashes = snapshot_source_dir(src_dir)
    out_dir = ctx.work_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    rules_path = write_json_file(ctx.work_dir / "rules.json", STD_RULES)
    req_path = out_dir / "request.json"
    plan_path = out_dir / "plan.json"
    out_pcb = out_dir / "routed.kicad_pcb"

    can_chmod = os.geteuid() != 0
    try:
        if can_chmod:
            bundle.pcb_path.chmod(0o444)
            src_dir.chmod(0o555)
        exp = ctx.json_driver.export_board(bundle.pcb_path, req_path, rules_path)
        assert_no_crash(exp.proc, "export")
        assert exp.proc.returncode == 0 and exp.output_bytes is not None
        eng = ctx.json_driver.run_engine(req_path, plan_path)
        assert_no_crash(eng.proc, "engine")
        assert eng.proc.returncode == 0 and eng.output_json is not None
        app = ctx.json_driver.apply_plan(bundle.pcb_path, plan_path, out_pcb, req_path)
        assert_no_crash(app.proc, "apply")
        assert app.proc.returncode == 0
    finally:
        if can_chmod:
            src_dir.chmod(0o755)
            bundle.pcb_path.chmod(0o644)

    assert set(src_dir.iterdir()) == {bundle.pcb_path}
    metrics = assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=src_dir,
        plan_json=eng.output_json,
        request_bytes=exp.output_bytes,
    )

    # Negative freshness subcheck: mutate source board and verify stale plan is refused
    pn = _pcbnew()
    mutated = pn.LoadBoard(str(bundle.pcb_path))
    for fp in mutated.GetFootprints():
        if fp.GetReference() == "J2":
            fp.SetPosition(pn.VECTOR2I(17_000_000, 12_000_000))
            for pad in fp.Pads():
                pad.SetPosition(pn.VECTOR2I(17_000_000, 12_000_000))
    assert pn.SaveBoard(str(bundle.pcb_path), mutated, True)
    stale_out = out_dir / "stale.kicad_pcb"
    stale_app = ctx.json_driver.apply_plan(bundle.pcb_path, plan_path, stale_out, req_path)
    assert_no_crash(stale_app.proc, "stale_apply")
    assert stale_app.proc.returncode == 2
    assert stale_app.stderr_json is not None and stale_app.stderr_json["status"] == "invalid"
    assert "does not match the input board" in stale_app.stderr_json["reason"]
    assert not stale_out.exists()
    return metrics


@register_test(
    test_id="T1-F4-05",
    tier=1,
    feature="F4",
    min_stage="BASELINE",
    name="Clean Dwgs.User board and footprint graphics accepted",
)
def t1_f4_05(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(seed=405)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    fp1 = b.add_footprint("J1", (3_000_000, 10_000_000))
    b.add_pad(fp1, "1", "SIGNAL", (3_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000))
    fp2 = b.add_footprint("J2", (17_000_000, 10_000_000))
    b.add_pad(fp2, "1", "SIGNAL", (17_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000))
    b.add_track("OBSTACLE", (10_000_000, 4_000_000), (10_000_000, 16_000_000), 250_000, locked=True)
    b.add_drawing(pn.Dwgs_User, pn.SHAPE_T_SEGMENT, (10_000_000, 5_000_000), (10_000_000, 15_000_000), width_nm=50_000)
    b.add_drawing(pn.Dwgs_User, pn.SHAPE_T_SEGMENT, (10_000_000, 5_000_000), (10_000_000, 15_000_000), width_nm=50_000, footprint=fp1)
    bundle = b.save(ctx.work_dir / "src")
    drc_in = run_native_drc(bundle.pcb_path)
    assert not any(v.get("type") == "copper_edge_clearance" for v in drc_in.violations)

    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    res, before_hashes = run_wire_or_pending(
        ctx, bundle.pcb_path, out_pcb, STD_RULES, allow_unsupported_engine=False
    )
    assert res.stage_failed is None, f"Dwgs.User board failed at {res.stage_failed}"
    assert res.engine_res is not None and res.engine_res.output_json is not None
    assert res.export_res.output_bytes is not None
    return assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        plan_json=res.engine_res.output_json,
        request_bytes=res.export_res.output_bytes,
    )


# ============================================================================
# Feature 5 — Extended Pad/Drill/Geometry Parity (T1-F5-01 .. T1-F5-05)
# ============================================================================


@register_test(
    test_id="T1-F5-01",
    tier=1,
    feature="F5",
    min_stage="M3",
    name="Arbitrary-angle (30, 45, 135 deg) rotated rectangular/oval/roundrect pads",
)
def t1_f5_01(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(seed=501)
    b.set_rect_outline(0, 0, 24_000_000, 20_000_000)
    fp1 = b.add_footprint("J1", (4_000_000, 10_000_000))
    b.add_pad(fp1, "1", "SIGNAL", (4_000_000, 10_000_000), shape=pn.PAD_SHAPE_RECT, size_nm=(2_000_000, 1_000_000), angle_deg=30.0)
    fp2 = b.add_footprint("J2", (20_000_000, 10_000_000))
    b.add_pad(fp2, "1", "SIGNAL", (20_000_000, 10_000_000), shape=pn.PAD_SHAPE_OVAL, size_nm=(2_000_000, 1_000_000), angle_deg=45.0)
    ob = b.add_footprint("OB1", (12_000_000, 10_000_000))
    b.add_pad(ob, "1", "", (12_000_000, 10_000_000), shape=pn.PAD_SHAPE_ROUNDRECT, size_nm=(3_000_000, 1_500_000), angle_deg=135.0, roundrect_ratio=0.25)
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, before_hashes = run_m3_board_or_pending(ctx, bundle, out_pcb)
    if mode == "wire":
        assert res.stage_failed is None, f"rotated pads failed at {res.stage_failed}"
    else:
        assert res.returncode == 0, f"native failed: {res.stderr}"
    return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=before_hashes, source_dir=bundle.directory)


@register_test(
    test_id="T1-F5-02",
    tier=1,
    feature="F5",
    min_stage="M3",
    name="PTH circular and oval/slotted drills on signal pads",
)
def t1_f5_02(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(seed=502)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    fp1 = b.add_footprint("J1", (4_000_000, 10_000_000))
    b.add_pad(
        fp1,
        "1",
        "SIGNAL",
        (4_000_000, 10_000_000),
        attrib=pn.PAD_ATTRIB_PTH,
        shape=pn.PAD_SHAPE_CIRCLE,
        size_nm=(2_000_000, 2_000_000),
        drill_shape=pn.PAD_DRILL_SHAPE_CIRCLE,
        drill_size_nm=(1_000_000, 1_000_000),
    )
    fp2 = b.add_footprint("J2", (16_000_000, 10_000_000))
    b.add_pad(
        fp2,
        "1",
        "SIGNAL",
        (16_000_000, 10_000_000),
        attrib=pn.PAD_ATTRIB_PTH,
        shape=pn.PAD_SHAPE_OVAL,
        size_nm=(3_000_000, 1_800_000),
        drill_shape=pn.PAD_DRILL_SHAPE_OBLONG,
        drill_size_nm=(1_600_000, 800_000),
    )
    b.add_track("OBSTACLE", (10_000_000, 4_000_000), (10_000_000, 16_000_000), 250_000, locked=True)
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, before_hashes = run_m3_board_or_pending(ctx, bundle, out_pcb)
    if mode == "wire":
        assert res.stage_failed is None, f"PTH pads failed at {res.stage_failed}"
    else:
        assert res.returncode == 0
    return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=before_hashes, source_dir=bundle.directory)


@register_test(
    test_id="T1-F5-03",
    tier=1,
    feature="F5",
    min_stage="M3",
    name="NPTH mounting hole avoidance",
)
def t1_f5_03(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(seed=503)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    for idx, x in enumerate((3_000_000, 17_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
        b.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), size_nm=(1_000_000, 1_000_000))
    mh = b.add_footprint("MH1", (10_000_000, 10_000_000))
    b.add_pad(
        mh,
        "1",
        "",
        (10_000_000, 10_000_000),
        attrib=pn.PAD_ATTRIB_NPTH,
        shape=pn.PAD_SHAPE_CIRCLE,
        size_nm=(3_000_000, 3_000_000),
        drill_shape=pn.PAD_DRILL_SHAPE_CIRCLE,
        drill_size_nm=(3_000_000, 3_000_000),
    )
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, before_hashes = run_m3_board_or_pending(ctx, bundle, out_pcb)
    if mode == "wire":
        assert res.stage_failed is None, f"NPTH route failed at {res.stage_failed}"
    else:
        assert res.returncode == 0
    return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=before_hashes, source_dir=bundle.directory)


@register_test(
    test_id="T1-F5-04",
    tier=1,
    feature="F5",
    min_stage="M3",
    name="Copper arcs (PCB_ARC) and non-rectangular Edge.Cuts outline",
)
def t1_f5_04(ctx: DriverContext) -> dict[str, Any]:
    b = BoardBuilder(seed=504)
    b.set_polygon_outline([
        (0, 0),
        (30_000_000, 0),
        (30_000_000, 15_000_000),
        (15_000_000, 15_000_000),
        (15_000_000, 30_000_000),
        (0, 30_000_000),
    ])
    for idx, y in enumerate((5_000_000, 25_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (5_000_000, y))
        b.add_pad(fp, "1", "SIGNAL", (5_000_000, y), size_nm=(1_000_000, 1_000_000))
    b.add_arc(
        "OBSTACLE",
        (1_000_000, 15_000_000),
        (6_000_000, 13_000_000),
        (11_000_000, 15_000_000),
        250_000,
        locked=True,
    )
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, before_hashes = run_m3_board_or_pending(ctx, bundle, out_pcb)
    if mode == "wire":
        assert res.stage_failed is None, f"arc/L-outline failed at {res.stage_failed}"
    else:
        assert res.returncode == 0
    return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=before_hashes, source_dir=bundle.directory)


@register_test(
    test_id="T1-F5-05",
    tier=1,
    feature="F5",
    min_stage="M3",
    name="Odd-nm pad dimensions and pad offsets (SetOffset)",
)
def t1_f5_05(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(seed=505)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    fp1 = b.add_footprint("J1", (4_000_000, 10_000_000))
    b.add_pad(fp1, "1", "SIGNAL", (4_000_000, 10_000_000), shape=pn.PAD_SHAPE_RECT, size_nm=(1_000_001, 500_001))
    fp2 = b.add_footprint("J2", (16_000_000, 10_000_000))
    b.add_pad(
        fp2,
        "1",
        "SIGNAL",
        (16_000_000, 10_000_000),
        shape=pn.PAD_SHAPE_RECT,
        size_nm=(2_000_000, 1_000_000),
        offset_nm=(500_000, 0),
    )
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, before_hashes = run_m3_board_or_pending(ctx, bundle, out_pcb)
    if mode == "wire":
        assert res.stage_failed is None, f"odd/offset pads failed at {res.stage_failed}"
    else:
        assert res.returncode == 0
    return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=before_hashes, source_dir=bundle.directory)


# ============================================================================
# Feature 6 — Rules, Multilayer, Vias & Search State (T1-F6-01 .. T1-F6-05)
# ============================================================================


@register_test(
    test_id="T1-F6-01",
    tier=1,
    feature="F6",
    min_stage="M3",
    name="Effective .kicad_pro netclasses (Default vs Power widths/vias)",
)
def t1_f6_01(ctx: DriverContext) -> dict[str, Any]:
    b = BoardBuilder(seed=601)
    b.set_rect_outline(0, 0, 24_000_000, 20_000_000)
    for idx, x in enumerate((4_000_000, 20_000_000), 1):
        fp_s = b.add_footprint(f"JS{idx}", (x, 6_000_000))
        b.add_pad(fp_s, "1", "SIG", (x, 6_000_000), size_nm=(1_000_000, 1_000_000))
        fp_p = b.add_footprint(f"JP{idx}", (x, 14_000_000))
        b.add_pad(fp_p, "1", "PWR_3V3", (x, 14_000_000), size_nm=(1_000_000, 1_000_000))
    b.set_project_rules(
        classes=[
            {"name": "Default", "priority": 0, "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "Power", "priority": 1, "clearance": 0.3, "track_width": 0.5, "via_diameter": 0.8, "via_drill": 0.4},
        ],
        netclass_patterns=[{"netclass": "Power", "pattern": "^PWR.*"}],
    )
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, before_hashes = run_m3_board_or_pending(ctx, bundle, out_pcb)
    if mode == "wire":
        assert res.stage_failed is None, f"netclass route failed at {res.stage_failed}"
    else:
        assert res.returncode == 0
    metrics = assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=before_hashes, source_dir=bundle.directory)
    pn = _pcbnew()
    routed = pn.LoadBoard(str(out_pcb))
    pwr_tracks = [t for t in routed.GetTracks() if t.GetNetname() == "PWR_3V3"]
    sig_tracks = [t for t in routed.GetTracks() if t.GetNetname() == "SIG"]
    assert pwr_tracks and all(t.GetWidth() >= 500_000 for t in pwr_tracks)
    assert sig_tracks and all(t.GetWidth() >= 200_000 for t in sig_tracks)
    return metrics


@register_test(
    test_id="T1-F6-02",
    tier=1,
    feature="F6",
    min_stage="M3",
    name="Effective .kicad_dru class-to-class clearance rule enforcement",
)
def t1_f6_02(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(seed=602)
    b.set_rect_outline(0, 0, 24_000_000, 20_000_000)
    for idx, x in enumerate((7_000_000, 17_000_000), 1):
        fp_p = b.add_footprint(f"JP{idx}", (x, 10_050_000))
        b.add_pad(fp_p, "1", "PWR_5V", (x, 10_050_000), shape=pn.PAD_SHAPE_RECT, size_nm=(1_000_000, 500_000))
    for idx, x in enumerate((3_000_000, 21_000_000), 1):
        fp_s = b.add_footprint(f"JS{idx}", (x, 9_400_000))
        b.add_pad(fp_s, "1", "SIG", (x, 9_400_000), shape=pn.PAD_SHAPE_RECT, size_nm=(1_000_000, 400_000))
    b.add_track("PWR_5V", (7_000_000, 10_050_000), (17_000_000, 10_050_000), 500_000, locked=True)
    # Corridor y=9.4M has 0.30 mm clearance to PWR_5V (top edge 9.80M vs SIG bottom 9.50M)
    # and 0.20 mm clearance to OBSTACLE (bottom edge 9.10M vs SIG top 9.30M):
    # legal under Default 0.20 mm, illegal under .kicad_dru 0.40 mm power_to_default.
    b.add_track("", (8_000_000, 9_000_000), (16_000_000, 9_000_000), 200_000, locked=True)
    b.set_project_rules(
        classes=[
            {"name": "Default", "priority": 0, "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "Power", "priority": 1, "clearance": 0.2, "track_width": 0.5, "via_diameter": 0.8, "via_drill": 0.4},
        ],
        netclass_patterns=[{"netclass": "Power", "pattern": "^PWR.*"}],
    )
    b.set_dru_text(
        '(version 1)\n(rule "power_to_default"\n  (condition "A.NetClass == \'Power\' && B.NetClass != \'Power\'")\n  (constraint clearance (min 0.4mm)))\n'
    )
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, before_hashes = run_m3_board_or_pending(ctx, bundle, out_pcb)
    if mode == "wire":
        assert res.stage_failed is None, f".kicad_dru route failed at {res.stage_failed}"
    else:
        assert res.returncode == 0
    verify_dru_loaded(out_pcb, sidecar_source=bundle.pcb_path)
    return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=before_hashes, source_dir=bundle.directory)


@register_test(
    test_id="T1-F6-03",
    tier=1,
    feature="F6",
    min_stage="M3",
    name="4-layer stackup F.Cu <-> B.Cu routing with through vias",
)
def t1_f6_03(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=4, seed=603)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    fp1 = b.add_footprint("J1", (4_000_000, 10_000_000))
    b.add_pad(fp1, "1", "SIGNAL", (4_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000), layers=[pn.F_Cu])
    fp2 = b.add_footprint("J2", (16_000_000, 10_000_000))
    b.add_pad(fp2, "1", "SIGNAL", (16_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000), layers=[pn.B_Cu])
    b.set_project_rules(
        classes=[{"name": "Default", "priority": 0, "clearance": 0.25, "track_width": 0.25, "via_diameter": 0.8, "via_drill": 0.4}]
    )
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, before_hashes = run_m3_board_or_pending(ctx, bundle, out_pcb)
    if mode == "wire":
        assert res.stage_failed is None, f"4-layer via route failed at {res.stage_failed}"
    else:
        assert res.returncode == 0
    metrics = assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=before_hashes, source_dir=bundle.directory)
    routed = pn.LoadBoard(str(out_pcb))
    vias = [t for t in routed.GetTracks() if t.GetClass() == "PCB_VIA"]
    assert vias and all(v.GetWidth(v.TopLayer()) >= 800_000 and v.GetDrill() >= 400_000 for v in vias)
    return metrics


@register_test(
    test_id="T1-F6-04",
    tier=1,
    feature="F6",
    min_stage="M3",
    name="Blind/buried/microvia span legality under project rules",
)
def t1_f6_04(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=4, seed=604)
    b.set_rect_outline(0, 0, 24_000_000, 20_000_000)
    fp1 = b.add_footprint("J1", (4_000_000, 10_000_000))
    b.add_pad(fp1, "1", "SIGNAL", (4_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000), layers=[pn.In1_Cu])
    fp2 = b.add_footprint("J2", (20_000_000, 10_000_000))
    b.add_pad(fp2, "1", "SIGNAL", (20_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000), layers=[pn.In2_Cu])
    b.add_keepout([pn.F_Cu, pn.B_Cu], [(0, 0), (24_000_000, 0), (24_000_000, 20_000_000), (0, 20_000_000)], no_tracks=True, no_vias=True)
    b.set_project_rules(
        rules={"allow_blind_buried_vias": True, "allow_microvias": True, "min_via_diameter": 0.6, "min_microvia_diameter": 0.3, "min_microvia_drill": 0.15}
    )
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, before_hashes = run_m3_board_or_pending(ctx, bundle, out_pcb)
    if mode == "wire":
        assert res.stage_failed is None, f"buried via route failed at {res.stage_failed}"
    else:
        assert res.returncode == 0
    metrics = assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=before_hashes, source_dir=bundle.directory)
    routed = pn.LoadBoard(str(out_pcb))
    vias = [t for t in routed.GetTracks() if t.GetClass() == "PCB_VIA"]
    assert vias and all((v.TopLayer(), v.BottomLayer()) == (pn.In1_Cu, pn.In2_Cu) for v in vias)
    return metrics


@register_test(
    test_id="T1-F6-05",
    tier=1,
    feature="F6",
    min_stage="M3",
    name="History-dependent prior-via witness (RE-02 augmented state)",
)
def t1_f6_05(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=4, seed=605)
    b.set_rect_outline(0, 0, 24_000_000, 20_000_000)
    fp1 = b.add_footprint("J1", (4_000_000, 10_000_000))
    b.add_pad(fp1, "1", "SIGNAL", (4_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000), layers=[pn.F_Cu])
    fp2 = b.add_footprint("J2", (20_000_000, 10_000_000))
    b.add_pad(fp2, "1", "SIGNAL", (20_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000), layers=[pn.B_Cu])
    # F.Cu wall at x=9.5M forces Via 1 at x<9.5M; In1.Cu walls force Via 2 at (10.5M, 10M);
    # B.Cu wall at x=9.5M prevents early B.Cu; In2.Cu sealed against track routing.
    b.add_track("", (7_000_000, 7_000_000), (7_000_000, 13_000_000), 500_000, layer=pn.F_Cu, locked=True)
    b.add_track("", (9_500_000, 750_000), (9_500_000, 19_250_000), 500_000, layer=pn.F_Cu, locked=True)
    b.add_track("", (10_500_000, 750_000), (10_500_000, 8_500_000), 500_000, layer=pn.In1_Cu, locked=True)
    b.add_track("", (10_500_000, 11_500_000), (10_500_000, 19_250_000), 500_000, layer=pn.In1_Cu, locked=True)
    b.add_track("", (11_500_000, 750_000), (11_500_000, 19_250_000), 500_000, layer=pn.In1_Cu, locked=True)
    b.add_track("", (9_500_000, 750_000), (9_500_000, 19_250_000), 500_000, layer=pn.B_Cu, locked=True)
    b.add_keepout([pn.In2_Cu], [(0, 0), (24_000_000, 0), (24_000_000, 20_000_000), (0, 20_000_000)], no_tracks=True, no_vias=False)
    b.set_project_rules(
        rules={"min_hole_to_hole": 3.0},
        classes=[{"name": "Default", "priority": 0, "clearance": 0.25, "track_width": 0.25, "via_diameter": 0.8, "via_drill": 0.4}],
        rule_severities={"hole_to_hole": "error"},
    )
    b.set_dru_text(
        '(version 1)\n(rule "via_spacing"\n  (condition "A.Type == \'Via\' && B.Type == \'Via\'")\n  (constraint hole_to_hole (min 3.0mm)))\n'
    )
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, before_hashes = run_m3_board_or_pending(ctx, bundle, out_pcb)
    if mode == "wire":
        assert res.stage_failed is None, f"history witness failed at {res.stage_failed}"
    else:
        assert res.returncode == 0
    return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=before_hashes, source_dir=bundle.directory)
