//! Minimum-cost planar terminal legs from the ordinary multilayer search.
use super::{Copper, Engine, Failure, Path, PathEdge, State, TargetBounds};
use crate::diff_pair::octilinear_elbow_paths;
use crate::geometry;
use crate::model::{Pad, Point, Shape};

/// Find a planar landing whose L1 cost equals the smallest unsettled forward
/// priority minus its settled prefix. Each 45-degree grid step costs one step
/// per axis. Only the last grid cell can be shorter, as with a planar contact.
pub(super) fn from_state(
    engine: &Engine,
    net: u32,
    state: State,
    remaining: u64,
    targets: &[TargetBounds<'_>],
    finished: &impl Fn(crate::model::Point, usize, Option<PathEdge>) -> Result<bool, Failure>,
) -> Result<Option<Path>, Failure> {
    let layer = usize::from(state.layer);
    let rate = engine.layer_policy.step(layer, 0);
    let steps = remaining / rate;
    if steps < 2 || remaining % rate != 0 {
        return Ok(None);
    }
    let grid = engine.input.rules.grid_nm;
    let a = state.at;
    for target in targets {
        engine.work.charge(1)?;
        let Copper::Pad(pad) = target.copper else {
            continue;
        };
        if target.mask & (1 << layer) == 0 || pad.drill.is_some() {
            continue;
        }
        let center: [i64; 2] = std::array::from_fn(|k| target.lo[k].midpoint(target.hi[k]));
        let sign: [i64; 2] = std::array::from_fn(|k| (center[k] - a[k]).signum());
        if sign.contains(&0) {
            continue;
        }
        // An integer movement on each axis must place the endpoint on pad
        // copper. Intersect the two intervals with x_steps + y_steps = steps.
        let extent = |k: usize| {
            let d0 = (target.lo[k] - a[k]) * sign[k];
            let d1 = (target.hi[k] - a[k]) * sign[k];
            let lo = d0.min(d1).max(1) as u64;
            (
                lo.div_ceil(grid as u64),
                (d0.max(d1).max(0) as u64).div_ceil(grid as u64),
            )
        };
        let (xmin, xmax) = extent(0);
        let (ymin, ymax) = extent(1);
        let lo = xmin.max(steps.saturating_sub(ymax));
        let hi = xmax.min(steps.saturating_sub(ymin));
        if hi < lo || ymin > steps {
            continue;
        }
        // The middle of the overlap includes the symmetric octant landing of
        // round pads. Its endpoints also cover long rectangular lands.
        let parts = pad.shape.parts();
        engine.work.charge(parts.len() as u64)?;
        let geometry_work = parts
            .iter()
            .map(|part| match part {
                Shape::ConvexPolygon { vertices, .. } => 1 + vertices.len() as u64,
                _ => 1,
            })
            .sum();
        let middle = lo.midpoint(hi);
        let splits = [middle, lo, hi];
        for (index, &dx) in splits.iter().enumerate() {
            if splits[..index].contains(&dx) {
                continue;
            }
            let dy = steps - dx;
            let b = [
                a[0] + (dx as i64) * grid * sign[0],
                a[1] + (dy as i64) * grid * sign[1],
            ];
            for mut points in octilinear_elbow_paths(a, b) {
                let tail = points.len() - 1;
                let Some(end) = interior_landing(
                    engine,
                    pad,
                    target,
                    geometry_work,
                    points[tail - 1],
                    b,
                    grid,
                )?
                else {
                    continue;
                };
                points[tail] = end;
                let mut edges = Vec::new();
                for pair in points.windows(2) {
                    engine.work.charge(1)?;
                    if !engine.legal_edge_on(net, layer, pair[0], pair[1])? {
                        break;
                    }
                    edges.push(PathEdge::Track {
                        start: pair[0],
                        end: pair[1],
                        layer: state.layer,
                    });
                }
                if edges.len() + 1 == points.len() && finished(end, layer, edges.last().copied())? {
                    return Ok(Some(Path {
                        points,
                        edges,
                        steps: remaining,
                    }));
                }
            }
        }
    }
    Ok(None)
}

// Clip the final axial or diagonal step to the target bounding box. A retreat
// of less than one grid interval preserves the step charge on every moving axis.
fn interior_landing(
    engine: &Engine,
    pad: &Pad,
    target: &TargetBounds<'_>,
    geometry_work: u64,
    bend: Point,
    tip: Point,
    grid: i64,
) -> Result<Option<Point>, Failure> {
    let direction: Point = std::array::from_fn(|k| (tip[k] - bend[k]).signum());
    let (mut low, mut high) = (0, grid - 1);
    for k in 0..2 {
        if direction[k] == 0 {
            if !(target.lo[k]..=target.hi[k]).contains(&tip[k]) {
                return Ok(None);
            }
        } else {
            let t0 = (tip[k] - target.lo[k]) * direction[k];
            let t1 = (tip[k] - target.hi[k]) * direction[k];
            low = low.max(t0.min(t1));
            high = high.min(t0.max(t1));
        }
    }
    if low > high {
        return Ok(None);
    }
    let retreats = [low, low.midpoint(high), high];
    for (index, &retreat) in retreats.iter().enumerate() {
        if retreats[..index].contains(&retreat) {
            continue;
        }
        engine.work.charge(geometry_work)?;
        let pt = std::array::from_fn(|k| tip[k] - direction[k] * retreat);
        if geometry::point_in_pad(pt, pad) {
            return Ok(Some(pt));
        }
    }
    Ok(None)
}
