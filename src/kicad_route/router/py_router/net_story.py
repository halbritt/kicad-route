"""Per-net routing "story" dump (KICAD_NET_STORY=1).

One JSON file per routing run telling the complete journey of every net:
its bus membership and corridor, its place in the routing order, every
recorded event (initial route, failures with named blockers, who ripped
it and when, reroutes, rescue rungs fired), its multipoint edge structure
and Phase-3 tap outcomes, and the costs it paid (iterations, length,
vias, per-layer copper). Events are copied when recorded; per-call results and bus maps are
snapshotted on exit. The outer batch_route writes once, including nested
repair calls, so their attempted work survives later geometry rollback.

Enable with KICAD_NET_STORY=1; batch_route writes
<output_basename>_netstory.json next to the output board. The dump is
read-only over engine state. See docs/routing-diagnostics.md for aggregation
and failure semantics.

Not yet included (needs Rust instrumentation): the A* cost breakdown per
move class (via cost, turn cost, proximity taxes). The per-net iteration
count is the search-effort proxy available today.
"""
from __future__ import annotations

import os
from typing import Any, Dict


def net_story_enabled() -> bool:
    return os.environ.get('KICAD_NET_STORY', '') in ('1', 'true', 'on')


def _layer_mm(segments) -> Dict[str, float]:
    """Per-layer copper length (mm) for a list of segments."""
    import math
    out: Dict[str, float] = {}
    for s in segments or []:
        d = math.hypot(s.end_x - s.start_x, s.end_y - s.start_y)
        out[s.layer] = round(out.get(s.layer, 0.0) + d, 2)
    return out


def _net_name(pcb_data, nid) -> str:
    n = pcb_data.nets.get(nid)
    return n.name if n else f"net{nid}"


def _jsonable(x) -> Any:
    """Best-effort conversion of event detail values (tuples, sets, numpy
    scalars, objects) into JSON-safe structures."""
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (str, int, float, bool)) or x is None:
        return x
    if hasattr(x, 'item'):  # numpy scalar
        try:
            value = x.item()
        except Exception:
            pass
        else:
            # Object scalars can contain mutable dicts/lists. Detach those too;
            # long-double scalars may return their own type, so use repr there.
            if type(value) is not type(x):
                return _jsonable(value)
    return repr(x)


def build_net_story(state, output_file: str, *, include_events=True) -> dict:
    """Snapshot the existing per-net fields of one routing state."""
    pcb_data = state.pcb_data
    story: Dict[str, Any] = {
        'board': output_file,
        'nets': {},
        # Whole-run context the per-net entries reference
        'bus_groups': {},
        'phase3_order': [nm for nm in getattr(state, 'story_phase3_order', [])],
    }

    b2g = getattr(state, 'bus_net_to_group', None) or {}
    corridors = getattr(state, 'bus_corridors', None) or {}
    for nid, bg in b2g.items():
        gname = bg.name
        if gname not in story['bus_groups']:
            story['bus_groups'][gname] = {
                'members_in_physical_order': [
                    _net_name(pcb_data, m) for m in bg.net_ids],
                'clique_endpoint': getattr(bg, 'clique_endpoint', None),
                'corridor_planned': gname in corridors,
                'corridor_points': len(corridors.get(gname, [])),
            }

    all_ids = set(getattr(state, 'net_history', {}) or {})
    all_ids |= set(getattr(state, 'routed_results', {}) or {})
    all_ids |= set(b2g)

    for nid in sorted(all_ids):
        entry: Dict[str, Any] = {'net_id': nid}
        if nid in b2g:
            bg = b2g[nid]
            entry['bus'] = {
                'group': bg.name,
                'position': (bg.net_ids.index(nid)
                             if nid in bg.net_ids else None),
                'corridor_planned': bg.name in corridors,
            }
        events = ((getattr(state, 'net_history', {}) or {}).get(nid, [])
                  if include_events else [])
        entry['events'] = [
            {'seq': e.get('sequence'), 'event': e.get('event'),
             'details': _jsonable(e.get('details'))}
            for e in events]
        res = (getattr(state, 'routed_results', {}) or {}).get(nid)
        if res:
            segs = res.get('new_segments') or []
            entry['result'] = {
                'iterations': res.get('iterations'),
                'segments': len(segs),
                'vias': len(res.get('new_vias') or []),
                'route_length_mm': res.get('route_length'),
                'stub_length_mm': res.get('stub_length'),
                'copper_by_layer_mm': _layer_mm(segs),
            }
            if res.get('is_multipoint'):
                mp = {
                    'mst_edges': [[a, b, round(l, 2)]
                                  for a, b, l in (res.get('mst_edges') or [])],
                    'main_edge_selection': res.get('edge_selection_note'),
                    'tap_edges_routed': res.get('tap_edges_routed'),
                    'tap_edges_failed': res.get('tap_edges_failed'),
                    'failed_pads': [
                        {'ref': p.get('component_ref'),
                         'pad': p.get('pad_number'),
                         'x': p.get('x'), 'y': p.get('y')}
                        for p in (res.get('failed_pads_info') or [])],
                }
                # Per-edge terminal blocking, blockers named (the raw record
                # keys blocked CELLS by edge; names were resolved when the
                # failure was analyzed and recorded as events -- here we keep
                # the edge shape + target for cross-reference).
                feb = res.get('failed_edge_blocking') or {}
                mp['failed_edges'] = [
                    {'edge': list(k) if isinstance(k, tuple) else str(k),
                     'target_xy': _jsonable(v[1] if isinstance(v, tuple)
                                            and len(v) > 1 else None)}
                    for k, v in feb.items()]
                entry['multipoint'] = mp
            entry['status'] = ('partial'
                               if res.get('failed_pads_info') else 'routed')
        else:
            entry['status'] = 'failed_or_untouched'
        story['nets'][_net_name(pcb_data, nid)] = entry

    return story


def story_path(output_file):
    base, _ = os.path.splitext(output_file or 'routing')
    return f"{base}_netstory.json"


def write_net_story(story, output_file):
    from route_summary import write_summary_file
    path = story_path(output_file)
    write_summary_file(path, story)
    print(f"  Net story written to {path} "
          f"({len(story['nets'])} net(s), KICAD_NET_STORY)")
    return path


def dump_net_story(state, output_file: str) -> str:
    """Write a standalone story, or defer to the active outer run owner."""
    from routing_run_history import defer_story
    if defer_story():
        return story_path(output_file)
    return write_net_story(build_net_story(state, output_file), output_file)
