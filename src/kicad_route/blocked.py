"""Blocked-by receipts for connections a routing attempt left open.

For every unconnected item KiCad reports on an in-scope net, a corridor is laid
between its two endpoints on each copper layer: a straight band and the two
L-shaped (Manhattan) bands, each as wide as the net class track plus its
clearance on both sides. Everything that intersects a corridor is listed per
layer: other nets' tracks, vias and pads, keepouts (rule areas) and the board
edge as hard obstacles, other nets' zones (they give way to tracks) and
via-only keepouts as soft ones. Copper is tagged with its lock state and origin
(`input`: present in the attempt's input board; `new`: placed by the backend).
Across all open connections, individual obstacles are then ranked by how many
connections they block on each connection's least obstructed layer.

This is diagnosis only. It reads board facts (kicad/facts.py, nanometres) and
the candidate's native DRC report (millimetres); it never changes a board and
never relaxes a rule. Straight and Manhattan corridors approximate where a route
would go: a blocker is a pointer for the next attempt, not a proof that no route
exists, and an empty corridor does not prove that one does.
"""
import json
import math
import re
import sys
from pathlib import Path

NM = 1_000_000
DEFAULT_TRACK_NM = 200_000       # KiCad's Default net class, used when a net has no class width
DEFAULT_CLEARANCE_NM = 200_000
RANKING_LIMIT = 50               # entries kept per ranking list in blocked-by.json
TOUCH_NM = 1000                  # 1 um: copper this close counts as touching (Specctra quantisation)
REPORT_VERSION = 1
NET_IN_ITEM = re.compile(r"\[([^\]]+)\]")
ZONE_ITEM = re.compile(r"^Zone(?: '([^']*)')? \[([^\]]*)\]")
CATEGORIES = ("keepout", "input_copper", "new_copper", "footprint", "board_edge")
CATEGORY_NOTE = {
    "keepout": "rule area: part of the design's constraints; route around it, never edit it",
    "input_copper": "copper from the attempt's input: moving it needs the owner's permission "
                    "(--protect locked frees only unlocked input copper)",
    "new_copper": "the backend's own output in this attempt: cheap to rip (re-route in another order or scope)",
    "footprint": "pads of other nets: placement is fixed for routing; go around or change layer",
    "board_edge": "the corridor leaves the board outline",
}


# ------------------------------------------------------------------ geometry
# Points are (x, y) pairs in any one unit (nanometres here).

def point_segment_distance(p, a, b):
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _on_segment(p, a, b):
    return min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])


def segments_intersect(a, b, c, d):
    """True when segment ab and segment cd share at least one point."""
    d1, d2, d3, d4 = _cross(c, d, a), _cross(c, d, b), _cross(a, b, c), _cross(a, b, d)
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True
    return ((d1 == 0 and _on_segment(a, c, d)) or (d2 == 0 and _on_segment(b, c, d))
            or (d3 == 0 and _on_segment(c, a, b)) or (d4 == 0 and _on_segment(d, a, b)))


def segment_distance(a, b, c, d):
    if segments_intersect(a, b, c, d):
        return 0.0
    return min(point_segment_distance(a, c, d), point_segment_distance(b, c, d),
               point_segment_distance(c, a, b), point_segment_distance(d, a, b))


def point_in_polygon(p, poly):
    """Even-odd rule; points on the boundary may land on either side."""
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _segments(path):
    if len(path) == 1:
        return [(path[0], path[0])]
    return list(zip(path, path[1:]))


def _edges(poly):
    return [(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly))]


def polyline_distance(path, other):
    """Shortest distance between two polylines (a single point is a polyline)."""
    return min(segment_distance(a, b, c, d) for a, b in _segments(path) for c, d in _segments(other))


def polyline_polygon_distance(path, poly):
    """0 when the polyline enters or crosses the polygon, else the gap between them."""
    if len(poly) < 3:
        return polyline_distance(path, poly)
    if any(point_in_polygon(p, poly) for p in path):
        return 0.0
    return min(segment_distance(a, b, c, d) for a, b in _segments(path) for c, d in _edges(poly))


def arc_points(start, mid, end, steps=8):
    """Polyline along the circular arc start-mid-end (the chord pair when collinear)."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if d == 0:
        return [start, mid, end]
    ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1) + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
    uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3) + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1, a2, a3 = (math.atan2(y - uy, x - ux) for x, y in (start, mid, end))
    sweep = (a3 - a1) % (2 * math.pi)
    if (a2 - a1) % (2 * math.pi) > sweep:  # mid is not on the counter-clockwise sweep: go the other way
        sweep -= 2 * math.pi
    inner = [(ux + r * math.cos(a1 + sweep * i / steps), uy + r * math.sin(a1 + sweep * i / steps))
             for i in range(1, steps)]
    return [tuple(start), *inner, tuple(end)]


def rect_polygon(center, size, angle_deg=0.0):
    """Corners of a size[0] x size[1] rectangle centred on center, rotated by angle_deg
    counter-clockwise as seen in KiCad (whose y axis points down)."""
    cx, cy = center
    hw, hh = size[0] / 2, size[1] / 2
    t = math.radians(angle_deg)
    c, s = math.cos(t), math.sin(t)
    return [(cx + x * c + y * s, cy - x * s + y * c) for x, y in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]


def bbox(points, grow=0.0):
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (min(xs) - grow, min(ys) - grow, max(xs) + grow, max(ys) + grow)


def bbox_overlap(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def corridor_paths(a, b):
    """The straight corridor and the two Manhattan (L-shaped) ones from a to b."""
    paths = {"straight": [a, b]}
    if a[0] != b[0] and a[1] != b[1]:
        paths["L_horizontal_first"] = [a, (b[0], a[1]), b]
        paths["L_vertical_first"] = [a, (a[0], b[1]), b]
    return paths


class Obstacle:
    """A shape on a set of copper layers: a thick polyline (tracks, vias, edges) or a polygon."""

    def __init__(self, info, layers, path=None, polygon=None, radius=0.0, hard=True):
        self.info, self.layers, self.radius, self.hard = info, set(layers), radius, hard
        self.path, self.polygon = path, polygon
        self.box = bbox(path or polygon, radius)

    def distance(self, path):
        if self.polygon is not None:
            return polyline_polygon_distance(path, self.polygon)
        return max(0.0, polyline_distance(path, self.path) - self.radius)

    def blocks(self, path, path_box, half_width):
        grown = (path_box[0] - half_width, path_box[1] - half_width, path_box[2] + half_width, path_box[3] + half_width)
        return bbox_overlap(grown, self.box) and self.distance(path) < half_width


# --------------------------------------------------------------- board model

def _mm(v):
    return round(v / NM, 4)


def _pt_mm(p):
    return [_mm(p[0]), _mm(p[1])]


def copper_layer_names(facts):
    return [layer["name"] for layer in facts.get("copper_layers", [])]


def via_layers(via, copper):
    if via.get("via_type") == "through":
        return list(copper)
    try:
        i, j = sorted(copper.index(name) for name in via["layers"])
    except ValueError:
        return list(copper)
    return copper[i:j + 1]


def track_path(t):
    if t.get("kind") == "arc" and "mid" in t:
        return arc_points(t["start"], t["mid"], t["end"])
    return [tuple(t["start"]), tuple(t["end"])]


def outline_segments(facts):
    segs = []
    for s in facts.get("outline", []):
        shape = s.get("shape", "").lower()
        if "points" in s:
            pts = [tuple(p) for p in s["points"]]
            segs += _edges(pts) if len(pts) > 2 else _segments(pts)
        elif "center" in s:
            c, r = s["center"], s["radius"]
            pts = [(c[0] + r * math.cos(2 * math.pi * i / 32), c[1] + r * math.sin(2 * math.pi * i / 32))
                   for i in range(32)]
            segs += _edges(pts)
        elif "mid" in s:
            segs += _segments(arc_points(s["start"], s["mid"], s["end"]))
        elif "rect" in shape:
            (x1, y1), (x2, y2) = s["start"], s["end"]
            segs += _edges([(x1, y1), (x2, y1), (x2, y2), (x1, y2)])
        elif "start" in s:
            segs.append((tuple(s["start"]), tuple(s["end"])))
    return segs


class CopperOrigin:
    """Tells input copper (present in the reference board) from copper the backend added:
    same uuid, or same net/layer/geometry within 1 um."""

    def __init__(self, ref_facts):
        ref_facts = ref_facts or {}
        items = ref_facts.get("tracks", []) + ref_facts.get("vias", [])
        self.uuids = {i.get("uuid") for i in items}
        self.by_key = {}
        for i in items:
            self.by_key.setdefault(self._key(i), []).append(i)

    @staticmethod
    def _key(i):
        return (i.get("net"), i.get("layer") or "via", i.get("kind") or "via")

    def of(self, item):
        if item.get("uuid") in self.uuids:
            return "input"
        pts = [item[k] for k in ("start", "mid", "end", "position") if k in item]
        for ref in self.by_key.get(self._key(item), []):
            rpts = [ref[k] for k in ("start", "mid", "end", "position") if k in ref]
            if len(rpts) == len(pts) and all(abs(p[0] - q[0]) <= TOUCH_NM and abs(p[1] - q[1]) <= TOUCH_NM
                                             for p, q in zip(pts, rpts)):
                return "input"
        return "new"


def _pad_polygon(fp, pad):
    size = pad.get("size") or [0, 0]
    if not size[0] or not size[1]:
        drill = pad.get("drill") or [0, 0]
        size = [max(drill), max(drill)]
    return rect_polygon(pad["position"], size, fp.get("orientation", 0.0))


def build_obstacles(facts, ref_facts):
    """Every potential obstacle on the candidate board, with what a receipt should say about it."""
    copper = copper_layer_names(facts)
    origin = CopperOrigin(ref_facts)
    obstacles = []
    for t in facts.get("tracks", []):
        o = origin.of(t)
        obstacles.append(Obstacle(
            {"id": t["uuid"], "kind": "track", "category": f"{o}_copper", "net": t["net"], "layer": t["layer"],
             "locked": t["locked"], "origin": o, "uuid": t["uuid"], "width_mm": _mm(t["width"]),
             "start_mm": _pt_mm(t["start"]), "end_mm": _pt_mm(t["end"])},
            [t["layer"]], path=track_path(t), radius=t["width"] / 2))
    for v in facts.get("vias", []):
        o = origin.of(v)
        obstacles.append(Obstacle(
            {"id": v["uuid"], "kind": "via", "category": f"{o}_copper", "net": v["net"], "locked": v["locked"],
             "origin": o, "uuid": v["uuid"], "position_mm": _pt_mm(v["position"]),
             "diameter_mm": _mm(v["diameter"])},
            via_layers(v, copper), path=[tuple(v["position"])], radius=v["diameter"] / 2))
    for ref, fp in facts.get("footprints", {}).items():
        for num, pad in fp.get("pads", {}).items():
            layers = [name for name in pad.get("layers", []) if name in copper]
            if not layers:
                continue
            obstacles.append(Obstacle(
                {"id": f"pad:{ref}.{num}", "kind": "pad", "category": "footprint", "footprint": ref, "pad": num,
                 "net": pad.get("net", ""), "locked": fp.get("locked"), "position_mm": _pt_mm(pad["position"])},
                layers, polygon=_pad_polygon(fp, pad)))
    for i, ra in enumerate(facts.get("rule_areas", [])):
        if len(ra.get("outline", [])) < 3:
            continue
        restrictions = [k[3:] for k in ("no_tracks", "no_vias", "no_pads", "no_footprints", "no_zone_fills")
                        if ra.get(k)]
        info = {"id": f"rule_area:{ra.get('name') or '#' + str(i)}", "kind": "rule_area", "category": "keepout",
                "name": ra.get("name") or None, "net": None, "restrictions": restrictions,
                "rule_area_layers": ra.get("layers", []), "bbox_mm": [_mm(v) for v in bbox(ra["outline"])]}
        if ra.get("no_tracks") or ra.get("no_vias"):
            # a track-blocking keepout is hard; one that only forbids vias still costs a layer change
            obstacles.append(Obstacle(info, [n for n in ra.get("layers", []) if n in copper],
                                      polygon=[tuple(p) for p in ra["outline"]], hard=bool(ra.get("no_tracks"))))
    for i, z in enumerate(facts.get("zones", [])):
        if len(z.get("outline", [])) < 3:
            continue
        obstacles.append(Obstacle(
            {"id": f"zone:{z.get('name') or '#' + str(i)}:{z.get('net', '')}", "kind": "zone", "category": "zone",
             "name": z.get("name") or None, "net": z.get("net", ""), "zone_layers": z.get("layers", []),
             "priority": z.get("priority")},
            [n for n in z.get("layers", []) if n in copper], polygon=[tuple(p) for p in z["outline"]], hard=False))
    edges = outline_segments(facts)
    for i, (a, b) in enumerate(edges):
        obstacles.append(Obstacle({"id": "board_edge", "kind": "board_edge", "category": "board_edge", "net": None,
                                   "segment_mm": [_pt_mm(a), _pt_mm(b)]}, copper, path=[a, b]))
    return obstacles


# ----------------------------------------------------------------- endpoints

def item_kind(description):
    d = description.lower()
    for kind in ("zone", "via", "track", "arc", "pad"):
        if d.startswith(kind) or (kind == "pad" and " pad " in f" {d}"):
            return kind
    return "other"


def _same_net_copper(facts, net, copper):
    """Same-net pads, tracks and vias as (record, obstacle-like shape, layers)."""
    out = []
    for t in facts.get("tracks", []):
        if t["net"] == net:
            out.append(({"kind": "track", "item": t}, Obstacle({}, [t["layer"]], path=track_path(t),
                                                                radius=t["width"] / 2)))
    for v in facts.get("vias", []):
        if v["net"] == net:
            out.append(({"kind": "via", "item": v, "position": v["position"]},
                        Obstacle({}, via_layers(v, copper), path=[tuple(v["position"])], radius=v["diameter"] / 2)))
    for ref, fp in facts.get("footprints", {}).items():
        for num, pad in fp.get("pads", {}).items():
            layers = [n for n in pad.get("layers", []) if n in copper]
            if pad.get("net") == net and layers:
                out.append(({"kind": "pad", "item": pad, "position": pad["position"], "label": f"{ref}.{num}"},
                            Obstacle({}, layers, polygon=_pad_polygon(fp, pad))))
    return out


def shape_gap(a, b):
    """Gap between two obstacle shapes (0 or less when they touch or overlap)."""
    if a.polygon is None and b.polygon is None:
        return polyline_distance(a.path, b.path) - a.radius - b.radius
    if a.polygon is None:
        return polyline_polygon_distance(a.path, b.polygon) - a.radius
    if b.polygon is None:
        return polyline_polygon_distance(b.path, a.polygon) - b.radius
    if point_in_polygon(b.polygon[0], a.polygon):
        return 0.0
    return polyline_polygon_distance(a.polygon + [a.polygon[0]], b.polygon)


def _touching(a, b):
    grown = (b.box[0] - TOUCH_NM, b.box[1] - TOUCH_NM, b.box[2] + TOUCH_NM, b.box[3] + TOUCH_NM)
    return bool(a.layers & b.layers) and bbox_overlap(a.box, grown) and shape_gap(a, b) <= TOUCH_NM


def islands(items):
    """Connected groups of same-net copper by geometric contact (zone fills not included)."""
    parent = list(range(len(items)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if root(i) != root(j) and _touching(items[i][1], items[j][1]):
                parent[root(i)] = root(j)
    return [root(i) for i in range(len(items))]


def zone_target(facts, zone_item, other_pos, net):
    """Where a missing pad-to-zone connection should land: the nearest same-net pad or via
    inside the zone outline that is not already on the other endpoint's copper island."""
    m = ZONE_ITEM.match(zone_item.get("description", ""))
    name = m.group(1) if m else None
    zone = next((z for z in facts.get("zones", []) if z.get("net") == net and (name is None or z.get("name") == name)),
                None)
    if zone is None or len(zone.get("outline", [])) < 3:
        return None, "zone not found in the board facts; its reported position is used"
    copper = copper_layer_names(facts)
    items = _same_net_copper(facts, net, copper)
    roots = islands(items)
    mine = {roots[i] for i, (_, shape) in enumerate(items) if shape.distance([other_pos]) <= TOUCH_NM}
    outline = [tuple(p) for p in zone["outline"]]
    best = None
    for i, (rec, shape) in enumerate(items):
        if rec["kind"] == "track" or roots[i] in mine or not (shape.layers & set(zone.get("layers", []))):
            continue
        if not point_in_polygon(rec["position"], outline):
            continue
        d = math.hypot(rec["position"][0] - other_pos[0], rec["position"][1] - other_pos[1])
        if best is None or d < best[0]:
            best = (d, rec)
    if best is None:
        return None, "no same-net pad or via inside the zone outside this island; the zone's reported position is used"
    rec = best[1]
    label = f"pad {rec['label']}" if rec["kind"] == "pad" else f"via {rec['item'].get('uuid', '')[:8]}"
    return tuple(rec["position"]), (f"zone endpoint: nearest same-net {label} inside the zone outline and off the "
                                    f"other endpoint's copper island (zone fill is not modelled)")


def endpoints(facts, unconnected, net):
    items = unconnected.get("items", [])[:2]
    out = []
    for it in items:
        pos = it.get("pos", {})
        out.append({"description": it.get("description", ""), "uuid": it.get("uuid"),
                    "kind": item_kind(it.get("description", "")),
                    "point": (round(pos.get("x", 0) * NM), round(pos.get("y", 0) * NM))})
    if len(out) != 2:
        return out
    for i in (0, 1):
        if out[i]["kind"] == "zone" and out[1 - i]["kind"] != "zone":
            target, note = zone_target(facts, items[i], out[1 - i]["point"], net)
            out[i]["note"] = note
            if target is not None:
                out[i]["point"] = target
    return out


# ------------------------------------------------------------------ analysis

def connection_net(unconnected, scope=None):
    nets = sorted({n for i in unconnected.get("items", []) for n in NET_IN_ITEM.findall(i.get("description", ""))})
    if scope is not None:
        nets = [n for n in nets if n in scope]
    return nets[0] if nets else None


def _brief(info):
    out = {k: v for k, v in info.items() if k in ("id", "kind", "category", "net", "layer", "locked", "origin",
                                                  "footprint", "pad", "name", "restrictions")}
    out["label"] = short_label(info)
    return out


def _few(items, n):
    return ", ".join(items[:n]) + (f" +{len(items) - n} more" if len(items) > n else "")


def short_label(info):
    """One obstacle in a few words (per-connection and per-net listings)."""
    kind = info["kind"]
    if kind == "rule_area":
        return f"keepout '{info.get('name') or info['id']}'"
    if kind == "track":
        return f"track {info.get('net') or '(no net)'} {info['layer']} {info['id'][:8]}"
    if kind == "via":
        return f"via {info.get('net') or '(no net)'} {info['id'][:8]}"
    if kind == "pad":
        return f"pad {info['footprint']}.{info['pad']} {info.get('net') or '(no net)'}"
    if kind == "zone":
        return f"zone '{info.get('name') or ''}' {info.get('net') or '(no net)'}"
    return "board edge"


def label(info):
    kind = info["kind"]
    if kind == "rule_area":
        restrictions = ", ".join("no " + r.replace("_", " ") for r in info["restrictions"])
        return (f"keepout '{info.get('name') or info['id']}' ({restrictions}) on "
                f"{', '.join(info.get('rule_area_layers', []))}")
    if kind in ("track", "via"):
        lock = "locked" if info.get("locked") else "unlocked"
        where = f" {info['layer']}" if kind == "track" else ""
        return f"{kind} {info.get('net') or '(no net)'}{where} ({info['origin']}, {lock}) {info['id'][:8]}"
    if kind == "footprint":
        return (f"footprint {info['footprint']} pads {_few(info['pads'], 6)} "
                f"({_few(info['nets'], 3) or 'no net'})")
    if kind == "pad":
        return f"pad {info['footprint']}.{info['pad']} {info.get('net') or '(no net)'}"
    if kind == "zone":
        return f"zone '{info.get('name') or ''}' {info.get('net') or '(no net)'} (soft: yields to tracks)"
    return "board edge"


def analyze(drc, facts, ref_facts=None, scope=None, top=10):
    """The full blocked-by report for the in-scope unconnected items of a candidate DRC."""
    copper = copper_layer_names(facts)
    nets = facts.get("nets", {})
    obstacles = build_obstacles(facts, ref_facts)
    per_layer = {name: [o for o in obstacles if name in o.layers] for name in copper}
    connections = []
    for u in drc.get("unconnected_items", []):
        net = connection_net(u, scope)
        if net is None:
            continue
        cls = nets.get(net, {})
        width = cls.get("track_width") or DEFAULT_TRACK_NM
        clearance = cls.get("clearance") or DEFAULT_CLEARANCE_NM
        half = width / 2 + clearance
        ends = endpoints(facts, u, net)
        conn = {"net": net, "netclass": cls.get("netclass"), "corridor_width_mm": _mm(2 * half),
                "endpoints": [{"description": e["description"], "uuid": e["uuid"], "kind": e["kind"],
                               "position_mm": _pt_mm(e["point"]), **({"note": e["note"]} if e.get("note") else {})}
                              for e in ends],
                "layers": {}}
        if len(ends) != 2:
            conn["error"] = "DRC item does not name two endpoints"
            connections.append(conn)
            continue
        a, b = ends[0]["point"], ends[1]["point"]
        conn["length_mm"] = _mm(math.hypot(b[0] - a[0], b[1] - a[1]))
        for layer in copper:
            variants = {}
            for name, path in corridor_paths(a, b).items():
                box = bbox(path)
                hit = [o for o in per_layer[layer] if o.info.get("net") != net and o.blocks(path, box, half)]
                hard = _dedupe([o.info for o in hit if o.hard])
                soft = _dedupe([o.info for o in hit if not o.hard])
                variants[name] = (hard, soft)
            best = min(variants, key=lambda k: (len(variants[k][0]), len(variants[k][1])))
            hard, soft = variants[best]
            conn["layers"][layer] = {
                "corridor": best, "hard_count": len(hard), "soft_count": len(soft),
                "hard": [_brief(i) for i in hard], "soft": [_brief(i) for i in soft],
                "corridors": {k: {"hard": len(h), "soft": len(s)} for k, (h, s) in variants.items()},
                "_hard_infos": hard}
        conn["best_layer"] = min(copper, key=lambda n: (conn["layers"][n]["hard_count"],
                                                        conn["layers"][n]["soft_count"]))
        conn["best_layer_hard_count"] = conn["layers"][conn["best_layer"]]["hard_count"]
        connections.append(conn)

    ranking = rank(connections)
    for c in connections:
        for lay in c["layers"].values():
            lay.pop("_hard_infos", None)
    return {
        "version": REPORT_VERSION,
        "method": "straight and L-shaped corridors (net-class track width + 2 x clearance) between the two "
                  "endpoints of each unconnected item, per copper layer; heuristic pointer, not a proof",
        "copper_layers": copper,
        "connections_analyzed": len(connections),
        "unobstructed_connections": sum(1 for c in connections if c.get("best_layer_hard_count") == 0),
        "category_notes": CATEGORY_NOTE,
        "ranking": ranking,
        "top": overall_top(ranking, top),
        "nets": per_net(connections),
        "connections": connections,
    }


def _dedupe(infos):
    seen, out = set(), []
    for i in infos:
        if i["id"] not in seen:
            seen.add(i["id"])
            out.append(i)
    return out


def _group_key(info):
    if info["kind"] == "pad":
        return f"footprint:{info['footprint']}"
    return info["id"]


def rank(connections):
    """Per category, obstacles ordered by the number of open connections they block on each
    connection's best layer (blocks), then on any layer's best corridor (blocks_any_layer)."""
    entries = {}

    def entry(info):
        key = _group_key(info)
        if key not in entries:
            if info["kind"] == "pad":
                base = {"id": key, "kind": "footprint", "category": "footprint", "footprint": info["footprint"],
                        "pads": [], "nets": [], "locked": info.get("locked")}
            else:
                base = {k: v for k, v in info.items() if k not in ("category",)}
                base["category"] = info["category"]
            base.update(blocks=set(), blocks_any_layer=set(), layers=set())
            entries[key] = base
        e = entries[key]
        if info["kind"] == "pad":
            if info["pad"] not in e["pads"]:
                e["pads"].append(info["pad"])
            if info.get("net") and info["net"] not in e["nets"]:
                e["nets"].append(info["net"])
        return e

    for idx, c in enumerate(connections):
        for layer, lay in c.get("layers", {}).items():
            for info in lay["_hard_infos"]:
                e = entry(info)
                e["blocks_any_layer"].add(idx)
                if layer == c["best_layer"]:
                    e["blocks"].add(idx)
                    e["layers"].add(layer)
    out = {cat: [] for cat in CATEGORIES}
    for e in entries.values():
        nets_blocked = sorted({connections[i]["net"] for i in e["blocks_any_layer"]})
        e["nets_blocked"] = nets_blocked
        e["blocks"], e["blocks_any_layer"], e["layers"] = len(e["blocks"]), len(e["blocks_any_layer"]), sorted(e["layers"])
        e["label"] = label(e)
        out.setdefault(e["category"], []).append(e)
    for cat, items in out.items():
        items.sort(key=lambda e: (-e["blocks"], -e["blocks_any_layer"], e["label"]))
    for cat in ("input_copper", "new_copper"):
        by_net = {}
        for e in out[cat]:
            n = by_net.setdefault(e.get("net") or "", {"net": e.get("net") or "", "items": 0, "locked": 0, "blocks": 0})
            n["items"] += 1
            n["locked"] += 1 if e.get("locked") else 0
            n["blocks"] = max(n["blocks"], e["blocks"])
        out[f"{cat}_by_net"] = sorted(by_net.values(), key=lambda n: (-n["blocks"], -n["items"], n["net"]))
    out["truncated"] = {k: len(v) for k, v in out.items() if len(v) > RANKING_LIMIT}
    return {k: v[:RANKING_LIMIT] if isinstance(v, list) else v for k, v in out.items()}


def overall_top(ranking, n):
    allitems = [e for cat in CATEGORIES for e in ranking.get(cat, []) if e["blocks"] or e["blocks_any_layer"]]
    allitems.sort(key=lambda e: (-e["blocks"], -e["blocks_any_layer"], CATEGORIES.index(e["category"]), e["label"]))
    return [{k: e[k] for k in ("id", "category", "kind", "label", "net", "locked", "origin", "blocks",
                               "blocks_any_layer", "nets_blocked") if k in e} for e in allitems[:n]]


def per_net(connections):
    out = {}
    for c in connections:
        if "best_layer" not in c:
            continue
        n = out.setdefault(c["net"], {"connections": 0, "best_layer": None, "hard_blockers_on_best_layer": None,
                                      "blockers": []})
        n["connections"] += 1
        lay = c["layers"][c["best_layer"]]
        if n["hard_blockers_on_best_layer"] is None or lay["hard_count"] > n["hard_blockers_on_best_layer"]:
            # the net's hardest connection decides what the net needs
            n["best_layer"], n["hard_blockers_on_best_layer"] = c["best_layer"], lay["hard_count"]
        for b in lay["hard"]:
            name = f"footprint {b['footprint']}" if b["kind"] == "pad" else b["label"]
            if name not in n["blockers"]:
                n["blockers"].append(name)
    for n in out.values():
        n["blockers"] = n["blockers"][:5]
    return dict(sorted(out.items()))


# ------------------------------------------------------------------- outputs

def summary(report, path):
    """The compact form kept under result.json routing.blocked_by."""
    nets = report["nets"]
    out = {"report": path, "connections_analyzed": report["connections_analyzed"],
           "unobstructed_connections": report["unobstructed_connections"],
           "method": "heuristic corridors; see docs/blocked.md",
           "top": report["top"], "nets": dict(list(nets.items())[:RANKING_LIMIT])}
    if len(nets) > RANKING_LIMIT:
        out["nets_truncated"] = len(nets)
    return out


def render_md(s):
    """RESULT.md lines for routing.blocked_by (nothing when absent)."""
    if not s:
        return []
    if s.get("error"):
        return [f"Blocked-by diagnosis failed: {s['error']}", ""]
    lines = [f"Blocked by (heuristic straight/L corridors, not a proof; details `{s['report']}`):", ""]
    if s.get("top"):
        lines += ["| Blocker | Category | Blocks (best layer) | Blocks (any layer) |", "|---|---|---|---|"]
        lines += [f"| {t['label']} | {t['category']} | {t['blocks']} | {t['blocks_any_layer']} |" for t in s["top"]]
        lines.append("")
    nets = list(s.get("nets", {}).items())
    for net, n in nets[:20]:
        lines.append(f"- `{net}`: {n['connections']} open; best layer {n['best_layer']} with "
                     f"{n['hard_blockers_on_best_layer']} hard blockers"
                     + (f" ({', '.join(n['blockers'])})" if n["blockers"] else ""))
    if len(nets) > 20 or s.get("nets_truncated"):
        lines.append(f"- … {(s.get('nets_truncated') or len(nets)) - 20} more nets in the report")
    if s.get("unobstructed_connections"):
        lines.append(f"- {s['unobstructed_connections']} connection(s) have an unobstructed corridor: the "
                     "obstacle is elsewhere (congestion, detours, the backend's search or budget)")
    return lines + [""]


def write_report(out_dir, drc, facts, ref_facts, scope, top=10, rel=str):
    """Compute the report, write out_dir/blocked-by.json, return the result.json summary.
    A failure is returned as {"error": ...}: diagnosis never changes a verdict."""
    try:
        report = analyze(drc, facts, ref_facts, scope, top)
        path = Path(out_dir) / "blocked-by.json"
        path.write_text(json.dumps(report, indent=1) + "\n")
        return summary(report, rel(path))
    except Exception as e:  # recorded in result.json and RESULT.md, not hidden
        return {"error": f"{type(e).__name__}: {e}"}


def _load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def locate(path):
    """(run_dir, validation_dir, result_json) for a run directory, its validate/ directory,
    or a revalidate*/<stamp> directory."""
    path = Path(path).resolve()
    if (path / "attempt.json").exists():
        return path, path / "validate", path / "result.json"
    if (path / "candidate-drc.json").exists():
        if path.name == "validate":
            return path.parent, path, path.parent / "result.json"
        return path.parent.parent, path, path / "result.json"
    sys.exit(f"{path}: not a routing run or validation directory (no attempt.json or candidate-drc.json)")


def recompute(path, top=10, rel=str, render=None):
    """`kicad-route blocked PATH`: (re)compute blocked-by.json for an existing run or revalidation.
    Updates routing.blocked_by in that directory's result.json (and the run's RESULT.md
    through render). Returns the summary, or None when nothing in scope is open."""
    run_dir, vdir, result_path = locate(path)
    attempt = _load(run_dir / "attempt.json")
    if attempt is None:
        sys.exit(f"{run_dir}: attempt.json missing")
    drc, facts = _load(vdir / "candidate-drc.json"), _load(vdir / "candidate-facts.json")
    if drc is None or facts is None:
        sys.exit(f"{vdir}: candidate-drc.json or candidate-facts.json missing (validation did not finish)")
    result = _load(result_path) or {}
    routing = result.get("routing") or {}
    scope = set(routing["unrouted_nets"]) if "unrouted_nets" in routing else set(attempt["scope"]["nets"])
    ref_facts_path, note = run_dir / attempt["reference_facts"], None
    if result.get("against"):  # validate --against: copper origin relative to the chain's first input
        origin = next((p for p in (Path(result["against"]), Path.cwd() / result["against"])
                       if (p / "attempt.json").exists()), None)
        if origin is not None:
            ref_facts_path = origin / _load(origin / "attempt.json")["reference_facts"]
        else:
            note = f"origin run {result['against']} not found: copper origin is relative to this run's input"
    s = write_report(vdir, drc, facts, _load(ref_facts_path), scope, top, rel)
    if s.get("connections_analyzed") == 0:
        (vdir / "blocked-by.json").unlink(missing_ok=True)
        return None
    if note:
        s["note"] = note
    if result:
        result.setdefault("routing", {})["blocked_by"] = s
        result_path.write_text(json.dumps(result, indent=2) + "\n")
        if render and result.get("kind") == "route" and result_path.parent == run_dir:
            (run_dir / "RESULT.md").write_text(render(result))
    return s
