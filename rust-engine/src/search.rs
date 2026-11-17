use crate::components::Copper;
use crate::model::{Failure, Point, Status, Track};
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
    if net == 0 {
        return Err(Failure::new(
            Status::Invalid,
            "net 0 is unconnected copper and cannot be routed",
        ));
    }
    let grid = engine.input.rules.grid_nm;
    let heuristic =
        |p: Point| ((p[0] - goal[0]).abs() / grid + (p[1] - goal[1]).abs() / grid) as u64;
    if !engine.legal_edge(net, start, start) || !engine.legal_edge(net, goal, goal) {
        return Err(Failure::new(
            Status::Unroutable,
            "terminal center violates routing clearance or board edge",
        ));
    }
    walk(
        engine,
        net,
        &[start],
        limit,
        expanded,
        heuristic,
        |at, _| at == goal,
    )
}

/// Unit-cost multi-source search. The existing copper component is free to
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
    let route = |a, b| Track {
        id: String::new(),
        net,
        start: a,
        end: b,
        width_nm: engine.input.rules.width_nm,
        locked: false,
    };
    let starts: BTreeSet<_> = source
        .iter()
        .flat_map(|c| c.anchors())
        .filter(|p| {
            engine.legal_edge(net, *p, *p) && source.iter().any(|c| c.touches_route(&route(*p, *p)))
        })
        .collect();
    if starts.is_empty() {
        return Err(Failure::new(
            Status::Unroutable,
            "component has no legal pad-center or incumbent-endpoint search seed",
        ));
    }
    let attachment = |a, b| {
        targets
            .iter()
            .flatten()
            .find_map(|c| c.attachment(a, b, net, engine.input.rules.width_nm))
    };
    let mut path = walk(
        engine,
        net,
        &starts.into_iter().collect::<Vec<_>>(),
        limit,
        expanded,
        |_| 0,
        |at, previous| previous.is_some_and(|before| attachment(before, at).is_some()),
    )?;
    let last = path.points.len() - 1;
    path.points[last] = attachment(path.points[last - 1], path.points[last])
        .expect("settled goal edge has a verified attachment");
    Ok(path)
}

fn walk(
    engine: &Engine,
    net: u32,
    starts: &[Point],
    limit: u64,
    expanded: &mut u64,
    heuristic: impl Fn(Point) -> u64,
    finished: impl Fn(Point, Option<Point>) -> bool,
) -> Result<Path, Failure> {
    let grid = engine.input.rules.grid_nm;
    let mut queue = BinaryHeap::new();
    let mut best = HashMap::new();
    let mut parent: HashMap<State, State> = HashMap::new();
    for at in starts {
        let initial = State {
            at: *at,
            direction: 4,
        };
        queue.push(Reverse((heuristic(*at), 0, initial)));
        best.insert(initial, 0u64);
    }
    while let Some(Reverse((_, cost, state))) = queue.pop() {
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
        if finished(state.at, parent.get(&state).map(|p| p.at)) {
            let mut path = vec![state.at];
            let mut s = state;
            while let Some(previous) = parent.get(&s) {
                s = *previous;
                path.push(s.at);
            }
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
            let Some(x) = state.at[0].checked_add(delta[0]) else {
                continue;
            };
            let Some(y) = state.at[1].checked_add(delta[1]) else {
                continue;
            };
            let next = State {
                at: [x, y],
                direction: direction as u8,
            };
            if !engine.legal_edge(net, state.at, next.at) {
                continue;
            }
            let g = cost.checked_add(1).expect("bounded search cost");
            if best.get(&next).is_some_and(|old| *old <= g) {
                continue;
            }
            best.insert(next, g);
            parent.insert(next, state);
            queue.push(Reverse((
                g.checked_add(heuristic(next.at))
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
