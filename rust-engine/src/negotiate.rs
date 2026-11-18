//! Global multi-net PathFinder congestion negotiation and bounded rip-up/reroute.
use crate::{
    geometry,
    model::{Failure, Point, Status, Via, MAX_EXPANSIONS},
    rules::DruItemKind,
    spatial::{CopperAabb, SweptAabb, TrackGeom},
    Engine, Transaction, TransactionError,
};
use std::{
    cmp::Reverse,
    collections::{BTreeMap, BTreeSet, VecDeque},
    ops::ControlFlow,
};

const BASE_CONTEST_INC: u64 = 4;
const RIP_FOOTPRINT_INC: u64 = 4;
const FRONTIER_INC: u64 = 1;
const HISTORY_CAP: u64 = 24;
const MAX_HISTORY_CELLS: usize = 65_536;
const MAX_FRONTIER_PROBES: usize = 4096;
const MAX_STAMP_STEPS_PER_ITEM: i64 = 512;
const MAX_RIPS_PER_UNIT: u32 = 8;
const MAX_CO_VICTIMS: usize = 4;
const MAX_RIPS_PER_ROUND: usize = 32;
const MAX_GLOBAL_PASSES: usize = 6;
const TRIAL_EXPANSION_CAP: u64 = 40_000;

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum RouteUnitId {
    DiffPair(usize),
    SingleNet(u32),
}

#[derive(Clone, Debug)]
pub struct UnitSpec {
    pub id: RouteUnitId,
    pub nets: Vec<u32>,
    pub priority_tier: u8,
    pub rippable_by_single: bool,
}

#[derive(Debug)]
pub enum UnitRouteError {
    Blocked(BlockedFrontier),
    Fatal(Failure),
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct NegotiationStats {
    pub initial_routed: usize,
    pub initial_failed: usize,
    pub rounds_attempted: u64,
    pub rounds_committed: u64,
    pub rounds_rolled_back: u64,
    pub global_passes: u64,
    pub units_ripped: u64,
    pub victim_recoveries: u64,
    pub history_cells_peak: usize,
    pub history_evictions: u64,
    pub frontier_probes: u64,
    pub expanded_states: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum BlockedProbe {
    Edge {
        layer: u8,
        a: Point,
        b: Point,
    },
    ViaSite {
        start_layer: u8,
        end_layer: u8,
        at: Point,
    },
}

#[derive(Clone, Debug, Default)]
pub struct BlockedFrontier {
    probes: Vec<BlockedProbe>,
    seen: BTreeSet<BlockedProbe>,
    total_seen: u64,
}

impl BlockedFrontier {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn is_empty(&self) -> bool {
        self.probes.is_empty()
    }

    pub fn len(&self) -> usize {
        self.probes.len()
    }

    pub fn total_seen(&self) -> u64 {
        self.total_seen
    }

    pub fn probes(&self) -> &[BlockedProbe] {
        &self.probes
    }

    pub fn push_edge(&mut self, layer: u8, a: Point, b: Point) {
        self.total_seen = self.total_seen.saturating_add(1);
        if self.probes.len() >= MAX_FRONTIER_PROBES {
            return;
        }
        let probe = BlockedProbe::Edge { layer, a, b };
        if self.seen.insert(probe) {
            self.probes.push(probe);
        }
    }

    pub fn push_via_site(&mut self, start_layer: u8, end_layer: u8, at: Point) {
        self.total_seen = self.total_seen.saturating_add(1);
        if self.probes.len() >= MAX_FRONTIER_PROBES {
            return;
        }
        let probe = BlockedProbe::ViaSite {
            start_layer,
            end_layer,
            at,
        };
        if self.seen.insert(probe) {
            self.probes.push(probe);
        }
    }
}

fn cell_coord(pt: Point, bounds_min: Point, grid_nm: i64) -> (i64, i64) {
    let g = grid_nm.max(1);
    (
        (pt[0] - bounds_min[0]).div_euclid(g),
        (pt[1] - bounds_min[1]).div_euclid(g),
    )
}

#[derive(Clone, Debug, Default)]
pub struct CongestionHistory {
    cells: BTreeMap<(u8, i64, i64), u64>,
    peak_cells: usize,
    evictions: u64,
}

impl CongestionHistory {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn is_empty(&self) -> bool {
        self.cells.is_empty()
    }

    pub fn len(&self) -> usize {
        self.cells.len()
    }

    pub fn peak_len(&self) -> usize {
        self.peak_cells
    }

    pub fn evictions(&self) -> u64 {
        self.evictions
    }

    pub fn step_penalty(&self, layer: u8, pt: Point, bounds_min: Point, grid_nm: i64) -> u64 {
        if self.cells.is_empty() {
            return 0;
        }
        let (cx, cy) = cell_coord(pt, bounds_min, grid_nm);
        self.cells.get(&(layer, cx, cy)).copied().unwrap_or(0)
    }

    pub fn via_penalty(
        &self,
        at: Point,
        start_layer: u8,
        end_layer: u8,
        bounds_min: Point,
        grid_nm: i64,
    ) -> u64 {
        if self.cells.is_empty() {
            return 0;
        }
        let (cx, cy) = cell_coord(at, bounds_min, grid_nm);
        let lo = start_layer.min(end_layer);
        let hi = start_layer.max(end_layer);
        let mut total = 0u64;
        for layer in lo..=hi {
            total = total.saturating_add(self.cells.get(&(layer, cx, cy)).copied().unwrap_or(0));
        }
        total
    }

    fn add_weight(
        &mut self,
        key: (u8, i64, i64),
        delta: u64,
        engine: &Engine,
    ) -> Result<(), Failure> {
        engine.charge_work(1)?;
        if let Some(cur) = self.cells.get_mut(&key) {
            *cur = cur.saturating_add(delta).min(HISTORY_CAP);
            return Ok(());
        }
        if self.cells.len() >= MAX_HISTORY_CELLS {
            engine.charge_work(1)?;
            if let Some((&evict_key, _)) =
                self.cells.iter().min_by_key(|(&k, &w)| (w, k.0, k.1, k.2))
            {
                self.cells.remove(&evict_key);
                self.evictions = self.evictions.saturating_add(1);
            }
        }
        self.cells.insert(key, delta.min(HISTORY_CAP));
        self.peak_cells = self.peak_cells.max(self.cells.len());
        Ok(())
    }
}

impl CongestionHistory {
    fn stamp_frontier(
        &mut self,
        frontier: &BlockedFrontier,
        engine: &Engine,
    ) -> Result<(), Failure> {
        let bounds_min = engine.input().bounds.min;
        let grid_nm = engine.input().rules.grid_nm;
        let mut stamped: BTreeSet<(u8, i64, i64)> = BTreeSet::new();
        for &probe in frontier.probes() {
            match probe {
                BlockedProbe::Edge { layer, a, b } => {
                    let c_a = cell_coord(a, bounds_min, grid_nm);
                    let c_b = cell_coord(b, bounds_min, grid_nm);
                    if stamped.insert((layer, c_b.0, c_b.1)) {
                        self.add_weight((layer, c_b.0, c_b.1), FRONTIER_INC, engine)?;
                    }
                    if c_a != c_b && stamped.insert((layer, c_a.0, c_a.1)) {
                        self.add_weight((layer, c_a.0, c_a.1), FRONTIER_INC, engine)?;
                    }
                }
                BlockedProbe::ViaSite {
                    start_layer,
                    end_layer,
                    at,
                } => {
                    let (cx, cy) = cell_coord(at, bounds_min, grid_nm);
                    for layer in start_layer.min(end_layer)..=start_layer.max(end_layer) {
                        if stamped.insert((layer, cx, cy)) {
                            self.add_weight((layer, cx, cy), FRONTIER_INC, engine)?;
                        }
                    }
                }
            }
        }
        Ok(())
    }

    fn stamp_cells(
        &mut self,
        cells: &BTreeSet<(u8, i64, i64)>,
        delta: u64,
        engine: &Engine,
    ) -> Result<(), Failure> {
        for &cell in cells {
            self.add_weight(cell, delta, engine)?;
        }
        Ok(())
    }

    fn stamp_unit_footprint(&mut self, unit: &UnitSpec, engine: &Engine) -> Result<(), Failure> {
        let bounds_min = engine.input().bounds.min;
        let grid_nm = engine.input().rules.grid_nm.max(1);
        let mut cells: BTreeSet<(u8, i64, i64)> = BTreeSet::new();
        for &net in &unit.nets {
            for t in engine.copper().net_generated_tracks(net) {
                engine.charge_work(1)?;
                let Some(l_idx) = engine.layer_index(&t.layer) else {
                    continue;
                };
                let layer = l_idx as u8;
                let dx = (t.end[0] - t.start[0]).abs();
                let dy = (t.end[1] - t.start[1]).abs();
                let steps = (dx.max(dy) / grid_nm).max(1);
                let step_stride = if steps > MAX_STAMP_STEPS_PER_ITEM {
                    steps / MAX_STAMP_STEPS_PER_ITEM
                } else {
                    1
                };
                let sample_count = (steps / step_stride).min(MAX_STAMP_STEPS_PER_ITEM);
                for s in 0..=sample_count {
                    let pt = [
                        t.start[0] + (t.end[0] - t.start[0]) * s / sample_count.max(1),
                        t.start[1] + (t.end[1] - t.start[1]) * s / sample_count.max(1),
                    ];
                    let (cx, cy) = cell_coord(pt, bounds_min, grid_nm);
                    cells.insert((layer, cx, cy));
                }
            }
            for v in engine.copper().net_generated_vias(net) {
                engine.charge_work(1)?;
                let s_idx = engine.layer_index(&v.start_layer).unwrap_or(0);
                let e_idx = engine.layer_index(&v.end_layer).unwrap_or(s_idx);
                let (cx, cy) = cell_coord(v.at, bounds_min, grid_nm);
                for l_idx in s_idx.min(e_idx)..=s_idx.max(e_idx) {
                    cells.insert((l_idx as u8, cx, cy));
                }
            }
        }
        self.stamp_cells(&cells, RIP_FOOTPRINT_INC, engine)
    }
}

/// Abstraction over single-ended nets and differential pairs for unified negotiation.
///
/// Note: `units` omits a separate `&WorkBudget` parameter because `WorkBudget` is
/// crate-private (`mod budget;`); implementations inside the crate charge `engine.work`
/// directly.
pub trait RouteUnitAdapter {
    fn units(&self, engine: &Engine) -> Result<Vec<UnitSpec>, Failure>;
    fn unit_for_net(&self, net: u32) -> Option<RouteUnitId>;
    fn route_unit(
        &mut self,
        engine: &mut Engine,
        unit: RouteUnitId,
        history: &CongestionHistory,
        max_expansions: u64,
        expanded: &mut u64,
    ) -> Result<(), UnitRouteError>;
    fn rip_unit(&mut self, engine: &mut Engine, unit: RouteUnitId) -> Result<(), Failure>;
    fn is_unit_complete(&self, engine: &Engine, unit: RouteUnitId) -> Result<bool, Failure>;
}

#[derive(Clone, Debug, Default)]
pub struct SingleEndedAdapter {
    pub pinned_nets: BTreeSet<u32>,
    pub active_nets: Option<BTreeSet<u32>>,
}

impl SingleEndedAdapter {
    pub fn new(pinned_nets: BTreeSet<u32>, active_nets: Option<BTreeSet<u32>>) -> Self {
        Self {
            pinned_nets,
            active_nets,
        }
    }

    pub fn all() -> Self {
        Self::default()
    }
}

impl RouteUnitAdapter for SingleEndedAdapter {
    fn units(&self, engine: &Engine) -> Result<Vec<UnitSpec>, Failure> {
        engine.charge_work(engine.input().nets.len() as u64)?;
        let mut nets: Vec<u32> = engine
            .input()
            .nets
            .iter()
            .filter(|n| {
                n.id != 0
                    && !self.pinned_nets.contains(&n.id)
                    && self.active_nets.as_ref().is_none_or(|s| s.contains(&n.id))
            })
            .map(|n| n.id)
            .collect();
        engine.charge_work(nets.len() as u64)?;
        nets.sort_unstable();
        Ok(nets
            .into_iter()
            .map(|net| UnitSpec {
                id: RouteUnitId::SingleNet(net),
                nets: vec![net],
                priority_tier: 1,
                rippable_by_single: true,
            })
            .collect())
    }

    fn unit_for_net(&self, net: u32) -> Option<RouteUnitId> {
        if net == 0 || self.pinned_nets.contains(&net) {
            return None;
        }
        if let Some(active) = &self.active_nets {
            if !active.contains(&net) {
                return None;
            }
        }
        Some(RouteUnitId::SingleNet(net))
    }

    fn route_unit(
        &mut self,
        engine: &mut Engine,
        unit: RouteUnitId,
        history: &CongestionHistory,
        max_expansions: u64,
        expanded: &mut u64,
    ) -> Result<(), UnitRouteError> {
        match unit {
            RouteUnitId::SingleNet(net) => {
                engine.route_net_negotiated(net, max_expansions, expanded, history)
            }
            RouteUnitId::DiffPair(_) => Err(UnitRouteError::Fatal(Failure::new(
                Status::Invalid,
                "SingleEndedAdapter cannot route differential pair units",
            ))),
        }
    }

    fn rip_unit(&mut self, engine: &mut Engine, unit: RouteUnitId) -> Result<(), Failure> {
        match unit {
            RouteUnitId::SingleNet(net) => engine.remove_generated_net(net),
            RouteUnitId::DiffPair(_) => Err(Failure::new(
                Status::Invalid,
                "SingleEndedAdapter cannot rip differential pair units",
            )),
        }
    }

    fn is_unit_complete(&self, engine: &Engine, unit: RouteUnitId) -> Result<bool, Failure> {
        match unit {
            RouteUnitId::SingleNet(net) => Ok(engine.components(net)?.len() <= 1),
            RouteUnitId::DiffPair(_) => Err(Failure::new(
                Status::Invalid,
                "SingleEndedAdapter cannot check differential pair units",
            )),
        }
    }
}

#[derive(Clone, Debug)]
struct BlockerScore {
    unit: RouteUnitId,
    blocked_probes: u64,
    gen_copper_count: usize,
    contested_cells: BTreeSet<(u8, i64, i64)>,
}

fn unit_is_rippable_by(victim: &UnitSpec, blocked: &UnitSpec) -> bool {
    if victim.id == blocked.id {
        return false;
    }
    if blocked.priority_tier > victim.priority_tier && !victim.rippable_by_single {
        return false;
    }
    true
}

fn union_swept(slot: &mut Option<SweptAabb>, box_s: SweptAabb) {
    *slot = Some(match *slot {
        None => box_s,
        Some(prev) => SweptAabb {
            min: [prev.min[0].min(box_s.min[0]), prev.min[1].min(box_s.min[1])],
            max: [prev.max[0].max(box_s.max[0]), prev.max[1].max(box_s.max[1])],
        },
    });
}

fn analyze_blockers(
    engine: &Engine,
    blocked_unit: &UnitSpec,
    frontier: &BlockedFrontier,
    adapter: &dyn RouteUnitAdapter,
    spec_by_id: &BTreeMap<RouteUnitId, UnitSpec>,
    rip_counts: &BTreeMap<RouteUnitId, u32>,
) -> Result<Vec<BlockerScore>, Failure> {
    if frontier.is_empty() {
        return Ok(Vec::new());
    }
    let primary_net = blocked_unit.nets.first().copied().unwrap_or(0);
    let net_filter = (blocked_unit.nets.len() == 1).then_some(primary_net);
    let bounds_min = engine.input().bounds.min;
    let grid_nm = engine.input().rules.grid_nm;
    let l_count = engine.layer_count();
    let stackup = engine.all_layers_slice();
    let rules = engine.effective_rules();
    let max_via_query_clr = rules
        .max_pair_clearance_nm
        .max(rules.max_hole_clearance_nm)
        .max(rules.max_hole_to_hole_nm);

    let mut layer_unions: Vec<Option<SweptAabb>> = vec![None; l_count];
    let mut via_union: Option<SweptAabb> = None;
    for &probe in frontier.probes() {
        match probe {
            BlockedProbe::Edge { layer, a, b } => {
                let l_idx = layer as usize;
                if l_idx >= l_count {
                    continue;
                }
                let width_nm = rules
                    .track_width_for(primary_net, &stackup[l_idx])
                    .unwrap_or(engine.input().rules.width_nm);
                union_swept(
                    &mut layer_unions[l_idx],
                    SweptAabb::swept(a, b, width_nm, rules.max_pair_clearance_nm),
                );
                union_swept(
                    &mut via_union,
                    SweptAabb::swept(a, b, width_nm, max_via_query_clr),
                );
            }
            BlockedProbe::ViaSite {
                start_layer,
                end_layer,
                at,
            } => {
                if l_count < 2 {
                    continue;
                }
                let s_lo = (start_layer.min(end_layer) as usize).min(l_count - 1);
                let s_hi = (start_layer.max(end_layer) as usize).min(l_count - 1);
                let (diam, _) = rules
                    .via_dims_for(primary_net, false, &stackup[s_lo..=s_hi])
                    .unwrap_or((
                        rules.max_via_diameter_nm.max(engine.input().rules.width_nm),
                        0,
                    ));
                let swept = SweptAabb::swept(at, at, diam, max_via_query_clr);
                for slot in &mut layer_unions[s_lo..=s_hi] {
                    union_swept(slot, swept);
                }
                union_swept(&mut via_union, swept);
            }
        }
    }

    let can_rip_net = |other_net: u32| -> bool {
        if blocked_unit.nets.contains(&other_net) {
            return false;
        }
        let Some(victim_id) = adapter.unit_for_net(other_net) else {
            return false;
        };
        let Some(victim_spec) = spec_by_id.get(&victim_id) else {
            return false;
        };
        unit_is_rippable_by(victim_spec, blocked_unit)
    };

    let mut layer_tracks: Vec<Vec<(CopperAabb, u32, TrackGeom)>> = vec![Vec::new(); l_count];
    for (l_idx, union_opt) in layer_unions.into_iter().enumerate() {
        let Some(u_swept) = union_opt else {
            continue;
        };
        let _ = engine.copper().visit_generated_layer_candidates(
            l_idx,
            u_swept,
            net_filter,
            engine.work(),
            |other_net, t| {
                if !can_rip_net(other_net) {
                    return Ok(ControlFlow::Continue(()));
                }
                if let Some(t_box) = SweptAabb::swept(t.start, t.end, t.width_nm, 0).clamp_i64() {
                    layer_tracks[l_idx].push((t_box, other_net, t));
                }
                Ok(ControlFlow::Continue(()))
            },
        )?;
    }
    let mut gen_vias: Vec<(CopperAabb, Via, u32)> = Vec::new();
    if let Some(u_via) = via_union {
        let _ = engine.copper().visit_generated_via_candidates(
            u_via,
            net_filter,
            engine.work(),
            |via, v_mask| {
                if !can_rip_net(via.net) {
                    return Ok(ControlFlow::Continue(()));
                }
                let r_diam = via.diameter_nm.max(via.drill_nm);
                if let Some(v_box) = SweptAabb::swept(via.at, via.at, r_diam, 0).clamp_i64() {
                    gen_vias.push((v_box, via.clone(), v_mask));
                }
                Ok(ControlFlow::Continue(()))
            },
        )?;
    }

    if layer_tracks.iter().all(|v| v.is_empty()) && gen_vias.is_empty() {
        return Ok(Vec::new());
    }

    let mut scores: BTreeMap<RouteUnitId, BlockerScore> = BTreeMap::new();
    for &probe in frontier.probes() {
        engine.charge_work(1)?;
        let mut probe_victims: BTreeSet<RouteUnitId> = BTreeSet::new();
        let mut record_hit = |other_net: u32, cell: (u8, i64, i64)| {
            let Some(victim_id) = adapter.unit_for_net(other_net) else {
                return;
            };
            let Some(victim_spec) = spec_by_id.get(&victim_id) else {
                return;
            };
            let entry = scores.entry(victim_id).or_insert_with(|| {
                let gen_count: usize = victim_spec
                    .nets
                    .iter()
                    .map(|&n| {
                        engine.copper().net_generated_tracks(n).len()
                            + engine.copper().net_generated_vias(n).len()
                    })
                    .sum();
                BlockerScore {
                    unit: victim_id,
                    blocked_probes: 0,
                    gen_copper_count: gen_count,
                    contested_cells: BTreeSet::new(),
                }
            });
            if probe_victims.insert(victim_id) {
                entry.blocked_probes = entry.blocked_probes.saturating_add(1);
            }
            entry.contested_cells.insert(cell);
        };
        match probe {
            BlockedProbe::Edge { layer, a, b } => {
                let l_idx = layer as usize;
                if l_idx >= l_count {
                    continue;
                }
                let layer_name = &stackup[l_idx];
                let width_nm = rules
                    .track_width_for(primary_net, layer_name)
                    .unwrap_or(engine.input().rules.width_nm);
                let (cx, cy) = cell_coord(b, bounds_min, grid_nm);
                if let Some(p_track_box) =
                    SweptAabb::swept(a, b, width_nm, rules.max_pair_clearance_nm).clamp_i64()
                {
                    for &(t_box, other_net, t) in &layer_tracks[l_idx] {
                        if !t_box.overlaps(p_track_box) {
                            continue;
                        }
                        engine.charge_work(1)?;
                        let clr = rules.pair_clearance(
                            primary_net,
                            DruItemKind::Track,
                            other_net,
                            DruItemKind::Track,
                            layer_name,
                            None,
                        );
                        if geometry::segments_near(
                            a,
                            b,
                            t.start,
                            t.end,
                            i128::from(width_nm) + i128::from(t.width_nm) + 2 * i128::from(clr),
                            false,
                        ) {
                            record_hit(other_net, (layer, cx, cy));
                        }
                    }
                }
                if let Some(p_via_box) =
                    SweptAabb::swept(a, b, width_nm, max_via_query_clr).clamp_i64()
                {
                    let layer_bit = 1u32 << l_idx;
                    let layer_slice = std::slice::from_ref(layer_name);
                    for (v_box, via, v_mask) in &gen_vias {
                        if (*v_mask & layer_bit) == 0 || !v_box.overlaps(p_via_box) {
                            continue;
                        }
                        engine.charge_work(1)?;
                        let copper_clr = rules.pair_clearance(
                            primary_net,
                            DruItemKind::Track,
                            via.net,
                            DruItemKind::Via,
                            layer_name,
                            None,
                        );
                        let s_idx = engine.layer_index(&via.start_layer).unwrap_or(0);
                        let e_idx = engine.layer_index(&via.end_layer).unwrap_or(s_idx);
                        let hole_clr = rules.pair_hole_clearance(
                            primary_net,
                            DruItemKind::Track,
                            layer_slice,
                            via.net,
                            DruItemKind::Via,
                            &stackup[s_idx.min(e_idx)..=s_idx.max(e_idx)],
                        );
                        let hit = geometry::point_near_segment(
                            via.at,
                            a,
                            b,
                            i128::from(width_nm)
                                + i128::from(via.diameter_nm)
                                + 2 * i128::from(copper_clr),
                            false,
                        ) || geometry::point_near_segment(
                            via.at,
                            a,
                            b,
                            i128::from(width_nm)
                                + i128::from(via.drill_nm)
                                + 2 * i128::from(hole_clr),
                            false,
                        );
                        if hit {
                            record_hit(via.net, (layer, cx, cy));
                        }
                    }
                }
            }
            BlockedProbe::ViaSite {
                start_layer,
                end_layer,
                at,
            } => {
                if l_count < 2 {
                    continue;
                }
                let s_lo = (start_layer.min(end_layer) as usize).min(l_count - 1);
                let s_hi = (start_layer.max(end_layer) as usize).min(l_count - 1);
                let span_layers = &stackup[s_lo..=s_hi];
                let (diam, drill) = rules
                    .via_dims_for(primary_net, false, span_layers)
                    .unwrap_or((
                        rules.max_via_diameter_nm.max(engine.input().rules.width_nm),
                        0,
                    ));
                let Some(p_via_box) = SweptAabb::swept(at, at, diam, max_via_query_clr).clamp_i64()
                else {
                    continue;
                };
                let (cx, cy) = cell_coord(at, bounds_min, grid_nm);
                for l_idx in s_lo..=s_hi {
                    let layer_name = &stackup[l_idx];
                    let layer_slice = std::slice::from_ref(layer_name);
                    for &(t_box, other_net, t) in &layer_tracks[l_idx] {
                        if !t_box.overlaps(p_via_box) {
                            continue;
                        }
                        engine.charge_work(1)?;
                        let clr = rules.pair_clearance(
                            primary_net,
                            DruItemKind::Via,
                            other_net,
                            DruItemKind::Track,
                            layer_name,
                            None,
                        );
                        let h_clr = rules.pair_hole_clearance(
                            primary_net,
                            DruItemKind::Via,
                            span_layers,
                            other_net,
                            DruItemKind::Track,
                            layer_slice,
                        );
                        let hit = geometry::point_near_segment(
                            at,
                            t.start,
                            t.end,
                            i128::from(diam) + i128::from(t.width_nm) + 2 * i128::from(clr),
                            false,
                        ) || (h_clr > 0
                            && geometry::point_near_segment(
                                at,
                                t.start,
                                t.end,
                                i128::from(drill) + i128::from(t.width_nm) + 2 * i128::from(h_clr),
                                false,
                            ));
                        if hit {
                            record_hit(other_net, (l_idx as u8, cx, cy));
                        }
                    }
                }
                let span_mask_q = crate::components::span_mask(s_lo, s_hi);
                for (v_box, via, v_mask) in &gen_vias {
                    if (span_mask_q & *v_mask) == 0 || !v_box.overlaps(p_via_box) {
                        continue;
                    }
                    engine.charge_work(1)?;
                    let vs = engine.layer_index(&via.start_layer).unwrap_or(0);
                    let ve = engine.layer_index(&via.end_layer).unwrap_or(vs);
                    let ev_layers = &stackup[vs.min(ve)..=vs.max(ve)];
                    let h2h = rules.pair_hole_to_hole(
                        primary_net,
                        DruItemKind::Via,
                        span_layers,
                        via.net,
                        DruItemKind::Via,
                        ev_layers,
                    );
                    let hole_clr = rules.pair_hole_clearance(
                        primary_net,
                        DruItemKind::Via,
                        span_layers,
                        via.net,
                        DruItemKind::Via,
                        ev_layers,
                    );
                    let mut hit = geometry::point_near_segment(
                        at,
                        via.at,
                        via.at,
                        i128::from(drill) + i128::from(via.drill_nm) + 2 * i128::from(h2h),
                        false,
                    ) || geometry::point_near_segment(
                        at,
                        via.at,
                        via.at,
                        i128::from(diam) + i128::from(via.drill_nm) + 2 * i128::from(hole_clr),
                        false,
                    );
                    if !hit {
                        for l_idx in s_lo..=s_hi {
                            if (*v_mask & (1u32 << l_idx)) != 0 {
                                let clr = rules.pair_clearance(
                                    primary_net,
                                    DruItemKind::Via,
                                    via.net,
                                    DruItemKind::Via,
                                    &stackup[l_idx],
                                    None,
                                );
                                if geometry::point_near_segment(
                                    at,
                                    via.at,
                                    via.at,
                                    i128::from(diam)
                                        + i128::from(via.diameter_nm)
                                        + 2 * i128::from(clr),
                                    false,
                                ) {
                                    hit = true;
                                    break;
                                }
                            }
                        }
                    }
                    if hit {
                        record_hit(via.net, (s_lo as u8, cx, cy));
                    }
                }
            }
        }
    }

    let mut ranked: Vec<BlockerScore> = scores.into_values().collect();
    ranked.sort_by_key(|b| {
        (
            rip_counts.get(&b.unit).copied().unwrap_or(0),
            Reverse(b.blocked_probes),
            b.gen_copper_count,
            b.unit,
        )
    });
    Ok(ranked)
}

enum TryUnitErr {
    Blocked {
        frontier: BlockedFrontier,
        blockers: Option<Vec<BlockerScore>>,
    },
    Fatal(Failure),
}

#[allow(clippy::too_many_arguments)]
fn try_unit_with_cap(
    engine: &mut Engine,
    adapter: &mut dyn RouteUnitAdapter,
    spec_by_id: &BTreeMap<RouteUnitId, UnitSpec>,
    unit: &UnitSpec,
    history: &CongestionHistory,
    batch_limit: u64,
    expanded: &mut u64,
    allow_trial_cap: bool,
    rip_counts: &BTreeMap<RouteUnitId, u32>,
) -> Result<(), TryUnitErr> {
    let remaining = batch_limit.saturating_sub(*expanded);
    if remaining == 0 {
        return Err(TryUnitErr::Fatal(Failure::new(
            Status::BudgetExhausted,
            "total expansion budget exhausted",
        )));
    }
    if allow_trial_cap && remaining > TRIAL_EXPANSION_CAP {
        let trial_limit = (*expanded).saturating_add(TRIAL_EXPANSION_CAP);
        match adapter.route_unit(engine, unit.id, history, trial_limit, expanded) {
            Ok(()) => return Ok(()),
            Err(UnitRouteError::Blocked(frontier)) => {
                if *expanded < trial_limit {
                    return Err(TryUnitErr::Blocked {
                        frontier,
                        blockers: None,
                    });
                }
                let blockers =
                    analyze_blockers(engine, unit, &frontier, adapter, spec_by_id, rip_counts)
                        .map_err(TryUnitErr::Fatal)?;
                if !blockers.is_empty() {
                    return Err(TryUnitErr::Blocked {
                        frontier,
                        blockers: Some(blockers),
                    });
                }
                if let Err(e) = adapter.rip_unit(engine, unit.id) {
                    return Err(TryUnitErr::Fatal(e));
                }
                return adapter
                    .route_unit(engine, unit.id, history, batch_limit, expanded)
                    .map_err(|e| match e {
                        UnitRouteError::Blocked(f) => TryUnitErr::Blocked {
                            frontier: f,
                            blockers: None,
                        },
                        UnitRouteError::Fatal(f) => TryUnitErr::Fatal(f),
                    });
            }
            Err(UnitRouteError::Fatal(e)) => {
                if e.status == Status::BudgetExhausted && *expanded < batch_limit {
                    if let Err(re) = adapter.rip_unit(engine, unit.id) {
                        return Err(TryUnitErr::Fatal(re));
                    }
                    return adapter
                        .route_unit(engine, unit.id, history, batch_limit, expanded)
                        .map_err(|e| match e {
                            UnitRouteError::Blocked(f) => TryUnitErr::Blocked {
                                frontier: f,
                                blockers: None,
                            },
                            UnitRouteError::Fatal(f) => TryUnitErr::Fatal(f),
                        });
                }
                return Err(TryUnitErr::Fatal(e));
            }
        }
    }
    adapter
        .route_unit(engine, unit.id, history, batch_limit, expanded)
        .map_err(|e| match e {
            UnitRouteError::Blocked(f) => TryUnitErr::Blocked {
                frontier: f,
                blockers: None,
            },
            UnitRouteError::Fatal(f) => TryUnitErr::Fatal(f),
        })
}

enum RoundOutcome {
    Unroutable,
    Fatal(Failure),
}

enum PassOutcome {
    Unroutable(RouteUnitId),
    Fatal(Failure),
}

fn first_net_of(unit: RouteUnitId, spec_by_id: &BTreeMap<RouteUnitId, UnitSpec>) -> u32 {
    match unit {
        RouteUnitId::SingleNet(net) => net,
        RouteUnitId::DiffPair(_) => spec_by_id
            .get(&unit)
            .and_then(|s| s.nets.first().copied())
            .unwrap_or(0),
    }
}

fn unwrap_round_tx_err(err: TransactionError<RoundOutcome>) -> Failure {
    match err {
        TransactionError::Limit(e) | TransactionError::Operation(RoundOutcome::Fatal(e)) => e,
        TransactionError::Operation(RoundOutcome::Unroutable) => Failure::new(
            Status::Unroutable,
            "rip-up round failed to resolve blocking conflict",
        ),
    }
}

#[allow(clippy::too_many_arguments)]
fn try_local_ripup_round(
    tx: &mut Transaction<'_>,
    adapter: &mut dyn RouteUnitAdapter,
    spec_by_id: &BTreeMap<RouteUnitId, UnitSpec>,
    unit: &UnitSpec,
    initial_frontier: &BlockedFrontier,
    precomputed_blockers: Option<Vec<BlockerScore>>,
    history: &mut CongestionHistory,
    rip_counts: &mut BTreeMap<RouteUnitId, u32>,
    stats: &mut NegotiationStats,
    batch_limit: u64,
    expanded: &mut u64,
) -> Result<(bool, Option<BlockerScore>), Failure> {
    let blockers = match precomputed_blockers {
        Some(b) => b,
        None => analyze_blockers(
            tx.engine,
            unit,
            initial_frontier,
            adapter,
            spec_by_id,
            rip_counts,
        )?,
    };
    if blockers.is_empty() {
        return Ok((false, None));
    }
    let top_blocker = blockers[0].clone();
    stats.rounds_attempted = stats.rounds_attempted.saturating_add(1);
    history.stamp_frontier(initial_frontier, tx.engine)?;
    history.stamp_cells(&blockers[0].contested_cells, BASE_CONTEST_INC, tx.engine)?;

    let mut local_rip_deltas: BTreeMap<RouteUnitId, u32> = BTreeMap::new();
    let mut local_units_ripped = 0u64;
    let mut local_victim_recoveries = 0u64;

    let res = tx.transaction(|round_tx| -> Result<(), RoundOutcome> {
        let mut reroute_queue: VecDeque<RouteUnitId> = VecDeque::new();
        let mut ripped_in_round: BTreeSet<RouteUnitId> = BTreeSet::new();
        ripped_in_round.insert(unit.id);
        let mut rips_this_round = 0usize;

        let pick_victim = |candidates: &[BlockerScore],
                           ripped_in_round: &BTreeSet<RouteUnitId>,
                           rip_counts: &BTreeMap<RouteUnitId, u32>,
                           local_rip_deltas: &BTreeMap<RouteUnitId, u32>|
         -> Option<usize> {
            candidates.iter().position(|b| {
                !ripped_in_round.contains(&b.unit)
                    && rip_counts.get(&b.unit).copied().unwrap_or(0)
                        + local_rip_deltas.get(&b.unit).copied().unwrap_or(0)
                        < MAX_RIPS_PER_UNIT
            })
        };

        let Some(first_idx) =
            pick_victim(&blockers, &ripped_in_round, rip_counts, &local_rip_deltas)
        else {
            return Err(RoundOutcome::Unroutable);
        };
        let first_victim = blockers[first_idx].unit;
        if let Some(v_spec) = spec_by_id.get(&first_victim) {
            history
                .stamp_unit_footprint(v_spec, round_tx.engine)
                .map_err(RoundOutcome::Fatal)?;
        }
        adapter
            .rip_unit(round_tx.engine, first_victim)
            .map_err(RoundOutcome::Fatal)?;
        *local_rip_deltas.entry(first_victim).or_insert(0) += 1;
        rips_this_round += 1;
        local_units_ripped += 1;
        ripped_in_round.insert(first_victim);
        reroute_queue.push_back(first_victim);

        let mut co_victims = 1usize;
        loop {
            match try_unit_with_cap(
                round_tx.engine,
                adapter,
                spec_by_id,
                unit,
                history,
                batch_limit,
                expanded,
                true,
                rip_counts,
            ) {
                Ok(()) => break,
                Err(TryUnitErr::Fatal(e)) => return Err(RoundOutcome::Fatal(e)),
                Err(TryUnitErr::Blocked {
                    frontier: next_frontier,
                    blockers: pre_b,
                }) => {
                    stats.frontier_probes = stats
                        .frontier_probes
                        .saturating_add(next_frontier.len() as u64);
                    if co_victims >= MAX_CO_VICTIMS || rips_this_round >= MAX_RIPS_PER_ROUND {
                        return Err(RoundOutcome::Unroutable);
                    }
                    let next_blockers = match pre_b {
                        Some(b) => b,
                        None => analyze_blockers(
                            round_tx.engine,
                            unit,
                            &next_frontier,
                            adapter,
                            spec_by_id,
                            rip_counts,
                        )
                        .map_err(RoundOutcome::Fatal)?,
                    };
                    let Some(idx) = pick_victim(
                        &next_blockers,
                        &ripped_in_round,
                        rip_counts,
                        &local_rip_deltas,
                    ) else {
                        return Err(RoundOutcome::Unroutable);
                    };
                    let co_v = next_blockers[idx].unit;
                    history
                        .stamp_frontier(&next_frontier, round_tx.engine)
                        .map_err(RoundOutcome::Fatal)?;
                    history
                        .stamp_cells(
                            &next_blockers[idx].contested_cells,
                            BASE_CONTEST_INC,
                            round_tx.engine,
                        )
                        .map_err(RoundOutcome::Fatal)?;
                    if let Some(v_spec) = spec_by_id.get(&co_v) {
                        history
                            .stamp_unit_footprint(v_spec, round_tx.engine)
                            .map_err(RoundOutcome::Fatal)?;
                    }
                    adapter
                        .rip_unit(round_tx.engine, co_v)
                        .map_err(RoundOutcome::Fatal)?;
                    *local_rip_deltas.entry(co_v).or_insert(0) += 1;
                    rips_this_round += 1;
                    co_victims += 1;
                    local_units_ripped += 1;
                    ripped_in_round.insert(co_v);
                    reroute_queue.push_back(co_v);
                }
            }
        }

        if !adapter
            .is_unit_complete(round_tx.engine, unit.id)
            .map_err(RoundOutcome::Fatal)?
        {
            return Err(RoundOutcome::Unroutable);
        }

        while let Some(victim_id) = reroute_queue.pop_front() {
            round_tx
                .engine
                .charge_work(1)
                .map_err(RoundOutcome::Fatal)?;
            let v_spec = spec_by_id
                .get(&victim_id)
                .expect("victim unit exists in unit spec map");
            match try_unit_with_cap(
                round_tx.engine,
                adapter,
                spec_by_id,
                v_spec,
                history,
                batch_limit,
                expanded,
                true,
                rip_counts,
            ) {
                Ok(()) => {
                    if !adapter
                        .is_unit_complete(round_tx.engine, victim_id)
                        .map_err(RoundOutcome::Fatal)?
                    {
                        return Err(RoundOutcome::Unroutable);
                    }
                    local_victim_recoveries += 1;
                }
                Err(TryUnitErr::Fatal(e)) => return Err(RoundOutcome::Fatal(e)),
                Err(TryUnitErr::Blocked {
                    frontier: v_frontier,
                    blockers: pre_vb,
                }) => {
                    stats.frontier_probes = stats
                        .frontier_probes
                        .saturating_add(v_frontier.len() as u64);
                    if rips_this_round >= MAX_RIPS_PER_ROUND {
                        return Err(RoundOutcome::Unroutable);
                    }
                    let v_blockers = match pre_vb {
                        Some(b) => b,
                        None => analyze_blockers(
                            round_tx.engine,
                            v_spec,
                            &v_frontier,
                            adapter,
                            spec_by_id,
                            rip_counts,
                        )
                        .map_err(RoundOutcome::Fatal)?,
                    };
                    let Some(w_idx) =
                        pick_victim(&v_blockers, &ripped_in_round, rip_counts, &local_rip_deltas)
                    else {
                        return Err(RoundOutcome::Unroutable);
                    };
                    let w_id = v_blockers[w_idx].unit;
                    history
                        .stamp_frontier(&v_frontier, round_tx.engine)
                        .map_err(RoundOutcome::Fatal)?;
                    history
                        .stamp_cells(
                            &v_blockers[w_idx].contested_cells,
                            BASE_CONTEST_INC,
                            round_tx.engine,
                        )
                        .map_err(RoundOutcome::Fatal)?;
                    if let Some(w_spec) = spec_by_id.get(&w_id) {
                        history
                            .stamp_unit_footprint(w_spec, round_tx.engine)
                            .map_err(RoundOutcome::Fatal)?;
                    }
                    adapter
                        .rip_unit(round_tx.engine, w_id)
                        .map_err(RoundOutcome::Fatal)?;
                    *local_rip_deltas.entry(w_id).or_insert(0) += 1;
                    rips_this_round += 1;
                    local_units_ripped += 1;
                    ripped_in_round.insert(w_id);
                    reroute_queue.push_back(w_id);
                    reroute_queue.push_front(victim_id);
                }
            }
        }
        Ok(())
    });

    stats.units_ripped = stats.units_ripped.saturating_add(local_units_ripped);
    match res {
        Ok(()) => {
            for (k, v) in local_rip_deltas {
                *rip_counts.entry(k).or_insert(0) += v;
            }
            stats.rounds_committed = stats.rounds_committed.saturating_add(1);
            stats.victim_recoveries = stats
                .victim_recoveries
                .saturating_add(local_victim_recoveries);
            Ok((true, Some(top_blocker)))
        }
        Err(TransactionError::Operation(RoundOutcome::Unroutable)) => {
            stats.rounds_rolled_back = stats.rounds_rolled_back.saturating_add(1);
            Ok((false, Some(top_blocker)))
        }
        Err(other) => Err(unwrap_round_tx_err(other)),
    }
}

pub fn solve_with_adapter(
    tx: &mut Transaction<'_>,
    adapter: &mut dyn RouteUnitAdapter,
    expanded: &mut u64,
) -> Result<NegotiationStats, Failure> {
    let batch_limit = tx.engine.input().max_expansions;
    if batch_limit == 0 || batch_limit > MAX_EXPANSIONS {
        return Err(Failure::new(
            Status::Invalid,
            "max_expansions must be in 1..=1000000",
        ));
    }
    let units = adapter.units(tx.engine)?;
    let mut spec_by_id: BTreeMap<RouteUnitId, UnitSpec> = BTreeMap::new();
    let mut original_order: BTreeMap<RouteUnitId, usize> = BTreeMap::new();
    for (idx, u) in units.iter().enumerate() {
        spec_by_id.insert(u.id, u.clone());
        original_order.insert(u.id, idx);
    }

    let mut history = CongestionHistory::new();
    let mut rip_counts: BTreeMap<RouteUnitId, u32> = BTreeMap::new();
    let mut failed_history_count: BTreeMap<RouteUnitId, u32> = BTreeMap::new();
    let mut failed_units: Vec<RouteUnitId> = Vec::new();
    let mut stats = NegotiationStats::default();
    let mut routed_rippable_count = 0usize;
    let mut saw_any_blockers = false;

    for u in &units {
        tx.engine.charge_work(1).map_err(|mut e| {
            e.expanded_states = *expanded;
            e
        })?;
        match try_unit_with_cap(
            tx.engine,
            adapter,
            &spec_by_id,
            u,
            &history,
            batch_limit,
            expanded,
            routed_rippable_count > 0,
            &rip_counts,
        ) {
            Ok(()) => {
                stats.initial_routed += 1;
                if u.rippable_by_single {
                    routed_rippable_count += 1;
                }
            }
            Err(TryUnitErr::Fatal(mut e)) => {
                e.expanded_states = *expanded;
                stats.history_cells_peak = history.peak_len();
                stats.history_evictions = history.evictions();
                stats.expanded_states = *expanded;
                tx.engine.set_last_negotiation_stats(Some(stats));
                return Err(if e.status == Status::Unroutable {
                    crate::scoped_routing_failure(e, first_net_of(u.id, &spec_by_id))
                } else {
                    e
                });
            }
            Err(TryUnitErr::Blocked { frontier, blockers }) => {
                stats.initial_failed += 1;
                stats.frontier_probes = stats.frontier_probes.saturating_add(frontier.len() as u64);
                let (resolved, top_b) = try_local_ripup_round(
                    tx,
                    adapter,
                    &spec_by_id,
                    u,
                    &frontier,
                    blockers,
                    &mut history,
                    &mut rip_counts,
                    &mut stats,
                    batch_limit,
                    expanded,
                )
                .map_err(|mut e| {
                    e.expanded_states = *expanded;
                    stats.history_cells_peak = history.peak_len();
                    stats.history_evictions = history.evictions();
                    stats.expanded_states = *expanded;
                    tx.engine.set_last_negotiation_stats(Some(stats));
                    e
                })?;
                if resolved {
                    if u.rippable_by_single {
                        routed_rippable_count += 1;
                    }
                } else {
                    saw_any_blockers |= top_b.is_some();
                    *failed_history_count.entry(u.id).or_insert(0) += 1;
                    failed_units.push(u.id);
                }
            }
        }
    }

    if failed_units.is_empty() {
        stats.history_cells_peak = history.peak_len();
        stats.history_evictions = history.evictions();
        stats.expanded_states = *expanded;
        tx.engine.set_last_negotiation_stats(Some(stats));
        return Ok(stats);
    }

    let mut first_failed = failed_units[0];
    if !saw_any_blockers && routed_rippable_count == 0 {
        stats.history_cells_peak = history.peak_len();
        stats.history_evictions = history.evictions();
        stats.expanded_states = *expanded;
        tx.engine.set_last_negotiation_stats(Some(stats));
        let mut e = Failure::new(
            Status::Unroutable,
            "search frontier exhausted with no rippable generated blockers",
        );
        e.expanded_states = *expanded;
        return Err(crate::scoped_routing_failure(
            e,
            first_net_of(first_failed, &spec_by_id),
        ));
    }

    for _pass in 0..MAX_GLOBAL_PASSES {
        stats.global_passes = stats.global_passes.saturating_add(1);
        let mut reordered = units.clone();
        reordered.sort_by_key(|u| {
            (
                u.priority_tier,
                Reverse(failed_history_count.get(&u.id).copied().unwrap_or(0)),
                original_order[&u.id],
            )
        });
        let mut pass_rip_counts: BTreeMap<RouteUnitId, u32> = BTreeMap::new();
        let pass_res = tx.transaction(|pass_tx| -> Result<(), PassOutcome> {
            for u in units.iter().rev() {
                if u.rippable_by_single {
                    adapter
                        .rip_unit(pass_tx.engine, u.id)
                        .map_err(PassOutcome::Fatal)?;
                }
            }
            let mut routed_in_pass = 0usize;
            for u in &reordered {
                pass_tx.engine.charge_work(1).map_err(PassOutcome::Fatal)?;
                match try_unit_with_cap(
                    pass_tx.engine,
                    adapter,
                    &spec_by_id,
                    u,
                    &history,
                    batch_limit,
                    expanded,
                    routed_in_pass > 0,
                    &pass_rip_counts,
                ) {
                    Ok(()) => {
                        routed_in_pass += 1;
                    }
                    Err(TryUnitErr::Fatal(e)) => return Err(PassOutcome::Fatal(e)),
                    Err(TryUnitErr::Blocked { frontier, blockers }) => {
                        stats.frontier_probes =
                            stats.frontier_probes.saturating_add(frontier.len() as u64);
                        match try_local_ripup_round(
                            pass_tx,
                            adapter,
                            &spec_by_id,
                            u,
                            &frontier,
                            blockers,
                            &mut history,
                            &mut pass_rip_counts,
                            &mut stats,
                            batch_limit,
                            expanded,
                        ) {
                            Ok((true, _)) => {
                                routed_in_pass += 1;
                            }
                            Ok((false, top_b)) => {
                                *failed_history_count.entry(u.id).or_insert(0) += 1;
                                if let Some(top) = top_b {
                                    if let Some(top_spec) = spec_by_id.get(&top.unit) {
                                        history
                                            .stamp_unit_footprint(top_spec, pass_tx.engine)
                                            .map_err(PassOutcome::Fatal)?;
                                    }
                                } else {
                                    history
                                        .stamp_frontier(&frontier, pass_tx.engine)
                                        .map_err(PassOutcome::Fatal)?;
                                }
                                return Err(PassOutcome::Unroutable(u.id));
                            }
                            Err(e) => return Err(PassOutcome::Fatal(e)),
                        }
                    }
                }
            }
            Ok(())
        });
        match pass_res {
            Ok(()) => {
                stats.history_cells_peak = history.peak_len();
                stats.history_evictions = history.evictions();
                stats.expanded_states = *expanded;
                tx.engine.set_last_negotiation_stats(Some(stats));
                return Ok(stats);
            }
            Err(TransactionError::Limit(mut e))
            | Err(TransactionError::Operation(PassOutcome::Fatal(mut e))) => {
                e.expanded_states = *expanded;
                stats.history_cells_peak = history.peak_len();
                stats.history_evictions = history.evictions();
                stats.expanded_states = *expanded;
                tx.engine.set_last_negotiation_stats(Some(stats));
                return Err(e);
            }
            Err(TransactionError::Operation(PassOutcome::Unroutable(u_id))) => {
                first_failed = u_id;
            }
        }
    }

    stats.history_cells_peak = history.peak_len();
    stats.history_evictions = history.evictions();
    stats.expanded_states = *expanded;
    tx.engine.set_last_negotiation_stats(Some(stats));
    let mut e = Failure::new(
        Status::Unroutable,
        "global negotiation exhausted passes without routing all nets",
    );
    e.expanded_states = *expanded;
    Err(crate::scoped_routing_failure(
        e,
        first_net_of(first_failed, &spec_by_id),
    ))
}

pub fn route_all_negotiated(
    engine: &mut Engine,
    pinned_nets: &BTreeSet<u32>,
    active_nets: Option<&BTreeSet<u32>>,
) -> Result<(), Failure> {
    let mut adapter = SingleEndedAdapter::new(pinned_nets.clone(), active_nets.cloned());
    engine.operation(|tx| {
        let mut expanded = 0u64;
        let _stats = solve_with_adapter(tx, &mut adapter, &mut expanded)?;
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::*;

    fn test_input() -> Input {
        Input {
            version: 1,
            source_id: "a".repeat(64),
            layer: "F.Cu".into(),
            layers: Vec::new(),
            bounds: Bounds {
                min: [0, 0],
                max: [100, 100],
            },
            rules: Rules {
                width_nm: 2,
                clearance_nm: 1,
                grid_nm: 10,
                edge_clearance_nm: 0,
                hole_clearance_nm: 0,
                same_net_hole_clearance_nm: 4,
                solder_mask_expansion_nm: 0,
                solder_mask_min_width_nm: 0,
                solder_mask_to_copper_clearance_nm: 0,
            },
            nets: vec![Net {
                id: 1,
                name: "N1".into(),
            }],
            pads: vec![
                Pad {
                    id: "10000000-0000-4000-8000-000000000001".into(),
                    net: 1,
                    at: [10, 50],
                    layers: Vec::new(),
                    local_clearance_nm: None,
                    zone_connection: PadZoneConnection::Inherited,
                    thermal_gap_nm: None,
                    thermal_spoke_width_nm: None,
                    thermal_spoke_angle_mdeg: None,
                    orientation_mdeg: 0,
                    shape: Shape::Circle { radius_nm: 2 },
                    drill: None,
                },
                Pad {
                    id: "10000000-0000-4000-8000-000000000002".into(),
                    net: 1,
                    at: [90, 50],
                    layers: Vec::new(),
                    local_clearance_nm: None,
                    zone_connection: PadZoneConnection::Inherited,
                    thermal_gap_nm: None,
                    thermal_spoke_width_nm: None,
                    thermal_spoke_angle_mdeg: None,
                    orientation_mdeg: 0,
                    shape: Shape::Circle { radius_nm: 2 },
                    drill: None,
                },
            ],
            tracks: Vec::new(),
            vias: Vec::new(),
            rule_areas: Vec::new(),
            max_expansions: 10_000,
            limits: Limits::default(),
            holes: Vec::new(),
            board_outline: None,
            graphics: Vec::new(),
            mask_apertures: Vec::new(),
            board_design_settings: None,
            project_rules: None,
            custom_rules: None,
            effective_rules: None,
            zones: Vec::new(),
        }
    }

    #[test]
    fn c_m3_03_collinear_uturn_does_not_coalesce_to_zero_length() {
        let mut engine = Engine::new(test_input()).unwrap();
        let uturn = crate::search::Path {
            points: vec![[10, 50], [30, 50], [10, 50]],
            edges: vec![
                crate::search::PathEdge::Track {
                    start: [10, 50],
                    end: [30, 50],
                    layer: 0,
                },
                crate::search::PathEdge::Track {
                    start: [30, 50],
                    end: [10, 50],
                    layer: 0,
                },
            ],
            steps: 2,
        };
        engine
            .transaction(|tx| {
                tx.engine.insert_path(1, uturn).unwrap();
                let tracks: Vec<_> = tx.copper().generated().collect();
                assert_eq!(tracks.len(), 1);
                assert_eq!(tracks[0].start, [10, 50]);
                assert_eq!(tracks[0].end, [30, 50]);
                Ok::<_, Failure>(())
            })
            .unwrap();

        let overshoot_uturn = crate::search::Path {
            points: vec![[20, 50], [40, 50], [10, 50]],
            edges: vec![
                crate::search::PathEdge::Track {
                    start: [20, 50],
                    end: [40, 50],
                    layer: 0,
                },
                crate::search::PathEdge::Track {
                    start: [40, 50],
                    end: [10, 50],
                    layer: 0,
                },
            ],
            steps: 2,
        };
        let mut engine2 = Engine::new(test_input()).unwrap();
        engine2
            .transaction(|tx| {
                tx.engine.insert_path(1, overshoot_uturn).unwrap();
                let tracks: Vec<_> = tx.copper().generated().collect();
                assert_eq!(tracks.len(), 2);
                assert!(tracks.iter().all(|t| t.start != t.end));
                assert!(tracks
                    .iter()
                    .any(|t| t.start == [20, 50] && t.end == [40, 50]));
                assert!(tracks
                    .iter()
                    .any(|t| t.start == [20, 50] && t.end == [10, 50]));
                Ok::<_, Failure>(())
            })
            .unwrap();
    }

    #[test]
    fn congestion_history_bounds_cells_and_evicts_deterministically() {
        let engine = Engine::new(test_input()).unwrap();
        let mut h = CongestionHistory::new();
        assert!(h.is_empty());
        assert_eq!(h.step_penalty(0, [10, 50], [0, 0], 10), 0);
        for i in 0..=(MAX_HISTORY_CELLS as i64) {
            h.add_weight((0, i, 0), if i == 42 { 1 } else { 5 }, &engine)
                .unwrap();
        }
        assert_eq!(h.len(), MAX_HISTORY_CELLS);
        assert_eq!(h.evictions(), 1);
        assert_eq!(h.peak_len(), MAX_HISTORY_CELLS);
        // Lowest-weight entry (0, 42, 0) with weight 1 was evicted first.
        assert_eq!(h.step_penalty(0, [420, 0], [0, 0], 10), 0);
        assert_eq!(h.step_penalty(0, [0, 0], [0, 0], 10), 5);
    }

    #[test]
    fn blocked_frontier_deduplicates_and_caps_at_limit() {
        let mut f = BlockedFrontier::new();
        assert!(f.is_empty());
        f.push_edge(0, [0, 0], [10, 0]);
        f.push_edge(0, [0, 0], [10, 0]);
        assert_eq!(f.len(), 1);
        assert_eq!(f.total_seen(), 2);
        for i in 1..(MAX_FRONTIER_PROBES as i64 + 50) {
            f.push_edge(0, [i * 10, 0], [i * 10 + 10, 0]);
        }
        assert_eq!(f.len(), MAX_FRONTIER_PROBES);
        assert_eq!(f.total_seen(), (MAX_FRONTIER_PROBES + 51) as u64);
    }

    #[test]
    fn congestion_history_steers_join_components_negotiated_admissibly() {
        let engine = Engine::new(test_input()).unwrap();
        let groups = engine.components(1).unwrap();
        assert_eq!(groups.len(), 2);

        let mut hist = CongestionHistory::new();
        // Heavily penalize the straight y=50 corridor at x=50..=60.
        for x in [40, 50, 60] {
            hist.add_weight((0, x / 10, 5), 20, &engine).unwrap();
        }
        let mut expanded = 0u64;
        let mut frontier = BlockedFrontier::new();
        let path = crate::search::join_components_negotiated(
            &engine,
            1,
            &groups[0],
            &groups[1..],
            10_000,
            &mut expanded,
            &hist,
            &mut frontier,
        )
        .unwrap();
        // Straight path pays 8 steps + 3*20 = 68; detouring via y=40 or y=60 pays 10 steps!
        assert!(
            path.steps <= 12,
            "expected congestion-weighted detour <= 12, got {}",
            path.steps
        );
        assert!(path.points.iter().any(|p| p[1] != 50));
    }
}
