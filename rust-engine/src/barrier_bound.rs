//! Rectilinear lower bounds from fixed layer-specific barriers. Other geometry is
//! removed in this relaxation. No choice of barrier can exclude a physical route.

use crate::budget::WorkBudget;
use crate::model::{Failure, Input, Point, RuleArea, Track};

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
        let mut active = false;
        let mut consider = |layer_name: &str, b: Barrier| -> Result<(), Failure> {
            let j = 1 - b.axis;
            let base = interval_gap(source.0[j], source.1[j], target.0[j], target.1[j]);
            let extra = b.detour(source, target).saturating_sub(base);
            if extra == 0 {
                return Ok(());
            }
            for (k, name) in layers.iter().enumerate() {
                work.charge(1)?;
                if name == layer_name {
                    if extra > scores[k][b.axis] {
                        scores[k][b.axis] = extra;
                        retained[k][b.axis] = Some(b);
                        active = true;
                    }
                    break;
                }
            }
            Ok(())
        };
        for t in tracks {
            work.charge(1)?;
            if t.net != net {
                if let Some(b) = Barrier::from_track(t) {
                    consider(&t.layer, b)?;
                }
            }
        }
        for area in &input.rule_areas {
            work.charge(1)?;
            if let Some((lo, hi)) = solid_rectangle(area) {
                for layer in &area.layers {
                    work.charge(1)?;
                    for k in 0..2 {
                        consider(
                            layer,
                            Barrier {
                                axis: k,
                                cut: lo[k].midpoint(hi[k]),
                                lo: lo[1 - k],
                                hi: hi[1 - k],
                            },
                        )?;
                    }
                }
            }
        }
        Ok(Self {
            source,
            target,
            layers: retained,
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
        self.toward(p, layer, self.target, work)
    }

    pub(crate) fn estimate_reverse(
        &self,
        p: Point,
        layer: usize,
        work: &WorkBudget,
    ) -> Result<u64, Failure> {
        self.toward(p, layer, self.source, work)
    }

    fn toward(
        &self,
        p: Point,
        layer: usize,
        target: Envelope,
        work: &WorkBudget,
    ) -> Result<u64, Failure> {
        work.charge(1)?;
        let steps: [u64; 2] = std::array::from_fn(|k| {
            interval_gap(p[k], p[k], target.0[k], target.1[k]).div_ceil(self.grid)
        });
        let base = steps[0].saturating_add(steps[1]).saturating_mul(self.step);
        let mut h = base;
        for b in self.layers[layer].iter().flatten() {
            work.charge(1)?;
            // Round once per axis over the entire relaxed path. Rounding each
            // leg independently could overcharge a terminal clipped mid-step.
            let turns = b.detour((p, p), target).div_ceil(self.grid);
            let planar = steps[b.axis]
                .saturating_add(turns)
                .saturating_mul(self.step);
            h = h.max(planar);
        }
        // A via may bypass every planar barrier. The relaxed baseline is the
        // same on every layer, so this cap also ensures consistency across it.
        Ok(self
            .layer_change
            .map_or(h, |cost| h.min(base.saturating_add(cost))))
    }
}

#[cfg(test)]
mod tests;
