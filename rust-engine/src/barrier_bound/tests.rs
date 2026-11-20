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
    let front_landings = both.with_terminal_layers(1, 1);
    assert_eq!(front_landings.estimate([0, 4], 0, &w).unwrap(), 28);
    assert_eq!(front_landings.estimate([0, 4], 1, &w).unwrap(), 23);
    assert_eq!(front_landings.estimate_reverse([12, 4], 0, &w).unwrap(), 28);
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
fn ignores_open_areas_and_traces_that_do_not_separate_the_terminals() {
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
        track([5, 6], [6, 8], 2, "F.Cu"),
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
    let legal = |k: usize, a: Point, b: Point| {
        !solids.iter().any(|s| {
            k == s.layer && (0..2).all(|v| a[v].min(b[v]) <= s.hi[v] && s.lo[v] <= a[v].max(b[v]))
        })
    };
    finite_distances(
        bound, stack, terminals, reach, grid, step, via, backward, legal,
    )
}

#[allow(clippy::too_many_arguments)]
fn finite_distances(
    bound: &BarrierBound,
    stack: usize,
    terminals: &[(usize, Point)],
    reach: i64,
    grid: i64,
    step: u64,
    via: u64,
    backward: bool,
    legal: impl Fn(usize, Point, Point) -> bool,
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
                    let on_front = h.with_terminal_layers(1u32 << (stack - 1), 1);
                    total += compare_oracle(
                        &on_front,
                        stack,
                        &solids,
                        &[(0, b)],
                        radius,
                        grid,
                        step,
                        via,
                        false,
                    );
                    total += compare_oracle(
                        &on_front,
                        stack,
                        &solids,
                        &[(stack - 1, a)],
                        0,
                        grid,
                        step,
                        via,
                        true,
                    );
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

// Independent dynamic program on every integer crossing height, without the
// production endpoint recurrence. Integer gate ends admit an integer optimum.
fn crossing_variation(y: i64, terminal: (i64, i64), gates: &[(i64, i64, i64)]) -> u64 {
    let mut values: [u64; 15] = std::array::from_fn(|i| y.abs_diff(i as i64));
    for &(_, low, high) in gates {
        let previous = values;
        for (i, v) in values.iter_mut().enumerate() {
            let open = (low > 0 && i as i64 <= low) || (high < 14 && i as i64 >= high);
            *v = if open {
                previous
                    .iter()
                    .enumerate()
                    .map(|(j, &dist)| dist.saturating_add(i.abs_diff(j) as u64))
                    .min()
                    .unwrap()
            } else {
                u64::MAX
            };
        }
    }
    values
        .into_iter()
        .enumerate()
        .map(|(i, d)| {
            let h = i as i64;
            d.saturating_add(h.abs_diff(h.clamp(terminal.0, terminal.1)))
        })
        .min()
        .unwrap()
}

#[test]
fn ordered_passages_use_total_transverse_travel_and_survive_free_vias() {
    let mut input = article();
    input.bounds.min = [0, 0];
    input.bounds.max = [14, 14];
    let layers = vec!["F.Cu".into(), "B.Cu".into()];
    let mut checks = 0;
    for transpose in [false, true] {
        let rotate = |[x, y]: Point| if transpose { [y, x] } else { [x, y] };
        for intervals in [
            [(3, 0, 9), (7, 4, 14), (11, 0, 8)], // alternating board-edge apertures
            [(3, 2, 10), (7, 4, 12), (11, 2, 10)], // both sides available
            [(3, 0, 9), (3, 6, 12), (11, 4, 14)], // two cuts at the same coordinate
        ] {
            input.rule_areas.clear();
            let mut solids = Vec::new();
            for (cut, low, high) in intervals {
                let mut area = rectangle("F.Cu");
                area.layers = layers.clone();
                area.outer = [
                    [cut - 1, low],
                    [cut + 1, low],
                    [cut + 1, high],
                    [cut - 1, high],
                ]
                .map(rotate)
                .to_vec();
                input.rule_areas.push(area);
                for layer in 0..layers.len() {
                    solids.push(Solid {
                        layer,
                        lo: rotate([cut - 1, low]),
                        hi: rotate([cut + 1, high]),
                    });
                }
            }
            for grid in [2, 3] {
                for reach in [0, 1, 2] {
                    let a = rotate([0, 5]);
                    let b = rotate([14, 6]);
                    let c = rotate([14, 7]);
                    let h = BarrierBound::for_join(
                        &input,
                        1,
                        &input.tracks,
                        &layers,
                        (a, a),
                        (b, c),
                        reach,
                        grid,
                        1,
                        0,
                        &budget(),
                    )
                    .unwrap();
                    if intervals[0] == (3, 0, 9) && intervals[1].0 == 7 && grid == 2 && reach == 0 {
                        // 14 nm longitudinal. 5->9->4->8->7 needs 14 nm transverse.
                        assert_eq!(h.estimate(a, 0, &budget()).unwrap(), 14);
                        // From the opposite end at y=6 the transverse travel is 15 nm.
                        assert_eq!(h.estimate_reverse(b, 1, &budget()).unwrap(), 15);
                    }
                    checks +=
                        compare_oracle(&h, 2, &solids, &[(0, b), (1, c)], reach, grid, 1, 0, false);
                    if grid == 2 && reach == 0 {
                        for x in 0..=14 {
                            let ahead: Vec<_> =
                                intervals.iter().copied().filter(|w| x < w.0).collect();
                            let behind: Vec<_> = intervals
                                .iter()
                                .rev()
                                .copied()
                                .filter(|w| w.0 < x)
                                .collect();
                            for y in 0..=14 {
                                let p = rotate([x, y]);
                                let f = (14_u64 - x as u64).div_ceil(2)
                                    + crossing_variation(y, (6, 7), &ahead).div_ceil(2);
                                let r = (x as u64).div_ceil(2)
                                    + crossing_variation(y, (5, 5), &behind).div_ceil(2);
                                assert_eq!(h.estimate(p, 0, &budget()).unwrap(), f);
                                assert_eq!(h.estimate_reverse(p, 1, &budget()).unwrap(), r);
                            }
                        }
                    }
                    checks += compare_oracle(&h, 2, &solids, &[(0, a)], 0, grid, 1, 0, true);
                }
            }
        }
    }
    assert!(checks > 80_000);
}

#[test]
fn ordered_preparation_and_queries_stop_at_their_shared_limits() {
    let layers = vec!["F.Cu".into(), "B.Cu".into()];
    let build = |count: usize, work: &WorkBudget| {
        let mut input = article();
        for i in 0..count {
            let mut wall = rectangle("F.Cu");
            wall.layers = layers.clone();
            for p in &mut wall.outer {
                p[0] += 3 * i as i64;
            }
            input.rule_areas.push(wall);
        }
        BarrierBound::for_join(
            &input,
            1,
            &input.tracks,
            &layers,
            ([0, 5], [0, 5]),
            ([100, 5], [100, 5]),
            0,
            2,
            1,
            0,
            work,
        )
    };
    let limited = |max_work| {
        WorkBudget::new(Limits {
            max_work,
            ..Default::default()
        })
    };
    let setup = budget();
    let h = build(3, &setup).unwrap();
    assert!(h.ordered[0].is_some());
    assert!(
        matches!(build(3,&limited(setup.used()-1)), Err(e) if e.status == crate::model::Status::WorkExhausted)
    );
    let query = budget();
    h.estimate([3, 0], 0, &query).unwrap();
    assert!(
        matches!(h.estimate([3,0],0,&limited(query.used()-1)), Err(e) if e.status == crate::model::Status::WorkExhausted)
    );

    // Exceeding the small family bound leaves the existing one-wall estimate
    // available. The router can still use the ordinary work and search limits.
    let many = build(MAX_ORDERED_CUTS + 1, &budget()).unwrap();
    assert!(many.is_active());
    assert!(many.ordered[0].is_none());
    assert!(many.estimate([0, 5], 0, &budget()).unwrap() > 50);
}

#[test]
fn circular_pad_banks_respect_tangent_gaps_layers_and_own_net_with_exact_distances() {
    use crate::{model::Shape, Engine};
    let mut edges = 0;
    for layer_count in [2, 4] {
        let mut sample = article();
        sample.layers = if layer_count == 2 {
            vec!["F.Cu".into(), "B.Cu".into()]
        } else {
            ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
                .map(str::to_owned)
                .to_vec()
        };
        let layer = layer_count - 1;
        sample.bounds.min = [0, 0];
        sample.bounds.max = [14, 14];
        sample.rules.width_nm = 2;
        sample.rules.clearance_nm = 1;
        sample.rules.edge_clearance_nm = 0;
        sample.pads.truncate(3);
        let source = [1, 7];
        let target = [13, 7];
        for (ys, own, local, active) in [
            ([3, 7, 11], false, 0, true),  // solid bank
            ([2, 8, 14], false, 0, false), // exact tangency at 5 and 11
            ([2, 8, 14], false, 1, false), // pad local field equal to current pair clearance
            ([2, 8, 14], false, 2, true),  // tighter local pad clearance closes the slit
            ([3, 7, 11], true, 0, false),  // own-net middle pad is a possible landing
        ] {
            for (i, p) in sample.pads.iter_mut().enumerate() {
                p.net = if i == 1 && own { 1 } else { 3 };
                p.shape = Shape::Circle { radius_nm: 1 };
                p.at = [7, ys[i]];
                p.layers = vec![sample.layers[layer].clone()];
                p.local_clearance_nm = (local != 0).then_some(local);
            }
            for grid in [1, 2] {
                sample.rules.grid_nm = grid;
                let engine = Engine::new(sample.clone()).unwrap();
                let work = budget();
                let mut bound = BarrierBound::for_join(
                    &sample,
                    1,
                    [],
                    &sample.layers,
                    (source, source),
                    (target, target),
                    1,
                    grid,
                    2,
                    7,
                    &work,
                )
                .unwrap();
                engine.pad_banks.include(&engine, 1, &mut bound).unwrap();
                assert_eq!(bound.layers[layer][0].is_some(), active);
                assert!(bound.layers[..layer]
                    .iter()
                    .all(|slots| slots.iter().all(Option::is_none)));
                // Test every integer point and shortened axial edge against
                // disk-to-segment squared distance, with unconstrained vias.
                let legal = |k: usize, a: Point, b: Point| {
                    if k != layer {
                        return true;
                    }
                    sample.pads.iter().filter(|p| p.net != 1).all(|p| {
                        let dx = p.at[0] - p.at[0].clamp(a[0].min(b[0]), a[0].max(b[0]));
                        let dy = p.at[1] - p.at[1].clamp(a[1].min(b[1]), a[1].max(b[1]));
                        4 * (dx * dx + dy * dy) >= (2 + 2 + 2 * local.max(1)).pow(2)
                    })
                };
                edges += finite_distances(
                    &bound,
                    layer_count,
                    &[(layer, target)],
                    1,
                    grid,
                    2,
                    7,
                    false,
                    legal,
                );
                edges += finite_distances(
                    &bound,
                    layer_count,
                    &[(layer, source)],
                    0,
                    grid,
                    2,
                    7,
                    true,
                    legal,
                );
            }
        }
    }
    assert!(edges > 60_000);
}

#[test]
fn isolated_elongated_lands_bound_terminal_escape_without_forbidding_vias() {
    use crate::{model::Shape, Engine};
    let mut cuts = 0;
    for transpose in [false, true] {
        let xy = |x, y| if transpose { [y, x] } else { [x, y] };
        for own in [false, true] {
            let mut input = article();
            input.layers = vec!["F.Cu".into(), "B.Cu".into()];
            input.bounds.min = [0, 0];
            input.bounds.max = [14, 14];
            input.rules.width_nm = 2;
            input.rules.clearance_nm = 1;
            input.rules.edge_clearance_nm = 0;
            input.pads.truncate(2);
            // Two staggered rectangular leads flank the source. The right
            // lead is the only obstacle between the source and the target.
            for (i, p) in input.pads.iter_mut().enumerate() {
                p.net = if i == 1 && own { 1 } else { 3 };
                p.at = if i == 0 { xy(2, 6) } else { xy(8, 7) };
                p.shape = Shape::Rect {
                    half_size_nm: xy(1, 3),
                };
                p.layers = vec!["B.Cu".into()];
            }
            for grid in [1, 2] {
                input.rules.grid_nm = grid;
                let engine = Engine::new(input.clone()).unwrap();
                let source = xy(5, 7);
                let target = xy(13, 9);
                let mut bound = BarrierBound::for_join(
                    &input,
                    1,
                    [],
                    &input.layers,
                    (source, source),
                    (target, target),
                    1,
                    grid,
                    2,
                    3,
                    &budget(),
                )
                .unwrap();
                engine.pad_banks.include(&engine, 1, &mut bound).unwrap();
                bound = bound.with_terminal_layers(2, 2);
                assert_eq!(bound.is_active(), !own);
                let legal = |layer: usize, a: Point, b: Point| {
                    layer == 0
                        || input.pads.iter().filter(|p| p.net != 1).all(|p| {
                            let Shape::Rect { half_size_nm } = p.shape else {
                                unreachable!()
                            };
                            let gaps: [i64; 2] = std::array::from_fn(|k| {
                                (p.at[k] - half_size_nm[k] - a[k].max(b[k]))
                                    .max(a[k].min(b[k]) - p.at[k] - half_size_nm[k])
                                    .max(0)
                            });
                            gaps[0] * gaps[0] + gaps[1] * gaps[1] >= 4
                        })
                };
                cuts += finite_distances(&bound, 2, &[(1, target)], 1, grid, 2, 3, false, legal);
                cuts += finite_distances(&bound, 2, &[(1, source)], 0, grid, 2, 3, true, legal);
            }
        }
    }
    assert!(cuts > 20_000);
}

#[test]
fn bank_uses_admitted_layer_width_for_tangency_and_charges_the_search() {
    use crate::model::{Shape, Status};
    use crate::rules::{DruConstraint, DruRule, DruSeverity, EffectiveRules};
    use crate::Engine;
    let mut input = article();
    input.layers = vec!["F.Cu".into(), "B.Cu".into()];
    input.rules.width_nm = 2;
    input.rules.clearance_nm = 1;
    input.pads.truncate(4);
    for (i, pad) in input.pads.iter_mut().enumerate() {
        pad.at = [7, if i % 2 == 0 { 3 } else { 9 }];
        pad.net = 3;
        pad.layers = vec![input.layers[i / 2].clone()];
        pad.shape = Shape::Circle { radius_nm: 1 };
    }
    let mut rules = EffectiveRules::from_uniform_rules(input.rules, &input.nets);
    rules.max_track_width_nm = 3;
    rules.dru_rules.push(DruRule {
        name: "back width".into(),
        layer: Some("B.Cu".into()),
        condition: None,
        constraint: DruConstraint::TrackWidth {
            min_nm: 3,
            max_nm: None,
        },
        severity: DruSeverity::Error,
    });
    input.effective_rules = Some(rules);
    let mut engine = Engine::new(input.clone()).unwrap();
    let run = |engine: &Engine, y| {
        let mut bound = BarrierBound::for_join(
            &input,
            1,
            [],
            &input.layers,
            ([1, y], [1, y]),
            ([13, y], [13, y]),
            0,
            1,
            1,
            10,
            &budget(),
        )?;
        engine.pad_banks.include(engine, 1, &mut bound)?;
        Ok::<_, Failure>(bound)
    };
    engine.work = budget();
    let bound = run(&engine, 6).unwrap();
    let used = engine.work.used();
    // One line lookup. Each of two layer rows charges the visit, one width
    // rule, two pad visits, two pair rules, two sort units and two union visits.
    assert_eq!(used, 1 + 2 * (1 + 1 + 2 + 2 + 2 + 2));
    assert!(bound.layers[0][0].is_none());
    let back = bound.layers[1][0].unwrap();
    assert_eq!((back.cut, back.lo, back.hi), (7, 0, 12));
    for limit in 0..used {
        engine.work = WorkBudget::new(Limits {
            max_work: limit,
            ..Limits::default()
        });
        assert!(matches!(run(&engine, 6),Err(e) if e.status==Status::WorkExhausted));
    }
    engine.work = WorkBudget::new(Limits {
        max_search_entries: 1,
        ..Limits::default()
    });
    assert!(matches!(run(&engine, 6),Err(e) if e.status==Status::AdmissionExceeded));

    // An unrelated route outside the transverse envelope only visits the rows.
    engine.work = WorkBudget::new(Limits {
        max_work: 3,
        max_search_entries: 0,
        ..Limits::default()
    });
    assert!(!run(&engine, 15).unwrap().is_active());
    assert_eq!(engine.work.used(), 3);
}

#[test]
fn doubled_centerline_sections_match_swept_native_shape_predicates() {
    use crate::model::Shape;
    let mut pad = article().pads.remove(0);
    pad.at = [-5, 3];
    let shapes = [
        Shape::Circle { radius_nm: 3 },
        Shape::Rect {
            half_size_nm: [2, 5],
        },
        Shape::RoundRect {
            half_size_nm: [3, 5],
            radius_nm: 2,
        },
        Shape::BoxExact {
            center_2x: [-7, 2],
            half_size_2x: [5, 3],
            radius_2x: 2,
        },
        Shape::Capsule {
            start_nm: [-8, 6],
            end_nm: [2, 6],
            width_nm: 5,
        },
        Shape::Capsule {
            start_nm: [-3, -4],
            end_nm: [-3, 7],
            width_nm: 3,
        },
    ];
    for shape in shapes {
        pad.shape = shape;
        let (center, half) = pad.axial_extents_2x().unwrap();
        for axis in 0..2 {
            if center[axis] % 2 != 0 {
                continue;
            }
            let j = 1 - axis;
            for width in 1..=4 {
                for clearance in 0..=2 {
                    for y in -18..=18 {
                        let mut p = [0; 2];
                        p[axis] = center[axis] / 2;
                        p[j] = y;
                        let expected = (2 * y - center[j]).abs() < half[j] + width + 2 * clearance;
                        assert_eq!(
                            crate::geometry::track_near_pad(p, p, width, clearance, &pad, false),
                            expected,
                            "{:?} cut={axis} at {p:?} width={width} clearance={clearance}",
                            pad.shape
                        );
                    }
                }
            }
        }
    }
    pad.shape = Shape::Capsule {
        start_nm: [-3, 3],
        end_nm: [5, 8],
        width_nm: 3,
    };
    assert!(pad.axial_extents_2x().is_none());
}

#[test]
fn ordered_layer_crossings_match_independent_grid_costs_and_consistency() {
    let mut input = article();
    input.bounds.min = [0, 0];
    input.bounds.max = [14, 14];
    let layers: Vec<String> = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        .map(String::from)
        .into();
    let w = budget();
    for rotate in [false, true] {
        let position = |p: Point| if rotate { [p[1], p[0]] } else { p };
        input.rule_areas.clear();
        let mut solids = Vec::new();
        for (cut, allowed) in [(3, 1u32), (7, 2), (11, 8)] {
            for (layer, name) in layers.iter().enumerate() {
                if allowed & (1 << layer) != 0 {
                    continue;
                }
                let mut area = rectangle(name);
                area.outer = [[cut - 1, 0], [cut + 1, 0], [cut + 1, 14], [cut - 1, 14]]
                    .map(position)
                    .to_vec();
                input.rule_areas.push(area);
                solids.push(Solid {
                    layer,
                    lo: position([cut - 1, 0]),
                    hi: position([cut + 1, 14]),
                });
            }
        }
        let a = position([1, 7]);
        let b = position([13, 7]);
        for (terminal, changes) in [(0, 3), (1, 3), (3, 2)] {
            for reach in [0, 1] {
                let h = BarrierBound::for_join(
                    &input,
                    1,
                    &input.tracks,
                    &layers,
                    (a, a),
                    (b, b),
                    reach,
                    2,
                    3,
                    5,
                    &w,
                )
                .unwrap()
                .with_terminal_layers(1, 1 << terminal);
                assert_eq!(h.estimate(a, 0, &w).unwrap(), 18 + changes * 5);
                compare_oracle(&h, 4, &solids, &[(terminal, b)], reach, 2, 3, 5, false);
                compare_oracle(&h, 4, &solids, &[(0, a)], 0, 2, 3, 5, true);
            }
        }
        // Every face is eligible for a direct finishing via in this control.
        let h = BarrierBound::for_join(
            &input,
            1,
            &input.tracks,
            &layers,
            (a, a),
            (b, b),
            0,
            2,
            3,
            5,
            &w,
        )
        .unwrap();
        assert_eq!(h.estimate(a, 0, &w).unwrap(), 28);
        // A real opening at either board edge keeps the planar detour.
        for area in &mut input.rule_areas {
            for p in &mut area.outer {
                let i = usize::from(!rotate);
                if p[i] == 0 {
                    p[i] = 1;
                }
            }
        }
        let h = BarrierBound::for_join(
            &input,
            1,
            &input.tracks,
            &layers,
            (a, a),
            (b, b),
            0,
            2,
            3,
            5,
            &w,
        )
        .unwrap()
        .with_terminal_layers(1, 1);
        assert!(h.crossing_layers.iter().all(BTreeMap::is_empty));
    }
}

#[test]
fn one_layer_change_can_serve_crossings_on_both_axes() {
    let mut input = article();
    input.bounds.min = [0, 0];
    input.bounds.max = [14, 14];
    let layers: Vec<String> = ["F.Cu", "B.Cu"].map(String::from).into();
    let mut solids = Vec::new();
    for (lo, hi) in [([4, 0], [6, 14]), ([0, 8], [14, 10])] {
        let mut area = rectangle(&layers[0]);
        area.outer = vec![lo, [hi[0], lo[1]], hi, [lo[0], hi[1]]];
        input.rule_areas.push(area);
        solids.push(Solid { layer: 0, lo, hi });
    }
    let a = [1, 1];
    let b = [13, 13];
    let w = budget();
    let h = BarrierBound::for_join(
        &input,
        1,
        &input.tracks,
        &layers,
        (a, a),
        (b, b),
        0,
        2,
        3,
        5,
        &w,
    )
    .unwrap()
    .with_terminal_layers(1, 1);
    assert_eq!(h.estimate(a, 0, &w).unwrap(), 46);
    compare_oracle(&h, 2, &solids, &[(0, b)], 0, 2, 3, 5, false);
}

#[test]
fn a_generated_trace_can_prove_a_crossing_only_while_it_is_kept() {
    let input = article();
    let layers = vec!["F.Cu".into(), "B.Cu".into()];
    let mut copper = crate::state::CopperStore::new_multilayer(
        Vec::new(),
        Vec::new(),
        layers.clone(),
        Default::default(),
        "0123456789abcdef0123456789abcdef",
        0,
    );
    let w = budget();
    let from = [0, 7];
    let to = [14, 7];
    let bound = BarrierBound::for_join(
        &input,
        1,
        &input.tracks,
        &layers,
        (from, from),
        (to, to),
        0,
        1,
        1,
        3,
        &w,
    )
    .unwrap();
    let measure = |copper: &crate::state::CopperStore, net, layer| {
        let before = w.used();
        let cost = bound.routed_crossing_cost(copper, net, layer, &w).unwrap();
        (cost, w.used() - before)
    };
    copper.begin(&w).unwrap();
    copper
        .insert(track([6, 1], [6, 13], 2, "F.Cu"), &w)
        .unwrap();
    copper
        .insert(track([11, 1], [11, 3], 3, "F.Cu"), &w)
        .unwrap();
    copper.commit();
    let first = measure(&copper, 1, 0);
    assert_eq!(first.0, Some(20));
    assert_eq!(measure(&copper, 2, 0).0, None);
    assert_eq!(measure(&copper, 1, 1).0, None);
    copper.begin(&w).unwrap();
    copper.remove_net(2, &w).unwrap();
    assert_eq!(measure(&copper, 1, 0).0, None);
    copper.rollback();
    assert_eq!(measure(&copper, 1, 0), first);
}

#[test]
fn slanted_divider_is_admissible_and_consistent_for_every_grid_edge() {
    let layers = vec!["F.Cu".to_owned(), "B.Cu".to_owned()];
    let w = budget();
    for (a, b) in [
        ([3, 2], [11, 12]),
        ([5, 0], [6, 8]),
        ([3, 11], [10, 3]),
        ([2, 6], [12, 9]),
    ] {
        let mut input = article();
        input.tracks.push(track(a, b, 2, "F.Cu"));
        // Compare the rational intersection of a physical wall and an axial
        // grid step by cross-multiplication, including a clipped final step.
        let legal = |layer, p: Point, q: Point| {
            if layer != 0 {
                return true;
            }
            let k = usize::from(p[0] != q[0]);
            let j = 1 - k;
            let (u, v) = if a[k] < b[k] { (a, b) } else { (b, a) };
            if p[k] < u[k] || p[k] > v[k] {
                return true;
            }
            let den = v[k] - u[k];
            let pos = u[j] * den + (p[k] - u[k]) * (v[j] - u[j]);
            pos < p[j].min(q[j]) * den || pos > p[j].max(q[j]) * den
        };
        let from = [1, 4];
        let to = [13, 8];
        for grid in [1, 2] {
            for reach in [0, 1] {
                let h = BarrierBound::for_join(
                    &input,
                    1,
                    &input.tracks,
                    &layers,
                    (from, from),
                    (to, to),
                    reach,
                    grid,
                    3,
                    5,
                    &w,
                )
                .unwrap()
                .with_terminal_layers(1, 1);
                if a == [3, 2] && reach == 0 {
                    assert!(h.is_active());
                    assert!(h.estimate(from, 0, &w).unwrap() > 16u64.div_ceil(grid as u64) * 3);
                }
                finite_distances(&h, 2, &[(0, to)], reach, grid, 3, 5, false, legal);
                finite_distances(&h, 2, &[(0, from)], 0, grid, 3, 5, true, legal);
            }
        }
    }
}
