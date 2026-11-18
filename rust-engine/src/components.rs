//! Connected components of supported copper, using the same conservative
//! contact predicate for incumbent and generated items. These are borrowed
//! graph nodes, not copies of board state.
use crate::{
    budget::WorkBudget,
    geometry,
    model::{CopperGraphic, Failure, GraphicShape, Pad, Point, Track},
};

#[derive(Clone, Copy)]
pub(crate) enum Copper<'a> {
    Pad(&'a Pad),
    Track(&'a Track),
    Graphic(&'a CopperGraphic),
}
impl Copper<'_> {
    fn touches(self, other: Self) -> bool {
        match (self, other) {
            (Self::Pad(a), Self::Pad(b)) => geometry::pads_touch(a, b),
            (Self::Track(t), Self::Pad(p)) | (Self::Pad(p), Self::Track(t)) => {
                geometry::track_connects_pad(t, p)
            }
            (Self::Track(a), Self::Track(b)) => geometry::tracks_connect(a, b),
            (Self::Graphic(g), Self::Track(t)) | (Self::Track(t), Self::Graphic(g)) => {
                geometry::track_connects_graphic(t, g)
            }
            (Self::Graphic(g), Self::Pad(p)) | (Self::Pad(p), Self::Graphic(g)) => {
                geometry::graphic_connects_pad(g, p)
            }
            (Self::Graphic(a), Self::Graphic(b)) => geometry::graphics_connect(a, b),
        }
    }
    pub(crate) fn touches_route(self, route: &Track, work: &WorkBudget) -> Result<bool, Failure> {
        work.charge(1)?;
        Ok(match self {
            Self::Pad(p) => geometry::track_connects_pad(route, p),
            Self::Track(t) => geometry::tracks_connect(route, t),
            Self::Graphic(g) => geometry::track_connects_graphic(route, g),
        })
    }
    /// First integer endpoint in the convex contact interval along an edge.
    /// A prefix capsule touches iff the edge has entered that interval, so the
    /// exact swept-contact predicate is monotone in prefix length. Bisection
    /// locates its first integer length; the final cap check rejects an interval
    /// lying entirely between integer endpoints. No square roots or tolerance.
    pub(crate) fn attachment(
        self,
        a: Point,
        b: Point,
        net: u32,
        width: i64,
        work: &WorkBudget,
    ) -> Result<Option<Point>, Failure> {
        let k = if a[0] != b[0] { 0 } else { 1 };
        let length = (b[k] - a[k]).abs();
        if length == 0 {
            return Ok(None);
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
        if !self.touches_route(&route(a, b), work)? {
            return Ok(None);
        }
        let mut lo = 1;
        let mut hi = length;
        while lo < hi {
            let mid = lo + (hi - lo) / 2;
            if self.touches_route(&route(a, point(mid)), work)? {
                hi = mid;
            } else {
                lo = mid + 1;
            }
        }
        let p = point(lo);
        Ok(self.touches_route(&route(p, p), work)?.then_some(p))
    }
    pub(crate) fn anchors(self, bounds_min: Point, grid_nm: i64) -> Vec<Point> {
        match self {
            Self::Pad(p) => {
                let c = p.copper_center();
                let mut pts = vec![c];
                if p.at != c {
                    pts.push(p.at);
                }
                let gx_lo = bounds_min[0] + (c[0] - bounds_min[0]).div_euclid(grid_nm) * grid_nm;
                let gx_hi = if gx_lo == c[0] {
                    gx_lo
                } else {
                    gx_lo + grid_nm
                };
                let gy_lo = bounds_min[1] + (c[1] - bounds_min[1]).div_euclid(grid_nm) * grid_nm;
                let gy_hi = if gy_lo == c[1] {
                    gy_lo
                } else {
                    gy_lo + grid_nm
                };
                for gx in [gx_lo, gx_hi] {
                    for gy in [gy_lo, gy_hi] {
                        let pt = [gx, gy];
                        if !pts.contains(&pt) {
                            pts.push(pt);
                        }
                    }
                }
                pts
            }
            Self::Track(t) => vec![t.start, t.end],
            Self::Graphic(g) => match &g.shape {
                GraphicShape::Segment { start, end, .. } => vec![*start, *end],
                GraphicShape::Arc {
                    start, mid, end, ..
                } => vec![*start, *mid, *end],
                GraphicShape::Circle {
                    center,
                    radius_nm,
                    filled,
                    ..
                } => {
                    if *filled {
                        vec![*center]
                    } else {
                        vec![
                            [center[0] + radius_nm, center[1]],
                            [center[0] - radius_nm, center[1]],
                            [center[0], center[1] + radius_nm],
                            [center[0], center[1] - radius_nm],
                        ]
                    }
                }
                GraphicShape::Polygon { outer, .. } => outer.first().copied().into_iter().collect(),
            },
        }
    }
}

pub(crate) fn partition<'a>(
    items: Vec<Copper<'a>>,
    work: &WorkBudget,
) -> Result<Vec<Vec<Copper<'a>>>, Failure> {
    work.charge(items.len() as u64)?;
    let mut seen = vec![false; items.len()];
    let mut groups = Vec::new();
    for root in 0..items.len() {
        work.charge(1)?;
        if seen[root] {
            continue;
        }
        seen[root] = true;
        let mut pending = vec![root];
        let mut group = Vec::new();
        while let Some(i) = pending.pop() {
            work.charge(1)?;
            group.push(items[i]);
            for j in 0..items.len() {
                work.charge(1)?;
                if seen[j] {
                    continue;
                }
                work.charge(1)?;
                if items[i].touches(items[j]) {
                    seen[j] = true;
                    pending.push(j);
                }
            }
        }
        groups.push(group);
    }
    Ok(groups)
}
