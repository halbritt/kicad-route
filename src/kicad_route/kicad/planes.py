"""Native plane producer/inspector, run only inside the planes sandbox."""
import argparse
import json
from pathlib import Path
import sys

# Direct invocation with the system interpreter, like the other native scripts.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kicad_route.plane_plan import load_plan, zone_ids


def outline(board, spec):
    import pcbnew as k
    poly = k.SHAPE_POLY_SET()
    if spec == 'board':
        if not board.GetBoardPolygonOutlines(poly, False) or poly.OutlineCount() != 1:
            raise ValueError('one explicit closed board outline required')
    else:
        poly.NewOutline()
        for x, y in spec['polygon_mm']:
            poly.Append(k.FromMM(x), k.FromMM(y))
        if poly.IsSelfIntersecting() or poly.Area() <= 0:
            raise ValueError('plane polygon must be simple with positive area')
    return poly


def planned_zone(board, spec, uid):
    import pcbnew as k
    net = board.FindNet(spec['net'])
    if net is None or net.GetNetCode() <= 0:
        raise ValueError('planned plane net is missing: ' + spec['net'])
    layer = board.GetLayerID(spec['layer'])
    if layer < 0 or not board.IsLayerEnabled(layer):
        raise ValueError('planned plane layer is disabled: ' + spec['layer'])
    zone = k.ZONE(board)
    zone.SetUuidDirect(k.KIID(uid))
    zone.SetZoneName('agent-ee-plane/' + uid)
    zone.SetNet(net)
    zone.SetLayer(layer)
    zone.SetLocalClearance(k.FromMM(spec['clearance_mm']))
    zone.SetMinThickness(k.FromMM(spec['min_width_mm']))
    zone.SetPadConnection({'solid': k.ZONE_CONNECTION_FULL, 'thermal': k.ZONE_CONNECTION_THERMAL,
                           'tht_thermal': k.ZONE_CONNECTION_THT_THERMAL}[spec['thermal']['connection']])
    zone.SetThermalReliefGap(k.FromMM(spec['thermal']['gap_mm']))
    zone.SetThermalReliefSpokeWidth(k.FromMM(spec['thermal']['spoke_width_mm']))
    zone.SetAssignedPriority(spec['priority'])
    zone.SetIslandRemovalMode(k.ISLAND_REMOVAL_MODE_ALWAYS if spec['island_removal'] == 'always'
                             else k.ISLAND_REMOVAL_MODE_NEVER)
    zone.Outline().Append(outline(board, spec['outline']))
    return zone


def inventory(board, plan):
    expected = dict(zip(zone_ids(plan), plan['zones']))
    if any(not z.GetIsRuleArea() for fp in board.GetFootprints() for z in fp.Zones()):
        raise ValueError('unplanned footprint-owned copper zone')
    found = {}
    for zone in board.Zones():
        if zone.GetIsRuleArea():
            continue
        uid = zone.m_Uuid.AsString()
        if (uid in found or uid not in expected or zone.GetZoneName() != 'agent-ee-plane/' + uid or
                zone.GetNetname() != expected[uid]['net'] or
                list(zone.GetLayerSet().Seq()) != [board.GetLayerID(expected[uid]['layer'])]):
            raise ValueError('unplanned copper zone or net')
        found[uid] = zone
    return found


def polygon(poly):
    return [[[(chain.CPoint(i).x, chain.CPoint(i).y) for i in range(chain.PointCount())]
             for chain in [poly.Outline(n), *(poly.Hole(n, h) for h in range(poly.HoleCount(n)))]]
            for n in range(poly.OutlineCount())]


def inspect(board, plan):
    found = inventory(board, plan)
    if set(found) != set(zone_ids(plan)):
        raise ValueError('saved planned zone inventory differs')
    zones = []
    for uid, spec in zip(zone_ids(plan), plan['zones']):
        actual, expected = found[uid], planned_zone(board, spec, uid)
        getters = ('GetLocalClearance', 'GetMinThickness', 'GetPadConnection', 'GetThermalReliefGap',
                   'GetThermalReliefSpokeWidth', 'GetAssignedPriority', 'GetIslandRemovalMode')
        if (any(getattr(actual, g)() != getattr(expected, g)() for g in getters) or
                polygon(actual.Outline()) != polygon(expected.Outline())):
            raise ValueError('saved plane settings or outline differ from plan')
        fill = actual.GetFilledPolysList(actual.GetLayer())
        if not actual.IsFilled() or fill.OutlineCount() == 0 or fill.Area() <= 0:
            raise ValueError('planned plane has no native filled copper')
        zones.append(dict(uuid=uid, net=actual.GetNetname(), layer=board.GetLayerName(actual.GetLayer()),
                          filled_area_mm2=fill.Area()/1e12, fill_polygons=fill.OutlineCount()))
    return zones


def apply(board, plan):
    import pcbnew as k
    existing = inventory(board, plan)
    planned = [planned_zone(board, spec, uid) for uid, spec in zip(zone_ids(plan), plan['zones'])]
    # Keep removed SWIG objects alive until after save; ownership remains native.
    removed = list(existing.values())
    for zone in removed:
        board.Remove(zone)
    for zone in planned:
        board.Add(zone)
    board.BuildConnectivity()
    if not k.ZONE_FILLER(board).Fill(board.Zones()):
        raise ValueError('native zone fill failed')
    return removed


def main():
    import pcbnew as k
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('apply', 'inspect'))
    parser.add_argument('board', type=Path)
    parser.add_argument('plan', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    plan = load_plan(args.plan)
    board = k.LoadBoard(str(args.board))
    if args.command == 'apply':
        removed = apply(board, plan)
        if not k.SaveBoard(str(args.board), board):
            raise ValueError('native plane save failed')
        zones = inventory(board, plan)
        facts = dict(zones_created=len(plan['zones']) - len(removed), zones_total=len(zones))
    else:
        facts = dict(zones=inspect(board, plan))
    facts['kicad_version'] = k.Version()
    args.output.write_text(json.dumps(facts, indent=2) + '\n')


if __name__ == '__main__':
    main()
