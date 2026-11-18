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
    v["zones"] = json!([]);
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
        },
        Pad {
            id: id(2),
            net: 1,
            at: [50, 50],
            shape: Shape::Circle { radius_nm: 4 },
            drill: Some(DrillShape::Circle { diameter_nm: 2 }),
            layers: Vec::new(),
            local_clearance_nm: None,
        },
        Pad {
            id: id(3),
            net: 1,
            at: [90, 50],
            shape: Shape::Circle { radius_nm: 4 },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
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
        },
    ];
    let ej = Engine::new(j.clone()).unwrap();
    let out_j = run(j);
    assert_eq!(out_j.status, Status::Complete, "{:?}", out_j.reason);
    assert!(!out_j.added_tracks.is_empty());
    for t in &out_j.added_tracks {
        assert!(ej.legal_edge(t.net, t.start, t.end).unwrap(), "{t:?}");
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
        },
        Pad {
            id: id(2),
            net: 1,
            at: [90, 30],
            layers: vec!["B.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 2 },
            drill: None,
        },
    ];
    i.rule_areas = vec![RuleArea {
        id: id(20),
        layers: vec!["F.Cu".into()],
        no_tracks: true,
        no_vias: false,
        outer: vec![[35, 0], [65, 0], [65, 60], [35, 60]],
        holes: Vec::new(),
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
        },
        Pad {
            id: id(2),
            net: 1,
            at: [90, 10],
            layers: vec!["B.Cu".into()],
            local_clearance_nm: None,
            shape: Shape::Circle { radius_nm: 2 },
            drill: None,
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
