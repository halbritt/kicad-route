#!/usr/bin/env python3
"""Thermal-spoke corridors: plan them, reserve them as rule areas for routing, strip them.

    spokes.py plan BOARD.kicad_pcb OUT.json [--per-pad N]   corridors as JSON (board untouched)
    spokes.py reserve BOARD.kicad_pcb PLAN.json              add the selected corridors as rule areas (in place)
    spokes.py strip BOARD.kicad_pcb                          remove every kicad-route:spoke: rule area (in place)

A pad connected to a same-net pour by thermal relief reaches the pour only through
its spokes. A new track of another net that passes close to the pad pushes the pour
back by its clearance, so a spoke can end in nothing and KiCad drops it; below the
zone's min_resolved_spokes DRC reports `starved_thermal`, after routing. A corridor
is the region one spoke needs to stay connected: from the pad edge across the
relief gap into the pour, long enough for the pour to keep min_thickness beyond the
spoke end, and wide enough that the pour beside the spoke survives a track at the
corridor edge:

    length     = gap + min_thickness + clearance + max_error      (from the pad edge)
    half-width = spoke_width / 2 + clearance

clearance is the largest that any other net can claim against the zone: the zone's
own clearance, every net class clearance, the board minimum, and every `clearance`
minimum in the project's .kicad_dru (conservative: it never under-reserves for a
custom rule).

plan: zones are refilled in memory (never saved). For each pad on a copper layer of
a same-net zone whose outline contains the pad and whose resolved connection is
thermal relief (pad, then footprint, then zone; THT-thermal means PTH only), the
four spoke directions follow KiCad's zone filler: pad orientation + the pad's spoke
angle + k*90 degrees, spokes reaching the pad's extent along that direction (its
support function: the rotated bounding box KiCad builds them in) plus gap. Gap and
spoke width come from the pad's overrides, else the zone (custom
thermal_relief_gap / thermal_spoke_width / zone_connection rules in .kicad_dru are
NOT evaluated: they are listed under `warnings`). A spoke is `live` when the refilled
pour covers KiCad's own test point (spoke end); a corridor is `eligible` when it is
live and overlaps no existing track, via or other pad on its layer. The best
`per_pad` eligible corridors are `selected` (default: the board's
min_resolved_spokes, normally 2), most pour beyond the gap first. Emits JSON only.

reserve: each selected corridor becomes a single-layer rule area that forbids tracks
and vias (pads, footprints and zone fills allowed), named
`kicad-route:spoke:<ref>:<pad>:<layer>:<k>`. Refuses a board that already has
such areas. strip: removes exactly those areas (by name prefix) and saves only if it
removed something. Both print `SPOKES_JSON: {...}`. Runs with KiCad's pcbnew module.
All lengths are integer nanometres; angles degrees.
"""
import json
import math
import re
import sys
from pathlib import Path

PREFIX = "kicad-route:spoke:"
UNIT_MM = {"mm": 1.0, "mil": 0.0254, "in": 25.4, "um": 0.001, None: 1.0}
DRU_CLEARANCE = re.compile(r"\(constraint\s+clearance\b[^()]*\(min\s+([0-9.]+)\s*(mm|mil|in|um)?\s*\)")
DRU_THERMAL = re.compile(r"\(constraint\s+(thermal_relief_gap|thermal_spoke_width|zone_connection|min_resolved_spokes)\b")


def poly_points(ps):
    """Outer outlines of a SHAPE_POLY_SET as [[(x, y), ...], ...]."""
    out = []
    for i in range(ps.OutlineCount()):
        chain = ps.COutline(i)
        out.append([(chain.CPoint(j).x, chain.CPoint(j).y) for j in range(chain.PointCount())])
    return out


def rect(center, d, start, end, half):
    """Corners of the rectangle from start to end along unit vector d, +/- half across it."""
    cx, cy = center
    ux, uy = d
    nx, ny = -uy, ux
    return [(round(cx + ux * s + nx * h), round(cy + uy * s + ny * h))
            for s, h in ((start, -half), (end, -half), (end, half), (start, half))]


def to_polyset(pcbnew, pts):
    ps = pcbnew.SHAPE_POLY_SET()
    ps.NewOutline()
    for x, y in pts:
        ps.Append(int(x), int(y))
    return ps


def bbox(pts):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def boxes_touch(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def ray_exit(outlines, center, d):
    """Farthest distance at which the ray from center along d crosses the pad outline."""
    cx, cy = center
    best = 0.0
    for pts in outlines:
        for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
            ex, ey = x2 - x1, y2 - y1
            den = d[0] * ey - d[1] * ex
            if abs(den) < 1e-12:
                continue
            t = ((x1 - cx) * ey - (y1 - cy) * ex) / den
            u = ((x1 - cx) * d[1] - (y1 - cy) * d[0]) / den
            if t > 0 and -1e-9 <= u <= 1 + 1e-9:
                best = max(best, t)
    return best


def dru_scan(board_path):
    dru = Path(board_path).with_suffix(".kicad_dru")
    if not dru.exists():
        return 0, []
    text = re.sub(r"(?m)^\s*#.*$", "", dru.read_text())
    mins = [float(v) * UNIT_MM[u or None] for v, u in DRU_CLEARANCE.findall(text)]
    kinds = sorted(set(DRU_THERMAL.findall(text)))
    return round(max(mins, default=0) * 1_000_000), kinds


def plan(pcbnew, board_path, per_pad=None):
    board = pcbnew.LoadBoard(str(board_path))
    ds = board.GetDesignSettings()
    max_err = int(ds.m_MaxError)
    min_spokes = int(getattr(ds, "m_MinResolvedSpokes", 0) or 2)
    per_pad = per_pad or min_spokes
    warnings = []
    zones = [z for z in board.Zones() if not z.GetIsRuleArea() and z.GetNetCode() > 0]
    if any(z.GetZoneName().startswith(PREFIX) for z in board.Zones()):
        warnings.append("board already carries kicad-route:spoke: rule areas")
    filled = pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    if not filled:
        warnings.append("in-memory zone refill failed; the fills stored in the file were used")

    # The largest clearance any foreign net can claim against a pour.
    net_settings = ds.m_NetSettings
    class_clear = [int(net_settings.GetEffectiveNetClass(str(n)).GetClearance())
                   for n in board.GetNetsByName().keys() if str(n)]
    dru_clear, dru_thermal = dru_scan(board_path)
    if dru_thermal:
        warnings.append(".kicad_dru has custom " + ", ".join(dru_thermal) + " constraints: corridors use the "
                        "pad/zone values, not the custom rules")
    base_clear = max([int(ds.m_MinClearance), dru_clear] + class_clear)

    # Existing copper per layer (bbox, polyset, owner) for the eligibility test.
    copper = {}

    def add_copper(layer, item, owner):
        ps = pcbnew.SHAPE_POLY_SET()
        item.TransformShapeToPolygon(ps, layer, 0, max_err, pcbnew.ERROR_INSIDE)
        pts = [p for o in poly_points(ps) for p in o]
        if pts:
            copper.setdefault(layer, []).append((bbox(pts), ps, owner))

    pads = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            pads.append((fp, pad))
            for layer in pad.GetLayerSet().CuStack():
                add_copper(layer, pad, ("pad", pad.m_Uuid.AsString()))
    for t in board.GetTracks():
        for layer in t.GetLayerSet().CuStack():
            add_copper(layer, t, ("via" if isinstance(t, pcbnew.PCB_VIA) else "track", t.m_Uuid.AsString()))

    def overlaps(layer, pts, own_uuid):
        box = bbox(pts)
        cps = None
        hits = []
        for b, ps, owner in copper.get(layer, []):
            if owner[1] == own_uuid or not boxes_touch(box, b):
                continue
            cps = cps or to_polyset(pcbnew, pts)
            x = pcbnew.SHAPE_POLY_SET(cps)
            x.BooleanIntersection(ps)
            if x.OutlineCount() and x.Area() > 0:
                hits.append(owner)
        return hits

    out_pads, skipped, seen = [], [], set()
    for zone in sorted(zones, key=lambda z: -int(z.GetAssignedPriority())):
        clearance = max(base_clear, int(zone.GetLocalClearance() or 0))
        min_thick = int(zone.GetMinThickness())
        for layer in zone.GetLayerSet().CuStack():
            fill = zone.GetFilledPolysList(layer)
            for fp, pad in pads:
                if pad.GetNetCode() != zone.GetNetCode() or not pad.IsOnLayer(layer):
                    continue
                if pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                    continue
                center = pad.ShapePos(layer)
                if not zone.Outline().Contains(center):
                    continue
                key = (pad.m_Uuid.AsString(), layer)
                if key in seen:
                    continue
                seen.add(key)
                rec = {"ref": fp.GetReference(), "pad": pad.GetNumber(), "uuid": pad.m_Uuid.AsString(),
                       "net": pad.GetNetname(), "layer": board.GetLayerName(layer), "zone": zone.GetZoneName(),
                       "position": [int(center.x), int(center.y)]}
                conn = pad.GetLocalZoneConnection()
                source = "pad"
                if conn == pcbnew.ZONE_CONNECTION_INHERITED:
                    conn, source = fp.GetLocalZoneConnection(), "footprint"
                if conn == pcbnew.ZONE_CONNECTION_INHERITED:
                    conn, source = zone.GetPadConnection(), "zone"
                if conn == pcbnew.ZONE_CONNECTION_THT_THERMAL:
                    conn = (pcbnew.ZONE_CONNECTION_THERMAL if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH
                            else pcbnew.ZONE_CONNECTION_FULL)
                if conn != pcbnew.ZONE_CONNECTION_THERMAL:
                    continue  # solid or no connection: no spokes to protect
                gap = pad.GetLocalThermalGapOverride()
                gap = int(gap) if gap is not None else int(zone.GetThermalReliefGap())
                spoke_w = pad.GetLocalThermalSpokeWidthOverride()
                spoke_w = int(spoke_w) if spoke_w is not None else int(zone.GetThermalReliefSpokeWidth())
                size = pad.GetSize(layer)
                spoke_w = min(spoke_w, int(size.x), int(size.y))
                rec.update(connection_from=source, gap=gap, spoke_width=spoke_w, clearance=clearance,
                           min_thickness=min_thick, spoke_angle=round(pad.GetThermalSpokeAngleDegrees(), 6),
                           orientation=round(pad.GetOrientationDegrees(), 6))
                if spoke_w < min_thick:
                    skipped.append(dict(rec, reason="spoke width below the zone min thickness: KiCad builds no spokes"))
                    continue
                shape = pad.GetShape(layer)
                if shape == pcbnew.PAD_SHAPE_CUSTOM:
                    rec["note"] = "custom pad shape: spokes modelled from its extent, not its spoke template"
                ps = pcbnew.SHAPE_POLY_SET()
                pad.TransformShapeToPolygon(ps, layer, 0, max_err, pcbnew.ERROR_INSIDE)
                outlines = poly_points(ps)
                c = (center.x, center.y)
                length = gap + min_thick + clearance + max_err
                half = spoke_w / 2 + clearance
                corridors = []
                for k in range(4):
                    ang = rec["orientation"] + rec["spoke_angle"] + 90 * k
                    # KiCad's y axis points down; positive angles turn counter-clockwise on screen
                    d = (math.cos(math.radians(ang)), -math.sin(math.radians(ang)))
                    reach = max(((x - c[0]) * d[0] + (y - c[1]) * d[1]) for o in outlines for x, y in o)
                    edge = ray_exit(outlines, c, d) or reach
                    end = reach + length
                    poly = rect(c, d, edge, end, half)
                    test = (c[0] + d[0] * (reach + gap + max_err + min_thick / 2),
                            c[1] + d[1] * (reach + gap + max_err + min_thick / 2))
                    live = bool(fill.Contains(pcbnew.VECTOR2I(round(test[0]), round(test[1]))))
                    # pour beyond the gap: sample a band twice the corridor's reach
                    samples = [(reach + gap + (2 * length - gap) * (i + 0.5) / 8, half * j / 2)
                               for i in range(8) for j in (-1, 0, 1)]
                    inside = sum(bool(fill.Contains(pcbnew.VECTOR2I(round(c[0] + d[0] * s - d[1] * h),
                                                                     round(c[1] + d[1] * s + d[0] * h))))
                                 for s, h in samples)
                    hits = overlaps(layer, poly, rec["uuid"])
                    corridors.append({"k": k, "direction_deg": round(ang % 360, 6), "start": round(edge),
                                      "end": round(end), "half_width": round(half), "polygon": poly,
                                      "live": live, "pour_fraction": round(inside / len(samples), 3),
                                      "overlaps_existing": [list(h) for h in hits[:5]],
                                      "eligible": live and not hits, "selected": False})
                ranked = sorted((cr for cr in corridors if cr["eligible"]),
                                key=lambda cr: (-cr["pour_fraction"], cr["k"]))
                for cr in ranked[:per_pad]:
                    cr["selected"] = True
                rec.update(live_spokes=sum(cr["live"] for cr in corridors), eligible=len(ranked),
                           selected=min(per_pad, len(ranked)), corridors=corridors)
                if rec["live_spokes"] < min_spokes:
                    rec["note"] = (rec.get("note", "") + "; " if rec.get("note") else "") + \
                        f"only {rec['live_spokes']} live spokes in the input fill (min_resolved_spokes {min_spokes})"
                out_pads.append(rec)
    out_pads.sort(key=lambda r: (r["ref"], r["pad"], r["layer"]))
    return {"board": str(board_path), "per_pad": per_pad, "min_resolved_spokes": min_spokes,
            "refilled": bool(filled), "clearance": base_clear, "dru_clearance": dru_clear,
            "max_error": max_err, "warnings": warnings, "pads": out_pads, "skipped": skipped,
            "pad_count": len(out_pads), "corridors_live": sum(p["live_spokes"] for p in out_pads),
            "corridors_selected": sum(p["selected"] for p in out_pads)}


def reserve(pcbnew, board_path, plan_path):
    board = pcbnew.LoadBoard(str(board_path))
    existing = [z.GetZoneName() for z in board.Zones() if z.GetZoneName().startswith(PREFIX)]
    if existing:
        sys.exit(f"{board_path}: already has {len(existing)} {PREFIX} rule areas; strip them first")
    layers = {board.GetLayerName(l): l for l in board.GetEnabledLayers().CuStack()}
    data = json.loads(Path(plan_path).read_text())
    added = 0
    for p in data["pads"]:
        for cr in p["corridors"]:
            if not cr["selected"]:
                continue
            area = pcbnew.ZONE(board)
            area.SetIsRuleArea(True)
            lset = pcbnew.LSET()
            lset.AddLayer(layers[p["layer"]])
            area.SetLayerSet(lset)
            area.SetDoNotAllowTracks(True)
            area.SetDoNotAllowVias(True)
            area.SetDoNotAllowPads(False)
            area.SetDoNotAllowFootprints(False)
            area.SetDoNotAllowZoneFills(False)
            area.SetZoneName(f"{PREFIX}{p['ref']}:{p['pad']}:{p['layer']}:{cr['k']}")
            o = area.Outline()
            o.NewOutline()
            for x, y in cr["polygon"]:
                o.Append(int(x), int(y))
            board.Add(area)
            added += 1
    board.Save(str(board_path))
    return {"reserved": added, "pads": sum(1 for p in data["pads"] if p["selected"])}


def strip(pcbnew, board_path):
    board = pcbnew.LoadBoard(str(board_path))
    ours = [z for z in board.Zones() if z.GetIsRuleArea() and z.GetZoneName().startswith(PREFIX)]
    for z in ours:
        board.Delete(z)  # Remove() leaves the SWIG proxy owning nothing (leak warnings)
    if ours:
        board.Save(str(board_path))
    left = sum(1 for z in board.Zones() if z.GetZoneName().startswith(PREFIX))
    return {"stripped": len(ours), "remaining": left,
            "rule_areas_left": sum(1 for z in board.Zones() if z.GetIsRuleArea())}


def main(argv):
    import pcbnew
    if len(argv) >= 3 and argv[0] == "plan":
        opts = dict(zip(argv[3::2], argv[4::2]))
        data = plan(pcbnew, Path(argv[1]), int(opts["--per-pad"]) if "--per-pad" in opts else None)
        Path(argv[2]).write_text(json.dumps(data, indent=1) + "\n")
        print("SPOKES_JSON: " + json.dumps({k: data[k] for k in (
            "pad_count", "corridors_live", "corridors_selected", "per_pad", "min_resolved_spokes", "clearance",
            "refilled", "warnings")}))
        return 0
    if len(argv) == 3 and argv[0] == "reserve":
        print("SPOKES_JSON: " + json.dumps(reserve(pcbnew, Path(argv[1]), Path(argv[2]))))
        return 0
    if len(argv) == 2 and argv[0] == "strip":
        print("SPOKES_JSON: " + json.dumps(strip(pcbnew, Path(argv[1]))))
        return 0
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
