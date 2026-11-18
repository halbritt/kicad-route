use crate::budget::WorkBudget;
use crate::components::{span_mask, Copper};
use crate::model::{Failure, Point, Status, Track, Via, ViaKind};
use crate::rules::EffectiveRules;
use crate::Engine;
use std::cmp::Reverse;
use std::collections::{BTreeSet, BinaryHeap, HashMap};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub(crate) struct PlacedViaKey {
    pub at: Point,
    pub start_layer: u8,
    pub end_layer: u8,
    pub kind: u8,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum PathEdge {
    Track { start: Point, end: Point, layer: u8 },
    Via(PlacedViaKey),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Hash)]
struct State {
    at: Point,
    layer: u8,
    direction: u8,
    arrived_by_via: bool,
    is_via_goal: bool,
    via_history: u32,
    via_count: u16,
}

#[derive(Clone, Copy, Debug)]
struct ConflictZone {
    center: Point,
    radius_sq_upper: i128,
}

#[derive(Clone, Debug, Default)]
struct ConflictFilter {
    zones: Vec<ConflictZone>,
}

impl ConflictFilter {
    fn contains(&self, p: Point) -> bool {
        self.zones.iter().any(|z| {
            let dx = i128::from(p[0] - z.center[0]);
            let dy = i128::from(p[1] - z.center[1]);
            dx * dx + dy * dy <= z.radius_sq_upper
        })
    }
}

struct BackwardHeuristic {
    dist: HashMap<(Point, u8), u64>,
    fallback: u64,
}

#[derive(Debug)]
pub struct Path {
    pub points: Vec<Point>,
    pub(crate) edges: Vec<PathEdge>,
    pub steps: u64,
}

struct ViaHistoryArena {
    entries: Vec<Vec<PlacedViaKey>>,
    intern: HashMap<Vec<PlacedViaKey>, u32>,
    total_len: usize,
}

impl ViaHistoryArena {
    fn new() -> Self {
        let mut intern = HashMap::new();
        intern.insert(Vec::new(), 0);
        Self {
            entries: vec![Vec::new()],
            intern,
            total_len: 0,
        }
    }

    fn get(&self, id: u32) -> &[PlacedViaKey] {
        &self.entries[id as usize]
    }

    fn can_append(&self, id: u32, key: PlacedViaKey) -> bool {
        let key_mask = span_mask(key.start_layer as usize, key.end_layer as usize);
        !self.entries[id as usize].iter().any(|existing| {
            existing.at == key.at
                && (span_mask(existing.start_layer as usize, existing.end_layer as usize)
                    & key_mask)
                    != 0
        })
    }

    fn append(&mut self, id: u32, key: PlacedViaKey) -> Option<u32> {
        if !self.can_append(id, key) {
            return None;
        }
        let mut vec = self.entries[id as usize].clone();
        vec.push(key);
        vec.sort_unstable();
        vec.dedup();
        if let Some(&existing_id) = self.intern.get(&vec) {
            return Some(existing_id);
        }
        let new_id = u32::try_from(self.entries.len()).expect("bounded via history arena");
        self.total_len += vec.len();
        self.intern.insert(vec.clone(), new_id);
        self.entries.push(vec);
        Some(new_id)
    }

    fn is_subset(&self, sub_id: u32, sup_id: u32) -> bool {
        if sub_id == 0 || sub_id == sup_id {
            return true;
        }
        let a = &self.entries[sub_id as usize];
        let b = &self.entries[sup_id as usize];
        if a.len() > b.len() {
            return false;
        }
        let (mut ia, mut ib) = (0usize, 0usize);
        while ia < a.len() && ib < b.len() {
            match a[ia].cmp(&b[ib]) {
                std::cmp::Ordering::Less => return false,
                std::cmp::Ordering::Equal => {
                    ia += 1;
                    ib += 1;
                }
                std::cmp::Ordering::Greater => ib += 1,
            }
        }
        ia == a.len()
    }
}

/// Four-direction unit-cost A*. Incoming direction is included in the key:
/// no location-only pruning can hide a different continuation state. This
/// slice has no bend penalty; Manhattan distance is an admissible heuristic.
pub fn search(
    engine: &Engine,
    net: u32,
    start: Point,
    goal: Point,
    limit: u64,
    expanded: &mut u64,
) -> Result<Path, Failure> {
    let _scope = engine.work.scope();
    search_inner(engine, net, start, goal, limit, expanded).map_err(|mut e| {
        e.work_used = engine.work.used();
        e.expanded_states = *expanded;
        e
    })
}
fn search_inner(
    engine: &Engine,
    net: u32,
    start: Point,
    goal: Point,
    limit: u64,
    expanded: &mut u64,
) -> Result<Path, Failure> {
    if net == 0 {
        return Err(Failure::new(
            Status::Invalid,
            "net 0 is unconnected copper and cannot be routed",
        ));
    }
    let grid = engine.input.rules.grid_nm;
    let heuristic =
        |p: Point| ((p[0] - goal[0]).abs() / grid + (p[1] - goal[1]).abs() / grid) as u64;
    if !engine.legal_edge(net, start, start)? || !engine.legal_edge(net, goal, goal)? {
        return Err(Failure::new(
            Status::Unroutable,
            "terminal center violates routing clearance or board edge",
        ));
    }
    walk(
        engine,
        &[start],
        limit,
        expanded,
        |p| Ok(heuristic(p)),
        |_, a, b| Ok(engine.legal_edge(net, a, b)?.then_some(b)),
        |at, _| Ok(at == goal),
    )
}

/// Unit-cost multi-source A* search. The existing copper component is free to
/// traverse; each legal pad-center/track-endpoint starts its own grid phase.
/// A target may attach anywhere along the last swept edge, not only at a
/// grid node or an incumbent endpoint. This is a bounded grid search, not a
/// proof that no continuous attachment exists when the frontier is exhausted.
pub(crate) fn join_components(
    engine: &Engine,
    net: u32,
    source: &[Copper<'_>],
    targets: &[Vec<Copper<'_>>],
    limit: u64,
    expanded: &mut u64,
) -> Result<Path, Failure> {
    let _scope = engine.work.scope();
    if targets.iter().flatten().next().is_none() {
        let mut failure = Failure::new(
            Status::Unroutable,
            "component join requires at least one target item",
        );
        failure.work_used = engine.work.used();
        failure.expanded_states = *expanded;
        return Err(failure);
    }
    let stackup = engine.all_layers_slice();
    let l_count = engine.layer_count();
    let mut starts: BTreeSet<(Point, u8)> = BTreeSet::new();
    for c in source {
        engine.work.charge(1)?;
        let c_mask = c.layer_mask(stackup);
        let anchors = c.anchors(engine.input.bounds.min, engine.input.rules.grid_nm);
        for l_idx in 0..l_count {
            if (c_mask & (1u32 << l_idx)) == 0 {
                continue;
            }
            let l_name = engine.layer_name(l_idx);
            let width_nm = engine.effective_rules.track_width_for(net, l_name)?;
            for &p in &anchors {
                engine.work.charge(1)?;
                if !engine.legal_edge_on(net, l_idx, p, p)? {
                    continue;
                }
                let probe = Track {
                    id: String::new(),
                    net,
                    start: p,
                    end: p,
                    width_nm,
                    locked: false,
                    layer: l_name.to_owned(),
                };
                for s in source {
                    if s.touches_route(&probe, stackup, &engine.work)? {
                        let key = (p, l_idx as u8);
                        engine
                            .work
                            .search_storage(starts.len() + usize::from(!starts.contains(&key)))?;
                        starts.insert(key);
                        break;
                    }
                }
            }
        }
    }
    if starts.is_empty() {
        let mut failure = Failure::new(
            Status::Unroutable,
            "component has no legal pad-center or incumbent-endpoint search seed",
        );
        failure.work_used = engine.work.used();
        failure.expanded_states = *expanded;
        return Err(failure);
    }
    let attachment_from = |layer_idx: usize,
                           anchor: Point,
                           a: Point,
                           b: Point,
                           prior_vias: &[PlacedViaKey]|
     -> Result<Option<Point>, Failure> {
        let l_name = engine.layer_name(layer_idx);
        let width_nm = engine.effective_rules.track_width_for(net, l_name)?;
        let mut nearest = None;
        for c in targets.iter().flatten() {
            engine.work.charge(1)?;
            if let Some(p) = c.attachment(a, b, net, width_nm, l_name, stackup, &engine.work)? {
                if engine.legal_edge_on(net, layer_idx, anchor, p)?
                    && engine.edge_clears_same_net_vias(net, layer_idx, anchor, p, prior_vias)?
                {
                    let key = |p: Point| ((p[0] - a[0]).abs() + (p[1] - a[1]).abs(), p);
                    if nearest.is_none_or(|old| key(p) < key(old)) {
                        nearest = Some(p);
                    }
                }
            }
        }
        Ok(nearest)
    };
    let attachment = |layer_idx: usize, a: Point, b: Point, prior_vias: &[PlacedViaKey]| {
        attachment_from(layer_idx, a, a, b, prior_vias)
    };

    let (max_via_diam, max_net_drill) = if l_count > 1 {
        let mut max_d = 0i64;
        let mut max_dr = 0i64;
        let mut consider_span = |is_micro: bool, span: &[String]| {
            if !engine.effective_rules.disallow_via_on(net, span) {
                if let Ok((d, dr)) = engine.effective_rules.via_dims_for(net, is_micro, span) {
                    max_d = max_d.max(d);
                    max_dr = max_dr.max(dr);
                }
            }
        };
        consider_span(false, stackup);
        if engine.effective_rules.allow_blind_buried_vias {
            for s_min in 0..l_count {
                for s_max in (s_min + 1)..l_count {
                    if s_min == 0 && s_max + 1 == l_count {
                        continue;
                    }
                    consider_span(false, &stackup[s_min..=s_max]);
                }
            }
        }
        if engine.effective_rules.allow_microvias {
            consider_span(true, &stackup[0..=1]);
            consider_span(true, &stackup[l_count - 2..=l_count - 1]);
        }
        (max_d, max_dr)
    } else {
        (0, 0)
    };
    let mut max_net_track_width = 0i64;
    for l_name in stackup {
        max_net_track_width =
            max_net_track_width.max(engine.effective_rules.track_width_for(net, l_name)?);
    }
    let max_eff_width = max_net_track_width.max(max_via_diam);

    let max_h2h = engine.effective_rules.max_hole_to_hole_nm;
    let conflict_radius_upper = i128::from(max_net_drill) + i128::from(max_h2h);
    let conflict_radius_sq_upper = conflict_radius_upper * conflict_radius_upper;

    let via_cost = engine.effective_rules.via_cost.max(1);
    let start_vec: Vec<(Point, u8)> = starts.into_iter().collect();
    let track_direction_matters = l_count == 1 || engine.input.rules.same_net_hole_clearance_nm > 0;
    let track_via_count = l_count > 1 && engine.effective_rules.net(net).max_via_count.is_some();

    let mut conflict_filter = ConflictFilter::default();
    let mut backward_h: Option<(u64, BackwardHeuristic)> = None;

    loop {
        let result = walk_multilayer(
            engine,
            net,
            &start_vec,
            limit,
            expanded,
            track_direction_matters,
            track_via_count,
            &conflict_filter,
            |p, layer_idx| {
                let h_comp = component_estimate_multilayer(
                    p,
                    layer_idx,
                    stackup,
                    targets,
                    &engine.effective_rules,
                    net,
                    max_eff_width,
                    via_cost,
                    engine.input.rules.grid_nm,
                    &engine.work,
                )?;
                if h_comp == 0 {
                    return Ok(0);
                }
                if let Some((_, ref bwd)) = backward_h {
                    let h_bwd = bwd
                        .dist
                        .get(&(p, layer_idx as u8))
                        .copied()
                        .unwrap_or(bwd.fallback)
                        .min(bwd.fallback);
                    return Ok(h_comp.max(h_bwd));
                }
                Ok(h_comp)
            },
            |layer_idx, run_start, a, b, prior_vias| {
                let l_name = engine.layer_name(layer_idx);
                let width_nm = engine.effective_rules.track_width_for(net, l_name)?;
                if let Some(p) = attachment(layer_idx, a, b, prior_vias)? {
                    return Ok(Some(p));
                }
                if engine.legal_edge_on(net, layer_idx, a, b)?
                    && engine.edge_clears_same_net_vias(net, layer_idx, a, b, prior_vias)?
                {
                    return Ok(Some(b));
                }
                if engine.input.rules.same_net_hole_clearance_nm > 0 {
                    if let Some(rs) = run_start {
                        if let Some(p) = attachment_from(layer_idx, rs, a, b, prior_vias)? {
                            return Ok(Some(p));
                        }
                        if engine.legal_edge_on(net, layer_idx, rs, b)?
                            && engine
                                .edge_clears_same_net_vias(net, layer_idx, rs, b, prior_vias)?
                        {
                            return Ok(Some(b));
                        }
                    }
                    let delta = [b[0] - a[0], b[1] - a[1]];
                    let axis = usize::from(delta[0] == 0);
                    let sign = delta[axis].signum();
                    let route_radius = (width_nm + 1) / 2;
                    let mut best_entry: Option<Point> = None;
                    for c in targets.iter().flatten() {
                        let Copper::Pad(pad) = *c else {
                            continue;
                        };
                        if pad.drill.is_none() || (c.layer_mask(stackup) & (1u32 << layer_idx)) == 0
                        {
                            continue;
                        }
                        let (lo, hi) = pad.copper_bounds();
                        if a[1 - axis] < lo[1 - axis] - route_radius
                            || a[1 - axis] > hi[1 - axis] + route_radius
                        {
                            continue;
                        }
                        if (sign > 0 && hi[axis] < a[axis]) || (sign < 0 && lo[axis] > a[axis]) {
                            continue;
                        }
                        let mut far = a;
                        far[axis] = if sign > 0 {
                            (hi[axis] + route_radius + engine.input.rules.grid_nm)
                                .min(engine.input.bounds.max[axis])
                        } else {
                            (lo[axis] - route_radius - engine.input.rules.grid_nm)
                                .max(engine.input.bounds.min[axis])
                        };
                        if far == a {
                            continue;
                        }
                        let hit = match attachment(layer_idx, a, far, prior_vias)? {
                            Some(p) => Some(p),
                            None => match run_start {
                                Some(rs) => attachment_from(layer_idx, rs, a, far, prior_vias)?,
                                None => None,
                            },
                        };
                        if let Some(p) = hit {
                            let key = |pt: Point| ((pt[0] - a[0]).abs() + (pt[1] - a[1]).abs(), pt);
                            if best_entry.is_none_or(|old| key(p) < key(old)) {
                                best_entry = Some(p);
                            }
                        }
                    }
                    if best_entry.is_some() {
                        return Ok(best_entry);
                    }
                }
                Ok(None)
            },
            |at, layer_idx, prior_vias, prior_via_count, out| {
                if l_count < 2 {
                    return Ok(());
                }
                if let Some(max_vc) = engine.effective_rules.net(net).max_via_count {
                    if engine.copper.via_count_for_net(net) + usize::from(prior_via_count) + 1
                        > max_vc as usize
                    {
                        return Ok(());
                    }
                }
                push_legal_via_transitions(engine, net, at, layer_idx, via_cost, prior_vias, out)
            },
            |at, layer_idx, edge| {
                let Some(prev_edge) = edge else {
                    return Ok(false);
                };
                match prev_edge {
                    PathEdge::Track { .. } => {
                        let l_name = engine.layer_name(layer_idx);
                        let width_nm = engine.effective_rules.track_width_for(net, l_name)?;
                        let probe = Track {
                            id: String::new(),
                            net,
                            start: at,
                            end: at,
                            width_nm,
                            locked: false,
                            layer: l_name.to_owned(),
                        };
                        for c in targets.iter().flatten() {
                            if c.touches_route(&probe, stackup, &engine.work)? {
                                return Ok(true);
                            }
                        }
                        Ok(false)
                    }
                    PathEdge::Via(pv) => {
                        let kind = match pv.kind {
                            0 => ViaKind::Through,
                            1 => ViaKind::BlindBuried,
                            _ => ViaKind::Micro,
                        };
                        let span_layers = &stackup[pv.start_layer as usize..=pv.end_layer as usize];
                        let (diam, drill) = engine.effective_rules.via_dims_for(
                            net,
                            kind == ViaKind::Micro,
                            span_layers,
                        )?;
                        let probe_via = Via {
                            id: String::new(),
                            net,
                            at: pv.at,
                            diameter_nm: diam,
                            drill_nm: drill,
                            start_layer: stackup[pv.start_layer as usize].clone(),
                            end_layer: stackup[pv.end_layer as usize].clone(),
                            kind,
                            locked: false,
                        };
                        let via_mask = span_mask(pv.start_layer as usize, pv.end_layer as usize);
                        for c in targets.iter().flatten() {
                            if (c.layer_mask(stackup) & via_mask) != 0
                                && c.touches_via(&probe_via, stackup, &engine.work)?
                            {
                                return Ok(true);
                            }
                        }
                        Ok(false)
                    }
                }
            },
        )?;

        let path_via_count = result
            .edges
            .iter()
            .filter(|e| matches!(e, PathEdge::Via(_)))
            .count();
        if l_count < 2
            || (path_via_count < 2
                && (engine.input.rules.same_net_hole_clearance_nm == 0 || path_via_count == 0))
        {
            return Ok(result);
        }

        if path_via_count >= 2 {
            if let Some((v_a, v_b)) = first_via_conflict(engine, net, &result)? {
                let dx = i128::from(v_a.at[0] - v_b.at[0]);
                let dy = i128::from(v_a.at[1] - v_b.at[1]);
                let pair_sq = dx * dx + dy * dy;
                conflict_filter.zones.push(ConflictZone {
                    center: v_b.at,
                    radius_sq_upper: conflict_radius_sq_upper.max(pair_sq),
                });
                if engine.input.rules.same_net_hole_clearance_nm == 0 {
                    let needed_cutoff = result.steps.saturating_add(via_cost);
                    if backward_h.as_ref().is_none_or(|(m, _)| needed_cutoff > *m) {
                        let bwd = compute_backward_heuristic(
                            engine,
                            net,
                            &start_vec,
                            targets,
                            max_eff_width,
                            via_cost,
                            needed_cutoff,
                            limit,
                        )?;
                        backward_h = Some((needed_cutoff, bwd));
                    }
                }
                continue;
            }
        }
        if engine.input.rules.same_net_hole_clearance_nm > 0 {
            if let Some(conflict_pv) = first_same_net_via_track_conflict(engine, net, &result)? {
                if conflict_filter.contains(conflict_pv.at) {
                    return Err(Failure::new(
                        Status::Unroutable,
                        "proposed path violates same-net via hole clearance",
                    ));
                }
                conflict_filter.zones.push(ConflictZone {
                    center: conflict_pv.at,
                    radius_sq_upper: 0,
                });
                continue;
            }
        }
        return Ok(result);
    }
}

fn push_legal_via_transitions(
    engine: &Engine,
    net: u32,
    at: Point,
    layer_idx: usize,
    via_cost: u64,
    prior_vias: &[PlacedViaKey],
    out: &mut Vec<(u8, PlacedViaKey, u64)>,
) -> Result<(), Failure> {
    let l_count = engine.layer_count();
    if l_count < 2 {
        return Ok(());
    }
    let can_blind = engine.effective_rules.allow_blind_buried_vias;
    let can_micro = engine.effective_rules.allow_microvias;
    let has_overlap = |s_idx: usize, e_idx: usize| -> bool {
        let m = span_mask(s_idx, e_idx);
        prior_vias.iter().any(|existing| {
            existing.at == at
                && (span_mask(existing.start_layer as usize, existing.end_layer as usize) & m) != 0
        })
    };

    if !has_overlap(0, l_count - 1)
        && engine
            .legal_via(net, at, 0, l_count - 1, ViaKind::Through, prior_vias)?
            .is_some()
    {
        let pv = PlacedViaKey {
            at,
            start_layer: 0,
            end_layer: (l_count - 1) as u8,
            kind: 0,
        };
        for target_layer in 0..l_count {
            if target_layer != layer_idx {
                out.push((target_layer as u8, pv, via_cost));
            }
        }
    }

    if can_blind || can_micro {
        for target_layer in 0..l_count {
            if target_layer == layer_idx {
                continue;
            }
            let s_min = layer_idx.min(target_layer);
            let s_max = layer_idx.max(target_layer);
            if can_blind && (s_min > 0 || s_max + 1 < l_count) && !has_overlap(s_min, s_max) {
                if engine
                    .legal_via(net, at, s_min, s_max, ViaKind::BlindBuried, prior_vias)?
                    .is_some()
                {
                    out.push((
                        target_layer as u8,
                        PlacedViaKey {
                            at,
                            start_layer: s_min as u8,
                            end_layer: s_max as u8,
                            kind: 1,
                        },
                        via_cost,
                    ));
                }
            }
            if can_micro
                && s_max == s_min + 1
                && (s_min == 0 || s_max + 1 == l_count)
                && !has_overlap(s_min, s_max)
            {
                if engine
                    .legal_via(net, at, s_min, s_max, ViaKind::Micro, prior_vias)?
                    .is_some()
                {
                    out.push((
                        target_layer as u8,
                        PlacedViaKey {
                            at,
                            start_layer: s_min as u8,
                            end_layer: s_max as u8,
                            kind: 2,
                        },
                        via_cost,
                    ));
                }
            }
        }
    }
    Ok(())
}

fn first_via_conflict(
    engine: &Engine,
    net: u32,
    path: &Path,
) -> Result<Option<(PlacedViaKey, PlacedViaKey)>, Failure> {
    let mut prior_vias: Vec<PlacedViaKey> = Vec::new();
    for edge in &path.edges {
        engine.work.charge(1)?;
        let PathEdge::Via(pv) = *edge else {
            continue;
        };
        let pv_mask = span_mask(pv.start_layer as usize, pv.end_layer as usize);
        if let Some(&prev) = prior_vias.iter().find(|existing| {
            existing.at == pv.at
                && (span_mask(existing.start_layer as usize, existing.end_layer as usize) & pv_mask)
                    != 0
        }) {
            return Ok(Some((prev, pv)));
        }
        let kind = match pv.kind {
            0 => ViaKind::Through,
            1 => ViaKind::BlindBuried,
            _ => ViaKind::Micro,
        };
        if engine
            .legal_via(
                net,
                pv.at,
                pv.start_layer as usize,
                pv.end_layer as usize,
                kind,
                &prior_vias,
            )?
            .is_none()
        {
            for &prev in &prior_vias {
                if engine
                    .legal_via(
                        net,
                        pv.at,
                        pv.start_layer as usize,
                        pv.end_layer as usize,
                        kind,
                        &[prev],
                    )?
                    .is_none()
                {
                    return Ok(Some((prev, pv)));
                }
            }
            return Ok(Some((pv, pv)));
        }
        prior_vias.push(pv);
    }
    Ok(None)
}

fn first_same_net_via_track_conflict(
    engine: &Engine,
    net: u32,
    path: &Path,
) -> Result<Option<PlacedViaKey>, Failure> {
    if engine.input.rules.same_net_hole_clearance_nm <= 0 || net == 0 {
        return Ok(None);
    }
    let mut coalesced_tracks: Vec<(usize, Point, Point)> = Vec::new();
    let mut path_vias: Vec<PlacedViaKey> = Vec::new();
    let mut can_coalesce_last = false;
    for edge in &path.edges {
        engine.work.charge(1)?;
        match *edge {
            PathEdge::Track { start, end, layer } => {
                let l = layer as usize;
                if can_coalesce_last {
                    if let Some((last_l, last_start, last_end)) = coalesced_tracks.last_mut() {
                        if *last_l == l
                            && *last_end == start
                            && ((last_start[0] == last_end[0] && last_end[0] == end[0])
                                || (last_start[1] == last_end[1] && last_end[1] == end[1]))
                        {
                            *last_end = end;
                            continue;
                        }
                    }
                }
                coalesced_tracks.push((l, start, end));
                can_coalesce_last = true;
            }
            PathEdge::Via(pv) => {
                can_coalesce_last = false;
                path_vias.push(pv);
            }
        }
    }
    for pv in path_vias {
        for &(l_idx, start, end) in &coalesced_tracks {
            if !engine.edge_clears_same_net_vias(net, l_idx, start, end, &[pv])? {
                return Ok(Some(pv));
            }
        }
    }
    Ok(None)
}

#[allow(clippy::too_many_arguments)]
fn compute_backward_heuristic(
    engine: &Engine,
    net: u32,
    starts: &[(Point, u8)],
    targets: &[Vec<Copper<'_>>],
    max_eff_width: i64,
    via_cost: u64,
    max_cost: u64,
    limit: u64,
) -> Result<BackwardHeuristic, Failure> {
    let stackup = engine.all_layers_slice();
    let l_count = engine.layer_count();
    let grid = engine.input.rules.grid_nm;
    let route_radius = (max_eff_width + 1) / 2;
    let bounds = engine.input.bounds;

    let mut phases: BTreeSet<(i64, i64)> = BTreeSet::new();
    for &(s, _) in starts {
        phases.insert((s[0].rem_euclid(grid), s[1].rem_euclid(grid)));
    }

    let mut best_d: HashMap<(Point, u8), u64> = HashMap::new();
    let mut settled_d: HashMap<(Point, u8), u64> = HashMap::new();
    let mut pq: BinaryHeap<Reverse<(u64, Point, u8)>> = BinaryHeap::new();

    for target in targets.iter().flatten() {
        let t_mask = target.layer_mask(stackup);
        let (lo, hi) = match *target {
            Copper::Pad(pad) => pad.copper_bounds(),
            Copper::Track(track) => track.copper_bounds(),
            Copper::Graphic(graphic) => graphic.copper_bounds(),
            Copper::Via(via) => {
                let r = (via.diameter_nm + 1) / 2;
                (
                    [via.at[0] - r, via.at[1] - r],
                    [via.at[0] + r, via.at[1] + r],
                )
            }
        };
        let min_x = lo[0].saturating_sub(route_radius + grid).max(bounds.min[0]);
        let max_x = hi[0].saturating_add(route_radius + grid).min(bounds.max[0]);
        let min_y = lo[1].saturating_sub(route_radius + grid).max(bounds.min[1]);
        let max_y = hi[1].saturating_add(route_radius + grid).min(bounds.max[1]);

        for &(ox, oy) in &phases {
            let x_rem = (ox - min_x.rem_euclid(grid)).rem_euclid(grid);
            let mut x = min_x.saturating_add(x_rem);
            while x <= max_x {
                let y_rem = (oy - min_y.rem_euclid(grid)).rem_euclid(grid);
                let mut y = min_y.saturating_add(y_rem);
                while y <= max_y {
                    let p = [x, y];
                    let d = contact_steps(p, *target, max_eff_width, grid);
                    if d <= 1 && d <= max_cost {
                        for l_idx in 0..l_count {
                            if (t_mask & (1u32 << l_idx)) == 0 {
                                continue;
                            }
                            engine.work.charge(1)?;
                            let key = (p, l_idx as u8);
                            if best_d.get(&key).is_none_or(|&old| d < old) {
                                engine.work.search_storage(
                                    pq.len() + best_d.len() + settled_d.len() + 2,
                                )?;
                                best_d.insert(key, d);
                                pq.push(Reverse((d, p, l_idx as u8)));
                            }
                        }
                    }
                    let Some(ny) = y.checked_add(grid) else {
                        break;
                    };
                    y = ny;
                }
                let Some(nx) = x.checked_add(grid) else {
                    break;
                };
                x = nx;
            }
        }
    }

    let max_pops = limit.max(50_000);
    let mut pops = 0u64;
    let mut fallback = max_cost.saturating_add(1);
    let mut via_buf: Vec<(u8, PlacedViaKey, u64)> = Vec::new();

    while let Some(Reverse((d, p, l))) = pq.pop() {
        engine.work.charge(1)?;
        if best_d.get(&(p, l)) != Some(&d) {
            continue;
        }
        if d > max_cost {
            fallback = max_cost.saturating_add(1);
            break;
        }
        if pops >= max_pops {
            fallback = d;
            break;
        }
        pops += 1;
        settled_d.insert((p, l), d);
        if d == max_cost {
            continue;
        }

        let nd = d.saturating_add(1);
        for delta in [[grid, 0], [0, grid], [-grid, 0], [0, -grid]] {
            engine.work.charge(1)?;
            let Some(qx) = p[0].checked_add(delta[0]) else {
                continue;
            };
            let Some(qy) = p[1].checked_add(delta[1]) else {
                continue;
            };
            let q = [qx, qy];
            let key = (q, l);
            if best_d.get(&key).is_some_and(|&old| old <= nd) {
                continue;
            }
            if !engine.legal_edge_on(net, l as usize, q, p)? {
                continue;
            }
            engine
                .work
                .search_storage(pq.len() + best_d.len() + settled_d.len() + 2)?;
            best_d.insert(key, nd);
            pq.push(Reverse((nd, q, l)));
        }

        if l_count >= 2 && d.saturating_add(via_cost) <= max_cost {
            via_buf.clear();
            push_legal_via_transitions(engine, net, p, l as usize, via_cost, &[], &mut via_buf)?;
            for &(prev_l, _, v_cost) in &via_buf {
                let nvd = d.saturating_add(v_cost);
                if nvd > max_cost {
                    continue;
                }
                let key = (p, prev_l);
                if best_d.get(&key).is_some_and(|&old| old <= nvd) {
                    continue;
                }
                engine
                    .work
                    .search_storage(pq.len() + best_d.len() + settled_d.len() + 2)?;
                best_d.insert(key, nvd);
                pq.push(Reverse((nvd, p, prev_l)));
            }
        }
    }

    Ok(BackwardHeuristic {
        dist: settled_d,
        fallback,
    })
}

#[cfg(test)]
fn component_estimate(
    p: Point,
    targets: &[Vec<Copper<'_>>],
    rules: &crate::model::Rules,
    work: &WorkBudget,
) -> Result<u64, Failure> {
    let mut lower_bound: Option<u64> = None;
    for target in targets.iter().flatten() {
        work.charge(1)?;
        let steps = contact_steps(p, *target, rules.width_nm, rules.grid_nm);
        lower_bound = Some(lower_bound.map_or(steps, |best| best.min(steps)));
    }
    lower_bound.ok_or_else(|| {
        Failure::new(
            Status::Unroutable,
            "component join requires at least one target item",
        )
    })
}

#[allow(clippy::too_many_arguments)]
fn component_estimate_multilayer(
    p: Point,
    layer_idx: usize,
    stackup: &[String],
    targets: &[Vec<Copper<'_>>],
    rules: &EffectiveRules,
    net: u32,
    max_eff_width: i64,
    via_cost: u64,
    grid: i64,
    work: &WorkBudget,
) -> Result<u64, Failure> {
    let l_name = &stackup[layer_idx];
    let width = rules.track_width_for(net, l_name)?;
    let eff_width = width.max(max_eff_width);
    let my_bit = 1u32 << layer_idx;
    let mut lower_bound: Option<u64> = None;
    for target in targets.iter().flatten() {
        work.charge(1)?;
        let d_via = contact_steps(p, *target, eff_width, grid);
        let steps = if (target.layer_mask(stackup) & my_bit) != 0 {
            let d_track = contact_steps(p, *target, width, grid);
            d_track.min(d_via.saturating_add(via_cost))
        } else {
            d_via.saturating_add(via_cost)
        };
        lower_bound = Some(lower_bound.map_or(steps, |best| best.min(steps)));
    }
    lower_bound.ok_or_else(|| {
        Failure::new(
            Status::Unroutable,
            "component join requires at least one target item",
        )
    })
}

fn isqrt_u128(n: u128) -> u128 {
    if n < 2 {
        return n;
    }
    let mut x = 1u128 << ((128 - n.leading_zeros()).div_ceil(2));
    loop {
        let next = (x + n / x) >> 1;
        if next >= x {
            return x;
        }
        x = next;
    }
}

/// Lower bound to a superset of all endpoint-cap contacts with this target.
/// Each orthogonal edge moves at most `grid` in one axis, including a shortened
/// final edge. The sum of ceil(axis gap / grid) therefore cannot exceed the
/// remaining step cost. Full copper bounds plus the rounded-up route radius
/// contain even the conservative one-nm-overlap contact set. For circular pads
/// and vias, Cauchy-Schwarz also bounds `|dx| + |dy| <= isqrt(2 * R_sum^2)`.
/// Ignoring obstacles and rounding outward can only decrease this bound. Each
/// bound changes by at most one across an edge; the minimum over all targets is
/// also consistent and is zero at every accepted contact endpoint.
fn contact_steps(p: Point, target: Copper<'_>, width: i64, grid: i64) -> u64 {
    let route_radius = (width + 1) / 2;
    let (lo, hi, circle_center_and_radius) = match target {
        Copper::Pad(pad) => {
            let (lo, hi) = pad.copper_bounds();
            let c = match pad.shape {
                crate::model::Shape::Circle { radius_nm } => Some((pad.at, radius_nm)),
                _ => None,
            };
            (lo, hi, c)
        }
        Copper::Track(track) => {
            let (lo, hi) = track.copper_bounds();
            (lo, hi, None)
        }
        Copper::Graphic(graphic) => {
            let (lo, hi) = graphic.copper_bounds();
            (lo, hi, None)
        }
        Copper::Via(via) => {
            let r = (via.diameter_nm + 1) / 2;
            (
                [via.at[0] - r, via.at[1] - r],
                [via.at[0] + r, via.at[1] + r],
                Some((via.at, r)),
            )
        }
    };
    let lo: Point = std::array::from_fn(|k| lo[k] - route_radius);
    let hi: Point = std::array::from_fn(|k| hi[k] + route_radius);
    let aabb_steps: u64 = (0..2)
        .map(|k| {
            let gap = (lo[k] - p[k]).max(p[k] - hi[k]).max(0);
            ((gap + grid - 1) / grid) as u64
        })
        .sum();
    if let Some((center, rad)) = circle_center_and_radius {
        let r_sum = u128::try_from(rad.max(0) + route_radius.max(0)).unwrap_or(0);
        let l1_limit = isqrt_u128(2 * r_sum * r_sum);
        let l1_dist = (p[0] - center[0]).abs() as u128 + (p[1] - center[1]).abs() as u128;
        if l1_dist > l1_limit {
            let gap = l1_dist - l1_limit;
            let g = grid as u128;
            let diag_steps = ((gap + g - 1) / g) as u64;
            return aabb_steps.max(diag_steps);
        }
    }
    aabb_steps
}

fn walk(
    engine: &Engine,
    starts: &[Point],
    limit: u64,
    expanded: &mut u64,
    heuristic: impl Fn(Point) -> Result<u64, Failure>,
    advance: impl Fn(Option<Point>, Point, Point) -> Result<Option<Point>, Failure>,
    finished: impl Fn(Point, Option<Point>) -> Result<bool, Failure>,
) -> Result<Path, Failure> {
    let start_vec: Vec<(Point, u8)> = starts.iter().map(|&p| (p, 0)).collect();
    let empty_filter = ConflictFilter::default();
    walk_multilayer(
        engine,
        0,
        &start_vec,
        limit,
        expanded,
        true,
        false,
        &empty_filter,
        |p, _| heuristic(p),
        |_, rs, a, b, _| advance(rs, a, b),
        |_, _, _, _, _| Ok(()),
        |at, _, edge| {
            let prev = match edge {
                Some(PathEdge::Track { start, .. }) => Some(start),
                Some(PathEdge::Via(pv)) => Some(pv.at),
                None => None,
            };
            finished(at, prev)
        },
    )
}

#[allow(clippy::too_many_arguments)]
fn walk_multilayer(
    engine: &Engine,
    net: u32,
    starts: &[(Point, u8)],
    limit: u64,
    expanded: &mut u64,
    track_direction_matters: bool,
    track_via_count: bool,
    conflict_filter: &ConflictFilter,
    heuristic: impl Fn(Point, usize) -> Result<u64, Failure>,
    advance: impl Fn(
        usize,
        Option<Point>,
        Point,
        Point,
        &[PlacedViaKey],
    ) -> Result<Option<Point>, Failure>,
    via_transitions: impl Fn(
        Point,
        usize,
        &[PlacedViaKey],
        u16,
        &mut Vec<(u8, PlacedViaKey, u64)>,
    ) -> Result<(), Failure>,
    finished: impl Fn(Point, usize, Option<PathEdge>) -> Result<bool, Failure>,
) -> Result<Path, Failure> {
    let grid = engine.input.rules.grid_nm;
    let l_count = engine.layer_count();
    let mut via_arena = ViaHistoryArena::new();
    let mut queue = BinaryHeap::new();
    let mut best: HashMap<State, u64> = HashMap::new();
    let mut settled_by_pos: HashMap<(Point, u8, u8, bool, bool), Vec<(u32, u16, u64)>> =
        HashMap::new();
    let mut parent: HashMap<State, (State, PathEdge)> = HashMap::new();
    for &(at, layer) in starts {
        engine
            .work
            .search_storage(queue.len() + best.len() + parent.len() + via_arena.total_len + 2)?;
        engine.work.charge(1)?;
        let initial = State {
            at,
            layer,
            direction: 4,
            arrived_by_via: false,
            is_via_goal: false,
            via_history: 0,
            via_count: 0,
        };
        let h = heuristic(at, layer as usize)?;
        queue.push(Reverse((h, 0u64, initial)));
        best.insert(initial, 0u64);
    }

    let is_dominated_by_settled = |settled: &HashMap<
        (Point, u8, u8, bool, bool),
        Vec<(u32, u16, u64)>,
    >,
                                   arena: &ViaHistoryArena,
                                   cand: State,
                                   cand_g: u64,
                                   work: &WorkBudget|
     -> Result<bool, Failure> {
        if let Some(list) = settled.get(&(
            cand.at,
            cand.layer,
            cand.direction,
            cand.arrived_by_via,
            cand.is_via_goal,
        )) {
            for &(v1, vc1, g1) in list {
                work.charge(1)?;
                if g1 <= cand_g && vc1 <= cand.via_count && arena.is_subset(v1, cand.via_history) {
                    return Ok(true);
                }
            }
        }
        Ok(false)
    };

    let mut via_buf: Vec<(u8, PlacedViaKey, u64)> = Vec::new();
    while !queue.is_empty() {
        engine.work.charge(1)?;
        let Reverse((_f, cost, state)) = queue.pop().unwrap();
        if best.get(&state) != Some(&cost) {
            continue;
        }
        if is_dominated_by_settled(&settled_by_pos, &via_arena, state, cost, &engine.work)? {
            continue;
        }
        if *expanded >= limit {
            return Err(Failure::new(
                Status::BudgetExhausted,
                "total expansion budget exhausted",
            ));
        }
        *expanded = expanded.checked_add(1).expect("bounded expansion count");
        settled_by_pos
            .entry((
                state.at,
                state.layer,
                state.direction,
                state.arrived_by_via,
                state.is_via_goal,
            ))
            .or_default()
            .push((state.via_history, state.via_count, cost));

        if state.is_via_goal
            || finished(
                state.at,
                state.layer as usize,
                parent.get(&state).map(|(_, e)| *e),
            )?
        {
            engine.work.charge(1)?;
            let mut edges = Vec::new();
            let mut points = vec![state.at];
            let mut s = state;
            while let Some((previous, edge)) = parent.get(&s) {
                engine.work.charge(1)?;
                edges.push(*edge);
                s = *previous;
                points.push(s.at);
            }
            engine.work.charge((points.len() + edges.len()) as u64)?;
            points.reverse();
            edges.reverse();
            return Ok(Path {
                points,
                edges,
                steps: cost,
            });
        }

        for (direction, delta) in [[grid, 0], [0, grid], [-grid, 0], [0, -grid]]
            .iter()
            .enumerate()
        {
            engine.work.charge(1)?;
            let Some(x) = state.at[0].checked_add(delta[0]) else {
                continue;
            };
            let Some(y) = state.at[1].checked_add(delta[1]) else {
                continue;
            };
            let run_start = if engine.input.rules.same_net_hole_clearance_nm > 0
                && direction as u8 == state.direction
            {
                let mut s = state;
                while let Some((prev, _)) = parent.get(&s) {
                    engine.work.charge(1)?;
                    if prev.layer != state.layer
                        || (prev.direction != state.direction && prev.direction != 4)
                    {
                        s = *prev;
                        break;
                    }
                    s = *prev;
                    if prev.direction == 4 {
                        break;
                    }
                }
                Some(s.at)
            } else {
                None
            };
            let Some(at) = advance(
                state.layer as usize,
                run_start,
                state.at,
                [x, y],
                via_arena.get(state.via_history),
            )?
            else {
                continue;
            };
            let next_dir = if track_direction_matters {
                direction as u8
            } else {
                4
            };
            let next = State {
                at,
                layer: state.layer,
                direction: next_dir,
                arrived_by_via: false,
                is_via_goal: false,
                via_history: state.via_history,
                via_count: state.via_count,
            };
            let g = cost.checked_add(1).expect("bounded search cost");
            if best.get(&next).is_some_and(|old| *old <= g) {
                continue;
            }
            if is_dominated_by_settled(&settled_by_pos, &via_arena, next, g, &engine.work)? {
                continue;
            }
            let extra = 1
                + usize::from(!best.contains_key(&next))
                + usize::from(!parent.contains_key(&next));
            engine.work.search_storage(
                queue.len() + best.len() + parent.len() + via_arena.total_len + extra,
            )?;
            engine.work.charge(1)?;
            best.insert(next, g);
            parent.insert(
                next,
                (
                    state,
                    PathEdge::Track {
                        start: state.at,
                        end: at,
                        layer: state.layer,
                    },
                ),
            );
            let h = heuristic(next.at, next.layer as usize)?;
            queue.push(Reverse((
                g.checked_add(h).expect("bounded heuristic"),
                g,
                next,
            )));
        }

        via_buf.clear();
        via_transitions(
            state.at,
            state.layer as usize,
            via_arena.get(state.via_history),
            state.via_count,
            &mut via_buf,
        )?;
        let prior_tracks: Vec<(usize, Point, Point)> =
            if engine.input.rules.same_net_hole_clearance_nm > 0 && !via_buf.is_empty() && net != 0
            {
                let mut tracks: Vec<(usize, Point, Point)> = Vec::new();
                let mut cur = state;
                let mut can_coalesce = false;
                while let Some((prev, edge)) = parent.get(&cur) {
                    engine.work.charge(1)?;
                    if let PathEdge::Track { start, end, layer } = *edge {
                        let l = layer as usize;
                        if can_coalesce {
                            if let Some((last_l, last_start, last_end)) = tracks.last_mut() {
                                if *last_l == l
                                    && *last_start == end
                                    && ((last_start[0] == last_end[0] && last_start[0] == start[0])
                                        || (last_start[1] == last_end[1]
                                            && last_start[1] == start[1]))
                                {
                                    *last_start = start;
                                    cur = *prev;
                                    continue;
                                }
                            }
                        }
                        tracks.push((l, start, end));
                        can_coalesce = true;
                    } else {
                        can_coalesce = false;
                    }
                    cur = *prev;
                }
                tracks
            } else {
                Vec::new()
            };
        for &(target_layer, pv, v_cost) in &via_buf {
            engine.work.charge(1)?;
            if !prior_tracks.is_empty() {
                let mut clears_all = true;
                for &(l_idx, t_start, t_end) in &prior_tracks {
                    if !engine.edge_clears_same_net_vias(net, l_idx, t_start, t_end, &[pv])? {
                        clears_all = false;
                        break;
                    }
                }
                if !clears_all {
                    continue;
                }
            }
            let next_vh = if conflict_filter.contains(pv.at) {
                let Some(vh) = via_arena.append(state.via_history, pv) else {
                    continue;
                };
                vh
            } else {
                state.via_history
            };
            let next_vc = if track_via_count {
                state.via_count.checked_add(1).expect("bounded via count")
            } else {
                0
            };
            let is_goal = finished(state.at, target_layer as usize, Some(PathEdge::Via(pv)))?;
            let next = State {
                at: state.at,
                layer: target_layer,
                direction: 4,
                arrived_by_via: l_count > 1,
                is_via_goal: is_goal,
                via_history: next_vh,
                via_count: next_vc,
            };
            let g = cost.checked_add(v_cost).expect("bounded search cost");
            if best.get(&next).is_some_and(|old| *old <= g) {
                continue;
            }
            if is_dominated_by_settled(&settled_by_pos, &via_arena, next, g, &engine.work)? {
                continue;
            }
            let extra = 1
                + usize::from(!best.contains_key(&next))
                + usize::from(!parent.contains_key(&next));
            engine.work.search_storage(
                queue.len() + best.len() + parent.len() + via_arena.total_len + extra,
            )?;
            engine.work.charge(1)?;
            best.insert(next, g);
            parent.insert(next, (state, PathEdge::Via(pv)));
            let h = if is_goal {
                0
            } else {
                heuristic(next.at, next.layer as usize)?
            };
            queue.push(Reverse((
                g.checked_add(h).expect("bounded heuristic"),
                g,
                next,
            )));
        }
    }
    Err(Failure::new(
        Status::Unroutable,
        "search frontier exhausted",
    ))
}

#[cfg(test)]
#[path = "search_tests.rs"]
mod tests;
