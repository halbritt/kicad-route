"""Shared fixture helpers and opaque-box assertions for Features 1-6."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Any

from tests.e2e_rust_router.drivers import (
    MeasuredProcessResult,
    RouteE2EResult,
    sha256_bytes,
    sha256_file,
)
from tests.e2e_rust_router.kicad_verify import (
    BoardBuilder,
    FixtureBundle,
    _pcbnew,
    is_canonical_non_nil_uuid,
    verify_routed_board,
)
from tests.e2e_rust_router.runner import DriverContext, PendingCapability

STD_RULES: dict[str, int] = {
    "width_nm": 250_000,
    "clearance_nm": 1_000_000,
    "grid_nm": 250_000,
    "edge_clearance_nm": 500_000,
}


def canonical_uuid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


def write_json_file(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def snapshot_source_dir(source_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for p in sorted(source_dir.iterdir()):
        if p.is_file():
            hashes[p.name] = sha256_file(p)
    return hashes


def assert_source_dir_untouched(source_dir: Path, before_hashes: dict[str, str]) -> None:
    after: dict[str, str] = {}
    for p in sorted(source_dir.iterdir()):
        if p.is_file():
            after[p.name] = sha256_file(p)
    if after != before_hashes:
        extra = set(after) - set(before_hashes)
        missing = set(before_hashes) - set(after)
        changed = [k for k in set(after) & set(before_hashes) if after[k] != before_hashes[k]]
        raise AssertionError(
            f"source directory mutated ({source_dir}): extra={sorted(extra)}, "
            f"missing={sorted(missing)}, changed={sorted(changed)}"
        )


def assert_no_crash(proc: MeasuredProcessResult, label: str) -> None:
    if proc.returncode == 7:
        raise AssertionError(f"{label} exited 7 (internal_error / panic): {proc.stderr}")
    if "Traceback (most recent call last)" in proc.stderr:
        raise AssertionError(f"{label} raised Python traceback:\n{proc.stderr}")
    if "panicked at" in proc.stderr.lower():
        raise AssertionError(f"{label} panicked:\n{proc.stderr}")


def assert_clean_e2e(
    before_pcb: Path,
    after_pcb: Path,
    *,
    before_hashes: dict[str, str],
    source_dir: Path,
    plan_json: dict[str, Any] | None = None,
    request_bytes: bytes | None = None,
    expected_opens: int = 0,
    refill_zones: bool = False,
    allow_removed_track_ids: tuple[str, ...] | set[str] = (),
    require_checks: tuple[str, ...] = (),
    check_pour_integrity: bool = False,
    pour_area_loss_pct: float | None = None,
    pour_min_spokes: int | None = None,
    check_partition_ratchet: bool = False,
    check_parity: bool = False,
) -> dict[str, Any]:
    assert_source_dir_untouched(source_dir, before_hashes)
    if not after_pcb.is_file():
        raise AssertionError(f"expected output board does not exist: {after_pcb}")
    if plan_json is not None:
        if plan_json.get("version") != 3:
            raise AssertionError(f"expected result version 3, got {plan_json.get('version')!r}")
        if plan_json.get("status") != "complete":
            raise AssertionError(f"expected complete status, got {plan_json.get('status')!r}")
        if request_bytes is not None:
            expected_sha = sha256_bytes(request_bytes)
            if plan_json.get("request_sha256") != expected_sha:
                raise AssertionError(
                    f"request_sha256 mismatch: {plan_json.get('request_sha256')} != {expected_sha}"
                )
        seen_ids: set[str] = set()
        for tr in plan_json.get("added_tracks", []):
            tid = tr.get("id")
            if not is_canonical_non_nil_uuid(tid) or tid in seen_ids:
                raise AssertionError(f"bad added_track uuid: {tr}")
            seen_ids.add(tid)
    return verify_routed_board(
        before_pcb,
        after_pcb,
        expected_opens=expected_opens,
        refill_zones=refill_zones,
        allow_removed_track_ids=allow_removed_track_ids,
        require_checks=require_checks,
        check_pour_integrity=check_pour_integrity,
        pour_area_loss_pct=pour_area_loss_pct,
        pour_min_spokes=pour_min_spokes,
        check_partition_ratchet=check_partition_ratchet,
        check_parity=check_parity,
    )


def run_wire_or_pending(
    ctx: DriverContext,
    board_pcb: Path,
    out_pcb: Path,
    rules: dict[str, int],
    *,
    tag: str = "run",
    max_expansions: int = 100_000,
    limits: dict[str, int] | None = None,
    allow_unsupported_engine: bool = True,
) -> tuple[RouteE2EResult, dict[str, str]]:
    source_dir = board_pcb.parent
    before_hashes = snapshot_source_dir(source_dir)
    run_dir = ctx.work_dir / tag
    rules_path = write_json_file(run_dir / "rules.json", rules)
    limits_path = write_json_file(run_dir / "limits.json", limits) if limits is not None else None

    res = ctx.json_driver.route_board_e2e(
        board_pcb,
        out_pcb,
        rules_path,
        work_dir=run_dir,
        max_expansions=max_expansions,
        limits_path=limits_path,
    )
    assert_no_crash(res.export_res.proc, f"{tag}:export")
    if res.engine_res is not None:
        assert_no_crash(res.engine_res.proc, f"{tag}:engine")
    if res.apply_res is not None:
        assert_no_crash(res.apply_res.proc, f"{tag}:apply")

    assert_source_dir_untouched(source_dir, before_hashes)

    if res.stage_failed == "export":
        sj = res.export_res.stderr_json or {}
        if res.export_res.proc.returncode == 2 and sj.get("status") == "unsupported":
            raise PendingCapability(
                f"export unsupported: {sj.get('reason', '')}",
                evidence={"stage": "export", "stderr_json": sj},
            )
    elif res.stage_failed == "engine" and allow_unsupported_engine:
        assert res.engine_res is not None
        oj = res.engine_res.output_json or {}
        if res.engine_res.proc.returncode == 2 and oj.get("status") == "unsupported":
            raise PendingCapability(
                f"engine unsupported: {oj.get('reason', '')}",
                evidence={"stage": "engine", "output_json": oj},
            )
    return res, before_hashes


def build_std_2pad_board(
    directory: Path,
    *,
    name: str = "board",
    seed: int = 1,
    x1: int = 20_000_000,
    y1: int = 20_000_000,
    with_obstacle: bool = True,
) -> FixtureBundle:
    b = BoardBuilder(copper_layers=2, seed=seed)
    b.set_rect_outline(0, 0, x1, y1)
    for idx, x in enumerate((3_000_000, 17_000_000), 1):
        fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
        b.add_pad(fp, "1", "SIGNAL", (x, 10_000_000), size_nm=(1_000_000, 1_000_000))
    if with_obstacle:
        b.add_track("OBSTACLE", (10_000_000, 4_000_000), (10_000_000, 16_000_000), 250_000, locked=True)
    return b.save(directory, name=name)


def build_input1_json(
    *,
    bounds_min: tuple[int, int] = (0, 0),
    bounds_max: tuple[int, int] = (20_000_000, 20_000_000),
    rules: dict[str, int] | None = None,
    nets: list[dict[str, Any]] | None = None,
    pads: list[dict[str, Any]] | None = None,
    tracks: list[dict[str, Any]] | None = None,
    max_expansions: int = 100_000,
    limits: dict[str, int] | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "version": 1,
        "source_id": source_id or ("a" * 64),
        "layer": "F.Cu",
        "bounds": {"min": list(bounds_min), "max": list(bounds_max)},
        "rules": dict(rules or STD_RULES),
        "nets": nets if nets is not None else [{"id": 0, "name": ""}, {"id": 1, "name": "SIGNAL"}],
        "pads": pads if pads is not None else [],
        "tracks": tracks if tracks is not None else [],
        "max_expansions": max_expansions,
    }
    if limits is not None:
        doc["limits"] = limits
    return doc


def run_m3_board_or_pending(
    ctx: DriverContext,
    bundle: FixtureBundle,
    out_pcb: Path,
    rules: dict[str, int] | None = None,
    *,
    tag: str = "m3",
    max_expansions: int = 100_000,
) -> tuple[str, Any, dict[str, str]]:
    """Run native orchestration if M2+ supported, else JSON wire; raise PendingCapability on exit-2 unsupported."""
    source_dir = bundle.pcb_path.parent
    before_hashes = snapshot_source_dir(source_dir)
    if ctx.native_driver.probe().supported:
        run_dir = ctx.work_dir / tag
        rules_path = write_json_file(run_dir / "rules.json", rules or STD_RULES) if rules else None
        proc = ctx.native_driver.run_native(
            bundle.pcb_path,
            out_pcb,
            rules_path=rules_path,
            max_expansions=max_expansions,
        )
        assert_no_crash(proc, f"{tag}:native")
        assert_source_dir_untouched(source_dir, before_hashes)
        if proc.returncode == 2:
            sj = parse_last_json_line_safe(proc.stderr)
            if sj and sj.get("status") == "unsupported":
                raise PendingCapability(f"native unsupported: {sj.get('reason', '')}", evidence=sj)
            if "unsupported" in proc.stderr.lower():
                raise PendingCapability(f"native unsupported: {proc.stderr.strip()}")
        return "native", proc, before_hashes

    res, before_hashes = run_wire_or_pending(
        ctx,
        bundle.pcb_path,
        out_pcb,
        rules or STD_RULES,
        tag=tag,
        max_expansions=max_expansions,
    )
    return "wire", res, before_hashes


def parse_last_json_line_safe(text: str) -> dict[str, Any] | None:
    from tests.e2e_rust_router.drivers import parse_last_json_line

    return parse_last_json_line(text)


def require_m3_capability(ctx: DriverContext) -> str:
    """Probe a positive M3 control board (45-deg rotated pad + .kicad_pro) before fail-closed M3 tests."""
    pn = _pcbnew()
    probe_dir = ctx.work_dir / "_m3_probe"
    b = BoardBuilder(copper_layers=2, seed=9903)
    b.set_rect_outline(0, 0, 20_000_000, 20_000_000)
    fp1 = b.add_footprint("J1", (4_000_000, 10_000_000))
    b.add_pad(
        fp1,
        "1",
        "SIGNAL",
        (4_000_000, 10_000_000),
        shape=pn.PAD_SHAPE_RECT,
        size_nm=(2_000_000, 1_000_000),
        angle_deg=45.0,
    )
    fp2 = b.add_footprint("J2", (16_000_000, 10_000_000))
    b.add_pad(fp2, "1", "SIGNAL", (16_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000))
    b.set_project_rules()
    bundle = b.save(probe_dir / "src")
    out_pcb = probe_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    before_hashes = snapshot_source_dir(bundle.directory)

    if ctx.native_driver.probe().supported:
        rules_path = write_json_file(probe_dir / "rules.json", STD_RULES)
        proc = ctx.native_driver.run_native(
            bundle.pcb_path, out_pcb, rules_path=rules_path, max_expansions=100_000
        )
        assert_no_crash(proc, "m3_probe:native")
        assert_source_dir_untouched(bundle.directory, before_hashes)
        if proc.returncode == 2:
            sj = parse_last_json_line_safe(proc.stderr)
            if (sj and sj.get("status") == "unsupported") or ("unsupported" in proc.stderr.lower()):
                raise PendingCapability(
                    f"M3 native capability missing: {(sj or {}).get('reason', proc.stderr.strip())}",
                    evidence=sj,
                )
        assert proc.returncode == 0, (
            f"M3 positive control failed unexpectedly (rc={proc.returncode}): {proc.stderr}"
        )
        return "native"

    res, _ = run_wire_or_pending(
        ctx, bundle.pcb_path, out_pcb, STD_RULES, tag="_m3_probe_wire"
    )
    assert res.stage_failed is None, f"M3 positive wire control failed at {res.stage_failed}"
    return "wire"


def run_m3_board_direct(
    ctx: DriverContext,
    bundle: FixtureBundle,
    out_pcb: Path,
    rules: dict[str, int] | None = None,
    *,
    mode: str,
    tag: str = "m3_direct",
    max_expansions: int = 100_000,
) -> tuple[str, Any, dict[str, str]]:
    """Invoke native_driver or json_driver directly without converting exit-2 unsupported to PendingCapability."""
    source_dir = bundle.pcb_path.parent
    before_hashes = snapshot_source_dir(source_dir)
    run_dir = ctx.work_dir / tag
    if mode == "native":
        rules_path = write_json_file(run_dir / "rules.json", rules) if rules else None
        proc = ctx.native_driver.run_native(
            bundle.pcb_path,
            out_pcb,
            rules_path=rules_path,
            max_expansions=max_expansions,
        )
        assert_no_crash(proc, f"{tag}:native")
        assert_source_dir_untouched(source_dir, before_hashes)
        return "native", proc, before_hashes

    rules_path = write_json_file(run_dir / "rules.json", rules or STD_RULES)
    res = ctx.json_driver.route_board_e2e(
        bundle.pcb_path,
        out_pcb,
        rules_path,
        work_dir=run_dir,
        max_expansions=max_expansions,
    )
    assert_no_crash(res.export_res.proc, f"{tag}:export")
    if res.engine_res is not None:
        assert_no_crash(res.engine_res.proc, f"{tag}:engine")
    if res.apply_res is not None:
        assert_no_crash(res.apply_res.proc, f"{tag}:apply")
    assert_source_dir_untouched(source_dir, before_hashes)
    return "wire", res, before_hashes


def stage_fixture_copy(src_dir: Path, dst_dir: Path) -> Path:
    """Copy an owned/MIT fixture directory into dst_dir without mutating src_dir."""
    src_dir = src_dir.resolve()
    dst_dir = dst_dir.resolve()
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    shutil.copytree(src_dir, dst_dir, symlinks=True)
    return dst_dir


def run_m4_board_or_pending(
    ctx: DriverContext,
    bundle: FixtureBundle,
    out_pcb: Path,
    rules: dict[str, int] | None = None,
    *,
    extra_args: list[str] | None = None,
    require_help_flags: tuple[str, ...] = (),
    tag: str = "m4",
    max_expansions: int = 200_000,
    limits: dict[str, int] | None = None,
) -> tuple[str, Any, dict[str, str]]:
    """Run M4 native orchestration (or JSON wire for plain routing); raise PendingCapability if unimplemented."""
    source_dir = bundle.pcb_path.parent
    before_hashes = snapshot_source_dir(source_dir)
    probe = ctx.native_driver.probe()
    if require_help_flags:
        if not probe.supported:
            raise PendingCapability(probe.reason, evidence={"probe": probe.reason})
        missing = [f for f in require_help_flags if f not in probe.help_stdout]
        if missing:
            raise PendingCapability(
                f"native CLI missing M4 flag(s): {missing}",
                evidence={"missing_flags": missing},
            )
    if probe.supported:
        run_dir = ctx.work_dir / tag
        rules_path = write_json_file(run_dir / "rules.json", rules) if rules else None
        limits_path = write_json_file(run_dir / "limits.json", limits) if limits else None
        proc = ctx.native_driver.run_native(
            bundle.pcb_path,
            out_pcb,
            rules_path=rules_path,
            max_expansions=max_expansions,
            limits_path=limits_path,
            extra_args=extra_args,
        )
        assert_no_crash(proc, f"{tag}:native")
        assert_source_dir_untouched(source_dir, before_hashes)
        if proc.returncode == 2:
            sj = parse_last_json_line_safe(proc.stderr)
            if (sj and sj.get("status") == "unsupported") or ("unsupported" in proc.stderr.lower()):
                raise PendingCapability(
                    f"native M4 unsupported: {(sj or {}).get('reason', proc.stderr.strip())}",
                    evidence=sj,
                )
        return "native", proc, before_hashes

    res, before_hashes = run_wire_or_pending(
        ctx,
        bundle.pcb_path,
        out_pcb,
        rules or STD_RULES,
        tag=tag,
        max_expansions=max_expansions,
        limits=limits,
    )
    return "wire", res, before_hashes


def require_m4_capability(
    ctx: DriverContext,
    *,
    require_help_flags: tuple[str, ...] = (),
) -> str:
    """Probe M4 rip-up/reroute (and optional M4 CLI flags) before fail-closed M4 tests."""
    probe = ctx.native_driver.probe()
    if require_help_flags:
        if not probe.supported:
            raise PendingCapability(probe.reason, evidence={"probe": probe.reason})
        missing = [f for f in require_help_flags if f not in probe.help_stdout]
        if missing:
            raise PendingCapability(
                f"native CLI missing M4 flag(s): {missing}",
                evidence={"missing_flags": missing},
            )
    probe_dir = ctx.work_dir / "_m4_probe"
    b = build_crossing_nets_board_builder(num_nets=2, seed=9904)
    bundle = b.save(probe_dir / "src")
    out_pcb = probe_dir / "out" / "routed.kicad_pcb"
    out_pcb.parent.mkdir(parents=True, exist_ok=True)
    mode, res, _ = run_m4_board_or_pending(ctx, bundle, out_pcb, tag="_m4_probe_run")
    if mode == "native":
        if res.returncode != 0:
            raise PendingCapability(
                f"M4 rip-up positive control did not complete (rc={res.returncode}): {res.stderr}",
                evidence={"returncode": res.returncode},
            )
        return "native"
    if res.stage_failed is not None:
        raise PendingCapability(
            f"M4 wire positive control failed at {res.stage_failed} (rip-up not yet landed)",
            evidence={"stage_failed": res.stage_failed},
        )
    return "wire"


def build_keepout_board(
    directory: Path,
    *,
    mode: str = "no_tracks",
    seed: int = 701,
) -> FixtureBundle:
    pn = _pcbnew()
    if mode == "no_vias_only":
        b = BoardBuilder(copper_layers=4, seed=seed)
        b.set_rect_outline(0, 0, 30_000_000, 20_000_000)
        fp1 = b.add_footprint("J1", (5_000_000, 10_000_000))
        b.add_pad(fp1, "1", "SIGNAL", (5_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000))
        fp2 = b.add_footprint("J2", (25_000_000, 10_000_000), side="B")
        b.add_pad(
            fp2,
            "1",
            "SIGNAL",
            (25_000_000, 10_000_000),
            size_nm=(1_000_000, 1_000_000),
            layers=[pn.B_Cu],
        )
        b.add_keepout(
            [pn.F_Cu],
            [(4_000_000, 4_000_000), (16_000_000, 4_000_000), (16_000_000, 16_000_000), (4_000_000, 16_000_000)],
            no_tracks=False,
            no_vias=True,
            no_zones=False,
        )
        b.set_project_rules()
        return b.save(directory)

    b = BoardBuilder(copper_layers=2, seed=seed)
    b.set_rect_outline(0, 0, 30_000_000, 20_000_000)
    fp1 = b.add_footprint("J1", (5_000_000, 10_000_000))
    b.add_pad(fp1, "1", "SIGNAL", (5_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000))
    fp2 = b.add_footprint("J2", (25_000_000, 10_000_000))
    b.add_pad(fp2, "1", "SIGNAL", (25_000_000, 10_000_000), size_nm=(1_000_000, 1_000_000))
    if mode == "full_wall":
        pts = [(14_000_000, 0), (16_000_000, 0), (16_000_000, 20_000_000), (14_000_000, 20_000_000)]
        b.add_keepout([pn.F_Cu, pn.B_Cu], pts, no_tracks=True, no_vias=True, no_zones=True)
    else:
        pts = [(13_000_000, 6_000_000), (17_000_000, 6_000_000), (17_000_000, 14_000_000), (13_000_000, 14_000_000)]
        b.add_keepout([pn.F_Cu], pts, no_tracks=True, no_vias=True, no_zones=True)
    b.set_project_rules()
    return b.save(directory)


def build_pour_board(
    directory: Path,
    *,
    layers: int = 2,
    min_resolved_spokes: int = 2,
    seed: int = 703,
) -> FixtureBundle:
    pn = _pcbnew()
    b = BoardBuilder(copper_layers=layers, seed=seed)
    b.set_design_settings(min_resolved_spokes=min_resolved_spokes)
    b.set_rect_outline(0, 0, 30_000_000, 20_000_000)
    if layers == 2:
        for idx, x in enumerate((6_000_000, 24_000_000), 1):
            fp = b.add_footprint(f"J{idx}", (x, 8_000_000))
            b.add_pad(
                fp,
                "1",
                "SIG",
                (x, 15_000_000),
                size_nm=(1_200_000, 1_200_000),
            )
            b.add_pad(
                fp,
                "2",
                "GND",
                (x, 7_000_000),
                attrib=pn.PAD_ATTRIB_PTH,
                size_nm=(1_700_000, 1_700_000),
                drill_size_nm=(800_000, 800_000),
            )
        b.add_zone(
            "GND",
            [pn.B_Cu],
            [(1_000_000, 1_000_000), (29_000_000, 1_000_000), (29_000_000, 19_000_000), (1_000_000, 19_000_000)],
            zone_name="GND_B",
        )
    else:
        for idx, x in enumerate((6_000_000, 24_000_000), 1):
            fp = b.add_footprint(f"J{idx}", (x, 10_000_000))
            b.add_pad(fp, "1", "SIG", (x, 15_000_000), size_nm=(1_000_000, 1_000_000))
            b.add_pad(fp, "2", "GND", (x, 7_000_000), size_nm=(1_200_000, 1_200_000))
        b.add_zone(
            "GND",
            [pn.In1_Cu],
            [(1_000_000, 1_000_000), (29_000_000, 1_000_000), (29_000_000, 19_000_000), (1_000_000, 19_000_000)],
            zone_name="GND_IN1",
        )
    b.set_project_rules()
    return b.save(directory)


def build_crossing_nets_board_builder(
    *,
    num_nets: int = 2,
    seed: int = 801,
) -> BoardBuilder:
    if num_nets == 2:
        b = BoardBuilder(copper_layers=2, seed=seed)
        b.set_rect_outline(0, 0, 24_000_000, 16_000_000)
        # Net A wants horizontal middle corridor y=8mm, Net B needs vertical bottleneck x=12mm.
        # B.Cu blocked by locked keepout/tracks on B.Cu so F.Cu rip-up/detour is forced.
        for idx, pos in enumerate(((4_000_000, 8_000_000), (20_000_000, 8_000_000)), 1):
            fp = b.add_footprint(f"JA{idx}", pos)
            b.add_pad(fp, "1", "NET_A", pos, size_nm=(1_000_000, 1_000_000))
        for idx, pos in enumerate(((12_000_000, 3_000_000), (12_000_000, 13_000_000)), 1):
            fp = b.add_footprint(f"JB{idx}", pos)
            b.add_pad(fp, "1", "NET_B", pos, size_nm=(1_000_000, 1_000_000))
        b.add_track("OBSTACLE", (10_000_000, 2_000_000), (10_000_000, 6_500_000), 250_000, locked=True)
        b.add_track("OBSTACLE", (14_000_000, 2_000_000), (14_000_000, 6_500_000), 250_000, locked=True)
        b.set_project_rules()
        return b

    b = BoardBuilder(copper_layers=2, seed=seed)
    b.set_rect_outline(0, 0, 30_000_000, 30_000_000)
    for i in range(num_nets):
        y0 = 4_000_000 + i * 3_000_000
        y1 = 4_000_000 + (num_nets - 1 - i) * 3_000_000
        fp1 = b.add_footprint(f"J{i + 1}A", (4_000_000, y0))
        b.add_pad(fp1, "1", f"SIG_{i}", (4_000_000, y0), size_nm=(1_000_000, 1_000_000))
        fp2 = b.add_footprint(f"J{i + 1}B", (26_000_000, y1))
        b.add_pad(fp2, "1", f"SIG_{i}", (26_000_000, y1), size_nm=(1_000_000, 1_000_000))
    b.set_project_rules()
    return b


def build_crossing_nets_board(
    directory: Path,
    *,
    num_nets: int = 2,
    seed: int = 801,
) -> FixtureBundle:
    return build_crossing_nets_board_builder(num_nets=num_nets, seed=seed).save(directory)


def build_diff_pair_board(
    directory: Path,
    *,
    pair_names: tuple[str, str] = ("CAN+", "CAN-"),
    copper_layers: int = 2,
    gap_mm: float = 0.35,
    width_mm: float = 0.25,
    clearance_mm: float = 0.2,
    seed: int = 901,
) -> FixtureBundle:
    b = BoardBuilder(copper_layers=copper_layers, seed=seed)
    b.set_rect_outline(0, 0, 30_000_000, 20_000_000)
    pitch_nm = int(round((width_mm + gap_mm) * 1_000_000))
    for ref, x in (("J1", 5_000_000), ("J2", 25_000_000)):
        fp = b.add_footprint(ref, (x, 10_000_000))
        for p_idx, net_name in enumerate(pair_names, 1):
            b.add_pad(
                fp,
                str(p_idx),
                net_name,
                (x, 10_000_000 + (p_idx - 1) * pitch_nm),
                size_nm=(600_000, 600_000),
            )
    b.set_project_rules(
        classes=[
            {
                "name": "Default",
                "priority": 0,
                "clearance": clearance_mm,
                "track_width": width_mm,
                "via_diameter": 0.6,
                "via_drill": 0.3,
                "microvia_diameter": 0.3,
                "microvia_drill": 0.15,
                "diff_pair_width": width_mm,
                "diff_pair_gap": gap_mm,
                "diff_pair_via_gap": gap_mm,
            }
        ]
    )
    b.set_dru_text(
        f'(version 1)\n'
        f'(rule "pair_gap"\n'
        f'  (constraint diff_pair_gap (min {gap_mm - 0.01:.2f}mm) (max {gap_mm + 0.01:.2f}mm))\n'
        f')\n'
    )
    return b.save(directory)
