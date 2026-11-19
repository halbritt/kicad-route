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
        zone_connection: crate::model::PadZoneConnection::Inherited,
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
        footprint_ref: None,
        pad_number: None,
        fromto_id: None,
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
                            zones: Vec::new(),
                            components: Vec::new(),
                            stackup: Vec::new(),
                            routing_options: None,
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
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
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
                        zones: Vec::new(),
                        components: Vec::new(),
                        stackup: Vec::new(),
                        routing_options: None,
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
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
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
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
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
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
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
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
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
                        zone_connection: crate::model::PadZoneConnection::Inherited,
                        thermal_gap_nm: None,
                        thermal_spoke_width_nm: None,
                        thermal_spoke_angle_mdeg: None,
                        orientation_mdeg: 0,
                        footprint_ref: None,
                        pad_number: None,
                        fromto_id: None,
                    },
                    Pad {
                        id: id(2),
                        net: 1,
                        at: [30, 10],
                        layers: vec!["B.Cu".into()],
                        local_clearance_nm: None,
                        shape: Shape::Circle { radius_nm: 1 },
                        drill: None,
                        zone_connection: crate::model::PadZoneConnection::Inherited,
                        thermal_gap_nm: None,
                        thermal_spoke_width_nm: None,
                        thermal_spoke_angle_mdeg: None,
                        orientation_mdeg: 0,
                        footprint_ref: None,
                        pad_number: None,
                        fromto_id: None,
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
                zones: Vec::new(),
                components: Vec::new(),
                stackup: Vec::new(),
                routing_options: None,
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
                        zone_connection: crate::model::PadZoneConnection::Inherited,
                        thermal_gap_nm: None,
                        thermal_spoke_width_nm: None,
                        thermal_spoke_angle_mdeg: None,
                        orientation_mdeg: 0,
                        footprint_ref: None,
                        pad_number: None,
                        fromto_id: None,
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
                zone_connection: crate::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
                footprint_ref: None,
                pad_number: None,
                fromto_id: None,
            },
            Pad {
                id: id(2),
                net: 1,
                at: [20, 30],
                layers: vec!["B.Cu".into()],
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 1 },
                drill: None,
                zone_connection: crate::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
                footprint_ref: None,
                pad_number: None,
                fromto_id: None,
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
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
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
            zone_connection: crate::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
        },
        Pad {
            id: id(2),
            net: 1,
            at: [20, 30],
            layers: vec!["B.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 1 },
            drill: None,
            zone_connection: crate::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
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
                zone_connection: crate::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
                footprint_ref: None,
                pad_number: None,
                fromto_id: None,
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
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
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
            zone_connection: crate::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
        },
        Pad {
            id: id(2),
            net: 1,
            at: [60, 20],
            layers: vec!["B.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 1 },
            drill: None,
            zone_connection: crate::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
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
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
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

#[test]
fn track_vs_via_goal_dominance_and_full_span_finished_re02() {
    // 1. Reviewer 1 probe_a: 2L PTH [0,0] on ["F.Cu", "B.Cu"] -> [460_000, 160_000] on ["F.Cu"].
    // Tracks only permitted in y in [-50_000, 50_000], x <= 250_000; Through via at [100_000, 0]
    // (or [200_000, 0]) contacts P2 (dist 304_631 < 400_000) via its 600_000 nm diameter.
    // Also test max_via_count = 1 boundary where Track at [100_000, 0] with vc=1 would dominate Via if conflated.
    let make_probe_a = |max_vc: Option<u32>| {
        let inp = Input {
            version: 1,
            source_id: "f".repeat(64),
            layer: "F.Cu".into(),
            layers: vec!["F.Cu".into(), "B.Cu".into()],
            bounds: Bounds {
                min: [-2_000_000, -2_000_000],
                max: [2_000_000, 2_000_000],
            },
            rules: Rules {
                width_nm: 100_000,
                clearance_nm: 50_000,
                grid_nm: 100_000,
                edge_clearance_nm: 100_000,
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
                    name: "SIG".into(),
                },
            ],
            pads: vec![
                Pad {
                    id: id(1),
                    net: 1,
                    at: [0, 0],
                    layers: vec!["F.Cu".into(), "B.Cu".into()],
                    local_clearance_nm: None,
                    shape: Shape::Circle { radius_nm: 50_000 },
                    drill: Some(DrillShape::Circle {
                        diameter_nm: 50_000,
                    }),
                    zone_connection: crate::model::PadZoneConnection::Inherited,
                    thermal_gap_nm: None,
                    thermal_spoke_width_nm: None,
                    thermal_spoke_angle_mdeg: None,
                    orientation_mdeg: 0,
                    footprint_ref: None,
                    pad_number: None,
                    fromto_id: None,
                },
                Pad {
                    id: id(2),
                    net: 1,
                    at: [460_000, 160_000],
                    layers: vec!["F.Cu".into()],
                    local_clearance_nm: None,
                    shape: Shape::Circle { radius_nm: 100_000 },
                    drill: None,
                    zone_connection: crate::model::PadZoneConnection::Inherited,
                    thermal_gap_nm: None,
                    thermal_spoke_width_nm: None,
                    thermal_spoke_angle_mdeg: None,
                    orientation_mdeg: 0,
                    footprint_ref: None,
                    pad_number: None,
                    fromto_id: None,
                },
            ],
            tracks: Vec::new(),
            vias: Vec::new(),
            rule_areas: vec![
                RuleArea {
                    id: id(10),
                    layers: vec!["F.Cu".into(), "B.Cu".into()],
                    no_tracks: true,
                    no_vias: false,
                    outer: vec![
                        [-1_900_000, 50_000],
                        [1_900_000, 50_000],
                        [1_900_000, 1_900_000],
                        [-1_900_000, 1_900_000],
                    ],
                    holes: Vec::new(),
                    name: None,
                    extra_polygons: Vec::new(),
                    no_pads: false,
                    no_footprints: false,
                    no_zone_fills: false,
                },
                RuleArea {
                    id: id(11),
                    layers: vec!["F.Cu".into(), "B.Cu".into()],
                    no_tracks: true,
                    no_vias: false,
                    outer: vec![
                        [-1_900_000, -1_900_000],
                        [1_900_000, -1_900_000],
                        [1_900_000, -50_000],
                        [-1_900_000, -50_000],
                    ],
                    holes: Vec::new(),
                    name: None,
                    extra_polygons: Vec::new(),
                    no_pads: false,
                    no_footprints: false,
                    no_zone_fills: false,
                },
                RuleArea {
                    id: id(12),
                    layers: vec!["F.Cu".into(), "B.Cu".into()],
                    no_tracks: true,
                    no_vias: false,
                    outer: vec![
                        [250_000, -100_000],
                        [1_900_000, -100_000],
                        [1_900_000, 100_000],
                        [250_000, 100_000],
                    ],
                    holes: Vec::new(),
                    name: None,
                    extra_polygons: Vec::new(),
                    no_pads: false,
                    no_footprints: false,
                    no_zone_fills: false,
                },
            ],
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
                        "min_via_diameter": 0.6,
                        "min_through_hole_diameter": 0.3,
                        "allow_blind_buried_vias": false,
                        "allow_microvias": false
                    }}},
                    "net_settings": {"classes": [{
                        "name": "Default",
                        "track_width": 0.1,
                        "clearance": 0.05,
                        "via_diameter": 0.6,
                        "via_drill": 0.3
                    }]}
                })
                .to_string(),
            ),
            custom_rules: max_vc
                .map(|m| format!("(version 1)\n(rule \"vc\" (constraint via_count (max {m})))")),
            effective_rules: None,
            zones: Vec::new(),
            components: Vec::new(),
            stackup: Vec::new(),
            routing_options: None,
        };
        inp
    };
    for max_vc in [None, Some(1)] {
        let eng = Engine::new(make_probe_a(max_vc)).unwrap();
        let path = join_components(
            &eng,
            1,
            &[Copper::Pad(&eng.input.pads[0])],
            &[vec![Copper::Pad(&eng.input.pads[1])]],
            50_000,
            &mut 0,
        )
        .unwrap_or_else(|e| panic!("probe_a (max_vc={max_vc:?}) failed: {e:?}"));
        assert!(
            path.edges.iter().any(|e| matches!(e, PathEdge::Via(_))),
            "probe_a (max_vc={max_vc:?}) must use a Via: {:?}",
            path.edges
        );
    }

    // 2. Reviewer 2 off-grid Via-only contact witness: [10,10]/[20,10] -> [23,14] on B.Cu.
    let mut offgrid_in = Input {
        version: 1,
        source_id: "0".repeat(64),
        layer: "F.Cu".into(),
        layers: vec!["F.Cu".into(), "B.Cu".into()],
        bounds: Bounds {
            min: [0, 0],
            max: [50, 50],
        },
        rules: Rules {
            width_nm: 2,
            clearance_nm: 1,
            grid_nm: 10,
            edge_clearance_nm: 1,
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
                name: "SIG".into(),
            },
        ],
        pads: vec![
            Pad {
                id: id(1),
                net: 1,
                at: [10, 10],
                layers: vec!["F.Cu".into(), "B.Cu".into()],
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 1 },
                drill: Some(DrillShape::Circle { diameter_nm: 1 }),
                zone_connection: crate::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
                footprint_ref: None,
                pad_number: None,
                fromto_id: None,
            },
            Pad {
                id: id(2),
                net: 1,
                at: [23, 14],
                layers: vec!["B.Cu".into()],
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 1 },
                drill: None,
                zone_connection: crate::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
                footprint_ref: None,
                pad_number: None,
                fromto_id: None,
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
        project_rules: Some(
            serde_json::json!({
                "meta": {"version": 3},
                "board": {"design_settings": {"rules": {
                    "min_via_diameter": 0.000010,
                    "min_through_hole_diameter": 0.000002,
                    "allow_blind_buried_vias": false,
                    "allow_microvias": false
                }}},
                "net_settings": {"classes": [{
                    "name": "Default",
                    "track_width": 0.000002,
                    "clearance": 0.000001,
                    "via_diameter": 0.000010,
                    "via_drill": 0.000002
                }]}
            })
            .to_string(),
        ),
        custom_rules: None,
        effective_rules: None,
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
    };
    let eng_off = Engine::new(offgrid_in.clone()).unwrap();
    let path_off = join_components(
        &eng_off,
        1,
        &[Copper::Pad(&eng_off.input.pads[0])],
        &[vec![Copper::Pad(&eng_off.input.pads[1])]],
        10_000,
        &mut 0,
    )
    .expect("off-grid Via-only contact witness from [10,10] must succeed");
    assert!(path_off.edges.iter().any(|e| matches!(e, PathEdge::Via(_))));

    offgrid_in.pads[0].at = [20, 10];
    offgrid_in.pads[0].drill = None;
    let eng_off2 = Engine::new(offgrid_in).unwrap();
    let path_off2 = join_components(
        &eng_off2,
        1,
        &[Copper::Pad(&eng_off2.input.pads[0])],
        &[vec![Copper::Pad(&eng_off2.input.pads[1])]],
        10_000,
        &mut 0,
    )
    .expect("off-grid Via-only contact witness from [20,10] must succeed");
    assert_eq!(path_off2.steps, 40);
}

#[test]
fn layer_scoped_annular_width_and_via_drill_admissible_heuristic_re02() {
    // 1. Reviewer 1 probe_b5: layer-scoped (layer "F.Cu") (constraint annular_width (min 1.0mm))
    // gives effective via diameter 2_200_000 (radius 1_100_000), making Via@[500_000,0] (cost 40)
    // beat the 41-step F.Cu detour around y=9_100_000.
    let mut b5 = Input {
        version: 1,
        source_id: "1".repeat(64),
        layer: "F.Cu".into(),
        layers: vec!["F.Cu".into(), "B.Cu".into()],
        bounds: Bounds {
            min: [-2_000_000, -2_000_000],
            max: [5_000_000, 12_000_000],
        },
        rules: Rules {
            width_nm: 100_000,
            clearance_nm: 100_000,
            grid_nm: 500_000,
            edge_clearance_nm: 100_000,
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
                name: "SIG".into(),
            },
        ],
        pads: vec![
            Pad {
                id: id(1),
                net: 1,
                at: [0, 0],
                layers: vec!["F.Cu".into(), "B.Cu".into()],
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 100_000 },
                drill: Some(DrillShape::Circle {
                    diameter_nm: 100_000,
                }),
                zone_connection: crate::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
                footprint_ref: None,
                pad_number: None,
                fromto_id: None,
            },
            Pad {
                id: id(2),
                net: 1,
                at: [500_000, 0],
                layers: vec!["B.Cu".into()],
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 100_000 },
                drill: None,
                zone_connection: crate::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
                footprint_ref: None,
                pad_number: None,
                fromto_id: None,
            },
            Pad {
                id: id(3),
                net: 1,
                at: [1_500_000, 0],
                layers: vec!["F.Cu".into()],
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 100_000 },
                drill: None,
                zone_connection: crate::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
                footprint_ref: None,
                pad_number: None,
                fromto_id: None,
            },
        ],
        tracks: vec![Track {
            id: id(10),
            net: 1,
            layer: "B.Cu".into(),
            start: [0, 0],
            end: [500_000, 0],
            width_nm: 100_000,
            locked: true,
        }],
        vias: Vec::new(),
        rule_areas: vec![RuleArea {
            id: id(11),
            layers: vec!["F.Cu".into()],
            no_tracks: true,
            no_vias: false,
            outer: vec![
                [900_000, -1_500_000],
                [1_100_000, -1_500_000],
                [1_100_000, 9_100_000],
                [900_000, 9_100_000],
            ],
            holes: Vec::new(),
            name: None,
            extra_polygons: Vec::new(),
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        }],
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
                    "min_via_diameter": 0.2,
                    "min_through_hole_diameter": 0.2,
                    "allow_blind_buried_vias": false,
                    "allow_microvias": false
                }}},
                "net_settings": {"classes": [{
                    "name": "Default",
                    "track_width": 0.1,
                    "clearance": 0.1,
                    "via_diameter": 0.2,
                    "via_drill": 0.2
                }]}
            })
            .to_string(),
        ),
        custom_rules: Some(
            "(version 1)\n(rule \"ann\" (layer \"F.Cu\") (constraint annular_width (min 1.0mm)))"
                .into(),
        ),
        effective_rules: None,
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
    };
    let eng_b5 = Engine::new(b5.clone()).unwrap();
    let b5_t0 = eng_b5.copper().tracks().next().unwrap();
    let path_b5 = join_components(
        &eng_b5,
        1,
        &[
            Copper::Pad(&eng_b5.input.pads[0]),
            Copper::Pad(&eng_b5.input.pads[1]),
            Copper::Track(b5_t0),
        ],
        &[vec![Copper::Pad(&eng_b5.input.pads[2])]],
        50_000,
        &mut 0,
    )
    .unwrap();
    assert_eq!(
        path_b5.steps, 40,
        "probe_b5 must choose optimal 40-cost Via: {:?}",
        path_b5.edges
    );
    assert!(path_b5.edges.iter().any(|e| matches!(e, PathEdge::Via(_))));

    // 2. Challenger 1 Probe 4: 3L board with min_via_annular_width = 1nm and
    // (layer "B.Cu") (constraint via_drill (min 0.000044mm)) gives drill = 44nm,
    // effective via diam = 46 nm, making [0,10]->[10,10] + Via@[10,10] (cost 41)
    // beat the 42-step F.Cu route [0,10]->[-418,10].
    let p4 = Input {
        version: 1,
        source_id: "2".repeat(64),
        layer: "F.Cu".into(),
        layers: vec!["F.Cu".into(), "In1.Cu".into(), "B.Cu".into()],
        bounds: Bounds {
            min: [-500, -100],
            max: [200, 100],
        },
        rules: Rules {
            width_nm: 2,
            clearance_nm: 1,
            grid_nm: 10,
            edge_clearance_nm: 1,
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
                name: "SIG".into(),
            },
        ],
        pads: vec![
            Pad {
                id: id(1),
                net: 1,
                at: [0, 10],
                layers: vec!["F.Cu".into()],
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 1 },
                drill: None,
                zone_connection: crate::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
},
            Pad {
                id: id(2),
                net: 1,
                at: [32, 10],
                layers: vec!["B.Cu".into()],
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 1 },
                drill: None,
                zone_connection: crate::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
},
            Pad {
                id: id(3),
                net: 1,
                at: [-418, 10],
                layers: vec!["F.Cu".into()],
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 1 },
                drill: None,
                zone_connection: crate::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
},
        ],
        tracks: vec![
            Track {
                id: id(10),
                net: 1,
                layer: "B.Cu".into(),
                start: [-418, 10],
                end: [-418, 60],
                width_nm: 2,
                locked: true,
            },
            Track {
                id: id(12),
                net: 1,
                layer: "B.Cu".into(),
                start: [-418, 60],
                end: [32, 60],
                width_nm: 2,
                locked: true,
            },
            Track {
                id: id(13),
                net: 1,
                layer: "B.Cu".into(),
                start: [32, 60],
                end: [32, 10],
                width_nm: 2,
                locked: true,
            },
        ],
        vias: vec![Via {
            id: id(11),
            net: 1,
            at: [-418, 10],
            diameter_nm: 4,
            drill_nm: 2,
            start_layer: "F.Cu".into(),
            end_layer: "B.Cu".into(),
            kind: ViaKind::Through,
            locked: true,
        }],
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
                    "min_via_annular_width": 0.000001,
                    "min_through_hole_diameter": 0.000002,
                    "allow_blind_buried_vias": false,
                    "allow_microvias": false
                }}},
                "net_settings": {"classes": [{
                    "name": "Default",
                    "track_width": 0.000002,
                    "clearance": 0.000001,
                    "via_diameter": 0.000004,
                    "via_drill": 0.000002,
                    "microvia_diameter": 0.000004,
                    "microvia_drill": 0.000002
                }]}
            })
            .to_string(),
        ),
        custom_rules: Some(
            "(version 1)\n(rule \"big_bcu_drill\" (layer \"B.Cu\") (constraint via_drill (min 0.000044mm)))"
                .into(),
        ),
        effective_rules: None,
        zones: Vec::new(),
            components: Vec::new(),
            stackup: Vec::new(),
            routing_options: None,
};
    let eng_p4 = Engine::new(p4).unwrap();
    let p4_tracks: Vec<_> = eng_p4.copper().tracks().map(Copper::Track).collect();
    let p4_v0 = eng_p4.copper().vias().next().unwrap();
    let mut p4_targets = vec![
        Copper::Pad(&eng_p4.input.pads[1]),
        Copper::Pad(&eng_p4.input.pads[2]),
        Copper::Via(p4_v0),
    ];
    p4_targets.extend(p4_tracks);
    let path_p4 = join_components(
        &eng_p4,
        1,
        &[Copper::Pad(&eng_p4.input.pads[0])],
        &[p4_targets],
        50_000,
        &mut 0,
    )
    .unwrap();
    assert_eq!(
        path_p4.steps, 41,
        "Probe 4 must choose optimal 41-cost Via route: {:?}",
        path_p4.edges
    );
    assert!(path_p4.edges.iter().any(|e| matches!(e, PathEdge::Via(_))));
    let _ = &mut b5;
}

#[test]
fn probe5_same_net_hole_clearance_same_path_vias_satisfy_legal_edge_on_reload() {
    let mut i = Input {
        version: 1,
        source_id: "3".repeat(64),
        layer: "F.Cu".into(),
        layers: vec!["F.Cu".into(), "B.Cu".into()],
        bounds: Bounds {
            min: [-20, 0],
            max: [60, 60],
        },
        rules: Rules {
            width_nm: 2,
            clearance_nm: 1,
            grid_nm: 10,
            edge_clearance_nm: 1,
            hole_clearance_nm: 0,
            same_net_hole_clearance_nm: 10,
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
                zone_connection: crate::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
                footprint_ref: None,
                pad_number: None,
                fromto_id: None,
            },
            Pad {
                id: id(2),
                net: 1,
                at: [30, 20],
                layers: vec!["B.Cu".into()],
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 1 },
                drill: None,
                zone_connection: crate::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
                footprint_ref: None,
                pad_number: None,
                fromto_id: None,
            },
        ],
        tracks: Vec::new(),
        vias: Vec::new(),
        rule_areas: vec![RuleArea {
            id: id(20),
            layers: vec!["F.Cu".into()],
            no_tracks: false,
            no_vias: true,
            outer: vec![[5, 0], [55, 0], [55, 55], [5, 55]],
            holes: Vec::new(),
            name: None,
            extra_polygons: Vec::new(),
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        }],
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
                    "allow_blind_buried_vias": false,
                    "allow_microvias": false
                }}},
                "net_settings": {"classes": [{
                    "name": "Default",
                    "track_width": 0.000002,
                    "clearance": 0.000001,
                    "via_diameter": 0.000004,
                    "via_drill": 0.000002
                }]}
            })
            .to_string(),
        ),
        custom_rules: None,
        effective_rules: None,
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
    };
    let mut eng = Engine::new(i.clone()).unwrap();
    eng.route(1, 50_000).unwrap();
    let vias: Vec<Via> = eng.copper().vias().cloned().collect();
    let tracks: Vec<Track> = eng.copper().tracks().cloned().collect();
    assert!(!vias.is_empty());

    for (idx, mut v) in vias.into_iter().enumerate() {
        v.id = id(100 + idx as u64);
        v.locked = true;
        i.vias.push(v);
    }
    let reload = Engine::new(i).unwrap();
    for t in &tracks {
        let l_idx = reload.layer_index(&t.layer).unwrap();
        assert!(
            reload.legal_edge_on(t.net, l_idx, t.start, t.end).unwrap(),
            "emitted track {t:?} violates Engine::legal_edge_on against emitted vias"
        );
    }
}

#[test]
fn probe_d_350_open_vs_ctrl_re02() {
    // Reviewer 1 probe_d_350_open vs probe_d_350_ctrl:
    // Short branch [0,0]->[100k,0]->[100k,100k]->[400k,100k] violates Via@[400k,100k]'s moat
    // (same_net_hole_clearance_nm = 350_000), while longer detour via [-100k,0]->[-100k,100k] is legal.
    let make_probe_d = |block_short_east: bool| {
        let mut rule_areas = vec![
            // Allow vias ONLY around [400_000, 100_000]
            RuleArea {
                id: id(10),
                layers: vec!["F.Cu".into(), "B.Cu".into()],
                no_tracks: false,
                no_vias: true,
                outer: vec![
                    [-1_000_000, -1_000_000],
                    [340_000, -1_000_000],
                    [340_000, 1_000_000],
                    [-1_000_000, 1_000_000],
                ],
                holes: Vec::new(),
                name: None,
                extra_polygons: Vec::new(),
                no_pads: false,
                no_footprints: false,
                no_zone_fills: false,
            },
            RuleArea {
                id: id(11),
                layers: vec!["F.Cu".into(), "B.Cu".into()],
                no_tracks: false,
                no_vias: true,
                outer: vec![
                    [460_000, -1_000_000],
                    [1_000_000, -1_000_000],
                    [1_000_000, 1_000_000],
                    [460_000, 1_000_000],
                ],
                holes: Vec::new(),
                name: None,
                extra_polygons: Vec::new(),
                no_pads: false,
                no_footprints: false,
                no_zone_fills: false,
            },
            RuleArea {
                id: id(12),
                layers: vec!["F.Cu".into(), "B.Cu".into()],
                no_tracks: false,
                no_vias: true,
                outer: vec![
                    [340_000, -1_000_000],
                    [460_000, -1_000_000],
                    [460_000, 40_000],
                    [340_000, 40_000],
                ],
                holes: Vec::new(),
                name: None,
                extra_polygons: Vec::new(),
                no_pads: false,
                no_footprints: false,
                no_zone_fills: false,
            },
            RuleArea {
                id: id(13),
                layers: vec!["F.Cu".into(), "B.Cu".into()],
                no_tracks: false,
                no_vias: true,
                outer: vec![
                    [340_000, 160_000],
                    [460_000, 160_000],
                    [460_000, 1_000_000],
                    [340_000, 1_000_000],
                ],
                holes: Vec::new(),
                name: None,
                extra_polygons: Vec::new(),
                no_pads: false,
                no_footprints: false,
                no_zone_fills: false,
            },
            // Force F.Cu tracks: block y <= -40k, y >= 140k, x >= 440k, [0,0]->[0,100k], and direct y=100k east of [100k,100k] except y=100k corridor
            RuleArea {
                id: id(14),
                layers: vec!["F.Cu".into()],
                no_tracks: true,
                no_vias: false,
                outer: vec![
                    [-1_000_000, -1_000_000],
                    [1_000_000, -1_000_000],
                    [1_000_000, -40_000],
                    [-1_000_000, -40_000],
                ],
                holes: Vec::new(),
                name: None,
                extra_polygons: Vec::new(),
                no_pads: false,
                no_footprints: false,
                no_zone_fills: false,
            },
            RuleArea {
                id: id(15),
                layers: vec!["F.Cu".into()],
                no_tracks: true,
                no_vias: false,
                outer: vec![
                    [-1_000_000, 140_000],
                    [1_000_000, 140_000],
                    [1_000_000, 1_000_000],
                    [-1_000_000, 1_000_000],
                ],
                holes: Vec::new(),
                name: None,
                extra_polygons: Vec::new(),
                no_pads: false,
                no_footprints: false,
                no_zone_fills: false,
            },
            RuleArea {
                id: id(16),
                layers: vec!["F.Cu".into()],
                no_tracks: true,
                no_vias: false,
                outer: vec![
                    [440_000, -40_000],
                    [1_000_000, -40_000],
                    [1_000_000, 140_000],
                    [440_000, 140_000],
                ],
                holes: Vec::new(),
                name: None,
                extra_polygons: Vec::new(),
                no_pads: false,
                no_footprints: false,
                no_zone_fills: false,
            },
            RuleArea {
                id: id(17),
                layers: vec!["F.Cu".into()],
                no_tracks: true,
                no_vias: false,
                outer: vec![
                    [-20_000, 40_000],
                    [20_000, 40_000],
                    [20_000, 60_000],
                    [-20_000, 60_000],
                ],
                holes: Vec::new(),
                name: None,
                extra_polygons: Vec::new(),
                no_pads: false,
                no_footprints: false,
                no_zone_fills: false,
            },
            RuleArea {
                id: id(18),
                layers: vec!["F.Cu".into()],
                no_tracks: true,
                no_vias: false,
                outer: vec![
                    [140_000, -40_000],
                    [440_000, -40_000],
                    [440_000, 60_000],
                    [140_000, 60_000],
                ],
                holes: Vec::new(),
                name: None,
                extra_polygons: Vec::new(),
                no_pads: false,
                no_footprints: false,
                no_zone_fills: false,
            },
        ];
        if block_short_east {
            rule_areas.push(RuleArea {
                id: id(19),
                layers: vec!["F.Cu".into()],
                no_tracks: true,
                no_vias: false,
                outer: vec![
                    [40_000, -20_000],
                    [60_000, -20_000],
                    [60_000, 20_000],
                    [40_000, 20_000],
                ],
                holes: Vec::new(),
                name: None,
                extra_polygons: Vec::new(),
                no_pads: false,
                no_footprints: false,
                no_zone_fills: false,
            });
        }
        Input {
            version: 1,
            source_id: "5".repeat(64),
            layer: "F.Cu".into(),
            layers: vec!["F.Cu".into(), "B.Cu".into()],
            bounds: Bounds {
                min: [-1_000_000, -1_000_000],
                max: [1_000_000, 1_000_000],
            },
            rules: Rules {
                width_nm: 20_000,
                clearance_nm: 10_000,
                grid_nm: 100_000,
                edge_clearance_nm: 10_000,
                hole_clearance_nm: 0,
                same_net_hole_clearance_nm: 350_000,
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
                    at: [0, 0],
                    layers: vec!["F.Cu".into()],
                    local_clearance_nm: None,
                    shape: Shape::Circle { radius_nm: 10_000 },
                    drill: None,
                    zone_connection: crate::model::PadZoneConnection::Inherited,
                    thermal_gap_nm: None,
                    thermal_spoke_width_nm: None,
                    thermal_spoke_angle_mdeg: None,
                    orientation_mdeg: 0,
                    footprint_ref: None,
                    pad_number: None,
                    fromto_id: None,
                },
                Pad {
                    id: id(2),
                    net: 1,
                    at: [400_000, 100_000],
                    layers: vec!["B.Cu".into()],
                    local_clearance_nm: None,
                    shape: Shape::Circle { radius_nm: 10_000 },
                    drill: None,
                    zone_connection: crate::model::PadZoneConnection::Inherited,
                    thermal_gap_nm: None,
                    thermal_spoke_width_nm: None,
                    thermal_spoke_angle_mdeg: None,
                    orientation_mdeg: 0,
                    footprint_ref: None,
                    pad_number: None,
                    fromto_id: None,
                },
            ],
            tracks: Vec::new(),
            vias: Vec::new(),
            rule_areas,
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
                        "min_via_diameter": 0.1,
                        "min_through_hole_diameter": 0.06,
                        "allow_blind_buried_vias": false,
                        "allow_microvias": false
                    }}},
                    "net_settings": {"classes": [{
                        "name": "Default",
                        "track_width": 0.02,
                        "clearance": 0.01,
                        "via_diameter": 0.1,
                        "via_drill": 0.06
                    }]}
                })
                .to_string(),
            ),
            custom_rules: None,
            effective_rules: None,
            zones: Vec::new(),
            components: Vec::new(),
            stackup: Vec::new(),
            routing_options: None,
        }
    };

    let open_out = crate::run(make_probe_d(false));
    let ctrl_out = crate::run(make_probe_d(true));
    assert_eq!(open_out.status, Status::Complete, "{:?}", open_out.reason);
    assert_eq!(ctrl_out.status, Status::Complete, "{:?}", ctrl_out.reason);
    assert_eq!(open_out.added_vias.len(), 1);
    assert_eq!(open_out.added_vias[0].at, [400_000, 100_000]);
    assert_eq!(open_out.added_tracks.len(), 3);
    assert_eq!(open_out.added_tracks, ctrl_out.added_tracks);
}

#[test]
fn moat_detour_0_20_to_20_0_and_scenario_a1_re02() {
    // 1. Reviewer 2 [0,20] -> [20,0] witness (same_net_hole_clearance_nm = 25, Via@[20,0] only):
    let witness_in = Input {
        version: 1,
        source_id: "6".repeat(64),
        layer: "F.Cu".into(),
        layers: vec!["F.Cu".into(), "B.Cu".into()],
        bounds: Bounds {
            min: [-100, -100],
            max: [100, 100],
        },
        rules: Rules {
            width_nm: 2,
            clearance_nm: 1,
            grid_nm: 10,
            edge_clearance_nm: 1,
            hole_clearance_nm: 0,
            same_net_hole_clearance_nm: 25,
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
                at: [0, 20],
                layers: vec!["F.Cu".into()],
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 1 },
                drill: None,
                zone_connection: crate::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
                footprint_ref: None,
                pad_number: None,
                fromto_id: None,
            },
            Pad {
                id: id(2),
                net: 1,
                at: [20, 0],
                layers: vec!["B.Cu".into()],
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 1 },
                drill: None,
                zone_connection: crate::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
                footprint_ref: None,
                pad_number: None,
                fromto_id: None,
            },
        ],
        tracks: Vec::new(),
        vias: Vec::new(),
        rule_areas: vec![
            RuleArea {
                id: id(10),
                layers: vec!["F.Cu".into()],
                no_tracks: true,
                no_vias: false,
                outer: vec![[5, 5], [25, 5], [25, 25], [5, 25]],
                holes: Vec::new(),
                name: None,
                extra_polygons: Vec::new(),
                no_pads: false,
                no_footprints: false,
                no_zone_fills: false,
            },
            RuleArea {
                id: id(11),
                layers: vec!["F.Cu".into()],
                no_tracks: false,
                no_vias: true,
                outer: vec![[-100, -100], [15, -100], [15, 100], [-100, 100]],
                holes: Vec::new(),
                name: None,
                extra_polygons: Vec::new(),
                no_pads: false,
                no_footprints: false,
                no_zone_fills: false,
            },
            RuleArea {
                id: id(12),
                layers: vec!["F.Cu".into()],
                no_tracks: false,
                no_vias: true,
                outer: vec![[25, -100], [100, -100], [100, 100], [25, 100]],
                holes: Vec::new(),
                name: None,
                extra_polygons: Vec::new(),
                no_pads: false,
                no_footprints: false,
                no_zone_fills: false,
            },
            RuleArea {
                id: id(13),
                layers: vec!["F.Cu".into()],
                no_tracks: false,
                no_vias: true,
                outer: vec![[15, -100], [25, -100], [25, -5], [15, -5]],
                holes: Vec::new(),
                name: None,
                extra_polygons: Vec::new(),
                no_pads: false,
                no_footprints: false,
                no_zone_fills: false,
            },
            RuleArea {
                id: id(14),
                layers: vec!["F.Cu".into()],
                no_tracks: false,
                no_vias: true,
                outer: vec![[15, 5], [25, 5], [25, 100], [15, 100]],
                holes: Vec::new(),
                name: None,
                extra_polygons: Vec::new(),
                no_pads: false,
                no_footprints: false,
                no_zone_fills: false,
            },
        ],
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
                    "allow_blind_buried_vias": false,
                    "allow_microvias": false
                }}},
                "net_settings": {"classes": [{
                    "name": "Default",
                    "track_width": 0.000002,
                    "clearance": 0.000001,
                    "via_diameter": 0.000004,
                    "via_drill": 0.000002
                }]}
            })
            .to_string(),
        ),
        custom_rules: None,
        effective_rules: None,
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
    };
    let eng_w = Engine::new(witness_in.clone()).unwrap();
    let path_w = join_components(
        &eng_w,
        1,
        &[Copper::Pad(&eng_w.input.pads[0])],
        &[vec![Copper::Pad(&eng_w.input.pads[1])]],
        50_000,
        &mut 0,
    )
    .expect("[0,20]->[20,0] moat-detour witness must find legal detour");
    assert!(path_w
        .edges
        .iter()
        .any(|e| matches!(e, PathEdge::Via(pv) if pv.at == [20, 0])));
    let out_w = crate::run(witness_in);
    assert_eq!(out_w.status, Status::Complete, "{:?}", out_w.reason);

    // 2. Challenger 1 Scenario A1 (open vs blocked Path 1 control):
    let make_a1 = |block_path1: bool| {
        let mut tracks = vec![
            // B.Cu box around [20, 10]
            track(10, 0, [10, 0], [30, 0], 4),
            track(11, 0, [10, 20], [30, 20], 4),
            track(12, 0, [10, 0], [10, 20], 4),
            track(13, 0, [30, 0], [30, 20], 4),
            // F.Cu outer wall & [0,10]..[10,10] blocker
            track(20, 0, [-20, -30], [30, -30], 4),
            track(21, 0, [-20, 20], [30, 20], 4),
            track(22, 0, [-20, -30], [-20, 20], 4),
            track(23, 0, [30, -30], [30, 20], 4),
            track(24, 0, [0, 10], [10, 10], 4),
        ];
        for t in &mut tracks[..4] {
            t.layer = "B.Cu".into();
        }
        if block_path1 {
            tracks.push(track(25, 0, [0, 0], [10, 0], 4));
            tracks.push(track(26, 0, [0, -10], [10, -10], 4));
        }
        Input {
            version: 1,
            source_id: "7".repeat(64),
            layer: "F.Cu".into(),
            layers: vec!["F.Cu".into(), "B.Cu".into()],
            bounds: Bounds {
                min: [-30, -40],
                max: [40, 30],
            },
            rules: Rules {
                width_nm: 2,
                clearance_nm: 1,
                grid_nm: 10,
                edge_clearance_nm: 1,
                hole_clearance_nm: 0,
                same_net_hole_clearance_nm: 20,
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
                    at: [-10, 10],
                    layers: vec!["F.Cu".into()],
                    local_clearance_nm: None,
                    shape: Shape::Circle { radius_nm: 1 },
                    drill: None,
                    zone_connection: crate::model::PadZoneConnection::Inherited,
                    thermal_gap_nm: None,
                    thermal_spoke_width_nm: None,
                    thermal_spoke_angle_mdeg: None,
                    orientation_mdeg: 0,
                    footprint_ref: None,
                    pad_number: None,
                    fromto_id: None,
                },
                Pad {
                    id: id(2),
                    net: 1,
                    at: [20, 10],
                    layers: vec!["B.Cu".into()],
                    local_clearance_nm: None,
                    shape: Shape::Circle { radius_nm: 1 },
                    drill: None,
                    zone_connection: crate::model::PadZoneConnection::Inherited,
                    thermal_gap_nm: None,
                    thermal_spoke_width_nm: None,
                    thermal_spoke_angle_mdeg: None,
                    orientation_mdeg: 0,
                    footprint_ref: None,
                    pad_number: None,
                    fromto_id: None,
                },
            ],
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
            project_rules: Some(
                serde_json::json!({
                    "meta": {"version": 3},
                    "board": {"design_settings": {"rules": {
                        "min_via_diameter": 0.000004,
                        "min_through_hole_diameter": 0.000002,
                        "allow_blind_buried_vias": false,
                        "allow_microvias": false
                    }}},
                    "net_settings": {"classes": [{
                        "name": "Default",
                        "track_width": 0.000002,
                        "clearance": 0.000001,
                        "via_diameter": 0.000004,
                        "via_drill": 0.000002
                    }]}
                })
                .to_string(),
            ),
            custom_rules: None,
            effective_rules: None,
            zones: Vec::new(),
            components: Vec::new(),
            stackup: Vec::new(),
            routing_options: None,
        }
    };

    let a1_open = crate::run(make_a1(false));
    let a1_ctrl = crate::run(make_a1(true));
    assert_eq!(a1_open.status, Status::Complete, "{:?}", a1_open.reason);
    assert_eq!(a1_ctrl.status, Status::Complete, "{:?}", a1_ctrl.reason);
    assert_eq!(a1_open.added_vias.len(), 1);
    assert_eq!(a1_open.added_vias[0].at, [20, 10]);
    assert_eq!(a1_open.added_tracks, a1_ctrl.added_tracks);
}

#[test]
fn same_net_hole_clearance_matches_augmented_dijkstra_oracle_re02() {
    use std::cmp::Reverse;
    use std::collections::{BinaryHeap, HashMap};

    let grid_pts: Vec<Point> = (0..4)
        .flat_map(|ix| (0..3).map(move |iy| [ix * 10, iy * 10]))
        .collect();
    let pt_idx = |p: Point| -> usize { grid_pts.iter().position(|&q| q == p).unwrap() };

    let mut reachable = 0usize;
    let mut unreachable = 0usize;
    for snh in [10i64, 20] {
        for mask in 0..32u32 {
            let mut input = Input {
                version: 1,
                source_id: "8".repeat(64),
                layer: "F.Cu".into(),
                layers: vec!["F.Cu".into(), "B.Cu".into()],
                bounds: Bounds {
                    min: [-10, -10],
                    max: [40, 30],
                },
                rules: Rules {
                    width_nm: 2,
                    clearance_nm: 1,
                    grid_nm: 10,
                    edge_clearance_nm: 1,
                    hole_clearance_nm: 0,
                    same_net_hole_clearance_nm: snh,
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
                        at: [0, 10],
                        layers: vec!["F.Cu".into()],
                        local_clearance_nm: None,
                        shape: Shape::Circle { radius_nm: 1 },
                        drill: None,
                        zone_connection: crate::model::PadZoneConnection::Inherited,
                        thermal_gap_nm: None,
                        thermal_spoke_width_nm: None,
                        thermal_spoke_angle_mdeg: None,
                        orientation_mdeg: 0,
                        footprint_ref: None,
                        pad_number: None,
                        fromto_id: None,
                    },
                    Pad {
                        id: id(2),
                        net: 1,
                        at: [30, 10],
                        layers: vec!["B.Cu".into()],
                        local_clearance_nm: None,
                        shape: Shape::Circle { radius_nm: 1 },
                        drill: None,
                        zone_connection: crate::model::PadZoneConnection::Inherited,
                        thermal_gap_nm: None,
                        thermal_spoke_width_nm: None,
                        thermal_spoke_angle_mdeg: None,
                        orientation_mdeg: 0,
                        footprint_ref: None,
                        pad_number: None,
                        fromto_id: None,
                    },
                ],
                tracks: Vec::new(),
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
                            "allow_blind_buried_vias": false,
                            "allow_microvias": false
                        }}},
                        "net_settings": {"classes": [{
                            "name": "Default",
                            "track_width": 0.000002,
                            "clearance": 0.000001,
                            "via_diameter": 0.000004,
                            "via_drill": 0.000002
                        }]}
                    })
                    .to_string(),
                ),
                custom_rules: None,
                effective_rules: None,
                zones: Vec::new(),
                components: Vec::new(),
                stackup: Vec::new(),
                routing_options: None,
            };

            // Obstacles exercising track-before-via, via-before-track, and unreachable worlds.
            if (mask & 1) != 0 {
                input.tracks.push(track(10, 0, [10, 10], [20, 10], 4));
            }
            if (mask & 2) != 0 {
                let mut t = track(11, 0, [10, 10], [20, 10], 4);
                t.layer = "B.Cu".into();
                input.tracks.push(t);
            }
            if (mask & 4) != 0 {
                // Force via away from [0,10] and [10,10] on F.Cu (track-before-via interactions)
                input.rule_areas.push(RuleArea {
                    id: id(12),
                    layers: vec!["F.Cu".into()],
                    no_tracks: false,
                    no_vias: true,
                    outer: vec![[-5, -5], [15, -5], [15, 25], [-5, 25]],
                    holes: Vec::new(),
                    name: None,
                    extra_polygons: Vec::new(),
                    no_pads: false,
                    no_footprints: false,
                    no_zone_fills: false,
                });
            }
            if (mask & 8) != 0 {
                // Block [0,0]->[10,0] on F.Cu or [20,0]->[30,0] on B.Cu
                input.tracks.push(track(13, 0, [10, 0], [10, 5], 4));
            }
            if (mask & 16) != 0 {
                // Force via away from [30,10] (via-before-track interactions on B.Cu)
                input.rule_areas.push(RuleArea {
                    id: id(14),
                    layers: vec!["B.Cu".into()],
                    no_tracks: false,
                    no_vias: true,
                    outer: vec![[25, -5], [35, -5], [35, 25], [25, 25]],
                    holes: Vec::new(),
                    name: None,
                    extra_polygons: Vec::new(),
                    no_pads: false,
                    no_footprints: false,
                    no_zone_fills: false,
                });
            }

            let engine = Engine::new(input.clone()).unwrap();
            let actual = join_components(
                &engine,
                1,
                &[Copper::Pad(&engine.input.pads[0])],
                &[vec![Copper::Pad(&engine.input.pads[1])]],
                50_000,
                &mut 0,
            );

            // Independent augmented small-world Dijkstra oracle for same_net_hole_clearance_nm > 0.
            let via_cost = engine.effective_rules.via_cost.max(1);
            type OracleKey = (Point, usize, Option<(u8, Point)>, u16, Vec<PlacedViaKey>);
            let oracle_cost = {
                let mut dist: HashMap<OracleKey, u64> = HashMap::new();
                let mut pq: BinaryHeap<Reverse<(u64, OracleKey)>> = BinaryHeap::new();
                if engine.legal_edge_on(1, 0, [0, 10], [0, 10]).unwrap() {
                    let k0: OracleKey = ([0, 10], 0, None, 0, Vec::new());
                    dist.insert(k0.clone(), 0);
                    pq.push(Reverse((0, k0)));
                }
                let finalize_run_mask = |l: usize, rs: Point, end: Point, mut bmask: u16| -> u16 {
                    for (idx, &gp) in grid_pts.iter().enumerate() {
                        if (bmask & (1u16 << idx)) != 0 {
                            continue;
                        }
                        let pv = PlacedViaKey {
                            at: gp,
                            start_layer: 0,
                            end_layer: 1,
                            kind: 0,
                        };
                        if !engine
                            .edge_clears_same_net_vias(1, l, rs, end, &[pv])
                            .unwrap()
                        {
                            bmask |= 1u16 << idx;
                        }
                    }
                    bmask
                };
                let mut found = None;
                while let Some(Reverse((g, state))) = pq.pop() {
                    if dist.get(&state).is_some_and(|&d| d < g) {
                        continue;
                    }
                    let (p, l, dir_rs, bmask, vias) = state;
                    if p == [30, 10] && l == 1 {
                        found = Some(g);
                        break;
                    }
                    for (d, delta) in [[10, 0], [0, 10], [-10, 0], [0, -10]].iter().enumerate() {
                        let q = [p[0] + delta[0], p[1] + delta[1]];
                        if !grid_pts.contains(&q) {
                            continue;
                        }
                        let (seg_start, new_bmask) = match dir_rs {
                            Some((prev_d, rs)) if prev_d == d as u8 => (rs, bmask),
                            Some((_, rs)) => (p, finalize_run_mask(l, rs, p, bmask)),
                            None => (p, bmask),
                        };
                        if !(engine.legal_edge_on(1, l, p, q).unwrap()
                            && engine.edge_clears_same_net_vias(1, l, p, q, &vias).unwrap()
                            || (seg_start != p
                                && engine.legal_edge_on(1, l, seg_start, q).unwrap()
                                && engine
                                    .edge_clears_same_net_vias(1, l, seg_start, q, &vias)
                                    .unwrap()))
                        {
                            continue;
                        }
                        let nk: OracleKey =
                            (q, l, Some((d as u8, seg_start)), new_bmask, vias.clone());
                        let ng = g + 1;
                        if dist.get(&nk).is_none_or(|&old| ng < old) {
                            dist.insert(nk.clone(), ng);
                            pq.push(Reverse((ng, nk)));
                        }
                    }
                    let pv = PlacedViaKey {
                        at: p,
                        start_layer: 0,
                        end_layer: 1,
                        kind: 0,
                    };
                    let idx = pt_idx(p);
                    if !vias.contains(&pv)
                        && (bmask & (1u16 << idx)) == 0
                        && dir_rs.is_none_or(|(_, rs)| {
                            engine
                                .edge_clears_same_net_vias(1, l, rs, p, &[pv])
                                .unwrap()
                        })
                        && engine
                            .legal_via(1, p, 0, 1, ViaKind::Through, &vias)
                            .unwrap()
                            .is_some()
                    {
                        let new_bmask = match dir_rs {
                            Some((_, rs)) => finalize_run_mask(l, rs, p, bmask),
                            None => bmask,
                        };
                        let mut nv = vias.clone();
                        nv.push(pv);
                        nv.sort_unstable();
                        let nk: OracleKey = (p, 1 - l, None, new_bmask, nv);
                        let ng = g + via_cost;
                        if dist.get(&nk).is_none_or(|&old| ng < old) {
                            dist.insert(nk.clone(), ng);
                            pq.push(Reverse((ng, nk)));
                        }
                    }
                }
                found
            };

            match (oracle_cost, actual) {
                (Some(c), Ok(path)) => {
                    reachable += 1;
                    assert_eq!(path.steps, c, "snh={snh} mask={mask}");
                    assert!(
                        first_same_net_via_track_conflict(&engine, 1, &path)
                            .unwrap()
                            .is_none(),
                        "production path has via-track conflict for snh={snh} mask={mask}: {:?}",
                        path.edges
                    );
                }
                (None, Err(err)) => {
                    unreachable += 1;
                    assert_eq!(err.status, Status::Unroutable, "snh={snh} mask={mask}");
                }
                (exp, got) => {
                    panic!("oracle mismatch snh={snh} mask={mask}: oracle={exp:?} got={got:?}")
                }
            }
        }
    }
    assert!(reachable > 0 && unreachable > 0);
}

#[test]
fn join_components_moat_crossing_locked_via_start_and_target_symmetry() {
    let run_and_reload = |input: Input| -> crate::model::Output {
        let res = crate::run(input.clone());
        assert_eq!(res.status, Status::Complete, "expected Complete: {res:?}");
        let mut reloaded = input;
        reloaded.tracks.extend(res.added_tracks.iter().cloned());
        reloaded.vias.extend(res.added_vias.iter().cloned());
        let res2 = crate::run(reloaded);
        assert_eq!(res2.status, Status::Complete);
        assert!(res2.added_tracks.is_empty());
        assert!(res2.added_vias.is_empty());
        res
    };

    let base_rules = |snh: i64| Rules {
        width_nm: 2,
        clearance_nm: 1,
        grid_nm: 10,
        edge_clearance_nm: 1,
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: snh,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    let make_via = |n: u64, at: Point, s: &str, e: &str, kind: ViaKind| Via {
        id: id(n),
        net: 1,
        at,
        diameter_nm: 4,
        drill_nm: 2,
        start_layer: s.into(),
        end_layer: e.into(),
        kind,
        locked: true,
    };

    // 1. Challenger 1 Reproduction A: 2L Pad@[0,0] -> locked Through Via@[30,0] with snh=15 and snh=25
    for snh in [15, 25] {
        let mut p = pad(1, 1, [0, 0], Shape::Circle { radius_nm: 2 });
        p.layers = vec!["F.Cu".into()];
        let inp = Input {
            version: INPUT_VERSION,
            source_id: "e".repeat(64),
            layer: "F.Cu".into(),
            layers: vec!["F.Cu".into(), "B.Cu".into()],
            bounds: Bounds {
                min: [-50, -50],
                max: [100, 50],
            },
            rules: base_rules(snh),
            nets: vec![Net {
                id: 1,
                name: "SIG".into(),
            }],
            pads: vec![p],
            tracks: vec![],
            vias: vec![make_via(2, [30, 0], "F.Cu", "B.Cu", ViaKind::Through)],
            rule_areas: vec![],
            max_expansions: 50_000,
            limits: Limits::default(),
            holes: vec![],
            board_outline: None,
            graphics: vec![],
            mask_apertures: vec![],
            board_design_settings: None,
            project_rules: None,
            custom_rules: None,
            effective_rules: None,
            zones: Vec::new(),
            components: Vec::new(),
            stackup: Vec::new(),
            routing_options: None,
        };
        let res = run_and_reload(inp);
        assert_eq!(res.added_tracks.len(), 1);
        assert_eq!(res.added_tracks[0].start, [0, 0]);
        assert_eq!(res.added_tracks[0].end, [28, 0]);
    }

    // 2. Symmetry: locked Through Via@[0,0] -> Pad@[30,0], and locked Through Via@[0,0] -> locked Through Via@[30,0]
    {
        let mut p = pad(2, 1, [30, 0], Shape::Circle { radius_nm: 2 });
        p.layers = vec!["F.Cu".into()];
        let via_to_pad = Input {
            version: INPUT_VERSION,
            source_id: "e".repeat(64),
            layer: "F.Cu".into(),
            layers: vec!["F.Cu".into(), "B.Cu".into()],
            bounds: Bounds {
                min: [-50, -50],
                max: [100, 50],
            },
            rules: base_rules(15),
            nets: vec![Net {
                id: 1,
                name: "SIG".into(),
            }],
            pads: vec![p],
            tracks: vec![],
            vias: vec![make_via(1, [0, 0], "F.Cu", "B.Cu", ViaKind::Through)],
            rule_areas: vec![],
            max_expansions: 50_000,
            limits: Limits::default(),
            holes: vec![],
            board_outline: None,
            graphics: vec![],
            mask_apertures: vec![],
            board_design_settings: None,
            project_rules: None,
            custom_rules: None,
            effective_rules: None,
            zones: Vec::new(),
            components: Vec::new(),
            stackup: Vec::new(),
            routing_options: None,
        };
        let res = run_and_reload(via_to_pad);
        assert_eq!(res.added_tracks.len(), 1);

        let via_to_via = Input {
            version: INPUT_VERSION,
            source_id: "e".repeat(64),
            layer: "F.Cu".into(),
            layers: vec!["F.Cu".into(), "B.Cu".into()],
            bounds: Bounds {
                min: [-50, -50],
                max: [100, 50],
            },
            rules: base_rules(15),
            nets: vec![Net {
                id: 1,
                name: "SIG".into(),
            }],
            pads: vec![],
            tracks: vec![],
            vias: vec![
                make_via(1, [0, 0], "F.Cu", "B.Cu", ViaKind::Through),
                make_via(2, [30, 0], "F.Cu", "B.Cu", ViaKind::Through),
            ],
            rule_areas: vec![],
            max_expansions: 50_000,
            limits: Limits::default(),
            holes: vec![],
            board_outline: None,
            graphics: vec![],
            mask_apertures: vec![],
            board_design_settings: None,
            project_rules: None,
            custom_rules: None,
            effective_rules: None,
            zones: Vec::new(),
            components: Vec::new(),
            stackup: Vec::new(),
            routing_options: None,
        };
        let res2 = run_and_reload(via_to_via);
        assert_eq!(res2.added_tracks.len(), 1);
        assert_eq!(res2.added_tracks[0].start, [0, 0]);
        assert_eq!(res2.added_tracks[0].end, [28, 0]);
    }

    // 3. Challenger 1 Reproduction B (4L Pad/Via -> locked Buried Via In1..In2) + 4L Buried Via start/target symmetry
    let layers_4l = vec![
        "F.Cu".into(),
        "In1.Cu".into(),
        "In2.Cu".into(),
        "B.Cu".into(),
    ];
    let allow_blind_pro = serde_json::json!({
        "meta": {"version": 3},
        "board": {"design_settings": {"rules": {
            "min_track_width": 0.000002,
            "min_clearance": 0.000001,
            "min_via_diameter": 0.000004,
            "min_through_hole_diameter": 0.000002,
            "allow_blind_buried_vias": true,
            "allow_microvias": false
        }}},
        "net_settings": {"classes": [{
            "name": "Default",
            "track_width": 0.000002,
            "clearance": 0.000001,
            "via_diameter": 0.000004,
            "via_drill": 0.000002
        }]}
    })
    .to_string();

    let mut p1 = pad(1, 1, [0, 0], Shape::Circle { radius_nm: 2 });
    p1.layers = vec!["F.Cu".into()];
    let mut p2 = pad(2, 1, [30, 0], Shape::Circle { radius_nm: 2 });
    p2.layers = vec!["B.Cu".into()];
    let mut t_b = track(3, 1, [0, 0], [29, 0], 2);
    t_b.layer = "B.Cu".into();
    let repro_b = Input {
        version: INPUT_VERSION,
        source_id: "e".repeat(64),
        layer: "F.Cu".into(),
        layers: layers_4l.clone(),
        bounds: Bounds {
            min: [-50, -50],
            max: [100, 50],
        },
        rules: base_rules(15),
        nets: vec![Net {
            id: 1,
            name: "SIG".into(),
        }],
        pads: vec![p1, p2],
        tracks: vec![t_b],
        vias: vec![
            make_via(4, [0, 0], "F.Cu", "B.Cu", ViaKind::Through),
            make_via(5, [10, 30], "In1.Cu", "In2.Cu", ViaKind::BlindBuried),
        ],
        rule_areas: vec![],
        max_expansions: 50_000,
        limits: Limits::default(),
        holes: vec![],
        board_outline: None,
        graphics: vec![],
        mask_apertures: vec![],
        board_design_settings: None,
        project_rules: Some(allow_blind_pro.clone()),
        custom_rules: None,
        effective_rules: None,
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
    };
    run_and_reload(repro_b);

    // 4L locked Buried Via (In1..In2) -> locked Buried Via (In1..In2) and -> F.Cu Pad
    let buried_to_buried = Input {
        version: INPUT_VERSION,
        source_id: "e".repeat(64),
        layer: "F.Cu".into(),
        layers: layers_4l.clone(),
        bounds: Bounds {
            min: [-50, -50],
            max: [100, 50],
        },
        rules: base_rules(15),
        nets: vec![Net {
            id: 1,
            name: "SIG".into(),
        }],
        pads: vec![],
        tracks: vec![],
        vias: vec![
            make_via(1, [0, 0], "In1.Cu", "In2.Cu", ViaKind::BlindBuried),
            make_via(2, [30, 0], "In1.Cu", "In2.Cu", ViaKind::BlindBuried),
        ],
        rule_areas: vec![],
        max_expansions: 50_000,
        limits: Limits::default(),
        holes: vec![],
        board_outline: None,
        graphics: vec![],
        mask_apertures: vec![],
        board_design_settings: None,
        project_rules: Some(allow_blind_pro.clone()),
        custom_rules: None,
        effective_rules: None,
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
    };
    run_and_reload(buried_to_buried);

    let mut p_f = pad(2, 1, [30, 0], Shape::Circle { radius_nm: 2 });
    p_f.layers = vec!["F.Cu".into()];
    let buried_to_fcu_pad = Input {
        version: INPUT_VERSION,
        source_id: "e".repeat(64),
        layer: "F.Cu".into(),
        layers: layers_4l,
        bounds: Bounds {
            min: [-50, -50],
            max: [100, 50],
        },
        rules: base_rules(15),
        nets: vec![Net {
            id: 1,
            name: "SIG".into(),
        }],
        pads: vec![p_f],
        tracks: vec![],
        vias: vec![make_via(
            1,
            [0, 0],
            "In1.Cu",
            "In2.Cu",
            ViaKind::BlindBuried,
        )],
        rule_areas: vec![],
        max_expansions: 50_000,
        limits: Limits::default(),
        holes: vec![],
        board_outline: None,
        graphics: vec![],
        mask_apertures: vec![],
        board_design_settings: None,
        project_rules: Some(allow_blind_pro),
        custom_rules: None,
        effective_rules: None,
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
    };
    run_and_reload(buried_to_fcu_pad);
}

#[test]
fn target_escape_reachable_probes_via_when_neighbor_attaches_in_pocket() {
    let mut start_pad = pad(
        1,
        1,
        [5_000_000, 15_000_000],
        Shape::Circle { radius_nm: 300_000 },
    );
    start_pad.layers = vec!["F.Cu".into(), "B.Cu".into()];
    let mut target_pad = pad(
        2,
        1,
        [40_000_000, 15_000_000],
        Shape::Rect {
            half_size_nm: [300_000, 200_000],
        },
    );
    target_pad.layers = vec!["F.Cu".into()];

    let mut pads = vec![start_pad, target_pad];
    // Box q = [39_500_000, 15_000_000] into a <64-cell F.Cu pocket using foreign pads/tracks
    // plus a mid-board F.Cu+B.Cu wall at x = 20_000_000 forcing > 2048 A* expansions.
    let foreign_track = |tid: u64, layer: &str, a: Point, b: Point| Track {
        id: id(tid),
        net: 0,
        layer: layer.into(),
        start: a,
        end: b,
        width_nm: 200_000,
        locked: true,
    };
    let tracks = vec![
        foreign_track(
            10,
            "F.Cu",
            [38_500_000, 13_500_000],
            [41_000_000, 13_500_000],
        ),
        foreign_track(
            11,
            "F.Cu",
            [38_500_000, 16_500_000],
            [41_000_000, 16_500_000],
        ),
        foreign_track(
            12,
            "F.Cu",
            [38_500_000, 13_500_000],
            [38_500_000, 16_500_000],
        ),
        foreign_track(
            13,
            "F.Cu",
            [41_000_000, 13_500_000],
            [41_000_000, 16_500_000],
        ),
        foreign_track(
            14,
            "F.Cu",
            [20_000_000, 2_000_000],
            [20_000_000, 28_000_000],
        ),
        foreign_track(
            15,
            "B.Cu",
            [20_000_000, 2_000_000],
            [20_000_000, 28_000_000],
        ),
    ];
    let mut fp = pad(
        20,
        0,
        [39_000_000, 14_000_000],
        Shape::Circle { radius_nm: 250_000 },
    );
    fp.layers = vec!["F.Cu".into()];
    pads.push(fp);

    let rule_areas = vec![
        RuleArea {
            id: id(100),
            name: None,
            layers: vec!["F.Cu".into()],
            outer: vec![
                [39_850_000, 14_500_000],
                [41_250_000, 14_500_000],
                [41_250_000, 15_500_000],
                [39_850_000, 15_500_000],
            ],
            holes: Vec::new(),
            extra_polygons: Vec::new(),
            no_tracks: true,
            no_vias: false,
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
        RuleArea {
            id: id(101),
            name: None,
            layers: vec!["F.Cu".into()],
            outer: vec![
                [38_700_000, 14_500_000],
                [39_700_000, 14_500_000],
                [39_700_000, 15_500_000],
                [38_700_000, 15_500_000],
            ],
            holes: Vec::new(),
            extra_polygons: Vec::new(),
            no_tracks: false,
            no_vias: true,
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
    ];

    let inp = Input {
        version: INPUT_VERSION,
        source_id: "f".repeat(64),
        layer: "F.Cu".into(),
        layers: vec!["F.Cu".into(), "B.Cu".into()],
        bounds: Bounds {
            min: [0, 0],
            max: [50_000_000, 30_000_000],
        },
        rules: Rules {
            width_nm: 200_000,
            clearance_nm: 150_000,
            grid_nm: 500_000,
            edge_clearance_nm: 200_000,
            hole_clearance_nm: 150_000,
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
        rule_areas,
        max_expansions: 100_000,
        limits: Limits::default(),
        holes: Vec::new(),
        board_outline: None,
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
        board_design_settings: None,
        project_rules: None,
        custom_rules: None,
        effective_rules: None,
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
    };

    let mut eng = Engine::new(inp.clone()).unwrap();
    assert!(
        eng.route_all().is_ok(),
        "route_all must succeed via target-center via when attached neighbor pocket has no vias"
    );
    let out = crate::run(inp);
    assert_eq!(out.status, Status::Complete);
    assert!(
        out.added_vias
            .iter()
            .any(|v| v.at == [40_000_000, 15_000_000]),
        "expected via at target pad center [40_000_000, 15_000_000], got {:?}",
        out.added_vias
    );
}

fn build_probe1b_4l_blind_buried_input(with_victim: bool) -> Input {
    let sig_net = if with_victim { 2 } else { 1 };
    let mut start_pad = pad(
        1,
        sig_net,
        [5_000_000, 15_000_000],
        Shape::Circle { radius_nm: 300_000 },
    );
    start_pad.layers = vec!["F.Cu".into(), "In2.Cu".into()];
    let mut target_pad = pad(
        2,
        sig_net,
        [40_000_000, 15_000_000],
        Shape::Rect {
            half_size_nm: [300_000, 200_000],
        },
    );
    target_pad.layers = vec!["In1.Cu".into()];

    let mut pads = vec![start_pad, target_pad];
    let mut nets = vec![Net {
        id: 0,
        name: String::new(),
    }];
    if with_victim {
        nets.push(Net {
            id: 1,
            name: "VIC".into(),
        });
        let mut vp1 = pad(
            3,
            1,
            [30_000_000, 15_000_000],
            Shape::Circle { radius_nm: 300_000 },
        );
        vp1.layers = vec!["In2.Cu".into()];
        let mut vp2 = pad(
            4,
            1,
            [45_000_000, 15_000_000],
            Shape::Circle { radius_nm: 300_000 },
        );
        vp2.layers = vec!["In2.Cu".into()];
        pads.push(vp1);
        pads.push(vp2);
    }
    nets.push(Net {
        id: sig_net,
        name: "SIG".into(),
    });

    let wall_track = |tid: u64, layer: &str| Track {
        id: id(tid),
        net: 0,
        layer: layer.into(),
        start: [20_000_000, 2_000_000],
        end: [20_000_000, 28_000_000],
        width_nm: 200_000,
        locked: true,
    };
    let tracks = vec![
        wall_track(10, "F.Cu"),
        wall_track(11, "In1.Cu"),
        wall_track(12, "In2.Cu"),
        wall_track(13, "B.Cu"),
    ];

    let mk_ra =
        |rid: u64, layer: &str, min: Point, max: Point, no_tracks: bool, no_vias: bool| RuleArea {
            id: id(rid),
            name: None,
            layers: vec![layer.into()],
            outer: vec![min, [max[0], min[1]], max, [min[0], max[1]]],
            holes: Vec::new(),
            extra_polygons: Vec::new(),
            no_tracks,
            no_vias,
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        };
    // Box [40M, 15M] into a 1-cell In1.Cu pocket leaving legal_edge_on(sig_net, 1, p, p) == true,
    // while disallowing B.Cu vias at [40M, 15M].
    let rule_areas = vec![
        mk_ra(
            100,
            "In1.Cu",
            [40_350_000, 14_000_000],
            [41_000_000, 16_000_000],
            true,
            true,
        ),
        mk_ra(
            101,
            "In1.Cu",
            [39_000_000, 14_000_000],
            [39_650_000, 16_000_000],
            true,
            true,
        ),
        mk_ra(
            102,
            "In1.Cu",
            [39_000_000, 15_350_000],
            [41_000_000, 16_000_000],
            true,
            true,
        ),
        mk_ra(
            103,
            "In1.Cu",
            [39_000_000, 14_000_000],
            [41_000_000, 14_650_000],
            true,
            true,
        ),
        mk_ra(
            104,
            "B.Cu",
            [39_500_000, 14_500_000],
            [40_500_000, 15_500_000],
            false,
            true,
        ),
    ];

    let allow_blind_pro = serde_json::json!({
        "meta": {"version": 3},
        "board": {"design_settings": {"rules": {
            "min_track_width": 0.2,
            "min_clearance": 0.15,
            "min_via_diameter": 0.4,
            "min_through_hole_diameter": 0.2,
            "allow_blind_buried_vias": true,
            "allow_microvias": false
        }}},
        "net_settings": {"classes": [{
            "name": "Default",
            "track_width": 0.2,
            "clearance": 0.15,
            "via_diameter": 0.6,
            "via_drill": 0.3
        }]}
    })
    .to_string();
    let custom_rules = "\
(version 1)
(rule \"no_0_1\" (condition \"!A.existsOnLayer('In2.Cu')\") (constraint disallow via))
(rule \"no_1_2\" (condition \"!A.existsOnLayer('F.Cu')\") (constraint disallow via))
";

    Input {
        version: INPUT_VERSION,
        source_id: "f".repeat(64),
        layer: "F.Cu".into(),
        layers: vec![
            "F.Cu".into(),
            "In1.Cu".into(),
            "In2.Cu".into(),
            "B.Cu".into(),
        ],
        bounds: Bounds {
            min: [0, 0],
            max: [50_000_000, 30_000_000],
        },
        rules: Rules {
            width_nm: 200_000,
            clearance_nm: 150_000,
            grid_nm: 500_000,
            edge_clearance_nm: 200_000,
            hole_clearance_nm: 150_000,
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
        },
        nets,
        pads,
        tracks,
        vias: Vec::new(),
        rule_areas,
        max_expansions: 100_000,
        limits: Limits::default(),
        holes: Vec::new(),
        board_outline: None,
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
        board_design_settings: None,
        project_rules: Some(allow_blind_pro),
        custom_rules: Some(custom_rules.into()),
        effective_rules: None,
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
    }
}

#[test]
fn target_escape_reachable_probes_interior_blind_buried_via_when_legal_edge_on_in_pocket() {
    let inp = build_probe1b_4l_blind_buried_input(false);
    let mut eng = Engine::new(inp.clone()).unwrap();
    assert!(
        eng.legal_edge_on(1, 1, [40_000_000, 15_000_000], [40_000_000, 15_000_000])
            .unwrap(),
        "precondition: [40M, 15M] itself must have legal_edge_on == true inside 1-cell pocket"
    );
    assert!(
        eng.route_all().is_ok(),
        "route_all must succeed via interior BlindBuried(0..=2) even when legal_edge_on == true"
    );
    let out = crate::run(inp);
    assert_eq!(out.status, Status::Complete);
    assert!(
        out.added_vias.iter().any(|v| {
            v.at == [40_000_000, 15_000_000]
                && v.kind == ViaKind::BlindBuried
                && v.start_layer == "F.Cu"
                && v.end_layer == "In2.Cu"
        }),
        "expected BlindBuried(F.Cu..In2.Cu) via at [40M, 15M], got {:?}",
        out.added_vias
    );
}

#[test]
fn target_escape_reachable_rips_victim_blocking_interior_blind_buried_via_in_pocket() {
    let inp = build_probe1b_4l_blind_buried_input(true);
    let mut eng = Engine::new(inp.clone()).unwrap();
    assert!(
        eng.route_all().is_ok(),
        "route_all must rip In2.Cu victim and complete both nets via BlindBuried(0..=2)"
    );
    assert!(eng.copper().index_consistent());
    let stats = eng
        .last_negotiation_stats()
        .expect("negotiation stats recorded");
    assert!(
        stats.units_ripped >= 1 && stats.rounds_committed >= 1,
        "expected victim rip-up round to commit: {stats:?}"
    );
    let out = crate::run(inp);
    assert_eq!(out.status, Status::Complete);
    assert!(
        out.added_vias.iter().any(|v| {
            v.net == 2
                && v.at == [40_000_000, 15_000_000]
                && v.kind == ViaKind::BlindBuried
                && v.start_layer == "F.Cu"
                && v.end_layer == "In2.Cu"
        }),
        "expected SIG BlindBuried(F.Cu..In2.Cu) via at [40M, 15M], got {:?}",
        out.added_vias
    );
}

#[test]
fn unprotected_smd_via_in_pad_rejected_and_protected_allowed() {
    let make_input = |cap_vias: bool, fill_vias: bool, box_tracks: bool| {
        let mut p1 = pad(
            1,
            1,
            [10_000_000, 10_000_000],
            Shape::Rect {
                half_size_nm: [500_000, 500_000],
            },
        );
        p1.layers = vec!["F.Cu".into()];
        let mut p2 = pad(
            2,
            1,
            [30_000_000, 10_000_000],
            Shape::Rect {
                half_size_nm: [500_000, 500_000],
            },
        );
        p2.layers = vec!["B.Cu".into()];
        let mut rule_areas = Vec::new();
        if box_tracks {
            rule_areas.push(RuleArea {
                id: id(90),
                name: None,
                layers: vec!["F.Cu".into()],
                outer: vec![
                    [8_000_000, 8_000_000],
                    [12_000_000, 8_000_000],
                    [12_000_000, 12_000_000],
                    [8_000_000, 12_000_000],
                ],
                holes: Vec::new(),
                extra_polygons: Vec::new(),
                no_tracks: true,
                no_vias: false,
                no_pads: false,
                no_footprints: false,
                no_zone_fills: false,
            });
        }
        Input {
            version: INPUT_VERSION,
            source_id: "e".repeat(64),
            layer: "F.Cu".into(),
            layers: vec!["F.Cu".into(), "B.Cu".into()],
            bounds: Bounds {
                min: [0, 0],
                max: [40_000_000, 20_000_000],
            },
            rules: Rules {
                width_nm: 200_000,
                clearance_nm: 200_000,
                grid_nm: 1_000_000,
                edge_clearance_nm: 500_000,
                hole_clearance_nm: 200_000,
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
            pads: vec![p1, p2],
            tracks: Vec::new(),
            vias: Vec::new(),
            rule_areas,
            max_expansions: 50_000,
            limits: Limits::default(),
            holes: Vec::new(),
            board_outline: None,
            graphics: Vec::new(),
            mask_apertures: Vec::new(),
            board_design_settings: Some(crate::rules::BoardDesignSettings {
                min_track_width_nm: 200_000,
                min_clearance_nm: 200_000,
                min_edge_clearance_nm: 500_000,
                min_via_diameter_nm: 500_000,
                min_via_annular_width_nm: 100_000,
                min_through_hole_diameter_nm: 300_000,
                min_hole_clearance_nm: 200_000,
                min_hole_to_hole_nm: 200_000,
                default_clearance_nm: 200_000,
                default_track_width_nm: 200_000,
                default_via_diameter_nm: 600_000,
                default_via_drill_nm: 300_000,
                cap_vias,
                fill_vias,
                ..Default::default()
            }),
            project_rules: None,
            custom_rules: None,
            effective_rules: None,
            zones: Vec::new(),
            components: Vec::new(),
            stackup: Vec::new(),
            routing_options: None,
        }
    };

    for (cap, fill) in [(false, false), (true, false), (false, true)] {
        let eng = Engine::new(make_input(cap, fill, false)).unwrap();
        assert!(
            eng.legal_via(1, [10_000_000, 10_000_000], 0, 1, ViaKind::Through, &[])
                .unwrap()
                .is_none(),
            "unprotected SMD via-in-pad (cap={cap}, fill={fill}) must be refused"
        );
        let out = crate::run(make_input(cap, fill, false));
        assert_eq!(out.status, Status::Complete);
        assert!(
            out.added_vias
                .iter()
                .all(|v| (v.at[0] - 10_000_000).abs() > 800_000 || (v.at[1] - 10_000_000).abs() > 800_000),
            "routed board without Type VII cap+fill must step off SMD pad before dropping via: {:?}",
            out.added_vias
        );
        let boxed = crate::run(make_input(cap, fill, true));
        assert_eq!(boxed.status, Status::Unroutable);
    }

    let eng_prot = Engine::new(make_input(true, true, true)).unwrap();
    assert!(
        eng_prot
            .legal_via(1, [10_000_000, 10_000_000], 0, 1, ViaKind::Through, &[])
            .unwrap()
            .is_some(),
        "protected SMD via-in-pad (cap=true, fill=true) must be permitted"
    );
    let out_prot = crate::run(make_input(true, true, true));
    assert_eq!(out_prot.status, Status::Complete);
    assert!(
        out_prot
            .added_vias
            .iter()
            .any(|v| v.at == [10_000_000, 10_000_000]),
        "expected Type VII via-in-pad at [10M, 10M], got {:?}",
        out_prot.added_vias
    );
}

#[test]
fn pad_interior_track_attachment_enters_offgrid_smd_pad_when_legal() {
    let mut p1 = pad(
        1,
        1,
        [0, 2_000_000],
        Shape::Rect {
            half_size_nm: [200_000, 200_000],
        },
    );
    p1.layers = vec!["F.Cu".into()];
    let mut p2 = pad(
        2,
        1,
        [10_000_000, 2_000_000],
        Shape::Rect {
            half_size_nm: [200_000, 200_000],
        },
    );
    p2.layers = vec!["F.Cu".into()];
    let inp = Input {
        version: INPUT_VERSION,
        source_id: "f".repeat(64),
        layer: "F.Cu".into(),
        layers: vec!["F.Cu".into()],
        bounds: Bounds {
            min: [-2_000_000, -2_000_000],
            max: [12_000_000, 6_000_000],
        },
        rules: Rules {
            width_nm: 293_000,
            clearance_nm: 200_000,
            grid_nm: 293_000,
            edge_clearance_nm: 200_000,
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
                name: "RF".into(),
            },
        ],
        pads: vec![p1.clone(), p2.clone()],
        tracks: Vec::new(),
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
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
    };
    let out = crate::run(inp);
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    assert_eq!(out.added_tracks.len(), 1);
    let trk = &out.added_tracks[0];
    assert!(
        crate::geometry::point_in_pad(trk.start, &p1),
        "expected track start inside source pad, got {:?}",
        trk.start
    );
    assert!(
        crate::geometry::point_in_pad(trk.end, &p2),
        "expected track endpoint inside target pad, got {:?}",
        trk.end
    );
}

#[test]
fn bidirectional_search_finds_exact_optimal_cost_with_smd_via_in_pad_disallowed() {
    let mut pads = vec![
        pad(
            1,
            1,
            [2_000_000, 10_000_000],
            Shape::Rect {
                half_size_nm: [350_000, 350_000],
            },
        ),
        pad(
            2,
            1,
            [42_000_000, 10_000_000],
            Shape::Rect {
                half_size_nm: [350_000, 350_000],
            },
        ),
    ];
    pads[0].layers = vec!["F.Cu".into()];
    pads[1].layers = vec!["F.Cu".into()];
    let mut next_id = 10u64;
    for row in 0..20 {
        let y = 600_000 + row * 990_000;
        let mut obs = pad(
            next_id,
            0,
            [22_000_000, y],
            Shape::Circle { radius_nm: 300_000 },
        );
        obs.layers = vec!["F.Cu".into()];
        pads.push(obs);
        next_id += 1;
    }
    let inp = Input {
        version: INPUT_VERSION,
        source_id: "b".repeat(64),
        layer: "F.Cu".into(),
        layers: vec!["F.Cu".into(), "B.Cu".into()],
        bounds: Bounds {
            min: [0, 0],
            max: [44_000_000, 20_000_000],
        },
        rules: Rules {
            width_nm: 200_000,
            clearance_nm: 200_000,
            grid_nm: 200_000,
            edge_clearance_nm: 500_000,
            hole_clearance_nm: 200_000,
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
        tracks: Vec::new(),
        vias: Vec::new(),
        rule_areas: Vec::new(),
        max_expansions: 100_000,
        limits: Limits::default(),
        holes: Vec::new(),
        board_outline: None,
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
        board_design_settings: Some(crate::rules::BoardDesignSettings {
            min_track_width_nm: 200_000,
            min_clearance_nm: 200_000,
            min_edge_clearance_nm: 500_000,
            min_via_diameter_nm: 500_000,
            min_via_annular_width_nm: 100_000,
            min_through_hole_diameter_nm: 300_000,
            min_hole_clearance_nm: 200_000,
            min_hole_to_hole_nm: 200_000,
            default_clearance_nm: 200_000,
            default_track_width_nm: 200_000,
            default_via_diameter_nm: 500_000,
            default_via_drill_nm: 300_000,
            cap_vias: false,
            fill_vias: false,
            ..Default::default()
        }),
        project_rules: None,
        custom_rules: None,
        effective_rules: None,
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
    };
    let out = crate::run(inp.clone());
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    assert!(
        out.expanded_states > 256,
        "expected bidirectional phase > 256 exp, got {}",
        out.expanded_states
    );
    assert_eq!(out.added_vias.len(), 2, "expected 2 vias around F.Cu wall");
    for v in &out.added_vias {
        assert!((v.at[0] - 2_000_000).abs() > 400_000 || (v.at[1] - 10_000_000).abs() > 400_000);
        assert!((v.at[0] - 42_000_000).abs() > 400_000 || (v.at[1] - 10_000_000).abs() > 400_000);
    }
    let out2 = crate::run(inp);
    assert_eq!(out.added_tracks, out2.added_tracks);
    assert_eq!(out.added_vias, out2.added_vias);
    assert_eq!(out.work_used, out2.work_used);
    assert_eq!(out.expanded_states, out2.expanded_states);
}

#[test]
fn bidirectional_first_via_conflict_retries_and_completes_legally() {
    let mut pads = vec![
        pad(
            1,
            1,
            [2_000_000, 10_000_000],
            Shape::Circle { radius_nm: 250_000 },
        ),
        pad(
            2,
            1,
            [42_000_000, 10_000_000],
            Shape::Circle { radius_nm: 250_000 },
        ),
    ];
    pads[0].layers = vec!["F.Cu".into()];
    pads[1].layers = vec!["F.Cu".into()];
    // F.Cu wall at x=22mm forcing >256 expansions (bidirectional A*) and transition to inner layer;
    // In1.Cu wall at x=25mm forcing a second blind/buried via transition, where two blind vias
    // without prior_vias checking trigger `first_via_conflict` -> `ConflictZone` retry.
    let mut tracks = Vec::new();
    let mut next_id = 10u64;
    for row in 0..20 {
        let y = 600_000 + row * 990_000;
        let mut obs = pad(
            next_id,
            0,
            [22_000_000, y],
            Shape::Circle { radius_nm: 300_000 },
        );
        obs.layers = vec!["F.Cu".into()];
        pads.push(obs);
        next_id += 1;
    }
    tracks.push(Track {
        id: id(900),
        net: 0,
        layer: "In1.Cu".into(),
        start: [25_000_000, 1_000_000],
        end: [25_000_000, 19_000_000],
        width_nm: 300_000,
        locked: true,
    });
    let inp = Input {
        version: INPUT_VERSION,
        source_id: "c".repeat(64),
        layer: "F.Cu".into(),
        layers: vec![
            "F.Cu".into(),
            "In1.Cu".into(),
            "In2.Cu".into(),
            "B.Cu".into(),
        ],
        bounds: Bounds {
            min: [0, 0],
            max: [44_000_000, 20_000_000],
        },
        rules: Rules {
            width_nm: 200_000,
            clearance_nm: 200_000,
            grid_nm: 500_000,
            edge_clearance_nm: 500_000,
            hole_clearance_nm: 200_000,
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
        max_expansions: 200_000,
        limits: Limits::default(),
        holes: Vec::new(),
        board_outline: None,
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
        board_design_settings: Some(crate::rules::BoardDesignSettings {
            min_track_width_nm: 200_000,
            min_clearance_nm: 200_000,
            min_edge_clearance_nm: 500_000,
            min_via_diameter_nm: 500_000,
            min_via_annular_width_nm: 100_000,
            min_through_hole_diameter_nm: 300_000,
            min_hole_clearance_nm: 200_000,
            min_hole_to_hole_nm: 200_000,
            default_clearance_nm: 200_000,
            default_track_width_nm: 200_000,
            default_via_diameter_nm: 500_000,
            default_via_drill_nm: 300_000,
            ..Default::default()
        }),
        project_rules: Some(
            r#"{"meta":{"version":3},"board":{"design_settings":{"rules":{"allow_blind_buried_vias":true}}}}"#
                .into(),
        ),
        custom_rules: None,
        effective_rules: None,
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
    };
    let out = crate::run(inp.clone());
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    assert!(out.expanded_states > 256);
    assert!(out.added_vias.len() >= 2);
    let mut check_inp = inp;
    check_inp.tracks.extend(out.added_tracks);
    check_inp.vias.extend(out.added_vias);
    let eng = Engine::new(check_inp).unwrap();
    assert!(eng.components(1).unwrap().len() <= 1);
}

#[test]
fn bidirectional_starts_over_16_with_via_only_matches_forward_oracle() {
    // 18 F.Cu-only start seeds (via_only transitions on SMD pads when SMD via-in-pad is
    // disallowed or pad is on B.Cu), exercising the `starts > 16` + `via_only` bidirectional path.
    let mut pads = Vec::new();
    let mut tracks = Vec::new();
    for k in 0..18i64 {
        let mut p = pad(
            (k + 1) as u64,
            1,
            [2_000_000, 1_500_000 + k * 900_000],
            Shape::Rect {
                half_size_nm: [250_000, 250_000],
            },
        );
        p.layers = vec!["F.Cu".into()];
        pads.push(p);
        if k > 0 {
            tracks.push(Track {
                id: id(100 + k as u64),
                net: 1,
                layer: "F.Cu".into(),
                start: [2_000_000, 1_500_000 + (k - 1) * 900_000],
                end: [2_000_000, 1_500_000 + k * 900_000],
                width_nm: 200_000,
                locked: true,
            });
        }
    }
    let mut tgt = pad(
        99,
        1,
        [38_000_000, 10_000_000],
        Shape::Rect {
            half_size_nm: [350_000, 350_000],
        },
    );
    tgt.layers = vec!["B.Cu".into()];
    pads.push(tgt);
    // F.Cu vertical wall at x=20mm forcing >256 expansions and via transition
    for row in 0..20 {
        let mut obs = pad(
            200 + row as u64,
            0,
            [20_000_000, 600_000 + row * 990_000],
            Shape::Circle { radius_nm: 300_000 },
        );
        obs.layers = vec!["F.Cu".into()];
        pads.push(obs);
    }
    let inp = Input {
        version: INPUT_VERSION,
        source_id: "d".repeat(64),
        layer: "F.Cu".into(),
        layers: vec!["F.Cu".into(), "B.Cu".into()],
        bounds: Bounds {
            min: [0, 0],
            max: [40_000_000, 20_000_000],
        },
        rules: Rules {
            width_nm: 200_000,
            clearance_nm: 200_000,
            grid_nm: 500_000,
            edge_clearance_nm: 500_000,
            hole_clearance_nm: 200_000,
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
        max_expansions: 100_000,
        limits: Limits::default(),
        holes: Vec::new(),
        board_outline: None,
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
        board_design_settings: Some(crate::rules::BoardDesignSettings {
            min_track_width_nm: 200_000,
            min_clearance_nm: 200_000,
            min_edge_clearance_nm: 500_000,
            min_via_diameter_nm: 500_000,
            min_via_annular_width_nm: 100_000,
            min_through_hole_diameter_nm: 300_000,
            min_hole_clearance_nm: 200_000,
            min_hole_to_hole_nm: 200_000,
            default_clearance_nm: 200_000,
            default_track_width_nm: 200_000,
            default_via_diameter_nm: 500_000,
            default_via_drill_nm: 300_000,
            cap_vias: false,
            fill_vias: false,
            ..Default::default()
        }),
        project_rules: None,
        custom_rules: None,
        effective_rules: None,
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
    };
    let out = crate::run(inp.clone());
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    assert!(!out.added_vias.is_empty());
    let bi_len: f64 = out
        .added_tracks
        .iter()
        .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
        .sum();
    // Same problem with same_net_hole_clearance_nm=1 forces forward walk_multilayer oracle
    let mut fwd_inp = inp;
    fwd_inp.rules.same_net_hole_clearance_nm = 1;
    let fwd_out = crate::run(fwd_inp);
    assert_eq!(fwd_out.status, Status::Complete, "{:?}", fwd_out.reason);
    let fwd_len: f64 = fwd_out
        .added_tracks
        .iter()
        .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
        .sum();
    assert!(
        (bi_len - fwd_len).abs() <= 1_000.0,
        "bidirectional len {bi_len} != forward oracle len {fwd_len}"
    );
}

#[test]
fn refill_landings_resolve_narrow_regions_holes_boundaries_and_budgets() {
    let poly = OutlinePolygon {
        outer: vec![[-6, -4], [8, -4], [8, 4], [-6, 4]],
        holes: vec![vec![[-1, -2], [1, -2], [1, 2], [-1, 2]]],
    };
    let work = WorkBudget::new(Limits::default());
    let mut landings = FillLandings::default();
    // Neither end of the grid step lies on copper. Land inside the first
    // strip traversed, before the hole, in either direction.
    assert_eq!(
        landings
            .on_segment(&poly, [-10, 0], [10, 0], &work)
            .unwrap(),
        Some([-4, 0])
    );
    let first_scan = work.used();
    assert_eq!(
        landings
            .on_segment(&poly, [10, 0], [-10, 0], &work)
            .unwrap(),
        Some([4, 0])
    );
    assert_eq!(work.used(), first_scan);
    assert_eq!(
        landings
            .on_segment(&poly, [0, -10], [0, 10], &work)
            .unwrap(),
        Some([0, -3])
    );
    assert_eq!(
        landings.on_segment(&poly, [0, 0], [0, 0], &work).unwrap(),
        None
    );
    assert_eq!(
        landings.on_segment(&poly, [-1, 2], [1, 2], &work).unwrap(),
        None
    );
    assert_eq!(
        landings.on_segment(&poly, [-6, 4], [8, 4], &work).unwrap(),
        None
    );
    assert_eq!(
        landings
            .on_segment(&poly, [-10, 0], [-3, 0], &work)
            .unwrap(),
        Some([-3, 0])
    );
    let mut limited = FillLandings::default();
    let budget = WorkBudget::new(Limits {
        max_work: 7,
        ..Limits::default()
    });
    assert_eq!(
        limited
            .on_segment(&poly, [0, 0], [0, 0], &budget)
            .unwrap_err()
            .status,
        Status::WorkExhausted
    );
    let budget = WorkBudget::new(Limits {
        max_search_entries: 2,
        ..Limits::default()
    });
    assert_eq!(
        limited
            .on_segment(&poly, [0, 0], [0, 0], &budget)
            .unwrap_err()
            .status,
        Status::AdmissionExceeded
    );
}
