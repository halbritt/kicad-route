"""Tier 2 boundary, corner, and negative E2E tests for Features 1-6 (36 tests)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import sys
from typing import Any

from tests.e2e_rust_router.core_helpers import (
    STD_RULES,
    assert_clean_e2e,
    assert_no_crash,
    assert_source_dir_untouched,
    build_input1_json,
    build_std_2pad_board,
    canonical_uuid,
    parse_last_json_line_safe,
    require_m3_capability,
    run_m3_board_direct,
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
)
from tests.e2e_rust_router.runner import DriverContext, PendingCapability, register_test


# ============================================================================
# Feature 1 — Lane 1: M7 Multi-Terminal (T2-F1-01 .. T2-F1-06)
# ============================================================================


@register_test(
    test_id="T2-F1-01",
    tier=2,
    feature="F1",
    min_stage="M1",
    name="Empty-target / singleton-net & zero-pad guard (C-M7-03)",
)
def t2_f1_01(ctx: DriverContext) -> dict[str, Any]:
    cases = {
        "zero_pad": build_input1_json(nets=[{"id": 1, "name": "N_EMPTY"}], pads=[], tracks=[]),
        "one_pad": build_input1_json(
            nets=[{"id": 1, "name": "N_ONE"}],
            pads=[{"id": canonical_uuid(1), "net": 1, "at": [5_000_000, 10_000_000], "shape": {"kind": "circle", "radius_nm": 500_000}}],
            tracks=[],
        ),
        "one_pad_plus_track": build_input1_json(
            nets=[{"id": 1, "name": "N_ONE_TR"}],
            pads=[{"id": canonical_uuid(2), "net": 1, "at": [3_000_000, 10_000_000], "shape": {"kind": "circle", "radius_nm": 500_000}}],
            tracks=[{"id": canonical_uuid(3), "net": 1, "start": [10_000_000, 10_000_000], "end": [15_000_000, 10_000_000], "width_nm": 250_000, "locked": True}],
        ),
        "three_pad_connected": build_input1_json(
            nets=[{"id": 1, "name": "N_THREE"}],
            pads=[
                {"id": canonical_uuid(4), "net": 1, "at": [3_000_000, 10_000_000], "shape": {"kind": "circle", "radius_nm": 500_000}},
                {"id": canonical_uuid(5), "net": 1, "at": [10_000_000, 10_000_000], "shape": {"kind": "circle", "radius_nm": 500_000}},
                {"id": canonical_uuid(6), "net": 1, "at": [17_000_000, 10_000_000], "shape": {"kind": "circle", "radius_nm": 500_000}},
            ],
            tracks=[{"id": canonical_uuid(7), "net": 1, "start": [3_000_000, 10_000_000], "end": [17_000_000, 10_000_000], "width_nm": 250_000, "locked": True}],
        ),
    }
    results: dict[str, Any] = {}
    for label, doc in cases.items():
        req = write_json_file(ctx.work_dir / f"{label}.req.json", doc)
        plan = ctx.work_dir / f"{label}.plan.json"
        res = ctx.json_driver.run_engine(req, plan)
        assert_no_crash(res.proc, label)
        results[label] = {"rc": res.proc.returncode, "json": res.output_json}
        if res.proc.returncode == 2 and (res.output_json or {}).get("status") == "unsupported":
            raise PendingCapability(f"{label} unsupported: {res.output_json}", evidence=results)

    for label in ("zero_pad", "one_pad", "three_pad_connected"):
        assert results[label]["rc"] == 0, results[label]
        assert results[label]["json"]["added_tracks"] == []
        assert results[label]["json"]["removed_track_ids"] == []
    assert results["one_pad_plus_track"]["rc"] == 0
    assert len(results["one_pad_plus_track"]["json"]["added_tracks"]) > 0
    return results


@register_test(
    test_id="T2-F1-02",
    tier=2,
    feature="F1",
    min_stage="M1",
    name="Collinear overlap normalization (C-M7-02)",
)
def t2_f1_02(ctx: DriverContext) -> dict[str, Any]:
    b = BoardBuilder(seed=1102)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    for idx, pos in enumerate([(3_000_000, 10_000_000), (17_000_000, 10_000_000), (10_000_000, 4_000_000)], 1):
        fp = b.add_footprint(f"J{idx}", pos)
        b.add_pad(fp, "1", "SIGNAL", pos, size_nm=(1_000_000, 1_000_000))
    walls = [
        ((3_000_000, 12_000_000), (17_000_000, 12_000_000)),
        ((3_000_000, 8_000_000), (8_000_000, 8_000_000)),
        ((12_000_000, 8_000_000), (17_000_000, 8_000_000)),
        ((8_000_000, 3_000_000), (8_000_000, 8_000_000)),
        ((12_000_000, 3_000_000), (12_000_000, 8_000_000)),
    ]
    for s, e in walls:
        b.add_track("", s, e, 250_000, locked=True)
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    res, before_hashes = run_wire_or_pending(ctx, bundle.pcb_path, out_pcb, STD_RULES)
    assert res.stage_failed is None, f"T-corridor failed at {res.stage_failed}"
    assert res.engine_res is not None and res.engine_res.output_json is not None
    added = res.engine_res.output_json["added_tracks"]
    # Check zero redundant collinear overlapping pairs among added tracks
    for i, a in enumerate(added):
        ax0, ay0 = a["start"]
        ax1, ay1 = a["end"]
        for b_tr in added[i + 1:]:
            bx0, by0 = b_tr["start"]
            bx1, by1 = b_tr["end"]
            if ay0 == ay1 == by0 == by1:
                ov = min(max(ax0, ax1), max(bx0, bx1)) - max(min(ax0, ax1), min(bx0, bx1))
                assert ov <= 0, f"collinear horizontal overlap {ov} nm: {a} vs {b_tr}"
            if ax0 == ax1 == bx0 == bx1:
                ov = min(max(ay0, ay1), max(by0, by1)) - max(min(ay0, ay1), min(by0, by1))
                assert ov <= 0, f"collinear vertical overlap {ov} nm: {a} vs {b_tr}"
    total_len = sum(abs(t["end"][0] - t["start"][0]) + abs(t["end"][1] - t["start"][1]) for t in added)
    assert total_len <= 20_000_000, f"expected normalized Steiner length <= 20mm, got {total_len}"
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
    test_id="T2-F1-03",
    tier=2,
    feature="F1",
    min_stage="M1",
    name="Unreachable final terminal refuses partial completion (F-M3-03)",
)
def t2_f1_03(ctx: DriverContext) -> dict[str, Any]:
    b = BoardBuilder(seed=1103)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    coords = [(3_000_000, 10_000_000), (7_000_000, 10_000_000), (3_000_000, 15_000_000), (17_000_000, 10_000_000)]
    for idx, pos in enumerate(coords, 1):
        fp = b.add_footprint(f"J{idx}", pos)
        b.add_pad(fp, "1", "SIGNAL", pos, size_nm=(1_000_000, 1_000_000))
    ring = [(15_000_000, 8_000_000), (19_000_000, 8_000_000), (19_000_000, 12_000_000), (15_000_000, 12_000_000)]
    for a, c in zip(ring, ring[1:] + ring[:1]):
        b.add_track("OBSTACLE", a, c, 250_000, locked=True)
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    res, before_hashes = run_wire_or_pending(ctx, bundle.pcb_path, out_pcb, STD_RULES)
    assert res.stage_failed == "engine" and res.engine_res is not None
    assert res.engine_res.proc.returncode == 3
    plan = res.engine_res.output_json
    assert plan is not None and plan["status"] == "unroutable"
    assert plan["added_tracks"] == [] and plan["removed_track_ids"] == []
    app = ctx.json_driver.apply_plan(bundle.pcb_path, res.plan_path, out_pcb, res.request_path)
    assert app.proc.returncode == 2
    assert not out_pcb.exists()
    assert_source_dir_untouched(bundle.directory, before_hashes)
    return {"status": plan["status"], "reason": plan.get("reason")}


@register_test(
    test_id="T2-F1-04",
    tier=2,
    feature="F1",
    min_stage="M1",
    name="Shared batch expansion budget exhaustion across multi-terminal nets",
)
def t2_f1_04(ctx: DriverContext) -> dict[str, Any]:
    rules = {"width_nm": 250_000, "clearance_nm": 1_000_000, "grid_nm": 500_000, "edge_clearance_nm": 500_000}
    rules_path = write_json_file(ctx.work_dir / "rules.json", rules)

    # Measure Net 1 alone
    b1 = BoardBuilder(seed=1104)
    b1.set_rect_outline(0, 0, 22_000_000, 22_000_000)
    for idx, pos in enumerate([(2_000_000, 4_000_000), (6_000_000, 4_000_000), (6_000_000, 6_000_000)], 1):
        fp = b1.add_footprint(f"A{idx}", pos)
        b1.add_pad(fp, "1", "SIG1", pos, size_nm=(1_000_000, 1_000_000))
    bundle1 = b1.save(ctx.work_dir / "n1")
    req1 = ctx.work_dir / "n1.req.json"
    plan1 = ctx.work_dir / "n1.plan.json"
    exp1 = ctx.json_driver.export_board(bundle1.pcb_path, req1, rules_path, max_expansions=100_000)
    assert_no_crash(exp1.proc, "exp1")
    assert exp1.proc.returncode == 0
    eng1 = ctx.json_driver.run_engine(req1, plan1)
    assert_no_crash(eng1.proc, "eng1")
    if eng1.proc.returncode == 2 and (eng1.output_json or {}).get("status") == "unsupported":
        raise PendingCapability(f"multi-terminal unsupported: {eng1.output_json}")
    assert eng1.proc.returncode == 0 and eng1.output_json is not None
    e_net1 = int(eng1.output_json["expanded_states"])

    # Full 2-net board
    b2 = BoardBuilder(seed=1105)
    b2.set_rect_outline(0, 0, 22_000_000, 22_000_000)
    for idx, pos in enumerate([(2_000_000, 4_000_000), (6_000_000, 4_000_000), (6_000_000, 6_000_000)], 1):
        fp = b2.add_footprint(f"A{idx}", pos)
        b2.add_pad(fp, "1", "SIG1", pos, size_nm=(1_000_000, 1_000_000))
    for idx, pos in enumerate([(2_000_000, 16_000_000), (18_000_000, 16_000_000), (18_000_000, 10_000_000)], 1):
        fp = b2.add_footprint(f"B{idx}", pos)
        b2.add_pad(fp, "1", "SIG2", pos, size_nm=(1_000_000, 1_000_000))
    b2.add_track("OBSTACLE", (10_000_000, 12_000_000), (10_000_000, 19_000_000), 250_000, locked=True)
    bundle2 = b2.save(ctx.work_dir / "full")
    req_full = ctx.work_dir / "full.req.json"
    plan_full = ctx.work_dir / "full.plan.json"
    exp_full = ctx.json_driver.export_board(bundle2.pcb_path, req_full, rules_path, max_expansions=100_000)
    assert exp_full.proc.returncode == 0
    eng_full = ctx.json_driver.run_engine(req_full, plan_full)
    assert eng_full.proc.returncode == 0 and eng_full.output_json is not None
    assert eng_full.output_json["expanded_states"] > e_net1 + 5

    budget = e_net1 + 5
    req_tight = ctx.work_dir / "tight.req.json"
    plan_tight = ctx.work_dir / "tight.plan.json"
    exp_tight = ctx.json_driver.export_board(bundle2.pcb_path, req_tight, rules_path, max_expansions=budget)
    assert exp_tight.proc.returncode == 0
    eng_tight = ctx.json_driver.run_engine(req_tight, plan_tight)
    assert_no_crash(eng_tight.proc, "eng_tight")
    assert eng_tight.proc.returncode == 4
    pj = eng_tight.output_json
    assert pj is not None and pj["status"] == "budget_exhausted"
    assert pj["added_tracks"] == [] and pj["removed_track_ids"] == []
    assert pj["expanded_states"] == budget
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    app = ctx.json_driver.apply_plan(bundle2.pcb_path, plan_tight, out_pcb, req_tight)
    assert app.proc.returncode == 2
    assert not out_pcb.exists()
    return {"e_net1": e_net1, "budget": budget}


@register_test(
    test_id="T2-F1-05",
    tier=2,
    feature="F1",
    min_stage="BASELINE",
    name="Tangent / diagonal sub-nm incumbent contact cannot skip terminal",
)
def t2_f1_05(ctx: DriverContext) -> dict[str, Any]:
    variants = {
        "axis_tangent": (
            STD_RULES,
            (3_000_000, 10_000_000),
            (17_000_000, 10_000_000),
            (1_000_000, 1_000_000),
            [((3_000_000, 10_000_000), (16_400_000, 10_000_000))],
        ),
        "diagonal_sub_nm": (
            {**STD_RULES, "grid_nm": 100_000},
            (1_000_000, 4_000_000),
            (2_400_000, 5_400_000),
            (200_000, 200_000),
            [
                ((1_000_000, 4_000_000), (3_142_620, 3_416_878)),
                ((2_050_220, 3_921_453), (2_444_123, 5_368_809)),
            ],
        ),
    }
    for label, (rules, p1, p2, pad_sz, tracks) in variants.items():
        sub = ctx.work_dir / label
        b = BoardBuilder(seed=1106)
        b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
        for idx, pos in enumerate((p1, p2), 1):
            fp = b.add_footprint(f"J{idx}", pos)
            b.add_pad(fp, "1", "SIGNAL", pos, size_nm=pad_sz)
        for s, e in tracks:
            b.add_track("SIGNAL", s, e, 200_000, locked=True)
        bundle = b.save(sub / "src")
        assert read_native_connectivity(bundle.pcb_path).unconnected_count == 1
        out_pcb = sub / "out" / "routed.kicad_pcb"
        out_pcb.parent.mkdir(parents=True, exist_ok=True)
        res, before_hashes = run_wire_or_pending(
            ctx, bundle.pcb_path, out_pcb, rules, tag=label, allow_unsupported_engine=False
        )
        assert res.stage_failed is None, f"{label} failed at {res.stage_failed}"
        assert res.engine_res is not None and res.engine_res.output_json is not None
        assert res.export_res.output_bytes is not None
        plan = res.engine_res.output_json
        assert len(plan["added_tracks"]) > 0, f"{label} skipped closing segment"
        assert_clean_e2e(
            bundle.pcb_path,
            out_pcb,
            before_hashes=before_hashes,
            source_dir=bundle.directory,
            plan_json=plan,
            request_bytes=res.export_res.output_bytes,
        )

        # Negative control: fabricated empty complete plan must be rejected by apply
        fab_plan = write_json_file(
            sub / "empty_plan.json",
            {
                "version": 3,
                "request_sha256": sha256_bytes(res.export_res.output_bytes),
                "source_id": sha256_file(bundle.pcb_path),
                "status": "complete",
                "added_tracks": [],
                "removed_track_ids": [],
                "expanded_states": 0,
                "work_used": 0,
                "reason": None,
            },
        )
        neg_out = sub / "out" / "neg.kicad_pcb"
        app_neg = ctx.json_driver.apply_plan(bundle.pcb_path, fab_plan, neg_out, res.request_path)
        assert app_neg.proc.returncode == 2
        assert app_neg.stderr_json is not None and app_neg.stderr_json["status"] == "native_validation_failed"
        assert not neg_out.exists()
    return {"variants": list(variants)}


@register_test(
    test_id="T2-F1-06",
    tier=2,
    feature="F1",
    min_stage="BASELINE",
    name="Two-isolated-pad shadowed-center domain boundary (C-M3-05)",
)
def t2_f1_06(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    rules = {"width_nm": 1_500_000, "clearance_nm": 1_000_000, "grid_nm": 500_000, "edge_clearance_nm": 500_000}
    b = BoardBuilder(seed=1107)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    fp1 = b.add_footprint("J1", (3_000_000, 10_000_000))
    b.add_pad(fp1, "1", "SIGNAL", (3_000_000, 10_000_000), shape=pn.PAD_SHAPE_RECT, size_nm=(2_000_000, 2_000_000))
    fp2 = b.add_footprint("J2", (15_000_000, 10_000_000))
    b.add_pad(fp2, "1", "SIGNAL", (15_000_000, 10_000_000), shape=pn.PAD_SHAPE_RECT, size_nm=(6_000_000, 500_000))
    b.add_track("OBSTACLE", (14_000_000, 11_750_000), (18_000_000, 11_750_000), 500_000, locked=True)
    bundle = b.save(ctx.work_dir / "src")
    drc_in = run_native_drc(bundle.pcb_path)
    assert [v for v in drc_in.violations if v.get("severity") == "error"] == []

    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    res, before_hashes = run_wire_or_pending(
        ctx, bundle.pcb_path, out_pcb, rules, allow_unsupported_engine=False
    )
    assert res.engine_res is not None
    if res.engine_res.proc.returncode == 3:
        pj = res.engine_res.output_json
        assert pj is not None and pj["status"] == "unroutable"
        assert pj["added_tracks"] == [] and pj["removed_track_ids"] == []
        app = ctx.json_driver.apply_plan(bundle.pcb_path, res.plan_path, out_pcb, res.request_path)
        assert app.proc.returncode == 2
        assert not out_pcb.exists()
        return {"outcome": "honest_unroutable", "reason": pj.get("reason")}
    assert res.stage_failed is None and res.engine_res.proc.returncode == 0
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
# Feature 2 — Lane 3: Native Oval/RoundRect (T2-F2-01 .. T2-F2-07)
# ============================================================================


@register_test(
    test_id="T2-F2-01",
    tier=2,
    feature="F2",
    min_stage="M1",
    name="Zero-size and negative-size pad dimensions rejected (no floor coercion)",
)
def t2_f2_01(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(seed=1201)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    for idx, x in enumerate((3_000_000, 17_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
        b.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), shape=pn.PAD_SHAPE_OVAL, size_nm=(2_000_000, 1_000_000))
    bundle = b.save(ctx.work_dir / "src")
    orig_text = bundle.pcb_path.read_text(encoding="utf-8")
    rules_path = write_json_file(ctx.work_dir / "rules.json", STD_RULES)

    # Probe baseline oval support first
    probe_req = ctx.work_dir / "probe.req.json"
    probe_exp = ctx.json_driver.export_board(bundle.pcb_path, probe_req, rules_path)
    assert_no_crash(probe_exp.proc, "probe_export")
    if probe_exp.proc.returncode == 2 and (probe_exp.stderr_json or {}).get("status") == "unsupported":
        raise PendingCapability(f"oval unsupported: {probe_exp.stderr_json}")

    for bad_size in ("(size 0 1)", "(size -1 1)"):
        patched = ctx.work_dir / f"bad_{bad_size.replace(' ', '_')}.kicad_pcb"
        patched.write_text(orig_text.replace("(size 2 1)", bad_size, 1), encoding="utf-8")
        req_out = ctx.work_dir / f"req_{bad_size.replace(' ', '_')}.json"
        exp = ctx.json_driver.export_board(patched, req_out, rules_path)
        assert_no_crash(exp.proc, f"export_{bad_size}")
        assert exp.proc.returncode == 2
        assert exp.stderr_json is not None and exp.stderr_json["status"] == "invalid"
        assert "pad dimensions" in exp.stderr_json["reason"]
        assert not req_out.exists()

    for bad_half in ([0, 500_000], [-1, 500_000]):
        doc = build_input1_json(
            pads=[
                {"id": canonical_uuid(1), "net": 1, "at": [3_000_000, 10_000_000], "shape": {"kind": "round_rect", "half_size_nm": bad_half, "radius_nm": 0}},
                {"id": canonical_uuid(2), "net": 1, "at": [17_000_000, 10_000_000], "shape": {"kind": "circle", "radius_nm": 500_000}},
            ]
        )
        req_p = write_json_file(ctx.work_dir / f"bad_json_{bad_half[0]}.json", doc)
        plan_p = ctx.work_dir / f"bad_json_{bad_half[0]}.plan.json"
        eng = ctx.json_driver.run_engine(req_p, plan_p)
        assert_no_crash(eng.proc, "engine_bad_half")
        assert eng.proc.returncode == 2
        assert eng.output_json is not None and eng.output_json["status"] == "invalid"
        assert eng.output_json["added_tracks"] == []
    return {"checked": ["size 0 1", "size -1 1", "json 0", "json -1"]}


@register_test(
    test_id="T2-F2-02",
    tier=2,
    feature="F2",
    min_stage="M1",
    name="Odd-nm track width (250001 nm) & odd-nm pad dimensions (2000001 nm)",
)
def t2_f2_02(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    # Subcase (a): 250001 nm track width boundary against oval & roundrect pads
    for shape in (pn.PAD_SHAPE_OVAL, pn.PAD_SHAPE_ROUNDRECT):
        for end_x in (15_874_999, 15_875_000, 15_875_001):
            tag = f"s{shape}_x{end_x}"
            sub = ctx.work_dir / tag
            b = BoardBuilder(seed=1210 + end_x % 10)
            b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
            for idx, x in enumerate((3_000_000, 17_000_000), 1):
                fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
                b.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), shape=shape, size_nm=(2_000_000, 1_000_000), roundrect_ratio=0.25)
            pts = [
                (3_000_000, 10_000_000),
                (3_000_000, 2_000_000),
                (15_000_000, 2_000_000),
                (15_000_000, 10_000_000),
                (end_x, 10_000_000),
            ]
            for a, c in zip(pts, pts[1:]):
                b.add_track("SIGNAL", a, c, 250_001, locked=True)
            bundle = b.save(sub / "src")
            out_pcb = sub / "out" / "routed.kicad_pcb"
            out_pcb.parent.mkdir(parents=True, exist_ok=True)
            res, before_hashes = run_wire_or_pending(ctx, bundle.pcb_path, out_pcb, STD_RULES, tag=tag)
            assert res.stage_failed is None, f"{tag} failed at {res.stage_failed}"
            assert res.engine_res is not None and res.engine_res.output_json is not None
            assert res.export_res.output_bytes is not None
            plan = res.engine_res.output_json
            if end_x <= 15_875_000:
                assert len(plan["added_tracks"]) > 0, f"{tag} should emit closing track"
            else:
                assert plan["added_tracks"] == [], f"{tag} should reuse incumbent"
            assert_clean_e2e(
                bundle.pcb_path,
                out_pcb,
                before_hashes=before_hashes,
                source_dir=bundle.directory,
                plan_json=plan,
                request_bytes=res.export_res.output_bytes,
            )

    # Subcase (b): odd pad dimensions refused cleanly at M1 (or clean if M3+ supports)
    b_odd = BoardBuilder(seed=1220)
    b_odd.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    fp1 = b_odd.add_footprint("J1", (3_000_000, 10_000_000))
    b_odd.add_pad(fp1, "1", "SIGNAL", (3_000_000, 10_000_000), shape=pn.PAD_SHAPE_OVAL, size_nm=(2_000_001, 1_000_000))
    fp2 = b_odd.add_footprint("J2", (17_000_000, 10_000_000))
    b_odd.add_pad(fp2, "1", "SIGNAL", (17_000_000, 10_000_000), shape=pn.PAD_SHAPE_ROUNDRECT, size_nm=(2_000_000, 1_000_001), roundrect_ratio=0.25)
    bundle_odd = b_odd.save(ctx.work_dir / "odd_pad")
    out_odd = ctx.work_dir / "odd_pad" / "routed.kicad_pcb"
    rules_path = write_json_file(ctx.work_dir / "rules.json", STD_RULES)
    exp_odd = ctx.json_driver.export_board(bundle_odd.pcb_path, ctx.work_dir / "odd.req.json", rules_path)
    assert_no_crash(exp_odd.proc, "odd_pad_export")
    if exp_odd.proc.returncode == 2:
        assert exp_odd.stderr_json is not None and exp_odd.stderr_json["status"] == "unsupported"
    else:
        res_odd, bh_odd = run_wire_or_pending(ctx, bundle_odd.pcb_path, out_odd, STD_RULES, tag="odd_pad")
        assert res_odd.stage_failed is None
        assert_clean_e2e(bundle_odd.pcb_path, out_odd, before_hashes=bh_odd, source_dir=bundle_odd.directory)
    return {"checked": "odd_width_and_odd_pad"}


@register_test(
    test_id="T2-F2-03",
    tier=2,
    feature="F2",
    min_stage="M1",
    name="Stripped SMD drill metadata coercion guard",
)
def t2_f2_03(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(seed=1230)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    for idx, x in enumerate((3_000_000, 17_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
        b.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), shape=pn.PAD_SHAPE_ROUNDRECT, size_nm=(2_000_000, 1_000_000), roundrect_ratio=0.25)
    bundle = b.save(ctx.work_dir / "src")
    orig_text = bundle.pcb_path.read_text(encoding="utf-8")
    rules_path = write_json_file(ctx.work_dir / "rules.json", STD_RULES)

    # Probe baseline roundrect support first
    probe_req = ctx.work_dir / "probe.req.json"
    probe_exp = ctx.json_driver.export_board(bundle.pcb_path, probe_req, rules_path)
    assert_no_crash(probe_exp.proc, "probe_export")
    if probe_exp.proc.returncode == 2 and (probe_exp.stderr_json or {}).get("status") == "unsupported":
        raise PendingCapability(f"roundrect unsupported: {probe_exp.stderr_json}")
    assert probe_exp.proc.returncode == 0
    probe_plan = ctx.work_dir / "probe.plan.json"
    probe_eng = ctx.json_driver.run_engine(probe_req, probe_plan)
    assert_no_crash(probe_eng.proc, "probe_engine")
    assert probe_eng.proc.returncode == 0 and probe_eng.output_json is not None

    for tag, inject in (("drill", " (drill 0.1)"), ("padstack", " (padstack default)")):
        patched_text, count = re.subn(
            r'(\(pad "1" smd (?:oval|roundrect))',
            rf"\1{inject}",
            orig_text,
            count=1,
        )
        assert count == 1
        bad_pcb = ctx.work_dir / f"bad_{tag}.kicad_pcb"
        bad_pcb.write_text(patched_text, encoding="utf-8")
        req_out = ctx.work_dir / f"bad_{tag}.req.json"
        exp = ctx.json_driver.export_board(bad_pcb, req_out, rules_path)
        assert_no_crash(exp.proc, f"export_{tag}")
        assert exp.proc.returncode == 2
        assert exp.stderr_json is not None and exp.stderr_json["status"] == "unsupported"
        assert tag in exp.stderr_json["reason"].lower()
        assert not req_out.exists()

        plan_obj = dict(probe_eng.output_json)
        plan_obj["source_id"] = sha256_file(bad_pcb)
        plan_for_bad = write_json_file(ctx.work_dir / f"plan_{tag}.json", plan_obj)
        bad_out = ctx.work_dir / f"bad_{tag}.out.kicad_pcb"
        app = ctx.json_driver.apply_plan(bad_pcb, plan_for_bad, bad_out, probe_req)
        assert_no_crash(app.proc, f"apply_{tag}")
        assert app.proc.returncode == 2
        assert app.stderr_json is not None and app.stderr_json["status"] == "unsupported"
        assert not bad_out.exists()
    return {"checked": ["drill", "padstack"]}


@register_test(
    test_id="T2-F2-04",
    tier=2,
    feature="F2",
    min_stage="M1",
    name="Out-of-range / non-finite roundrect_rratio rejected",
)
def t2_f2_04(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(seed=1240)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    for idx, x in enumerate((3_000_000, 17_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
        b.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), shape=pn.PAD_SHAPE_ROUNDRECT, size_nm=(2_000_000, 1_000_000), roundrect_ratio=0.25)
    bundle = b.save(ctx.work_dir / "src")
    orig_text = bundle.pcb_path.read_text(encoding="utf-8")
    rules_path = write_json_file(ctx.work_dir / "rules.json", STD_RULES)

    probe_exp = ctx.json_driver.export_board(bundle.pcb_path, ctx.work_dir / "probe.req.json", rules_path)
    assert_no_crash(probe_exp.proc, "probe_export")
    if probe_exp.proc.returncode == 2 and (probe_exp.stderr_json or {}).get("status") == "unsupported":
        raise PendingCapability(f"roundrect unsupported: {probe_exp.stderr_json}")

    for bad_ratio in ("-0.01", "0.500001", "1", "nan", "inf"):
        patched = orig_text.replace("(roundrect_rratio 0.25)", f"(roundrect_rratio {bad_ratio})", 1)
        bad_pcb = ctx.work_dir / f"bad_r_{bad_ratio}.kicad_pcb"
        bad_pcb.write_text(patched, encoding="utf-8")
        req_out = ctx.work_dir / f"bad_r_{bad_ratio}.req.json"
        exp = ctx.json_driver.export_board(bad_pcb, req_out, rules_path)
        assert_no_crash(exp.proc, f"export_r_{bad_ratio}")
        assert exp.proc.returncode == 2
        assert exp.stderr_json is not None and exp.stderr_json["status"] == "invalid"
        assert "radius" in exp.stderr_json["reason"].lower()

    for bad_r in (-1, 500_001):
        doc = build_input1_json(
            pads=[
                {"id": canonical_uuid(1), "net": 1, "at": [3_000_000, 10_000_000], "shape": {"kind": "round_rect", "half_size_nm": [1_000_000, 500_000], "radius_nm": bad_r}},
                {"id": canonical_uuid(2), "net": 1, "at": [17_000_000, 10_000_000], "shape": {"kind": "circle", "radius_nm": 500_000}},
            ]
        )
        req_p = write_json_file(ctx.work_dir / f"bad_json_r_{bad_r}.json", doc)
        eng = ctx.json_driver.run_engine(req_p, ctx.work_dir / f"bad_json_r_{bad_r}.plan.json")
        assert_no_crash(eng.proc, f"engine_r_{bad_r}")
        assert eng.proc.returncode == 2
        assert eng.output_json is not None and eng.output_json["status"] == "invalid"
    return {"checked_ratios": ["-0.01", "0.500001", "1", "nan", "inf", -1, 500_001]}


@register_test(
    test_id="T2-F2-05",
    tier=2,
    feature="F2",
    min_stage="M1",
    name="Sub-nanometre dimension precision & quoted-string positive control",
)
def t2_f2_05(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(seed=1250)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    for idx, x in enumerate((3_000_000, 17_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
        b.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), shape=pn.PAD_SHAPE_OVAL, size_nm=(2_000_000, 1_000_000))
    bundle = b.save(ctx.work_dir / "src")
    orig_text = bundle.pcb_path.read_text(encoding="utf-8")
    rules_path = write_json_file(ctx.work_dir / "rules.json", STD_RULES)

    probe_exp = ctx.json_driver.export_board(bundle.pcb_path, ctx.work_dir / "probe.req.json", rules_path)
    assert_no_crash(probe_exp.proc, "probe_export")
    if probe_exp.proc.returncode == 2 and (probe_exp.stderr_json or {}).get("status") == "unsupported":
        raise PendingCapability(f"oval unsupported: {probe_exp.stderr_json}")

    bad_dims = [
        "0.0000001 1",
        "1.000000000000000000000000000001 1",
        "201 1",
        "nan 1",
        "inf 1",
    ]
    for idx, dims in enumerate(bad_dims):
        bad_pcb = ctx.work_dir / f"bad_dim_{idx}.kicad_pcb"
        bad_pcb.write_text(orig_text.replace("(size 2 1)", f"(size {dims})", 1), encoding="utf-8")
        exp = ctx.json_driver.export_board(bad_pcb, ctx.work_dir / f"bad_dim_{idx}.req.json", rules_path)
        assert_no_crash(exp.proc, f"export_dim_{idx}")
        assert exp.proc.returncode == 2
        assert exp.stderr_json is not None and exp.stderr_json["status"] == "invalid"
        assert "pad dimensions" in exp.stderr_json["reason"]

    # Positive control: quoted string containing "(pad x (size 0 1) (roundrect_rratio 9))"
    ctrl_dir = ctx.work_dir / "quoted_ctrl"
    ctrl_dir.mkdir(parents=True, exist_ok=True)
    ctrl_pcb = ctrl_dir / "board.kicad_pcb"
    ctrl_text = orig_text.replace(
        "(footprint",
        '(property "note" "(pad x (size 0 1) (roundrect_rratio 9))")\n\t(footprint',
        1,
    )
    ctrl_pcb.write_text(ctrl_text, encoding="utf-8")
    out_ctrl = ctx.work_dir / "quoted_out" / "routed.kicad_pcb"
    out_ctrl.parent.mkdir(parents=True, exist_ok=True)
    res, before_hashes = run_wire_or_pending(ctx, ctrl_pcb, out_ctrl, STD_RULES, tag="quoted_ctrl")
    assert res.stage_failed is None
    assert res.engine_res is not None and res.engine_res.output_json is not None
    assert res.export_res.output_bytes is not None
    return assert_clean_e2e(
        ctrl_pcb,
        out_ctrl,
        before_hashes=before_hashes,
        source_dir=ctrl_dir,
        plan_json=res.engine_res.output_json,
        request_bytes=res.export_res.output_bytes,
    )


@register_test(
    test_id="T2-F2-06",
    tier=2,
    feature="F2",
    min_stage="M1",
    name="Near-rounded-corner clearance false-complete plan rejection (C-M1-04)",
)
def t2_f2_06(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(seed=1260)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    for idx, x in enumerate((3_000_000, 17_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
        b.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), shape=pn.PAD_SHAPE_ROUNDRECT, size_nm=(2_000_000, 1_000_000), roundrect_ratio=0.25)
    b.add_track("OBSTACLE", (1_000_000, 1_000_000), (2_000_000, 1_000_000), 250_000, locked=True)
    bundle = b.save(ctx.work_dir / "src")
    assert read_native_connectivity(bundle.pcb_path).unconnected_count == 1
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    res, before_hashes = run_wire_or_pending(ctx, bundle.pcb_path, out_pcb, STD_RULES)
    assert res.stage_failed is None
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

    # Tamper plan with OBSTACLE (net 2) track intruding into rounded corner clearance
    obs_code = next(n["id"] for n in res.export_res.output_json["nets"] if n["name"] == "OBSTACLE")
    bad_plan = dict(res.engine_res.output_json)
    bad_plan["added_tracks"] = list(bad_plan["added_tracks"]) + [
        {
            "id": canonical_uuid(9999),
            "net": obs_code,
            "start": [18_160_000, 10_640_000],
            "end": [18_160_000, 11_450_000],
            "width_nm": 250_000,
            "locked": False,
        }
    ]
    bad_plan_path = write_json_file(ctx.work_dir / "bad_plan.json", bad_plan)
    bad_out = ctx.work_dir / "out" / "tampered.kicad_pcb"
    app_bad = ctx.json_driver.apply_plan(bundle.pcb_path, bad_plan_path, bad_out, res.request_path)
    assert app_bad.proc.returncode == 2
    assert app_bad.stderr_json is not None and app_bad.stderr_json["status"] == "native_validation_failed"
    assert "DRC violations" in app_bad.stderr_json["reason"]
    assert not bad_out.exists()
    return {"rejected_reason": app_bad.stderr_json["reason"]}


@register_test(
    test_id="T2-F2-07",
    tier=2,
    feature="F2",
    min_stage="M1",
    name="Non-orthogonal (45 deg) rounded pad & PAD_SHAPE_CUSTOM baseline refusal",
)
def t2_f2_07(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    rules_path = write_json_file(ctx.work_dir / "rules.json", STD_RULES)

    # Control: 0 deg oval must work at >= M1 (else PendingCapability at BASELINE)
    b0 = BoardBuilder(seed=1270)
    b0.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    for idx, x in enumerate((3_000_000, 17_000_000), 1):
        fp = b0.add_footprint(f"J{idx}", (x, 10_000_000))
        b0.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), shape=pn.PAD_SHAPE_OVAL, size_nm=(2_000_000, 1_000_000))
    bundle0 = b0.save(ctx.work_dir / "ctrl0")
    exp0 = ctx.json_driver.export_board(bundle0.pcb_path, ctx.work_dir / "ctrl0.req.json", rules_path)
    assert_no_crash(exp0.proc, "ctrl0")
    if exp0.proc.returncode == 2 and (exp0.stderr_json or {}).get("status") == "unsupported":
        raise PendingCapability(f"oval unsupported: {exp0.stderr_json}")
    assert exp0.proc.returncode == 0

    # 45 deg oval/roundrect (unsupported at M1-M2; may be supported at M3+)
    for shape in (pn.PAD_SHAPE_OVAL, pn.PAD_SHAPE_ROUNDRECT):
        b45 = BoardBuilder(seed=1271 + shape)
        b45.set_rect_outline(0, 0, 20_000_000, 20_000_000)
        fp1 = b45.add_footprint("J1", (3_000_000, 10_000_000))
        b45.add_pad(fp1, "1", "SIGNAL", (3_000_000, 10_000_000), shape=shape, size_nm=(2_000_000, 1_000_000), angle_deg=45.0, roundrect_ratio=0.25)
        fp2 = b45.add_footprint("J2", (17_000_000, 10_000_000))
        b45.add_pad(fp2, "1", "SIGNAL", (17_000_000, 10_000_000), shape=pn.PAD_SHAPE_OVAL, size_nm=(2_000_000, 1_000_000))
        bundle45 = b45.save(ctx.work_dir / f"ang45_{shape}")
        exp45 = ctx.json_driver.export_board(bundle45.pcb_path, ctx.work_dir / f"ang45_{shape}.req.json", rules_path)
        assert_no_crash(exp45.proc, f"ang45_{shape}")
        if not ctx.stage_at_least("M3"):
            assert exp45.proc.returncode == 2
            assert exp45.stderr_json is not None and exp45.stderr_json["status"] == "unsupported"

    # PAD_SHAPE_CUSTOM fail-closed
    bc = BoardBuilder(seed=1275)
    bc.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    fp1 = bc.add_footprint("J1", (4_000_000, 10_000_000))
    bc.add_pad(
        fp1,
        "1",
        "SIGNAL",
        (4_000_000, 10_000_000),
        shape=pn.PAD_SHAPE_CUSTOM,
        size_nm=(1_000_000, 1_000_000),
        custom_poly_nm=[(0, 0), (1_500_000, 0), (0, 1_500_000)],
    )
    fp2 = bc.add_footprint("J2", (16_000_000, 10_000_000))
    bc.add_pad(fp2, "1", "SIGNAL", (16_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000))
    bundle_c = bc.save(ctx.work_dir / "custom")
    exp_c = ctx.json_driver.export_board(bundle_c.pcb_path, ctx.work_dir / "custom.req.json", rules_path)
    assert_no_crash(exp_c.proc, "custom_pad")
    assert exp_c.proc.returncode == 2
    assert exp_c.stderr_json is not None and exp_c.stderr_json["status"] == "unsupported"
    return {"checked": ["45_deg_oval", "45_deg_roundrect", "PAD_SHAPE_CUSTOM"]}


# ============================================================================
# Feature 3 — Lane 2: Spatial Index (T2-F3-01 .. T2-F3-05)
# ============================================================================


@register_test(
    test_id="T2-F3-01",
    tier=2,
    feature="F3",
    min_stage="M2",
    name="Post-rollback work_used and ID determinism across failed batches",
)
def t2_f3_01(ctx: DriverContext) -> dict[str, Any]:
    n1_pads = [
        {"id": canonical_uuid(1), "net": 1, "at": [2_000_000, 26_000_000], "shape": {"kind": "circle", "radius_nm": 500_000}},
        {"id": canonical_uuid(2), "net": 1, "at": [28_000_000, 26_000_000], "shape": {"kind": "circle", "radius_nm": 500_000}},
    ]
    n2_pads = [
        {"id": canonical_uuid(3), "net": 2, "at": [2_000_000, 6_000_000], "shape": {"kind": "circle", "radius_nm": 500_000}},
        {"id": canonical_uuid(4), "net": 2, "at": [28_000_000, 6_000_000], "shape": {"kind": "circle", "radius_nm": 500_000}},
    ]
    wall = [{"id": canonical_uuid(5), "net": 0, "start": [15_000_000, 20_000_000], "end": [15_000_000, 29_000_000], "width_nm": 250_000, "locked": True}]

    req_n2 = write_json_file(
        ctx.work_dir / "n2.json",
        build_input1_json(bounds_max=(30_000_000, 30_000_000), nets=[{"id": 2, "name": "N2"}], pads=n2_pads, max_expansions=100_000),
    )
    req_fail = write_json_file(
        ctx.work_dir / "fail.json",
        build_input1_json(bounds_max=(30_000_000, 30_000_000), nets=[{"id": 0, "name": ""}, {"id": 1, "name": "N1"}, {"id": 2, "name": "N2"}], pads=n1_pads + n2_pads, tracks=wall, max_expansions=50),
    )
    req_full = write_json_file(
        ctx.work_dir / "full.json",
        build_input1_json(bounds_max=(30_000_000, 30_000_000), nets=[{"id": 0, "name": ""}, {"id": 1, "name": "N1"}, {"id": 2, "name": "N2"}], pads=n1_pads + n2_pads, tracks=wall, max_expansions=100_000),
    )

    c1 = ctx.json_driver.run_engine(req_n2, ctx.work_dir / "c1.plan.json")
    f1 = ctx.json_driver.run_engine(req_fail, ctx.work_dir / "f1.plan.json")
    full1 = ctx.json_driver.run_engine(req_full, ctx.work_dir / "full1.plan.json")
    full2 = ctx.json_driver.run_engine(req_full, ctx.work_dir / "full2.plan.json")
    c2 = ctx.json_driver.run_engine(req_n2, ctx.work_dir / "c2.plan.json")
    for label, r in (("c1", c1), ("f1", f1), ("full1", full1), ("full2", full2), ("c2", c2)):
        assert_no_crash(r.proc, label)

    assert c1.proc.returncode == 0 and c2.proc.returncode == 0
    assert c1.output_bytes == c2.output_bytes
    assert f1.proc.returncode == 4 and f1.output_json is not None and f1.output_json["added_tracks"] == []
    assert full1.proc.returncode == 0 and full2.proc.returncode == 0
    assert full1.output_bytes == full2.output_bytes
    if not ctx.stage_at_least("M2"):
        raise PendingCapability("M2 spatial index rollback determinism gate pending")
    return {"work_used": full1.output_json["work_used"] if full1.output_json else None}


@register_test(
    test_id="T2-F3-02",
    tier=2,
    feature="F3",
    min_stage="M2",
    name="Dense neighborhood work-exhaustion boundary (C-SP-03)",
)
def t2_f3_02(ctx: DriverContext) -> dict[str, Any]:
    pads = [
        {"id": canonical_uuid(1), "net": 1, "at": [-20_000_000, 0], "shape": {"kind": "circle", "radius_nm": 500_000}},
        {"id": canonical_uuid(2), "net": 1, "at": [95_000_000, 0], "shape": {"kind": "circle", "radius_nm": 500_000}},
    ]
    for i in range(2000):
        col = i % 40
        row = i // 40
        pads.append({
            "id": canonical_uuid(100 + i),
            "net": 0,
            "at": [10_000_000 + col * 2_000_000, -50_000_000 + row * 2_000_000],
            "shape": {"kind": "circle", "radius_nm": 250_000},
        })
    req = write_json_file(
        ctx.work_dir / "dense_tight.json",
        build_input1_json(
            bounds_min=(-100_000_000, -100_000_000),
            bounds_max=(100_000_000, 100_000_000),
            pads=pads,
            max_expansions=100_000,
            limits={"max_work": 5_000},
        ),
    )
    res = ctx.json_driver.run_engine(req, ctx.work_dir / "dense_tight.plan.json", timeout_s=15.0)
    assert_no_crash(res.proc, "dense_tight")
    assert not res.proc.timed_out
    assert res.proc.returncode == 5
    pj = res.output_json
    assert pj is not None and pj["status"] == "work_exhausted"
    assert pj["added_tracks"] == [] and pj["removed_track_ids"] == []
    assert 0 < pj["work_used"] <= 5_000
    if not ctx.stage_at_least("M2"):
        raise PendingCapability("M2 spatial index milestone pending", evidence={"work_used": pj["work_used"]})
    return {"work_used": pj["work_used"], "wall_s": res.proc.wall_measured_s}


@register_test(
    test_id="T2-F3-03",
    tier=2,
    feature="F3",
    min_stage="M2",
    name="Tiny (1 nm) & boundary-touching items without i128 overflow",
)
def t2_f3_03(ctx: DriverContext) -> dict[str, Any]:
    rules = {"width_nm": 1, "clearance_nm": 0, "grid_nm": 1_000_000, "edge_clearance_nm": 0}
    doc = build_input1_json(
        bounds_min=(-100_000_000, -100_000_000),
        bounds_max=(100_000_000, 100_000_000),
        rules=rules,
        nets=[{"id": 0, "name": ""}, {"id": 1, "name": "SIGNAL"}],
        pads=[
            {"id": canonical_uuid(1), "net": 1, "at": [-100_000_000, -100_000_000], "shape": {"kind": "circle", "radius_nm": 1}},
            {"id": canonical_uuid(2), "net": 1, "at": [100_000_000, 100_000_000], "shape": {"kind": "rect", "half_size_nm": [1, 1]}},
            {"id": canonical_uuid(3), "net": 0, "at": [0, 0], "shape": {"kind": "circle", "radius_nm": 1}},
        ],
        tracks=[
            {"id": canonical_uuid(4), "net": 0, "start": [-100_000_000, 0], "end": [-100_000_000, 1_000], "width_nm": 1, "locked": True}
        ],
    )
    req = write_json_file(ctx.work_dir / "extreme.json", doc)
    res = ctx.json_driver.run_engine(req, ctx.work_dir / "extreme.plan.json")
    assert_no_crash(res.proc, "extreme_domain")
    assert res.proc.returncode in (0, 3), f"unexpected returncode {res.proc.returncode}: {res.output_json}"

    # Zero-length track must exit 2 invalid, never panic
    bad_doc = build_input1_json(
        tracks=[{"id": canonical_uuid(5), "net": 0, "start": [0, 0], "end": [0, 0], "width_nm": 250_000, "locked": True}]
    )
    bad_req = write_json_file(ctx.work_dir / "zero_len.json", bad_doc)
    bad_res = ctx.json_driver.run_engine(bad_req, ctx.work_dir / "zero_len.plan.json")
    assert_no_crash(bad_res.proc, "zero_len")
    assert bad_res.proc.returncode == 2
    assert bad_res.output_json is not None and bad_res.output_json["status"] == "invalid"
    if not ctx.stage_at_least("M2"):
        raise PendingCapability("M2 spatial index milestone pending", evidence={"extreme_rc": res.proc.returncode})
    assert res.proc.returncode == 0
    return {"extreme_rc": res.proc.returncode}


@register_test(
    test_id="T2-F3-04",
    tier=2,
    feature="F3",
    min_stage="M2",
    name="Near-maximum item admission (max_items boundary)",
)
def t2_f3_04(ctx: DriverContext) -> dict[str, Any]:
    pads = [
        {"id": canonical_uuid(1), "net": 1, "at": [2_000_000, 10_000_000], "shape": {"kind": "circle", "radius_nm": 500_000}},
        {"id": canonical_uuid(2), "net": 1, "at": [18_000_000, 10_000_000], "shape": {"kind": "circle", "radius_nm": 500_000}},
    ]
    tracks = [
        {"id": canonical_uuid(10 + i), "net": 0, "start": [2_000_000 + i * 1_000_000, 2_000_000], "end": [2_500_000 + i * 1_000_000, 2_000_000], "width_nm": 250_000, "locked": True}
        for i in range(10)
    ]
    # base_items = 2 nets + 2 pads + 10 tracks = 14
    outcomes = {}
    for max_items, expected_rc in ((13, 6), (14, 6), (15, 0)):
        doc = build_input1_json(
            nets=[{"id": 0, "name": ""}, {"id": 1, "name": "SIGNAL"}],
            pads=pads,
            tracks=tracks,
            limits={"max_items": max_items},
        )
        req = write_json_file(ctx.work_dir / f"lim_{max_items}.json", doc)
        res = ctx.json_driver.run_engine(req, ctx.work_dir / f"lim_{max_items}.plan.json")
        assert_no_crash(res.proc, f"max_items_{max_items}")
        assert res.proc.returncode == expected_rc, f"max_items={max_items}: rc={res.proc.returncode}, json={res.output_json}"
        outcomes[max_items] = res.output_json
        if expected_rc == 6:
            assert res.output_json is not None and res.output_json["status"] == "admission_exceeded"
            assert res.output_json["added_tracks"] == []
        else:
            assert res.output_json is not None and len(res.output_json["added_tracks"]) == 1
    if not ctx.stage_at_least("M2"):
        raise PendingCapability("M2 spatial index milestone pending")
    return {"tested_max_items": [13, 14, 15]}


@register_test(
    test_id="T2-F3-05",
    tier=2,
    feature="F3",
    min_stage="M2",
    name="1 nm clearance exact equality vs 1 nm intrusion rejection",
)
def t2_f3_05(ctx: DriverContext) -> dict[str, Any]:
    # (a) exact 1_250_000 nm center-to-center -> routes straight along y=10M
    # (b) 1 nm intrusion top at 11_249_999 -> detours cleanly
    # (c) full vertical wall with 1 nm gap -> exits 3 unroutable
    for mode, walls, expect_rc in (
        ("exact_eq", [((8_000_000, 11_250_000), (12_000_000, 11_250_000)), ((8_000_000, 8_750_000), (12_000_000, 8_750_000))], 0),
        ("intrude_detour", [((8_000_000, 11_249_999), (12_000_000, 11_249_999)), ((8_000_000, 8_750_000), (12_000_000, 8_750_000))], 0),
        ("intrude_wall", [((10_000_000, 11_249_999), (10_000_000, 19_375_000)), ((10_000_000, 625_000), (10_000_000, 8_750_000))], 3),
    ):
        sub = ctx.work_dir / mode
        b = BoardBuilder(seed=1350)
        b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
        for idx, x in enumerate((2_000_000, 18_000_000), 1):
            fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
            b.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), size_nm=(1_000_000, 1_000_000))
        for s, e in walls:
            b.add_track("", s, e, 250_000, locked=True)
        bundle = b.save(sub / "src")
        out_pcb = sub / "out" / "routed.kicad_pcb"
        out_pcb.parent.mkdir(parents=True, exist_ok=True)
        res, before_hashes = run_wire_or_pending(
            ctx, bundle.pcb_path, out_pcb, STD_RULES, tag=mode, allow_unsupported_engine=False
        )
        assert res.engine_res is not None and res.engine_res.output_json is not None
        assert res.engine_res.proc.returncode == expect_rc
        if expect_rc == 0:
            assert res.stage_failed is None
            assert res.export_res.output_bytes is not None
            assert_clean_e2e(
                bundle.pcb_path,
                out_pcb,
                before_hashes=before_hashes,
                source_dir=bundle.directory,
                plan_json=res.engine_res.output_json,
                request_bytes=res.export_res.output_bytes,
            )
            ys = {p[1] for t in res.engine_res.output_json["added_tracks"] for p in (t["start"], t["end"])}
            if mode == "exact_eq":
                assert ys == {10_000_000}
            else:
                assert ys != {10_000_000}
        else:
            assert res.engine_res.output_json["added_tracks"] == []
    if not ctx.stage_at_least("M2"):
        raise PendingCapability("M2 spatial index milestone pending")
    return {"checked": ["exact_eq", "intrude_detour", "intrude_wall"]}


# ============================================================================
# Feature 4 — Lane 4: Native Orchestration (T2-F4-01 .. T2-F4-07)
# ============================================================================


@register_test(
    test_id="T2-F4-01",
    tier=2,
    feature="F4",
    min_stage="M2",
    name="Original-path .kicad_pro/.kicad_dru detection before staging",
)
def t2_f4_01(ctx: DriverContext) -> dict[str, Any]:
    probe = ctx.native_driver.probe()
    if not probe.supported:
        raise PendingCapability(probe.reason)

    rules_path = write_json_file(ctx.work_dir / "rules.json", STD_RULES)
    # (a) malformed .kicad_dru
    b_a = BoardBuilder(seed=1401)
    b_a.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    for idx, x in enumerate((3_000_000, 17_000_000), 1):
        fp = b_a.add_footprint(f"J{idx}", (x, 10_000_000))
        b_a.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), size_nm=(1_000_000, 1_000_000))
    b_a.set_dru_text('(version 1)\n(rule "broken_unclosed" (constraint clearance')
    bundle_a = b_a.save(ctx.work_dir / "src_a")
    bh_a = snapshot_source_dir(bundle_a.directory)
    out_a = ctx.work_dir / "out_a" / "routed.kicad_pcb"
    out_a.parent.mkdir(parents=True, exist_ok=True)
    proc_a = ctx.native_driver.run_native(bundle_a.pcb_path, out_a, rules_path=rules_path)
    assert_no_crash(proc_a, "dru_malformed")
    assert proc_a.returncode == 2
    assert not out_a.exists()
    assert_source_dir_untouched(bundle_a.directory, bh_a)

    # (b) tight 5.0mm clearance .kicad_dru beside narrow board
    b_b = BoardBuilder(seed=1402)
    b_b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    for idx, x in enumerate((3_000_000, 17_000_000), 1):
        fp = b_b.add_footprint(f"J{idx}", (x, 10_000_000))
        b_b.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), size_nm=(1_000_000, 1_000_000))
    b_b.add_track("OBSTACLE", (10_000_000, 4_000_000), (10_000_000, 16_000_000), 250_000, locked=True)
    b_b.set_dru_text('(version 1)\n(rule "tight_5mm" (constraint clearance (min 5.0mm)))\n')
    bundle_b = b_b.save(ctx.work_dir / "src_b")
    bh_b = snapshot_source_dir(bundle_b.directory)
    out_b = ctx.work_dir / "out_b" / "routed.kicad_pcb"
    out_b.parent.mkdir(parents=True, exist_ok=True)
    proc_b = ctx.native_driver.run_native(bundle_b.pcb_path, out_b, rules_path=rules_path)
    assert_no_crash(proc_b, "dru_tight")
    assert proc_b.returncode in (2, 3), f"expected exit 2 or 3, got {proc_b.returncode}"
    assert not out_b.exists()
    assert_source_dir_untouched(bundle_b.directory, bh_b)
    return {"rc_a": proc_a.returncode, "rc_b": proc_b.returncode}


@register_test(
    test_id="T2-F4-02",
    tier=2,
    feature="F4",
    min_stage="M2",
    name="Atomic O_EXCL no-overwrite & PID-collision safety (F-M5-N4)",
)
def t2_f4_02(ctx: DriverContext) -> dict[str, Any]:
    probe = ctx.native_driver.probe()
    if not probe.supported or probe.subcommand is None:
        raise PendingCapability(probe.reason)

    bundle = build_std_2pad_board(ctx.work_dir / "src", seed=1403)
    rules_path = write_json_file(ctx.work_dir / "rules.json", STD_RULES)

    # (a) Pre-existing destination must never be overwritten
    out_existing = ctx.work_dir / "out_a" / "existing.kicad_pcb"
    out_existing.parent.mkdir(parents=True, exist_ok=True)
    sentinel = b"existing owner output\n"
    out_existing.write_bytes(sentinel)
    proc_a = ctx.native_driver.run_native(bundle.pcb_path, out_existing, rules_path=rules_path)
    assert_no_crash(proc_a, "existing_dest")
    assert proc_a.returncode in (2, 8)
    assert out_existing.read_bytes() == sentinel

    # (b) Foreign PID-named temp collision via execv wrapper preserving PID
    out_b_dir = ctx.work_dir / "out_b"
    out_b_dir.mkdir(parents=True, exist_ok=True)
    out_b = out_b_dir / "routed.kicad_pcb"
    wrapper = ctx.work_dir / "pid_exec.py"
    wrapper.write_text(
        "import os, pathlib, sys\n"
        "out_p = pathlib.Path(sys.argv[1])\n"
        "tmp = out_p.with_name(f'.{out_p.name}.{os.getpid()}.tmp')\n"
        "tmp.write_bytes(b'foreign temporary')\n"
        "os.execv(sys.argv[2], sys.argv[2:])\n",
        encoding="utf-8",
    )
    from tests.e2e_rust_router.drivers import run_measured

    bh = snapshot_source_dir(bundle.directory)
    proc_b = run_measured(
        [
            "/usr/bin/python3",
            str(wrapper),
            str(out_b),
            str(ctx.provenance.binary_path),
            probe.subcommand,
            str(bundle.pcb_path),
            str(out_b),
            "--rules",
            str(rules_path),
        ],
        cwd=ctx.repo_root,
        work_dir=ctx.work_dir / "metrics",
    )
    assert_no_crash(proc_b, "pid_collision")
    tmp_matches = list(out_b_dir.glob(".routed.kicad_pcb.*.tmp"))
    assert tmp_matches and any(p.read_bytes() == b"foreign temporary" for p in tmp_matches)
    if proc_b.returncode == 0:
        assert_clean_e2e(bundle.pcb_path, out_b, before_hashes=bh, source_dir=bundle.directory)
    else:
        assert proc_b.returncode == 8
        assert not out_b.exists()
    return {"rc_a": proc_a.returncode, "rc_b": proc_b.returncode}


@register_test(
    test_id="T2-F4-03",
    tier=2,
    feature="F4",
    min_stage="BASELINE",
    name="Stale request_sha256 & same-board A/B mismatch refusal",
)
def t2_f4_03(ctx: DriverContext) -> dict[str, Any]:
    bundle = build_std_2pad_board(ctx.work_dir / "src", seed=1404)
    before_hashes = snapshot_source_dir(bundle.directory)
    rules_a = write_json_file(ctx.work_dir / "rules_a.json", STD_RULES)
    req_a = ctx.work_dir / "req_a.json"
    plan_a = ctx.work_dir / "plan_a.json"
    exp_a = ctx.json_driver.export_board(bundle.pcb_path, req_a, rules_a)
    assert exp_a.proc.returncode == 0 and exp_a.output_bytes is not None
    eng_a = ctx.json_driver.run_engine(req_a, plan_a)
    assert eng_a.proc.returncode == 0 and eng_a.output_json is not None

    # Request B variants (same source_id, different rules/limits/bytes)
    rules_b = write_json_file(ctx.work_dir / "rules_b.json", {**STD_RULES, "width_nm": 500_000})
    lim_b = write_json_file(ctx.work_dir / "lim_b.json", {"max_work": 1})
    req_b1 = ctx.work_dir / "req_b1.json"
    req_b2 = ctx.work_dir / "req_b2.json"
    req_b3 = ctx.work_dir / "req_b3.json"
    req_b4 = ctx.work_dir / "req_b4.json"
    assert ctx.json_driver.export_board(bundle.pcb_path, req_b1, rules_b).proc.returncode == 0
    assert ctx.json_driver.export_board(bundle.pcb_path, req_b2, rules_a, limits_path=lim_b).proc.returncode == 0
    assert ctx.json_driver.export_board(bundle.pcb_path, req_b3, rules_a, max_expansions=1).proc.returncode == 0
    req_b4.write_bytes(exp_a.output_bytes + b" ")

    out_dir = ctx.work_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    for idx, rb in enumerate((req_b1, req_b2, req_b3, req_b4)):
        bad_out = out_dir / f"mismatch_{idx}.kicad_pcb"
        app = ctx.json_driver.apply_plan(bundle.pcb_path, plan_a, bad_out, rb)
        assert_no_crash(app.proc, f"mismatch_{idx}")
        assert app.proc.returncode == 2
        assert app.stderr_json is not None and app.stderr_json["status"] == "invalid"
        assert "request_sha256" in app.stderr_json["reason"]
        assert not bad_out.exists()

    # Tampered request_sha256 values on plan
    digest = eng_a.output_json["request_sha256"]
    tamper_vals: list[Any] = [digest.upper(), digest + " ", "0" * 64, None, 123]
    for idx, val in enumerate(tamper_vals):
        bad_p = dict(eng_a.output_json)
        bad_p["request_sha256"] = val
        bad_pp = write_json_file(ctx.work_dir / f"bad_sha_{idx}.json", bad_p)
        bad_out = out_dir / f"bad_sha_{idx}.kicad_pcb"
        app = ctx.json_driver.apply_plan(bundle.pcb_path, bad_pp, bad_out, req_a)
        assert app.proc.returncode == 2
        assert app.stderr_json is not None and app.stderr_json["status"] == "invalid"
        assert "request_sha256" in app.stderr_json["reason"]
        assert not bad_out.exists()

    missing_p = dict(eng_a.output_json)
    missing_p.pop("request_sha256", None)
    missing_pp = write_json_file(ctx.work_dir / "missing_sha.json", missing_p)
    app_m = ctx.json_driver.apply_plan(bundle.pcb_path, missing_pp, out_dir / "m.kicad_pcb", req_a)
    assert app_m.proc.returncode == 2 and app_m.stderr_json is not None and "request_sha256" in app_m.stderr_json["reason"]

    # Positive control Request A + plan_a
    good_out = out_dir / "good.kicad_pcb"
    app_ok = ctx.json_driver.apply_plan(bundle.pcb_path, plan_a, good_out, req_a)
    assert app_ok.proc.returncode == 0
    return assert_clean_e2e(
        bundle.pcb_path,
        good_out,
        before_hashes=before_hashes,
        source_dir=bundle.directory,
        plan_json=eng_a.output_json,
        request_bytes=exp_a.output_bytes,
    )


@register_test(
    test_id="T2-F4-04",
    tier=2,
    feature="F4",
    min_stage="BASELINE",
    name="Oversized (16 MiB + 1), duplicate-key, & 500k-depth malformed JSON",
)
def t2_f4_04(ctx: DriverContext) -> dict[str, Any]:
    over = 16 * 1024 * 1024 + 1
    bundle = build_std_2pad_board(ctx.work_dir / "src", seed=1405)
    rules_path = write_json_file(ctx.work_dir / "rules.json", STD_RULES)
    req_ok = ctx.work_dir / "req_ok.json"
    plan_ok = ctx.work_dir / "plan_ok.json"
    assert ctx.json_driver.export_board(bundle.pcb_path, req_ok, rules_path).proc.returncode == 0
    assert ctx.json_driver.run_engine(req_ok, plan_ok).proc.returncode == 0

    # (a) 16 MiB + 1 byte documents
    over_req = ctx.work_dir / "over.req.json"
    req_bytes = req_ok.read_bytes()
    over_req.write_bytes(req_bytes + b" " * (over - len(req_bytes)))
    eng_over = ctx.json_driver.run_engine(over_req, ctx.work_dir / "over.plan.json")
    assert_no_crash(eng_over.proc, "eng_over")
    assert eng_over.proc.returncode == 6
    assert eng_over.output_json is not None and eng_over.output_json["status"] == "admission_exceeded"
    assert eng_over.output_json["request_sha256"] is None
    assert eng_over.output_json["added_tracks"] == []

    over_pcb = ctx.work_dir / "over.kicad_pcb"
    pcb_b = bundle.pcb_path.read_bytes()
    over_pcb.write_bytes(pcb_b + b" " * (over - len(pcb_b)))
    exp_over_pcb = ctx.json_driver.export_board(over_pcb, ctx.work_dir / "eop.json", rules_path)
    assert exp_over_pcb.proc.returncode == 2
    assert exp_over_pcb.stderr_json is not None and exp_over_pcb.stderr_json["status"] == "invalid"
    assert "exceeds 16777216 bytes" in exp_over_pcb.stderr_json["reason"]

    over_rules = ctx.work_dir / "over_rules.json"
    rb = rules_path.read_bytes()
    over_rules.write_bytes(rb + b" " * (over - len(rb)))
    exp_over_r = ctx.json_driver.export_board(bundle.pcb_path, ctx.work_dir / "eor.json", over_rules)
    assert exp_over_r.proc.returncode == 2 and exp_over_r.stderr_json is not None
    assert "exceeds 16777216 bytes" in exp_over_r.stderr_json["reason"]

    over_plan = ctx.work_dir / "over_plan.json"
    pb = plan_ok.read_bytes()
    over_plan.write_bytes(pb + b" " * (over - len(pb)))
    app_over_p = ctx.json_driver.apply_plan(bundle.pcb_path, over_plan, ctx.work_dir / "op.kicad_pcb", req_ok)
    assert app_over_p.proc.returncode == 2 and app_over_p.stderr_json is not None
    assert "exceeds 16777216 bytes" in app_over_p.stderr_json["reason"]

    # (b) Duplicate JSON keys
    dup_req = ctx.work_dir / "dup.req.json"
    dup_req.write_text('{"version":1,"version":1}', encoding="utf-8")
    eng_dup = ctx.json_driver.run_engine(dup_req, ctx.work_dir / "dup.plan.json")
    assert eng_dup.proc.returncode == 2
    assert eng_dup.output_json is not None and eng_dup.output_json["status"] == "invalid"

    dup_lim = ctx.work_dir / "dup_lim.json"
    dup_lim.write_text('{"max_work":10,"max_work":20}', encoding="utf-8")
    exp_dup_l = ctx.json_driver.export_board(bundle.pcb_path, ctx.work_dir / "edl.json", rules_path, limits_path=dup_lim)
    assert exp_dup_l.proc.returncode == 2 and exp_dup_l.stderr_json is not None and exp_dup_l.stderr_json["status"] == "invalid"

    dup_plan = ctx.work_dir / "dup_plan.json"
    dup_plan.write_text('{"version":3,"version":3}', encoding="utf-8")
    app_dup_p = ctx.json_driver.apply_plan(bundle.pcb_path, dup_plan, ctx.work_dir / "dp.kicad_pcb", req_ok)
    assert app_dup_p.proc.returncode == 2 and app_dup_p.stderr_json is not None and app_dup_p.stderr_json["status"] == "invalid"

    # (c) 500,000-deep nested array JSON (inside known "version" key so engine hits recursion limit, not unknown-field)
    deep_file = ctx.work_dir / "deep500k.json"
    deep_file.write_text('{"version":' + "[" * 500_000 + "1" + "]" * 500_000 + "}", encoding="utf-8")
    eng_deep = ctx.json_driver.run_engine(deep_file, ctx.work_dir / "deep.plan.json")
    assert_no_crash(eng_deep.proc, "eng_deep")
    assert eng_deep.proc.returncode == 2
    assert eng_deep.output_json is not None and eng_deep.output_json["status"] == "invalid"
    assert eng_deep.output_json["added_tracks"] == []

    app_deep = ctx.json_driver.apply_plan(bundle.pcb_path, deep_file, ctx.work_dir / "deep.kicad_pcb", req_ok)
    assert_no_crash(app_deep.proc, "app_deep")
    assert app_deep.proc.returncode == 2
    assert app_deep.stderr_json is not None and app_deep.stderr_json["status"] == "invalid"
    assert app_deep.stderr_json["reason"] == "JSON input exceeds parser nesting capacity"
    return {"checked": ["16MiB+1", "duplicate_keys", "depth_500k"]}


@register_test(
    test_id="T2-F4-05",
    tier=2,
    feature="F4",
    min_stage="BASELINE",
    name="Invalid UUIDv8 & bad schema fields refused",
)
def t2_f4_05(ctx: DriverContext) -> dict[str, Any]:
    bundle = build_std_2pad_board(ctx.work_dir / "src", seed=1406)
    rules_path = write_json_file(ctx.work_dir / "rules.json", STD_RULES)
    req_ok = ctx.work_dir / "req_ok.json"
    plan_ok = ctx.work_dir / "plan_ok.json"
    exp = ctx.json_driver.export_board(bundle.pcb_path, req_ok, rules_path)
    assert exp.proc.returncode == 0 and exp.output_json is not None
    eng = ctx.json_driver.run_engine(req_ok, plan_ok)
    assert eng.proc.returncode == 0 and eng.output_json is not None

    # (a) Engine input/1 refusals
    nil_uuid = "00000000-0000-0000-0000-000000000000"
    upper_uuid = "10000000-0000-4000-8000-00000000000A"
    for label, bad_id, exp_status in (
        ("nil", nil_uuid, "invalid"),
        ("upper", upper_uuid, "invalid"),
        ("dup", exp.output_json["pads"][0]["id"], "invalid"),
    ):
        doc = json.loads(req_ok.read_text(encoding="utf-8"))
        doc["pads"][1]["id"] = bad_id
        p = write_json_file(ctx.work_dir / f"in_{label}.json", doc)
        r = ctx.json_driver.run_engine(p, ctx.work_dir / f"in_{label}.plan.json")
        assert_no_crash(r.proc, f"in_{label}")
        assert r.proc.returncode == 2
        assert r.output_json is not None and r.output_json["status"] == exp_status
        assert r.output_json["added_tracks"] == []

    for label, mut in (("extra", {"extra": 1}), ("ver2", {"version": 2})):
        doc = json.loads(req_ok.read_text(encoding="utf-8"))
        doc.update(mut)
        p = write_json_file(ctx.work_dir / f"in_{label}.json", doc)
        r = ctx.json_driver.run_engine(p, ctx.work_dir / f"in_{label}.plan.json")
        assert r.proc.returncode == 2
        assert r.output_json is not None and r.output_json["status"] == "unsupported"

    # (b) Apply result/3 refusals
    bad_plans: list[tuple[str, dict[str, Any]]] = []
    for ver in (1, 2, 4, True, "3"):
        d = dict(eng.output_json)
        d["version"] = ver
        bad_plans.append((f"ver_{ver}", d))
    for label, tid in (("nil", nil_uuid), ("upper", upper_uuid)):
        d = json.loads(plan_ok.read_text(encoding="utf-8"))
        d["added_tracks"][0]["id"] = tid
        bad_plans.append((f"tid_{label}", d))
    d_unr = dict(eng.output_json)
    d_unr["status"] = "unroutable"
    bad_plans.append(("status_unroutable", d_unr))
    for label, wu in (("neg", -1), ("bool", True), ("float", 1.5)):
        d = dict(eng.output_json)
        d["work_used"] = wu
        bad_plans.append((f"wu_{label}", d))

    for label, payload in bad_plans:
        pp = write_json_file(ctx.work_dir / f"bad_p_{label}.json", payload)
        out_p = ctx.work_dir / f"bad_p_{label}.kicad_pcb"
        app = ctx.json_driver.apply_plan(bundle.pcb_path, pp, out_p, req_ok)
        assert_no_crash(app.proc, f"app_{label}")
        assert app.proc.returncode == 2
        assert app.stderr_json is not None and app.stderr_json["status"] == "invalid"
        assert not out_p.exists()
    return {"checked": len(bad_plans) + 5}


@register_test(
    test_id="T2-F4-06",
    tier=2,
    feature="F4",
    min_stage="BASELINE",
    name="Same-input/output path & unwritable destination exit 8",
)
def t2_f4_06(ctx: DriverContext) -> dict[str, Any]:
    from tests.e2e_rust_router.drivers import run_measured

    bundle = build_std_2pad_board(ctx.work_dir / "src", seed=1407)
    rules_path = write_json_file(ctx.work_dir / "rules.json", STD_RULES)
    req_path = ctx.work_dir / "req.json"
    plan_path = ctx.work_dir / "plan.json"
    assert ctx.json_driver.export_board(bundle.pcb_path, req_path, rules_path).proc.returncode == 0
    req_before = req_path.read_bytes()

    # (a) same path, symlink same path, wrong arg count
    sym_path = ctx.work_dir / "sym_req.json"
    sym_path.symlink_to(req_path)
    for args in ([str(req_path), str(req_path)], [str(req_path), str(sym_path)], [str(req_path)]):
        r = run_measured([str(ctx.provenance.binary_path), *args], cwd=ctx.repo_root, work_dir=ctx.work_dir / "m")
        assert r.returncode == 8
        assert "no fresh result established" in r.stderr
        assert req_path.read_bytes() == req_before

    # (b) unwritable output directory
    if os.geteuid() != 0:
        ro_dir = ctx.work_dir / "ro_out"
        ro_dir.mkdir(parents=True, exist_ok=True)
        old_out = ro_dir / "out.json"
        old_out.write_bytes(b"older_result\n")
        try:
            ro_dir.chmod(0o555)
            r_ro = ctx.json_driver.run_engine(req_path, old_out)
            assert r_ro.proc.returncode == 8
            assert "no fresh result established" in r_ro.proc.stderr
        finally:
            ro_dir.chmod(0o755)
        assert old_out.read_bytes() == b"older_result\n"

    # (c) apply with output == source
    assert ctx.json_driver.run_engine(req_path, plan_path).proc.returncode == 0
    board_before = bundle.pcb_path.read_bytes()
    app_same = ctx.json_driver.apply_plan(bundle.pcb_path, plan_path, bundle.pcb_path, req_path)
    assert app_same.proc.returncode == 2
    assert app_same.stderr_json is not None and app_same.stderr_json["status"] == "invalid"
    assert bundle.pcb_path.read_bytes() == board_before
    return {"exit8_verified": True}


@register_test(
    test_id="T2-F4-07",
    tier=2,
    feature="F4",
    min_stage="M2",
    name="External SIGTERM/SIGKILL, deadline timeout, and RLIMIT_AS OOM",
)
def t2_f4_07(ctx: DriverContext) -> dict[str, Any]:
    probe = ctx.native_driver.probe()
    if not probe.supported:
        raise PendingCapability(probe.reason)

    # Large 1200-obstacle board so native execution has non-trivial duration
    b = BoardBuilder(seed=1408)
    b.set_rect_outline(0, 0, 100_000_000, 100_000_000)
    rows = (
        [8_000_000 + 2_000_000 * r for r in range(20)]
        + [48_500_000, 51_500_000]
        + [54_000_000 + 2_000_000 * r for r in range(8)]
    )
    assert len(rows) == 30
    for ix in range(40):
        ox = 10_000_000 + ix * 2_000_000
        for iy, oy in enumerate(rows):
            fp = b.add_footprint(f"D_{ix}_{iy}", (ox, oy))
            b.add_pad(fp, "1", "", (ox, oy), size_nm=(500_000, 500_000))
    for idx, x in enumerate((2_000_000, 98_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 50_000_000))
        b.add_pad(fp, "1", "SIGNAL", (x, 50_000_000), size_nm=(1_000_000, 1_000_000))
    bundle = b.save(ctx.work_dir / "src")
    bh = snapshot_source_dir(bundle.directory)
    rules_path = write_json_file(ctx.work_dir / "rules.json", STD_RULES)

    # (a) SIGTERM & SIGKILL mid-route
    for sig_name, sig in (("sigterm", signal.SIGTERM), ("sigkill", signal.SIGKILL)):
        out_p = ctx.work_dir / f"out_{sig_name}" / "routed.kicad_pcb"
        out_p.parent.mkdir(parents=True, exist_ok=True)
        r = ctx.native_driver.run_native(
            bundle.pcb_path, out_p, rules_path=rules_path, cancel_after_s=0.03, cancel_signal=sig
        )
        assert r.returncode != 0
        assert not out_p.exists()
        assert_source_dir_untouched(bundle.directory, bh)

    # (b) Subprocess timeout
    out_to = ctx.work_dir / "out_to" / "routed.kicad_pcb"
    out_to.parent.mkdir(parents=True, exist_ok=True)
    r_to = ctx.native_driver.run_native(bundle.pcb_path, out_to, rules_path=rules_path, timeout_s=0.02)
    assert r_to.timed_out or r_to.returncode != 0
    assert not out_to.exists()

    # (c) Tight RLIMIT_AS OOM
    out_oom = ctx.work_dir / "out_oom" / "routed.kicad_pcb"
    out_oom.parent.mkdir(parents=True, exist_ok=True)
    r_oom = ctx.native_driver.run_native(
        bundle.pcb_path, out_oom, rules_path=rules_path, rlimit_as_bytes=8 * 1024 * 1024
    )
    assert r_oom.returncode != 0
    assert not out_oom.exists()
    assert_source_dir_untouched(bundle.directory, bh)
    return {"checked": ["SIGTERM", "SIGKILL", "timeout", "RLIMIT_AS"]}


# ============================================================================
# Feature 5 — Extended Pad/Drill/Geometry Parity (T2-F5-01 .. T2-F5-05)
# ============================================================================


@register_test(
    test_id="T2-F5-01",
    tier=2,
    feature="F5",
    min_stage="BASELINE",
    name="Board & footprint Margin geometry rejection (F-G1-Margin)",
)
def t2_f5_01(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    rules_path = write_json_file(ctx.work_dir / "rules.json", STD_RULES)

    # First obtain a valid plan on a clean board to test apply rejection too
    clean = build_std_2pad_board(ctx.work_dir / "clean", seed=1500)
    clean_req = ctx.work_dir / "clean.req.json"
    clean_plan = ctx.work_dir / "clean.plan.json"
    assert ctx.json_driver.export_board(clean.pcb_path, clean_req, rules_path).proc.returncode == 0
    assert ctx.json_driver.run_engine(clean_req, clean_plan).proc.returncode == 0

    for loc in ("board", "footprint"):
        sub = ctx.work_dir / f"margin_{loc}"
        b = BoardBuilder(seed=1501)
        b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
        fp1 = b.add_footprint("J1", (3_000_000, 10_000_000))
        b.add_pad(fp1, "1", "SIGNAL", (3_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000))
        fp2 = b.add_footprint("J2", (17_000_000, 10_000_000))
        b.add_pad(fp2, "1", "SIGNAL", (17_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000))
        b.add_track("OBSTACLE", (10_000_000, 4_000_000), (10_000_000, 16_000_000), 250_000, locked=True)
        b.add_drawing(
            pn.Margin,
            pn.SHAPE_T_SEGMENT,
            (10_000_000, 5_000_000),
            (10_000_000, 15_000_000),
            width_nm=50_000,
            footprint=(fp1 if loc == "footprint" else None),
        )
        bundle = b.save(sub / "src")
        drc = run_native_drc(bundle.pcb_path)
        assert any(v.get("type") == "copper_edge_clearance" for v in drc.violations)

        exp = ctx.json_driver.export_board(bundle.pcb_path, sub / "req.json", rules_path)
        assert_no_crash(exp.proc, f"export_{loc}")
        assert exp.proc.returncode == 2
        assert exp.stderr_json is not None and exp.stderr_json["status"] == "unsupported"
        assert "Margin" in exp.stderr_json["reason"]

        # Apply with matching request_sha256 & source_id must also refuse Margin
        plan_obj = json.loads(clean_plan.read_text(encoding="utf-8"))
        plan_obj["source_id"] = sha256_file(bundle.pcb_path)
        req_for_margin = write_json_file(sub / "req_m.json", json.loads(clean_req.read_text(encoding="utf-8")))
        plan_obj["request_sha256"] = sha256_file(req_for_margin)
        plan_for_margin = write_json_file(sub / "plan_m.json", plan_obj)
        out_p = sub / "out.kicad_pcb"
        app = ctx.json_driver.apply_plan(bundle.pcb_path, plan_for_margin, out_p, req_for_margin)
        assert_no_crash(app.proc, f"apply_{loc}")
        assert app.proc.returncode == 2
        assert app.stderr_json is not None and app.stderr_json["status"] == "unsupported"
        assert "Margin" in app.stderr_json["reason"]
        assert not out_p.exists()
    return {"checked": ["board_margin", "footprint_margin"]}


@register_test(
    test_id="T2-F5-02",
    tier=2,
    feature="F5",
    min_stage="M3",
    name="Cross-net & same-net physical drill/hole clearance boundary",
)
def t2_f5_02(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    # (a) Cross-net NPTH hole clearance (0.8 mm)
    ba = BoardBuilder(seed=1502)
    ba.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    for idx, x in enumerate((3_000_000, 17_000_000), 1):
        fp = ba.add_footprint(f"J{idx}", (x, 10_000_000))
        ba.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), size_nm=(1_000_000, 1_000_000))
    mh = ba.add_footprint("MH1", (10_000_000, 10_000_000))
    ba.add_pad(
        mh,
        "1",
        "",
        (10_000_000, 10_000_000),
        attrib=pn.PAD_ATTRIB_NPTH,
        shape=pn.PAD_SHAPE_CIRCLE,
        size_nm=(2_000_000, 2_000_000),
        drill_shape=pn.PAD_DRILL_SHAPE_CIRCLE,
        drill_size_nm=(2_000_000, 2_000_000),
    )
    ba.set_project_rules(
        rules={"min_hole_clearance": 0.8},
        classes=[{"name": "Default", "priority": 0, "clearance": 0.25, "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3}],
    )
    bundle_a = ba.save(ctx.work_dir / "src_a")
    out_a = ctx.work_dir / "out_a" / "routed.kicad_pcb"
    out_a.parent.mkdir(parents=True, exist_ok=True)
    mode, res_a, bh_a = run_m3_board_or_pending(ctx, bundle_a, out_a, tag="hc_a")
    if mode == "wire":
        assert res_a.stage_failed is None
    else:
        assert res_a.returncode == 0
    assert_clean_e2e(bundle_a.pcb_path, out_a, before_hashes=bh_a, source_dir=bundle_a.directory)

    # (b) Corridor passable under 0.25mm copper clearance but blocked by 0.8mm hole_clearance
    bb = BoardBuilder(seed=1503)
    bb.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    for idx, x in enumerate((3_000_000, 17_000_000), 1):
        fp = bb.add_footprint(f"J{idx}", (x, 10_000_000))
        bb.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), size_nm=(1_000_000, 1_000_000))
    mhb = bb.add_footprint("MH1", (10_000_000, 10_000_000))
    bb.add_pad(
        mhb,
        "1",
        "",
        (10_000_000, 10_000_000),
        attrib=pn.PAD_ATTRIB_NPTH,
        shape=pn.PAD_SHAPE_CIRCLE,
        size_nm=(2_000_000, 2_000_000),
        drill_shape=pn.PAD_DRILL_SHAPE_CIRCLE,
        drill_size_nm=(2_000_000, 2_000_000),
    )
    bb.add_track("", (10_000_000, 1_000_000), (10_000_000, 7_775_000), 250_000, locked=True)
    bb.add_track("", (10_000_000, 12_225_000), (10_000_000, 19_000_000), 250_000, locked=True)
    bb.add_keepout(
        [pn.B_Cu],
        [(0, 0), (20_000_000, 0), (20_000_000, 20_000_000), (0, 20_000_000)],
        no_tracks=True,
        no_vias=True,
    )
    bb.set_project_rules(
        rules={"min_hole_clearance": 0.8},
        classes=[{"name": "Default", "priority": 0, "clearance": 0.25, "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3}],
    )
    bundle_b = bb.save(ctx.work_dir / "src_b")
    out_b = ctx.work_dir / "out_b" / "routed.kicad_pcb"
    out_b.parent.mkdir(parents=True, exist_ok=True)
    mode_b, res_b, _ = run_m3_board_or_pending(ctx, bundle_b, out_b, tag="hc_b")
    if mode_b == "wire":
        assert res_b.engine_res is not None and res_b.engine_res.proc.returncode == 3
    else:
        assert res_b.returncode == 3
    assert not out_b.exists()
    return {"checked": ["open_hole_clearance", "blocked_hole_clearance"]}


@register_test(
    test_id="T2-F5-03",
    tier=2,
    feature="F5",
    min_stage="M3",
    name="Solder-mask-web rule & exposed mask aperture clearance",
)
def t2_f5_03(ctx: DriverContext) -> dict[str, Any]:
    m3_mode = require_m3_capability(ctx)
    pn = _pcbnew()
    b = BoardBuilder(seed=1504)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    b.set_design_settings(solder_mask_min_width=200_000, solder_mask_to_copper_clearance=100_000)
    for idx, x in enumerate((3_000_000, 17_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
        b.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), size_nm=(1_000_000, 1_000_000))
    b.add_drawing(pn.F_Mask, pn.SHAPE_T_SEGMENT, (10_000_000, 8_000_000), (10_000_000, 12_000_000), width_nm=1_000_000)
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m3_board_direct(ctx, bundle, out_pcb, mode=m3_mode, tag="f5_03")
    if mode == "wire":
        if res.stage_failed is None:
            return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)
        assert res.stage_failed in ("export", "engine")
        failed_res = res.export_res if res.stage_failed == "export" else res.engine_res
        assert failed_res is not None and failed_res.proc.returncode == 2
        assert not out_pcb.exists()
        return {"refused": failed_res.stderr_json}
    if res.returncode == 0:
        return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)
    assert res.returncode == 2
    assert not out_pcb.exists()
    return {"refused_rc": res.returncode}


@register_test(
    test_id="T2-F5-04",
    tier=2,
    feature="F5",
    min_stage="M3",
    name="Internal footprint board cut (Edge.Cuts slot) intrusion guard",
)
def t2_f5_04(ctx: DriverContext) -> dict[str, Any]:
    m3_mode = require_m3_capability(ctx)
    pn = _pcbnew()
    b = BoardBuilder(seed=1505)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    for idx, x in enumerate((3_000_000, 17_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
        b.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), size_nm=(1_000_000, 1_000_000))
    slot_fp = b.add_footprint("U_SLOT", (10_000_000, 10_000_000))
    slot_pts = [(9_000_000, 6_000_000), (11_000_000, 6_000_000), (11_000_000, 14_000_000), (9_000_000, 14_000_000)]
    for s, e in zip(slot_pts, slot_pts[1:] + slot_pts[:1]):
        b.add_drawing(pn.Edge_Cuts, pn.SHAPE_T_SEGMENT, s, e, width_nm=50_000, footprint=slot_fp)
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m3_board_direct(ctx, bundle, out_pcb, mode=m3_mode, tag="f5_04")
    if mode == "wire":
        if res.stage_failed is None:
            return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)
        assert res.stage_failed in ("export", "engine")
        failed_res = res.export_res if res.stage_failed == "export" else res.engine_res
        assert failed_res is not None and failed_res.proc.returncode == 2
        assert not out_pcb.exists()
        return {"refused": failed_res.stderr_json}
    if res.returncode == 0:
        return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)
    assert res.returncode == 2
    assert not out_pcb.exists()
    return {"refused_rc": res.returncode}


@register_test(
    test_id="T2-F5-05",
    tier=2,
    feature="F5",
    min_stage="M3",
    name="Unsupported PAD_SHAPE_CUSTOM & Bezier copper artwork fail-closed",
)
def t2_f5_05(ctx: DriverContext) -> dict[str, Any]:
    m3_mode = require_m3_capability(ctx)
    pn = _pcbnew()
    # Subcase (a): non-convex custom polygon pad
    ba = BoardBuilder(seed=1506)
    ba.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    fp1 = ba.add_footprint("J1", (4_000_000, 10_000_000))
    ba.add_pad(
        fp1,
        "1",
        "SIGNAL",
        (4_000_000, 10_000_000),
        shape=pn.PAD_SHAPE_CUSTOM,
        size_nm=(1_000_000, 1_000_000),
        custom_poly_nm=[(0, 0), (2_000_000, 0), (2_000_000, 1_000_000), (1_000_000, 1_000_000), (1_000_000, 2_000_000), (0, 2_000_000)],
    )
    fp2 = ba.add_footprint("J2", (16_000_000, 10_000_000))
    ba.add_pad(fp2, "1", "SIGNAL", (16_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000))
    bundle_a = ba.save(ctx.work_dir / "src_a")
    out_a = ctx.work_dir / "out_a" / "routed.kicad_pcb"
    out_a.parent.mkdir(parents=True, exist_ok=True)
    mode_a, res_a, bh_a = run_m3_board_direct(ctx, bundle_a, out_a, mode=m3_mode, tag="custom_poly")
    if mode_a == "wire":
        if res_a.stage_failed is None:
            assert_clean_e2e(bundle_a.pcb_path, out_a, before_hashes=bh_a, source_dir=bundle_a.directory)
        else:
            assert res_a.stage_failed in ("export", "engine")
            failed_a = res_a.export_res if res_a.stage_failed == "export" else res_a.engine_res
            assert failed_a is not None and failed_a.proc.returncode == 2
            assert not out_a.exists()
    elif res_a.returncode == 0:
        assert_clean_e2e(bundle_a.pcb_path, out_a, before_hashes=bh_a, source_dir=bundle_a.directory)
    else:
        assert res_a.returncode == 2 and not out_a.exists()

    # Subcase (b): Bezier copper graphic on F.Cu
    bb = BoardBuilder(seed=1507)
    bb.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    for idx, x in enumerate((3_000_000, 17_000_000), 1):
        fp = bb.add_footprint(f"J{idx}", (x, 10_000_000))
        bb.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), size_nm=(1_000_000, 1_000_000))
    bb.add_drawing(
        pn.F_Cu,
        pn.SHAPE_T_BEZIER,
        (10_000_000, 6_000_000),
        (10_000_000, 14_000_000),
        width_nm=250_000,
        bezier_c1_nm=(12_000_000, 8_000_000),
        bezier_c2_nm=(8_000_000, 12_000_000),
    )
    bundle_b = bb.save(ctx.work_dir / "src_b")
    out_b = ctx.work_dir / "out_b" / "routed.kicad_pcb"
    out_b.parent.mkdir(parents=True, exist_ok=True)
    mode_b, res_b, bh_b = run_m3_board_direct(ctx, bundle_b, out_b, mode=m3_mode, tag="bezier")
    if mode_b == "wire":
        if res_b.stage_failed is None:
            assert_clean_e2e(bundle_b.pcb_path, out_b, before_hashes=bh_b, source_dir=bundle_b.directory)
        else:
            assert res_b.stage_failed in ("export", "engine")
            failed_b = res_b.export_res if res_b.stage_failed == "export" else res_b.engine_res
            assert failed_b is not None and failed_b.proc.returncode == 2
            assert not out_b.exists()
    elif res_b.returncode == 0:
        assert_clean_e2e(bundle_b.pcb_path, out_b, before_hashes=bh_b, source_dir=bundle_b.directory)
    else:
        assert res_b.returncode == 2 and not out_b.exists()
    return {"checked": ["PAD_SHAPE_CUSTOM", "SHAPE_T_BEZIER"]}


# ============================================================================
# Feature 6 — Rules, Multilayer, Vias & Search State (T2-F6-01 .. T2-F6-06)
# ============================================================================


@register_test(
    test_id="T2-F6-01",
    tier=2,
    feature="F6",
    min_stage="M3",
    name="Position-only dominance regression control (via-history adverse case)",
)
def t2_f6_01(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=4, seed=1601)
    b.set_rect_outline(0, 0, 24_000_000, 20_000_000)
    fp1 = b.add_footprint("J1", (4_000_000, 10_000_000))
    b.add_pad(fp1, "1", "SIGNAL", (4_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000), layers=[pn.F_Cu])
    fp2 = b.add_footprint("J2", (20_000_000, 10_000_000))
    b.add_pad(fp2, "1", "SIGNAL", (20_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000), layers=[pn.B_Cu])
    # Adverse strictly-shorter-prefix-poisoned witness:
    # F.Cu funnel (7M..9.5M, y in [8.5M..11.5M]) lets Path A reach (9.5M, 10M, In1.Cu) strictly shorter
    # via Via 1 inside x>=8.4M (<3.0mm from sole Via 2 pocket at (10.5M, 10M)), whereas clean Path B
    # drops Via 1 at x<=6.6M and detours around In1.Cu wall (7.5M, 6M..14M).
    b.add_track("", (7_000_000, 750_000), (7_000_000, 8_500_000), 500_000, layer=pn.F_Cu, locked=True)
    b.add_track("", (7_000_000, 11_500_000), (7_000_000, 19_250_000), 500_000, layer=pn.F_Cu, locked=True)
    b.add_track("", (7_000_000, 8_500_000), (9_500_000, 8_500_000), 500_000, layer=pn.F_Cu, locked=True)
    b.add_track("", (7_000_000, 11_500_000), (9_500_000, 11_500_000), 500_000, layer=pn.F_Cu, locked=True)
    b.add_track("", (9_500_000, 750_000), (9_500_000, 19_250_000), 500_000, layer=pn.F_Cu, locked=True)
    b.add_track("", (7_500_000, 6_000_000), (7_500_000, 14_000_000), 500_000, layer=pn.In1_Cu, locked=True)
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
    mode, res, bh = run_m3_board_or_pending(ctx, bundle, out_pcb)
    if mode == "wire":
        assert res.stage_failed is None, f"dominance witness failed at {res.stage_failed}"
    else:
        assert res.returncode == 0
    metrics = assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)

    # Budget-bounded dominance check: must return complete (0) or budget_exhausted (4), never unroutable (3)
    out_tight = ctx.work_dir / "out_tight" / "routed.kicad_pcb"
    out_tight.parent.mkdir(parents=True, exist_ok=True)
    mode_t, res_t, bh_t = run_m3_board_direct(
        ctx, bundle, out_tight, mode=mode, tag="tight_dom", max_expansions=15
    )
    if mode_t == "wire":
        assert res_t.engine_res is not None
        rc_t = res_t.engine_res.proc.returncode
        # exit code not pinned: tight budget may complete (0) or exhaust budget (4), never unroutable (3)
        assert rc_t in (0, 4), f"unexpected tight-budget status rc={rc_t}: {res_t.engine_res.output_json}"
        if rc_t == 0:
            assert_clean_e2e(bundle.pcb_path, out_tight, before_hashes=bh_t, source_dir=bundle.directory)
        else:
            assert not out_tight.exists()
    else:
        # exit code not pinned: tight budget may complete (0) or exhaust budget (4), never unroutable (3)
        assert res_t.returncode in (0, 4), f"unexpected tight-budget rc={res_t.returncode}: {res_t.stderr}"
        if res_t.returncode == 0:
            assert_clean_e2e(bundle.pcb_path, out_tight, before_hashes=bh_t, source_dir=bundle.directory)
        else:
            assert not out_tight.exists()
    return metrics


@register_test(
    test_id="T2-F6-02",
    tier=2,
    feature="F6",
    min_stage="M3",
    name="Board minimums override lower netclass (board_minimums floor)",
)
def t2_f6_02(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=2, seed=1602)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    b.set_design_settings(min_track_width=300_000, min_via_diameter=1_500_000)
    fp1 = b.add_footprint("J1", (3_000_000, 10_000_000))
    b.add_pad(fp1, "1", "SIGNAL", (3_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000), layers=[pn.F_Cu])
    fp2 = b.add_footprint("J2", (17_000_000, 10_000_000))
    b.add_pad(fp2, "1", "SIGNAL", (17_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000), layers=[pn.B_Cu])
    b.set_project_rules(
        rules={"min_track_width": 0.3, "min_via_diameter": 1.5},
        classes=[{"name": "Default", "priority": 0, "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3}],
    )
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m3_board_or_pending(ctx, bundle, out_pcb)
    if mode == "wire":
        assert res.stage_failed is None
    else:
        assert res.returncode == 0
    metrics = assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)
    routed = pn.LoadBoard(str(out_pcb))
    tracks = [t for t in routed.GetTracks() if t.GetClass() == "PCB_TRACK"]
    vias = [t for t in routed.GetTracks() if t.GetClass() == "PCB_VIA"]
    assert tracks and all(t.GetWidth() >= 300_000 for t in tracks)
    assert vias and all(v.GetWidth(v.TopLayer()) >= 1_500_000 for v in vias)
    return metrics


@register_test(
    test_id="T2-F6-03",
    tier=2,
    feature="F6",
    min_stage="M3",
    name="Regex netclass_patterns vs glob (^in\\d+$, .*out\\d+$)",
)
def t2_f6_03(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(seed=1603)
    b.set_rect_outline(0, 0, 24_000_000, 24_000_000)
    for name, y in (("in1", 5_000_000), ("clk_out2", 12_000_000), ("plain_in", 19_000_000)):
        for idx, x in enumerate((4_000_000, 20_000_000), 1):
            fp = b.add_footprint(f"{name}_{idx}", (x, y))
            b.add_pad(fp, "1", name, (x, y), size_nm=(1_000_000, 1_000_000))
    b.set_project_rules(
        classes=[
            {"name": "Default", "priority": 0, "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "FastIn", "priority": 1, "clearance": 0.2, "track_width": 0.35, "via_diameter": 0.6, "via_drill": 0.3},
            {"name": "OutBus", "priority": 2, "clearance": 0.2, "track_width": 0.6, "via_diameter": 0.8, "via_drill": 0.4},
        ],
        netclass_patterns=[
            {"netclass": "FastIn", "pattern": "^in\\d+$"},
            {"netclass": "OutBus", "pattern": ".*out\\d+$"},
        ],
    )
    bundle = b.save(ctx.work_dir / "src")
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m3_board_or_pending(ctx, bundle, out_pcb)
    if mode == "wire":
        assert res.stage_failed is None
    else:
        assert res.returncode == 0
    metrics = assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)
    routed = pn.LoadBoard(str(out_pcb))
    assert all(t.GetWidth() >= 350_000 for t in routed.GetTracks() if t.GetNetname() == "in1")
    assert all(t.GetWidth() >= 600_000 for t in routed.GetTracks() if t.GetNetname() == "clk_out2")
    assert all(200_000 <= t.GetWidth() < 350_000 for t in routed.GetTracks() if t.GetNetname() == "plain_in")
    return metrics


@register_test(
    test_id="T2-F6-04",
    tier=2,
    feature="F6",
    min_stage="M3",
    name="Malformed/mismatched .kicad_pro or unparseable .kicad_dru refused",
)
def t2_f6_04(ctx: DriverContext) -> dict[str, Any]:
    m3_mode = require_m3_capability(ctx)
    cases = [
        ("bad_pro_json", '{"meta": {"version": 3', None),
        ("bad_pro_ver", '{"meta": {"version": 999}}', None),
        ("bad_dru_sexpr", None, '(version 1)\n(rule "broken" (constraint clearance'),
    ]
    for label, pro_txt, dru_txt in cases:
        sub = ctx.work_dir / label
        bundle = build_std_2pad_board(sub / "src", name="main", seed=1604)
        if pro_txt is not None:
            bundle.pcb_path.with_suffix(".kicad_pro").write_text(pro_txt, encoding="utf-8")
        if dru_txt is not None:
            bundle.pcb_path.with_suffix(".kicad_dru").write_text(dru_txt, encoding="utf-8")
        before_hashes = snapshot_source_dir(bundle.directory)
        out_pcb = sub / "out" / "routed.kicad_pcb"
        out_pcb.parent.mkdir(parents=True, exist_ok=True)
        if m3_mode == "native":
            rules_p = write_json_file(sub / "rules.json", STD_RULES)
            proc = ctx.native_driver.run_native(bundle.pcb_path, out_pcb, rules_path=rules_p)
            assert_no_crash(proc, label)
            assert proc.returncode == 2
            sj = parse_last_json_line_safe(proc.stderr)
            assert sj is not None and sj.get("status") in ("invalid", "unsupported")
        else:
            rules_p = write_json_file(sub / "rules.json", STD_RULES)
            res = ctx.json_driver.route_board_e2e(
                bundle.pcb_path, out_pcb, rules_p, work_dir=sub / "wire"
            )
            assert_no_crash(res.export_res.proc, label)
            assert res.stage_failed == "export" and res.export_res.proc.returncode == 2
            assert res.export_res.stderr_json is not None
            assert res.export_res.stderr_json.get("status") in ("invalid", "unsupported")
        assert_source_dir_untouched(bundle.directory, before_hashes)
        assert not out_pcb.exists()
    return {"checked": [c[0] for c in cases]}


@register_test(
    test_id="T2-F6-05",
    tier=2,
    feature="F6",
    min_stage="M3",
    name="Local pad/footprint clearance override (2.0 mm) enforced or refused",
)
def t2_f6_05(ctx: DriverContext) -> dict[str, Any]:
    m3_mode = require_m3_capability(ctx)
    for scope in ("pad", "footprint"):
        sub = ctx.work_dir / f"override_{scope}"
        b = BoardBuilder(seed=1605)
        b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
        for idx, x in enumerate((3_000_000, 17_000_000), 1):
            fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
            b.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), size_nm=(1_000_000, 1_000_000))
        ob_fp = b.add_footprint(
            "J3",
            (10_000_000, 10_000_000),
            local_clearance_nm=(2_000_000 if scope == "footprint" else None),
        )
        b.add_pad(
            ob_fp,
            "1",
            "",
            (10_000_000, 10_000_000),
            size_nm=(1_000_000, 1_000_000),
            local_clearance_nm=(2_000_000 if scope == "pad" else None),
        )
        b.set_project_rules(
            classes=[{"name": "Default", "priority": 0, "clearance": 0.25, "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3}]
        )
        bundle = b.save(sub / "src")
        out_pcb = sub / "out" / "routed.kicad_pcb"
        out_pcb.parent.mkdir(parents=True, exist_ok=True)
        mode, res, bh = run_m3_board_direct(ctx, bundle, out_pcb, mode=m3_mode, tag=f"ovr_{scope}")
        if mode == "wire":
            if res.stage_failed is None:
                assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)
            else:
                assert res.stage_failed in ("export", "engine")
                failed_res = res.export_res if res.stage_failed == "export" else res.engine_res
                assert failed_res is not None and failed_res.proc.returncode == 2
                assert not out_pcb.exists()
        elif res.returncode == 0:
            assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)
        else:
            assert res.returncode == 2 and not out_pcb.exists()
    return {"checked": ["pad_override", "footprint_override"]}


@register_test(
    test_id="T2-F6-06",
    tier=2,
    feature="F6",
    min_stage="M3",
    name="Unreachable layer or disabled via type (microvia/blind disallowed)",
)
def t2_f6_06(ctx: DriverContext) -> dict[str, Any]:
    m3_mode = require_m3_capability(ctx)
    pn = _pcbnew()
    # (a) Blind/microvias disabled while F.Cu and B.Cu have no_vias keepouts
    ba = BoardBuilder(copper_layers=4, seed=1606)
    ba.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    fp1 = ba.add_footprint("J1", (4_000_000, 10_000_000))
    ba.add_pad(fp1, "1", "SIGNAL", (4_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000), layers=[pn.In1_Cu])
    fp2 = ba.add_footprint("J2", (16_000_000, 10_000_000))
    ba.add_pad(fp2, "1", "SIGNAL", (16_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000), layers=[pn.In2_Cu])
    ba.add_keepout([pn.F_Cu, pn.B_Cu], [(0, 0), (20_000_000, 0), (20_000_000, 20_000_000), (0, 20_000_000)], no_tracks=True, no_vias=True)
    ba.set_project_rules(rules={"allow_blind_buried_vias": False, "allow_microvias": False})
    bundle_a = ba.save(ctx.work_dir / "src_a")
    out_a = ctx.work_dir / "out_a" / "routed.kicad_pcb"
    out_a.parent.mkdir(parents=True, exist_ok=True)
    mode_a, res_a, _ = run_m3_board_direct(ctx, bundle_a, out_a, mode=m3_mode, tag="disallowed_via")
    if mode_a == "wire":
        assert res_a.stage_failed is not None
    else:
        assert res_a.returncode in (2, 3)
    assert not out_a.exists()

    # (b) Pad on disabled inner layer In1_Cu in 2-layer board
    bb = BoardBuilder(copper_layers=2, seed=1607)
    bb.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    fp_b1 = bb.add_footprint("J1", (4_000_000, 10_000_000))
    bb.add_pad(fp_b1, "1", "SIGNAL", (4_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000), layers=[pn.F_Cu])
    fp_b2 = bb.add_footprint("J2", (16_000_000, 10_000_000))
    bb.add_pad(fp_b2, "1", "SIGNAL", (16_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000), layers=[pn.In1_Cu])
    bundle_b = bb.save(ctx.work_dir / "src_b")
    out_b = ctx.work_dir / "out_b" / "routed.kicad_pcb"
    out_b.parent.mkdir(parents=True, exist_ok=True)
    mode_b, res_b, _ = run_m3_board_direct(ctx, bundle_b, out_b, mode=m3_mode, tag="unreachable_layer")
    if mode_b == "wire":
        assert res_b.stage_failed is not None
    else:
        assert res_b.returncode in (2, 3)
    assert not out_b.exists()
    return {"checked": ["disabled_blind_microvia", "unreachable_layer"]}
