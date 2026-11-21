//! Labels own immutable centerline prefixes. Final miter, via jog and fanout
//! validation can distinguish two paths even when their last steps agree.
use crate::budget::WorkBudget;
use crate::model::{Failure, Point};
use std::cmp::Reverse;
use std::collections::{BTreeMap, BinaryHeap};

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(super) enum Arrival {
    Straight,
    Turn,
    Via,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(super) struct Pose {
    pub gx: i32,
    pub gy: i32,
    pub li: u8,
    pub dir: u8,
    pub arrival: Arrival,
}

impl Pose {
    pub fn step(self, gx: i32, gy: i32, dir: u8) -> (Self, u64) {
        let turned = self.dir < 8 && self.dir != dir;
        let penalty = match (turned, self.arrival) {
            (false, _) => 0,
            (true, Arrival::Via) => 300_000,
            (true, _) => 25_000,
        };
        (
            Self {
                gx,
                gy,
                dir,
                arrival: if turned {
                    Arrival::Turn
                } else {
                    Arrival::Straight
                },
                ..self
            },
            penalty,
        )
    }

    pub fn can_start_via(self) -> bool {
        self.dir < 8 && self.arrival != Arrival::Via
    }

    pub fn via(self, li: u8, step_nm: i64) -> (Self, u64) {
        let penalty = if step_nm < 800_000 && self.arrival == Arrival::Turn {
            300_000
        } else {
            0
        };
        (
            Self {
                li,
                arrival: Arrival::Via,
                ..self
            },
            penalty,
        )
    }
}

/// Cheapest relaxed octilinear walk to the closed snap box. A snap tail and
/// any via performed by the tail builder have zero g-cost. Use the actual
/// rounded integer edge costs, including a layer with fractional weight.
pub(super) fn snap_bound(
    pt: Point,
    end: Point,
    radius: u64,
    step: u64,
    axial: u64,
    diagonal: u64,
) -> u64 {
    let steps = |i: usize| pt[i].abs_diff(end[i]).saturating_sub(radius).div_ceil(step);
    let (x, y) = (steps(0), steps(1));
    let both = x.min(y);
    // A diagonal can also advance just the larger remaining coordinate.
    let one = axial.min(diagonal);
    both.saturating_mul(diagonal.min(one.saturating_mul(2)))
        .saturating_add(x.abs_diff(y).saturating_mul(one))
}

#[derive(Clone, Copy)]
pub(super) struct Label {
    pub pose: Pose,
    pub parent: Option<usize>,
    pub g: u64,
    depth: usize,
}

// Lower bound first, then closer approach to the end fanout and stable pose
// and insertion order. Each heap entry references exactly one immutable path.
type Entry = (Reverse<u64>, Reverse<u64>, Pose, Reverse<usize>);

#[derive(Default)]
pub(super) struct Frontier {
    labels: Vec<Label>,
    preferred: BinaryHeap<Entry>,
    alternatives: BinaryHeap<Entry>,
    expanded: BTreeMap<Pose, u64>,
}

impl Frontier {
    fn entries(&self) -> usize {
        self.labels.len() + self.preferred.len() + self.alternatives.len() + self.expanded.len()
    }

    pub fn push(
        &mut self,
        work: &WorkBudget,
        pose: Pose,
        parent: Option<usize>,
        g: u64,
        bound: u64,
        approach: u64,
    ) -> Result<(), Failure> {
        work.search_storage(self.entries() + 2)?;
        work.charge(1)?;
        let depth = parent.map_or(1, |id| self.labels[id].depth + 1);
        let id = self.labels.len();
        self.labels.push(Label {
            pose,
            parent,
            g,
            depth,
        });
        let entry = (
            Reverse(g.saturating_add(bound)),
            Reverse(approach),
            pose,
            Reverse(id),
        );
        if self.expanded.get(&pose).is_some_and(|&prior| prior <= g) {
            self.alternatives.push(entry);
        } else {
            self.preferred.push(entry);
        }
        Ok(())
    }

    pub fn pop(
        &mut self,
        work: &WorkBudget,
        expand_alternative: bool,
    ) -> Result<Option<(usize, Label)>, Failure> {
        if expand_alternative && !self.alternatives.is_empty() {
            return self.pop_alternative(work);
        }
        while !self.preferred.is_empty() {
            work.charge(1)?;
            let entry @ (_, _, pose, Reverse(id)) = self.preferred.pop().unwrap();
            let label = self.labels[id];
            if self
                .expanded
                .get(&pose)
                .is_some_and(|&prior| prior <= label.g)
            {
                self.alternatives.push(entry);
                continue;
            }
            self.expanded.insert(pose, label.g);
            return Ok(Some((id, label)));
        }
        self.pop_alternative(work)
    }

    fn pop_alternative(&mut self, work: &WorkBudget) -> Result<Option<(usize, Label)>, Failure> {
        if self.alternatives.is_empty() {
            return Ok(None);
        }
        work.charge(1)?;
        let (_, _, _, Reverse(id)) = self.alternatives.pop().unwrap();
        Ok(Some((id, self.labels[id])))
    }

    pub fn path(&self, work: &WorkBudget, tail: usize) -> Result<Vec<Pose>, Failure> {
        let depth = self.labels[tail].depth;
        work.search_storage(self.entries() + depth)?;
        work.charge(2 * depth as u64)?;
        let mut chain = Vec::with_capacity(depth);
        let mut cursor = Some(tail);
        while let Some(id) = cursor {
            let label = self.labels[id];
            chain.push(label.pose);
            cursor = label.parent;
        }
        chain.reverse();
        Ok(chain)
    }
}

#[cfg(test)]
mod tests;
