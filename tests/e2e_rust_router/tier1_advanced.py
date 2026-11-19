"""Tier 1 isolated happy-path E2E tests for Features 7-11 (25 tests)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from tests.e2e_rust_router.core_helpers import (
    assert_clean_e2e,
    assert_no_crash,
    assert_source_dir_untouched,
    build_crossing_nets_board,
    build_diff_pair_board,
    build_keepout_board,
    build_pour_board,
    parse_last_json_line_safe,
    require_m4_capability,
    run_m3_board_or_pending,
    run_m4_board_or_pending,
    snapshot_source_dir,
    stage_fixture_copy,
    write_consistent_schematic,
)
from tests.e2e_rust_router.drivers import run_measured, sha256_file
from tests.e2e_rust_router.kicad_verify import (
    BoardBuilder,
    _pcbnew,
    run_native_drc,
    verify_diff_pair_native,
    verify_fromto_length_groups,
)
from tests.e2e_rust_router.runner import DriverContext, PendingCapability, register_test


def _assert_m3_complete(
    mode: str,
    res: Any,
    bundle_pcb: Path,
    out_pcb: Path,
    before_hashes: dict[str, str],
    source_dir: Path,
    *,
    refill_zones: bool = False,
    check_pour_integrity: bool = False,
) -> dict[str, Any]:
    if mode == "native":
        assert res.returncode == 0, f"native route failed (rc={res.returncode}): {res.stderr}"
        return assert_clean_e2e(
            bundle_pcb,
            out_pcb,
            before_hashes=before_hashes,
            source_dir=source_dir,
            refill_zones=refill_zones,
            check_pour_integrity=check_pour_integrity,
        )
    assert res.stage_failed is None, f"wire route failed at {res.stage_failed}"
    assert res.engine_res is not None and res.engine_res.output_json is not None
    return assert_clean_e2e(
        bundle_pcb,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=source_dir,
        plan_json=res.engine_res.output_json,
        request_bytes=res.export_res.output_bytes,
        refill_zones=refill_zones,
        check_pour_integrity=check_pour_integrity,
    )


# ============================================================================
# Feature 7 — Zones, Planes, Thermals & Keepouts (T1-F7-01 .. T1-F7-05)
# ============================================================================


@register_test(
    test_id="T1-F7-01",
    tier=1,
    feature="F7",
    min_stage="M3",
    name="Rule-area keepout (no_tracks) detours signal net cleanly",
)
def t1_f7_01(ctx: DriverContext) -> dict[str, Any]:
    bundle = build_keepout_board(ctx.work_dir / "src", mode="no_tracks", seed=7101)
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m3_board_or_pending(ctx, bundle, out_pcb, tag="f7_01")
    metrics = _assert_m3_complete(mode, res, bundle.pcb_path, out_pcb, bh, bundle.directory)
    drc = run_native_drc(out_pcb, sidecar_source=bundle.pcb_path)
    assert not [v for v in drc.violations if v.get("type") == "items_not_allowed"]
    return metrics


@register_test(
    test_id="T1-F7-02",
    tier=1,
    feature="F7",
    min_stage="M3",
    name="Rule-area keepout (no_vias only) allows track, forbids via",
)
def t1_f7_02(ctx: DriverContext) -> dict[str, Any]:
    bundle = build_keepout_board(ctx.work_dir / "src", mode="no_vias_only", seed=7102)
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m3_board_or_pending(ctx, bundle, out_pcb, tag="f7_02")
    metrics = _assert_m3_complete(mode, res, bundle.pcb_path, out_pcb, bh, bundle.directory)
    pn = _pcbnew()
    board = pn.LoadBoard(str(out_pcb))
    vias = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]
    assert vias, "expected via transition between F.Cu and B.Cu"
    for v in vias:
        assert v.GetPosition().x > 16_000_000 or v.GetPosition().x < 4_000_000
    drc = run_native_drc(out_pcb, sidecar_source=bundle.pcb_path)
    assert not [v for v in drc.violations if v.get("type") == "items_not_allowed"]
    return metrics


@register_test(
    test_id="T1-F7-03",
    tier=1,
    feature="F7",
    min_stage="M3",
    name="Filled GND zone on B.Cu connects THT pads with --refill-zones",
)
def t1_f7_03(ctx: DriverContext) -> dict[str, Any]:
    bundle = build_pour_board(ctx.work_dir / "src", layers=2, min_resolved_spokes=2, seed=7103)
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m3_board_or_pending(ctx, bundle, out_pcb, tag="f7_03")
    return _assert_m3_complete(
        mode,
        res,
        bundle.pcb_path,
        out_pcb,
        bh,
        bundle.directory,
        refill_zones=True,
        check_pour_integrity=True,
    )


@register_test(
    test_id="T1-F7-04",
    tier=1,
    feature="F7",
    min_stage="M3",
    name="4-layer inner GND plane (In1.Cu) pad via-fanout",
)
def t1_f7_04(ctx: DriverContext) -> dict[str, Any]:
    fixture_src = ctx.repo_root / "reports" / "routing-engine" / "inputs" / "four-layer-plane"
    if (fixture_src / "four-layer-plane.kicad_pcb").is_file():
        src_dir = stage_fixture_copy(fixture_src, ctx.work_dir / "src")
        pcb_path = src_dir / "four-layer-plane.kicad_pcb"
        pro_path = src_dir / "four-layer-plane.kicad_pro"
        from tests.e2e_rust_router.kicad_verify import FixtureBundle

        bundle = FixtureBundle(directory=src_dir, pcb_path=pcb_path, pro_path=pro_path if pro_path.is_file() else None)
    else:
        bundle = build_pour_board(ctx.work_dir / "src", layers=4, seed=7104)
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m3_board_or_pending(ctx, bundle, out_pcb, tag="f7_04")
    return _assert_m3_complete(
        mode,
        res,
        bundle.pcb_path,
        out_pcb,
        bh,
        bundle.directory,
        refill_zones=True,
        check_pour_integrity=True,
    )


@register_test(
    test_id="T1-F7-05",
    tier=1,
    feature="F7",
    min_stage="M3",
    name="Multi-zone priority and thermal-spoke preservation around signal routing",
)
def t1_f7_05(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=2, seed=7105)
    b.set_design_settings(min_resolved_spokes=2)
    b.set_rect_outline(0, 0, 40_000_000, 30_000_000)
    for idx, x in enumerate((8_000_000, 32_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 15_000_000))
        b.add_pad(fp, "1", "SIG", (x, 22_000_000), size_nm=(1_200_000, 1_200_000))
        b.add_pad(
            fp,
            "2",
            "GND",
            (x, 10_000_000),
            attrib=pn.PAD_ATTRIB_PTH,
            size_nm=(1_700_000, 1_700_000),
            drill_size_nm=(800_000, 800_000),
        )
    b.add_zone(
        "GND",
        [pn.B_Cu],
        [(2_000_000, 2_000_000), (38_000_000, 2_000_000), (38_000_000, 28_000_000), (2_000_000, 28_000_000)],
        priority=0,
        zone_name="GND_OUTER",
    )
    b.add_zone(
        "GND",
        [pn.B_Cu],
        [(4_000_000, 4_000_000), (36_000_000, 4_000_000), (36_000_000, 16_000_000), (4_000_000, 16_000_000)],
        priority=1,
        zone_name="GND_INNER",
    )
    b.set_project_rules()
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m3_board_or_pending(ctx, bundle, out_pcb, tag="f7_05")
    return _assert_m3_complete(
        mode,
        res,
        bundle.pcb_path,
        out_pcb,
        bh,
        bundle.directory,
        refill_zones=True,
        check_pour_integrity=True,
    )


# ============================================================================
# Feature 8 — Global Multi-Net & Rip-Up/Reroute (T1-F8-01 .. T1-F8-05)
# ============================================================================


@register_test(
    test_id="T1-F8-01",
    tier=1,
    feature="F8",
    min_stage="M4",
    name="Crossing 2-net bottleneck resolved by rip-up/reroute",
)
def t1_f8_01(ctx: DriverContext) -> dict[str, Any]:
    bundle = build_crossing_nets_board(ctx.work_dir / "src", num_nets=2, seed=8101)
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m4_board_or_pending(ctx, bundle, out_pcb, tag="f8_01")
    if mode == "wire":
        if res.stage_failed == "engine" and res.engine_res is not None and res.engine_res.proc.returncode in (3, 4):
            raise PendingCapability("M4 rip-up/reroute not yet landed on baseline", evidence={"rc": res.engine_res.proc.returncode})
    elif res.returncode in (3, 4):
        raise PendingCapability("M4 rip-up/reroute not yet landed in native router", evidence={"rc": res.returncode})
    return _assert_m3_complete(mode, res, bundle.pcb_path, out_pcb, bh, bundle.directory)


@register_test(
    test_id="T1-F8-02",
    tier=1,
    feature="F8",
    min_stage="M4",
    name="Congestion-aware multi-net negotiation (6 crossing nets)",
)
def t1_f8_02(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx)
    bundle = build_crossing_nets_board(ctx.work_dir / "src", num_nets=6, seed=8102)
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m4_board_or_pending(ctx, bundle, out_pcb, tag="f8_02")
    return _assert_m3_complete(mode, res, bundle.pcb_path, out_pcb, bh, bundle.directory)


@register_test(
    test_id="T1-F8-03",
    tier=1,
    feature="F8",
    min_stage="M4",
    name="Whole-net repair preserves immutable input copper and unlocks only generated",
)
def t1_f8_03(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx)
    b = BoardBuilder(copper_layers=2, seed=8103)
    b.set_rect_outline(0, 0, 30_000_000, 20_000_000)
    pre_locked = b.add_track("PRE_LOCK", (2_000_000, 3_000_000), (28_000_000, 3_000_000), 250_000, locked=True)
    pre_unlocked = b.add_track("PRE_OPEN", (2_000_000, 17_000_000), (28_000_000, 17_000_000), 250_000, locked=False)
    for i in range(3):
        y0 = 6_000_000 + i * 3_500_000
        y1 = 13_000_000 - i * 3_500_000
        fp1 = b.add_footprint(f"J{i + 1}A", (5_000_000, y0))
        b.add_pad(fp1, "1", f"SIG_{i}", (5_000_000, y0), size_nm=(1_000_000, 1_000_000))
        fp2 = b.add_footprint(f"J{i + 1}B", (25_000_000, y1))
        b.add_pad(fp2, "1", f"SIG_{i}", (25_000_000, y1), size_nm=(1_000_000, 1_000_000))
    b.set_project_rules()
    bundle = b.save(ctx.work_dir / "src")
    input_ids = {pre_locked.m_Uuid.AsString(), pre_unlocked.m_Uuid.AsString()}
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m4_board_or_pending(ctx, bundle, out_pcb, tag="f8_03")
    if mode == "wire" and res.engine_res and res.engine_res.output_json:
        removed = set(res.engine_res.output_json.get("removed_track_ids", []))
        assert not (removed & input_ids), f"removed input tracks: {removed & input_ids}"
    return _assert_m3_complete(mode, res, bundle.pcb_path, out_pcb, bh, bundle.directory)


@register_test(
    test_id="T1-F8-04",
    tier=1,
    feature="F8",
    min_stage="M4",
    name="Deterministic multi-net scheduling across identical runs (8 nets)",
)
def t1_f8_04(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx)
    bundle = build_crossing_nets_board(ctx.work_dir / "src", num_nets=8, seed=8104)
    limits = {"max_work": 5_000_000}
    out_a = ctx.work_dir / "out_a" / "routed.kicad_pcb"
    out_b = ctx.work_dir / "out_b" / "routed.kicad_pcb"
    out_a.parent.mkdir(parents=True, exist_ok=True)
    out_b.parent.mkdir(parents=True, exist_ok=True)
    mode_a, res_a, bh_a = run_m4_board_or_pending(
        ctx, bundle, out_a, tag="f8_04_a", limits=limits
    )
    mode_b, res_b, bh_b = run_m4_board_or_pending(
        ctx, bundle, out_b, tag="f8_04_b", limits=limits
    )
    assert mode_a == mode_b
    metrics = _assert_m3_complete(mode_a, res_a, bundle.pcb_path, out_a, bh_a, bundle.directory)
    _assert_m3_complete(mode_b, res_b, bundle.pcb_path, out_b, bh_b, bundle.directory)
    if mode_a == "wire":
        assert res_a.engine_res is not None and res_b.engine_res is not None
        assert res_a.engine_res.output_bytes == res_b.engine_res.output_bytes
    else:
        assert sha256_file(out_a) == sha256_file(out_b)
    return metrics


@register_test(
    test_id="T1-F8-05",
    tier=1,
    feature="F8",
    min_stage="M4",
    name="Multi-round rip-up inside single outer work budget",
)
def t1_f8_05(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx)
    bundle = build_crossing_nets_board(ctx.work_dir / "src", num_nets=4, seed=8105)
    limits = {"max_work": 5_000_000}
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m4_board_or_pending(ctx, bundle, out_pcb, tag="f8_05", limits=limits)
    metrics = _assert_m3_complete(mode, res, bundle.pcb_path, out_pcb, bh, bundle.directory)
    if mode == "wire" and res.engine_res and res.engine_res.output_json:
        assert res.engine_res.output_json["work_used"] <= limits["max_work"]
    return metrics


# ============================================================================
# Feature 9 — Differential Pairs, Length & Delay (T1-F9-01 .. T1-F9-05)
# ============================================================================


@register_test(
    test_id="T1-F9-01",
    tier=1,
    feature="F9",
    min_stage="M4",
    name="Coupled diff-pair routing with .kicad_dru gap rule",
)
def t1_f9_01(ctx: DriverContext) -> dict[str, Any]:
    src_fixture = ctx.repo_root / "reports" / "routing-engine" / "inputs" / "diff-pair"
    if (src_fixture / "pair.kicad_pcb").is_file():
        src_dir = stage_fixture_copy(src_fixture, ctx.work_dir / "src")
        from tests.e2e_rust_router.kicad_verify import FixtureBundle

        bundle = FixtureBundle(
            directory=src_dir,
            pcb_path=src_dir / "pair.kicad_pcb",
            pro_path=src_dir / "pair.kicad_pro",
            dru_path=src_dir / "pair.kicad_dru",
        )
        pairs = [("/CAN+", "/CAN-")]
    else:
        bundle = build_diff_pair_board(ctx.work_dir / "src", seed=9101)
        pairs = [("CAN+", "CAN-")]
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m4_board_or_pending(
        ctx,
        bundle,
        out_pcb,
        extra_args=["--diff-pairs", "auto"],
        require_help_flags=("--diff-pairs",),
        tag="f9_01",
    )
    metrics = _assert_m3_complete(mode, res, bundle.pcb_path, out_pcb, bh, bundle.directory)
    dp = verify_diff_pair_native(bundle.pcb_path, out_pcb, pairs)
    return {**metrics, "diff_pair": dp}


@register_test(
    test_id="T1-F9-02",
    tier=1,
    feature="F9",
    min_stage="M4",
    name="Differential pair layer transition across 4-layer board",
)
def t1_f9_02(ctx: DriverContext) -> dict[str, Any]:
    src_dir = stage_fixture_copy(ctx.repo_root / "fixtures" / "pair_transition", ctx.work_dir / "src")
    from tests.e2e_rust_router.kicad_verify import FixtureBundle

    bundle = FixtureBundle(
        directory=src_dir,
        pcb_path=src_dir / "board.kicad_pcb",
        pro_path=src_dir / "board.kicad_pro",
        dru_path=src_dir / "board.kicad_dru",
    )
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m4_board_or_pending(
        ctx,
        bundle,
        out_pcb,
        extra_args=["--diff-pairs", "auto", "--pair-layers", "F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
        require_help_flags=("--diff-pairs", "--pair-layers"),
        tag="f9_02",
    )
    metrics = _assert_m3_complete(mode, res, bundle.pcb_path, out_pcb, bh, bundle.directory)
    dp = verify_diff_pair_native(bundle.pcb_path, out_pcb, [("CAN+", "CAN-")])
    return {**metrics, "diff_pair": dp}


@register_test(
    test_id="T1-F9-03",
    tier=1,
    feature="F9",
    min_stage="M4",
    name="Single-ended length-match group (fromTo skew grading)",
)
def t1_f9_03(ctx: DriverContext) -> dict[str, Any]:
    src_dir = stage_fixture_copy(ctx.repo_root / "fixtures" / "group_matching", ctx.work_dir / "src")
    from tests.e2e_rust_router.kicad_verify import FixtureBundle

    bundle = FixtureBundle(
        directory=src_dir,
        pcb_path=src_dir / "group.kicad_pcb",
        pro_path=src_dir / "group.kicad_pro",
        dru_path=src_dir / "group.kicad_dru",
    )
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m4_board_or_pending(
        ctx,
        bundle,
        out_pcb,
        extra_args=[
            "--length-match-group",
            "DQ0",
            "DQ1",
            "DQ2",
            "DQ3",
            "--group-match-tolerance",
            "0.1",
            "--group-meander-amplitude",
            "3",
        ],
        require_help_flags=("--length-match-group", "--group-match-tolerance"),
        tag="f9_03",
    )
    metrics = _assert_m3_complete(mode, res, bundle.pcb_path, out_pcb, bh, bundle.directory)
    ft = verify_fromto_length_groups(out_pcb, [["DQ0", "DQ1", "DQ2", "DQ3"]], tolerance_mm=0.1, sidecar_source=bundle.pcb_path)
    assert ft.passed, f"fromTo length matching failed: skew={ft.skew_violations}, cov={ft.coverage_errors}"
    return {**metrics, "measurements": len(ft.measurements)}


@register_test(
    test_id="T1-F9-04",
    tier=1,
    feature="F9",
    min_stage="M4",
    name="Nominal propagation delay matching against KiCad 10 tuning profiles",
)
def t1_f9_04(ctx: DriverContext) -> dict[str, Any]:
    src_dir = stage_fixture_copy(ctx.repo_root / "fixtures" / "delay_matching", ctx.work_dir / "src")
    from tests.e2e_rust_router.kicad_verify import FixtureBundle

    bundle = FixtureBundle(
        directory=src_dir,
        pcb_path=src_dir / "delay.kicad_pcb",
        pro_path=src_dir / "delay.kicad_pro",
        dru_path=src_dir / "delay.kicad_dru",
    )
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m4_board_or_pending(
        ctx,
        bundle,
        out_pcb,
        extra_args=[
            "--length-match-group",
            "DQ0",
            "DQ1",
            "--group-delay-tolerance",
            "1",
        ],
        require_help_flags=("--length-match-group", "--group-delay-tolerance"),
        tag="f9_04",
    )
    metrics = _assert_m3_complete(
        mode, res, bundle.pcb_path, out_pcb, bh, bundle.directory, refill_zones=True
    )
    ft = verify_fromto_length_groups(out_pcb, [["DQ0", "DQ1"]], tolerance_ps=1.0, sidecar_source=bundle.pcb_path)
    assert ft.passed, f"fromTo delay matching failed: skew={ft.skew_violations}, cov={ft.coverage_errors}"
    return {**metrics, "measurements": len(ft.measurements)}


@register_test(
    test_id="T1-F9-05",
    tier=1,
    feature="F9",
    min_stage="M4",
    name="Coupled pair-to-pair length matching (inter_pair_matching)",
)
def t1_f9_05(ctx: DriverContext) -> dict[str, Any]:
    src_dir = stage_fixture_copy(ctx.repo_root / "fixtures" / "inter_pair_matching", ctx.work_dir / "src")
    from tests.e2e_rust_router.kicad_verify import FixtureBundle

    bundle = FixtureBundle(
        directory=src_dir,
        pcb_path=src_dir / "pairs.kicad_pcb",
        pro_path=src_dir / "pairs.kicad_pro",
        dru_path=src_dir / "pairs.kicad_dru",
    )
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m4_board_or_pending(
        ctx,
        bundle,
        out_pcb,
        extra_args=[
            "--diff-pairs",
            "auto",
            "--length-match-group",
            "A_P",
            "A_N",
            "B_P",
            "B_N",
            "--group-match-tolerance",
            "0.15",
        ],
        require_help_flags=("--diff-pairs", "--length-match-group"),
        tag="f9_05",
    )
    metrics = _assert_m3_complete(mode, res, bundle.pcb_path, out_pcb, bh, bundle.directory)
    dp = verify_diff_pair_native(bundle.pcb_path, out_pcb, [("A_P", "A_N"), ("B_P", "B_N")])
    ft = verify_fromto_length_groups(
        out_pcb, [["A_P", "A_N", "B_P", "B_N"]], tolerance_mm=0.15, sidecar_source=bundle.pcb_path
    )
    assert ft.passed, f"inter-pair fromTo failed: skew={ft.skew_violations}, cov={ft.coverage_errors}"
    return {**metrics, "diff_pair": dp, "measurements": len(ft.measurements)}


# ============================================================================
# Feature 10 — agent-ee Integration & Provenance (T1-F10-01 .. T1-F10-05)
# ============================================================================


@register_test(
    test_id="T1-F10-01",
    tier=1,
    feature="F10",
    min_stage="M5",
    name="agent-ee routing_adapter invokes Rust router on placed RC board",
)
def t1_f10_01(ctx: DriverContext) -> dict[str, Any]:
    ctx.agent_ee_driver.probe_rust_integration(ctx.native_driver)
    ae_root = ctx.agent_ee_driver.agent_ee_root
    placed_src = ae_root / "fixtures" / "rc" / "stages" / "placed-unrouted"
    assert (placed_src / "rc_stage.kicad_pcb").is_file()
    cand_root = ctx.work_dir / "cand"
    placed_dir = stage_fixture_copy(placed_src, cand_root / "placed")
    before_hashes = snapshot_source_dir(placed_dir)
    checkout = ctx.agent_ee_driver.clone_clean_checkout(ctx.repo_root, ctx.work_dir / "checkout")
    state = ctx.work_dir / "state"
    ctx.agent_ee_driver.make_synthetic_state(checkout, state)
    work_root = ctx.work_dir / "step_work"
    work_root.mkdir(parents=True, exist_ok=True)
    script = """
import json, sys
from pathlib import Path
from agent_ee import kicad_route_attempt, routing_adapter
from agent_ee.executor import execute

cand_root, work_root, checkout, state = map(Path, sys.argv[1:5])
spec = routing_adapter.step(
    project='placed', board='rc_stage.kicad_pcb', schematic='rc_stage.kicad_sch',
    name='rc', label='e2e-m5', checkout=str(checkout), state=str(state), budget=120,
)
res = execute(
    cand_root,
    work_root,
    spec['argv'],
    timeout=spec['timeout_s'],
    extra_ro=[(m['source'], m['at']) for m in spec['mounts']],
)
inputs = kicad_route_attempt.files_of(kicad_route_attempt.tree(cand_root))
summary = routing_adapter.verify(work_root, inputs)
print(json.dumps({'exit_code': res['exit_code'], 'summary': summary}))
"""
    proc = ctx.agent_ee_driver.run_python(
        script, str(cand_root), str(work_root), str(checkout), str(state), timeout_s=300.0
    )
    assert_no_crash(proc, "T1-F10-01")
    assert proc.returncode == 0, f"agent-ee step failed: {proc.stderr}"
    assert_source_dir_untouched(placed_dir, before_hashes)
    payload = json.loads(proc.stdout)
    assert payload["exit_code"] == 0, f"executor exit_code={payload['exit_code']}: {payload}"
    assert payload["summary"]["proposal"] == "eligible"
    cand_pcb = work_root / "routing" / "candidate" / "placed" / "rc_stage.kicad_pcb"
    return assert_clean_e2e(
        placed_dir / "rc_stage.kicad_pcb",
        cand_pcb,
        before_hashes=before_hashes,
        source_dir=placed_dir,
        refill_zones=True,
    )


@register_test(
    test_id="T1-F10-02",
    tier=1,
    feature="F10",
    min_stage="M5",
    name="agent-ee 4-layer VIA_BOARD routes through Rust adapter with class vias",
)
def t1_f10_02(ctx: DriverContext) -> dict[str, Any]:
    ctx.agent_ee_driver.probe_rust_integration(ctx.native_driver)
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=4, seed=10102)
    b.set_rect_outline(40_000_000, 40_000_000, 70_000_000, 60_000_000)
    fp1 = b.add_footprint("J1", (47_000_000, 50_000_000))
    fp2 = b.add_footprint("J2", (63_000_000, 50_000_000), side="B")
    for idx, net in enumerate(("A", "B", "C"), 1):
        y = 47_000_000 + (idx - 1) * 3_000_000
        b.add_pad(fp1, str(idx), net, (47_000_000, y), size_nm=(600_000, 600_000))
        b.add_pad(fp2, str(idx), net, (63_000_000, y), size_nm=(600_000, 600_000), layers=[pn.B_Cu])
    b.set_project_rules(
        rules={
            "min_via_diameter": 0.25,
            "min_via_annular_width": 0.05,
            "min_through_hole_diameter": 0.2,
        },
        classes=[
            {
                "name": "Default",
                "priority": 0,
                "clearance": 0.2,
                "track_width": 0.2,
                "via_diameter": 0.45,
                "via_drill": 0.2,
                "microvia_diameter": 0.3,
                "microvia_drill": 0.15,
            }
        ],
        rule_severities={"annular_width": "error", "hole_clearance": "error"},
    )
    cand_root = ctx.work_dir / "cand"
    bundle = b.save(cand_root / "project")
    write_consistent_schematic(bundle, project_name="board")
    before_hashes = snapshot_source_dir(bundle.directory)
    checkout = ctx.agent_ee_driver.clone_clean_checkout(ctx.repo_root, ctx.work_dir / "checkout")
    state = ctx.work_dir / "state"
    ctx.agent_ee_driver.make_synthetic_state(checkout, state)
    work_root = ctx.work_dir / "step_work"
    work_root.mkdir(parents=True, exist_ok=True)
    script = """
import json, sys
from pathlib import Path
from agent_ee import kicad_route_attempt, routing_adapter
from agent_ee.executor import execute

cand_root, work_root, checkout, state = map(Path, sys.argv[1:5])
spec = routing_adapter.step(
    project='project', board='board.kicad_pcb', schematic='board.kicad_sch',
    name='via_board', label='e2e-m5', checkout=str(checkout), state=str(state), budget=120,
)
res = execute(
    cand_root,
    work_root,
    spec['argv'],
    timeout=spec['timeout_s'],
    extra_ro=[(m['source'], m['at']) for m in spec['mounts']],
)
inputs = kicad_route_attempt.files_of(kicad_route_attempt.tree(cand_root))
summary = routing_adapter.verify(work_root, inputs)
print(json.dumps({'exit_code': res['exit_code'], 'summary': summary}))
"""
    proc = ctx.agent_ee_driver.run_python(
        script, str(cand_root), str(work_root), str(checkout), str(state), timeout_s=300.0
    )
    assert_no_crash(proc, "T1-F10-02")
    assert proc.returncode == 0, f"VIA_BOARD failed: {proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["exit_code"] == 0, f"VIA_BOARD executor exit_code={payload['exit_code']}: {payload}"
    summary = payload["summary"]
    assert summary["classification"] == "complete"
    cand_pcb = work_root / "routing" / "candidate" / "project" / "board.kicad_pcb"
    metrics = assert_clean_e2e(
        bundle.pcb_path,
        cand_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        check_parity=True,
    )
    board = pn.LoadBoard(str(cand_pcb))
    vias = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]
    assert vias, "expected via placement on 4-layer VIA_BOARD"
    for v in vias:
        assert int(v.GetWidth(v.TopLayer())) == 450_000 and int(v.GetDrill()) == 200_000
    return metrics


@register_test(
    test_id="T1-F10-03",
    tier=1,
    feature="F10",
    min_stage="M5",
    name="agent-ee PAIR_BOARD routes differential pair through Rust adapter",
)
def t1_f10_03(ctx: DriverContext) -> dict[str, Any]:
    ctx.agent_ee_driver.probe_rust_integration(ctx.native_driver)
    cand_root = ctx.work_dir / "cand"
    bundle = build_diff_pair_board(cand_root / "project", seed=10103)
    write_consistent_schematic(bundle, project_name="board")
    before_hashes = snapshot_source_dir(bundle.directory)
    checkout = ctx.agent_ee_driver.clone_clean_checkout(ctx.repo_root, ctx.work_dir / "checkout")
    state = ctx.work_dir / "state"
    ctx.agent_ee_driver.make_synthetic_state(checkout, state)
    work_root = ctx.work_dir / "step_work"
    work_root.mkdir(parents=True, exist_ok=True)
    script = """
import json, sys
from pathlib import Path
from agent_ee import kicad_route_attempt, routing_adapter
from agent_ee.executor import execute

cand_root, work_root, checkout, state = map(Path, sys.argv[1:5])
spec = routing_adapter.step(
    project='project', board='board.kicad_pcb', schematic='board.kicad_sch',
    name='pair_board', label='e2e-m5', checkout=str(checkout), state=str(state), budget=120,
    diff_pairs='auto',
)
res = execute(
    cand_root,
    work_root,
    spec['argv'],
    timeout=spec['timeout_s'],
    extra_ro=[(m['source'], m['at']) for m in spec['mounts']],
)
inputs = kicad_route_attempt.files_of(kicad_route_attempt.tree(cand_root))
summary = routing_adapter.verify(work_root, inputs)
print(json.dumps({'exit_code': res['exit_code'], 'summary': summary}))
"""
    proc = ctx.agent_ee_driver.run_python(
        script, str(cand_root), str(work_root), str(checkout), str(state), timeout_s=300.0
    )
    assert_no_crash(proc, "T1-F10-03")
    assert proc.returncode == 0, f"PAIR_BOARD failed: {proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["exit_code"] == 0, f"PAIR_BOARD executor exit_code={payload['exit_code']}: {payload}"
    summary = payload["summary"]
    assert summary["classification"] == "complete"
    cand_pcb = work_root / "routing" / "candidate" / "project" / "board.kicad_pcb"
    metrics = assert_clean_e2e(
        bundle.pcb_path,
        cand_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        check_parity=True,
    )
    dp = verify_diff_pair_native(bundle.pcb_path, cand_pcb, [("CAN+", "CAN-")])
    return {**metrics, "diff_pair": dp}


@register_test(
    test_id="T1-F10-04",
    tier=1,
    feature="F10",
    min_stage="M5",
    name="--protect human multi-step chain and net_completion ratchet",
)
def t1_f10_04(ctx: DriverContext) -> dict[str, Any]:
    ctx.agent_ee_driver.probe_rust_integration(ctx.native_driver)
    b = BoardBuilder(copper_layers=2, seed=10104)
    b.set_rect_outline(0, 0, 30_000_000, 20_000_000)
    for net, y in (("/SIG_A", 7_000_000), ("/SIG_B", 13_000_000)):
        for idx, x in enumerate((5_000_000, 25_000_000), 1):
            fp = b.add_footprint(f"J_{net[-1]}_{idx}", (x, y))
            b.add_pad(fp, "1", net, (x, y), size_nm=(1_000_000, 1_000_000))
    b.add_track("/SIG_B", (5_000_000, 13_000_000), (25_000_000, 13_000_000), 250_000, locked=False)
    b.set_project_rules()
    bundle = b.save(ctx.work_dir / "src")
    bh = snapshot_source_dir(bundle.directory)
    step1_pcb = ctx.work_dir / "step1" / "routed.kicad_pcb"
    step2_pcb = ctx.work_dir / "step2" / "routed.kicad_pcb"
    step1_pcb.parent.mkdir(parents=True, exist_ok=True)
    step2_pcb.parent.mkdir(parents=True, exist_ok=True)

    p1 = ctx.native_driver.run_native(
        bundle.pcb_path, step1_pcb, extra_args=["--nets", "/SIG_A"]
    )
    assert p1.returncode == 0, f"Step 1 failed: {p1.stderr}"
    p2 = ctx.native_driver.run_native(
        step1_pcb, step2_pcb, extra_args=["--protect", "human", "--nets", "!/SIG_A"]
    )
    assert p2.returncode == 0, f"Step 2 --protect human failed: {p2.stderr}"
    return assert_clean_e2e(
        bundle.pcb_path,
        step2_pcb,
        before_hashes=bh,
        source_dir=bundle.directory,
        check_partition_ratchet=True,
    )


@register_test(
    test_id="T1-F10-05",
    tier=1,
    feature="F10",
    min_stage="M5",
    name="Provenance, engine ownership, Cargo.lock, and license audit check",
)
def t1_f10_05(ctx: DriverContext) -> dict[str, Any]:
    ctx.agent_ee_driver.probe_rust_integration(ctx.native_driver)
    res_cand, data_cand = ctx.scripts_driver.run_ownership_check("rust-candidate")
    assert res_cand.returncode == 0 and data_cand is not None
    assert data_cand.get("inventory_passed") is True
    assert {row["responsibility"] for row in data_cand["paths"]} == {
        "state",
        "geometry_legality",
        "search",
        "orchestration",
        "rip_restore",
        "output",
    }
    argv = ctx.agent_ee_driver.board_route_argv(
        ctx.repo_root, ctx.work_dir / "staged", ctx.work_dir / "out", budget_s=30
    )
    assert len(argv) >= 2 and argv[1] == "route-native", (
        f"agent-ee production entrypoint must emit route-native: {argv}"
    )
    assert not any(str(a).endswith("bin/kicad-route") for a in argv), (
        f"agent-ee production entrypoint must not target legacy bin/kicad-route: {argv}"
    )
    cargo_lock = ctx.repo_root / "rust-engine" / "Cargo.lock"
    assert cargo_lock.is_file()
    assert (ctx.repo_root / "LICENSE").is_file()
    assert (ctx.repo_root / "src" / "kicad_route" / "router" / "LICENSE").is_file()
    origins_script = ctx.agent_ee_driver.agent_ee_root / "scripts" / "check_origins.py"
    origins_proc = run_measured(
        ["/usr/bin/python3", "-B", str(origins_script)],
        cwd=ctx.agent_ee_driver.agent_ee_root,
        work_dir=ctx.work_dir / "origins-metrics",
        timeout_s=30.0,
    )
    assert origins_proc.returncode == 0, f"check_origins.py failed: {origins_proc.stderr}"
    return {"cargo_lock_sha256": sha256_file(cargo_lock), "ownership": "PASS"}


# ============================================================================
# Feature 11 — E2E Acceptance & RE-08 Attribution (T1-F11-01 .. T1-F11-05)
# ============================================================================


@register_test(
    test_id="T1-F11-01",
    tier=1,
    feature="F11",
    min_stage="M5",
    name="fixtures/route_demo end-to-end Rust routing + native grading",
)
def t1_f11_01(ctx: DriverContext) -> dict[str, Any]:
    probe = ctx.native_driver.probe()
    if not probe.supported:
        raise PendingCapability(probe.reason)
    ctx.agent_ee_driver.probe_rust_integration(ctx.native_driver)
    src_dir = stage_fixture_copy(ctx.repo_root / "fixtures" / "route_demo", ctx.work_dir / "src")
    before_hashes = snapshot_source_dir(src_dir)
    pcb_in = src_dir / "route_demo.kicad_pcb"
    out_pcb = ctx.work_dir / "out" / "route_demo.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    proc = ctx.native_driver.run_native(pcb_in, out_pcb, timeout_s=180.0)
    assert_no_crash(proc, "T1-F11-01")
    assert proc.returncode == 0, f"route_demo native route failed: {proc.stderr}"
    return assert_clean_e2e(
        pcb_in,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=src_dir,
        refill_zones=True,
        check_pour_integrity=True,
        check_partition_ratchet=True,
        check_parity=True,
    )


@register_test(
    test_id="T1-F11-02",
    tier=1,
    feature="F11",
    min_stage="M5",
    name="RE-08 phase-attributed CPU/wall/RSS measurement run",
)
def t1_f11_02(ctx: DriverContext) -> dict[str, Any]:
    probe = ctx.native_driver.probe()
    if not probe.supported:
        raise PendingCapability(probe.reason)
    ctx.agent_ee_driver.probe_rust_integration(ctx.native_driver)
    bundle = build_crossing_nets_board(ctx.work_dir / "src", num_nets=6, seed=11102)
    before_hashes = snapshot_source_dir(bundle.directory)
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    proc = ctx.native_driver.run_native(bundle.pcb_path, out_pcb, timeout_s=120.0)
    assert_no_crash(proc, "T1-F11-02")
    assert proc.returncode == 0, f"RE-08 run failed: {proc.stderr}"
    assert proc.metrics is not None
    assert proc.metrics.wall_s >= 0.0
    assert proc.metrics.cpu_s >= 0.0
    assert proc.metrics.max_rss_kib > 0
    summary = parse_last_json_line_safe(proc.stdout) or parse_last_json_line_safe(proc.stderr)
    assert isinstance(summary, dict), (
        f"expected native JSON phase summary: stdout={proc.stdout!r}, stderr={proc.stderr!r}"
    )
    assert summary.get("schema") == "kicad-route/native-operation/1", (
        f"unexpected native operation schema in {summary}"
    )
    raw_phases = summary.get("phases")
    assert isinstance(raw_phases, list), f"expected phases list in {summary}"
    phases: dict[str, float] = {}
    for entry in raw_phases:
        if isinstance(entry, dict) and isinstance(entry.get("phase"), str):
            elapsed = entry.get("elapsed_ms")
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and elapsed >= 0:
                phases[entry["phase"]] = float(elapsed) / 1000.0
    for phase_name in ("native_export", "rust_route", "native_apply_validate"):
        assert phase_name in phases, f"missing or invalid phase timing {phase_name!r} in {raw_phases}"
    routing = summary.get("routing")
    assert isinstance(routing, dict), f"missing routing summary in {summary}"
    for key in ("work_used", "expanded_states"):
        val = routing.get(key)
        assert isinstance(val, int) and not isinstance(val, bool) and val >= 0, (
            f"missing or invalid counter {key!r} in {routing}"
        )
    clean = assert_clean_e2e(
        bundle.pcb_path, out_pcb, before_hashes=before_hashes, source_dir=bundle.directory
    )
    return {
        **clean,
        "wall_s": proc.metrics.wall_s,
        "cpu_s": proc.metrics.cpu_s,
        "max_rss_kib": proc.metrics.max_rss_kib,
        "export_s": phases["native_export"],
        "search_s": phases["rust_route"],
        "apply_drc_s": phases["native_apply_validate"],
        "work_used": routing["work_used"],
        "expanded_states": routing["expanded_states"],
    }


@register_test(
    test_id="T1-F11-03",
    tier=1,
    feature="F11",
    min_stage="M5",
    name="Multi-run geometric and copper-signature determinism on route_demo",
)
def t1_f11_03(ctx: DriverContext) -> dict[str, Any]:
    probe = ctx.native_driver.probe()
    if not probe.supported:
        raise PendingCapability(probe.reason)
    ctx.agent_ee_driver.probe_rust_integration(ctx.native_driver)
    src_dir = stage_fixture_copy(ctx.repo_root / "fixtures" / "route_demo", ctx.work_dir / "src")
    before_hashes = snapshot_source_dir(src_dir)
    pcb_in = src_dir / "route_demo.kicad_pcb"
    out_a = ctx.work_dir / "out_a" / "route_demo.kicad_pcb"
    out_b = ctx.work_dir / "out_b" / "route_demo.kicad_pcb"
    out_a.parent.mkdir(parents=True, exist_ok=True)
    out_b.parent.mkdir(parents=True, exist_ok=True)
    pa = ctx.native_driver.run_native(pcb_in, out_a, timeout_s=180.0)
    pb = ctx.native_driver.run_native(pcb_in, out_b, timeout_s=180.0)
    assert pa.returncode == 0 and pb.returncode == 0
    assert_clean_e2e(
        pcb_in, out_a, before_hashes=before_hashes, source_dir=src_dir, refill_zones=True
    )
    assert_clean_e2e(
        pcb_in, out_b, before_hashes=before_hashes, source_dir=src_dir, refill_zones=True
    )
    pn = _pcbnew()

    def copper_sig(pcb_path: Path) -> str:
        board = pn.LoadBoard(str(pcb_path))
        items = []
        for t in board.GetTracks():
            cls = t.GetClass()
            if cls == "PCB_VIA":
                items.append(("V", t.GetNetname(), t.GetPosition().x, t.GetPosition().y, t.GetWidth(t.TopLayer()), t.GetDrill()))
            elif cls == "PCB_ARC":
                items.append(("A", t.GetNetname(), t.GetLayer(), t.GetStart().x, t.GetStart().y, t.GetMid().x, t.GetMid().y, t.GetEnd().x, t.GetEnd().y, t.GetWidth()))
            else:
                items.append(("T", t.GetNetname(), t.GetLayer(), t.GetStart().x, t.GetStart().y, t.GetEnd().x, t.GetEnd().y, t.GetWidth()))
        items.sort()
        return hashlib.sha256(json.dumps(items).encode("utf-8")).hexdigest()

    sig_a, sig_b = copper_sig(out_a), copper_sig(out_b)
    assert sig_a == sig_b, f"non-deterministic copper signature: {sig_a} != {sig_b}"
    return {"copper_signature": sig_a}


@register_test(
    test_id="T1-F11-04",
    tier=1,
    feature="F11",
    min_stage="M5",
    name="Representative agent-ee owned boards (rc_stage and fab4) routed E2E",
)
def t1_f11_04(ctx: DriverContext) -> dict[str, Any]:
    ctx.agent_ee_driver.probe_rust_integration(ctx.native_driver)
    ae_root = ctx.agent_ee_driver.agent_ee_root
    rc_src = ae_root / "fixtures" / "rc" / "stages" / "placed-unrouted"
    fab4_src = ae_root / "fixtures" / "fab-export" / "fab4.kicad_pcb"
    assert (rc_src / "rc_stage.kicad_pcb").is_file() and fab4_src.is_file()

    rc_dir = stage_fixture_copy(rc_src, ctx.work_dir / "rc_src")
    rc_before = snapshot_source_dir(rc_dir)
    rc_out = ctx.work_dir / "rc_out" / "rc_stage.kicad_pcb"
    rc_out.parent.mkdir(parents=True, exist_ok=True)
    p_rc = ctx.native_driver.run_native(rc_dir / "rc_stage.kicad_pcb", rc_out, timeout_s=180.0)
    assert_no_crash(p_rc, "T1-F11-04:rc")
    assert p_rc.returncode == 0, f"rc_stage failed: {p_rc.stderr}"
    m_rc = assert_clean_e2e(
        rc_dir / "rc_stage.kicad_pcb",
        rc_out,
        before_hashes=rc_before,
        source_dir=rc_dir,
        refill_zones=True,
        check_partition_ratchet=True,
    )

    fab_dir = ctx.work_dir / "fab4_src"
    fab_dir.mkdir(parents=True, exist_ok=True)
    fab_in = fab_dir / "fab4.kicad_pcb"
    shutil.copy2(fab4_src, fab_in)
    fab_before = snapshot_source_dir(fab_dir)
    fab_out = ctx.work_dir / "fab4_out" / "fab4.kicad_pcb"
    fab_out.parent.mkdir(parents=True, exist_ok=True)
    p_fab = ctx.native_driver.run_native(fab_in, fab_out, timeout_s=180.0)
    assert_no_crash(p_fab, "T1-F11-04:fab4")
    assert p_fab.returncode == 0, f"fab4 failed: {p_fab.stderr}"
    m_fab = assert_clean_e2e(
        fab_in,
        fab_out,
        before_hashes=fab_before,
        source_dir=fab_dir,
        check_partition_ratchet=True,
    )
    return {"rc_stage": m_rc, "fab4": m_fab}


@register_test(
    test_id="T1-F11-05",
    tier=1,
    feature="F11",
    min_stage="M5",
    name="RE-10 exact-revision traceability and requirements catalog consistency",
)
def t1_f11_05(ctx: DriverContext) -> dict[str, Any]:
    ctx.agent_ee_driver.probe_rust_integration(ctx.native_driver)
    req_path = ctx.repo_root / "docs" / "routing-requirements.json"
    parity_path = ctx.repo_root / "docs" / "rust-parity.md"
    cargo_toml = ctx.repo_root / "rust-engine" / "Cargo.toml"
    assert req_path.is_file() and parity_path.is_file() and cargo_toml.is_file()
    catalog = json.loads(req_path.read_text(encoding="utf-8"))
    reqs = {r["id"]: r for r in catalog.get("requirements", [])}
    expected_ids = {f"RE-{i:02d}" for i in range(1, 11)}
    assert expected_ids <= set(reqs), f"missing RE ids: {expected_ids - set(reqs)}"
    missing_paths: list[str] = []
    for req_id in sorted(expected_ids):
        for rel in reqs[req_id].get("paths", []):
            p = (ctx.repo_root / rel).resolve()
            if not p.is_relative_to(ctx.repo_root.resolve()) or not p.exists():
                missing_paths.append(f"{req_id}:{rel}")
    seam_rel = (catalog.get("allowed_compatibility_seam") or {}).get("path")
    if not seam_rel or not (ctx.repo_root / seam_rel).is_file():
        missing_paths.append(f"seam:{seam_rel}")
    assert not missing_paths, f"stale or missing paths in routing-requirements.json: {missing_paths}"
    parity_text = parity_path.read_text(encoding="utf-8")
    lines = iter(parity_text.splitlines())
    header = next((line for line in lines if line.strip().startswith("| Capability |")), None)
    assert header is not None, "capability table missing from docs/rust-parity.md"
    separator = next(lines, "").strip()
    assert re.fullmatch(r"\|(?:\s*:?-{3,}:?\s*\|){3}", separator), "invalid capability table separator"
    data_rows = []
    for line in lines:
        if not line.strip().startswith("|"):
            break
        data_rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    assert len(data_rows) >= 9, f"expected >= 9 capability rows in docs/rust-parity.md, got {len(data_rows)}"
    for idx, cols in enumerate(data_rows):
        assert len(cols) == 3 and all(cols), f"malformed parity row {idx}: {cols}"
    for doc_rel in re.findall(r"`(docs/[A-Za-z0-9_.\-/]+\.md)`", parity_text):
        assert (ctx.repo_root / doc_rel).is_file(), f"missing doc referenced in rust-parity.md: {doc_rel}"
    cargo_text = cargo_toml.read_text(encoding="utf-8")
    for dep in ("serde", "serde_json", "sha2"):
        assert re.search(rf"^\s*{re.escape(dep)}\s*=", cargo_text, re.M), f"missing dep {dep} in Cargo.toml"
    assert (ctx.repo_root / "rust-engine" / "Cargo.lock").is_file()
    assert (ctx.repo_root / "LICENSE").is_file()
    assert (ctx.repo_root / "src" / "kicad_route" / "router" / "LICENSE").is_file()
    head = subprocess.run(
        ["git", "-C", str(ctx.repo_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{40}", head)
    return {
        "head": head,
        "requirements_checked": len(expected_ids),
        "parity_rows": len(data_rows),
    }
