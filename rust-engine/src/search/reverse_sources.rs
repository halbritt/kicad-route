use super::{BarrierBound, Failure, Point, WorkBudget};
use std::collections::BTreeMap;

// A reverse step moves exactly one grid unit or changes layer at the same
// point. It can reach only starts with the same pair of coordinate residues.
// Keep that subgraph's bound separate even when another branch is nearby.
#[derive(Default)]
pub(super) struct SourcePhase {
    starts: Vec<(Point, u8)>,
    boxes: Vec<(u8, ProjectedBox)>,
}

// The routing input bounds each coordinate to 100,000,000 nm. Each cardinal
// step changes either diagonal projection by at most one grid pitch.
fn source_projections([x, y]: Point) -> [i64; 4] {
    [x, y, x + y, x - y]
}

#[derive(Clone, Copy)]
struct ProjectedBox {
    lo: [i64; 4],
    hi: [i64; 4],
}

impl ProjectedBox {
    fn point(p: [i64; 4]) -> Self {
        Self { lo: p, hi: p }
    }

    fn include(&mut self, p: [i64; 4]) {
        for k in 0..4 {
            self.lo[k] = self.lo[k].min(p[k]);
            self.hi[k] = self.hi[k].max(p[k]);
        }
    }

    fn distance(&self, p: [i64; 4]) -> u64 {
        let gap = |k: usize| p[k].abs_diff(p[k].clamp(self.lo[k], self.hi[k]));
        (gap(0) + gap(1)).max(gap(2)).max(gap(3))
    }

    fn straight(&self) -> bool {
        (0..4).any(|k| self.lo[k] == self.hi[k])
    }
}

pub(super) struct ReverseSources<'a> {
    pub(super) barriers: Option<&'a BarrierBound>,
    pub(super) grid: i64,
    pub(super) phases: BTreeMap<Point, SourcePhase>,
}

impl<'a> ReverseSources<'a> {
    pub(super) fn new(
        starts: &[(Point, u8, bool)],
        grid: i64,
        work: &WorkBudget,
        goals: &[Point],
    ) -> Result<Self, Failure> {
        let mut phases = BTreeMap::<Point, SourcePhase>::new();
        let mut entries = starts.len();
        work.charge(entries as u64)?;
        work.search_storage(entries)?;
        for &(point, layer, _) in starts {
            let phase = point.map(|n| n.rem_euclid(grid));
            if !phases.contains_key(&phase) {
                entries += 1;
                work.search_storage(entries)?;
            }
            phases.entry(phase).or_default().starts.push((point, layer));
        }
        for source in phases.values_mut().filter(|p| p.starts.len() > 16) {
            let mut extents = BTreeMap::<u8, ProjectedBox>::new();
            work.charge(2 * source.starts.len() as u64)?;
            for &(at, layer) in &source.starts {
                if !extents.contains_key(&layer) {
                    entries += 1;
                    work.search_storage(entries)?;
                }
                let p = source_projections(at);
                extents
                    .entry(layer)
                    .or_insert_with(|| ProjectedBox::point(p))
                    .include(p);
            }
            let mut quadrants = BTreeMap::<(u8, u8), ProjectedBox>::new();
            if !goals.is_empty() && extents.values().any(|b| !b.straight()) {
                work.charge(2 * source.starts.len() as u64)?;
                for &(point, layer) in &source.starts {
                    let b = &extents[&layer];
                    if b.straight() {
                        continue;
                    }
                    let section = u8::from(point[0] > b.lo[0].midpoint(b.hi[0]))
                        + 2 * u8::from(point[1] > b.lo[1].midpoint(b.hi[1]));
                    if !quadrants.contains_key(&(layer, section)) {
                        entries += 1;
                        work.search_storage(entries)?;
                    }
                    let p = source_projections(point);
                    quadrants
                        .entry((layer, section))
                        .or_insert_with(|| ProjectedBox::point(p))
                        .include(p);
                }
            }
            for (layer, whole) in extents {
                let parts = quadrants.range((layer, 0)..=(layer, 3));
                let mut refine = false;
                for &goal in goals {
                    if whole.straight() {
                        break;
                    }
                    let p = source_projections(goal);
                    work.charge(10)?;
                    if parts
                        .clone()
                        .map(|(_, b)| b.distance(p))
                        .min()
                        .is_some_and(|d| d > whole.distance(p))
                    {
                        refine = true;
                        break;
                    }
                }
                if refine {
                    for (_, b) in parts {
                        entries += 1;
                        work.search_storage(entries)?;
                        source.boxes.push((layer, *b));
                    }
                } else {
                    entries += 1;
                    work.search_storage(entries)?;
                    source.boxes.push((layer, whole));
                }
                entries -= 1;
            }
            entries -= quadrants.len();
        }
        Ok(Self {
            grid,
            phases,
            barriers: None,
        })
    }

    pub(super) fn estimate(
        &self,
        at: Point,
        layer: u8,
        min_step: u64,
        via_cost: u64,
        work: &WorkBudget,
    ) -> Result<u64, Failure> {
        let phase = at.map(|n| n.rem_euclid(self.grid));
        let source = &self.phases[&phase];
        let steps = |p: Point, source_layer: u8| {
            let distance =
                at[0].abs_diff(p[0]) / self.grid as u64 + at[1].abs_diff(p[1]) / self.grid as u64;
            distance
                .saturating_mul(min_step)
                .saturating_add(if layer == source_layer { 0 } else { via_cost })
        };
        let best = if source.boxes.is_empty() {
            work.charge(source.starts.len() as u64)?;
            source.starts.iter().map(|&(p, l)| steps(p, l)).min()
        } else {
            work.charge(2 * source.boxes.len() as u64)?;
            let p = source_projections(at);
            source
                .boxes
                .iter()
                .map(|&(l, ref area)| {
                    (area.distance(p) / self.grid as u64)
                        .saturating_mul(min_step)
                        .saturating_add(if layer == l { 0 } else { via_cost })
                })
                .min()
        };
        let direct = best.expect("a reverse phase has at least one forward start");
        match self.barriers {
            Some(bounds) => Ok(direct.max(bounds.estimate_reverse(at, layer as usize, work)?)),
            None => Ok(direct),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Limits;

    #[test]
    fn concave_launch_tree_bound_respects_all_phase_layer_and_unit_edges() {
        let work = WorkBudget::new(Limits::default());
        let mut starts = Vec::new();
        for k in 0..=10 {
            starts.push(([0, 10 * k], 0, false));
            starts.push(([100, 10 * k], 0, false));
            starts.push(([10 * k, 100], 0, false));
        }
        starts.push(([0, 0], 1, false));
        starts.push(([51, 41], 0, false));
        let near = ReverseSources::new(&starts, 10, &work, &[[50, 40]]).unwrap();
        let broad = ReverseSources::new(&starts, 10, &work, &[]).unwrap();
        assert!(
            near.estimate([50, 40], 0, 1, 8, &work).unwrap()
                > broad.estimate([50, 40], 0, 1, 8, &work).unwrap()
        );
        for phase in [0, 1] {
            for x in -1..=11 {
                for y in -1..=11 {
                    let p = [10 * x + phase, 10 * y + phase];
                    for layer in 0..2 {
                        let h = near.estimate(p, layer, 1, 8, &work).unwrap();
                        let exact = starts
                            .iter()
                            .filter(|(q, _, _)| q.map(|n| n.rem_euclid(10)) == [phase; 2])
                            .map(|(q, l, _)| {
                                (p[0].abs_diff(q[0]) + p[1].abs_diff(q[1])) / 10
                                    + u64::from(*l != layer) * 8
                            })
                            .min()
                            .unwrap();
                        assert!(h <= exact);
                        for q in [
                            [p[0] + 10, p[1]],
                            [p[0] - 10, p[1]],
                            [p[0], p[1] + 10],
                            [p[0], p[1] - 10],
                        ] {
                            assert!(h <= 1 + near.estimate(q, layer, 1, 8, &work).unwrap());
                        }
                        assert!(h <= 8 + near.estimate(p, 1 - layer, 1, 8, &work).unwrap());
                    }
                }
            }
        }
    }
}
