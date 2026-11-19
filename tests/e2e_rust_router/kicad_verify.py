"""Programmatic pcbnew 10.0.8 fixture builder and isolated native KiCad verifier."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

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
CRITICAL_PHYSICAL_DRC_CHECKS: tuple[str, ...] = (
    "shorting_items",
    "clearance",
    "copper_edge_clearance",
    "copper_sliver",
    "hole_clearance",
    "holes_co_located",
    "hole_to_hole",
    "track_width",
    "via_diameter",
    "microvia_drill_out_of_range",
    "annular_width",
    "drill_out_of_range",
    "tracks_crossing",
    "zones_intersect",
    "items_not_allowed",
    "isolated_copper",
    "starved_thermal",
    "unconnected_items",
    "connection_width",
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
        min_resolved_spokes: int | None = None,
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
        if min_resolved_spokes is not None:
            ds.m_MinResolvedSpokes = min_resolved_spokes
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

    def add_zone(
        self,
        net_name: str,
        layers: list[int],
        outline_pts_nm: list[tuple[int, int]],
        *,
        priority: int = 0,
        clearance_nm: int = 250_000,
        min_thickness_nm: int = 250_000,
        pad_connection: int | None = None,
        thermal_gap_nm: int = 500_000,
        thermal_spoke_nm: int = 500_000,
        island_removal: int = 0,
        fill_mode: int = 0,
        zone_name: str = "",
        locked: bool = False,
        hole_pts_nm: list[list[tuple[int, int]]] | None = None,
    ) -> Any:
        pn = self.pn
        zone = pn.ZONE(self.board)
        lset = pn.LSET()
        for lid in layers:
            lset.AddLayer(lid)
        zone.SetLayerSet(lset)
        zone.SetIsRuleArea(False)
        zone.SetNetCode(self.ensure_net(net_name))
        zone.SetAssignedPriority(priority)
        zone.SetLocalClearance(clearance_nm)
        zone.SetMinThickness(min_thickness_nm)
        zone.SetPadConnection(
            pn.ZONE_CONNECTION_THERMAL if pad_connection is None else pad_connection
        )
        zone.SetThermalReliefGap(thermal_gap_nm)
        zone.SetThermalReliefSpokeWidth(thermal_spoke_nm)
        zone.SetIslandRemovalMode(island_removal)
        zone.SetFillMode(fill_mode)
        if zone_name:
            zone.SetZoneName(zone_name)
        zone.SetLocked(locked)
        zone.Outline().NewOutline()
        for x, y in outline_pts_nm:
            zone.Outline().Append(x, y)
        if hole_pts_nm:
            for hole in hole_pts_nm:
                hi = zone.Outline().NewHole(0)
                for x, y in hole:
                    zone.Outline().Append(x, y, 0, hi)
        self.board.Add(zone)
        return zone

    def set_project_rules(
        self,
        rules: dict[str, Any] | None = None,
        *,
        classes: list[dict[str, Any]] | None = None,
        netclass_patterns: list[dict[str, str]] | None = None,
        rule_severities: dict[str, str] | None = None,
        tuning_profiles: dict[str, Any] | None = None,
        use_height_for_length_calcs: bool | None = None,
    ) -> BoardBuilder:
        default_rules: dict[str, Any] = {
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
        if use_height_for_length_calcs is not None:
            default_rules["use_height_for_length_calcs"] = use_height_for_length_calcs
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
        if tuning_profiles is not None:
            self._pro_data["tuning_profiles"] = tuning_profiles
        return self

    def set_stackup_sexpr(self, stackup_block: str) -> BoardBuilder:
        self._stackup_sexpr = stackup_block
        return self

    def set_dru_text(self, text: str) -> BoardBuilder:
        self._dru_text = text
        return self

    def save(self, directory: Path, name: str = "board") -> FixtureBundle:
        directory.mkdir(parents=True, exist_ok=True)
        pcb_path = directory / f"{name}.kicad_pcb"
        if not self.pn.SaveBoard(str(pcb_path), self.board, True):
            raise RuntimeError(f"pcbnew.SaveBoard failed for {pcb_path}")
        if self._stackup_sexpr is not None:
            text = pcb_path.read_text(encoding="utf-8")
            marker = "\t(setup\n"
            if marker not in text:
                raise RuntimeError(f"could not locate (setup block in saved board {pcb_path}")
            indented = "\n".join(
                ("\t\t" + ln if ln.strip() else ln)
                for ln in self._stackup_sexpr.strip("\n").splitlines()
            )
            text = text.replace(marker, f"{marker}{indented}\n", 1)
            pcb_path.write_text(text, encoding="utf-8")
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
        if sidecar_dir is not None and sidecar_stem is not None:
            sidecar_file = sidecar_dir / f"{sidecar_stem}{suffix}"
            if (
                suffix in (".kicad_pro", ".kicad_dru")
                and cand.is_file()
                and cand.resolve() != sidecar_file.resolve()
                and (
                    not sidecar_file.is_file()
                    or cand.read_bytes() != sidecar_file.read_bytes()
                )
            ):
                raise AssertionError(
                    f"output board sidecar {cand} conflicts with authoritative {sidecar_file}"
                )
            if sidecar_file.is_file():
                shutil.copy2(sidecar_file, stage_dir / f"{stem}{suffix}")
                continue
        if cand.is_file():
            shutil.copy2(cand, stage_dir / cand.name)

    for table in ("fp-lib-table", "sym-lib-table"):
        cand = src_dir / table
        sidecar_table = (sidecar_dir / table) if sidecar_dir is not None else None
        if sidecar_table is not None and sidecar_table.is_file():
            shutil.copy2(sidecar_table, stage_dir / table)
        elif cand.is_file():
            shutil.copy2(cand, stage_dir / table)

    for pretty in src_dir.glob("*.pretty"):
        if pretty.is_dir():
            shutil.copytree(pretty, stage_dir / pretty.name)
    src_lib = src_dir / "lib"
    if src_lib.is_dir():
        shutil.copytree(src_lib, stage_dir / "lib")
    if sidecar_dir is not None:
        for pretty in sidecar_dir.glob("*.pretty"):
            dest = stage_dir / pretty.name
            if pretty.is_dir() and not dest.exists():
                shutil.copytree(pretty, dest)
        sidecar_lib = sidecar_dir / "lib"
        dest_lib = stage_dir / "lib"
        if sidecar_lib.is_dir() and not dest_lib.exists():
            shutil.copytree(sidecar_lib, dest_lib)
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
    save_board_out: Path | None = None,
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
        if save_board_out is not None:
            cmd.append("--save-board")
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
        required_checks = set(CRITICAL_PHYSICAL_DRC_CHECKS) | set(require_checks)
        missing_req = [k for k in sorted(required_checks) if k in ignored_keys]
        if missing_req:
            raise AssertionError(f"required DRC checks were ignored: {missing_req}")
        if verify_dru and has_dru:
            verify_dru_loaded(staged_pcb, timeout_s=timeout_s)
        if save_board_out is not None:
            save_board_out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged_pcb, save_board_out)
        return DrcReport(
            raw=data,
            violations=data["violations"],
            unconnected_items=data["unconnected_items"],
            ignored_checks=data["ignored_checks"],
            schematic_parity=data["schematic_parity"],
            console=console,
        )


def run_native_drc_refilled(
    board_path: Path,
    *,
    refilled_out: Path | None = None,
    check_parity: bool = False,
    require_checks: tuple[str, ...] = (),
    timeout_s: float = 60.0,
    sidecar_source: Path | None = None,
    verify_dru: bool = True,
) -> DrcReport:
    return run_native_drc(
        board_path,
        refill_zones=True,
        check_parity=check_parity,
        require_checks=require_checks,
        timeout_s=timeout_s,
        sidecar_source=sidecar_source,
        verify_dru=verify_dru,
        save_board_out=refilled_out,
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
                "LOAD_WITNESS failed: .kicad_dru rules were not compiled/enforced by KiCad"
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
    check_pour_integrity: bool = False,
    pour_area_loss_pct: float | None = None,
    pour_min_spokes: int | None = None,
    check_partition_ratchet: bool = False,
    check_parity: bool = False,
) -> dict[str, Any]:
    _, added = verify_preservation(
        before_pcb, after_pcb, allow_removed_track_ids=allow_removed_track_ids
    )
    pn = _pcbnew()
    with tempfile.TemporaryDirectory(prefix="e2e-minw-") as tmp:
        staged_after = _stage_board_bundle(after_pcb, Path(tmp), sidecar_source=before_pcb)
        b_after = pn.LoadBoard(str(staged_after))
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

    if refill_zones:
        with tempfile.TemporaryDirectory(prefix="e2e-verify-") as tmp:
            td = Path(tmp)
            refilled_before = (
                td / "before-refilled.kicad_pcb" if check_partition_ratchet else None
            )
            refilled_after = td / "after-refilled.kicad_pcb"
            before_drc = run_native_drc(
                before_pcb,
                refill_zones=True,
                check_parity=check_parity,
                require_checks=require_checks,
                verify_dru=verify_dru,
                save_board_out=refilled_before,
            )
            after_drc = run_native_drc(
                after_pcb,
                refill_zones=True,
                check_parity=check_parity,
                require_checks=require_checks,
                sidecar_source=before_pcb,
                verify_dru=verify_dru,
                save_board_out=refilled_after,
            )
            conn = read_native_connectivity(refilled_after)
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
            if len(after_drc.unconnected_items) != expected_opens:
                raise AssertionError(
                    f"DRC unconnected_items={len(after_drc.unconnected_items)} != {expected_opens}"
                )
            introduced, resolved = diff_drc_violations(before_drc, after_drc)
            if introduced:
                raise AssertionError(f"routed board introduced native DRC violations: {introduced}")
            if check_partition_ratchet:
                assert refilled_before is not None
                verify_partition_ratchet(refilled_before, refilled_after)
    else:
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
            refill_zones=False,
            check_parity=check_parity,
            require_checks=require_checks,
            verify_dru=verify_dru,
        )
        after_drc = run_native_drc(
            after_pcb,
            refill_zones=False,
            check_parity=check_parity,
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
        if check_partition_ratchet:
            verify_partition_ratchet(before_pcb, after_pcb)
    pour_info: dict[str, Any] | None = None
    if check_pour_integrity:
        pour_rep = verify_pour_integrity(
            before_pcb,
            after_pcb,
            area_loss_pct=pour_area_loss_pct,
            min_spokes=pour_min_spokes,
            sidecar_source=before_pcb,
            check_parity=check_parity,
            verify_dru=verify_dru,
        )
        if not pour_rep.passed:
            raise AssertionError(f"pour_integrity failed: {pour_rep.failures}")
        pour_info = {"min_spokes": pour_rep.min_spokes, "layers": pour_rep.layers}
    return {
        "added_count": len(added),
        "unconnected_count": conn.unconnected_count,
        "before_violations": len(before_drc.violations),
        "after_violations": len(after_drc.violations),
        "resolved_violations": len(resolved),
        **({"pour_integrity": pour_info} if pour_info is not None else {}),
    }



class _DisjointSet:
    def __init__(self) -> None:
        self.parent: dict[Any, Any] = {}

    def find(self, a: Any) -> Any:
        self.parent.setdefault(a, a)
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if str(ra) > str(rb):
                ra, rb = rb, ra
            self.parent[rb] = ra


def _seg_dist2_nm(p: tuple[int, int], a: tuple[int, int], b: tuple[int, int]) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    cx, cy = ax + t * dx, ay + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2


def extract_pour_facts(refilled_pcb: Path) -> dict[str, Any]:
    """Extract native pour/spoke/island facts from a refilled .kicad_pcb via pcbnew."""
    pn = _pcbnew()
    nm = 1_000_000
    edge_band = 20_000
    cell = 2 * nm
    leaf_vertices = 256
    leaf_size = nm // 2

    board = pn.LoadBoard(str(refilled_pcb))
    if board is None:
        raise AssertionError(f"pcbnew could not load refilled board {refilled_pcb}")
    bds = board.GetDesignSettings()
    max_err = int(bds.m_MaxError)
    lname = board.GetLayerName
    conn_names = {
        pn.ZONE_CONNECTION_NONE: "none",
        pn.ZONE_CONNECTION_THERMAL: "thermal",
        pn.ZONE_CONNECTION_FULL: "solid",
        pn.ZONE_CONNECTION_THT_THERMAL: "tht_thermal",
    }

    def shape(item: Any, layer: int, clearance: int = 0, err: int | None = None) -> Any:
        s = pn.SHAPE_POLY_SET()
        item.TransformShapeToPolygon(
            s, layer, int(clearance), max_err, pn.ERROR_INSIDE if err is None else err
        )
        return s

    def overlap(a: Any, b: Any) -> float:
        if not a.BBox().Intersects(b.BBox()):
            return 0.0
        x = pn.SHAPE_POLY_SET(a)
        x.BooleanIntersection(b)
        return float(x.Area())

    def rect(x0: int, y0: int, x1: int, y1: int) -> Any:
        r = pn.SHAPE_POLY_SET()
        r.NewOutline()
        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            r.Append(int(x), int(y))
        return r

    def bounds(bb: Any) -> tuple[int, int, int, int]:
        return int(bb.GetLeft()), int(bb.GetTop()), int(bb.GetRight()), int(bb.GetBottom())

    def hits(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
        return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]

    def tile(poly: Any, bb: tuple[int, int, int, int] | None = None) -> list[tuple[tuple[int, int, int, int], Any]]:
        bb = bb or bounds(poly.BBox())
        if poly.TotalVertices() <= leaf_vertices or max(bb[2] - bb[0], bb[3] - bb[1]) <= leaf_size:
            return [(bounds(poly.BBox()), poly)]
        mx, my = (bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2
        out: list[tuple[tuple[int, int, int, int], Any]] = []
        for q in (
            (bb[0], bb[1], mx, my),
            (mx, bb[1], bb[2], my),
            (bb[0], my, mx, bb[3]),
            (mx, my, bb[2], bb[3]),
        ):
            part = pn.SHAPE_POLY_SET(poly)
            part.BooleanIntersection(rect(*q))
            if part.OutlineCount():
                out.extend(tile(part, q))
        return out

    def area_on(s: Any, tiles: list[tuple[tuple[int, int, int, int], Any]]) -> float:
        sb = bounds(s.BBox())
        return sum(overlap(s, piece) for tb, piece in tiles if hits(sb, tb))

    def mm(v: float) -> float:
        return round(v / nm, 4)

    def box(s: Any) -> list[float]:
        bb = s.BBox()
        return [mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom())]

    zones = [z for z in board.Zones() if not z.GetIsRuleArea() and z.GetNetCode() > 0]
    zone_nets = {z.GetNetname() for z in zones}

    pads: dict[str, Any] = {}
    for fp in board.GetFootprints():
        seen_nums: set[str] = set()
        for pad in fp.Pads():
            key = pad.GetNumber() or f"<unnumbered@{pad.GetPosition().x},{pad.GetPosition().y}>"
            while key in seen_nums:
                key += "'"
            seen_nums.add(key)
            if pad.GetNetname() in zone_nets:
                pads[f"{fp.GetReference()}.{key}"] = pad

    tracks: list[Any] = []
    vias: list[Any] = []
    for t in board.GetTracks():
        if t.GetNetname() not in zone_nets:
            continue
        if isinstance(t, pn.PCB_VIA):
            vias.append(t)
        else:
            tracks.append(t)

    def track_points(t: Any) -> list[tuple[int, int]]:
        pts = [(int(t.GetStart().x), int(t.GetStart().y))]
        if isinstance(t, pn.PCB_ARC):
            pts.append((int(t.GetMid().x), int(t.GetMid().y)))
        pts.append((int(t.GetEnd().x), int(t.GetEnd().y)))
        return pts

    uf = _DisjointSet()
    polygons: dict[str, dict[str, Any]] = {}
    filled_layers: set[tuple[str, int]] = set()
    for zi, z in enumerate(
        sorted(zones, key=lambda item: (item.GetNetname(), item.GetZoneName(), -item.GetAssignedPriority()))
    ):
        net = z.GetNetname()
        for layer in z.GetLayerSet().CuStack():
            if not z.HasFilledPolysForLayer(layer):
                continue
            fill = z.GetFilledPolysList(layer)
            filled_layers.add((net, layer))
            pieces = []
            for i in range(fill.OutlineCount()):
                s = pn.SHAPE_POLY_SET()
                s.AddOutline(fill.COutline(i))
                pieces.append(s)
            pieces.sort(key=lambda s: -s.Area())
            for i, s in enumerate(pieces):
                pid = f"{net}@{lname(layer)}/{z.GetZoneName() or 'zone' + str(zi)}#{i}"
                polygons[pid] = {
                    "net": net,
                    "layer": lname(layer),
                    "zone": z.GetZoneName(),
                    "area_mm2": round(s.Area() / nm / nm, 4),
                    "bbox_mm": box(s),
                    "pads": [],
                    "vias": 0,
                    "tracks": 0,
                    "_tiles": tile(s),
                    "_bounds": bounds(s.BBox()),
                    "_layer": layer,
                    "_zone_idx": zi,
                }
                uf.find(("poly", pid))

    def polys_on(net: str, layer: int) -> list[tuple[str, dict[str, Any]]]:
        return [(pid, p) for pid, p in polygons.items() if p["net"] == net and p["_layer"] == layer]

    def local_fill(net: str, layer: int, bb: tuple[int, int, int, int]) -> Any:
        out = pn.SHAPE_POLY_SET()
        for p in polygons.values():
            if p["net"] != net or p["_layer"] != layer or not hits(bb, p["_bounds"]):
                continue
            for tb, piece in p["_tiles"]:
                if hits(bb, tb):
                    out.BooleanAdd(piece)
        if out.OutlineCount():
            out.BooleanIntersection(rect(*bb))
        return out

    def in_fill(net: str, layer: int, pt: Any) -> bool:
        b = (int(pt.x), int(pt.y), int(pt.x), int(pt.y))
        return any(
            hits(b, tb) and piece.Contains(pt)
            for p in polygons.values()
            if p["net"] == net and p["_layer"] == layer and hits(b, p["_bounds"])
            for tb, piece in p["_tiles"]
        )

    ids = sorted(polygons)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            pa, pb = polygons[a], polygons[b]
            if (
                pa["net"] == pb["net"]
                and pa["_layer"] == pb["_layer"]
                and pa["_zone_idx"] != pb["_zone_idx"]
                and hits(pa["_bounds"], pb["_bounds"])
                and any(hits(ta, tb) and overlap(qa, qb) > 0 for ta, qa in pa["_tiles"] for tb, qb in pb["_tiles"])
            ):
                uf.union(("poly", a), ("poly", b))

    pad_shapes: dict[tuple[str, int], Any] = {}
    for key, pad in pads.items():
        uf.find(("pad", key))
        for layer in pad.GetLayerSet().CuStack():
            s = shape(pad, layer)
            pad_shapes[(key, layer)] = s
            for pid, p in polys_on(pad.GetNetname(), layer):
                if area_on(s, p["_tiles"]) > 0:
                    p["pads"].append(key)
                    uf.union(("pad", key), ("poly", pid))

    via_shapes: list[dict[int, Any]] = []
    via_fill: set[int] = set()
    for vi, v in enumerate(vias):
        uf.find(("via", vi))
        per_layer: dict[int, Any] = {}
        for layer in v.GetLayerSet().CuStack():
            if not v.FlashLayer(layer):
                continue
            s = shape(v, layer)
            per_layer[layer] = s
            for pid, p in polys_on(v.GetNetname(), layer):
                if area_on(s, p["_tiles"]) > 0:
                    p["vias"] += 1
                    via_fill.add(vi)
                    uf.union(("via", vi), ("poly", pid))
        via_shapes.append(per_layer)

    for vi, per_layer in enumerate(via_shapes):
        v_net = vias[vi].GetNetname()
        for layer, vs in per_layer.items():
            for (key, p_layer), ps in pad_shapes.items():
                if p_layer == layer and pads[key].GetNetname() == v_net and overlap(ps, vs) > 0:
                    uf.union(("via", vi), ("pad", key))

    grid: dict[tuple[str, int, tuple[int, int]], list[tuple[str, Any, Any]]] = {}

    def cells(x0: int, y0: int, x1: int, y1: int):
        for gx in range(int(x0 // cell), int(x1 // cell) + 1):
            for gy in range(int(y0 // cell), int(y1 // cell) + 1):
                yield gx, gy

    def insert(net: str, layer: int, bb: tuple[int, int, int, int], item: tuple[str, Any, Any]) -> None:
        for c in cells(*bb):
            grid.setdefault((net, layer, c), []).append(item)

    def near(net: str, layer: int, pt: tuple[int, int]) -> list[tuple[str, Any, Any]]:
        return grid.get((net, layer, (int(pt[0] // cell), int(pt[1] // cell))), [])

    for (key, layer), s in pad_shapes.items():
        bb = s.BBox()
        insert(pads[key].GetNetname(), layer, (bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()), ("pad", key, s))
    for vi, per_layer in enumerate(via_shapes):
        for layer, s in per_layer.items():
            bb = s.BBox()
            insert(vias[vi].GetNetname(), layer, (bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()), ("via", vi, s))
    track_data: list[tuple[int, str, list[tuple[int, int]], int]] = []
    for ti, t in enumerate(tracks):
        pts, w = track_points(t), int(t.GetWidth())
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        track_data.append((int(t.GetLayer()), t.GetNetname(), pts, w))
        insert(t.GetNetname(), int(t.GetLayer()), (min(xs) - w, min(ys) - w, max(xs) + w, max(ys) + w), ("track", ti, None))

    track_pads: dict[str, list[tuple[int, int]]] = {}
    for ti, (layer, net, pts, w) in enumerate(track_data):
        uf.find(("track", ti))
        s = shape(tracks[ti], layer)
        for pid, p in polys_on(net, layer):
            if area_on(s, p["_tiles"]) > 0:
                p["tracks"] += 1
                uf.union(("track", ti), ("poly", pid))
        for end in (pts[0], pts[-1]):
            seen_items: set[tuple[str, Any]] = set()
            for kind, ident, other in near(net, layer, end):
                if (kind, ident) in seen_items or (kind == "track" and ident == ti):
                    continue
                seen_items.add((kind, ident))
                if kind == "track":
                    _, _, opts, ow = track_data[ident]
                    tol2 = (max(w, ow) / 2.0) ** 2
                    hit = any(_seg_dist2_nm(end, q0, q1) <= tol2 for q0, q1 in zip(opts, opts[1:]))
                else:
                    hit = bool(other.Contains(pn.VECTOR2I(int(end[0]), int(end[1]))))
                if hit:
                    uf.union(("track", ti), (kind, ident))
                    if kind == "pad" and (ti, layer) not in track_pads.get(ident, []):
                        track_pads.setdefault(ident, []).append((ti, layer))

    comp_area: dict[str, dict[Any, float]] = {}
    for pid, p in polygons.items():
        root = uf.find(("poly", pid))
        comp_area.setdefault(p["net"], {}).setdefault(root, 0.0)
        comp_area[p["net"]][root] += p["area_mm2"]
    main = {net: max(c.items(), key=lambda kv: (kv[1], str(kv[0])))[0] for net, c in comp_area.items()}
    for pid, p in polygons.items():
        p["in_main"] = uf.find(("poly", pid)) == main.get(p["net"])

    pad_records: dict[str, dict[str, Any]] = {}
    for z in sorted(zones, key=lambda item: -item.GetAssignedPriority()):
        net = z.GetNetname()
        zone_conn = z.GetPadConnection()
        for layer in z.GetLayerSet().CuStack():
            for key, pad in pads.items():
                rec_key = f"{key}@{lname(layer)}"
                if pad.GetNetname() != net or (key, layer) not in pad_shapes or rec_key in pad_records:
                    continue
                fp = pad.GetParentFootprint()
                conn = pad.GetLocalZoneConnection()
                if conn == pn.ZONE_CONNECTION_INHERITED and fp is not None:
                    conn = fp.GetLocalZoneConnection()
                if conn == pn.ZONE_CONNECTION_INHERITED:
                    conn = zone_conn
                pth = pad.GetAttribute() == pn.PAD_ATTRIB_PTH
                if conn == pn.ZONE_CONNECTION_THT_THERMAL:
                    conn = pn.ZONE_CONNECTION_THERMAL if pth else pn.ZONE_CONNECTION_FULL
                gap = pad.GetLocalThermalGapOverride()
                gap_nm = int(gap) if gap is not None else int(z.GetThermalReliefGap())
                reach = shape(pad, layer, gap_nm)
                if overlap(reach, z.Outline()) <= 0:
                    continue
                ps = pad_shapes[(key, layer)]
                rb = bounds(reach.BBox())
                fill = local_fill(net, layer, rb) if (net, layer) in filled_layers else None
                touches = fill is not None and overlap(ps, fill) > 0
                spokes, coverage, solid, track_spokes = 0, 0.0, False, 0
                ring = None
                hit = None
                if fill is not None:
                    ring = shape(pad, layer, edge_band)
                    ring.BooleanSubtract(shape(pad, layer, -edge_band, pn.ERROR_OUTSIDE))
                    hit = pn.SHAPE_POLY_SET(ring)
                    hit.BooleanIntersection(fill)
                    spokes = int(hit.OutlineCount())
                    ring_area = ring.Area()
                    coverage = hit.Area() / ring_area if ring_area > 0 else 0.0
                    solid = any(hit.HoleCount(i) > 0 for i in range(hit.OutlineCount()))
                if ring is not None and hit is not None:
                    combined = pn.SHAPE_POLY_SET(hit)
                    for ti in {ti for ti, lyr in track_pads.get(key, []) if lyr == layer}:
                        ends = [pn.VECTOR2I(int(x), int(y)) for x, y in (track_data[ti][2][0], track_data[ti][2][-1])]
                        inside = [bool(ps.Contains(e)) for e in ends]
                        if inside.count(True) == 1 and in_fill(net, layer, ends[inside.index(False)]):
                            t_ring = pn.SHAPE_POLY_SET(ring)
                            t_ring.BooleanIntersection(shape(tracks[ti], layer))
                            if t_ring.OutlineCount() > 0:
                                combined.BooleanAdd(t_ring)
                    track_spokes = max(0, int(combined.OutlineCount()) - spokes)
                on_layer = [ti for ti, lyr in track_pads.get(key, []) if lyr == layer]
                via_spokes = sum(
                    1
                    for vi in via_fill
                    if vias[vi].GetNetname() == net
                    and layer in via_shapes[vi]
                    and overlap(via_shapes[vi][layer], ps) > 0
                )
                pos = pad.GetPosition()
                pad_records[rec_key] = {
                    "pad": key,
                    "net": net,
                    "layer": lname(layer),
                    "zone": z.GetZoneName(),
                    "position_mm": [mm(pos.x), mm(pos.y)],
                    "pth": pth,
                    "connection": conn_names.get(conn, str(conn)),
                    "thermal_gap_mm": mm(gap_nm),
                    "touches_fill": touches,
                    "spokes": spokes,
                    "ring_coverage": round(coverage, 3),
                    "solid": solid,
                    "polygons": sorted(pid for pid, p in polys_on(net, layer) if key in p["pads"]),
                    "in_main": uf.find(("pad", key)) == main.get(net),
                    "tracks_on_layer": len(on_layer),
                    "tracks_attached": len(track_pads.get(key, [])),
                    "track_spokes": track_spokes,
                    "via_spokes": via_spokes,
                }

    pad_reach: dict[str, dict[str, Any]] = {}
    for key, pad in pads.items():
        net = pad.GetNetname()
        pad_root = uf.find(("pad", key))
        touches_fill = any(key in p["pads"] for p in polygons.values() if p["net"] == net)
        in_fill_via = any(
            vi in via_fill and uf.find(("via", vi)) == pad_root
            for vi, v in enumerate(vias)
            if v.GetNetname() == net
        )
        in_fill_track = any(
            p["tracks"] > 0 and uf.find(("poly", pid)) == pad_root
            for pid, p in polygons.items()
            if p["net"] == net
        )
        pad_reach[key] = {
            "pad": key,
            "net": net,
            "in_main": pad_root == main.get(net),
            "touches_fill": touches_fill,
            "via_fill": in_fill_via,
            "in_any_fill": touches_fill or in_fill_via or in_fill_track,
        }

    layers: dict[str, dict[str, Any]] = {}
    for p in polygons.values():
        lk = f"{p['net']}@{p['layer']}"
        rec = layers.setdefault(
            lk,
            {"net": p["net"], "layer": p["layer"], "area_mm2": 0.0, "polygons": 0, "islands_off_main": 0},
        )
        rec["area_mm2"] = round(rec["area_mm2"] + p["area_mm2"], 4)
        rec["polygons"] += 1
        rec["islands_off_main"] += 0 if p["in_main"] else 1
    return {
        "schema": 1,
        "board": refilled_pcb.name,
        "min_resolved_spokes": int(bds.m_MinResolvedSpokes),
        "zones": sorted(
            (
                {
                    "name": z.GetZoneName(),
                    "net": z.GetNetname(),
                    "layers": sorted(lname(lid) for lid in z.GetLayerSet().CuStack()),
                    "connection": conn_names.get(z.GetPadConnection(), str(z.GetPadConnection())),
                }
                for z in zones
            ),
            key=lambda r: json.dumps(r, sort_keys=True),
        ),
        "layers": dict(sorted(layers.items())),
        "polygons": {
            pid: {k: v for k, v in p.items() if not k.startswith("_")} | {"pads": sorted(p["pads"])}
            for pid, p in sorted(polygons.items())
        },
        "pads": dict(sorted(pad_records.items())),
        "pad_reach": dict(sorted(pad_reach.items())),
    }


@dataclass(frozen=True)
class PourIntegrityReport:
    passed: bool
    failures: list[dict[str, Any]]
    informational: list[dict[str, Any]]
    layers: list[dict[str, Any]]
    pads: list[dict[str, Any]]
    min_spokes: int


def verify_pour_integrity(
    before_pcb: Path,
    after_pcb: Path,
    *,
    area_loss_pct: float | None = None,
    min_spokes: int | None = None,
    sidecar_source: Path | None = None,
    check_parity: bool = False,
    verify_dru: bool = True,
) -> PourIntegrityReport:
    with tempfile.TemporaryDirectory(prefix="e2e-pour-") as tmp:
        td = Path(tmp)
        ref_refilled = td / "before-refilled.kicad_pcb"
        cand_refilled = td / "after-refilled.kicad_pcb"
        run_native_drc_refilled(
            before_pcb,
            refilled_out=ref_refilled,
            check_parity=check_parity,
            verify_dru=verify_dru,
        )
        run_native_drc_refilled(
            after_pcb,
            refilled_out=cand_refilled,
            check_parity=check_parity,
            sidecar_source=sidecar_source or before_pcb,
            verify_dru=verify_dru,
        )
        ref = extract_pour_facts(ref_refilled)
        cand = extract_pour_facts(cand_refilled)

    board_min = int(ref.get("min_resolved_spokes") or 0)
    minimum = max(board_min, int(min_spokes or 0))
    failures: list[dict[str, Any]] = []
    info: list[dict[str, Any]] = []

    def eff_spokes(p: dict[str, Any]) -> float:
        return float("inf") if p["solid"] else float(p["spokes"] + p["track_spokes"])

    layers: list[dict[str, Any]] = []
    for lk in sorted(set(ref["layers"]) | set(cand["layers"])):
        r, c = ref["layers"].get(lk), cand["layers"].get(lk)
        ra, ca = (r or {}).get("area_mm2", 0.0), (c or {}).get("area_mm2", 0.0)
        pct = round(100.0 * (ca - ra) / ra, 3) if ra else None
        rec = {
            "net_layer": lk,
            "area_mm2": [ra, ca],
            "area_delta_pct": pct,
            "polygons": [(r or {}).get("polygons", 0), (c or {}).get("polygons", 0)],
            "islands_off_main": [(r or {}).get("islands_off_main", 0), (c or {}).get("islands_off_main", 0)],
        }
        layers.append(rec)
        if area_loss_pct is not None and pct is not None and -pct > area_loss_pct:
            failures.append(
                {
                    "kind": "area_loss",
                    "net_layer": lk,
                    "area_mm2": [ra, ca],
                    "delta_pct": pct,
                    "limit_pct": area_loss_pct,
                }
            )

    pads_out: list[dict[str, Any]] = []
    for key in sorted(set(ref["pads"]) | set(cand["pads"])):
        r, c = ref["pads"].get(key), cand["pads"].get(key)
        if r is None or c is None:
            if r is not None and r["connection"] != "none" and r["touches_fill"]:
                failures.append({"kind": "lost_pad_reach", "pad": key, "connection": r["connection"]})
            else:
                info.append({"kind": "pad_reach_changed", "pad": key, "reference": r is not None, "candidate": c is not None})
            continue
        if r["connection"] == "none":
            continue
        rec = {
            "pad": key,
            "connection": r["connection"],
            "touches_fill": [r["touches_fill"], c["touches_fill"]],
            "spokes": [r["spokes"], c["spokes"]],
            "solid": [r["solid"], c["solid"]],
            "track_spokes": [r["track_spokes"], c["track_spokes"]],
            "in_main": [r["in_main"], c["in_main"]],
        }
        pads_out.append(rec)
        if r["touches_fill"] and not c["touches_fill"]:
            failures.append({"kind": "lost_fill_contact", **rec})
            continue
        if r["in_main"] and not c["in_main"]:
            failures.append({"kind": "isolated_island", **rec})
            continue
        es_r, es_c = eff_spokes(r), eff_spokes(c)
        if r["connection"] == "thermal" and r["touches_fill"] and es_r >= minimum > es_c:
            failures.append({"kind": "spokes_below_minimum", **rec, "minimum": minimum})
        elif es_c < es_r or (r["solid"] and not c["solid"]):
            info.append({"kind": "spokes_reduced", **rec})

    flagged_pads = {f.get("pad") for f in failures if f.get("pad")}
    ref_reach = ref.get("pad_reach", {})
    cand_reach = cand.get("pad_reach", {})
    for key in sorted(set(ref_reach) | set(cand_reach)):
        if any(fp == key or fp.startswith(f"{key}@") for fp in flagged_pads):
            continue
        rr, cr = ref_reach.get(key), cand_reach.get(key)
        if rr is None or cr is None:
            if rr is not None and rr["in_any_fill"]:
                failures.append({"kind": "lost_pad_reach", "pad": key, "net": rr["net"]})
            continue
        if rr["in_any_fill"] and not cr["in_any_fill"]:
            failures.append({"kind": "lost_fill_contact", "pad": key, "net": rr["net"]})
        elif rr["in_main"] and not cr["in_main"]:
            failures.append(
                {"kind": "isolated_island", "pad": key, "net": rr["net"], "in_main": [True, False]}
            )
        elif cr["in_any_fill"] and not cr["in_main"] and not (rr["in_any_fill"] and not rr["in_main"]):
            failures.append(
                {"kind": "isolated_island", "pad": key, "net": cr["net"], "in_main": [rr["in_main"], False]}
            )

    return PourIntegrityReport(
        passed=not failures,
        failures=failures,
        informational=info,
        layers=layers,
        pads=pads_out,
        min_spokes=minimum,
    )


def find_split_partitions(before_pcb: Path, after_pcb: Path) -> list[dict[str, Any]]:
    """Return any pre-existing >=2-pad connected component that was split in after_pcb."""
    before_conn = read_native_connectivity(before_pcb)
    after_conn = read_native_connectivity(after_pcb)
    splits: list[dict[str, Any]] = []
    for net_name, comps_before in sorted(before_conn.pad_partitions_by_net.items()):
        comps_after = after_conn.pad_partitions_by_net.get(net_name, [])
        for comp in comps_before:
            if len(comp) < 2:
                continue
            if not any(comp <= after_comp for after_comp in comps_after):
                splits.append({"net": net_name, "pads": sorted(comp)})
    return splits


def verify_partition_ratchet(before_pcb: Path, after_pcb: Path) -> None:
    splits = find_split_partitions(before_pcb, after_pcb)
    if splits:
        raise AssertionError(f"pre-existing connected terminal partitions split: {splits}")


@dataclass(frozen=True)
class FromToGroupReport:
    passed: bool
    unit: str
    measurements: list[dict[str, Any]]
    skew_violations: list[dict[str, Any]]
    unconnected_items: list[dict[str, Any]]
    coverage_errors: list[str]


_FROMTO_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.]+$")
_ACTUAL_MEASURE_RE = re.compile(r"actual\s+(-?\d+(?:\.\d+)?)\s*(mm|ps)\b")


def verify_fromto_length_groups(
    board_path: Path,
    groups: list[list[str]],
    *,
    tolerance_mm: float | None = None,
    tolerance_ps: float | None = None,
    sidecar_source: Path | None = None,
    timeout_s: float = 60.0,
) -> FromToGroupReport:
    if (tolerance_mm is None) == (tolerance_ps is None):
        raise ValueError("specify exactly one of tolerance_mm or tolerance_ps")
    unit = "ps" if tolerance_ps is not None else "mm"
    tol = float(tolerance_ps if tolerance_ps is not None else tolerance_mm)

    pn = _pcbnew()
    board = pn.LoadBoard(str(board_path))
    if board is None:
        raise AssertionError(f"pcbnew could not load {board_path}")
    zone_nets = {z.GetNetname() for z in board.Zones() if not z.GetIsRuleArea() and z.GetNetCode() > 0}

    pads_by_net: dict[str, list[str]] = defaultdict(list)
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            if pad.GetNetCode() > 0:
                num = pad.GetNumber()
                if _FROMTO_TOKEN_RE.match(ref) and _FROMTO_TOKEN_RE.match(num):
                    pads_by_net[pad.GetNetname()].append(f"{ref}-{num}")

    measurements: list[dict[str, Any]] = []
    skew_violations: list[dict[str, Any]] = []
    unconnected_all: list[dict[str, Any]] = []
    coverage_errors: list[str] = []

    for g_idx, group in enumerate(groups):
        if len(group) < 2:
            raise ValueError(f"fromTo group {g_idx} needs at least 2 nets")
        clauses: list[str] = []
        for net_name in group:
            if net_name in zone_nets:
                coverage_errors.append(f"net {net_name!r} has zone copper")
                continue
            terms = pads_by_net.get(net_name, [])
            if len(terms) != 2:
                coverage_errors.append(f"net {net_name!r} has {len(terms)} valid fromTo terminals (expected 2)")
                continue
            clauses.append(f"A.fromTo('{terms[0]}', '{terms[1]}')")
        if len(clauses) != len(group):
            continue

        with tempfile.TemporaryDirectory(prefix=f"e2e-fromto-{g_idx}-") as tmp:
            td = Path(tmp)
            staged_pcb = _stage_board_bundle(board_path, td, sidecar_source=sidecar_source)
            pro_path = staged_pcb.with_suffix(".kicad_pro")
            pro_data = json.loads(pro_path.read_text(encoding="utf-8")) if pro_path.is_file() else {"meta": {"version": 3}}
            pro_data.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rule_severities", {})
            pro_data["board"]["design_settings"]["rule_severities"]["length_out_of_range"] = "error"
            pro_data["board"]["design_settings"]["rule_severities"]["skew_out_of_range"] = "error"
            pro_path.write_text(json.dumps(pro_data, indent=2) + "\n", encoding="utf-8")

            dru_path = staged_pcb.with_suffix(".kicad_dru")
            existing_dru = dru_path.read_text(encoding="utf-8") if dru_path.is_file() else "(version 1)\n"
            rule_name = f"_e2e_fromto_group_{g_idx}"
            cond = " || ".join(clauses)
            dru_rule = (
                f'\n(rule "{rule_name}"\n'
                f'  (condition "{cond}")\n'
                f"  (constraint length (max 0{unit}))\n"
                f"  (constraint skew (max {tol}{unit}))\n"
                f")\n"
            )
            dru_path.write_text(existing_dru.rstrip() + dru_rule, encoding="utf-8")

            drc = run_native_drc(
                staged_pcb,
                require_checks=("length_out_of_range", "skew_out_of_range"),
                timeout_s=timeout_s,
                verify_dru=False,
            )
            unconnected_all.extend(drc.unconnected_items)
            len_hits = [
                v
                for v in drc.violations
                if v.get("type") == "length_out_of_range" and rule_name in v.get("description", "")
            ]
            skew_hits = [
                v
                for v in drc.violations
                if v.get("type") == "skew_out_of_range" and rule_name in v.get("description", "")
            ]
            skew_violations.extend(skew_hits)
            if len(len_hits) != len(group):
                coverage_errors.append(
                    f"group {g_idx} expected {len(group)} fromTo length findings, got {len(len_hits)}"
                )
            for hit in len_hits:
                desc = str(hit.get("description", ""))
                m = _ACTUAL_MEASURE_RE.search(desc)
                measurements.append(
                    {
                        "group": g_idx,
                        "description": desc,
                        "actual": float(m.group(1)) if m else None,
                        "unit": m.group(2) if m else unit,
                    }
                )

    passed = not skew_violations and not unconnected_all and not coverage_errors
    return FromToGroupReport(
        passed=passed,
        unit=unit,
        measurements=measurements,
        skew_violations=skew_violations,
        unconnected_items=unconnected_all,
        coverage_errors=coverage_errors,
    )


def _net_planar_length_mm(board: Any, net_name: str) -> float:
    total_nm = 0.0
    for tr in board.GetTracks():
        if tr.GetNetname() != net_name:
            continue
        cls = tr.GetClass()
        if cls == "PCB_ARC":
            total_nm += float(tr.GetLength())
        elif cls == "PCB_TRACK":
            dx = float(tr.GetEnd().x - tr.GetStart().x)
            dy = float(tr.GetEnd().y - tr.GetStart().y)
            total_nm += math.hypot(dx, dy)
    return round(total_nm / 1_000_000.0, 6)


def verify_diff_pair_native(
    before_pcb: Path,
    after_pcb: Path,
    pairs: list[tuple[str, str]],
    *,
    check_drc_rules: bool = True,
    sidecar_source: Path | None = None,
    max_uncoupled_mm: float = 4.0,
) -> dict[str, Any]:
    pn = _pcbnew()
    board = pn.LoadBoard(str(after_pcb))
    if board is None:
        raise AssertionError(f"pcbnew could not load {after_pcb}")
    conn = read_native_connectivity(after_pcb)
    drc = None
    if check_drc_rules:
        with tempfile.TemporaryDirectory(prefix="e2e-diffpair-") as tmp:
            td = Path(tmp)
            staged_pcb = _stage_board_bundle(
                after_pcb, td, sidecar_source=(sidecar_source or before_pcb)
            )
            pro_path = staged_pcb.with_suffix(".kicad_pro")
            pro_data = (
                json.loads(pro_path.read_text(encoding="utf-8"))
                if pro_path.is_file()
                else {"meta": {"version": 3}}
            )
            pro_data.setdefault("board", {}).setdefault("design_settings", {}).setdefault(
                "rule_severities", {}
            )
            sevs = pro_data["board"]["design_settings"]["rule_severities"]
            sevs["diff_pair_gap_out_of_range"] = "error"
            sevs["diff_pair_uncoupled_length_too_long"] = "error"
            pro_path.write_text(json.dumps(pro_data, indent=2) + "\n", encoding="utf-8")

            dru_path = staged_pcb.with_suffix(".kicad_dru")
            existing_dru = dru_path.read_text(encoding="utf-8") if dru_path.is_file() else "(version 1)\n"
            if "diff_pair_uncoupled" not in existing_dru:
                existing_dru = (
                    existing_dru.rstrip()
                    + '\n(rule "_e2e_verify_diff_pair_uncoupled"\n'
                    + f"  (constraint diff_pair_uncoupled (max {max_uncoupled_mm:.3f}mm))\n"
                    + ")\n"
                )
                dru_path.write_text(existing_dru, encoding="utf-8")
            drc = run_native_drc(
                staged_pcb,
                require_checks=("diff_pair_gap_out_of_range", "diff_pair_uncoupled_length_too_long"),
                verify_dru=False,
            )
    pair_gap_errs = (
        [v for v in drc.violations if v.get("type") == "diff_pair_gap_out_of_range"]
        if drc is not None
        else []
    )
    uncoupled_errs = (
        [v for v in drc.violations if v.get("type") == "diff_pair_uncoupled_length_too_long"]
        if drc is not None
        else []
    )
    if pair_gap_errs:
        raise AssertionError(f"diff_pair_gap_out_of_range violations: {pair_gap_errs}")
    if uncoupled_errs:
        raise AssertionError(f"diff_pair_uncoupled_length_too_long violations: {uncoupled_errs}")

    pair_records: list[dict[str, Any]] = []
    for p_net, n_net in pairs:
        p_parts = conn.pad_partitions_by_net.get(p_net, [])
        n_parts = conn.pad_partitions_by_net.get(n_net, [])
        p_ok = len(p_parts) == 1
        n_ok = len(n_parts) == 1
        if p_ok != n_ok:
            raise AssertionError(
                f"single-ended partial fallback detected on diff pair ({p_net}, {n_net}): "
                f"p_parts={len(p_parts)}, n_parts={len(n_parts)}"
            )
        if not (p_ok and n_ok):
            raise AssertionError(
                f"diff pair ({p_net}, {n_net}) incomplete: p_parts={len(p_parts)}, n_parts={len(n_parts)}"
            )
        len_p = _net_planar_length_mm(board, p_net)
        len_n = _net_planar_length_mm(board, n_net)
        pair_records.append(
            {
                "p": p_net,
                "n": n_net,
                "length_p_mm": len_p,
                "length_n_mm": len_n,
                "skew_mm": round(abs(len_p - len_n), 6),
            }
        )
    return {
        "pairs": pair_records,
        "gap_violations": len(pair_gap_errs),
        "uncoupled_violations": len(uncoupled_errs),
    }


def copper_signature(pcb_path: Path) -> str:
    """Canonical sorted SHA-256 across tracks, vias, and arcs on a KiCad board."""
    pn = _pcbnew()
    board = pn.LoadBoard(str(pcb_path))
    if board is None:
        raise AssertionError(f"pcbnew could not load {pcb_path}")
    items: list[tuple[Any, ...]] = []
    for t in board.GetTracks():
        cls = t.GetClass()
        if cls == "PCB_VIA":
            items.append(
                (
                    "V",
                    t.GetNetname(),
                    [int(t.TopLayer()), int(t.BottomLayer())],
                    [int(t.GetPosition().x), int(t.GetPosition().y)],
                    int(t.GetWidth(t.TopLayer())),
                    int(t.GetDrill()),
                )
            )
        elif cls == "PCB_ARC":
            items.append(
                (
                    "A",
                    t.GetNetname(),
                    int(t.GetLayer()),
                    [
                        int(t.GetStart().x),
                        int(t.GetStart().y),
                        int(t.GetMid().x),
                        int(t.GetMid().y),
                        int(t.GetEnd().x),
                        int(t.GetEnd().y),
                    ],
                    int(t.GetWidth()),
                )
            )
        else:
            items.append(
                (
                    "T",
                    t.GetNetname(),
                    int(t.GetLayer()),
                    [
                        int(t.GetStart().x),
                        int(t.GetStart().y),
                        int(t.GetEnd().x),
                        int(t.GetEnd().y),
                    ],
                    int(t.GetWidth()),
                )
            )
    items.sort()
    return hashlib.sha256(json.dumps(items, separators=(",", ":")).encode("utf-8")).hexdigest()
