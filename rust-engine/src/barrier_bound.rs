//! Rectilinear lower bounds from layer-specific barriers fixed during a join. Other geometry is
//! removed in this relaxation. No choice of barrier can exclude a physical route.

mod pad_banks;
mod slanted;
pub(crate) use pad_banks::PadBanks;
use slanted::SlantedWall;

use crate::budget::WorkBudget;
use crate::model::{Failure, Input, Point, Track};
use std::collections::BTreeMap;

type Envelope = (Point, Point);

#[derive(Clone, Copy, Debug)]
struct Barrier {
    /// The divider is perpendicular to this axis.
    axis: usize,
    cut: i64,
    lo: i64,
    hi: i64,
}

fn interval_gap(a: i64, b: i64, c: i64, d: i64) -> u64 {
    if b < c {
        b.abs_diff(c)
    } else if d < a {
        d.abs_diff(a)
    } else {
        0
    }
}

impl Barrier {
    fn from_segment(start: Point, end: Point) -> Option<Self> {
        let axis = (0..2).find(|&k| start[k] == end[k] && start[1 - k] != end[1 - k])?;
        Some(Self {
            axis,
            cut: start[axis],
            lo: start[1 - axis].min(end[1 - axis]),
            hi: start[1 - axis].max(end[1 - axis]),
        })
    }

    /// If the full rectangles are on opposite sides, a planar centerline must
    /// pass one end of the divider. End tangency is free in this relaxation.
    fn detour(self, from: Envelope, to: Envelope) -> u64 {
        let k = self.axis;
        if !((from.1[k] < self.cut && self.cut < to.0[k])
            || (to.1[k] < self.cut && self.cut < from.0[k]))
        {
            return 0;
        }
        let j = 1 - k;
        // Crossing anywhere beyond an endpoint is free. If both envelopes
        // reach that half-line, their closest transverse coordinates suffice.
        if (from.0[j] <= self.lo && to.0[j] <= self.lo)
            || (from.1[j] >= self.hi && to.1[j] >= self.hi)
        {
            return interval_gap(from.0[j], from.1[j], to.0[j], to.1[j]);
        }
        let through = |y| {
            interval_gap(from.0[j], from.1[j], y, y)
                .saturating_add(interval_gap(y, y, to.0[j], to.1[j]))
        };
        through(self.lo).min(through(self.hi))
    }
}

/// Longer families keep the single strongest divider. This bounds the quadratic
/// preparation of the two directional profiles independently of the search.
const MAX_ORDERED_CUTS: usize = 16;

struct Passage {
    wall: Barrier,
    // Remaining transverse travel from each open end to the terminal interval.
    remaining: [Option<u64>; 2],
}

impl Passage {
    fn detour(&self, y: i64) -> Option<u64> {
        let [lower, upper] = self.remaining;
        if lower.is_some() && y <= self.wall.lo || upper.is_some() && y >= self.wall.hi {
            return None;
        }
        lower
            .map(|v| v.saturating_add(y.abs_diff(self.wall.lo)))
            .into_iter()
            .chain(upper.map(|v| v.saturating_add(y.abs_diff(self.wall.hi))))
            .min()
    }
}

/// Each gate independently permits crossing on either half-line outside its
/// solid interval. A legal track center is strictly inside the board bounds,
/// so a wall meeting a board edge has no passage on that side.
struct PassageProfile {
    // Closest to the terminal first, so both build and query traverse the
    // relevant suffix in physical crossing order by walking backwards.
    passages: Vec<Passage>,
    increasing: bool,
}

impl PassageProfile {
    fn build(
        ordered: &[Barrier],
        target: Envelope,
        board: Envelope,
        work: &WorkBudget,
    ) -> Result<Self, Failure> {
        let axis = ordered[0].axis;
        let j = 1 - axis;
        let increasing = ordered[0].cut < target.0[axis];
        let mut profile = Self {
            passages: Vec::with_capacity(ordered.len()),
            increasing,
        };
        for i in 0..ordered.len() {
            work.charge(1)?;
            let wall = ordered[if increasing { ordered.len() - 1 - i } else { i }];
            let mut remaining = [None; 2];
            for (k, (y, open)) in [
                (wall.lo, wall.lo > board.0[j]),
                (wall.hi, wall.hi < board.1[j]),
            ]
            .into_iter()
            .enumerate()
            {
                if open {
                    remaining[k] =
                        Some(profile.transverse(y, (target.0[j], target.1[j]), work, |_| true)?);
                }
            }
            if remaining != [None; 2] {
                profile.passages.push(Passage { wall, remaining });
            }
        }
        Ok(profile)
    }

    /// Distance-to-go through the remaining gates is 1-Lipschitz in y. If
    /// the current y is allowed at a gate, crossing there cannot cost more
    /// than changing y first. Otherwise the optimum passes through one of
    /// its open ends, whose suffix costs were computed during preparation.
    fn transverse(
        &self,
        y: i64,
        terminal: (i64, i64),
        work: &WorkBudget,
        between: impl Fn(i64) -> bool,
    ) -> Result<u64, Failure> {
        for passage in self.passages.iter().rev() {
            work.charge(1)?;
            if between(passage.wall.cut) {
                if let Some(distance) = passage.detour(y) {
                    return Ok(distance);
                }
            }
        }
        Ok(interval_gap(y, y, terminal.0, terminal.1))
    }

    fn estimate(
        &self,
        axis: usize,
        p: Point,
        target: Envelope,
        work: &WorkBudget,
    ) -> Result<u64, Failure> {
        let j = 1 - axis;
        self.transverse(p[j], (target.0[j], target.1[j]), work, |cut| {
            if self.increasing {
                p[axis] < cut
            } else {
                p[axis] > cut
            }
        })
    }
}

struct OrderedBounds {
    forward: PassageProfile,
    reverse: PassageProfile,
}

pub(crate) struct BarrierBound {
    source: Envelope,
    target: Envelope,
    source_layers: u32,
    target_layers: u32,
    layers: Vec<[Option<Barrier>; 2]>,
    slanted: Vec<Option<SlantedWall>>,
    through_stack: [Option<Barrier>; 2],
    crossing_layers: [BTreeMap<i64, u32>; 2],
    ordered: [Option<OrderedBounds>; 2],
    grid: u64,
    step: u64,
    layer_change: Option<u64>,
    active: bool,
}

impl BarrierBound {
    /// `source` encloses all forward launch points; `target` encloses every
    /// target's copper on all layers. `reach_nm` is the maximum outward-rounded
    /// track or via radius on this join. Costs are minima in engine cost ticks.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn for_join<'a>(
        input: &Input,
        net: u32,
        tracks: impl IntoIterator<Item = &'a Track>,
        layers: &[String],
        source: Envelope,
        target: Envelope,
        reach_nm: i64,
        grid_nm: i64,
        minimum_step: u64,
        via_cost: u64,
        work: &WorkBudget,
    ) -> Result<Self, Failure> {
        let target = (
            target.0.map(|v| v.saturating_sub(reach_nm)),
            target.1.map(|v| v.saturating_add(reach_nm)),
        );
        work.charge(layers.len() as u64)?;
        work.search_storage(layers.len() * 3)?;
        let mut slanted = vec![None; layers.len()];
        let mut retained = vec![[None; 2]; layers.len()];
        let mut scores = vec![[0; 2]; layers.len()];
        let mut through_stack = [None; 2];
        let mut through_scores = [0; 2];
        let mut crossing_layers = [BTreeMap::<i64, u32>::new(), BTreeMap::new()];
        let mut parallel = [Vec::new(), Vec::new()];
        let mut too_many = [false; 2];
        let mut active = false;
        let mut consider = |slots: &mut [Option<Barrier>; 2], scores: &mut [u64; 2], b: Barrier| {
            let j = 1 - b.axis;
            let base = interval_gap(source.0[j], source.1[j], target.0[j], target.1[j]);
            let extra = b.detour(source, target).saturating_sub(base);
            if extra > scores[b.axis] {
                scores[b.axis] = extra;
                slots[b.axis] = Some(b);
                active = true;
            }
        };
        let layer_index = |name: &str| {
            for (k, layer) in layers.iter().enumerate() {
                work.charge(1)?;
                if layer == name {
                    return Ok::<_, Failure>(Some(k));
                }
            }
            Ok(None)
        };
        for t in tracks {
            work.charge(1)?;
            if t.net != net {
                if let Some(k) = layer_index(&t.layer)? {
                    if let Some(b) = Barrier::from_segment(t.start, t.end) {
                        consider(&mut retained[k], &mut scores[k], b);
                    } else {
                        SlantedWall::consider(
                            &mut slanted[k],
                            t.start,
                            t.end,
                            source,
                            target,
                            work,
                        )?;
                    }
                }
            }
        }
        // Identical solid rectangles may be expressed as one multilayer area
        // or as separate per-layer areas. Only their exact shared footprint
        // can close the planar divider for *every* layer transition.
        let mut rectangles = BTreeMap::<Envelope, u64>::new();
        for area in &input.rule_areas {
            work.charge(1)?;
            if !area.no_tracks {
                continue;
            }
            if let Some(rect) = area.rectangular_bounds() {
                let mut mask = 0;
                for name in &area.layers {
                    if let Some(k) = layer_index(name)? {
                        mask |= 1u64 << k;
                    }
                }
                if mask != 0 {
                    work.charge(1)?;
                    if !rectangles.contains_key(&rect) {
                        work.search_storage(rectangles.len() + 1)?;
                    }
                    *rectangles.entry(rect).or_default() |= mask;
                }
            }
        }
        // Validated physical stacks have at most 32 named copper layers.
        let all = (1u64 << layers.len()) - 1;
        for ((lo, hi), mask) in rectangles {
            work.charge(1)?;
            for axis in 0..2 {
                let b = Barrier {
                    axis,
                    cut: lo[axis].midpoint(hi[axis]),
                    lo: lo[1 - axis],
                    hi: hi[1 - axis],
                };
                if mask == all {
                    consider(&mut through_stack, &mut through_scores, b);
                    if !too_many[axis]
                        && ((source.1[axis] < b.cut && b.cut < target.0[axis])
                            || (target.1[axis] < b.cut && b.cut < source.0[axis]))
                    {
                        if parallel[axis].len() == MAX_ORDERED_CUTS {
                            too_many[axis] = true;
                        } else {
                            work.search_storage(parallel[axis].len() + 1)?;
                            parallel[axis].push(b);
                        }
                    }
                } else {
                    // A centerline crossing at this cut must occupy an open
                    // layer. Only a rectangle spanning both board edges can
                    // rule out a planar detour on its covered layers.
                    if b.lo <= input.bounds.min[1 - axis] && b.hi >= input.bounds.max[1 - axis] {
                        let cuts = &mut crossing_layers[axis];
                        work.charge(1)?;
                        let available = all as u32 & !(mask as u32);
                        if let Some(open) = cuts.get_mut(&b.cut) {
                            *open &= available;
                        } else if cuts.len() < MAX_ORDERED_CUTS {
                            work.search_storage(cuts.len() + 1)?;
                            cuts.insert(b.cut, available);
                        }
                    }
                    for k in 0..layers.len() {
                        work.charge(1)?;
                        if mask & (1u64 << k) != 0 {
                            consider(&mut retained[k], &mut scores[k], b);
                        }
                    }
                }
            }
        }
        let mut ordered = [None, None];
        for (axis, mut walls) in parallel.into_iter().enumerate() {
            if too_many[axis] || walls.len() < 2 {
                continue;
            }
            work.search_storage(2 * walls.len())?;
            // Input rectangles already have deterministic board order. Insert
            // the small retained family in cut order and charge each comparison.
            let key = |b: Barrier| (b.cut, b.lo, b.hi);
            for end in 1..walls.len() {
                for i in (1..=end).rev() {
                    work.charge(1)?;
                    if key(walls[i - 1]) <= key(walls[i]) {
                        break;
                    }
                    walls.swap(i - 1, i);
                }
            }
            let board = (input.bounds.min, input.bounds.max);
            let forward = PassageProfile::build(&walls, target, board, work)?;
            let reverse = PassageProfile::build(&walls, source, board, work)?;
            if forward.passages.len() == walls.len() {
                ordered[axis] = Some(OrderedBounds { forward, reverse });
                active = true;
            }
        }
        active |= slanted.iter().any(Option::is_some);
        Ok(Self {
            source,
            target,
            source_layers: u32::MAX,
            target_layers: u32::MAX,
            layers: retained,
            slanted,
            through_stack,
            ordered,
            grid: grid_nm as u64,
            step: minimum_step,
            layer_change: (layers.len() > 1).then_some(via_cost),
            active: active || crossing_layers.iter().any(|c| !c.is_empty()),
            crossing_layers,
        })
    }

    /// Every launch and landing lies on this face. If crossing a foreign
    /// trace on that face costs strictly more than two layer changes, an
    /// actual two-via path of the stated cost attains the whole join's bound.
    /// Negotiation may change the trace before the next join.
    pub(crate) fn routed_crossing_cost(
        &self,
        copper: &crate::state::CopperStore,
        net: u32,
        layer: usize,
        work: &WorkBudget,
    ) -> Result<Option<u64>, Failure> {
        use crate::spatial::SweptAabb;
        use std::ops::ControlFlow;
        let Some(via) = self.layer_change else {
            return Ok(None);
        };
        let mut a = [0; 2];
        let mut b = [0; 2];
        for k in 0..2 {
            a[k] = self.target.0[k].clamp(self.source.0[k], self.source.1[k]);
            b[k] = a[k].clamp(self.target.0[k], self.target.1[k]);
        }
        if a == b {
            return Ok(None);
        }
        let steps: [u64; 2] = std::array::from_fn(|k| a[k].abs_diff(b[k]).div_ceil(self.grid));
        let crossing = (steps[0] + steps[1])
            .saturating_mul(self.step)
            .saturating_add(via.saturating_mul(2));
        let found = copper.visit_generated_layer_candidates(
            layer,
            SweptAabb::swept(a, b, 0, 0),
            Some(net),
            work,
            |_, t| {
                if let Some(wall) = Barrier::from_segment(t.start, t.end) {
                    work.charge(1)?;
                    let detour = wall.detour(self.source, self.target).div_ceil(self.grid);
                    if (steps[wall.axis] + detour).saturating_mul(self.step) > crossing {
                        return Ok(ControlFlow::Break(()));
                    }
                }
                Ok(ControlFlow::Continue(()))
            },
        )?;
        Ok(found.is_break().then_some(crossing))
    }

    fn retain_layer_wall(&mut self, layer: usize, wall: Barrier) {
        let axis = wall.axis;
        let j = 1 - axis;
        let baseline = interval_gap(
            self.source.0[j],
            self.source.1[j],
            self.target.0[j],
            self.target.1[j],
        );
        let prior =
            self.layers[layer][axis].map_or(baseline, |b| b.detour(self.source, self.target));
        if wall.lo < wall.hi && wall.detour(self.source, self.target) > prior {
            self.layers[layer][axis] = Some(wall);
            self.active = true;
        }
    }

    /// Each required crossing may be traversed on any open layer. The set
    /// carries every layer reachable with the lowest number of changes seen
    /// so far. If none is open at the next crossing, one change reaches any
    /// of its open layers. Permitting a direct change between every pair is a
    /// relaxation for blind and microvias too. A path must meet the cuts on
    /// both axes, but the same change can serve both sequences.
    fn crossing_changes(
        &self,
        p: Point,
        layer: usize,
        target: Envelope,
        terminals: u32,
        work: &WorkBudget,
    ) -> Result<u64, Failure> {
        let mut lower = 0;
        for (axis, cuts) in self.crossing_layers.iter().enumerate() {
            if cuts.is_empty() {
                continue;
            }
            let mut possible = 1u32 << layer;
            let mut changes = 0;
            let mut cross = |(&position, &allowed): (&i64, &u32)| -> Result<(), Failure> {
                work.charge(1)?;
                let between = p[axis] < position && position < target.0[axis]
                    || target.1[axis] < position && position < p[axis];
                if between && allowed != 0 {
                    if possible & allowed == 0 {
                        changes += 1;
                        possible = allowed;
                    } else {
                        possible &= allowed;
                    }
                }
                Ok(())
            };
            if p[axis] <= target.0[axis] {
                for cut in cuts {
                    cross(cut)?
                }
            } else {
                for cut in cuts.iter().rev() {
                    cross(cut)?
                }
            }
            lower = lower.max(changes + u64::from(possible & terminals == 0));
        }
        Ok(lower)
    }

    pub(crate) fn necessary_vias(
        &self,
        at: Point,
        layer: usize,
        terminals: u32,
        work: &WorkBudget,
    ) -> Result<u64, Failure> {
        self.crossing_changes(at, layer, self.target, terminals, work)
    }

    pub(crate) fn is_active(&self) -> bool {
        self.active
    }

    /// A via that can finish directly on target copper may terminate on a
    /// different layer. Leave every target layer open in that case.
    pub(crate) fn with_terminal_layers(mut self, source: u32, target: u32) -> Self {
        self.source_layers = source;
        self.target_layers = target;
        self
    }

    pub(crate) fn estimate(
        &self,
        p: Point,
        layer: usize,
        work: &WorkBudget,
    ) -> Result<u64, Failure> {
        self.toward(p, layer, self.target, false, work)
    }

    pub(crate) fn estimate_reverse(
        &self,
        p: Point,
        layer: usize,
        work: &WorkBudget,
    ) -> Result<u64, Failure> {
        self.toward(p, layer, self.source, true, work)
    }

    fn toward(
        &self,
        p: Point,
        layer: usize,
        target: Envelope,
        backward: bool,
        work: &WorkBudget,
    ) -> Result<u64, Failure> {
        work.charge(1)?;
        let steps: [u64; 2] = std::array::from_fn(|k| {
            interval_gap(p[k], p[k], target.0[k], target.1[k]).div_ceil(self.grid)
        });
        let base = steps[0].saturating_add(steps[1]).saturating_mul(self.step);
        let planar_bound = |barriers: &[Option<Barrier>; 2]| {
            let mut h = base;
            for b in barriers.iter().flatten() {
                work.charge(1)?;
                // Round once per axis over the entire relaxed path. Rounding
                // each leg separately could overcharge a clipped final edge.
                let turns = b.detour((p, p), target).div_ceil(self.grid);
                h = h.max(
                    steps[b.axis]
                        .saturating_add(turns)
                        .saturating_mul(self.step),
                );
            }
            Ok::<_, Failure>(h)
        };
        let mut local = planar_bound(&self.layers[layer])?;
        if let Some(wall) = self.slanted[layer] {
            work.charge(8)?;
            local = local.max(
                wall.distance((p, p), target)
                    .div_ceil(self.grid)
                    .saturating_mul(self.step),
            );
        }
        let terminal_layers = if backward {
            self.source_layers
        } else {
            self.target_layers
        };
        let current = 1u32 << layer;
        let escaped = match self.layer_change {
            None => local,
            Some(via) if terminal_layers & current == 0 => base.saturating_add(via),
            Some(via) => {
                // With just one terminal layer, a path that leaves it to
                // avoid a barrier must return before making planar contact.
                let transitions = if terminal_layers == current { 2 } else { 1 };
                local.min(base.saturating_add(via.saturating_mul(transitions)))
            }
        };
        let mut bound = base;
        for axis in 0..2 {
            let travel = if let Some(ordered) = &self.ordered[axis] {
                let profile = if backward {
                    &ordered.reverse
                } else {
                    &ordered.forward
                };
                profile.estimate(axis, p, target, work)?
            } else if let Some(wall) = self.through_stack[axis] {
                work.charge(1)?;
                wall.detour((p, p), target)
            } else {
                continue;
            };
            // Round transverse travel once. Each physical planar edge moves
            // one axis by at most one grid pitch, including clipped contacts.
            bound = bound.max(
                steps[axis]
                    .saturating_add(travel.div_ceil(self.grid))
                    .saturating_mul(self.step),
            );
        }
        if let Some(via) = self.layer_change {
            let changes = self.crossing_changes(p, layer, target, terminal_layers, work)?;
            bound = bound.saturating_add(changes.saturating_mul(via));
        }
        Ok(bound.max(escaped))
    }
}

#[cfg(test)]
mod tests;
