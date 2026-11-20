//! Rectilinear lower bounds from fixed layer-specific barriers. Other geometry is
//! removed in this relaxation. No choice of barrier can exclude a physical route.

mod pad_banks;
pub(crate) use pad_banks::PadBanks;

use crate::budget::WorkBudget;
use crate::model::{Failure, Input, Point, RuleArea, Track};
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
    fn from_track(track: &Track) -> Option<Self> {
        let axis = (0..2)
            .find(|&k| track.start[k] == track.end[k] && track.start[1 - k] != track.end[1 - k])?;
        Some(Self {
            axis,
            cut: track.start[axis],
            lo: track.start[1 - axis].min(track.end[1 - axis]),
            hi: track.start[1 - axis].max(track.end[1 - axis]),
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

/// A ring with four alternating axis-aligned nonempty sides bounds its full
/// rectangle. An optional repeated closing vertex does not change the ring.
fn solid_rectangle(area: &RuleArea) -> Option<Envelope> {
    if !area.no_tracks || !area.holes.is_empty() || !area.extra_polygons.is_empty() {
        return None;
    }
    let p = match area.outer.as_slice() {
        [a, b, c, d] => [a, b, c, d],
        [a, b, c, d, end] if a == end => [a, b, c, d],
        _ => return None,
    };
    let lo = [p[0][0].min(p[2][0]), p[0][1].min(p[2][1])];
    let hi = [p[0][0].max(p[2][0]), p[0][1].max(p[2][1])];
    if (0..2).any(|k| lo[k].abs_diff(hi[k]) < 2) {
        return None;
    }
    let corners = [[p[0][0], p[2][1]], [p[2][0], p[0][1]]];
    ((*p[1] == corners[0] && *p[3] == corners[1]) || (*p[1] == corners[1] && *p[3] == corners[0]))
        .then_some((lo, hi))
}

pub(crate) struct BarrierBound {
    source: Envelope,
    target: Envelope,
    layers: Vec<[Option<Barrier>; 2]>,
    through_stack: [Option<Barrier>; 2],
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
        work.search_storage(layers.len() * 2)?;
        let mut retained = vec![[None; 2]; layers.len()];
        let mut scores = vec![[0; 2]; layers.len()];
        let mut through_stack = [None; 2];
        let mut through_scores = [0; 2];
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
                if let Some(b) = Barrier::from_track(t) {
                    if let Some(k) = layer_index(&t.layer)? {
                        consider(&mut retained[k], &mut scores[k], b);
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
            if let Some(rect) = solid_rectangle(area) {
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
        Ok(Self {
            source,
            target,
            layers: retained,
            through_stack,
            ordered,
            grid: grid_nm as u64,
            step: minimum_step,
            layer_change: (layers.len() > 1).then_some(via_cost),
            active,
        })
    }

    pub(crate) fn is_active(&self) -> bool {
        self.active
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
        let local = planar_bound(&self.layers[layer])?;
        // A via may bypass all layer-local barriers in one transition, but
        // its center never moves. Any through-stack divider must still be
        // passed before reaching the (via-radius-expanded) goal envelope.
        let escaped = self
            .layer_change
            .map_or(local, |cost| local.min(base.saturating_add(cost)));
        let mut bound = escaped;
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
        Ok(bound)
    }
}

#[cfg(test)]
mod tests;
