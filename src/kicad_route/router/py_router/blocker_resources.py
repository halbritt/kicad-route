"""Bounded, disposable relaxed searches. Paths are ordering hints, never cuts."""
from copy import deepcopy
from dataclasses import fields
from hashlib import sha256

import numpy as np

MAX_RESOURCE_CELLS = 250000


class UnsupportedProbe(ValueError):
    pass


def _decode_resources(data, rung):
    """Bound expansion before decoding the ordinary track/via multiset once."""
    from obstacle_cache import _cache_cells_and_vias
    spans = (data.blocked_cell_spans, data.blocked_via_spans)
    count = len(data.blocked_cells) + len(data.blocked_vias)
    count += sum(sum(max(0, int(hi) - int(lo) + 1) for lo, hi in a[:, 1:3])
                 for a in spans if len(a))
    count += len(data.blocked_vias_small)
    count += sum(len(a) for a in data.blocked_vias_rungs.values())
    if count > MAX_RESOURCE_CELLS:
        raise UnsupportedProbe('resource_expansion_limit')
    decoded = _cache_cells_and_vias(data)
    cells, vias = decoded
    if rung:
        vias = data.blocked_vias_rungs.get(rung)
        if vias is None:
            raise UnsupportedProbe('missing_rung_cache')
    if len(cells) + len(vias) > MAX_RESOURCE_CELLS:
        raise UnsupportedProbe('resource_expansion_limit')
    return decoded


def _resources(data, rung, decoded):
    """Unique ownership only; authoritative map updates retain cached multisets."""
    cells, vias = decoded
    if rung:
        vias = data.blocked_vias_rungs[rung]
    return set(map(tuple, cells.tolist())), set(map(tuple, vias.tolist()))


def _same_cache(left, right, left_decoded, right_decoded):

    def same_rows(a, b):
        if a.shape != b.shape:
            return False
        if not len(a):
            return True
        return np.array_equal(a[np.lexsort(a.T)], b[np.lexsort(b.T)])

    return (all(same_rows(a, b) for a, b in
                zip(left_decoded, right_decoded))
            and same_rows(left.blocked_vias_small, right.blocked_vias_small)
            and left.blocked_vias_rungs.keys() == right.blocked_vias_rungs.keys()
            and all(same_rows(left.blocked_vias_rungs[r], right.blocked_vias_rungs[r])
                    for r in left.blocked_vias_rungs))


def probe_order(pcb_data, config, working, net_id, blockers, cache,
                routed_results, pair_by_net, diagnostics):
    from grid_router import GridRouter
    from kicad_parser import PCBData
    from connectivity import get_net_endpoints
    from obstacle_cache import (precompute_net_obstacles, remove_net_obstacles_from_cache,
                                add_net_obstacles_from_cache, rung_for_net)
    from pcb_modification import remove_route_from_pcb_data
    from protected_nets import rip_refusal

    def unknown(reason):
        diagnostics.update(status='unknown', reason=reason, coverage='incomplete')
        return [], False

    if routed_results is None:
        return unknown('missing_route_membership')
    if any(getattr(pcb_data, n, None) is not None for n in
           ('exact_fill_provider', 'live_rules_provider', 'live_records_provider')):
        return unknown('live_board_provider')
    try:
        # Omit all derived identity caches and callbacks. One deepcopy memo keeps
        # result membership, footprint pads and pads_by_net aliases coherent.
        fresh = PCBData(**{f.name: getattr(pcb_data, f.name) for f in fields(PCBData)})
        board, cfg, routes = deepcopy((fresh, config, routed_results))
        for name in ('_rip_override_names', '_protection_map_memo'):
            if hasattr(pcb_data, name):
                setattr(board, name, deepcopy(getattr(pcb_data, name)))
        if any(cfg.track_margins_for_net(net_id)):
            return unknown('swept_track_margin')
        if getattr(cfg, 'net_via_sizes', {}).get(net_id) is not None:
            # Current per-net recipe is stamped against the run's reserve; raw
            # search must not silently choose a different reserve/rung.
            return unknown('per_net_via_geometry')
        rung = int(getattr(cfg, 'via_rung', 0))
        if rung != rung_for_net(cfg, board, net_id):
            return unknown('unbound_via_rung')
        clone = working.clone_fresh()
        clone.clear_allowed_cells()
        clone.clear_endpoint_exempt()
        clone.clear_free_vias()
        # Existing same-net via reuse requires padstack-aware exemption prep.
        if any(v.net_id == net_id for v in board.vias) or any(
                p.drill > 0 for p in board.pads_by_net.get(net_id, [])):
            return unknown('existing_free_via')
        if rung and not clone.blocked_via_cells_at_rung(rung):
            return unknown('inactive_via_rung')
        def validate_cache(nid, data):
            recipe = getattr(data, '_probe_recipe', None)
            if not recipe or recipe[0] != sha256(repr(config).encode()).hexdigest():
                raise UnsupportedProbe('unbound_stamp_recipe')
            decoded = _decode_resources(data, rung)
            rebuilt = precompute_net_obstacles(board, nid, cfg, recipe[1], recipe[2])
            rebuilt_decoded = _decode_resources(rebuilt, rung)
            if not _same_cache(rebuilt, data, rebuilt_decoded, decoded):
                raise UnsupportedProbe('cache_geometry_mismatch')
            return recipe, decoded

        own = cache.get(net_id)
        if own is not None:
            validate_cache(net_id, own)
            remove_net_obstacles_from_cache(clone, own)
        owners = {}
        rows, via_rows = [], []
        cost = cfg.cell_cost(5.0)
        members = getattr(cfg, 'bus_member_net_ids', None) or set()
        resistance = float(getattr(cfg, 'bus_rip_resistance', 1.0) or 1.0)
        total = 0
        for nid in dict.fromkeys(b.net_id for b in blockers):
            if nid == net_id or nid in (pair_by_net or {}) or net_id in (pair_by_net or {}):
                return unknown('unsupported_repair_unit')
            if rip_refusal(board, nid, context='relaxed probe'):
                continue
            result = routes.get(nid)
            data = cache.get(nid)
            if result is None or data is None:
                return unknown('missing_victim_membership_or_cache')
            removed = list(result.get('new_segments') or []) + list(result.get('new_vias') or [])
            live_ids = {id(v) for v in board.segments + board.vias}
            if not removed or any(id(v) not in live_ids or v.net_id != nid
                                  or getattr(v, 'locked', False) or getattr(v, 'graphic', False)
                                  for v in removed):
                return unknown('unsupported_route_membership')
            recipe, decoded = validate_cache(nid, data)
            cells, vias = _resources(data, rung, decoded)
            remove_route_from_pcb_data(board, result)
            survivor = precompute_net_obstacles(board, nid, cfg, recipe[1], recipe[2])
            survivor_decoded = _decode_resources(survivor, rung)
            hard_cells, hard_vias = _resources(survivor, rung, survivor_decoded)
            remove_net_obstacles_from_cache(clone, data)
            add_net_obstacles_from_cache(clone, survivor)
            cells -= hard_cells
            vias -= hard_vias
            owners[nid] = cells, vias
            total += len(cells) + len(vias)
            if total > MAX_RESOURCE_CELLS:
                raise UnsupportedProbe('resource_expansion_limit')
            price = int(cost * resistance) if nid in members and resistance > 1 else cost
            rows.extend((li, x, y, price) for x, y, li in cells)
            via_rows.extend((x, y, price) for x, y in vias)
        # Keep max overlap pricing, matching existing proximity semantics.
        clone.set_layer_proximity_batch(np.asarray(rows, dtype=np.int32).reshape(-1, 4))
        clone.set_probe_via_costs(rung, np.asarray(via_rows, dtype=np.int32).reshape(-1, 3))
        sources, targets, error = get_net_endpoints(board, net_id, cfg)
        if error or not sources or not targets:
            return unknown('unsupported_endpoints')
        sources = [tuple(v[:3]) for v in sources]
        targets = [tuple(v[:3]) for v in targets]
        # Never punch a hole through a fixed foreign terminal obstruction.
        if any(clone.is_blocked(*v) for v in sources + targets):
            return unknown('blocked_terminal')
        router = GridRouter(via_cost=cfg.via_cost_units(), h_weight=cfg.heuristic_weight,
                            turn_cost=cfg.turn_cost, via_proximity_cost=cfg.via_proximity_cost_int(),
                            layer_costs=cfg.get_layer_costs(),
                            layer_direction_preferences=cfg.get_layer_direction_preferences(),
                            direction_preference_cost=cfg.direction_preference_cost)
        path, iterations, _ = router.route_multi(clone, sources, targets,
                min(cfg.max_iterations, cfg.max_probe_iterations), via_rung=rung)
        if not path:
            return unknown('no_path_or_budget')
        order, seen = [], set()
        for i, (x, y, li) in enumerate(path):
            for nid, (cells, vias) in owners.items():
                via = i and path[i-1][2] != li and (x, y) in vias
                if nid not in seen and ((x, y, li) in cells or via):
                    order.append(nid)
                    seen.add(nid)
        diagnostics.update(status='path', reason=None, coverage='complete', iterations=iterations)
        return order, True
    except UnsupportedProbe as error:
        return unknown(str(error))
    except (TypeError, ValueError, RuntimeError, AttributeError, OverflowError) as error:
        # The optional ranking hint must not abort the production repair ladder.
        # Preserve an explicit failure receipt, never manufacture feasibility.
        diagnostics.update(error_type=type(error).__name__, error=str(error)[:240])
        return unknown('probe_error')
