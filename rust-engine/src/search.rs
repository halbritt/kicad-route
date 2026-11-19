use crate::budget::WorkBudget;
use crate::components::{span_mask, Copper};
use crate::geometry;
use crate::model::{Failure, OutlinePolygon, Point, Status, Track, Via, ViaKind};
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
    via_only: bool,
    via_history: u32,
    via_count: u16,
    track_via_blocked: u32,
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

// A route that only grazes a refillable polygon with its rounded end cap can
// lose the contact when KiCad refills. Cache scanlines once per component join
// so even a narrow, off-grid region can receive a centerline landing.
#[derive(Default)]
struct FillLandings {
    rows: HashMap<(*const OutlinePolygon, usize, i64), Vec<(i64, i64)>>,
    entries: usize,
}

impl FillLandings {
    fn on_segment(
        &mut self,
        poly: &OutlinePolygon,
        a: Point,
        b: Point,
        work: &WorkBudget,
    ) -> Result<Option<Point>, Failure> {
        let axis = usize::from(a[0] == b[0] && a[1] != b[1]);
        let line = a[1 - axis];
        let key = (std::ptr::from_ref(poly), axis, line);
        let ranges = match self.rows.entry(key) {
            std::collections::hash_map::Entry::Occupied(row) => row.into_mut(),
            std::collections::hash_map::Entry::Vacant(row) => {
                let vertices = poly.outer.len() + poly.holes.iter().map(Vec::len).sum::<usize>();
                work.charge(vertices as u64)?;
                let ranges = geometry::fill_landing_ranges(poly, axis, line);
                work.search_storage(self.entries + ranges.len() + 1)?;
                self.entries += ranges.len() + 1;
                row.insert(ranges)
            }
        };
        let lo = a[axis].min(b[axis]);
        let hi = a[axis].max(b[axis]);
        // Ranges are sorted. Take the first range entered on the way to b.
        // Prefer a full grid step, or the midpoint of a narrower crossing.
        let first = ranges.partition_point(|&(_, r)| r < lo);
        let last = ranges.partition_point(|&(l, _)| l <= hi);
        if first >= last {
            return Ok(None);
        }
        let (l, r) = ranges[if a[axis] <= b[axis] { first } else { last - 1 }];
        let mut pt = b;
        if !(l..=r).contains(&b[axis]) {
            pt[axis] = l.max(lo) + (r.min(hi) - l.max(lo)) / 2;
        }
        Ok(Some(pt))
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

struct TrackViaBlockedArena {
    entries: Vec<Vec<u16>>,
    intern: HashMap<Vec<u16>, u32>,
    total_len: usize,
}

impl TrackViaBlockedArena {
    fn new() -> Self {
        let mut intern = HashMap::new();
        intern.insert(Vec::new(), 0);
        Self {
            entries: vec![Vec::new()],
            intern,
            total_len: 0,
        }
    }

    fn contains(&self, id: u32, idx: u16) -> bool {
        id != 0 && self.entries[id as usize].binary_search(&idx).is_ok()
    }

    fn insert_all(&mut self, id: u32, add: &[u16]) -> u32 {
        if add.is_empty() {
            return id;
        }
        let mut vec = self.entries[id as usize].clone();
        vec.extend_from_slice(add);
        vec.sort_unstable();
        vec.dedup();
        if let Some(&existing_id) = self.intern.get(&vec) {
            return existing_id;
        }
        let new_id = u32::try_from(self.entries.len()).expect("bounded track-via conflict arena");
        self.total_len += vec.len();
        self.intern.insert(vec.clone(), new_id);
        self.entries.push(vec);
        new_id
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
    let mut budget = crate::negotiate::TrialBudget::fixed(limit);
    join_components_inner(
        engine,
        net,
        source,
        targets,
        &mut budget,
        expanded,
        None,
        None,
        false,
    )
}

#[allow(clippy::too_many_arguments)]
#[allow(dead_code)]
pub(crate) fn join_components_negotiated(
    engine: &Engine,
    net: u32,
    source: &[Copper<'_>],
    targets: &[Vec<Copper<'_>>],
    limit: u64,
    expanded: &mut u64,
    history: &crate::negotiate::CongestionHistory,
    frontier: &mut crate::negotiate::BlockedFrontier,
) -> Result<Path, Failure> {
    let mut budget = crate::negotiate::TrialBudget::fixed(limit);
    join_components_negotiated_with_budget(
        engine,
        net,
        source,
        targets,
        &mut budget,
        expanded,
        history,
        frontier,
    )
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn join_components_negotiated_with_budget(
    engine: &Engine,
    net: u32,
    source: &[Copper<'_>],
    targets: &[Vec<Copper<'_>>],
    budget: &mut crate::negotiate::TrialBudget<'_>,
    expanded: &mut u64,
    history: &crate::negotiate::CongestionHistory,
    frontier: &mut crate::negotiate::BlockedFrontier,
) -> Result<Path, Failure> {
    let hist_opt = (!history.is_empty()).then_some(history);
    join_components_inner(
        engine,
        net,
        source,
        targets,
        budget,
        expanded,
        hist_opt,
        Some(frontier),
        engine.prefer_higher_g(),
    )
}

#[allow(clippy::too_many_arguments)]
fn join_components_inner(
    engine: &Engine,
    net: u32,
    source: &[Copper<'_>],
    targets: &[Vec<Copper<'_>>],
    budget: &mut crate::negotiate::TrialBudget<'_>,
    expanded: &mut u64,
    history: Option<&crate::negotiate::CongestionHistory>,
    mut frontier: Option<&mut crate::negotiate::BlockedFrontier>,
    prefer_higher_g: bool,
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
    // A through-hole pad can reach an orphaned thermal island electrically
    // while its spokes still lack a connection to the main fill on that face.
    let mut source_layers = u32::MAX;
    let mut fill_targets = Vec::new();
    if engine
        .input
        .zones
        .iter()
        .any(|z| z.net == net && z.refillable && z.filled)
    {
        engine.work.charge(source.len() as u64)?;
        let surfaces: BTreeSet<_> = source
            .iter()
            .filter_map(|c| c.refillable_surface())
            .collect();
        if !surfaces.is_empty() {
            let mut common_layers = 0u32;
            for group in targets {
                engine.work.charge(group.len() as u64)?;
                let mut fills = Vec::new();
                for c in group {
                    if let Some(key) = c.refillable_surface() {
                        if surfaces.contains(&key) {
                            common_layers |= 1u32 << key.0;
                            fills.push(*c);
                        }
                    }
                }
                if !fills.is_empty() {
                    fill_targets.push(fills);
                }
            }
            if common_layers != 0 {
                source_layers = common_layers;
            }
        }
    }
    let targets = if fill_targets.is_empty() {
        targets
    } else {
        &fill_targets
    };
    let landings = std::cell::RefCell::new(FillLandings::default());
    let stackup = engine.all_layers_slice();
    let l_count = engine.layer_count();
    let via_cost = engine.effective_rules.via_cost.max(1);
    let mut track_starts: BTreeSet<(Point, u8)> = BTreeSet::new();
    let mut via_only_starts: BTreeSet<(Point, u8)> = BTreeSet::new();
    let mut via_seed_buf: Vec<(u8, PlacedViaKey, u64)> = Vec::new();
    let mut seed_blocked_vias: Vec<(u8, u8, bool)> = Vec::new();
    for c in source {
        engine.work.charge(1)?;
        let c_mask = c.layer_mask(stackup);
        let anchors = c.anchors(engine.input.bounds.min, engine.input.rules.grid_nm);
        for l_idx in 0..l_count {
            if (c_mask & source_layers & (1u32 << l_idx)) == 0 {
                continue;
            }
            let l_name = engine.layer_name(l_idx);
            let width_opt = match engine.effective_rules.track_width_for(net, l_name) {
                Ok(w) => Some(w),
                Err(e) if e.status == Status::Unroutable => None,
                Err(e) => return Err(e),
            };
            for &p in &anchors {
                engine.work.charge(1)?;
                let key = (p, l_idx as u8);
                let mut track_ok = false;
                engine.clear_last_block_generated();
                if let Some(width_nm) = width_opt {
                    if engine.legal_edge_on(net, l_idx, p, p)? {
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
                                track_ok = true;
                                break;
                            }
                        }
                    }
                }
                let track_block_generated = !track_ok && engine.last_block_generated();
                let mut via_ok = false;
                seed_blocked_vias.clear();
                if track_ok {
                    via_only_starts.remove(&key);
                    engine.work.search_storage(
                        track_starts.len()
                            + via_only_starts.len()
                            + usize::from(!track_starts.contains(&key)),
                    )?;
                    track_starts.insert(key);
                } else if l_count > 1
                    && !track_starts.contains(&key)
                    && !via_only_starts.contains(&key)
                {
                    via_seed_buf.clear();
                    push_legal_via_transitions_with_blocked(
                        engine,
                        net,
                        p,
                        l_idx,
                        via_cost,
                        &[],
                        &mut via_seed_buf,
                        frontier.as_deref().map(|_| &mut seed_blocked_vias),
                    )?;
                    'via_seed: for &(_, pv, _) in &via_seed_buf {
                        let kind = match pv.kind {
                            0 => ViaKind::Through,
                            1 => ViaKind::BlindBuried,
                            _ => ViaKind::Micro,
                        };
                        let span_layers = &stackup[pv.start_layer as usize..=pv.end_layer as usize];
                        if let Ok((diam, drill)) = engine.effective_rules.via_dims_for(
                            net,
                            kind == ViaKind::Micro,
                            span_layers,
                        ) {
                            let probe_via = Via {
                                id: String::new(),
                                net,
                                at: p,
                                diameter_nm: diam,
                                drill_nm: drill,
                                start_layer: stackup[pv.start_layer as usize].clone(),
                                end_layer: stackup[pv.end_layer as usize].clone(),
                                kind,
                                locked: false,
                            };
                            for s in source {
                                if (s.layer_mask(stackup) & (1u32 << l_idx)) != 0
                                    && s.touches_via(&probe_via, stackup, &engine.work)?
                                {
                                    via_ok = true;
                                    break 'via_seed;
                                }
                            }
                        }
                    }
                    if via_ok {
                        engine
                            .work
                            .search_storage(track_starts.len() + via_only_starts.len() + 1)?;
                        via_only_starts.insert(key);
                    }
                }
                if !track_ok
                    && !via_ok
                    && !track_starts.contains(&key)
                    && !via_only_starts.contains(&key)
                {
                    if let Some(f) = frontier.as_deref_mut() {
                        f.push_edge_classified(l_idx as u8, p, p, track_block_generated);
                        for &(s_lo, s_hi, via_gen) in &seed_blocked_vias {
                            f.push_via_site_classified(s_lo, s_hi, p, via_gen);
                        }
                    }
                }
            }
        }
    }
    if track_starts.is_empty() && via_only_starts.is_empty() {
        let mut failure = Failure::new(
            Status::Unroutable,
            "component has no legal pad-center or incumbent-endpoint search seed",
        );
        failure.work_used = engine.work.used();
        failure.expanded_states = *expanded;
        return Err(failure);
    }
    let mut layer_targets: Vec<Vec<(Copper<'_>, Point, Point)>> = vec![Vec::new(); l_count];
    let mut layer_target_union: Vec<Option<(Point, Point)>> = vec![None; l_count];
    for c in targets.iter().flatten().copied() {
        let mask = c.layer_mask(stackup);
        let (lo, hi) = c.copper_bounds();
        for l_idx in 0..l_count {
            if (mask & (1u32 << l_idx)) != 0 {
                layer_targets[l_idx].push((c, lo, hi));
                layer_target_union[l_idx] = Some(match layer_target_union[l_idx] {
                    None => (lo, hi),
                    Some((u_lo, u_hi)) => (
                        [u_lo[0].min(lo[0]), u_lo[1].min(lo[1])],
                        [u_hi[0].max(hi[0]), u_hi[1].max(hi[1])],
                    ),
                });
            }
        }
    }

    let attachment_from = |layer_idx: usize,
                           anchor: Point,
                           a: Point,
                           b: Point,
                           prior_vias: &[PlacedViaKey]|
     -> Result<Option<Point>, Failure> {
        let Some((u_lo, u_hi)) = layer_target_union[layer_idx] else {
            return Ok(None);
        };
        let l_name = engine.layer_name(layer_idx);
        let width_nm = match engine.effective_rules.track_width_for(net, l_name) {
            Ok(w) => w,
            Err(e) if e.status == Status::Unroutable => return Ok(None),
            Err(e) => return Err(e),
        };
        let route_radius = (width_nm + 1) / 2;
        let edge_lo = [a[0].min(b[0]) - route_radius, a[1].min(b[1]) - route_radius];
        let edge_hi = [a[0].max(b[0]) + route_radius, a[1].max(b[1]) + route_radius];
        if edge_hi[0] < u_lo[0]
            || edge_lo[0] > u_hi[0]
            || edge_hi[1] < u_lo[1]
            || edge_lo[1] > u_hi[1]
        {
            return Ok(None);
        }
        let mut nearest = None;
        let check_legal = |pt: Point| -> Result<bool, Failure> {
            Ok(engine.legal_edge_on(net, layer_idx, anchor, pt)?
                && engine.edge_clears_same_net_vias(net, layer_idx, anchor, pt, prior_vias)?)
        };
        for &(c, lo, hi) in &layer_targets[layer_idx] {
            if edge_hi[0] < lo[0] || edge_lo[0] > hi[0] || edge_hi[1] < lo[1] || edge_lo[1] > hi[1]
            {
                continue;
            }
            engine.work.charge(1)?;
            if let Copper::ZoneFill { zone, poly, .. } = c {
                if zone.refillable {
                    if let Some(p) = landings.borrow_mut().on_segment(poly, a, b, &engine.work)? {
                        if check_legal(p)? {
                            let distance =
                                |q: Point| ((q[0] - a[0]).abs() + (q[1] - a[1]).abs(), q);
                            if nearest.is_none_or(|old| distance(p) < distance(old)) {
                                nearest = Some(p);
                            }
                        }
                    }
                    continue;
                }
            }
            if let Some(p_first) =
                c.attachment(a, b, net, width_nm, l_name, stackup, &engine.work)?
            {
                let chosen = match c {
                    Copper::Pad(pad) if !geometry::point_in_pad(p_first, pad) => {
                        engine.work.charge((pad.shape.parts().len() - 1) as u64)?;
                        let [cand_deep, cand_near] =
                            geometry::segment_pad_interior_candidates(a, b, pad);
                        let mut interior_hit = None;
                        for cand in [cand_deep, cand_near].into_iter().flatten() {
                            if check_legal(cand)? {
                                interior_hit = Some(cand);
                                break;
                            }
                        }
                        if interior_hit.is_some() {
                            interior_hit
                        } else {
                            check_legal(p_first)?.then_some(p_first)
                        }
                    }
                    _ => check_legal(p_first)?.then_some(p_first),
                };
                if let Some(p) = chosen {
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
        match engine.effective_rules.track_width_for(net, l_name) {
            Ok(w) => max_net_track_width = max_net_track_width.max(w),
            Err(e) if e.status == Status::Unroutable => {}
            Err(e) => return Err(e),
        }
    }
    let max_eff_width = max_net_track_width.max(max_via_diam);

    let max_h2h = engine.effective_rules.max_hole_to_hole_nm;
    let conflict_radius_upper = (i128::from(max_net_drill) + i128::from(max_h2h)).max(
        (i128::from(max_via_diam) + i128::from(max_net_drill)) / 2
            + i128::from(max_h2h.max(engine.input.rules.same_net_hole_clearance_nm)),
    );
    let conflict_radius_sq_upper = conflict_radius_upper * conflict_radius_upper;

    let mut start_vec: Vec<(Point, u8, bool)> = track_starts
        .iter()
        .map(|&(p, l)| (p, l, false))
        .chain(via_only_starts.iter().map(|&(p, l)| (p, l, true)))
        .collect();
    start_vec.sort_unstable();
    let bwd_starts: Vec<(Point, u8)> = start_vec.iter().map(|&(p, l, _)| (p, l)).collect();
    let has_zone_target = targets
        .iter()
        .flatten()
        .any(|c| matches!(c, Copper::ZoneFill { .. }));
    let track_direction_matters = l_count == 1 || engine.input.rules.same_net_hole_clearance_nm > 0;
    let track_via_count = l_count > 1 && engine.effective_rules.net(net).max_via_count.is_some();

    let mut conflict_filter = ConflictFilter::default();
    let mut track_via_conflicts: Vec<PlacedViaKey> = Vec::new();
    let mut backward_h: Option<(u64, BackwardHeuristic)> = None;
    let disallow_smd_via_in_pad = engine
        .input
        .board_design_settings
        .as_ref()
        .is_some_and(|bds| !(bds.cap_vias && bds.fill_vias));
    let can_probe_escape = frontier.is_some()
        && l_count > 1
        && engine.input.rules.same_net_hole_clearance_nm == 0
        && !has_zone_target
        && targets.iter().flatten().count() <= 8
        && budget.batch_limit.saturating_sub(*expanded) >= 10_000;
    let mut escape_probe = |exp: &mut u64,
                            f: &mut crate::negotiate::BlockedFrontier,
                            b: &mut crate::negotiate::TrialBudget<'_>|
     -> Result<bool, Failure> {
        if !can_probe_escape {
            return Ok(true);
        }
        target_escape_reachable(
            engine,
            net,
            &bwd_starts,
            targets,
            max_eff_width,
            via_cost,
            b,
            exp,
            f,
            &attachment,
        )
    };

    loop {
        let use_bidirectional = l_count >= 2
            && engine.input.rules.same_net_hole_clearance_nm == 0
            && conflict_filter.zones.is_empty()
            && track_via_conflicts.is_empty()
            && !track_via_count
            && !has_zone_target
            && targets.iter().flatten().count() <= 8;
        let h_fwd_fn = |p, layer_idx| {
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
                disallow_smd_via_in_pad,
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
        };
        let advance_fn = |layer_idx, run_start, a, b, prior_vias: &[PlacedViaKey]| {
            engine.clear_last_block_generated();
            let l_name = engine.layer_name(layer_idx);
            let width_nm = match engine.effective_rules.track_width_for(net, l_name) {
                Ok(w) => w,
                Err(e) if e.status == Status::Unroutable => return Ok(None),
                Err(e) => return Err(e),
            };
            if let Some(p) = attachment(layer_idx, a, b, prior_vias)? {
                return Ok(Some(p));
            }
            if engine.legal_edge_on(net, layer_idx, a, b)?
                && engine.edge_clears_same_net_vias(net, layer_idx, a, b, prior_vias)?
            {
                return Ok(Some(b));
            }
            let primary_gen = engine.last_block_generated();
            if engine.input.rules.same_net_hole_clearance_nm > 0 {
                if let Some(rs) = run_start {
                    if let Some(p) = attachment_from(layer_idx, rs, a, b, prior_vias)? {
                        return Ok(Some(p));
                    }
                    if engine.legal_edge_on(net, layer_idx, rs, b)?
                        && engine.edge_clears_same_net_vias(net, layer_idx, rs, b, prior_vias)?
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
                    let (lo, hi) = match *c {
                        Copper::Pad(pad) => {
                            if pad.drill.is_none()
                                || (c.layer_mask(stackup) & (1u32 << layer_idx)) == 0
                            {
                                continue;
                            }
                            pad.copper_bounds()
                        }
                        Copper::Via(via) => {
                            if (c.layer_mask(stackup) & (1u32 << layer_idx)) == 0 {
                                continue;
                            }
                            let r = (via.diameter_nm + 1) / 2;
                            (
                                [via.at[0] - r, via.at[1] - r],
                                [via.at[0] + r, via.at[1] + r],
                            )
                        }
                        _ => continue,
                    };
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
            if primary_gen {
                engine.set_last_block_generated(true);
            }
            Ok(None)
        };
        let via_transitions_fn =
            |at,
             layer_idx,
             prior_vias: &[PlacedViaKey],
             prior_via_count,
             via_only,
             out: &mut Vec<(u8, PlacedViaKey, u64)>,
             blocked_out: Option<&mut Vec<(u8, u8, bool)>>| {
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
                push_legal_via_transitions_with_blocked(
                    engine,
                    net,
                    at,
                    layer_idx,
                    via_cost,
                    prior_vias,
                    out,
                    blocked_out,
                )?;
                if via_only {
                    let mut write = 0usize;
                    for read in 0..out.len() {
                        let entry = out[read];
                        let (_, pv, _) = entry;
                        let kind = match pv.kind {
                            0 => ViaKind::Through,
                            1 => ViaKind::BlindBuried,
                            _ => ViaKind::Micro,
                        };
                        let span_layers = &stackup[pv.start_layer as usize..=pv.end_layer as usize];
                        let mut touches = false;
                        if let Ok((diam, drill)) = engine.effective_rules.via_dims_for(
                            net,
                            kind == ViaKind::Micro,
                            span_layers,
                        ) {
                            let probe_via = Via {
                                id: String::new(),
                                net,
                                at,
                                diameter_nm: diam,
                                drill_nm: drill,
                                start_layer: stackup[pv.start_layer as usize].clone(),
                                end_layer: stackup[pv.end_layer as usize].clone(),
                                kind,
                                locked: false,
                            };
                            for s in source {
                                if (s.layer_mask(stackup) & (1u32 << layer_idx)) != 0
                                    && s.touches_via(&probe_via, stackup, &engine.work)?
                                {
                                    touches = true;
                                    break;
                                }
                            }
                        }
                        if touches {
                            out[write] = entry;
                            write += 1;
                        }
                    }
                    out.truncate(write);
                }
                Ok(())
            };
        let finished_fn = |at: Point, layer_idx: usize, edge: Option<PathEdge>| {
            let Some(prev_edge) = edge else {
                return Ok(false);
            };
            match prev_edge {
                PathEdge::Track { .. } => {
                    let Some((u_lo, u_hi)) = layer_target_union[layer_idx] else {
                        return Ok(false);
                    };
                    let l_name = engine.layer_name(layer_idx);
                    let width_nm = match engine.effective_rules.track_width_for(net, l_name) {
                        Ok(w) => w,
                        Err(e) if e.status == Status::Unroutable => return Ok(false),
                        Err(e) => return Err(e),
                    };
                    let r = (width_nm + 1) / 2;
                    if at[0] + r < u_lo[0]
                        || at[0] - r > u_hi[0]
                        || at[1] + r < u_lo[1]
                        || at[1] - r > u_hi[1]
                    {
                        return Ok(false);
                    }
                    let probe = Track {
                        id: String::new(),
                        net,
                        start: at,
                        end: at,
                        width_nm,
                        locked: false,
                        layer: l_name.to_owned(),
                    };
                    for &(c, lo, hi) in &layer_targets[layer_idx] {
                        if at[0] + r < lo[0]
                            || at[0] - r > hi[0]
                            || at[1] + r < lo[1]
                            || at[1] - r > hi[1]
                        {
                            continue;
                        }
                        if let Copper::ZoneFill { zone, poly, .. } = c {
                            if zone.refillable {
                                if landings
                                    .borrow_mut()
                                    .on_segment(poly, at, at, &engine.work)?
                                    .is_some()
                                {
                                    return Ok(true);
                                }
                                continue;
                            }
                        }
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
                    let (diam, drill) = match engine.effective_rules.via_dims_for(
                        net,
                        kind == ViaKind::Micro,
                        span_layers,
                    ) {
                        Ok(dims) => dims,
                        Err(e) if e.status == Status::Unroutable => return Ok(false),
                        Err(e) => return Err(e),
                    };
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
                        if let Copper::ZoneFill {
                            zone,
                            poly,
                            layer_idx,
                        } = c
                        {
                            if zone.refillable {
                                if (1u32 << layer_idx) & via_mask != 0
                                    && landings
                                        .borrow_mut()
                                        .on_segment(poly, pv.at, pv.at, &engine.work)?
                                        .is_some()
                                {
                                    return Ok(true);
                                }
                                continue;
                            }
                        }
                        if (c.layer_mask(stackup) & via_mask) != 0
                            && c.touches_via(&probe_via, stackup, &engine.work)?
                        {
                            return Ok(true);
                        }
                    }
                    Ok(false)
                }
            }
        };
        let mut result = if use_bidirectional {
            walk_multilayer_bidirectional(
                engine,
                net,
                &start_vec,
                targets,
                max_eff_width,
                via_cost,
                budget,
                expanded,
                history,
                frontier.as_deref_mut(),
                prefer_higher_g,
                can_probe_escape.then_some(
                    &mut escape_probe
                        as &mut dyn FnMut(
                            &mut u64,
                            &mut crate::negotiate::BlockedFrontier,
                            &mut crate::negotiate::TrialBudget<'_>,
                        ) -> Result<bool, Failure>,
                ),
                h_fwd_fn,
                &attachment,
                via_transitions_fn,
                finished_fn,
            )?
        } else {
            walk_multilayer(
                engine,
                net,
                &start_vec,
                budget,
                expanded,
                track_direction_matters,
                track_via_count,
                &conflict_filter,
                &track_via_conflicts,
                history,
                frontier.as_deref_mut(),
                prefer_higher_g,
                can_probe_escape.then_some(
                    &mut escape_probe
                        as &mut dyn FnMut(
                            &mut u64,
                            &mut crate::negotiate::BlockedFrontier,
                            &mut crate::negotiate::TrialBudget<'_>,
                        ) -> Result<bool, Failure>,
                ),
                h_fwd_fn,
                advance_fn,
                via_transitions_fn,
                finished_fn,
            )?
        };

        let path_via_count = result
            .edges
            .iter()
            .filter(|e| matches!(e, PathEdge::Via(_)))
            .count();
        if l_count < 2
            || (path_via_count < 2
                && (engine.input.rules.same_net_hole_clearance_nm == 0 || path_via_count == 0))
        {
            extend_path_into_target_pad(engine, net, &layer_targets, &mut result)?;
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
                if !has_zone_target && engine.input.rules.same_net_hole_clearance_nm == 0 {
                    let needed_cutoff = result.steps.saturating_add(via_cost);
                    if backward_h.as_ref().is_none_or(|(m, _)| needed_cutoff > *m) {
                        let bwd = compute_backward_heuristic(
                            engine,
                            net,
                            &bwd_starts,
                            targets,
                            max_eff_width,
                            via_cost,
                            needed_cutoff,
                            budget.batch_limit,
                        )?;
                        backward_h = Some((needed_cutoff, bwd));
                    }
                }
                continue;
            }
        }
        if engine.input.rules.same_net_hole_clearance_nm > 0 {
            if let Some((conflict_pv, track_before_via)) =
                first_same_net_via_track_conflict(engine, net, &result)?
            {
                if track_before_via {
                    if track_via_conflicts.contains(&conflict_pv) {
                        return Err(Failure::new(
                            Status::Unroutable,
                            "proposed path violates same-net via hole clearance",
                        ));
                    }
                    track_via_conflicts.push(conflict_pv);
                } else {
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
                }
                continue;
            }
        }
        extend_path_into_target_pad(engine, net, &layer_targets, &mut result)?;
        return Ok(result);
    }
}

fn extend_path_into_target_pad(
    engine: &Engine,
    net: u32,
    layer_targets: &[Vec<(Copper<'_>, Point, Point)>],
    result: &mut Path,
) -> Result<(), Failure> {
    let Some(PathEdge::Track { start, end, layer }) = result.edges.last().copied() else {
        return Ok(());
    };
    let layer_idx = layer as usize;
    let l_name = engine.layer_name(layer_idx);
    let Ok(width_nm) = engine.effective_rules.track_width_for(net, l_name) else {
        return Ok(());
    };
    let stackup = engine.all_layers_slice();
    let probe = Track {
        id: String::new(),
        net,
        start: end,
        end,
        width_nm,
        locked: false,
        layer: l_name.to_owned(),
    };
    let r = (width_nm + 1) / 2;
    let mut touched_pad: Option<&crate::model::Pad> = None;
    for &(c, lo, hi) in &layer_targets[layer_idx] {
        if end[0] + r < lo[0] || end[0] - r > hi[0] || end[1] + r < lo[1] || end[1] - r > hi[1] {
            continue;
        }
        if c.touches_route(&probe, stackup, &engine.work)? {
            match c {
                Copper::Pad(pad) => {
                    if geometry::point_in_pad(end, pad) {
                        return Ok(());
                    }
                    if touched_pad.is_none() {
                        touched_pad = Some(pad);
                    }
                }
                _ => return Ok(()),
            }
        }
    }
    let Some(pad) = touched_pad else {
        return Ok(());
    };
    let prior_vias: Vec<PlacedViaKey> = result
        .edges
        .iter()
        .filter_map(|e| match *e {
            PathEdge::Via(pv) => Some(pv),
            PathEdge::Track { .. } => None,
        })
        .collect();
    let collinear_anchor = if engine.input.rules.same_net_hole_clearance_nm > 0 {
        let axis = usize::from(start[0] == end[0]);
        let sign = (end[axis] - start[axis]).signum();
        let mut anchor = start;
        for e in result.edges[..result.edges.len() - 1].iter().rev() {
            let PathEdge::Track {
                start: ps,
                end: pe,
                layer: pl,
            } = *e
            else {
                break;
            };
            if pl != layer || ps[1 - axis] != pe[1 - axis] || (pe[axis] - ps[axis]).signum() != sign
            {
                break;
            }
            anchor = ps;
        }
        anchor
    } else {
        start
    };
    let is_legal_from = |anchor: Point, pt: Point| -> Result<bool, Failure> {
        Ok(engine.legal_edge_on(net, layer_idx, anchor, pt)?
            && engine.edge_clears_same_net_vias(net, layer_idx, anchor, pt, &prior_vias)?)
    };
    let axis = usize::from(start[0] == end[0]);
    let sign = (end[axis] - start[axis]).signum();
    if sign != 0 {
        let (plo, phi) = pad.copper_bounds();
        let mut far = end;
        far[axis] = if sign > 0 { phi[axis] } else { plo[axis] };
        if (far[axis] - end[axis]).signum() == sign {
            engine.work.charge((pad.shape.parts().len() - 1) as u64)?;
            let [cand_deep, cand_near] = geometry::segment_pad_interior_candidates(end, far, pad);
            for p_in in [cand_deep, cand_near].into_iter().flatten() {
                if is_legal_from(collinear_anchor, p_in)? {
                    if let Some(PathEdge::Track { end: last_end, .. }) = result.edges.last_mut() {
                        *last_end = p_in;
                    }
                    if let Some(last_pt) = result.points.last_mut() {
                        *last_pt = p_in;
                    }
                    return Ok(());
                }
            }
        }
    }
    let center = pad.copper_center();
    let mut ortho_targets = Vec::new();
    for target in [center, pad.at] {
        for cand in [[target[0], end[1]], [end[0], target[1]]] {
            if cand != end && !ortho_targets.contains(&cand) {
                ortho_targets.push(cand);
            }
        }
    }
    for far in ortho_targets {
        engine.work.charge((pad.shape.parts().len() - 1) as u64)?;
        let [cand_deep, cand_near] = geometry::segment_pad_interior_candidates(end, far, pad);
        for p_in in [cand_deep, cand_near].into_iter().flatten() {
            if is_legal_from(end, p_in)? {
                result.points.push(p_in);
                result.edges.push(PathEdge::Track {
                    start: end,
                    end: p_in,
                    layer,
                });
                return Ok(());
            }
        }
    }
    for target in [center, pad.at] {
        if !geometry::point_in_pad(target, pad) {
            continue;
        }
        for corner in [[target[0], end[1]], [end[0], target[1]]] {
            if corner != end
                && corner != target
                && is_legal_from(end, corner)?
                && is_legal_from(corner, target)?
            {
                result.points.push(corner);
                result.points.push(target);
                result.edges.push(PathEdge::Track {
                    start: end,
                    end: corner,
                    layer,
                });
                result.edges.push(PathEdge::Track {
                    start: corner,
                    end: target,
                    layer,
                });
                return Ok(());
            }
        }
    }
    Ok(())
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
    push_legal_via_transitions_with_blocked(
        engine, net, at, layer_idx, via_cost, prior_vias, out, None,
    )
}

#[allow(clippy::too_many_arguments)]
fn push_legal_via_transitions_with_blocked(
    engine: &Engine,
    net: u32,
    at: Point,
    layer_idx: usize,
    via_cost: u64,
    prior_vias: &[PlacedViaKey],
    out: &mut Vec<(u8, PlacedViaKey, u64)>,
    mut blocked_spans: Option<&mut Vec<(u8, u8, bool)>>,
) -> Result<(), Failure> {
    let l_count = engine.layer_count();
    if l_count < 2 {
        return Ok(());
    }
    let can_blind = engine.effective_rules.allow_blind_buried_vias;
    let can_micro = engine.effective_rules.allow_microvias;
    let stackup = engine.all_layers_slice();
    let at_via_limit = engine
        .effective_rules
        .net(net)
        .max_via_count
        .is_some_and(|max_vc| {
            engine.copper.via_count_for_net(net) + prior_vias.len() + 1 > max_vc as usize
        });
    let has_overlap = |s_idx: usize, e_idx: usize| -> bool {
        let m = span_mask(s_idx, e_idx);
        prior_vias.iter().any(|existing| {
            existing.at == at
                && (span_mask(existing.start_layer as usize, existing.end_layer as usize) & m) != 0
        })
    };

    if !has_overlap(0, l_count - 1) {
        if engine
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
        } else if let Some(b) = blocked_spans.as_deref_mut() {
            if !at_via_limit && !engine.effective_rules.disallow_via_on(net, stackup) {
                b.push((0, (l_count - 1) as u8, engine.last_block_generated()));
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
            let span_layers = &stackup[s_min..=s_max];
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
                } else if let Some(b) = blocked_spans.as_deref_mut() {
                    if !at_via_limit && !engine.effective_rules.disallow_via_on(net, span_layers) {
                        b.push((s_min as u8, s_max as u8, engine.last_block_generated()));
                    }
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
                } else if let Some(b) = blocked_spans.as_deref_mut() {
                    if !at_via_limit && !engine.effective_rules.disallow_via_on(net, span_layers) {
                        b.push((s_min as u8, s_max as u8, engine.last_block_generated()));
                    }
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
) -> Result<Option<(PlacedViaKey, bool)>, Failure> {
    if engine.input.rules.same_net_hole_clearance_nm <= 0 || net == 0 {
        return Ok(None);
    }
    let mut coalesced_tracks: Vec<(usize, Point, Point, usize)> = Vec::new();
    let mut path_vias: Vec<(PlacedViaKey, usize)> = Vec::new();
    let mut can_coalesce_last = false;
    for (edge_idx, edge) in path.edges.iter().enumerate() {
        engine.work.charge(1)?;
        match *edge {
            PathEdge::Track { start, end, layer } => {
                let l = layer as usize;
                if can_coalesce_last {
                    if let Some((last_l, last_start, last_end, last_idx)) =
                        coalesced_tracks.last_mut()
                    {
                        if *last_l == l
                            && *last_end == start
                            && ((last_start[0] == last_end[0]
                                && last_end[0] == end[0]
                                && last_end[1] != last_start[1]
                                && (last_end[1] - last_start[1]).signum()
                                    == (end[1] - start[1]).signum())
                                || (last_start[1] == last_end[1]
                                    && last_end[1] == end[1]
                                    && last_end[0] != last_start[0]
                                    && (last_end[0] - last_start[0]).signum()
                                        == (end[0] - start[0]).signum()))
                        {
                            *last_end = end;
                            *last_idx = edge_idx;
                            continue;
                        }
                    }
                }
                coalesced_tracks.push((l, start, end, edge_idx));
                can_coalesce_last = true;
            }
            PathEdge::Via(pv) => {
                can_coalesce_last = false;
                path_vias.push((pv, edge_idx));
            }
        }
    }
    for &(pv, via_idx) in &path_vias {
        for &(l_idx, start, end, trk_idx) in &coalesced_tracks {
            if !engine.edge_clears_same_net_vias(net, l_idx, start, end, &[pv])? {
                return Ok(Some((pv, trk_idx < via_idx)));
            }
        }
    }
    Ok(None)
}

#[allow(clippy::too_many_arguments)]
fn target_escape_reachable(
    engine: &Engine,
    net: u32,
    starts: &[(Point, u8)],
    targets: &[Vec<Copper<'_>>],
    max_eff_width: i64,
    via_cost: u64,
    budget: &mut crate::negotiate::TrialBudget<'_>,
    expanded: &mut u64,
    frontier: &mut crate::negotiate::BlockedFrontier,
    attachment: &impl Fn(usize, Point, Point, &[PlacedViaKey]) -> Result<Option<Point>, Failure>,
) -> Result<bool, Failure> {
    const TARGET_ESCAPE_BUDGET: usize = 64;
    let stackup = engine.all_layers_slice();
    let l_count = engine.layer_count();
    let grid = engine.input.rules.grid_nm;
    let route_radius = (max_eff_width + 1) / 2;
    let bounds = engine.input.bounds;

    let start_set: BTreeSet<(Point, u8)> = starts.iter().copied().collect();
    let mut phases: BTreeSet<(i64, i64)> = BTreeSet::new();
    for &(s, _) in starts {
        phases.insert((s[0].rem_euclid(grid), s[1].rem_euclid(grid)));
    }

    let mut visited: BTreeSet<(Point, u8)> = BTreeSet::new();
    let mut queue: std::collections::VecDeque<(Point, u8)> = std::collections::VecDeque::new();
    let mut via_buf: Vec<(u8, PlacedViaKey, u64)> = Vec::new();
    let mut blocked_via_spans: Vec<(u8, u8, bool)> = Vec::new();

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
            Copper::ZoneFill { .. } => continue,
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
                    if d <= 1 {
                        for l_idx in 0..l_count {
                            if (t_mask & (1u32 << l_idx)) == 0 {
                                continue;
                            }
                            engine.work.charge(1)?;
                            let key = (p, l_idx as u8);
                            if start_set.contains(&key) {
                                return Ok(true);
                            }
                            let l_name = &stackup[l_idx];
                            let width_opt =
                                match engine.effective_rules.track_width_for(net, l_name) {
                                    Ok(w) => Some(w),
                                    Err(e) if e.status == Status::Unroutable => None,
                                    Err(e) => return Err(e),
                                };
                            if width_opt.is_some() {
                                engine.clear_last_block_generated();
                                if engine.legal_edge_on(net, l_idx, p, p)? {
                                    if visited.insert(key) {
                                        queue.push_back(key);
                                    }
                                } else {
                                    frontier.push_edge_classified(
                                        l_idx as u8,
                                        p,
                                        p,
                                        engine.last_block_generated(),
                                    );
                                    for delta in [[grid, 0], [0, grid], [-grid, 0], [0, -grid]] {
                                        let Some(qx) = p[0].checked_add(delta[0]) else {
                                            continue;
                                        };
                                        let Some(qy) = p[1].checked_add(delta[1]) else {
                                            continue;
                                        };
                                        let q = [qx, qy];
                                        if attachment(l_idx, q, p, &[])?.is_some() {
                                            let q_key = (q, l_idx as u8);
                                            if start_set.contains(&q_key) {
                                                return Ok(true);
                                            }
                                            if visited.insert(q_key) {
                                                queue.push_back(q_key);
                                            }
                                        }
                                    }
                                }
                            }
                            if l_count >= 2 {
                                via_buf.clear();
                                blocked_via_spans.clear();
                                if !visited.contains(&key) {
                                    push_legal_via_transitions_with_blocked(
                                        engine,
                                        net,
                                        p,
                                        l_idx,
                                        via_cost,
                                        &[],
                                        &mut via_buf,
                                        Some(&mut blocked_via_spans),
                                    )?;
                                }
                                if engine.effective_rules.allow_blind_buried_vias && l_count >= 3 {
                                    let at_via_limit =
                                        engine.effective_rules.net(net).max_via_count.is_some_and(
                                            |max_vc| {
                                                engine.copper.via_count_for_net(net) + 1
                                                    > max_vc as usize
                                            },
                                        );
                                    for s_min in 0..l_idx {
                                        for s_max in (l_idx + 1)..l_count {
                                            if s_min == 0 && s_max + 1 == l_count {
                                                continue;
                                            }
                                            if engine
                                                .legal_via(
                                                    net,
                                                    p,
                                                    s_min,
                                                    s_max,
                                                    ViaKind::BlindBuried,
                                                    &[],
                                                )?
                                                .is_some()
                                            {
                                                let pv = PlacedViaKey {
                                                    at: p,
                                                    start_layer: s_min as u8,
                                                    end_layer: s_max as u8,
                                                    kind: 1,
                                                };
                                                via_buf.push((s_min as u8, pv, via_cost));
                                                via_buf.push((s_max as u8, pv, via_cost));
                                            } else if !at_via_limit
                                                && !engine
                                                    .effective_rules
                                                    .disallow_via_on(net, &stackup[s_min..=s_max])
                                            {
                                                blocked_via_spans.push((
                                                    s_min as u8,
                                                    s_max as u8,
                                                    engine.last_block_generated(),
                                                ));
                                            }
                                        }
                                    }
                                }
                                for &(prev_l, pv, _) in &via_buf {
                                    let kind = match pv.kind {
                                        0 => ViaKind::Through,
                                        1 => ViaKind::BlindBuried,
                                        _ => ViaKind::Micro,
                                    };
                                    let span_layers =
                                        &stackup[pv.start_layer as usize..=pv.end_layer as usize];
                                    if let Ok((diam, drill)) = engine.effective_rules.via_dims_for(
                                        net,
                                        kind == ViaKind::Micro,
                                        span_layers,
                                    ) {
                                        let probe_via = Via {
                                            id: String::new(),
                                            net,
                                            at: p,
                                            diameter_nm: diam,
                                            drill_nm: drill,
                                            start_layer: stackup[pv.start_layer as usize].clone(),
                                            end_layer: stackup[pv.end_layer as usize].clone(),
                                            kind,
                                            locked: false,
                                        };
                                        if target.touches_via(&probe_via, stackup, &engine.work)? {
                                            let vkey = (p, prev_l);
                                            if start_set.contains(&vkey) {
                                                return Ok(true);
                                            }
                                            if visited.insert(vkey) {
                                                queue.push_back(vkey);
                                            }
                                        }
                                    }
                                }
                                for &(s_lo, s_hi, via_gen) in &blocked_via_spans {
                                    frontier.push_via_site_classified(s_lo, s_hi, p, via_gen);
                                }
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

    let mut pops = 0usize;
    while let Some((p, l)) = queue.pop_front() {
        if start_set.contains(&(p, l)) || pops >= TARGET_ESCAPE_BUDGET {
            return Ok(true);
        }
        budget.check_before_expand(engine, *expanded, Some(frontier))?;
        *expanded = expanded.checked_add(1).expect("bounded expansion count");
        pops += 1;
        let l_idx = l as usize;
        if engine
            .effective_rules
            .track_width_for(net, &stackup[l_idx])
            .is_ok()
        {
            for delta in [[grid, 0], [0, grid], [-grid, 0], [0, -grid]] {
                engine.work.charge(1)?;
                let Some(qx) = p[0].checked_add(delta[0]) else {
                    continue;
                };
                let Some(qy) = p[1].checked_add(delta[1]) else {
                    continue;
                };
                let q = [qx, qy];
                let nkey = (q, l);
                if visited.contains(&nkey) {
                    continue;
                }
                engine.clear_last_block_generated();
                if engine.legal_edge_on(net, l_idx, q, p)? {
                    if start_set.contains(&nkey) {
                        return Ok(true);
                    }
                    visited.insert(nkey);
                    queue.push_back(nkey);
                } else {
                    frontier.push_edge_classified(l, q, p, engine.last_block_generated());
                }
            }
        }
        if l_count >= 2 {
            via_buf.clear();
            blocked_via_spans.clear();
            push_legal_via_transitions_with_blocked(
                engine,
                net,
                p,
                l_idx,
                via_cost,
                &[],
                &mut via_buf,
                Some(&mut blocked_via_spans),
            )?;
            for &(prev_l, _, _) in &via_buf {
                let vkey = (p, prev_l);
                if start_set.contains(&vkey) {
                    return Ok(true);
                }
                if visited.insert(vkey) {
                    queue.push_back(vkey);
                }
            }
            for &(s_lo, s_hi, via_gen) in &blocked_via_spans {
                frontier.push_via_site_classified(s_lo, s_hi, p, via_gen);
            }
        }
    }

    Ok(pops == 0)
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
            Copper::ZoneFill { .. } => continue,
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
    disallow_smd_via_in_pad: bool,
    work: &WorkBudget,
) -> Result<u64, Failure> {
    let l_name = &stackup[layer_idx];
    let track_width = match rules.track_width_for(net, l_name) {
        Ok(w) => Some(w),
        Err(e) if e.status == Status::Unroutable => None,
        Err(e) => return Err(e),
    };
    let eff_width = track_width.map_or(max_eff_width, |w| w.max(max_eff_width));
    let my_bit = 1u32 << layer_idx;
    let mut lower_bound: Option<u64> = None;
    for target in targets.iter().flatten() {
        work.charge(1)?;
        let t_mask = target.layer_mask(stackup);
        let smd_no_via = disallow_smd_via_in_pad
            && net != 0
            && matches!(*target, Copper::Pad(pad) if pad.drill.is_none());
        let d_via_bound = if smd_no_via {
            let mut d_pad_track: Option<u64> = None;
            for (t_idx, t_layer) in stackup.iter().enumerate() {
                if (t_mask & (1u32 << t_idx)) == 0 {
                    continue;
                }
                match rules.track_width_for(net, t_layer) {
                    Ok(w_l) => {
                        let d = contact_steps(p, *target, w_l, grid);
                        d_pad_track = Some(d_pad_track.map_or(d, |best| best.min(d)));
                    }
                    Err(e) if e.status == Status::Unroutable => {}
                    Err(e) => return Err(e),
                }
            }
            d_pad_track.unwrap_or_else(|| contact_steps(p, *target, eff_width, grid))
        } else {
            contact_steps(p, *target, eff_width, grid)
        };
        let steps = if let Some(width) = track_width {
            if (t_mask & my_bit) != 0 {
                let d_track = contact_steps(p, *target, width, grid);
                d_track.min(d_via_bound.saturating_add(via_cost))
            } else {
                d_via_bound.saturating_add(via_cost)
            }
        } else {
            d_via_bound.saturating_add(via_cost)
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
        Copper::ZoneFill { poly, .. } => {
            let mut lo = [i64::MAX, i64::MAX];
            let mut hi = [i64::MIN, i64::MIN];
            for pt in &poly.outer {
                for k in 0..2 {
                    lo[k] = lo[k].min(pt[k]);
                    hi[k] = hi[k].max(pt[k]);
                }
            }
            (lo, hi, None)
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

#[derive(Clone, Copy, PartialEq, Eq)]
struct TieG {
    g: u64,
    prefer_higher: bool,
}

impl Ord for TieG {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        if self.prefer_higher {
            other.g.cmp(&self.g)
        } else {
            self.g.cmp(&other.g)
        }
    }
}

impl PartialOrd for TieG {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
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
    let start_vec: Vec<(Point, u8, bool)> = starts.iter().map(|&p| (p, 0, false)).collect();
    let empty_filter = ConflictFilter::default();
    let mut budget = crate::negotiate::TrialBudget::fixed(limit);
    walk_multilayer(
        engine,
        0,
        &start_vec,
        &mut budget,
        expanded,
        true,
        false,
        &empty_filter,
        &[],
        None,
        None,
        false,
        None,
        |p, _| heuristic(p),
        |_, rs, a, b, _| advance(rs, a, b),
        |_, _, _, _, _, _, _| Ok(()),
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
    starts: &[(Point, u8, bool)],
    budget: &mut crate::negotiate::TrialBudget<'_>,
    expanded: &mut u64,
    track_direction_matters: bool,
    track_via_count: bool,
    conflict_filter: &ConflictFilter,
    track_via_conflicts: &[PlacedViaKey],
    history: Option<&crate::negotiate::CongestionHistory>,
    mut frontier: Option<&mut crate::negotiate::BlockedFrontier>,
    prefer_higher_g: bool,
    mut escape_probe: Option<
        &mut dyn FnMut(
            &mut u64,
            &mut crate::negotiate::BlockedFrontier,
            &mut crate::negotiate::TrialBudget<'_>,
        ) -> Result<bool, Failure>,
    >,
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
        bool,
        &mut Vec<(u8, PlacedViaKey, u64)>,
        Option<&mut Vec<(u8, u8, bool)>>,
    ) -> Result<(), Failure>,
    finished: impl Fn(Point, usize, Option<PathEdge>) -> Result<bool, Failure>,
) -> Result<Path, Failure> {
    let grid = engine.input.rules.grid_nm;
    let l_count = engine.layer_count();
    let mut via_arena = ViaHistoryArena::new();
    let mut tv_arena = TrackViaBlockedArena::new();
    let mut queue = BinaryHeap::new();
    let mut best: HashMap<State, u64> = HashMap::new();
    let mut settled_by_pos: HashMap<(Point, u8, u8, bool, bool, bool), Vec<(u32, u32, u16, u64)>> =
        HashMap::new();
    let mut parent: HashMap<State, (State, PathEdge)> = HashMap::new();
    let can_cache_vias = frontier.is_some() && l_count >= 2;
    let only_through_vias =
        !engine.effective_rules.allow_blind_buried_vias && !engine.effective_rules.allow_microvias;
    let mut via_cache: HashMap<(Point, u8), (Vec<(u8, PlacedViaKey, u64)>, Vec<(u8, u8, bool)>)> =
        HashMap::new();
    for &(at, layer, via_only) in starts {
        engine.work.search_storage(
            queue.len() + best.len() + parent.len() + via_arena.total_len + tv_arena.total_len + 2,
        )?;
        engine.work.charge(1)?;
        let initial = State {
            at,
            layer,
            direction: 4,
            arrived_by_via: false,
            is_via_goal: false,
            via_only,
            via_history: 0,
            via_count: 0,
            track_via_blocked: 0,
        };
        let h = heuristic(at, layer as usize)?;
        queue.push(Reverse((
            h,
            TieG {
                g: 0u64,
                prefer_higher: prefer_higher_g,
            },
            initial,
        )));
        best.insert(initial, 0u64);
    }

    let is_dominated_by_settled =
        |settled: &HashMap<(Point, u8, u8, bool, bool, bool), Vec<(u32, u32, u16, u64)>>,
         arena: &ViaHistoryArena,
         tv_arena: &TrackViaBlockedArena,
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
                cand.via_only,
            )) {
                for &(v1, tvb1, vc1, g1) in list {
                    work.charge(1)?;
                    if g1 <= cand_g
                        && vc1 <= cand.via_count
                        && arena.is_subset(v1, cand.via_history)
                        && tv_arena.is_subset(tvb1, cand.track_via_blocked)
                    {
                        return Ok(true);
                    }
                }
            }
            Ok(false)
        };

    fn reconstruct_path(
        parent: &HashMap<State, (State, PathEdge)>,
        work: &WorkBudget,
        goal_state: State,
        goal_cost: u64,
    ) -> Result<Path, Failure> {
        work.charge(1)?;
        let mut edges = Vec::new();
        let mut points = vec![goal_state.at];
        let mut s = goal_state;
        while let Some((previous, edge)) = parent.get(&s) {
            work.charge(1)?;
            edges.push(*edge);
            s = *previous;
            points.push(s.at);
        }
        work.charge((points.len() + edges.len()) as u64)?;
        points.reverse();
        edges.reverse();
        Ok(Path {
            points,
            edges,
            steps: goal_cost,
        })
    }

    let mut via_buf: Vec<(u8, PlacedViaKey, u64)> = Vec::new();
    let mut blocked_via_spans: Vec<(u8, u8, bool)> = Vec::new();
    let mut local_exp = 0u64;
    while !queue.is_empty() {
        engine.work.charge(1)?;
        let Reverse((_f, tie_g, state)) = queue.pop().unwrap();
        let cost = tie_g.g;
        if best.get(&state) != Some(&cost) {
            continue;
        }
        if is_dominated_by_settled(
            &settled_by_pos,
            &via_arena,
            &tv_arena,
            state,
            cost,
            &engine.work,
        )? {
            continue;
        }
        budget.check_before_expand(engine, *expanded, frontier.as_deref())?;
        *expanded = expanded.checked_add(1).expect("bounded expansion count");
        local_exp += 1;
        if local_exp == 2048 {
            if let (Some(probe), Some(f)) = (escape_probe.take(), frontier.as_deref_mut()) {
                if !probe(expanded, f, budget)? {
                    return Err(Failure::new(
                        Status::Unroutable,
                        "target terminal is locally unreachable from routing grid",
                    ));
                }
            }
        }
        settled_by_pos
            .entry((
                state.at,
                state.layer,
                state.direction,
                state.arrived_by_via,
                state.is_via_goal,
                state.via_only,
            ))
            .or_default()
            .push((
                state.via_history,
                state.track_via_blocked,
                state.via_count,
                cost,
            ));

        if state.is_via_goal
            || (!state.via_only
                && finished(
                    state.at,
                    state.layer as usize,
                    parent.get(&state).map(|(_, e)| *e),
                )?)
        {
            return reconstruct_path(&parent, &engine.work, state, cost);
        }

        let state_run_start =
            if engine.input.rules.same_net_hole_clearance_nm > 0 && state.direction < 4 {
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
        let base_turned_blocked = if !track_via_conflicts.is_empty() && net != 0 {
            if let Some(rs) = state_run_start {
                let mut add: Vec<u16> = Vec::new();
                for (idx, &cv) in track_via_conflicts.iter().enumerate() {
                    let idx_u16 = u16::try_from(idx).map_err(|_| {
                        Failure::new(
                            Status::BudgetExhausted,
                            "track-via conflict set exceeds u16 capacity",
                        )
                    })?;
                    if tv_arena.contains(state.track_via_blocked, idx_u16) {
                        continue;
                    }
                    if state.layer < cv.start_layer || state.layer > cv.end_layer {
                        continue;
                    }
                    if !engine.edge_clears_same_net_vias(
                        net,
                        state.layer as usize,
                        rs,
                        state.at,
                        &[cv],
                    )? {
                        add.push(idx_u16);
                    }
                }
                tv_arena.insert_all(state.track_via_blocked, &add)
            } else {
                state.track_via_blocked
            }
        } else {
            0
        };

        let mut any_track_blocked = false;
        if !state.via_only {
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
                let is_continuing = direction as u8 == state.direction;
                let run_start = if is_continuing { state_run_start } else { None };
                let Some(at) = advance(
                    state.layer as usize,
                    run_start,
                    state.at,
                    [x, y],
                    via_arena.get(state.via_history),
                )?
                else {
                    any_track_blocked = true;
                    if let Some(f) = frontier.as_deref_mut() {
                        f.push_edge_classified(
                            state.layer,
                            state.at,
                            [x, y],
                            engine.last_block_generated(),
                        );
                    }
                    continue;
                };
                let next_tvb = if !track_via_conflicts.is_empty() && net != 0 {
                    let (base_tvb, seg_start) = if is_continuing {
                        (state.track_via_blocked, state_run_start.unwrap_or(state.at))
                    } else {
                        (base_turned_blocked, state.at)
                    };
                    let mut add: Vec<u16> = Vec::new();
                    for (idx, &cv) in track_via_conflicts.iter().enumerate() {
                        let idx_u16 = u16::try_from(idx).map_err(|_| {
                            Failure::new(
                                Status::BudgetExhausted,
                                "track-via conflict set exceeds u16 capacity",
                            )
                        })?;
                        if tv_arena.contains(base_tvb, idx_u16) {
                            continue;
                        }
                        if state.layer < cv.start_layer || state.layer > cv.end_layer {
                            continue;
                        }
                        let dot = i128::from(cv.at[0] - at[0]) * i128::from(delta[0])
                            + i128::from(cv.at[1] - at[1]) * i128::from(delta[1]);
                        if dot <= 0
                            && !engine.edge_clears_same_net_vias(
                                net,
                                state.layer as usize,
                                seg_start,
                                at,
                                &[cv],
                            )?
                        {
                            add.push(idx_u16);
                        }
                    }
                    tv_arena.insert_all(base_tvb, &add)
                } else {
                    0
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
                    via_only: false,
                    via_history: state.via_history,
                    via_count: state.via_count,
                    track_via_blocked: next_tvb,
                };
                let step_penalty = match history {
                    Some(hist) if !hist.is_empty() => {
                        engine.work.charge(1)?;
                        hist.step_penalty(state.layer, at, engine.input.bounds.min, grid)
                    }
                    _ => 0,
                };
                let step_cost = 1u64.checked_add(step_penalty).expect("bounded step cost");
                let g = cost.checked_add(step_cost).expect("bounded search cost");
                if best.get(&next).is_some_and(|old| *old <= g) {
                    continue;
                }
                if is_dominated_by_settled(
                    &settled_by_pos,
                    &via_arena,
                    &tv_arena,
                    next,
                    g,
                    &engine.work,
                )? {
                    continue;
                }
                let extra = 1
                    + usize::from(!best.contains_key(&next))
                    + usize::from(!parent.contains_key(&next));
                engine.work.search_storage(
                    queue.len()
                        + best.len()
                        + parent.len()
                        + via_arena.total_len
                        + tv_arena.total_len
                        + extra,
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
                    TieG {
                        g,
                        prefer_higher: prefer_higher_g,
                    },
                    next,
                )));
            }
        }

        if !(only_through_vias && state.arrived_by_via) {
            via_buf.clear();
            blocked_via_spans.clear();
            let want_via_frontier =
                l_count >= 2 && frontier.is_some() && (state.via_only || any_track_blocked);
            let prior_vias_slice = via_arena.get(state.via_history);
            if can_cache_vias
                && !state.via_only
                && state.via_count == 0
                && prior_vias_slice.is_empty()
            {
                let cache_key = (state.at, if only_through_vias { 0 } else { state.layer });
                if let Some((cached_vias, cached_blocked)) = via_cache.get(&cache_key) {
                    engine.work.charge(1)?;
                    if only_through_vias {
                        if let Some(&(_, pv, v_cost)) = cached_vias.first() {
                            for tl in 0..l_count {
                                if tl as u8 != state.layer {
                                    via_buf.push((tl as u8, pv, v_cost));
                                }
                            }
                        }
                    } else {
                        via_buf.extend_from_slice(cached_vias);
                    }
                    if want_via_frontier {
                        blocked_via_spans.extend_from_slice(cached_blocked);
                    }
                } else {
                    via_transitions(
                        state.at,
                        state.layer as usize,
                        prior_vias_slice,
                        0,
                        false,
                        &mut via_buf,
                        Some(&mut blocked_via_spans),
                    )?;
                    engine.work.search_storage(via_cache.len() + 1)?;
                    via_cache.insert(cache_key, (via_buf.clone(), blocked_via_spans.clone()));
                    if !want_via_frontier {
                        blocked_via_spans.clear();
                    }
                }
            } else {
                via_transitions(
                    state.at,
                    state.layer as usize,
                    prior_vias_slice,
                    state.via_count,
                    state.via_only,
                    &mut via_buf,
                    want_via_frontier.then_some(&mut blocked_via_spans),
                )?;
            }
            if want_via_frontier {
                if let Some(f) = frontier.as_deref_mut() {
                    for &(s_lo, s_hi, via_gen) in &blocked_via_spans {
                        f.push_via_site_classified(s_lo, s_hi, state.at, via_gen);
                    }
                }
            }
            for &(target_layer, pv, v_cost) in &via_buf {
                engine.work.charge(1)?;
                if let Some(idx) = track_via_conflicts.iter().position(|&cv| cv == pv) {
                    let idx_u16 = u16::try_from(idx).map_err(|_| {
                        Failure::new(
                            Status::BudgetExhausted,
                            "track-via conflict set exceeds u16 capacity",
                        )
                    })?;
                    if tv_arena.contains(base_turned_blocked, idx_u16) {
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
                    via_only: false,
                    via_history: next_vh,
                    via_count: next_vc,
                    track_via_blocked: base_turned_blocked,
                };
                let v_penalty = match history {
                    Some(hist) if !hist.is_empty() => {
                        engine.work.charge(1)?;
                        hist.via_penalty(
                            pv.at,
                            pv.start_layer,
                            pv.end_layer,
                            engine.input.bounds.min,
                            grid,
                        )
                    }
                    _ => 0,
                };
                let total_v_cost = v_cost.checked_add(v_penalty).expect("bounded via cost");
                let g = cost.checked_add(total_v_cost).expect("bounded search cost");
                if best.get(&next).is_some_and(|old| *old <= g) {
                    continue;
                }
                if is_dominated_by_settled(
                    &settled_by_pos,
                    &via_arena,
                    &tv_arena,
                    next,
                    g,
                    &engine.work,
                )? {
                    continue;
                }
                let extra = 1
                    + usize::from(!best.contains_key(&next))
                    + usize::from(!parent.contains_key(&next));
                engine.work.search_storage(
                    queue.len()
                        + best.len()
                        + parent.len()
                        + via_arena.total_len
                        + tv_arena.total_len
                        + extra,
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
                    TieG {
                        g,
                        prefer_higher: prefer_higher_g,
                    },
                    next,
                )));
            }
        }
    }
    Err(Failure::new(
        Status::Unroutable,
        "search frontier exhausted",
    ))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Hash)]
struct BiKey {
    at: Point,
    layer: u8,
    arrived_by_via: bool,
}

#[derive(Clone, Copy, Debug)]
struct BwdStep {
    next: Option<BiKey>,
    edge: PathEdge,
}

#[derive(Clone, Copy, Debug)]
enum MeetRecord {
    DirectFwd(State),
    DirectFwdVia { prev: State, pv: PlacedViaKey },
    Joined { fwd_key: BiKey, bwd_key: BiKey },
}

fn h_bwd_estimate(
    p: Point,
    layer_idx: usize,
    starts: &[(Point, u8, bool)],
    layer_bboxes: &[Option<(i64, i64, i64, i64)>],
    via_cost: u64,
    grid: i64,
) -> u64 {
    let g = grid as u64;
    let l_u8 = layer_idx as u8;
    let mut best = u64::MAX;
    if starts.len() <= 16 {
        for &(s, s_l, _) in starts {
            let dx = p[0].abs_diff(s[0]).div_ceil(g);
            let dy = p[1].abs_diff(s[1]).div_ceil(g);
            let v = if l_u8 == s_l { 0 } else { via_cost };
            best = best.min(dx.saturating_add(dy).saturating_add(v));
        }
    } else {
        for (s_l, bbox) in layer_bboxes.iter().enumerate() {
            let Some((min_x, max_x, min_y, max_y)) = *bbox else {
                continue;
            };
            let dx_nm = if p[0] < min_x {
                min_x.abs_diff(p[0])
            } else if p[0] > max_x {
                p[0].abs_diff(max_x)
            } else {
                0
            };
            let dy_nm = if p[1] < min_y {
                min_y.abs_diff(p[1])
            } else if p[1] > max_y {
                p[1].abs_diff(max_y)
            } else {
                0
            };
            let dx = dx_nm.div_ceil(g);
            let dy = dy_nm.div_ceil(g);
            let v = if layer_idx == s_l { 0 } else { via_cost };
            best = best.min(dx.saturating_add(dy).saturating_add(v));
        }
    }
    best
}

fn reconstruct_bi_path(
    parent_fwd: &HashMap<State, (State, PathEdge)>,
    parent_bwd: &HashMap<BiKey, BwdStep>,
    work: &WorkBudget,
    rec: &MeetRecord,
    total_cost: u64,
) -> Result<Path, Failure> {
    work.charge(1)?;
    let mut edges = Vec::new();
    let mut points = Vec::new();
    match *rec {
        MeetRecord::DirectFwd(goal_state) => {
            points.push(goal_state.at);
            let mut s = goal_state;
            while let Some((previous, edge)) = parent_fwd.get(&s) {
                work.charge(1)?;
                edges.push(*edge);
                s = *previous;
                points.push(s.at);
            }
            work.charge((points.len() + edges.len()) as u64)?;
            points.reverse();
            edges.reverse();
        }
        MeetRecord::DirectFwdVia { prev, pv } => {
            points.push(pv.at);
            edges.push(PathEdge::Via(pv));
            points.push(prev.at);
            let mut s = prev;
            while let Some((previous, edge)) = parent_fwd.get(&s) {
                work.charge(1)?;
                edges.push(*edge);
                s = *previous;
                points.push(s.at);
            }
            work.charge((points.len() + edges.len()) as u64)?;
            points.reverse();
            edges.reverse();
        }
        MeetRecord::Joined { fwd_key, bwd_key } => {
            let mut s = State {
                at: fwd_key.at,
                layer: fwd_key.layer,
                direction: 4,
                arrived_by_via: fwd_key.arrived_by_via,
                is_via_goal: false,
                via_only: false,
                via_history: 0,
                via_count: 0,
                track_via_blocked: 0,
            };
            points.push(s.at);
            while let Some((previous, edge)) = parent_fwd.get(&s) {
                work.charge(1)?;
                edges.push(*edge);
                s = *previous;
                points.push(s.at);
            }
            points.reverse();
            edges.reverse();
            let mut cur = Some(bwd_key);
            while let Some(k) = cur {
                work.charge(1)?;
                let step = parent_bwd.get(&k).expect("backward parent step exists");
                edges.push(step.edge);
                let next_pt = match step.edge {
                    PathEdge::Track { end, .. } => end,
                    PathEdge::Via(pv) => pv.at,
                };
                points.push(next_pt);
                cur = step.next;
            }
            work.charge((points.len() + edges.len()) as u64)?;
        }
    }
    Ok(Path {
        points,
        edges,
        steps: total_cost,
    })
}

#[allow(clippy::too_many_arguments)]
fn seed_backward_a_star(
    engine: &Engine,
    net: u32,
    starts: &[(Point, u8, bool)],
    targets: &[Vec<Copper<'_>>],
    max_eff_width: i64,
    via_cost: u64,
    only_through_vias: bool,
    prefer_higher_g: bool,
    track_ok_on_layer: &[bool],
    layer_bboxes: &[Option<(i64, i64, i64, i64)>],
    history: Option<&crate::negotiate::CongestionHistory>,
    attachment: &impl Fn(usize, Point, Point, &[PlacedViaKey]) -> Result<Option<Point>, Failure>,
    via_transitions: &impl Fn(
        Point,
        usize,
        &[PlacedViaKey],
        u16,
        bool,
        &mut Vec<(u8, PlacedViaKey, u64)>,
        Option<&mut Vec<(u8, u8, bool)>>,
    ) -> Result<(), Failure>,
    via_cache: &mut HashMap<(Point, u8), (Vec<(u8, PlacedViaKey, u64)>, Vec<(u8, u8, bool)>)>,
    q_bwd: &mut BinaryHeap<Reverse<(u64, TieG, BiKey)>>,
    best_bwd: &mut HashMap<BiKey, u64>,
    parent_bwd: &mut HashMap<BiKey, BwdStep>,
    best_fwd_meet: &HashMap<BiKey, u64>,
    best_mu: &mut Option<(u64, MeetRecord)>,
) -> Result<(), Failure> {
    let stackup = engine.all_layers_slice();
    let l_count = engine.layer_count();
    let grid = engine.input.rules.grid_nm;
    let route_radius = (max_eff_width + 1) / 2;
    let bounds = engine.input.bounds;

    let mut phases: BTreeSet<(i64, i64)> = BTreeSet::new();
    for &(s, _, _) in starts {
        phases.insert((s[0].rem_euclid(grid), s[1].rem_euclid(grid)));
    }

    let mut via_buf: Vec<(u8, PlacedViaKey, u64)> = Vec::new();
    let mut blocked_buf: Vec<(u8, u8, bool)> = Vec::new();

    let mut record_seed = |bkey: BiKey, g_seed: u64, step: BwdStep| -> Result<(), Failure> {
        if best_bwd.get(&bkey).is_some_and(|&old| old <= g_seed) {
            return Ok(());
        }
        let extra = 1
            + usize::from(!best_bwd.contains_key(&bkey))
            + usize::from(!parent_bwd.contains_key(&bkey));
        engine
            .work
            .search_storage(q_bwd.len() + best_bwd.len() + parent_bwd.len() + extra)?;
        engine.work.charge(1)?;
        best_bwd.insert(bkey, g_seed);
        parent_bwd.insert(bkey, step);
        let h = h_bwd_estimate(
            bkey.at,
            bkey.layer as usize,
            starts,
            layer_bboxes,
            via_cost,
            grid,
        );
        q_bwd.push(Reverse((
            g_seed.checked_add(h).expect("bounded bwd heuristic"),
            TieG {
                g: g_seed,
                prefer_higher: prefer_higher_g,
            },
            bkey,
        )));
        for fwd_via in [false, true] {
            if only_through_vias && fwd_via && bkey.arrived_by_via {
                continue;
            }
            let fkey = BiKey {
                at: bkey.at,
                layer: bkey.layer,
                arrived_by_via: fwd_via,
            };
            if let Some(&g_fwd) = best_fwd_meet.get(&fkey) {
                let total = g_fwd.checked_add(g_seed).expect("bounded meet cost");
                if best_mu.as_ref().is_none_or(|(c, _)| total < *c) {
                    *best_mu = Some((
                        total,
                        MeetRecord::Joined {
                            fwd_key: fkey,
                            bwd_key: bkey,
                        },
                    ));
                }
            }
        }
        Ok(())
    };

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
            Copper::ZoneFill { .. } => continue,
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
                    if contact_steps(p, *target, max_eff_width, grid) <= 1 {
                        for l_idx in 0..l_count {
                            if (t_mask & (1u32 << l_idx)) != 0 && track_ok_on_layer[l_idx] {
                                for delta in [[grid, 0], [0, grid], [-grid, 0], [0, -grid]] {
                                    engine.work.charge(1)?;
                                    let Some(qx) = p[0].checked_add(delta[0]) else {
                                        continue;
                                    };
                                    let Some(qy) = p[1].checked_add(delta[1]) else {
                                        continue;
                                    };
                                    if let Some(p_attach) = attachment(l_idx, p, [qx, qy], &[])? {
                                        let step_pen = match history {
                                            Some(hist) if !hist.is_empty() => {
                                                engine.work.charge(1)?;
                                                hist.step_penalty(
                                                    l_idx as u8,
                                                    p_attach,
                                                    bounds.min,
                                                    grid,
                                                )
                                            }
                                            _ => 0,
                                        };
                                        let g_seed =
                                            1u64.checked_add(step_pen).expect("bounded step cost");
                                        let bkey = BiKey {
                                            at: p,
                                            layer: l_idx as u8,
                                            arrived_by_via: false,
                                        };
                                        let step = BwdStep {
                                            next: None,
                                            edge: PathEdge::Track {
                                                start: p,
                                                end: p_attach,
                                                layer: l_idx as u8,
                                            },
                                        };
                                        record_seed(bkey, g_seed, step)?;
                                    }
                                }
                            }
                        }
                        if l_count >= 2 {
                            for l_from in 0..l_count {
                                via_buf.clear();
                                blocked_buf.clear();
                                let cache_key =
                                    (p, if only_through_vias { 0 } else { l_from as u8 });
                                if let Some((cached_vias, _)) = via_cache.get(&cache_key) {
                                    engine.work.charge(1)?;
                                    if only_through_vias {
                                        if let Some(&(_, pv, v_cost)) = cached_vias.first() {
                                            for tl in 0..l_count {
                                                if tl != l_from {
                                                    via_buf.push((tl as u8, pv, v_cost));
                                                }
                                            }
                                        }
                                    } else {
                                        via_buf.extend_from_slice(cached_vias);
                                    }
                                } else {
                                    via_transitions(
                                        p,
                                        l_from,
                                        &[],
                                        0,
                                        false,
                                        &mut via_buf,
                                        Some(&mut blocked_buf),
                                    )?;
                                    engine.work.search_storage(via_cache.len() + 1)?;
                                    via_cache
                                        .insert(cache_key, (via_buf.clone(), blocked_buf.clone()));
                                }
                                for &(_, pv, v_cost) in &via_buf {
                                    let via_mask =
                                        span_mask(pv.start_layer as usize, pv.end_layer as usize);
                                    if (t_mask & via_mask) == 0 {
                                        continue;
                                    }
                                    let kind = match pv.kind {
                                        0 => ViaKind::Through,
                                        1 => ViaKind::BlindBuried,
                                        _ => ViaKind::Micro,
                                    };
                                    let span_layers =
                                        &stackup[pv.start_layer as usize..=pv.end_layer as usize];
                                    if let Ok((diam, drill)) = engine.effective_rules.via_dims_for(
                                        net,
                                        kind == ViaKind::Micro,
                                        span_layers,
                                    ) {
                                        let probe_via = Via {
                                            id: String::new(),
                                            net,
                                            at: p,
                                            diameter_nm: diam,
                                            drill_nm: drill,
                                            start_layer: stackup[pv.start_layer as usize].clone(),
                                            end_layer: stackup[pv.end_layer as usize].clone(),
                                            kind,
                                            locked: false,
                                        };
                                        if target.touches_via(&probe_via, stackup, &engine.work)? {
                                            let v_pen = match history {
                                                Some(hist) if !hist.is_empty() => {
                                                    engine.work.charge(1)?;
                                                    hist.via_penalty(
                                                        pv.at,
                                                        pv.start_layer,
                                                        pv.end_layer,
                                                        bounds.min,
                                                        grid,
                                                    )
                                                }
                                                _ => 0,
                                            };
                                            let g_seed = v_cost
                                                .checked_add(v_pen)
                                                .expect("bounded via cost");
                                            let bkey = BiKey {
                                                at: p,
                                                layer: l_from as u8,
                                                arrived_by_via: true,
                                            };
                                            let step = BwdStep {
                                                next: None,
                                                edge: PathEdge::Via(pv),
                                            };
                                            record_seed(bkey, g_seed, step)?;
                                        }
                                    }
                                }
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
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn walk_multilayer_bidirectional(
    engine: &Engine,
    net: u32,
    starts: &[(Point, u8, bool)],
    targets: &[Vec<Copper<'_>>],
    max_eff_width: i64,
    via_cost: u64,
    budget: &mut crate::negotiate::TrialBudget<'_>,
    expanded: &mut u64,
    history: Option<&crate::negotiate::CongestionHistory>,
    mut frontier: Option<&mut crate::negotiate::BlockedFrontier>,
    prefer_higher_g: bool,
    mut escape_probe: Option<
        &mut dyn FnMut(
            &mut u64,
            &mut crate::negotiate::BlockedFrontier,
            &mut crate::negotiate::TrialBudget<'_>,
        ) -> Result<bool, Failure>,
    >,
    heuristic: impl Fn(Point, usize) -> Result<u64, Failure>,
    attachment: &impl Fn(usize, Point, Point, &[PlacedViaKey]) -> Result<Option<Point>, Failure>,
    via_transitions: impl Fn(
        Point,
        usize,
        &[PlacedViaKey],
        u16,
        bool,
        &mut Vec<(u8, PlacedViaKey, u64)>,
        Option<&mut Vec<(u8, u8, bool)>>,
    ) -> Result<(), Failure>,
    finished: impl Fn(Point, usize, Option<PathEdge>) -> Result<bool, Failure>,
) -> Result<Path, Failure> {
    let grid = engine.input.rules.grid_nm;
    let l_count = engine.layer_count();
    let stackup = engine.all_layers_slice();
    let only_through_vias =
        !engine.effective_rules.allow_blind_buried_vias && !engine.effective_rules.allow_microvias;
    let mut track_ok_on_layer = vec![false; l_count];
    for (l_idx, l_name) in stackup.iter().enumerate() {
        match engine.effective_rules.track_width_for(net, l_name) {
            Ok(_) => track_ok_on_layer[l_idx] = true,
            Err(e) if e.status == Status::Unroutable => {}
            Err(e) => return Err(e),
        }
    }
    let mut layer_bboxes: Vec<Option<(i64, i64, i64, i64)>> = vec![None; l_count];
    for &(s, s_l, _) in starts {
        let slot = &mut layer_bboxes[s_l as usize];
        *slot = Some(match *slot {
            None => (s[0], s[0], s[1], s[1]),
            Some((mnx, mxx, mny, mxy)) => {
                (mnx.min(s[0]), mxx.max(s[0]), mny.min(s[1]), mxy.max(s[1]))
            }
        });
    }

    let mut q_fwd: BinaryHeap<Reverse<(u64, TieG, State)>> = BinaryHeap::new();
    let mut best_fwd: HashMap<State, u64> = HashMap::new();
    let mut best_fwd_meet: HashMap<BiKey, u64> = HashMap::new();
    let mut settled_fwd: std::collections::HashSet<State> = std::collections::HashSet::new();
    let mut parent_fwd: HashMap<State, (State, PathEdge)> = HashMap::new();

    let mut q_bwd: BinaryHeap<Reverse<(u64, TieG, BiKey)>> = BinaryHeap::new();
    let mut best_bwd: HashMap<BiKey, u64> = HashMap::new();
    let mut settled_bwd: std::collections::HashSet<BiKey> = std::collections::HashSet::new();
    let mut parent_bwd: HashMap<BiKey, BwdStep> = HashMap::new();

    let mut via_cache: HashMap<(Point, u8), (Vec<(u8, PlacedViaKey, u64)>, Vec<(u8, u8, bool)>)> =
        HashMap::new();
    let mut via_only_unpopped = 0usize;
    for &(at, layer, via_only) in starts {
        engine
            .work
            .search_storage(q_fwd.len() + best_fwd.len() + parent_fwd.len() + 2)?;
        engine.work.charge(1)?;
        let initial = State {
            at,
            layer,
            direction: 4,
            arrived_by_via: false,
            is_via_goal: false,
            via_only,
            via_history: 0,
            via_count: 0,
            track_via_blocked: 0,
        };
        let h = heuristic(at, layer as usize)?;
        q_fwd.push(Reverse((
            h,
            TieG {
                g: 0,
                prefer_higher: prefer_higher_g,
            },
            initial,
        )));
        best_fwd.insert(initial, 0);
        if via_only {
            via_only_unpopped += 1;
        } else {
            best_fwd_meet.insert(
                BiKey {
                    at,
                    layer,
                    arrived_by_via: false,
                },
                0,
            );
        }
    }

    let mut bwd_seeded = false;
    let mut best_mu: Option<(u64, MeetRecord)> = None;
    let mut via_buf: Vec<(u8, PlacedViaKey, u64)> = Vec::new();
    let mut blocked_via_spans: Vec<(u8, u8, bool)> = Vec::new();
    let mut local_exp = 0u64;

    while !q_fwd.is_empty() || (bwd_seeded && !q_bwd.is_empty()) {
        if bwd_seeded {
            while let Some(Reverse((_, tie_g, st))) = q_fwd.peek() {
                if best_fwd.get(st) == Some(&tie_g.g) && !settled_fwd.contains(st) {
                    break;
                }
                q_fwd.pop();
            }
            while let Some(Reverse((_, tie_g, bk))) = q_bwd.peek() {
                if best_bwd.get(bk) == Some(&tie_g.g) && !settled_bwd.contains(bk) {
                    break;
                }
                q_bwd.pop();
            }
            if let Some((mu, ref rec)) = best_mu {
                let min_f_fwd = q_fwd.peek().map_or(u64::MAX, |Reverse((f, _, _))| *f);
                let min_f_bwd = q_bwd.peek().map_or(u64::MAX, |Reverse((f, _, _))| *f);
                if mu <= min_f_fwd || (via_only_unpopped == 0 && mu <= min_f_bwd) {
                    return reconstruct_bi_path(&parent_fwd, &parent_bwd, &engine.work, rec, mu);
                }
            } else if q_fwd.is_empty()
                || (!best_bwd.is_empty() && q_bwd.is_empty() && via_only_unpopped == 0)
            {
                // All reverse attachment states were explored on every source
                // grid phase. A via-only start can still join on its first via.
                // With no reverse seeds, forward probes still collect blockers.
                break;
            }
        }

        let expand_fwd =
            !bwd_seeded || q_bwd.is_empty() || (!q_fwd.is_empty() && q_fwd.len() <= q_bwd.len());
        if expand_fwd {
            engine.work.charge(1)?;
            let Reverse((_f, tie_g, state)) = q_fwd.pop().unwrap();
            let cost = tie_g.g;
            if best_fwd.get(&state) != Some(&cost) || !settled_fwd.insert(state) {
                continue;
            }
            if state.via_only {
                via_only_unpopped -= 1;
            }
            budget.check_before_expand(engine, *expanded, frontier.as_deref())?;
            *expanded = expanded.checked_add(1).expect("bounded expansion count");
            local_exp += 1;
            if local_exp == 256 && !bwd_seeded {
                bwd_seeded = true;
                seed_backward_a_star(
                    engine,
                    net,
                    starts,
                    targets,
                    max_eff_width,
                    via_cost,
                    only_through_vias,
                    prefer_higher_g,
                    &track_ok_on_layer,
                    &layer_bboxes,
                    history,
                    attachment,
                    &via_transitions,
                    &mut via_cache,
                    &mut q_bwd,
                    &mut best_bwd,
                    &mut parent_bwd,
                    &best_fwd_meet,
                    &mut best_mu,
                )?;
            }
            if local_exp == 2048 {
                if let (Some(probe), Some(f)) = (escape_probe.take(), frontier.as_deref_mut()) {
                    if !probe(expanded, f, budget)? {
                        return Err(Failure::new(
                            Status::Unroutable,
                            "target terminal is locally unreachable from routing grid",
                        ));
                    }
                }
            }
            if state.is_via_goal
                || (!state.via_only
                    && finished(
                        state.at,
                        state.layer as usize,
                        parent_fwd.get(&state).map(|(_, e)| *e),
                    )?)
            {
                if !bwd_seeded {
                    return reconstruct_bi_path(
                        &parent_fwd,
                        &parent_bwd,
                        &engine.work,
                        &MeetRecord::DirectFwd(state),
                        cost,
                    );
                }
                if best_mu.as_ref().is_none_or(|(c, _)| cost < *c) {
                    best_mu = Some((cost, MeetRecord::DirectFwd(state)));
                }
                continue;
            }

            let mut any_track_blocked = false;
            if !state.via_only && track_ok_on_layer[state.layer as usize] {
                let l_idx = state.layer as usize;
                for delta in [[grid, 0], [0, grid], [-grid, 0], [0, -grid]] {
                    engine.work.charge(1)?;
                    let Some(x) = state.at[0].checked_add(delta[0]) else {
                        continue;
                    };
                    let Some(y) = state.at[1].checked_add(delta[1]) else {
                        continue;
                    };
                    engine.clear_last_block_generated();
                    let at_opt = if let Some(p) = attachment(l_idx, state.at, [x, y], &[])? {
                        Some(p)
                    } else if engine.legal_edge_on(net, l_idx, state.at, [x, y])? {
                        Some([x, y])
                    } else {
                        None
                    };
                    let Some(at) = at_opt else {
                        any_track_blocked = true;
                        if let Some(f) = frontier.as_deref_mut() {
                            f.push_edge_classified(
                                state.layer,
                                state.at,
                                [x, y],
                                engine.last_block_generated(),
                            );
                        }
                        continue;
                    };
                    let next = State {
                        at,
                        layer: state.layer,
                        direction: 4,
                        arrived_by_via: false,
                        is_via_goal: false,
                        via_only: false,
                        via_history: 0,
                        via_count: 0,
                        track_via_blocked: 0,
                    };
                    let step_penalty = match history {
                        Some(hist) if !hist.is_empty() => {
                            engine.work.charge(1)?;
                            hist.step_penalty(state.layer, at, engine.input.bounds.min, grid)
                        }
                        _ => 0,
                    };
                    let g = cost
                        .checked_add(1u64.checked_add(step_penalty).expect("bounded step cost"))
                        .expect("bounded search cost");
                    if best_fwd.get(&next).is_some_and(|&old| old <= g)
                        || settled_fwd.contains(&next)
                    {
                        continue;
                    }
                    let extra = 1
                        + usize::from(!best_fwd.contains_key(&next))
                        + usize::from(!parent_fwd.contains_key(&next));
                    engine.work.search_storage(
                        q_fwd.len()
                            + best_fwd.len()
                            + parent_fwd.len()
                            + q_bwd.len()
                            + best_bwd.len()
                            + parent_bwd.len()
                            + extra,
                    )?;
                    engine.work.charge(1)?;
                    best_fwd.insert(next, g);
                    parent_fwd.insert(
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
                    let fkey = BiKey {
                        at,
                        layer: state.layer,
                        arrived_by_via: false,
                    };
                    if best_fwd_meet.get(&fkey).is_none_or(|&old| g < old) {
                        best_fwd_meet.insert(fkey, g);
                        if bwd_seeded {
                            for bwd_via in [false, true] {
                                let bkey = BiKey {
                                    at,
                                    layer: state.layer,
                                    arrived_by_via: bwd_via,
                                };
                                if let Some(&g_bwd) = best_bwd.get(&bkey) {
                                    let total = g.checked_add(g_bwd).expect("bounded meet cost");
                                    if best_mu.as_ref().is_none_or(|(c, _)| total < *c) {
                                        best_mu = Some((
                                            total,
                                            MeetRecord::Joined {
                                                fwd_key: fkey,
                                                bwd_key: bkey,
                                            },
                                        ));
                                    }
                                }
                            }
                        }
                    }
                    let h = heuristic(next.at, next.layer as usize)?;
                    q_fwd.push(Reverse((
                        g.checked_add(h).expect("bounded heuristic"),
                        TieG {
                            g,
                            prefer_higher: prefer_higher_g,
                        },
                        next,
                    )));
                }
            }

            if !(only_through_vias && state.arrived_by_via) {
                via_buf.clear();
                blocked_via_spans.clear();
                let want_via_frontier =
                    l_count >= 2 && frontier.is_some() && (state.via_only || any_track_blocked);
                if l_count >= 2 && !state.via_only {
                    let cache_key = (state.at, if only_through_vias { 0 } else { state.layer });
                    if let Some((cached_vias, cached_blocked)) = via_cache.get(&cache_key) {
                        engine.work.charge(1)?;
                        if only_through_vias {
                            if let Some(&(_, pv, v_cost)) = cached_vias.first() {
                                for tl in 0..l_count {
                                    if tl as u8 != state.layer {
                                        via_buf.push((tl as u8, pv, v_cost));
                                    }
                                }
                            }
                        } else {
                            via_buf.extend_from_slice(cached_vias);
                        }
                        if want_via_frontier {
                            blocked_via_spans.extend_from_slice(cached_blocked);
                        }
                    } else {
                        via_transitions(
                            state.at,
                            state.layer as usize,
                            &[],
                            0,
                            false,
                            &mut via_buf,
                            Some(&mut blocked_via_spans),
                        )?;
                        engine.work.search_storage(via_cache.len() + 1)?;
                        via_cache.insert(cache_key, (via_buf.clone(), blocked_via_spans.clone()));
                        if !want_via_frontier {
                            blocked_via_spans.clear();
                        }
                    }
                } else {
                    via_transitions(
                        state.at,
                        state.layer as usize,
                        &[],
                        0,
                        state.via_only,
                        &mut via_buf,
                        want_via_frontier.then_some(&mut blocked_via_spans),
                    )?;
                }
                if want_via_frontier {
                    if let Some(f) = frontier.as_deref_mut() {
                        for &(s_lo, s_hi, via_gen) in &blocked_via_spans {
                            f.push_via_site_classified(s_lo, s_hi, state.at, via_gen);
                        }
                    }
                }
                for &(target_layer, pv, v_cost) in &via_buf {
                    engine.work.charge(1)?;
                    let is_goal =
                        finished(state.at, target_layer as usize, Some(PathEdge::Via(pv)))?;
                    let next = State {
                        at: state.at,
                        layer: target_layer,
                        direction: 4,
                        arrived_by_via: l_count > 1,
                        is_via_goal: is_goal,
                        via_only: false,
                        via_history: 0,
                        via_count: 0,
                        track_via_blocked: 0,
                    };
                    let v_penalty = match history {
                        Some(hist) if !hist.is_empty() => {
                            engine.work.charge(1)?;
                            hist.via_penalty(
                                pv.at,
                                pv.start_layer,
                                pv.end_layer,
                                engine.input.bounds.min,
                                grid,
                            )
                        }
                        _ => 0,
                    };
                    let g = cost
                        .checked_add(v_cost.checked_add(v_penalty).expect("bounded via cost"))
                        .expect("bounded search cost");
                    if is_goal && best_mu.as_ref().is_none_or(|(c, _)| g < *c) {
                        best_mu = Some((g, MeetRecord::DirectFwdVia { prev: state, pv }));
                    }
                    if best_fwd.get(&next).is_some_and(|&old| old <= g)
                        || settled_fwd.contains(&next)
                    {
                        continue;
                    }
                    let extra = 1
                        + usize::from(!best_fwd.contains_key(&next))
                        + usize::from(!parent_fwd.contains_key(&next));
                    engine.work.search_storage(
                        q_fwd.len()
                            + best_fwd.len()
                            + parent_fwd.len()
                            + q_bwd.len()
                            + best_bwd.len()
                            + parent_bwd.len()
                            + extra,
                    )?;
                    engine.work.charge(1)?;
                    best_fwd.insert(next, g);
                    parent_fwd.insert(next, (state, PathEdge::Via(pv)));
                    if !is_goal {
                        let fkey = BiKey {
                            at: state.at,
                            layer: target_layer,
                            arrived_by_via: l_count > 1,
                        };
                        if best_fwd_meet.get(&fkey).is_none_or(|&old| g < old) {
                            best_fwd_meet.insert(fkey, g);
                            if bwd_seeded {
                                for bwd_via in [false, true] {
                                    if only_through_vias && fkey.arrived_by_via && bwd_via {
                                        continue;
                                    }
                                    let bkey = BiKey {
                                        at: state.at,
                                        layer: target_layer,
                                        arrived_by_via: bwd_via,
                                    };
                                    if let Some(&g_bwd) = best_bwd.get(&bkey) {
                                        let total =
                                            g.checked_add(g_bwd).expect("bounded meet cost");
                                        if best_mu.as_ref().is_none_or(|(c, _)| total < *c) {
                                            best_mu = Some((
                                                total,
                                                MeetRecord::Joined {
                                                    fwd_key: fkey,
                                                    bwd_key: bkey,
                                                },
                                            ));
                                        }
                                    }
                                }
                            }
                        }
                    }
                    let h = if is_goal {
                        0
                    } else {
                        heuristic(next.at, next.layer as usize)?
                    };
                    q_fwd.push(Reverse((
                        g.checked_add(h).expect("bounded heuristic"),
                        TieG {
                            g,
                            prefer_higher: prefer_higher_g,
                        },
                        next,
                    )));
                }
            }
        } else {
            engine.work.charge(1)?;
            let Reverse((_f, tie_g, bkey)) = q_bwd.pop().unwrap();
            let cost = tie_g.g;
            if best_bwd.get(&bkey) != Some(&cost) || !settled_bwd.insert(bkey) {
                continue;
            }
            budget.check_before_expand(engine, *expanded, frontier.as_deref())?;
            *expanded = expanded.checked_add(1).expect("bounded expansion count");
            local_exp += 1;
            if local_exp == 2048 {
                if let (Some(probe), Some(f)) = (escape_probe.take(), frontier.as_deref_mut()) {
                    if !probe(expanded, f, budget)? {
                        return Err(Failure::new(
                            Status::Unroutable,
                            "target terminal is locally unreachable from routing grid",
                        ));
                    }
                }
            }

            let mut any_track_blocked = false;
            let l_idx = bkey.layer as usize;
            if track_ok_on_layer[l_idx] {
                let step_penalty = match history {
                    Some(hist) if !hist.is_empty() => {
                        engine.work.charge(1)?;
                        hist.step_penalty(bkey.layer, bkey.at, engine.input.bounds.min, grid)
                    }
                    _ => 0,
                };
                let g = cost
                    .checked_add(1u64.checked_add(step_penalty).expect("bounded step cost"))
                    .expect("bounded search cost");
                for delta in [[grid, 0], [0, grid], [-grid, 0], [0, -grid]] {
                    engine.work.charge(1)?;
                    let Some(qx) = bkey.at[0].checked_add(delta[0]) else {
                        continue;
                    };
                    let Some(qy) = bkey.at[1].checked_add(delta[1]) else {
                        continue;
                    };
                    let q = [qx, qy];
                    let next_key = BiKey {
                        at: q,
                        layer: bkey.layer,
                        arrived_by_via: false,
                    };
                    if best_bwd.get(&next_key).is_some_and(|&old| old <= g)
                        || settled_bwd.contains(&next_key)
                    {
                        continue;
                    }
                    engine.clear_last_block_generated();
                    if !engine.legal_edge_on(net, l_idx, q, bkey.at)? {
                        any_track_blocked = true;
                        if let Some(f) = frontier.as_deref_mut() {
                            f.push_edge_classified(
                                bkey.layer,
                                q,
                                bkey.at,
                                engine.last_block_generated(),
                            );
                        }
                        continue;
                    }
                    let extra = 1
                        + usize::from(!best_bwd.contains_key(&next_key))
                        + usize::from(!parent_bwd.contains_key(&next_key));
                    engine.work.search_storage(
                        q_fwd.len()
                            + best_fwd.len()
                            + parent_fwd.len()
                            + q_bwd.len()
                            + best_bwd.len()
                            + parent_bwd.len()
                            + extra,
                    )?;
                    engine.work.charge(1)?;
                    best_bwd.insert(next_key, g);
                    parent_bwd.insert(
                        next_key,
                        BwdStep {
                            next: Some(bkey),
                            edge: PathEdge::Track {
                                start: q,
                                end: bkey.at,
                                layer: bkey.layer,
                            },
                        },
                    );
                    for fwd_via in [false, true] {
                        let fkey = BiKey {
                            at: q,
                            layer: bkey.layer,
                            arrived_by_via: fwd_via,
                        };
                        if let Some(&g_fwd) = best_fwd_meet.get(&fkey) {
                            let total = g_fwd.checked_add(g).expect("bounded meet cost");
                            if best_mu.as_ref().is_none_or(|(c, _)| total < *c) {
                                best_mu = Some((
                                    total,
                                    MeetRecord::Joined {
                                        fwd_key: fkey,
                                        bwd_key: next_key,
                                    },
                                ));
                            }
                        }
                    }
                    let h = h_bwd_estimate(q, l_idx, starts, &layer_bboxes, via_cost, grid);
                    q_bwd.push(Reverse((
                        g.checked_add(h).expect("bounded bwd heuristic"),
                        TieG {
                            g,
                            prefer_higher: prefer_higher_g,
                        },
                        next_key,
                    )));
                }
            }

            if !(only_through_vias && bkey.arrived_by_via) {
                via_buf.clear();
                blocked_via_spans.clear();
                let want_via_frontier = l_count >= 2 && frontier.is_some() && any_track_blocked;
                let cache_key = (bkey.at, if only_through_vias { 0 } else { bkey.layer });
                if let Some((cached_vias, cached_blocked)) = via_cache.get(&cache_key) {
                    engine.work.charge(1)?;
                    if only_through_vias {
                        if let Some(&(_, pv, v_cost)) = cached_vias.first() {
                            for tl in 0..l_count {
                                if tl as u8 != bkey.layer {
                                    via_buf.push((tl as u8, pv, v_cost));
                                }
                            }
                        }
                    } else {
                        via_buf.extend_from_slice(cached_vias);
                    }
                    if want_via_frontier {
                        blocked_via_spans.extend_from_slice(cached_blocked);
                    }
                } else {
                    via_transitions(
                        bkey.at,
                        l_idx,
                        &[],
                        0,
                        false,
                        &mut via_buf,
                        Some(&mut blocked_via_spans),
                    )?;
                    engine.work.search_storage(via_cache.len() + 1)?;
                    via_cache.insert(cache_key, (via_buf.clone(), blocked_via_spans.clone()));
                    if !want_via_frontier {
                        blocked_via_spans.clear();
                    }
                }
                if want_via_frontier {
                    if let Some(f) = frontier.as_deref_mut() {
                        for &(s_lo, s_hi, via_gen) in &blocked_via_spans {
                            f.push_via_site_classified(s_lo, s_hi, bkey.at, via_gen);
                        }
                    }
                }
                for &(prev_l, pv, v_cost) in &via_buf {
                    engine.work.charge(1)?;
                    let next_key = BiKey {
                        at: bkey.at,
                        layer: prev_l,
                        arrived_by_via: l_count > 1,
                    };
                    let v_penalty = match history {
                        Some(hist) if !hist.is_empty() => {
                            engine.work.charge(1)?;
                            hist.via_penalty(
                                pv.at,
                                pv.start_layer,
                                pv.end_layer,
                                engine.input.bounds.min,
                                grid,
                            )
                        }
                        _ => 0,
                    };
                    let g = cost
                        .checked_add(v_cost.checked_add(v_penalty).expect("bounded via cost"))
                        .expect("bounded search cost");
                    if best_bwd.get(&next_key).is_some_and(|&old| old <= g)
                        || settled_bwd.contains(&next_key)
                    {
                        continue;
                    }
                    let extra = 1
                        + usize::from(!best_bwd.contains_key(&next_key))
                        + usize::from(!parent_bwd.contains_key(&next_key));
                    engine.work.search_storage(
                        q_fwd.len()
                            + best_fwd.len()
                            + parent_fwd.len()
                            + q_bwd.len()
                            + best_bwd.len()
                            + parent_bwd.len()
                            + extra,
                    )?;
                    engine.work.charge(1)?;
                    best_bwd.insert(next_key, g);
                    parent_bwd.insert(
                        next_key,
                        BwdStep {
                            next: Some(bkey),
                            edge: PathEdge::Via(pv),
                        },
                    );
                    for fwd_via in [false, true] {
                        if only_through_vias && fwd_via && next_key.arrived_by_via {
                            continue;
                        }
                        let fkey = BiKey {
                            at: bkey.at,
                            layer: prev_l,
                            arrived_by_via: fwd_via,
                        };
                        if let Some(&g_fwd) = best_fwd_meet.get(&fkey) {
                            let total = g_fwd.checked_add(g).expect("bounded meet cost");
                            if best_mu.as_ref().is_none_or(|(c, _)| total < *c) {
                                best_mu = Some((
                                    total,
                                    MeetRecord::Joined {
                                        fwd_key: fkey,
                                        bwd_key: next_key,
                                    },
                                ));
                            }
                        }
                    }
                    let h = h_bwd_estimate(
                        bkey.at,
                        prev_l as usize,
                        starts,
                        &layer_bboxes,
                        via_cost,
                        grid,
                    );
                    q_bwd.push(Reverse((
                        g.checked_add(h).expect("bounded bwd heuristic"),
                        TieG {
                            g,
                            prefer_higher: prefer_higher_g,
                        },
                        next_key,
                    )));
                }
            }
        }
    }

    if let Some((mu, ref rec)) = best_mu {
        return reconstruct_bi_path(&parent_fwd, &parent_bwd, &engine.work, rec, mu);
    }
    Err(Failure::new(
        Status::Unroutable,
        "search frontier exhausted",
    ))
}

#[cfg(test)]
#[path = "search_tests.rs"]
mod tests;
