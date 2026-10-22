"""Qualify captured native trace/via copper against an explicit experiment policy.

This is an additional experiment gate, not a replacement for native DRC or
preservation. Linear polygon boundaries and straight tracks are supported;
unsupported geometry yields NOT_RUN. All distances use integer nanometres.
"""
import json
import re
import uuid
from pathlib import Path

from .common import sha256_of

_FIELDS = {'schema', 'experiment_class', 'allowed_trace_layers', 'allowed_via_spans',
           'rule_areas_sha256'}


def _strings(value):
    return (isinstance(value, list) and bool(value) and
            all(isinstance(v, str) and v for v in value) and len(value) == len(set(value)))


def _policy(p):
    if not isinstance(p, dict) or set(p) != _FIELDS:
        raise ValueError('policy requires exactly the routability-policy/1 fields')
    if p['schema'] != 'routability-policy/1' or p['experiment_class'] not in (
            'current-policy', 'historical-all-layer'):
        raise ValueError('unsupported policy schema or experiment_class')
    if not _strings(p['allowed_trace_layers']):
        raise ValueError('allowed_trace_layers must be unique nonempty layer names')
    spans = p['allowed_via_spans']
    if not isinstance(spans, list) or any(not _strings(s) or len(s) != 2 for s in spans):
        raise ValueError('allowed_via_spans must contain distinct endpoint pairs')
    if len({tuple(s) for s in spans}) != len(spans):
        raise ValueError('duplicate allowed_via_spans')
    if not isinstance(p['rule_areas_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', p['rule_areas_sha256']):
        raise ValueError('rule_areas_sha256 must be a SHA256 digest')
    return p


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON object key')
        result[key] = value
    return result


def load(path):
    """Read a caller declaration; malformed or unsupported declarations raise."""
    try:
        return _policy(json.loads(Path(path).read_text(), object_pairs_hook=_json_object))
    except json.JSONDecodeError as exc:
        raise ValueError(f'invalid policy JSON: {exc}') from exc


def _point(p):
    if not isinstance(p, list) or len(p) != 2 or any(type(v) is not int for v in p):
        raise ValueError('invalid integer coordinate')
    return p


def _cross(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])


def _on(p, a, b):
    return (_cross(a, b, p) == 0 and min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= p[1] <= max(a[1], b[1]))


def _intersects(a, b, c, d):
    ac, ad, ca, cb = _cross(a, b, c), _cross(a, b, d), _cross(c, d, a), _cross(c, d, b)
    return ((ac * ad < 0 and ca * cb < 0) or _on(c, a, b) or _on(d, a, b)
            or _on(a, c, d) or _on(b, c, d))


def _edges(ring):
    return zip(ring, ring[1:] + ring[:1])


def _inside(p, ring):
    # Boundary is part of restricted copper, including the boundary of a hole.
    if any(_on(p, a, b) for a, b in _edges(ring)):
        return 0
    inside = False
    for a, b in _edges(ring):
        if (a[1] > p[1]) != (b[1] > p[1]):
            cross = _cross(a, b, p)
            if (cross > 0) == (b[1] > a[1]):
                inside = not inside
    return 1 if inside else -1


def _ring(r):
    if not isinstance(r, list):
        raise ValueError('missing polygon ring')
    r = [_point(p) for p in r]
    if len(r) > 1 and r[0] == r[-1]:
        r = r[:-1]
    if len(r) < 3 or len({tuple(p) for p in r}) != len(r):
        raise ValueError('degenerate polygon ring')
    if not sum(a[0]*b[1]-a[1]*b[0] for a, b in _edges(r)):
        raise ValueError('zero-area polygon ring')
    edges = list(_edges(r))
    for i, p in enumerate(r):
        before, after = r[i-1], r[(i+1) % len(r)]
        if _on(after, before, p) or _on(before, p, after):
            raise ValueError('overlapping adjacent polygon edges')
    for i, (a, b) in enumerate(edges):
        for j in range(i+1, len(edges)):
            if j == i+1 or (i == 0 and j == len(edges)-1):
                continue
            if _intersects(a, b, *edges[j]):
                raise ValueError('self-intersecting polygon ring')
    return r


def _polygons(area):
    polys = area.get('polygons')
    if not isinstance(polys, list) or not polys:
        raise ValueError('missing complete rule-area polygons')
    result = []
    for poly in polys:
        if not isinstance(poly, dict) or set(poly) != {'outline', 'holes'} or not isinstance(poly['holes'], list):
            raise ValueError('malformed polygon/holes')
        outer, holes = _ring(poly['outline']), [_ring(h) for h in poly['holes']]
        for i, h in enumerate(holes):
            if any(_inside(p, outer) != 1 for p in h):
                raise ValueError('hole outside or touching outline')
            for other in [outer, *holes[:i]]:
                if any(_intersects(a, b, c, d) for a, b in _edges(h) for c, d in _edges(other)):
                    raise ValueError('intersecting polygon rings')
            if any(_inside(h[0], other) != -1 or _inside(other[0], h) != -1 for other in holes[:i]):
                raise ValueError('nested polygon holes')
        result.append((outer, holes))
    return result


def _near(p, a, b, diameter):
    # Squared point-to-segment distance <= (diameter/2)^2, without rounding.
    dx, dy = b[0]-a[0], b[1]-a[1]
    length = dx*dx + dy*dy
    projection = (p[0]-a[0])*dx + (p[1]-a[1])*dy
    if projection <= 0 or not length:
        return 4*((p[0]-a[0])**2+(p[1]-a[1])**2) <= diameter*diameter
    if projection >= length:
        return 4*((p[0]-b[0])**2+(p[1]-b[1])**2) <= diameter*diameter
    return 4*_cross(a, b, p)**2 <= diameter*diameter*length


def _overlap(a, b, diameter, polygons):
    for outer, holes in polygons:
        for p in (a, b):
            if _inside(p, outer) >= 0 and all(_inside(p, h) != 1 for h in holes):
                return True
        for ring in [outer, *holes]:
            for c, d in _edges(ring):
                if (_intersects(a, b, c, d) or _near(a, c, d, diameter)
                        or _near(b, c, d, diameter) or _near(c, a, b, diameter)
                        or _near(d, a, b, diameter)):
                    return True
    return False


def _size(value):
    if type(value) is not int or value <= 0:
        raise ValueError('invalid copper diameter/width')
    return value


def _uuid(value):
    if not isinstance(value, str):
        raise ValueError('missing native copper/area UUID')
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError('noncanonical native UUID')
    except (ValueError, AttributeError) as exc:
        raise ValueError('invalid native copper/area UUID') from exc
    return value


def validate(policy, facts):
    """Return PASS/FAIL/NOT_RUN. Missing proof always takes precedence over FAIL.

    Scope: actual trace layers, via spans and track/via rule-area prohibitions.
    Pads, footprints and pours remain under preservation and native DRC gates.
    """
    failures, unknown = [], []
    result = {'status': 'NOT_RUN', 'reasons': [], 'policy_sha256': None,
              'rule_areas_sha256': None, 'coverage': None}
    try:
        _policy(policy)
        result['policy_sha256'] = sha256_of(policy)
        if not isinstance(facts, dict) or facts.get('rule_areas_coverage') != 'board-and-footprint' or facts.get('rule_areas_status') != 'PASS' or facts.get('rule_areas_reasons') != []:
            raise ValueError('complete native board-and-footprint rule-area coverage required')
        result['coverage'] = 'board-and-footprint'
        stack = facts.get('copper_layers')
        if not isinstance(stack, list) or any(not isinstance(x, dict) for x in stack):
            raise ValueError('missing physical copper stack')
        stack = [x.get('name') for x in stack]
        if not _strings(stack) or len(stack) < 2:
            raise ValueError('invalid physical copper stack')
        if any(l not in stack for l in policy['allowed_trace_layers']):
            raise ValueError('declared trace layer outside physical stack')
        for top, bottom in policy['allowed_via_spans']:
            if top not in stack or bottom not in stack or stack.index(top) >= stack.index(bottom):
                raise ValueError('declared via span outside physical stack or reversed')
        areas = facts.get('rule_areas')
        if not isinstance(areas, list):
            raise ValueError('missing native rule-area inventory')
        result['rule_areas_sha256'] = sha256_of(areas)
        if result['rule_areas_sha256'] != policy['rule_areas_sha256']:
            failures.append('native rule areas differ from declared policy hash')
        qualified, seen = [], set()
        for area in areas:
            if not isinstance(area, dict) or not isinstance(area.get('uuid'), str) or not area['uuid'] or area['uuid'] in seen:
                raise ValueError('invalid/duplicate rule-area identity')
            seen.add(area['uuid'])
            _uuid(area['uuid'])
            owner = area.get('owner')
            if not isinstance(owner, dict) or set(owner) != {'kind', 'uuid', 'reference'} or owner.get('kind') not in ('board', 'footprint'):
                raise ValueError('missing rule-area ownership')
            if owner['kind'] == 'board' and (owner['uuid'] is not None or owner['reference'] is not None):
                raise ValueError('board rule-area owner must use the serialized board marker')
            if owner['kind'] == 'footprint' and (not isinstance(owner.get('uuid'), str) or not owner['uuid'] or not isinstance(owner.get('reference'), str)):
                raise ValueError('invalid footprint rule-area owner')
            if owner['kind'] == 'footprint':
                _uuid(owner['uuid'])
            layers = area.get('layers')
            if not _strings(layers) or any(l not in stack for l in layers):
                raise ValueError('unsupported rule-area layer')
            if any(type(area.get(k)) is not bool for k in ('no_tracks', 'no_vias', 'no_pads', 'no_footprints', 'no_zone_fills')):
                raise ValueError('missing native rule-area restriction flags')
            qualified.append((area, _polygons(area)))
        tracks, vias = facts.get('tracks'), facts.get('vias')
        if not isinstance(tracks, list) or not isinstance(vias, list):
            raise ValueError('missing actual copper inventory')
        for t in tracks:
            if not isinstance(t, dict) or t.get('kind') != 'segment':
                raise ValueError('unsupported actual trace geometry (only straight segments qualified)')
            _uuid(t.get('uuid'))
            a, b, width = _point(t.get('start')), _point(t.get('end')), _size(t.get('width'))
            layer = t.get('layer')
            if layer not in stack:
                raise ValueError('actual trace layer outside physical stack')
            if layer not in policy['allowed_trace_layers']:
                failures.append(f"track {t.get('uuid')} uses forbidden layer {layer}")
            for area, polygons in qualified:
                if area['no_tracks'] and layer in area['layers'] and _overlap(a, b, width, polygons):
                    failures.append(f"track {t.get('uuid')} intersects rule area {area['uuid']}")
        for v in vias:
            if not isinstance(v, dict) or v.get('via_type') not in ('through', 'blind', 'buried', 'micro'):
                raise ValueError('unsupported actual via type')
            _uuid(v.get('uuid'))
            if v.get('annulus_geometry') != 'circular-layer-discs':
                raise ValueError('unsupported or missing native via annulus geometry')
            pos = _point(v.get('position'))
            span = v.get('layers')
            if not _strings(span) or len(span) != 2 or any(l not in stack for l in span) or stack.index(span[0]) >= stack.index(span[1]):
                raise ValueError('invalid actual via span')
            if v['via_type'] == 'through' and span != [stack[0], stack[-1]]:
                raise ValueError('through via does not cover physical stack')
            if span not in policy['allowed_via_spans']:
                failures.append(f"via {v.get('uuid')} uses forbidden span {span}")
            physical = stack[stack.index(span[0]):stack.index(span[1])+1]
            sizes = v.get('layer_diameters')
            if (not isinstance(sizes, list) or len(sizes) != len(physical)
                    or any(not isinstance(s, dict) or set(s) != {'layer', 'diameter'} for s in sizes)
                    or [s['layer'] for s in sizes] != physical):
                raise ValueError('incomplete native per-layer via annulus coverage')
            diameters = {s['layer']: _size(s['diameter']) for s in sizes}
            for area, polygons in qualified:
                if area['no_vias'] and any(l in area['layers'] and _overlap(pos, pos, diameters[l], polygons) for l in physical):
                    failures.append(f"via {v.get('uuid')} intersects rule area {area['uuid']}")
    except (ValueError, TypeError, KeyError) as exc:
        unknown.append(str(exc))
    result['status'] = 'NOT_RUN' if unknown else ('FAIL' if failures else 'PASS')
    result['reasons'] = unknown + failures
    return result
