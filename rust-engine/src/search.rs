use crate::budget::WorkBudget;
use crate::components::Copper;
use crate::model::{Failure, Point, Rules, Shape, Status, Track};
use crate::Engine;
use std::cmp::Reverse;
use std::collections::{BTreeSet, BinaryHeap, HashMap};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Hash)]
struct State {
    at: Point,
    direction: u8,
}
#[derive(Debug)]
pub struct Path {
    pub points: Vec<Point>,
    pub steps: u64,
}
/// Four-direction unit-cost A*. Incoming direction is included in the key:
/// no location-only pruning can hide a different continuation state. This
/// slice has no bend penalty; Manhattan distance is an admissible heuristic.
pub fn search(
    engine: &Engine,
    net: u32,
    start: Point,
    goal: Point,
    limit: u64,
    expanded: &mut u64,
) -> Result<Path, Failure> {
    let _scope = engine.work.scope();
    search_inner(engine, net, start, goal, limit, expanded).map_err(|mut e| {
        e.work_used = engine.work.used();
        e.expanded_states = *expanded;
        e
    })
}
fn search_inner(
    engine: &Engine,
    net: u32,
    start: Point,
    goal: Point,
    limit: u64,
    expanded: &mut u64,
) -> Result<Path, Failure> {
    if net == 0 {
        return Err(Failure::new(
            Status::Invalid,
            "net 0 is unconnected copper and cannot be routed",
        ));
    }
    let grid = engine.input.rules.grid_nm;
    let heuristic =
        |p: Point| ((p[0] - goal[0]).abs() / grid + (p[1] - goal[1]).abs() / grid) as u64;
    if !engine.legal_edge(net, start, start)? || !engine.legal_edge(net, goal, goal)? {
        return Err(Failure::new(
            Status::Unroutable,
            "terminal center violates routing clearance or board edge",
        ));
    }
    walk(
        engine,
        &[start],
        limit,
        expanded,
        |p| Ok(heuristic(p)),
        |a, b| Ok(engine.legal_edge(net, a, b)?.then_some(b)),
        |at, _| Ok(at == goal),
    )
}

/// Unit-cost multi-source A* search. The existing copper component is free to
/// traverse; each legal pad-center/track-endpoint starts its own grid phase.
/// A target may attach anywhere along the last swept edge, not only at a
/// grid node or an incumbent endpoint. This is a bounded grid search, not a
/// proof that no continuous attachment exists when the frontier is exhausted.
pub(crate) fn join_components(
    engine: &Engine,
    net: u32,
    source: &[Copper<'_>],
    targets: &[Vec<Copper<'_>>],
    limit: u64,
    expanded: &mut u64,
) -> Result<Path, Failure> {
    let _scope = engine.work.scope();
    let route = |a, b| Track {
        id: String::new(),
        net,
        start: a,
        end: b,
        width_nm: engine.input.rules.width_nm,
        locked: false,
    };
    let mut starts = BTreeSet::new();
    for c in source {
        engine.work.charge(1)?;
        for p in c.anchors() {
            engine.work.charge(1)?;
            if !engine.legal_edge(net, p, p)? {
                continue;
            }
            for s in source {
                if s.touches_route(&route(p, p), &engine.work)? {
                    engine
                        .work
                        .search_storage(starts.len() + usize::from(!starts.contains(&p)))?;
                    starts.insert(p);
                    break;
                }
            }
        }
    }
    if starts.is_empty() {
        return Err(Failure::new(
            Status::Unroutable,
            "component has no legal pad-center or incumbent-endpoint search seed",
        ));
    }
    let attachment = |a, b| -> Result<Option<Point>, Failure> {
        let mut nearest = None;
        for c in targets.iter().flatten() {
            engine.work.charge(1)?;
            if let Some(p) = c.attachment(a, b, net, engine.input.rules.width_nm, &engine.work)? {
                if engine.legal_edge(net, a, p)? {
                    let key = |p: Point| ((p[0] - a[0]).abs() + (p[1] - a[1]).abs(), p);
                    if nearest.is_none_or(|old| key(p) < key(old)) {
                        nearest = Some(p);
                    }
                }
            }
        }
        Ok(nearest)
    };
    walk(
        engine,
        &starts.into_iter().collect::<Vec<_>>(),
        limit,
        expanded,
        |p| component_estimate(p, targets, &engine.input.rules, &engine.work),
        |a, b| {
            // Check the actual shortened goal edge before rejecting the full
            // grid step: its unused suffix may hit an obstacle or board edge.
            if let Some(p) = attachment(a, b)? {
                Ok(Some(p))
            } else {
                Ok(engine.legal_edge(net, a, b)?.then_some(b))
            }
        },
        |at, previous| {
            if previous.is_some() {
                for c in targets.iter().flatten() {
                    if c.touches_route(&route(at, at), &engine.work)? {
                        return Ok(true);
                    }
                }
            }
            Ok(false)
        },
    )
}

// The caller supplies nonempty components from groups[1..] after finding at
// least two disconnected groups. Estimate work belongs to its active scope.
fn component_estimate(
    p: Point,
    targets: &[Vec<Copper<'_>>],
    rules: &Rules,
    work: &WorkBudget,
) -> Result<u64, Failure> {
    let mut lower_bound = u64::MAX;
    for target in targets.iter().flatten() {
        work.charge(1)?;
        lower_bound = lower_bound.min(contact_steps(p, *target, rules.width_nm, rules.grid_nm));
    }
    Ok(lower_bound)
}

/// Lower bound to a superset of all endpoint-cap contacts with this target.
/// Each orthogonal edge moves at most `grid` in one axis, including a shortened
/// final edge. The sum of ceil(axis gap / grid) therefore cannot exceed the
/// remaining step cost. Full copper bounds plus the rounded-up route radius
/// contain even the conservative one-nm-overlap contact set. Ignoring obstacles
/// and rounding the envelope outward can only decrease this bound. Each bound
/// changes by at most one across an edge; the minimum over all targets is also
/// consistent and is zero at every accepted contact endpoint.
fn contact_steps(p: Point, target: Copper<'_>, width: i64, grid: i64) -> u64 {
    let route_radius = (width + 1) / 2;
    let (lo, hi): (Point, Point) = match target {
        Copper::Pad(pad) => {
            let half = match pad.shape {
                Shape::Circle { radius_nm } => [radius_nm; 2],
                Shape::Rect { half_size_nm } => half_size_nm,
            };
            (
                std::array::from_fn(|k| pad.at[k] - half[k] - route_radius),
                std::array::from_fn(|k| pad.at[k] + half[k] + route_radius),
            )
        }
        Copper::Track(t) => {
            let radius = (t.width_nm + 1) / 2 + route_radius;
            (
                std::array::from_fn(|k| t.start[k].min(t.end[k]) - radius),
                std::array::from_fn(|k| t.start[k].max(t.end[k]) + radius),
            )
        }
    };
    (0..2)
        .map(|k| {
            let gap = (lo[k] - p[k]).max(p[k] - hi[k]).max(0);
            ((gap + grid - 1) / grid) as u64
        })
        .sum()
}

fn walk(
    engine: &Engine,
    starts: &[Point],
    limit: u64,
    expanded: &mut u64,
    heuristic: impl Fn(Point) -> Result<u64, Failure>,
    advance: impl Fn(Point, Point) -> Result<Option<Point>, Failure>,
    finished: impl Fn(Point, Option<Point>) -> Result<bool, Failure>,
) -> Result<Path, Failure> {
    let grid = engine.input.rules.grid_nm;
    let mut queue = BinaryHeap::new();
    let mut best = HashMap::new();
    let mut parent: HashMap<State, State> = HashMap::new();
    for at in starts {
        engine
            .work
            .search_storage(queue.len() + best.len() + parent.len() + 2)?;
        engine.work.charge(1)?;
        let initial = State {
            at: *at,
            direction: 4,
        };
        queue.push(Reverse((heuristic(*at)?, 0, initial)));
        best.insert(initial, 0u64);
    }
    while !queue.is_empty() {
        engine.work.charge(1)?;
        let Reverse((_, cost, state)) = queue.pop().unwrap();
        if best.get(&state) != Some(&cost) {
            continue;
        }
        if *expanded >= limit {
            return Err(Failure::new(
                Status::BudgetExhausted,
                "total expansion budget exhausted",
            ));
        }
        *expanded = expanded.checked_add(1).expect("bounded expansion count");
        if finished(state.at, parent.get(&state).map(|p| p.at))? {
            engine.work.charge(1)?;
            let mut path = vec![state.at];
            let mut s = state;
            while let Some(previous) = parent.get(&s) {
                engine.work.charge(1)?;
                s = *previous;
                path.push(s.at);
            }
            engine.work.charge(path.len() as u64)?;
            path.reverse();
            return Ok(Path {
                points: path,
                steps: cost,
            });
        }
        for (direction, delta) in [[grid, 0], [0, grid], [-grid, 0], [0, -grid]]
            .iter()
            .enumerate()
        {
            engine.work.charge(1)?;
            let Some(x) = state.at[0].checked_add(delta[0]) else {
                continue;
            };
            let Some(y) = state.at[1].checked_add(delta[1]) else {
                continue;
            };
            let Some(at) = advance(state.at, [x, y])? else {
                continue;
            };
            let next = State {
                at,
                direction: direction as u8,
            };
            let g = cost.checked_add(1).expect("bounded search cost");
            if best.get(&next).is_some_and(|old| *old <= g) {
                continue;
            }
            let extra = 1
                + usize::from(!best.contains_key(&next))
                + usize::from(!parent.contains_key(&next));
            engine
                .work
                .search_storage(queue.len() + best.len() + parent.len() + extra)?;
            engine.work.charge(1)?;
            best.insert(next, g);
            parent.insert(next, state);
            queue.push(Reverse((
                g.checked_add(heuristic(next.at)?)
                    .expect("bounded heuristic"),
                g,
                next,
            )));
        }
    }
    Err(Failure::new(
        Status::Unroutable,
        "search frontier exhausted",
    ))
}

#[cfg(test)]
#[path = "search_tests.rs"]
mod tests;
