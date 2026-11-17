"""Programmatic pcbnew 10.0.8 fixture builder and isolated native KiCad verifier."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
import uuid

MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
CANONICAL_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
NIL_UUID = "00000000-0000-0000-0000-000000000000"
CONSOLE_ERROR_RE = re.compile(
    r"(?im)^(?:\d{2}:\d{2}:\d{2}:\s*)?(?:fatal\s+)?error\b"
)
PARITY_SKIP_RE = re.compile(
    r"(?i)failed to fetch schematic netlist[^\n]*|parity tests require[^\n]*"
)


def _pcbnew():
    import pcbnew

    return pcbnew


def is_canonical_non_nil_uuid(value: str) -> bool:
    return (
        isinstance(value, str)
        and bool(CANONICAL_UUID_RE.match(value))
        and value != NIL_UUID
    )


@dataclass(frozen=True)
class FixtureBundle:
    directory: Path
    pcb_path: Path
    pro_path: Path | None = None
    dru_path: Path | None = None


class BoardBuilder:
    """Declarative programmatic KiCad 10 board/project/rule fixture builder."""

    def __init__(self, *, copper_layers: int = 2, seed: int = 1) -> None:
        pn = _pcbnew()
        pn.KIID.SeedGenerator(seed)
        self.pn = pn
        self.board = pn.BOARD()
        self.board.SetCopperLayerCount(copper_layers)
        self._nets: dict[str, int] = {"": 0}
        self._next_net_code = 1
        self._pro_data: dict[str, Any] | None = None
        self._dru_text: str | None = None
        self._stackup_sexpr: str | None = None

    def ensure_net(self, name: str, code: int | None = None) -> int:
        if not name:
            return 0
        if name in self._nets:
            return self._nets[name]
        assigned = code if code is not None else self._next_net_code
        while assigned in self._nets.values():
            assigned += 1
        self._next_net_code = max(self._next_net_code, assigned + 1)
        net = self.pn.NETINFO_ITEM(self.board, name, assigned)
        self.board.Add(net)
        self._nets[name] = assigned
        return assigned

    def set_design_settings(
        self,
        *,
        min_track_width: int | None = None,
        min_clearance: int | None = None,
        min_copper_edge_clearance: int | None = None,
        min_via_diameter: int | None = None,
        min_via_annular_width: int | None = None,
        min_hole_clearance: int | None = None,
        solder_mask_min_width: int | None = None,
        solder_mask_to_copper_clearance: int | None = None,
        solder_mask_expansion: int | None = None,
    ) -> BoardBuilder:
        ds = self.board.GetDesignSettings()
        if min_track_width is not None:
            ds.m_TrackMinWidth = min_track_width
        if min_clearance is not None:
            ds.m_MinClearance = min_clearance
        if min_copper_edge_clearance is not None:
            ds.m_CopperEdgeClearance = min_copper_edge_clearance
        if min_via_diameter is not None:
            ds.m_ViasMinSize = min_via_diameter
        if min_via_annular_width is not None:
            ds.m_ViasMinAnnularWidth = min_via_annular_width
        if min_hole_clearance is not None:
            ds.m_HoleClearance = min_hole_clearance
        if solder_mask_min_width is not None:
            ds.m_SolderMaskMinWidth = solder_mask_min_width
        if solder_mask_to_copper_clearance is not None:
            ds.m_SolderMaskToCopperClearance = solder_mask_to_copper_clearance
        if solder_mask_expansion is not None:
            ds.m_SolderMaskExpansion = solder_mask_expansion
        return self

    def set_rect_outline(
        self, x0: int, y0: int, x1: int, y1: int, *, width_nm: int = 50_000
    ) -> BoardBuilder:
        return self.set_polygon_outline(
            [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], width_nm=width_nm
        )

    def set_polygon_outline(
        self, vertices_nm: list[tuple[int, int]], *, width_nm: int = 50_000
    ) -> BoardBuilder:
        pn = self.pn
        for a, b in zip(vertices_nm, vertices_nm[1:] + vertices_nm[:1]):
            edge = pn.PCB_SHAPE(self.board)
            edge.SetShape(pn.SHAPE_T_SEGMENT)
            edge.SetLayer(pn.Edge_Cuts)
            edge.SetStart(pn.VECTOR2I(*a))
            edge.SetEnd(pn.VECTOR2I(*b))
            edge.SetWidth(width_nm)
            self.board.Add(edge)
        return self

    def add_drawing(
        self,
        layer: int,
        shape_t: int,
        start_nm: tuple[int, int],
        end_nm: tuple[int, int],
        *,
        width_nm: int = 50_000,
        footprint: Any = None,
        mid_nm: tuple[int, int] | None = None,
        bezier_c1_nm: tuple[int, int] | None = None,
        bezier_c2_nm: tuple[int, int] | None = None,
    ) -> Any:
        pn = self.pn
        parent = footprint if footprint is not None else self.board
        shape = pn.PCB_SHAPE(parent)
        shape.SetShape(shape_t)
        shape.SetLayer(layer)
        shape.SetStart(pn.VECTOR2I(*start_nm))
        shape.SetEnd(pn.VECTOR2I(*end_nm))
        if mid_nm is not None:
            shape.SetArcGeometry(
                pn.VECTOR2I(*start_nm), pn.VECTOR2I(*mid_nm), pn.VECTOR2I(*end_nm)
            )
        if bezier_c1_nm is not None:
            shape.SetBezierC1(pn.VECTOR2I(*bezier_c1_nm))
        if bezier_c2_nm is not None:
            shape.SetBezierC2(pn.VECTOR2I(*bezier_c2_nm))
        shape.SetWidth(width_nm)
        parent.Add(shape)
        return shape

    def add_footprint(
        self,
        ref: str,
        pos_nm: tuple[int, int],
        *,
        angle_deg: float = 0.0,
        side: str = "F",
        attrs: int | None = None,
        local_clearance_nm: int | None = None,
        local_mask_margin_nm: int | None = None,
        net_tie_groups: list[str] | None = None,
    ) -> Any:
        pn = self.pn
        fp = pn.FOOTPRINT(self.board)
        fp.SetReference(ref)
        fp.SetPosition(pn.VECTOR2I(*pos_nm))
        if angle_deg:
            fp.SetOrientationDegrees(angle_deg)
        if side == "B":
            fp.SetLayer(pn.B_Cu)
        if attrs is not None:
            fp.SetAttributes(attrs)
        if local_clearance_nm is not None:
            fp.SetLocalClearance(local_clearance_nm)
        if local_mask_margin_nm is not None:
            fp.SetLocalSolderMaskMargin(local_mask_margin_nm)
        if net_tie_groups:
            for grp in net_tie_groups:
                fp.AddNetTiePadGroup(grp)
        self.board.Add(fp)
        return fp

    def add_pad(
        self,
        footprint: Any,
        number: str,
        net_name: str,
        pos_nm: tuple[int, int],
        *,
        attrib: int | None = None,
        shape: int | None = None,
        size_nm: tuple[int, int] = (1_000_000, 1_000_000),
        angle_deg: float = 0.0,
        roundrect_ratio: float = 0.25,
        custom_poly_nm: list[tuple[int, int]] | None = None,
        custom_anchor: int | None = None,
        offset_nm: tuple[int, int] = (0, 0),
        drill_shape: int | None = None,
        drill_size_nm: tuple[int, int] = (0, 0),
        local_clearance_nm: int | None = None,
        local_mask_margin_nm: int | None = None,
        layers: list[int] | Any | None = None,
    ) -> Any:
        pn = self.pn
        pad = pn.PAD(footprint)
        pad.SetNumber(number)
        pad_attrib = pn.PAD_ATTRIB_SMD if attrib is None else attrib
        pad_shape = pn.PAD_SHAPE_CIRCLE if shape is None else shape
        pad.SetAttribute(pad_attrib)
        pad.SetShape(pad_shape)
        pad.SetPosition(pn.VECTOR2I(*pos_nm))
        pad.SetSize(pn.VECTOR2I(*size_nm))
        if angle_deg:
            pad.SetOrientationDegrees(angle_deg)
        if pad_shape == pn.PAD_SHAPE_ROUNDRECT:
            pad.SetRoundRectRadiusRatio(roundrect_ratio)
        if pad_shape == pn.PAD_SHAPE_CUSTOM:
            anchor = pn.PAD_SHAPE_CIRCLE if custom_anchor is None else custom_anchor
            pad.SetAnchorPadShape(pn.F_Cu, anchor)
            if custom_poly_nm:
                poly = pn.VECTOR_VECTOR2I()
                for x, y in custom_poly_nm:
                    poly.append(pn.VECTOR2I(x, y))
                pad.AddPrimitivePoly(pn.F_Cu, poly, 0, True)
        if offset_nm != (0, 0):
            pad.SetOffset(pn.VECTOR2I(*offset_nm))
        if drill_shape is not None:
            pad.SetDrillShape(drill_shape)
        if drill_size_nm != (0, 0):
            pad.SetDrillSize(pn.VECTOR2I(*drill_size_nm))
        if local_clearance_nm is not None:
            pad.SetLocalClearance(local_clearance_nm)
        if local_mask_margin_nm is not None:
            pad.SetLocalSolderMaskMargin(local_mask_margin_nm)
        if layers is None:
            lset = pn.LSET()
            if pad_attrib == pn.PAD_ATTRIB_PTH:
                lset = pn.PAD.PTHMask()
            elif pad_attrib == pn.PAD_ATTRIB_NPTH:
                lset = pn.PAD.UnplatedHoleMask()
            else:
                lset.AddLayer(pn.F_Cu)
            pad.SetLayerSet(lset)
        elif isinstance(layers, list):
            lset = pn.LSET()
            for lid in layers:
                lset.AddLayer(lid)
            pad.SetLayerSet(lset)
        else:
            pad.SetLayerSet(layers)
        pad.SetNetCode(self.ensure_net(net_name))
        footprint.Add(pad)
        return pad


    def add_track(
        self,
        net_name: str,
        start_nm: tuple[int, int],
        end_nm: tuple[int, int],
        width_nm: int,
        *,
        layer: int | None = None,
        locked: bool = False,
        uuid_str: str | None = None,
    ) -> Any:
        pn = self.pn
        tr = pn.PCB_TRACK(self.board)
        tr.SetStart(pn.VECTOR2I(*start_nm))
        tr.SetEnd(pn.VECTOR2I(*end_nm))
        tr.SetWidth(width_nm)
        tr.SetLayer(pn.F_Cu if layer is None else layer)
        tr.SetNetCode(self.ensure_net(net_name))
        tr.SetLocked(locked)
        if uuid_str is not None:
            tr.SetUuid(pn.KIID(uuid_str))
        self.board.Add(tr)
        return tr

    def add_arc(
        self,
        net_name: str,
        start_nm: tuple[int, int],
        mid_nm: tuple[int, int],
        end_nm: tuple[int, int],
        width_nm: int,
        *,
        layer: int | None = None,
        locked: bool = False,
        uuid_str: str | None = None,
    ) -> Any:
        pn = self.pn
        arc = pn.PCB_ARC(self.board)
        arc.SetStart(pn.VECTOR2I(*start_nm))
        arc.SetMid(pn.VECTOR2I(*mid_nm))
        arc.SetEnd(pn.VECTOR2I(*end_nm))
        arc.SetWidth(width_nm)
        arc.SetLayer(pn.F_Cu if layer is None else layer)
        arc.SetNetCode(self.ensure_net(net_name))
        arc.SetLocked(locked)
        if uuid_str is not None:
            arc.SetUuid(pn.KIID(uuid_str))
        self.board.Add(arc)
        return arc

    def add_via(
        self,
        net_name: str,
        pos_nm: tuple[int, int],
        width_nm: int,
        drill_nm: int,
        *,
        via_type: int | None = None,
        layer_pair: tuple[int, int] | None = None,
        locked: bool = False,
        uuid_str: str | None = None,
    ) -> Any:
        pn = self.pn
        via = pn.PCB_VIA(self.board)
        via.SetPosition(pn.VECTOR2I(*pos_nm))
        via.SetWidth(width_nm)
        via.SetDrill(drill_nm)
        via.SetViaType(pn.VIATYPE_THROUGH if via_type is None else via_type)
        if layer_pair is not None:
            via.SetLayerPair(layer_pair[0], layer_pair[1])
        via.SetNetCode(self.ensure_net(net_name))
        via.SetLocked(locked)
        if uuid_str is not None:
            via.SetUuid(pn.KIID(uuid_str))
        self.board.Add(via)
        return via

    def add_keepout(
        self,
        layers: list[int],
        outline_pts_nm: list[tuple[int, int]],
        *,
        no_tracks: bool = True,
        no_vias: bool = True,
        no_zones: bool = True,
        no_pads: bool = False,
        no_footprints: bool = False,
    ) -> Any:
        pn = self.pn
        zone = pn.ZONE(self.board)
        lset = pn.LSET()
        for lid in layers:
            lset.AddLayer(lid)
        zone.SetLayerSet(lset)
        zone.SetIsRuleArea(True)
        zone.SetDoNotAllowTracks(no_tracks)
        zone.SetDoNotAllowVias(no_vias)
        zone.SetDoNotAllowZoneFills(no_zones)
        zone.SetDoNotAllowPads(no_pads)
        zone.SetDoNotAllowFootprints(no_footprints)
        zone.Outline().NewOutline()
        for x, y in outline_pts_nm:
            zone.Outline().Append(x, y)
        self.board.Add(zone)
        return zone

    def set_project_rules(
        self,
        rules: dict[str, Any] | None = None,
        *,
        classes: list[dict[str, Any]] | None = None,
        netclass_patterns: list[dict[str, str]] | None = None,
        rule_severities: dict[str, str] | None = None,
    ) -> BoardBuilder:
        default_rules = {
            "min_clearance": 0.2,
            "min_track_width": 0.2,
            "min_copper_edge_clearance": 0.5,
            "min_via_diameter": 0.5,
            "min_through_hole_diameter": 0.3,
            "min_via_annular_width": 0.1,
            "min_hole_clearance": 0.25,
            "min_hole_to_hole": 0.25,
            "min_microvia_diameter": 0.2,
            "min_microvia_drill": 0.1,
            "allow_blind_buried_vias": False,
            "allow_microvias": False,
        }
        if rules:
            default_rules.update(rules)
        default_classes = classes or [
            {
                "name": "Default",
                "priority": 0,
                "clearance": 0.2,
                "track_width": 0.2,
                "via_diameter": 0.6,
                "via_drill": 0.3,
                "microvia_diameter": 0.3,
                "microvia_drill": 0.15,
            }
        ]
        self._pro_data = {
            "meta": {"version": 3},
            "board": {
                "design_settings": {
                    "rules": default_rules,
                    "rule_severities": dict(rule_severities or {}),
                }
            },
            "net_settings": {
                "meta": {"version": 5},
                "classes": default_classes,
                "netclass_patterns": list(netclass_patterns or []),
            },
        }
        return self

    def set_dru_text(self, text: str) -> BoardBuilder:
        self._dru_text = text
        return self

    def save(self, directory: Path, name: str = "board") -> FixtureBundle:
        directory.mkdir(parents=True, exist_ok=True)
        pcb_path = directory / f"{name}.kicad_pcb"
        if not self.pn.SaveBoard(str(pcb_path), self.board, True):
            raise RuntimeError(f"pcbnew.SaveBoard failed for {pcb_path}")
        pro_path = None
        dru_path = None
        if self._pro_data is not None:
            pro_path = directory / f"{name}.kicad_pro"
            pro_path.write_text(
                json.dumps(self._pro_data, indent=2) + "\n", encoding="utf-8"
            )
        if self._dru_text is not None:
            dru_path = directory / f"{name}.kicad_dru"
            dru_path.write_text(self._dru_text, encoding="utf-8")
        return FixtureBundle(
            directory=directory, pcb_path=pcb_path, pro_path=pro_path, dru_path=dru_path
        )


@dataclass(frozen=True)
class DrcReport:
    raw: dict[str, Any]
    violations: list[dict[str, Any]]
    unconnected_items: list[dict[str, Any]]
    ignored_checks: list[dict[str, Any]]
    schematic_parity: list[dict[str, Any]]
    console: str


@dataclass(frozen=True)
class ConnectivityReport:
    unconnected_count: int
    pad_partitions_by_net: dict[str, list[frozenset[str]]]
    connected_classes_by_pad: dict[str, set[str]]
    pad_net_by_uuid: dict[str, str]
    track_count: int
    via_count: int
    arc_count: int


def _stage_board_bundle(
    board_path: Path,
    stage_dir: Path,
    *,
    sidecar_source: Path | None = None,
) -> Path:
    board_path = board_path.resolve()
    src_dir = board_path.parent
    staged_pcb = stage_dir / board_path.name
    shutil.copy2(board_path, staged_pcb)
    stem = board_path.stem
    sidecar_dir = sidecar_source.resolve().parent if sidecar_source is not None else None
    sidecar_stem = sidecar_source.resolve().stem if sidecar_source is not None else None

    for suffix in (".kicad_pro", ".kicad_dru", ".kicad_sch"):
        cand = src_dir / f"{stem}{suffix}"
        if cand.is_file():
            shutil.copy2(cand, stage_dir / cand.name)
        elif sidecar_dir is not None and sidecar_stem is not None:
            fallback = sidecar_dir / f"{sidecar_stem}{suffix}"
            if fallback.is_file():
                shutil.copy2(fallback, stage_dir / f"{stem}{suffix}")

    for table in ("fp-lib-table", "sym-lib-table"):
        cand = src_dir / table
        if cand.is_file():
            shutil.copy2(cand, stage_dir / table)
        elif sidecar_dir is not None and (sidecar_dir / table).is_file():
            shutil.copy2(sidecar_dir / table, stage_dir / table)

    for pretty in src_dir.glob("*.pretty"):
        if pretty.is_dir():
            shutil.copytree(pretty, stage_dir / pretty.name)
    if sidecar_dir is not None:
        for pretty in sidecar_dir.glob("*.pretty"):
            dest = stage_dir / pretty.name
            if pretty.is_dir() and not dest.exists():
                shutil.copytree(pretty, dest)
    return staged_pcb


def run_native_drc(
    board_path: Path,
    *,
    refill_zones: bool = False,
    check_parity: bool = False,
    require_checks: tuple[str, ...] = (),
    timeout_s: float = 60.0,
    sidecar_source: Path | None = None,
    verify_dru: bool = True,
) -> DrcReport:
    with tempfile.TemporaryDirectory(prefix="e2e-drc-") as tmp:
        stage_dir = Path(tmp)
        staged_pcb = _stage_board_bundle(
            board_path, stage_dir, sidecar_source=sidecar_source
        )
        has_dru = staged_pcb.with_suffix(".kicad_dru").is_file()
        report_path = stage_dir / "drc-report.json"
        cmd = [
            "kicad-cli",
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-all",
            "--all-track-errors",
        ]
        if refill_zones:
            cmd.append("--refill-zones")
        if check_parity:
            cmd.append("--schematic-parity")
        cmd.extend(["--units", "mm", "-o", str(report_path), str(staged_pcb)])
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s
        )
        console = f"{res.stdout}\n{res.stderr}".strip()
        if res.returncode != 0:
            raise AssertionError(
                f"kicad-cli pcb drc failed (exit {res.returncode}): {console}"
            )
        if not report_path.is_file():
            raise AssertionError("kicad-cli pcb drc produced no report file")
        raw_bytes = report_path.read_bytes()
        if len(raw_bytes) > MAX_DOCUMENT_BYTES:
            raise AssertionError(f"DRC report exceeds 16 MiB: {len(raw_bytes)}")
        data = json.loads(raw_bytes.decode("utf-8"))
        if data.get("coordinate_units") != "mm":
            raise AssertionError(f"unexpected DRC units: {data.get('coordinate_units')!r}")
        kver = str(data.get("kicad_version", ""))
        if not kver.startswith("10."):
            raise AssertionError(f"unexpected KiCad version in DRC: {kver!r}")
        for key in ("violations", "unconnected_items", "ignored_checks", "schematic_parity"):
            if not isinstance(data.get(key), list):
                raise AssertionError(f"DRC report missing list field {key!r}")
        if CONSOLE_ERROR_RE.search(console):
            raise AssertionError(f"kicad-cli emitted console error: {console}")
        if check_parity and PARITY_SKIP_RE.search(console):
            raise AssertionError(f"kicad-cli skipped parity check: {console}")
        compile_errs = [v for v in data["violations"] if v.get("type") == "rule_compile_error"]
        if compile_errs:
            raise AssertionError(f"rule_compile_error in DRC: {compile_errs}")
        ignored_keys = {entry.get("key") for entry in data["ignored_checks"]}
        missing_req = [k for k in require_checks if k in ignored_keys]
        if missing_req:
            raise AssertionError(f"required DRC checks were ignored: {missing_req}")
        if verify_dru and has_dru:
            verify_dru_loaded(staged_pcb, timeout_s=timeout_s)
        return DrcReport(
            raw=data,
            violations=data["violations"],
            unconnected_items=data["unconnected_items"],
            ignored_checks=data["ignored_checks"],
            schematic_parity=data["schematic_parity"],
            console=console,
        )


def verify_dru_loaded(
    board_path: Path,
    *,
    timeout_s: float = 60.0,
    sidecar_source: Path | None = None,
) -> None:
    pn = _pcbnew()
    witness_rule = "_e2e_dru_witness"
    with tempfile.TemporaryDirectory(prefix="e2e-dru-witness-") as tmp:
        stage_dir = Path(tmp)
        staged_pcb = _stage_board_bundle(
            board_path, stage_dir, sidecar_source=sidecar_source
        )
        dru_path = staged_pcb.with_suffix(".kicad_dru")
        existing = dru_path.read_text(encoding="utf-8") if dru_path.is_file() else "(version 1)\n"
        dru_path.write_text(
            existing.rstrip() + f'\n(rule "{witness_rule}" (constraint disallow track))\n',
            encoding="utf-8",
        )
        pro_path = staged_pcb.with_suffix(".kicad_pro")
        if pro_path.is_file():
            pro = json.loads(pro_path.read_text(encoding="utf-8"))
            pro.setdefault("board", {}).setdefault("design_settings", {}).setdefault(
                "rule_severities", {}
            )["items_not_allowed"] = "error"
            pro_path.write_text(json.dumps(pro, indent=2) + "\n", encoding="utf-8")

        board = pn.LoadBoard(str(staged_pcb))
        assert board is not None
        if not list(board.GetTracks()):
            tr = pn.PCB_TRACK(board)
            tr.SetStart(pn.VECTOR2I(1_000_000, 1_000_000))
            tr.SetEnd(pn.VECTOR2I(2_000_000, 1_000_000))
            tr.SetWidth(250_000)
            tr.SetLayer(pn.F_Cu)
            board.Add(tr)
            pn.SaveBoard(str(staged_pcb), board, True)

        report = run_native_drc(staged_pcb, timeout_s=timeout_s, verify_dru=False)
        hit = [
            v
            for v in report.violations
            if v.get("type") == "items_not_allowed" and witness_rule in v.get("description", "")
        ]
        if not hit:
            raise AssertionError(
                f"LOAD_WITNESS failed: .kicad_dru rules were not compiled/enforced by KiCad"
            )


def _violation_key(v: dict[str, Any]) -> tuple[Any, ...]:
    items = tuple(
        sorted(
            (
                str(it.get("description", "")),
                round(float(it.get("pos", {}).get("x", 0.0)), 3),
                round(float(it.get("pos", {}).get("y", 0.0)), 3),
            )
            for it in v.get("items", [])
        )
    )
    return (v.get("type"), v.get("severity"), items)


def diff_drc_violations(
    before: DrcReport, after: DrcReport
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    before_counts = Counter(_violation_key(v) for v in before.violations)
    after_counts = Counter(_violation_key(v) for v in after.violations)
    introduced_counts = after_counts - before_counts
    resolved_counts = before_counts - after_counts

    introduced: list[dict[str, Any]] = []
    seen_after: Counter[tuple[Any, ...]] = Counter()
    for v in after.violations:
        k = _violation_key(v)
        seen_after[k] += 1
        if seen_after[k] <= introduced_counts[k]:
            introduced.append(v)

    resolved: list[dict[str, Any]] = []
    seen_before: Counter[tuple[Any, ...]] = Counter()
    for v in before.violations:
        k = _violation_key(v)
        seen_before[k] += 1
        if seen_before[k] <= resolved_counts[k]:
            resolved.append(v)

    return introduced, resolved


def read_native_connectivity(board_path: Path) -> ConnectivityReport:
    pn = _pcbnew()
    board = pn.LoadBoard(str(board_path))
    if board is None:
        raise AssertionError(f"pcbnew could not load {board_path}")
    if not board.BuildConnectivity():
        raise AssertionError(f"BuildConnectivity() failed on {board_path}")
    conn = board.GetConnectivity()
    unconnected_count = int(conn.GetUnconnectedCount(False))

    pad_net_by_uuid: dict[str, str] = {}
    electrical_pads: dict[str, Any] = {}
    seen_pad_uuids: set[str] = set()
    for pad in board.GetPads():
        pid = pad.m_Uuid.AsString()
        if pid in seen_pad_uuids:
            raise AssertionError(f"duplicate pad UUID in {board_path}: {pid}")
        seen_pad_uuids.add(pid)
        if pad.GetNetCode() > 0:
            electrical_pads[pid] = pad
            pad_net_by_uuid[pid] = pad.GetNetname()

    seen_copper_uuids: set[str] = set()
    for t in board.GetTracks():
        tid = t.m_Uuid.AsString()
        if tid in seen_copper_uuids:
            raise AssertionError(f"duplicate copper UUID ({t.GetClass()}) in {board_path}: {tid}")
        seen_copper_uuids.add(tid)

    adj: dict[str, set[str]] = {}
    classes_by_pad: dict[str, set[str]] = {}
    for pid, pad in electrical_pads.items():
        net_name = pad.GetNetname()
        connected_items = conn.GetConnectedItems(pad, 0)
        classes: set[str] = set()
        pad_peers: set[str] = {pid}
        for item in connected_items:
            cls = item.GetClass()
            classes.add(cls)
            if item.GetNetname() != net_name and item.GetNetCode() > 0:
                raise AssertionError(
                    f"cross-net connectivity on pad {pid} ({net_name} vs {item.GetNetname()})"
                )
            if cls == "PAD":
                pad_peers.add(item.m_Uuid.AsString())
        adj[pid] = pad_peers
        classes_by_pad[pid] = classes

    for pid, peers in adj.items():
        for qid in peers:
            if qid not in adj or pid not in adj[qid]:
                raise AssertionError(f"asymmetric pad connectivity between {pid} and {qid}")

    visited: set[str] = set()
    partitions_by_net: dict[str, list[frozenset[str]]] = defaultdict(list)
    for pid in sorted(electrical_pads):
        if pid in visited:
            continue
        comp = frozenset(adj[pid])
        visited.update(comp)
        partitions_by_net[pad_net_by_uuid[pid]].append(comp)

    track_count = sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_TRACK")
    via_count = sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_VIA")
    arc_count = sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_ARC")
    return ConnectivityReport(
        unconnected_count=unconnected_count,
        pad_partitions_by_net=dict(partitions_by_net),
        connected_classes_by_pad=classes_by_pad,
        pad_net_by_uuid=pad_net_by_uuid,
        track_count=track_count,
        via_count=via_count,
        arc_count=arc_count,
    )


def _pad_snapshot(pad: Any) -> tuple[Any, ...]:
    return (
        pad.m_Uuid.AsString(),
        pad.GetNumber(),
        pad.GetNetname(),
        (int(pad.GetPosition().x), int(pad.GetPosition().y)),
        int(pad.GetAttribute()),
        int(pad.GetShape()),
        (int(pad.GetSize().x), int(pad.GetSize().y)),
        round(float(pad.GetOrientationDegrees()), 6),
        round(float(pad.GetRoundRectRadiusRatio()), 6),
        int(pad.GetDrillShape()),
        (int(pad.GetDrillSize().x), int(pad.GetDrillSize().y)),
        (int(pad.GetOffset().x), int(pad.GetOffset().y)),
        pad.GetLocalClearance(),
        pad.GetLocalSolderMaskMargin(),
    )


def _track_snapshot(item: Any) -> tuple[Any, ...]:
    cls = item.GetClass()
    if cls == "PCB_VIA":
        return (
            cls,
            item.m_Uuid.AsString(),
            item.GetNetname(),
            (int(item.GetPosition().x), int(item.GetPosition().y)),
            int(item.GetWidth(item.TopLayer())),
            int(item.GetDrill()),
            int(item.GetViaType()),
            (int(item.TopLayer()), int(item.BottomLayer())),
            bool(item.IsLocked()),
        )
    if cls == "PCB_ARC":
        return (
            cls,
            item.m_Uuid.AsString(),
            item.GetNetname(),
            (int(item.GetStart().x), int(item.GetStart().y)),
            (int(item.GetMid().x), int(item.GetMid().y)),
            (int(item.GetEnd().x), int(item.GetEnd().y)),
            int(item.GetWidth()),
            int(item.GetLayer()),
            bool(item.IsLocked()),
        )
    return (
        cls,
        item.m_Uuid.AsString(),
        item.GetNetname(),
        (int(item.GetStart().x), int(item.GetStart().y)),
        (int(item.GetEnd().x), int(item.GetEnd().y)),
        int(item.GetWidth()),
        int(item.GetLayer()),
        bool(item.IsLocked()),
    )


def _poly_set_snapshot(poly_set: Any) -> tuple[Any, ...]:
    outlines = []
    for oi in range(poly_set.OutlineCount()):
        outer = poly_set.Outline(oi)
        outer_pts = tuple(
            (int(outer.CPoint(i).x), int(outer.CPoint(i).y))
            for i in range(outer.PointCount())
        )
        holes = []
        for hi in range(poly_set.HoleCount(oi)):
            hole = poly_set.Hole(oi, hi)
            holes.append(
                tuple(
                    (int(hole.CPoint(i).x), int(hole.CPoint(i).y))
                    for i in range(hole.PointCount())
                )
            )
        outlines.append((outer_pts, tuple(holes)))
    return tuple(outlines)


def _drawing_snapshot(d: Any) -> tuple[Any, ...]:
    poly_snap = (
        _poly_set_snapshot(d.GetPolyShape())
        if hasattr(d, "GetPolyShape")
        else None
    )
    return (
        d.GetClass(),
        d.m_Uuid.AsString(),
        int(d.GetLayer()),
        bool(d.IsLocked()) if hasattr(d, "IsLocked") else None,
        int(d.GetShape()) if hasattr(d, "GetShape") else None,
        (int(d.GetStart().x), int(d.GetStart().y)) if hasattr(d, "GetStart") else None,
        (int(d.GetArcMid().x), int(d.GetArcMid().y)) if hasattr(d, "GetArcMid") else None,
        (int(d.GetEnd().x), int(d.GetEnd().y)) if hasattr(d, "GetEnd") else None,
        (int(d.GetCenter().x), int(d.GetCenter().y)) if hasattr(d, "GetCenter") else None,
        round(float(d.GetArcAngle().AsDegrees()), 6) if hasattr(d, "GetArcAngle") else None,
        (int(d.GetBezierC1().x), int(d.GetBezierC1().y)) if hasattr(d, "GetBezierC1") else None,
        (int(d.GetBezierC2().x), int(d.GetBezierC2().y)) if hasattr(d, "GetBezierC2") else None,
        int(d.GetWidth()) if hasattr(d, "GetWidth") else None,
        d.GetText() if hasattr(d, "GetText") else None,
        poly_snap,
    )


def _zone_snapshot(z: Any) -> tuple[Any, ...]:
    return (
        z.m_Uuid.AsString(),
        bool(z.GetIsRuleArea()),
        bool(z.GetDoNotAllowTracks()),
        bool(z.GetDoNotAllowVias()),
        bool(z.GetDoNotAllowZoneFills()),
        bool(z.GetDoNotAllowPads()),
        bool(z.GetDoNotAllowFootprints()),
        tuple(sorted(int(lid) for lid in z.GetLayerSet().Seq())),
        _poly_set_snapshot(z.Outline()),
        z.GetNetname(),
        int(z.GetAssignedPriority()),
        int(z.GetLocalClearance()),
        int(z.GetMinThickness()),
        int(z.GetPadConnection()),
        bool(z.IsLocked()),
    )


def _collect_tracks_checked(board: Any, label: str) -> dict[str, tuple[Any, ...]]:
    out: dict[str, tuple[Any, ...]] = {}
    for t in board.GetTracks():
        tid = t.m_Uuid.AsString()
        if tid in out:
            raise AssertionError(
                f"duplicate {t.GetClass()} UUID in {label} board: {tid}"
            )
        out[tid] = _track_snapshot(t)
    return out


def verify_preservation(
    before_pcb: Path,
    after_pcb: Path,
    *,
    allow_removed_track_ids: tuple[str, ...] | set[str] = (),
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    if before_pcb.resolve() == after_pcb.resolve():
        raise AssertionError("output board path must differ from input board path")
    pn = _pcbnew()
    b_before = pn.LoadBoard(str(before_pcb))
    b_after = pn.LoadBoard(str(after_pcb))
    if b_before is None or b_after is None:
        raise AssertionError("failed to load before/after board in pcbnew")

    fps_before = sorted(
        (
            fp.m_Uuid.AsString(),
            fp.GetReference(),
            int(fp.GetPosition().x),
            int(fp.GetPosition().y),
            tuple(sorted(_drawing_snapshot(g) for g in fp.GraphicalItems())),
            tuple(sorted(_zone_snapshot(z) for z in fp.Zones() if not z.IsTeardropArea())),
        )
        for fp in b_before.GetFootprints()
    )
    fps_after = sorted(
        (
            fp.m_Uuid.AsString(),
            fp.GetReference(),
            int(fp.GetPosition().x),
            int(fp.GetPosition().y),
            tuple(sorted(_drawing_snapshot(g) for g in fp.GraphicalItems())),
            tuple(sorted(_zone_snapshot(z) for z in fp.Zones() if not z.IsTeardropArea())),
        )
        for fp in b_after.GetFootprints()
    )
    if fps_before != fps_after:
        raise AssertionError(f"footprints altered:\nbefore={fps_before}\nafter={fps_after}")

    pads_before_list = [_pad_snapshot(p) for p in b_before.GetPads()]
    pads_after_list = [_pad_snapshot(p) for p in b_after.GetPads()]
    if len({p[0] for p in pads_before_list}) != len(pads_before_list):
        raise AssertionError("duplicate pad UUID in before board")
    if len({p[0] for p in pads_after_list}) != len(pads_after_list):
        raise AssertionError("duplicate pad UUID in after board")
    pads_before = sorted(pads_before_list)
    pads_after = sorted(pads_after_list)
    if pads_before != pads_after:
        raise AssertionError(f"pads altered:\nbefore={pads_before}\nafter={pads_after}")

    drw_before = sorted(_drawing_snapshot(d) for d in b_before.GetDrawings())
    drw_after = sorted(_drawing_snapshot(d) for d in b_after.GetDrawings())
    if drw_before != drw_after:
        raise AssertionError(f"board drawings/outline altered:\nbefore={drw_before}\nafter={drw_after}")

    zones_before = sorted(
        _zone_snapshot(z) for z in b_before.Zones() if not z.IsTeardropArea()
    )
    zones_after = sorted(
        _zone_snapshot(z) for z in b_after.Zones() if not z.IsTeardropArea()
    )
    if zones_before != zones_after:
        raise AssertionError(f"board zones/keepouts altered:\nbefore={zones_before}\nafter={zones_after}")

    before_tracks = _collect_tracks_checked(b_before, "before")
    after_tracks = _collect_tracks_checked(b_after, "after")
    allowed_rm = set(allow_removed_track_ids)

    for tid, snap in before_tracks.items():
        locked = snap[-1]
        if tid not in after_tracks:
            if locked or tid not in allowed_rm:
                raise AssertionError(f"input copper item removed unexpectedly: {snap}")
        elif after_tracks[tid] != snap:
            raise AssertionError(f"input copper item mutated: {snap} -> {after_tracks[tid]}")

    added = [snap for tid, snap in after_tracks.items() if tid not in before_tracks]
    return list(before_tracks.values()), added


def verify_routed_board(
    before_pcb: Path,
    after_pcb: Path,
    *,
    expected_opens: int = 0,
    refill_zones: bool = False,
    allow_removed_track_ids: tuple[str, ...] | set[str] = (),
    require_checks: tuple[str, ...] = (),
    verify_dru: bool = True,
) -> dict[str, Any]:
    _, added = verify_preservation(
        before_pcb, after_pcb, allow_removed_track_ids=allow_removed_track_ids
    )
    pn = _pcbnew()
    b_after = pn.LoadBoard(str(after_pcb))
    min_width = int(b_after.GetDesignSettings().m_TrackMinWidth)
    seen_uuids: set[str] = set()
    for snap in added:
        cls, tid, net_name = snap[0], snap[1], snap[2]
        if not is_canonical_non_nil_uuid(tid) or tid in seen_uuids:
            raise AssertionError(f"invalid or duplicate added copper UUID: {snap}")
        seen_uuids.add(tid)
        if not net_name:
            raise AssertionError(f"added copper on net 0: {snap}")
        width = snap[4] if cls == "PCB_VIA" else (snap[6] if cls == "PCB_ARC" else snap[5])
        if width < min_width:
            raise AssertionError(f"added copper width {width} < min_track_width {min_width}: {snap}")

    conn = read_native_connectivity(after_pcb)
    if conn.unconnected_count != expected_opens:
        raise AssertionError(
            f"expected {expected_opens} native opens, got {conn.unconnected_count}"
        )
    if expected_opens == 0:
        for net_name, comps in conn.pad_partitions_by_net.items():
            if len(comps) > 1:
                raise AssertionError(
                    f"net {net_name!r} has {len(comps)} disconnected pad components: {comps}"
                )

    before_drc = run_native_drc(
        before_pcb,
        refill_zones=refill_zones,
        require_checks=require_checks,
        verify_dru=verify_dru,
    )
    after_drc = run_native_drc(
        after_pcb,
        refill_zones=refill_zones,
        require_checks=require_checks,
        sidecar_source=before_pcb,
        verify_dru=verify_dru,
    )
    if len(after_drc.unconnected_items) != expected_opens:
        raise AssertionError(
            f"DRC unconnected_items={len(after_drc.unconnected_items)} != {expected_opens}"
        )
    introduced, resolved = diff_drc_violations(before_drc, after_drc)
    if introduced:
        raise AssertionError(f"routed board introduced native DRC violations: {introduced}")
    return {
        "added_count": len(added),
        "unconnected_count": conn.unconnected_count,
        "before_violations": len(before_drc.violations),
        "after_violations": len(after_drc.violations),
        "resolved_violations": len(resolved),
    }
