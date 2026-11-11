#!/usr/bin/env python3
"""Areas a board closes to new copper without saying so in a rule area: plan them, reserve them, strip them.

    closed.py plan BOARD.kicad_pcb OUT.json       the areas to close, as JSON (board untouched)
    closed.py reserve BOARD.kicad_pcb PLAN.json   add the planned areas as rule areas (in place)
    closed.py strip BOARD.kicad_pcb               remove every kicad-route:closed: rule area (in place)

KiCad's DRC rejects new copper in places that no rule area marks, and a routing backend
that does not model them routes straight through. `plan` finds them, so that they can be
handed to any backend the one way every backend understands: as keep-out rule areas.

Solder-mask apertures that are not pad openings
-----------------------------------------------
KiCad's `solder_mask_bridge` check (pcbnew/drc/drc_test_provider_solder_mask.cpp,
KiCad 10) calls an item a mask aperture when it is on F.Mask or B.Mask and on no copper
layer: a shape, text, text box, dimension, table or barcode drawn on a mask layer, on
the board or in a footprint. Such an aperture has no net of its own. It may expose
copper of one net: the first net found under it becomes its net, and copper of any
other net under it is a bridge. The fill of a pour counts. A copper item is under the
aperture when it comes within the board's solder-mask-to-copper clearance, plus a
shape's own solder mask expansion, of the aperture on the outer copper layer of that
side. The apertures of a footprint that allows solder mask bridges are not checked.

Every aperture gives one window on the outer copper layer of its side, the aperture
grown by `grow`:

    grow = expansion + max(mask-to-copper clearance, untented-via term)

`expansion` is the shape's solder mask expansion (KiCad adds it for shapes only). The
untented-via term is `min web width + 2 * board mask expansion` on a side whose vias
the board does not tent: KiCad tests such a via as an aperture of its own, against the
minimum web width. A shape keeps its exact outline, holes included; anything that
carries text is closed as the convex hull of its outline.

Windows of one layer that touch are merged. Copper the input already has on that layer
must stay reachable, because a rule area shuts out every net, also the one a window
was drawn for. A pad that has a net, a track or a via that lies wholly under a window
can only be reached through it, so that window is left open and listed with the
copper. Any other such copper gets a berth, the copper grown by its net class's
clearance plus track width, and the berth is cut out of the window: the copper is then
reached over its own part outside the window. What is left is closed to tracks and
vias. A ring is closed like any other window: copper inside it is reached from
another layer, or not at all, which is also all that KiCad accepts when the ring
shows a pour.

Not modelled: `solder_mask_expansion` constraints in a .kicad_dru (the shape's and the
board's values are used), and the net an aperture already shows. KiCad allows that one
net to add copper under the aperture; a rule area cannot tell nets apart, so it does
not.

Copper without a net
--------------------
A shape or text drawn on a copper layer without a net -- the tab of a land pattern, a
solder-jumper bridge, a printed antenna, a logo, a board marking -- is foreign copper
to every net: KiCad reports new copper that touches it as `shorting_items` or as
`clearance` with nothing left, and new copper beside it as `clearance`. A backend may
not model it, or may give it to the net of a pad it touches and then route that net
across it. Each such shape is closed on its layer, at its exact outline (text as the
convex hull of its outline; hidden text is not copper). The backend keeps its own
clearance from a rule area, as it does from copper.

A shape can lie over a pad of the input, so that the pad cannot be connected at all
without touching the shape; KiCad reports that pad against the shape on the unrouted
board already. The pad must stay reachable, and new copper should touch the shape as
little as that takes. So copper of the input that overlaps the shape (a pad with a
net, a track, a via) gets its berth, as above, cut out of the area that is closed to
tracks. The whole shape stays closed to vias. When a berth lies wholly on the shape,
there is no way to the copper except across the shape: then the shape is left open to
tracks, and closed to vias only.

Commands
--------
plan: emits JSON only. `areas` lists what to close: `source`, `layer`, `no_tracks`,
`no_vias`, `outline` and `holes`.

reserve: each area becomes a single-layer rule area with those two flags (pads,
footprints and zone fills allowed), named `kicad-route:closed:<source>:<layer>:<n>`.
Refuses a board that already has such areas. strip: removes exactly those areas (by
name prefix) and saves only if it removed something. Every command prints
`CLOSED_JSON: {...}`. Runs with KiCad's pcbnew module. All lengths are integer
nanometres.
"""
import json
import sys
from pathlib import Path

PREFIX = "kicad-route:closed:"


# ------------------------------------------------------------- geometry (pure)

def convex_hull(points):
    """Convex hull of integer points, counter-clockwise, without the closing point
    (Andrew's monotone chain). Fewer than three distinct points come back as they are."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def mask_grow(expansion, mask_to_copper, web_width, board_expansion, vias_tented):
    """How far a mask window reaches beyond its aperture (see the module text)."""
    reach = mask_to_copper
    if not vias_tented:
        reach = max(reach, web_width + 2 * max(0, board_expansion))
    return max(0, expansion + reach)


def boxes_touch(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


# ------------------------------------------------------------- pcbnew helpers

def chain_points(chain):
    return [(int(chain.CPoint(i).x), int(chain.CPoint(i).y)) for i in range(chain.PointCount())]


def polyset_of(pcbnew, outline):
    """A SHAPE_POLY_SET with one outline."""
    ps = pcbnew.SHAPE_POLY_SET()
    ps.NewOutline()
    for x, y in outline:
        ps.Append(int(x), int(y))
    return ps


def component(pcbnew, polyset, index):
    """Outline `index` of a SHAPE_POLY_SET, with its holes, as a SHAPE_POLY_SET of its own."""
    ps = pcbnew.SHAPE_POLY_SET()
    ps.AddOutline(polyset.COutline(index))
    for hole in range(polyset.HoleCount(index)):
        ps.AddHole(polyset.CHole(index, hole))
    return ps


def box_of(ps):
    box = ps.BBox()
    return (int(box.GetLeft()), int(box.GetTop()), int(box.GetRight()), int(box.GetBottom()))


def has_area(ps):
    return ps.OutlineCount() > 0 and ps.Area() > 0


def area_records(source, layer_name, polyset, no_tracks=True, no_vias=True):
    """One record per outline of `polyset` (with its holes): what `reserve` turns into a rule area."""
    out = []
    for i in range(polyset.OutlineCount()):
        outline = chain_points(polyset.COutline(i))
        if len(outline) < 3:
            continue
        holes = [h for h in (chain_points(polyset.CHole(i, j)) for j in range(polyset.HoleCount(i))) if len(h) >= 3]
        out.append({"source": source, "layer": layer_name, "no_tracks": no_tracks, "no_vias": no_vias,
                    "outline": outline, "holes": holes})
    return out


def outline_of(pcbnew, item, layer, grow, max_err):
    """The copper or opening `item` draws on `layer`, grown by `grow`, as a simplified
    SHAPE_POLY_SET, and how it was outlined: "shape" (exact), "hull" (anything that
    carries text) or "box" (KiCad gave no outline)."""
    ps = pcbnew.SHAPE_POLY_SET()
    plain_shape = item.GetClass() == "PCB_SHAPE"
    # Text has two outlines in KiCad: its glyphs, and the box a knockout text fills
    # (which is also all that a dimension, a table cell or a barcode offers here).
    calls = [lambda: item.TransformShapeToPolygon(ps, layer, grow, max_err, pcbnew.ERROR_OUTSIDE)]
    if not plain_shape and hasattr(item, "TransformTextToPolySet"):
        calls.append(lambda: item.TransformTextToPolySet(ps, grow, max_err, pcbnew.ERROR_OUTSIDE))
    for call in calls:
        try:
            call()
        except (AttributeError, RuntimeError, TypeError):
            pass
    ps.Simplify()
    how = "shape"
    if not plain_shape or ps.OutlineCount() == 0:
        points = [p for i in range(ps.OutlineCount()) for p in chain_points(ps.COutline(i))]
        how = "hull"
        if len(set(points)) < 3:  # nothing to outline: the item's bounding box, grown
            box = item.GetBoundingBox()
            x0, y0, x1, y1 = (int(box.GetLeft()) - grow, int(box.GetTop()) - grow,
                              int(box.GetRight()) + grow, int(box.GetBottom()) + grow)
            points, how = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], "box"
        hull = convex_hull(points)
        if len(hull) < 3:
            return pcbnew.SHAPE_POLY_SET(), how
        ps = polyset_of(pcbnew, hull)
        ps.Simplify()
    return ps, how


def drawn_items(board):
    """(owner footprint or None, item) for everything drawn on the board or in a
    footprint, in the order KiCad's DRC visits them."""
    for item in board.GetDrawings():
        yield None, item
        if hasattr(item, "GetCells"):  # a table: KiCad also visits each cell
            for cell in item.GetCells():
                yield None, cell
    for fp in board.GetFootprints():
        for item in fp.GetFields():
            yield fp, item
        for item in fp.GraphicalItems():
            yield fp, item


class InputCopper:
    """Pads that have a net, tracks and vias of the input, per copper layer: each as
    (bounding box of the berth, berth, copper, label). The berth is the copper grown by
    its net class's clearance plus track width, the room its own net needs to reach it."""

    def __init__(self, pcbnew, board, max_err):
        self.pcbnew, self.board, self.max_err = pcbnew, board, max_err
        self.net_settings = board.GetDesignSettings().m_NetSettings
        self.room, self.layers = {}, {}

    def room_of(self, net):
        if net not in self.room:
            cls = self.net_settings.GetEffectiveNetClass(net)
            self.room[net] = int(cls.GetClearance()) + int(cls.GetTrackWidth())
        return self.room[net]

    def on(self, layer):
        if layer not in self.layers:
            pcbnew, found = self.pcbnew, []

            def add(item, label, net):
                copper, berth = pcbnew.SHAPE_POLY_SET(), pcbnew.SHAPE_POLY_SET()
                item.TransformShapeToPolygon(copper, layer, 0, self.max_err, pcbnew.ERROR_INSIDE)
                item.TransformShapeToPolygon(berth, layer, self.room_of(net), self.max_err, pcbnew.ERROR_OUTSIDE)
                copper.Simplify()
                berth.Simplify()
                if copper.OutlineCount() and berth.OutlineCount():
                    found.append((box_of(berth), berth, copper, label))

            for fp in self.board.GetFootprints():
                for pad in fp.Pads():
                    if pad.GetNetCode() > 0 and pad.IsOnLayer(layer):  # a pad without a net needs no track
                        add(pad, {"kind": "pad", "ref": fp.GetReference(), "pad": pad.GetNumber(),
                                  "net": pad.GetNetname()}, pad.GetNetname())
            for track in self.board.GetTracks():
                if track.IsOnLayer(layer):
                    kind = "via" if isinstance(track, pcbnew.PCB_VIA) else "track"
                    add(track, {"kind": kind, "uuid": track.m_Uuid.AsString(), "net": track.GetNetname()},
                        track.GetNetname())
            self.layers[layer] = found
        return self.layers[layer]


# ------------------------------------------------------------- solder-mask windows

def mask_windows(pcbnew, board, copper, max_err):
    """-> (record, areas): the apertures, the windows they merge into and what to close."""
    ds = board.GetDesignSettings()
    mask_to_copper = int(ds.m_SolderMaskToCopperClearance)
    web_width = int(ds.m_SolderMaskMinWidth)
    board_expansion = int(ds.m_SolderMaskExpansion)
    tented = {pcbnew.F_Mask: bool(getattr(ds, "m_TentViasFront", True)),
              pcbnew.B_Mask: bool(getattr(ds, "m_TentViasBack", True))}
    copper_of = {pcbnew.F_Mask: pcbnew.F_Cu, pcbnew.B_Mask: pcbnew.B_Cu}

    apertures, ignored, merged = [], 0, {}
    for owner, item in drawn_items(board):
        layers = item.GetLayerSet()
        if any(pcbnew.IsCopperLayer(layer) for layer in layers.Seq()):
            continue  # copper with a mask opening of its own is not an aperture
        for mask in (m for m in copper_of if layers.Contains(m)):
            if owner is not None and owner.AllowSolderMaskBridges():
                ignored += 1  # KiCad does not check these
                continue
            expansion = int(item.GetSolderMaskExpansion()) if item.GetClass() == "PCB_SHAPE" else 0
            grow = mask_grow(expansion, mask_to_copper, web_width, board_expansion, tented[mask])
            shape, how = outline_of(pcbnew, item, mask, grow, max_err)
            if shape.OutlineCount() == 0:
                continue
            layer = copper_of[mask]
            merged.setdefault(layer, pcbnew.SHAPE_POLY_SET()).BooleanAdd(shape)
            apertures.append({"owner": owner.GetReference() if owner is not None else None,
                              "class": item.GetClass(), "uuid": item.m_Uuid.AsString(),
                              "mask_layer": board.GetLayerName(mask), "layer": board.GetLayerName(layer),
                              "expansion": expansion, "grow": grow, "outlined_as": how,
                              "bbox": list(box_of(shape)), "window": None, "_shape": shape, "_layer": layer})

    windows, areas = [], []
    for layer in sorted(merged):
        union = merged[layer]
        union.Simplify()
        for i in range(union.OutlineCount()):
            window = component(pcbnew, union, i)
            box = box_of(window)
            rec = {"layer": board.GetLayerName(layer), "bbox": list(box), "apertures": 0, "closed": False,
                   "enclosed": [], "kept_reachable": []}
            for a in apertures:
                if a["window"] is None and a["_layer"] == layer and boxes_touch(box, tuple(a["bbox"])):
                    inside = pcbnew.SHAPE_POLY_SET(a["_shape"])
                    inside.BooleanIntersection(window)
                    if has_area(inside):
                        a["window"] = len(windows)
                        rec["apertures"] += 1
            rest = pcbnew.SHAPE_POLY_SET(window)
            for berth_box, berth, own, label in copper.on(layer):
                if not boxes_touch(box, berth_box):
                    continue
                overlap = pcbnew.SHAPE_POLY_SET(berth)
                overlap.BooleanIntersection(window)
                if not has_area(overlap):
                    continue
                outside = pcbnew.SHAPE_POLY_SET(own)
                outside.BooleanSubtract(window)
                if has_area(outside):
                    rec["kept_reachable"].append(label)
                    rest.BooleanSubtract(berth)
                else:
                    rec["enclosed"].append(label)
            windows.append(rec)
            if rec["enclosed"]:
                continue  # left open: its copper can only be reached through it
            rest.Simplify()
            closed = area_records("mask", rec["layer"], rest)
            rec["closed"] = bool(closed)
            areas += closed
    for a in apertures:
        del a["_shape"], a["_layer"]
    record = {"mask_to_copper_clearance": mask_to_copper, "web_width": web_width,
              "mask_expansion": board_expansion,
              "vias_tented": {board.GetLayerName(m): t for m, t in tented.items()},
              "apertures": apertures, "windows": windows,
              "aperture_count": len(apertures), "window_count": len(windows),
              "closed": sum(w["closed"] for w in windows),
              "left_open": sum(bool(w["enclosed"]) for w in windows), "ignored": ignored}
    return record, areas


# ------------------------------------------------------------- copper without a net

def netless_copper(pcbnew, board, copper, max_err):
    """-> (record, areas): the copper graphics without a net and what to close on them."""
    enabled = list(board.GetEnabledLayers().CuStack())
    shapes, to_all, to_vias = [], {}, {}

    def close(merged, layer, polyset):
        merged.setdefault(layer, pcbnew.SHAPE_POLY_SET()).BooleanAdd(polyset)

    for owner, item in drawn_items(board):
        if not item.IsOnCopperLayer():
            continue
        if hasattr(item, "GetNetCode") and item.GetNetCode() > 0:
            continue  # copper of a net: a backend reads it as it reads a track
        if isinstance(item, pcbnew.EDA_TEXT) and not item.IsVisible():
            continue  # a hidden field is not copper
        layers = item.GetLayerSet()
        for layer in (l for l in enabled if layers.Contains(l)):
            shape, how = outline_of(pcbnew, item, layer, 0, max_err)
            if not has_area(shape):
                continue
            box = box_of(shape)
            rec = {"owner": owner.GetReference() if owner is not None else None, "class": item.GetClass(),
                   "uuid": item.m_Uuid.AsString(), "layer": board.GetLayerName(layer), "outlined_as": how,
                   "bbox": list(box), "touching": [], "tracks": "closed"}
            rest, no_way_round = pcbnew.SHAPE_POLY_SET(shape), False
            for berth_box, berth, own, label in copper.on(layer):
                if not boxes_touch(box, berth_box):
                    continue
                overlap = pcbnew.SHAPE_POLY_SET(own)
                overlap.BooleanIntersection(shape)
                if not has_area(overlap):
                    continue  # copper beside the shape is reached without touching it
                rec["touching"].append(label)
                outside = pcbnew.SHAPE_POLY_SET(berth)
                outside.BooleanSubtract(shape)
                if has_area(outside):
                    rest.BooleanSubtract(berth)
                else:
                    no_way_round = True
            shapes.append(rec)
            if no_way_round:
                rec["tracks"] = "open"  # its copper can only be reached across it
                close(to_vias, layer, shape)
                continue
            close(to_all, layer, rest)
            if rec["touching"]:
                rec["tracks"] = "berths open"
                berths = pcbnew.SHAPE_POLY_SET(shape)
                berths.BooleanSubtract(rest)
                close(to_vias, layer, berths)
    # Shapes that touch (the strokes of a logo) close as one area.
    areas = []
    for merged, no_tracks in ((to_all, True), (to_vias, False)):
        for layer in sorted(merged):
            merged[layer].Simplify()
            areas += area_records("copper", board.GetLayerName(layer), merged[layer], no_tracks=no_tracks)
    record = {"shapes": shapes, "shape_count": len(shapes),
              "closed": sum(s["tracks"] == "closed" for s in shapes),
              "berths_open": sum(s["tracks"] == "berths open" for s in shapes),
              "left_open": sum(s["tracks"] == "open" for s in shapes)}
    return record, areas


# ------------------------------------------------------------- commands

def plan(pcbnew, board_path):
    board = pcbnew.LoadBoard(str(board_path))
    max_err = int(board.GetDesignSettings().m_MaxError)
    warnings = []
    if any(z.GetIsRuleArea() and z.GetZoneName().startswith(PREFIX) for z in board.Zones()):
        warnings.append(f"board already carries {PREFIX} rule areas")
    dru = Path(board_path).with_suffix(".kicad_dru")
    if dru.is_file() and "solder_mask_expansion" in dru.read_text(errors="replace"):
        warnings.append(".kicad_dru has solder_mask_expansion constraints: mask windows use the shape and "
                        "board values, not the custom rules")
    copper = InputCopper(pcbnew, board, max_err)
    mask, areas = mask_windows(pcbnew, board, copper, max_err)
    netless, more = netless_copper(pcbnew, board, copper, max_err)
    areas += more
    return {"board": str(board_path), "max_error": max_err, "warnings": warnings, "mask": mask,
            "copper": netless, "areas": areas, "area_count": len(areas)}


def summary(data):
    """What the host logs and records: counts, not geometry."""
    return {"area_count": data["area_count"], "warnings": data["warnings"],
            "mask": {k: data["mask"][k] for k in ("aperture_count", "window_count", "closed", "left_open",
                                                   "ignored")},
            "copper": {k: data["copper"][k] for k in ("shape_count", "closed", "berths_open", "left_open")}}


def reserve(pcbnew, board_path, plan_path):
    board = pcbnew.LoadBoard(str(board_path))
    existing = [z.GetZoneName() for z in board.Zones() if z.GetZoneName().startswith(PREFIX)]
    if existing:
        sys.exit(f"{board_path}: already has {len(existing)} {PREFIX} rule areas; strip them first")
    layers = {board.GetLayerName(l): l for l in board.GetEnabledLayers().CuStack()}
    data = json.loads(Path(plan_path).read_text())
    added = 0
    for n, rec in enumerate(data["areas"]):
        area = pcbnew.ZONE(board)
        area.SetIsRuleArea(True)
        lset = pcbnew.LSET()
        lset.AddLayer(layers[rec["layer"]])
        area.SetLayerSet(lset)
        area.SetDoNotAllowTracks(bool(rec["no_tracks"]))
        area.SetDoNotAllowVias(bool(rec["no_vias"]))
        area.SetDoNotAllowPads(False)
        area.SetDoNotAllowFootprints(False)
        area.SetDoNotAllowZoneFills(False)
        area.SetZoneName(f"{PREFIX}{rec['source']}:{rec['layer']}:{n}")
        outline = area.Outline()
        outline.NewOutline()
        for x, y in rec["outline"]:
            outline.Append(int(x), int(y))
        for hole in rec["holes"]:
            index = outline.NewHole()
            for x, y in hole:
                outline.Append(int(x), int(y), 0, index)
        board.Add(area)
        added += 1
    if added:
        board.Save(str(board_path))
    return {"reserved": added}


def strip(pcbnew, board_path):
    board = pcbnew.LoadBoard(str(board_path))
    ours = [z for z in board.Zones() if z.GetIsRuleArea() and z.GetZoneName().startswith(PREFIX)]
    for z in ours:
        board.Delete(z)  # Remove() leaves the SWIG proxy owning nothing (leak warnings)
    if ours:
        board.Save(str(board_path))
    left = sum(1 for z in board.Zones() if z.GetZoneName().startswith(PREFIX))
    return {"stripped": len(ours), "remaining": left}


def main(argv):
    import pcbnew
    if len(argv) == 3 and argv[0] == "plan":
        data = plan(pcbnew, Path(argv[1]))
        Path(argv[2]).write_text(json.dumps(data, indent=1) + "\n")
        print("CLOSED_JSON: " + json.dumps(summary(data)))
        return 0
    if len(argv) == 3 and argv[0] == "reserve":
        print("CLOSED_JSON: " + json.dumps(reserve(pcbnew, Path(argv[1]), Path(argv[2]))))
        return 0
    if len(argv) == 2 and argv[0] == "strip":
        print("CLOSED_JSON: " + json.dumps(strip(pcbnew, Path(argv[1]))))
        return 0
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
