"""Tier 2 boundary, corner, and negative E2E tests for Features 7-11 (27 tests)."""

from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
from typing import Any

from tests.e2e_rust_router.core_helpers import (
    STD_RULES,
    assert_clean_e2e,
    assert_no_crash,
    assert_source_dir_untouched,
    build_crossing_nets_board,
    build_diff_pair_board,
    build_keepout_board,
    build_pour_board,
    build_std_2pad_board,
    canonical_uuid,
    parse_last_json_line_safe,
    require_m3_capability,
    require_m4_capability,
    run_m3_board_direct,
    run_m4_board_or_pending,
    snapshot_source_dir,
    stage_fixture_copy,
    write_json_file,
)
from tests.e2e_rust_router.drivers import sha256_file
from tests.e2e_rust_router.kicad_verify import (
    BoardBuilder,
    FixtureBundle,
    _pcbnew,
    find_split_partitions,
    run_native_drc,
    verify_fromto_length_groups,
    verify_pour_integrity,
)
from tests.e2e_rust_router.runner import DriverContext, PendingCapability, register_test


# ============================================================================
# Feature 7 — Zones, Planes, Thermals & Keepouts (T2-F7-01 .. T2-F7-06)
# ============================================================================


@register_test(
    test_id="T2-F7-01",
    tier=2,
    feature="F7",
    min_stage="M3",
    name="Full-span keepout wall makes net unroutable",
)
def t2_f7_01(ctx: DriverContext) -> dict[str, Any]:
    m3_mode = require_m3_capability(ctx)
    bundle = build_keepout_board(ctx.work_dir / "src", mode="full_wall", seed=7201)
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, _ = run_m3_board_direct(ctx, bundle, out_pcb, mode=m3_mode, tag="f7_01")
    if mode == "native":
        assert res.returncode == 3, f"expected exit 3 unroutable, got {res.returncode}: {res.stderr}"
    else:
        assert res.stage_failed == "engine" and res.engine_res is not None
        assert res.engine_res.proc.returncode == 3
        oj = res.engine_res.output_json or {}
        assert oj.get("status") == "unroutable"
        assert oj.get("added_tracks") == []
    assert not out_pcb.exists()
    return {"checked": "full_span_keepout_unroutable"}


@register_test(
    test_id="T2-F7-02",
    tier=2,
    feature="F7",
    min_stage="M3",
    name="Pour isolated_island negative validation control",
)
def t2_f7_02(ctx: DriverContext) -> dict[str, Any]:
    require_m3_capability(ctx)
    pn = _pcbnew()
    bundle = build_pour_board(ctx.work_dir / "src", layers=2, min_resolved_spokes=2, seed=7202)
    cand_dir = ctx.work_dir / "cand"
    stage_fixture_copy(bundle.directory, cand_dir)
    cand_pcb = cand_dir / bundle.pcb_path.name
    # Slice B.Cu GND pour completely from y=0..20mm at x=15mm with a SIG track.
    board = pn.LoadBoard(str(cand_pcb))
    tr = pn.PCB_TRACK(board)
    tr.SetStart(pn.VECTOR2I(15_000_000, 1_000_000))
    tr.SetEnd(pn.VECTOR2I(15_000_000, 19_000_000))
    tr.SetWidth(500_000)
    tr.SetLayer(pn.B_Cu)
    tr.SetNetCode(board.FindNet("SIG").GetNetCode())
    board.Add(tr)
    pn.SaveBoard(str(cand_pcb), board, True)
    rep = verify_pour_integrity(bundle.pcb_path, cand_pcb)
    assert not rep.passed
    kinds = {f["kind"] for f in rep.failures}
    assert "isolated_island" in kinds, f"expected isolated_island, got {rep.failures}"
    return {"detected": sorted(kinds)}


@register_test(
    test_id="T2-F7-03",
    tier=2,
    feature="F7",
    min_stage="M3",
    name="Pour spokes_below_minimum and lost_fill_contact negative controls",
)
def t2_f7_03(ctx: DriverContext) -> dict[str, Any]:
    require_m3_capability(ctx)
    pn = _pcbnew()
    bundle = build_pour_board(ctx.work_dir / "src", layers=2, min_resolved_spokes=2, seed=7203)
    # J1.2 is at (6mm, 7mm), pad 1.7mm dia. Place 3 vs 4 diagonal SIG stubs crowding thermal spokes.
    detected: dict[str, set[str]] = {}
    for label, offsets in (
        ("three_blocked", [(1_400_000, 1_400_000), (-1_400_000, 1_400_000), (-1_400_000, -1_400_000)]),
        (
            "four_blocked",
            [
                (1_400_000, 1_400_000),
                (-1_400_000, 1_400_000),
                (-1_400_000, -1_400_000),
                (1_400_000, -1_400_000),
            ],
        ),
    ):
        cand_dir = ctx.work_dir / label
        stage_fixture_copy(bundle.directory, cand_dir)
        cand_pcb = cand_dir / bundle.pcb_path.name
        board = pn.LoadBoard(str(cand_pcb))
        sig_code = board.FindNet("SIG").GetNetCode()
        for dx, dy in offsets:
            tr = pn.PCB_TRACK(board)
            tr.SetStart(pn.VECTOR2I(6_000_000 + dx, 7_000_000 + dy))
            tr.SetEnd(pn.VECTOR2I(6_000_000 + dx + (300_000 if dx > 0 else -300_000), 7_000_000 + dy))
            tr.SetWidth(400_000)
            tr.SetLayer(pn.B_Cu)
            tr.SetNetCode(sig_code)
            board.Add(tr)
        pn.SaveBoard(str(cand_pcb), board, True)
        rep = verify_pour_integrity(bundle.pcb_path, cand_pcb)
        assert not rep.passed, f"expected failure for {label}"
        detected[label] = {f["kind"] for f in rep.failures}
    assert "spokes_below_minimum" in detected["three_blocked"], detected
    assert "lost_fill_contact" in detected["four_blocked"], detected
    return {"three_blocked": sorted(detected["three_blocked"]), "four_blocked": sorted(detected["four_blocked"])}


@register_test(
    test_id="T2-F7-04",
    tier=2,
    feature="F7",
    min_stage="M3",
    name="Pour lost_pad_reach and --pour-area-loss threshold breach",
)
def t2_f7_04(ctx: DriverContext) -> dict[str, Any]:
    require_m3_capability(ctx)
    pn = _pcbnew()
    bundle = build_pour_board(ctx.work_dir / "src", layers=2, min_resolved_spokes=2, seed=7204)

    # (a) Shrink zone outline so J2.2 (at 24mm, 7mm) is excluded -> lost_pad_reach
    cand_a_dir = ctx.work_dir / "cand_reach"
    stage_fixture_copy(bundle.directory, cand_a_dir)
    cand_a_pcb = cand_a_dir / bundle.pcb_path.name
    ba = pn.LoadBoard(str(cand_a_pcb))
    z = [item for item in ba.Zones() if not item.GetIsRuleArea()][0]
    z.Outline().RemoveAllContours()
    z.Outline().NewOutline()
    for x, y in ((1_000_000, 1_000_000), (20_000_000, 1_000_000), (20_000_000, 19_000_000), (1_000_000, 19_000_000)):
        z.Outline().Append(x, y)
    pn.SaveBoard(str(cand_a_pcb), ba, True)
    rep_a = verify_pour_integrity(bundle.pcb_path, cand_a_pcb)
    assert not rep_a.passed and "lost_pad_reach" in {f["kind"] for f in rep_a.failures}, rep_a.failures

    # (b) Cut a >5% area strip near y=17mm (above all GND pads at y=7mm) -> passes default, fails 1% cap
    cand_b_dir = ctx.work_dir / "cand_area"
    stage_fixture_copy(bundle.directory, cand_b_dir)
    cand_b_pcb = cand_b_dir / bundle.pcb_path.name
    bb = pn.LoadBoard(str(cand_b_pcb))
    tr = pn.PCB_TRACK(bb)
    tr.SetStart(pn.VECTOR2I(1_000_000, 16_000_000))
    tr.SetEnd(pn.VECTOR2I(29_000_000, 16_000_000))
    tr.SetWidth(500_000)
    tr.SetLayer(pn.B_Cu)
    tr.SetNetCode(bb.FindNet("SIG").GetNetCode())
    bb.Add(tr)
    pn.SaveBoard(str(cand_b_pcb), bb, True)
    rep_b_default = verify_pour_integrity(bundle.pcb_path, cand_b_pcb)
    assert rep_b_default.passed, f"unexpected default failure: {rep_b_default.failures}"
    rep_b_strict = verify_pour_integrity(bundle.pcb_path, cand_b_pcb, area_loss_pct=1.0)
    assert not rep_b_strict.passed and "area_loss" in {f["kind"] for f in rep_b_strict.failures}
    return {"checked": ["lost_pad_reach", "area_loss"]}


@register_test(
    test_id="T2-F7-05",
    tier=2,
    feature="F7",
    min_stage="M3",
    name="Duplicate all-zero UUID rule areas refused fail-closed",
)
def t2_f7_05(ctx: DriverContext) -> dict[str, Any]:
    m3_mode = require_m3_capability(ctx)
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=2, seed=7205)
    b.set_rect_outline(0, 0, 30_000_000, 20_000_000)
    for idx, x in enumerate((5_000_000, 25_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
        b.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), size_nm=(1_000_000, 1_000_000))
    z1 = b.add_keepout([pn.F_Cu], [(10_000_000, 4_000_000), (12_000_000, 4_000_000), (12_000_000, 8_000_000), (10_000_000, 8_000_000)])
    z2 = b.add_keepout([pn.F_Cu], [(14_000_000, 4_000_000), (16_000_000, 4_000_000), (16_000_000, 8_000_000), (14_000_000, 8_000_000)])
    dup_uuid = "00000000-0000-0000-0000-000000000000"
    z1.SetUuid(pn.KIID(dup_uuid))
    z2.SetUuid(pn.KIID(dup_uuid))
    b.set_project_rules()
    bundle = b.save(ctx.work_dir / "src")
    text = bundle.pcb_path.read_text(encoding="utf-8")
    # Ensure duplicate all-zero UUID survives SaveBoard in raw S-expr if SaveBoard regenerated it
    if text.count(dup_uuid) < 2:
        import re

        zone_uuid_re = re.compile(r'(\(zone\b.*?\(uuid ")([0-9a-f-]+)(")', re.S)
        count = 0

        def repl(m: re.Match[str]) -> str:
            nonlocal count
            count += 1
            return f"{m.group(1)}{dup_uuid}{m.group(3)}"

        bundle.pcb_path.write_text(zone_uuid_re.sub(repl, text, count=2), encoding="utf-8")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, _ = run_m3_board_direct(ctx, bundle, out_pcb, mode=m3_mode, tag="f7_05")
    rc = res.returncode if mode == "native" else res.export_res.proc.returncode
    assert rc == 2, f"expected exit 2 on duplicate rule-area UUID, got {rc}"
    assert not out_pcb.exists()
    return {"checked": "duplicate_nil_rule_area_uuid"}


@register_test(
    test_id="T2-F7-06",
    tier=2,
    feature="F7",
    min_stage="M3",
    name="Pre-existing joined terminal partition preserved under zone refill",
)
def t2_f7_06(ctx: DriverContext) -> dict[str, Any]:
    require_m3_capability(ctx)
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=2, seed=7206)
    b.set_rect_outline(0, 0, 35_000_000, 25_000_000)
    # 3-pad GND net: J1-J2 joined by B.Cu zone, J3 isolated outside zone; SIG needs route nearby
    for idx, x in enumerate((6_000_000, 14_000_000, 29_000_000), 1):
        fp = b.add_footprint(f"JG{idx}", (x, 8_000_000))
        b.add_pad(
            fp,
            "1",
            "GND",
            (x, 8_000_000),
            attrib=pn.PAD_ATTRIB_PTH,
            size_nm=(1_700_000, 1_700_000),
            drill_size_nm=(800_000, 800_000),
        )
    b.add_zone(
        "GND",
        [pn.B_Cu],
        [(2_000_000, 2_000_000), (20_000_000, 2_000_000), (20_000_000, 14_000_000), (2_000_000, 14_000_000)],
        zone_name="GND_LEFT",
    )
    for idx, x in enumerate((5_000_000, 30_000_000), 1):
        fp = b.add_footprint(f"JS{idx}", (x, 19_000_000))
        b.add_pad(fp, "1", "SIG", (x, 19_000_000), size_nm=(1_000_000, 1_000_000))
    b.set_project_rules()
    bundle = b.save(ctx.work_dir / "src")

    # Negative check: synthetic split where JG1 is disconnected and JG2-JG3 are connected instead
    ref_refilled = ctx.work_dir / "ref_refilled.kicad_pcb"
    from tests.e2e_rust_router.kicad_verify import run_native_drc_refilled

    run_native_drc_refilled(bundle.pcb_path, refilled_out=ref_refilled)
    cand_split = ctx.work_dir / "cand_split.kicad_pcb"
    shutil.copy2(ref_refilled, cand_split)
    bs = pn.LoadBoard(str(cand_split))
    for z in list(bs.Zones()):
        if not z.GetIsRuleArea():
            bs.Delete(z)
    gnd_code = bs.FindNet("GND").GetNetCode()
    tr = pn.PCB_TRACK(bs)
    tr.SetStart(pn.VECTOR2I(14_000_000, 8_000_000))
    tr.SetEnd(pn.VECTOR2I(29_000_000, 8_000_000))
    tr.SetWidth(250_000)
    tr.SetLayer(pn.F_Cu)
    tr.SetNetCode(gnd_code)
    bs.Add(tr)
    pn.SaveBoard(str(cand_split), bs, True)
    splits = find_split_partitions(ref_refilled, cand_split)
    assert splits and splits[0]["net"] == "GND", f"expected GND split detection, got {splits}"
    return {"splits_detected": len(splits)}


# ============================================================================
# Feature 8 — Global Multi-Net & Rip-Up/Reroute (T2-F8-01 .. T2-F8-06)
# ============================================================================


@register_test(
    test_id="T2-F8-01",
    tier=2,
    feature="F8",
    min_stage="M4",
    name="Single outer-transaction rip-up rollback on unrecoverable victim",
)
def t2_f8_01(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx)
    b = BoardBuilder(copper_layers=2, seed=8201)
    b.set_rect_outline(0, 0, 20_000_000, 16_000_000)
    # Net 1 pre-routed unlocked track; Net 2 enclosed by obstacle box so no reroute exists
    for idx, x in enumerate((4_000_000, 16_000_000), 1):
        fp = b.add_footprint(f"J1_{idx}", (x, 8_000_000))
        b.add_pad(fp, "1", "NET1", (x, 8_000_000), size_nm=(1_000_000, 1_000_000))
    for idx, y in enumerate((3_000_000, 13_000_000), 1):
        fp = b.add_footprint(f"J2_{idx}", (10_000_000, y))
        b.add_pad(fp, "1", "NET2", (10_000_000, y), size_nm=(1_000_000, 1_000_000))
    # Locked enclosure around J2_2 on F.Cu and B.Cu
    pn = _pcbnew()
    for lyr in (pn.F_Cu, pn.B_Cu):
        for a, c in (
            ((7_500_000, 10_500_000), (12_500_000, 10_500_000)),
            ((12_500_000, 10_500_000), (12_500_000, 15_000_000)),
            ((12_500_000, 15_000_000), (7_500_000, 15_000_000)),
            ((7_500_000, 15_000_000), (7_500_000, 10_500_000)),
        ):
            b.add_track("LOCKWALL", a, c, 250_000, layer=lyr, locked=True)
    b.set_project_rules()
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, _ = run_m4_board_or_pending(ctx, bundle, out_pcb, tag="f8_01")
    if mode == "native":
        assert res.returncode in (3, 4, 5)
    else:
        assert res.stage_failed == "engine" and res.engine_res is not None
        assert res.engine_res.proc.returncode in (3, 4, 5)
        oj = res.engine_res.output_json or {}
        assert oj.get("added_tracks") == [] and oj.get("removed_track_ids") == []
    assert not out_pcb.exists()
    return {"checked": "outer_tx_rollback"}


@register_test(
    test_id="T2-F8-02",
    tier=2,
    feature="F8",
    min_stage="M4",
    name="Post-rip-up work_used and ID determinism on subsequent retry",
)
def t2_f8_02(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx)
    bundle = build_crossing_nets_board(ctx.work_dir / "src", num_nets=4, seed=8202)
    out_fail = ctx.work_dir / "out_fail" / "routed.kicad_pcb"
    out_ok1 = ctx.work_dir / "out_ok1" / "routed.kicad_pcb"
    out_ok2 = ctx.work_dir / "out_ok2" / "routed.kicad_pcb"
    for p in (out_fail, out_ok1, out_ok2):
        p.parent.mkdir(parents=True, exist_ok=True)
    m_fail, r_fail, _ = run_m4_board_or_pending(
        ctx, bundle, out_fail, tag="f8_02_fail", max_expansions=5, limits={"max_work": 5}
    )
    assert (r_fail.returncode != 0 if m_fail == "native" else r_fail.stage_failed is not None), (
        "tight-budget pre-run unexpectedly succeeded"
    )
    assert not out_fail.exists(), f"tight-budget pre-run unexpectedly wrote {out_fail}"
    m1, r1, bh1 = run_m4_board_or_pending(ctx, bundle, out_ok1, tag="f8_02_ok1", limits={"max_work": 5_000_000})
    m2, r2, bh2 = run_m4_board_or_pending(ctx, bundle, out_ok2, tag="f8_02_ok2", limits={"max_work": 5_000_000})
    assert m1 == m2
    if m1 == "wire":
        assert r1.engine_res is not None and r2.engine_res is not None
        assert r1.engine_res.output_bytes == r2.engine_res.output_bytes
    else:
        assert sha256_file(out_ok1) == sha256_file(out_ok2)
    assert_clean_e2e(bundle.pcb_path, out_ok1, before_hashes=bh1, source_dir=bundle.directory)
    return {"checked": "post_ripup_determinism"}


@register_test(
    test_id="T2-F8-03",
    tier=2,
    feature="F8",
    min_stage="M4",
    name="removed_track_ids malformed, nil-UUID, and unknown UUID refused as invalid",
)
def t2_f8_03(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx)
    bundle = build_std_2pad_board(ctx.work_dir / "src", seed=8203)
    rules_p = write_json_file(ctx.work_dir / "rules.json", STD_RULES)
    req_p = ctx.work_dir / "request.json"
    exp = ctx.json_driver.export_board(bundle.pcb_path, req_p, rules_p)
    assert exp.proc.returncode == 0 and exp.output_bytes is not None and exp.output_json is not None
    from tests.e2e_rust_router.drivers import sha256_bytes

    req_sha = sha256_bytes(exp.output_bytes)
    src_id = exp.output_json["source_id"]
    for idx, bad_id in enumerate(
        ("not-a-uuid", "00000000-0000-0000-0000-000000000000", canonical_uuid(999999))
    ):
        plan_p = write_json_file(
            ctx.work_dir / f"bad_plan_{idx}.json",
            {
                "version": 3,
                "request_sha256": req_sha,
                "source_id": src_id,
                "status": "complete",
                "added_tracks": [],
                "removed_track_ids": [bad_id],
                "expanded_states": 0,
                "work_used": 0,
                "reason": None,
            },
        )
        out_pcb = ctx.work_dir / f"out_{idx}.kicad_pcb"
        app = ctx.json_driver.apply_plan(bundle.pcb_path, plan_p, out_pcb, req_p)
        assert_no_crash(app.proc, f"T2-F8-03:{idx}")
        assert app.proc.returncode == 2
        assert app.stderr_json is not None and app.stderr_json.get("status") == "invalid"
        assert not out_pcb.exists()
    return {"checked": ["not-a-uuid", "nil-uuid", "unknown-uuid"]}


@register_test(
    test_id="T2-F8-04",
    tier=2,
    feature="F8",
    min_stage="M4",
    name="Attempt to remove locked/input copper via removed_track_ids refused",
)
def t2_f8_04(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx)
    bundle = build_std_2pad_board(ctx.work_dir / "src", seed=8204, with_obstacle=True)
    pn = _pcbnew()
    b = pn.LoadBoard(str(bundle.pcb_path))
    locked_uuid = next(t.m_Uuid.AsString() for t in b.GetTracks() if t.IsLocked())
    rules_p = write_json_file(ctx.work_dir / "rules.json", STD_RULES)
    req_p = ctx.work_dir / "request.json"
    exp = ctx.json_driver.export_board(bundle.pcb_path, req_p, rules_p)
    assert exp.proc.returncode == 0 and exp.output_bytes is not None and exp.output_json is not None
    from tests.e2e_rust_router.drivers import sha256_bytes

    plan_p = write_json_file(
        ctx.work_dir / "plan_locked_rm.json",
        {
            "version": 3,
            "request_sha256": sha256_bytes(exp.output_bytes),
            "source_id": exp.output_json["source_id"],
            "status": "complete",
            "added_tracks": [],
            "removed_track_ids": [locked_uuid],
            "expanded_states": 0,
            "work_used": 0,
            "reason": None,
        },
    )
    out_pcb = ctx.work_dir / "out.kicad_pcb"
    app = ctx.json_driver.apply_plan(bundle.pcb_path, plan_p, out_pcb, req_p)
    assert_no_crash(app.proc, "T2-F8-04")
    assert app.proc.returncode == 2
    assert app.stderr_json is not None and app.stderr_json.get("status") in ("invalid", "unsupported")
    assert not out_pcb.exists()
    return {"locked_uuid_refused": locked_uuid}


@register_test(
    test_id="T2-F8-05",
    tier=2,
    feature="F8",
    min_stage="M4",
    name="Rip-up loop exhausts single outer max_work without budget reset",
)
def t2_f8_05(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx)
    bundle = build_crossing_nets_board(ctx.work_dir / "src", num_nets=6, seed=8205)
    limits = {"max_work": 40}
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, _ = run_m4_board_or_pending(
        ctx, bundle, out_pcb, tag="f8_05", max_expansions=1000, limits=limits
    )
    if mode == "native":
        assert res.returncode == 5, f"expected exit 5 work_exhausted, got {res.returncode}"
    else:
        assert res.stage_failed == "engine" and res.engine_res is not None
        assert res.engine_res.proc.returncode == 5
        oj = res.engine_res.output_json or {}
        assert oj.get("status") == "work_exhausted"
        assert oj.get("work_used", 0) <= limits["max_work"]
        assert oj.get("added_tracks") == [] and oj.get("removed_track_ids") == []
    assert not out_pcb.exists()
    return {"checked": "outer_work_exhausted"}


@register_test(
    test_id="T2-F8-06",
    tier=2,
    feature="F8",
    min_stage="M4",
    name="journal_exhausted during deep rip-up/reroute sequence",
)
def t2_f8_06(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx)
    bundle = build_crossing_nets_board(ctx.work_dir / "src", num_nets=6, seed=8206)
    limits = {"max_journal_entries": 2}
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, _ = run_m4_board_or_pending(
        ctx, bundle, out_pcb, tag="f8_06", max_expansions=10_000, limits=limits
    )
    if mode == "native":
        assert res.returncode == 6, f"expected exit 6 journal_exhausted, got {res.returncode}"
    else:
        assert res.stage_failed == "engine" and res.engine_res is not None
        assert res.engine_res.proc.returncode == 6
        oj = res.engine_res.output_json or {}
        assert oj.get("status") == "journal_exhausted"
        assert oj.get("added_tracks") == [] and oj.get("removed_track_ids") == []
    assert not out_pcb.exists()
    return {"checked": "journal_exhausted"}


# ============================================================================
# Feature 9 — Differential Pairs, Length & Delay (T2-F9-01 .. T2-F9-05)
# ============================================================================


@register_test(
    test_id="T2-F9-01",
    tier=2,
    feature="F9",
    min_stage="M4",
    name="Illegal single-ended fallback on blocked differential pair refused",
)
def t2_f9_01(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx, require_help_flags=("--diff-pairs",))
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=2, seed=9201)
    b.set_rect_outline(0, 0, 30_000_000, 20_000_000)
    # USB_P and USB_N pads at left and right, separated by keepout wall leaving two isolated 1-track corridors
    for ref, x in (("J1", 4_000_000), ("J2", 26_000_000)):
        fp = b.add_footprint(ref, (x, 10_000_000))
        b.add_pad(fp, "1", "USB_P", (x, 5_000_000), size_nm=(600_000, 600_000))
        b.add_pad(fp, "2", "USB_N", (x, 15_000_000), size_nm=(600_000, 600_000))
    # Central keepout across y=7..13mm blocking coupled pair corridor while leaving y=3..6mm and y=14..17mm open
    b.add_keepout(
        [pn.F_Cu, pn.B_Cu],
        [(10_000_000, 6_500_000), (20_000_000, 6_500_000), (20_000_000, 13_500_000), (10_000_000, 13_500_000)],
    )
    b.set_project_rules(
        classes=[
            {
                "name": "Default",
                "priority": 0,
                "clearance": 0.2,
                "track_width": 0.25,
                "via_diameter": 0.6,
                "via_drill": 0.3,
                "microvia_diameter": 0.3,
                "microvia_drill": 0.15,
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
    proc = ctx.native_driver.run_native(
        bundle.pcb_path, out_pcb, extra_args=["--diff-pairs", "auto"]
    )
    assert_no_crash(proc, "T2-F9-01")
    sj = parse_last_json_line_safe(proc.stderr)
    assert (sj or {}).get("status") != "unsupported", f"T2-F9-01 returned unsupported: {sj}"
    assert proc.returncode in (2, 3), f"expected unroutable/refusal without single-ended fallback, got {proc.returncode}"
    assert not out_pcb.exists()
    return {"checked": "no_single_ended_fallback"}


@register_test(
    test_id="T2-F9-02",
    tier=2,
    feature="F9",
    min_stage="M4",
    name="Unpaired _P/_N, mismatched netclasses, or gap < clearance refused",
)
def t2_f9_02(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx, require_help_flags=("--diff-pairs",))
    # (a) Unpaired TX_P without TX_N
    b_unpaired = BoardBuilder(copper_layers=2, seed=9202)
    b_unpaired.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    for idx, x in enumerate((4_000_000, 16_000_000), 1):
        fp = b_unpaired.add_footprint(f"J{idx}", (x, 10_000_000))
        b_unpaired.add_pad(fp, "1", "TX_P", (x, 10_000_000), size_nm=(600_000, 600_000))
    b_unpaired.set_project_rules()
    bun_a = b_unpaired.save(ctx.work_dir / "unpaired")

    # (b) Mismatched netclasses: CAN+ in ClassA, CAN- in ClassB
    bun_b = build_diff_pair_board(ctx.work_dir / "mismatch_class", seed=9203)
    pro_b = json.loads(bun_b.pro_path.read_text(encoding="utf-8"))
    pro_b["net_settings"]["classes"] = [
        {"name": "ClassA", "priority": 0, "clearance": 0.2, "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3, "diff_pair_width": 0.25, "diff_pair_gap": 0.35},
        {"name": "ClassB", "priority": 1, "clearance": 0.25, "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3, "diff_pair_width": 0.25, "diff_pair_gap": 0.35},
    ]
    pro_b["net_settings"]["netclass_patterns"] = [
        {"netclass": "ClassA", "pattern": r"CAN\+"},
        {"netclass": "ClassB", "pattern": r"CAN-"},
    ]
    bun_b.pro_path.write_text(json.dumps(pro_b, indent=2) + "\n", encoding="utf-8")

    # (c) diff_pair_gap (0.15) < clearance (0.25)
    bun_c = build_diff_pair_board(
        ctx.work_dir / "gap_lt_clr", gap_mm=0.15, clearance_mm=0.25, seed=9204
    )

    for label, bun in (("unpaired", bun_a), ("mismatch_class", bun_b), ("gap_lt_clr", bun_c)):
        out_pcb = ctx.work_dir / f"out_{label}.kicad_pcb"
        proc = ctx.native_driver.run_native(
            bun.pcb_path, out_pcb, extra_args=["--diff-pairs", "auto"]
        )
        assert_no_crash(proc, f"T2-F9-02:{label}")
        sj = parse_last_json_line_safe(proc.stderr)
        assert (sj or {}).get("status") != "unsupported", f"T2-F9-02:{label} returned unsupported: {sj}"
        assert proc.returncode in (2, 4), f"{label} expected exit 2/4, got {proc.returncode}"
        assert not out_pcb.exists()
    return {"checked": ["unpaired", "mismatch_class", "gap_lt_clr"]}


@register_test(
    test_id="T2-F9-03",
    tier=2,
    feature="F9",
    min_stage="M4",
    name="Tight uncoupled meander budget (1 mm vs 2.0833 mm) rejected",
)
def t2_f9_03(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx, require_help_flags=("--diff-pairs", "--length-match-group"))
    src_dir = stage_fixture_copy(ctx.repo_root / "fixtures" / "inter_pair_matching", ctx.work_dir / "src")
    dru = src_dir / "pairs.kicad_dru"
    dru_txt = dru.read_text(encoding="utf-8")
    dru.write_text(dru_txt.replace("3mm", "1mm"), encoding="utf-8")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    proc = ctx.native_driver.run_native(
        src_dir / "pairs.kicad_pcb",
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
    )
    assert_no_crash(proc, "T2-F9-03")
    sj = parse_last_json_line_safe(proc.stderr)
    assert (sj or {}).get("status") != "unsupported", f"T2-F9-03 returned unsupported: {sj}"
    assert proc.returncode in (2, 3), f"expected rejection on impossible 1mm uncoupled budget, got {proc.returncode}"
    assert not out_pcb.exists()
    return {"checked": "impossible_uncoupled_meander"}


@register_test(
    test_id="T2-F9-04",
    tier=2,
    feature="F9",
    min_stage="M4",
    name="Dead-end branch/loop copper cannot satisfy useful fromTo path length",
)
def t2_f9_04(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(ctx, require_help_flags=("--length-match-group",))
    pn = _pcbnew()
    src_dir = stage_fixture_copy(ctx.repo_root / "fixtures" / "group_matching", ctx.work_dir / "src")
    pcb = src_dir / "group.kicad_pcb"
    board = pn.LoadBoard(str(pcb))
    by_net: dict[str, list[Any]] = {}
    for pad in board.GetPads():
        if pad.GetNetCode() > 0:
            by_net.setdefault(pad.GetNetname(), []).append(pad)
    for net, pads in by_net.items():
        if len(pads) == 2:
            tr = pn.PCB_TRACK(board)
            tr.SetStart(pads[0].GetPosition())
            tr.SetEnd(pads[1].GetPosition())
            tr.SetWidth(250_000)
            tr.SetLayer(pn.F_Cu)
            tr.SetNetCode(pads[0].GetNetCode())
            board.Add(tr)
    # Add a 2.0 mm dead-end loop branch on DQ1 (48mm direct + 2mm branch = 50mm raw, still 48mm fromTo)
    dq1_pads = by_net["DQ1"]
    mid_x = (dq1_pads[0].GetPosition().x + dq1_pads[1].GetPosition().x) // 2
    y0 = dq1_pads[0].GetPosition().y
    dq1_code = dq1_pads[0].GetNetCode()
    for a, b in (
        ((mid_x, y0), (mid_x + 750_000, y0 + 661_438)),
        ((mid_x + 750_000, y0 + 661_438), (mid_x + 1_500_000, y0)),
    ):
        br = pn.PCB_TRACK(board)
        br.SetStart(pn.VECTOR2I(*a))
        br.SetEnd(pn.VECTOR2I(*b))
        br.SetWidth(250_000)
        br.SetLayer(pn.F_Cu)
        br.SetNetCode(dq1_code)
        board.Add(br)
    pn.SaveBoard(str(pcb), board, True)
    rep = verify_fromto_length_groups(pcb, [["DQ0", "DQ1", "DQ2", "DQ3"]], tolerance_mm=0.1)
    assert not rep.passed and rep.skew_violations, "expected fromTo skew_out_of_range to catch dead-end loop"
    return {"skew_violations": len(rep.skew_violations)}


@register_test(
    test_id="T2-F9-05",
    tier=2,
    feature="F9",
    min_stage="M4",
    name="Missing stackup Er/thickness or asymmetric stripline refused fail-closed",
)
def t2_f9_05(ctx: DriverContext) -> dict[str, Any]:
    require_m4_capability(
        ctx,
        require_help_flags=(
            "--length-match-group",
            "--group-delay-tolerance",
            "--diff-pairs",
            "--pair-layers",
            "--pair-impedance",
        ),
    )
    import re

    # (a) Strip epsilon_r from delay_matching stackup
    dir_a = stage_fixture_copy(ctx.repo_root / "fixtures" / "delay_matching", ctx.work_dir / "no_er")
    pcb_a = dir_a / "delay.kicad_pcb"
    txt_a = pcb_a.read_text(encoding="utf-8")
    pcb_a.write_text(re.sub(r"\(epsilon_r\s+[^\)]+\)", "", txt_a), encoding="utf-8")
    out_a = ctx.work_dir / "out_a.kicad_pcb"
    proc_a = ctx.native_driver.run_native(
        pcb_a,
        out_a,
        extra_args=["--length-match-group", "DQ0", "DQ1", "--group-delay-tolerance", "1"],
    )
    assert_no_crash(proc_a, "T2-F9-05:no_er")
    sj_a = parse_last_json_line_safe(proc_a.stderr)
    assert (sj_a or {}).get("status") != "unsupported", f"T2-F9-05:no_er returned unsupported: {sj_a}"
    assert proc_a.returncode in (2, 4) and not out_a.exists()

    # (b) Asymmetric stripline dielectric thickness in pair_stripline with nominal impedance
    dir_b = stage_fixture_copy(ctx.repo_root / "fixtures" / "pair_stripline", ctx.work_dir / "asym")
    pcb_b = dir_b / "board.kicad_pcb"
    txt_b = pcb_b.read_text(encoding="utf-8")
    pcb_b.write_text(txt_b.replace("0.486666", "0.520000", 1), encoding="utf-8")
    out_b = ctx.work_dir / "out_b.kicad_pcb"
    proc_b = ctx.native_driver.run_native(
        pcb_b,
        out_b,
        extra_args=[
            "--diff-pairs",
            "auto",
            "--pair-layers",
            "In1.Cu",
            "--pair-impedance",
            "100",
            "--impedance-tolerance",
            "10",
            "--impedance-reference",
            "GND",
            "--impedance-uncoupled",
            "4",
        ],
    )
    assert_no_crash(proc_b, "T2-F9-05:asym")
    sj_b = parse_last_json_line_safe(proc_b.stderr)
    assert (sj_b or {}).get("status") != "unsupported", f"T2-F9-05:asym returned unsupported: {sj_b}"
    assert proc_b.returncode in (2, 4) and not out_b.exists()
    return {"checked": ["missing_epsilon_r", "asymmetric_stripline"]}


# ============================================================================
# Feature 10 — agent-ee Integration & Provenance (T2-F10-01 .. T2-F10-05)
# ============================================================================


@register_test(
    test_id="T2-F10-01",
    tier=2,
    feature="F10",
    min_stage="BASELINE",
    name="Audit hook blocks legacy Python router while Rust wire succeeds",
)
def t2_f10_01(ctx: DriverContext) -> dict[str, Any]:
    bundle = build_std_2pad_board(ctx.work_dir / "src", seed=10201)
    rules_p = write_json_file(ctx.work_dir / "rules.json", STD_RULES)
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    before_hashes = snapshot_source_dir(bundle.directory)

    res = ctx.json_driver.route_board_e2e(
        bundle.pcb_path, out_pcb, rules_p, work_dir=ctx.work_dir / "wire"
    )
    assert res.stage_failed is None, f"positive guarded route failed at {res.stage_failed}"
    assert res.engine_res is not None and res.engine_res.output_json is not None
    assert res.export_res.output_bytes is not None
    assert any(r.get("guard_started") is True for r in res.export_res.proc.audit_rows)
    assert res.apply_res is not None
    assert any(r.get("guard_started") is True for r in res.apply_res.proc.audit_rows)
    assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        plan_json=res.engine_res.output_json,
        request_bytes=res.export_res.output_bytes,
    )

    guard = ctx.json_driver.guard
    negative_probes = [
        ("bin_kicad_route", [str(ctx.repo_root / "bin" / "kicad-route"), "--help"], None),
        ("module_cli", ["-m", "kicad_route.cli", "--help"], [ctx.repo_root / "src"]),
        (
            "py_router_route",
            [str(ctx.repo_root / "src" / "kicad_route" / "router" / "py_router" / "route.py"), "--help"],
            None,
        ),
    ]
    for label, args, extra_pp in negative_probes:
        proc, rows = guard.run_python_probe(
            args, extra_pythonpath=extra_pp, work_dir=ctx.work_dir / f"probe_{label}"
        )
        assert proc.returncode != 0, f"{label} unexpectedly succeeded under GuardedEnv"
        assert "ARCHITECTURE_BOUNDARY" in proc.stderr, f"{label} missing ARCHITECTURE_BOUNDARY: {proc.stderr}"
        assert any(r.get("allowed") is False for r in rows), f"{label} audit log missed violation: {rows}"
    return {"negative_probes_blocked": len(negative_probes)}


@register_test(
    test_id="T2-F10-02",
    tier=2,
    feature="F10",
    min_stage="BASELINE",
    name="Dirty checkout or tampered manifest.json refuses preflight",
)
def t2_f10_02(ctx: DriverContext) -> dict[str, Any]:
    ok, reason = ctx.agent_ee_driver.is_available()
    if not ok:
        raise PendingCapability(reason)
    if (ctx.repo_root / ".git").is_file():
        wt_pf = ctx.agent_ee_driver.preflight(ctx.repo_root)
        assert wt_pf["problems"], "expected worktree .git file to refuse preflight"

    checkout = ctx.agent_ee_driver.clone_clean_checkout(ctx.repo_root, ctx.work_dir / "checkout")
    state = ctx.work_dir / "state"
    clean_manifest = ctx.agent_ee_driver.make_synthetic_state(checkout, state)
    pf_clean = ctx.agent_ee_driver.preflight(checkout, state)
    assert pf_clean["problems"] == [], f"clean synthetic preflight had problems: {pf_clean['problems']}"
    assert ctx.agent_ee_driver.verify_state(checkout, state) == []

    manifest_path = state / "manifest.json"
    ext_rel = clean_manifest["krt"]["extension_module"]["path"]
    ext_path = state / ext_rel
    ext_orig = ext_path.read_bytes()

    def restore_manifest(data: dict[str, Any] | str) -> None:
        if isinstance(data, str):
            manifest_path.write_text(data, encoding="utf-8")
        else:
            manifest_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    tamper_cases: list[str] = []
    # State manifest & artifact tampers
    m1 = json.loads(json.dumps(clean_manifest))
    m1["krt"]["source_sha256"] = "0" * 64
    restore_manifest(m1)
    assert ctx.agent_ee_driver.verify_state(checkout, state)
    tamper_cases.append("source_sha256")

    m2 = json.loads(json.dumps(clean_manifest))
    m2["krt"]["extension_module"]["sha256"] = None
    restore_manifest(m2)
    assert ctx.agent_ee_driver.verify_state(checkout, state)
    tamper_cases.append("ext_sha_none")

    m3 = json.loads(json.dumps(clean_manifest))
    m3["krt"]["extension_module"]["path"] = "/usr/bin/python3"
    restore_manifest(m3)
    assert ctx.agent_ee_driver.verify_state(checkout, state)
    tamper_cases.append("ext_path_escape")

    m4 = json.loads(json.dumps(clean_manifest))
    m4["krt"]["venv"]["packages_sha256"] = "0" * 64
    restore_manifest(m4)
    assert ctx.agent_ee_driver.verify_state(checkout, state)
    tamper_cases.append("packages_sha256")

    m5 = json.loads(json.dumps(clean_manifest))
    m5["state_dir"] = ""
    restore_manifest(m5)
    assert ctx.agent_ee_driver.verify_state(checkout, state)
    tamper_cases.append("empty_state_dir")

    m6 = json.loads(json.dumps(clean_manifest))
    m6["schema"] = "kicad-route/state/999"
    restore_manifest(m6)
    assert ctx.agent_ee_driver.verify_state(checkout, state)
    tamper_cases.append("bad_schema")

    restore_manifest("{not json")
    assert ctx.agent_ee_driver.verify_state(checkout, state)
    tamper_cases.append("malformed_json")

    manifest_path.unlink()
    assert ctx.agent_ee_driver.verify_state(checkout, state)
    tamper_cases.append("missing_manifest")

    restore_manifest(clean_manifest)
    ext_path.write_bytes(b"corrupted-so")
    assert ctx.agent_ee_driver.verify_state(checkout, state)
    tamper_cases.append("corrupt_ext_so")
    ext_path.write_bytes(ext_orig)

    py_link = state / ".venv-krt" / "bin" / "python"
    py_link.unlink()
    assert ctx.agent_ee_driver.preflight(checkout, state)["problems"]
    tamper_cases.append("missing_venv_python")
    py_link.symlink_to("/usr/bin/python3")

    # Checkout tampers
    upstream = checkout / "src" / "kicad_route" / "router" / "UPSTREAM.json"
    orig_upstream = upstream.read_text(encoding="utf-8")
    upstream.write_text(orig_upstream + "\n", encoding="utf-8")
    pf_dirty = ctx.agent_ee_driver.preflight(checkout, state)
    assert pf_dirty["problems"] and pf_dirty["clean"] is False
    tamper_cases.append("dirty_tracked_file")
    upstream.write_text(orig_upstream, encoding="utf-8")

    untracked = checkout / "src" / "untracked_probe.py"
    untracked.write_text("# probe\n", encoding="utf-8")
    pf_untracked = ctx.agent_ee_driver.preflight(checkout, state)
    assert pf_untracked["problems"] and pf_untracked["clean"] is False
    tamper_cases.append("untracked_file")
    untracked.unlink()

    (checkout / ".git" / "HEAD").write_text("not-a-commit\n", encoding="utf-8")
    assert ctx.agent_ee_driver.preflight(checkout, state)["problems"]
    tamper_cases.append("corrupt_git_head")

    return {"tampers_verified": tamper_cases}


@register_test(
    test_id="T2-F10-03",
    tier=2,
    feature="F10",
    min_stage="BASELINE",
    name="Forged validated_board_sha256 and tampered candidate rejected by verify()",
)
def t2_f10_03(ctx: DriverContext) -> dict[str, Any]:
    ok, reason = ctx.agent_ee_driver.is_available()
    if not ok:
        raise PendingCapability(reason)
    script = """
import contextlib, hashlib, io, json, sys
from pathlib import Path
from agent_ee import kicad_route_attempt as attempt, routing_adapter
from tests.kicad_route_fixture import make_candidate, make_checkout

root = Path(sys.argv[1])
REQ = ['--project', 'project', '--board', 'rc.kicad_pcb', '--schematic', 'rc.kicad_sch',
       '--name', 'rc', '--label', 'step-1', '--budget', '5', '--timeout', '30']

def run_case(tag):
    sub = root / tag
    co, _ = make_checkout(sub / 'checkout', 'complete')
    cand = make_candidate(sub / 'candidate')
    work = sub / 'work'
    work.mkdir()
    layout = {'work': work, 'candidate': cand, 'checkout': co, 'state': co, 'probe': []}
    inputs = attempt.files_of(attempt.tree(cand))
    with contextlib.redirect_stdout(io.StringIO()):
        rc = attempt.main(list(REQ), layout)
    assert rc == 0, f'expected rc 0, got {rc}'
    summary = routing_adapter.verify(work, inputs, layout=layout)
    assert summary['proposal'] == 'eligible'
    return work, inputs, layout

# 1. Forge validated_board_sha256 in result.json (updating adapter-report retained sha256)
w1, inp1, lay1 = run_case('case1')
res_p = w1 / 'routing/attempt/result.json'
res_d = json.loads(res_p.read_text(encoding='utf-8'))
res_d['validation']['validated_board_sha256'] = '0' * 64
res_p.write_text(json.dumps(res_d, indent=2) + '\\n', encoding='utf-8')
rep_p = w1 / 'routing/adapter-report.json'
rep_d = json.loads(rep_p.read_text(encoding='utf-8'))
rep_d['retained']['attempt/result.json'] = hashlib.sha256(res_p.read_bytes()).hexdigest()
rep_d['identity'] = attempt.canonical_identity({k: v for k, v in rep_d.items() if k != 'identity'})
rep_p.write_text(json.dumps(rep_d, indent=2) + '\\n', encoding='utf-8')
try:
    routing_adapter.verify(w1, inp1, layout=lay1)
    raise AssertionError('verify() accepted forged validated_board_sha256')
except ValueError as exc:
    assert 'not the board the router validated' in str(exc), str(exc)

# 2. Tamper candidate board bytes after route
w2, inp2, lay2 = run_case('case2')
cand_pcb = w2 / 'routing/candidate/project/rc.kicad_pcb'
cand_pcb.write_text(cand_pcb.read_text(encoding='utf-8') + '\\n# forged\\n', encoding='utf-8')
try:
    routing_adapter.verify(w2, inp2, layout=lay2)
    raise AssertionError('verify() accepted tampered candidate board')
except ValueError as exc:
    assert 'routed candidate differs' in str(exc), str(exc)

# 3. bind_board_attempt() rejects stale candidate digest
route_dir = root / 'bind_probe'
run_dir = route_dir / 'out/run'
cand_dir = route_dir / 'cand'
run_dir.mkdir(parents=True)
cand_dir.mkdir(parents=True)
board_file = cand_dir / 'rc.kicad_pcb'
board_file.write_text('(kicad_pcb (version 20260206) (generator forged))\\n', encoding='utf-8')
res_d2 = json.loads((w2 / 'routing/attempt/result.json').read_text(encoding='utf-8'))
res_d2['candidate'] = {'dir': 'cand', 'board': 'cand/rc.kicad_pcb'}
res_d2['validation']['candidate_board_sha256'] = 'f' * 64
(run_dir / 'result.json').write_text(json.dumps(res_d2), encoding='utf-8')
rec = routing_adapter.bind_board_attempt(route_dir, ['kicad-route', 'run'], 0)
assert rec['status'] == 'stale_candidate' and rec['failure_kind'] == 'environment', rec
print(json.dumps({'verified': ['validated_board_sha256', 'candidate_bytes', 'bind_board_attempt']}))
"""
    proc = ctx.agent_ee_driver.run_python(script, str(ctx.work_dir / "t2_f10_03"))
    assert_no_crash(proc, "T2-F10-03")
    assert proc.returncode == 0, f"T2-F10-03 failed:\n{proc.stderr}\n{proc.stdout}"
    return json.loads(proc.stdout)


@register_test(
    test_id="T2-F10-04",
    tier=2,
    feature="F10",
    min_stage="M5",
    name="--protect human fail-closed when designer edits copper between steps",
)
def t2_f10_04(ctx: DriverContext) -> dict[str, Any]:
    ctx.agent_ee_driver.probe_rust_integration(ctx.native_driver)
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=2, seed=10204)
    b.set_rect_outline(0, 0, 30_000_000, 20_000_000)
    # Block B.Cu so /SIG_B (vertical) must cross /SIG_A (horizontal) on F.Cu by detouring around it.
    b.add_keepout(
        [pn.B_Cu],
        [(0, 0), (30_000_000, 0), (30_000_000, 20_000_000), (0, 20_000_000)],
        no_tracks=True,
        no_vias=True,
        no_zones=True,
    )
    for idx, x in enumerate((5_000_000, 25_000_000), 1):
        fp = b.add_footprint(f"J_A_{idx}", (x, 10_000_000))
        b.add_pad(fp, "1", "/SIG_A", (x, 10_000_000), size_nm=(1_000_000, 1_000_000))
    for idx, y in enumerate((3_000_000, 17_000_000), 1):
        fp = b.add_footprint(f"J_B_{idx}", (15_000_000, y))
        b.add_pad(fp, "1", "/SIG_B", (15_000_000, y), size_nm=(1_000_000, 1_000_000))
    b.set_project_rules()
    bundle = b.save(ctx.work_dir / "src")
    step1_pcb = ctx.work_dir / "step1" / "routed.kicad_pcb"
    step2_pcb = ctx.work_dir / "step2" / "routed.kicad_pcb"
    step1_pcb.parent.mkdir(parents=True, exist_ok=True)
    step2_pcb.parent.mkdir(parents=True, exist_ok=True)
    p1 = ctx.native_driver.run_native(bundle.pcb_path, step1_pcb, extra_args=["--nets", "/SIG_A"])
    assert p1.returncode == 0, f"Step 1 failed: {p1.stderr}"

    # Designer edits one Step-1 segment width by +0.05 mm before Step 2 --protect human
    edited_pcb = ctx.work_dir / "edited" / "routed.kicad_pcb"
    edited_pcb.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(step1_pcb, edited_pcb)
    shutil.copy2(bundle.pro_path, edited_pcb.with_suffix(".kicad_pro"))
    board = pn.LoadBoard(str(edited_pcb))
    sig_a_tracks = [t for t in board.GetTracks() if t.GetNetname() == "/SIG_A" and t.GetClass() == "PCB_TRACK"]
    assert sig_a_tracks
    sig_a_tracks[0].SetWidth(int(sig_a_tracks[0].GetWidth()) + 50_000)
    edited_uuid = sig_a_tracks[0].m_Uuid.AsString()
    edited_width = int(sig_a_tracks[0].GetWidth())
    pn.SaveBoard(str(edited_pcb), board, True)
    edited_bh = snapshot_source_dir(edited_pcb.parent)

    p2 = ctx.native_driver.run_native(
        edited_pcb, step2_pcb, extra_args=["--protect", "human", "--nets", "!/SIG_A"]
    )
    assert p2.returncode == 0, f"Step 2 failed: {p2.stderr}"
    metrics = assert_clean_e2e(
        edited_pcb,
        step2_pcb,
        before_hashes=edited_bh,
        source_dir=edited_pcb.parent,
    )
    board2 = pn.LoadBoard(str(step2_pcb))
    after_seg = next(t for t in board2.GetTracks() if t.m_Uuid.AsString() == edited_uuid)
    assert int(after_seg.GetWidth()) == edited_width
    return {**metrics, "edited_uuid_preserved": edited_uuid}


@register_test(
    test_id="T2-F10-05",
    tier=2,
    feature="F10",
    min_stage="M5",
    name="net_completion ratchet rejects splitting an incomplete net's joined subset",
)
def t2_f10_05(ctx: DriverContext) -> dict[str, Any]:
    ctx.agent_ee_driver.probe_rust_integration(ctx.native_driver)
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=2, seed=10205)
    b.set_rect_outline(0, 0, 35_000_000, 20_000_000)
    for idx, x in enumerate((5_000_000, 17_000_000, 29_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
        b.add_pad(fp, "1", "SIG", (x, 10_000_000), size_nm=(1_000_000, 1_000_000))
    b.add_track("SIG", (5_000_000, 10_000_000), (17_000_000, 10_000_000), 250_000, locked=False)
    b.set_project_rules()
    bundle = b.save(ctx.work_dir / "src")

    # Candidate that splits J1-J2 and joins J2-J3 (still 1 open!)
    cand_dir = ctx.work_dir / "cand"
    stage_fixture_copy(bundle.directory, cand_dir)
    cand_pcb = cand_dir / bundle.pcb_path.name
    bc = pn.LoadBoard(str(cand_pcb))
    for t in list(bc.GetTracks()):
        bc.Delete(t)
    tr = pn.PCB_TRACK(bc)
    tr.SetStart(pn.VECTOR2I(17_000_000, 10_000_000))
    tr.SetEnd(pn.VECTOR2I(29_000_000, 10_000_000))
    tr.SetWidth(250_000)
    tr.SetLayer(pn.F_Cu)
    tr.SetNetCode(bc.FindNet("SIG").GetNetCode())
    bc.Add(tr)
    pn.SaveBoard(str(cand_pcb), bc, True)

    splits = find_split_partitions(bundle.pcb_path, cand_pcb)
    assert splits and splits[0]["net"] == "SIG", f"expected split partition on SIG, got {splits}"
    return {"split_components": splits}


# ============================================================================
# Feature 11 — E2E Acceptance & RE-08 Attribution (T2-F11-01 .. T2-F11-05)
# ============================================================================


def _make_compare_sample(
    *,
    routing_status: str = "COMPLETE",
    unconnected: int = 0,
    open_nets: list[str] | None = None,
    wall_s: float = 1.0,
    max_rss_kib: Any = 10_000,
    tree_rss_kib: Any = 12_000,
    copper: str = "b" * 64,
    wall_clock_stop: bool = False,
) -> dict[str, Any]:
    return {
        "wall_s": wall_s,
        "max_rss_kib": max_rss_kib,
        "process_tree_peak_rss_kib": tree_rss_kib,
        "quality": {
            "process": "PASS",
            "routing": routing_status,
            "validation": "PASS",
            "unconnected": unconnected,
            "open_nets": list(open_nets or []),
            "preservation": {"footprints": "PASS", "copper": "PASS"},
            "new_violations": 0,
            "input_fingerprint": "a" * 64,
            "copper": copper,
            "pairs": [],
            "wall_clock_stop": wall_clock_stop,
        },
    }


@register_test(
    test_id="T2-F11-01",
    tier=2,
    feature="F11",
    min_stage="BASELINE",
    name="Equivalent incomplete baseline refused for outcome-fitness",
)
def t2_f11_01(ctx: DriverContext) -> dict[str, Any]:
    inc = lambda: _make_compare_sample(
        routing_status="INCOMPLETE", unconnected=2, open_nets=["A", "B"]
    )
    payload = {
        "reference": [inc() for _ in range(3)],
        "candidate": [inc() for _ in range(3)],
        "warmup": {"reference": inc(), "candidate": inc()},
        "mode": "migration-equivalence",
        "claim": "outcome-fitness",
    }
    proc, verdict = ctx.scripts_driver.eval_compare_router("compare", payload)
    assert proc.returncode == 0, f"compare helper failed: {proc.stderr}"
    assert verdict["comparison_passed"] is True
    assert verdict["outcome_fitness"]["passed"] is False
    assert verdict["passed"] is False
    assert any("zero-open" in f for f in verdict["failures"]), verdict["failures"]
    assert verdict["architecture_conformance"]["status"] == "NOT_EVALUATED"
    return {"failures": verdict["failures"]}


@register_test(
    test_id="T2-F11-02",
    tier=2,
    feature="F11",
    min_stage="BASELINE",
    name="compare_router.py rejects >1.10x wall/RSS regression and invalid metrics",
)
def t2_f11_02(ctx: DriverContext) -> dict[str, Any]:
    ref_samples = [_make_compare_sample(wall_s=10.0, max_rss_kib=10_000, tree_rss_kib=20_000) for _ in range(3)]
    warmup = {"reference": _make_compare_sample(), "candidate": _make_compare_sample()}

    # Exactly 1.10x passes
    cand_ok = [
        _make_compare_sample(wall_s=11.0, max_rss_kib=11_000, tree_rss_kib=22_000)
        for _ in range(3)
    ]
    proc_ok, v_ok = ctx.scripts_driver.eval_compare_router(
        "compare",
        {"reference": ref_samples, "candidate": cand_ok, "warmup": warmup, "mode": "migration-equivalence", "claim": "outcome-fitness"},
    )
    assert proc_ok.returncode == 0 and v_ok["passed"] is True, v_ok

    # 1.1001x on each gate fails
    for key, bad_val in (("wall_s", 11.001), ("max_rss_kib", 11_001), ("process_tree_peak_rss_kib", 22_002)):
        kw = {
            "wall_s": 10.0,
            "max_rss_kib": 10_000,
            "tree_rss_kib": 20_000,
        }
        kw["tree_rss_kib" if key == "process_tree_peak_rss_kib" else key] = bad_val
        cand_bad = [_make_compare_sample(**kw) for _ in range(3)]
        proc_b, v_b = ctx.scripts_driver.eval_compare_router(
            "compare",
            {"reference": ref_samples, "candidate": cand_bad, "warmup": warmup, "mode": "migration-equivalence", "claim": "comparison"},
        )
        assert proc_b.returncode == 0 and v_b["passed"] is False, f"{key} should fail at >1.10x: {v_b}"
        assert any(f"{key} exceeds 10% regression limit" in f for f in v_b["failures"]), v_b["failures"]

    # Invalid metrics (None, 0, -1, NaN, inf, str, bool)
    for bad in (None, 0, -1, float("nan"), float("inf"), "12", True):
        cand_inv = [_make_compare_sample(wall_s=bad) for _ in range(3)]
        proc_i, v_i = ctx.scripts_driver.eval_compare_router(
            "compare",
            {"reference": ref_samples, "candidate": cand_inv, "warmup": warmup, "mode": "migration-equivalence", "claim": "comparison"},
        )
        assert proc_i.returncode == 0 and v_i["passed"] is False, f"expected invalid metric {bad!r} to fail"
    return {"checked": ["1.10x_pass", "1.1001x_fail", "invalid_metrics_fail"]}


@register_test(
    test_id="T2-F11-03",
    tier=2,
    feature="F11",
    min_stage="BASELINE",
    name="Warmup failure or wall-clock stop fails measurement qualification",
)
def t2_f11_03(ctx: DriverContext) -> dict[str, Any]:
    ref_samples = [_make_compare_sample() for _ in range(3)]
    cand_samples = [_make_compare_sample() for _ in range(3)]

    # (a) Candidate warmup has unconnected=1
    proc_w, v_w = ctx.scripts_driver.eval_compare_router(
        "compare",
        {
            "reference": ref_samples,
            "candidate": cand_samples,
            "warmup": {
                "reference": _make_compare_sample(),
                "candidate": _make_compare_sample(routing_status="COMPLETE", unconnected=1, open_nets=["SIG"]),
            },
            "mode": "migration-equivalence",
            "claim": "outcome-fitness",
        },
    )
    assert proc_w.returncode == 0 and v_w["passed"] is False
    assert any("routing completion regressed" in f for f in v_w["failures"]), v_w["failures"]

    # (b) quality() detects stalled command -> wall_clock_stop: True -> compare fails
    raw_res = {
        "schema": "kicad-route/route/1",
        "process": {"status": "PASS", "commands": [{}, {"stalled": True}]},
        "routing": {"status": "COMPLETE", "unconnected_total": 0, "unrouted_nets": []},
        "validation": {
            "status": "PASS",
            "preservation": {"footprints": "PASS"},
            "drc": {"new_violations": {"count": 0}},
        },
        "input": {"fingerprint": "a" * 64},
        "evidence": {"candidate_copper_signature": {"sha256": "b" * 64}},
    }
    proc_q, q_out = ctx.scripts_driver.eval_compare_router("quality", {"result": raw_res})
    assert proc_q.returncode == 0 and q_out["wall_clock_stop"] is True

    proc_s, v_s = ctx.scripts_driver.eval_compare_router(
        "compare",
        {
            "reference": ref_samples,
            "candidate": [
                _make_compare_sample(wall_clock_stop=True),
                _make_compare_sample(),
                _make_compare_sample(),
            ],
            "warmup": {"reference": _make_compare_sample(), "candidate": _make_compare_sample()},
            "mode": "migration-equivalence",
            "claim": "comparison",
        },
    )
    assert proc_s.returncode == 0 and v_s["passed"] is False
    assert any("wall-clock-censored attempt" in f for f in v_s["failures"]), v_s["failures"]
    return {"checked": ["warmup_unconnected", "wall_clock_stop"]}


@register_test(
    test_id="T2-F11-04",
    tier=2,
    feature="F11",
    min_stage="M2",
    name="Failed run with identical request bytes never adopts stale destination",
)
def t2_f11_04(ctx: DriverContext) -> dict[str, Any]:
    probe = ctx.native_driver.probe()
    if not probe.supported:
        raise PendingCapability(probe.reason, evidence={"probe": probe.reason})
    bundle = build_std_2pad_board(ctx.work_dir / "src", seed=11204)
    rules_p = write_json_file(ctx.work_dir / "rules.json", STD_RULES)
    out_a = ctx.work_dir / "out" / "routed_a.kicad_pcb"
    out_b = ctx.work_dir / "out" / "routed_b.kicad_pcb"
    out_a.parent.mkdir(parents=True, exist_ok=True)
    pa = ctx.native_driver.run_native(bundle.pcb_path, out_a, rules_path=rules_p)
    assert pa.returncode == 0 and out_a.is_file()

    # Pre-seed out_b with sentinel bytes; run native orchestration expecting no-overwrite exit 8
    sentinel = b"(kicad_pcb (version 20260206) (generator sentinel-stale))\n"
    out_b.write_bytes(sentinel)
    pb = ctx.native_driver.run_native(bundle.pcb_path, out_b, rules_path=rules_p)
    assert_no_crash(pb, "T2-F11-04")
    assert pb.returncode == 8, f"expected exit 8 no-overwrite refusal, got {pb.returncode}"
    assert out_b.read_bytes() == sentinel
    return {"checked": "stale_destination_never_overwritten"}


@register_test(
    test_id="T2-F11-05",
    tier=2,
    feature="F11",
    min_stage="M5",
    name="Rule-floor / .kicad_pro tampering or moved footprint rejected",
)
def t2_f11_05(ctx: DriverContext) -> dict[str, Any]:
    ctx.agent_ee_driver.probe_rust_integration(ctx.native_driver)
    src_dir = stage_fixture_copy(ctx.repo_root / "fixtures" / "route_demo", ctx.work_dir / "src")
    cand_dir = stage_fixture_copy(src_dir, ctx.work_dir / "cand")
    ref_pcb = src_dir / "route_demo.kicad_pcb"
    cand_pcb = cand_dir / "route_demo.kicad_pcb"

    # (a) Tamper candidate .kicad_pro clearance lower
    cand_pro = cand_dir / "route_demo.kicad_pro"
    pro_d = json.loads(cand_pro.read_text(encoding="utf-8"))
    pro_d["board"]["design_settings"]["rules"]["min_clearance"] = 0.05
    cand_pro.write_text(json.dumps(pro_d, indent=2) + "\n", encoding="utf-8")
    try:
        run_native_drc(cand_pcb, sidecar_source=ref_pcb)
        raise AssertionError("expected sidecar conflict refusal on tampered .kicad_pro")
    except AssertionError as exc:
        assert "conflicts with authoritative" in str(exc)

    # (b) Move footprint 0.5 mm on candidate
    cand_dir2 = stage_fixture_copy(src_dir, ctx.work_dir / "cand2")
    cand_pcb2 = cand_dir2 / "route_demo.kicad_pcb"
    pn = _pcbnew()
    b2 = pn.LoadBoard(str(cand_pcb2))
    fp = list(b2.GetFootprints())[0]
    pos = fp.GetPosition()
    fp.SetPosition(pn.VECTOR2I(pos.x + 500_000, pos.y))
    pn.SaveBoard(str(cand_pcb2), b2, True)
    from tests.e2e_rust_router.kicad_verify import verify_preservation

    try:
        verify_preservation(ref_pcb, cand_pcb2)
        raise AssertionError("expected verify_preservation to reject shifted footprint")
    except AssertionError as exc:
        assert "footprints altered" in str(exc)
    return {"checked": ["pro_tamper", "footprint_shift"]}
