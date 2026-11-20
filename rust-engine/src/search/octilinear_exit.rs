//! Continuous zero-via escape between two isolated surface pads after the
//! four-direction grid, alternate grid phases and pad-field escapes are exhausted.
use super::{Copper, Engine, Failure, Path, PathEdge};
use crate::diff_pair::octilinear_elbow_paths;
use crate::geometry;
use crate::negotiate::{BlockedFrontier, CongestionHistory, TrialBudget};

#[allow(clippy::too_many_arguments)]
pub(super) fn between_pads(
    engine: &Engine,
    net: u32,
    source: &[Copper<'_>],
    targets: &[Vec<Copper<'_>>],
    history: Option<&CongestionHistory>,
    budget: &mut TrialBudget<'_>,
    expanded: &mut u64,
    frontier: Option<&BlockedFrontier>,
) -> Result<Option<Path>, Failure> {
    let ([Copper::Pad(source)], [group]) = (source, targets) else {
        return Ok(None);
    };
    let [Copper::Pad(target)] = group.as_slice() else {
        return Ok(None);
    };
    let stack = engine.all_layers_slice();
    let layer_mask = Copper::Pad(source).layer_mask(stack);
    if source.drill.is_some()
        || target.drill.is_some()
        || layer_mask.count_ones() != 1
        || Copper::Pad(target).layer_mask(stack) != layer_mask
    {
        return Ok(None);
    }
    let layer = layer_mask.trailing_zeros() as usize;
    let a = source.copper_center();
    let b = target.copper_center();
    // A custom pad can have its geometric center outside its copper.
    if a == b {
        return Ok(None);
    }
    engine.work.charge(2)?;
    if !geometry::point_in_pad(a, source)
        || !geometry::point_in_pad(b, target)
        || !engine.legal_edge_on(net, layer, a, a)?
        || !engine.legal_edge_on(net, layer, b, b)?
    {
        return Ok(None);
    }
    let grid = engine.input.rules.grid_nm;
    let mut best: Option<Path> = None;
    let mut try_path = |points: Vec<crate::model::Point>| -> Result<bool, Failure> {
        budget.check_before_expand(engine, *expanded, frontier)?;
        *expanded += 1;
        engine.work.charge(1)?;
        let mut edges = Vec::new();
        let mut cost = 0u64;
        for w in points.windows(2) {
            let start = w[0];
            let end = w[1];
            if !engine.legal_edge_on(net, layer, start, end)? {
                break;
            }
            // Path cost uses the existing grid axis-step units, with two axes
            // charged on a diagonal. The swept copper is a single native segment.
            let dx = start[0].abs_diff(end[0]);
            let dy = start[1].abs_diff(end[1]);
            let axis_steps = dx.div_ceil(grid as u64) + dy.div_ceil(grid as u64);
            cost =
                cost.saturating_add(axis_steps.saturating_mul(engine.layer_policy.weight(layer)));
            if let Some(h) = history {
                let len = dx.max(dy);
                for i in 1..=len.div_ceil(grid as u64) {
                    engine.work.charge(1)?;
                    let d = (i * (grid as u64)).min(len) as i64;
                    let p = std::array::from_fn(|j| start[j] + (end[j] - start[j]).signum() * d);
                    cost = cost.saturating_add(engine.layer_policy.scale(h.step_penalty(
                        layer as u8,
                        p,
                        engine.input.bounds.min,
                        grid,
                    )));
                }
            }
            edges.push(PathEdge::Track {
                start,
                end,
                layer: layer as u8,
            });
        }
        if edges.len() + 1 == points.len() && best.as_ref().is_none_or(|old| cost < old.steps) {
            best = Some(Path {
                points,
                edges,
                steps: cost,
            });
            return Ok(true);
        }
        Ok(false)
    };
    let mut found = false;
    for points in octilinear_elbow_paths(a, b) {
        found |= try_path(points)?;
        if found && history.is_none() {
            break;
        }
    }
    if !found {
        diagonal_exits(engine, net, layer, a, b, try_path)?;
    }
    Ok(best)
}

type Point = crate::model::Point;

// From both pads, find the farthest legal nanometre on each 45-degree ray.
// Two points on these rays can share a horizontal or vertical middle leg.
// Intersect the two legal distance intervals before testing any complete path.
fn diagonal_exits(
    engine: &Engine,
    net: u32,
    layer: usize,
    a: Point,
    b: Point,
    mut visit: impl FnMut(Vec<Point>) -> Result<bool, Failure>,
) -> Result<(), Failure> {
    let bounds = engine.input.bounds;
    let rays = |p: Point| -> Result<Vec<(Point, i64)>, Failure> {
        let mut open = Vec::new();
        for d in [[1, 1], [1, -1], [-1, 1], [-1, -1]] {
            engine.work.charge(1)?;
            let extent = |k: usize| {
                if d[k] > 0 {
                    bounds.max[k] - p[k]
                } else {
                    p[k] - bounds.min[k]
                }
            };
            let end = extent(0).min(extent(1)).max(0);
            let span = super::monotone_trunk::clear_span(end, |s| {
                engine.legal_edge_on(net, layer, p, along(p, d, s))
            })?;
            if span > 0 {
                open.push((d, span));
            }
        }
        Ok(open)
    };
    let (first, last) = (rays(a)?, rays(b)?);
    for (u, first_max) in first {
        for &(v, last_max) in &last {
            for axis in 0..2 {
                engine.work.charge(1)?;
                // t = base + slope*s at the two ends of an axial bridge.
                let base = (a[axis] - b[axis]) * v[axis];
                let slope = u[axis] * v[axis];
                let at_lo = (1 - base) * slope;
                let at_hi = (last_max - base) * slope;
                let lo = 1.max(at_lo.min(at_hi));
                let hi = first_max.min(at_lo.max(at_hi));
                if lo > hi {
                    continue;
                }
                let k = 1 - axis;
                let speed = u[k] - v[k] * slope;
                let near = if speed == 0 {
                    lo
                } else {
                    ((b[k] + v[k] * base - a[k]) / speed).clamp(lo, hi)
                };
                let samples = [near, hi, lo, lo.midpoint(hi)];
                for (i, &s) in samples.iter().enumerate() {
                    if samples[..i].contains(&s) {
                        continue;
                    }
                    engine.work.charge(1)?;
                    let left = along(a, u, s);
                    let right = along(b, v, base + slope * s);
                    let points = if left == right {
                        vec![a, left, b]
                    } else {
                        if geometry::intersects(a, left, b, right) {
                            continue;
                        }
                        vec![a, left, right, b]
                    };
                    visit(points)?;
                }
            }
        }
    }
    Ok(())
}

fn along(p: Point, d: Point, s: i64) -> Point {
    [p[0] + d[0] * s, p[1] + d[1] * s]
}
