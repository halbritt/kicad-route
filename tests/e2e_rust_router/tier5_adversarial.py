"""Tier 5 adversarial coverage hardening E2E tests (8 tests)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from tests.e2e_rust_router.core_helpers import (
    STD_RULES,
    assert_clean_e2e,
    assert_no_crash,
    snapshot_source_dir,
    write_json_file,
)
from tests.e2e_rust_router.drivers import parse_last_json_line
from tests.e2e_rust_router.kicad_verify import (
    BoardBuilder,
    _pcbnew,
    _track_snapshot,
    verify_routed_board,
)
from tests.e2e_rust_router.runner import DriverContext, register_test


@register_test(
    test_id="T5-01",
    tier=5,
    feature="F7",
    min_stage="M5",
    name="Real route-native --pour-area-loss rejects zone-splitting copper route",
)
def t5_01(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=2, seed=5001)
    b.set_rect_outline(0, 0, 30_000_000, 20_000_000)
    # Force SIG onto B.Cu across the middle of a GND pour, splitting off >25% pour area
    b.add_keepout(
        [pn.F_Cu],
        [(10_000_000, 1_000_000), (20_000_000, 1_000_000), (20_000_000, 19_000_000), (10_000_000, 19_000_000)],
        no_tracks=True,
        no_vias=True,
        no_zones=False,
    )
    for idx, x in enumerate((4_000_000, 26_000_000), 1):
        fp = b.add_footprint(f"JS{idx}", (x, 10_000_000))
        b.add_pad(
            fp,
            "1",
            "SIG",
            (x, 10_000_000),
            size_nm=(800_000, 800_000),
            layers=[pn.B_Cu],
        )
    for idx, x in enumerate((4_000_000, 26_000_000), 1):
        fp = b.add_footprint(f"JG{idx}", (x, 4_000_000))
        b.add_pad(
            fp,
            "1",
            "GND",
            (x, 4_000_000),
            size_nm=(800_000, 800_000),
            layers=[pn.B_Cu],
        )
    b.add_zone(
        "GND",
        [pn.B_Cu],
        [(2_000_000, 2_000_000), (28_000_000, 2_000_000), (28_000_000, 18_000_000), (2_000_000, 18_000_000)],
        clearance_nm=300_000,
    )
    b.set_design_settings(min_track_width=250_000, min_clearance=200_000, min_copper_edge_clearance=500_000)
    b.set_project_rules({"min_resolved_spokes": 1})
    bundle = b.save(ctx.work_dir / "src")
    bh = snapshot_source_dir(bundle.directory)
    rules_path = write_json_file(
        ctx.work_dir / "rules.json",
        {**STD_RULES, "width_nm": 500_000, "clearance_nm": 250_000},
    )

    rejected_pcb = ctx.work_dir / "out" / "rejected.kicad_pcb"
    rejected_pcb.parent.mkdir(parents=True, exist_ok=True)
    p_fail = ctx.native_driver.run_native(
        bundle.pcb_path,
        rejected_pcb,
        rules_path=rules_path,
        extra_args=["--pour-area-loss", "1.0"],
    )
    assert p_fail.returncode != 0, f"expected --pour-area-loss 1.0 failure, got {p_fail.returncode}"
    assert not rejected_pcb.exists(), "rejected output must not be published"

    ok_pcb = ctx.work_dir / "out" / "ok.kicad_pcb"
    p_ok = ctx.native_driver.run_native(
        bundle.pcb_path,
        ok_pcb,
        rules_path=rules_path,
        extra_args=["--pour-area-loss", "50.0"],
    )
    assert p_ok.returncode == 0, f"expected --pour-area-loss 50.0 pass, got {p_ok.returncode}: {p_ok.stderr}"
    metrics = assert_clean_e2e(
        bundle.pcb_path,
        ok_pcb,
        before_hashes=bh,
        source_dir=bundle.directory,
        refill_zones=True,
    )
    return {**metrics, "rejected_rc": p_fail.returncode}


@register_test(
    test_id="T5-02",
    tier=5,
    feature="F10",
    min_stage="M5",
    name="--protect human extends locked and unlocked stubs and refuses a blocked board",
)
def t5_02(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()

    def _build_partial_board(name: str, *, locked_stub: bool, b_keepout: bool = False) -> Path:
        b = BoardBuilder(copper_layers=2, seed=5002)
        b.set_rect_outline(0, 0, 30_000_000, 20_000_000)
        if b_keepout:
            b.add_keepout(
                [pn.B_Cu],
                [(500_000, 500_000), (29_500_000, 500_000), (29_500_000, 19_500_000), (500_000, 19_500_000)],
                no_tracks=True,
                no_vias=True,
                no_zones=True,
            )
        for idx, x in enumerate((4_000_000, 26_000_000), 1):
            fp = b.add_footprint(f"JA{idx}", (x, 8_000_000))
            b.add_pad(fp, "1", "/SIG_A", (x, 8_000_000), size_nm=(1_000_000, 1_000_000))
        for idx, x in enumerate((4_000_000, 26_000_000), 1):
            fp = b.add_footprint(f"JB{idx}", (x, 14_000_000))
            b.add_pad(fp, "1", "/SIG_B", (x, 14_000_000), size_nm=(1_000_000, 1_000_000))
        b.set_project_rules()
        bundle = b.save(ctx.work_dir / name)
        bd = pn.LoadBoard(str(bundle.pcb_path))
        tr = pn.PCB_TRACK(bd)
        tr.SetStart(pn.VECTOR2I(4_000_000, 14_000_000))
        tr.SetEnd(pn.VECTOR2I(8_000_000, 14_000_000))
        tr.SetWidth(250_000)
        tr.SetLayer(pn.F_Cu)
        tr.SetNetCode(bd.FindNet("/SIG_B").GetNetCode())
        tr.SetLocked(locked_stub)
        bd.Add(tr)
        pn.SaveBoard(str(bundle.pcb_path), bd, True)
        return bundle.pcb_path

    # A locked stub stays byte-for-byte identifiable when its open net is completed.
    locked_pcb = _build_partial_board("locked_partial", locked_stub=True)
    locked_out = ctx.work_dir / "out" / "locked_out.kicad_pcb"
    locked_out.parent.mkdir(parents=True, exist_ok=True)
    before_locked = snapshot_source_dir(locked_pcb.parent)
    p_locked = ctx.native_driver.run_native(
        locked_pcb, locked_out, extra_args=["--protect", "human"]
    )
    assert p_locked.returncode == 0, p_locked.stderr
    locked_metrics = assert_clean_e2e(locked_pcb, locked_out, before_hashes=before_locked, source_dir=locked_pcb.parent)

    def locked_tracks(path):
        board = pn.LoadBoard(str(path))
        return {_track_snapshot(t) for t in board.GetTracks() if t.IsLocked()}

    original_locked = locked_tracks(locked_pcb)
    assert len(original_locked) == 1 and locked_tracks(locked_out) == original_locked

    # 2. Unlocked partial stub under --protect human completes open portion while preserving input stub
    unlocked_pcb = _build_partial_board("unlocked_partial", locked_stub=False)
    bh_unlocked = snapshot_source_dir(unlocked_pcb.parent)
    unlocked_out = ctx.work_dir / "out" / "unlocked_out.kicad_pcb"
    p_unlocked = ctx.native_driver.run_native(
        unlocked_pcb, unlocked_out, extra_args=["--protect", "human"]
    )
    assert p_unlocked.returncode == 0, f"expected exit 0, got {p_unlocked.returncode}: {p_unlocked.stderr}"
    metrics = assert_clean_e2e(
        unlocked_pcb, unlocked_out, before_hashes=bh_unlocked, source_dir=unlocked_pcb.parent
    )

    # 3. Designer edits Step-1 copper to add a locked /SIG_A wall blocking F.Cu before Step 2 (--protect human --nets !/SIG_A) -> fails closed
    wall_src_pcb = _build_partial_board("wall_src", locked_stub=False, b_keepout=True)
    step1_pcb = ctx.work_dir / "step1" / "routed.kicad_pcb"
    step1_pcb.parent.mkdir(parents=True, exist_ok=True)
    p_s1 = ctx.native_driver.run_native(wall_src_pcb, step1_pcb, extra_args=["--nets", "/SIG_A"])
    assert p_s1.returncode == 0, p_s1.stderr
    broken_pcb = ctx.work_dir / "broken" / "routed.kicad_pcb"
    broken_pcb.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(step1_pcb, broken_pcb)
    shutil.copy2(wall_src_pcb.with_suffix(".kicad_pro"), broken_pcb.with_suffix(".kicad_pro"))
    bd = pn.LoadBoard(str(broken_pcb))
    wall = pn.PCB_TRACK(bd)
    wall.SetStart(pn.VECTOR2I(15_000_000, 650_000))
    wall.SetEnd(pn.VECTOR2I(15_000_000, 19_350_000))
    wall.SetWidth(250_000)
    wall.SetLayer(pn.F_Cu)
    wall.SetNetCode(bd.FindNet("/SIG_A").GetNetCode())
    wall.SetLocked(True)
    bd.Add(wall)
    pn.SaveBoard(str(broken_pcb), bd, True)
    broken_out = ctx.work_dir / "out" / "broken_out.kicad_pcb"
    p_broken = ctx.native_driver.run_native(
        broken_pcb, broken_out, extra_args=["--protect", "human", "--nets", "!/SIG_A"]
    )
    assert p_broken.returncode == 3, f"expected exit 3 unroutable, got {p_broken.returncode}: {p_broken.stderr}"
    assert not broken_out.exists()
    return {**metrics, "locked_rc": p_locked.returncode, "locked": locked_metrics, "broken_rc": p_broken.returncode}


@register_test(
    test_id="T5-03",
    tier=5,
    feature="F6",
    min_stage="M5",
    name="route-native and verify_routed_board refuse ignored physical DRC checks and rule_compile_error",
)
def t5_03(ctx: DriverContext) -> dict[str, Any]:
    b = BoardBuilder(copper_layers=2, seed=5003)
    b.set_rect_outline(0, 0, 25_000_000, 15_000_000)
    for idx, x in enumerate((4_000_000, 21_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 7_500_000))
        b.add_pad(fp, "1", "SIG", (x, 7_500_000), size_nm=(800_000, 800_000))
    b.set_project_rules(rule_severities={"clearance": "ignore"})
    bundle_ign = b.save(ctx.work_dir / "ign_src")
    out_ign = ctx.work_dir / "out" / "ign.kicad_pcb"
    out_ign.parent.mkdir(parents=True, exist_ok=True)
    p_ign = ctx.native_driver.run_native(bundle_ign.pcb_path, out_ign)
    assert p_ign.returncode == 2, f"expected exit 2 on ignored clearance, got {p_ign.returncode}: {p_ign.stderr}"
    assert not out_ign.exists()

    # verify_routed_board oracle also refuses ignored physical DRC checks
    b_clean = BoardBuilder(copper_layers=2, seed=5003)
    b_clean.set_rect_outline(0, 0, 25_000_000, 15_000_000)
    for idx, x in enumerate((4_000_000, 21_000_000), 1):
        fp = b_clean.add_footprint(f"J{idx}", (x, 7_500_000))
        b_clean.add_pad(fp, "1", "SIG", (x, 7_500_000), size_nm=(800_000, 800_000))
    b_clean.set_project_rules()
    bundle_clean = b_clean.save(ctx.work_dir / "clean_src")
    out_clean = ctx.work_dir / "out" / "clean.kicad_pcb"
    p_clean = ctx.native_driver.run_native(bundle_clean.pcb_path, out_clean)
    assert p_clean.returncode == 0, p_clean.stderr
    try:
        verify_routed_board(bundle_ign.pcb_path, out_clean)
        raise AssertionError("verify_routed_board must reject clearance=ignore")
    except AssertionError as exc:
        assert "clearance" in str(exc), str(exc)

    # Broken .kicad_dru emitting rule_compile_error fails closed
    b_dru = BoardBuilder(copper_layers=2, seed=5003)
    b_dru.set_rect_outline(0, 0, 25_000_000, 15_000_000)
    for idx, x in enumerate((4_000_000, 21_000_000), 1):
        fp = b_dru.add_footprint(f"J{idx}", (x, 7_500_000))
        b_dru.add_pad(fp, "1", "SIG", (x, 7_500_000), size_nm=(800_000, 800_000))
    b_dru.set_project_rules()
    b_dru.set_dru_text("(version 1)\n(rule broken (constraint bogus_unknown_constraint (min 0.5mm)))\n")
    bundle_dru = b_dru.save(ctx.work_dir / "dru_src")
    out_dru = ctx.work_dir / "out" / "dru.kicad_pcb"
    p_dru = ctx.native_driver.run_native(bundle_dru.pcb_path, out_dru)
    assert p_dru.returncode == 2, f"expected exit 2 on rule_compile_error, got {p_dru.returncode}: {p_dru.stderr}"
    assert not out_dru.exists()

    # R2-GAP-04: ignored track_dangling / via_dangling fail closed in both route-native and verify_routed_board
    for chk in ("track_dangling", "via_dangling"):
        b_dang = BoardBuilder(copper_layers=2, seed=5003)
        b_dang.set_rect_outline(0, 0, 25_000_000, 15_000_000)
        for idx, x in enumerate((4_000_000, 21_000_000), 1):
            fp = b_dang.add_footprint(f"J{idx}", (x, 7_500_000))
            b_dang.add_pad(fp, "1", "SIG", (x, 7_500_000), size_nm=(800_000, 800_000))
        b_dang.set_project_rules(rule_severities={chk: "ignore"})
        bundle_dang = b_dang.save(ctx.work_dir / f"{chk}_src")
        out_dang = ctx.work_dir / "out" / f"{chk}.kicad_pcb"
        p_dang = ctx.native_driver.run_native(bundle_dang.pcb_path, out_dang)
        assert p_dang.returncode == 2, f"expected exit 2 on ignored {chk}, got {p_dang.returncode}: {p_dang.stderr}"
        assert not out_dang.exists()
        try:
            verify_routed_board(bundle_dang.pcb_path, out_clean)
            raise AssertionError(f"verify_routed_board must reject {chk}=ignore")
        except AssertionError as exc:
            assert chk in str(exc), str(exc)

    return {"ign_rc": p_ign.returncode, "dru_rc": p_dru.returncode}


@register_test(
    test_id="T5-04",
    tier=5,
    feature="F4",
    min_stage="M5",
    name="Symlinked .kicad_pcb resolves target sidecars and FIFO sidecar fails fast",
)
def t5_04(ctx: DriverContext) -> dict[str, Any]:
    b = BoardBuilder(copper_layers=2, seed=5004)
    b.set_rect_outline(0, 0, 25_000_000, 15_000_000)
    for idx, x in enumerate((4_000_000, 21_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 7_500_000))
        b.add_pad(fp, "1", "SIG", (x, 7_500_000), size_nm=(800_000, 800_000))
    b.set_project_rules({"min_resolved_spokes": 4})
    b.set_dru_text("(version 1)\n(rule sym_clr (constraint clearance (min 0.35mm)))\n")
    bundle = b.save(ctx.work_dir / "real")

    link_dir = ctx.work_dir / "link"
    link_dir.mkdir(parents=True, exist_ok=True)
    link_pcb = link_dir / bundle.pcb_path.name
    link_pcb.symlink_to(bundle.pcb_path)
    out_sym = ctx.work_dir / "out" / "sym.kicad_pcb"
    out_sym.parent.mkdir(parents=True, exist_ok=True)
    save_req = ctx.work_dir / "out" / "sym_req.json"
    p_sym = ctx.native_driver.run_native(
        link_pcb, out_sym, extra_args=["--save-request", str(save_req)]
    )
    assert p_sym.returncode == 0, f"symlinked route-native failed: {p_sym.stderr}"
    req_doc = json.loads(save_req.read_text(encoding="utf-8"))
    assert req_doc["board_design_settings"]["min_resolved_spokes"] == 4
    assert "custom_rules" in req_doc

    fifo_dir = ctx.work_dir / "fifo"
    fifo_dir.mkdir(parents=True, exist_ok=True)
    fifo_pcb = fifo_dir / "fifo_board.kicad_pcb"
    fifo_pcb.write_bytes(bundle.pcb_path.read_bytes())
    os.mkfifo(fifo_pcb.with_suffix(".kicad_pro"))
    out_fifo = ctx.work_dir / "out" / "fifo.kicad_pcb"
    p_fifo = ctx.native_driver.run_native(fifo_pcb, out_fifo, timeout_s=10.0)
    assert p_fifo.returncode != 0, "FIFO sidecar must fail immediately"
    assert not out_fifo.exists()
    return {"symlink_ok": True, "fifo_rc": p_fifo.returncode}


@register_test(
    test_id="T5-05",
    tier=5,
    feature="F8",
    min_stage="M5",
    name="Indirect MaskAperture sharing triggers rip-up and reroute E2E",
)
def t5_05(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=2, seed=5005)
    b.set_rect_outline(0, 0, 30_000_000, 20_000_000)
    b.set_design_settings(
        min_track_width=200_000,
        min_clearance=200_000,
        min_copper_edge_clearance=200_000,
        solder_mask_to_copper_clearance=0,
    )
    # N1 shorter (HPWL 6mm), N2 longer (HPWL 14mm) trapped on F.Cu through shared F.Mask aperture
    for idx, x in enumerate((12_000_000, 18_000_000), 1):
        fp = b.add_footprint(f"J1_{idx}", (x, 10_000_000))
        b.add_pad(fp, "1", "N1", (x, 10_000_000), size_nm=(500_000, 500_000), layers=[pn.F_Cu])
    for idx, x in enumerate((8_000_000, 22_000_000), 1):
        fp = b.add_footprint(f"J2_{idx}", (x, 14_000_000))
        b.add_pad(fp, "1", "N2", (x, 14_000_000), size_nm=(500_000, 500_000), layers=[pn.F_Cu])
    # Unowned F.Mask aperture spanning y=8..15.5mm across x=14..16mm
    mask_poly = pn.PCB_SHAPE(b.board)
    mask_poly.SetShape(pn.SHAPE_T_POLY)
    mask_poly.SetLayer(pn.F_Mask)
    mask_poly.SetFilled(True)
    mask_poly.SetWidth(0)
    poly_chain = mask_poly.GetPolyShape()
    poly_chain.NewOutline()
    for x, y in ((14_000_000, 8_000_000), (16_000_000, 8_000_000), (16_000_000, 15_500_000), (14_000_000, 15_500_000)):
        poly_chain.Append(x, y)
    b.board.Add(mask_poly)
    b.add_keepout([pn.B_Cu], [(500_000, 500_000), (29_500_000, 500_000), (29_500_000, 19_500_000), (500_000, 19_500_000)], no_tracks=True, no_vias=True)
    for rect in (
        [(6_500_000, 11_500_000), (23_500_000, 11_500_000), (23_500_000, 13_000_000), (6_500_000, 13_000_000)],
        [(6_500_000, 15_000_000), (23_500_000, 15_000_000), (23_500_000, 18_500_000), (6_500_000, 18_500_000)],
        [(6_500_000, 11_500_000), (7_500_000, 11_500_000), (7_500_000, 18_000_000), (6_500_000, 18_000_000)],
        [(22_500_000, 11_500_000), (23_500_000, 11_500_000), (23_500_000, 18_000_000), (22_500_000, 18_000_000)],
    ):
        b.add_keepout([pn.F_Cu], rect, no_tracks=True)
    b.set_project_rules({"min_copper_edge_clearance": 0.2})
    bundle = b.save(ctx.work_dir / "src")
    bh = snapshot_source_dir(bundle.directory)
    rules_path = write_json_file(
        ctx.work_dir / "rules.json",
        {**STD_RULES, "width_nm": 200_000, "clearance_nm": 200_000, "grid_nm": 500_000, "edge_clearance_nm": 200_000},
    )
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    p = ctx.native_driver.run_native(bundle.pcb_path, out_pcb, rules_path=rules_path)
    assert p.returncode == 0, f"T5-05 failed: {p.stderr}"
    return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)


@register_test(
    test_id="T5-06",
    tier=5,
    feature="F9",
    min_stage="M5",
    name="Bounded meander amplitude E2E respects small --meander-amplitude ceiling",
)
def t5_06(ctx: DriverContext) -> dict[str, Any]:
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=2, seed=5006)
    b.set_rect_outline(0, 0, 45_000_000, 25_000_000)
    b.set_design_settings(min_track_width=150_000, min_clearance=150_000)
    for idx, x in enumerate((10_000_000, 30_000_000), 1):
        fp = b.add_footprint(f"JN1_{idx}", (x, 10_000_000))
        b.add_pad(fp, "1", "N1", (x, 10_000_000), size_nm=(400_000, 400_000), layers=[pn.F_Cu])
    for idx, x in enumerate((10_000_000, 30_200_000), 1):
        fp = b.add_footprint(f"JN2_{idx}", (x, 15_000_000))
        b.add_pad(fp, "1", "N2", (x, 15_000_000), size_nm=(400_000, 400_000), layers=[pn.F_Cu])
    b.set_project_rules(
        {"min_copper_edge_clearance": 0.2, "min_track_width": 0.15, "min_clearance": 0.15},
        classes=[{
            "name": "Default",
            "priority": 0,
            "clearance": 0.15,
            "track_width": 0.15,
            "via_diameter": 0.6,
            "via_drill": 0.3,
            "microvia_diameter": 0.3,
            "microvia_drill": 0.15,
        }],
    )
    bundle = b.save(ctx.work_dir / "src")
    bh = snapshot_source_dir(bundle.directory)
    rules_path = write_json_file(
        ctx.work_dir / "rules.json",
        {**STD_RULES, "width_nm": 150_000, "clearance_nm": 150_000, "grid_nm": 100_000},
    )
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    p = ctx.native_driver.run_native(
        bundle.pcb_path,
        out_pcb,
        rules_path=rules_path,
        extra_args=[
            "--length-match-group",
            "N1",
            "N2",
            "--group-match-tolerance",
            "0.05",
            "--group-meander-amplitude",
            "0.11",
        ],
    )
    assert p.returncode == 0, f"T5-06 failed: {p.stderr}"
    metrics = assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)
    bd = pn.LoadBoard(str(out_pcb))
    n1_tracks = [tr for tr in bd.GetTracks() if tr.GetNetname() == "N1"]
    assert len(n1_tracks) > 1, f"expected real meander segments on N1, got {len(n1_tracks)}"
    max_exc = 0
    for tr in n1_tracks:
        exc = max(abs(int(tr.GetStart().y) - 10_000_000), abs(int(tr.GetEnd().y) - 10_000_000))
        assert exc <= 110_000, f"meander excursion {exc} nm exceeded 110_000 nm ceiling"
        max_exc = max(max_exc, exc)
    assert max_exc > 0, "expected non-zero meander excursion on N1"

    out_fail_pcb = ctx.work_dir / "out" / "routed_fail.kicad_pcb"
    p_fail = ctx.native_driver.run_native(
        bundle.pcb_path,
        out_fail_pcb,
        rules_path=rules_path,
        extra_args=[
            "--length-match-group",
            "N1",
            "N2",
            "--group-match-tolerance",
            "0.05",
            "--group-meander-amplitude",
            "0.05",
        ],
    )
    assert p_fail.returncode == 3, f"expected fail-closed exit 3 for 0.05mm amplitude, got {p_fail.returncode}: {p_fail.stderr}"
    assert not out_fail_pcb.exists()
    return {**metrics, "n1_segments": len(n1_tracks), "max_excursion_nm": max_exc, "small_amp_rc": p_fail.returncode}


@register_test(
    test_id="T5-07",
    tier=5,
    feature="F10",
    min_stage="M5",
    name="agent-ee re01_enforced allows live route-native --protect and --nets and parse_last_json_line is strict",
)
def t5_07(ctx: DriverContext) -> dict[str, Any]:
    assert parse_last_json_line('{"a": 1} [1, 2]') is None
    assert parse_last_json_line('{"a": 1}\n[1, 2]') is None
    assert parse_last_json_line('{"ok": 1}\n{"ok": 2') is None
    assert parse_last_json_line('{"a": NaN}') is None
    assert parse_last_json_line('{"a": 1} {"b": 2}') == {"b": 2}

    ctx.agent_ee_driver.probe_rust_integration(ctx.native_driver)
    b = BoardBuilder(copper_layers=2, seed=5007)
    b.set_rect_outline(0, 0, 25_000_000, 15_000_000)
    for idx, x in enumerate((4_000_000, 21_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 7_500_000))
        b.add_pad(fp, "1", "/SIG_A", (x, 7_500_000), size_nm=(800_000, 800_000))
    b.set_project_rules({"min_copper_edge_clearance": 0.2})
    bundle = b.save(ctx.work_dir / "src")
    bh = snapshot_source_dir(bundle.directory)
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    agent_src = ctx.agent_ee_driver.agent_ee_root / "src"
    demo = ctx.repo_root / "fixtures" / "route_demo" / "route_demo.kicad_pcb"
    scoped_out = out_pcb.with_name("selected.kicad_pcb")
    script = f"""
import json, os, subprocess, sys
sys.path.insert(0, {str(agent_src)!r})
from agent_ee import routing_adapter
child_env = {{k: v for k, v in os.environ.items() if k != "PYTHONPATH"}}

def route(source, destination, *patterns):
    with routing_adapter.re01_enforced():
        done = subprocess.run(
            [{str(ctx.provenance.binary_path)!r}, "route-native", source, destination,
             "--protect", "existing", "--nets", *patterns],
            env=child_env, capture_output=True, text=True, timeout=60,
        )
    assert done.returncode == 0, done.stderr
    return done.returncode

route({str(bundle.pcb_path)!r}, {str(out_pcb)!r}, "/SIG_A")
rc = route({str(demo)!r}, {str(scoped_out)!r}, "!SIG_[AB]")
print(json.dumps({{"rc": rc}}))
"""
    proc = ctx.agent_ee_driver.run_python(script, str(ctx.work_dir / "t5_07"))
    assert_no_crash(proc, "T5-07")
    assert proc.returncode == 0, f"T5-07 failed:\n{proc.stderr}\n{proc.stdout}"
    before = _pcbnew().LoadBoard(str(demo))
    after = _pcbnew().LoadBoard(str(scoped_out))
    excluded = {"/SIG_A", "/SIG_B"}

    def excluded_copper(board):
        return {_track_snapshot(t) for t in board.GetTracks() if t.GetNetname() in excluded}

    original, saved = excluded_copper(before), excluded_copper(after)
    assert original == saved, f"Excluded copper changed: removed={original - saved}, added={saved - original}"
    assert len(list(after.GetTracks())) > len(list(before.GetTracks()))
    return assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)


@register_test(
    test_id="T5-08",
    tier=5,
    feature="F4",
    min_stage="M5",
    name="route-native refuses sub-nm AC flags / empty chain with exit 8 and preserves UTF-8 stackup",
)
def t5_08(ctx: DriverContext) -> dict[str, Any]:
    b = BoardBuilder(copper_layers=2, seed=5008)
    b.set_rect_outline(0, 0, 20_000_000, 12_000_000)
    for idx, x in enumerate((4_000_000, 16_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 6_000_000))
        b.add_pad(fp, "1", "SIG", (x, 6_000_000), size_nm=(800_000, 800_000))
    b.set_project_rules({"min_copper_edge_clearance": 0.2})
    bundle = b.save(ctx.work_dir / "src")
    for idx, extra in enumerate((
        ["--ac-match-tolerance", "1e-7"],
        ["--ac-meander-amplitude", "1e-7"],
        ["--ac-couple-chain-json", "[]"],
    )):
        out_p = ctx.work_dir / "out" / f"bad_{idx}.kicad_pcb"
        out_p.parent.mkdir(parents=True, exist_ok=True)
        res = ctx.native_driver.run_native(bundle.pcb_path, out_p, extra_args=extra)
        assert res.returncode == 8, f"expected exit 8 for {extra}, got {res.returncode}: {res.stderr}"
        assert not out_p.exists()

    # Check tools.rust_engine_kicad._parse_pcb_stackup preserves UTF-8 layer names
    bridge_check = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            (
                "import sys; sys.path.insert(0, 'tools'); "
                "import rust_engine_kicad as b; "
                "res = b._parse_pcb_stackup(b'(kicad_pcb (setup (stackup (layer \"\\xc2\\xb5Cu\" (type \"copper\") (thickness 0.035)))))'); "
                "assert res == [{'name': 'µCu', 'kind': 'copper', 'thickness_nm': 35000}], res"
            ),
        ],
        cwd=str(ctx.repo_root),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert bridge_check.returncode == 0, bridge_check.stderr
    return {"cli_exit8": 3, "utf8_stackup": True}
