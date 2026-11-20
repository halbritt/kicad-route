//! Shortest three-leg planar routes between a source and a certified pad landing.
use super::{Engine, Failure};
use crate::model::Point;

/// Intersect the clear runs from opposite terminals. Moving the trunk within
/// that overlap preserves the Manhattan distance in either H-V-H or V-H-V.
/// The tip is on the source grid; the landing can be inside its final cell.
pub(super) fn between(
    engine: &Engine,
    net: u32,
    layer: usize,
    source: Point,
    tip: Point,
    landing: Point,
    axis: usize,
) -> Result<Option<Vec<Point>>, Failure> {
    let grid = engine.input.rules.grid_nm;
    let delta = tip[axis] - source[axis];
    let steps = delta.abs() / grid;
    if steps < 2 {
        return Ok(None);
    }
    let step = grid * delta.signum();
    let at = |mut row: Point, k: i64| {
        row[axis] = source[axis] + k * step;
        row
    };
    let legal = |a, b| engine.legal_edge_on(net, layer, a, b);
    let max = clear_span(steps - 1, |k| legal(source, at(source, k)))?;
    if max == 0 {
        return Ok(None);
    }
    let min = steps - clear_span(steps - 1, |k| legal(landing, at(landing, steps - k)))?;
    if min > max {
        return Ok(None);
    }
    let candidates = [min.midpoint(max), min, max];
    'trunks: for (i, &k) in candidates.iter().enumerate() {
        if candidates[..i].contains(&k) {
            continue;
        }
        let first = at(source, k);
        let second = at(landing, k);
        if !legal(first, second)? {
            continue;
        }
        let mut points = vec![source];
        for (bend, next) in [(first, second), (second, landing)] {
            chamfer_corner(&mut points, bend, next, grid, &legal)?;
        }
        if points.last() != Some(&landing) {
            points.push(landing);
        }
        // Recheck the exact copper geometry, including any clipped pad contact.
        for pair in points.windows(2) {
            if !legal(pair[0], pair[1])? {
                continue 'trunks;
            }
        }
        return Ok(Some(points));
    }
    Ok(None)
}

/// Replace an axial turn with a legal 45-degree segment. Trimming an equal
/// integer number of steps on each leg preserves both cost and grid phase.
fn chamfer_corner(
    points: &mut Vec<Point>,
    bend: Point,
    next: Point,
    grid: i64,
    legal: &impl Fn(Point, Point) -> Result<bool, Failure>,
) -> Result<(), Failure> {
    let previous = *points.last().expect("source is present");
    let distance = |p: Point| (p[0] - bend[0]).abs() + (p[1] - bend[1]).abs();
    let shifted = |toward: Point, k: i64| {
        std::array::from_fn(|axis| bend[axis] + (toward[axis] - bend[axis]).signum() * k * grid)
    };
    let cut = clear_span(distance(previous).min(distance(next)) / grid, |k| {
        legal(shifted(previous, k), shifted(next, k))
    })?;
    let from = shifted(previous, cut);
    let to = shifted(next, cut);
    if from != previous {
        points.push(from);
    }
    if to != from {
        points.push(to);
    }
    Ok(())
}

// Shorter swept axial copper is contained in a longer legal run. Some
// diagonals may skip an obstacle at larger sizes; subdivision then finds a
// smaller valid chamfer. Always validate the assembled path separately.
fn clear_span(last: i64, legal: impl Fn(i64) -> Result<bool, Failure>) -> Result<i64, Failure> {
    if last == 0 || legal(last)? {
        return Ok(last);
    }
    let (mut lo, mut hi) = (0, last);
    while hi - lo > 1 {
        let mid = lo.midpoint(hi);
        if legal(mid)? {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    Ok(lo)
}
