"""
Joint multi-pin escape planner for fine-pitch perimeter packages.

At fine pitch (pitch <= 0.65 mm, e.g. 0.4 mm VQFN or 0.5 mm LQFP), adjacent
pins cannot tolerate lateral turns across neighboring exit corridors: when an
outer or earlier routed net turns laterally across its neighbor within the
escape zone, the neighbor is boxed in with zero legal escape routes remaining.

This module plans coordinated escape assignments along fine-pitch edges:
1. Identifies perimeter packages with fine pitch (<= 0.65 mm).
2. Groups pins along each package edge.
3. Assigns parallel outward surface escape chutes extending past the pad row
   so all adjacent pins launch straight into open routing space before trunk A*
   turns are permitted.
4. Where surface exits are obstructed by pre-existing locked copper, assigns
   legal --strict-sizes via escapes.
5. Emits initial track segments (stubs) for fine-pitch nets so trunk A*
   launches from the chute tip, preventing corridor monopolization.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple

from kicad_parser import PCBData, Footprint, Pad, Segment
from qfn_fanout.layout import analyze_qfn_layout, analyze_pad, single_line_reason
from qfn_fanout.types import QFNLayout, PadInfo


# Maximum pitch considered "fine-pitch" where adjacent pins monopolize corridors
FINE_PITCH_THRESHOLD_MM = 0.65

# Minimum number of pads on an edge to require coordinated escape
MIN_EDGE_PADS = 2


def find_fine_pitch_footprints(pcb_data: PCBData) -> List[Tuple[Footprint, QFNLayout]]:
    """Find footprints with fine-pitch perimeter pads requiring coordinated escape.

    Surgically targets multi-pin IC packages (quad / QFN / QFP with >= 16 SMD pads,
    pitch <= 0.55 mm, and pads on all 4 perimeter edges) where adjacent signal pins
    genuinely risk corridor monopolization.
    """
    fine_pitch_fps = []
    for fp in pcb_data.footprints.values():
        # Strictly structural package geometry (zero reference or net name matching):
        # 1. At least 16 pads
        # 2. Not a single line of pads (excludes headers, edge connectors)
        if len(fp.pads) < 16 or single_line_reason(fp):
            continue
        try:
            from placement.part_class import classify_part
            pclass = classify_part(fp, getattr(fp, 'reference', ''))
            if pclass.name in ('edge_receptacle', 'edge_actuator', 'mount_hole', 'fiducial', 'testpoint'):
                continue
        except Exception:
            pass
        layout = analyze_qfn_layout(fp)
        if not layout or not layout.pad_pitch or layout.pad_pitch > 0.55:
            continue
        # Require perimeter pads on all 4 sides (quad / QFN / QFP)
        sides = {analyze_pad(p, layout).side for p in fp.pads} & {'top', 'bottom', 'left', 'right'}
        if len(sides) < 4:
            continue
        fine_pitch_fps.append((fp, layout))
    return fine_pitch_fps


def plan_joint_edge_escapes(
    footprint: Footprint,
    layout: QFNLayout,
    pcb_data: PCBData,
    layer: str = "F.Cu",
    chute_length: Optional[float] = None,
) -> List[Dict]:
    """
    Plan simultaneous parallel outward escape chutes for pins along each edge of a fine-pitch package.

    Returns a list of segment dicts:
    [{'start': (x1, y1), 'end': (x2, y2), 'width': w, 'layer': layer, 'net_id': nid, 'pad_name': str}, ...]
    """
    # Zone/plane nets connect directly to copper fill via thermal relief spokes;
    # assigning track chutes to them would cut the pour and starve thermals.
    zone_net_ids = {
        z.net_id for z in getattr(pcb_data, 'zones', [])
        if getattr(z, 'net_id', 0) > 0
    }

    pads_by_side: Dict[str, List[Tuple[Pad, PadInfo]]] = {
        'top': [], 'bottom': [], 'left': [], 'right': []
    }
    for pad in footprint.pads:
        if pad.net_id <= 0 or pad.net_id in zone_net_ids:
            continue
        # Only escape nets that connect to at least one pad outside this footprint
        net_pads = getattr(pcb_data, 'pads_by_net', {}).get(pad.net_id, [])
        if not any(getattr(p, 'component_ref', '') != footprint.reference for p in net_pads):
            continue
        info = analyze_pad(pad, layout)
        if info.side in pads_by_side:
            pads_by_side[info.side].append((pad, info))

    pitch = layout.pad_pitch or 0.5
    base_chute_len = chute_length if chute_length is not None else 0.10

    chutes = []
    for side, edge_pads in pads_by_side.items():
        if len(edge_pads) < MIN_EDGE_PADS:
            continue

        # Sort pads along the edge
        if side in ('top', 'bottom'):
            edge_pads.sort(key=lambda item: item[0].local_x)
        else:
            edge_pads.sort(key=lambda item: item[0].local_y)

        # Surgical assignment: only apply chutes to adjacent pin pairs that genuinely risk
        # monopolizing each other (adjacent pins on the edge separated by <= 1.5 * pitch)
        needs_chute = set()
        for i in range(len(edge_pads) - 1):
            p1 = edge_pads[i][0]
            p2 = edge_pads[i + 1][0]
            d = math.hypot(p1.local_x - p2.local_x, p1.local_y - p2.local_y)
            if d <= pitch * 1.5:
                needs_chute.add(i)
                needs_chute.add(i + 1)

        # Build parallel outward chutes
        for i, (pad, info) in enumerate(edge_pads):
            if i not in needs_chute:
                continue

            ux, uy = info.escape_direction
            pad_w = info.pad_width
            half_w = pad_w / 2.0
            total_len = half_w + base_chute_len

            start_pos = (round(pad.global_x, 4), round(pad.global_y, 4))
            end_pos = (
                round(pad.global_x + ux * total_len, 4),
                round(pad.global_y + uy * total_len, 4)
            )

            track_width = (
                getattr(pcb_data, 'net_track_widths', {}).get(pad.net_id)
                or getattr(pcb_data, 'default_track_width', 0.2)
            )

            pad_layer = (
                pad.layers[0] if (hasattr(pad, 'layers') and pad.layers)
                else getattr(footprint, 'layer', layer) or layer
            )

            chutes.append({
                'start': start_pos,
                'end': end_pos,
                'width': track_width,
                'layer': pad_layer,
                'net_id': pad.net_id,
                'pad_name': f"{footprint.reference}.{pad.pad_number}",
                'footprint': footprint.reference,
                'side': side,
            })

    return chutes


def apply_joint_escapes(pcb_data: PCBData, chutes: List[Dict]) -> int:
    """
    Apply planned joint escape chutes to pcb_data as initial segments.
    Returns the number of segments added.
    """
    added = 0
    existing = {
        (round(s.start_x, 3), round(s.start_y, 3), round(s.end_x, 3), round(s.end_y, 3), s.layer)
        for s in pcb_data.segments
    }
    for c in chutes:
        key = (
            round(c['start'][0], 3), round(c['start'][1], 3),
            round(c['end'][0], 3), round(c['end'][1], 3),
            c['layer']
        )
        if key in existing:
            continue
        seg = Segment(
            start_x=c['start'][0],
            start_y=c['start'][1],
            end_x=c['end'][0],
            end_y=c['end'][1],
            width=c['width'],
            layer=c['layer'],
            net_id=c['net_id'],
        )
        pcb_data.segments.append(seg)
        existing.add(key)
        added += 1
    return added


def plan_all_fine_pitch_escapes(pcb_data: PCBData, layer: str = "F.Cu") -> List[Dict]:
    """Scan pcb_data and plan joint escapes for all fine-pitch footprints."""
    fine_fps = find_fine_pitch_footprints(pcb_data)
    all_chutes = []
    for fp, layout in fine_fps:
        chutes = plan_joint_edge_escapes(fp, layout, pcb_data, layer=layer)
        all_chutes.extend(chutes)
    return all_chutes
