"""Opt-in nominal pair synthesis and saved-native geometry checks.

One outer microstrip or symmetric inner stripline layer, with each adjacent
reference verified from saved fill. No via, coated-line, coplanar or field-solver claim.
"""
import argparse
import math
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kicad_route import common


def settings(args):
    values = {name: getattr(args, flag, None) for name, flag in (
        ('target_ohm', 'pair_impedance'), ('tolerance_percent', 'impedance_tolerance'),
        ('reference_net', 'impedance_reference'), ('max_uncoupled_mm', 'impedance_uncoupled'))}
    if all(value is None for value in values.values()):
        return None
    if any(value is None for value in values.values()):
        raise ValueError('pair impedance requires target, tolerance, reference net and uncoupled-length limit')
    if getattr(args, 'diff_pairs', None) != 'auto':
        raise ValueError('pair impedance requires --diff-pairs auto')
    for name in ('target_ohm', 'tolerance_percent', 'max_uncoupled_mm'):
        if not math.isfinite(values[name]) or values[name] <= 0:
            raise ValueError('pair impedance numbers must be finite and positive')
    if values['tolerance_percent'] >= 100 or not values['reference_net'].strip():
        raise ValueError('pair impedance requires tolerance below 100 percent and a named reference net')
    return values


def run(board, output, request, api, *, check=False):
    request_path = output.with_suffix('.request.json')
    common.write_json(request_path, request)
    argv = [api.pcbnew_python(), '-B', Path(__file__), board, request_path, output]
    if check:
        argv.append('--check')
    rec = common.run_cmd(argv, board.parent, output.with_suffix('.log'), timeout=120)
    report = common.load_json(output) if output.exists() else None
    if rec['exit_code'] != 0 or not isinstance(report, dict):
        raise ValueError(f'impedance native operation failed (see {rec["log"]})')
    return report


def model(board_path, layer):
    from kicad_route import engine
    from kicad_route.kicad.facts import setup_block, find
    sys.path.insert(0, str(engine.ROOT / 'py_router'))
    from kicad_parser import parse_kicad_pcb
    from impedance import calculate_width_for_impedance, calculate_impedance_for_layer
    setup = setup_block(board_path.read_text())
    stack = find(setup or [], 'stackup')
    if not stack:
        raise ValueError('pair impedance requires an explicit native stackup')
    rows = [row for row in stack[1:] if isinstance(row, list) and row[0] == 'layer']
    copper = [row for row in rows if find(row, 'type') == ['type', 'copper']]
    if layer not in [row[1] for row in copper]:
        raise ValueError('pair impedance requires an explicit copper signal layer')
    index = next(i for i, row in enumerate(rows) if row[1] == layer)
    directions = [1] if layer == copper[0][1] else [-1] if layer == copper[-1][1] else [-1, 1]
    dielectrics, references = [], []
    for direction in directions:
        if not 0 <= index + 2 * direction < len(rows):
            raise ValueError('pair impedance requires adjacent reference copper')
        dielectric, reference = rows[index + direction], rows[index + 2 * direction]
        if find(dielectric, 'type') not in (['type', 'core'], ['type', 'prepreg']) or reference not in copper:
            raise ValueError('pair impedance requires one explicit dielectric to each adjacent reference copper')
        dielectrics.append(dielectric);references.append(reference[1])
    for row, field in [(rows[index], 'thickness'),
                       *((d, f) for d in dielectrics for f in ('thickness', 'epsilon_r'))]:
        value = find(row, field)
        if (not value or len(value) != 2 or not math.isfinite(float(value[1])) or float(value[1]) <= 0
                or sum(isinstance(v, list) and v[0] == field for v in row) != 1):
            raise ValueError('pair impedance requires positive explicit copper/dielectric thickness and epsilon_r')
    if len(dielectrics) == 2:
        h = [float(find(d, 'thickness')[1]) for d in dielectrics]
        er = [float(find(d, 'epsilon_r')[1]) for d in dielectrics]
        if abs(h[0] - h[1]) > 1.000001e-6 or abs(er[0] - er[1]) > 1e-9:
            raise ValueError('qualified stripline impedance requires symmetric dielectric heights and equal epsilon_r')
    pcb = parse_kicad_pcb(str(board_path))
    return pcb, references, calculate_width_for_impedance, calculate_impedance_for_layer


def plan(board_path, request):
    layers = request['layers']
    if not isinstance(layers, list) or len(layers) != 1:
        raise ValueError('qualified impedance synthesis requires exactly one explicit pair layer')
    if not request['pairs']:
        raise ValueError('pair impedance requires at least one detected pair')
    layer = layers[0]
    pcb, references, solve, calculate = model(board_path, layer)
    spec = request['requirement']
    if spec['reference_net'] not in {net.name for net in pcb.nets.values()}:
        raise ValueError('declared impedance reference net is absent')
    planned = []
    for pair in request['pairs']:
        solved = solve(pcb, layer, spec['target_ohm'], pair['gap_mm'], is_differential=True)
        width = solved.get('calculated_width_mm')
        if width is None or not math.isfinite(width) or width <= 0:
            raise ValueError('native stackup did not yield a positive impedance width')
        # The native class width remains a floor; never weaken an authored size.
        width = round(max(width, pair['width_mm']) * 1e6) / 1e6
        actual = calculate(pcb, layer, width, pair['gap_mm']).get('zdiff')
        if actual is None or not math.isfinite(actual) or abs(actual / spec['target_ohm'] - 1) * 100 > spec['tolerance_percent']:
            raise ValueError('target impedance cannot be met above the native pair-width floor')
        planned.append({**pair, 'width_mm': width, 'native_width_mm': pair['width_mm'],
                        'nominal_impedance_ohm': actual})
    return {'status': 'PASS', 'board_sha256': common.sha256(board_path),
            'layer': layer, 'reference_layers': references,
            **({'reference_layer': references[0]} if len(references) == 1 else {}),
            'requirement': spec, 'pairs': planned,
            'model': ('owned KRT nominal uncoated microstrip' if len(references) == 1 else
                      'owned KRT nominal symmetric homogeneous stripline') +
                     '; independent saved geometry and native fill grade required'}


def _interval_length(intervals):
    end, total = 0.0, 0.0
    for lo, hi in sorted(intervals):
        total += max(0.0, hi - max(lo, end))
        end = max(end, hi)
    return total


def check(board_path, planned):
    import pcbnew as k
    if not planned.get('pairs'):
        raise ValueError('impedance check has no required pairs')
    layer_name = planned['layer']
    pcb, references, _, calculate = model(board_path, layer_name)
    if references != planned.get('reference_layers', [planned.get('reference_layer')]):
        raise ValueError('native impedance reference layer changed')
    spec = planned['requirement']
    board = k.LoadBoard(str(board_path))
    layer = board.GetLayerID(layer_name)
    defects, results = [], []
    planes = {}
    for reference in references:
        ref_layer = board.GetLayerID(reference)
        ground = k.SHAPE_POLY_SET()
        zones = []
        for zone in board.Zones():
            if zone.GetIsRuleArea():
                continue
            if zone.IsOnLayer(layer):
                defects.append('coplanar pours require a separate impedance model')
            if zone.IsOnLayer(ref_layer) and str(zone.GetNetname()) == spec['reference_net']:
                if not zone.IsFilled() or not zone.HasFilledPolysForLayer(ref_layer):
                    defects.append(reference + ': reference plane has no saved native fill')
                else:
                    ground.Append(zone.GetFilledPolysList(ref_layer));zones.append(zone)
        ground.Simplify()
        if not ground.OutlineCount():
            defects.append(reference + ': declared filled reference is missing')
        planes[reference] = (ground, zones)
    for pair in planned['pairs']:
        members = {name: [t for t in board.GetTracks() if str(t.GetNetname()) == name]
                   for name in (pair['p'], pair['n'])}
        for name, tracks in members.items():
            other = members[pair['n'] if name == pair['p'] else pair['p']]
            if not tracks:
                defects.append(name + ': no saved copper')
            pads = [pad for fp in board.GetFootprints() for pad in fp.Pads()
                    if str(pad.GetNetname()) == name and pad.IsOnLayer(layer)]
            antipads = {}
            for reference, (ground, zones) in planes.items():
                ref_layer = board.GetLayerID(reference)
                openings = k.SHAPE_POLY_SET()
                if len(references) == 2 and zones:
                    # Only the native zone's explicit clearance around this net's
                    # own PTH pads may interrupt an inner reference. Use the
                    # smallest declared clearance; larger rule-driven openings
                    # fail closed rather than licensing an arbitrary plane slot.
                    clearance = min(int(z.GetLocalClearance() or 0) for z in zones)
                    # Native fill and this pad envelope use different polygon
                    # tessellations. Bound that numerical difference explicitly.
                    clearance += int(board.GetDesignSettings().m_MaxError) + 1000
                    for pad in pads:
                        if pad.GetAttribute() == k.PAD_ATTRIB_PTH and pad.IsOnLayer(ref_layer):
                            pad.TransformShapeToPolygon(openings, ref_layer, clearance, 1000, k.ERROR_OUTSIDE)
                    openings.Simplify()
                antipads[reference] = openings
            antipad_area = {reference: 0.0 for reference in references}
            total = covered = 0.0
            zs, gaps, escapes = [], [], []
            for track in tracks:
                if track.GetClass() != 'PCB_TRACK' or track.GetLayer() != layer:
                    defects.append(name + ': via, arc or different-layer geometry needs a qualified model')
                    continue
                if track.GetWidth() < round(pair['native_width_mm'] * 1e6) - 1:
                    defects.append(name + ': saved width is below the native pair floor')
                for reference in references:
                    shape = k.SHAPE_POLY_SET()
                    track.TransformShapeToPolygon(shape, layer, 0, 1000, k.ERROR_OUTSIDE)
                    # Measure the actual exception, not the whole pad envelope.
                    shape.BooleanSubtract(planes[reference][0]);shape.Simplify()
                    antipad_area[reference] += shape.Area() / 1e12
                    shape.BooleanSubtract(antipads[reference]);shape.Simplify()
                    if shape.OutlineCount():
                        defects.append(name + ': saved trace copper is not fully over the declared filled reference ' + reference)
                a, b = track.GetStart(), track.GetEnd()
                dx, dy = b.x - a.x, b.y - a.y
                length = math.hypot(dx, dy)
                if length == 0:
                    defects.append(name + ': zero-length track')
                    continue
                total += length / 1e6
                ux, uy = dx / length, dy / length
                intervals = []
                for partner in other:
                    if partner.GetClass() != 'PCB_TRACK' or partner.GetLayer() != layer:
                        continue
                    c, d = partner.GetStart(), partner.GetEnd()
                    px, py = d.x - c.x, d.y - c.y
                    if abs(ux * py - uy * px) > 1:  # at most one native nm off parallel
                        continue
                    ends = [ux * (v.x - a.x) + uy * (v.y - a.y) for v in (c, d)]
                    lo, hi = max(0, min(ends)), min(length, max(ends))
                    if hi <= lo:
                        continue
                    gap = (abs(ux * (c.y - a.y) - uy * (c.x - a.x))
                           - (track.GetWidth() + partner.GetWidth()) / 2) / 1e6
                    # Only a coupled trunk at the declared gap discharges coverage;
                    # terminal escapes remain visible under the explicit length limit.
                    if abs(gap - pair['gap_mm']) > .001 or abs(partner.GetWidth() - track.GetWidth()) > 1:
                        continue
                    value = calculate(pcb, layer_name, track.GetWidth() / 1e6, gap).get('zdiff')
                    if value is None or not math.isfinite(value) or abs(value / spec['target_ohm'] - 1) * 100 > spec['tolerance_percent']:
                        defects.append(name + ': saved coupled geometry misses impedance tolerance')
                    else:
                        intervals.append((lo, hi));zs.append(value);gaps.append(gap)
                        if len(references) == 2:
                            # The stripline model also needs reference copper
                            # between the members, not just under each trace.
                            offset = ux * (c.y - a.y) - uy * (c.x - a.x)
                            low = min(-track.GetWidth() / 2, offset - partner.GetWidth() / 2)
                            high = max(track.GetWidth() / 2, offset + partner.GetWidth() / 2)
                            for reference, (ground, _) in planes.items():
                                corridor = k.SHAPE_POLY_SET();corridor.NewOutline()
                                for along, across in ((lo, low), (hi, low), (hi, high), (lo, high)):
                                    corridor.Append(round(a.x + ux * along - uy * across),
                                                    round(a.y + uy * along + ux * across))
                                corridor.BooleanSubtract(ground);corridor.Simplify()
                                if corridor.OutlineCount():
                                    defects.append(name + ': coupled corridor lacks filled reference ' + reference)
                covered += _interval_length(intervals) / 1e6
                # Unmatched intervals are allowed only as pad-connected launches,
                # never as an ungraded discontinuity in the middle of a trunk.
                cursor = 0.0
                for lo, hi in [*sorted(intervals), (length, length)]:
                    if lo > cursor:
                        escapes.append(((round(a.x + ux * cursor), round(a.y + uy * cursor)),
                                        (round(a.x + ux * lo), round(a.y + uy * lo))))
                    cursor = max(cursor, hi)
            graph = {}
            for start, end in escapes:
                graph.setdefault(start, set()).add(end)
                graph.setdefault(end, set()).add(start)
            pending = [point for point in graph if any(pad.HitTest(k.VECTOR2I(*point)) for pad in pads)]
            reached = set(pending)
            while pending:
                for neighbor in graph[pending.pop()] - reached:
                    reached.add(neighbor)
                    pending.append(neighbor)
            if set(graph) - reached:
                defects.append(name + ': ungraded geometry is not a pad-connected terminal escape')
            uncoupled = total - covered
            if uncoupled > spec['max_uncoupled_mm'] + 1e-6 or not zs:
                defects.append(name + ': coupled geometry coverage misses declared uncoupled-length limit')
            results.append({'net': name, 'length_mm': total, 'coupled_mm': covered, 'uncoupled_mm': uncoupled,
                            'reference_opening_area_mm2': antipad_area,
                            'antipad_polygon_margin_mm': (board.GetDesignSettings().m_MaxError + 1000) / 1e6 if len(references) == 2 else 0,
                            'widths_mm': sorted({t.GetWidth() / 1e6 for t in tracks if t.GetClass() == 'PCB_TRACK'}),
                            'impedance_ohm_range': [min(zs), max(zs)] if zs else None,
                            'gap_mm_range': [min(gaps), max(gaps)] if gaps else None})
    return {'status': 'FAIL' if defects else 'PASS', 'board_sha256': common.sha256(board_path),
            'defects': sorted(set(defects)), 'nets': results, 'plan': planned,
            'limits': 'Nominal microstrip or symmetric homogeneous stripline trunks; each adjacent native filled reference checked with 1um polygon bound. Inner PTH terminal antipads limited to own pad plus the smallest explicit zone clearance and the reported native polygon error bound. Terminal escapes separately limited. No field solver, supplier tolerance, launch-impedance or physical measurement.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('board', type=Path);parser.add_argument('request', type=Path);parser.add_argument('output', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        request = common.load_json(args.request)
        result = check(args.board, request) if args.check else plan(args.board, request)
    except ValueError as exc:
        result = {'status': 'FAIL', 'board_sha256': common.sha256(args.board), 'defects': [str(exc)]}
    common.write_json(args.output, result)


if __name__ == '__main__':
    main()
