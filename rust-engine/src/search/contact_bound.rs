//! Lower bounds on the grid moves needed to contact supported copper.
use crate::budget::WorkBudget;
use crate::components::Copper;
use crate::geometry;
use crate::model::{Failure, Point};

fn isqrt_u128(n: u128) -> u128 {
    if n < 2 {
        return n;
    }
    let mut x = 1u128 << ((128 - n.leading_zeros()).div_ceil(2));
    loop {
        let next = (x + n / x) >> 1;
        if next >= x {
            return x;
        }
        x = next;
    }
}

/// The supporting line of a slanted trace or oval. A cardinal grid step
/// changes the signed cross product by at most grid * max(|dx|, |dy|).
struct SlantedCapsule {
    origin: Point,
    delta: Point,
    length_ceil: u64,
    radius: i64,
}

impl SlantedCapsule {
    fn for_copper(copper: Copper<'_>) -> Option<Self> {
        let (start, end, width) = match copper {
            Copper::Track(t) => (t.start, t.end, t.width_nm),
            Copper::Pad(p) => match p.shape {
                crate::model::Shape::Capsule {
                    start_nm,
                    end_nm,
                    width_nm,
                } => (start_nm, end_nm, width_nm),
                _ => return None,
            },
            _ => return None,
        };
        let delta: Point = std::array::from_fn(|k| end[k] - start[k]);
        if delta.contains(&0) {
            return None;
        }
        let squared = delta.iter().map(|v| i128::from(*v).pow(2) as u128).sum();
        let length = isqrt_u128(squared);
        Some(Self {
            origin: start,
            delta,
            length_ceil: (length + u128::from(length * length < squared)) as u64,
            radius: (width + 1) / 2,
        })
    }

    fn steps(&self, p: Point, contact_radius: i64, grid: i64) -> u64 {
        let [dx, dy] = self.delta.map(i128::from);
        let det = dx * i128::from(p[1] - self.origin[1]) - dy * i128::from(p[0] - self.origin[0]);
        // Round the supporting strip outward to include the full endpoint cap.
        let strip = u128::from(self.length_ceil) * (self.radius + contact_radius) as u128;
        let gap = det.unsigned_abs().saturating_sub(strip);
        gap.div_ceil(dx.unsigned_abs().max(dy.unsigned_abs()) * grid as u128) as u64
    }
}

/// Copper envelopes are invariant throughout a component join. Computing a
/// filled ring's bounds during every queue expansion would rescan its vertices.
pub(super) struct TargetBounds<'a> {
    pub(super) copper: Copper<'a>,
    pub(super) lo: Point,
    pub(super) hi: Point,
    pub(super) mask: u32,
    slanted: Option<SlantedCapsule>,
}

impl<'a> TargetBounds<'a> {
    pub(super) fn new(
        copper: Copper<'a>,
        stackup: &[String],
        work: &WorkBudget,
    ) -> Result<Self, Failure> {
        use crate::model::{GraphicShape, Shape};
        match copper {
            Copper::ZoneFill { poly, .. } => work.charge(poly.outer.len() as u64)?,
            Copper::Graphic(g) => match &g.shape {
                GraphicShape::Polygon { outer, holes, .. } => {
                    work.charge(outer.len() as u64)?;
                    for ring in holes {
                        work.charge(ring.len() as u64)?;
                    }
                }
                _ => (),
            },
            Copper::Pad(pad) => {
                let parts = pad.shape.parts();
                if parts.len() > 1 {
                    work.charge(parts.len() as u64)?;
                }
                for part in parts {
                    if let Shape::ConvexPolygon { vertices, .. } = part {
                        work.charge(vertices.len() as u64)?;
                    }
                }
            }
            _ => (),
        }
        let slanted = SlantedCapsule::for_copper(copper);
        if slanted.is_some() {
            work.charge(1)?;
        }
        let (lo, hi) = copper.copper_bounds();
        Ok(Self {
            copper,
            lo,
            hi,
            mask: copper.layer_mask(stackup),
            slanted,
        })
    }
    /// Lower bound to a superset of all endpoint-cap contacts with this target.
    /// Each orthogonal edge moves at most `grid` in one axis, including a shortened
    /// final edge. The sum of ceil(axis gap / grid) therefore cannot exceed the
    /// remaining step cost. Pad, track, and via contacts floor the route half-width
    /// and require one nm of overlap. Use their reachable endpoint bounds, so
    /// an edge that can only reach a tangent still needs one more step. For circular pads
    /// and vias, Cauchy-Schwarz also bounds `|dx| + |dy| <= isqrt(2 * R_sum^2)`.
    /// Ignoring obstacles and rounding outward can only decrease this bound. Each
    /// bound changes by at most one across an edge; the minimum over all targets is
    /// also consistent and is zero at every accepted contact endpoint.
    pub(super) fn contact_steps(&self, p: Point, width: i64, grid: i64) -> u64 {
        let route_radius = match self.copper {
            Copper::Pad(_) | Copper::Track(_) | Copper::Via(_) => {
                (width / 2 - geometry::CONTACT_OVERLAP_NM).max(0)
            }
            Copper::Graphic(_) | Copper::ZoneFill { .. } => (width + 1) / 2,
        };
        let circle = match self.copper {
            Copper::Pad(pad) => match pad.shape {
                crate::model::Shape::Circle { radius_nm } => Some((pad.at, radius_nm)),
                _ => None,
            },
            Copper::Via(via) => Some((via.at, (via.diameter_nm + 1) / 2)),
            _ => None,
        };
        let lo: Point = std::array::from_fn(|k| self.lo[k] - route_radius);
        let hi: Point = std::array::from_fn(|k| self.hi[k] + route_radius);
        let aabb_steps: u64 = (0..2)
            .map(|k| {
                let gap = (lo[k] - p[k]).max(p[k] - hi[k]).max(0);
                ((gap + grid - 1) / grid) as u64
            })
            .sum();
        if let Some((center, rad)) = circle {
            let r_sum = u128::try_from(rad.max(0) + route_radius.max(0)).unwrap_or(0);
            let l1_limit = isqrt_u128(2 * r_sum * r_sum);
            let l1_dist = (p[0] - center[0]).abs() as u128 + (p[1] - center[1]).abs() as u128;
            if l1_dist > l1_limit {
                let gap = l1_dist - l1_limit;
                let g = grid as u128;
                let diag_steps = ((gap + g - 1) / g) as u64;
                return aabb_steps.max(diag_steps);
            }
        }
        self.slanted.as_ref().map_or(aabb_steps, |line| {
            aabb_steps.max(line.steps(p, route_radius, grid))
        })
    }
}

#[cfg(test)]
pub(super) fn contact_steps(p: Point, target: Copper<'_>, width: i64, grid: i64) -> u64 {
    let (lo, hi) = target.copper_bounds();
    TargetBounds {
        copper: target,
        slanted: SlantedCapsule::for_copper(target),
        lo,
        hi,
        mask: 0,
    }
    .contact_steps(p, width, grid)
}
