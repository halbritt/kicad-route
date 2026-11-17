"""Tier 3 pairwise cross-feature E2E tests (T3-01 .. T3-14, 14 tests)."""

from __future__ import annotations

import json
import signal
from pathlib import Path
from typing import Any

from tests.e2e_rust_router.core_helpers import (
    STD_RULES,
    assert_clean_e2e,
    assert_no_crash,
    assert_source_dir_untouched,
    build_diff_pair_board,
    require_m3_capability,
    require_m4_capability,
    run_m3_board_or_pending,
    run_m4_board_or_pending,
    run_wire_or_pending,
    snapshot_source_dir,
    stage_fixture_copy,
    write_json_file,
)
from tests.e2e_rust_router.drivers import sha256_file
from tests.e2e_rust_router.kicad_verify import (
    BoardBuilder,
    FixtureBundle,
    _pcbnew,
    read_native_connectivity,
    verify_diff_pair_native,
    verify_fromto_length_groups,
)
from tests.e2e_rust_router.runner import DriverContext, PendingCapability, register_test


@register_test(
    test_id="T3-01",
    tier=3,
    feature="F2",
    min_stage="M1",
    name="Pairwise F1+F2: 4-terminal net with oval, roundrect, and 250001 nm incumbent",
    description="Pairs: F1 + F2",
)
def t3_01(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=2, seed=3001)
    b.set_rect_outline(0, 0, 30_000_000, 20_000_000)
    fp1 = b.add_footprint("J1", (4_000_000, 10_000_000))
    b.add_pad(fp1, "1", "SIGNAL", (4_000_000, 10_000_000), shape=pn.PAD_SHAPE_OVAL, size_nm=(2_000_000, 1_000_000), angle_deg=90.0)
    fp2 = b.add_footprint("J2", (12_000_000, 10_000_000))
    b.add_pad(fp2, "1", "SIGNAL", (12_000_000, 10_000_000), shape=pn.PAD_SHAPE_ROUNDRECT, size_nm=(1_600_000, 1_000_000), roundrect_ratio=0.25)
    fp3 = b.add_footprint("J3", (20_000_000, 6_000_000))
    b.add_pad(fp3, "1", "SIGNAL", (20_000_000, 6_000_000), shape=pn.PAD_SHAPE_OVAL, size_nm=(2_000_000, 1_000_000))
    fp4 = b.add_footprint("J4", (26_000_000, 14_000_000))
    b.add_pad(fp4, "1", "SIGNAL", (26_000_000, 14_000_000), shape=pn.PAD_SHAPE_ROUNDRECT, size_nm=(1_600_000, 1_000_000), roundrect_ratio=0.25)
    b.add_track("SIGNAL", (4_000_000, 10_000_000), (11_500_000, 10_000_000), 250_001, locked=True)
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    res, bh = run_wire_or_pending(ctx, bundle.pcb_path, out_pcb, STD_RULES, tag="t3_01")
    assert res.stage_failed is None and res.engine_res is not None
    return assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=bh,
        source_dir=bundle.directory,
        plan_json=res.engine_res.output_json,
        request_bytes=res.export_res.output_bytes,
    )


@register_test(
    test_id="T3-02",
    tier=3,
    feature="F3",
    min_stage="M2",
    name="Pairwise F1+F3: 8-terminal star net in 400-item dense neighborhood + rollback",
    description="Pairs: F1 + F3",
)
def t3_02(ctx: DriverContext) -> dict[str, Any]:
    b = BoardBuilder(copper_layers=2, seed=3002)
    b.set_rect_outline(0, 0, 60_000_000, 60_000_000)
    star_coords = [
        (30_000_000, 30_000_000),
        (10_000_000, 10_000_000),
        (50_000_000, 10_000_000),
        (10_000_000, 50_000_000),
        (50_000_000, 50_000_000),
        (30_000_000, 10_000_000),
        (30_000_000, 50_000_000),
        (10_000_000, 30_000_000),
    ]
    for idx, pos in enumerate(star_coords, 1):
        fp = b.add_footprint(f"JS{idx}", pos)
        b.add_pad(fp, "1", "STAR", pos, size_nm=(1_000_000, 1_000_000))
    obs_fp = b.add_footprint("OBS", (0, 0))
    count = 0
    for gx in range(3, 58, 3):
        for gy in range(3, 58, 3):
            px, py = gx * 1_000_000, gy * 1_000_000
            if any(abs(px - sx) < 2_500_000 and abs(py - sy) < 2_500_000 for sx, sy in star_coords):
                continue
            count += 1
            b.add_pad(obs_fp, f"O{count}", "", (px, py), size_nm=(400_000, 400_000))
            if count >= 400:
                break
        if count >= 400:
            break
    bundle = b.save(ctx.work_dir / "src")
    out_ok1 = ctx.work_dir / "out_ok1" / "routed.kicad_pcb"
    out_fail = ctx.work_dir / "out_fail" / "routed.kicad_pcb"
    out_ok2 = ctx.work_dir / "out_ok2" / "routed.kicad_pcb"
    for p in (out_ok1, out_fail, out_ok2):
        p.parent.mkdir(parents=True, exist_ok=True)
    res1, bh1 = run_wire_or_pending(ctx, bundle.pcb_path, out_ok1, STD_RULES, tag="t3_02_ok1", max_expansions=500_000)
    assert res1.stage_failed is None and res1.engine_res is not None
    metrics = assert_clean_e2e(
        bundle.pcb_path,
        out_ok1,
        before_hashes=bh1,
        source_dir=bundle.directory,
        plan_json=res1.engine_res.output_json,
        request_bytes=res1.export_res.output_bytes,
    )
    res_fail, _ = run_wire_or_pending(
        ctx, bundle.pcb_path, out_fail, STD_RULES, tag="t3_02_fail", max_expansions=500_000, limits={"max_work": 10}
    )
    assert res_fail.stage_failed == "engine", f"expected tight-budget fail, got {res_fail.stage_failed}"
    assert not out_fail.exists(), f"tight-budget run unexpectedly wrote {out_fail}"
    res2, _ = run_wire_or_pending(ctx, bundle.pcb_path, out_ok2, STD_RULES, tag="t3_02_ok2", max_expansions=500_000)
    assert res2.stage_failed is None and res2.engine_res is not None
    assert res1.engine_res.output_bytes == res2.engine_res.output_bytes
    return metrics


@register_test(
    test_id="T3-03",
    tier=3,
    feature="F3",
    min_stage="M2",
    name="Pairwise F2+F3: 200 rotated oval/roundrect pads in dense neighborhood",
    description="Pairs: F2 + F3",
)
def t3_03(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=2, seed=3003)
    b.set_rect_outline(0, 0, 50_000_000, 50_000_000)
    for i in range(4):
        y = 10_000_000 + i * 10_000_000
        fp1 = b.add_footprint(f"JA{i}", (5_000_000, y))
        b.add_pad(fp1, "1", f"SIG{i}", (5_000_000, y), shape=pn.PAD_SHAPE_OVAL, size_nm=(1_600_000, 800_000), angle_deg=90.0)
        fp2 = b.add_footprint(f"JB{i}", (45_000_000, y))
        b.add_pad(fp2, "1", f"SIG{i}", (45_000_000, y), shape=pn.PAD_SHAPE_ROUNDRECT, size_nm=(1_600_000, 800_000), angle_deg=90.0, roundrect_ratio=0.25)
    obs = b.add_footprint("OBS", (0, 0))
    idx = 0
    for gx in range(10, 42, 3):
        for gy in range(4, 48, 2):
            if gy in (10, 20, 30, 40):
                continue
            idx += 1
            shp = pn.PAD_SHAPE_OVAL if idx % 2 == 0 else pn.PAD_SHAPE_ROUNDRECT
            b.add_pad(obs, f"P{idx}", "", (gx * 1_000_000, gy * 1_000_000), shape=shp, size_nm=(900_000, 500_000), angle_deg=90.0, roundrect_ratio=0.25)
            if idx >= 200:
                break
        if idx >= 200:
            break
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    res, bh = run_wire_or_pending(ctx, bundle.pcb_path, out_pcb, STD_RULES, tag="t3_03", max_expansions=500_000)
    assert res.stage_failed is None and res.engine_res is not None
    return assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=bh,
        source_dir=bundle.directory,
        plan_json=res.engine_res.output_json,
        request_bytes=res.export_res.output_bytes,
    )


@register_test(
    test_id="T3-04",
    tier=3,
    feature="F4",
    min_stage="M2",
    name="Pairwise F3+F4: 500-item dense board via native orchestration + O_EXCL + cancel",
    description="Pairs: F3 + F4",
)
def t3_04(ctx: DriverContext) -> dict[str, Any]:
    probe = ctx.native_driver.probe()
    if not probe.supported:
        raise PendingCapability(probe.reason, evidence={"probe": probe.reason})
    b = BoardBuilder(copper_layers=2, seed=3004)
    b.set_rect_outline(0, 0, 80_000_000, 80_000_000)
    for idx, x in enumerate((6_000_000, 74_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 40_000_000))
        b.add_pad(fp, "1", "SIGNAL", (x, 40_000_000), size_nm=(1_000_000, 1_000_000))
    obs = b.add_footprint("OBS", (0, 0))
    cnt = 0
    for gx in range(12, 68, 2):
        for gy in range(6, 76, 3):
            if abs(gy - 40) <= 3:
                continue
            cnt += 1
            b.add_pad(obs, f"D{cnt}", "", (gx * 1_000_000, gy * 1_000_000), size_nm=(400_000, 400_000))
            if cnt >= 500:
                break
        if cnt >= 500:
            break
    bundle = b.save(ctx.work_dir / "src")
    rules_p = write_json_file(ctx.work_dir / "rules.json", STD_RULES)
    bh = snapshot_source_dir(bundle.directory)

    # (a) Clean native run
    out_ok = ctx.work_dir / "out" / "ok.kicad_pcb"
    out_ok.parent.mkdir(parents=True, exist_ok=True)
    p_ok = ctx.native_driver.run_native(bundle.pcb_path, out_ok, rules_path=rules_p, max_expansions=500_000)
    assert p_ok.returncode == 0
    metrics = assert_clean_e2e(bundle.pcb_path, out_ok, before_hashes=bh, source_dir=bundle.directory)

    # (b) Existing destination O_EXCL refusal
    sentinel = b"EXISTING-SENTINEL\n"
    out_excl = ctx.work_dir / "out" / "existing.kicad_pcb"
    out_excl.write_bytes(sentinel)
    p_excl = ctx.native_driver.run_native(bundle.pcb_path, out_excl, rules_path=rules_p)
    assert p_excl.returncode == 8 and out_excl.read_bytes() == sentinel

    # (c) SIGTERM cancellation leaves no partial destination
    out_sig = ctx.work_dir / "out" / "sigterm.kicad_pcb"
    p_sig = ctx.native_driver.run_native(
        bundle.pcb_path, out_sig, rules_path=rules_p, cancel_after_s=0.01, cancel_signal=signal.SIGTERM
    )
    if p_sig.returncode == 0:
        assert_clean_e2e(bundle.pcb_path, out_sig, before_hashes=bh, source_dir=bundle.directory)
    else:
        assert not out_sig.exists(), f"killed process left partial destination: {out_sig}"
    assert_source_dir_untouched(bundle.directory, bh)
    return metrics


@register_test(
    test_id="T3-05",
    tier=3,
    feature="F5",
    min_stage="M3",
    name="Pairwise F1+F5: 5-terminal net with 45-deg SMD, slotted PTH, and NPTH obstacle",
    description="Pairs: F1 + F5",
)
def t3_05(ctx: DriverContext) -> dict[str, Any]:
    require_m3_capability(ctx)
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=2, seed=3005)
    b.set_rect_outline(0, 0, 35_000_000, 25_000_000)
    fp1 = b.add_footprint("J1", (5_000_000, 12_000_000))
    b.add_pad(fp1, "1", "SIGNAL", (5_000_000, 12_000_000), shape=pn.PAD_SHAPE_RECT, size_nm=(1_500_000, 800_000), angle_deg=45.0)
    fp2 = b.add_footprint("J2", (13_000_000, 7_000_000))
    b.add_pad(fp2, "1", "SIGNAL", (13_000_000, 7_000_000), attrib=pn.PAD_ATTRIB_PTH, shape=pn.PAD_SHAPE_OVAL, size_nm=(2_200_000, 1_400_000), drill_shape=pn.PAD_DRILL_SHAPE_OBLONG, drill_size_nm=(1_200_000, 600_000))
    for idx, pos in enumerate(((13_000_000, 18_000_000), (24_000_000, 7_000_000), (30_000_000, 18_000_000)), 3):
        fp = b.add_footprint(f"J{idx}", pos)
        b.add_pad(fp, "1", "SIGNAL", pos, size_nm=(1_000_000, 1_000_000))
    npth = b.add_footprint("H1", (20_000_000, 12_000_000))
    b.add_pad(npth, "1", "", (20_000_000, 12_000_000), attrib=pn.PAD_ATTRIB_NPTH, size_nm=(2_000_000, 2_000_000), drill_size_nm=(2_000_000, 2_000_000))
    b.set_project_rules()
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m3_board_or_pending(ctx, bundle, out_pcb, tag="t3_05")
    assert (res.returncode == 0 if mode == "native" else res.stage_failed is None)
    return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)


@register_test(
    test_id="T3-06",
    tier=3,
    feature="F6",
    min_stage="M3",
    name="Pairwise F5+F6: 4-layer L-shaped Edge.Cuts + hole_clearance + Power via span",
    description="Pairs: F5 + F6",
)
def t3_06(ctx: DriverContext) -> dict[str, Any]:
    require_m3_capability(ctx)
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=4, seed=3006)
    b.set_polygon_outline([(0, 0), (30_000_000, 0), (30_000_000, 15_000_000), (15_000_000, 15_000_000), (15_000_000, 30_000_000), (0, 30_000_000)])
    fp1 = b.add_footprint("J1", (5_000_000, 24_000_000))
    b.add_pad(fp1, "1", "PWR", (5_000_000, 24_000_000), size_nm=(1_200_000, 1_200_000))
    fp2 = b.add_footprint("J2", (24_000_000, 5_000_000), side="B")
    b.add_pad(fp2, "1", "PWR", (24_000_000, 5_000_000), size_nm=(1_200_000, 1_200_000), layers=[pn.B_Cu])
    npth = b.add_footprint("H1", (10_000_000, 10_000_000))
    b.add_pad(npth, "1", "", (10_000_000, 10_000_000), attrib=pn.PAD_ATTRIB_NPTH, size_nm=(2_000_000, 2_000_000), drill_size_nm=(2_000_000, 2_000_000))
    b.set_project_rules(
        classes=[
            {"name": "Default", "priority": 0, "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "Power", "priority": 1, "clearance": 0.3, "track_width": 0.5, "via_diameter": 0.8, "via_drill": 0.4},
        ],
        netclass_patterns=[{"netclass": "Power", "pattern": "PWR"}],
    )
    b.set_dru_text('(version 1)\n(rule "hole_clr" (constraint hole_clearance (min 0.35mm)))\n')
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m3_board_or_pending(ctx, bundle, out_pcb, tag="t3_06")
    assert (res.returncode == 0 if mode == "native" else res.stage_failed is None)
    return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)


@register_test(
    test_id="T3-07",
    tier=3,
    feature="F7",
    min_stage="M3",
    name="Pairwise F6+F7: via-history search with no_vias keepout and In1.Cu GND zone",
    description="Pairs: F6 + F7",
)
def t3_07(ctx: DriverContext) -> dict[str, Any]:
    require_m3_capability(ctx)
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=4, seed=3007)
    b.set_rect_outline(0, 0, 30_000_000, 20_000_000)
    fp1 = b.add_footprint("J1", (4_000_000, 10_000_000))
    b.add_pad(fp1, "1", "SIG", (4_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000))
    fp2 = b.add_footprint("J2", (26_000_000, 10_000_000), side="B")
    b.add_pad(fp2, "1", "SIG", (26_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000), layers=[pn.B_Cu])
    b.add_keepout([pn.F_Cu], [(3_000_000, 3_000_000), (15_000_000, 3_000_000), (15_000_000, 17_000_000), (3_000_000, 17_000_000)], no_tracks=False, no_vias=True, no_zones=False)
    b.add_zone("GND", [pn.In1_Cu], [(1_000_000, 1_000_000), (29_000_000, 1_000_000), (29_000_000, 19_000_000), (1_000_000, 19_000_000)], zone_name="GND_IN1")
    b.set_project_rules()
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m3_board_or_pending(ctx, bundle, out_pcb, tag="t3_07")
    assert (res.returncode == 0 if mode == "native" else res.stage_failed is None)
    return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory, refill_zones=True, check_pour_integrity=True)


@register_test(
    test_id="T3-08",
    tier=3,
    feature="F7",
    min_stage="M3",
    name="Pairwise F1+F7: 6-terminal GND net (4 SMD + 2 thermal PTH) + SIG routes",
    description="Pairs: F1 + F7",
)
def t3_08(ctx: DriverContext) -> dict[str, Any]:
    require_m3_capability(ctx)
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=2, seed=3008)
    b.set_design_settings(min_resolved_spokes=2)
    b.set_rect_outline(0, 0, 40_000_000, 30_000_000)
    for idx, x in enumerate((8_000_000, 32_000_000), 1):
        fp = b.add_footprint(f"JP{idx}", (x, 8_000_000))
        b.add_pad(fp, "1", "GND", (x, 8_000_000), attrib=pn.PAD_ATTRIB_PTH, size_nm=(1_700_000, 1_700_000), drill_size_nm=(800_000, 800_000))
    for idx, pos in enumerate(((8_000_000, 22_000_000), (16_000_000, 22_000_000), (24_000_000, 22_000_000), (32_000_000, 22_000_000)), 1):
        fp = b.add_footprint(f"JS{idx}", pos)
        b.add_pad(fp, "1", "GND", pos, size_nm=(1_000_000, 1_000_000))
    for idx, (x, y) in enumerate(((5_000_000, 15_000_000), (35_000_000, 15_000_000)), 1):
        fp = b.add_footprint(f"JA{idx}", (x, y))
        b.add_pad(fp, "1", "SIGA", (x, y), size_nm=(1_000_000, 1_000_000))
    b.add_zone("GND", [pn.B_Cu], [(2_000_000, 2_000_000), (38_000_000, 2_000_000), (38_000_000, 28_000_000), (2_000_000, 28_000_000)], zone_name="GND_B")
    b.set_project_rules()
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m3_board_or_pending(ctx, bundle, out_pcb, tag="t3_08")
    assert (res.returncode == 0 if mode == "native" else res.stage_failed is None)
    return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory, refill_zones=True, check_pour_integrity=True)


@register_test(
    test_id="T3-09",
    tier=3,
    feature="F8",
    min_stage="M4",
    name="Pairwise F3+F8: 300-item dense board with 4 crossing nets + rip-up rollback",
    description="Pairs: F3 + F8",
)
def t3_09(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx)
    b = BoardBuilder(copper_layers=2, seed=3009)
    b.set_rect_outline(0, 0, 50_000_000, 50_000_000)
    for i in range(4):
        y0 = 15_000_000 + i * 5_000_000
        y1 = 30_000_000 - i * 5_000_000
        fp1 = b.add_footprint(f"JA{i}", (6_000_000, y0))
        b.add_pad(fp1, "1", f"SIG_{i}", (6_000_000, y0), size_nm=(1_000_000, 1_000_000))
        fp2 = b.add_footprint(f"JB{i}", (44_000_000, y1))
        b.add_pad(fp2, "1", f"SIG_{i}", (44_000_000, y1), size_nm=(1_000_000, 1_000_000))
    obs = b.add_footprint("OBS", (0, 0))
    cnt = 0
    for gx in range(10, 42, 2):
        for gy in range(4, 48, 2):
            if 13 <= gy <= 32:
                continue
            cnt += 1
            b.add_pad(obs, f"D{cnt}", "", (gx * 1_000_000, gy * 1_000_000), size_nm=(400_000, 400_000))
            if cnt >= 300:
                break
        if cnt >= 300:
            break
    b.set_project_rules()
    bundle = b.save(ctx.work_dir / "src")
    out_fail = ctx.work_dir / "out_fail" / "routed.kicad_pcb"
    out_ok1 = ctx.work_dir / "out_ok1" / "routed.kicad_pcb"
    out_ok2 = ctx.work_dir / "out_ok2" / "routed.kicad_pcb"
    for p in (out_fail, out_ok1, out_ok2):
        p.parent.mkdir(parents=True, exist_ok=True)
    m_fail, r_fail, _ = run_m4_board_or_pending(
        ctx, bundle, out_fail, tag="t3_09_fail", max_expansions=5, limits={"max_work": 10}
    )
    assert (r_fail.returncode != 0 if m_fail == "native" else r_fail.stage_failed is not None), (
        "tight-budget pre-run unexpectedly succeeded"
    )
    assert not out_fail.exists(), f"tight-budget pre-run unexpectedly wrote {out_fail}"
    m1, r1, bh1 = run_m4_board_or_pending(ctx, bundle, out_ok1, tag="t3_09_ok1", limits={"max_work": 5_000_000})
    m2, r2, _ = run_m4_board_or_pending(ctx, bundle, out_ok2, tag="t3_09_ok2", limits={"max_work": 5_000_000})
    assert m1 == m2
    if m1 == "wire":
        assert r1.engine_res is not None and r2.engine_res is not None
        assert r1.engine_res.output_bytes == r2.engine_res.output_bytes
    else:
        assert sha256_file(out_ok1) == sha256_file(out_ok2)
    return assert_clean_e2e(bundle.pcb_path, out_ok1, before_hashes=bh1, source_dir=bundle.directory)


@register_test(
    test_id="T3-10",
    tier=3,
    feature="F8",
    min_stage="M4",
    name="Pairwise F6+F8: rip-up preserves Power class widths/clearances",
    description="Pairs: F6 + F8",
)
def t3_10(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx)
    b = BoardBuilder(copper_layers=4, seed=3010)
    b.set_rect_outline(0, 0, 30_000_000, 25_000_000)
    for idx, pos in enumerate(((4_000_000, 12_000_000), (26_000_000, 12_000_000)), 1):
        fp = b.add_footprint(f"JP{idx}", pos)
        b.add_pad(fp, "1", "PWR", pos, size_nm=(1_200_000, 1_200_000))
    for idx, pos in enumerate(((15_000_000, 4_000_000), (15_000_000, 21_000_000)), 1):
        fp = b.add_footprint(f"JS{idx}", pos)
        b.add_pad(fp, "1", "SIG", pos, size_nm=(1_000_000, 1_000_000))
    b.set_project_rules(
        classes=[
            {"name": "Default", "priority": 0, "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "Power", "priority": 1, "clearance": 0.4, "track_width": 0.5, "via_diameter": 0.8, "via_drill": 0.4},
        ],
        netclass_patterns=[{"netclass": "Power", "pattern": "PWR"}],
    )
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m4_board_or_pending(ctx, bundle, out_pcb, tag="t3_10")
    assert (res.returncode == 0 if mode == "native" else res.stage_failed is None)
    metrics = assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)
    pn = _pcbnew()
    board = pn.LoadBoard(str(out_pcb))
    for t in board.GetTracks():
        if t.GetNetname() == "PWR" and t.GetClass() == "PCB_TRACK":
            assert int(t.GetWidth()) >= 500_000
    return metrics


@register_test(
    test_id="T3-11",
    tier=3,
    feature="F8",
    min_stage="M4",
    name="Pairwise F7+F8: rip-up near B.Cu GND pour preserves thermals & keepout",
    description="Pairs: F7 + F8",
)
def t3_11(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx)
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=2, seed=3011)
    b.set_design_settings(min_resolved_spokes=2)
    b.set_rect_outline(0, 0, 40_000_000, 30_000_000)
    for idx, x in enumerate((8_000_000, 32_000_000), 1):
        fp = b.add_footprint(f"JG{idx}", (x, 7_000_000))
        b.add_pad(fp, "1", "GND", (x, 7_000_000), attrib=pn.PAD_ATTRIB_PTH, size_nm=(1_700_000, 1_700_000), drill_size_nm=(800_000, 800_000))
    for i in range(3):
        y0 = 14_000_000 + i * 4_000_000
        y1 = 22_000_000 - i * 4_000_000
        fp1 = b.add_footprint(f"JA{i}", (6_000_000, y0))
        b.add_pad(fp1, "1", f"SIG{i}", (6_000_000, y0), size_nm=(1_000_000, 1_000_000))
        fp2 = b.add_footprint(f"JB{i}", (34_000_000, y1))
        b.add_pad(fp2, "1", f"SIG{i}", (34_000_000, y1), size_nm=(1_000_000, 1_000_000))
    b.add_zone("GND", [pn.B_Cu], [(2_000_000, 2_000_000), (38_000_000, 2_000_000), (38_000_000, 28_000_000), (2_000_000, 28_000_000)], zone_name="GND_B")
    b.add_keepout([pn.F_Cu], [(18_000_000, 2_000_000), (22_000_000, 2_000_000), (22_000_000, 12_000_000), (18_000_000, 12_000_000)], no_tracks=True, no_vias=True, no_zones=False)
    b.set_project_rules()
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m4_board_or_pending(ctx, bundle, out_pcb, tag="t3_11")
    assert (res.returncode == 0 if mode == "native" else res.stage_failed is None)
    return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory, refill_zones=True, check_pour_integrity=True)


@register_test(
    test_id="T3-12",
    tier=3,
    feature="F9",
    min_stage="M4",
    name="Pairwise F6+F9: 4-layer stackup rules + diff-pair transition + fromTo delay",
    description="Pairs: F6 + F9",
)
def t3_12(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx, require_help_flags=("--diff-pairs", "--pair-layers"))
    src_dir = stage_fixture_copy(ctx.repo_root / "fixtures" / "pair_transition", ctx.work_dir / "src")
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
        extra_args=[
            "--diff-pairs",
            "auto",
            "--pair-layers",
            "F.Cu",
            "In1.Cu",
            "In2.Cu",
            "B.Cu",
            "--length-match-group",
            "CAN+",
            "CAN-",
            "--group-match-tolerance",
            "0.15",
        ],
        require_help_flags=("--diff-pairs", "--pair-layers", "--length-match-group"),
        tag="t3_12",
    )
    assert mode == "native" and res.returncode == 0
    metrics = assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)
    dp = verify_diff_pair_native(bundle.pcb_path, out_pcb, [("CAN+", "CAN-")])
    ft = verify_fromto_length_groups(out_pcb, [["CAN+", "CAN-"]], tolerance_mm=0.15, sidecar_source=bundle.pcb_path)
    assert ft.passed, f"T3-12 fromTo failed: {ft.skew_violations}"
    return {**metrics, "diff_pair": dp}


@register_test(
    test_id="T3-13",
    tier=3,
    feature="F9",
    min_stage="M4",
    name="Pairwise F8+F9: coupled diff pair + 4 crossing single-ended rip-up nets",
    description="Pairs: F8 + F9",
)
def t3_13(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx, require_help_flags=("--diff-pairs",))
    b = BoardBuilder(copper_layers=2, seed=3013)
    b.set_rect_outline(0, 0, 35_000_000, 25_000_000)
    for ref, x in (("J1", 5_000_000), ("J2", 30_000_000)):
        fp = b.add_footprint(ref, (x, 12_000_000))
        b.add_pad(fp, "1", "CAN+", (x, 12_000_000), size_nm=(400_000, 400_000))
        b.add_pad(fp, "2", "CAN-", (x, 12_600_000), size_nm=(400_000, 400_000))
    for i in range(4):
        x0 = 10_000_000 + i * 4_500_000
        fp1 = b.add_footprint(f"JS{i}A", (x0, 5_000_000))
        b.add_pad(fp1, "1", f"SE_{i}", (x0, 5_000_000), size_nm=(800_000, 800_000))
        fp2 = b.add_footprint(f"JS{i}B", (x0 + 1_000_000, 20_000_000))
        b.add_pad(fp2, "1", f"SE_{i}", (x0 + 1_000_000, 20_000_000), size_nm=(800_000, 800_000))
    b.set_project_rules(
        classes=[
            {
                "name": "Default",
                "priority": 0,
                "clearance": 0.2,
                "track_width": 0.25,
                "via_diameter": 0.6,
                "via_drill": 0.3,
                "diff_pair_width": 0.25,
                "diff_pair_gap": 0.35,
                "diff_pair_via_gap": 0.35,
            }
        ]
    )
    b.set_dru_text('(version 1)\n(rule "pair_gap" (constraint diff_pair_gap (min 0.34mm) (max 0.36mm)))\n')
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m4_board_or_pending(
        ctx, bundle, out_pcb, extra_args=["--diff-pairs", "auto"], require_help_flags=("--diff-pairs",), tag="t3_13"
    )
    assert mode == "native" and res.returncode == 0
    metrics = assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)
    dp = verify_diff_pair_native(bundle.pcb_path, out_pcb, [("CAN+", "CAN-")])
    return {**metrics, "diff_pair": dp}


@register_test(
    test_id="T3-14",
    tier=3,
    feature="F10",
    min_stage="M5",
    name="Pairwise F4+F10: agent-ee sandboxed routing_adapter executes Rust native orchestration",
    description="Pairs: F4 + F10",
)
def t3_14(ctx: DriverContext) -> dict[str, Any]:
    ctx.agent_ee_driver.probe_rust_integration(ctx.native_driver)
    ae_root = ctx.agent_ee_driver.agent_ee_root
    placed_src = ae_root / "fixtures" / "rc" / "stages" / "placed-unrouted"
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
    name='rc', label='e2e-t3-14', checkout=str(checkout), state=str(state), budget=120,
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
report = json.loads((work_root / 'routing/adapter-report.json').read_text(encoding='utf-8'))
print(json.dumps({'exit_code': res['exit_code'], 'summary': summary, 'state_write_probe': report['state_write_probe']}))
"""
    proc = ctx.agent_ee_driver.run_python(
        script, str(cand_root), str(work_root), str(checkout), str(state), timeout_s=300.0
    )
    assert_no_crash(proc, "T3-14")
    assert proc.returncode == 0, f"T3-14 failed: {proc.stderr}"
    assert_source_dir_untouched(placed_dir, before_hashes)
    payload = json.loads(proc.stdout)
    assert payload["exit_code"] == 0, f"T3-14 executor exit_code={payload['exit_code']}: {payload}"
    assert payload["summary"]["proposal"] == "eligible"
    assert all(p.get("refused") is True for p in payload["state_write_probe"])
    cand_pcb = work_root / "routing" / "candidate" / "placed" / "rc_stage.kicad_pcb"
    return assert_clean_e2e(
        placed_dir / "rc_stage.kicad_pcb",
        cand_pcb,
        before_hashes=before_hashes,
        source_dir=placed_dir,
        refill_zones=True,
    )
