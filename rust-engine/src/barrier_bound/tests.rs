use super::*;
use crate::model::Limits;
use std::{cmp::Reverse, collections::BinaryHeap};

fn article() -> Input {
    let mut input: Input = serde_json::from_str(include_str!(
        "../../tests/fixtures/multiterminal-native-two-nets.json"
    ))
    .unwrap();
    input.tracks.clear();
    input.rule_areas.clear();
    input
}

fn track(a: Point, b: Point, net: u32, layer: &str) -> Track {
    Track {
        start: a,
        end: b,
        net,
        layer: layer.into(),
        width_nm: 1,
        locked: false,
        id: String::new(),
    }
}

fn rectangle(layer: &str) -> RuleArea {
    RuleArea {
        id: String::new(),
        name: None,
        layers: vec![layer.into()],
        outer: vec![[4, 1], [6, 1], [6, 9], [4, 9]],
        holes: vec![],
        extra_polygons: vec![],
        no_tracks: true,
        no_vias: false,
        no_pads: false,
        no_footprints: false,
        no_zone_fills: false,
    }
}

fn budget() -> WorkBudget {
    WorkBudget::new(Limits::default())
}

#[test]
fn foreign_centerline_forces_either_end_with_layer_escape_and_clipped_contact() {
    let mut input = article();
    input.tracks = vec![track([5, 0], [5, 8], 2, "F.Cu")];
    let layers = vec!["F.Cu".into(), "B.Cu".into()];
    let w = budget();
    let build = |stack: &[String], radius| {
        BarrierBound::for_join(
            &input,
            1,
            &input.tracks,
            stack,
            ([0, 4], [0, 4]),
            ([12, 4], [12, 4]),
            radius,
            2,
            3,
            5,
            &w,
        )
        .unwrap()
    };
    let front = build(&layers[..1], 0);
    assert!(front.is_active());
    assert_eq!(front.estimate([0, 4], 0, &w).unwrap(), 30); // 6 normal steps plus 4 transverse
    let both = build(&layers, 0);
    assert_eq!(both.estimate([0, 4], 0, &w).unwrap(), 23); // 6 steps plus one layer change
    assert_eq!(both.estimate([0, 4], 1, &w).unwrap(), 18);
    assert_eq!(both.estimate_reverse([12, 4], 0, &w).unwrap(), 23);
    let clipped = build(&layers, 2);
    assert_eq!(clipped.estimate([10, 4], 0, &w).unwrap(), 0);
    assert_eq!(clipped.estimate([9, 4], 0, &w).unwrap(), 3);
    assert_eq!(clipped.estimate_reverse([0, 4], 1, &w).unwrap(), 0);
    let next_to_wall_end = BarrierBound::for_join(
        &input,
        1,
        &input.tracks,
        &layers[..1],
        ([0, 2], [0, 2]),
        ([12, 7], [12, 9]),
        1,
        2,
        3,
        5,
        &w,
    )
    .unwrap();
    assert!(next_to_wall_end.is_active());
    // The target reaches above the upper tip. A straight path there need
    // neither descend to that tip nor pay a whole extra clipped step.
    assert_eq!(next_to_wall_end.estimate([0, 9], 0, &w).unwrap(), 18);
}

#[test]
fn supports_only_proved_solid_rectangles_and_foreign_straight_tracks() {
    let mut input = article();
    let mut hole = rectangle("F.Cu");
    hole.holes.push(vec![[5, 3], [5, 6], [6, 4]]);
    let mut open = rectangle("F.Cu");
    open.no_tracks = false;
    let mut skew = rectangle("F.Cu");
    skew.outer[2] = [7, 9];
    let mut crossed = rectangle("F.Cu");
    crossed.outer.swap(1, 2);
    input.rule_areas = vec![hole, open, skew, crossed];
    input.tracks = vec![
        track([5, 0], [5, 8], 1, "F.Cu"),
        track([5, 0], [6, 8], 2, "F.Cu"),
        track([5, 0], [5, 8], 2, "B.Cu"),
    ];
    let w = budget();
    let layers = vec!["F.Cu".into()];
    let build = |inp: &Input| {
        BarrierBound::for_join(
            inp,
            1,
            &inp.tracks,
            &layers,
            ([0, 4], [0, 4]),
            ([12, 4], [12, 4]),
            0,
            2,
            1,
            3,
            &w,
        )
        .unwrap()
    };
    assert!(!build(&input).is_active());
    let mut real = rectangle("F.Cu");
    real.outer.reverse();
    real.outer.push(real.outer[0]);
    input.rule_areas.push(real);
    let h = build(&input);
    assert!(h.is_active());
    assert_eq!(h.estimate([0, 4], 0, &w).unwrap(), 9);
    assert_eq!(h.estimate([4, 0], 0, &w).unwrap(), 6);
}

#[derive(Clone, Copy)]
struct Solid {
    layer: usize,
    lo: Point,
    hi: Point,
}

/// Independent finite-grid shortest paths, allowing every phase and even vias
/// inside track prohibition areas. A planar edge can finish at any nm before
/// its full grid length, including before an obstacle later on that edge.
#[allow(clippy::too_many_arguments)]
fn compare_oracle(
    bound: &BarrierBound,
    stack: usize,
    solids: &[Solid],
    terminals: &[(usize, Point)],
    reach: i64,
    grid: i64,
    step: u64,
    via: u64,
    backward: bool,
) -> usize {
    const SIZE: usize = 15;
    let sink = stack * SIZE * SIZE;
    let mut reverse = vec![Vec::new(); sink + 1];
    let mut edges = Vec::new();
    let mut values = vec![0; sink + 1];
    let w = budget();
    let id = |k: usize, p: Point| k * SIZE * SIZE + (p[1] as usize) * SIZE + p[0] as usize;
    let inside = |p: Point| p.iter().all(|n| (0..SIZE as i64).contains(n));
    let goal = |k: usize, p: Point| {
        terminals
            .iter()
            .any(|&(l, c)| l == k && (p[0] - c[0]).abs() <= reach && (p[1] - c[1]).abs() <= reach)
    };
    let legal = |k: usize, a: Point, b: Point| {
        !solids.iter().any(|s| {
            k == s.layer && (0..2).all(|v| a[v].min(b[v]) <= s.hi[v] && s.lo[v] <= a[v].max(b[v]))
        })
    };
    let mut add = |a: usize, b: usize, c: u64| {
        reverse[b].push((a, c));
        edges.push((a, b, c));
    };
    for k in 0..stack {
        for x in 0..SIZE as i64 {
            for y in 0..SIZE as i64 {
                let p = [x, y];
                let i = id(k, p);
                values[i] = if backward {
                    bound.estimate_reverse(p, k, &w)
                } else {
                    bound.estimate(p, k, &w)
                }
                .unwrap();
                if goal(k, p) {
                    add(i, sink, 0)
                }
                for d in [[-1, 0], [0, -1], [1, 0], [0, 1]] {
                    for r in 1..=grid {
                        let b = [x + r * d[0], y + r * d[1]];
                        if !inside(b) || !legal(k, p, b) {
                            break;
                        }
                        if goal(k, b) {
                            add(i, sink, step)
                        }
                        if r == grid {
                            add(i, id(k, b), step)
                        }
                    }
                }
                for other in 0..stack {
                    if other != k {
                        add(i, id(other, p), via)
                    }
                }
            }
        }
    }
    let mut distances = vec![u64::MAX; sink + 1];
    distances[sink] = 0;
    let mut heap = BinaryHeap::from([Reverse((0u64, sink))]);
    while let Some(Reverse((g, i))) = heap.pop() {
        if distances[i] != g {
            continue;
        }
        for &(pre, cost) in &reverse[i] {
            let next = g + cost;
            if next < distances[pre] {
                distances[pre] = next;
                heap.push(Reverse((next, pre)))
            }
        }
    }
    for (i, &dist) in distances.iter().enumerate() {
        assert!(
            values[i] <= dist,
            "state {i}, stack={stack} reach={reach} grid={grid} backward={backward} terminals={terminals:?} source={:?} target={:?}: bound {} > oracle {dist}",
            bound.source,
            bound.target,
            values[i]
        );
    }
    for &(a, b, c) in &edges {
        assert!(
            values[a] <= c + values[b],
            "edge {a} -> {b}: {} > {c} + {}",
            values[a],
            values[b]
        );
    }
    edges.len()
}

#[test]
fn finite_world_oracle_all_states_edges_layers_phases_and_partial_final_edges() {
    let mut input = article();
    input.tracks = vec![
        track([5, 2], [5, 12], 2, "F.Cu"),
        track([2, 8], [11, 8], 3, "F.Cu"),
        track([9, 2], [9, 12], 3, "B.Cu"),
    ];
    let mut area = rectangle("B.Cu");
    area.outer = vec![[2, 5], [10, 5], [10, 7], [2, 7]];
    input.rule_areas.push(area);
    let solids = [
        Solid {
            layer: 0,
            lo: [5, 2],
            hi: [5, 12],
        },
        Solid {
            layer: 0,
            lo: [2, 8],
            hi: [11, 8],
        },
        Solid {
            layer: 1,
            lo: [9, 2],
            hi: [9, 12],
        },
        Solid {
            layer: 1,
            lo: [2, 5],
            hi: [10, 7],
        },
    ];
    let layers = vec!["F.Cu".into(), "B.Cu".into()];
    let mut total = 0;
    for stack in [1, 2] {
        for grid in [2, 3] {
            for radius in [0, 1, 2] {
                for (a, b) in [([1, 4], [12, 7]), ([2, 3], [11, 12]), ([12, 7], [1, 5])] {
                    let second = [b[0], b[1] + 1];
                    let end = (b, second);
                    let via = 5;
                    let step = 3;
                    let h = BarrierBound::for_join(
                        &input,
                        1,
                        &input.tracks,
                        &layers[..stack],
                        (a, a),
                        end,
                        radius,
                        grid,
                        step,
                        via,
                        &budget(),
                    )
                    .unwrap();
                    total += compare_oracle(
                        &h,
                        stack,
                        &solids,
                        &[(0, b), (stack - 1, second)],
                        radius,
                        grid,
                        step,
                        via,
                        false,
                    );
                    total +=
                        compare_oracle(&h, stack, &solids, &[(0, a)], 0, grid, step, via, true);
                }
            }
        }
    }
    assert!(total > 60_000);
}

#[test]
fn same_solid_on_every_physical_layer_survives_even_free_layer_changes() {
    let layers: Vec<String> = ["F.Cu", "In1.Cu", "B.Cu"].map(str::to_owned).into();
    let mut input = article();
    let mut r = rectangle("F.Cu");
    r.layers = layers.clone(); // via prohibition intentionally false
    let a = [1, 5];
    let b = [12, 5];
    let w = budget();
    let build = |inp: &Input| {
        BarrierBound::for_join(inp, 1, &inp.tracks, &layers, (a, a), (b, b), 1, 2, 3, 1, &w)
            .unwrap()
    };
    let solids: Vec<_> = (0..layers.len())
        .map(|layer| Solid {
            layer,
            lo: [4, 1],
            hi: [6, 9],
        })
        .collect();
    for areas in [vec![r], layers.iter().map(|l| rectangle(l)).collect()] {
        input.rule_areas = areas;
        let bound = build(&input);
        for layer in 0..layers.len() {
            assert_eq!(bound.estimate(a, layer, &w).unwrap(), 27);
            assert_eq!(bound.estimate_reverse(b, layer, &w).unwrap(), 30);
            assert_eq!(bound.estimate([11, 5], layer, &w).unwrap(), 0);
        }
        compare_oracle(&bound, 3, &solids, &[(0, b)], 1, 2, 3, 1, false);
        compare_oracle(&bound, 3, &solids, &[(0, a)], 0, 2, 3, 1, true);
    }
    // One layer differs, so the common cut is not established, even if
    // the affected area on the other layers is geometrically identical.
    input.rule_areas[1].outer[0] = [3, 0];
    let bound = build(&input);
    assert_eq!(bound.estimate(a, 0, &w).unwrap(), 16);
    assert_eq!(bound.estimate(a, 1, &w).unwrap(), 15);
    assert_eq!(bound.estimate(a, 2, &w).unwrap(), 16);
    compare_oracle(
        &bound,
        3,
        &[solids[0], solids[2]],
        &[(0, b)],
        1,
        2,
        3,
        1,
        false,
    );
}
