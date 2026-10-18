#!/usr/bin/env python3
"""Collision barriers: locate DRC collisions on a graded board, inject small keepouts, strip them.

    barrier.py locate BOARD.kicad_pcb REQUEST.json OUT.json   barrier points (board untouched)
    barrier.py inject BOARD.kicad_pcb PLAN.json               add the planned barriers as rule areas (in place)
    barrier.py strip BOARD.kicad_pcb                          remove every kicad-route:barrier: rule area (in place)

locate: REQUEST lists DRC findings of the board, each with the UUID of the NEW copper
item (track, arc or via the routing placed) and the UUIDs of the other items in the
finding. For every copper layer the new item shares with the others, the point on the
new item's centreline (a via: its centre) closest to the others' copper is found; the
barrier goes at the closest of these, on that layer. That is where the route ran too
close: a keepout there makes the backend route elsewhere.

inject: each barrier becomes a single-layer square rule area, `size` on a side,
centred on the point, that forbids tracks and vias (pads, footprints and zone fills
allowed), named `kicad-route:barrier:<x>:<y>` (mm). A barrier whose square touches
existing copper on its layer (a track, via or pad of any net, in the board being
injected) is skipped: it would block that copper's own net. Refuses a board that
already has barrier areas. strip: removes exactly those areas (by name prefix) and
saves only if it removed something. Every command prints `BARRIER_JSON: {...}`; a
board KiCad cannot load prints `BARRIER_LOAD_ERROR: "..."` and exits 3.
Runs with KiCad's pcbnew module. All lengths are integer nanometres.
"""
import json
import math
import sys
from pathlib import Path

PREFIX = "kicad-route:barrier:"
LOAD_ERROR = "BARRIER_LOAD_ERROR:"  # the board did not load (exit 3): the host refuses instead of failing


class LoadError(Exception):
    pass


def load(pcbnew, board_path):
    try:
        board = pcbnew.LoadBoard(str(board_path))
    except Exception as e:  # older pcbnew raises IO_ERROR, KiCad 10 returns None
        raise LoadError(f"{board_path}: KiCad could not load the board ({e})") from e
    if board is None:
        raise LoadError(f"{board_path}: KiCad could not load the board")
    return board


# ------------------------------------------------------------- geometry (pure)

def closest_on_segment(p, a, b):
    """The point of segment a-b closest to p."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
    return (a[0] + t * dx, a[1] + t * dy)


def _intersection(p1, p2, q1, q2):
    """Proper or touching intersection point of two segments, else None."""
    r = (p2[0] - p1[0], p2[1] - p1[1])
    s = (q2[0] - q1[0], q2[1] - q1[1])
    den = r[0] * s[1] - r[1] * s[0]
    if den == 0:
        return None
    qp = (q1[0] - p1[0], q1[1] - p1[1])
    t = (qp[0] * s[1] - qp[1] * s[0]) / den
    u = (qp[0] * r[1] - qp[1] * r[0]) / den
    if 0 <= t <= 1 and 0 <= u <= 1:
        return (p1[0] + t * r[0], p1[1] + t * r[1])
    return None


def seg_seg_closest(p1, p2, q1, q2):
    """(distance, point on p1-p2) of the closest approach of two segments."""
    x = _intersection(p1, p2, q1, q2)
    if x is not None:
        return 0.0, x
    best = None
    for cand_p in (p1, p2, closest_on_segment(q1, p1, p2), closest_on_segment(q2, p1, p2)):
        q = closest_on_segment(cand_p, q1, q2)
        d = math.hypot(cand_p[0] - q[0], cand_p[1] - q[1])
        if best is None or d < best[0]:
            best = (d, cand_p)
    return best


def inside(p, poly):
    """Point in polygon (even-odd)."""
    res = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > p[1]) != (y2 > p[1]) and p[0] < (x2 - x1) * (p[1] - y1) / (y2 - y1) + x1:
            res = not res
    return res


def _nearest_on_path(path, q):
    segs = list(zip(path, path[1:])) or [(path[0], path[0])]
    pts = [closest_on_segment(q, a, b) for a, b in segs]
    return min(pts, key=lambda p: math.hypot(p[0] - q[0], p[1] - q[1]))


def closest_on_path(path, polys):
    """(distance, point) on the polyline `path` closest to any polygon in `polys`.
    0 when the path enters a polygon: then the middle of the overlap (the path point
    nearest the centroid of the edge crossings; for a path wholly inside, of the
    polygon's vertices)."""
    best, zero = None, []
    segs = list(zip(path, path[1:])) or [(path[0], path[0])]
    for poly in polys:
        edges = list(zip(poly, poly[1:] + poly[:1]))
        for a, b in segs:
            for e1, e2 in edges:
                d, pt = seg_seg_closest(a, b, e1, e2)
                if d == 0:
                    zero.append(pt)
                if best is None or d < best[0]:
                    best = (d, pt)
        if all(inside(p, poly) for p in path):
            zero += poly
    if zero:
        c = (sum(p[0] for p in zero) / len(zero), sum(p[1] for p in zero) / len(zero))
        return 0.0, _nearest_on_path(path, c)
    return best


def square(center, size):
    h = size / 2
    x, y = center
    return [(x - h, y - h), (x + h, y - h), (x + h, y + h), (x - h, y + h)]


def polys_touch(a, b):
    """True when two simple polygons overlap or touch."""
    if any(inside(p, b) for p in a) or any(inside(p, a) for p in b):
        return True
    ea, eb = list(zip(a, a[1:] + a[:1])), list(zip(b, b[1:] + b[:1]))
    return any(seg_seg_closest(p1, p2, q1, q2)[0] == 0 for p1, p2 in ea for q1, q2 in eb)


def mm_text(nm):
    return f"{nm / 1e6:.4f}".rstrip("0").rstrip(".")


def barrier_name(x, y):
    return f"{PREFIX}{mm_text(x)}:{mm_text(y)}"


# ------------------------------------------------------------- pcbnew

def _items(board):
    m = {}
    for t in board.GetTracks():
        m[t.m_Uuid.AsString()] = t
    for fp in board.GetFootprints():
        for p in fp.Pads():
            m[p.m_Uuid.AsString()] = p
    for z in board.Zones():
        m[z.m_Uuid.AsString()] = z
    for d in board.GetDrawings():
        m[d.m_Uuid.AsString()] = d
    return m


def _polys(pcbnew, board, item, layer):
    """Outlines of an item's copper on one layer, [[(x, y), ...], ...]."""
    ps = pcbnew.SHAPE_POLY_SET()
    if item.GetClass() == "ZONE":
        if not item.IsOnLayer(layer):
            return []
        fill = item.GetFilledPolysList(layer)
        ps = fill if fill is not None and fill.OutlineCount() else item.Outline()
    else:
        item.TransformShapeToPolygon(ps, layer, 0, board.GetDesignSettings().m_MaxError, pcbnew.ERROR_INSIDE)
    out = []
    for i in range(ps.OutlineCount()):
        o = ps.Outline(i)
        out.append([(o.CPoint(k).x, o.CPoint(k).y) for k in range(o.PointCount())])
    return [p for p in out if len(p) >= 3]


def _centreline(item):
    cls = item.GetClass()
    if cls == "PCB_VIA":
        p = item.GetPosition()
        return [(p.x, p.y)]
    if cls == "PCB_ARC":
        s, m, e = item.GetStart(), item.GetMid(), item.GetEnd()
        (x1, y1), (x2, y2), (x3, y3) = (s.x, s.y), (m.x, m.y), (e.x, e.y)
        d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
        if abs(d) < 1e-9:
            return [(x1, y1), (x3, y3)]
        ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1) + (x3 ** 2 + y3 ** 2) * (y1 - y2)) / d
        uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3) + (x3 ** 2 + y3 ** 2) * (x2 - x1)) / d
        r = math.hypot(x1 - ux, y1 - uy)
        a1, a2, a3 = (math.atan2(y - uy, x - ux) for x, y in ((x1, y1), (x2, y2), (x3, y3)))
        sweep = (a3 - a1) % (2 * math.pi)
        if (a2 - a1) % (2 * math.pi) > sweep:
            sweep -= 2 * math.pi
        return [(ux + r * math.cos(a1 + sweep * i / 16), uy + r * math.sin(a1 + sweep * i / 16)) for i in range(17)]
    s, e = item.GetStart(), item.GetEnd()
    return [(s.x, s.y), (e.x, e.y)]


def locate(pcbnew, board_path, request_path):
    board = load(pcbnew, board_path)
    req = json.loads(Path(request_path).read_text())
    items = _items(board)
    cu = list(board.GetEnabledLayers().CuStack())
    barriers, skipped = [], []
    for f in req["findings"]:
        new = items.get(f["new_uuid"])
        others = [items[u] for u in f["other_uuids"] if u in items]
        base = {k: f[k] for k in ("index", "type")}
        if new is None or new.GetClass() not in ("PCB_TRACK", "PCB_ARC", "PCB_VIA"):
            skipped.append(dict(base, reason="the new copper item is not a track, arc or via on this board"))
            continue
        if not others:
            skipped.append(dict(base, reason="the other items of the finding are not on this board"))
            continue
        path = _centreline(new)
        best = None
        for layer in cu:
            if not new.IsOnLayer(layer):
                continue
            polys = [p for o in others if o.IsOnLayer(layer) for p in _polys(pcbnew, board, o, layer)]
            if not polys:
                continue
            d, pt = closest_on_path(path, polys)
            if best is None or d < best[0]:
                best = (d, pt, layer)
        if best is None:  # e.g. copper_edge_clearance: the partner is an Edge.Cuts shape
            polys = [p for o in others for p in _polys(pcbnew, board, o, o.GetLayer())]
            layer = next(l for l in cu if new.IsOnLayer(l))
            if polys:
                d, pt = closest_on_path(path, polys)
                best = (d, pt, layer)
        if best is None:
            skipped.append(dict(base, reason="the new item shares no copper layer with the other items"))
            continue
        d, (x, y), layer = best
        x, y = int(round(x)), int(round(y))
        barriers.append(dict(base, x=x, y=y, layer=board.GetLayerName(layer), name=barrier_name(x, y),
                             distance_to_other_copper=int(round(d)),
                             new_item={"uuid": f["new_uuid"], "kind": new.GetClass(), "net": new.GetNetname()},
                             other_uuids=f["other_uuids"]))
    return {"board": str(board_path), "barriers": barriers, "skipped": skipped}


def inject(pcbnew, board_path, plan_path):
    board = load(pcbnew, board_path)
    existing = [z.GetZoneName() for z in board.Zones() if z.GetZoneName().startswith(PREFIX)]
    if existing:
        sys.exit(f"{board_path}: already has {len(existing)} {PREFIX} rule areas; strip them first")
    layers = {board.GetLayerName(l): l for l in board.GetEnabledLayers().CuStack()}
    plan = json.loads(Path(plan_path).read_text())
    size = int(plan["size"])
    copper = list(board.GetTracks()) + [p for fp in board.GetFootprints() for p in fp.Pads()]
    injected, skipped, seen = [], [], set()
    for b in plan["barriers"]:
        key = (b["x"], b["y"], b["layer"])
        if key in seen:
            continue
        seen.add(key)
        if b["layer"] not in layers:
            skipped.append({"name": b["name"], "layer": b["layer"], "reason": "layer not enabled on this board"})
            continue
        layer = layers[b["layer"]]
        sq = square((b["x"], b["y"]), size)
        hit = next((c for c in copper if c.IsOnLayer(layer) and
                    any(polys_touch(sq, p) for p in _polys(pcbnew, board, c, layer))), None)
        if hit is not None:
            skipped.append({"name": b["name"], "layer": b["layer"],
                            "reason": f"overlaps existing copper ({hit.GetClass()} {hit.GetNetname() or '<no net>'}): "
                                      "a keepout there would block that copper's own net"})
            continue
        area = pcbnew.ZONE(board)
        area.SetIsRuleArea(True)
        lset = pcbnew.LSET()
        lset.AddLayer(layer)
        area.SetLayerSet(lset)
        area.SetDoNotAllowTracks(True)
        area.SetDoNotAllowVias(True)
        area.SetDoNotAllowPads(False)
        area.SetDoNotAllowFootprints(False)
        area.SetDoNotAllowZoneFills(False)
        area.SetZoneName(b["name"])
        o = area.Outline()
        o.NewOutline()
        for x, y in sq:
            o.Append(int(round(x)), int(round(y)))
        board.Add(area)
        injected.append({"name": b["name"], "layer": b["layer"], "x": b["x"], "y": b["y"]})
    if injected:
        board.Save(str(board_path))
    return {"injected": len(injected), "barriers": injected, "skipped": skipped, "size": size}


def strip(pcbnew, board_path):
    board = load(pcbnew, board_path)
    ours = [z for z in board.Zones() if z.GetIsRuleArea() and z.GetZoneName().startswith(PREFIX)]
    for z in ours:
        board.Delete(z)
    if ours:
        board.Save(str(board_path))
    left = sum(1 for z in board.Zones() if z.GetZoneName().startswith(PREFIX))
    return {"stripped": len(ours), "remaining": left,
            "rule_areas_left": sum(1 for z in board.Zones() if z.GetIsRuleArea())}


def main(argv):
    try:
        return _main(argv)
    except LoadError as e:
        print(LOAD_ERROR + " " + json.dumps(str(e)))
        return 3


def _main(argv):
    import pcbnew
    if len(argv) == 4 and argv[0] == "locate":
        data = locate(pcbnew, Path(argv[1]), Path(argv[2]))
        Path(argv[3]).write_text(json.dumps(data, indent=1) + "\n")
        print("BARRIER_JSON: " + json.dumps({"barriers": len(data["barriers"]), "skipped": len(data["skipped"])}))
        return 0
    if len(argv) == 3 and argv[0] == "inject":
        print("BARRIER_JSON: " + json.dumps(inject(pcbnew, Path(argv[1]), Path(argv[2]))))
        return 0
    if len(argv) == 2 and argv[0] == "strip":
        print("BARRIER_JSON: " + json.dumps(strip(pcbnew, Path(argv[1]))))
        return 0
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
