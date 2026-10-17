#!/usr/bin/env python3
"""Pour facts of a REFILLED KiCad board as JSON, for the pour_integrity check.

    pour.py BOARD.kicad_pcb OUT.json

Runs with a Python that can import KiCad's `pcbnew` (normally /usr/bin/python3),
on a board whose zones KiCad has just refilled (`kicad-cli pcb drc --refill-zones
--save-board`): the fill polygons read here are the ones KiCad grades. Lengths in
the output are millimetres, areas mm^2.

For every copper zone net and layer it records the filled area and each fill
polygon (island) with the pads, vias and tracks of the net that touch it. The
net's copper (fill polygons, pads, vias, tracks) is joined into connected
components; the component holding the most fill area is the net's main pour, and
every polygon or pad outside it is cut off from that pour. For every pad of a zone
net that the zone reaches it records the configured connection, whether the pad
touches the fill, and the thermal spokes actually present, counted like KiCad's
starved-thermal test: the fill is intersected with a thin band along the pad edge
(20 um either side) and each separate piece is a spoke (KiCad counts pad-outline
crossings / 2); fill that closes all the way around the pad (the piece keeps a
hole) is a solid connection. Tracks of the net with one end in the pad and the
other in the fill count as spokes too, as in KiCad. Vias in the pad are recorded
but not counted.
"""
import json
import sys
from pathlib import Path

NM = 1_000_000
SCHEMA = 1  # bump when the facts change meaning; cached baseline facts of another schema are recomputed
EDGE_BAND = 20_000  # nm either side of the pad edge
CELL = 2 * NM  # connectivity grid
LEAF_VERTICES, LEAF_SIZE = 256, NM // 2  # fill tiles


class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, a):
        self.parent.setdefault(a, a)
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def seg_dist2(p, a, b):
    """Squared distance from point p to segment ab (integer nm coordinates)."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    cx, cy = ax + t * dx, ay + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2


def main(board_path, out_path):
    import pcbnew

    board = pcbnew.LoadBoard(str(board_path))
    bds = board.GetDesignSettings()
    max_err = int(bds.m_MaxError)
    lname = board.GetLayerName
    CONN = {pcbnew.ZONE_CONNECTION_NONE: "none", pcbnew.ZONE_CONNECTION_THERMAL: "thermal",
            pcbnew.ZONE_CONNECTION_FULL: "solid", pcbnew.ZONE_CONNECTION_THT_THERMAL: "tht_thermal"}

    def shape(item, layer, clearance=0, err=pcbnew.ERROR_INSIDE):
        s = pcbnew.SHAPE_POLY_SET()
        item.TransformShapeToPolygon(s, layer, int(clearance), max_err, err)
        return s

    def overlap(a, b):
        """Area of a & b (nm^2); a and b are left untouched."""
        if not a.BBox().Intersects(b.BBox()):
            return 0.0
        x = pcbnew.SHAPE_POLY_SET(a)
        x.BooleanIntersection(b)
        return x.Area()

    def rect(x0, y0, x1, y1):
        r = pcbnew.SHAPE_POLY_SET()
        r.NewOutline()
        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            r.Append(int(x), int(y))
        return r

    def bounds(bb):
        return bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()

    def hits(a, b):
        return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]

    def tile(poly, bb=None):
        """Quadtree tiles of a fill polygon: [(bounds, piece)]. A real pour has tens of
        thousands of vertices; clipping every small item against it costs ~25 ms per
        boolean, against a tile almost nothing. The tiles are disjoint and cover it."""
        bb = bb or bounds(poly.BBox())
        if poly.TotalVertices() <= LEAF_VERTICES or max(bb[2] - bb[0], bb[3] - bb[1]) <= LEAF_SIZE:
            return [(bounds(poly.BBox()), poly)]
        mx, my = (bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2
        out = []
        for q in ((bb[0], bb[1], mx, my), (mx, bb[1], bb[2], my), (bb[0], my, mx, bb[3]), (mx, my, bb[2], bb[3])):
            part = pcbnew.SHAPE_POLY_SET(poly)
            part.BooleanIntersection(rect(*q))
            if part.OutlineCount():
                out += tile(part, q)
        return out

    def area_on(s, tiles):
        """Area of s & the tiled polygon."""
        sb, total = bounds(s.BBox()), 0.0
        for tb, piece in tiles:
            if hits(sb, tb):
                total += overlap(s, piece)
        return total

    def local_fill(net, layer, bb):
        """The net's fill on a layer, clipped to the box bb (bounds)."""
        out = pcbnew.SHAPE_POLY_SET()
        for p in polygons.values():
            if p["net"] != net or p["_layer"] != layer or not hits(bb, p["_bounds"]):
                continue
            for tb, piece in p["_tiles"]:
                if hits(bb, tb):
                    out.BooleanAdd(piece)
        if out.OutlineCount():
            out.BooleanIntersection(rect(*bb))
        return out

    def in_fill(net, layer, pt):
        b = (pt.x, pt.y, pt.x, pt.y)
        return any(hits(b, tb) and piece.Contains(pt) for p in polygons.values()
                   if p["net"] == net and p["_layer"] == layer and hits(b, p["_bounds"]) for tb, piece in p["_tiles"])

    def mm(v):
        return round(v / NM, 4)

    def box(s):
        bb = s.BBox()
        return [mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom())]

    zones = [z for z in board.Zones() if not z.GetIsRuleArea() and z.GetNetCode() > 0]
    zone_nets = {z.GetNetname() for z in zones}

    # Pads keyed like facts.py: "<ref>.<number>", with ' appended for duplicate numbers.
    pads = {}
    for fp in board.GetFootprints():
        seen = set()
        for pad in fp.Pads():
            key = pad.GetNumber() or f"<unnumbered@{pad.GetPosition().x},{pad.GetPosition().y}>"
            while key in seen:
                key += "'"
            seen.add(key)
            if pad.GetNetname() in zone_nets:
                pads[f"{fp.GetReference()}.{key}"] = pad

    tracks, vias = [], []
    for t in board.GetTracks():
        if t.GetNetname() not in zone_nets:
            continue
        if isinstance(t, pcbnew.PCB_VIA):
            vias.append(t)
        else:
            tracks.append(t)

    def track_points(t):
        pts = [(t.GetStart().x, t.GetStart().y)]
        if isinstance(t, pcbnew.PCB_ARC):
            pts.append((t.GetMid().x, t.GetMid().y))
        pts.append((t.GetEnd().x, t.GetEnd().y))
        return pts

    uf = UnionFind()
    polygons = {}  # id -> record (private: "_tiles", "_bounds", "_layer")
    filled_layers = set()  # (net, layer) with fill
    for zi, z in enumerate(sorted(zones, key=lambda z: (z.GetNetname(), z.GetZoneName(), -z.GetAssignedPriority()))):
        net = z.GetNetname()
        for layer in z.GetLayerSet().CuStack():
            if not z.HasFilledPolysForLayer(layer):
                continue
            fill = z.GetFilledPolysList(layer)
            filled_layers.add((net, layer))
            pieces = []
            for i in range(fill.OutlineCount()):
                s = pcbnew.SHAPE_POLY_SET()
                s.AddOutline(fill.COutline(i))
                pieces.append(s)
            pieces.sort(key=lambda s: -s.Area())
            for i, s in enumerate(pieces):
                pid = f"{net}@{lname(layer)}/{z.GetZoneName() or 'zone' + str(zi)}#{i}"
                polygons[pid] = {"net": net, "layer": lname(layer), "zone": z.GetZoneName(),
                                 "area_mm2": round(s.Area() / NM / NM, 4), "bbox_mm": box(s),
                                 "pads": [], "vias": 0, "tracks": 0, "_tiles": tile(s),
                                 "_bounds": bounds(s.BBox()), "_layer": layer}
                uf.find(("poly", pid))

    # Fill polygons of the same net on the same layer (overlapping zones) touch each other.
    ids = sorted(polygons)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            pa, pb = polygons[a], polygons[b]
            if pa["net"] == pb["net"] and pa["_layer"] == pb["_layer"] and pa["zone"] != pb["zone"] \
                    and hits(pa["_bounds"], pb["_bounds"]) \
                    and any(hits(ta, tb) and overlap(qa, qb) > 0 for ta, qa in pa["_tiles"] for tb, qb in pb["_tiles"]):
                uf.union(("poly", a), ("poly", b))

    def polys_on(net, layer):
        return [(pid, p) for pid, p in polygons.items() if p["net"] == net and p["_layer"] == layer]

    # Pads, vias and tracks against the fill polygons of their net.
    pad_shapes = {}
    for key, pad in pads.items():
        uf.find(("pad", key))
        for layer in pad.GetLayerSet().CuStack():
            s = shape(pad, layer)
            pad_shapes[(key, layer)] = s
            for pid, p in polys_on(pad.GetNetname(), layer):
                if area_on(s, p["_tiles"]) > 0:
                    p["pads"].append(key)
                    uf.union(("pad", key), ("poly", pid))
    via_shapes = []
    via_fill = set()  # vias that touch a fill polygon of their net on some layer
    for vi, v in enumerate(vias):
        uf.find(("via", vi))
        per_layer = {}
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
    # Track endpoints against pads, vias and other tracks of the net on the same layer,
    # through a coarse grid so large boards stay linear-ish.
    grid = {}

    def cells(x0, y0, x1, y1):
        for gx in range(int(x0 // CELL), int(x1 // CELL) + 1):
            for gy in range(int(y0 // CELL), int(y1 // CELL) + 1):
                yield gx, gy

    def insert(net, layer, bb, item):
        for c in cells(bb[0], bb[1], bb[2], bb[3]):
            grid.setdefault((net, layer, c), []).append(item)

    def near(net, layer, pt):
        return grid.get((net, layer, (int(pt[0] // CELL), int(pt[1] // CELL))), [])

    for (key, layer), s in pad_shapes.items():
        bb = s.BBox()
        insert(pads[key].GetNetname(), layer, (bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()),
               ("pad", key, s))
    for vi, per_layer in enumerate(via_shapes):
        for layer, s in per_layer.items():
            bb = s.BBox()
            insert(vias[vi].GetNetname(), layer, (bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()),
                   ("via", vi, s))
    track_data = []
    for ti, t in enumerate(tracks):
        pts, w = track_points(t), t.GetWidth()
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        track_data.append((t.GetLayer(), t.GetNetname(), pts, w))
        insert(t.GetNetname(), t.GetLayer(), (min(xs) - w, min(ys) - w, max(xs) + w, max(ys) + w), ("track", ti, None))

    track_pads = {}  # pad key -> list of (track index, layer)
    for ti, (layer, net, pts, w) in enumerate(track_data):
        uf.find(("track", ti))
        s = shape(tracks[ti], layer)
        for pid, p in polys_on(net, layer):
            if area_on(s, p["_tiles"]) > 0:
                p["tracks"] += 1
                uf.union(("track", ti), ("poly", pid))
        for end in (pts[0], pts[-1]):
            seen = set()
            for kind, ident, other in near(net, layer, end):
                if (kind, ident) in seen or (kind == "track" and ident == ti):
                    continue
                seen.add((kind, ident))
                if kind == "track":
                    _, _, opts, ow = track_data[ident]
                    tol2 = (max(w, ow) / 2) ** 2
                    hit = any(seg_dist2(end, q0, q1) <= tol2 for q0, q1 in zip(opts, opts[1:]))
                else:
                    hit = other.Contains(pcbnew.VECTOR2I(int(end[0]), int(end[1])))
                if hit:
                    uf.union(("track", ti), (kind, ident))
                    if kind == "pad" and (ti, layer) not in track_pads.get(ident, []):
                        track_pads.setdefault(ident, []).append((ti, layer))

    # Main pour of each net: the connected component holding the most fill area.
    comp_area = {}
    for pid, p in polygons.items():
        root = uf.find(("poly", pid))
        comp_area.setdefault(p["net"], {}).setdefault(root, 0.0)
        comp_area[p["net"]][root] += p["area_mm2"]
    main = {net: max(c.items(), key=lambda kv: (kv[1], str(kv[0])))[0] for net, c in comp_area.items()}
    for pid, p in polygons.items():
        p["in_main"] = uf.find(("poly", pid)) == main.get(p["net"])

    # Pads the zones reach: connection, fill contact, spokes.
    pad_records = {}
    for z in sorted(zones, key=lambda z: -z.GetAssignedPriority()):
        net = z.GetNetname()
        zone_conn = z.GetPadConnection()
        for layer in z.GetLayerSet().CuStack():
            for key, pad in pads.items():
                rec_key = f"{key}@{lname(layer)}"
                if pad.GetNetname() != net or (key, layer) not in pad_shapes or rec_key in pad_records:
                    continue
                fp = pad.GetParentFootprint()
                conn = pad.GetLocalZoneConnection()
                if conn == pcbnew.ZONE_CONNECTION_INHERITED and fp is not None:
                    conn = fp.GetLocalZoneConnection()
                if conn == pcbnew.ZONE_CONNECTION_INHERITED:
                    conn = zone_conn
                pth = pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH
                if conn == pcbnew.ZONE_CONNECTION_THT_THERMAL:
                    conn = pcbnew.ZONE_CONNECTION_THERMAL if pth else pcbnew.ZONE_CONNECTION_FULL
                gap = pad.GetLocalThermalGapOverride()
                gap = int(gap) if gap is not None else int(z.GetThermalReliefGap())
                reach = shape(pad, layer, gap)
                if overlap(reach, z.Outline()) <= 0:
                    continue  # the zone does not reach this pad
                ps = pad_shapes[(key, layer)]
                rb = bounds(reach.BBox())
                fill = local_fill(net, layer, rb) if (net, layer) in filled_layers else None
                touches = fill is not None and overlap(ps, fill) > 0
                spokes, coverage, solid, track_spokes = 0, 0.0, False, 0
                if fill is not None:
                    ring = shape(pad, layer, EDGE_BAND)
                    ring.BooleanSubtract(shape(pad, layer, -EDGE_BAND, pcbnew.ERROR_OUTSIDE))
                    hit = pcbnew.SHAPE_POLY_SET(ring)
                    hit.BooleanIntersection(fill)
                    spokes = hit.OutlineCount()
                    coverage = hit.Area() / ring.Area() if ring.Area() > 0 else 0.0
                    solid = any(hit.HoleCount(i) > 0 for i in range(hit.OutlineCount()))
                # Tracks count as spokes the way KiCad's DRC counts them: one end in the pad,
                # the other outside it and inside the fill (a stub inside the pad does not).
                for ti in {ti for ti, lyr in track_pads.get(key, []) if lyr == layer}:
                    ends = [pcbnew.VECTOR2I(int(x), int(y)) for x, y in (track_data[ti][2][0], track_data[ti][2][-1])]
                    inside = [ps.Contains(e) for e in ends]
                    if inside.count(True) == 1 and in_fill(net, layer, ends[inside.index(False)]):
                        track_spokes += 1
                on_layer = [ti for ti, lyr in track_pads.get(key, []) if lyr == layer]
                # A via in the pad that reaches a fill (any layer) is a path of its own; KiCad's
                # spoke count ignores it, so it is recorded but not counted as a spoke.
                via_spokes = sum(1 for vi in via_fill if vias[vi].GetNetname() == net and layer in via_shapes[vi]
                                 and overlap(via_shapes[vi][layer], ps) > 0)
                pos = pad.GetPosition()
                pad_records[rec_key] = {
                    "pad": key, "net": net, "layer": lname(layer), "zone": z.GetZoneName(),
                    "position_mm": [mm(pos.x), mm(pos.y)], "pth": pth,
                    "connection": CONN.get(conn, str(conn)), "thermal_gap_mm": mm(gap),
                    "touches_fill": touches, "spokes": spokes, "ring_coverage": round(coverage, 3),
                    "solid": solid,
                    "polygons": sorted(pid for pid, p in polys_on(net, layer) if key in p["pads"]),
                    "in_main": uf.find(("pad", key)) == main.get(net),
                    "tracks_on_layer": len(on_layer), "tracks_attached": len(track_pads.get(key, [])),
                    "track_spokes": track_spokes, "via_spokes": via_spokes,
                }

    layers = {}
    for p in polygons.values():
        lk = f"{p['net']}@{p['layer']}"
        rec = layers.setdefault(lk, {"net": p["net"], "layer": p["layer"], "area_mm2": 0.0, "polygons": 0,
                                     "islands_off_main": 0})
        rec["area_mm2"] = round(rec["area_mm2"] + p["area_mm2"], 4)
        rec["polygons"] += 1
        rec["islands_off_main"] += 0 if p["in_main"] else 1
    out = {
        "schema": SCHEMA, "kicad_version": pcbnew.GetBuildVersion(), "board": Path(board_path).name,
        "min_resolved_spokes": int(bds.m_MinResolvedSpokes),
        "zones": sorted(({"name": z.GetZoneName(), "net": z.GetNetname(),
                          "layers": sorted(lname(l) for l in z.GetLayerSet().CuStack()),
                          "connection": CONN.get(z.GetPadConnection(), str(z.GetPadConnection())),
                          "thermal_gap_mm": mm(z.GetThermalReliefGap()),
                          "spoke_width_mm": mm(z.GetThermalReliefSpokeWidth()),
                          "island_removal": int(z.GetIslandRemovalMode()), "filled": bool(z.IsFilled())}
                         for z in zones), key=lambda r: json.dumps(r, sort_keys=True)),
        "layers": dict(sorted(layers.items())),
        "polygons": {pid: {k: v for k, v in p.items() if not k.startswith("_")} | {"pads": sorted(p["pads"])}
                     for pid, p in sorted(polygons.items())},
        "pads": dict(sorted(pad_records.items())),
    }
    Path(out_path).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
