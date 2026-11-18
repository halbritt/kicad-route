use kicad_route_engine::{geometry, model, model::*, Engine, TransactionError};
use std::collections::BTreeSet;
use std::panic::{catch_unwind, AssertUnwindSafe};

#[path = "support/contact.rs"]
mod contact;
use contact::Body;

fn fixture() -> Input {
    Input {
        version: 1,
        source_id: "a".repeat(64),
        layer: "F.Cu".into(),
        layers: Vec::new(),
        bounds: Bounds {
            min: [-5000, -5000],
            max: [5000, 5000],
        },
        rules: Rules {
            width_nm: 3,
            clearance_nm: 2,
            grid_nm: 1,
            edge_clearance_nm: 0,
            hole_clearance_nm: 0,
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
        },
        nets: (0..=3)
            .map(|id| Net {
                id,
                name: if id == 0 {
                    String::new()
                } else {
                    format!("N{id}")
                },
            })
            .collect(),
        pads: Vec::new(),
        tracks: Vec::new(),
        max_expansions: 100_000,
        limits: Limits::default(),
        holes: Vec::new(),
        board_outline: None,
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
        vias: Vec::new(),
        rule_areas: Vec::new(),
        board_design_settings: None,
        project_rules: None,
        custom_rules: None,
        effective_rules: None,
    }
}

fn uuid(n: usize) -> String {
    format!("10000000-0000-4000-8000-{n:012x}")
}

// This oracle enumerates every authoritative obstacle. It deliberately has no
// dependency on copper_bounds, candidate selection or spatial metadata.
fn linear_legal(
    input: &Input,
    tracks: impl Iterator<Item = Track>,
    net: u32,
    a: Point,
    b: Point,
) -> bool {
    let r = input.rules;
    let margin = i128::from(r.width_nm) + 2 * i128::from(r.edge_clearance_nm);
    for p in [a, b] {
        for k in 0..2 {
            if 2 * i128::from(p[k]) < 2 * i128::from(input.bounds.min[k]) + margin
                || 2 * i128::from(p[k]) > 2 * i128::from(input.bounds.max[k]) - margin
            {
                return false;
            }
        }
    }
    for pad in &input.pads {
        if (pad.net == 0 || pad.net != net)
            && geometry::track_near_pad(a, b, r.width_nm, r.clearance_nm, pad, false)
        {
            return false;
        }
    }
    for track in tracks {
        if (track.net == 0 || track.net != net)
            && geometry::segments_near(
                a,
                b,
                track.start,
                track.end,
                i128::from(r.width_nm)
                    + i128::from(track.width_nm)
                    + 2 * i128::from(r.clearance_nm),
                false,
            )
        {
            return false;
        }
    }
    true
}

fn compare(input: &Input, engine: &Engine, net: u32, a: Point, b: Point) {
    let expected = linear_legal(input, engine.copper().tracks().cloned(), net, a, b);
    assert_eq!(
        engine.legal_edge(net, a, b).unwrap(),
        expected,
        "net={net}, {a:?}->{b:?}"
    );
}

fn random(seed: &mut u64) -> u64 {
    *seed = seed
        .wrapping_mul(6364136223846793005)
        .wrapping_add(1442695040888963407);
    *seed
}

fn coordinate(seed: &mut u64) -> i64 {
    (random(seed) % 9001) as i64 - 4500
}

#[test]
fn indexed_legality_matches_linear_scan_in_deterministic_mixed_worlds() {
    for initial in 0..8 {
        let mut seed = initial + 11;
        let mut input = fixture();
        input.rules.width_nm = 1 + (initial as i64 % 5) * 2;
        input.rules.clearance_nm = initial as i64;
        for n in 0..96 {
            let net = (random(&mut seed) % 4) as u32;
            let at = [coordinate(&mut seed), coordinate(&mut seed)];
            let size = 2 + (random(&mut seed) % 30) as i64;
            let shape = match n % 5 {
                0 => Shape::Circle { radius_nm: size },
                1 => Shape::Rect {
                    half_size_nm: [size, 1 + size / 2],
                },
                2 => Shape::RoundRect {
                    half_size_nm: [size, 1 + size / 3],
                    radius_nm: (1 + size / 4).min(1 + size / 3),
                },
                3 => {
                    let minor = 1 + size / 2;
                    Shape::RoundRect {
                        half_size_nm: [size, minor],
                        radius_nm: minor.min(size),
                    }
                }
                _ => {
                    let minor = 1 + size / 2;
                    Shape::RoundRect {
                        half_size_nm: [minor, size],
                        radius_nm: minor.min(size),
                    }
                }
            };
            input.pads.push(Pad {
                id: uuid(n + 1),
                net,
                at,
                shape,
                drill: None,
                layers: Vec::new(),
                local_clearance_nm: None,
            });
        }
        for n in 0..128 {
            let start = [coordinate(&mut seed), coordinate(&mut seed)];
            let mut end = [coordinate(&mut seed), coordinate(&mut seed)];
            if start == end {
                end[0] += 1;
            }
            input.tracks.push(Track {
                id: uuid(1000 + n),
                net: (random(&mut seed) % 4) as u32,
                start,
                end,
                width_nm: 1 + (random(&mut seed) % 15) as i64,
                locked: n % 2 == 0,
                layer: "F.Cu".into(),
            });
        }
        let engine = Engine::new(input.clone()).unwrap();
        for n in 0..1000 {
            let a = [coordinate(&mut seed), coordinate(&mut seed)];
            let b = if n % 4 == 0 {
                [coordinate(&mut seed), coordinate(&mut seed)]
            } else {
                [
                    a[0] + (random(&mut seed) % 101) as i64 - 50,
                    a[1] + (random(&mut seed) % 101) as i64 - 50,
                ]
            };
            compare(&input, &engine, n % 4, a, b);
        }
        assert!(engine.copper().index_consistent());
    }
}

#[test]
fn boundary_equality_odd_widths_diagonals_roundrect_and_unconnected_copper_are_not_lost() {
    let mut input = fixture();
    input.rules.width_nm = 3;
    input.rules.clearance_nm = 2;
    input.pads = vec![
        Pad {
            id: uuid(1),
            net: 0,
            at: [-100, -100],
            shape: Shape::Rect {
                half_size_nm: [3, 5],
            },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
        },
        Pad {
            id: uuid(2),
            net: 2,
            at: [100, 100],
            shape: Shape::Circle { radius_nm: 7 },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
        },
        Pad {
            id: uuid(5),
            net: 0,
            at: [300, -100],
            shape: Shape::RoundRect {
                half_size_nm: [12, 6],
                radius_nm: 3,
            },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
        },
        Pad {
            id: uuid(6),
            net: 2,
            at: [-300, 100],
            shape: Shape::RoundRect {
                half_size_nm: [5, 12],
                radius_nm: 5,
            },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
        },
    ];
    input.tracks = vec![
        Track {
            id: uuid(3),
            net: 0,
            start: [-50, 50],
            end: [50, -50],
            width_nm: 5,
            locked: true,
            layer: "F.Cu".into(),
        },
        Track {
            id: uuid(4),
            net: 3,
            start: [-200, -200],
            end: [200, -200],
            width_nm: 1,
            locked: false,
            layer: "F.Cu".into(),
        },
    ];
    let engine = Engine::new(input.clone()).unwrap();
    for y in -210..=-190 {
        for net in 0..=3 {
            compare(&input, &engine, net, [-190, y], [190, y]);
        }
    }
    for center in [[-100, -100], [100, 100], [300, -100], [-300, 100], [0, 0]] {
        for dx in -18..=18 {
            for dy in -18..=18 {
                let a = [center[0] + dx, center[1] + dy];
                compare(&input, &engine, 1, a, [a[0] + 1, a[1] - 1]);
            }
        }
    }
    // Asymmetric RoundRect [12, 6], r=3 at [300, -100]: sweep along both X and Y boundaries.
    for x in [284, 285, 315, 316] {
        compare(&input, &engine, 1, [x, -110], [x, -90]);
    }
    for y in [-110, -109, -91, -90] {
        compare(&input, &engine, 1, [290, y], [310, y]);
    }
    // Vertical oval [5, 12], r=5 at [-300, 100]: sweep along X and Y boundaries + corner arc.
    for x in [-309, -308, -292, -291] {
        compare(&input, &engine, 1, [x, 90], [x, 110]);
    }
    for y in [84, 85, 115, 116] {
        compare(&input, &engine, 1, [-304, y], [-296, y]);
    }
    // Check independent Body oracle parity for RoundRect & oval corner regions.
    assert!(!Body::pad(&input.pads[2]).contacts(Body::pad(&input.pads[3])));
    for pad in &input.pads[2..] {
        for dx in -17..=17 {
            for dy in -17..=17 {
                let a = [pad.at[0] + dx, pad.at[1] + dy];
                let b = [a[0] + 2, a[1]];
                let t = Track {
                    id: uuid(99),
                    net: 1,
                    start: a,
                    end: b,
                    width_nm: input.rules.width_nm,
                    locked: false,
                    layer: "F.Cu".into(),
                };
                let expected = !Body::pad(pad).violates(Body::track(&t), input.rules.clearance_nm);
                assert_eq!(engine.legal_edge(1, a, b).unwrap(), expected);
            }
        }
    }
    // Doubled radii: route3 + obstacle1 + 2*clearance2 = 8, so gap 4 is equality.
    assert!(engine.legal_edge(1, [-190, -196], [190, -196]).unwrap());
    assert!(!engine.legal_edge(1, [-190, -197], [190, -197]).unwrap());
    assert!(!engine.legal_edge(0, [-1, 0], [1, 0]).unwrap());
    for a in [
        [i64::MIN, 0],
        [i64::MAX, 0],
        [-4998, 0],
        [-4999, 0],
        [4998, 0],
        [4999, 0],
    ] {
        compare(&input, &engine, 1, a, a);
    }
}

fn routing_fixture() -> Input {
    let mut input = fixture();
    input.bounds = Bounds {
        min: [-100, -100],
        max: [100, 100],
    };
    input.rules = Rules {
        width_nm: 3,
        clearance_nm: 2,
        grid_nm: 10,
        edge_clearance_nm: 0,
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    input.pads = vec![
        Pad {
            id: uuid(1),
            net: 1,
            at: [-50, 0],
            shape: Shape::Circle { radius_nm: 3 },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
        },
        Pad {
            id: uuid(2),
            net: 1,
            at: [50, 0],
            shape: Shape::Circle { radius_nm: 3 },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
        },
    ];
    input
}

fn bytes(engine: &Engine) -> Vec<u8> {
    assert!(engine.copper().index_consistent());
    serde_json::to_vec(&engine.copper().tracks().collect::<Vec<_>>()).unwrap()
}

#[test]
fn committed_generated_copper_blocks_actual_legality_and_reroute_preserves_it() {
    let input = routing_fixture();
    let mut engine = Engine::new(input.clone()).unwrap();
    assert!(engine.legal_edge(2, [0, -10], [0, 10]).unwrap());
    engine.route(1, 1000).unwrap();
    assert!(!engine.legal_edge(2, [0, -10], [0, 10]).unwrap());
    assert!(engine.legal_edge(1, [0, -10], [0, 10]).unwrap());
    engine.reroute(1, 1000).unwrap();
    assert!(!engine.legal_edge(2, [0, -10], [0, 10]).unwrap());
    compare(&input, &engine, 0, [0, -10], [0, 10]);
    assert!(engine.copper().index_consistent());
}

#[test]
fn inner_commit_outer_rollback_and_unwind_restore_index_and_allocator() {
    let input = routing_fixture();
    let mut engine = Engine::new(input.clone()).unwrap();
    let before = bytes(&engine);
    let result = engine.transaction(|outer| {
        outer.route(1, 1000).unwrap();
        outer
            .transaction(|inner| {
                inner.reroute(1, 1000).unwrap();
                Ok::<_, ()>(())
            })
            .unwrap();
        assert!(outer.copper().index_consistent());
        Err::<(), _>("reject")
    });
    assert_eq!(result, Err(TransactionError::Operation("reject")));
    assert_eq!(bytes(&engine), before);
    assert!(engine.legal_edge(2, [0, -10], [0, 10]).unwrap());
    let panic = catch_unwind(AssertUnwindSafe(|| {
        let _: Result<(), TransactionError<()>> = engine.transaction(|outer| {
            outer.route(1, 1000).unwrap();
            outer
                .transaction(|inner| {
                    inner.reroute(1, 1000).unwrap();
                    Ok::<_, ()>(())
                })
                .unwrap();
            panic!("unwind after index edits")
        });
    }));
    assert!(panic.is_err());
    assert_eq!(bytes(&engine), before);
    assert!(engine.legal_edge(2, [0, -10], [0, 10]).unwrap());
    engine.route(1, 1000).unwrap();
    let mut control = Engine::new(input).unwrap();
    control.route(1, 1000).unwrap();
    assert_eq!(bytes(&engine), bytes(&control));
    assert!(!engine.legal_edge(2, [0, -10], [0, 10]).unwrap());
}

#[test]
fn exhaustion_after_prior_mutation_restores_generated_obstacle_and_allocator() {
    let mut input = routing_fixture();
    input.limits.max_journal_entries = 1;
    let mut engine = Engine::new(input.clone()).unwrap();
    engine.route(1, 1000).unwrap();
    let before = bytes(&engine);
    assert_eq!(
        engine.reroute(1, 1000).unwrap_err().status,
        Status::JournalExhausted
    );
    assert_eq!(bytes(&engine), before);
    assert!(!engine.legal_edge(2, [0, -10], [0, 10]).unwrap());
    compare(&input, &engine, 2, [0, -10], [0, 10]);
}

#[test]
fn linear_oracle_covers_multiple_generated_nets_after_edits_and_rollback() {
    let mut input = routing_fixture();
    input.pads.clear();
    for net in 1..=3 {
        for (ordinal, x) in [-50, 50].into_iter().enumerate() {
            input.pads.push(Pad {
                id: uuid(net as usize * 2 + ordinal),
                net,
                at: [x, (i64::from(net) - 2) * 30],
                shape: Shape::Circle { radius_nm: 3 },
                drill: None,
                layers: Vec::new(),
                local_clearance_nm: None,
            });
        }
    }
    let mut engine = Engine::new(input.clone()).unwrap();
    engine.route_all().unwrap();
    assert_eq!(engine.copper().generated().count(), 3);
    let assert_world = |engine: &Engine| {
        assert!(engine.copper().index_consistent());
        for x in (-80..=80).step_by(10) {
            for y in (-80..=80).step_by(10) {
                for net in 0..=3 {
                    compare(&input, engine, net, [x, y], [x + 7, y + 9]);
                }
            }
        }
    };
    assert_world(&engine);
    engine.reroute(2, 1000).unwrap();
    assert_world(&engine);
    let before = bytes(&engine);
    let result = engine.transaction(|outer| {
        outer.reroute(1, 1000).unwrap();
        outer
            .transaction(|inner| {
                inner.reroute(3, 1000).unwrap();
                Ok::<_, ()>(())
            })
            .unwrap();
        Err::<(), _>("restore generated obstacle world")
    });
    assert_eq!(
        result,
        Err(TransactionError::Operation(
            "restore generated obstacle world"
        ))
    );
    assert_eq!(bytes(&engine), before);
    assert_world(&engine);
}

#[test]
fn roundrect_and_oval_pads_route_and_roll_back_deterministically() {
    let mut input = routing_fixture();
    input.bounds = Bounds {
        min: [-120, -120],
        max: [120, 120],
    };
    input.pads = vec![
        Pad {
            id: uuid(1),
            net: 1,
            at: [-60, -20],
            shape: Shape::RoundRect {
                half_size_nm: [8, 4],
                radius_nm: 4,
            },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
        },
        Pad {
            id: uuid(2),
            net: 1,
            at: [60, -20],
            shape: Shape::RoundRect {
                half_size_nm: [6, 4],
                radius_nm: 2,
            },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
        },
        Pad {
            id: uuid(3),
            net: 2,
            at: [-60, 20],
            shape: Shape::RoundRect {
                half_size_nm: [4, 8],
                radius_nm: 4,
            },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
        },
        Pad {
            id: uuid(4),
            net: 2,
            at: [60, 20],
            shape: Shape::RoundRect {
                half_size_nm: [5, 5],
                radius_nm: 3,
            },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
        },
        Pad {
            id: uuid(5),
            net: 0,
            at: [0, 0],
            shape: Shape::RoundRect {
                half_size_nm: [12, 6],
                radius_nm: 6,
            },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
        },
    ];
    let mut fresh = Engine::new(input.clone()).unwrap();
    fresh.route_all().unwrap();
    let mut history = Engine::new(input.clone()).unwrap();
    history.route_all().unwrap();
    let _ = history.transaction(|outer| {
        outer.reroute(1, 1000).unwrap();
        outer
            .transaction(|inner| {
                inner.reroute(2, 1000).unwrap();
                Ok::<_, ()>(())
            })
            .unwrap();
        Err::<(), _>(())
    });
    assert_eq!(bytes(&fresh), bytes(&history));
    for y in (-40..=40).step_by(10) {
        assert_eq!(
            fresh.legal_edge(1, [-40, y], [40, y]).unwrap(),
            history.legal_edge(1, [-40, y], [40, y]).unwrap()
        );
        assert_eq!(fresh.work_used(), history.work_used());
        compare(&input, &history, 1, [-40, y], [40, y]);
    }
}

#[test]
fn post_rollback_and_unwind_yield_zero_query_and_reroute_work_diffs() {
    let mut seed = 0x5eed_0401u64;
    let mut below = |n: i64| -> i64 { (random(&mut seed) >> 33) as i64 % n };
    let mut worlds = 0usize;
    let mut query_cases = 0u64;
    let mut query_differs = 0u64;
    let mut op_cases = 0u64;
    let mut op_differs = 0u64;

    for _case in 0..25 {
        let mut input = Input {
            version: 1,
            source_id: "e".repeat(64),
            layer: "F.Cu".into(),
            layers: Vec::new(),
            bounds: Bounds {
                min: [-200, -200],
                max: [200, 200],
            },
            rules: Rules {
                width_nm: 2,
                clearance_nm: 1,
                grid_nm: 10,
                edge_clearance_nm: 0,
                hole_clearance_nm: 0,
                same_net_hole_clearance_nm: 0,
                solder_mask_expansion_nm: 0,
                solder_mask_min_width_nm: 0,
                solder_mask_to_copper_clearance_nm: 0,
            },
            nets: (0..=4)
                .map(|id| Net {
                    id,
                    name: if id == 0 {
                        String::new()
                    } else {
                        format!("N{id}")
                    },
                })
                .collect(),
            pads: Vec::new(),
            tracks: Vec::new(),
            max_expansions: 50_000,
            limits: Limits::default(),
            holes: Vec::new(),
            board_outline: None,
            graphics: Vec::new(),
            mask_apertures: Vec::new(),
            vias: Vec::new(),
            rule_areas: Vec::new(),
            board_design_settings: None,
            project_rules: None,
            custom_rules: None,
            effective_rules: None,
        };
        let mut used = BTreeSet::new();
        let mut n = 0;
        for net in 1..=4u32 {
            let count = 3 + below(3);
            for _ in 0..count {
                let at = loop {
                    let c = [-15 + below(31), -15 + below(31)];
                    if used.insert(c) {
                        break [c[0] * 10, c[1] * 10];
                    }
                };
                n += 1;
                let shape = if n % 3 == 0 {
                    Shape::RoundRect {
                        half_size_nm: [3, 2],
                        radius_nm: 2,
                    }
                } else {
                    Shape::Circle { radius_nm: 2 }
                };
                input.pads.push(Pad {
                    id: format!("40000000-0000-4000-8000-{n:012x}"),
                    net,
                    at,
                    shape,
                    drill: None,
                    layers: Vec::new(),
                    local_clearance_nm: None,
                });
            }
        }

        let mut fresh = Engine::new(input.clone()).unwrap();
        if fresh.route_all().is_err() {
            continue;
        }
        let mut history = Engine::new(input.clone()).unwrap();
        history.route_all().unwrap();
        assert_eq!(bytes(&fresh), bytes(&history));

        let mut rolled_back = 0;
        for net in 1..=4 {
            if history.reroute(net, 1).is_err() {
                rolled_back += 1;
            }
        }
        if rolled_back == 0 {
            continue;
        }
        let _ = catch_unwind(AssertUnwindSafe(|| {
            let _: Result<(), TransactionError<()>> = history.transaction(|tx| {
                let _ = tx.reroute(1, 50_000);
                std::panic::panic_any(7u32)
            });
        }));
        assert_eq!(bytes(&fresh), bytes(&history));
        worlds += 1;

        for x in (-190..=190).step_by(20) {
            for y in (-190..=190).step_by(20) {
                let (a, b) = ([x, y], [x + 10, y]);
                let net = (query_cases % 5) as u32;
                let left = fresh.legal_edge(net, a, b).unwrap();
                let left_work = fresh.work_used();
                let right = history.legal_edge(net, a, b).unwrap();
                let right_work = history.work_used();
                assert_eq!(left, right);
                query_cases += 1;
                if left_work != right_work {
                    query_differs += 1;
                }
            }
        }

        for net in 1..=4 {
            let mut f = Engine::new(input.clone()).unwrap();
            f.route_all().unwrap();
            let mut h = Engine::new(input.clone()).unwrap();
            h.route_all().unwrap();
            for other in 1..=4 {
                let _ = h.reroute(other, 1);
            }
            assert_eq!(bytes(&f), bytes(&h));
            let fr = f.reroute(net, 50_000);
            let fw = f.work_used();
            let hr = h.reroute(net, 50_000);
            let hw = h.work_used();
            op_cases += 1;
            assert_eq!(fr.is_ok(), hr.is_ok());
            if fw != hw {
                op_differs += 1;
            }
        }
    }

    assert!(worlds >= 10, "expected >= 10 routed worlds, got {worlds}");
    assert!(
        query_cases >= 5200,
        "expected >= 5200 queries, got {query_cases}"
    );
    assert!(op_cases >= 52, "expected >= 52 ops, got {op_cases}");
    assert_eq!(query_differs, 0, "post-rollback query work_used differed");
    assert_eq!(op_differs, 0, "post-rollback reroute work_used differed");
}

#[test]
fn generated_samenet_rollback_determinism() {
    let mut input = routing_fixture();
    input.bounds = Bounds {
        min: [-120, -120],
        max: [120, 120],
    };
    input.pads = vec![
        Pad {
            id: uuid(1),
            net: 1,
            at: [-50, -20],
            shape: Shape::Circle { radius_nm: 3 },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
        },
        Pad {
            id: uuid(2),
            net: 1,
            at: [50, -20],
            shape: Shape::Circle { radius_nm: 3 },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
        },
        Pad {
            id: uuid(3),
            net: 2,
            at: [-50, 20],
            shape: Shape::Circle { radius_nm: 3 },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
        },
        Pad {
            id: uuid(4),
            net: 2,
            at: [50, 20],
            shape: Shape::Circle { radius_nm: 3 },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
        },
    ];
    let mut fresh = Engine::new(input.clone()).unwrap();
    fresh.route_all().unwrap();
    let mut history = Engine::new(input).unwrap();
    history.route_all().unwrap();
    assert_eq!(bytes(&fresh), bytes(&history));

    let _ = history.transaction(|outer| {
        outer.reroute(1, 1000).unwrap();
        outer
            .transaction(|inner| {
                inner.reroute(2, 1000).unwrap();
                Ok::<_, ()>(())
            })
            .unwrap();
        Err::<(), _>(())
    });
    let _ = catch_unwind(AssertUnwindSafe(|| {
        let _: Result<(), TransactionError<()>> = history.transaction(|tx| {
            let _ = tx.reroute(1, 50_000);
            std::panic::panic_any(11u32)
        });
    }));
    assert_eq!(bytes(&fresh), bytes(&history));
    for net in [1u32, 2u32] {
        for y in (-40..=40).step_by(10) {
            let f_ok = fresh.legal_edge(net, [-40, y], [40, y]).unwrap();
            let fw = fresh.work_used();
            let h_ok = history.legal_edge(net, [-40, y], [40, y]).unwrap();
            let hw = history.work_used();
            assert_eq!(f_ok, h_ok);
            assert_eq!(fw, hw);
            assert_eq!(f_ok, (net == 1 && y != 20) || (net == 2 && y != -20));
        }
    }
}
