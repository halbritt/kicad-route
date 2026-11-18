use kicad_route_engine::{model::*, run, run_json, Engine, TransactionError};
use std::collections::{BTreeSet, VecDeque};
mod model {
    pub use kicad_route_engine::model::*;
}
#[path = "support/contact.rs"]
mod contact;
use contact::Body;

fn id(n: usize) -> String {
    format!("10000000-0000-4000-8000-{n:012x}")
}
fn fixture(points: &[Point]) -> Input {
    Input {
        version: 1,
        source_id: "a".repeat(64),
        layer: "F.Cu".into(),
        bounds: Bounds {
            min: [0, 0],
            max: [100, 100],
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
            name: "SIGNAL".into(),
        }],
        pads: points
            .iter()
            .enumerate()
            .map(|(n, &at)| Pad {
                id: id(n + 1),
                net: 1,
                at,
                layers: Vec::new(),
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 1 },
                drill: None,
            })
            .collect(),
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
fn track(n: usize, net: u32, start: Point, end: Point, locked: bool) -> Track {
    Track {
        id: id(n),
        net,
        start,
        end,
        width_nm: 2,
        locked,
        layer: "F.Cu".into(),
    }
}
fn all_bodies(i: &Input, added: &[Track]) -> Vec<(u32, Body)> {
    i.pads
        .iter()
        .map(|p| (p.net, Body::pad(p)))
        .chain(
            i.tracks
                .iter()
                .chain(added)
                .map(|t| (t.net, Body::track(t))),
        )
        .collect()
}
// Independent box-gap/contact graph: no production partition/search/geometry.
// Domain here is circular pads and axis-aligned tracks, with integer radii.
fn connected(i: &Input, added: &[Track], net: u32) -> bool {
    let bodies: Vec<_> = all_bodies(i, added)
        .into_iter()
        .filter(|(n, _)| *n == net)
        .map(|(_, b)| b)
        .collect();
    if bodies.is_empty() {
        return true;
    }
    let mut reached = BTreeSet::new();
    let mut todo = vec![0];
    while let Some(n) = todo.pop() {
        if reached.insert(n) {
            for j in 0..bodies.len() {
                if !reached.contains(&j) && bodies[n].contacts(bodies[j]) {
                    todo.push(j);
                }
            }
        }
    }
    reached.len() == bodies.len()
}
fn valid_complete(i: &Input, added: &[Track]) {
    for net in &i.nets {
        if net.id != 0 {
            assert!(
                connected(i, added, net.id),
                "unjoined net {}: {added:?}",
                net.id
            );
        }
    }
    let mut ids: BTreeSet<_> = i
        .pads
        .iter()
        .map(|p| &p.id)
        .chain(i.tracks.iter().map(|t| &t.id))
        .collect();
    let bodies = all_bodies(i, added);
    for t in added {
        assert!(ids.insert(&t.id), "duplicate generated identity");
        assert_ne!(t.start, t.end);
        let body = Body::track(t);
        for k in 0..2 {
            assert!(
                2 * (body.lo[k] - i.bounds.min[k]) >= t.width_nm + 2 * i.rules.edge_clearance_nm
            );
            assert!(
                2 * (i.bounds.max[k] - body.hi[k]) >= t.width_nm + 2 * i.rules.edge_clearance_nm
            );
        }
        for &(net, foreign) in &bodies {
            if net != t.net {
                assert!(
                    !body.violates(foreign, i.rules.clearance_nm),
                    "foreign copper clearance"
                );
            }
        }
    }
}
fn snapshot(e: &Engine) -> Vec<u8> {
    assert!(e.copper().index_consistent());
    serde_json::to_vec(&e.copper().tracks().collect::<Vec<_>>()).unwrap()
}
fn generated(e: &Engine) -> Vec<Track> {
    e.copper().generated().cloned().collect()
}
fn cross() -> Input {
    fixture(&[[50, 50], [10, 50], [90, 50], [50, 10], [50, 90], [80, 80]])
}

#[test]
fn three_four_six_and_nine_terminals_are_connected_and_deterministic() {
    let positions = [
        [50, 50],
        [10, 50],
        [90, 50],
        [50, 10],
        [50, 90],
        [80, 80],
        [20, 20],
        [20, 80],
        [80, 20],
    ];
    for count in [3, 4, 6, 9] {
        let i = fixture(&positions[..count]);
        let output = run(i.clone());
        assert_eq!(
            output.status,
            Status::Complete,
            "count={count}: {:?}",
            output.reason
        );
        valid_complete(&i, &output.added_tracks);
        assert_eq!(
            serde_json::to_vec(&output).unwrap(),
            serde_json::to_vec(&run(i.clone())).unwrap()
        );
        let mut e = Engine::new(i.clone()).unwrap();
        e.route(1, 10000).unwrap();
        valid_complete(&i, &generated(&e));
        assert_eq!(e.copper().input(), i.tracks);
        let before = snapshot(&e);
        assert_eq!(e.route(1, 10000).unwrap(), 0); // reuse all connected terminals
        assert_eq!(snapshot(&e), before);
    }
}
#[test]
fn central_terminal_supports_branched_connections() {
    let i = cross();
    let output = run(i.clone());
    assert_eq!(output.status, Status::Complete);
    valid_complete(&i, &output.added_tracks);
    let hub = Body::pad(&i.pads[0]);
    assert!(
        output
            .added_tracks
            .iter()
            .filter(|t| hub.contacts(Body::track(t)))
            .count()
            >= 3
    );
}
#[test]
fn preconnected_groups_and_disconnected_incumbents_are_all_preserved_and_joined() {
    let mut i = fixture(&[[10, 20], [20, 20], [40, 20], [50, 20], [70, 20], [80, 20]]);
    i.tracks = vec![
        track(100, 1, [10, 20], [20, 20], true),
        track(101, 1, [40, 20], [50, 20], false),
        track(102, 1, [70, 20], [80, 20], true),
        track(103, 1, [45, 60], [55, 60], false),
    ];
    let mut e = Engine::new(i.clone()).unwrap();
    e.route_all().unwrap();
    assert_eq!(e.copper().input(), i.tracks);
    valid_complete(&i, &generated(&e));
    e.reroute(1, 10000).unwrap();
    assert_eq!(e.copper().input(), i.tracks);
    valid_complete(&i, &generated(&e));
}
#[test]
fn independent_connectivity_control_rejects_omitted_last_terminal() {
    let i = fixture(&[[10, 10], [20, 10], [30, 10], [80, 80]]);
    let mut omitted = i.clone();
    omitted.pads.pop();
    let partial = run(omitted.clone());
    assert_eq!(partial.status, Status::Complete);
    assert!(connected(&omitted, &partial.added_tracks, 1));
    assert!(!connected(&i, &partial.added_tracks, 1));
    let all = run(i.clone());
    assert_eq!(all.status, Status::Complete);
    valid_complete(&i, &all.added_tracks);
}

fn two_nets() -> Input {
    let mut i = fixture(&[[10, 10], [20, 10], [40, 10]]);
    i.nets.push(Net {
        id: 2,
        name: "SECOND".into(),
    });
    i.pads.extend(
        [[10, 80], [20, 80], [40, 80]]
            .iter()
            .enumerate()
            .map(|(n, &at)| Pad {
                id: id(20 + n),
                net: 2,
                at,
                shape: Shape::Circle { radius_nm: 1 },
                drill: None,
                layers: Vec::new(),
                local_clearance_nm: None,
            }),
    );
    i
}
#[test]
fn expansion_exhaustion_after_a_join_rolls_back_and_preserves_next_ids() {
    let i = fixture(&[[10, 10], [20, 10], [40, 10]]);
    let mut complete = Engine::new(i.clone()).unwrap();
    let needed = complete.route(1, 10000).unwrap();
    assert!(needed > 2);
    assert_eq!(generated(&complete).len(), 2); // two successful joins, one segment each
    let mut limited = Engine::new(i.clone()).unwrap();
    let before = snapshot(&limited);
    let failure = limited.route(1, needed - 1).unwrap_err();
    assert_eq!(failure.status, Status::BudgetExhausted);
    assert_eq!(failure.expanded_states, needed - 1);
    assert_eq!(snapshot(&limited), before);
    // At the same expansion cap, refusing the first insertion occurs earlier:
    // the normal failure therefore got past that insertion and into a later join.
    let mut no_journal = i.clone();
    no_journal.limits.max_journal_entries = 0;
    let early = Engine::new(no_journal)
        .unwrap()
        .route(1, needed - 1)
        .unwrap_err();
    assert_eq!(early.status, Status::JournalExhausted);
    assert!(early.expanded_states < failure.expanded_states);
    limited.route(1, 10000).unwrap();
    assert_eq!(snapshot(&limited), snapshot(&complete));
    let mut batch = i;
    batch.max_expansions = needed - 1;
    let output = run(batch);
    assert_eq!(output.status, Status::BudgetExhausted);
    assert!(output.added_tracks.is_empty() && output.removed_track_ids.is_empty());
}
#[test]
fn multiple_nets_share_expansions_and_late_failure_discards_earlier_net() {
    let mut i = two_nets();
    let a = Engine::new(i.clone()).unwrap().route(1, 10000).unwrap();
    let b = Engine::new(i.clone()).unwrap().route(2, 10000).unwrap();
    i.max_expansions = a.max(b);
    let mut e = Engine::new(i.clone()).unwrap();
    let before = snapshot(&e);
    let error = e.route_all().unwrap_err();
    assert_eq!(error.status, Status::BudgetExhausted);
    assert_eq!(error.expanded_states, i.max_expansions);
    assert_eq!(snapshot(&e), before);
    e.route(2, 10000).unwrap();
    let mut fresh = Engine::new(i.clone()).unwrap();
    fresh.route(2, 10000).unwrap();
    assert_eq!(snapshot(&e), snapshot(&fresh));
    let failed = run(i.clone());
    assert!(failed.added_tracks.is_empty());
    i.max_expansions = 10000;
    let success = run(i.clone());
    assert_eq!(success.status, Status::Complete);
    valid_complete(&i, &success.added_tracks);
    assert!(success.expanded_states > i.max_expansions.min(a.max(b)));
}
#[test]
fn work_exhaustion_at_second_insertion_rolls_back_without_refund() {
    let mut i = two_nets();
    i.nets.push(Net {
        id: 3,
        name: "RECOVERY".into(),
    });
    for (n, at) in [[80, 80], [90, 80]].into_iter().enumerate() {
        i.pads.push(Pad {
            id: id(50 + n),
            net: 3,
            at,
            shape: Shape::Circle { radius_nm: 1 },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
        });
    }
    // One undo entry admits the entire first straight join; the next insert fails.
    i.limits.max_journal_entries = 1;
    let mut probe = Engine::new(i.clone()).unwrap();
    let failure = probe.route_all().unwrap_err();
    assert_eq!(failure.status, Status::JournalExhausted);
    assert!(failure.expanded_states >= 4);
    assert!(generated(&probe).is_empty());
    i.limits.max_journal_entries = Limits::default().max_journal_entries;
    i.limits.max_work = failure.work_used;
    let mut e = Engine::new(i.clone()).unwrap();
    let before = snapshot(&e);
    let failed = e.route_all().unwrap_err();
    assert_eq!(failed.status, Status::WorkExhausted);
    assert_eq!(failed.expanded_states, failure.expanded_states);
    assert_eq!(failed.work_used, i.limits.max_work);
    assert_eq!(e.work_used(), failed.work_used); // rollback did not refund
    assert_eq!(snapshot(&e), before);
    // A later outer operation has a fresh budget and the original allocator.
    e.route(3, 10000).unwrap();
    let mut fresh = Engine::new(i.clone()).unwrap();
    fresh.route(3, 10000).unwrap();
    assert_eq!(snapshot(&e), snapshot(&fresh));
    let output = run(i);
    assert_eq!(output.status, Status::WorkExhausted);
    assert_eq!(output.expanded_states, failed.expanded_states);
    assert!(output.added_tracks.is_empty() && output.removed_track_ids.is_empty());
}
#[test]
fn unreachable_last_terminal_cannot_publish_partial_success() {
    let mut i = two_nets();
    i.pads.push(Pad {
        id: id(99),
        net: 1,
        at: [80, 10],
        shape: Shape::Circle { radius_nm: 1 },
        drill: None,
        layers: Vec::new(),
        local_clearance_nm: None,
    });
    i.nets.push(Net {
        id: 0,
        name: String::new(),
    });
    i.tracks.push(track(100, 0, [60, 0], [60, 100], true));
    let mut e = Engine::new(i.clone()).unwrap();
    let before = snapshot(&e);
    let failed = e.route(1, 10000).unwrap_err();
    assert_eq!(failed.status, Status::Unroutable);
    assert!(failed.reason.contains("fixed grid"));
    assert_eq!(snapshot(&e), before);
    e.route(2, 10000).unwrap();
    let mut fresh = Engine::new(i.clone()).unwrap();
    fresh.route(2, 10000).unwrap();
    assert_eq!(snapshot(&e), snapshot(&fresh));
    let output = run(i);
    assert_eq!(output.status, Status::Unroutable);
    assert!(output.added_tracks.is_empty() && output.removed_track_ids.is_empty());
}
#[test]
fn nested_reroute_failure_and_outer_rollback_restore_all_branches_and_ids() {
    let i = cross();
    let mut e = Engine::new(i.clone()).unwrap();
    e.route(1, 10000).unwrap();
    let before = snapshot(&e);
    let mut control = Engine::new(i.clone()).unwrap();
    control.route(1, 10000).unwrap();
    let outcome = e.transaction(|outer| {
        let prior = serde_json::to_vec(&outer.copper().tracks().collect::<Vec<_>>()).unwrap();
        let failed = outer.reroute(1, 1).unwrap_err();
        assert_eq!(failed.status, Status::BudgetExhausted);
        assert_eq!(
            serde_json::to_vec(&outer.copper().tracks().collect::<Vec<_>>()).unwrap(),
            prior
        );
        assert!(outer.copper().index_consistent());
        outer
            .transaction(|inner| {
                inner.reroute(1, 10000)?;
                Ok::<_, Failure>(())
            })
            .unwrap();
        Err::<(), _>("reject outer changes")
    });
    assert_eq!(
        outcome,
        Err(TransactionError::Operation("reject outer changes"))
    );
    assert_eq!(snapshot(&e), before);
    e.reroute(1, 10000).unwrap();
    control.reroute(1, 10000).unwrap();
    assert_eq!(snapshot(&e), snapshot(&control));
    valid_complete(&i, &generated(&e));
}

// Independent position-only flood fill for open chambers separated by whole
// outline-spanning walls. Each available lattice edge has >=7 nm clearance
// from foreign copper. A wall cannot be bypassed continuously inside bounds.
// This proves structural reachability in this domain, not shortest trees.
fn chamber_reachable(terminals: &[Point], walls: u8) -> bool {
    let available = |p: Point| {
        (1..=5).contains(&(p[0] / 10))
            && (1..=5).contains(&(p[1] / 10))
            && !(walls & 1 != 0 && p[0] == 30)
            && !(walls & 2 != 0 && p[1] == 30)
    };
    let mut reached = BTreeSet::new();
    let mut todo = VecDeque::from([terminals[0]]);
    while let Some(p) = todo.pop_front() {
        if !available(p) || !reached.insert(p) {
            continue;
        }
        for d in [[10, 0], [-10, 0], [0, 10], [0, -10]] {
            todo.push_back([p[0] + d[0], p[1] + d[1]]);
        }
    }
    terminals.iter().all(|p| reached.contains(p))
}
#[test]
fn multi_terminal_completion_matches_independent_reachability_in_620_worlds() {
    let positions = [
        [10, 10],
        [20, 20],
        [40, 10],
        [50, 20],
        [10, 40],
        [20, 50],
        [40, 40],
        [50, 50],
    ];
    let mut worlds = 0;
    let mut possible = 0;
    let mut impossible = 0;
    for mask in 0u32..256 {
        if ![3, 4, 6, 8].contains(&mask.count_ones()) {
            continue;
        }
        let points: Vec<_> = positions
            .iter()
            .enumerate()
            .filter(|(n, _)| mask & (1 << n) != 0)
            .map(|(_, p)| *p)
            .collect();
        for walls in 0..4 {
            let mut i = fixture(&points);
            i.bounds.max = [60, 60];
            i.nets.push(Net {
                id: 0,
                name: String::new(),
            });
            if walls & 1 != 0 {
                i.tracks.push(track(100, 0, [30, 0], [30, 60], true));
            }
            if walls & 2 != 0 {
                i.tracks.push(track(101, 0, [0, 30], [60, 30], true));
            }
            let expected = chamber_reachable(&points, walls);
            let output = run(i.clone());
            if expected {
                assert_eq!(
                    output.status,
                    Status::Complete,
                    "mask={mask},walls={walls}: {:?}",
                    output.reason
                );
                valid_complete(&i, &output.added_tracks);
                possible += 1;
            } else {
                assert_eq!(
                    output.status,
                    Status::Unroutable,
                    "mask={mask},walls={walls}"
                );
                assert!(output.added_tracks.is_empty() && output.removed_track_ids.is_empty());
                impossible += 1;
            }
            worlds += 1;
        }
    }
    assert_eq!(worlds, 620);
    assert_eq!((possible, impossible), (175, 445));
}
#[test]
fn multi_terminal_wire_contract_and_admission_remain_strict() {
    let i = cross();
    let bytes = serde_json::to_vec(&i).unwrap();
    let output = run_json(&bytes);
    assert_eq!(output.version, 3);
    assert_eq!(output.status, Status::Complete);
    assert_eq!(
        output.request_sha256,
        kicad_route_engine::request_sha256(&bytes)
    );
    valid_complete(&i, &output.added_tracks);
    for (field, value, status) in [
        ("pads", serde_json::Value::Null, Status::Invalid),
        ("version", serde_json::json!(2), Status::Unsupported),
        (
            "limits",
            serde_json::json!({"max_items":6}),
            Status::AdmissionExceeded,
        ),
        (
            "limits",
            serde_json::json!({"max_work":0}),
            Status::WorkExhausted,
        ),
        (
            "limits",
            serde_json::json!({"max_depth":0}),
            Status::NestingExhausted,
        ),
        (
            "limits",
            serde_json::json!({"max_search_entries":0}),
            Status::AdmissionExceeded,
        ),
        (
            "limits",
            serde_json::json!({"max_journal_entries":0}),
            Status::JournalExhausted,
        ),
    ] {
        let mut doc = serde_json::to_value(&i).unwrap();
        doc[field] = value;
        let bytes = serde_json::to_vec(&doc).unwrap();
        let failed = run_json(&bytes);
        assert_eq!(failed.status, status, "{field}");
        assert_eq!(
            failed.request_sha256,
            kicad_route_engine::request_sha256(&bytes)
        );
        assert!(failed.added_tracks.is_empty() && failed.removed_track_ids.is_empty());
    }
    let dir = std::env::temp_dir().join(format!("rust-multi-cli-{}", std::process::id()));
    std::fs::create_dir(&dir).unwrap();
    let request = dir.join("request.json");
    let result = dir.join("result.json");
    std::fs::write(&request, &bytes).unwrap();
    let done = std::process::Command::new(env!("CARGO_BIN_EXE_kicad-route-engine"))
        .args([&request, &result])
        .output()
        .unwrap();
    assert_eq!(done.status.code(), Some(0));
    let observed: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&result).unwrap()).unwrap();
    assert_eq!(observed, serde_json::to_value(output).unwrap());
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn already_connected_six_terminal_incumbent_tree_needs_no_edits() {
    let mut i = fixture(&[[10, 20], [20, 20], [40, 20], [50, 20], [70, 20], [80, 20]]);
    i.tracks = vec![
        track(100, 1, [10, 20], [80, 20], true),
        track(101, 1, [40, 20], [40, 60], false),
    ];
    let expected = i.tracks.clone();
    let mut e = Engine::new(i.clone()).unwrap();
    assert_eq!(e.route_all().unwrap(), 0);
    assert_eq!(e.copper().input(), expected);
    assert!(generated(&e).is_empty());
    assert_eq!(e.reroute(1, 10000).unwrap(), 0);
    assert_eq!(e.copper().input(), expected);
    assert!(generated(&e).is_empty());
    valid_complete(&i, &[]);
}

// Exact owner-authored native failure, retained byte-for-byte with its limits.
#[test]
fn frozen_native_two_net_request_completes_with_original_budget() {
    let raw = include_str!("fixtures/multiterminal-native-two-nets.json");
    let input: Input = serde_json::from_str(raw).unwrap();
    assert_eq!(input.max_expansions, 100000);
    let output = run_json(raw.as_bytes());
    assert_eq!(
        output.request_sha256.as_deref(),
        Some("dfce0b7268f51eb34cb2593e8d97e35e37978f9ff91a40f2fdef717f5fc61833")
    );
    assert_eq!(output.status, Status::Complete, "{output:?}");
    valid_complete(&input, &output.added_tracks);
    assert!(output.expanded_states <= input.max_expansions);
    assert_eq!(
        serde_json::to_vec(&output).unwrap(),
        serde_json::to_vec(&run_json(raw.as_bytes())).unwrap()
    );
    eprintln!(
        "frozen native request: expansions={} work={} tracks={}",
        output.expanded_states,
        output.work_used,
        output.added_tracks.len()
    );
}

fn assert_zero_collinear_overlap(i: &Input, added: &[Track], net: u32) {
    let net_tracks: Vec<_> = i
        .tracks
        .iter()
        .chain(added.iter())
        .filter(|t| t.net == net)
        .collect();
    for (idx, a) in net_tracks.iter().enumerate() {
        for b in &net_tracks[idx + 1..] {
            for axis in 0..2 {
                let ortho = 1 - axis;
                if a.start[ortho] == a.end[ortho]
                    && b.start[ortho] == b.end[ortho]
                    && a.start[ortho] == b.start[ortho]
                {
                    let a_lo = a.start[axis].min(a.end[axis]);
                    let a_hi = a.start[axis].max(a.end[axis]);
                    let b_lo = b.start[axis].min(b.end[axis]);
                    let b_hi = b.start[axis].max(b.end[axis]);
                    let overlap = a_hi.min(b_hi) - a_lo.max(b_lo);
                    assert!(
                        overlap <= 0,
                        "positive collinear overlap {overlap} nm between {a:?} and {b:?}"
                    );
                }
            }
        }
    }
}

fn p2a_forced_t_corridor(swap_j1_j2: bool) -> Input {
    let j1 = Pad {
        id: id(1),
        net: 1,
        at: [4_000_000, 10_000_000],
        shape: Shape::Circle { radius_nm: 500_000 },
        drill: None,
        layers: Vec::new(),
        local_clearance_nm: None,
    };
    let j2 = Pad {
        id: id(2),
        net: 1,
        at: [12_000_000, 10_000_000],
        shape: Shape::Circle { radius_nm: 500_000 },
        drill: None,
        layers: Vec::new(),
        local_clearance_nm: None,
    };
    let j3 = Pad {
        id: id(3),
        net: 1,
        at: [8_000_000, 16_000_000],
        shape: Shape::Circle { radius_nm: 500_000 },
        drill: None,
        layers: Vec::new(),
        local_clearance_nm: None,
    };
    let pads = if swap_j1_j2 {
        vec![j2, j1, j3]
    } else {
        vec![j1, j2, j3]
    };
    let wall = |n, a, b| Track {
        id: id(n),
        net: 0,
        start: a,
        end: b,
        width_nm: 250_000,
        locked: true,
        layer: "F.Cu".into(),
    };
    Input {
        version: 1,
        source_id: "a".repeat(64),
        layer: "F.Cu".into(),
        bounds: Bounds {
            min: [0, 0],
            max: [20_000_000, 20_000_000],
        },
        rules: Rules {
            width_nm: 250_000,
            clearance_nm: 1_000_000,
            grid_nm: 500_000,
            edge_clearance_nm: 500_000,
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
                name: "SIGNAL".into(),
            },
        ],
        pads,
        tracks: vec![
            wall(10, [2_300_000, 8_300_000], [6_750_000, 8_300_000]),
            wall(11, [2_300_000, 11_700_000], [6_750_000, 11_700_000]),
            wall(12, [2_300_000, 8_300_000], [2_300_000, 11_700_000]),
            wall(13, [6_750_000, 11_250_000], [6_750_000, 15_000_000]),
            wall(14, [9_250_000, 8_300_000], [13_700_000, 8_300_000]),
            wall(15, [9_250_000, 11_700_000], [13_700_000, 11_700_000]),
            wall(16, [13_700_000, 8_300_000], [13_700_000, 11_700_000]),
            wall(17, [9_250_000, 11_250_000], [9_250_000, 15_000_000]),
        ],
        max_expansions: 100_000,
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

#[test]
fn forced_t_corridor_normalizes_collinear_overlaps_without_duplicate_copper() {
    for swap in [false, true] {
        let i = p2a_forced_t_corridor(swap);
        let o = run(i.clone());
        assert_eq!(o.status, Status::Complete, "swap={swap}: {:?}", o.reason);
        valid_complete(&i, &o.added_tracks);
        assert_zero_collinear_overlap(&i, &o.added_tracks, 1);
        let total_len: i64 = o
            .added_tracks
            .iter()
            .map(|t| (t.end[0] - t.start[0]).abs() + (t.end[1] - t.start[1]).abs())
            .sum();
        // swap=false: [4M..11.375001M] (7375001) + stem (5375001) = 12750002 (2 tracks, full overlap dropped).
        // swap=true: [12M..4.624999M] (7375001) + [4M..4.624999M] (624999) + stem (5375001) = 13375001 (3 tracks, partial overlap trimmed).
        assert_eq!(
            total_len,
            if swap { 13_375_001 } else { 12_750_002 },
            "swap={swap}: {:?}",
            o.added_tracks
        );
        assert_eq!(o.added_tracks.len(), if swap { 3 } else { 2 });
    }
}

#[test]
fn straddling_overlap_splits_and_work_exhaustion_rolls_back_cleanly() {
    let mut i = fixture(&[[10, 30], [60, 50]]);
    i.bounds.max = [80, 60];
    i.nets.push(Net {
        id: 0,
        name: String::new(),
    });
    i.tracks = vec![
        track(10, 1, [10, 30], [10, 10], true),
        track(11, 1, [10, 10], [32, 10], true),
        track(12, 1, [32, 10], [32, 30], true),
        track(13, 1, [32, 30], [48, 30], true),
        track(20, 0, [0, 40], [56, 40], true),
        track(21, 0, [64, 40], [80, 40], true),
        track(22, 0, [56, 40], [56, 60], true),
        track(23, 0, [64, 40], [64, 60], true),
    ];
    let mut baseline = Engine::new(i.clone()).unwrap();
    baseline.route(1, 10_000).unwrap();
    let baseline_tracks = generated(&baseline);
    valid_complete(&i, &baseline_tracks);
    assert_zero_collinear_overlap(&i, &baseline_tracks, 1);
    assert_eq!(
        baseline_tracks
            .iter()
            .map(|t| (t.start, t.end))
            .collect::<Vec<_>>(),
        vec![
            ([10, 30], [32, 30]),
            ([48, 30], [60, 30]),
            ([60, 30], [60, 49])
        ]
    );
    // Exhaust work right before final insert after straddling split; must roll back cleanly.
    let mut limited = i.clone();
    limited.limits.max_work = baseline.work_used() - 1;
    let mut e = Engine::new(limited.clone()).unwrap();
    let before = snapshot(&e);
    let err = e.route(1, 10_000).unwrap_err();
    assert_eq!(err.status, Status::WorkExhausted);
    assert_eq!(snapshot(&e), before);
    let failed = run(limited);
    assert_eq!(failed.status, Status::WorkExhausted);
    assert!(failed.added_tracks.is_empty());
    // Subsequent fresh operation with full budget reproduces exact IDs and tracks.
    let mut fresh = Engine::new(i).unwrap();
    fresh.route(1, 10_000).unwrap();
    assert_eq!(generated(&fresh), baseline_tracks);
}
