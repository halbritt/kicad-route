use kicad_route_engine::{geometry::*, model::*, run, run_json, search, Engine};
use serde_json::{json, Value};
use std::{
    cmp::Reverse,
    collections::{BTreeMap, BTreeSet, BinaryHeap},
};

fn id(n: u64) -> String {
    format!("10000000-0000-4000-8000-{n:012x}")
}
fn pad(n: u64, net: u32, at: Point) -> Pad {
    Pad {
        id: id(n),
        net,
        at,
        shape: Shape::Circle { radius_nm: 1 },
        drill: None,
        layers: Vec::new(),
        local_clearance_nm: None,
        zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
        footprint_ref: None,
        pad_number: None,
        fromto_id: None,
    }
}
fn base() -> Input {
    Input {
        version: 1,
        source_id: "a".repeat(64),
        layer: "F.Cu".into(),
        bounds: Bounds {
            min: [0, 0],
            max: [60, 60],
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
        nets: vec![Net {
            id: 1,
            name: "A".into(),
        }],
        pads: vec![pad(1, 1, [10, 10]), pad(2, 1, [50, 10])],
        tracks: vec![],
        max_expansions: 10000,
        limits: Limits::default(),
        holes: Vec::new(),
        board_outline: None,
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
        layers: Vec::new(),
        vias: Vec::new(),
        rule_areas: Vec::new(),
        board_design_settings: None,
        project_rules: None,
        custom_rules: None,
        effective_rules: None,
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
    }
}
fn wire(n: u64, net: u32, a: Point, b: Point) -> Track {
    Track {
        id: id(n),
        net,
        start: a,
        end: b,
        width_nm: 2,
        locked: true,
        layer: "F.Cu".into(),
    }
}
fn output_bytes(input: Input) -> Vec<u8> {
    serde_json::to_vec(&run(input)).unwrap()
}
#[test]
fn off_grid_smd_escape_uses_the_other_pad_phase_after_exhaustion() {
    let mut input = base();
    input.bounds.max = [6_000_000, 4_000_000];
    input.rules.grid_nm = 250_000;
    input.rules.width_nm = 100_000;
    input.rules.clearance_nm = 100_000;
    input.rules.edge_clearance_nm = 100_000;
    input.pads[0].at = [1_000_000, 2_000_000];
    input.pads[0].shape = Shape::Rect {
        half_size_nm: [250_000, 650_000],
    };
    input.pads[1].at = [5_000_000, 2_125_000];
    input.pads[1].shape = Shape::Circle { radius_nm: 150_000 };
    for (index, (lo, hi)) in [(0, 2_025_000), (2_225_000, 4_000_000)]
        .into_iter()
        .enumerate()
    {
        input.rule_areas.push(RuleArea {
            id: id(3 + index as u64),
            name: None,
            layers: vec!["F.Cu".into()],
            outer: vec![
                [3_000_000, lo],
                [3_500_000, lo],
                [3_500_000, hi],
                [3_000_000, hi],
            ],
            holes: vec![],
            extra_polygons: vec![],
            no_tracks: true,
            no_vias: true,
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        });
    }
    let open = run(input.clone());
    assert_eq!(open.status, Status::Complete, "{:?}", open.reason);
    assert_eq!(open.added_tracks.len(), 1);
    let track = &open.added_tracks[0];
    assert_eq!(track.start[1], 2_125_000);
    assert_eq!(track.end[1], 2_125_000);
    assert!(input
        .pads
        .iter()
        .all(|p| kicad_route_engine::geometry::track_connects_pad(track, p)));
    let mut reordered = input.clone();
    reordered.pads.reverse();
    let direct = run(reordered);
    assert_eq!(direct.status, Status::Complete);
    assert_eq!(direct.added_tracks, open.added_tracks);
    assert!(direct.expanded_states < open.expanded_states);

    let mut limited = input.clone();
    limited.max_expansions = open.expanded_states - 1;
    let stopped = run(limited);
    assert_eq!(stopped.status, Status::BudgetExhausted);
    assert!(stopped.added_tracks.is_empty());
    for point in &mut input.rule_areas[0].outer[2..] {
        point[1] = 2_225_000;
    }
    let sealed = run(input);
    assert_eq!(sealed.status, Status::Unroutable);
    assert!(sealed.added_tracks.is_empty());
}

#[test]
fn bidirectional_search_finishes_when_the_target_is_enclosed() {
    let mut input = base();
    input.bounds.max = [20_000_000, 20_000_000];
    input.rules.grid_nm = 200_000;
    input.rules.width_nm = 120_000;
    input.rules.clearance_nm = 100_000;
    input.layers = vec!["F.Cu".into(), "B.Cu".into()];
    input.pads[0].at = [3_000_000, 10_000_000];
    input.pads[1].at = [10_000_000, 10_000_000];
    for pad in &mut input.pads {
        pad.shape = Shape::Circle { radius_nm: 200_000 };
    }
    input.max_expansions = 1500;
    let obstacle = RuleArea {
        id: id(10),
        name: None,
        layers: input.layers.clone(),
        outer: vec![
            [9_000_000, 9_000_000],
            [11_000_000, 9_000_000],
            [11_000_000, 11_000_000],
            [9_000_000, 11_000_000],
        ],
        holes: vec![vec![
            [9_600_000, 9_600_000],
            [9_600_000, 10_400_000],
            [10_400_000, 10_400_000],
            [10_400_000, 9_600_000],
        ]],
        extra_polygons: Vec::new(),
        no_tracks: true,
        no_vias: true,
        no_pads: false,
        no_footprints: false,
        no_zone_fills: false,
    };
    let open = run(input.clone());
    assert_eq!(open.status, Status::Complete, "{:?}", open.reason);
    input.rule_areas.push(obstacle);
    let blocked = run(input);
    assert_eq!(blocked.status, Status::Unroutable, "{:?}", blocked.reason);
    assert!(blocked.added_tracks.is_empty() && blocked.added_vias.is_empty());
}

#[test]
fn continuous_geometry_crossings_midpoints_and_tangency() {
    assert!(segments_near(
        [0, 0],
        [100, 100],
        [0, 100],
        [100, 0],
        2,
        false
    ));
    // Endpoints are far away, but the swept segment hits the middle pad.
    assert!(point_near_segment([50, 0], [0, 0], [100, 0], 2, false));
    assert!(!point_near_segment([50, 3], [0, 0], [100, 0], 6, false));
    assert!(point_near_segment([50, 3], [0, 0], [100, 0], 6, true));
    assert!(point_near_segment([50, 3], [0, 0], [100, 0], 7, false));
    assert!(!point_near_segment([10, 0], [0, 0], [2, 0], 10, true));
    assert!(segment_near_rect(
        [-10, 0],
        [10, 0],
        [-1, -1],
        [1, 1],
        1,
        false
    ));
    // Rounded rectangle corner distance is Euclidean, not a square inflation.
    assert!(!segment_near_rect(
        [2, 2],
        [3, 3],
        [-1, -1],
        [1, 1],
        2,
        false
    ));
    assert!(segment_near_rect(
        [2, 2],
        [3, 3],
        [-1, -1],
        [1, 1],
        3,
        false
    ));
    assert!(!segments_near([0, 0], [10, 0], [11, 0], [20, 0], 2, false));
    assert!(segments_near([0, 0], [10, 0], [11, 0], [20, 0], 2, true));
}
#[test]
fn bounded_extreme_geometry_and_odd_width_edge_are_exact() {
    assert!(segments_near(
        [-100000000, -100000000],
        [100000000, 100000000],
        [-100000000, 100000000],
        [100000000, -100000000],
        400000000,
        false
    ));
    assert!(point_near_segment(
        [100000000, 100000000],
        [-100000000, -100000000],
        [100000000, -100000000],
        500000000,
        false
    ));
    let mut input = base();
    input.rules.width_nm = 3;
    input.rules.edge_clearance_nm = 1;
    let e = Engine::new(input).unwrap();
    assert!(!e.legal_edge(1, [2, 10], [2, 20]).unwrap());
    assert!(e.legal_edge(1, [3, 10], [3, 20]).unwrap());
    assert!(!e.legal_edge(1, [58, 10], [58, 20]).unwrap());
}
#[test]
fn same_net_contact_allowed_foreign_swept_copper_blocked() {
    let mut i = base();
    i.nets.push(Net {
        id: 2,
        name: "B".into(),
    });
    i.tracks.push(wire(3, 2, [30, 0], [30, 60]));
    let e = Engine::new(i).unwrap();
    assert!(!e.legal_edge(1, [10, 20], [50, 20]).unwrap());
    assert!(e.legal_edge(2, [10, 20], [50, 20]).unwrap());
}
#[test]
fn existing_copper_connectivity_is_reused_without_duplication() {
    let mut i = base();
    i.tracks.push(wire(3, 1, [10, 10], [30, 10]));
    i.tracks.push(wire(4, 1, [30, 10], [50, 10]));
    let mut e = Engine::new(i).unwrap();
    let before = e.copper().input().to_vec();
    assert_eq!(e.route_all().unwrap(), 0);
    assert_eq!(e.copper().generated().count(), 0);
    assert_eq!(e.copper().input(), before);
    assert_eq!(e.reroute(1, 1).unwrap(), 0);
    assert_eq!(e.copper().input(), before);
}
#[test]
fn repeated_output_is_deterministic_and_all_generated_edges_legal() {
    let input = base();
    let first = output_bytes(input.clone());
    assert_eq!(first, output_bytes(input.clone()));
    let output = run(input.clone());
    assert_eq!(output.status, Status::Complete);
    assert!(!output.added_tracks.is_empty());
    let e = Engine::new(input).unwrap();
    for t in output.added_tracks {
        assert!(canonical_uuid(&t.id));
        assert!(!t.locked);
        assert!(e.legal_edge(t.net, t.start, t.end).unwrap());
    }
}
#[test]
fn source_derived_id_collision_is_skipped() {
    let mut i = base();
    i.pads[0].id = "aaaaaaaa-aaaa-8aaa-8aaa-000000000001".into();
    let ids: BTreeSet<_> = i.pads.iter().map(|p| p.id.clone()).collect();
    let o = run(i);
    assert_eq!(o.status, Status::Complete);
    assert!(o.added_tracks.iter().all(|t| !ids.contains(&t.id)));
}
#[test]
fn two_competing_nets_detour_without_crossing() {
    let mut i = base();
    i.bounds.max = [80, 80];
    i.pads = vec![
        pad(1, 1, [10, 30]),
        pad(2, 1, [50, 30]),
        pad(3, 2, [30, 10]),
        pad(4, 2, [30, 50]),
    ];
    i.nets.push(Net {
        id: 2,
        name: "B".into(),
    });
    let out = run(i);
    assert_eq!(out.status, Status::Complete);
    let a: Vec<_> = out.added_tracks.iter().filter(|t| t.net == 1).collect();
    let b: Vec<_> = out.added_tracks.iter().filter(|t| t.net == 2).collect();
    assert!(!a.is_empty() && !b.is_empty());
    assert!(b.iter().any(|t| t.start[0] >= 60 || t.end[0] >= 60));
    for t in &a {
        for u in &b {
            assert!(!segments_near(t.start, t.end, u.start, u.end, 6, false));
        }
    }
}
#[test]
fn last_net_failure_rolls_back_first_net_and_index() {
    let mut i = base();
    i.nets.extend([
        Net {
            id: 2,
            name: "B".into(),
        },
        Net {
            id: 3,
            name: "WALL".into(),
        },
    ]);
    i.pads.extend([pad(3, 2, [30, 20]), pad(4, 2, [30, 50])]);
    i.tracks.push(wire(5, 3, [0, 30], [60, 30]));
    let mut e = Engine::new(i.clone()).unwrap();
    let original = e.copper().input().to_vec();
    let err = e.route_all().unwrap_err();
    assert_eq!(err.status, Status::Unroutable);
    assert!(err.expanded_states > 5);
    assert_eq!(e.copper().generated().count(), 0);
    assert!(e.copper().index_consistent());
    assert_eq!(e.copper().input(), original);
    let out = run(i);
    assert_eq!(out.status, Status::Unroutable);
    assert!(out.added_tracks.is_empty());
    assert!(out.removed_track_ids.is_empty());
}
#[test]
fn total_budget_is_not_reset_for_second_net() {
    let mut i = base();
    i.nets.push(Net {
        id: 2,
        name: "B".into(),
    });
    i.pads.extend([pad(3, 2, [10, 40]), pad(4, 2, [50, 40])]);
    i.max_expansions = 5;
    let o = run(i);
    assert_eq!(o.status, Status::BudgetExhausted);
    assert_eq!(o.expanded_states, 5);
    assert!(o.added_tracks.is_empty());
}

#[test]
fn straight_route_settles_each_required_grid_step_and_avoids_a_blocker() {
    let mut input = base();
    input.bounds.max = [60_000_000, 40_000_000];
    input.rules.width_nm = 100_000;
    input.rules.clearance_nm = 150_000;
    input.rules.grid_nm = 100_000;
    input.pads[0].at = [4_000_000, 20_000_000];
    input.pads[1].at = [54_000_000, 20_000_000];
    for pad in &mut input.pads {
        pad.shape = Shape::Circle { radius_nm: 250_000 };
    }
    // The cap of the 497th straight step is tangent to the pad. A route
    // needs a 498th step to overlap copper, and its seed also counts.
    input.max_expansions = 499;
    let clear = run(input.clone());
    assert_eq!(clear.status, Status::Complete);
    assert_eq!(clear.expanded_states, 499);
    assert_eq!(clear.added_tracks.len(), 1);
    assert_eq!(clear.added_tracks[0].start, input.pads[0].at);
    assert_eq!(clear.added_tracks[0].end, [53_800_000, 20_000_000]);
    input.max_expansions -= 1;
    let limited = run(input.clone());
    assert_eq!(limited.status, Status::BudgetExhausted);
    assert!(limited.added_tracks.is_empty());

    input.max_expansions = 100_000;
    input.nets.push(Net {
        id: 2,
        name: "BLOCKER".into(),
    });
    let mut blocker = wire(3, 2, [29_000_000, 17_000_000], [29_000_000, 23_000_000]);
    blocker.width_nm = 500_000;
    input.tracks.push(blocker.clone());
    let detour = run(input);
    assert_eq!(detour.status, Status::Complete);
    assert!(detour.added_tracks.len() > 1);
    for track in &detour.added_tracks {
        let clearance_diameter = i128::from(track.width_nm + blocker.width_nm + 2 * 150_000);
        assert!(!segments_near(
            track.start,
            track.end,
            blocker.start,
            blocker.end,
            clearance_diameter,
            false
        ));
    }
}

#[test]
fn failed_generated_reroute_restores_ids_geometry_index_and_allocator() {
    let mut input = base();
    input.nets.push(Net {
        id: 2,
        name: "INPUT".into(),
    });
    input.tracks.push(wire(3, 2, [0, 0], [10, 0]));
    let mut e = Engine::new(input.clone()).unwrap();
    e.route_all().unwrap();
    let old: Vec<_> = e.copper().tracks().cloned().collect();
    let err = e.reroute(1, 1).unwrap_err();
    assert_eq!(err.status, Status::BudgetExhausted);
    assert_eq!(old, e.copper().tracks().cloned().collect::<Vec<_>>());
    assert!(e.copper().index_consistent());
    e.reroute(1, 10000).unwrap();
    let mut control = Engine::new(input).unwrap();
    control.route_all().unwrap();
    control.reroute(1, 10000).unwrap();
    assert_eq!(
        e.copper().tracks().cloned().collect::<Vec<_>>(),
        control.copper().tracks().cloned().collect::<Vec<_>>()
    );
}
// Independent position-only Dijkstra on this test's lattice. Obstacles are
// tiny circles centered on lattice nodes: an edge is blocked iff an endpoint
// is blocked. This oracle does NOT call production geometry or search code.
fn oracle(start: Point, goal: Point, blocked: &BTreeSet<Point>) -> Option<u64> {
    if blocked.contains(&start) || blocked.contains(&goal) {
        return None;
    }
    let mut queue = BinaryHeap::from([Reverse((0, start))]);
    let mut dist = BTreeMap::from([(start, 0)]);
    while let Some(Reverse((d, p))) = queue.pop() {
        if dist[&p] != d {
            continue;
        }
        if p == goal {
            return Some(d);
        }
        for delta in [[10, 0], [-10, 0], [0, 10], [0, -10]] {
            let q = [p[0] + delta[0], p[1] + delta[1]];
            if q.iter().any(|v| *v < 10 || *v > 50) || blocked.contains(&q) {
                continue;
            }
            if dist.get(&q).is_some_and(|old| *old <= d + 1) {
                continue;
            }
            dist.insert(q, d + 1);
            queue.push(Reverse((d + 1, q)));
        }
    }
    None
}
#[test]
fn direction_state_astar_matches_independent_dijkstra_for_256_worlds() {
    let positions = [
        [20, 10],
        [20, 20],
        [20, 30],
        [20, 40],
        [20, 50],
        [30, 20],
        [40, 30],
        [40, 40],
    ];
    for mask in 0..256 {
        let mut i = base();
        i.rules.clearance_nm = 0;
        i.pads[1].at = [50, 50];
        let mut blocked = BTreeSet::new();
        for (j, p) in positions.iter().enumerate() {
            if mask & (1 << j) != 0 {
                let net = j as u32 + 2;
                i.nets.push(Net {
                    id: net,
                    name: format!("O{j}"),
                });
                i.pads.push(pad(j as u64 + 10, net, *p));
                blocked.insert(*p);
            }
        }
        let e = Engine::new(i).unwrap();
        let mut expanded = 0;
        let actual = search::search(&e, 1, [10, 10], [50, 50], 10000, &mut expanded);
        match (oracle([10, 10], [50, 50], &blocked), actual) {
            (Some(cost), Ok(path)) => {
                assert_eq!(path.steps, cost, "mask {mask}");
                assert_eq!(path.points.len() as u64, cost + 1);
            }
            (None, Err(err)) => assert_eq!(err.status, Status::Unroutable),
            (expected, actual) => panic!("mask {mask}: oracle={expected:?}, search={actual:?}"),
        }
    }
}
fn document() -> Value {
    json!({"version":1,"source_id":"a".repeat(64),"layer":"F.Cu","bounds":{"min":[0,0],"max":[60,60]},"rules":{"width_nm":2,"clearance_nm":1,"grid_nm":10,"edge_clearance_nm":0},"nets":[{"id":1,"name":"A"}],"pads":[{"id":id(1),"net":1,"at":[10,10],"shape":{"kind":"circle","radius_nm":1}},{"id":id(2),"net":1,"at":[50,10],"shape":{"kind":"rect","half_size_nm":[1,1]}}],"tracks":[],"max_expansions":10000})
}
#[test]
fn malformed_and_unsupported_never_become_empty_success() {
    for (pointer, value, status) in [
        ("/version", json!(2), Status::Unsupported),
        ("/layer", json!("B.Cu"), Status::Unsupported),
        ("/rules/grid_nm", json!(0), Status::Invalid),
        ("/rules/clearance_nm", json!(-1), Status::Invalid),
        ("/pads/0/at", json!([100000001, 10]), Status::Invalid),
        ("/pads/0/net", json!(999), Status::Invalid),
        ("/pads/0/id", json!(id(2)), Status::Invalid),
        ("/pads/0/id", json!(""), Status::Invalid),
        ("/bounds/max", json!([i64::MAX, 60]), Status::Invalid),
        ("/pads/0/shape/radius_nm", json!(0), Status::Invalid),
        ("/max_expansions", json!(0), Status::Invalid),
        ("/max_expansions", json!(1000001), Status::Invalid),
        ("/tracks", Value::Null, Status::Invalid),
        ("/source_id", json!("not-a-digest"), Status::Invalid),
    ] {
        let mut v = document();
        *v.pointer_mut(pointer).unwrap() = value;
        let o = run_json(&serde_json::to_vec(&v).unwrap());
        assert_eq!(o.status, status, "{pointer}");
        assert!(o.added_tracks.is_empty());
        assert!(o.removed_track_ids.is_empty());
    }
    let mut v = document();
    v["pads"][0]["rotation"] = json!(45);
    assert_eq!(
        run_json(&serde_json::to_vec(&v).unwrap()).status,
        Status::Unsupported
    );
    let mut v = document();
    v["unsupported_future"] = json!([]);
    assert_eq!(
        run_json(&serde_json::to_vec(&v).unwrap()).status,
        Status::Unsupported
    );
    let mut v = document();
    v["zones"] = json!([{
        "id": id(99),
        "net": 1,
        "layers": ["F.Cu"],
        "outer": [[0, 0], [20, 0], [20, 20], [0, 20]],
        "fill_mode": "hatch"
    }]);
    assert_eq!(
        run_json(&serde_json::to_vec(&v).unwrap()).status,
        Status::Unsupported
    );
    assert_eq!(run_json(b"{").status, Status::Invalid);
    assert_eq!(run_json(b"{}").status, Status::Invalid);
    let mut i = base();
    i.pads.push(pad(3, 1, [30, 30]));
    assert_eq!(run(i).status, Status::Complete); // multi-terminal input is supported
}
#[test]
fn singleton_nets_and_immutable_unlocked_copper_are_preserved() {
    let mut i = base();
    i.pads.pop();
    let mut t = wire(3, 1, [10, 10], [20, 10]);
    t.locked = false;
    i.tracks.push(t.clone());
    let mut e = Engine::new(i).unwrap();
    assert_eq!(e.route_all().unwrap(), 0);
    assert_eq!(e.copper().input(), &[t]);
    assert_eq!(e.copper().generated().count(), 0);
}
#[test]
fn cli_structured_errors_replace_old_edits_and_have_nonzero_exit() {
    use std::{fs, process::Command};
    let dir = std::env::temp_dir().join(format!("kicad-rust-cli-{}", std::process::id()));
    fs::create_dir(&dir).unwrap();
    let input = dir.join("in.json");
    let output = dir.join("out.json");
    let check = (|| {
        fs::write(&input, serde_json::to_vec(&document()).unwrap()).unwrap();
        assert!(Command::new(env!("CARGO_BIN_EXE_kicad-route-engine"))
            .args([&input, &output])
            .status()
            .unwrap()
            .success());
        let good: Value = serde_json::from_slice(&fs::read(&output).unwrap()).unwrap();
        assert_eq!(good["status"], "complete");
        assert_eq!(good["version"], 3);
        assert!(!good["added_tracks"].as_array().unwrap().is_empty());
        fs::write(&input, b"{broken").unwrap();
        assert!(!Command::new(env!("CARGO_BIN_EXE_kicad-route-engine"))
            .args([&input, &output])
            .status()
            .unwrap()
            .success());
        let bad: Value = serde_json::from_slice(&fs::read(&output).unwrap()).unwrap();
        assert_eq!(bad["status"], "invalid");
        assert_eq!(bad["version"], 3);
        assert_eq!(bad["removed_track_ids"], json!([]));
        assert!(bad["added_tracks"].as_array().unwrap().is_empty());
        Ok::<(), ()>(())
    })();
    fs::remove_dir_all(&dir).unwrap();
    check.unwrap();
}

#[test]
fn duplicate_json_fields_cannot_hide_input_copper() {
    let text = serde_json::to_string(&document()).unwrap();
    for duplicate in ["\"tracks\":[],", "\"pads\":[],", "\"version\":1,"] {
        let bad = format!("{{{duplicate}{}", &text[1..]);
        let out = run_json(bad.as_bytes());
        assert_eq!(out.status, Status::Invalid);
        assert!(out.added_tracks.is_empty());
        assert!(out.reason.unwrap().contains("duplicate field"));
    }
}

#[test]
fn every_structured_failure_has_no_edits() {
    let mut documents = Vec::new();
    let mut budget = document();
    budget["max_expansions"] = json!(1);
    documents.push((budget, Status::BudgetExhausted));
    let mut unsupported = document();
    unsupported["layer"] = json!("B.Cu");
    documents.push((unsupported, Status::Unsupported));
    let mut blocked = document();
    blocked["nets"]
        .as_array_mut()
        .unwrap()
        .push(json!({"id":2,"name":"wall"}));
    blocked["tracks"] =
        json!([{"id":id(10),"net":2,"start":[30,0],"end":[30,60],"width_nm":2,"locked":false}]);
    documents.push((blocked, Status::Unroutable));
    for (value, status) in documents {
        let o = run_json(&serde_json::to_vec(&value).unwrap());
        assert_eq!(o.status, status);
        assert!(o.added_tracks.is_empty());
        assert!(o.removed_track_ids.is_empty());
        assert!(o.reason.is_some());
    }
}

#[test]
fn unconnected_net_is_declared_but_never_routed_or_joined() {
    let mut i = base();
    i.nets = vec![Net {
        id: 0,
        name: String::new(),
    }];
    i.pads = vec![
        pad(1, 0, [10, 10]),
        pad(2, 0, [50, 10]),
        pad(3, 0, [30, 30]),
    ];
    i.tracks = vec![wire(4, 0, [10, 10], [50, 10])];
    let original = i.tracks.clone();
    let mut e = Engine::new(i).unwrap();
    assert_eq!(e.route_all().unwrap(), 0);
    assert_eq!(e.copper().input(), original);
    assert_eq!(e.copper().generated().count(), 0);
    assert_eq!(e.reroute(0, 100).unwrap_err().status, Status::Invalid);
    assert!(!e.legal_edge(0, [10, 10], [50, 10]).unwrap());
    assert_eq!(
        search::search(&e, 0, [10, 10], [50, 10], 100, &mut 0)
            .unwrap_err()
            .status,
        Status::Invalid
    );
}
#[test]
fn unconnected_tracks_and_pads_remain_foreign_obstacles() {
    let mut i = base();
    i.nets.push(Net {
        id: 0,
        name: String::new(),
    });
    assert_eq!(run(i.clone()).status, Status::Complete); // native net table always includes 0
    i.pads.push(pad(3, 0, [30, 10]));
    let out = run(i.clone());
    assert_eq!(out.status, Status::Complete);
    assert!(out.added_tracks.iter().all(|t| t.net == 1));
    assert!(out
        .added_tracks
        .iter()
        .any(|t| t.start[1] > 10 || t.end[1] > 10));
    i.tracks.push(wire(4, 0, [30, 0], [30, 60]));
    let out = run(i);
    assert_eq!(out.status, Status::Unroutable);
    assert!(out.added_tracks.is_empty());
    assert!(out.removed_track_ids.is_empty());
}

#[test]
fn pad_connectivity_requires_positive_overlap_for_all_supported_shapes() {
    let mut a = pad(1, 1, [20, 20]);
    a.shape = Shape::Circle { radius_nm: 5 };
    let mut b = pad(2, 1, [30, 20]);
    b.shape = Shape::Circle { radius_nm: 5 };
    assert!(!pads_touch(&a, &b));
    b.at = [29, 20];
    assert!(pads_touch(&a, &b));
    b.shape = Shape::Rect {
        half_size_nm: [5, 5],
    };
    b.at = [30, 20];
    assert!(!pads_touch(&a, &b));
    assert!(!pads_touch(&b, &a));
    b.at = [29, 20];
    assert!(pads_touch(&a, &b));
    b.at = [30, 30];
    a.at = [22, 21]; // 3-4-5 circle/rectangle corner tangency
    assert!(!pads_touch(&a, &b));
    a.at = [23, 21]; // sqrt(20) distance: only .528 nm overlap
    assert!(!pads_touch(&a, &b));
    a.at = [23, 22]; // sqrt(13) distance: more than 1 nm overlap
    assert!(pads_touch(&a, &b));
    a.shape = Shape::Rect {
        half_size_nm: [5, 5],
    };
    a.at = [20, 20];
    for at in [[30, 20], [30, 30]] {
        b.at = at;
        assert!(!pads_touch(&a, &b));
    }
    b.at = [29, 29];
    assert!(pads_touch(&a, &b));
}

#[test]
fn tangent_track_pad_is_routed_but_positive_overlap_reuses_copper() {
    for shape in [
        Shape::Circle { radius_nm: 1 },
        Shape::Rect {
            half_size_nm: [1, 1],
        },
    ] {
        for width in [2, 3] {
            // odd width's half-nm contact is not trusted
            for end in [48, 49] {
                let mut i = base();
                i.pads[1].shape = shape.clone();
                let mut t = wire(3, 1, [10, 10], [end, 10]);
                t.width_nm = width;
                i.tracks.push(t);
                let o = run(i);
                assert_eq!(o.status, Status::Complete);
                assert_eq!(
                    o.added_tracks.is_empty(),
                    end == 49,
                    "width={width}, end={end}, shape={shape:?}"
                );
            }
        }
    }
}

#[test]
fn tangent_track_chain_is_not_complete_and_odd_widths_are_conservative() {
    for (wa, wb) in [(2, 2), (3, 2), (3, 3), (4, 3)] {
        let radius_sum = wa / 2 + wb / 2;
        for overlap in [0, 1] {
            let mut i = base();
            let mut a = wire(3, 1, [10, 10], [29, 10]);
            a.width_nm = wa;
            let mut b = wire(4, 1, [29 + radius_sum - overlap, 10], [50, 10]);
            b.width_nm = wb;
            i.tracks.extend([a, b]);
            let o = run(i);
            assert_eq!(o.status, Status::Complete);
            assert_eq!(
                o.added_tracks.is_empty(),
                overlap == 1,
                "widths={wa},{wb} overlap={overlap}"
            );
        }
    }
}

#[test]
fn tangent_pad_pair_routes_instead_of_skipping() {
    for shape in [
        Shape::Circle { radius_nm: 5 },
        Shape::Rect {
            half_size_nm: [5, 5],
        },
    ] {
        let mut i = base();
        i.pads[0].at = [20, 20];
        i.pads[1].at = [30, 20];
        i.pads[0].shape = shape.clone();
        i.pads[1].shape = shape;
        let o = run(i);
        assert_eq!(o.status, Status::Complete);
        assert!(!o.added_tracks.is_empty());
    }
}

#[test]
fn clearance_equality_remains_legal_and_failure_reason_is_scoped() {
    let mut i = base();
    i.nets.push(Net {
        id: 2,
        name: "B".into(),
    });
    i.tracks.push(wire(3, 2, [10, 20], [50, 20]));
    let e = Engine::new(i).unwrap();
    assert!(e.legal_edge(1, [10, 17], [50, 17]).unwrap()); // 1+1 half-widths + 1 clearance
    assert!(!e.legal_edge(1, [10, 18], [50, 18]).unwrap());
    let mut i = base();
    i.nets.push(Net {
        id: 2,
        name: "wall".into(),
    });
    i.tracks.push(wire(3, 2, [30, 0], [30, 60]));
    let o = run(i);
    assert_eq!(o.status, Status::Unroutable);
    let reason = o.reason.unwrap();
    assert!(reason.contains("fixed net order"));
    assert!(reason.contains("not proof of global infeasibility"));
}

#[test]
fn diagonal_incumbent_sub_nm_overlap_is_not_false_complete() {
    // Actual binary/native counterexample: native KiCad 10.0.8 has one open.
    // Exact centre distance is 199999.814949... nm, so positive overlap alone
    // was insufficient. A one-expansion budget distinguishes reuse from search.
    for (shift, reused) in [(0, false), (-2, true), (2, false)] {
        let mut i = base();
        i.bounds = Bounds {
            min: [-10000000, -10000000],
            max: [10000000, 10000000],
        };
        i.rules.grid_nm = 1;
        i.rules.width_nm = 200000;
        i.rules.clearance_nm = 0;
        i.max_expansions = 1;
        i.pads = vec![
            pad(1, 1, [1000000, 4000000]),
            pad(2, 1, [2444123, 5368809 + shift]),
        ];
        for p in &mut i.pads {
            p.shape = Shape::Circle { radius_nm: 100000 };
        }
        let mut a = wire(3, 1, [1000000, 4000000], [3142620, 3416878]);
        let mut b = wire(4, 1, [2050220, 3921453 + shift], [2444123, 5368809 + shift]);
        a.width_nm = 200000;
        b.width_nm = 200000;
        i.tracks = vec![a, b];
        let out = run(i);
        assert_eq!(
            out.status,
            if reused {
                Status::Complete
            } else {
                Status::BudgetExhausted
            },
            "shift={shift}"
        );
        assert!(out.added_tracks.is_empty());
    }
}

#[test]
fn diagonal_circle_pad_track_contact_requires_one_nm_overlap() {
    // Diagonal projection: distance from [50,52] to y=x is sqrt(2).
    // Track radius 1 + circle radius 1 gives only .586 nm overlap.
    for (y, reused) in [(52, false), (51, true), (53, false)] {
        let mut i = base();
        i.rules.grid_nm = 1;
        i.max_expansions = 1;
        i.pads[1].at = [50, y];
        i.tracks = vec![wire(3, 1, [10, 10], [55, 55])];
        let out = run(i);
        assert_eq!(
            out.status,
            if reused {
                Status::Complete
            } else {
                Status::BudgetExhausted
            },
            "y={y}"
        );
    }
}

#[test]
fn full_nm_contact_threshold_handles_corners_and_tiny_widths() {
    let a = pad(1, 1, [10, 10]);
    let mut b = pad(2, 1, [11, 11]); // radii sum 2, distance sqrt(2), overlap < 1
    assert!(!pads_touch(&a, &b));
    b.at = [11, 10]; // exactly one nm overlap
    assert!(pads_touch(&a, &b));
    let mut rect = pad(5, 1, [30, 30]);
    rect.shape = Shape::Rect {
        half_size_nm: [5, 5],
    };
    let mut t = wire(3, 1, [0, 0], [23, 21]);
    t.width_nm = 10;
    assert!(!track_connects_pad(&t, &rect)); // corner distance sqrt(20) > 5-1
    t.end = [23, 22];
    assert!(track_connects_pad(&t, &rect)); // sqrt(13) < 5-1
    t.end = [25, 21];
    assert!(track_connects_pad(&t, &rect)); // distance 4, equality at 1 nm overlap
    t.width_nm = 1;
    t.end = [30, 30];
    assert!(!track_connects_pad(&t, &rect)); // negative effective radius cannot be squared
    let mut u = wire(4, 1, [0, 30], [30, 0]);
    u.width_nm = 1;
    assert!(!tracks_connect(&t, &u)); // even intersecting centre lines need enough radius
    assert!(!point_near_segment([0, 0], [0, 0], [10, 10], -2, true));
    assert!(!segments_near([0, 0], [10, 10], [0, 10], [10, 0], -2, true));
}

#[test]
fn direct_route_and_reroute_failures_name_the_search_domain() {
    let mut input = base();
    input.nets.push(Net {
        id: 2,
        name: "wall".into(),
    });
    input.tracks.push(wire(3, 2, [30, 0], [30, 60]));
    let mut e = Engine::new(input).unwrap();
    let before = serde_json::to_vec(&e.copper().tracks().collect::<Vec<_>>()).unwrap();
    let route = e.route(1, 10000).unwrap_err();
    let reroute = e.reroute(1, 10000).unwrap_err();
    for failure in [route, reroute, e.route_all().unwrap_err()] {
        assert_eq!(failure.status, Status::Unroutable);
        assert!(failure.reason.contains("current copper"));
        assert!(failure
            .reason
            .contains("fixed grid and pad-center/track-endpoint seeds"));
        assert_eq!(
            failure
                .reason
                .matches("not proof of global infeasibility")
                .count(),
            1
        );
        assert!(failure.expanded_states > 0);
        assert!(failure.work_used > 0);
    }
    assert!(e.copper().index_consistent());
    assert_eq!(
        serde_json::to_vec(&e.copper().tracks().collect::<Vec<_>>()).unwrap(),
        before
    );
    assert!(!e
        .route(1, 1)
        .unwrap_err()
        .reason
        .contains("global infeasibility"));
    assert!(!e
        .route(0, 1000)
        .unwrap_err()
        .reason
        .contains("global infeasibility"));
}

#[test]
fn net_name_limit_reports_utf8_bytes_at_the_boundary() {
    for name in ["a".repeat(1024), "é".repeat(512)] {
        let mut input = base();
        input.nets[0].name = name;
        assert_eq!(run(input).status, Status::Complete);
    }
    for name in ["a".repeat(1025), "é".repeat(513)] {
        let mut input = base();
        input.nets[0].name = name;
        let result = run(input);
        assert_eq!(result.status, Status::Invalid);
        assert_eq!(result.version, 3);
        assert_eq!(
            result.reason.as_deref(),
            Some("net name exceeds 1024 UTF-8 bytes")
        );
        assert_eq!(result.work_used, 0);
        assert!(result.added_tracks.is_empty());
    }
}

#[test]
fn subprocess_input_and_result_versions_are_independent_and_publication_failure_is_explicit() {
    use std::{fs, process::Command};
    let dir = std::env::temp_dir().join(format!("kicad-cli-versions-{}", std::process::id()));
    fs::create_dir(&dir).unwrap();
    let input = dir.join("in.json");
    let output = dir.join("out.json");
    for (version, exit, status) in [
        (1, 0, "complete"),
        (2, 2, "unsupported"),
        (0, 2, "unsupported"),
    ] {
        let mut doc = document();
        doc["version"] = json!(version);
        fs::write(&input, serde_json::to_vec(&doc).unwrap()).unwrap();
        let process = Command::new(env!("CARGO_BIN_EXE_kicad-route-engine"))
            .args([&input, &output])
            .output()
            .unwrap();
        assert_eq!(process.status.code(), Some(exit));
        let result: Value = serde_json::from_slice(&fs::read(&output).unwrap()).unwrap();
        assert_eq!(result["version"], 3);
        assert_eq!(result["status"], status);
        assert!(result["expanded_states"].is_u64());
        assert!(result["work_used"].is_u64());
        assert_eq!(result["removed_track_ids"], json!([]));
        if exit != 0 {
            assert_eq!(result["added_tracks"], json!([]));
        }
    }
    fs::write(&input, serde_json::to_vec(&document()).unwrap()).unwrap();
    fs::remove_file(&output).unwrap();
    fs::create_dir(&output).unwrap(); // rename cannot replace a directory
    let process = Command::new(env!("CARGO_BIN_EXE_kicad-route-engine"))
        .args([&input, &output])
        .output()
        .unwrap();
    assert_eq!(process.status.code(), Some(8));
    let stderr = String::from_utf8(process.stderr).unwrap();
    assert!(stderr.contains("no fresh result established"));
    assert!(stderr.contains("destination may still contain an older result"));
    assert!(output.is_dir());
    assert_eq!(fs::read_dir(&dir).unwrap().count(), 2); // owned temporary removed
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn exact_request_identity_binds_rules_limits_budget_and_byte_representation() {
    use kicad_route_engine::{input_request_sha256, request_sha256};
    let doc = document();
    let original = serde_json::to_vec(&doc).unwrap();
    let expected = request_sha256(&original).unwrap();
    assert_eq!(
        run_json(&original).request_sha256.as_deref(),
        Some(expected.as_str())
    );
    for (pointer, value) in [
        ("/rules/width_nm", json!(4)),
        ("/rules/clearance_nm", json!(2)),
        ("/rules/grid_nm", json!(5)),
        ("/rules/edge_clearance_nm", json!(1)),
        ("/max_expansions", json!(1)),
        ("/source_id", json!("b".repeat(64))),
    ] {
        let mut changed = doc.clone();
        *changed.pointer_mut(pointer).unwrap() = value;
        let output = run_json(&serde_json::to_vec(&changed).unwrap());
        assert_ne!(
            output.request_sha256.as_deref(),
            Some(expected.as_str()),
            "{pointer}"
        );
    }
    for field in [
        "max_work",
        "max_items",
        "max_search_entries",
        "max_journal_entries",
        "max_depth",
    ] {
        let mut changed = doc.clone();
        changed["limits"] = json!({field: 0});
        let output = run_json(&serde_json::to_vec(&changed).unwrap());
        assert_ne!(output.status, Status::Complete);
        assert_ne!(
            output.request_sha256.as_deref(),
            Some(expected.as_str()),
            "{field}"
        );
    }
    let pretty = serde_json::to_vec_pretty(&doc).unwrap();
    assert_ne!(request_sha256(&pretty), Some(expected));
    assert_eq!(run_json(&pretty).status, Status::Complete);
    let typed: Input = serde_json::from_value(doc).unwrap();
    let serialized = serde_json::to_vec(&typed).unwrap();
    let typed_digest = input_request_sha256(&typed);
    assert_eq!(
        request_sha256(&serialized).as_deref(),
        Some(typed_digest.as_str())
    );
    assert_ne!(request_sha256(&original), Some(typed_digest.clone())); // defaults are explicit
    assert_eq!(
        serde_json::to_value(run(typed)).unwrap(),
        serde_json::to_value(run_json(&serialized)).unwrap()
    );
}

#[test]
fn malformed_and_oversized_request_identity_is_explicit() {
    use kicad_route_engine::{identity::MAX_REQUEST_BYTES, request_sha256};
    for bytes in [b"{broken".as_slice(), b"", b"{\"version\":1,\"version\":1}"] {
        let result = run_json(bytes);
        assert_ne!(result.status, Status::Complete);
        assert_eq!(result.request_sha256, request_sha256(bytes));
        assert!(result.request_sha256.is_some());
    }
    let mut exact = serde_json::to_vec(&document()).unwrap();
    exact.resize(MAX_REQUEST_BYTES, b' ');
    assert_eq!(run_json(&exact).request_sha256, request_sha256(&exact));
    assert_eq!(run_json(&exact).status, Status::Complete);
    exact.push(b' ');
    let over = run_json(&exact);
    assert_eq!(over.status, Status::AdmissionExceeded);
    assert!(over.request_sha256.is_none());
    assert!(request_sha256(&exact).is_none());
    assert_eq!(
        serde_json::to_value(over).unwrap()["request_sha256"],
        Value::Null
    );
}

#[test]
fn subprocess_matches_full_request_and_distinguishes_no_publication() {
    use std::{fs, process::Command};
    let dir = std::env::temp_dir().join(format!("rust-request-freshness-{}", std::process::id()));
    fs::create_dir(&dir).unwrap();
    let input = dir.join("in.json");
    let output = dir.join("out.json");
    let mut old_digest = None;
    let mut doc = document();
    for change in 0..4 {
        match change {
            1 => doc["rules"]["width_nm"] = json!(4),
            2 => doc["limits"] = json!({"max_work": 0}),
            3 => doc["max_expansions"] = json!(1),
            _ => {}
        }
        let bytes = serde_json::to_vec(&doc).unwrap();
        fs::write(&input, &bytes).unwrap();
        let process = Command::new(env!("CARGO_BIN_EXE_kicad-route-engine"))
            .args([&input, &output])
            .output()
            .unwrap();
        assert_eq!(process.status.code(), Some(if change < 2 { 0 } else { 5 }));
        let result: Value = serde_json::from_slice(&fs::read(&output).unwrap()).unwrap();
        assert_eq!(result, serde_json::to_value(run_json(&bytes)).unwrap());
        let digest = result["request_sha256"].as_str().unwrap().to_owned();
        assert_ne!(Some(&digest), old_digest.as_ref());
        old_digest = Some(digest);
    }
    fs::write(&input, b"{broken").unwrap();
    let invalid = Command::new(env!("CARGO_BIN_EXE_kicad-route-engine"))
        .args([&input, &output])
        .output()
        .unwrap();
    assert_eq!(invalid.status.code(), Some(2));
    let invalid_bytes = fs::read(&output).unwrap();
    let invalid_json: Value = serde_json::from_slice(&invalid_bytes).unwrap();
    assert_eq!(invalid_json["status"], "invalid");
    assert_eq!(
        invalid_json["request_sha256"],
        json!(kicad_route_engine::request_sha256(b"{broken"))
    );
    let refused = Command::new(env!("CARGO_BIN_EXE_kicad-route-engine"))
        .args([&output, &output])
        .output()
        .unwrap();
    assert_eq!(refused.status.code(), Some(8));
    assert_eq!(fs::read(&output).unwrap(), invalid_bytes);
    let usage = Command::new(env!("CARGO_BIN_EXE_kicad-route-engine"))
        .output()
        .unwrap();
    assert_eq!(usage.status.code(), Some(8));
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn typed_request_hashes_the_entire_input_without_a_wire_prefix_identity() {
    use kicad_route_engine::{identity::MAX_REQUEST_BYTES, input_request_sha256, request_sha256};
    let mut input = base();
    input.nets[0].name = "a".repeat(MAX_REQUEST_BYTES + 1);
    let bytes = serde_json::to_vec(&input).unwrap();
    assert!(request_sha256(&bytes).is_none());
    let first = input_request_sha256(&input);
    input.nets[0].name.push('b'); // past any CLI bounded prefix
    let second = input_request_sha256(&input);
    assert_ne!(first, second);
    let result = run(input);
    assert_eq!(result.status, Status::Invalid);
    assert_eq!(
        result.reason.as_deref(),
        Some("net name exceeds 1024 UTF-8 bytes")
    );
    assert_eq!(result.request_sha256, Some(second));
    assert!(result.added_tracks.is_empty());
}

#[test]
fn typed_limits_each_change_request_identity() {
    use kicad_route_engine::input_request_sha256;
    let input = base();
    let baseline_digest = input_request_sha256(&input);
    let baseline_result = run(input.clone());
    assert_eq!(baseline_result.status, Status::Complete);
    assert_eq!(
        baseline_result.request_sha256.as_ref(),
        Some(&baseline_digest)
    );
    let defaults = input.limits;
    let changes = [
        (
            "max_work",
            Limits {
                max_work: defaults.max_work - 1,
                ..defaults
            },
        ),
        (
            "max_items",
            Limits {
                max_items: defaults.max_items - 1,
                ..defaults
            },
        ),
        (
            "max_search_entries",
            Limits {
                max_search_entries: defaults.max_search_entries - 1,
                ..defaults
            },
        ),
        (
            "max_journal_entries",
            Limits {
                max_journal_entries: defaults.max_journal_entries - 1,
                ..defaults
            },
        ),
        (
            "max_depth",
            Limits {
                max_depth: defaults.max_depth - 1,
                ..defaults
            },
        ),
    ];
    for (field, limits) in changes {
        let mut changed = input.clone();
        changed.limits = limits;
        let digest = input_request_sha256(&changed);
        let result = run(changed);
        // These limits still permit the same route: identity must bind the
        // request even when the resulting edits and counters do not change.
        assert_eq!(result.status, Status::Complete, "{field}");
        assert_eq!(result.added_tracks, baseline_result.added_tracks, "{field}");
        assert_eq!(
            result.expanded_states, baseline_result.expanded_states,
            "{field}"
        );
        assert_eq!(result.work_used, baseline_result.work_used, "{field}");
        assert_ne!(digest, baseline_digest, "helper omitted typed {field}");
        assert_ne!(
            result.request_sha256, baseline_result.request_sha256,
            "run omitted typed {field}"
        );
        assert_eq!(result.request_sha256.as_ref(), Some(&digest), "{field}");
    }
}

#[test]
fn typed_limits_defaults_are_explicit_in_serialized_input() {
    let input: Input = serde_json::from_value(document()).unwrap();
    let serialized = serde_json::to_value(input).unwrap();
    assert_eq!(
        serialized["limits"],
        json!({
            "max_work": 100_000_000,
            "max_items": 100_000,
            "max_search_entries": 1_000_000,
            "max_journal_entries": 1_000_000,
            "max_depth": 64
        })
    );
}

#[test]
fn m3_1_off_grid_offset_and_rotated_pads_route_legally() {
    let mut i = base();
    i.bounds.max = [200, 200];
    // Pad 1: off-grid position [11, 10] with grid_nm=10.
    i.pads[0].at = [11, 10];
    i.pads[0].shape = Shape::ConvexPolygon {
        vertices: vec![[11, 6], [15, 10], [11, 14], [7, 10]],
        radius_nm: 1,
    };
    // Pad 2: offset pad where pad.at=[150, 10] is outside copper centered at [160, 10].
    i.pads[1].at = [150, 10];
    i.pads[1].shape = Shape::BoxExact {
        center_2x: [320, 20],
        half_size_2x: [6, 6],
        radius_2x: 0,
    };
    // Foreign rotated capsule obstacle blocking direct y=10 corridor.
    i.nets.push(Net {
        id: 2,
        name: "OBS".into(),
    });
    i.pads.push(Pad {
        id: id(3),
        net: 2,
        at: [80, 10],
        shape: Shape::Capsule {
            start_nm: [75, 5],
            end_nm: [85, 15],
            width_nm: 4,
        },
        drill: None,
        layers: Vec::new(),
        local_clearance_nm: None,
        zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
        footprint_ref: None,
        pad_number: None,
        fromto_id: None,
    });
    let out = run(i.clone());
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    assert!(!out.added_tracks.is_empty());
    let e = Engine::new(i).unwrap();
    for t in &out.added_tracks {
        assert!(e.legal_edge(t.net, t.start, t.end).unwrap());
    }
}

#[test]
fn m3_1_pth_npth_hole_clearance_and_same_net_drill_rules() {
    // NPTH mounting hole blocks direct corridor under hole_clearance_nm=12 even though copper clearance_nm=1.
    let mut i = base();
    i.bounds.max = [100, 100];
    i.pads[0].at = [10, 50];
    i.pads[1].at = [90, 50];
    i.rules.hole_clearance_nm = 12;
    i.holes.push(Hole {
        id: id(10),
        net: 0,
        at: [50, 50],
        shape: DrillShape::Circle { diameter_nm: 10 },
    });
    let e = Engine::new(i.clone()).unwrap();
    // Direct edge [10, 50] -> [90, 50] passes through NPTH hole.
    assert!(!e.legal_edge(1, [10, 50], [90, 50]).unwrap());
    // Edge at y=67: distance 17 > half_track(1) + half_hole(5) + hole_clr(12) = 18 => still blocked; y=69 legal.
    assert!(!e.legal_edge(1, [10, 67], [90, 67]).unwrap());
    assert!(e.legal_edge(1, [10, 69], [90, 69]).unwrap());
    let out = run(i);
    assert_eq!(out.status, Status::Complete);
    for t in &out.added_tracks {
        assert!(e.legal_edge(t.net, t.start, t.end).unwrap());
    }

    // Same-net PTH drill: connecting track enters pad's own drill, while non-connecting same-net track
    // must clear another PTH pad's drill when same_net_hole_clearance_nm > 0.
    let mut j = base();
    j.bounds.max = [100, 100];
    j.rules.same_net_hole_clearance_nm = 8;
    j.pads = vec![
        Pad {
            id: id(1),
            net: 1,
            at: [10, 50],
            shape: Shape::Circle { radius_nm: 4 },
            drill: Some(DrillShape::Circle { diameter_nm: 2 }),
            layers: Vec::new(),
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
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
            at: [50, 50],
            shape: Shape::Circle { radius_nm: 4 },
            drill: Some(DrillShape::Circle { diameter_nm: 2 }),
            layers: Vec::new(),
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
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
            at: [90, 50],
            shape: Shape::Circle { radius_nm: 4 },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
        },
    ];
    let ej = Engine::new(j.clone()).unwrap();
    // Connecting to pad 1 at [10, 50] is allowed.
    assert!(ej.legal_edge(1, [10, 50], [20, 50]).unwrap());
    // Passing close to pad 2 at y=57 without connecting to pad 2 (pad radius 4 + track radius 1 = 5 < 7,
    // while drill radius 1 + track radius 1 + same_net_hole_clr 8 = 10 > 7) is blocked.
    assert!(!ej.legal_edge(1, [40, 57], [60, 57]).unwrap());
    assert_eq!(run(j).status, Status::Complete);
}

#[test]
fn m3_1_concave_outline_cutout_arc_graphic_and_mask_web_enforce_legality() {
    let mut i = base();
    i.bounds = Bounds {
        min: [0, 0],
        max: [100, 100],
    };
    i.rules.edge_clearance_nm = 2;
    i.rules.solder_mask_min_width_nm = 4;
    i.rules.solder_mask_to_copper_clearance_nm = 3;
    i.pads[0].at = [20, 20];
    i.pads[1].at = [20, 80];
    i.board_outline = Some(BoardOutline {
        polygons: vec![OutlinePolygon {
            outer: vec![[0, 0], [100, 0], [100, 40], [50, 40], [50, 100], [0, 100]],
            holes: vec![vec![[10, 45], [25, 45], [25, 55], [10, 55]]],
        }],
    });
    i.nets.push(Net {
        id: 2,
        name: "OBS".into(),
    });
    i.graphics.push(CopperGraphic {
        id: id(20),
        net: 2,
        layer: "F.Cu".into(),
        shape: GraphicShape::Arc {
            start: [2, 60],
            mid: [12, 65],
            end: [22, 60],
            width_nm: 2,
        },
    });
    i.mask_apertures.push(MaskAperture {
        id: id(21),
        net: None,
        group_id: None,
        layer: "F.Mask".into(),
        shape: GraphicShape::Segment {
            start: [28, 60],
            end: [28, 70],
            width_nm: 4,
        },
        expansion_nm: 1,
    });
    let e = Engine::new(i.clone()).unwrap();
    // Point inside L-notch [70, 70] and inside cutout slot [15, 50] are illegal.
    assert!(!e.legal_edge(1, [70, 70], [80, 70]).unwrap());
    assert!(!e.legal_edge(1, [15, 50], [20, 50]).unwrap());
    // Mid-arc bulge at [12, 65] and subdivided arc interior at x=7 (~[7, 64]) must block
    // tracks within radius 1 + track_radius 1 + clearance 1 = 3 (kills straight-chord & unsubdivided mutants).
    assert!(!e.legal_edge(1, [8, 67], [16, 67]).unwrap());
    assert!(e.legal_edge(1, [8, 71], [16, 71]).unwrap());
    assert!(!e.legal_edge(1, [7, 66], [7, 68]).unwrap());
    // Crossing F.Mask aperture at [28, 65] is illegal; routing around x=40 is legal.
    assert!(!e.legal_edge(1, [20, 65], [35, 65]).unwrap());
    assert!(e.legal_edge(1, [40, 60], [40, 70]).unwrap());
    let out = run(i);
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    for t in &out.added_tracks {
        assert!(e.legal_edge(t.net, t.start, t.end).unwrap());
    }
}

#[test]
fn m3_1_same_net_copper_graphic_bridges_components_and_rollback_has_zero_work_diff() {
    let mut i = base();
    i.bounds.max = [120, 80];
    i.pads[0].at = [10, 40];
    i.pads[1].at = [100, 40];
    // Same-net copper segment touches pad 0 at [10, 40] and reaches [50, 40].
    i.graphics.push(CopperGraphic {
        id: id(30),
        net: 1,
        layer: "F.Cu".into(),
        shape: GraphicShape::Segment {
            start: [10, 40],
            end: [50, 40],
            width_nm: 2,
        },
    });
    let mut e = Engine::new(i).unwrap();
    assert!(e.legal_edge(1, [60, 10], [60, 70]).unwrap());
    let baseline_work = e.work_used();

    let _ = e.transaction(|tx| {
        tx.route_all().unwrap();
        Err::<(), ()>(())
    });
    assert!(e.copper().index_consistent());
    assert!(e.legal_edge(1, [60, 10], [60, 70]).unwrap());
    assert_eq!(e.work_used(), baseline_work);

    let out = e.route_all().unwrap();
    assert!(out > 0);
    assert!(e.copper().index_consistent());
    // Because the same-net graphic already reached x=50, generated copper starts at x >= 49.
    assert!(e
        .copper()
        .generated()
        .all(|t| t.start[0] >= 49 && t.end[0] >= 49));
}

#[test]
fn m3_1_major_arc_exceeding_bounds_refuses_cleanly_without_panic() {
    for end in [[10_000_000, 100], [100_000_000, 100]] {
        let mut i = base();
        i.bounds = Bounds {
            min: [-100_000_000, -100_000_000],
            max: [100_000_000, 100_000_000],
        };
        i.graphics.push(CopperGraphic {
            id: id(40),
            net: 0,
            layer: "F.Cu".into(),
            shape: GraphicShape::Arc {
                start: [0, 0],
                mid: [-1, 0],
                end,
                width_nm: 1000,
            },
        });
        let out = run(i);
        assert_eq!(out.status, Status::Unsupported, "{:?}", out.reason);
    }

    let mut m = base();
    m.bounds = Bounds {
        min: [-100_000_000, -100_000_000],
        max: [100_000_000, 100_000_000],
    };
    m.mask_apertures.push(MaskAperture {
        id: id(41),
        net: None,
        group_id: None,
        layer: "F.Mask".into(),
        shape: GraphicShape::Arc {
            start: [0, 0],
            mid: [-1, 0],
            end: [10_000_000, 100],
            width_nm: 1000,
        },
        expansion_nm: 0,
    });
    let out_m = run(m);
    assert_eq!(out_m.status, Status::Unsupported, "{:?}", out_m.reason);
}

#[test]
fn m3_1_graphics_connect_hollow_circle_filled_poly_symmetry_and_hole_bounds() {
    // Primary Reviewer Probe 1: unfilled Circle must NEVER falsely merge an isolated interior Segment.
    let mut i = base();
    i.bounds = Bounds {
        min: [0, 0],
        max: [20_000, 20_000],
    };
    i.rules.width_nm = 200;
    i.rules.clearance_nm = 100;
    i.rules.grid_nm = 500;
    i.rules.edge_clearance_nm = 100;
    i.pads[0].at = [15_000, 10_000];
    i.pads[0].shape = Shape::Circle { radius_nm: 500 };
    i.pads[1].at = [10_000, 10_000];
    i.pads[1].shape = Shape::Circle { radius_nm: 500 };
    i.graphics.push(CopperGraphic {
        id: id(50),
        net: 1,
        layer: "F.Cu".into(),
        shape: GraphicShape::Circle {
            center: [10_000, 10_000],
            radius_nm: 5_000,
            width_nm: 200,
            filled: false,
        },
    });
    i.graphics.push(CopperGraphic {
        id: id(51),
        net: 1,
        layer: "F.Cu".into(),
        shape: GraphicShape::Segment {
            start: [10_000, 10_000],
            end: [10_500, 10_000],
            width_nm: 200,
        },
    });
    let out = run(i);
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    assert!(
        !out.added_tracks.is_empty(),
        "hollow circle must not falsely close disconnected interior net"
    );

    // Secondary Reviewer Probe 3: unfilled Circle listed before isolated interior Segment.
    let mut s = base();
    s.bounds = Bounds {
        min: [0, 0],
        max: [1_000, 1_000],
    };
    s.rules.width_nm = 10;
    s.rules.clearance_nm = 5;
    s.rules.grid_nm = 50;
    s.rules.edge_clearance_nm = 5;
    s.pads[0].at = [450, 500];
    s.pads[0].shape = Shape::Circle { radius_nm: 20 };
    s.pads[1].at = [700, 500];
    s.pads[1].shape = Shape::Circle { radius_nm: 20 };
    s.graphics.push(CopperGraphic {
        id: id(52),
        net: 1,
        layer: "F.Cu".into(),
        shape: GraphicShape::Circle {
            center: [500, 500],
            radius_nm: 200,
            width_nm: 10,
            filled: false,
        },
    });
    s.graphics.push(CopperGraphic {
        id: id(53),
        net: 1,
        layer: "F.Cu".into(),
        shape: GraphicShape::Segment {
            start: [500, 500],
            end: [520, 500],
            width_nm: 10,
        },
    });
    let out_s = run(s);
    assert_eq!(out_s.status, Status::Complete, "{:?}", out_s.reason);
    assert!(!out_s.added_tracks.is_empty());

    // Primary Reviewer Probe 2: outer-before-inner width=0 FilledPolygon stays 1 component.
    let mut p = base();
    p.bounds = Bounds {
        min: [0, 0],
        max: [12_000, 12_000],
    };
    p.rules.width_nm = 100;
    p.rules.clearance_nm = 50;
    p.rules.grid_nm = 200;
    p.rules.edge_clearance_nm = 50;
    p.pads[0].at = [2_000, 2_000];
    p.pads[0].shape = Shape::Circle { radius_nm: 200 };
    p.pads[1].at = [5_000, 5_000];
    p.pads[1].shape = Shape::Circle { radius_nm: 200 };
    p.graphics.push(CopperGraphic {
        id: id(54),
        net: 1,
        layer: "F.Cu".into(),
        shape: GraphicShape::Polygon {
            outer: vec![
                [1_000, 1_000],
                [10_000, 1_000],
                [10_000, 10_000],
                [1_000, 10_000],
            ],
            holes: Vec::new(),
            width_nm: 0,
            filled: true,
        },
    });
    p.graphics.push(CopperGraphic {
        id: id(55),
        net: 1,
        layer: "F.Cu".into(),
        shape: GraphicShape::Polygon {
            outer: vec![
                [4_000, 4_000],
                [6_000, 4_000],
                [6_000, 6_000],
                [4_000, 6_000],
            ],
            holes: Vec::new(),
            width_nm: 0,
            filled: true,
        },
    });
    let out_p = run(p);
    assert_eq!(out_p.status, Status::Complete, "{:?}", out_p.reason);
    assert!(
        out_p.added_tracks.is_empty(),
        "nested filled polygons must form 1 component without redundant routing"
    );

    // Disjoint hole outside outer ring is rejected as Invalid; width=0 unfilled polygon obstacle blocks track.
    let mut bad_hole = base();
    bad_hole.bounds = Bounds {
        min: [0, 0],
        max: [100, 100],
    };
    bad_hole.graphics.push(CopperGraphic {
        id: id(56),
        net: 0,
        layer: "F.Cu".into(),
        shape: GraphicShape::Polygon {
            outer: vec![[10, 10], [30, 10], [30, 30], [10, 30]],
            holes: vec![vec![[50, 50], [60, 50], [60, 60], [50, 60]]],
            width_nm: 2,
            filled: true,
        },
    });
    assert_eq!(run(bad_hole).status, Status::Invalid);

    let mut hairline = base();
    hairline.bounds = Bounds {
        min: [0, 0],
        max: [100, 100],
    };
    hairline.nets.push(Net {
        id: 2,
        name: "OBS".into(),
    });
    hairline.graphics.push(CopperGraphic {
        id: id(57),
        net: 2,
        layer: "F.Cu".into(),
        shape: GraphicShape::Polygon {
            outer: vec![[40, 20], [60, 20], [60, 80], [40, 80]],
            holes: Vec::new(),
            width_nm: 0,
            filled: false,
        },
    });
    let eh = Engine::new(hairline).unwrap();
    assert!(!eh.legal_edge(1, [30, 50], [50, 50]).unwrap());
}

#[test]
fn m3_1_solder_mask_min_width_without_copper_clearance_allows_fcu_tracks() {
    let mut i = base();
    i.bounds = Bounds {
        min: [0, 0],
        max: [20_000_000, 20_000_000],
    };
    i.rules = Rules {
        width_nm: 250_000,
        clearance_nm: 200_000,
        grid_nm: 500_000,
        edge_clearance_nm: 250_000,
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 200_000,
        solder_mask_to_copper_clearance_nm: 0,
    };
    i.pads[0].at = [4_000_000, 10_000_000];
    i.pads[1].at = [16_000_000, 10_000_000];
    i.mask_apertures.push(MaskAperture {
        id: id(60),
        net: None,
        group_id: None,
        layer: "F.Mask".into(),
        shape: GraphicShape::Polygon {
            outer: vec![
                [3_000_000, 9_000_000],
                [5_000_000, 9_000_000],
                [5_000_000, 11_000_000],
                [3_000_000, 11_000_000],
            ],
            holes: Vec::new(),
            width_nm: 0,
            filled: true,
        },
        expansion_nm: 0,
    });
    let e = Engine::new(i.clone()).unwrap();
    assert!(e
        .legal_edge(1, [4_000_000, 10_000_000], [4_500_000, 10_000_000])
        .unwrap());
    let out = run(i);
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
}

#[test]
fn input_validate_rejects_nonpositive_ac_match_options() {
    for (ac_tol, ac_amp) in [
        (Some(0), None),
        (Some(-1), None),
        (None, Some(0)),
        (None, Some(-5)),
    ] {
        let mut inp = base();
        inp.routing_options = Some(RoutingOptions {
            ac_match_tolerance_nm: ac_tol,
            ac_meander_amplitude_nm: ac_amp,
            ..Default::default()
        });
        let err = match Engine::new(inp) {
            Ok(_) => panic!("expected Invalid"),
            Err(e) => e,
        };
        assert_eq!(err.status, Status::Invalid, "{err:?}");
    }
}

#[test]
fn mask_aperture_segment_and_circle_block_when_unowned_or_positive_to_cu() {
    for (tag, shape) in [
        (
            "segment",
            GraphicShape::Segment {
                start: [25_000_000, 6_000_000],
                end: [25_000_000, 14_000_000],
                width_nm: 400_000,
            },
        ),
        (
            "circle",
            GraphicShape::Circle {
                center: [25_000_000, 10_000_000],
                radius_nm: 1_500_000,
                width_nm: 0,
                filled: true,
            },
        ),
    ] {
        for to_cu in [0, 200_000] {
            let mut i = base();
            i.layers = vec!["F.Cu".into()];
            i.bounds = Bounds {
                min: [0, 0],
                max: [50_000_000, 20_000_000],
            };
            i.rules = Rules {
                width_nm: 200_000,
                clearance_nm: 200_000,
                grid_nm: 500_000,
                edge_clearance_nm: 200_000,
                hole_clearance_nm: 200_000,
                same_net_hole_clearance_nm: 0,
                solder_mask_expansion_nm: 0,
                solder_mask_min_width_nm: 0,
                solder_mask_to_copper_clearance_nm: to_cu,
            };
            i.pads = vec![
                Pad {
                    id: id(1),
                    net: 1,
                    at: [5_000_000, 10_000_000],
                    shape: Shape::Circle { radius_nm: 250_000 },
                    drill: None,
                    layers: vec!["F.Cu".into()],
                    local_clearance_nm: None,
                    zone_connection: PadZoneConnection::Inherited,
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
                    at: [45_000_000, 10_000_000],
                    shape: Shape::Circle { radius_nm: 250_000 },
                    drill: None,
                    layers: vec!["F.Cu".into()],
                    local_clearance_nm: None,
                    zone_connection: PadZoneConnection::Inherited,
                    thermal_gap_nm: None,
                    thermal_spoke_width_nm: None,
                    thermal_spoke_angle_mdeg: None,
                    orientation_mdeg: 0,
                    footprint_ref: None,
                    pad_number: None,
                    fromto_id: None,
                },
            ];
            let mask = MaskAperture {
                id: id(50),
                net: None,
                group_id: None,
                layer: "F.Mask".into(),
                shape: shape.clone(),
                expansion_nm: 100_000,
            };
            i.mask_apertures.push(mask.clone());
            let out = run(i);
            assert_eq!(
                out.status,
                Status::Complete,
                "{tag} to_cu={to_cu}: {:?}",
                out.reason
            );
            if to_cu > 0 {
                assert!(
                    out.added_tracks.len() > 1,
                    "{tag} with to_cu={to_cu} should detour around mask aperture"
                );
                for t in &out.added_tracks {
                    assert!(
                        !track_near_mask(t.start, t.end, t.width_nm, to_cu, &mask, false),
                        "{tag} track touched mask aperture with to_cu={to_cu}"
                    );
                }
            }
        }
    }
}

#[test]
fn m3_1_same_net_hole_clearance_moat_routes_cleanly_while_blocking_non_connecting_pass_by() {
    // Challenger 2's reproduction: empty 20x20mm board with two same-net PTH pads where
    // same_net_hole_clearance_nm > annular_ring + grid_nm.
    let mut i = base();
    i.bounds = Bounds {
        min: [0, 0],
        max: [20_000_000, 20_000_000],
    };
    i.rules = Rules {
        width_nm: 250_000,
        clearance_nm: 1_000_000,
        grid_nm: 500_000,
        edge_clearance_nm: 500_000,
        hole_clearance_nm: 1_500_000,
        same_net_hole_clearance_nm: 1_200_000,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    i.pads = vec![
        Pad {
            id: id(1),
            net: 1,
            at: [4_000_000, 10_000_000],
            shape: Shape::Circle {
                radius_nm: 1_000_000,
            },
            drill: Some(DrillShape::Circle {
                diameter_nm: 1_000_000,
            }),
            layers: Vec::new(),
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
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
            at: [16_000_000, 10_000_000],
            shape: Shape::Circle {
                radius_nm: 1_000_000,
            },
            drill: Some(DrillShape::Circle {
                diameter_nm: 1_000_000,
            }),
            layers: Vec::new(),
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
        },
    ];
    let e = Engine::new(i.clone()).unwrap();
    // Non-connecting same-net track passing Pad 2 at dy=1_500_000 (< 500k + 125k + 1.2M = 1.825M) is blocked.
    assert!(!e
        .legal_edge(1, [10_000_000, 11_500_000], [18_000_000, 11_500_000])
        .unwrap());
    let out = run(i);
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    assert!(!out.added_tracks.is_empty());
    for t in &out.added_tracks {
        assert!(e.legal_edge(t.net, t.start, t.end).unwrap(), "{t:?}");
    }

    // Non-collinear PTH pair with obstacle between pads must also escape and enter cleanly.
    let mut j = base();
    j.bounds = Bounds {
        min: [0, 0],
        max: [20_000_000, 20_000_000],
    };
    j.rules = Rules {
        width_nm: 250_000,
        clearance_nm: 500_000,
        grid_nm: 500_000,
        edge_clearance_nm: 500_000,
        hole_clearance_nm: 1_500_000,
        same_net_hole_clearance_nm: 1_200_000,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    j.nets.push(Net {
        id: 2,
        name: "OBS".into(),
    });
    j.pads = vec![
        Pad {
            id: id(1),
            net: 1,
            at: [4_000_000, 6_000_000],
            shape: Shape::Circle {
                radius_nm: 1_000_000,
            },
            drill: Some(DrillShape::Circle {
                diameter_nm: 1_000_000,
            }),
            layers: Vec::new(),
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
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
            at: [16_000_000, 14_000_000],
            shape: Shape::Circle {
                radius_nm: 1_000_000,
            },
            drill: Some(DrillShape::Circle {
                diameter_nm: 1_000_000,
            }),
            layers: Vec::new(),
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
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
            net: 2,
            at: [10_000_000, 6_000_000],
            shape: Shape::Rect {
                half_size_nm: [500_000, 500_000],
            },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
        },
    ];
    let ej = Engine::new(j.clone()).unwrap();
    let out_j = run(j);
    assert_eq!(out_j.status, Status::Complete, "{:?}", out_j.reason);
    assert!(!out_j.added_tracks.is_empty());
    for t in &out_j.added_tracks {
        assert!(ej.legal_edge(t.net, t.start, t.end).unwrap(), "{t:?}");
    }
}

#[test]
fn m3_1_non_crossing_u_notch_filled_polygons_merge_without_spurious_tracks() {
    let m = 1_000_000i64;
    let mut i = base();
    i.bounds = Bounds {
        min: [-5 * m, -5 * m],
        max: [5 * m, 5 * m],
    };
    i.rules = Rules {
        width_nm: 200_000,
        clearance_nm: 100_000,
        grid_nm: 500_000,
        edge_clearance_nm: 100_000,
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    i.pads[0].at = [3_500_000, 2_500_000];
    i.pads[0].shape = Shape::Circle { radius_nm: 200_000 };
    i.pads[1].at = [-3_500_000, 2_500_000];
    i.pads[1].shape = Shape::Circle { radius_nm: 200_000 };
    i.graphics = vec![
        CopperGraphic {
            id: id(70),
            net: 1,
            layer: "F.Cu".into(),
            shape: GraphicShape::Polygon {
                outer: vec![
                    [0, 0],
                    [4 * m, 0],
                    [4 * m, 3 * m],
                    [3 * m, 3 * m],
                    [3 * m, m],
                    [m, m],
                    [m, 3 * m],
                    [0, 3 * m],
                ],
                holes: Vec::new(),
                width_nm: 0,
                filled: true,
            },
        },
        CopperGraphic {
            id: id(71),
            net: 1,
            layer: "F.Cu".into(),
            shape: GraphicShape::Polygon {
                outer: vec![
                    [-4 * m, 0],
                    [m, 0],
                    [m, 3 * m],
                    [0, 3 * m],
                    [0, m],
                    [-3 * m, m],
                    [-3 * m, 3 * m],
                    [-4 * m, 3 * m],
                ],
                holes: Vec::new(),
                width_nm: 0,
                filled: true,
            },
        },
    ];
    let out = run(i);
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    assert!(
        out.added_tracks.is_empty(),
        "overlapping U-notch filled polygons must merge: {:?}",
        out.added_tracks
    );
}

#[test]
fn m3_1_same_net_hole_clearance_incumbent_stub_in_moat_never_emits_illegal_fragment() {
    // Challenger 1 reproduction: incumbent collinear stub ending inside P1's moat must not
    // cause subtract_collinear_overlaps to emit an illegal track violating Engine::legal_edge.
    for stub_start in [[0, 0], [-2_000_000, 0]] {
        let mut i = base();
        i.bounds = Bounds {
            min: [-5_000_000, -5_000_000],
            max: [10_000_000, 5_000_000],
        };
        i.rules = Rules {
            width_nm: 200_000,
            clearance_nm: 200_000,
            grid_nm: 500_000,
            edge_clearance_nm: 200_000,
            hole_clearance_nm: 1_000_000,
            same_net_hole_clearance_nm: 1_000_000,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
        };
        i.pads = vec![
            Pad {
                id: id(1),
                net: 1,
                at: [0, 0],
                shape: Shape::Circle { radius_nm: 600_000 },
                drill: Some(DrillShape::Circle {
                    diameter_nm: 400_000,
                }),
                layers: Vec::new(),
                local_clearance_nm: None,
                zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
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
                at: [5_000_000, 0],
                shape: Shape::Circle { radius_nm: 600_000 },
                drill: Some(DrillShape::Circle {
                    diameter_nm: 400_000,
                }),
                layers: Vec::new(),
                local_clearance_nm: None,
                zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
                footprint_ref: None,
                pad_number: None,
                fromto_id: None,
            },
        ];
        i.tracks = vec![Track {
            id: id(3),
            net: 1,
            start: stub_start,
            end: [800_000, 0],
            width_nm: 200_000,
            locked: true,
            layer: "F.Cu".into(),
        }];
        let e = Engine::new(i.clone()).unwrap();
        assert!(!e.legal_edge(1, [800_000, 0], [4_300_001, 0]).unwrap());
        let out = run(i);
        assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
        assert!(!out.added_tracks.is_empty());
        for t in &out.added_tracks {
            assert!(
                e.legal_edge(t.net, t.start, t.end).unwrap(),
                "emitted illegal moat fragment for stub_start={stub_start:?}: {t:?}"
            );
        }
    }

    // Also verify partial subtraction outside moat: [0,0]->[800k,0] (in moat) + [2M,0]->[3M,0] (outside moat)
    // still subtracts [2M,0]->[3M,0] while preserving [0,0] pad contact.
    let mut j = base();
    j.bounds = Bounds {
        min: [-5_000_000, -5_000_000],
        max: [10_000_000, 5_000_000],
    };
    j.rules = Rules {
        width_nm: 200_000,
        clearance_nm: 200_000,
        grid_nm: 500_000,
        edge_clearance_nm: 200_000,
        hole_clearance_nm: 1_000_000,
        same_net_hole_clearance_nm: 1_000_000,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    j.pads = vec![
        Pad {
            id: id(1),
            net: 1,
            at: [0, 0],
            shape: Shape::Circle { radius_nm: 600_000 },
            drill: Some(DrillShape::Circle {
                diameter_nm: 400_000,
            }),
            layers: Vec::new(),
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
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
            at: [5_000_000, 0],
            shape: Shape::Circle { radius_nm: 600_000 },
            drill: Some(DrillShape::Circle {
                diameter_nm: 400_000,
            }),
            layers: Vec::new(),
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
        },
    ];
    j.tracks = vec![
        Track {
            id: id(3),
            net: 1,
            start: [0, 0],
            end: [800_000, 0],
            width_nm: 200_000,
            locked: true,
            layer: "F.Cu".into(),
        },
        Track {
            id: id(4),
            net: 1,
            start: [2_000_000, 0],
            end: [3_000_000, 0],
            width_nm: 200_000,
            locked: true,
            layer: "F.Cu".into(),
        },
    ];
    let ej = Engine::new(j.clone()).unwrap();
    let out_j = run(j);
    assert_eq!(out_j.status, Status::Complete, "{:?}", out_j.reason);
    for t in &out_j.added_tracks {
        assert!(ej.legal_edge(t.net, t.start, t.end).unwrap(), "{t:?}");
    }
}

#[test]
fn m3_2_multilayer_via_spans_hole_to_hole_and_keepouts() {
    let mut i = base();
    i.layers = vec![
        "F.Cu".into(),
        "In1.Cu".into(),
        "In2.Cu".into(),
        "B.Cu".into(),
    ];
    i.bounds = Bounds {
        min: [0, 0],
        max: [100, 60],
    };
    i.rules = Rules {
        width_nm: 2,
        clearance_nm: 1,
        grid_nm: 10,
        edge_clearance_nm: 1,
        hole_clearance_nm: 2,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    i.pads = vec![
        Pad {
            id: id(1),
            net: 1,
            at: [10, 30],
            layers: vec!["F.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 2 },
            drill: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
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
            at: [90, 30],
            layers: vec!["B.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 2 },
            drill: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
        },
    ];
    i.rule_areas = vec![RuleArea {
        id: id(20),
        layers: vec!["F.Cu".into()],
        no_tracks: true,
        no_vias: false,
        outer: vec![[35, 0], [65, 0], [65, 60], [35, 60]],
        holes: Vec::new(),
        name: None,
        extra_polygons: Vec::new(),
        no_pads: false,
        no_footprints: false,
        no_zone_fills: false,
    }];
    i.project_rules = Some(
        json!({
            "meta": {"version": 3},
            "board": {"design_settings": {"rules": {
                "min_via_diameter": 0.000004,
                "min_through_hole_diameter": 0.000002,
                "min_hole_to_hole": 0.000008,
                "allow_blind_buried_vias": true,
                "allow_microvias": true
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
    );

    let out = run(i);
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    assert!(!out.added_vias.is_empty());
    // Must not place any F.Cu track overlapping the F.Cu keepout [35..65].
    for t in &out.added_tracks {
        if t.layer == "F.Cu" {
            let lo = t.start[0].min(t.end[0]);
            let hi = t.start[0].max(t.end[0]);
            assert!(hi < 35 || lo > 65, "F.Cu track entered keepout: {t:?}");
        }
    }
    // At least one segment must transition to In1.Cu/In2.Cu/B.Cu to cross [35..65].
    assert!(out.added_tracks.iter().any(|t| t.layer != "F.Cu"));
}

#[test]
fn m3_2_copper_store_multilayer_via_rollback_zero_work_diff_and_index_consistent() {
    let mut i = base();
    i.layers = vec!["F.Cu".into(), "B.Cu".into()];
    i.bounds = Bounds {
        min: [0, 0],
        max: [100, 100],
    };
    i.rules.clearance_nm = 1;
    i.pads = vec![
        Pad {
            id: id(1),
            net: 1,
            at: [10, 10],
            layers: vec!["F.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 2 },
            drill: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
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
            at: [90, 10],
            layers: vec!["B.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 2 },
            drill: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
        },
    ];
    i.project_rules = Some(
        json!({
            "meta": {"version": 3},
            "net_settings": {"classes": [{
                "name": "Default",
                "track_width": 0.000002,
                "clearance": 0.000001,
                "via_diameter": 0.000004,
                "via_drill": 0.000002
            }]}
        })
        .to_string(),
    );
    let mut e = Engine::new(i.clone()).unwrap();
    let baseline = e.copper().tracks().count() + e.copper().vias().count();
    let _: Result<(), kicad_route_engine::TransactionError<&str>> = e.transaction(|tx| {
        tx.route(1, 5000).map_err(|_| "route err")?;
        assert!(tx.copper().index_consistent());
        assert!(tx.copper().vias().count() > 0);
        Err("rollback")
    });
    assert!(e.copper().index_consistent());
    assert_eq!(
        e.copper().tracks().count() + e.copper().vias().count(),
        baseline
    );
    let first_work = {
        let mut e2 = Engine::new(i).unwrap();
        e2.route(1, 5000).unwrap();
        e2.work_used()
    };
    e.route(1, 5000).unwrap();
    assert_eq!(e.work_used(), first_work);
}

#[test]
fn m3_2_project_netclasses_regex_dru_and_local_pad_clearance() {
    let mut i: Input = serde_json::from_value(json!({
        "version": 1,
        "source_id": "a".repeat(64),
        "layer": "F.Cu",
        "bounds": {"min": [0, 0], "max": [4_000_000, 4_000_000]},
        "nets": [
            {"id": 0, "name": ""},
            {"id": 1, "name": "HV_BUS"},
            {"id": 2, "name": "LOW"}
        ],
        "pads": [
            {"id": id(1), "net": 1, "at": [500_000, 2_000_000], "layers": ["F.Cu"], "shape": {"kind": "circle", "radius_nm": 100_000}},
            {"id": id(2), "net": 1, "at": [3_500_000, 2_000_000], "layers": ["F.Cu"], "shape": {"kind": "circle", "radius_nm": 100_000}},
            {"id": id(3), "net": 2, "at": [2_000_000, 2_000_000], "layers": ["F.Cu"], "local_clearance_nm": 400_000, "shape": {"kind": "circle", "radius_nm": 100_000}}
        ],
        "tracks": [],
        "max_expansions": 10_000
    }))
    .unwrap();
    i.project_rules = Some(
        json!({
            "meta": {"version": 3},
            "board": {"design_settings": {"rules": {"min_track_width": 0.2, "min_clearance": 0.15}}},
            "net_settings": {
                "classes": [
                    {"name": "Default", "track_width": 0.2, "clearance": 0.15},
                    {"name": "HV", "track_width": 0.3, "clearance": 0.25}
                ],
                "netclass_patterns": [{"netclass": "HV", "pattern": "HV_.*"}]
            }
        })
        .to_string(),
    );
    i.custom_rules = Some(
        "(version 1)\n(rule \"hv_gap\" (condition \"A.NetClass == 'HV'\") (constraint clearance (min 0.35mm)))"
            .into(),
    );
    let out = run(i.clone());
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    assert!(!out.added_tracks.is_empty());
    // Regex matched HV_BUS -> track width 300_000 nm; pad 3 local_clearance_nm=400_000 forces detour >= 650_000 from [2M, 2M].
    for t in &out.added_tracks {
        assert_eq!(t.width_nm, 300_000);
        if t.start[1] == t.end[1]
            && t.start[0].min(t.end[0]) < 2_000_000
            && t.start[0].max(t.end[0]) > 2_000_000
        {
            assert!(
                (t.start[1] - 2_000_000).abs() >= 650_000,
                "track violated local_clearance_nm: {t:?}"
            );
        }
    }

    // Unsupported .kicad_dru expression fails closed with Status::Unsupported.
    let mut bad = i;
    bad.custom_rules = Some(
        "(version 1)\n(rule \"bad\" (condition \"A.onLayer('F.Cu')\") (constraint clearance (min 0.2mm)))".into(),
    );
    assert_eq!(run(bad).status, Status::Unsupported);
}

#[test]
fn m3_2_pth_pad_drill_blocks_overlapping_buried_via_hole_to_hole() {
    // 4-layer board with PTH pad declared only on F.Cu+B.Cu; buried via on In1.Cu..In2.Cu
    // must still respect physical drill hole_to_hole against the PTH drill.
    let mut i = base();
    i.layers = vec![
        "F.Cu".into(),
        "In1.Cu".into(),
        "In2.Cu".into(),
        "B.Cu".into(),
    ];
    i.bounds = Bounds {
        min: [0, 0],
        max: [100, 60],
    };
    i.rules = Rules {
        width_nm: 2,
        clearance_nm: 1,
        grid_nm: 10,
        edge_clearance_nm: 1,
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    i.nets.push(Net {
        id: 2,
        name: "OBS".into(),
    });
    i.pads = vec![
        Pad {
            id: id(1),
            net: 1,
            at: [10, 30],
            layers: vec!["In1.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 2 },
            drill: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
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
            at: [50, 30],
            layers: vec!["In2.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 2 },
            drill: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
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
            net: 2,
            at: [30, 20],
            layers: vec!["F.Cu".into(), "B.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 2 },
            drill: Some(DrillShape::Circle { diameter_nm: 2 }),
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
        },
    ];
    i.project_rules = Some(
        json!({
            "meta": {"version": 3},
            "board": {"design_settings": {"rules": {
                "min_via_diameter": 0.000004,
                "min_through_hole_diameter": 0.000002,
                "min_hole_to_hole": 0.000015,
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
    // Block all paths except x=30 so the buried via cannot be placed within hole_to_hole=15 of [30,30].
    i.rule_areas = vec![
        RuleArea {
            id: id(10),
            layers: vec!["In1.Cu".into()],
            no_tracks: false,
            no_vias: true,
            outer: vec![[0, 0], [25, 0], [25, 60], [0, 60]],
            holes: Vec::new(),
            name: None,
            extra_polygons: Vec::new(),
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
        RuleArea {
            id: id(11),
            layers: vec!["In1.Cu".into()],
            no_tracks: false,
            no_vias: true,
            outer: vec![[35, 0], [100, 0], [100, 60], [35, 60]],
            holes: Vec::new(),
            name: None,
            extra_polygons: Vec::new(),
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
        RuleArea {
            id: id(12),
            layers: vec!["In1.Cu".into()],
            no_tracks: false,
            no_vias: true,
            outer: vec![[25, 0], [35, 0], [35, 25], [25, 25]],
            holes: Vec::new(),
            name: None,
            extra_polygons: Vec::new(),
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
        RuleArea {
            id: id(13),
            layers: vec!["In1.Cu".into()],
            no_tracks: false,
            no_vias: true,
            outer: vec![[25, 35], [35, 35], [35, 60], [25, 60]],
            holes: Vec::new(),
            name: None,
            extra_polygons: Vec::new(),
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
    ];
    let out = run(i);
    assert_eq!(
        out.status,
        Status::Unroutable,
        "buried via must not overlap PTH drill (reason={:?}): {:?}",
        out.reason,
        out.added_vias
    );
}

#[test]
fn m3_2_same_net_hole_clearance_blocks_buried_via_near_non_overlapping_same_net_pth_pad() {
    // Same-net PTH pad declared on F.Cu+B.Cu only: buried via on In1.Cu..In2.Cu does NOT
    // connect to pad copper on In1/In2, so same_net_hole_clearance_nm must block placing it in the moat.
    let mut i = base();
    i.layers = vec![
        "F.Cu".into(),
        "In1.Cu".into(),
        "In2.Cu".into(),
        "B.Cu".into(),
    ];
    i.bounds = Bounds {
        min: [0, 0],
        max: [100, 60],
    };
    i.rules = Rules {
        width_nm: 2,
        clearance_nm: 1,
        grid_nm: 10,
        edge_clearance_nm: 1,
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: 15,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    i.pads = vec![
        Pad {
            id: id(1),
            net: 1,
            at: [10, 30],
            layers: vec!["In1.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 2 },
            drill: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
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
            at: [50, 30],
            layers: vec!["In2.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 2 },
            drill: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
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
            at: [30, 30],
            layers: vec!["F.Cu".into(), "B.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 4 },
            drill: Some(DrillShape::Circle { diameter_nm: 2 }),
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
        },
    ];
    i.project_rules = Some(
        json!({
            "meta": {"version": 3},
            "board": {"design_settings": {"rules": {
                "min_via_diameter": 0.000006,
                "min_through_hole_diameter": 0.000002,
                "allow_blind_buried_vias": true,
                "allow_microvias": false
            }}},
            "net_settings": {
                "classes": [{
                    "name": "Default",
                    "track_width": 0.000002,
                    "clearance": 0.000001,
                    "via_diameter": 0.000006,
                    "via_drill": 0.000002
                }]
            }
        })
        .to_string(),
    );
    // Force via only at [30, 30] (same xy as Pad 3, non-overlapping layers In1..In2 vs F.Cu/B.Cu).
    i.rule_areas = vec![
        RuleArea {
            id: id(10),
            layers: vec!["In1.Cu".into()],
            no_tracks: false,
            no_vias: true,
            outer: vec![[0, 0], [25, 0], [25, 60], [0, 60]],
            holes: Vec::new(),
            name: None,
            extra_polygons: Vec::new(),
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
        RuleArea {
            id: id(11),
            layers: vec!["In1.Cu".into()],
            no_tracks: false,
            no_vias: true,
            outer: vec![[35, 0], [100, 0], [100, 60], [35, 60]],
            holes: Vec::new(),
            name: None,
            extra_polygons: Vec::new(),
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
        RuleArea {
            id: id(12),
            layers: vec!["In1.Cu".into()],
            no_tracks: false,
            no_vias: true,
            outer: vec![[25, 0], [35, 0], [35, 25], [25, 25]],
            holes: Vec::new(),
            name: None,
            extra_polygons: Vec::new(),
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
        RuleArea {
            id: id(13),
            layers: vec!["In1.Cu".into()],
            no_tracks: false,
            no_vias: true,
            outer: vec![[25, 35], [35, 35], [35, 60], [25, 60]],
            holes: Vec::new(),
            name: None,
            extra_polygons: Vec::new(),
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
    ];
    let out = run(i);
    assert_eq!(
        out.status,
        Status::Unroutable,
        "buried via on non-overlapping layer must not falsely connect to pad 3: {:?}",
        out.added_vias
    );
}

#[test]
fn m3_2_probe5_same_net_hole_clearance_same_path_via_is_legal_on_reload() {
    // Challenger 1 Probe 5: 2L board with same_net_hole_clearance_nm=10.
    // Every emitted track must remain legal when emitted vias are reloaded as locked input vias.
    let mut i = base();
    i.layers = vec!["F.Cu".into(), "B.Cu".into()];
    i.bounds = Bounds {
        min: [-20, 0],
        max: [60, 60],
    };
    i.rules = Rules {
        width_nm: 2,
        clearance_nm: 1,
        grid_nm: 10,
        edge_clearance_nm: 1,
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: 10,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    i.pads = vec![
        Pad {
            id: id(1),
            net: 1,
            at: [10, 10],
            layers: vec!["F.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 1 },
            drill: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
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
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
        },
    ];
    // Block via at [10,10], [20,10], [30,10] on F.Cu so it must step to [0,10], place Via@[0,10],
    // and then route around Via@[0,10]'s same-net hole clearance moat on B.Cu.
    i.rule_areas = vec![RuleArea {
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
    }];
    i.project_rules = Some(
        json!({
            "meta": {"version": 3},
            "board": {"design_settings": {"rules": {
                "min_via_diameter": 0.000004,
                "min_through_hole_diameter": 0.000002,
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
    );
    let out = run(i.clone());
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    assert!(!out.added_vias.is_empty());

    // Reload with emitted vias & tracks locked and verify:
    // 1) every emitted track either connects to an emitted via or clears its drill by same_net_hole_clearance_nm;
    // 2) a board with emitted vias locked routes cleanly for any non-connecting track endpoints, and
    //    rejects a shortcut track passing within same_net_hole_clearance_nm without connecting to the via.
    let mut reload = i.clone();
    for (idx, v) in out.added_vias.iter().enumerate() {
        let mut lv = v.clone();
        lv.id = id(100 + idx as u64);
        lv.locked = true;
        reload.vias.push(lv);
    }
    for (idx, t) in out.added_tracks.iter().enumerate() {
        let mut lt = t.clone();
        lt.id = id(200 + idx as u64);
        lt.locked = true;
        reload.tracks.push(lt);
    }
    // Verify adding another component on the reloaded board succeeds cleanly (valid board + valid copper).
    let mut extra = reload.clone();
    extra.rule_areas.clear();
    extra.pads.push(Pad {
        id: id(301),
        net: 1,
        at: [30, 50],
        layers: vec!["B.Cu".into()],
        local_clearance_nm: None,
        shape: Shape::Circle { radius_nm: 1 },
        drill: None,
        zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
        footprint_ref: None,
        pad_number: None,
        fromto_id: None,
    });
    let extra_out = run(extra);
    assert_eq!(extra_out.status, Status::Complete, "{:?}", extra_out.reason);

    // Also verify every non-connecting emitted track clears every same-layer emitted via drill
    // by >= same_net_hole_clearance_nm (and any track inside the moat connects to the via).
    for t in &out.added_tracks {
        for v in &out.added_vias {
            let (ax, ay) = (t.start[0] as i128, t.start[1] as i128);
            let (bx, by) = (t.end[0] as i128, t.end[1] as i128);
            let (px, py) = (v.at[0] as i128, v.at[1] as i128);
            let (abx, aby) = (bx - ax, by - ay);
            let len2 = abx * abx + aby * aby;
            let proj_d2_scaled = if len2 == 0 {
                let dx = px - ax;
                let dy = py - ay;
                (dx * dx + dy * dy, 1i128)
            } else {
                let num = (px - ax) * abx + (py - ay) * aby;
                if num <= 0 {
                    let dx = px - ax;
                    let dy = py - ay;
                    (dx * dx + dy * dy, 1i128)
                } else if num >= len2 {
                    let dx = px - bx;
                    let dy = py - by;
                    (dx * dx + dy * dy, 1i128)
                } else {
                    let cross = (px - ax) * aby - (py - ay) * abx;
                    (cross * cross, len2)
                }
            };
            let conn_r = (t.width_nm / 2 + v.diameter_nm / 2 - 1) as i128;
            let connects = proj_d2_scaled.0 <= conn_r * conn_r * proj_d2_scaled.1;
            if !connects {
                let clr_sum =
                    (t.width_nm + v.drill_nm + 2 * i.rules.same_net_hole_clearance_nm) as i128;
                assert!(
                    4 * proj_d2_scaled.0 >= clr_sum * clr_sum * proj_d2_scaled.1,
                    "track {t:?} violates same_net_hole_clearance against via {v:?}"
                );
            }
        }
    }
}

#[test]
fn m3_2_same_net_hole_clearance_via_to_via_and_pad_copper_symmetry() {
    // 1. Non-connecting same-net via-to-via (locked input via) must be rejected when
    //    same_net_hole_clearance_nm > 0, while connecting same-net via-to-via is allowed.
    let make_2l = |snh: i64| {
        let mut i = base();
        i.layers = vec!["F.Cu".into(), "B.Cu".into()];
        i.bounds = Bounds {
            min: [0, 0],
            max: [100, 60],
        };
        i.rules = Rules {
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
        i.pads = vec![
            Pad {
                id: id(1),
                net: 1,
                at: [10, 30],
                layers: vec!["F.Cu".into()],
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 2 },
                drill: None,
                zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
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
                at: [50, 30],
                layers: vec!["B.Cu".into()],
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 2 },
                drill: None,
                zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
                footprint_ref: None,
                pad_number: None,
                fromto_id: None,
            },
        ];
        i.project_rules = Some(
            json!({
                "meta": {"version": 3},
                "board": {"design_settings": {"rules": {
                    "min_via_diameter": 0.000006,
                    "min_through_hole_diameter": 0.000002,
                    "allow_blind_buried_vias": false,
                    "allow_microvias": false
                }}},
                "net_settings": {
                    "classes": [{
                        "name": "Default",
                        "track_width": 0.000002,
                        "clearance": 0.000001,
                        "via_diameter": 0.000006,
                        "via_drill": 0.000002
                    }]
                }
            })
            .to_string(),
        );
        // Force via ONLY at [30, 30].
        i.rule_areas = vec![
            RuleArea {
                id: id(10),
                layers: vec!["F.Cu".into()],
                no_tracks: false,
                no_vias: true,
                outer: vec![[0, 0], [25, 0], [25, 60], [0, 60]],
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
                outer: vec![[35, 0], [100, 0], [100, 60], [35, 60]],
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
                outer: vec![[25, 0], [35, 0], [35, 25], [25, 25]],
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
                outer: vec![[25, 35], [35, 35], [35, 60], [25, 60]],
                holes: Vec::new(),
                name: None,
                extra_polygons: Vec::new(),
                no_pads: false,
                no_footprints: false,
                no_zone_fills: false,
            },
        ];
        i
    };

    // 1. Non-connecting same-net via-to-via (locked input via on In1.Cu..In2.Cu) must be rejected when
    //    same_net_hole_clearance_nm > 0, while connecting same-net via-to-via is allowed.
    let make_4l_buried_obs = |via3_at: Point| {
        let mut i = make_2l(8);
        i.layers = vec![
            "F.Cu".into(),
            "In1.Cu".into(),
            "In2.Cu".into(),
            "B.Cu".into(),
        ];
        i.project_rules = Some(
            json!({
                "meta": {"version": 3},
                "board": {"design_settings": {"rules": {
                    "min_via_diameter": 0.000006,
                    "min_through_hole_diameter": 0.000002,
                    "allow_blind_buried_vias": true,
                    "allow_microvias": false
                }}},
                "net_settings": {
                    "classes": [{
                        "name": "Default",
                        "track_width": 0.000002,
                        "clearance": 0.000001,
                        "via_diameter": 0.000006,
                        "via_drill": 0.000002
                    }]
                }
            })
            .to_string(),
        );
        i.vias = vec![Via {
            id: id(20),
            net: 1,
            at: via3_at,
            diameter_nm: 6,
            drill_nm: 2,
            start_layer: "In1.Cu".into(),
            end_layer: "In2.Cu".into(),
            kind: ViaKind::BlindBuried,
            locked: true,
        }];
        i
    };

    // Locked same-net via at [30, 20]: dist 10 > connect(5), < snh moat(12) -> Unroutable.
    assert_eq!(run(make_4l_buried_obs([30, 20])).status, Status::Unroutable);

    // Connecting positive control: locked same-net via at [30, 25] (dist 5 <= connect(5))
    // allows Via@[30, 30] even though dist 5 < moat(12), while F.Cu/B.Cu tracks stay clear of In1..In2.
    let out_via_conn = run(make_4l_buried_obs([30, 25]));
    assert_eq!(
        out_via_conn.status,
        Status::Complete,
        "{:?}",
        out_via_conn.reason
    );

    // 2. Non-connecting same-net SMD pad copper to via drill (pad_spatial):
    // SMD pad on B.Cu at [30, 20] with radius 4: dist 10 > connect(3+4-1=6), < drill/2 + 4 + 15 = 20 -> Unroutable.
    let mut pad_rej = make_2l(15);
    pad_rej.pads.push(Pad {
        id: id(22),
        net: 1,
        at: [30, 20],
        layers: vec!["B.Cu".into()],
        local_clearance_nm: None,
        shape: Shape::Circle { radius_nm: 4 },
        drill: None,
        zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
        footprint_ref: None,
        pad_number: None,
        fromto_id: None,
    });
    assert_eq!(run(pad_rej).status, Status::Unroutable);

    // Connecting positive control: SMD pad on B.Cu at [30, 24] (dist 6 <= connect(6)) -> Complete.
    let mut pad_conn = make_2l(15);
    pad_conn.pads.push(Pad {
        id: id(23),
        net: 1,
        at: [30, 24],
        layers: vec!["B.Cu".into()],
        local_clearance_nm: None,
        shape: Shape::Circle { radius_nm: 4 },
        drill: None,
        zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
        footprint_ref: None,
        pad_number: None,
        fromto_id: None,
    });
    let out_pad_conn = run(pad_conn);
    assert_eq!(
        out_pad_conn.status,
        Status::Complete,
        "{:?}",
        out_pad_conn.reason
    );

    // 3. Same-path 2-via via-to-via non-connecting same_net_hole_clearance_nm (prior_vias):
    // 4L board where V1 must be at [20, 30] and V2 must be at [30, 30] (dist 10 > connect(5), < moat(19)).
    let mut same_path = make_2l(15);
    same_path.layers = vec![
        "F.Cu".into(),
        "In1.Cu".into(),
        "In2.Cu".into(),
        "B.Cu".into(),
    ];
    same_path.project_rules = Some(
        json!({
            "meta": {"version": 3},
            "board": {"design_settings": {"rules": {
                "min_via_diameter": 0.000006,
                "min_through_hole_diameter": 0.000002,
                "allow_blind_buried_vias": true,
                "allow_microvias": false
            }}},
            "net_settings": {
                "classes": [{
                    "name": "Default",
                    "track_width": 0.000002,
                    "clearance": 0.000001,
                    "via_diameter": 0.000006,
                    "via_drill": 0.000002
                }]
            }
        })
        .to_string(),
    );
    // Block F.Cu..In1.Cu..In2.Cu..B.Cu direct paths except V1@[20,30](F..In1) and V2@[30,30](In1..B),
    // blocking all other vias on In1.Cu.
    same_path.rule_areas = vec![
        RuleArea {
            id: id(30),
            layers: vec!["F.Cu".into(), "B.Cu".into()],
            no_tracks: false,
            no_vias: true,
            outer: vec![[0, 0], [100, 0], [100, 25], [0, 25]],
            holes: Vec::new(),
            name: None,
            extra_polygons: Vec::new(),
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
        RuleArea {
            id: id(31),
            layers: vec!["F.Cu".into(), "B.Cu".into()],
            no_tracks: false,
            no_vias: true,
            outer: vec![[0, 35], [100, 35], [100, 60], [0, 60]],
            holes: Vec::new(),
            name: None,
            extra_polygons: Vec::new(),
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
        RuleArea {
            id: id(32),
            layers: vec!["F.Cu".into()],
            no_tracks: false,
            no_vias: true,
            outer: vec![[25, 25], [100, 25], [100, 35], [25, 35]],
            holes: Vec::new(),
            name: None,
            extra_polygons: Vec::new(),
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
        RuleArea {
            id: id(33),
            layers: vec!["F.Cu".into()],
            no_tracks: false,
            no_vias: true,
            outer: vec![[0, 25], [15, 25], [15, 35], [0, 35]],
            holes: Vec::new(),
            name: None,
            extra_polygons: Vec::new(),
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
        RuleArea {
            id: id(34),
            layers: vec!["B.Cu".into()],
            no_tracks: false,
            no_vias: true,
            outer: vec![[0, 25], [25, 25], [25, 35], [0, 35]],
            holes: Vec::new(),
            name: None,
            extra_polygons: Vec::new(),
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
        RuleArea {
            id: id(35),
            layers: vec!["B.Cu".into()],
            no_tracks: false,
            no_vias: true,
            outer: vec![[35, 25], [100, 25], [100, 35], [35, 35]],
            holes: Vec::new(),
            name: None,
            extra_polygons: Vec::new(),
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
    ];
    assert_eq!(run(same_path).status, Status::Unroutable);
}

#[test]
fn m3_2_dru_max_bounds_only_disallow_violating_layers_and_via_spans() {
    let make_mm_board = |layers: Vec<&str>, pad_a_layer: &str, pad_b_layer: &str| -> Input {
        let mut p1 = pad(1, 1, [2_000_000, 5_000_000]);
        p1.shape = Shape::Circle { radius_nm: 400_000 };
        p1.layers = vec![pad_a_layer.into()];
        let mut p2 = pad(2, 1, [8_000_000, 5_000_000]);
        p2.shape = Shape::Circle { radius_nm: 400_000 };
        p2.layers = vec![pad_b_layer.into()];
        Input {
            version: 1,
            source_id: "f".repeat(64),
            layer: "F.Cu".into(),
            layers: layers.into_iter().map(String::from).collect(),
            bounds: Bounds {
                min: [0, 0],
                max: [10_000_000, 10_000_000],
            },
            rules: Rules {
                width_nm: 200_000,
                clearance_nm: 150_000,
                grid_nm: 500_000,
                edge_clearance_nm: 200_000,
                hole_clearance_nm: 0,
                same_net_hole_clearance_nm: 0,
                solder_mask_expansion_nm: 0,
                solder_mask_min_width_nm: 0,
                solder_mask_to_copper_clearance_nm: 0,
            },
            nets: vec![Net {
                id: 1,
                name: "SIG".into(),
            }],
            pads: vec![p1, p2],
            tracks: Vec::new(),
            vias: Vec::new(),
            rule_areas: Vec::new(),
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
        }
    };

    let pro_2l = json!({
        "meta": {"version": 3},
        "board": {"design_settings": {"rules": {
            "min_track_width": 0.1,
            "min_clearance": 0.1,
            "min_via_diameter": 0.4,
            "min_through_hole_diameter": 0.2,
            "allow_blind_buried_vias": false,
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

    // 1. 2L F.Cu-only pads with Default via_diameter=0.6mm and (constraint via_diameter (max 0.5mm)) -> Complete on F.Cu
    let mut s1 = make_mm_board(vec!["F.Cu", "B.Cu"], "F.Cu", "F.Cu");
    s1.project_rules = Some(pro_2l.clone());
    s1.custom_rules =
        Some("(version 1)\n(rule \"max_via\" (constraint via_diameter (max 0.5mm)))\n".into());
    let r1 = run(s1);
    assert_eq!(r1.status, Status::Complete, "s1 failed: {r1:?}");
    assert!(r1.added_vias.is_empty());
    assert!(r1.added_tracks.iter().all(|t| t.layer == "F.Cu"));

    // 2. 4L F.Cu -> In1.Cu with allow_microvias=true, microvia_diameter=0.4mm <= 0.5mm, via_diameter=0.6mm > 0.5mm,
    // and (constraint via_diameter (max 0.5mm)) -> Complete using Micro via
    let mut s2 = make_mm_board(vec!["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"], "F.Cu", "In1.Cu");
    s2.project_rules = Some(
        json!({
            "meta": {"version": 3},
            "board": {"design_settings": {"rules": {
                "min_track_width": 0.1,
                "min_clearance": 0.1,
                "min_via_diameter": 0.4,
                "min_through_hole_diameter": 0.2,
                "min_microvia_diameter": 0.3,
                "min_microvia_drill": 0.15,
                "allow_blind_buried_vias": false,
                "allow_microvias": true
            }}},
            "net_settings": {"classes": [{
                "name": "Default",
                "track_width": 0.2,
                "clearance": 0.15,
                "via_diameter": 0.6,
                "via_drill": 0.3,
                "microvia_diameter": 0.4,
                "microvia_drill": 0.2
            }]}
        })
        .to_string(),
    );
    s2.custom_rules =
        Some("(version 1)\n(rule \"cap_via\" (constraint via_diameter (max 0.5mm)))\n".into());
    let r2 = run(s2.clone());
    assert_eq!(r2.status, Status::Complete, "s2 failed: {r2:?}");
    assert_eq!(r2.added_vias.len(), 1);
    assert_eq!(r2.added_vias[0].kind, ViaKind::Micro);
    assert_eq!(r2.added_vias[0].diameter_nm, 400_000);
    // Verify emitted via reloads cleanly through Input::validate()
    let mut s2_reload = s2;
    s2_reload.tracks.extend(r2.added_tracks);
    s2_reload.vias.extend(r2.added_vias);
    assert_eq!(run(s2_reload).status, Status::Complete);

    // 3. 2L F.Cu-only pads with Default track_width=0.20mm and (layer "B.Cu") (constraint track_width (max 0.15mm)) -> Complete on F.Cu
    let mut s3 = make_mm_board(vec!["F.Cu", "B.Cu"], "F.Cu", "F.Cu");
    s3.project_rules = Some(pro_2l.clone());
    s3.custom_rules = Some(
        "(version 1)\n(rule \"thin_bcu\" (layer \"B.Cu\") (constraint track_width (max 0.15mm)))\n"
            .into(),
    );
    let r3 = run(s3);
    assert_eq!(r3.status, Status::Complete, "s3 failed: {r3:?}");
    assert!(r3.added_tracks.iter().all(|t| t.layer == "F.Cu"));

    // 4. Control: genuinely unroutable when cross-layer route needs a via and ALL via types violate max
    let mut s4 = make_mm_board(vec!["F.Cu", "B.Cu"], "F.Cu", "B.Cu");
    s4.project_rules = Some(pro_2l);
    s4.custom_rules =
        Some("(version 1)\n(rule \"cap_via\" (constraint via_diameter (max 0.5mm)))\n".into());
    assert_eq!(run(s4).status, Status::Unroutable);
}

#[test]
fn m3_2_effective_rules_invalid_via_and_microvia_dims_rejected() {
    let mut base_inp = base();
    base_inp.layers = vec![
        "F.Cu".into(),
        "In1.Cu".into(),
        "In2.Cu".into(),
        "B.Cu".into(),
    ];
    base_inp.bounds = Bounds {
        min: [0, 0],
        max: [10_000_000, 10_000_000],
    };
    base_inp.rules = Rules {
        width_nm: 200_000,
        clearance_nm: 150_000,
        grid_nm: 500_000,
        edge_clearance_nm: 200_000,
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    base_inp.pads = vec![
        Pad {
            id: id(1),
            net: 1,
            at: [2_000_000, 5_000_000],
            shape: Shape::Circle { radius_nm: 400_000 },
            drill: None,
            layers: vec!["F.Cu".into()],
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
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
            at: [8_000_000, 5_000_000],
            shape: Shape::Circle { radius_nm: 400_000 },
            drill: None,
            layers: vec!["B.Cu".into()],
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
        },
    ];
    let eff = kicad_route_engine::rules::EffectiveRules::from_uniform_rules(
        base_inp.rules,
        &base_inp.nets,
    );

    let mut zero_drill = base_inp.clone();
    let mut eff1 = eff.clone();
    eff1.net_rules.get_mut(&1).unwrap().via_drill_nm = 0;
    zero_drill.effective_rules = Some(eff1);
    assert_eq!(run(zero_drill).status, Status::Invalid);

    let mut drill_eq_diam = base_inp.clone();
    let mut eff2 = eff.clone();
    eff2.net_rules.get_mut(&1).unwrap().via_drill_nm = 600_000;
    eff2.net_rules.get_mut(&1).unwrap().via_diameter_nm = 600_000;
    drill_eq_diam.effective_rules = Some(eff2);
    assert_eq!(run(drill_eq_diam).status, Status::Invalid);

    let mut zero_micro = base_inp;
    let mut eff3 = eff;
    eff3.allow_microvias = true;
    eff3.net_rules.get_mut(&1).unwrap().microvia_diameter_nm = 0;
    eff3.net_rules.get_mut(&1).unwrap().microvia_drill_nm = 0;
    zero_micro.effective_rules = Some(eff3);
    assert_eq!(run(zero_micro).status, Status::Invalid);
}

fn m3_3_zone(
    n: u64,
    net: u32,
    layer: &str,
    outer: Vec<Point>,
    filled: bool,
    refillable: bool,
) -> Zone {
    let layer_fills = if filled {
        vec![ZoneLayerFill {
            layer: layer.into(),
            polygons: vec![OutlinePolygon {
                outer: outer.clone(),
                holes: Vec::new(),
            }],
        }]
    } else {
        Vec::new()
    };
    Zone {
        id: id(n),
        name: None,
        net,
        layers: vec![layer.into()],
        outer,
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        priority: 0,
        local_clearance_nm: 0,
        min_thickness_nm: 200_000,
        connection: ZoneConnection::Thermal,
        thermal_gap_nm: 200_000,
        thermal_spoke_width_nm: 200_000,
        island_removal: ZoneIslandRemoval::Always,
        island_area_min_nm2: 0,
        fill_mode: ZoneFillMode::Polygon,
        filled,
        refillable,
        locked: false,
        layer_fills,
    }
}

#[test]
fn m3_3_rule_area_five_flags_extra_polygons_and_duplicate_layer_refusal() {
    let mut inp = base();
    inp.bounds = Bounds {
        min: [0, 0],
        max: [80, 60],
    };
    inp.pads = vec![pad(1, 1, [10, 10]), pad(2, 1, [70, 10])];
    // Extra polygon blocks direct horizontal line y=10 between x=30..50 while outer sits away at y=40..50
    inp.rule_areas = vec![
        RuleArea {
            id: id(50),
            name: Some("track_ko".into()),
            layers: vec!["F.Cu".into()],
            outer: vec![[30, 40], [50, 40], [50, 50], [30, 50]],
            holes: Vec::new(),
            extra_polygons: vec![OutlinePolygon {
                outer: vec![[30, 5], [50, 5], [50, 20], [30, 20]],
                holes: Vec::new(),
            }],
            no_tracks: true,
            no_vias: false,
            no_pads: true,
            no_footprints: true,
            no_zone_fills: true,
        },
        RuleArea {
            id: id(51),
            name: Some("pour_only_ko".into()),
            layers: vec!["F.Cu".into()],
            outer: vec![[0, 45], [10, 45], [10, 55], [0, 55]],
            holes: Vec::new(),
            extra_polygons: Vec::new(),
            no_tracks: false,
            no_vias: false,
            no_pads: false,
            no_footprints: false,
            no_zone_fills: true,
        },
    ];
    let out = run(inp.clone());
    assert_eq!(out.status, Status::Complete);
    assert!(out
        .added_tracks
        .iter()
        .any(|t| t.start[1] > 20 || t.end[1] > 20));

    let mut dup_layer = inp;
    dup_layer.rule_areas[0].layers = vec!["F.Cu".into(), "F.Cu".into()];
    assert_eq!(run(dup_layer).status, Status::Invalid);
}

#[test]
fn m3_3_via_only_start_seed_inside_no_tracks_keepout() {
    let mut inp = base();
    inp.layers = vec!["F.Cu".into(), "B.Cu".into()];
    inp.bounds = Bounds {
        min: [0, 0],
        max: [10_000_000, 8_000_000],
    };
    inp.rules = Rules {
        width_nm: 200_000,
        clearance_nm: 150_000,
        grid_nm: 500_000,
        edge_clearance_nm: 0,
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    let mut p1 = pad(1, 1, [2_000_000, 4_000_000]);
    p1.shape = Shape::Circle { radius_nm: 400_000 };
    p1.layers = vec!["F.Cu".into()];
    let mut p2 = pad(2, 1, [8_000_000, 4_000_000]);
    p2.shape = Shape::Circle { radius_nm: 200_000 };
    p2.layers = vec!["B.Cu".into()];
    inp.pads = vec![p1, p2];
    inp.rule_areas = vec![RuleArea {
        id: id(60),
        name: None,
        layers: vec!["F.Cu".into()],
        outer: vec![
            [1_000_000, 3_000_000],
            [3_000_000, 3_000_000],
            [3_000_000, 5_000_000],
            [1_000_000, 5_000_000],
        ],
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        no_tracks: true,
        no_vias: false,
        no_pads: false,
        no_footprints: false,
        no_zone_fills: false,
    }];
    let out = run(inp.clone());
    assert_eq!(
        out.status,
        Status::Complete,
        "via-only start seed failed: {out:?}"
    );
    assert_eq!(
        out.added_vias.len(),
        1,
        "unexpected vias={:?} tracks={:?}",
        out.added_vias,
        out.added_tracks
    );
    assert_eq!(out.added_vias[0].at, [2_000_000, 4_000_000]);
    assert!(out.added_tracks.iter().all(|t| t.layer == "B.Cu"));

    let mut both_ko = inp;
    both_ko.rule_areas[0].no_vias = true;
    assert_eq!(run(both_ko).status, Status::Unroutable);
}

#[test]
fn m3_3_zone_fill_connects_pad_zero_width_polygon_rect_and_box_exact() {
    let fill = vec![
        [0, 0],
        [1_000_000, 0],
        [1_000_000, 1_000_000],
        [0, 1_000_000],
    ];
    let hole = vec![vec![
        [400_000, 400_000],
        [600_000, 400_000],
        [600_000, 600_000],
        [400_000, 600_000],
    ]];

    let mut rect_pad = pad(1, 1, [100_000, 100_000]);
    rect_pad.shape = Shape::Rect {
        half_size_nm: [50_000, 50_000],
    };
    assert!(zone_fill_connects_pad(&fill, &[], &rect_pad));
    // Inside hole (gap >= 0 from fill) -> false
    rect_pad.at = [500_000, 500_000];
    assert!(!zone_fill_connects_pad(&fill, &hole, &rect_pad));
    // Separated outside -> false
    rect_pad.at = [1_200_000, 500_000];
    assert!(!zone_fill_connects_pad(&fill, &[], &rect_pad));

    let mut box_pad = pad(2, 1, [200_000, 200_000]);
    box_pad.shape = Shape::BoxExact {
        center_2x: [400_000, 400_000],
        half_size_2x: [100_001, 100_001],
        radius_2x: 100_001,
    };
    assert!(zone_fill_connects_pad(&fill, &[], &box_pad));
    box_pad.shape = Shape::BoxExact {
        center_2x: [2_400_000, 1_000_000],
        half_size_2x: [100_001, 100_001],
        radius_2x: 100_001,
    };
    assert!(!zone_fill_connects_pad(&fill, &[], &box_pad));

    let mut poly_pad = pad(3, 1, [200_000, 200_000]);
    poly_pad.shape = Shape::ConvexPolygon {
        vertices: vec![
            [-50_000, -50_000],
            [50_000, -50_000],
            [50_000, 50_000],
            [-50_000, 50_000],
        ],
        radius_nm: 0,
    };
    assert!(zone_fill_connects_pad(&fill, &[], &poly_pad));
}

#[test]
fn m3_3_zone_fill_connections_and_unfilled_refusal() {
    let make_board = |z_conn: ZoneConnection,
                      p_conn: PadZoneConnection,
                      filled: bool,
                      drill: Option<DrillShape>| {
        let mut inp = base();
        inp.bounds = Bounds {
            min: [0, 0],
            max: [10_000_000, 10_000_000],
        };
        inp.rules = Rules {
            width_nm: 200_000,
            clearance_nm: 200_000,
            grid_nm: 500_000,
            edge_clearance_nm: 0,
            hole_clearance_nm: 0,
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
        };
        let mut p1 = pad(1, 1, [2_000_000, 5_000_000]);
        p1.shape = Shape::Rect {
            half_size_nm: [500_000, 500_000],
        };
        p1.drill = drill;
        p1.zone_connection = p_conn;
        let mut p2 = pad(2, 1, [8_000_000, 5_000_000]);
        p2.shape = Shape::Rect {
            half_size_nm: [500_000, 500_000],
        };
        p2.drill = drill;
        p2.zone_connection = p_conn;
        inp.pads = vec![p1, p2];
        let mut z = m3_3_zone(
            70,
            1,
            "F.Cu",
            vec![
                [1_000_000, 1_000_000],
                [9_000_000, 1_000_000],
                [9_000_000, 9_000_000],
                [1_000_000, 9_000_000],
            ],
            filled,
            true,
        );
        z.connection = z_conn;
        inp.zones = vec![z];
        inp
    };

    let thermal_ok = run(make_board(
        ZoneConnection::Thermal,
        PadZoneConnection::Inherited,
        true,
        None,
    ));
    assert_eq!(thermal_ok.status, Status::Complete);
    assert!(thermal_ok.added_tracks.is_empty());

    let solid_ok = run(make_board(
        ZoneConnection::Solid,
        PadZoneConnection::Inherited,
        true,
        None,
    ));
    assert_eq!(solid_ok.status, Status::Complete);
    assert!(solid_ok.added_tracks.is_empty());

    let zone_none = run(make_board(
        ZoneConnection::None,
        PadZoneConnection::Inherited,
        true,
        None,
    ));
    assert_eq!(zone_none.status, Status::Complete);
    assert!(!zone_none.added_tracks.is_empty());

    let pad_none = run(make_board(
        ZoneConnection::Thermal,
        PadZoneConnection::None,
        true,
        None,
    ));
    assert_eq!(pad_none.status, Status::Complete);
    assert!(!pad_none.added_tracks.is_empty());

    // ThtThermal on SMD resolves to Solid per KiCad semantics; on THT resolves to Thermal
    let tht_smd = run(make_board(
        ZoneConnection::None,
        PadZoneConnection::ThtThermal,
        true,
        None,
    ));
    assert_eq!(tht_smd.status, Status::Complete);
    assert!(tht_smd.added_tracks.is_empty());

    let tht_pin = run(make_board(
        ZoneConnection::None,
        PadZoneConnection::ThtThermal,
        true,
        Some(DrillShape::Circle {
            diameter_nm: 300_000,
        }),
    ));
    assert_eq!(tht_pin.status, Status::Complete);
    assert!(tht_pin.added_tracks.is_empty());

    let unfilled = run(make_board(
        ZoneConnection::Thermal,
        PadZoneConnection::Inherited,
        false,
        None,
    ));
    assert_eq!(unfilled.status, Status::Complete);
    assert!(!unfilled.added_tracks.is_empty());
}

#[test]
fn m3_3_refillable_vs_non_refillable_zone_clearance_and_spoke_corridors() {
    let make_foreign_board = |refillable: bool| {
        let mut inp = base();
        inp.bounds = Bounds {
            min: [0, 0],
            max: [12_000_000, 10_000_000],
        };
        inp.rules = Rules {
            width_nm: 200_000,
            clearance_nm: 200_000,
            grid_nm: 500_000,
            edge_clearance_nm: 0,
            hole_clearance_nm: 0,
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
        };
        inp.nets = vec![
            Net {
                id: 1,
                name: "SIG".into(),
            },
            Net {
                id: 2,
                name: "GND".into(),
            },
        ];
        let mut p1 = pad(1, 1, [1_000_000, 5_000_000]);
        p1.shape = Shape::Circle { radius_nm: 300_000 };
        let mut p2 = pad(2, 1, [11_000_000, 5_000_000]);
        p2.shape = Shape::Circle { radius_nm: 300_000 };
        inp.pads = vec![p1, p2];
        // Zone spans the full y range [0..10mm] across x=[3mm..9mm]
        inp.zones = vec![m3_3_zone(
            80,
            2,
            "F.Cu",
            vec![
                [3_000_000, 0],
                [9_000_000, 0],
                [9_000_000, 10_000_000],
                [3_000_000, 10_000_000],
            ],
            true,
            refillable,
        )];
        inp
    };

    assert_eq!(run(make_foreign_board(false)).status, Status::Unroutable);
    assert_eq!(run(make_foreign_board(true)).status, Status::Complete);

    // Refillable zone with a net-2 thermal pad at [6mm, 5mm] creates spoke corridors that block a foreign track at y=5mm
    let mut spoke_board = make_foreign_board(true);
    let mut gnd_pad = pad(3, 2, [6_000_000, 5_000_000]);
    gnd_pad.shape = Shape::Rect {
        half_size_nm: [600_000, 600_000],
    };
    spoke_board.pads.push(gnd_pad);
    // Constrain y strictly to 4.5mm..5.5mm using track keepouts above & below so foreign track must cross east/west spoke corridors
    spoke_board.rule_areas = vec![
        RuleArea {
            id: id(81),
            name: None,
            layers: vec!["F.Cu".into()],
            outer: vec![
                [3_000_000, 0],
                [9_000_000, 0],
                [9_000_000, 4_200_000],
                [3_000_000, 4_200_000],
            ],
            holes: Vec::new(),
            extra_polygons: Vec::new(),
            no_tracks: true,
            no_vias: true,
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
        RuleArea {
            id: id(82),
            name: None,
            layers: vec!["F.Cu".into()],
            outer: vec![
                [3_000_000, 5_800_000],
                [9_000_000, 5_800_000],
                [9_000_000, 10_000_000],
                [3_000_000, 10_000_000],
            ],
            holes: Vec::new(),
            extra_polygons: Vec::new(),
            no_tracks: true,
            no_vias: true,
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
    ];
    let spoke_out = run(spoke_board);
    assert_ne!(spoke_out.status, Status::Complete);
}

#[test]
fn m3_3_dru_zone_clearance_hatched_unsupported_and_input1_sha256_compat() {
    let mut inp = base();
    inp.bounds = Bounds {
        min: [0, 0],
        max: [10_000_000, 10_000_000],
    };
    inp.rules = Rules {
        width_nm: 200_000,
        clearance_nm: 150_000,
        grid_nm: 500_000,
        edge_clearance_nm: 0,
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    inp.nets = vec![
        Net {
            id: 1,
            name: "SIG".into(),
        },
        Net {
            id: 2,
            name: "GND".into(),
        },
    ];
    let mut p1 = pad(1, 1, [2_000_000, 5_000_000]);
    p1.shape = Shape::Circle { radius_nm: 300_000 };
    let mut p2 = pad(2, 1, [8_000_000, 5_000_000]);
    p2.shape = Shape::Circle { radius_nm: 300_000 };
    inp.pads = vec![p1, p2];
    // Non-refillable zone at y=[0..4.5mm]: track at y=5mm (width 200um => edge at 4.9mm, gap=400um) clears 150um default but fails 500um Zone DRU!
    inp.zones = vec![m3_3_zone(
        90,
        2,
        "F.Cu",
        vec![
            [3_500_000, 0],
            [6_500_000, 0],
            [6_500_000, 4_500_000],
            [3_500_000, 4_500_000],
        ],
        true,
        false,
    )];
    let ok_default = run(inp.clone());
    assert_eq!(ok_default.status, Status::Complete);
    assert!(ok_default
        .added_tracks
        .iter()
        .all(|t| t.start[1] == 5_000_000));

    let mut with_dru = inp.clone();
    with_dru.custom_rules = Some(
        "(version 1)\n(rule \"zone_clr\" (condition \"A.Type == 'Zone' && B.Type == 'Track'\") (constraint clearance (min 0.5mm)))\n"
            .into(),
    );
    let routed_around = run(with_dru);
    assert_eq!(routed_around.status, Status::Complete);
    assert!(routed_around
        .added_tracks
        .iter()
        .any(|t| t.start[1] > 5_000_000));

    // Hatched fill fails closed as Unsupported
    let mut hatched = inp.clone();
    hatched.zones[0].fill_mode = ZoneFillMode::Hatch;
    assert_eq!(run(hatched).status, Status::Unsupported);

    // input/1 backward compatibility: omitting all M3.3 fields gives identical input_request_sha256
    let legacy_json = json!({
        "version": 1,
        "source_id": "a".repeat(64),
        "layer": "F.Cu",
        "bounds": {"min": [0, 0], "max": [60, 60]},
        "rules": {"width_nm": 2, "clearance_nm": 1, "grid_nm": 10, "edge_clearance_nm": 0},
        "nets": [{"id": 1, "name": "A"}],
        "pads": [
            {"id": id(1), "net": 1, "at": [10, 10], "shape": {"kind": "circle", "radius_nm": 1}},
            {"id": id(2), "net": 1, "at": [50, 10], "shape": {"kind": "circle", "radius_nm": 1}}
        ],
        "tracks": [],
        "max_expansions": 10000
    });
    let parsed_legacy: Input = serde_json::from_str(&legacy_json.to_string()).unwrap();
    let v_legacy = run(parsed_legacy);
    let v_explicit = run(base());
    assert_eq!(v_legacy.request_sha256, v_explicit.request_sha256);
    let explicit_val = serde_json::to_value(base()).unwrap();
    assert!(explicit_val.get("zones").is_none());

    // Work determinism and rollback on zone + multi-net board
    let mut det = inp;
    det.zones[0].refillable = true;
    let mut p3 = pad(3, 2, [4_000_000, 2_000_000]);
    p3.shape = Shape::Circle { radius_nm: 300_000 };
    let mut p4 = pad(4, 2, [6_000_000, 2_000_000]);
    p4.shape = Shape::Circle { radius_nm: 300_000 };
    det.pads.push(p3);
    det.pads.push(p4);
    let mut e1 = Engine::new(det.clone()).unwrap();
    let r1 = e1.route_all().unwrap();
    let mut e2 = Engine::new(det).unwrap();
    let r2 = e2.route_all().unwrap();
    assert_eq!(r1, r2);
    assert_eq!(e1.work_used(), e2.work_used());
}

#[test]
fn m3_3_off_grid_pad_2layer_does_not_emit_unconnected_track_from_via_only_seed() {
    let mut inp = base();
    inp.layers = vec!["F.Cu".into(), "B.Cu".into()];
    inp.bounds = Bounds {
        min: [-1_000_000, -1_000_000],
        max: [4_000_000, 1_000_000],
    };
    inp.rules = Rules {
        width_nm: 200_000,
        clearance_nm: 150_000,
        grid_nm: 500_000,
        edge_clearance_nm: 0,
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    let mut p1 = pad(1, 1, [200_000, 0]);
    p1.shape = Shape::Circle { radius_nm: 100_000 };
    let mut p2 = pad(2, 1, [2_000_000, 0]);
    p2.shape = Shape::Circle { radius_nm: 100_000 };
    inp.pads = vec![p1, p2];
    let out = run(inp.clone());
    assert_eq!(
        out.status,
        Status::Complete,
        "2L off-grid pad routing regressed: {out:?}"
    );
    assert!(!out.added_tracks.is_empty());
    assert!(out
        .added_tracks
        .iter()
        .any(|t| track_connects_pad(t, &inp.pads[0])));
    assert!(
        out.added_tracks
            .iter()
            .any(|t| track_connects_pad(t, &inp.pads[1]))
            || out
                .added_vias
                .iter()
                .any(|v| via_connects_pad(v, &inp.pads[1])),
        "route did not reach pad 2: tracks={:?} vias={:?}",
        out.added_tracks,
        out.added_vias
    );

    let mut reload = inp.clone();
    reload.tracks.extend(out.added_tracks);
    reload.vias.extend(out.added_vias);
    let out2 = run(reload);
    assert_eq!(out2.status, Status::Complete);
    assert!(out2.added_tracks.is_empty());
    assert!(out2.added_vias.is_empty());

    // Also verify [200_000, 0] -> [2_000_000, 0] with via_cost=4 connects both pads via added_tracks with 0 vias
    let mut vc_inp = inp.clone();
    let mut eff =
        kicad_route_engine::rules::EffectiveRules::from_uniform_rules(vc_inp.rules, &vc_inp.nets);
    eff.via_cost = 4;
    vc_inp.effective_rules = Some(eff);
    let vc_out = run(vc_inp.clone());
    assert_eq!(vc_out.status, Status::Complete);
    assert!(vc_out.added_vias.is_empty());
    assert!(vc_out
        .added_tracks
        .iter()
        .any(|t| track_connects_pad(t, &vc_inp.pads[0])));
    assert!(vc_out
        .added_tracks
        .iter()
        .any(|t| track_connects_pad(t, &vc_inp.pads[1])));

    // Also verify [200_000, 0] -> [2_200_000, 0] connects both pads directly on F.Cu
    let mut direct_inp = inp;
    direct_inp.pads[1].at = [2_200_000, 0];
    let direct_out = run(direct_inp.clone());
    assert_eq!(direct_out.status, Status::Complete);
    assert!(direct_out
        .added_tracks
        .iter()
        .any(|t| track_connects_pad(t, &direct_inp.pads[0])));
    assert!(direct_out
        .added_tracks
        .iter()
        .any(|t| track_connects_pad(t, &direct_inp.pads[1])));
}

#[test]
fn m3_3_via_only_anchor_outside_keepout_places_via_before_track() {
    let mut inp = base();
    inp.layers = vec!["F.Cu".into(), "B.Cu".into()];
    inp.bounds = Bounds {
        min: [-1_000_000, -1_000_000],
        max: [4_000_000, 1_000_000],
    };
    inp.rules = Rules {
        width_nm: 200_000,
        clearance_nm: 150_000,
        grid_nm: 500_000,
        edge_clearance_nm: 0,
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    let mut p1 = pad(1, 1, [200_000, 0]);
    p1.layers = vec!["F.Cu".into()];
    p1.shape = Shape::Circle { radius_nm: 100_000 };
    let mut p2 = pad(2, 1, [2_000_000, 0]);
    p2.layers = vec!["B.Cu".into()];
    p2.shape = Shape::Circle { radius_nm: 100_000 };
    inp.pads = vec![p1, p2];
    inp.rule_areas = vec![RuleArea {
        id: id(95),
        name: None,
        layers: vec!["F.Cu".into()],
        outer: vec![
            [-500_000, -500_000],
            [350_000, -500_000],
            [350_000, 500_000],
            [-500_000, 500_000],
        ],
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        no_tracks: true,
        no_vias: false,
        no_pads: false,
        no_footprints: false,
        no_zone_fills: false,
    }];
    // Also add microvia smaller than through-via so microvia at [500_000, 0] does NOT touch p1
    inp.project_rules = Some(
        serde_json::json!({
            "meta": {"version": 3},
            "board": {
                "design_settings": {
                    "rules": {
                        "allow_microvias": true,
                        "microvia_diameter_nm": 300_000,
                        "microvia_drill_nm": 100_000,
                        "via_diameter_nm": 600_000,
                        "via_drill_nm": 300_000
                    }
                }
            }
        })
        .to_string(),
    );
    let out = run(inp.clone());
    assert_eq!(
        out.status,
        Status::Complete,
        "keepout anchor failed: {out:?}"
    );
    assert_eq!(out.added_vias.len(), 1, "expected 1 via: {out:?}");
    assert_eq!(out.added_vias[0].at, [500_000, 0]);
    assert_eq!(out.added_vias[0].kind, ViaKind::Through);
    assert!(
        out.added_tracks.iter().all(|t| t.layer == "B.Cu"),
        "unexpected non-B.Cu track: {:?}",
        out.added_tracks
    );
    assert!(out
        .added_tracks
        .iter()
        .any(|t| track_connects_via(t, &out.added_vias[0])));
    assert!(out
        .added_tracks
        .iter()
        .any(|t| track_connects_pad(t, &inp.pads[1])));
}

#[test]
fn m3_3_zone_fill_connects_pad_odd_box_exact_and_thin_capsule_boundaries() {
    let fill_x_le_0 = vec![[-100, -100], [0, -100], [0, 100], [-100, 100]];
    let fill_x_le_1 = vec![[-100, -100], [1, -100], [1, 100], [-100, 100]];
    let fill_all = vec![[-100, -100], [100, -100], [100, 100], [-100, 100]];

    let mut box_1x1 = pad(1, 1, [0, 0]);
    box_1x1.shape = Shape::BoxExact {
        center_2x: [1, 1],
        half_size_2x: [1, 1],
        radius_2x: 0,
    };
    assert!(!zone_fill_connects_pad(&fill_x_le_0, &[], &box_1x1));
    assert!(!zone_fill_connects_pad(&fill_all, &[], &box_1x1));

    let mut box_2x2 = pad(2, 1, [0, 0]);
    box_2x2.shape = Shape::BoxExact {
        center_2x: [1, 1],
        half_size_2x: [2, 2],
        radius_2x: 0,
    };
    assert!(!zone_fill_connects_pad(&fill_x_le_0, &[], &box_2x2));
    assert!(zone_fill_connects_pad(&fill_x_le_1, &[], &box_2x2));

    let mut box_2x2_round = pad(3, 1, [0, 0]);
    box_2x2_round.shape = Shape::BoxExact {
        center_2x: [1, 1],
        half_size_2x: [2, 2],
        radius_2x: 2,
    };
    assert!(!zone_fill_connects_pad(&fill_x_le_0, &[], &box_2x2_round));
    assert!(zone_fill_connects_pad(&fill_all, &[], &box_2x2_round));

    let mut thin_cap = pad(4, 1, [0, 0]);
    thin_cap.shape = Shape::Capsule {
        start_nm: [0, 0],
        end_nm: [10, 0],
        width_nm: 1,
    };
    assert!(!zone_fill_connects_pad(&fill_all, &[], &thin_cap));

    let mut thin_cap_2 = pad(5, 1, [0, 0]);
    thin_cap_2.shape = Shape::Capsule {
        start_nm: [0, 0],
        end_nm: [10, 0],
        width_nm: 2,
    };
    assert!(zone_fill_connects_pad(&fill_all, &[], &thin_cap_2));

    let mut zero_circle = pad(6, 1, [0, 0]);
    zero_circle.shape = Shape::Circle { radius_nm: 0 };
    assert!(!zone_fill_connects_pad(&fill_all, &[], &zero_circle));
}

#[test]
fn m3_3_spoke_corridor_avoids_foreign_hole_and_graphic() {
    let mut inp = base();
    inp.bounds = Bounds {
        min: [0, 0],
        max: [12_000_000, 10_000_000],
    };
    inp.rules = Rules {
        width_nm: 200_000,
        clearance_nm: 200_000,
        grid_nm: 500_000,
        edge_clearance_nm: 0,
        hole_clearance_nm: 200_000,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    inp.nets = vec![
        Net {
            id: 1,
            name: "SIG".into(),
        },
        Net {
            id: 2,
            name: "GND".into(),
        },
    ];
    let mut p1 = pad(1, 1, [1_000_000, 6_000_000]);
    p1.shape = Shape::Circle { radius_nm: 300_000 };
    let mut p2 = pad(2, 1, [11_000_000, 6_000_000]);
    p2.shape = Shape::Circle { radius_nm: 300_000 };
    let mut gnd_pad = pad(3, 2, [6_000_000, 5_000_000]);
    gnd_pad.shape = Shape::Rect {
        half_size_nm: [600_000, 600_000],
    };
    inp.pads = vec![p1, p2, gnd_pad];
    inp.zones = vec![m3_3_zone(
        80,
        2,
        "F.Cu",
        vec![
            [3_000_000, 0],
            [9_000_000, 0],
            [9_000_000, 10_000_000],
            [3_000_000, 10_000_000],
        ],
        true,
        true,
    )];
    // Block k=0 (-y = [6_000_000, 4_200_000]) with an NPTH hole and k=1 (-x = [5_200_000, 5_000_000])
    // with a foreign graphic, forcing min_resolved_spokes=2 to protect k=2 (+y = south, up to y=6_005_000)
    // and k=3 (+x = east). Force SIG through y=6_000_000 (the k=2 spoke corridor) via keepouts.
    inp.holes = vec![Hole {
        id: id(83),
        net: 0,
        at: [6_000_000, 4_000_000],
        shape: DrillShape::Circle {
            diameter_nm: 300_000,
        },
    }];
    inp.graphics = vec![CopperGraphic {
        id: id(84),
        net: 1,
        layer: "F.Cu".into(),
        shape: GraphicShape::Segment {
            start: [5_000_000, 4_700_000],
            end: [5_000_000, 5_300_000],
            width_nm: 200_000,
        },
    }];
    inp.rule_areas = vec![
        RuleArea {
            id: id(85),
            name: None,
            layers: vec!["F.Cu".into()],
            outer: vec![
                [3_000_000, 0],
                [9_000_000, 0],
                [9_000_000, 5_800_000],
                [3_000_000, 5_800_000],
            ],
            holes: Vec::new(),
            extra_polygons: Vec::new(),
            no_tracks: true,
            no_vias: true,
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
        RuleArea {
            id: id(86),
            name: None,
            layers: vec!["F.Cu".into()],
            outer: vec![
                [3_000_000, 6_200_000],
                [9_000_000, 6_200_000],
                [9_000_000, 10_000_000],
                [3_000_000, 10_000_000],
            ],
            holes: Vec::new(),
            extra_polygons: Vec::new(),
            no_tracks: true,
            no_vias: true,
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        },
    ];
    // With k=0 & k=1 skipped due to hole/graphic conflict, k=2 (+y spoke) IS protected and blocks SIG at y=6_000_000.
    assert_eq!(run(inp.clone()).status, Status::Unroutable);
    // Without the hole & graphic, k=0 and k=1 take the 2 slots so k=2 is unprotected and SIG routes at y=6_000_000.
    inp.holes.clear();
    inp.graphics.clear();
    assert_eq!(run(inp).status, Status::Complete);
}

#[test]
fn m5_2_1_zero_to_cu_mask_apertures_block_cross_net_bridges_tracks_and_vias() {
    // Challenge 1: when solder_mask_to_copper_clearance_nm == 0, an F.Mask or B.Mask
    // aperture exposing GND (net 2) or generated copper must block SIG (net 1) tracks & vias.
    let mut i = base();
    i.layers = vec!["F.Cu".into(), "B.Cu".into()];
    i.bounds = Bounds {
        min: [0, 0],
        max: [20_000_000, 20_000_000],
    };
    i.rules = Rules {
        width_nm: 250_000,
        clearance_nm: 250_000,
        grid_nm: 250_000,
        edge_clearance_nm: 500_000,
        hole_clearance_nm: 250_000,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    i.nets.push(Net {
        id: 2,
        name: "GND".into(),
    });
    i.pads[0].at = [5_000_000, 10_000_000];
    i.pads[0].layers = vec!["F.Cu".into()];
    i.pads[1].at = [15_000_000, 10_000_000];
    i.pads[1].layers = vec!["F.Cu".into()];
    i.pads.push(Pad {
        id: id(10),
        net: 2,
        at: [10_000_000, 12_000_000],
        shape: Shape::Rect {
            half_size_nm: [500_000, 500_000],
        },
        drill: None,
        layers: vec!["F.Cu".into()],
        local_clearance_nm: None,
        zone_connection: Default::default(),
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
        footprint_ref: None,
        pad_number: None,
        fromto_id: None,
    });
    i.mask_apertures.push(MaskAperture {
        id: id(11),
        net: None,
        group_id: None,
        layer: "F.Mask".into(),
        shape: GraphicShape::Polygon {
            outer: vec![
                [9_000_000, 9_500_000],
                [11_000_000, 9_500_000],
                [11_000_000, 12_500_000],
                [9_000_000, 12_500_000],
            ],
            holes: Vec::new(),
            width_nm: 0,
            filled: true,
        },
        expansion_nm: 0,
    });
    // Also place a B.Mask expanded circle over a B.Cu GND pad at y=10M? Use a separate B.Mask
    // segment at x=10M, y=5..8M with expansion_nm=300_000 and B.Cu GND pad at (10M, 6M).
    i.pads.push(Pad {
        id: id(12),
        net: 2,
        at: [10_000_000, 6_000_000],
        shape: Shape::Rect {
            half_size_nm: [400_000, 400_000],
        },
        drill: None,
        layers: vec!["B.Cu".into()],
        local_clearance_nm: None,
        zone_connection: Default::default(),
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
        footprint_ref: None,
        pad_number: None,
        fromto_id: None,
    });
    i.mask_apertures.push(MaskAperture {
        id: id(13),
        net: None,
        group_id: None,
        layer: "B.Mask".into(),
        shape: GraphicShape::Segment {
            start: [10_000_000, 5_500_000],
            end: [10_000_000, 7_500_000],
            width_nm: 200_000,
        },
        expansion_nm: 300_000,
    });
    let mut e = Engine::new(i.clone()).unwrap();
    // Crossing F.Mask aperture on F.Cu is blocked; via inside F.Mask aperture is blocked;
    // detour on F.Cu at y=7M is legal (B.Mask only affects B.Cu).
    assert!(!e
        .legal_edge(1, [5_000_000, 10_000_000], [15_000_000, 10_000_000])
        .unwrap());
    assert!(e
        .legal_edge(1, [5_000_000, 7_000_000], [15_000_000, 7_000_000])
        .unwrap());
    let out = e.route_all();
    assert!(out.is_ok(), "{out:?}");
    let tracks: Vec<_> = e.copper().generated().cloned().collect();
    assert!(!tracks.is_empty());
    // Net 1 (SIG) tracks must stay clear of F.Mask / B.Mask apertures (which expose net 2 GND pads).
    assert!(tracks.iter().any(|t| t.net == 1));
    for t in tracks.iter().filter(|t| t.net == 1) {
        if t.layer == "F.Cu" {
            assert!(!kicad_route_engine::geometry::track_near_mask(
                t.start,
                t.end,
                t.width_nm,
                0,
                &i.mask_apertures[0],
                false
            ));
        } else if t.layer == "B.Cu" {
            assert!(!kicad_route_engine::geometry::track_near_mask(
                t.start,
                t.end,
                t.width_nm,
                0,
                &i.mask_apertures[1],
                false
            ));
        }
    }
    // Single-layer F.Cu board must detour around the F.Mask aperture on F.Cu.
    let mut single = i.clone();
    single.layers.clear();
    single.pads.pop();
    single.mask_apertures.pop();
    let mut es = Engine::new(single.clone()).unwrap();
    es.route_all().unwrap();
    let single_tracks: Vec<_> = es.copper().generated().cloned().collect();
    assert!(single_tracks
        .iter()
        .any(|t| t.start[1] != 10_000_000 || t.end[1] != 10_000_000));
    for t in &single_tracks {
        assert!(!kicad_route_engine::geometry::track_near_mask(
            t.start,
            t.end,
            t.width_nm,
            0,
            &single.mask_apertures[0],
            false
        ));
    }

    // Empty F.Mask aperture allows net 1 alone, then blocks net 2 once net 1 generates copper over it.
    let mut g = base();
    g.bounds = Bounds {
        min: [0, 0],
        max: [20_000_000, 20_000_000],
    };
    g.rules = i.rules;
    g.nets.push(Net {
        id: 2,
        name: "SIG2".into(),
    });
    g.pads[0].at = [5_000_000, 10_000_000];
    g.pads[1].at = [15_000_000, 10_000_000];
    g.pads.push(Pad {
        id: id(20),
        net: 2,
        at: [10_000_000, 5_000_000],
        shape: Shape::Circle { radius_nm: 300_000 },
        drill: None,
        layers: vec!["F.Cu".into()],
        local_clearance_nm: None,
        zone_connection: Default::default(),
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
        footprint_ref: None,
        pad_number: None,
        fromto_id: None,
    });
    g.pads.push(Pad {
        id: id(21),
        net: 2,
        at: [10_000_000, 15_000_000],
        shape: Shape::Circle { radius_nm: 300_000 },
        drill: None,
        layers: vec!["F.Cu".into()],
        local_clearance_nm: None,
        zone_connection: Default::default(),
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
        footprint_ref: None,
        pad_number: None,
        fromto_id: None,
    });
    g.mask_apertures.push(MaskAperture {
        id: id(22),
        net: None,
        group_id: None,
        layer: "F.Mask".into(),
        shape: GraphicShape::Polygon {
            outer: vec![
                [9_000_000, 8_000_000],
                [11_000_000, 8_000_000],
                [11_000_000, 12_000_000],
                [9_000_000, 12_000_000],
            ],
            holes: Vec::new(),
            width_nm: 0,
            filled: true,
        },
        expansion_nm: 0,
    });
    let mut eg = Engine::new(g.clone()).unwrap();
    assert!(eg
        .legal_edge(1, [5_000_000, 10_000_000], [15_000_000, 10_000_000])
        .unwrap());
    eg.route(1, 50_000).unwrap();
    // Now net 1 has generated tracks across the empty F.Mask aperture, so net 2 must be blocked at y=9M (away from net 1's copper at y=10M) and detour.
    assert!(!eg
        .legal_edge(2, [8_000_000, 8_500_000], [12_000_000, 8_500_000])
        .unwrap());
    eg.route(2, 50_000).unwrap();
    assert!(!eg.copper().generated().any(|t| t.net == 2
        && kicad_route_engine::geometry::track_near_mask(
            t.start,
            t.end,
            t.width_nm,
            0,
            &g.mask_apertures[0],
            false
        )));
}

#[test]
fn m5_2_1_zero_to_cu_pad_solder_mask_expansion_enforced_outer_only() {
    // Challenge 2: solder_mask_expansion_nm=350k with to_cu=0 must enforce mask_pad_clr=350k on F.Cu/B.Cu
    // and 0 on inner layers (In1.Cu), plus propagate minimums when effective_rules is supplied directly.
    let mut i = base();
    i.layers = vec![
        "F.Cu".into(),
        "In1.Cu".into(),
        "In2.Cu".into(),
        "B.Cu".into(),
    ];
    i.bounds = Bounds {
        min: [0, 0],
        max: [20_000_000, 20_000_000],
    };
    i.rules = Rules {
        width_nm: 250_000,
        clearance_nm: 250_000,
        grid_nm: 250_000,
        edge_clearance_nm: 500_000,
        hole_clearance_nm: 250_000,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 350_000,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    i.nets.push(Net {
        id: 2,
        name: "GND".into(),
    });
    i.pads[0].at = [5_000_000, 10_000_000];
    i.pads[0].layers = vec![
        "F.Cu".into(),
        "In1.Cu".into(),
        "In2.Cu".into(),
        "B.Cu".into(),
    ];
    i.pads[0].drill = Some(DrillShape::Circle {
        diameter_nm: 300_000,
    });
    i.pads[0].shape = Shape::Circle { radius_nm: 500_000 };
    i.pads[1].at = [15_000_000, 10_000_000];
    i.pads[1].layers = i.pads[0].layers.clone();
    i.pads[1].drill = Some(DrillShape::Circle {
        diameter_nm: 300_000,
    });
    i.pads[1].shape = Shape::Circle { radius_nm: 500_000 };
    // GND pad at (10M, 10.95M) on F.Cu, In1.Cu, B.Cu: gap to y=10M is 325k (>250k copper, <350k mask).
    i.pads.push(Pad {
        id: id(30),
        net: 2,
        at: [10_000_000, 10_950_000],
        shape: Shape::Rect {
            half_size_nm: [500_000, 500_000],
        },
        drill: None,
        layers: vec!["F.Cu".into(), "In1.Cu".into(), "B.Cu".into()],
        local_clearance_nm: None,
        zone_connection: Default::default(),
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
        footprint_ref: None,
        pad_number: None,
        fromto_id: None,
    });
    let e = Engine::new(i.clone()).unwrap();
    // F.Cu edge at y=10M must be blocked by mask_pad_clr=350k (325k < 350k).
    assert!(!e
        .legal_edge(1, [5_000_000, 10_000_000], [15_000_000, 10_000_000])
        .unwrap());
    // Detour at y=9.75M has gap 575k >= 350k -> legal on F.Cu.
    assert!(e
        .legal_edge(1, [5_000_000, 9_750_000], [15_000_000, 9_750_000])
        .unwrap());

    // Block F.Cu and B.Cu corridor around x=8..12M so router must use In1.Cu at y=10M (325k > 250k).
    let mut inner_test = i.clone();
    for (k, lname) in ["F.Cu", "B.Cu"].into_iter().enumerate() {
        inner_test.rule_areas.push(RuleArea {
            id: id(40 + k as u64),
            name: None,
            layers: vec![lname.into()],
            outer: vec![
                [8_000_000, 0],
                [12_000_000, 0],
                [12_000_000, 20_000_000],
                [8_000_000, 20_000_000],
            ],
            holes: Vec::new(),
            extra_polygons: Vec::new(),
            no_tracks: true,
            no_vias: true,
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        });
    }
    let out_inner = run(inner_test);
    assert_eq!(out_inner.status, Status::Complete, "{:?}", out_inner.reason);
    assert!(out_inner
        .added_tracks
        .iter()
        .any(|t| t.layer == "In1.Cu" || t.layer == "In2.Cu"));

    // Verify rules.rs effective_rules path preserves solder_mask_expansion_nm when input.rules is omitted.
    let mut eff = kicad_route_engine::rules::EffectiveRules::from_uniform_rules(i.rules, &i.nets);
    eff.minimums.solder_mask_expansion_nm = 350_000;
    let mut val = serde_json::to_value(&i).unwrap();
    val.as_object_mut().unwrap().remove("rules");
    val.as_object_mut().unwrap().insert(
        "effective_rules".into(),
        serde_json::to_value(&eff).unwrap(),
    );
    let from_eff: Input = serde_json::from_value(val).unwrap();
    let e_eff = Engine::new(from_eff).unwrap();
    assert!(!e_eff
        .legal_edge(1, [5_000_000, 10_000_000], [15_000_000, 10_000_000])
        .unwrap());
}

#[test]
fn m5_2_1_umask_single_net_aperture_allows_owner_even_with_positive_to_cu() {
    for to_cu in [0, 100_000] {
        let mut i = base();
        i.bounds = Bounds {
            min: [0, 0],
            max: [20_000_000, 20_000_000],
        };
        i.rules = Rules {
            width_nm: 250_000,
            clearance_nm: 250_000,
            grid_nm: 250_000,
            edge_clearance_nm: 500_000,
            hole_clearance_nm: 250_000,
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 200_000,
            solder_mask_to_copper_clearance_nm: to_cu,
        };
        i.nets.push(Net {
            id: 2,
            name: "GND".into(),
        });
        i.pads[0].at = [5_000_000, 10_000_000];
        i.pads[0].shape = Shape::Rect {
            half_size_nm: [250_000, 250_000],
        };
        i.pads[1].at = [15_000_000, 10_000_000];
        i.pads[1].shape = Shape::Rect {
            half_size_nm: [250_000, 250_000],
        };
        // Unowned F.Mask aperture (net: None, as exported for a 2-net footprint) covers ONLY Pad 1 (net 1).
        i.mask_apertures.push(MaskAperture {
            id: id(60),
            net: None,
            group_id: None,
            layer: "F.Mask".into(),
            shape: GraphicShape::Polygon {
                outer: vec![
                    [4_600_000, 9_600_000],
                    [5_400_000, 9_600_000],
                    [5_400_000, 10_400_000],
                    [4_600_000, 10_400_000],
                ],
                holes: Vec::new(),
                width_nm: 0,
                filled: true,
            },
            expansion_nm: 0,
        });
        let e = Engine::new(i.clone()).unwrap();
        assert!(e
            .legal_edge(1, [5_000_000, 10_000_000], [5_500_000, 10_000_000])
            .unwrap());
        assert!(!e
            .legal_edge(2, [4_000_000, 10_000_000], [6_000_000, 10_000_000])
            .unwrap());
        let out = run(i);
        assert_eq!(
            out.status,
            Status::Complete,
            "to_cu={to_cu}: {:?}",
            out.reason
        );
    }
}

#[test]
fn m5_2_1_static_pad_mask_expansion_and_min_width_bridge_to_apertures() {
    struct Case {
        name: &'static str,
        exp: i64,
        min_width: i64,
        gnd_at: Point,
        aperture: MaskAperture,
    }
    let rect_shape = |x0: i64, y0: i64, x1: i64, y1: i64| GraphicShape::Polygon {
        outer: vec![[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
        holes: Vec::new(),
        width_nm: 0,
        filled: true,
    };
    let cases = [
        Case {
            name: "E1_pad_web_touches_rect",
            exp: 0,
            min_width: 200_000,
            gnd_at: [15_000_000, 12_000_000],
            aperture: MaskAperture {
                id: id(70),
                net: None,
                group_id: None,
                layer: "F.Mask".into(),
                shape: rect_shape(14_000_000, 9_500_000, 16_000_000, 11_500_000),
                expansion_nm: 0,
            },
        },
        Case {
            name: "E2_pad_exp_touches_text_segment",
            exp: 200_000,
            min_width: 0,
            gnd_at: [15_000_000, 12_000_000],
            aperture: MaskAperture {
                id: id(71),
                net: None,
                group_id: None,
                layer: "F.Mask".into(),
                shape: GraphicShape::Segment {
                    start: [15_000_000, 9_500_000],
                    end: [15_000_000, 11_300_000],
                    width_nm: 400_000,
                },
                expansion_nm: 0,
            },
        },
        Case {
            name: "D_npth_mask_bridge",
            exp: 200_000,
            min_width: 0,
            gnd_at: [15_000_000, 12_400_000],
            aperture: MaskAperture {
                id: id(72),
                net: None,
                group_id: None,
                layer: "F.Mask".into(),
                shape: GraphicShape::Circle {
                    center: [15_000_000, 10_000_000],
                    radius_nm: 2_000_000,
                    width_nm: 0,
                    filled: true,
                },
                expansion_nm: 0,
            },
        },
        Case {
            name: "pad_exp_touches_rect",
            exp: 350_000,
            min_width: 0,
            gnd_at: [15_000_000, 12_400_000],
            aperture: MaskAperture {
                id: id(73),
                net: None,
                group_id: None,
                layer: "F.Mask".into(),
                shape: rect_shape(14_000_000, 9_500_000, 16_000_000, 11_500_000),
                expansion_nm: 350_000,
            },
        },
    ];
    for c in cases {
        let mut i = base();
        i.bounds = Bounds {
            min: [0, 0],
            max: [30_000_000, 20_000_000],
        };
        i.rules = Rules {
            width_nm: 250_000,
            clearance_nm: 250_000,
            grid_nm: 250_000,
            edge_clearance_nm: 500_000,
            hole_clearance_nm: 250_000,
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: c.exp,
            solder_mask_min_width_nm: c.min_width,
            solder_mask_to_copper_clearance_nm: 0,
        };
        i.nets.push(Net {
            id: 2,
            name: "GND".into(),
        });
        i.pads[0].at = [5_000_000, 10_000_000];
        i.pads[0].shape = Shape::Rect {
            half_size_nm: [400_000, 400_000],
        };
        i.pads[1].at = [25_000_000, 10_000_000];
        i.pads[1].shape = Shape::Rect {
            half_size_nm: [400_000, 400_000],
        };
        let mut gnd = pad(3, 2, c.gnd_at);
        gnd.shape = Shape::Rect {
            half_size_nm: [400_000, 400_000],
        };
        i.pads.push(gnd);
        i.mask_apertures.push(c.aperture.clone());
        let e = Engine::new(i.clone()).unwrap();
        assert!(
            !e.legal_edge(1, [5_000_000, 10_000_000], [25_000_000, 10_000_000])
                .unwrap(),
            "{}: straight edge across bridged mask aperture must be illegal",
            c.name
        );
        let out = run(i);
        assert_eq!(out.status, Status::Complete, "{}: {:?}", c.name, out.reason);
        for t in out.added_tracks.iter().filter(|t| t.net == 1) {
            assert!(
                !track_near_mask(t.start, t.end, t.width_nm, 0, &c.aperture, false),
                "{}: track {t:?} touched bridged mask aperture",
                c.name
            );
        }
    }
}
