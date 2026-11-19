"""Tier 4 real-world KiCad E2E scenarios (T4-01 .. T4-07, 7 tests)."""

from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
from typing import Any

from tests.e2e_rust_router.core_helpers import (
    assert_clean_e2e,
    assert_no_crash,
    assert_source_tree_untouched,
    parse_last_json_line_safe,
    require_m4_capability,
    run_m3_board_or_pending,
    run_m4_board_or_pending,
    snapshot_source_dir,
    snapshot_source_tree,
    stage_fixture_copy,
)
from tests.e2e_rust_router.kicad_verify import (
    BoardBuilder,
    FixtureBundle,
    _pcbnew,
    copper_signature,
    extract_pour_facts,
    read_native_connectivity,
    run_native_drc,
    run_native_drc_refilled,
    verify_diff_pair_native,
    verify_fromto_length_groups,
    verify_partition_ratchet,
    verify_pour_integrity,
)
from tests.e2e_rust_router.runner import DriverContext, PendingCapability, register_test


def _hide_ref(fp: Any) -> None:
    fp.Reference().SetVisible(False)


def _baseline_drc_clean(
    pcb_path: Path,
    *,
    refill_zones: bool = False,
    check_parity: bool = False,
) -> tuple[int, int]:
    conn = read_native_connectivity(pcb_path)
    assert conn.unconnected_count > 0, f"expected unrouted baseline opens on {pcb_path}"
    drc = (
        run_native_drc_refilled(pcb_path, check_parity=check_parity)
        if refill_zones
        else run_native_drc(pcb_path, check_parity=check_parity)
    )
    assert not drc.violations, f"baseline DRC not clean on {pcb_path}: {drc.violations}"
    assert not drc.schematic_parity, f"baseline schematic parity errors on {pcb_path}: {drc.schematic_parity}"
    assert len(drc.unconnected_items) > 0, f"expected baseline DRC unconnected_items on {pcb_path}"
    return conn.unconnected_count, len(drc.unconnected_items)


# ============================================================================
# T4-01: MCU + USB + Power 4-Layer Mixed-Rule Board
# ============================================================================


def _build_t4_01_board(directory: Path) -> FixtureBundle:
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=4, seed=4001)
    b.set_rect_outline(0, 0, 70_000_000, 50_000_000)
    b.set_design_settings(
        min_track_width=150_000,
        min_clearance=150_000,
        min_copper_edge_clearance=300_000,
        min_via_diameter=500_000,
        min_via_annular_width=100_000,
        min_hole_clearance=250_000,
    )

    j1 = b.add_footprint("J1", (10_000_000, 25_000_000))
    _hide_ref(j1)
    for idx, pos in enumerate(
        (
            (6_000_000, 20_500_000),
            (6_000_000, 29_500_000),
            (13_000_000, 20_500_000),
            (13_000_000, 29_500_000),
        ),
        1,
    ):
        b.add_pad(
            j1,
            f"S{idx}",
            "GND",
            pos,
            attrib=pn.PAD_ATTRIB_PTH,
            shape=pn.PAD_SHAPE_OVAL,
            size_nm=(2_400_000, 1_400_000),
            drill_shape=pn.PAD_DRILL_SHAPE_OBLONG,
            drill_size_nm=(1_400_000, 700_000),
        )
    for num, net, y_nm, h_nm in (
        ("A1", "GND", 22_500_000, 500_000),
        ("A4", "VBUS", 23_500_000, 500_000),
        ("A6", "USB_D_P", 24_700_000, 350_000),
        ("A7", "USB_D_N", 25_300_000, 350_000),
        ("B12", "GND", 26_500_000, 500_000),
    ):
        b.add_pad(
            j1,
            num,
            net,
            (13_500_000, y_nm),
            shape=pn.PAD_SHAPE_ROUNDRECT,
            size_nm=(1_200_000, h_nm),
            roundrect_ratio=0.25,
        )

    u1 = b.add_footprint("U1", (42_000_000, 25_000_000))
    _hide_ref(u1)
    qfp_nets = {
        1: "3V3",
        2: "GND",
        5: "USB_D_P",
        6: "USB_D_N",
        25: "GPIO0",
        26: "GPIO1",
        30: "GPIO2",
    }
    for i in range(12):
        b.add_pad(
            u1,
            str(1 + i),
            qfp_nets.get(1 + i, ""),
            (34_000_000, 19_500_000 + i * 1_000_000),
            shape=pn.PAD_SHAPE_ROUNDRECT,
            size_nm=(1_400_000, 450_000),
            angle_deg=0.0,
            roundrect_ratio=0.25,
        )
        b.add_pad(
            u1,
            str(13 + i),
            qfp_nets.get(13 + i, ""),
            (36_500_000 + i * 1_000_000, 33_000_000),
            shape=pn.PAD_SHAPE_ROUNDRECT,
            size_nm=(1_400_000, 450_000),
            angle_deg=90.0,
            roundrect_ratio=0.25,
        )
        b.add_pad(
            u1,
            str(25 + i),
            qfp_nets.get(25 + i, ""),
            (50_000_000, 30_500_000 - i * 1_000_000),
            shape=pn.PAD_SHAPE_ROUNDRECT,
            size_nm=(1_400_000, 450_000),
            angle_deg=0.0,
            roundrect_ratio=0.25,
        )
        b.add_pad(
            u1,
            str(37 + i),
            qfp_nets.get(37 + i, ""),
            (47_500_000 - i * 1_000_000, 17_000_000),
            shape=pn.PAD_SHAPE_ROUNDRECT,
            size_nm=(1_400_000, 450_000),
            angle_deg=90.0,
            roundrect_ratio=0.25,
        )

    u2 = b.add_footprint("U2", (22_000_000, 11_000_000))
    _hide_ref(u2)
    for num, net, x_nm in (("1", "VBUS", 20_000_000), ("2", "GND", 22_000_000), ("3", "3V3", 24_000_000)):
        b.add_pad(
            u2,
            num,
            net,
            (x_nm, 12_500_000),
            shape=pn.PAD_SHAPE_ROUNDRECT,
            size_nm=(1_000_000, 1_400_000),
            roundrect_ratio=0.2,
        )

    for ref, center, p1, p2 in (
        ("C1", (28_000_000, 11_000_000), (28_000_000, 10_000_000), (28_000_000, 12_000_000)),
        ("C2", (37_000_000, 13_000_000), (36_000_000, 13_000_000), (38_000_000, 13_000_000)),
        ("C3", (54_000_000, 31_000_000), (54_000_000, 30_000_000), (54_000_000, 32_000_000)),
    ):
        cap = b.add_footprint(ref, center)
        _hide_ref(cap)
        b.add_pad(cap, "1", "3V3", p1, shape=pn.PAD_SHAPE_ROUNDRECT, size_nm=(900_000, 900_000), roundrect_ratio=0.25)
        b.add_pad(cap, "2", "GND", p2, shape=pn.PAD_SHAPE_ROUNDRECT, size_nm=(900_000, 900_000), roundrect_ratio=0.25)

    j2 = b.add_footprint("J2", (60_000_000, 25_000_000), side="B")
    _hide_ref(j2)
    for idx, net in enumerate(("GPIO0", "GPIO1", "GPIO2", "GND")):
        b.add_pad(
            j2,
            str(idx + 1),
            net,
            (60_000_000, 22_000_000 + idx * 2_000_000),
            size_nm=(1_200_000, 1_200_000),
            layers=[pn.B_Cu],
        )

    b.set_project_rules(
        rules={
            "min_track_width": 0.15,
            "min_clearance": 0.15,
            "min_copper_edge_clearance": 0.3,
            "min_via_diameter": 0.5,
            "min_via_annular_width": 0.1,
            "min_hole_clearance": 0.25,
        },
        classes=[
            {
                "name": "Default",
                "priority": 0,
                "clearance": 0.2,
                "track_width": 0.2,
                "via_diameter": 0.6,
                "via_drill": 0.3,
            },
            {
                "name": "Power",
                "priority": 1,
                "clearance": 0.3,
                "track_width": 0.5,
                "via_diameter": 0.8,
                "via_drill": 0.4,
            },
            {
                "name": "USB_DP",
                "priority": 2,
                "clearance": 0.2,
                "track_width": 0.25,
                "via_diameter": 0.6,
                "via_drill": 0.3,
                "diff_pair_width": 0.25,
                "diff_pair_gap": 0.35,
                "diff_pair_via_gap": 0.35,
            },
        ],
        netclass_patterns=[
            {"netclass": "Power", "pattern": "^(3V3|VBUS|GND)$"},
            {"netclass": "USB_DP", "pattern": "^USB_D_[PN]$"},
        ],
    )
    b.set_dru_text(
        "(version 1)\n"
        '(rule "power_to_other_clearance"\n'
        "  (condition \"A.NetClass == 'Power' && B.NetClass != 'Power'\")\n"
        "  (constraint clearance (min 0.4mm))\n"
        ")\n"
        '(rule "usb_pair_gap"\n'
        "  (condition \"A.NetClass == 'USB_DP'\")\n"
        "  (constraint diff_pair_gap (min 0.34mm) (max 0.36mm))\n"
        ")\n"
    )
    return b.save(directory)


@register_test(
    test_id="T4-01",
    tier=4,
    feature="F6",
    min_stage="M4",
    name="MCU + USB + Power 4-layer mixed-rule board",
    description="Exercises: F1, F2, F5, F6, F9",
)
def t4_01(ctx: DriverContext) -> dict[str, Any]:
    bundle = _build_t4_01_board(ctx.work_dir / "src")
    _baseline_drc_clean(bundle.pcb_path)
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m4_board_or_pending(
        ctx,
        bundle,
        out_pcb,
        extra_args=["--diff-pairs", "auto"],
        require_help_flags=("--diff-pairs",),
        tag="t4_01",
        timeout_s=180.0,
    )
    assert mode == "native" and res.returncode == 0, f"T4-01 route failed: {res.stderr}"
    metrics = assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)
    dp = verify_diff_pair_native(bundle.pcb_path, out_pcb, [("USB_D_P", "USB_D_N")])

    pn = _pcbnew()
    board = pn.LoadBoard(str(out_pcb))
    power_nets = {"3V3", "VBUS", "GND"}
    pwr_tracks = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK" and t.GetNetname() in power_nets]
    assert pwr_tracks, "expected routed Power tracks"
    for tr in pwr_tracks:
        assert int(tr.GetWidth()) >= 500_000, f"Power track below 0.5mm: {tr.GetNetname()} {tr.GetWidth()}"
    pwr_vias = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetNetname() in power_nets]
    for via in pwr_vias:
        assert int(via.GetWidth(via.TopLayer())) >= 800_000 and int(via.GetDrill()) >= 400_000
    usb_tracks = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK" and t.GetNetname() in ("USB_D_P", "USB_D_N")]
    assert usb_tracks, "expected routed USB_D_P/USB_D_N tracks"
    for tr in usb_tracks:
        assert int(tr.GetWidth()) >= 250_000, f"USB_DP track below 0.25mm: {tr.GetNetname()} {tr.GetWidth()}"

    conn = read_native_connectivity(out_pcb)
    v33_parts = conn.pad_partitions_by_net.get("3V3", [])
    assert len(v33_parts) == 1 and len(v33_parts[0]) == 5, f"expected 5-terminal 3V3 component, got {v33_parts}"
    fps = {fp.GetReference(): fp for fp in board.GetFootprints()}
    u1_rr = [p for p in fps["U1"].Pads() if p.GetShape() == pn.PAD_SHAPE_ROUNDRECT]
    j1_slotted = [p for p in fps["J1"].Pads() if p.GetDrillShape() == pn.PAD_DRILL_SHAPE_OBLONG]
    assert len(u1_rr) == 48 and len(j1_slotted) == 4
    return {**metrics, "diff_pair": dp, "power_tracks": len(pwr_tracks), "power_vias": len(pwr_vias)}


# ============================================================================
# T4-02: Mixed SMD/THT + Rotated/Oval/RoundRect + NPTH Board
# ============================================================================


def _build_t4_02_board(directory: Path) -> FixtureBundle:
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=2, seed=4002)
    b.set_polygon_outline(
        [
            (4_000_000, 0),
            (56_000_000, 0),
            (60_000_000, 4_000_000),
            (60_000_000, 46_000_000),
            (56_000_000, 50_000_000),
            (4_000_000, 50_000_000),
            (0, 46_000_000),
            (0, 4_000_000),
        ]
    )
    b.set_design_settings(
        min_track_width=200_000,
        min_clearance=200_000,
        min_copper_edge_clearance=500_000,
        min_hole_clearance=300_000,
    )

    for idx, pos in enumerate(
        ((6_000_000, 6_000_000), (54_000_000, 6_000_000), (54_000_000, 44_000_000), (6_000_000, 44_000_000)),
        1,
    ):
        mh = b.add_footprint(f"MH{idx}", pos)
        _hide_ref(mh)
        b.add_pad(
            mh,
            "1",
            "",
            pos,
            attrib=pn.PAD_ATTRIB_NPTH,
            shape=pn.PAD_SHAPE_CIRCLE,
            size_nm=(3_000_000, 3_000_000),
            drill_size_nm=(3_000_000, 3_000_000),
        )

    cx, cy = 30_000_000, 25_000_000
    u1 = b.add_footprint("U1", (cx, cy), angle_deg=45.0)
    _hide_ref(u1)
    c45 = math.cos(math.radians(45.0))
    s45 = math.sin(math.radians(45.0))

    def rot_pt(lx_nm: int, ly_nm: int) -> tuple[int, int]:
        return (
            round(cx + lx_nm * c45 + ly_nm * s45),
            round(cy - lx_nm * s45 + ly_nm * c45),
        )

    qfn_nets = {
        1: "IO_VCC",
        2: "IO_SIG0",
        3: "IO_SIG1",
        4: "GND",
        5: "BUS_CLK",
        6: "BUS_DAT",
        9: "IO_VCC",
        13: "IO_SIG2",
    }
    qfn_specs: list[tuple[int, int, int, float]] = []
    for i in range(4):
        qfn_specs.append((1 + i, -4_000_000, -1_800_000 + i * 1_200_000, 45.0))
    for i in range(4):
        qfn_specs.append((5 + i, -1_800_000 + i * 1_200_000, 4_000_000, 135.0))
    for i in range(4):
        qfn_specs.append((9 + i, 4_000_000, 1_800_000 - i * 1_200_000, 225.0))
    for i in range(4):
        qfn_specs.append((13 + i, 1_800_000 - i * 1_200_000, -4_000_000, 315.0))

    pad_pos: dict[int, tuple[int, int]] = {}
    for pnum, lx, ly, ang in qfn_specs:
        pos = rot_pt(lx, ly)
        pad_pos[pnum] = pos
        b.add_pad(
            u1,
            str(pnum),
            qfn_nets.get(pnum, ""),
            pos,
            shape=pn.PAD_SHAPE_ROUNDRECT,
            size_nm=(1_400_000, 500_000),
            angle_deg=ang,
            roundrect_ratio=0.25,
        )

    b.add_drawing(pn.Dwgs_User, pn.SHAPE_T_SEGMENT, (26_000_000, 21_000_000), (34_000_000, 21_000_000), footprint=u1)
    b.add_drawing(pn.Dwgs_User, pn.SHAPE_T_SEGMENT, (10_000_000, 6_000_000), (50_000_000, 6_000_000))
    b.add_drawing(pn.F_SilkS, pn.SHAPE_T_SEGMENT, (14_000_000, 8_000_000), (46_000_000, 8_000_000))

    passives = [
        ("R1", (18_000_000, 15_000_000), (16_500_000, 15_000_000), (19_500_000, 15_000_000), "IO_SIG0", "IO_OUT0", pn.PAD_SHAPE_OVAL, (0.0, 180.0), 0.0),
        ("R2", (18_000_000, 35_000_000), (18_000_000, 33_500_000), (18_000_000, 36_500_000), "IO_SIG1", "IO_OUT1", pn.PAD_SHAPE_ROUNDRECT, (90.0, 270.0), 0.25),
        ("C1", (42_000_000, 13_000_000), (42_000_000, 11_500_000), (42_000_000, 14_500_000), "IO_VCC", "GND", pn.PAD_SHAPE_OVAL, (90.0, 270.0), 0.0),
        ("R3", (42_000_000, 21_000_000), (40_500_000, 21_000_000), (43_500_000, 21_000_000), "IO_SIG2", "IO_OUT2", pn.PAD_SHAPE_ROUNDRECT, (0.0, 180.0), 0.30),
    ]
    for ref, center, p1, p2, n1, n2, shp, (a1, a2), ratio in passives:
        fp = b.add_footprint(ref, center)
        _hide_ref(fp)
        b.add_pad(fp, "1", n1, p1, shape=shp, size_nm=(1_600_000, 900_000), angle_deg=a1, roundrect_ratio=ratio)
        b.add_pad(fp, "2", n2, p2, shape=shp, size_nm=(1_600_000, 900_000), angle_deg=a2, roundrect_ratio=ratio)

    j1 = b.add_footprint("J1", (8_000_000, 25_000_000))
    _hide_ref(j1)
    for idx, net in enumerate(("IO_VCC", "IO_OUT0", "IO_OUT1", "IO_OUT2", "GND")):
        b.add_pad(
            j1,
            str(idx + 1),
            net,
            (8_000_000, 17_000_000 + idx * 4_000_000),
            attrib=pn.PAD_ATTRIB_PTH,
            shape=pn.PAD_SHAPE_OVAL,
            size_nm=(2_400_000, 1_400_000),
            drill_shape=pn.PAD_DRILL_SHAPE_OBLONG,
            drill_size_nm=(1_400_000, 700_000),
        )

    j2 = b.add_footprint("J2", (50_000_000, 39_500_000))
    _hide_ref(j2)
    b.add_pad(j2, "1", "BUS_CLK", (48_000_000, 41_000_000), size_nm=(1_000_000, 1_000_000))
    b.add_pad(j2, "2", "BUS_DAT", (52_000_000, 38_000_000), size_nm=(1_000_000, 1_000_000))

    p5, p6 = pad_pos[5], pad_pos[6]
    for a, c in ((p5, (34_000_000, 34_000_000)), ((34_000_000, 34_000_000), (42_000_000, 41_000_000)), ((42_000_000, 41_000_000), (48_000_000, 41_000_000))):
        b.add_track("BUS_CLK", a, c, 250_001, locked=True)
    for a, c in ((p6, (36_000_000, 32_000_000)), ((36_000_000, 32_000_000), (46_000_000, 38_000_000)), ((46_000_000, 38_000_000), (52_000_000, 38_000_000))):
        b.add_track("BUS_DAT", a, c, 250_001, locked=True)

    b.set_project_rules(
        rules={
            "min_clearance": 0.2,
            "min_track_width": 0.2,
            "min_copper_edge_clearance": 0.5,
            "min_hole_clearance": 0.3,
        },
        classes=[
            {
                "name": "Default",
                "priority": 0,
                "clearance": 0.2,
                "track_width": 0.25,
                "via_diameter": 0.6,
                "via_drill": 0.3,
            }
        ],
    )
    return b.save(directory)


@register_test(
    test_id="T4-02",
    tier=4,
    feature="F5",
    min_stage="M3",
    name="Mixed SMD/THT + rotated/oval/roundrect + NPTH board",
    description="Exercises: F1, F2, F5, F6",
)
def t4_02(ctx: DriverContext) -> dict[str, Any]:
    bundle = _build_t4_02_board(ctx.work_dir / "src")
    _baseline_drc_clean(bundle.pcb_path)
    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m3_board_or_pending(ctx, bundle, out_pcb, tag="t4_02", timeout_s=120.0)
    assert (res.returncode == 0 if mode == "native" else res.stage_failed is None), "T4-02 failed"
    metrics = assert_clean_e2e(bundle.pcb_path, out_pcb, before_hashes=bh, source_dir=bundle.directory)
    drc = run_native_drc(out_pcb, sidecar_source=bundle.pcb_path)
    assert not drc.violations, f"expected 0 DRC violations on T4-02: {drc.violations}"

    pn = _pcbnew()
    board = pn.LoadBoard(str(out_pcb))
    bus_tracks = [
        t for t in board.GetTracks()
        if t.GetClass() == "PCB_TRACK" and t.GetNetname() in ("BUS_CLK", "BUS_DAT")
    ]
    assert len(bus_tracks) == 6
    assert all(int(t.GetWidth()) == 250_001 and t.IsLocked() for t in bus_tracks)
    pads = list(board.GetPads())
    assert sum(1 for p in pads if p.GetAttribute() == pn.PAD_ATTRIB_NPTH) == 4
    assert sum(1 for p in pads if p.GetDrillShape() == pn.PAD_DRILL_SHAPE_OBLONG) == 5
    return {**metrics, "locked_bus_tracks": len(bus_tracks)}


# ============================================================================
# T4-03: 4-Layer GND/PWR Pour + Thermals + Keepouts + Via Spans
# ============================================================================


def _build_t4_03_board(directory: Path) -> FixtureBundle:
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=4, seed=4003)
    b.set_rect_outline(0, 0, 50_000_000, 40_000_000)
    b.set_design_settings(min_resolved_spokes=2)

    for ref, x_nm, nets in (
        ("J1", 8_000_000, ("GND", "3V3", "SIG_F", "SIG_B")),
        ("J2", 42_000_000, ("GND", "5V", "SIG_F", "SIG_B")),
    ):
        fp = b.add_footprint(ref, (x_nm, 19_000_000))
        _hide_ref(fp)
        for idx, net in enumerate(nets):
            b.add_pad(
                fp,
                str(idx + 1),
                net,
                (x_nm, 10_000_000 + idx * 6_000_000),
                attrib=pn.PAD_ATTRIB_PTH,
                size_nm=(1_700_000, 1_700_000),
                drill_size_nm=(800_000, 800_000),
            )

    for ref, x_nm, pwr in (("U_L", 16_000_000, "3V3"), ("U_R", 34_000_000, "5V")):
        fp = b.add_footprint(ref, (x_nm, 16_000_000))
        _hide_ref(fp)
        b.add_pad(fp, "1", pwr, (x_nm, 14_000_000), size_nm=(1_000_000, 1_000_000))
        b.add_pad(fp, "2", "GND", (x_nm, 18_000_000), size_nm=(1_000_000, 1_000_000))

    js1 = b.add_footprint("JS1", (14_000_000, 32_000_000))
    _hide_ref(js1)
    b.add_pad(js1, "1", "SIG_SPAN", (14_000_000, 32_000_000), size_nm=(1_000_000, 1_000_000))
    js2 = b.add_footprint("JS2", (36_000_000, 32_000_000), side="B")
    _hide_ref(js2)
    b.add_pad(js2, "1", "SIG_SPAN", (36_000_000, 32_000_000), size_nm=(1_000_000, 1_000_000), layers=[pn.B_Cu])

    jb1 = b.add_footprint("JB1", (16_000_000, 6_000_000))
    _hide_ref(jb1)
    b.add_pad(jb1, "1", "SIG_BLIND", (16_000_000, 6_000_000), size_nm=(1_000_000, 1_000_000))
    jb2 = b.add_footprint("JB2", (34_000_000, 6_000_000))
    _hide_ref(jb2)
    b.add_pad(jb2, "1", "SIG_BLIND", (34_000_000, 6_000_000), size_nm=(1_000_000, 1_000_000))

    # F.Cu closed keepout ring enclosing JB1 (16, 6) with interior island [14.5..17.5] x [4.5..7.5]
    # + In2.Cu/B.Cu barrier across [13..37] x [3..9] forcing an (F.Cu, In1.Cu) blind via inside the island
    for ring_poly in (
        [(13_500_000, 3_500_000), (18_500_000, 3_500_000), (18_500_000, 4_500_000), (13_500_000, 4_500_000)],
        [(13_500_000, 7_500_000), (18_500_000, 7_500_000), (18_500_000, 8_500_000), (13_500_000, 8_500_000)],
        [(13_500_000, 3_500_000), (14_500_000, 3_500_000), (14_500_000, 8_500_000), (13_500_000, 8_500_000)],
        [(17_500_000, 3_500_000), (18_500_000, 3_500_000), (18_500_000, 8_500_000), (17_500_000, 8_500_000)],
    ):
        b.add_keepout(
            [pn.F_Cu],
            ring_poly,
            no_tracks=True,
            no_vias=True,
            no_zones=False,
        )
    b.add_keepout(
        [pn.In2_Cu, pn.B_Cu],
        [(13_000_000, 3_000_000), (37_000_000, 3_000_000), (37_000_000, 9_000_000), (13_000_000, 9_000_000)],
        no_tracks=True,
        no_vias=True,
        no_zones=False,
    )
    # Antenna keepout on all 4 copper layers
    b.add_keepout(
        [pn.F_Cu, pn.In1_Cu, pn.In2_Cu, pn.B_Cu],
        [(21_000_000, 20_000_000), (29_000_000, 20_000_000), (29_000_000, 26_000_000), (21_000_000, 26_000_000)],
        no_tracks=True,
        no_vias=True,
        no_zones=True,
    )
    # Mechanical no_vias keepout
    b.add_keepout(
        [pn.F_Cu, pn.B_Cu],
        [(20_000_000, 29_000_000), (30_000_000, 29_000_000), (30_000_000, 36_000_000), (20_000_000, 36_000_000)],
        no_tracks=False,
        no_vias=True,
        no_zones=False,
    )

    b.add_zone(
        "GND",
        [pn.In1_Cu],
        [(1_000_000, 1_000_000), (49_000_000, 1_000_000), (49_000_000, 39_000_000), (1_000_000, 39_000_000)],
        pad_connection=pn.ZONE_CONNECTION_FULL,
        zone_name="GND_IN1",
    )
    b.add_zone(
        "GND",
        [pn.B_Cu],
        [(1_000_000, 1_000_000), (49_000_000, 1_000_000), (49_000_000, 39_000_000), (1_000_000, 39_000_000)],
        pad_connection=pn.ZONE_CONNECTION_THERMAL,
        zone_name="GND_B",
    )
    b.add_zone(
        "3V3",
        [pn.In2_Cu],
        [(1_000_000, 1_000_000), (24_000_000, 1_000_000), (24_000_000, 39_000_000), (1_000_000, 39_000_000)],
        pad_connection=pn.ZONE_CONNECTION_FULL,
        zone_name="PWR_3V3",
    )
    b.add_zone(
        "5V",
        [pn.In2_Cu],
        [(26_000_000, 1_000_000), (49_000_000, 1_000_000), (49_000_000, 39_000_000), (26_000_000, 39_000_000)],
        pad_connection=pn.ZONE_CONNECTION_FULL,
        zone_name="PWR_5V",
    )
    b.set_project_rules(rules={"allow_blind_buried_vias": True})
    return b.save(directory)


@register_test(
    test_id="T4-03",
    tier=4,
    feature="F7",
    min_stage="M3",
    name="4-layer GND/PWR pour + thermals + keepouts + via spans",
    description="Exercises: F1, F5, F6, F7",
)
def t4_03(ctx: DriverContext) -> dict[str, Any]:
    bundle = _build_t4_03_board(ctx.work_dir / "src")
    _baseline_drc_clean(bundle.pcb_path, refill_zones=True)
    assert verify_pour_integrity(bundle.pcb_path, bundle.pcb_path).passed

    out_pcb = ctx.work_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, bh = run_m3_board_or_pending(ctx, bundle, out_pcb, tag="t4_03", timeout_s=120.0)
    assert (res.returncode == 0 if mode == "native" else res.stage_failed is None), "T4-03 failed"
    metrics = assert_clean_e2e(
        bundle.pcb_path,
        out_pcb,
        before_hashes=bh,
        source_dir=bundle.directory,
        refill_zones=True,
        check_pour_integrity=True,
    )
    refilled = ctx.work_dir / "refilled.kicad_pcb"
    drc = run_native_drc_refilled(out_pcb, refilled_out=refilled, sidecar_source=bundle.pcb_path)
    assert not drc.violations and not drc.unconnected_items
    facts = extract_pour_facts(refilled)
    for lk in ("GND@In1.Cu", "GND@B.Cu", "3V3@In2.Cu", "5V@In2.Cu"):
        assert facts["layers"][lk]["islands_off_main"] == 0, f"isolated island on {lk}: {facts['layers'][lk]}"
    for pk in ("J1.1@B.Cu", "J2.1@B.Cu"):
        assert facts["pads"][pk]["spokes"] >= 2, f"low thermal spokes on {pk}: {facts['pads'][pk]}"

    pn = _pcbnew()
    board = pn.LoadBoard(str(out_pcb))
    vias = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]
    has_through = any(v.GetViaType() == pn.VIATYPE_THROUGH and (v.TopLayer(), v.BottomLayer()) == (pn.F_Cu, pn.B_Cu) for v in vias)
    has_blind = any(v.GetViaType() == pn.VIATYPE_BLIND and (v.TopLayer(), v.BottomLayer()) == (pn.F_Cu, pn.In1_Cu) for v in vias)
    assert has_through and has_blind, f"expected both through and blind vias, got {[(v.GetViaType(), v.TopLayer(), v.BottomLayer()) for v in vias]}"
    for v in vias:
        vx, vy = int(v.GetPosition().x), int(v.GetPosition().y)
        assert not (20_000_000 <= vx <= 30_000_000 and 29_000_000 <= vy <= 36_000_000), f"via inside mechanical no_vias keepout: {(vx, vy)}"
    return {**metrics, "vias": len(vias)}


# ============================================================================
# T4-04: High-Density Congested Multi-Net + Rip-Up/Reroute + Spatial Scaling
# ============================================================================


def _build_t4_04_board(directory: Path) -> FixtureBundle:
    b = BoardBuilder(copper_layers=4, seed=4004)
    b.set_rect_outline(0, 0, 90_000_000, 70_000_000)
    b.set_design_settings(
        min_track_width=200_000,
        min_clearance=200_000,
        min_copper_edge_clearance=500_000,
        min_via_diameter=500_000,
        min_via_annular_width=100_000,
    )

    for i in range(24):
        net = f"SIG_{i:02d}"
        y_left = 10_000_000 + i * 2_200_000
        y_mid = 10_000_000 + ((i * 7 + 5) % 24) * 2_200_000
        y_right = 10_000_000 + (23 - i) * 2_200_000
        for side_tag, pos in (("L", (6_000_000, y_left)), ("M", (45_000_000, y_mid)), ("R", (84_000_000, y_right))):
            fp = b.add_footprint(f"J_{i:02d}_{side_tag}", pos)
            _hide_ref(fp)
            b.add_pad(fp, "1", net, pos, size_nm=(700_000, 700_000))

    obs_fp = b.add_footprint("OBS", (45_000_000, 35_000_000))
    _hide_ref(obs_fp)
    x_cols = [28_000_000 + c * 1_100_000 for c in range(14)] + [
        47_700_000 + c * 1_100_000 for c in range(14)
    ]
    obs_idx = 0
    for x_nm in x_cols:
        for r in range(24):
            obs_idx += 1
            y_nm = 10_000_000 + r * 2_200_000
            b.add_pad(obs_fp, f"O{obs_idx}", "", (x_nm, y_nm), size_nm=(350_000, 350_000))

    b.set_project_rules(
        rules={
            "min_track_width": 0.2,
            "min_clearance": 0.2,
            "min_copper_edge_clearance": 0.5,
            "min_via_diameter": 0.5,
            "min_via_annular_width": 0.1,
        },
        classes=[
            {
                "name": "Default",
                "priority": 0,
                "clearance": 0.2,
                "track_width": 0.2,
                "via_diameter": 0.5,
                "via_drill": 0.3,
            }
        ],
    )
    return b.save(directory)


@register_test(
    test_id="T4-04",
    tier=4,
    feature="F8",
    min_stage="M4",
    name="High-density congested multi-net + rip-up/reroute + spatial scaling",
    description="Exercises: F1, F3, F6, F8, F11",
)
def t4_04(ctx: DriverContext) -> dict[str, Any]:
    bundle = _build_t4_04_board(ctx.work_dir / "src")
    _baseline_drc_clean(bundle.pcb_path)
    require_m4_capability(ctx)

    out_a = ctx.work_dir / "out_a" / "routed.kicad_pcb"
    out_b = ctx.work_dir / "out_b" / "routed.kicad_pcb"
    out_a.parent.mkdir(parents=True, exist_ok=True)
    out_b.parent.mkdir(parents=True, exist_ok=True)
    limits = {"max_work": 50_000_000}

    mode_a, res_a, bh_a = run_m4_board_or_pending(
        ctx, bundle, out_a, tag="t4_04_a", max_expansions=1_000_000, limits=limits, timeout_s=180.0
    )
    mode_b, res_b, bh_b = run_m4_board_or_pending(
        ctx, bundle, out_b, tag="t4_04_b", max_expansions=1_000_000, limits=limits, timeout_s=180.0
    )
    assert mode_a == "native" and mode_b == "native"
    assert res_a.returncode == 0, f"T4-04 run A failed: {res_a.stderr}"
    assert res_b.returncode == 0, f"T4-04 run B failed: {res_b.stderr}"

    metrics_a = assert_clean_e2e(bundle.pcb_path, out_a, before_hashes=bh_a, source_dir=bundle.directory)
    assert_clean_e2e(bundle.pcb_path, out_b, before_hashes=bh_b, source_dir=bundle.directory)
    sig_a = copper_signature(out_a)
    sig_b = copper_signature(out_b)
    assert sig_a == sig_b, f"non-deterministic copper signature on T4-04: {sig_a} != {sig_b}"

    assert res_a.metrics is not None and res_a.metrics.wall_s < 120.0
    assert res_a.metrics.cpu_s >= 0.0 and res_a.metrics.max_rss_kib > 0
    sum_a = parse_last_json_line_safe(res_a.stdout) or parse_last_json_line_safe(res_a.stderr) or {}
    sum_b = parse_last_json_line_safe(res_b.stdout) or parse_last_json_line_safe(res_b.stderr) or {}
    if "work_used" in sum_a and "work_used" in sum_b:
        assert sum_a["work_used"] == sum_b["work_used"] <= limits["max_work"]
    if "expanded_states" in sum_a and "expanded_states" in sum_b:
        assert sum_a["expanded_states"] == sum_b["expanded_states"]

    return {
        **metrics_a,
        "copper_signature": sig_a,
        "wall_s": res_a.metrics.wall_s,
        "cpu_s": res_a.metrics.cpu_s,
        "max_rss_kib": res_a.metrics.max_rss_kib,
        **({"work_used": sum_a["work_used"]} if "work_used" in sum_a else {}),
        **({"expanded_states": sum_a["expanded_states"]} if "expanded_states" in sum_a else {}),
    }


# ============================================================================
# T4-05: High-Speed 4-Layer Differential Pairs + Length/Delay/Skew Matching
# ============================================================================


def _build_t4_05_board(directory: Path) -> FixtureBundle:
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=4, seed=4005)
    b.set_rect_outline(10_000_000, 10_000_000, 110_000_000, 90_000_000)
    b.set_design_settings(min_resolved_spokes=2)

    for ref_base, x0, x1, y_nm, p_net, n_net in (
        ("JA", 20_000_000, 70_000_000, 24_000_000, "A_P", "A_N"),
        ("JB", 20_000_000, 80_000_000, 42_000_000, "B_P", "B_N"),
    ):
        for tag, x_nm in (("1", x0), ("2", x1)):
            fp = b.add_footprint(f"{ref_base}{tag}", (x_nm, y_nm))
            _hide_ref(fp)
            b.add_pad(fp, "1", p_net, (x_nm, y_nm), size_nm=(400_000, 400_000))
            b.add_pad(fp, "2", n_net, (x_nm, y_nm + 600_000), size_nm=(400_000, 400_000))

    b.add_keepout(
        [pn.F_Cu, pn.B_Cu],
        [(46_000_000, 9_000_000), (50_000_000, 9_000_000), (50_000_000, 91_000_000), (46_000_000, 91_000_000)],
        no_tracks=True,
        no_vias=True,
        no_zones=True,
    )

    for idx, (y_nm, span_mm) in enumerate(((56_000_000, 44), (62_000_000, 46), (68_000_000, 48), (74_000_000, 50))):
        net = f"DQ{idx}"
        fp1 = b.add_footprint(f"JD{idx}A", (20_000_000, y_nm))
        _hide_ref(fp1)
        b.add_pad(fp1, "1", net, (20_000_000, y_nm), size_nm=(600_000, 600_000))
        x_end = 20_000_000 + span_mm * 1_000_000
        if idx == 2:
            fp2 = b.add_footprint(f"JD{idx}B", (x_end, y_nm), side="B")
            _hide_ref(fp2)
            b.add_pad(fp2, "1", net, (x_end, y_nm), size_nm=(600_000, 600_000), layers=[pn.B_Cu])
        else:
            fp2 = b.add_footprint(f"JD{idx}B", (x_end, y_nm))
            _hide_ref(fp2)
            b.add_pad(fp2, "1", net, (x_end, y_nm), size_nm=(600_000, 600_000))

    b.add_zone(
        "GND",
        [pn.In1_Cu],
        [(12_000_000, 12_000_000), (108_000_000, 12_000_000), (108_000_000, 88_000_000), (12_000_000, 88_000_000)],
        pad_connection=pn.ZONE_CONNECTION_THERMAL,
        zone_name="GND_IN1",
    )
    for ref, x_nm in (("JG1", 15_000_000), ("JG2", 105_000_000)):
        fp = b.add_footprint(ref, (x_nm, 84_000_000))
        _hide_ref(fp)
        b.add_pad(
            fp,
            "1",
            "GND",
            (x_nm, 84_000_000),
            attrib=pn.PAD_ATTRIB_PTH,
            size_nm=(1_700_000, 1_700_000),
            drill_size_nm=(800_000, 800_000),
        )

    layers = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    rows = []
    for i, layer in enumerate(layers):
        rows.append(f'(layer "{layer}" (type "copper") (thickness 0.035))')
        if i < 3:
            rows.append(
                f'(layer "dielectric {i + 1}" (type "core") (thickness 0.2) '
                '(material "FR4 qualification assumption") (epsilon_r 4) (loss_tangent 0.02))'
            )
    b.set_stackup_sexpr("(stackup\n" + "\n".join(rows) + "\n)")

    outer_er = (4 + 1) / 2 + (4 - 1) / 2 / math.sqrt(1 + 12 / (0.25 / 0.2))
    outer = math.sqrt(outer_er) / 0.299792458
    inner = math.sqrt(4) / 0.299792458
    entries = [
        {
            "signal_layer": "F.Cu",
            "top_reference_layer": "UNDEFINED",
            "bottom_reference_layer": "In1.Cu",
            "width": 250000,
            "diff_pair_gap": 0,
            "delay": round(outer * 1e6),
        },
        {
            "signal_layer": "In2.Cu",
            "top_reference_layer": "In1.Cu",
            "bottom_reference_layer": "UNDEFINED",
            "width": 250000,
            "diff_pair_gap": 0,
            "delay": round(inner * 1e6),
        },
        {
            "signal_layer": "B.Cu",
            "top_reference_layer": "UNDEFINED",
            "bottom_reference_layer": "In1.Cu",
            "width": 250000,
            "diff_pair_gap": 0,
            "delay": round(outer * 1e6),
        },
    ]
    b.set_project_rules(
        use_height_for_length_calcs=True,
        classes=[
            {
                "name": "Default",
                "priority": 0,
                "clearance": 0.2,
                "track_width": 0.25,
                "via_diameter": 0.6,
                "via_drill": 0.3,
                "tuning_profile": "nominal-qualification",
            },
            {
                "name": "CAN",
                "priority": 1,
                "clearance": 0.2,
                "track_width": 0.25,
                "via_diameter": 0.6,
                "via_drill": 0.3,
                "diff_pair_width": 0.25,
                "diff_pair_gap": 0.35,
                "diff_pair_via_gap": 0.35,
            },
        ],
        netclass_patterns=[{"netclass": "CAN", "pattern": "^[AB]_[PN]$"}],
        tuning_profiles={
            "meta": {"version": 0},
            "tuning_profiles_impedance_geometric": [
                {
                    "profile_name": "nominal-qualification",
                    "type": 0,
                    "target_impedance": 0,
                    "enable_time_domain_tuning": True,
                    "layer_entries": entries,
                    "via_prop_delay": round(inner * 1e6),
                    "via_overrides": [],
                }
            ],
        },
    )
    b.set_dru_text(
        "(version 1)\n"
        '(rule "can_width" (condition "A.NetClass == \'CAN\'") (constraint track_width (min 0.25mm) (max 0.25mm)))\n'
        '(rule "can_gap" (condition "A.NetClass == \'CAN\'") (constraint diff_pair_gap (min 0.34mm) (max 0.36mm)))\n'
        '(rule "can_intra_skew" (condition "A.NetClass == \'CAN\'") (constraint skew (max 0.1mm) (within_diff_pairs)))\n'
        '(rule "can_group_skew" (condition "A.NetClass == \'CAN\'") (constraint skew (max 0.15mm)))\n'
        '(rule "can_uncoupled" (condition "A.NetClass == \'CAN\'") (constraint diff_pair_uncoupled (max 4mm)))\n'
        '(rule "dq_delay_skew" (condition "A.NetName == \'DQ0\' || A.NetName == \'DQ1\' || A.NetName == \'DQ2\' || A.NetName == \'DQ3\'") (constraint skew (max 1ps)))\n'
    )
    return b.save(directory)


@register_test(
    test_id="T4-05",
    tier=4,
    feature="F9",
    min_stage="M4",
    name="High-speed 4-layer differential pairs + length/delay/skew matching",
    description="Exercises: F5, F6, F7, F9",
)
def t4_05(ctx: DriverContext) -> dict[str, Any]:
    bundle = _build_t4_05_board(ctx.work_dir / "src")
    _baseline_drc_clean(bundle.pcb_path, refill_zones=True)
    assert verify_pour_integrity(bundle.pcb_path, bundle.pcb_path).passed

    require_m4_capability(
        ctx,
        require_help_flags=(
            "--diff-pairs",
            "--pair-layers",
            "--length-match-group",
            "--group-match-tolerance",
            "--group-delay-tolerance",
            "--protect",
            "--nets",
        ),
    )

    step1_pcb = ctx.work_dir / "step1" / "routed.kicad_pcb"
    step2_pcb = ctx.work_dir / "step2" / "routed.kicad_pcb"
    step1_pcb.parent.mkdir(parents=True, exist_ok=True)
    step2_pcb.parent.mkdir(parents=True, exist_ok=True)
    bh = snapshot_source_dir(bundle.directory)

    p1 = ctx.native_driver.run_native(
        bundle.pcb_path,
        step1_pcb,
        extra_args=[
            "--diff-pairs", "auto",
            "--pair-layers", "F.Cu", "In2.Cu", "B.Cu",
            "--nets", "A_P", "A_N", "B_P", "B_N",
            "--length-match-group", "A_P", "A_N", "B_P", "B_N",
            "--group-match-tolerance", "0.15",
            "--group-meander-amplitude", "6",
        ],
        timeout_s=180.0,
    )
    assert_no_crash(p1, "T4-05:step1")
    assert p1.returncode == 0, f"T4-05 step1 failed: {p1.stderr}"
    for suffix in (".kicad_pro", ".kicad_dru"):
        src_sidecar = bundle.pcb_path.with_suffix(suffix)
        if src_sidecar.is_file():
            shutil.copy2(src_sidecar, step1_pcb.with_suffix(suffix))

    p2 = ctx.native_driver.run_native(
        step1_pcb,
        step2_pcb,
        extra_args=[
            "--protect", "existing",
            "--nets", "DQ0", "DQ1", "DQ2", "DQ3",
            "--length-match-group", "DQ0", "DQ1", "DQ2", "DQ3",
            "--group-delay-tolerance", "1",
            "--group-meander-amplitude", "2",
        ],
        timeout_s=180.0,
    )
    assert_no_crash(p2, "T4-05:step2")
    assert p2.returncode == 0, f"T4-05 step2 failed: {p2.stderr}"

    metrics = assert_clean_e2e(
        bundle.pcb_path,
        step2_pcb,
        before_hashes=bh,
        source_dir=bundle.directory,
        refill_zones=True,
        check_pour_integrity=True,
    )
    dp = verify_diff_pair_native(bundle.pcb_path, step2_pcb, [("A_P", "A_N"), ("B_P", "B_N")], max_uncoupled_mm=4.0)
    ft_mm = verify_fromto_length_groups(
        step2_pcb, [["A_P", "A_N", "B_P", "B_N"]], tolerance_mm=0.15, sidecar_source=bundle.pcb_path
    )
    assert ft_mm.passed, f"T4-05 mm fromTo failed: {ft_mm.skew_violations}, {ft_mm.coverage_errors}"
    ft_ps = verify_fromto_length_groups(
        step2_pcb, [["DQ0", "DQ1", "DQ2", "DQ3"]], tolerance_ps=1.0, sidecar_source=bundle.pcb_path
    )
    assert ft_ps.passed, f"T4-05 ps fromTo failed: {ft_ps.skew_violations}, {ft_ps.coverage_errors}"

    pn = _pcbnew()
    board = pn.LoadBoard(str(step2_pcb))
    pair_tracks = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK" and t.GetNetname() in {"A_P", "A_N", "B_P", "B_N"}]
    assert any(t.GetLayer() == pn.In2_Cu for t in pair_tracks), "expected inner In2.Cu transition around F.Cu/B.Cu keepout"
    return {**metrics, "diff_pair": dp, "mm_measurements": len(ft_mm.measurements), "ps_measurements": len(ft_ps.measurements)}


# ============================================================================
# T4-06: Full agent-ee Native Workflow + Atomic Publish + Provenance
# ============================================================================


@register_test(
    test_id="T4-06",
    tier=4,
    feature="F10",
    min_stage="M5",
    name="Full agent-ee native workflow on placed RC board + provenance + ratchet + O_EXCL",
    description="Exercises: F4, F10, F11",
)
def t4_06(ctx: DriverContext) -> dict[str, Any]:
    ok, reason = ctx.agent_ee_driver.is_available()
    if not ok:
        raise PendingCapability(reason, evidence={"agent_ee_root": str(ctx.agent_ee_driver.agent_ee_root)})
    ae_root = ctx.agent_ee_driver.agent_ee_root
    placed_src = ae_root / "fixtures" / "rc" / "stages" / "placed-unrouted"
    assert (placed_src / "rc_stage.kicad_pcb").is_file()
    src_tree_before = snapshot_source_tree(placed_src)

    cand_root = ctx.work_dir / "cand"
    placed_dir = stage_fixture_copy(placed_src, cand_root / "placed")
    placed_tree_before = snapshot_source_tree(placed_dir)
    placed_before = snapshot_source_dir(placed_dir)
    placed_pcb = placed_dir / "rc_stage.kicad_pcb"

    _baseline_drc_clean(placed_pcb, refill_zones=True, check_parity=True)
    assert verify_pour_integrity(placed_pcb, placed_pcb, check_parity=True).passed
    assert_source_tree_untouched(placed_src, src_tree_before)
    assert_source_tree_untouched(placed_dir, placed_tree_before)

    ctx.agent_ee_driver.probe_rust_integration(ctx.native_driver)

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
    name='rc', label='e2e-t4-06', checkout=str(checkout), state=str(state), budget=120,
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
print(json.dumps({
    'exit_code': res['exit_code'],
    'summary': summary,
    'state_write_probe': report['state_write_probe'],
}))
"""
    proc = ctx.agent_ee_driver.run_python(
        script, str(cand_root), str(work_root), str(checkout), str(state), timeout_s=300.0
    )
    assert_no_crash(proc, "T4-06:adapter")
    assert proc.returncode == 0, f"T4-06 adapter failed: {proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["exit_code"] == 0, f"executor exit_code={payload['exit_code']}: {payload}"
    assert payload["summary"]["proposal"] == "eligible"
    assert payload["summary"]["classification"] == "complete"
    assert payload["state_write_probe"] and all(p.get("refused") is True for p in payload["state_write_probe"])
    cand_pcb = work_root / "routing" / "candidate" / "placed" / "rc_stage.kicad_pcb"
    adapter_metrics = assert_clean_e2e(
        placed_pcb,
        cand_pcb,
        before_hashes=placed_before,
        source_dir=placed_dir,
        refill_zones=True,
        check_pour_integrity=True,
        check_partition_ratchet=True,
        check_parity=True,
    )

    argv = ctx.agent_ee_driver.board_route_argv(
        checkout, placed_dir, ctx.work_dir / "argv_out", budget_s=60
    )
    assert not (
        any(a.endswith("bin/kicad-route") for a in argv)
        and "--backend" in argv
        and argv[argv.index("--backend") + 1] in ("builtin", "krt")
    ), f"board_route_argv still targets legacy Python backend: {argv}"

    direct_out = ctx.work_dir / "direct" / "rc_stage.kicad_pcb"
    direct_out.parent.mkdir(parents=True, exist_ok=True)
    p_dir = ctx.native_driver.run_native(placed_pcb, direct_out, timeout_s=180.0)
    assert_no_crash(p_dir, "T4-06:direct")
    assert p_dir.returncode == 0, f"T4-06 direct native failed: {p_dir.stderr}"
    assert_clean_e2e(
        placed_pcb,
        direct_out,
        before_hashes=placed_before,
        source_dir=placed_dir,
        refill_zones=True,
        check_pour_integrity=True,
        check_partition_ratchet=True,
        check_parity=True,
    )

    step1_pcb = ctx.work_dir / "step1" / "rc_stage.kicad_pcb"
    step2_pcb = ctx.work_dir / "step2" / "rc_stage.kicad_pcb"
    step1_pcb.parent.mkdir(parents=True, exist_ok=True)
    step2_pcb.parent.mkdir(parents=True, exist_ok=True)
    p1 = ctx.native_driver.run_native(placed_pcb, step1_pcb, extra_args=["--nets", "/IN"], timeout_s=180.0)
    assert p1.returncode == 0, f"T4-06 step1 failed: {p1.stderr}"
    for suffix in (".kicad_pro", ".kicad_dru", ".kicad_sch"):
        src_sidecar = placed_pcb.with_suffix(suffix)
        if src_sidecar.is_file():
            shutil.copy2(src_sidecar, step1_pcb.with_suffix(suffix))
    for table in ("fp-lib-table", "sym-lib-table"):
        src_table = placed_dir / table
        if src_table.is_file():
            shutil.copy2(src_table, step1_pcb.parent / table)
    src_lib = placed_dir / "lib"
    if src_lib.is_dir():
        shutil.copytree(src_lib, step1_pcb.parent / "lib", dirs_exist_ok=True)
    for pretty in placed_dir.glob("*.pretty"):
        if pretty.is_dir():
            shutil.copytree(pretty, step1_pcb.parent / pretty.name, dirs_exist_ok=True)
    p2 = ctx.native_driver.run_native(
        step1_pcb, step2_pcb, extra_args=["--protect", "human", "--nets", "!/IN"], timeout_s=180.0
    )
    assert p2.returncode == 0, f"T4-06 step2 --protect human failed: {p2.stderr}"
    verify_partition_ratchet(step1_pcb, step2_pcb)
    assert_clean_e2e(
        placed_pcb,
        step2_pcb,
        before_hashes=placed_before,
        source_dir=placed_dir,
        refill_zones=True,
        check_pour_integrity=True,
        check_partition_ratchet=True,
        check_parity=True,
    )

    sentinel = b"SENTINEL-O-EXCL-DO-NOT-OVERWRITE\n"
    sentinel_out = ctx.work_dir / "excl" / "rc_stage.kicad_pcb"
    sentinel_out.parent.mkdir(parents=True, exist_ok=True)
    sentinel_out.write_bytes(sentinel)
    p_excl = ctx.native_driver.run_native(placed_pcb, sentinel_out, timeout_s=180.0)
    assert_no_crash(p_excl, "T4-06:o_excl")
    assert p_excl.returncode == 8 and sentinel_out.read_bytes() == sentinel

    sig_cand = copper_signature(cand_pcb)
    wall_s = float(p_dir.metrics.wall_s) if p_dir.metrics and p_dir.metrics.wall_s > 0 else 1.0
    rss_kib = int(p_dir.metrics.max_rss_kib) if p_dir.metrics and p_dir.metrics.max_rss_kib > 0 else 10_000
    sample = {
        "wall_s": wall_s,
        "max_rss_kib": rss_kib,
        "process_tree_peak_rss_kib": rss_kib,
        "quality": {
            "process": "PASS",
            "routing": "COMPLETE",
            "validation": "PASS",
            "unconnected": 0,
            "open_nets": [],
            "preservation": {"footprints": "PASS", "copper": "PASS"},
            "new_violations": 0,
            "input_fingerprint": placed_before["rc_stage.kicad_pcb"],
            "copper": sig_cand,
            "pairs": [],
            "wall_clock_stop": False,
        },
    }
    cmp_proc, cmp_verdict = ctx.scripts_driver.eval_compare_router(
        "compare",
        {
            "reference": [sample, sample, sample],
            "candidate": [sample, sample, sample],
            "warmup": {"reference": sample, "candidate": sample},
            "mode": "algorithm-change",
            "claim": "outcome-fitness",
        },
    )
    assert cmp_proc.returncode == 0 and cmp_verdict["passed"] is True
    assert cmp_verdict["outcome_fitness"]["passed"] is True

    assert_source_tree_untouched(placed_src, src_tree_before)
    assert_source_tree_untouched(placed_dir, placed_tree_before)
    return {
        **adapter_metrics,
        "copper_signature": sig_cand,
        "o_excl_exit": p_excl.returncode,
        "outcome_fitness": True,
    }


# ============================================================================
# T4-07: Representative kicad-route/fixtures/* Regression Corpus
# ============================================================================


@register_test(
    test_id="T4-07",
    tier=4,
    feature="F11",
    min_stage="M5",
    name="Representative kicad-route/fixtures/* regression corpus (route_demo, bus_routing, ac_matching)",
    description="Exercises: F1, F2, F3, F4, F5, F6, F7, F8, F9, F10, F11",
)
def t4_07(ctx: DriverContext) -> dict[str, Any]:
    corpus_specs = (
        ("route_demo", "route_demo.kicad_pcb", True, True, True, True),
        ("bus_routing", "bus.kicad_pcb", False, False, False, False),
        ("ac_matching", "chain.kicad_pcb", False, False, False, False),
    )

    staged_bundles: list[tuple[str, Path, Path, dict[str, str], dict[str, str], dict[str, str], bool, bool, bool, bool]] = []
    for name, pcb_name, refill_zones, check_pour, check_ratchet, check_parity in corpus_specs:
        orig_dir = ctx.repo_root / "fixtures" / name
        assert (orig_dir / pcb_name).is_file(), f"missing fixture board: {orig_dir / pcb_name}"
        orig_tree = snapshot_source_tree(orig_dir)
        src_dir = stage_fixture_copy(orig_dir, ctx.work_dir / name / "src")
        src_tree = snapshot_source_tree(src_dir)
        src_top = snapshot_source_dir(src_dir)
        pcb_in = src_dir / pcb_name
        _baseline_drc_clean(pcb_in, refill_zones=refill_zones, check_parity=check_parity)
        if check_pour:
            assert verify_pour_integrity(pcb_in, pcb_in, check_parity=check_parity).passed
        assert_source_tree_untouched(orig_dir, orig_tree)
        staged_bundles.append(
            (name, orig_dir, src_dir, orig_tree, src_tree, src_top, refill_zones, check_pour, check_ratchet, check_parity)
        )

    ctx.agent_ee_driver.probe_rust_integration(ctx.native_driver)
    probe = ctx.native_driver.probe()
    pn = _pcbnew()
    fixture_results: dict[str, Any] = {}

    for name, orig_dir, src_dir, orig_tree, src_tree, src_top, refill_zones, check_pour, check_ratchet, check_parity in staged_bundles:
        pcb_name = dict((s[0], s[1]) for s in corpus_specs)[name]
        pcb_in = src_dir / pcb_name
        out_pcb = ctx.work_dir / name / "out" / pcb_name
        out_pcb.parent.mkdir(parents=True, exist_ok=True)

        extra_args: list[str] | None = None
        if name == "bus_routing" and "--bus" in probe.help_stdout:
            extra_args = ["--bus", "--bus-detection-radius", "20", "--bus-min-nets", "3"]
        elif name == "ac_matching":
            extra_args = ["--diff-pairs", "auto", "--pair-layers", "F.Cu"]
            if "--ac-couple-chain-json" in probe.help_stdout:
                extra_args.extend(
                    ["--ac-couple-chain-json", '["C1","C2"]', "--ac-match-tolerance", "0.1", "--ac-meander-amplitude", "3"]
                )

        proc = ctx.native_driver.run_native(pcb_in, out_pcb, extra_args=extra_args, timeout_s=180.0)
        assert_no_crash(proc, f"T4-07:{name}")
        if proc.returncode == 2:
            sj = parse_last_json_line_safe(proc.stderr)
            if (sj and sj.get("status") == "unsupported") or ("unsupported" in proc.stderr.lower()):
                raise PendingCapability(f"T4-07:{name} unsupported: {proc.stderr.strip()}", evidence=sj)
        assert proc.returncode == 0, f"T4-07:{name} failed (rc={proc.returncode}): {proc.stderr}"

        clean = assert_clean_e2e(
            pcb_in,
            out_pcb,
            before_hashes=src_top,
            source_dir=src_dir,
            refill_zones=refill_zones,
            check_pour_integrity=check_pour,
            check_partition_ratchet=check_ratchet,
            check_parity=check_parity,
        )
        assert_source_tree_untouched(src_dir, src_tree)
        assert_source_tree_untouched(orig_dir, orig_tree)

        extra_info: dict[str, Any] = {}
        if name == "route_demo":
            board = pn.LoadBoard(str(out_pcb))
            for t in board.GetTracks():
                if t.GetClass() == "PCB_TRACK" and t.GetNetname() in ("/VIN", "GND"):
                    assert int(t.GetWidth()) >= 500_000, f"route_demo Power track below class width: {t.GetNetname()} {t.GetWidth()}"
                elif t.GetClass() == "PCB_VIA" and t.GetNetname() in ("/VIN", "GND"):
                    assert int(t.GetWidth(t.TopLayer())) >= 800_000 and int(t.GetDrill()) >= 400_000, (
                        f"route_demo Power via below class size: {t.GetNetname()} {t.GetWidth(t.TopLayer())}/{t.GetDrill()}"
                    )
        elif name == "bus_routing":
            conn = read_native_connectivity(out_pcb)
            for net_name in ("AUX", *(f"D{i}" for i in range(8))):
                parts = conn.pad_partitions_by_net.get(net_name, [])
                assert len(parts) == 1, f"bus_routing net {net_name} disconnected: {parts}"
            assert len(conn.pad_partitions_by_net["D3"][0]) == 3
            board = pn.LoadBoard(str(out_pcb))
            for t in board.GetTracks():
                if t.GetClass() == "PCB_TRACK":
                    assert int(t.GetWidth()) >= 250_000
        elif name == "ac_matching":
            dp = verify_diff_pair_native(
                pcb_in, out_pcb, [("DPA_P", "DPA_N"), ("DPB_P", "DPB_N")], max_uncoupled_mm=4.0
            )
            ft_local = verify_fromto_length_groups(
                out_pcb, [["DPA_P", "DPA_N"], ["DPB_P", "DPB_N"]], tolerance_mm=0.2, sidecar_source=pcb_in
            )
            assert ft_local.passed, f"ac_matching local skew failed: {ft_local.skew_violations}, {ft_local.coverage_errors}"
            ft_p = verify_fromto_length_groups(
                out_pcb, [["DPA_P", "DPB_P"]], tolerance_mm=50.0, sidecar_source=pcb_in
            )
            ft_n = verify_fromto_length_groups(
                out_pcb, [["DPA_N", "DPB_N"]], tolerance_mm=50.0, sidecar_source=pcb_in
            )
            assert not ft_p.coverage_errors and not ft_n.coverage_errors
            assert len(ft_p.measurements) == 2 and len(ft_n.measurements) == 2
            for m in (*ft_p.measurements, *ft_n.measurements):
                assert m.get("actual") is not None, f"missing actual measurement: {m}"
            sum_p = sum(float(m["actual"]) for m in ft_p.measurements)
            sum_n = sum(float(m["actual"]) for m in ft_n.measurements)
            composed_skew = round(abs(sum_p - sum_n), 6)
            assert composed_skew <= 0.1, f"ac_matching composed chain skew {composed_skew} mm > 0.1 mm"
            extra_info = {"diff_pair": dp, "composed_skew_mm": composed_skew}

        fixture_results[name] = {
            **clean,
            **extra_info,
            "copper_signature": copper_signature(out_pcb),
            "wall_s": proc.metrics.wall_s if proc.metrics else None,
            "cpu_s": proc.metrics.cpu_s if proc.metrics else None,
            "max_rss_kib": proc.metrics.max_rss_kib if proc.metrics else None,
        }

    return {"fixtures": fixture_results}
