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
        },
        nets: vec![Net {
            id: 1,
            name: "A".into(),
        }],
        pads: vec![pad(1, 1, [10, 10]), pad(2, 1, [50, 10])],
        tracks: vec![],
        max_expansions: 10000,
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
    assert!(!e.legal_edge(1, [2, 10], [2, 20]));
    assert!(e.legal_edge(1, [3, 10], [3, 20]));
    assert!(!e.legal_edge(1, [58, 10], [58, 20]));
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
    assert!(!e.legal_edge(1, [10, 20], [50, 20]));
    assert!(e.legal_edge(2, [10, 20], [50, 20]));
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
        assert!(e.legal_edge(t.net, t.start, t.end));
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
        ("/pads/0/at", json!([11, 10]), Status::Unsupported),
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
    assert_eq!(run(i).status, Status::Unsupported);
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
        assert!(!good["added_tracks"].as_array().unwrap().is_empty());
        fs::write(&input, b"{broken").unwrap();
        assert!(!Command::new(env!("CARGO_BIN_EXE_kicad-route-engine"))
            .args([&input, &output])
            .status()
            .unwrap()
            .success());
        let bad: Value = serde_json::from_slice(&fs::read(&output).unwrap()).unwrap();
        assert_eq!(bad["status"], "invalid");
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
    assert!(!e.legal_edge(0, [10, 10], [50, 10]));
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
