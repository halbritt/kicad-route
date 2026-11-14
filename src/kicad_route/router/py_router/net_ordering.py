"""
Net ordering strategies for PCB routing.

This module provides different strategies for ordering nets before routing,
including MPS (Maximum Planar Subset), inside-out BGA ordering, and original order.
"""
from __future__ import annotations

from typing import List, Tuple, Dict, Optional, Set

from kicad_parser import PCBData
from routing_config import DiffPairNet


def order_nets_mps(
    pcb_data: PCBData,
    net_ids: List[Tuple[str, int]],
    diff_pairs: Dict[str, DiffPairNet],
    mps_unroll: bool,
    bga_exclusion_zones: Optional[List[Tuple[float, float, float, float]]],
    mps_reverse_rounds: int,
    crossing_layer_check: bool,
    mps_segment_intersection: bool,
    mps_layer_swap: bool = False,
    enable_layer_switch: bool = False,
    config=None,
    can_swap_to_top_layer: bool = True,
    all_segment_modifications: List = None,
    all_swap_vias: List = None,
    all_stubs_by_layer: Dict = None,
    stub_endpoints_by_layer: Dict = None,
    verbose: bool = False
) -> Tuple[List[Tuple[str, int]], int]:
    """
    Order nets using Maximum Planar Subset algorithm.

    Args:
        pcb_data: PCB data structure
        net_ids: List of (name, id) tuples for nets to order
        diff_pairs: Dictionary of detected diff pairs
        mps_unroll: Whether to use MPS unroll boundary ordering
        bga_exclusion_zones: BGA zones for ordering
        mps_reverse_rounds: Number of MPS reverse rounds
        crossing_layer_check: Whether to check crossing layers
        mps_segment_intersection: Use segment intersection for MPS
        mps_layer_swap: Whether to use MPS-aware layer swaps
        enable_layer_switch: Whether layer switching is enabled
        config: Routing configuration (for layer swaps)
        can_swap_to_top_layer: Whether top layer swaps are allowed
        all_segment_modifications: List to append segment mods (for layer swaps)
        all_swap_vias: List to append swap vias (for layer swaps)
        all_stubs_by_layer: Stubs by layer (for layer swaps)
        stub_endpoints_by_layer: Stub endpoints by layer (for layer swaps)
        verbose: Print detailed output

    Returns:
        Tuple of (ordered_net_ids, num_layer_swaps)
    """
    from net_queries import compute_mps_net_ordering

    print("\nUsing MPS ordering strategy...")
    all_net_ids = [nid for _, nid in net_ids]
    total_layer_swaps = 0

    # If MPS layer swap is enabled, get extended info for conflict analysis
    if mps_layer_swap and enable_layer_switch and config is not None:
        from mps_layer_swap import try_mps_aware_layer_swaps

        mps_result = compute_mps_net_ordering(
            pcb_data, all_net_ids, diff_pairs=diff_pairs,
            use_boundary_ordering=mps_unroll,
            bga_exclusion_zones=bga_exclusion_zones,
            reverse_rounds=mps_reverse_rounds,
            crossing_layer_check=crossing_layer_check,
            return_extended_info=True,
            use_segment_intersection=True if mps_segment_intersection else None
        )

        if mps_result.num_rounds > 1:
            print(f"\nMPS detected {mps_result.num_rounds} rounds - attempting layer swaps to reduce crossings...")

            swap_result = try_mps_aware_layer_swaps(
                pcb_data, config, mps_result, diff_pairs,
                available_layers=config.routable_layers(),
                can_swap_to_top_layer=can_swap_to_top_layer,
                all_segment_modifications=all_segment_modifications,
                all_swap_vias=all_swap_vias,
                all_stubs_by_layer=all_stubs_by_layer,
                stub_endpoints_by_layer=stub_endpoints_by_layer,
                verbose=verbose
            )

            if swap_result.swaps_applied > 0:
                total_layer_swaps = swap_result.swaps_applied
                # Re-run MPS ordering with updated layer assignments
                print("Re-running MPS ordering after layer swaps...")
                ordered_ids = compute_mps_net_ordering(
                    pcb_data, all_net_ids, diff_pairs=diff_pairs,
                    use_boundary_ordering=mps_unroll,
                    bga_exclusion_zones=bga_exclusion_zones,
                    reverse_rounds=mps_reverse_rounds,
                    crossing_layer_check=crossing_layer_check,
                    use_segment_intersection=True if mps_segment_intersection else None
                )
            else:
                ordered_ids = mps_result.ordered_ids
        else:
            ordered_ids = mps_result.ordered_ids
    else:
        ordered_ids = compute_mps_net_ordering(
            pcb_data, all_net_ids, diff_pairs=diff_pairs,
            use_boundary_ordering=mps_unroll,
            bga_exclusion_zones=bga_exclusion_zones,
            reverse_rounds=mps_reverse_rounds,
            crossing_layer_check=crossing_layer_check,
            use_segment_intersection=True if mps_segment_intersection else None
        )

    # Rebuild net_ids in the new order
    id_to_name = {nid: name for name, nid in net_ids}
    ordered_net_ids = [(id_to_name[nid], nid) for nid in ordered_ids if nid in id_to_name]

    return ordered_net_ids, total_layer_swaps


def order_pin_joins_last(net_ids: List[Tuple[str, int]]) -> List[Tuple[str, int]]:
    """Move the 'unconnected-*' nets to the end, keeping both partitions' order (#1405).

    Such a net joins one footprint's same-number pads (a reversible
    footprint's doubled pins, a connector's shield tabs), which no signal
    depends on. Routed early, it claims corridors the named nets need; routed
    last it gets what a separate final step used to give it.
    """
    pin_join = [t for t in net_ids if (t[0] or '').lower().startswith('unconnected-')]
    if not pin_join or len(pin_join) == len(net_ids):
        return net_ids
    print(f"Pin-join nets last (#1405): {len(pin_join)} 'unconnected-*' "
          f"net(s) routed after the named nets")
    return [t for t in net_ids if t not in pin_join] + pin_join


def order_nets_inside_out(
    pcb_data: PCBData,
    net_ids: List[Tuple[str, int]],
    bga_exclusion_zones: List[Tuple[float, float, float, float]]
) -> List[Tuple[str, int]]:
    """
    Order nets inside-out from BGA center(s) for better escape routing.

    Only reorders nets that have pads inside a BGA zone. Non-BGA nets
    are kept in original order and placed after BGA nets.

    Args:
        pcb_data: PCB data structure
        net_ids: List of (name, id) tuples for nets to order
        bga_exclusion_zones: List of (x1, y1, x2, y2) BGA zone tuples

    Returns:
        Ordered list of (name, id) tuples
    """
    def pad_in_bga_zone(pad):
        """Check if a pad is inside any BGA zone."""
        for zone in bga_exclusion_zones:
            if zone[0] <= pad.global_x <= zone[2] and zone[1] <= pad.global_y <= zone[3]:
                return True
        return False

    def get_min_distance_to_bga_center(net_id):
        """Get minimum distance from any BGA pad of this net to its BGA center."""
        pads = pcb_data.pads_by_net.get(net_id, [])
        if not pads:
            return float('inf')

        min_dist = float('inf')
        for zone in bga_exclusion_zones:
            center_x = (zone[0] + zone[2]) / 2
            center_y = (zone[1] + zone[3]) / 2
            for pad in pads:
                # Only consider pads that are inside this BGA zone
                if zone[0] <= pad.global_x <= zone[2] and zone[1] <= pad.global_y <= zone[3]:
                    dist = ((pad.global_x - center_x) ** 2 + (pad.global_y - center_y) ** 2) ** 0.5
                    min_dist = min(min_dist, dist)
        return min_dist

    # Separate BGA nets from non-BGA nets
    bga_nets = []
    non_bga_nets = []
    for net_name, net_id in net_ids:
        pads = pcb_data.pads_by_net.get(net_id, [])
        has_bga_pad = any(pad_in_bga_zone(pad) for pad in pads)
        if has_bga_pad:
            bga_nets.append((net_name, net_id))
        else:
            non_bga_nets.append((net_name, net_id))

    # Sort BGA nets inside-out, keep non-BGA nets in original order
    bga_nets.sort(key=lambda x: get_min_distance_to_bga_center(x[1]))
    ordered_net_ids = bga_nets + non_bga_nets

    if bga_nets:
        print(f"\nSorted {len(bga_nets)} BGA nets inside-out ({len(non_bga_nets)} non-BGA nets unchanged)")

    return ordered_net_ids


def separate_nets_by_type(
    net_ids: List[Tuple[str, int]],
    diff_pairs: Dict[str, DiffPairNet],
    diff_pair_net_ids: Set[int]
) -> Tuple[List[Tuple[str, DiffPairNet]], List[Tuple[str, int]]]:
    """
    Separate ordered nets into diff pairs and single-ended nets.

    Args:
        net_ids: Ordered list of (name, id) tuples
        diff_pairs: Dictionary of detected diff pairs
        diff_pair_net_ids: Set of net IDs belonging to diff pairs

    Returns:
        Tuple of (diff_pair_ids_to_route, single_ended_nets)
        where diff_pair_ids_to_route is List[(pair_name, DiffPairNet)]
        and single_ended_nets is List[(net_name, net_id)]
    """
    single_ended_nets = []
    diff_pair_ids_to_route = []  # (pair_name, pair) tuples
    processed_pair_net_ids = set()

    for net_name, net_id in net_ids:
        if net_id in diff_pair_net_ids and net_id not in processed_pair_net_ids:
            # Find the differential pair this net belongs to
            for pair_name, pair in diff_pairs.items():
                if pair.p_net_id == net_id or pair.n_net_id == net_id:
                    diff_pair_ids_to_route.append((pair_name, pair))
                    processed_pair_net_ids.add(pair.p_net_id)
                    processed_pair_net_ids.add(pair.n_net_id)
                    break
        elif net_id not in diff_pair_net_ids:
            single_ended_nets.append((net_name, net_id))

    return diff_pair_ids_to_route, single_ended_nets


def order_fine_pitch_perimeter_nets(
    pcb_data: PCBData,
    net_ids: List[Tuple[str, int]]
) -> List[Tuple[str, int]]:
    """Prioritize nets whose fine-pitch (<= 0.55 mm) perimeter pads have fewer open
    exit directions ahead of less-constrained neighbors on the same package edge.

    Within each fine-pitch package edge (QFN/LQFP), inner pads flanked on both
    sides by adjacent pins must escape straight outward with zero lateral degrees
    of freedom; corner/end pads have access to the open corner quadrant. Prioritizing
    inner constrained pads ahead of less-constrained edge neighbors prevents earlier
    corner routes from monopolizing the outward exit corridor.

    Strictly structural (zero reference/net name matching) and zero segment mutation.
    """
    import heapq
    from qfn_fanout.joint_escape import find_fine_pitch_footprints
    from qfn_fanout.layout import analyze_pad

    fine_fps = find_fine_pitch_footprints(pcb_data)
    if not fine_fps:
        return net_ids

    precedence_edges = set()

    for fp, layout in fine_fps:
        pitch = layout.pad_pitch or 0.5
        pads_by_side = {'top': [], 'bottom': [], 'left': [], 'right': []}
        for pad in fp.pads:
            if pad.net_id <= 0:
                continue
            info = analyze_pad(pad, layout)
            if info.side in pads_by_side:
                pads_by_side[info.side].append((pad, info))

        for side, edge_pads in pads_by_side.items():
            if len(edge_pads) < 2:
                continue

            # Sort pads along the edge by local coordinate
            if side in ('top', 'bottom'):
                edge_pads.sort(key=lambda item: item[0].local_x)
            else:
                edge_pads.sort(key=lambda item: item[0].local_y)

            # Measure distance from center of edge (inner pads are near center)
            n_pads = len(edge_pads)
            center_idx = (n_pads - 1) / 2.0

            # Find constraint level for each pad:
            # Flanked inner pad (dist_from_center smaller) has fewer exit directions.
            pad_constraints = []
            for i, (pad, info) in enumerate(edge_pads):
                # Check if flanked on both sides by close neighbors
                has_prev = (i > 0 and abs(
                    (edge_pads[i][0].local_x if side in ('top', 'bottom') else edge_pads[i][0].local_y) -
                    (edge_pads[i-1][0].local_x if side in ('top', 'bottom') else edge_pads[i-1][0].local_y)
                ) <= 1.5 * pitch)
                has_next = (i < n_pads - 1 and abs(
                    (edge_pads[i+1][0].local_x if side in ('top', 'bottom') else edge_pads[i+1][0].local_y) -
                    (edge_pads[i][0].local_x if side in ('top', 'bottom') else edge_pads[i][0].local_y)
                ) <= 1.5 * pitch)

                # Fewer open exit directions = lower rank number (higher priority)
                if has_prev and has_next:
                    # Flanked on both sides: only 1 outward direction
                    open_dirs = 1
                elif has_prev or has_next:
                    # Corner/end pad: 2+ exit directions
                    open_dirs = 2
                else:
                    open_dirs = 3

                dist_from_center = abs(i - center_idx)
                pad_constraints.append((pad.net_id, open_dirs, dist_from_center))

            # Pairwise precedence on the same package edge:
            # Inner/constrained pads (open_dirs=1) must precede less-constrained pads (open_dirs>=2)
            for i in range(len(pad_constraints)):
                net_i, dirs_i, dist_i = pad_constraints[i]
                for j in range(i + 1, len(pad_constraints)):
                    net_j, dirs_j, dist_j = pad_constraints[j]
                    if net_i == net_j:
                        continue
                    if dirs_i < dirs_j or (dirs_i == dirs_j and dist_i < dist_j):
                        precedence_edges.add((net_i, net_j))
                    elif dirs_j < dirs_i or (dirs_i == dirs_j and dist_j < dist_i):
                        precedence_edges.add((net_j, net_i))

    if not precedence_edges:
        return net_ids

    # Topological ordering respecting precedence, preserving original order as tie-breaker
    items = [nid for _, nid in net_ids]
    item_set = set(items)
    orig_idx = {nid: i for i, nid in enumerate(items)}
    id_to_tuple = {nid: (name, nid) for name, nid in net_ids}

    adj = {nid: [] for nid in items}
    in_degree = {nid: 0 for nid in items}

    for u, v in precedence_edges:
        if u in item_set and v in item_set and u != v:
            adj[u].append(v)
            in_degree[v] += 1

    pq = [(orig_idx[nid], nid) for nid in items if in_degree[nid] == 0]
    heapq.heapify(pq)

    ordered_nids = []
    while pq:
        idx, u = heapq.heappop(pq)
        ordered_nids.append(u)
        for v in adj[u]:
            in_degree[v] -= 1
            if in_degree[v] == 0:
                heapq.heappush(pq, (orig_idx[v], v))

    if len(ordered_nids) < len(items):
        remaining = [x for x in items if x not in set(ordered_nids)]
        ordered_nids.extend(remaining)

    return [id_to_tuple[nid] for nid in ordered_nids if nid in id_to_tuple]

