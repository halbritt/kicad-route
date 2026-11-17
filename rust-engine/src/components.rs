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
    /// First integer endpoint in the convex contact interval along an edge.
    /// A prefix capsule touches iff the edge has entered that interval, so the
    /// exact swept-contact predicate is monotone in prefix length. Bisection
    /// locates its first integer length; the final cap check rejects an interval
    /// lying entirely between integer endpoints. No square roots or tolerance.
    pub(crate) fn attachment(self, a: Point, b: Point, net: u32, width: i64) -> Option<Point> {
        let k = if a[0] != b[0] { 0 } else { 1 };
        let length = (b[k] - a[k]).abs();
        if length == 0 {
            return None;
        }
        let point = |distance| {
            let mut p = a;
            p[k] += distance * (b[k] - a[k]).signum();
            p
        };
        let route = |start, end| Track {
            id: String::new(),
            net,
            start,
            end,
            width_nm: width,
            locked: false,
        };
        if !self.touches_route(&route(a, b)) {
            return None;
        }
        let mut lo = 1;
        let mut hi = length;
        while lo < hi {
            let mid = lo + (hi - lo) / 2;
            if self.touches_route(&route(a, point(mid))) {
                hi = mid;
            } else {
                lo = mid + 1;
            }
        }
        let p = point(lo);
        self.touches_route(&route(p, p)).then_some(p)
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
