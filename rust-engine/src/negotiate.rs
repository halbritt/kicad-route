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
const MAX_STATIC_FRONTIER_PROBES: usize = 1024;
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
    static_probes: Vec<BlockedProbe>,
    static_seen: BTreeSet<BlockedProbe>,
    total_seen: u64,
    truncated: bool,
}

impl BlockedFrontier {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn is_empty(&self) -> bool {
        self.probes.is_empty() && self.static_probes.is_empty()
    }

    pub fn len(&self) -> usize {
        self.probes.len() + self.static_probes.len()
    }

    pub fn total_seen(&self) -> u64 {
        self.total_seen
    }

    pub fn is_truncated(&self) -> bool {
        self.truncated
    }

    pub fn probes(&self) -> &[BlockedProbe] {
        &self.probes
    }

    pub fn static_probes(&self) -> &[BlockedProbe] {
        &self.static_probes
    }

    fn push_probe(&mut self, probe: BlockedProbe, from_generated: bool) {
        self.total_seen = self.total_seen.saturating_add(1);
        if from_generated {
            if self.probes.len() >= MAX_FRONTIER_PROBES {
                if !self.seen.contains(&probe) {
                    self.truncated = true;
                }
                return;
            }
            if self.seen.insert(probe) {
                self.probes.push(probe);
            }
        } else {
            if self.static_probes.len() >= MAX_STATIC_FRONTIER_PROBES {
                if !self.static_seen.contains(&probe) {
                    self.truncated = true;
                }
                return;
            }
            if self.static_seen.insert(probe) {
                self.static_probes.push(probe);
            }
        }
    }

    pub fn push_edge(&mut self, layer: u8, a: Point, b: Point) {
        self.push_probe(BlockedProbe::Edge { layer, a, b }, true);
    }

    pub fn push_via_site(&mut self, start_layer: u8, end_layer: u8, at: Point) {
        self.push_probe(
            BlockedProbe::ViaSite {
                start_layer,
                end_layer,
                at,
            },
            true,
        );
    }

    pub fn push_edge_classified(&mut self, layer: u8, a: Point, b: Point, from_generated: bool) {
        self.push_probe(BlockedProbe::Edge { layer, a, b }, from_generated);
    }

    pub fn push_via_site_classified(
        &mut self,
        start_layer: u8,
        end_layer: u8,
        at: Point,
        from_generated: bool,
    ) {
        self.push_probe(
            BlockedProbe::ViaSite {
                start_layer,
                end_layer,
                at,
            },
            from_generated,
        );
    }
}

fn cell_coord(pt: Point, bounds_min: Point, grid_nm: i64) -> (i64, i64) {
    let g = grid_nm.max(1);
    (
        (pt[0] - bounds_min[0]).div_euclid(g),
        (pt[1] - bounds_min[1]).div_euclid(g),
    )
}

#[derive(Clone, Debug)]
struct HistoryCheckpoint {
    undo: Vec<((u8, i64, i64), Option<u64>)>,
    seen: BTreeSet<(u8, i64, i64)>,
    peak_cells: usize,
    evictions: u64,
}

#[derive(Clone, Debug, Default)]
pub struct CongestionHistory {
    cells: BTreeMap<(u8, i64, i64), u64>,
    eviction_index: BTreeSet<(u64, u8, i64, i64)>,
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

    #[cfg(test)]
    fn index_consistent(&self) -> bool {
        self.cells.len() == self.eviction_index.len()
            && self
                .cells
                .iter()
                .all(|(&(l, cx, cy), &w)| self.eviction_index.contains(&(w, l, cx, cy)))
    }

    fn checkpoint(&self) -> HistoryCheckpoint {
        HistoryCheckpoint {
            undo: Vec::new(),
            seen: BTreeSet::new(),
            peak_cells: self.peak_cells,
            evictions: self.evictions,
        }
    }

    fn rollback(&mut self, ckpt: HistoryCheckpoint) {
        for (key, prev_opt) in ckpt.undo.into_iter().rev() {
            if let Some(cur_w) = self.cells.remove(&key) {
                let removed = self.eviction_index.remove(&(cur_w, key.0, key.1, key.2));
                debug_assert!(removed);
            }
            if let Some(prev_w) = prev_opt {
                self.cells.insert(key, prev_w);
                self.eviction_index.insert((prev_w, key.0, key.1, key.2));
            }
        }
        self.peak_cells = ckpt.peak_cells;
        self.evictions = ckpt.evictions;
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

    #[cfg(test)]
    fn add_weight(
        &mut self,
        key: (u8, i64, i64),
        delta: u64,
        engine: &Engine,
    ) -> Result<(), Failure> {
        self.add_weight_logged(key, delta, engine, None)
    }

    fn add_weight_logged(
        &mut self,
        key: (u8, i64, i64),
        delta: u64,
        engine: &Engine,
        mut ckpt: Option<&mut HistoryCheckpoint>,
    ) -> Result<(), Failure> {
        engine.charge_work(1)?;
        if let Some(&old_w) = self.cells.get(&key) {
            let new_w = old_w.saturating_add(delta).min(HISTORY_CAP);
            if new_w != old_w {
                if let Some(c) = ckpt.as_deref_mut() {
                    if c.seen.insert(key) {
                        c.undo.push((key, Some(old_w)));
                    }
                }
                self.cells.insert(key, new_w);
                let removed = self.eviction_index.remove(&(old_w, key.0, key.1, key.2));
                debug_assert!(removed);
                self.eviction_index.insert((new_w, key.0, key.1, key.2));
            }
            return Ok(());
        }
        if self.cells.len() >= MAX_HISTORY_CELLS {
            engine.charge_work(1)?;
            if let Some((evict_w, l, cx, cy)) = self.eviction_index.pop_first() {
                let evict_key = (l, cx, cy);
                if let Some(c) = ckpt.as_deref_mut() {
                    if c.seen.insert(evict_key) {
                        c.undo.push((evict_key, Some(evict_w)));
                    }
                }
                let removed = self.cells.remove(&evict_key);
                debug_assert_eq!(removed, Some(evict_w));
                self.evictions = self.evictions.saturating_add(1);
            }
        }
        let init_w = delta.min(HISTORY_CAP);
        if let Some(c) = ckpt.as_deref_mut() {
            if c.seen.insert(key) {
                c.undo.push((key, None));
            }
        }
        self.cells.insert(key, init_w);
        self.eviction_index.insert((init_w, key.0, key.1, key.2));
        self.peak_cells = self.peak_cells.max(self.cells.len());
        Ok(())
    }
}

impl CongestionHistory {
    fn stamp_frontier_logged(
        &mut self,
        frontier: &BlockedFrontier,
        engine: &Engine,
        mut ckpt: Option<&mut HistoryCheckpoint>,
    ) -> Result<(), Failure> {
        let bounds_min = engine.input().bounds.min;
        let grid_nm = engine.input().rules.grid_nm;
        let mut stamped: BTreeSet<(u8, i64, i64)> = BTreeSet::new();
        for &probe in frontier
            .probes()
            .iter()
            .chain(frontier.static_probes().iter())
        {
            match probe {
                BlockedProbe::Edge { layer, a, b } => {
                    let c_a = cell_coord(a, bounds_min, grid_nm);
                    let c_b = cell_coord(b, bounds_min, grid_nm);
                    if stamped.insert((layer, c_b.0, c_b.1)) {
                        self.add_weight_logged(
                            (layer, c_b.0, c_b.1),
                            FRONTIER_INC,
                            engine,
                            ckpt.as_deref_mut(),
                        )?;
                    }
                    if c_a != c_b && stamped.insert((layer, c_a.0, c_a.1)) {
                        self.add_weight_logged(
                            (layer, c_a.0, c_a.1),
                            FRONTIER_INC,
                            engine,
                            ckpt.as_deref_mut(),
                        )?;
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
                            self.add_weight_logged(
                                (layer, cx, cy),
                                FRONTIER_INC,
                                engine,
                                ckpt.as_deref_mut(),
                            )?;
                        }
                    }
                }
            }
        }
        Ok(())
    }

    fn stamp_cells_logged(
        &mut self,
        cells: &BTreeSet<(u8, i64, i64)>,
        delta: u64,
        engine: &Engine,
        mut ckpt: Option<&mut HistoryCheckpoint>,
    ) -> Result<(), Failure> {
        for &cell in cells {
            self.add_weight_logged(cell, delta, engine, ckpt.as_deref_mut())?;
        }
        Ok(())
    }

    fn stamp_unit_footprint_logged(
        &mut self,
        unit: &UnitSpec,
        engine: &Engine,
        ckpt: Option<&mut HistoryCheckpoint>,
    ) -> Result<(), Failure> {
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
        self.stamp_cells_logged(&cells, RIP_FOOTPRINT_INC, engine, ckpt)
    }
}

/// Abstraction over single-ended nets and differential pairs for unified negotiation.
///
/// Note: `units` omits a separate `&WorkBudget` parameter because `WorkBudget` is
/// crate-private (`mod budget;`); implementations inside the crate charge `engine.work`
#[allow(clippy::type_complexity)]
pub(crate) struct TrialBudget<'a> {
    pub(crate) current_limit: u64,
    pub(crate) batch_limit: u64,
    pub(crate) aborted: bool,
    on_trial_limit: Option<&'a mut dyn FnMut(&Engine, &BlockedFrontier) -> Result<bool, Failure>>,
}

impl<'a> TrialBudget<'a> {
    pub(crate) fn fixed(limit: u64) -> Self {
        Self {
            current_limit: limit,
            batch_limit: limit,
            aborted: false,
            on_trial_limit: None,
        }
    }

    pub(crate) fn with_trial(
        trial_limit: u64,
        batch_limit: u64,
        on_trial_limit: &'a mut dyn FnMut(&Engine, &BlockedFrontier) -> Result<bool, Failure>,
    ) -> Self {
        Self {
            current_limit: trial_limit.min(batch_limit),
            batch_limit,
            aborted: false,
            on_trial_limit: Some(on_trial_limit),
        }
    }

    #[inline(always)]
    pub(crate) fn check_before_expand(
        &mut self,
        engine: &Engine,
        expanded: u64,
        frontier: Option<&BlockedFrontier>,
    ) -> Result<(), Failure> {
        if expanded < self.current_limit {
            return Ok(());
        }
        if let Some(cb) = self.on_trial_limit.take() {
            let empty = BlockedFrontier::new();
            let f = frontier.unwrap_or(&empty);
            if cb(engine, f)? {
                self.aborted = true;
                return Err(Failure::new(
                    Status::BudgetExhausted,
                    "trial expansion cap reached with rippable blockers",
                ));
            }
            self.current_limit = self.batch_limit;
            if expanded < self.current_limit {
                return Ok(());
            }
        }
        Err(Failure::new(
            Status::BudgetExhausted,
            "total expansion budget exhausted",
        ))
    }
}

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
    #[doc(hidden)]
    #[allow(clippy::too_many_arguments)]
    fn route_unit_with_trial(
        &mut self,
        engine: &mut Engine,
        unit: RouteUnitId,
        history: &CongestionHistory,
        trial_limit: u64,
        batch_limit: u64,
        expanded: &mut u64,
        on_trial_limit: &mut dyn FnMut(&Engine, &BlockedFrontier) -> Result<bool, Failure>,
    ) -> Result<(), UnitRouteError> {
        let cap_limit = trial_limit.min(batch_limit);
        if cap_limit >= batch_limit {
            return self.route_unit(engine, unit, history, batch_limit, expanded);
        }
        match self.route_unit(engine, unit, history, cap_limit, expanded) {
            Ok(()) => Ok(()),
            Err(UnitRouteError::Blocked(frontier)) => {
                if *expanded < cap_limit {
                    return Err(UnitRouteError::Blocked(frontier));
                }
                match on_trial_limit(engine, &frontier) {
                    Ok(true) => Err(UnitRouteError::Blocked(frontier)),
                    Ok(false) => {
                        if let Err(e) = self.rip_unit(engine, unit) {
                            return Err(UnitRouteError::Fatal(e));
                        }
                        self.route_unit(engine, unit, history, batch_limit, expanded)
                    }
                    Err(e) => Err(UnitRouteError::Fatal(e)),
                }
            }
            Err(UnitRouteError::Fatal(e)) => {
                if e.status == Status::BudgetExhausted && *expanded < batch_limit {
                    let empty = BlockedFrontier::new();
                    match on_trial_limit(engine, &empty) {
                        Ok(true) => Err(UnitRouteError::Blocked(empty)),
                        Ok(false) => {
                            if let Err(re) = self.rip_unit(engine, unit) {
                                return Err(UnitRouteError::Fatal(re));
                            }
                            self.route_unit(engine, unit, history, batch_limit, expanded)
                        }
                        Err(err) => Err(UnitRouteError::Fatal(err)),
                    }
                } else {
                    Err(UnitRouteError::Fatal(e))
                }
            }
        }
    }
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

/// A new surface net with very few usable pad exits can lose those exits
/// when a more flexible net routes first. Existing net order breaks ties.
pub(crate) fn prioritize_pad_escapes(engine: &Engine, nets: &mut [u32]) -> Result<(), Failure> {
    if nets.len() < 2 || engine.layer_count() < 2 {
        return Ok(());
    }
    let input = engine.input();
    let mut eligible = BTreeMap::<u32, (usize, u8)>::new();
    engine.work.search_storage(nets.len())?;
    engine.work.charge(nets.len() as u64)?;
    for net in nets.iter().copied() {
        eligible.insert(net, (0, 3));
    }
    // Existing same-net conductors can already provide a way out of a pad.
    engine.work.charge(
        (input.tracks.len() + input.vias.len() + input.zones.len() + input.graphics.len()) as u64,
    )?;
    for net in input
        .tracks
        .iter()
        .map(|t| t.net)
        .chain(input.vias.iter().map(|v| v.net))
        .chain(input.zones.iter().map(|z| z.net))
        .chain(input.graphics.iter().map(|g| g.net))
    {
        eligible.remove(&net);
    }
    engine.work.charge(input.pads.len() as u64)?;
    for p in &input.pads {
        if let Some((count, _)) = eligible.get_mut(&p.net) {
            *count += 1
        }
    }
    eligible.retain(|net, (count, _)| *count >= 2 && !engine.copper.has_generated_for_net(*net));
    if eligible.is_empty() {
        return Ok(());
    }
    engine.work.charge(input.pads.len() as u64)?;
    for p in &input.pads {
        if let Some((_, rank)) = eligible.get_mut(&p.net) {
            if *rank > 1 {
                *rank = (*rank).min(crate::search::pad_escape_rank(engine, p)?);
            }
        }
    }
    nets.sort_by_key(|net| eligible.get(net).map_or(3, |(_, rank)| *rank));
    Ok(())
}

impl RouteUnitAdapter for SingleEndedAdapter {
    fn units(&self, engine: &Engine) -> Result<Vec<UnitSpec>, Failure> {
        engine.charge_work(engine.input().nets.len() as u64)?;
        let mut net_bounds: BTreeMap<u32, ([i64; 2], [i64; 2], usize)> = BTreeMap::new();
        let mut nets: Vec<u32> = engine
            .input()
            .nets
            .iter()
            .filter(|n| {
                n.id != 0
                    && !self.pinned_nets.contains(&n.id)
                    && self.active_nets.as_ref().is_none_or(|s| s.contains(&n.id))
            })
            .map(|n| {
                net_bounds.insert(n.id, ([i64::MAX, i64::MAX], [i64::MIN, i64::MIN], 0));
                n.id
            })
            .collect();
        engine.charge_work(engine.input().pads.len() as u64)?;
        for pad in &engine.input().pads {
            if let Some((lo, hi, count)) = net_bounds.get_mut(&pad.net) {
                lo[0] = lo[0].min(pad.at[0]);
                lo[1] = lo[1].min(pad.at[1]);
                hi[0] = hi[0].max(pad.at[0]);
                hi[1] = hi[1].max(pad.at[1]);
                *count += 1;
            }
        }
        engine.charge_work(nets.len() as u64)?;
        nets.sort_unstable_by_key(|&net| {
            let (lo, hi, pad_count) = net_bounds[&net];
            let hpwl = if pad_count == 0 {
                0i128
            } else {
                (i128::from(hi[0]) - i128::from(lo[0])) + (i128::from(hi[1]) - i128::from(lo[1]))
            };
            (hpwl, pad_count, net)
        });
        prioritize_pad_escapes(engine, &mut nets)?;
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

    fn route_unit_with_trial(
        &mut self,
        engine: &mut Engine,
        unit: RouteUnitId,
        history: &CongestionHistory,
        trial_limit: u64,
        batch_limit: u64,
        expanded: &mut u64,
        on_trial_limit: &mut dyn FnMut(&Engine, &BlockedFrontier) -> Result<bool, Failure>,
    ) -> Result<(), UnitRouteError> {
        match unit {
            RouteUnitId::SingleNet(net) => {
                let mut budget = TrialBudget::with_trial(trial_limit, batch_limit, on_trial_limit);
                engine.route_net_negotiated_with_budget(net, &mut budget, expanded, history)
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
    if !victim.rippable_by_single
        && (blocked.priority_tier > victim.priority_tier
            || matches!(blocked.id, RouteUnitId::SingleNet(_)))
    {
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
    analyze_blockers_with_lookup(
        engine,
        blocked_unit,
        frontier,
        &|n| adapter.unit_for_net(n),
        spec_by_id,
        rip_counts,
    )
}

fn analyze_blockers_with_lookup(
    engine: &Engine,
    blocked_unit: &UnitSpec,
    frontier: &BlockedFrontier,
    unit_for_net: &dyn Fn(u32) -> Option<RouteUnitId>,
    spec_by_id: &BTreeMap<RouteUnitId, UnitSpec>,
    rip_counts: &BTreeMap<RouteUnitId, u32>,
) -> Result<Vec<BlockerScore>, Failure> {
    if frontier.probes().is_empty() {
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
        let Some(victim_id) = unit_for_net(other_net) else {
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

    let mask_sharing_active = !engine.input().mask_apertures.is_empty()
        && engine.input().rules.solder_mask_to_copper_clearance_nm <= 0;

    if layer_tracks.iter().all(|v| v.is_empty()) && gen_vias.is_empty() && !mask_sharing_active {
        return Ok(Vec::new());
    }

    let mut scores: BTreeMap<RouteUnitId, BlockerScore> = BTreeMap::new();
    let mut mask_group_nets_cache: BTreeMap<(usize, usize), Vec<u32>> = BTreeMap::new();
    let mut hit_groups_buf: Vec<usize> = Vec::new();
    let mut group_nets_buf: Vec<u32> = Vec::new();
    for &probe in frontier.probes() {
        engine.charge_work(1)?;
        let mut probe_victims: BTreeSet<RouteUnitId> = BTreeSet::new();
        let mut record_hit = |other_net: u32, cell: (u8, i64, i64)| {
            let Some(victim_id) = unit_for_net(other_net) else {
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
        let mut mask_hit_nets_buf: Vec<u32> = Vec::new();
        let mut collect_mask_hits = |l_idx: usize,
                                     a: Point,
                                     b: Point,
                                     width_nm: i64,
                                     out_nets: &mut Vec<u32>|
         -> Result<(), Failure> {
            out_nets.clear();
            if !mask_sharing_active {
                return Ok(());
            }
            engine.mask_apertures_hit(l_idx, a, b, width_nm, &mut hit_groups_buf)?;
            for &g_id in &hit_groups_buf {
                let key = (l_idx, g_id);
                if !mask_group_nets_cache.contains_key(&key) {
                    engine.generated_nets_touching_mask_group(
                        net_filter,
                        l_idx,
                        g_id,
                        &mut group_nets_buf,
                    )?;
                    let rippable: Vec<u32> = group_nets_buf
                        .iter()
                        .copied()
                        .filter(|&n| can_rip_net(n))
                        .collect();
                    mask_group_nets_cache.insert(key, rippable);
                }
                if let Some(other_nets) = mask_group_nets_cache.get(&key) {
                    out_nets.extend_from_slice(other_nets);
                }
            }
            Ok(())
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
                        let s_idx = engine.layer_index(&via.start_layer).unwrap_or(0);
                        let e_idx = engine.layer_index(&via.end_layer).unwrap_or(s_idx);
                        let via_layers = &stackup[s_idx.min(e_idx)..=s_idx.max(e_idx)];
                        let copper_clr = rules.pair_clearance_on_layer(
                            (primary_net, DruItemKind::Track, layer_slice),
                            (via.net, DruItemKind::Via, via_layers),
                            layer_name,
                            None,
                        );
                        let hole_clr = rules.pair_hole_clearance(
                            primary_net,
                            DruItemKind::Track,
                            layer_slice,
                            via.net,
                            DruItemKind::Via,
                            via_layers,
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
                collect_mask_hits(l_idx, a, b, width_nm, &mut mask_hit_nets_buf)?;
                for &other_net in &mask_hit_nets_buf {
                    record_hit(other_net, (layer, cx, cy));
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
                        let clr = rules.pair_clearance_on_layer(
                            (primary_net, DruItemKind::Via, span_layers),
                            (other_net, DruItemKind::Track, layer_slice),
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
                                let clr = rules.pair_clearance_on_layer(
                                    (primary_net, DruItemKind::Via, span_layers),
                                    (via.net, DruItemKind::Via, ev_layers),
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
                for l_idx in s_lo..=s_hi {
                    collect_mask_hits(l_idx, at, at, diam, &mut mask_hit_nets_buf)?;
                    for &other_net in &mask_hit_nets_buf {
                        record_hit(other_net, (l_idx as u8, cx, cy));
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

fn trial_cap_for(batch_limit: u64, max_ok_unit_exp: u64) -> Option<u64> {
    let half = batch_limit / 2;
    if half < TRIAL_EXPANSION_CAP {
        return None;
    }
    if max_ok_unit_exp == 0 {
        return Some(TRIAL_EXPANSION_CAP);
    }
    Some(
        max_ok_unit_exp
            .saturating_mul(6)
            .clamp(TRIAL_EXPANSION_CAP, half),
    )
}

enum TryUnitErr {
    Blocked {
        frontier: BlockedFrontier,
        blockers: Option<Vec<BlockerScore>>,
        hit_trial_cap: bool,
    },
    Fatal(Failure),
}

struct TrialRipFilter<'a> {
    ripped_in_round: &'a BTreeSet<RouteUnitId>,
    local_rip_deltas: &'a BTreeMap<RouteUnitId, u32>,
}

impl TrialRipFilter<'_> {
    fn is_eligible(&self, b: &BlockerScore, rip_counts: &BTreeMap<RouteUnitId, u32>) -> bool {
        !self.ripped_in_round.contains(&b.unit)
            && rip_counts.get(&b.unit).copied().unwrap_or(0)
                + self.local_rip_deltas.get(&b.unit).copied().unwrap_or(0)
                < MAX_RIPS_PER_UNIT
    }
}

#[allow(clippy::too_many_arguments)]
fn try_unit_with_cap(
    engine: &mut Engine,
    adapter: &mut dyn RouteUnitAdapter,
    net_to_unit: &BTreeMap<u32, RouteUnitId>,
    spec_by_id: &BTreeMap<RouteUnitId, UnitSpec>,
    unit: &UnitSpec,
    history: &CongestionHistory,
    batch_limit: u64,
    expanded: &mut u64,
    trial_cap: Option<u64>,
    rip_counts: &BTreeMap<RouteUnitId, u32>,
    rip_filter: Option<TrialRipFilter<'_>>,
) -> Result<(), TryUnitErr> {
    let remaining = batch_limit.saturating_sub(*expanded);
    if remaining == 0 {
        return Err(TryUnitErr::Fatal(Failure::new(
            Status::BudgetExhausted,
            "total expansion budget exhausted",
        )));
    }
    if let Some(cap) = trial_cap.filter(|&c| remaining > c) {
        let trial_limit = (*expanded).saturating_add(cap);
        let is_blocker_eligible = |b: &BlockerScore| -> bool {
            if let Some(filter) = &rip_filter {
                filter.is_eligible(b, rip_counts)
            } else {
                b.unit != unit.id
                    && rip_counts.get(&b.unit).copied().unwrap_or(0) < MAX_RIPS_PER_UNIT
            }
        };
        let mut trial_triggered = false;
        let mut trial_blockers: Option<Vec<BlockerScore>> = None;
        let result = {
            let mut on_trial_limit =
                |eng: &Engine, frontier: &BlockedFrontier| -> Result<bool, Failure> {
                    trial_triggered = true;
                    let blockers = analyze_blockers_with_lookup(
                        eng,
                        unit,
                        frontier,
                        &|n| net_to_unit.get(&n).copied(),
                        spec_by_id,
                        rip_counts,
                    )?;
                    if blockers.iter().any(&is_blocker_eligible) {
                        trial_blockers = Some(blockers);
                        Ok(true)
                    } else {
                        Ok(false)
                    }
                };
            adapter.route_unit_with_trial(
                engine,
                unit.id,
                history,
                trial_limit,
                batch_limit,
                expanded,
                &mut on_trial_limit,
            )
        };
        return match result {
            Ok(()) => Ok(()),
            Err(UnitRouteError::Blocked(frontier)) => {
                if let Some(blockers) = trial_blockers {
                    Err(TryUnitErr::Blocked {
                        frontier,
                        blockers: Some(blockers),
                        hit_trial_cap: true,
                    })
                } else if !trial_triggered && *expanded >= trial_limit {
                    let blockers = analyze_blockers_with_lookup(
                        engine,
                        unit,
                        &frontier,
                        &|n| net_to_unit.get(&n).copied(),
                        spec_by_id,
                        rip_counts,
                    )
                    .map_err(TryUnitErr::Fatal)?;
                    if blockers.iter().any(&is_blocker_eligible) {
                        Err(TryUnitErr::Blocked {
                            frontier,
                            blockers: Some(blockers),
                            hit_trial_cap: true,
                        })
                    } else {
                        Err(TryUnitErr::Blocked {
                            frontier,
                            blockers: None,
                            hit_trial_cap: false,
                        })
                    }
                } else {
                    Err(TryUnitErr::Blocked {
                        frontier,
                        blockers: None,
                        hit_trial_cap: false,
                    })
                }
            }
            Err(UnitRouteError::Fatal(e)) => Err(TryUnitErr::Fatal(e)),
        };
    }
    adapter
        .route_unit(engine, unit.id, history, batch_limit, expanded)
        .map_err(|e| match e {
            UnitRouteError::Blocked(f) => TryUnitErr::Blocked {
                frontier: f,
                blockers: None,
                hit_trial_cap: false,
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
    Unrecoverable(RouteUnitId),
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
    net_to_unit: &BTreeMap<u32, RouteUnitId>,
    spec_by_id: &BTreeMap<RouteUnitId, UnitSpec>,
    unit: &UnitSpec,
    initial_frontier: &BlockedFrontier,
    precomputed_blockers: Option<Vec<BlockerScore>>,
    history: &mut CongestionHistory,
    rip_counts: &mut BTreeMap<RouteUnitId, u32>,
    stats: &mut NegotiationStats,
    batch_limit: u64,
    expanded: &mut u64,
    trial_cap: Option<u64>,
    is_trial_attempt: bool,
    no_trial_cap: &mut BTreeSet<RouteUnitId>,
    max_ok_unit_exp: &mut u64,
) -> Result<(bool, Option<BlockerScore>), Failure> {
    let blockers = match precomputed_blockers {
        Some(b) => b,
        None => analyze_blockers_with_lookup(
            tx.engine,
            unit,
            initial_frontier,
            &|n| net_to_unit.get(&n).copied(),
            spec_by_id,
            rip_counts,
        )?,
    };
    if blockers.is_empty() {
        return Ok((false, None));
    }
    let top_blocker = blockers[0].clone();
    stats.rounds_attempted = stats.rounds_attempted.saturating_add(1);
    let mut hist_ckpt = is_trial_attempt.then(|| history.checkpoint());
    history.stamp_frontier_logged(initial_frontier, tx.engine, hist_ckpt.as_mut())?;
    history.stamp_cells_logged(
        &blockers[0].contested_cells,
        BASE_CONTEST_INC,
        tx.engine,
        hist_ckpt.as_mut(),
    )?;

    let mut local_rip_deltas: BTreeMap<RouteUnitId, u32> = BTreeMap::new();
    let mut local_units_ripped = 0u64;
    let mut local_victim_recoveries = 0u64;

    let res = tx.transaction(
        |round_tx| -> Result<(Vec<RouteUnitId>, u64), RoundOutcome> {
            let mut reroute_queue: VecDeque<RouteUnitId> = VecDeque::new();
            let mut ripped_in_round: BTreeSet<RouteUnitId> = BTreeSet::new();
            ripped_in_round.insert(unit.id);
            let mut rips_this_round = 0usize;
            let mut local_no_trial_cap: Vec<RouteUnitId> = Vec::new();
            let mut local_max_exp: u64 = 0;

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
                    .stamp_unit_footprint_logged(v_spec, round_tx.engine, hist_ckpt.as_mut())
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
                let unit_exp_before = *expanded;
                let can_rip_more_u =
                    co_victims < MAX_CO_VICTIMS && rips_this_round < MAX_RIPS_PER_ROUND;
                let u_cap = (can_rip_more_u
                    && !no_trial_cap.contains(&unit.id)
                    && !local_no_trial_cap.contains(&unit.id))
                .then_some(trial_cap)
                .flatten();
                match try_unit_with_cap(
                    round_tx.engine,
                    adapter,
                    net_to_unit,
                    spec_by_id,
                    unit,
                    history,
                    batch_limit,
                    expanded,
                    u_cap,
                    rip_counts,
                    Some(TrialRipFilter {
                        ripped_in_round: &ripped_in_round,
                        local_rip_deltas: &local_rip_deltas,
                    }),
                ) {
                    Ok(()) => {
                        local_max_exp = local_max_exp.max(expanded.saturating_sub(unit_exp_before));
                        break;
                    }
                    Err(TryUnitErr::Fatal(e)) => return Err(RoundOutcome::Fatal(e)),
                    Err(TryUnitErr::Blocked {
                        frontier: next_frontier,
                        blockers: pre_b,
                        hit_trial_cap: u_hit_cap,
                    }) => {
                        stats.frontier_probes = stats
                            .frontier_probes
                            .saturating_add(next_frontier.len() as u64);
                        let can_rip_more =
                            co_victims < MAX_CO_VICTIMS && rips_this_round < MAX_RIPS_PER_ROUND;
                        let next_blockers = if can_rip_more {
                            match pre_b {
                                Some(b) => b,
                                None => analyze_blockers_with_lookup(
                                    round_tx.engine,
                                    unit,
                                    &next_frontier,
                                    &|n| net_to_unit.get(&n).copied(),
                                    spec_by_id,
                                    rip_counts,
                                )
                                .map_err(RoundOutcome::Fatal)?,
                            }
                        } else {
                            Vec::new()
                        };
                        let picked = if can_rip_more {
                            pick_victim(
                                &next_blockers,
                                &ripped_in_round,
                                rip_counts,
                                &local_rip_deltas,
                            )
                        } else {
                            None
                        };
                        if let Some(idx) = picked {
                            let co_v = next_blockers[idx].unit;
                            history
                                .stamp_frontier_logged(
                                    &next_frontier,
                                    round_tx.engine,
                                    hist_ckpt.as_mut(),
                                )
                                .map_err(RoundOutcome::Fatal)?;
                            history
                                .stamp_cells_logged(
                                    &next_blockers[idx].contested_cells,
                                    BASE_CONTEST_INC,
                                    round_tx.engine,
                                    hist_ckpt.as_mut(),
                                )
                                .map_err(RoundOutcome::Fatal)?;
                            if let Some(v_spec) = spec_by_id.get(&co_v) {
                                history
                                    .stamp_unit_footprint_logged(
                                        v_spec,
                                        round_tx.engine,
                                        hist_ckpt.as_mut(),
                                    )
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
                        } else if u_hit_cap {
                            adapter
                                .rip_unit(round_tx.engine, unit.id)
                                .map_err(RoundOutcome::Fatal)?;
                            let retry_before = *expanded;
                            match try_unit_with_cap(
                                round_tx.engine,
                                adapter,
                                net_to_unit,
                                spec_by_id,
                                unit,
                                history,
                                batch_limit,
                                expanded,
                                None,
                                rip_counts,
                                None,
                            ) {
                                Ok(()) => {
                                    local_no_trial_cap.push(unit.id);
                                    local_max_exp =
                                        local_max_exp.max(expanded.saturating_sub(retry_before));
                                    break;
                                }
                                Err(TryUnitErr::Fatal(e)) => return Err(RoundOutcome::Fatal(e)),
                                Err(TryUnitErr::Blocked { .. }) => {
                                    return Err(RoundOutcome::Unroutable);
                                }
                            }
                        } else {
                            return Err(RoundOutcome::Unroutable);
                        }
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
                let v_exp_before = *expanded;
                let can_rip_more_v = rips_this_round < MAX_RIPS_PER_ROUND;
                let v_cap = (can_rip_more_v
                    && !no_trial_cap.contains(&victim_id)
                    && !local_no_trial_cap.contains(&victim_id))
                .then_some(trial_cap)
                .flatten();
                match try_unit_with_cap(
                    round_tx.engine,
                    adapter,
                    net_to_unit,
                    spec_by_id,
                    v_spec,
                    history,
                    batch_limit,
                    expanded,
                    v_cap,
                    rip_counts,
                    Some(TrialRipFilter {
                        ripped_in_round: &ripped_in_round,
                        local_rip_deltas: &local_rip_deltas,
                    }),
                ) {
                    Ok(()) => {
                        local_max_exp = local_max_exp.max(expanded.saturating_sub(v_exp_before));
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
                        hit_trial_cap: v_hit_cap,
                    }) => {
                        stats.frontier_probes = stats
                            .frontier_probes
                            .saturating_add(v_frontier.len() as u64);
                        let can_rip_more = rips_this_round < MAX_RIPS_PER_ROUND;
                        let v_blockers = if can_rip_more {
                            match pre_vb {
                                Some(b) => b,
                                None => analyze_blockers_with_lookup(
                                    round_tx.engine,
                                    v_spec,
                                    &v_frontier,
                                    &|n| net_to_unit.get(&n).copied(),
                                    spec_by_id,
                                    rip_counts,
                                )
                                .map_err(RoundOutcome::Fatal)?,
                            }
                        } else {
                            Vec::new()
                        };
                        let picked = if can_rip_more {
                            pick_victim(
                                &v_blockers,
                                &ripped_in_round,
                                rip_counts,
                                &local_rip_deltas,
                            )
                        } else {
                            None
                        };
                        if let Some(w_idx) = picked {
                            let w_id = v_blockers[w_idx].unit;
                            history
                                .stamp_frontier_logged(
                                    &v_frontier,
                                    round_tx.engine,
                                    hist_ckpt.as_mut(),
                                )
                                .map_err(RoundOutcome::Fatal)?;
                            history
                                .stamp_cells_logged(
                                    &v_blockers[w_idx].contested_cells,
                                    BASE_CONTEST_INC,
                                    round_tx.engine,
                                    hist_ckpt.as_mut(),
                                )
                                .map_err(RoundOutcome::Fatal)?;
                            if let Some(w_spec) = spec_by_id.get(&w_id) {
                                history
                                    .stamp_unit_footprint_logged(
                                        w_spec,
                                        round_tx.engine,
                                        hist_ckpt.as_mut(),
                                    )
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
                        } else if v_hit_cap {
                            adapter
                                .rip_unit(round_tx.engine, victim_id)
                                .map_err(RoundOutcome::Fatal)?;
                            let retry_before = *expanded;
                            match try_unit_with_cap(
                                round_tx.engine,
                                adapter,
                                net_to_unit,
                                spec_by_id,
                                v_spec,
                                history,
                                batch_limit,
                                expanded,
                                None,
                                rip_counts,
                                None,
                            ) {
                                Ok(()) => {
                                    local_no_trial_cap.push(victim_id);
                                    local_max_exp =
                                        local_max_exp.max(expanded.saturating_sub(retry_before));
                                    if !adapter
                                        .is_unit_complete(round_tx.engine, victim_id)
                                        .map_err(RoundOutcome::Fatal)?
                                    {
                                        return Err(RoundOutcome::Unroutable);
                                    }
                                    local_victim_recoveries += 1;
                                }
                                Err(TryUnitErr::Fatal(e)) => return Err(RoundOutcome::Fatal(e)),
                                Err(TryUnitErr::Blocked { .. }) => {
                                    return Err(RoundOutcome::Unroutable);
                                }
                            }
                        } else {
                            return Err(RoundOutcome::Unroutable);
                        }
                    }
                }
            }
            Ok((local_no_trial_cap, local_max_exp))
        },
    );

    stats.units_ripped = stats.units_ripped.saturating_add(local_units_ripped);
    match res {
        Ok((local_no_cap, local_max_exp)) => {
            for (k, v) in local_rip_deltas {
                *rip_counts.entry(k).or_insert(0) += v;
            }
            for id in local_no_cap {
                no_trial_cap.insert(id);
            }
            *max_ok_unit_exp = (*max_ok_unit_exp).max(local_max_exp);
            stats.rounds_committed = stats.rounds_committed.saturating_add(1);
            stats.victim_recoveries = stats
                .victim_recoveries
                .saturating_add(local_victim_recoveries);
            Ok((true, Some(top_blocker)))
        }
        Err(TransactionError::Operation(RoundOutcome::Unroutable)) => {
            if let Some(ckpt) = hist_ckpt {
                history.rollback(ckpt);
            }
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
    let mut units = adapter.units(tx.engine)?;
    if let Some(options) = tx.engine.input.routing_options.as_ref().and_then(|o| o.bus) {
        tx.engine.bus_plan = crate::bus::BusPlan::build(
            &mut units,
            &tx.engine.input.pads,
            options,
            &tx.engine.work,
        )?;
    }
    // Prefer progress on a single net as well, unless its own drilled holes
    // constrain future copper. In that case explore equal-distance approaches
    // together before committing to a detour.
    let prefer_higher_g =
        units.len() > 1 || tx.engine.input().rules.same_net_hole_clearance_nm == 0;
    let prev_prefer_higher_g = tx.engine.set_prefer_higher_g(prefer_higher_g);
    let res = solve_with_adapter_units(tx, adapter, expanded, batch_limit, units);
    tx.engine.set_prefer_higher_g(prev_prefer_higher_g);
    tx.engine.bus_plan.finish();
    res
}

fn solve_with_adapter_units(
    tx: &mut Transaction<'_>,
    adapter: &mut dyn RouteUnitAdapter,
    expanded: &mut u64,
    batch_limit: u64,
    units: Vec<UnitSpec>,
) -> Result<NegotiationStats, Failure> {
    let mut spec_by_id: BTreeMap<RouteUnitId, UnitSpec> = BTreeMap::new();
    let mut net_to_unit: BTreeMap<u32, RouteUnitId> = BTreeMap::new();
    let mut original_order: BTreeMap<RouteUnitId, usize> = BTreeMap::new();
    for (idx, u) in units.iter().enumerate() {
        for &net in &u.nets {
            net_to_unit.insert(net, u.id);
        }
        spec_by_id.insert(u.id, u.clone());
        original_order.insert(u.id, idx);
    }

    let mut history = CongestionHistory::new();
    let mut rip_counts: BTreeMap<RouteUnitId, u32> = BTreeMap::new();
    let mut failed_history_count: BTreeMap<RouteUnitId, u32> = BTreeMap::new();
    let mut failed_units: Vec<RouteUnitId> = Vec::new();
    let mut stats = NegotiationStats::default();
    let mut routed_rippable_count = 0usize;
    let mut routed_any_count = 0usize;
    let mut max_ok_unit_exp: u64 = 0;
    let mut no_trial_cap: BTreeSet<RouteUnitId> = BTreeSet::new();
    let mut saw_any_blockers = false;
    let mut saw_truncated_frontier = false;

    for u in &units {
        tx.engine.charge_work(1).map_err(|mut e| {
            e.expanded_states = *expanded;
            e
        })?;
        let unit_exp_before = *expanded;
        let can_rip_any =
            routed_rippable_count > 0 || (u.priority_tier == 0 && routed_any_count > 0);
        let t_cap = if can_rip_any && !no_trial_cap.contains(&u.id) {
            trial_cap_for(batch_limit, max_ok_unit_exp)
        } else {
            None
        };
        match try_unit_with_cap(
            tx.engine,
            adapter,
            &net_to_unit,
            &spec_by_id,
            u,
            &history,
            batch_limit,
            expanded,
            t_cap,
            &rip_counts,
            None,
        ) {
            Ok(()) => {
                max_ok_unit_exp = max_ok_unit_exp.max(expanded.saturating_sub(unit_exp_before));
                stats.initial_routed += 1;
                routed_any_count += 1;
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
            Err(TryUnitErr::Blocked {
                frontier,
                blockers,
                hit_trial_cap,
            }) => {
                saw_truncated_frontier |= frontier.is_truncated();
                stats.initial_failed += 1;
                stats.frontier_probes = stats.frontier_probes.saturating_add(frontier.len() as u64);
                let round_cap = trial_cap_for(batch_limit, max_ok_unit_exp);
                let (resolved, top_b) = try_local_ripup_round(
                    tx,
                    adapter,
                    &net_to_unit,
                    &spec_by_id,
                    u,
                    &frontier,
                    blockers,
                    &mut history,
                    &mut rip_counts,
                    &mut stats,
                    batch_limit,
                    expanded,
                    round_cap,
                    hit_trial_cap,
                    &mut no_trial_cap,
                    &mut max_ok_unit_exp,
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
                    routed_any_count += 1;
                    if u.rippable_by_single {
                        routed_rippable_count += 1;
                    }
                } else if hit_trial_cap {
                    adapter.rip_unit(tx.engine, u.id).map_err(|mut e| {
                        e.expanded_states = *expanded;
                        stats.history_cells_peak = history.peak_len();
                        stats.history_evictions = history.evictions();
                        stats.expanded_states = *expanded;
                        tx.engine.set_last_negotiation_stats(Some(stats));
                        e
                    })?;
                    let retry_before = *expanded;
                    match try_unit_with_cap(
                        tx.engine,
                        adapter,
                        &net_to_unit,
                        &spec_by_id,
                        u,
                        &history,
                        batch_limit,
                        expanded,
                        None,
                        &rip_counts,
                        None,
                    ) {
                        Ok(()) => {
                            no_trial_cap.insert(u.id);
                            max_ok_unit_exp =
                                max_ok_unit_exp.max(expanded.saturating_sub(retry_before));
                            routed_any_count += 1;
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
                        Err(TryUnitErr::Blocked {
                            frontier: full_f,
                            blockers: full_b,
                            ..
                        }) => {
                            saw_truncated_frontier |= full_f.is_truncated();
                            let fb = match full_b {
                                Some(b) => b,
                                None => analyze_blockers(
                                    tx.engine,
                                    u,
                                    &full_f,
                                    adapter,
                                    &spec_by_id,
                                    &rip_counts,
                                )
                                .map_err(|mut e| {
                                    e.expanded_states = *expanded;
                                    stats.history_cells_peak = history.peak_len();
                                    stats.history_evictions = history.evictions();
                                    stats.expanded_states = *expanded;
                                    tx.engine.set_last_negotiation_stats(Some(stats));
                                    e
                                })?,
                            };
                            saw_any_blockers |= !fb.is_empty();
                            *failed_history_count.entry(u.id).or_insert(0) += 1;
                            failed_units.push(u.id);
                        }
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
    let mut min_failed_tier = failed_units
        .iter()
        .filter_map(|id| spec_by_id.get(id).map(|s| s.priority_tier))
        .min()
        .unwrap_or(1);
    let can_rip_routed =
        routed_rippable_count > 0 || (min_failed_tier == 0 && routed_any_count > 0);
    if !saw_any_blockers && (!can_rip_routed || !saw_truncated_frontier) {
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
                if u.rippable_by_single || u.priority_tier >= min_failed_tier {
                    adapter
                        .rip_unit(pass_tx.engine, u.id)
                        .map_err(PassOutcome::Fatal)?;
                }
            }
            let mut rippable_routed_in_pass = 0usize;
            let mut routed_any_in_pass = 0usize;
            for u in &reordered {
                pass_tx.engine.charge_work(1).map_err(PassOutcome::Fatal)?;
                let unit_exp_before = *expanded;
                let can_rip_any =
                    rippable_routed_in_pass > 0 || (u.priority_tier == 0 && routed_any_in_pass > 0);
                let t_cap = if can_rip_any && !no_trial_cap.contains(&u.id) {
                    trial_cap_for(batch_limit, max_ok_unit_exp)
                } else {
                    None
                };
                match try_unit_with_cap(
                    pass_tx.engine,
                    adapter,
                    &net_to_unit,
                    &spec_by_id,
                    u,
                    &history,
                    batch_limit,
                    expanded,
                    t_cap,
                    &pass_rip_counts,
                    None,
                ) {
                    Ok(()) => {
                        max_ok_unit_exp =
                            max_ok_unit_exp.max(expanded.saturating_sub(unit_exp_before));
                        routed_any_in_pass += 1;
                        if u.rippable_by_single {
                            rippable_routed_in_pass += 1;
                        }
                    }
                    Err(TryUnitErr::Fatal(e)) => return Err(PassOutcome::Fatal(e)),
                    Err(TryUnitErr::Blocked {
                        frontier,
                        blockers,
                        hit_trial_cap,
                    }) => {
                        stats.frontier_probes =
                            stats.frontier_probes.saturating_add(frontier.len() as u64);
                        let round_cap = trial_cap_for(batch_limit, max_ok_unit_exp);
                        match try_local_ripup_round(
                            pass_tx,
                            adapter,
                            &net_to_unit,
                            &spec_by_id,
                            u,
                            &frontier,
                            blockers,
                            &mut history,
                            &mut pass_rip_counts,
                            &mut stats,
                            batch_limit,
                            expanded,
                            round_cap,
                            hit_trial_cap,
                            &mut no_trial_cap,
                            &mut max_ok_unit_exp,
                        ) {
                            Ok((true, _)) => {
                                routed_any_in_pass += 1;
                                if u.rippable_by_single {
                                    rippable_routed_in_pass += 1;
                                }
                            }
                            Ok((false, mut top_b)) => {
                                let mut stamp_f = &frontier;
                                let full_f_holder: BlockedFrontier;
                                if hit_trial_cap {
                                    pass_tx.engine.charge_work(1).map_err(PassOutcome::Fatal)?;
                                    adapter
                                        .rip_unit(pass_tx.engine, u.id)
                                        .map_err(PassOutcome::Fatal)?;
                                    let retry_before = *expanded;
                                    match try_unit_with_cap(
                                        pass_tx.engine,
                                        adapter,
                                        &net_to_unit,
                                        &spec_by_id,
                                        u,
                                        &history,
                                        batch_limit,
                                        expanded,
                                        None,
                                        &pass_rip_counts,
                                        None,
                                    ) {
                                        Ok(()) => {
                                            no_trial_cap.insert(u.id);
                                            max_ok_unit_exp = max_ok_unit_exp
                                                .max(expanded.saturating_sub(retry_before));
                                            routed_any_in_pass += 1;
                                            if u.rippable_by_single {
                                                rippable_routed_in_pass += 1;
                                            }
                                            continue;
                                        }
                                        Err(TryUnitErr::Fatal(e)) => {
                                            return Err(PassOutcome::Fatal(e));
                                        }
                                        Err(TryUnitErr::Blocked {
                                            frontier: full_f,
                                            blockers: full_b,
                                            ..
                                        }) => {
                                            full_f_holder = full_f;
                                            let fb = match full_b {
                                                Some(b) => b,
                                                None => analyze_blockers(
                                                    pass_tx.engine,
                                                    u,
                                                    &full_f_holder,
                                                    adapter,
                                                    &spec_by_id,
                                                    &pass_rip_counts,
                                                )
                                                .map_err(PassOutcome::Fatal)?,
                                            };
                                            top_b = fb.into_iter().next();
                                            stamp_f = &full_f_holder;
                                        }
                                    }
                                }
                                *failed_history_count.entry(u.id).or_insert(0) += 1;
                                if rippable_routed_in_pass == 0
                                    && !(u.priority_tier == 0 && routed_any_in_pass > 0)
                                    && top_b.is_none()
                                {
                                    return Err(PassOutcome::Unrecoverable(u.id));
                                }
                                if let Some(top) = top_b {
                                    if let Some(top_spec) = spec_by_id.get(&top.unit) {
                                        history
                                            .stamp_unit_footprint_logged(
                                                top_spec,
                                                pass_tx.engine,
                                                None,
                                            )
                                            .map_err(PassOutcome::Fatal)?;
                                    }
                                } else {
                                    history
                                        .stamp_frontier_logged(stamp_f, pass_tx.engine, None)
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
            Err(TransactionError::Operation(PassOutcome::Unrecoverable(u_id))) => {
                first_failed = u_id;
                break;
            }
            Err(TransactionError::Operation(PassOutcome::Unroutable(u_id))) => {
                first_failed = u_id;
                if let Some(s) = spec_by_id.get(&u_id) {
                    min_failed_tier = min_failed_tier.min(s.priority_tier);
                }
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
                    footprint_ref: None,
                    pad_number: None,
                    fromto_id: None,
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
                    footprint_ref: None,
                    pad_number: None,
                    fromto_id: None,
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
            components: Vec::new(),
            stackup: Vec::new(),
            routing_options: None,
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
        assert!(h.index_consistent());
        // Lowest-weight entry (0, 42, 0) with weight 1 was evicted first.
        assert_eq!(h.step_penalty(0, [420, 0], [0, 0], 10), 0);
        assert_eq!(h.step_penalty(0, [0, 0], [0, 0], 10), 5);
    }

    #[test]
    fn congestion_history_eviction_scales_logarithmically_under_heavy_churn() {
        let mut inp = test_input();
        inp.limits.max_work = 500_000;
        let engine = Engine::new(inp).unwrap();
        let mut h = CongestionHistory::new();
        let t0 = std::time::Instant::now();
        for i in 0..(MAX_HISTORY_CELLS as i64) {
            h.add_weight((0, i, 0), ((i % 7) + 1) as u64, &engine)
                .unwrap();
        }
        assert_eq!(h.len(), MAX_HISTORY_CELLS);
        assert!(h.index_consistent());

        let extra = 25_000i64;
        for i in 0..extra {
            h.add_weight((0, MAX_HISTORY_CELLS as i64 + i, 1), 10, &engine)
                .unwrap();
        }
        let elapsed = t0.elapsed();
        assert_eq!(h.len(), MAX_HISTORY_CELLS);
        assert_eq!(h.evictions(), extra as u64);
        assert!(h.index_consistent());
        assert!(engine.work_used() <= 200_000);
        assert!(
            elapsed < std::time::Duration::from_secs(2),
            "eviction churn took {elapsed:?}"
        );

        // Verify HistoryCheckpoint rollback restores cells, eviction_index, peak_cells, and evictions.
        let before_evictions = h.evictions();
        let before_peak = h.peak_len();
        let before_sample =
            h.step_penalty(0, [(MAX_HISTORY_CELLS as i64 + 1) * 10, 10], [0, 0], 10);
        let mut ckpt = h.checkpoint();
        for j in 0..100i64 {
            h.add_weight_logged((0, 1_000_000 + j, 2), 12, &engine, Some(&mut ckpt))
                .unwrap();
        }
        assert_eq!(h.evictions(), before_evictions + 100);
        assert!(h.index_consistent());
        h.rollback(ckpt);
        assert!(h.index_consistent());
        assert_eq!(h.len(), MAX_HISTORY_CELLS);
        assert_eq!(h.evictions(), before_evictions);
        assert_eq!(h.peak_len(), before_peak);
        assert_eq!(
            h.step_penalty(0, [(MAX_HISTORY_CELLS as i64 + 1) * 10, 10], [0, 0], 10),
            before_sample
        );
        assert_eq!(h.step_penalty(0, [10_000_000, 20], [0, 0], 10), 0);
    }

    #[test]
    fn c_m3_03_vertical_uturn_and_diagonal_do_not_coalesce_to_zero_length() {
        // Vertical exact U-turn: [50,10] -> [50,30] -> [50,10] coalesces to 1 nonzero track.
        let mut engine1 = Engine::new(test_input()).unwrap();
        let v_uturn = crate::search::Path {
            points: vec![[50, 10], [50, 30], [50, 10]],
            edges: vec![
                crate::search::PathEdge::Track {
                    start: [50, 10],
                    end: [50, 30],
                    layer: 0,
                },
                crate::search::PathEdge::Track {
                    start: [50, 30],
                    end: [50, 10],
                    layer: 0,
                },
            ],
            steps: 2,
        };
        engine1
            .transaction(|tx| {
                tx.engine.insert_path(1, v_uturn).unwrap();
                let tracks: Vec<_> = tx.copper().generated().collect();
                assert_eq!(tracks.len(), 1);
                assert!(tracks.iter().all(|t| t.start != t.end));
                assert_eq!(tracks[0].start, [50, 10]);
                assert_eq!(tracks[0].end, [50, 30]);
                Ok::<_, Failure>(())
            })
            .unwrap();

        // Vertical overshoot U-turn: [50,20] -> [50,40] -> [50,10] -> 2 nonzero tracks.
        let mut engine2 = Engine::new(test_input()).unwrap();
        let v_overshoot = crate::search::Path {
            points: vec![[50, 20], [50, 40], [50, 10]],
            edges: vec![
                crate::search::PathEdge::Track {
                    start: [50, 20],
                    end: [50, 40],
                    layer: 0,
                },
                crate::search::PathEdge::Track {
                    start: [50, 40],
                    end: [50, 10],
                    layer: 0,
                },
            ],
            steps: 2,
        };
        engine2
            .transaction(|tx| {
                tx.engine.insert_path(1, v_overshoot).unwrap();
                let tracks: Vec<_> = tx.copper().generated().collect();
                assert_eq!(tracks.len(), 2);
                assert!(tracks.iter().all(|t| t.start != t.end));
                assert!(tracks
                    .iter()
                    .any(|t| t.start == [50, 20] && t.end == [50, 40]));
                assert!(tracks
                    .iter()
                    .any(|t| t.start == [50, 20] && t.end == [50, 10]));
                Ok::<_, Failure>(())
            })
            .unwrap();

        // Vertical same-direction: [50,10] -> [50,20] -> [50,40] -> 1 coalesced track [50,10] -> [50,40].
        let mut engine3 = Engine::new(test_input()).unwrap();
        let v_same_dir = crate::search::Path {
            points: vec![[50, 10], [50, 20], [50, 40]],
            edges: vec![
                crate::search::PathEdge::Track {
                    start: [50, 10],
                    end: [50, 20],
                    layer: 0,
                },
                crate::search::PathEdge::Track {
                    start: [50, 20],
                    end: [50, 40],
                    layer: 0,
                },
            ],
            steps: 2,
        };
        engine3
            .transaction(|tx| {
                tx.engine.insert_path(1, v_same_dir).unwrap();
                let tracks: Vec<_> = tx.copper().generated().collect();
                assert_eq!(tracks.len(), 1);
                assert_eq!(tracks[0].start, [50, 10]);
                assert_eq!(tracks[0].end, [50, 40]);
                Ok::<_, Failure>(())
            })
            .unwrap();

        // Diagonal U-turn: [10,10] -> [20,20] -> [10,10] -> 0 zero-length tracks.
        let mut engine4 = Engine::new(test_input()).unwrap();
        let diag = crate::search::Path {
            points: vec![[10, 10], [20, 20], [10, 10]],
            edges: vec![
                crate::search::PathEdge::Track {
                    start: [10, 10],
                    end: [20, 20],
                    layer: 0,
                },
                crate::search::PathEdge::Track {
                    start: [20, 20],
                    end: [10, 10],
                    layer: 0,
                },
            ],
            steps: 2,
        };
        engine4
            .transaction(|tx| {
                tx.engine.insert_path(1, diag).unwrap();
                let tracks: Vec<_> = tx.copper().generated().collect();
                assert!(tracks.iter().all(|t| t.start != t.end));
                Ok::<_, Failure>(())
            })
            .unwrap();
    }

    #[test]
    fn blocked_frontier_deduplicates_and_caps_at_limit() {
        let mut f = BlockedFrontier::new();
        assert!(f.is_empty());
        assert!(!f.is_truncated());
        f.push_edge(0, [0, 0], [10, 0]);
        f.push_edge(0, [0, 0], [10, 0]);
        assert_eq!(f.len(), 1);
        assert_eq!(f.total_seen(), 2);
        assert!(!f.is_truncated());
        for i in 1..(MAX_FRONTIER_PROBES as i64 + 50) {
            f.push_edge(0, [i * 10, 0], [i * 10 + 10, 0]);
        }
        assert_eq!(f.len(), MAX_FRONTIER_PROBES);
        assert_eq!(f.total_seen(), (MAX_FRONTIER_PROBES + 51) as u64);
        assert!(f.is_truncated());
    }

    #[test]
    fn blocked_frontier_retains_generated_probes_after_static_saturation() {
        let mut f = BlockedFrontier::new();
        for i in 0..5000i64 {
            f.push_edge_classified(0, [i * 10, 0], [i * 10 + 10, 0], false);
        }
        assert!(f.is_truncated());
        assert_eq!(f.static_probes().len(), MAX_STATIC_FRONTIER_PROBES);
        assert!(f.probes().is_empty());
        for j in 0..12i64 {
            f.push_edge_classified(0, [j * 10, 100], [j * 10 + 10, 100], true);
        }
        f.push_via_site_classified(0, 1, [50, 100], true);
        assert_eq!(f.probes().len(), 13);
        assert_eq!(f.total_seen(), 5013);
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
