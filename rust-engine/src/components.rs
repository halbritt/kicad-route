//! Connected components of supported copper. Imported junctions keep native
//! contact semantics. New tracks must reach the copper body they join.
//! Nodes borrow the authoritative board state.
use crate::{
    budget::WorkBudget,
    geometry,
    model::{CopperGraphic, Failure, GraphicShape, OutlinePolygon, Pad, Point, Track, Via, Zone},
    zones::{resolve_pad_zone_connection, ResolvedPadZoneConnection},
};

pub(crate) fn layer_bit(stackup: &[String], layer: &str) -> u32 {
    stackup
        .iter()
        .position(|l| l == layer)
        .map_or(0, |idx| 1u32 << idx)
}

pub(crate) fn span_mask(start_idx: usize, end_idx: usize) -> u32 {
    let mut m = 0u32;
    for i in start_idx..=end_idx {
        if i < 32 {
            m |= 1u32 << i;
        }
    }
    m
}

#[derive(Clone, Copy)]
pub(crate) enum Copper<'a> {
    Pad(&'a Pad),
    Track(&'a Track),
    Graphic(&'a CopperGraphic),
    Via(&'a Via),
    ZoneFill {
        layer_idx: usize,
        zone: &'a Zone,
        poly: &'a OutlinePolygon,
    },
}
impl<'a> Copper<'a> {
    pub(crate) fn refillable_surface(self) -> Option<(usize, &'a str)> {
        match self {
            Self::ZoneFill {
                layer_idx, zone, ..
            } if zone.refillable => Some((layer_idx, zone.id.as_str())),
            _ => None,
        }
    }

    pub(crate) fn copper_bounds(self) -> (Point, Point) {
        match self {
            Self::Pad(p) => p.copper_bounds(),
            Self::Track(t) => t.copper_bounds(),
            Self::Graphic(g) => g.copper_bounds(),
            Self::Via(v) => {
                let r = (v.diameter_nm + 1) / 2;
                ([v.at[0] - r, v.at[1] - r], [v.at[0] + r, v.at[1] + r])
            }
            Self::ZoneFill { poly, .. } => {
                let mut lo = [i64::MAX, i64::MAX];
                let mut hi = [i64::MIN, i64::MIN];
                for p in &poly.outer {
                    lo[0] = lo[0].min(p[0]);
                    lo[1] = lo[1].min(p[1]);
                    hi[0] = hi[0].max(p[0]);
                    hi[1] = hi[1].max(p[1]);
                }
                (lo, hi)
            }
        }
    }

    pub(crate) fn layer_mask(self, stackup: &[String]) -> u32 {
        match self {
            Self::Pad(p) => {
                if p.layers.is_empty() {
                    layer_bit(stackup, "F.Cu")
                } else {
                    p.layers.iter().fold(0u32, |m, l| m | layer_bit(stackup, l))
                }
            }
            Self::Track(t) => layer_bit(stackup, &t.layer),
            Self::Graphic(g) => layer_bit(stackup, &g.layer),
            Self::Via(v) => {
                let Some(s) = stackup.iter().position(|l| l == &v.start_layer) else {
                    return 0;
                };
                let Some(e) = stackup.iter().position(|l| l == &v.end_layer) else {
                    return 0;
                };
                if s <= e {
                    span_mask(s, e)
                } else {
                    0
                }
            }
            Self::ZoneFill { layer_idx, .. } => {
                if layer_idx < 32 {
                    1u32 << layer_idx
                } else {
                    0
                }
            }
        }
    }

    fn primitive_count(self) -> u64 {
        match self {
            Self::Pad(p) => p.shape.parts().len() as u64,
            _ => 1,
        }
    }

    pub(crate) fn touches(
        self,
        other: Self,
        stackup: &[String],
        both_from_input: impl Fn(&str, &str) -> bool,
    ) -> bool {
        if (self.layer_mask(stackup) & other.layer_mask(stackup)) == 0 {
            return false;
        }
        match (self, other) {
            (Self::Pad(a), Self::Pad(b)) => geometry::pads_touch(a, b),
            (Self::Track(t), Self::Pad(p)) | (Self::Pad(p), Self::Track(t)) => {
                geometry::track_connects_pad(t, p)
            }
            (Self::Track(a), Self::Track(b)) => {
                geometry::tracks_land_together(a, b)
                    || (geometry::tracks_connect(a, b) && both_from_input(&a.id, &b.id))
            }
            (Self::Graphic(g), Self::Track(t)) | (Self::Track(t), Self::Graphic(g)) => {
                geometry::track_connects_graphic(t, g)
            }
            (Self::Graphic(g), Self::Pad(p)) | (Self::Pad(p), Self::Graphic(g)) => {
                geometry::graphic_connects_pad(g, p)
            }
            (Self::Graphic(a), Self::Graphic(b)) => geometry::graphics_connect(a, b),
            (Self::Via(v), Self::Pad(p)) | (Self::Pad(p), Self::Via(v)) => {
                geometry::via_connects_pad(v, p)
            }
            (Self::Via(v), Self::Track(t)) | (Self::Track(t), Self::Via(v)) => {
                geometry::via_anchored_on_track(v, t)
                    || (geometry::track_connects_via(t, v) && both_from_input(&t.id, &v.id))
            }
            (Self::Via(v), Self::Graphic(g)) | (Self::Graphic(g), Self::Via(v)) => {
                geometry::via_connects_graphic(v, g)
            }
            (Self::Via(a), Self::Via(b)) => geometry::vias_connect(a, b),
            (Self::ZoneFill { zone, poly, .. }, Self::Pad(p))
            | (Self::Pad(p), Self::ZoneFill { zone, poly, .. }) => {
                resolve_pad_zone_connection(p, zone) != ResolvedPadZoneConnection::None
                    && geometry::zone_fill_connects_pad(&poly.outer, &poly.holes, p)
            }
            (Self::ZoneFill { poly, .. }, Self::Track(t))
            | (Self::Track(t), Self::ZoneFill { poly, .. }) => {
                geometry::track_connects_filled_polygon(t, &poly.outer, &poly.holes)
            }
            (Self::ZoneFill { poly, .. }, Self::Via(v))
            | (Self::Via(v), Self::ZoneFill { poly, .. }) => {
                geometry::via_connects_filled_polygon(v, &poly.outer, &poly.holes)
            }
            (Self::ZoneFill { poly, .. }, Self::Graphic(g))
            | (Self::Graphic(g), Self::ZoneFill { poly, .. }) => {
                geometry::graphic_connects_filled_polygon(g, &poly.outer, &poly.holes)
            }
            (Self::ZoneFill { poly: pa, .. }, Self::ZoneFill { poly: pb, .. }) => {
                geometry::filled_polygons_overlap(&pa.outer, &pa.holes, &pb.outer, &pb.holes)
            }
        }
    }

    pub(crate) fn touches_route(
        self,
        route: &Track,
        stackup: &[String],
        work: &WorkBudget,
    ) -> Result<bool, Failure> {
        work.charge(self.primitive_count())?;
        if (self.layer_mask(stackup) & layer_bit(stackup, &route.layer)) == 0 {
            return Ok(false);
        }
        Ok(match self {
            Self::Pad(p) => geometry::track_connects_pad(route, p),
            Self::Track(t) => geometry::tracks_land_together(route, t),
            Self::Graphic(g) => geometry::track_connects_graphic(route, g),
            Self::Via(v) => geometry::track_lands_on_via(route, v),
            Self::ZoneFill { poly, .. } => {
                geometry::track_connects_filled_polygon(route, &poly.outer, &poly.holes)
            }
        })
    }

    pub(crate) fn touches_via(
        self,
        via: &Via,
        stackup: &[String],
        work: &WorkBudget,
    ) -> Result<bool, Failure> {
        work.charge(self.primitive_count())?;
        if (self.layer_mask(stackup) & Copper::Via(via).layer_mask(stackup)) == 0 {
            return Ok(false);
        }
        Ok(match self {
            Self::Pad(p) => geometry::via_connects_pad(via, p),
            Self::Track(t) => geometry::via_anchored_on_track(via, t),
            Self::Graphic(g) => geometry::via_connects_graphic(via, g),
            Self::Via(v) => geometry::vias_connect(via, v),
            Self::ZoneFill { poly, .. } => {
                geometry::via_connects_filled_polygon(via, &poly.outer, &poly.holes)
            }
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
        layer: &str,
        stackup: &[String],
        work: &WorkBudget,
    ) -> Result<Option<Point>, Failure> {
        if (self.layer_mask(stackup) & layer_bit(stackup, layer)) == 0 {
            return Ok(None);
        }
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
            layer: layer.to_owned(),
        };
        if !self.touches_route(&route(a, b), stackup, work)? {
            return Ok(None);
        }
        let mut lo = 1;
        let mut hi = length;
        while lo < hi {
            let mid = lo + (hi - lo) / 2;
            if self.touches_route(&route(a, point(mid)), stackup, work)? {
                hi = mid;
            } else {
                lo = mid + 1;
            }
        }
        let p = point(lo);
        Ok(self
            .touches_route(&route(p, p), stackup, work)?
            .then_some(p))
    }
    pub(crate) fn anchors(self, bounds_min: Point, grid_nm: i64) -> Vec<Point> {
        let grid_box_around = |c: Point, extra: Option<Point>| {
            let mut pts = vec![c];
            if let Some(alt) = extra {
                if alt != c {
                    pts.push(alt);
                }
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
        };
        match self {
            Self::Pad(p) => grid_box_around(p.copper_center(), Some(p.at)),
            Self::Via(v) => grid_box_around(v.at, None),
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
            Self::ZoneFill { poly, .. } => {
                let mut pts = Vec::new();
                let n = poly.outer.len();
                if n > 0 {
                    for idx in [0, n / 4, n / 2, (3 * n) / 4] {
                        for pt in grid_box_around(poly.outer[idx], None) {
                            if !pts.contains(&pt) {
                                pts.push(pt);
                            }
                        }
                    }
                }
                pts
            }
        }
    }
}

pub(crate) fn partition<'a>(
    items: Vec<Copper<'a>>,
    stackup: &[String],
    work: &WorkBudget,
    both_from_input: impl Fn(&str, &str) -> bool,
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
                work.charge(items[i].primitive_count() * items[j].primitive_count())?;
                if items[i].touches(items[j], stackup, &both_from_input) {
                    seen[j] = true;
                    pending.push(j);
                }
            }
        }
        groups.push(group);
    }
    Ok(groups)
}
