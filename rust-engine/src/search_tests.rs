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
        layers: Vec::new(),
        local_clearance_nm: None,
        shape,
        drill: None,
    }
}
fn track(n: u64, net: u32, a: Point, b: Point, w: i64) -> Track {
    Track {
        id: id(n),
        net,
        layer: "F.Cu".into(),
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
                            layers: Vec::new(),
                            bounds: Bounds {
                                min: [0, 0],
                                max: [20, 20],
                            },
                            rules: Rules {
                                width_nm: width,
                                clearance_nm: clearance,
                                grid_nm: 4,
                                edge_clearance_nm: 0,
                                hole_clearance_nm: 0,
                                same_net_hole_clearance_nm: 0,
                                solder_mask_expansion_nm: 0,
                                solder_mask_min_width_nm: 0,
                                solder_mask_to_copper_clearance_nm: 0,
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
                            vias: Vec::new(),
                            rule_areas: Vec::new(),
                            max_expansions: 10000,
                            limits: Limits::default(),
                            holes: Vec::new(),
                            board_outline: None,
                            graphics: Vec::new(),
                            mask_apertures: Vec::new(),
                            board_design_settings: None,
                            project_rules: None,
                            custom_rules: None,
                            effective_rules: None,
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
    let stack = [String::from("F.Cu")];
    assert!(Copper::Track(&t)
        .attachment(
            [4, 4],
            [9, 4],
            1,
            1,
            "F.Cu",
            &stack,
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
                "F.Cu",
                &stack,
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
        layers: Vec::new(),
        bounds: Bounds {
            min: [0, 0],
            max: [20, 20],
        },
        rules: Rules {
            width_nm: 2,
            clearance_nm: 1,
            grid_nm: 8,
            edge_clearance_nm: 0,
            hole_clearance_nm: 0,
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
        },
        nets: vec![Net {
            id: 1,
            name: "N".into(),
        }],
        pads: vec![],
        tracks: vec![source.clone(), near.clone(), far.clone()],
        vias: Vec::new(),
        rule_areas: Vec::new(),
        max_expansions: 1000,
        limits: Limits::default(),
        holes: Vec::new(),
        board_outline: None,
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
        board_design_settings: None,
        project_rules: None,
        custom_rules: None,
        effective_rules: None,
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
    let rounded = pad(
        5,
        1,
        [11, 12],
        Shape::RoundRect {
            half_size_nm: [4, 3],
            radius_nm: 2,
        },
    );
    let capsule = pad(
        6,
        1,
        [11, 12],
        Shape::RoundRect {
            half_size_nm: [3, 5],
            radius_nm: 3,
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
        Copper::Pad(&rounded),
        Copper::Pad(&capsule),
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
                        if target
                            .touches_route(&cap, &[String::from("F.Cu")], &work)
                            .unwrap()
                        {
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
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
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
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
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
                        layers: Vec::new(),
                        bounds: Bounds {
                            min: [0, 0],
                            max: [20, 20],
                        },
                        rules: Rules {
                            width_nm: width,
                            clearance_nm: 1,
                            grid_nm: grid,
                            edge_clearance_nm: 0,
                            hole_clearance_nm: 0,
                            same_net_hole_clearance_nm: 0,
                            solder_mask_expansion_nm: 0,
                            solder_mask_min_width_nm: 0,
                            solder_mask_to_copper_clearance_nm: 0,
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
                        vias: Vec::new(),
                        rule_areas: Vec::new(),
                        max_expansions: 10000,
                        limits: Limits::default(),
                        holes: Vec::new(),
                        board_outline: None,
                        graphics: Vec::new(),
                        mask_apertures: Vec::new(),
                        board_design_settings: None,
                        project_rules: None,
                        custom_rules: None,
                        effective_rules: None,
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
        layers: Vec::new(),
        bounds: Bounds {
            min: [0, 0],
            max: [10, 10],
        },
        rules: Rules {
            width_nm: 2,
            clearance_nm: 0,
            grid_nm: 1,
            edge_clearance_nm: 0,
            hole_clearance_nm: 0,
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
        },
        nets: vec![Net {
            id: 1,
            name: "N".into(),
        }],
        pads: vec![],
        tracks: vec![],
        vias: Vec::new(),
        rule_areas: Vec::new(),
        max_expansions: 100,
        limits: Limits::default(),
        holes: Vec::new(),
        board_outline: None,
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
        board_design_settings: None,
        project_rules: None,
        custom_rules: None,
        effective_rules: None,
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
        |_, _, b| Ok(((0..=2).contains(&b[0]) && (0..=1).contains(&b[1])).then_some(b)),
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
        layers: Vec::new(),
        bounds: Bounds {
            min: [0, 0],
            max: [10, 10],
        },
        rules: Rules {
            width_nm: 2,
            clearance_nm: 0,
            grid_nm: 1,
            edge_clearance_nm: 0,
            hole_clearance_nm: 0,
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
        },
        nets: vec![Net {
            id: 1,
            name: "N".into(),
        }],
        pads: vec![],
        tracks: vec![],
        vias: Vec::new(),
        rule_areas: Vec::new(),
        max_expansions: 1000,
        limits: Limits::default(),
        holes: Vec::new(),
        board_outline: None,
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
        board_design_settings: None,
        project_rules: None,
        custom_rules: None,
        effective_rules: None,
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
                |_, _, b| Ok(in_bounds(b).then_some(b)),
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
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
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
        layers: Vec::new(),
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
        vias: Vec::new(),
        rule_areas: Vec::new(),
        max_expansions: 100,
        limits: Limits::default(),
        holes: Vec::new(),
        board_outline: None,
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
        board_design_settings: None,
        project_rules: None,
        custom_rules: None,
        effective_rules: None,
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

#[test]
fn join_components_empty_starts_records_work_used_and_expanded_states() {
    let rules = Rules {
        width_nm: 2,
        clearance_nm: 10,
        grid_nm: 10,
        edge_clearance_nm: 0,
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    let p1 = pad(1, 1, [10, 10], Shape::Circle { radius_nm: 1 });
    let p2 = pad(2, 1, [40, 40], Shape::Circle { radius_nm: 1 });
    let blocker = pad(3, 2, [20, 10], Shape::Circle { radius_nm: 1 });
    let i = Input {
        version: 1,
        source_id: "a".repeat(64),
        layer: "F.Cu".into(),
        layers: Vec::new(),
        bounds: Bounds {
            min: [0, 0],
            max: [50, 50],
        },
        rules,
        nets: vec![
            Net {
                id: 1,
                name: "N1".into(),
            },
            Net {
                id: 2,
                name: "N2".into(),
            },
        ],
        pads: vec![p1.clone(), p2.clone(), blocker],
        tracks: vec![],
        vias: Vec::new(),
        rule_areas: Vec::new(),
        max_expansions: 100,
        limits: Limits::default(),
        holes: Vec::new(),
        board_outline: None,
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
        board_design_settings: None,
        project_rules: None,
        custom_rules: None,
        effective_rules: None,
    };
    let engine = Engine::new(i).unwrap();
    let mut expanded = 0;
    let err = join_components(
        &engine,
        1,
        &[Copper::Pad(&p1)],
        &[vec![Copper::Pad(&p2)]],
        100,
        &mut expanded,
    )
    .unwrap_err();
    assert_eq!(err.status, Status::Unroutable);
    assert_eq!(expanded, 0);
    assert_eq!(err.expanded_states, 0);
    assert!(err.work_used > 0);
}

#[test]
fn multilayer_astar_matches_independent_augmented_dijkstra_oracle() {
    use std::cmp::Reverse;
    use std::collections::{BinaryHeap, HashMap};

    let stack = vec!["F.Cu".to_owned(), "In1.Cu".to_owned(), "B.Cu".to_owned()];
    let mut reachable = 0usize;
    let mut unreachable = 0usize;
    for allow_blind in [false, true] {
        for mask in 0..32u32 {
            let mut input = Input {
                version: 1,
                source_id: "b".repeat(64),
                layer: "F.Cu".into(),
                layers: stack.clone(),
                bounds: Bounds {
                    min: [0, 0],
                    max: [40, 20],
                },
                rules: Rules {
                    width_nm: 2,
                    clearance_nm: 1,
                    grid_nm: 10,
                    edge_clearance_nm: 1,
                    hole_clearance_nm: 2,
                    same_net_hole_clearance_nm: 0,
                    solder_mask_expansion_nm: 0,
                    solder_mask_min_width_nm: 0,
                    solder_mask_to_copper_clearance_nm: 0,
                },
                nets: vec![
                    Net {
                        id: 0,
                        name: String::new(),
                    },
                    Net {
                        id: 1,
                        name: "SIG".into(),
                    },
                ],
                pads: vec![
                    Pad {
                        id: id(1),
                        net: 1,
                        at: [10, 10],
                        layers: vec!["F.Cu".into()],
                        local_clearance_nm: None,
                        shape: Shape::Circle { radius_nm: 1 },
                        drill: None,
                    },
                    Pad {
                        id: id(2),
                        net: 1,
                        at: [30, 10],
                        layers: vec!["B.Cu".into()],
                        local_clearance_nm: None,
                        shape: Shape::Circle { radius_nm: 1 },
                        drill: None,
                    },
                ],
                tracks: Vec::new(),
                vias: Vec::new(),
                rule_areas: Vec::new(),
                max_expansions: 10_000,
                limits: Limits::default(),
                holes: Vec::new(),
                board_outline: None,
                graphics: Vec::new(),
                mask_apertures: Vec::new(),
                board_design_settings: None,
                project_rules: None,
                custom_rules: None,
                effective_rules: None,
            };
            input.project_rules = Some(
                serde_json::json!({
                    "meta": {"version": 3},
                    "board": {
                        "design_settings": {
                            "rules": {
                                "min_via_diameter": 0.000004,
                                "min_through_hole_diameter": 0.000002,
                                "min_hole_to_hole": 0.000002,
                                "allow_blind_buried_vias": allow_blind,
                                "allow_microvias": false
                            }
                        }
                    },
                    "net_settings": {
                        "classes": [{
                            "name": "Default",
                            "track_width": 0.000002,
                            "clearance": 0.000001,
                            "via_diameter": 0.000004,
                            "via_drill": 0.000002
                        }]
                    }
                })
                .to_string(),
            );
            let blockers = [
                ("F.Cu", [20, 10]),
                ("In1.Cu", [20, 10]),
                ("B.Cu", [20, 10]),
                ("F.Cu", [30, 10]),
                ("B.Cu", [10, 10]),
            ];
            for (bit, (lname, pt)) in blockers.iter().enumerate() {
                if (mask & (1 << bit)) != 0 {
                    input.pads.push(Pad {
                        id: id(10 + bit as u64),
                        net: 0,
                        at: *pt,
                        layers: vec![(*lname).to_owned()],
                        local_clearance_nm: None,
                        shape: Shape::Circle { radius_nm: 2 },
                        drill: None,
                    });
                }
            }
            let engine = Engine::new(input).unwrap();
            let src_pad = &engine.input.pads[0];
            let dst_pad = &engine.input.pads[1];
            let actual = join_components(
                &engine,
                1,
                &[Copper::Pad(src_pad)],
                &[vec![Copper::Pad(dst_pad)]],
                10_000,
                &mut 0,
            );

            // Independent state-augmented Dijkstra oracle over (at, layer, sorted placed vias).
            let via_cost = engine.effective_rules.via_cost.max(1);
            let oracle_cost = {
                let mut dist: HashMap<(Point, usize, Vec<PlacedViaKey>), u64> = HashMap::new();
                let mut pq: BinaryHeap<Reverse<(u64, Point, usize, Vec<PlacedViaKey>)>> =
                    BinaryHeap::new();
                if engine.legal_edge_on(1, 0, [10, 10], [10, 10]).unwrap() {
                    dist.insert(([10, 10], 0, Vec::new()), 0);
                    pq.push(Reverse((0, [10, 10], 0, Vec::new())));
                }
                let mut found = None;
                while let Some(Reverse((g, p, l, vias))) = pq.pop() {
                    if dist.get(&(p, l, vias.clone())).is_some_and(|&d| d < g) {
                        continue;
                    }
                    if p == [30, 10] && l == 2 {
                        found = Some(g);
                        break;
                    }
                    for d in [[10, 0], [-10, 0], [0, 10], [0, -10]] {
                        let q = [p[0] + d[0], p[1] + d[1]];
                        if engine.legal_edge_on(1, l, p, q).unwrap() {
                            let ng = g + 1;
                            let k = (q, l, vias.clone());
                            if dist.get(&k).is_none_or(|&old| ng < old) {
                                dist.insert(k, ng);
                                pq.push(Reverse((ng, q, l, vias.clone())));
                            }
                        }
                    }
                    for tl in 0..3usize {
                        if tl == l {
                            continue;
                        }
                        let mut cands = vec![(0, 2, ViaKind::Through, 0u8)];
                        if allow_blind && (l.min(tl) > 0 || l.max(tl) < 2) {
                            cands.push((l.min(tl), l.max(tl), ViaKind::BlindBuried, 1u8));
                        }
                        for (s, e, kind, kcode) in cands {
                            let pv = PlacedViaKey {
                                at: p,
                                start_layer: s as u8,
                                end_layer: e as u8,
                                kind: kcode,
                            };
                            if vias.contains(&pv) {
                                continue;
                            }
                            if engine.legal_via(1, p, s, e, kind, &vias).unwrap().is_some() {
                                let mut nv = vias.clone();
                                nv.push(pv);
                                nv.sort_unstable();
                                let ng = g + via_cost;
                                let k = (p, tl, nv.clone());
                                if dist.get(&k).is_none_or(|&old| ng < old) {
                                    dist.insert(k, ng);
                                    pq.push(Reverse((ng, p, tl, nv)));
                                }
                            }
                        }
                    }
                }
                found
            };
            match (oracle_cost, actual) {
                (Some(c), Ok(path)) => {
                    reachable += 1;
                    assert_eq!(path.steps, c, "allow_blind={allow_blind} mask={mask}");
                    for e in &path.edges {
                        if let PathEdge::Track { start, end, layer } = *e {
                            assert!(engine.legal_edge_on(1, layer as usize, start, end).unwrap());
                        }
                    }
                }
                (None, Err(err)) => {
                    unreachable += 1;
                    assert_eq!(err.status, Status::Unroutable);
                }
                (exp, got) => panic!(
                    "mismatch allow_blind={allow_blind} mask={mask}: oracle={exp:?} got={got:?}"
                ),
            }
        }
    }
    assert!(reachable > 0 && unreachable > 0);
}

#[test]
fn two_via_position_only_witness_and_max_expansions_budget() {
    // 2-via position-only witness: short F.Cu->In1.Cu->B.Cu path places V1 at [20, 10] which
    // blocks the only exit via V2 at [20, 20] (dist 10 < hole_to_hole 12); longer path places
    // V1' at [0, 10] and succeeds. Also checks max_expansions=15 returns BudgetExhausted, never Unroutable.
    let stack = vec!["F.Cu".to_owned(), "In1.Cu".to_owned(), "B.Cu".to_owned()];
    let mut input = Input {
        version: 1,
        source_id: "c".repeat(64),
        layer: "F.Cu".into(),
        layers: stack,
        bounds: Bounds {
            min: [-10, 0],
            max: [50, 40],
        },
        rules: Rules {
            width_nm: 2,
            clearance_nm: 1,
            grid_nm: 10,
            edge_clearance_nm: 1,
            hole_clearance_nm: 2,
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
        },
        nets: vec![
            Net {
                id: 0,
                name: String::new(),
            },
            Net {
                id: 1,
                name: "SIG".into(),
            },
        ],
        pads: vec![
            Pad {
                id: id(1),
                net: 1,
                at: [10, 10],
                layers: vec!["F.Cu".into()],
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 1 },
                drill: None,
            },
            Pad {
                id: id(2),
                net: 1,
                at: [20, 30],
                layers: vec!["B.Cu".into()],
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 1 },
                drill: None,
            },
        ],
        tracks: vec![
            Track {
                id: id(10),
                net: 0,
                layer: "In1.Cu".into(),
                start: [20, 30],
                end: [30, 30],
                width_nm: 4,
                locked: true,
            },
            Track {
                id: id(11),
                net: 0,
                layer: "In1.Cu".into(),
                start: [10, 20],
                end: [10, 30],
                width_nm: 4,
                locked: true,
            },
            Track {
                id: id(12),
                net: 0,
                layer: "F.Cu".into(),
                start: [10, 20],
                end: [20, 20],
                width_nm: 4,
                locked: true,
            },
            Track {
                id: id(15),
                net: 0,
                layer: "F.Cu".into(),
                start: [0, 30],
                end: [30, 30],
                width_nm: 4,
                locked: true,
            },
            Track {
                id: id(13),
                net: 0,
                layer: "B.Cu".into(),
                start: [0, 10],
                end: [30, 10],
                width_nm: 4,
                locked: true,
            },
            Track {
                id: id(14),
                net: 0,
                layer: "B.Cu".into(),
                start: [30, 10],
                end: [30, 30],
                width_nm: 4,
                locked: true,
            },
            Track {
                id: id(16),
                net: 0,
                layer: "B.Cu".into(),
                start: [0, 20],
                end: [10, 30],
                width_nm: 4,
                locked: true,
            },
            Track {
                id: id(17),
                net: 0,
                layer: "B.Cu".into(),
                start: [30, 30],
                end: [40, 30],
                width_nm: 4,
                locked: true,
            },
        ],
        vias: Vec::new(),
        rule_areas: Vec::new(),
        max_expansions: 5000,
        limits: Limits::default(),
        holes: Vec::new(),
        board_outline: None,
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
        board_design_settings: None,
        project_rules: None,
        custom_rules: None,
        effective_rules: None,
    };
    input.project_rules = Some(
        serde_json::json!({
            "meta": {"version": 3},
            "board": {"design_settings": {"rules": {
                "min_via_diameter": 0.000004,
                "min_through_hole_diameter": 0.000002,
                "min_hole_to_hole": 0.000010,
                "allow_blind_buried_vias": true,
                "allow_microvias": false
            }}},
            "net_settings": {
                "classes": [{
                    "name": "Default",
                    "track_width": 0.000002,
                    "clearance": 0.000001,
                    "via_diameter": 0.000004,
                    "via_drill": 0.000002
                }]
            }
        })
        .to_string(),
    );
    let engine = Engine::new(input.clone()).unwrap();
    let res = join_components(
        &engine,
        1,
        &[Copper::Pad(&engine.input.pads[0])],
        &[vec![Copper::Pad(&engine.input.pads[1])]],
        5000,
        &mut 0,
    )
    .expect("2-via position-only witness must find legal route");
    let via_count = res
        .edges
        .iter()
        .filter(|e| matches!(e, PathEdge::Via(_)))
        .count();
    assert!(via_count >= 2);
    for e in &res.edges {
        if let PathEdge::Track { start, end, layer } = *e {
            assert!(engine.legal_edge_on(1, layer as usize, start, end).unwrap());
        }
    }

    // max_expansions=15 on a distant target returns BudgetExhausted, never Unroutable.
    let mut far_input = input;
    far_input.bounds = Bounds {
        min: [-10, 0],
        max: [700, 40],
    };
    far_input.tracks.clear();
    far_input.pads[1].at = [650, 10];
    let far_engine = Engine::new(far_input).unwrap();
    let err = join_components(
        &far_engine,
        1,
        &[Copper::Pad(&far_engine.input.pads[0])],
        &[vec![Copper::Pad(&far_engine.input.pads[1])]],
        15,
        &mut 0,
    )
    .unwrap_err();
    assert_eq!(err.status, Status::BudgetExhausted);
}

#[test]
fn three_via_last_via_only_counterexample_witness_re02() {
    use std::cmp::Reverse;
    use std::collections::{BinaryHeap, HashMap};

    // 4-layer world with blind/buried vias and hole_to_hole=10 (min center dist 12 on grid 10):
    // Path A: [10,10](F.Cu)->[20,10]->V1(F..In2)->[30,10]->[40,10]->[40,20]->V2(In1..In2)->[30,20]->[20,20](In1.Cu)
    // Path B: [10,10](F.Cu)->[0,10]->V1'(F..In2)->[0,20]->[0,30]->[40,30]->[40,20]->V2(In1..In2)->[30,20]->[20,20](In1.Cu)
    // Both reach ([20,20], In1.Cu, dir=-x) with last_via=V2@[40,20]. Path A is shorter, so a last-via-only
    // probe settles ([20,20], In1.Cu, -x, V2) with {V1@[20,10], V2@[40,20]} and drops Path B. The only exit
    // to target [20,30](B.Cu) is V3@[20,20](In1..B), which conflicts with V1@[20,10] (dist 10 < 12) so
    // last_via_only returns Unroutable, while production full via-history search finds the legal 3-via path!
    let stack = vec![
        "F.Cu".to_owned(),
        "In1.Cu".to_owned(),
        "In2.Cu".to_owned(),
        "B.Cu".to_owned(),
    ];
    let mut pads = vec![
        Pad {
            id: id(1),
            net: 1,
            at: [10, 10],
            layers: vec!["F.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 1 },
            drill: None,
        },
        Pad {
            id: id(2),
            net: 1,
            at: [20, 30],
            layers: vec!["B.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 1 },
            drill: None,
        },
    ];
    let mut tracks = Vec::new();
    let mut add_block = |nid: u64, layer: &str, a: Point, b: Point| {
        if a == b {
            pads.push(Pad {
                id: id(nid),
                net: 0,
                at: a,
                layers: vec![layer.to_owned()],
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 2 },
                drill: None,
            });
        } else {
            tracks.push(Track {
                id: id(nid),
                net: 0,
                layer: layer.to_owned(),
                start: a,
                end: b,
                width_nm: 4,
                locked: true,
            });
        }
    };
    // Force start->V1/V1' on F.Cu; block [20,20] & [20,30] on F.Cu and [10,20],[10,30] on F.Cu.
    add_block(10, "F.Cu", [-10, 20], [50, 20]);
    add_block(11, "F.Cu", [-10, 30], [50, 30]);
    add_block(12, "F.Cu", [-10, 0], [50, 0]);
    add_block(13, "F.Cu", [-10, 40], [50, 40]);
    add_block(14, "F.Cu", [30, 10], [30, 10]);
    // Block B.Cu everywhere except [20,20]->[20,30]
    add_block(20, "B.Cu", [-10, 10], [50, 10]);
    add_block(21, "B.Cu", [-10, 20], [10, 20]);
    add_block(22, "B.Cu", [30, 20], [50, 20]);
    add_block(23, "B.Cu", [-10, 30], [10, 30]);
    add_block(24, "B.Cu", [30, 30], [50, 30]);
    add_block(25, "B.Cu", [-10, 40], [50, 40]);
    add_block(26, "B.Cu", [-10, 0], [50, 0]);
    // Block In1.Cu exits from [20,20] except arriving from [30,20] (use [20,15] between V1 and V3)
    add_block(30, "In1.Cu", [10, 20], [10, 20]);
    add_block(31, "In1.Cu", [20, 15], [20, 15]);
    add_block(32, "In1.Cu", [20, 30], [20, 30]);
    add_block(33, "In1.Cu", [0, 20], [0, 30]);
    add_block(34, "In1.Cu", [30, 10], [30, 10]);
    add_block(35, "In1.Cu", [30, 30], [30, 30]);
    add_block(36, "In1.Cu", [10, 10], [10, 10]);
    add_block(37, "In1.Cu", [10, 30], [10, 30]);
    add_block(38, "In1.Cu", [-10, 40], [50, 40]);
    add_block(39, "In1.Cu", [-10, 0], [50, 0]);
    add_block(50, "In1.Cu", [40, 10], [40, 10]);
    add_block(51, "In1.Cu", [40, 30], [40, 30]);
    // Block In2.Cu direct drop to [20,20]/[20,30] without blocking V3 through-path at [20,20]
    add_block(40, "In2.Cu", [10, 20], [10, 20]);
    add_block(41, "In2.Cu", [20, 30], [20, 30]);
    add_block(42, "In2.Cu", [10, 30], [10, 30]);
    add_block(43, "In2.Cu", [10, 10], [10, 10]);
    add_block(44, "In2.Cu", [30, 20], [30, 20]);
    add_block(45, "In2.Cu", [-10, 0], [50, 0]);
    add_block(46, "In2.Cu", [20, 15], [20, 15]);

    let mut input = Input {
        version: 1,
        source_id: "d".repeat(64),
        layer: "F.Cu".into(),
        layers: stack,
        bounds: Bounds {
            min: [-10, 0],
            max: [50, 50],
        },
        rules: Rules {
            width_nm: 2,
            clearance_nm: 1,
            grid_nm: 10,
            edge_clearance_nm: 1,
            hole_clearance_nm: 2,
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
        },
        nets: vec![
            Net {
                id: 0,
                name: String::new(),
            },
            Net {
                id: 1,
                name: "SIG".into(),
            },
        ],
        pads,
        tracks,
        vias: Vec::new(),
        rule_areas: Vec::new(),
        max_expansions: 50_000,
        limits: Limits::default(),
        holes: Vec::new(),
        board_outline: None,
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
        board_design_settings: None,
        project_rules: None,
        custom_rules: None,
        effective_rules: None,
    };
    input.project_rules = Some(
        serde_json::json!({
            "meta": {"version": 3},
            "board": {"design_settings": {"rules": {
                "min_via_diameter": 0.000004,
                "min_through_hole_diameter": 0.000002,
                "min_hole_to_hole": 0.000010,
                "allow_blind_buried_vias": true,
                "allow_microvias": false
            }}},
            "net_settings": {
                "classes": [{
                    "name": "Default",
                    "track_width": 0.000002,
                    "clearance": 0.000001,
                    "via_diameter": 0.000004,
                    "via_drill": 0.000002
                }]
            }
        })
        .to_string(),
    );
    let engine = Engine::new(input).unwrap();
    let res = join_components(
        &engine,
        1,
        &[Copper::Pad(&engine.input.pads[0])],
        &[vec![Copper::Pad(&engine.input.pads[1])]],
        50_000,
        &mut 0,
    )
    .expect("full via-history search must find 3-via route");
    let via_count = res
        .edges
        .iter()
        .filter(|e| matches!(e, PathEdge::Via(_)))
        .count();
    assert!(via_count >= 3, "expected >= 3 vias, got {:?}", res.edges);

    // Reference last-via-only Dijkstra probe on the exact same transition rules:
    // dominance key is (at, layer, dir, last_via_only).
    let via_cost = engine.effective_rules.via_cost.max(1);
    let mut best_last: HashMap<(Point, usize, u8, Option<PlacedViaKey>), u64> = HashMap::new();
    let mut pq: BinaryHeap<
        Reverse<(
            u64,
            Point,
            usize,
            u8,
            Option<PlacedViaKey>,
            Vec<PlacedViaKey>,
        )>,
    > = BinaryHeap::new();
    best_last.insert(([10, 10], 0, 4, None), 0);
    pq.push(Reverse((0, [10, 10], 0, 4, None, Vec::new())));
    let mut last_via_only_found = None;
    while let Some(Reverse((g, p, l, d, lv, hist))) = pq.pop() {
        if best_last.get(&(p, l, d, lv)).is_some_and(|&old| old < g) {
            continue;
        }
        if p == [20, 30] && l == 3 {
            last_via_only_found = Some((g, hist.clone()));
            break;
        }
        for (dc, delta) in [[10, 0], [0, 10], [-10, 0], [0, -10]].iter().enumerate() {
            let q = [p[0] + delta[0], p[1] + delta[1]];
            if engine.legal_edge_on(1, l, p, q).unwrap() {
                let k = (q, l, dc as u8, lv);
                let ng = g + 1;
                if best_last.get(&k).is_none_or(|&old| ng < old) {
                    best_last.insert(k, ng);
                    pq.push(Reverse((ng, q, l, dc as u8, lv, hist.clone())));
                }
            }
        }
        for tl in 0..4usize {
            if tl == l {
                continue;
            }
            let (s, e, kind, kcode) = if l.min(tl) == 0 && l.max(tl) == 3 {
                (0, 3, ViaKind::Through, 0u8)
            } else {
                (l.min(tl), l.max(tl), ViaKind::BlindBuried, 1u8)
            };
            let pv = PlacedViaKey {
                at: p,
                start_layer: s as u8,
                end_layer: e as u8,
                kind: kcode,
            };
            if hist.iter().any(|h| h.at == p) {
                continue;
            }
            if engine.legal_via(1, p, s, e, kind, &hist).unwrap().is_some() {
                let mut nh = hist.clone();
                nh.push(pv);
                let k = (p, tl, 4, Some(pv));
                let ng = g + via_cost;
                if best_last.get(&k).is_none_or(|&old| ng < old) {
                    best_last.insert(k, ng);
                    pq.push(Reverse((ng, p, tl, 4, Some(pv), nh)));
                }
            }
        }
    }
    assert!(
        last_via_only_found.is_none(),
        "last-via-only probe must fail (Unroutable) on RE-02 3-via witness, got {last_via_only_found:?}, prod={:?}",
        res.edges
    );
}

#[test]
fn cegar_mr_and_backward_h_enforce_dru_via_count_across_rounds() {
    // 4-layer board with hole_to_hole=30 (drill=2 -> min center dist >= 32 on grid=10)
    // and full-height F.Cu/B.Cu walls at x=50 forcing >= 2 vias.
    // Pad 1 at [40,20] (F.Cu), Pad 2 at [60,20] (B.Cu).
    // Round 1 (R = empty) places conflicting V1@[40,20] & V2@[60,20] (dist 20 < 32),
    // expanding R and triggering Round 2+ with backward Dijkstra h'.
    // Verify DruConstraint::ViaCount { max } is enforced in both Round 1 (max=1 -> Unroutable)
    // and Round 2+ (max=2 -> finds conflict-free 2-via detour V1@[20,20], V2@[60,20] with
    // exactly 2 vias, while a 3-via witness board with max=2 fails Unroutable in Round 2+).
    let stack = vec![
        "F.Cu".to_owned(),
        "In1.Cu".to_owned(),
        "In2.Cu".to_owned(),
        "B.Cu".to_owned(),
    ];
    let pads = vec![
        Pad {
            id: id(1),
            net: 1,
            at: [40, 20],
            layers: vec!["F.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 1 },
            drill: None,
        },
        Pad {
            id: id(2),
            net: 1,
            at: [60, 20],
            layers: vec!["B.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 1 },
            drill: None,
        },
    ];
    let tracks = vec![
        Track {
            id: id(10),
            net: 0,
            layer: "F.Cu".into(),
            start: [50, 0],
            end: [50, 40],
            width_nm: 4,
            locked: true,
        },
        Track {
            id: id(11),
            net: 0,
            layer: "B.Cu".into(),
            start: [50, 0],
            end: [50, 40],
            width_nm: 4,
            locked: true,
        },
    ];
    let make_input = |max_vc: u32| Input {
        version: 1,
        source_id: "e".repeat(64),
        layer: "F.Cu".into(),
        layers: stack.clone(),
        bounds: Bounds {
            min: [0, 0],
            max: [100, 40],
        },
        rules: Rules {
            width_nm: 2,
            clearance_nm: 1,
            grid_nm: 10,
            edge_clearance_nm: 1,
            hole_clearance_nm: 2,
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
        },
        nets: vec![
            Net {
                id: 0,
                name: String::new(),
            },
            Net {
                id: 1,
                name: "SIG".into(),
            },
        ],
        pads: pads.clone(),
        tracks: tracks.clone(),
        vias: Vec::new(),
        rule_areas: Vec::new(),
        max_expansions: 50_000,
        limits: Limits::default(),
        holes: Vec::new(),
        board_outline: None,
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
        board_design_settings: None,
        project_rules: Some(
            serde_json::json!({
                "meta": {"version": 3},
                "board": {"design_settings": {"rules": {
                    "min_via_diameter": 0.000004,
                    "min_through_hole_diameter": 0.000002,
                    "min_hole_to_hole": 0.000030,
                    "allow_blind_buried_vias": false,
                    "allow_microvias": false
                }}},
                "net_settings": {
                    "classes": [{
                        "name": "Default",
                        "track_width": 0.000002,
                        "clearance": 0.000001,
                        "via_diameter": 0.000004,
                        "via_drill": 0.000002
                    }]
                }
            })
            .to_string(),
        ),
        custom_rules: Some(format!(
            "(version 1)\n(rule \"vc\" (constraint via_count (max {max_vc})))"
        )),
        effective_rules: None,
    };

    // max_via_count = 1 must refuse Unroutable (cannot cross F.Cu/B.Cu walls with <= 1 via).
    let eng1 = Engine::new(make_input(1)).unwrap();
    let err1 = join_components(
        &eng1,
        1,
        &[Copper::Pad(&eng1.input.pads[0])],
        &[vec![Copper::Pad(&eng1.input.pads[1])]],
        50_000,
        &mut 0,
    )
    .unwrap_err();
    assert_eq!(err1.status, Status::Unroutable);

    // max_via_count = 2: Round 1 hits V1/V2 hole-to-hole conflict, Round 2+ (M_R + h')
    // finds the legal conflict-free 2-via route V1@[20,20] -> V2@[60,20] (dist 40 >= 32).
    let eng2 = Engine::new(make_input(2)).unwrap();
    let path2 = join_components(
        &eng2,
        1,
        &[Copper::Pad(&eng2.input.pads[0])],
        &[vec![Copper::Pad(&eng2.input.pads[1])]],
        50_000,
        &mut 0,
    )
    .expect("Round 2+ M_R + h' must find legal 2-via route under via_count max 2");
    let vias2: Vec<PlacedViaKey> = path2
        .edges
        .iter()
        .filter_map(|e| match *e {
            PathEdge::Via(pv) => Some(pv),
            _ => None,
        })
        .collect();
    assert_eq!(vias2.len(), 2, "expected 2 vias, got {vias2:?}");
    let dx = i128::from(vias2[0].at[0] - vias2[1].at[0]);
    let dy = i128::from(vias2[0].at[1] - vias2[1].at[1]);
    assert!(dx * dx + dy * dy >= 32 * 32);

    // Also verify Round 2+ enforces max_via_count=2 when F.Cu/B.Cu corridors only allow
    // [40,20] on F.Cu and [60,20] on B.Cu (conflict triggers Round 2 M_R + h', which must
    // return Unroutable rather than exceeding max_via_count=2).
    let mut eng3_in = make_input(2);
    eng3_in.tracks.push(Track {
        id: id(12),
        net: 0,
        layer: "F.Cu".into(),
        start: [30, 0],
        end: [30, 40],
        width_nm: 4,
        locked: true,
    });
    eng3_in.tracks.push(Track {
        id: id(13),
        net: 0,
        layer: "B.Cu".into(),
        start: [70, 0],
        end: [70, 40],
        width_nm: 4,
        locked: true,
    });
    let eng3 = Engine::new(eng3_in).unwrap();
    let err3 = join_components(
        &eng3,
        1,
        &[Copper::Pad(&eng3.input.pads[0])],
        &[vec![Copper::Pad(&eng3.input.pads[1])]],
        50_000,
        &mut 0,
    )
    .unwrap_err();
    assert_eq!(err3.status, Status::Unroutable);
}
