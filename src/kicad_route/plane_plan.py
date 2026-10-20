"""Strict agent-ee.plane-plan/1 contract (MIT agent-ee add_planes compatibility).

Keep field names, numeric bounds and deterministic zone IDs compatible with the
agent-ee producer. Native board constraints are checked by kicad/planes.py.
"""
import hashlib
import json
import math
import uuid

SCHEMA = 'agent-ee.plane-plan/1'
NAMESPACE = uuid.UUID('ac798940-3e63-57eb-9fe4-72405f8101df')


def identity(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def load_plan(path):
    def pairs(items):
        record = {}
        for key, value in items:
            if key in record:
                raise ValueError('duplicate plane-plan key: ' + key)
            record[key] = value
        return record
    return validate_plan(json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs))


def _number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high or not math.isfinite(value):
        raise ValueError(f'{name} must be finite and within [{low}, {high}]')
    return value


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {'schema', 'zones'} or plan['schema'] != SCHEMA:
        raise ValueError('expected agent-ee.plane-plan/1 with zones')
    if not isinstance(plan['zones'], list) or not 1 <= len(plan['zones']) <= 64:
        raise ValueError('plane plan requires 1..64 zones')
    for zone in plan['zones']:
        required = {'net', 'layer', 'outline', 'clearance_mm', 'min_width_mm', 'thermal', 'priority', 'island_removal'}
        if not isinstance(zone, dict) or set(zone) != required:
            raise ValueError('plane zone has unknown or missing fields')
        if not isinstance(zone['net'], str) or not zone['net'].strip() or '\x00' in zone['net']:
            raise ValueError('plane net must be an explicit nonempty name')
        if not isinstance(zone['layer'], str) or zone['layer'] not in {'F.Cu', 'B.Cu', *(f'In{i}.Cu' for i in range(1, 31))}:
            raise ValueError('plane layer must name one copper layer')
        _number(zone['clearance_mm'], 'clearance_mm', .001, 100)
        _number(zone['min_width_mm'], 'min_width_mm', .001, 100)
        if isinstance(zone['priority'], bool) or not isinstance(zone['priority'], int) or not 0 <= zone['priority'] <= 100:
            raise ValueError('plane priority must be an integer in [0, 100]')
        if zone['island_removal'] not in ('always', 'never'):
            raise ValueError('island_removal must explicitly be always or never')
        thermal = zone['thermal']
        if not isinstance(thermal, dict) or set(thermal) != {'connection', 'gap_mm', 'spoke_width_mm'}:
            raise ValueError('thermal requires connection, gap_mm and spoke_width_mm')
        if thermal['connection'] not in ('solid', 'thermal', 'tht_thermal'):
            raise ValueError('unsupported plane pad connection')
        _number(thermal['gap_mm'], 'thermal gap_mm', .001, 100)
        _number(thermal['spoke_width_mm'], 'thermal spoke_width_mm', .001, 100)
        outline = zone['outline']
        if outline != 'board':
            if not isinstance(outline, dict) or set(outline) != {'polygon_mm'}:
                raise ValueError('outline must be board or an explicit polygon_mm')
            points = outline['polygon_mm']
            if not isinstance(points, list) or not 3 <= len(points) <= 4096:
                raise ValueError('polygon needs 3..4096 vertices')
            for point in points:
                if not isinstance(point, list) or len(point) != 2:
                    raise ValueError('polygon vertices must be [x_mm, y_mm]')
                for value in point:
                    _number(value, 'polygon coordinate', -10000, 10000)
            if len({tuple(p) for p in points}) != len(points):
                raise ValueError('polygon vertices must be distinct; closure is implicit')
    return plan


def zone_ids(plan):
    return [str(uuid.uuid5(NAMESPACE, f'{identity(plan)}/{i}')) for i in range(len(plan['zones']))]
