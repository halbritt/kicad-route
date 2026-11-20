//! A straight copper segment can separate two terminal envelopes at any angle.
use super::{interval_gap, Envelope, Point};
use crate::budget::WorkBudget;
use crate::geometry::{intersects, orientation};
use crate::model::Failure;

#[derive(Clone, Copy)]
pub(super) struct SlantedWall {
    start: Point,
    end: Point,
}

fn closest(a: Envelope, b: Envelope) -> (Point, Point) {
    let p: Point = std::array::from_fn(|i| b.0[i].clamp(a.0[i], a.1[i]));
    let q = std::array::from_fn(|i| p[i].clamp(b.0[i], b.1[i]));
    (p, q)
}

fn distance(p: Point, region: Envelope) -> u64 {
    (0..2)
        .map(|k| interval_gap(p[k], p[k], region.0[k], region.1[k]))
        .sum()
}

impl SlantedWall {
    // Orientation varies linearly across a rectangle. Its extrema lie at
    // opposing corners selected by the signs of the line's normal.
    fn side(self, region: Envelope) -> i8 {
        let normal = [self.start[1] - self.end[1], self.end[0] - self.start[0]];
        let lo = std::array::from_fn(|i| {
            if normal[i] >= 0 {
                region.0[i]
            } else {
                region.1[i]
            }
        });
        let hi = std::array::from_fn(|i| {
            if normal[i] >= 0 {
                region.1[i]
            } else {
                region.0[i]
            }
        });
        if orientation(self.start, self.end, lo) > 0 {
            1
        } else if orientation(self.start, self.end, hi) < 0 {
            -1
        } else {
            0
        }
    }

    // Every path between opposite sides crosses the supporting line. Along
    // that line the sum of distances to the two rectangles is convex. Its
    // minimum occurs where the segment connecting their closest points meets
    // the line. If this point lies on the wall, the minimum on either free
    // ray therefore occurs at that end of the wall.
    pub(super) fn distance(self, from: Envelope, to: Envelope) -> u64 {
        let side = self.side(from);
        if side == 0 || side != -self.side(to) {
            return 0;
        }
        let (p, q) = closest(from, to);
        if !intersects(p, q, self.start, self.end) {
            return 0;
        }
        let via_tip = |tip| distance(tip, from) + distance(tip, to);
        via_tip(self.start).min(via_tip(self.end))
    }

    pub(super) fn consider(
        slot: &mut Option<Self>,
        a: Point,
        b: Point,
        source: Envelope,
        target: Envelope,
        work: &WorkBudget,
    ) -> Result<bool, Failure> {
        if a[0] == b[0] || a[1] == b[1] {
            return Ok(false);
        }
        work.charge(8)?;
        let candidate = Self { start: a, end: b };
        let detour = candidate.distance(source, target);
        let (p, q) = closest(source, target);
        let direct = p[0].abs_diff(q[0]) + p[1].abs_diff(q[1]);
        if detour <= direct {
            return Ok(false);
        }
        if let Some(held) = slot {
            work.charge(8)?;
            if detour <= held.distance(source, target) {
                return Ok(false);
            }
        }
        *slot = Some(candidate);
        Ok(true)
    }
}
