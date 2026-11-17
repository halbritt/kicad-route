//! Connected components of supported copper, using the same conservative
//! contact predicate for incumbent and generated items. These are borrowed
//! graph nodes, not copies of board state.
use crate::{
    geometry,
    model::{Pad, Point, Track},
};

#[derive(Clone, Copy)]
pub(crate) enum Copper<'a> {
    Pad(&'a Pad),
    Track(&'a Track),
}
impl Copper<'_> {
    fn touches(self, other: Self) -> bool {
        match (self, other) {
            (Self::Pad(a), Self::Pad(b)) => geometry::pads_touch(a, b),
            (Self::Track(t), Self::Pad(p)) | (Self::Pad(p), Self::Track(t)) => {
                geometry::track_connects_pad(t, p)
            }
            (Self::Track(a), Self::Track(b)) => geometry::tracks_connect(a, b),
        }
    }
    pub(crate) fn touches_route(self, route: &Track) -> bool {
        match self {
            Self::Pad(p) => geometry::track_connects_pad(route, p),
            Self::Track(t) => geometry::tracks_connect(route, t),
        }
    }
    /// Snap the final orthogonal edge to an integer point nearest the target's
    /// center geometry. Verify contact exactly after rounding; do not publish
    /// a full grid step extending past the target as a dangling spur.
    pub(crate) fn attachment(self, a: Point, b: Point, net: u32, width: i64) -> Option<Point> {
        let k = if a[0] != b[0] { 0 } else { 1 };
        let j = 1 - k;
        let lo = a[k].min(b[k]);
        let hi = a[k].max(b[k]);
        let mut coordinates = Vec::new();
        match self {
            Self::Pad(p) => coordinates.push(p.at[k]),
            Self::Track(t) => {
                coordinates.extend([t.start[k], t.end[k]]);
                let delta = i128::from(t.end[j]) - i128::from(t.start[j]);
                if delta != 0 {
                    let offset = i128::from(a[j]) - i128::from(t.start[j]);
                    if (delta > 0 && offset >= 0 && offset <= delta)
                        || (delta < 0 && offset <= 0 && offset >= delta)
                    {
                        let numerator = i128::from(t.start[k]) * delta
                            + (i128::from(t.end[k]) - i128::from(t.start[k])) * offset;
                        let floor = if delta > 0 {
                            numerator.div_euclid(delta)
                        } else {
                            (-numerator).div_euclid(-delta)
                        };
                        coordinates.extend([floor as i64, (floor + 1) as i64]);
                    }
                }
            }
        }
        // A one-nm segment can be needed when the closest point is the seed
        // itself: zero-length copper is not a valid output object.
        coordinates.push(a[k] + (b[k] - a[k]).signum());
        coordinates.sort_by_key(|v| ((*v).clamp(lo, hi) - a[k]).abs());
        for coordinate in coordinates {
            let mut p = a;
            p[k] = coordinate.clamp(lo, hi);
            if p == a {
                continue;
            }
            let cap = Track {
                id: String::new(),
                net,
                start: p,
                end: p,
                width_nm: width,
                locked: false,
            };
            if self.touches_route(&cap) {
                return Some(p);
            }
        }
        None
    }
    pub(crate) fn anchors(self) -> Vec<Point> {
        match self {
            Self::Pad(p) => vec![p.at],
            Self::Track(t) => vec![t.start, t.end],
        }
    }
}

pub(crate) fn partition(items: Vec<Copper<'_>>) -> Vec<Vec<Copper<'_>>> {
    let mut seen = vec![false; items.len()];
    let mut groups = Vec::new();
    for root in 0..items.len() {
        if seen[root] {
            continue;
        }
        seen[root] = true;
        let mut pending = vec![root];
        let mut group = Vec::new();
        while let Some(i) = pending.pop() {
            group.push(items[i]);
            for j in 0..items.len() {
                if !seen[j] && items[i].touches(items[j]) {
                    seen[j] = true;
                    pending.push(j);
                }
            }
        }
        groups.push(group);
    }
    groups
}
