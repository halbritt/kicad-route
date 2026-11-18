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
    via_history: u32,
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
    let attachment_from =
        |layer_idx: usize, anchor: Point, a: Point, b: Point| -> Result<Option<Point>, Failure> {
            let l_name = engine.layer_name(layer_idx);
            let width_nm = engine.effective_rules.track_width_for(net, l_name)?;
            let mut nearest = None;
            for c in targets.iter().flatten() {
                engine.work.charge(1)?;
                if let Some(p) = c.attachment(a, b, net, width_nm, l_name, stackup, &engine.work)? {
                    if engine.legal_edge_on(net, layer_idx, anchor, p)? {
                        let key = |p: Point| ((p[0] - a[0]).abs() + (p[1] - a[1]).abs(), p);
                        if nearest.is_none_or(|old| key(p) < key(old)) {
                            nearest = Some(p);
                        }
                    }
                }
            }
            Ok(nearest)
        };
    let attachment = |layer_idx: usize, a: Point, b: Point| attachment_from(layer_idx, a, a, b);

    let max_via_diam = if l_count > 1 {
        let nr = engine.effective_rules.net(net);
        nr.via_diameter_nm.max(nr.microvia_diameter_nm)
    } else {
        0
    };
    let via_cost = engine.effective_rules.via_cost.max(1);
    let start_vec: Vec<(Point, u8)> = starts.into_iter().collect();

    walk_multilayer(
        engine,
        &start_vec,
        limit,
        expanded,
        |p, layer_idx| {
            component_estimate_multilayer(
                p,
                layer_idx,
                stackup,
                targets,
                &engine.effective_rules,
                net,
                max_via_diam,
                via_cost,
                engine.input.rules.grid_nm,
                &engine.work,
            )
        },
        |layer_idx, run_start, a, b| {
            let l_name = engine.layer_name(layer_idx);
            let width_nm = engine.effective_rules.track_width_for(net, l_name)?;
            if let Some(p) = attachment(layer_idx, a, b)? {
                return Ok(Some(p));
            }
            if engine.legal_edge_on(net, layer_idx, a, b)? {
                return Ok(Some(b));
            }
            if engine.input.rules.same_net_hole_clearance_nm > 0 {
                if let Some(rs) = run_start {
                    if let Some(p) = attachment_from(layer_idx, rs, a, b)? {
                        return Ok(Some(p));
                    }
                    if engine.legal_edge_on(net, layer_idx, rs, b)? {
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
                    if pad.drill.is_none() || (c.layer_mask(stackup) & (1u32 << layer_idx)) == 0 {
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
                    let hit = match attachment(layer_idx, a, far)? {
                        Some(p) => Some(p),
                        None => match run_start {
                            Some(rs) => attachment_from(layer_idx, rs, a, far)?,
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
        |at, layer_idx, prior_vias, out| {
            if l_count < 2 {
                return Ok(());
            }
            let can_blind = engine.effective_rules.allow_blind_buried_vias;
            let can_micro = engine.effective_rules.allow_microvias;
            for target_layer in 0..l_count {
                if target_layer == layer_idx {
                    continue;
                }
                let s_min = layer_idx.min(target_layer);
                let s_max = layer_idx.max(target_layer);
                let mut candidates: Vec<(usize, usize, ViaKind, u8)> = Vec::new();
                candidates.push((0, l_count - 1, ViaKind::Through, 0));
                if can_blind && (s_min > 0 || s_max + 1 < l_count) {
                    candidates.push((s_min, s_max, ViaKind::BlindBuried, 1));
                }
                if can_micro && s_max == s_min + 1 && (s_min == 0 || s_max + 1 == l_count) {
                    candidates.push((s_min, s_max, ViaKind::Micro, 2));
                }
                for (s_idx, e_idx, kind, kind_code) in candidates {
                    let pv = PlacedViaKey {
                        at,
                        start_layer: s_idx as u8,
                        end_layer: e_idx as u8,
                        kind: kind_code,
                    };
                    let span_mask_v = span_mask(s_idx, e_idx);
                    if prior_vias.iter().any(|existing| {
                        existing.at == at
                            && (span_mask(
                                existing.start_layer as usize,
                                existing.end_layer as usize,
                            ) & span_mask_v)
                                != 0
                    }) {
                        continue;
                    }
                    if engine
                        .legal_via(net, at, s_idx, e_idx, kind, prior_vias)?
                        .is_some()
                    {
                        out.push((target_layer as u8, pv, via_cost));
                    }
                }
            }
            Ok(())
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
                    let target_bit = 1u32 << layer_idx;
                    for c in targets.iter().flatten() {
                        if (c.layer_mask(stackup) & target_bit) != 0
                            && c.touches_via(&probe_via, stackup, &engine.work)?
                        {
                            return Ok(true);
                        }
                    }
                    Ok(false)
                }
            }
        },
    )
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
    max_via_diam: i64,
    via_cost: u64,
    grid: i64,
    work: &WorkBudget,
) -> Result<u64, Failure> {
    let l_name = &stackup[layer_idx];
    let width = rules.track_width_for(net, l_name)?;
    let eff_width = width.max(max_via_diam);
    let my_bit = 1u32 << layer_idx;
    let mut lower_bound: Option<u64> = None;
    for target in targets.iter().flatten() {
        work.charge(1)?;
        let d2 = contact_steps(p, *target, eff_width, grid);
        let h_layer = if (target.layer_mask(stackup) & my_bit) != 0 {
            0
        } else {
            via_cost
        };
        let steps = d2.saturating_add(h_layer);
        lower_bound = Some(lower_bound.map_or(steps, |best| best.min(steps)));
    }
    lower_bound.ok_or_else(|| {
        Failure::new(
            Status::Unroutable,
            "component join requires at least one target item",
        )
    })
}

/// Lower bound to a superset of all endpoint-cap contacts with this target.
/// Each orthogonal edge moves at most `grid` in one axis, including a shortened
/// final edge. The sum of ceil(axis gap / grid) therefore cannot exceed the
/// remaining step cost. Full copper bounds plus the rounded-up route radius
/// contain even the conservative one-nm-overlap contact set. Ignoring obstacles
/// and rounding the envelope outward can only decrease this bound. Each bound
/// changes by at most one across an edge; the minimum over all targets is also
/// consistent and is zero at every accepted contact endpoint.
fn contact_steps(p: Point, target: Copper<'_>, width: i64, grid: i64) -> u64 {
    let route_radius = (width + 1) / 2;
    let (lo, hi) = match target {
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
    let lo: Point = std::array::from_fn(|k| lo[k] - route_radius);
    let hi: Point = std::array::from_fn(|k| hi[k] + route_radius);
    (0..2)
        .map(|k| {
            let gap = (lo[k] - p[k]).max(p[k] - hi[k]).max(0);
            ((gap + grid - 1) / grid) as u64
        })
        .sum()
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
    walk_multilayer(
        engine,
        &start_vec,
        limit,
        expanded,
        |p, _| heuristic(p),
        |_, rs, a, b| advance(rs, a, b),
        |_, _, _, _| Ok(()),
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
    starts: &[(Point, u8)],
    limit: u64,
    expanded: &mut u64,
    heuristic: impl Fn(Point, usize) -> Result<u64, Failure>,
    advance: impl Fn(usize, Option<Point>, Point, Point) -> Result<Option<Point>, Failure>,
    via_transitions: impl Fn(
        Point,
        usize,
        &[PlacedViaKey],
        &mut Vec<(u8, PlacedViaKey, u64)>,
    ) -> Result<(), Failure>,
    finished: impl Fn(Point, usize, Option<PathEdge>) -> Result<bool, Failure>,
) -> Result<Path, Failure> {
    let grid = engine.input.rules.grid_nm;
    let mut via_arena = ViaHistoryArena::new();
    let mut queue = BinaryHeap::new();
    let mut best: HashMap<State, u64> = HashMap::new();
    let mut settled_by_pos: HashMap<(Point, u8, u8), Vec<(u32, u64)>> = HashMap::new();
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
            via_history: 0,
        };
        let h = heuristic(at, layer as usize)?;
        queue.push(Reverse((h, h, initial)));
        best.insert(initial, 0u64);
    }

    let is_dominated_by_settled = |settled: &HashMap<(Point, u8, u8), Vec<(u32, u64)>>,
                                   arena: &ViaHistoryArena,
                                   cand: State,
                                   cand_g: u64,
                                   work: &WorkBudget|
     -> Result<bool, Failure> {
        if let Some(list) = settled.get(&(cand.at, cand.layer, cand.direction)) {
            for &(v1, g1) in list {
                work.charge(1)?;
                if g1 <= cand_g && arena.is_subset(v1, cand.via_history) {
                    return Ok(true);
                }
            }
        }
        Ok(false)
    };

    let mut via_buf: Vec<(u8, PlacedViaKey, u64)> = Vec::new();
    while !queue.is_empty() {
        engine.work.charge(1)?;
        let Reverse((f, h_cur, state)) = queue.pop().unwrap();
        let cost = f - h_cur;
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
            .entry((state.at, state.layer, state.direction))
            .or_default()
            .push((state.via_history, cost));

        if finished(
            state.at,
            state.layer as usize,
            parent.get(&state).map(|(_, e)| *e),
        )? {
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
            let Some(at) = advance(state.layer as usize, run_start, state.at, [x, y])? else {
                continue;
            };
            let next = State {
                at,
                layer: state.layer,
                direction: direction as u8,
                via_history: state.via_history,
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
                h,
                next,
            )));
        }

        via_buf.clear();
        via_transitions(
            state.at,
            state.layer as usize,
            via_arena.get(state.via_history),
            &mut via_buf,
        )?;
        for &(target_layer, pv, v_cost) in &via_buf {
            engine.work.charge(1)?;
            let Some(next_vh) = via_arena.append(state.via_history, pv) else {
                continue;
            };
            let next = State {
                at: state.at,
                layer: target_layer,
                direction: 4,
                via_history: next_vh,
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
            let h = heuristic(next.at, next.layer as usize)?;
            queue.push(Reverse((
                g.checked_add(h).expect("bounded heuristic"),
                h,
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
