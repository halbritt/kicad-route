//! Shortest three-leg planar routes between a source and a certified pad landing.
use super::{Copper, Engine, Failure, Path, PathEdge, PlacedViaKey, State, TargetBounds};
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

/// A source can reach an offset land through several alternating passages.
/// Use the ordinary clipped final edge so a track cap touching the pad inside
/// its last cell costs exactly as much as it does in the grid search.
pub(super) fn from_launch(
    engine: &Engine,
    net: u32,
    state: State,
    cost: u64,
    targets: &[TargetBounds<'_>],
    attachment: &impl Fn(usize, Point, Point, &[PlacedViaKey]) -> Result<Option<Point>, Failure>,
) -> Result<Option<Path>, Failure> {
    let layer = usize::from(state.layer);
    let rate = engine.layer_policy.weight(layer);
    if cost % rate != 0 {
        return Ok(None);
    }
    let steps = cost / rate;
    let grid = engine.input.rules.grid_nm;
    let width = engine
        .effective_rules
        .track_width_for(net, engine.layer_name(layer))?;
    let radius = (width / 2 - crate::geometry::CONTACT_OVERLAP_NM).max(0);
    for target in targets {
        engine.work.charge(1)?;
        if target.mask & (1 << layer) == 0
            || !matches!(target.copper, Copper::Pad(p) if p.drill.is_none())
        {
            continue;
        }
        // A monotone route stays inside the terminal rectangle. Prefer this
        // planar escape when a via keepout covers the entire rectangle.
        let a = state.at;
        let on_layer = engine.layer_name(layer);
        let mut confined = false;
        for area in &engine.input.rule_areas {
            engine.work.charge(1)?;
            if !area.no_vias {
                continue;
            }
            if let Some((lo, hi)) = area.rectangular_bounds() {
                if (0..2)
                    .all(|k| lo[k] <= a[k].min(target.lo[k]) && a[k].max(target.hi[k]) <= hi[k])
                {
                    engine.work.charge(area.layers.len() as u64)?;
                    if area.layers.iter().any(|layer| layer == on_layer) {
                        confined = true;
                        break;
                    }
                }
            }
        }
        if !confined {
            continue;
        }
        let sign: Point =
            std::array::from_fn(|k| (target.lo[k].midpoint(target.hi[k]) - a[k]).signum());
        if sign.contains(&0) {
            continue;
        }
        let Some([lo, hi]) =
            super::octilinear_terminal::split_range(a, target, sign, grid, steps, radius)
        else {
            continue;
        };
        let candidates = [lo.midpoint(hi), lo, hi];
        for (i, &dx) in candidates.iter().enumerate() {
            if candidates[..i].contains(&dx) {
                continue;
            }
            let tip = [
                a[0] + dx as i64 * grid * sign[0],
                a[1] + (steps - dx) as i64 * grid * sign[1],
            ];
            for axis in 0..2 {
                let mut previous = tip;
                previous[axis] -= grid * sign[axis];
                let Some(end) = attachment(layer, previous, tip, &[])? else {
                    continue;
                };
                if let Some(points) = staircase(engine, net, layer, a, tip, end, axis)? {
                    let edges = points
                        .windows(2)
                        .map(|w| PathEdge::Track {
                            start: w[0],
                            end: w[1],
                            layer: state.layer,
                        })
                        .collect();
                    return Ok(Some(Path {
                        points,
                        edges,
                        steps: cost,
                    }));
                }
            }
        }
    }
    Ok(None)
}

/// Follow clear, alternating axial runs toward the last cell. Every turn
/// advances on the source grid without moving away from the landing. With
/// no feasible turn the ordinary search retains all other choices.
fn staircase(
    engine: &Engine,
    net: u32,
    layer: usize,
    source: Point,
    tip: Point,
    landing: Point,
    last_axis: usize,
) -> Result<Option<Vec<Point>>, Failure> {
    let grid = engine.input.rules.grid_nm;
    let leg = (tip[last_axis] - source[last_axis]).signum() * grid;
    if leg == 0 {
        return Ok(None);
    }
    let mut goal = tip;
    goal[last_axis] -= leg;
    let legal = |a, b| engine.legal_edge_on(net, layer, a, b);
    if !legal(goal, landing)? {
        return Ok(None);
    }
    let mut at = source;
    let mut bends = vec![source];
    let mut axis = 1 - last_axis;
    while at != goal {
        engine.work.charge(1)?;
        let delta = goal[axis] - at[axis];
        let step = delta.signum() * grid;
        let to = |n| {
            let mut p = at;
            p[axis] += n * step;
            p
        };
        let span = clear_span(delta.abs() / grid, |n| legal(at, to(n)))?;
        if span == 0 {
            return Ok(None);
        }
        at = to(span);
        engine.work.search_storage(bends.len() + 1)?;
        bends.push(at);
        axis = 1 - axis;
    }
    if bends.len() < 4 {
        return Ok(None);
    }
    if bends.last() != Some(&landing) {
        bends.push(landing);
    }
    let mut points = vec![source];
    for pair in bends[1..].windows(2) {
        engine.work.charge(1)?;
        chamfer_corner(&mut points, pair[0], pair[1], grid, &legal)?;
    }
    if points.last() != Some(&landing) {
        points.push(landing);
    }
    for pair in points.windows(2) {
        if !legal(pair[0], pair[1])? {
            return Ok(None);
        }
    }
    Ok(Some(points))
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
