use crate::model::{Failure, Point, Status};
use crate::Engine;
use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap};

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
    let initial = State {
        at: start,
        direction: 4,
    };
    let mut queue = BinaryHeap::from([Reverse((heuristic(start), 0, initial))]);
    let mut best = HashMap::from([(initial, 0u64)]);
    let mut parent = HashMap::new();
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
        if state.at == goal {
            let mut path = vec![goal];
            let mut s = state;
            while s != initial {
                s = parent[&s];
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
