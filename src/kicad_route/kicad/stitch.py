#!/usr/bin/env python3
"""Stitch a piece of pour that routing ringed off back to its net.

    stitch.py place BEFORE.kicad_pcb ROUTED.kicad_pcb NETS.json   vias placed (ROUTED in place)
    stitch.py remove ROUTED.kicad_pcb UUIDS.json                  remove the named vias again (in place)

New tracks around a pad can ring the pour it sits in off from the rest of that pour: a piece
of copper is left whose only tie to its net is that one pad's thermal spokes. KiCad
(pcbnew/drc/drc_test_provider_zone_connections.cpp, KiCad 10) does not count spokes into such
a piece, and that shows in one of two ways:

  spokes  the pad still has spokes into live copper on that layer, but fewer than the board
          asks for (its minimum spoke count): "Thermal relief connection to zone incomplete
          (N spokes connected to isolated island)". On a two-layer board with ground poured
          on both sides this decided whether a complete routing was clean, with whatever
          order the nets happened to take: a 20 mm^2 piece of the back pour that took two of
          a capacitor pin's three spokes, ringed by two tracks.
  open    the pad has nothing else: no track, no other piece of pour. It is cut from its net,
          and the board has an open connection that it did not have before the routing. This
          one does not need thermal relief: on a two-layer hub board the fan-out of the
          neighbouring pins left two ground pins of a 48-pin package on patches of pour less
          than a square millimetre each.

A pad with all its spokes of a layer in such a piece and a connection elsewhere (a pin that
reaches the pour on the other side) is neither: KiCad says nothing, and nothing is done.

A third kind of piece has nothing of its net on it at all:

  isolated  no pad, no via, no track end. KiCad reports it as "Isolated copper fill" when the
          zone keeps its islands. A whole plane can be one: on a four-layer board whose
          ground pads all sit close together the router joined them with tracks on the
          outside and never touched the ground plane declared inside, and the caller's
          check of that plane failed on two runs of three, whenever the placement happened
          to need no via.

A designer puts a via in the piece, or beside the pin. `place` does the same. After a refill
in memory it looks at every filled piece of every zone with a net, finds those that one pad
alone ties to the net, and for each that is one of the two cases looks for a place for a via
of the net's class (raised to the board's minimum via, hole and annular ring): its centre in
the piece and, on another layer, in copper of the same net that is not cut off itself (a
piece of pour that is tied to the net, or a track of the net, the via then sitting on the
track's centre line), so that the refill joins both to it. For an isolated piece with no such
place (a plane under pads that short tracks join), the via goes beside a surface pad of the
net, within LEG_MAX_MM, with a track of the class width from the pad to it; the class clearance (and never less than the board's) kept
from all copper of other nets, from every pad of another net and from every other hole on
every layer; off the copper of the pads of its own net; the board's edge clearance kept; and
no rule area against vias (the corridors that --reserve-spokes holds for a pad's own spokes
do not count). The place with the most of the piece around it wins. A piece with no such
place is left and listed with the reasons. Only nets named in NETS.json (the run's scope) get
a via, and for the first two cases only for a pad that BEFORE did not have in the same state
already: what the input brought is the input's. (An isolated piece of a net in scope is the
routing's to join whatever the input looked like: on an unrouted board every plane is one.) ROUTED is saved only when a via was placed, without the refill.

The caller then asks KiCad's DRC, and `remove` takes out any via a new finding names.
Runs with KiCad's pcbnew module.
"""
import json
import math
import sys

import spokes

STEP_MM = 0.25           # raster of the places tried
MIN_STEP_MM = 0.05       # ... in a piece too small for that
RING = 12                # points on the via's rim that must be in copper too
EDGE_MM = 0.05           # the via keeps this much copper around it in a piece
RIM_STEP_MM = 0.02       # spokes are counted along the pad's outline at this pitch
LEG_MAX_MM = 2.5         # a via beside a pad is joined to it by a track no longer than this
LEG_STEP_MM = 0.25       # ... tried at these distances, in LEG_ANGLES directions
LEG_ANGLES = 16


def _via_size(board, net_name, pcbnew):
    settings = board.GetDesignSettings()
    netclass = settings.m_NetSettings.GetEffectiveNetClass(net_name)
    hole = max(netclass.GetViaDrill(), settings.m_MinThroughDrill)
    diameter = max(netclass.GetViaDiameter(), settings.m_ViasMinSize, hole + 2 * settings.m_ViasMinAnnularWidth)
    return int(diameter), int(hole), int(max(netclass.GetClearance(), settings.m_MinClearance))


def _pieces(zone, layer, pcbnew):
    fill = zone.GetFilledPolysList(layer)
    for index in range(fill.OutlineCount()):
        piece = pcbnew.SHAPE_POLY_SET()
        piece.AddOutline(fill.Outline(index))
        for hole in range(fill.HoleCount(index)):
            piece.AddHole(fill.Hole(index, hole), -1)
        yield piece


def _thermal(zone, pad, pcbnew):
    """Whether this pad joins this zone by thermal relief (the pad's setting, else the zone's)."""
    connection = pad.GetLocalZoneConnection()
    if connection == pcbnew.ZONE_CONNECTION_INHERITED:
        connection = zone.GetPadConnection()
    if connection == pcbnew.ZONE_CONNECTION_THT_THERMAL:
        return pad.GetDrillSize().x > 0
    return connection == pcbnew.ZONE_CONNECTION_THERMAL


def _disc_inside(piece, centre, radius, pcbnew):
    if not piece.Contains(centre):
        return False
    for k in range(RING):
        angle = 2 * math.pi * k / RING
        rim = pcbnew.VECTOR2I(int(centre.x + radius * math.cos(angle)), int(centre.y + radius * math.sin(angle)))
        if not piece.Contains(rim):
            return False
    return True


def _segment_distance(p, a, b):
    dx, dy = b.x - a.x, b.y - a.y
    if dx == 0 and dy == 0:
        return math.hypot(p.x - a.x, p.y - a.y)
    t = max(0.0, min(1.0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy))


def _gap(zone, pad):
    try:
        return zone.GetThermalReliefGap(pad)
    except TypeError:
        return zone.GetThermalReliefGap()


def _spokes(piece, zone, pad, layer, pcbnew):
    """How many of the pad's spokes run into this piece. KiCad counts where the pad's outline,
    grown by half the thermal gap, crosses the piece's edge, two crossings to a spoke; this
    walks the same outline and counts the stretches that lie in the piece."""
    grown = pcbnew.SHAPE_POLY_SET()
    pad.TransformShapeToPolygon(grown, layer, _gap(zone, pad) // 2, pcbnew.FromMM(0.005), pcbnew.ERROR_OUTSIDE)
    if not grown.OutlineCount():
        return 0
    rim, step, inside = grown.Outline(0), pcbnew.FromMM(RIM_STEP_MM), []
    count = rim.PointCount()
    for i in range(count):
        a, b = rim.CPoint(i), rim.CPoint((i + 1) % count)
        parts = max(1, int(math.hypot(b.x - a.x, b.y - a.y) // step))
        for k in range(parts):
            inside.append(piece.Contains(pcbnew.VECTOR2I(int(a.x + (b.x - a.x) * k / parts), int(a.y + (b.y - a.y) * k / parts))))
    return sum(1 for i, here in enumerate(inside) if here and not inside[i - 1])


def survey(board, pcbnew):
    """Every filled piece of every zone with a net, after a refill in memory, with what of its
    net sits on it: [{zone, layer, net, piece, pads [(pad, by thermal relief)], others, live, area}].
    A piece is live unless one pad is all that ties it to its net."""
    zones = [z for z in board.Zones() if not z.GetIsRuleArea() and z.GetNetname()]
    if not zones:
        return []
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    copper = [layer for layer in board.GetEnabledLayers().CuStack()]
    pads = [pad for footprint in board.GetFootprints() for pad in footprint.Pads()]
    tracks = list(board.GetTracks())
    pieces = []
    for zone in zones:
        for layer in copper:
            if not zone.IsOnLayer(layer):
                continue
            net = zone.GetNetname()
            for piece in _pieces(zone, layer, pcbnew):
                tied = []
                for pad in pads:
                    if pad.GetNetname() != net or not pad.IsOnLayer(layer):
                        continue
                    reach = max(pad.GetSize(layer).x, pad.GetSize(layer).y) // 2 + zone.GetThermalReliefGap() + pcbnew.FromMM(0.05)
                    if piece.Collide(pad.GetPosition(), reach):
                        tied.append((pad, _thermal(zone, pad, pcbnew)))
                others = 0
                for item in tracks:
                    if item.GetNetname() != net:
                        continue
                    if isinstance(item, pcbnew.PCB_VIA):
                        others += item.IsOnLayer(layer) and piece.Contains(item.GetPosition())
                    elif item.GetLayer() == layer:
                        others += piece.Contains(item.GetStart()) or piece.Contains(item.GetEnd())
                pieces.append({"zone": zone, "layer": layer, "net": net, "piece": piece, "pads": tied, "others": others,
                               "live": not (len(tied) == 1 and not others),
                               "attached": len(tied) + others, "area": abs(piece.Outline(0).Area())})
    return pieces


def cut_off(board, pieces, pcbnew):
    """The pieces to tie back to their net, each with "case" and "pad": the ones that one pad alone
    ties to its net and that count against that pad ("spokes", "open"), and the ones with nothing
    of their net on them ("isolated", pad None). See the top of this file. `pieces` is survey(board)."""
    minimum = board.GetDesignSettings().m_MinResolvedSpokes
    connectivity = board.GetConnectivity()
    found = []
    for cut in pieces:
        if cut["live"] or not cut["pads"]:
            continue
        (pad, thermal), layer = cut["pads"][0], cut["layer"]
        same_pad = [p for p in pieces if p is not cut and any(q.m_Uuid == pad.m_Uuid for q, _ in p["pads"])]
        live = sum(_spokes(p["piece"], p["zone"], pad, layer, pcbnew) for p in same_pad
                   if p["live"] and p["layer"] == layer and p["zone"].m_Uuid == cut["zone"].m_Uuid) if thermal else 0
        if thermal and minimum > 0 and 0 < live < minimum:
            found.append(dict(cut, pad=pad, case="spokes", live_spokes=live,
                              spokes=_spokes(cut["piece"], cut["zone"], pad, layer, pcbnew)))
        elif not any(p["live"] for p in same_pad) and not len(connectivity.GetConnectedTracks(pad)):
            found.append(dict(cut, pad=pad, case="open"))
    found += [dict(cut, pad=None, case="isolated") for cut in pieces if not cut["attached"]]
    return found


def place(before_path, routed_path, nets_path, pcbnew):
    with open(nets_path) as fh:
        scope = set(json.load(fh))
    # what the input already had in that state: not this run's doing, not this run's to change
    earlier = pcbnew.LoadBoard(before_path)
    already = {(cut["pad"].m_Uuid.AsString(), earlier.GetLayerName(cut["layer"]), cut["case"])
               for cut in cut_off(earlier, survey(earlier, pcbnew), pcbnew) if cut["pad"] is not None}
    board = pcbnew.LoadBoard(routed_path)
    result = {"pieces_cut_off": 0, "vias": [], "left": []}
    pieces = survey(board, pcbnew)
    copper = [layer for layer in board.GetEnabledLayers().CuStack()]
    settings = board.GetDesignSettings()
    pads = [pad for footprint in board.GetFootprints() for pad in footprint.Pads()]
    tracks = list(board.GetTracks())
    # A corridor that --reserve-spokes keeps free for a pad's own spokes is still on the board here
    # (it comes off the backend's output afterwards) and is no reason against copper of that pad's net.
    no_via = [z for z in board.Zones() if z.GetIsRuleArea() and z.GetDoNotAllowVias()
              and not z.GetZoneName().startswith(spokes.PREFIX)]
    edge = board.GetBoardEdgesBoundingBox()
    outline = pcbnew.SHAPE_POLY_SET()
    try:
        if not board.GetBoardPolygonOutlines(outline) or not outline.OutlineCount():
            outline = None
    except Exception:                        # no closed outline: the bounding box has to do
        outline = None
    from_edge = settings.m_CopperEdgeClearance

    def blocked(centre, radius, hole, clearance, net):
        """Why a via here is not allowed on some layer, or None."""
        if not edge.Contains(centre) or (outline is not None and not _disc_inside(outline, centre, radius + from_edge, pcbnew)):
            return "board edge"
        for area in no_via:
            if area.Outline().Contains(centre) or area.Outline().Collide(centre, radius):
                return "rule area"
        for pad in pads:
            # the class clearance from a pad of another net; off the copper of a pad of its own net
            # (a via in a pad is the designer's decision, not this step's)
            apart = clearance if pad.GetNetname() != net else pcbnew.FromMM(EDGE_MM)
            reach = radius + apart + max(pad.GetSize(pcbnew.F_Cu).x, pad.GetSize(pcbnew.F_Cu).y) // 2
            if (pad.GetPosition() - centre).EuclideanNorm() > reach + pcbnew.FromMM(1.0):
                continue
            for layer in copper:
                if pad.IsOnLayer(layer) and pad.GetEffectivePolygon(layer, pcbnew.ERROR_INSIDE).Collide(centre, radius + apart):
                    return "pad"
            if pad.GetDrillSize().x > 0 and (pad.GetPosition() - centre).EuclideanNorm() < (
                    pad.GetDrillSize().x // 2 + hole // 2 + max(settings.m_HoleToHoleMin, settings.m_HoleClearance)):
                return "hole"
        for item in tracks:
            if isinstance(item, pcbnew.PCB_VIA):
                gap = (item.GetPosition() - centre).EuclideanNorm()
                if gap < item.GetDrillValue() // 2 + hole // 2 + max(settings.m_HoleToHoleMin, settings.m_HoleClearance):
                    return "hole"
                if item.GetNetname() != net and gap < item.GetWidth(pcbnew.F_Cu) // 2 + radius + clearance:
                    return "via"
            elif item.GetNetname() != net:
                if _segment_distance(centre, item.GetStart(), item.GetEnd()) < item.GetWidth() // 2 + radius + clearance:
                    return "track"
        return None

    no_track = [z for z in board.Zones() if z.GetIsRuleArea() and z.GetDoNotAllowTracks()
                and not z.GetZoneName().startswith(spokes.PREFIX)]

    def leg_blocked(start, end, width, clearance, net, on_layer):
        """Why a track of `net` from start to end on `on_layer` is not allowed, or None. The track
        leaves a pad of its own net, so that net's pads are no obstacle."""
        length = math.hypot(end.x - start.x, end.y - start.y)
        parts = max(1, int(length // pcbnew.FromMM(0.05)))
        half = width // 2
        for k in range(parts + 1):
            point = pcbnew.VECTOR2I(int(start.x + (end.x - start.x) * k / parts), int(start.y + (end.y - start.y) * k / parts))
            if outline is not None and not _disc_inside(outline, point, half + from_edge, pcbnew):
                return "board edge"
            for area in no_track:
                if area.IsOnLayer(on_layer) and (area.Outline().Contains(point) or area.Outline().Collide(point, half)):
                    return "rule area"
            for other in pads:
                if other.GetNetname() == net or not other.IsOnLayer(on_layer):
                    continue
                if (other.GetPosition() - point).EuclideanNorm() > half + clearance + pcbnew.FromMM(4.0):
                    continue
                if other.GetEffectivePolygon(on_layer, pcbnew.ERROR_INSIDE).Collide(point, half + clearance):
                    return "pad"
            for item in tracks:
                if item.GetNetname() == net:
                    continue
                if isinstance(item, pcbnew.PCB_VIA):
                    if (item.GetPosition() - point).EuclideanNorm() < item.GetWidth(pcbnew.F_Cu) // 2 + half + clearance:
                        return "via"
                elif item.GetLayer() == on_layer:
                    if _segment_distance(point, item.GetStart(), item.GetEnd()) < item.GetWidth() // 2 + half + clearance:
                        return "track"
        return None

    for cut in cut_off(board, pieces, pcbnew):
        net, layer, piece, pad = cut["net"], cut["layer"], cut["piece"], cut["pad"]
        if net not in scope:
            continue
        if pad is not None and (pad.m_Uuid.AsString(), board.GetLayerName(layer), cut["case"]) in already:
            continue
        result["pieces_cut_off"] += 1
        diameter, hole, clearance = _via_size(board, net, pcbnew)
        partners = [p for p in pieces if p["net"] == net and p["layer"] != layer and p["live"] and p["attached"]]
        own_tracks = [t for t in tracks if not isinstance(t, pcbnew.PCB_VIA) and t.GetNetname() == net and t.GetLayer() != layer]
        box = piece.BBox()
        # a pin's patch of pour can be a millimetre across: the raster follows the piece
        step = max(pcbnew.FromMM(MIN_STEP_MM), min(pcbnew.FromMM(STEP_MM), min(box.GetWidth(), box.GetHeight()) // 8))
        best = None
        why = {}                             # places in the piece that were turned down, by reason

        def places():
            """(centre, what the via lands on, on another layer): the raster over the piece for a
            pour of the net, then the centre lines of the net's tracks on other layers."""
            y = box.GetTop()
            while y <= box.GetBottom():
                x = box.GetLeft()
                while x <= box.GetRight():
                    centre = pcbnew.VECTOR2I(int(x), int(y))
                    x += step
                    if piece.Contains(centre):
                        yield centre, "pour" if any(p["piece"].Contains(centre) for p in partners) else None
                y += step
            for track in own_tracks:
                a, b = track.GetStart(), track.GetEnd()
                if not box.Intersects(track.GetBoundingBox()):
                    continue
                parts = max(1, int(math.hypot(b.x - a.x, b.y - a.y) // step))
                for k in range(parts + 1):
                    centre = pcbnew.VECTOR2I(int(a.x + (b.x - a.x) * k / parts), int(a.y + (b.y - a.y) * k / parts))
                    if piece.Contains(centre):
                        yield centre, "track"

        for centre, onto in places():
            # The via's centre in the piece and in live copper of the net on another layer: the
            # refill joins both to it. Its rim may reach past the piece, to where the class
            # clearance still holds (a pour keeps its own, wider clearance).
            if onto is None:
                why["no copper of the net on another layer"] = why.get("no copper of the net on another layer", 0) + 1
                continue
            reason = blocked(centre, diameter // 2, hole, clearance, net)
            if reason:
                why[reason] = why.get(reason, 0) + 1
                continue
            # the place with the most of the piece around it
            room = min(abs(centre.x - box.GetLeft()), abs(box.GetRight() - centre.x),
                       abs(centre.y - box.GetTop()), abs(box.GetBottom() - centre.y))
            if best is None or room > best[0]:
                best = (room, centre, onto)
        where = {"net": net, "layer": board.GetLayerName(layer), "area_mm2": round(cut["area"] / 1e12, 3),
                 "pads": [f"{pad.GetParentFootprint().GetReference()}.{pad.GetNumber()}"] if pad is not None else [],
                 "case": cut["case"]}
        if cut["case"] == "spokes":
            where.update(spokes=cut["spokes"], live_spokes=cut["live_spokes"])
        leg = None
        if best is None and cut["case"] == "isolated":
            # Nothing of the net crosses the piece on another layer where a via fits (a plane under
            # pads that short tracks join). A designer puts the via beside one of the net's pads and
            # draws the few millimetres between them.
            netclass = settings.m_NetSettings.GetEffectiveNetClass(net)
            width = int(max(netclass.GetTrackWidth(), settings.m_TrackMinWidth))
            for own in pads:
                if own.GetNetname() != net or own.GetDrillSize().x > 0:
                    continue
                for on_layer in copper:
                    if on_layer == layer or not own.IsOnLayer(on_layer):
                        continue
                    start = own.GetPosition()
                    size = own.GetSize(on_layer)
                    reach = int(math.hypot(size.x, size.y) / 2) + diameter // 2 + pcbnew.FromMM(EDGE_MM)
                    while reach <= pcbnew.FromMM(LEG_MAX_MM) and (leg is None or reach < leg[0]):
                        for k in range(LEG_ANGLES):
                            angle = 2 * math.pi * k / LEG_ANGLES
                            centre = pcbnew.VECTOR2I(int(start.x + reach * math.cos(angle)), int(start.y + reach * math.sin(angle)))
                            if not piece.Contains(centre):
                                continue
                            reason = blocked(centre, diameter // 2, hole, clearance, net)
                            if reason is None:
                                reason = leg_blocked(start, centre, width, clearance, net, on_layer)
                                reason = reason and "track to the via: " + reason
                            if reason:
                                why[reason] = why.get(reason, 0) + 1
                                continue
                            leg = (reach, centre, start, on_layer, width, own)
                            break
                        reach += pcbnew.FromMM(LEG_STEP_MM)
            if leg is not None:
                best = (0, leg[1], "pad")
        if best is None:
            # no room at all for a via of this size, or the reasons the places with room were turned down
            result["left"].append(dict(where, via_mm=pcbnew.ToMM(diameter), why=why or {"no room in the piece": 1}))
            continue
        via = pcbnew.PCB_VIA(board)          # on the judged copy: the next piece sees this via
        via.SetPosition(best[1])
        via.SetWidth(pcbnew.F_Cu, diameter)
        via.SetDrill(hole)
        via.SetNetCode(cut["zone"].GetNetCode())
        via.SetLayerPair(copper[0], copper[-1])
        board.Add(via)
        tracks.append(via)
        for known in pieces:                 # the piece is live from here on, as a partner for the next
            if known["piece"] is piece:
                known["live"] = True
        row = dict(where, onto=best[2], x_mm=round(pcbnew.ToMM(best[1].x), 4), y_mm=round(pcbnew.ToMM(best[1].y), 4),
                   diameter_mm=pcbnew.ToMM(diameter), hole_mm=pcbnew.ToMM(hole))
        if leg is not None:
            track = pcbnew.PCB_TRACK(board)  # on the judged copy too: the next piece keeps clear of it
            track.SetStart(leg[2]); track.SetEnd(leg[1]); track.SetLayer(leg[3]); track.SetWidth(leg[4])
            track.SetNetCode(cut["zone"].GetNetCode())
            board.Add(track)
            tracks.append(track)
            row["leg"] = {"pad": f"{leg[5].GetParentFootprint().GetReference()}.{leg[5].GetNumber()}",
                          "layer": board.GetLayerName(leg[3]), "width_mm": pcbnew.ToMM(leg[4]),
                          "from_mm": [round(pcbnew.ToMM(leg[2].x), 4), round(pcbnew.ToMM(leg[2].y), 4)]}
        result["vias"].append(row)
    if result["vias"]:
        # as the backend wrote it plus the vias: the refill made here is not saved
        saved = pcbnew.LoadBoard(routed_path)
        layers = [layer for layer in saved.GetEnabledLayers().CuStack()]
        for row in result["vias"]:
            via = pcbnew.PCB_VIA(saved)
            via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(row["x_mm"]), pcbnew.FromMM(row["y_mm"])))
            via.SetWidth(pcbnew.F_Cu, pcbnew.FromMM(row["diameter_mm"]))
            via.SetDrill(pcbnew.FromMM(row["hole_mm"]))
            via.SetNetCode(saved.GetNetcodeFromNetname(row["net"]))
            via.SetLayerPair(layers[0], layers[-1])
            saved.Add(via)
            row["uuid"] = via.m_Uuid.AsString()
            if row.get("leg"):
                track = pcbnew.PCB_TRACK(saved)
                track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(row["leg"]["from_mm"][0]), pcbnew.FromMM(row["leg"]["from_mm"][1])))
                track.SetEnd(via.GetPosition())
                track.SetLayer(saved.GetLayerID(row["leg"]["layer"]))
                track.SetWidth(pcbnew.FromMM(row["leg"]["width_mm"]))
                track.SetNetCode(saved.GetNetcodeFromNetname(row["net"]))
                saved.Add(track)
                row["leg"]["uuid"] = track.m_Uuid.AsString()
        pcbnew.SaveBoard(routed_path, saved, aSkipSettings=True)
    return result


def remove(routed_path, uuids_path, pcbnew):
    with open(uuids_path) as fh:
        wanted = set(json.load(fh))
    board = pcbnew.LoadBoard(routed_path)
    gone = [item for item in board.GetTracks() if item.m_Uuid.AsString() in wanted]
    for item in gone:
        board.RemoveNative(item)
    if gone:
        pcbnew.SaveBoard(routed_path, board, aSkipSettings=True)
    return {"removed": len(gone)}


def main(argv):
    import pcbnew
    if len(argv) == 4 and argv[0] == "place":
        result = place(argv[1], argv[2], argv[3], pcbnew)
    elif len(argv) == 3 and argv[0] == "remove":
        result = remove(argv[1], argv[2], pcbnew)
    else:
        sys.exit(__doc__)
    print("STITCH_JSON: " + json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
