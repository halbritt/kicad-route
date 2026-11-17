use super::*;
use crate::model::*;
use std::collections::{BTreeSet, VecDeque};
#[path = "../tests/support/contact.rs"]
mod contact;
use contact::Body;
fn id(n: u64) -> String {
    format!("10000000-0000-4000-8000-{n:012x}")
}
fn pad(n: u64, net: u32, at: Point, shape: Shape) -> Pad {
    Pad {
        id: id(n),
        net,
        at,
        shape,
    }
}
fn track(n: u64, net: u32, a: Point, b: Point, w: i64) -> Track {
    Track {
        id: id(n),
        net,
        start: a,
        end: b,
        width_nm: w,
        locked: true,
    }
}
fn legal(i: &Input, foreign: &[Body], a: Point, b: Point) -> bool {
    let r = i.rules.width_nm + 2 * i.rules.edge_clearance_nm;
    if [a, b].iter().any(|p| {
        (0..2).any(|k| 2 * p[k] < 2 * i.bounds.min[k] + r || 2 * p[k] > 2 * i.bounds.max[k] - r)
    }) {
        return false;
    }
    let route = Body::segment(a, b, i.rules.width_nm);
    !foreign
        .iter()
        .any(|f| route.violates(*f, i.rules.clearance_nm))
}
// Position-only BFS is an independent cost oracle: uniform positive step cost
// has no bend penalty. It explicitly tries EVERY integer terminal endpoint on
// EACH edge, not a projection, production attachment, or prefix bisection.
fn oracle(
    i: &Input,
    seeds: &[Point],
    source: &[Body],
    targets: &[Body],
    foreign: &[Body],
) -> Option<u64> {
    let mut seen = BTreeSet::new();
    let mut q = VecDeque::new();
    for p in seeds {
        if legal(i, foreign, *p, *p)
            && source
                .iter()
                .any(|s| s.contacts(Body::segment(*p, *p, i.rules.width_nm)))
            && seen.insert(*p)
        {
            q.push_back((*p, 0));
        }
    }
    while let Some((a, d)) = q.pop_front() {
        for delta in [[1, 0], [0, 1], [-1, 0], [0, -1]] {
            for n in 1..=i.rules.grid_nm {
                let p = [a[0] + n * delta[0], a[1] + n * delta[1]];
                if legal(i, foreign, a, p)
                    && targets
                        .iter()
                        .any(|t| t.contacts(Body::segment(p, p, i.rules.width_nm)))
                {
                    return Some(d + 1);
                }
            }
            let b = [
                a[0] + i.rules.grid_nm * delta[0],
                a[1] + i.rules.grid_nm * delta[1],
            ];
            if legal(i, foreign, a, b) && seen.insert(b) {
                q.push_back((b, d + 1));
            }
        }
    }
    None
}
#[test]
fn exhaustive_integer_terminal_oracle_matches_component_reachability_and_cost() {
    let mut reachable = 0;
    let mut unreachable = 0;
    for offgrid in [false, true] {
        for width in [2, 3, 4, 6] {
            for clearance in [1, 2] {
                for kind in 0..4 {
                    for mask in 0..64 {
                        let mut i = Input {
                            version: 1,
                            source_id: "a".repeat(64),
                            layer: "F.Cu".into(),
                            bounds: Bounds {
                                min: [0, 0],
                                max: [20, 20],
                            },
                            rules: Rules {
                                width_nm: width,
                                clearance_nm: clearance,
                                grid_nm: 4,
                                edge_clearance_nm: 0,
                            },
                            nets: vec![
                                Net {
                                    id: 0,
                                    name: String::new(),
                                },
                                Net {
                                    id: 1,
                                    name: "N".into(),
                                },
                            ],
                            pads: vec![],
                            tracks: vec![],
                            max_expansions: 10000,
                            limits: Limits::default(),
                        };
                        if offgrid {
                            i.tracks.push(track(1, 1, [3, 3], [7, 3], 2));
                        } else {
                            i.pads
                                .push(pad(1, 1, [4, 4], Shape::Circle { radius_nm: 1 }));
                            i.tracks.push(track(2, 1, [4, 4], [4, 8], 2));
                        }
                        let target_pad = kind < 2;
                        if target_pad {
                            i.pads.push(pad(
                                3,
                                1,
                                [12, 12],
                                if kind == 0 {
                                    Shape::Circle { radius_nm: 1 }
                                } else {
                                    Shape::Rect {
                                        half_size_nm: [2, 3],
                                    }
                                },
                            ));
                        } else {
                            i.tracks.push(if kind == 2 {
                                track(3, 1, [11, 8], [11, 16], 2)
                            } else {
                                track(3, 1, [8, 12], [16, 12], 2)
                            });
                        }
                        if mask & 1 != 0 {
                            i.tracks.push(track(10, 0, [8, 0], [8, 10], 2));
                        }
                        if mask & 2 != 0 {
                            i.tracks.push(track(11, 0, [8, 8], [20, 8], 2));
                        }
                        if mask & 4 != 0 {
                            i.pads
                                .push(pad(12, 0, [8, 4], Shape::Circle { radius_nm: 1 }));
                        }
                        if mask & 8 != 0 {
                            i.pads.push(pad(
                                13,
                                0,
                                [12, 8],
                                Shape::Rect {
                                    half_size_nm: [1, 1],
                                },
                            ));
                        }
                        if mask & 16 != 0 {
                            i.tracks.push(track(14, 0, [4, 15], [16, 15], 2));
                        }
                        if mask & 32 != 0 {
                            i.tracks.push(track(15, 0, [16, 4], [16, 20], 2));
                        }
                        let mut source = vec![Copper::Track(&i.tracks[0])];
                        let mut seeds = vec![i.tracks[0].start, i.tracks[0].end];
                        let mut source_bodies = vec![Body::track(&i.tracks[0])];
                        if !offgrid {
                            source.push(Copper::Pad(&i.pads[0]));
                            seeds.push(i.pads[0].at);
                            source_bodies.push(Body::pad(&i.pads[0]));
                        }
                        let (target, target_body) = if target_pad {
                            let p = &i.pads[usize::from(!offgrid)];
                            (Copper::Pad(p), Body::pad(p))
                        } else {
                            (Copper::Track(&i.tracks[1]), Body::track(&i.tracks[1]))
                        };
                        let foreign: Vec<_> = i
                            .pads
                            .iter()
                            .filter(|p| p.net == 0)
                            .map(Body::pad)
                            .chain(i.tracks.iter().filter(|t| t.net == 0).map(Body::track))
                            .collect();
                        let expected = oracle(&i, &seeds, &source_bodies, &[target_body], &foreign);
                        let engine = Engine::new(i.clone()).unwrap();
                        let mut expanded = 0;
                        let actual = join_components(
                            &engine,
                            1,
                            &source,
                            &[vec![target]],
                            10000,
                            &mut expanded,
                        );
                        match (expected, actual) {
                            (Some(cost), Ok(path)) => {
                                reachable += 1;
                                assert_eq!(path.steps, cost,
                                    "offgrid={offgrid} width={width} clearance={clearance} kind={kind} mask={mask}");
                                assert!(path.points.windows(2).all(|p| legal(&i, &foreign, p[0], p[1])));
                                let last = *path.points.last().unwrap();
                                assert!(target_body.contacts(Body::segment(last, last, width)));
                            }
                            (None, Err(e)) => {
                                unreachable += 1;
                                assert_eq!(e.status, Status::Unroutable);
                            }
                            (expected, actual) => panic!(
                                "offgrid={offgrid} width={width} clearance={clearance} kind={kind} mask={mask}: oracle={expected:?} search={actual:?}"),
                        }
                    }
                }
            }
        }
    }
    assert!(reachable > 0 && unreachable > 0);
    eprintln!("4096 small worlds: {reachable} reachable, {unreachable} unreachable");
}
#[test]
fn diagonal_interval_between_integer_endpoints_is_not_a_contact() {
    let t = track(1, 1, [5, 1], [8, 8], 3); // effective radius 0 with a width1 route
    assert!(Copper::Track(&t)
        .attachment(
            [4, 4],
            [9, 4],
            1,
            1,
            &crate::budget::WorkBudget::new(Limits::default())
        )
        .unwrap()
        .is_none());
    let t = track(1, 1, [5, 1], [8, 7], 3); // intersection at (6,3) is integral
    assert_eq!(
        Copper::Track(&t)
            .attachment(
                [4, 3],
                [9, 3],
                1,
                1,
                &crate::budget::WorkBudget::new(Limits::default())
            )
            .unwrap(),
        Some([6, 3])
    );
}

#[test]
fn nearer_contact_wins_regardless_of_target_order() {
    let source = track(1, 1, [4, 4], [4, 5], 2);
    let near = track(2, 1, [8, 3], [8, 6], 2);
    let far = track(3, 1, [10, 3], [10, 6], 2);
    let i = Input {
        version: 1,
        source_id: "a".repeat(64),
        layer: "F.Cu".into(),
        bounds: Bounds {
            min: [0, 0],
            max: [20, 20],
        },
        rules: Rules {
            width_nm: 2,
            clearance_nm: 1,
            grid_nm: 8,
            edge_clearance_nm: 0,
        },
        nets: vec![Net {
            id: 1,
            name: "N".into(),
        }],
        pads: vec![],
        tracks: vec![source.clone(), near.clone(), far.clone()],
        max_expansions: 1000,
        limits: Limits::default(),
    };
    let engine = Engine::new(i).unwrap();
    for targets in [
        vec![vec![Copper::Track(&far)], vec![Copper::Track(&near)]],
        vec![vec![Copper::Track(&near)], vec![Copper::Track(&far)]],
    ] {
        let mut expanded = 0;
        let path = join_components(
            &engine,
            1,
            &[Copper::Track(&source)],
            &targets,
            1000,
            &mut expanded,
        )
        .unwrap();
        assert_eq!(path.steps, 1);
        assert_eq!(path.points.last().unwrap()[0], 7);
    }
}

#[test]
fn contact_bound_contains_caps_and_is_consistent_on_shortened_edges() {
    let circle = pad(1, 1, [11, 12], Shape::Circle { radius_nm: 3 });
    let rect = pad(
        2,
        1,
        [11, 12],
        Shape::Rect {
            half_size_nm: [2, 4],
        },
    );
    let vertical = track(3, 1, [13, 6], [13, 16], 3);
    let horizontal = track(4, 1, [6, 13], [16, 13], 5);
    let diagonals = [
        track(5, 1, [-15, -11], [14, 7], 3),
        track(6, 1, [-12, 13], [11, -14], 5),
        track(7, 1, [-6, -15], [4, 17], 7),
        track(8, 1, [-16, 3], [15, -4], 1),
        track(9, 1, [14, 7], [-15, -11], 3),
        track(10, 1, [11, -14], [-12, 13], 5),
        track(11, 1, [4, 17], [-6, -15], 7),
        track(12, 1, [15, -4], [-16, 3], 1),
    ];
    let mut targets = vec![
        Copper::Pad(&circle),
        Copper::Pad(&rect),
        Copper::Track(&vertical),
        Copper::Track(&horizontal),
    ];
    targets.extend(diagonals.iter().map(Copper::Track));
    let mut contacts = vec![0; targets.len()];
    let work = WorkBudget::new(Limits::default());
    for width in [1, 2, 3, 5, 6, 7] {
        for grid in [1, 4, 7] {
            for x in -20..=24 {
                for y in -20..=24 {
                    let p = [x, y];
                    let cap = track(100, 1, p, p, width);
                    for (n, target) in targets.iter().enumerate() {
                        let h = contact_steps(p, *target, width, grid);
                        // This is the actual search goal predicate, including
                        // arbitrary-angle incumbent copper and odd half-widths.
                        // The separate axis-aligned cost oracles stay independent.
                        if target.touches_route(&cap, &work).unwrap() {
                            contacts[n] += 1;
                            assert_eq!(h, 0, "production goal cap must have zero heuristic: target={n} point={p:?} width={width}");
                        }
                        for delta in [[1, 0], [0, 1], [-1, 0], [0, -1]] {
                            for length in 1..=grid {
                                let q = [x + length * delta[0], y + length * delta[1]];
                                assert!(h <= 1 + contact_steps(q, *target, width, grid),
                                    "inconsistent target={n} edge={p:?}->{q:?} width={width} grid={grid}");
                            }
                        }
                    }
                }
            }
        }
    }
    assert!(contacts.iter().all(|n| *n > 0));
    eprintln!("production contact counts per target: {contacts:?}");
}

#[test]
fn component_estimate_charges_every_target_in_the_outer_work_scope() {
    let rules = Rules {
        width_nm: 3,
        clearance_nm: 1,
        grid_nm: 4,
        edge_clearance_nm: 0,
    };
    let near = pad(1, 1, [4, 4], Shape::Circle { radius_nm: 2 });
    let far = track(2, 1, [12, 5], [17, 19], 3);
    let work = WorkBudget::new(Limits::default());
    let _outer = work.scope();
    work.charge(7).unwrap(); // unrelated work already spent by the caller
    let mut expected = 7;
    for count in [1, 2, 5] {
        // First target has estimate zero. Even then each later item in this
        // component and the other component must charge its own evaluation.
        let targets = vec![
            vec![Copper::Pad(&near)],
            vec![Copper::Track(&far); count - 1],
        ];
        {
            let _child = work.scope();
            assert_eq!(
                component_estimate([4, 4], &targets, &rules, &work).unwrap(),
                0
            );
            expected += count as u64;
            assert_eq!(
                work.used(),
                expected,
                "one charged evaluation per target item"
            );
        }
        assert_eq!(
            work.used(),
            expected,
            "leaving a child scope must not refund estimates"
        );
    }
}

#[test]
fn component_estimate_refuses_at_each_exact_work_cutoff() {
    let rules = Rules {
        width_nm: 3,
        clearance_nm: 1,
        grid_nm: 4,
        edge_clearance_nm: 0,
    };
    let near = pad(1, 1, [4, 4], Shape::Circle { radius_nm: 2 });
    let far = track(2, 1, [12, 5], [17, 19], 3);
    let targets = vec![
        vec![Copper::Pad(&near), Copper::Track(&far)],
        vec![Copper::Track(&far); 3],
    ];
    for available in 0..=5 {
        let work = WorkBudget::new(Limits {
            max_work: 7 + available,
            ..Limits::default()
        });
        let _outer = work.scope();
        work.charge(7).unwrap();
        {
            let _child = work.scope();
            let result = component_estimate([4, 4], &targets, &rules, &work);
            if available == 5 {
                assert_eq!(result.unwrap(), 0);
            } else {
                let error = result.unwrap_err();
                assert_eq!(error.status, Status::WorkExhausted);
                assert_eq!(error.work_used, 7 + available);
            }
        }
        assert_eq!(work.used(), 7 + available);
        // A handled child error (or success exactly at the limit) cannot make
        // any unit available for a second estimate in this same outer scope.
        let error = component_estimate([4, 4], &targets, &rules, &work).unwrap_err();
        assert_eq!(error.status, Status::WorkExhausted);
        assert_eq!(error.work_used, 7 + available);
    }
}

#[test]
fn multi_target_astar_matches_independent_cost_oracle_in_256_worlds() {
    let mut reachable = 0;
    let mut unreachable = 0;
    for width in [2, 3, 4, 6] {
        for grid in [4, 5] {
            for shift in [0, 1] {
                for mask in 0..16 {
                    let source = track(1, 1, [3 + shift, 3], [7 + shift, 3], 2);
                    let target_pad =
                        pad(2, 1, [3 * grid, 3 * grid], Shape::Circle { radius_nm: 2 });
                    let target_track = track(3, 1, [16, 5], [16, 9], 3);
                    let mut i = Input {
                        version: 1,
                        source_id: "a".repeat(64),
                        layer: "F.Cu".into(),
                        bounds: Bounds {
                            min: [0, 0],
                            max: [20, 20],
                        },
                        rules: Rules {
                            width_nm: width,
                            clearance_nm: 1,
                            grid_nm: grid,
                            edge_clearance_nm: 0,
                        },
                        nets: vec![
                            Net {
                                id: 0,
                                name: String::new(),
                            },
                            Net {
                                id: 1,
                                name: "N".into(),
                            },
                        ],
                        pads: vec![target_pad.clone()],
                        tracks: vec![source.clone(), target_track.clone()],
                        max_expansions: 10000,
                        limits: Limits::default(),
                    };
                    for (bit, a, b) in [
                        (1, [10, 0], [10, 10]),
                        (2, [10, 10], [10, 20]),
                        (4, [0, 10], [10, 10]),
                        (8, [10, 10], [20, 10]),
                    ] {
                        if mask & bit != 0 {
                            i.tracks.push(track(10 + bit, 0, a, b, 2));
                        }
                    }
                    let foreign: Vec<_> = i
                        .tracks
                        .iter()
                        .filter(|t| t.net == 0)
                        .map(Body::track)
                        .collect();
                    let expected = oracle(
                        &i,
                        &[source.start, source.end],
                        &[Body::track(&source)],
                        &[Body::pad(&target_pad), Body::track(&target_track)],
                        &foreign,
                    );
                    let engine = Engine::new(i.clone()).unwrap();
                    let targets = [
                        vec![Copper::Pad(&target_pad)],
                        vec![Copper::Track(&target_track)],
                    ];
                    let actual = join_components(
                        &engine,
                        1,
                        &[Copper::Track(&source)],
                        &targets,
                        10000,
                        &mut 0,
                    );
                    match (expected, actual) {
                        (Some(cost), Ok(path)) => {
                            reachable += 1;
                            assert_eq!(path.steps, cost, "width={width} grid={grid} shift={shift} mask={mask}");
                            assert!(path.points.windows(2).all(|p| legal(&i, &foreign, p[0], p[1])));
                            let cap = Body::segment(*path.points.last().unwrap(), *path.points.last().unwrap(), width);
                            assert!(Body::pad(&target_pad).contacts(cap) || Body::track(&target_track).contacts(cap));
                        }
                        (None, Err(e)) => { unreachable += 1; assert_eq!(e.status, Status::Unroutable); }
                        (expected, actual) => panic!("width={width} grid={grid} shift={shift} mask={mask}: oracle={expected:?} search={actual:?}"),
                    }
                }
            }
        }
    }
    assert!(reachable > 0 && unreachable > 0);
    eprintln!("256 multi-target worlds: {reachable} reachable, {unreachable} unreachable");
}

#[test]
fn walk_preserves_distinct_incoming_headings_in_dominance_key() {
    let i = Input {
        version: 1,
        source_id: "a".repeat(64),
        layer: "F.Cu".into(),
        bounds: Bounds {
            min: [0, 0],
            max: [10, 10],
        },
        rules: Rules {
            width_nm: 2,
            clearance_nm: 0,
            grid_nm: 1,
            edge_clearance_nm: 0,
        },
        nets: vec![Net {
            id: 1,
            name: "N".into(),
        }],
        pads: vec![],
        tracks: vec![],
        max_expansions: 100,
        limits: Limits::default(),
    };
    let engine = Engine::new(i).unwrap();
    let mut expanded = 0;
    // Both [0,0]->[1,0]->[1,1] and [0,0]->[0,1]->[1,1] settle [1,1] at g=2.
    // Reaching [1,1] from [2,1] at g=4 requires State to retain incoming direction.
    let path = walk(
        &engine,
        &[[0, 0]],
        100,
        &mut expanded,
        |_| Ok(0),
        |_, b| Ok(((0..=2).contains(&b[0]) && (0..=1).contains(&b[1])).then_some(b)),
        |at, previous| Ok(at == [1, 1] && previous == Some([2, 1])),
    )
    .unwrap();
    assert_eq!(path.steps, 4);
    assert_eq!(path.points.last().copied(), Some([1, 1]));
    assert_eq!(
        path.points.get(path.points.len() - 2).copied(),
        Some([2, 1])
    );
}

#[test]
fn walk_matches_heading_aware_bfs_oracle_for_all_four_headings() {
    let i = Input {
        version: 1,
        source_id: "a".repeat(64),
        layer: "F.Cu".into(),
        bounds: Bounds {
            min: [0, 0],
            max: [10, 10],
        },
        rules: Rules {
            width_nm: 2,
            clearance_nm: 0,
            grid_nm: 1,
            edge_clearance_nm: 0,
        },
        nets: vec![Net {
            id: 1,
            name: "N".into(),
        }],
        pads: vec![],
        tracks: vec![],
        max_expansions: 1000,
        limits: Limits::default(),
    };
    let engine = Engine::new(i).unwrap();
    let blocks = [[1, 1], [3, 1], [1, 3], [3, 3], [1, 2], [2, 1]];
    let required_prevs = [
        [1, 2], // direction 0: +x
        [2, 1], // direction 1: +y
        [3, 2], // direction 2: -x
        [2, 3], // direction 3: -y
    ];
    let mut reachable = 0;
    let mut unreachable = 0;
    for prev_req in required_prevs {
        for mask in 0..(1u32 << blocks.len()) {
            let blocked: BTreeSet<Point> = blocks
                .iter()
                .enumerate()
                .filter_map(|(idx, &pt)| ((mask & (1 << idx)) != 0).then_some(pt))
                .collect();
            let in_bounds = |p: Point| {
                (0..=4).contains(&p[0]) && (0..=4).contains(&p[1]) && !blocked.contains(&p)
            };
            // Independent heading-aware (Point, Option<u8>) BFS oracle.
            let oracle = {
                let mut seen: BTreeSet<(Point, Option<u8>)> = BTreeSet::from([([0, 0], None)]);
                let mut q: VecDeque<(Point, Option<Point>, u64)> =
                    VecDeque::from([([0, 0], None, 0)]);
                let mut found = None;
                while let Some((at, prev, d)) = q.pop_front() {
                    if at == [2, 2] && prev == Some(prev_req) {
                        found = Some(d);
                        break;
                    }
                    for (dir, delta) in [[1, 0], [0, 1], [-1, 0], [0, -1]].iter().enumerate() {
                        let next = [at[0] + delta[0], at[1] + delta[1]];
                        if in_bounds(next) && seen.insert((next, Some(dir as u8))) {
                            q.push_back((next, Some(at), d + 1));
                        }
                    }
                }
                found
            };
            let mut expanded = 0;
            let actual = walk(
                &engine,
                &[[0, 0]],
                1000,
                &mut expanded,
                |_| Ok(0),
                |_, b| Ok(in_bounds(b).then_some(b)),
                |at, prev| Ok(at == [2, 2] && prev == Some(prev_req)),
            );
            match (oracle, actual) {
                (Some(cost), Ok(path)) => {
                    reachable += 1;
                    assert_eq!(path.steps, cost, "prev_req={prev_req:?} mask={mask}");
                    assert_eq!(path.points.last().copied(), Some([2, 2]));
                    assert_eq!(
                        path.points.get(path.points.len().wrapping_sub(2)).copied(),
                        Some(prev_req)
                    );
                }
                (None, Err(err)) => {
                    unreachable += 1;
                    assert_eq!(err.status, Status::Unroutable);
                }
                (expected, actual) => {
                    panic!("prev_req={prev_req:?} mask={mask}: oracle={expected:?} walk={actual:?}")
                }
            }
        }
    }
    assert!(reachable > 0 && unreachable > 0);
}

#[test]
fn component_estimate_and_join_components_refuse_empty_targets_without_panicking() {
    let rules = Rules {
        width_nm: 2,
        clearance_nm: 1,
        grid_nm: 10,
        edge_clearance_nm: 0,
    };
    let work = WorkBudget::new(Limits::default());
    let empty: Vec<Vec<Copper<'_>>> = Vec::new();
    let one_empty: Vec<Vec<Copper<'_>>> = vec![vec![]];
    for targets in [&empty, &one_empty] {
        let _scope = work.scope();
        let err = component_estimate([10, 10], targets, &rules, &work).unwrap_err();
        assert_eq!(err.status, Status::Unroutable);
    }
    let p = pad(1, 1, [10, 10], Shape::Circle { radius_nm: 1 });
    let i = Input {
        version: 1,
        source_id: "a".repeat(64),
        layer: "F.Cu".into(),
        bounds: Bounds {
            min: [0, 0],
            max: [40, 40],
        },
        rules,
        nets: vec![Net {
            id: 1,
            name: "N".into(),
        }],
        pads: vec![p.clone()],
        tracks: vec![],
        max_expansions: 100,
        limits: Limits::default(),
    };
    let engine = Engine::new(i).unwrap();
    for targets in [&empty, &one_empty] {
        let mut expanded = 0;
        let err = join_components(&engine, 1, &[Copper::Pad(&p)], targets, 100, &mut expanded)
            .unwrap_err();
        assert_eq!(err.status, Status::Unroutable);
        assert_eq!(expanded, 0);
    }
}
