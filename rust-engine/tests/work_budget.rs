use kicad_route_engine::{model::*, run, run_json, Engine, TransactionError};
use std::panic::{catch_unwind, AssertUnwindSafe};

fn fixture(paired: bool) -> Input {
    let mut i: Input = serde_json::from_value(serde_json::json!({
        "version":1,"source_id":"a".repeat(64),"layer":"F.Cu",
        "bounds":{"min":[0,0],"max":[80,80]},
        "rules":{"width_nm":2,"clearance_nm":1,"grid_nm":10,"edge_clearance_nm":0},
        "nets":[{"id":1,"name":"N"}],
        "pads":[{"id":"10000000-0000-4000-8000-000000000001","net":1,"at":[10,30],
            "shape":{"kind":"circle","radius_nm":1}}],"tracks":[],"max_expansions":1000
    }))
    .unwrap();
    if paired {
        let mut p = i.pads[0].clone();
        p.id = "10000000-0000-4000-8000-000000000002".into();
        p.at = [50, 30];
        i.pads.push(p);
    }
    i
}
fn bytes(e: &Engine) -> Vec<u8> {
    assert!(e.copper().index_consistent());
    serde_json::to_vec(&e.copper().tracks().collect::<Vec<_>>()).unwrap()
}
#[test]
fn singleton_batch_has_an_independently_countable_exact_boundary() {
    // One scope entry + net enumeration/sort/dispatch (3) + item enumeration (1)
    // + partition initialization/root/pop/pair visit (4) = 9. No geometry/search.
    for limit in 0..=10 {
        let mut i = fixture(false);
        i.limits.max_work = limit;
        let o = run(i);
        assert_eq!(o.work_used, limit.min(9));
        assert_eq!(o.expanded_states, 0);
        assert_eq!(
            o.status,
            if limit < 9 {
                Status::WorkExhausted
            } else {
                Status::Complete
            }
        );
        assert!(o.added_tracks.is_empty());
    }
}
#[test]
fn handled_nested_errors_and_rollbacks_do_not_refund_work() {
    let mut i = fixture(false);
    i.limits.max_work = 15;
    let mut e = Engine::new(i).unwrap();
    e.transaction(|outer| {
        assert_eq!(outer.work_used(), 1);
        let rejected = outer.transaction(|child| {
            child.route(1, 1000).unwrap();
            // Child scope1 + route scope1/net1/enumeration1/partition4 =8.
            assert_eq!(child.work_used(), 9);
            Err::<(), _>("caller error")
        });
        assert_eq!(rejected, Err(TransactionError::Operation("caller error")));
        assert_eq!(outer.work_used(), 9);
        let failure = outer.route(1, MAX_EXPANSIONS).unwrap_err();
        assert_eq!(failure.status, Status::WorkExhausted);
        assert_eq!(failure.work_used, 15);
        assert_eq!(
            outer.reroute(1, MAX_EXPANSIONS).unwrap_err().status,
            Status::WorkExhausted
        );
        assert_eq!(outer.work_used(), 15);
        Ok::<_, ()>(())
    })
    .unwrap();
    assert_eq!(e.work_used(), 15);
    // A different outer operation gets a fresh limit; a child never does.
    e.route(1, 1000).unwrap();
    assert_eq!(e.work_used(), 7);
}
#[test]
fn batch_work_is_not_reset_between_nets() {
    let mut i = fixture(false);
    i.nets.push(Net {
        id: 2,
        name: "OTHER".into(),
    });
    let mut p = i.pads[0].clone();
    p.net = 2;
    p.at = [50, 30];
    p.id = "10000000-0000-4000-8000-000000000003".into();
    i.pads.push(p);
    // Scope1 + enumerate/sort four units + two*(dispatch1+scan2+partition4)=19.
    i.limits.max_work = 18;
    let o = run(i.clone());
    assert_eq!(o.status, Status::WorkExhausted);
    assert_eq!(o.work_used, 18);
    i.limits.max_work = 19;
    let o = run(i);
    assert_eq!(o.status, Status::Complete);
    assert_eq!(o.work_used, 19);
}
#[test]
fn exhausted_work_stops_before_quadratic_partition() {
    let mut i = fixture(false);
    i.pads.clear();
    for n in 0..1000 {
        i.tracks.push(Track {
            id: format!("10000000-0000-4000-8000-{n:012x}"),
            net: 1,
            start: [1, 1],
            end: [2, 1],
            width_nm: 2,
            locked: false,
            layer: "F.Cu".into(),
        });
    }
    i.limits.max_work = 20;
    let o = run(i);
    assert_eq!(o.status, Status::WorkExhausted);
    assert_eq!(o.work_used, 20);
    assert_eq!(o.expanded_states, 0);
    assert!(o.added_tracks.is_empty());
}
#[test]
fn standalone_clearance_query_charges_pruning_and_candidate_geometry_before_work() {
    let mut i = fixture(false);
    i.pads[0].net = 0;
    i.nets = vec![Net {
        id: 0,
        name: String::new(),
    }];
    for (y, needed) in [(10, 2), (30, 4)] {
        // Far: bounds1 + pruned root1. Near: bounds1 + root visit1 +
        // pad lookup1 + geometry1.
        for limit in 0..=needed {
            i.limits.max_work = limit;
            let e = Engine::new(i.clone()).unwrap();
            let result = e.legal_edge(1, [10, y], [50, y]);
            if limit < needed {
                let failure = result.unwrap_err();
                assert_eq!(failure.status, Status::WorkExhausted);
                assert_eq!(failure.work_used, limit);
            } else {
                assert_eq!(result.unwrap(), y == 10);
                assert_eq!(e.work_used(), needed);
            }
        }
    }
}
#[test]
fn journal_exhaustion_preserves_parent_and_allocator() {
    let mut i = fixture(true);
    i.limits.max_journal_entries = 1;
    let mut e = Engine::new(i.clone()).unwrap();
    let initial = bytes(&e);
    let result = e.transaction(|parent| {
        parent.route(1, 1000).unwrap();
        let saved = serde_json::to_vec(&parent.copper().generated().collect::<Vec<_>>()).unwrap();
        // The parent's insert already occupies the sole undo slot. Removal
        // must be refused before mutation, even inside a child scope.
        let f = parent.reroute(1, 1000).unwrap_err();
        assert_eq!(f.status, Status::JournalExhausted);
        assert_eq!(
            serde_json::to_vec(&parent.copper().generated().collect::<Vec<_>>()).unwrap(),
            saved
        );
        Err::<(), _>("outer rollback")
    });
    assert_eq!(result, Err(TransactionError::Operation("outer rollback")));
    assert_eq!(bytes(&e), initial);
    e.route(1, 1000).unwrap();
    let mut control = Engine::new(i).unwrap();
    control.route(1, 1000).unwrap();
    assert_eq!(bytes(&e), bytes(&control));
    // A fresh reroute can remove, but cannot then insert: rollback must restore
    // the removed track and derived index when no forward journal slot remains.
    let saved = bytes(&e);
    assert_eq!(
        e.reroute(1, 1000).unwrap_err().status,
        Status::JournalExhausted
    );
    assert_eq!(bytes(&e), saved);
}
#[test]
fn depth_admission_does_not_invoke_rejected_closure() {
    let mut i = fixture(false);
    i.limits.max_depth = 2;
    let mut e = Engine::new(i).unwrap();
    e.transaction(|outer| {
        outer
            .transaction(|inner| {
                let r =
                    inner.transaction(|_| -> Result<(), ()> { panic!("must not enter depth3") });
                assert!(matches!(
                    r,
                    Err(TransactionError::Limit(Failure {
                        status: Status::NestingExhausted,
                        ..
                    }))
                ));
                Ok::<_, ()>(())
            })
            .unwrap();
        outer.route(1, 1000).unwrap();
        Ok::<_, ()>(())
    })
    .unwrap();
}
#[test]
fn storage_admission_refuses_before_publication() {
    let mut i = fixture(true);
    i.limits.max_items = 2;
    assert_eq!(run(i.clone()).status, Status::AdmissionExceeded); // two pads + net
    i.limits.max_items = 3;
    let o = run(i.clone());
    assert_eq!(o.status, Status::AdmissionExceeded);
    assert!(o.added_tracks.is_empty());
    i.limits.max_items = 4;
    i.limits.max_search_entries = 0;
    let o = run(i.clone());
    assert_eq!(o.status, Status::AdmissionExceeded);
    assert_eq!(o.expanded_states, 0);
    i.limits.max_search_entries = 1000;
    i.limits.max_journal_entries = 0;
    let o = run(i);
    assert_eq!(o.status, Status::JournalExhausted);
    assert!(o.added_tracks.is_empty());
}
#[test]
fn repeated_reroutes_share_work_and_failed_outer_restores_everything() {
    let mut i = fixture(true);
    i.limits.max_work = 2000;
    let mut e = Engine::new(i.clone()).unwrap();
    e.route(1, 1000).unwrap();
    let before = bytes(&e);
    let failure = e
        .transaction(|outer| {
            let mut previous = outer.work_used();
            for _ in 0..64 {
                match outer.reroute(1, 1000) {
                    Ok(_) => {
                        assert!(outer.work_used() > previous);
                        previous = outer.work_used();
                    }
                    Err(e) => {
                        assert_eq!(e.status, Status::WorkExhausted);
                        assert_eq!(outer.work_used(), e.work_used);
                        return Err::<(), _>(e);
                    }
                }
            }
            panic!("64 reroutes escaped their outer work cap")
        })
        .unwrap_err();
    assert!(matches!(
        failure,
        TransactionError::Operation(Failure {
            status: Status::WorkExhausted,
            ..
        })
    ));
    assert_eq!(bytes(&e), before);
    e.reroute(1, 1000).unwrap();
    let mut control = Engine::new(i).unwrap();
    control.route(1, 1000).unwrap();
    control.reroute(1, 1000).unwrap();
    assert_eq!(bytes(&e), bytes(&control));
}
#[test]
fn unwind_preserves_payload_and_spent_work_then_allows_new_outer_scope() {
    let mut i = fixture(false);
    i.limits.max_work = 8;
    let mut e = Engine::new(i).unwrap();
    let p = catch_unwind(AssertUnwindSafe(|| {
        let _: Result<(), TransactionError<()>> = e.transaction(|tx| {
            tx.route(1, 1000).unwrap();
            std::panic::panic_any(42u32)
        });
    }))
    .unwrap_err();
    assert_eq!(*p.downcast::<u32>().unwrap(), 42);
    assert_eq!(e.work_used(), 8);
    e.route(1, 1000).unwrap();
    assert_eq!(e.work_used(), 7);
}
#[test]
fn compatible_json_defaults_and_typed_cli_refusal_agree() {
    let input = serde_json::json!({"version":1,"source_id":"a".repeat(64),"layer":"F.Cu",
        "bounds":{"min":[0,0],"max":[80,80]},"rules":{"width_nm":2,"clearance_nm":1,"grid_nm":10,"edge_clearance_nm":0},
        "nets":[],"pads":[],"tracks":[],"max_expansions":1000});
    let mut value = input.clone();
    assert_eq!(
        run_json(&serde_json::to_vec(&value).unwrap()).status,
        Status::Complete
    );
    value["limits"] = serde_json::json!({"max_work":0});
    let expected = run_json(&serde_json::to_vec(&value).unwrap());
    assert_eq!(expected.status, Status::WorkExhausted);
    assert_eq!(expected.work_used, 0);
    let dir = std::env::temp_dir().join(format!("rust-budget-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let source = dir.join("input.json");
    let output = dir.join("output.json");
    std::fs::write(&source, serde_json::to_vec(&value).unwrap()).unwrap();
    std::fs::write(&output, b"old candidate").unwrap();
    let status = std::process::Command::new(env!("CARGO_BIN_EXE_kicad-route-engine"))
        .args([&source, &output])
        .status()
        .unwrap();
    assert_eq!(status.code(), Some(5));
    let observed: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&output).unwrap()).unwrap();
    assert_eq!(observed, serde_json::to_value(expected).unwrap());
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn every_work_cutoff_rolls_back_component_join_and_id_allocation() {
    let mut i = fixture(true);
    // Force general component search rather than the two-pad fast path.
    i.pads.pop();
    i.tracks.push(Track {
        id: "10000000-0000-4000-8000-000000000003".into(),
        net: 1,
        start: [45, 11],
        end: [45, 49],
        width_nm: 2,
        locked: false,
        layer: "F.Cu".into(),
    });
    i.nets.push(Net {
        id: 2,
        name: "CHEAP".into(),
    });
    for (n, x) in [(4, 10), (5, 20)] {
        let mut p = i.pads[0].clone();
        p.id = format!("10000000-0000-4000-8000-{n:012x}");
        p.net = 2;
        p.at = [x, 60];
        i.pads.push(p);
    }
    let mut baseline = Engine::new(i.clone()).unwrap();
    baseline.route(1, 1000).unwrap();
    let complete_work = baseline.work_used();
    let mut cheap = Engine::new(i.clone()).unwrap();
    cheap.route(2, 1000).unwrap();
    let cheap_work = cheap.work_used();
    let expected = bytes(&cheap);
    for limit in 0..=complete_work {
        i.limits.max_work = limit;
        let mut e = Engine::new(i.clone()).unwrap();
        let initial = bytes(&e);
        let r = e.route(1, 1000);
        if limit == complete_work {
            r.unwrap();
            assert_eq!(bytes(&e), bytes(&baseline));
        } else {
            let f = r.unwrap_err();
            assert_eq!(f.status, Status::WorkExhausted, "limit={limit}");
            assert!(f.work_used <= limit);
            assert_eq!(f.work_used, e.work_used());
            assert_eq!(bytes(&e), initial, "limit={limit}");
            if limit >= cheap_work {
                e.route(2, 1000).unwrap();
                assert_eq!(bytes(&e), expected, "allocator at limit={limit}");
            }
        }
    }
}
#[test]
fn nested_journal_exact_capacity_includes_inner_commits() {
    for capacity in [2, 3] {
        let mut i = fixture(true);
        i.limits.max_journal_entries = capacity;
        let mut e = Engine::new(i).unwrap();
        let initial = bytes(&e);
        let result = e.transaction(|outer| {
            outer.route(1, 1000).unwrap(); // one insert retained by parent
            let replacement = outer.reroute(1, 1000); // remove + insert need two more
            if capacity == 2 {
                assert_eq!(replacement.unwrap_err().status, Status::JournalExhausted);
            } else {
                replacement.unwrap();
            }
            Err::<(), _>(())
        });
        assert_eq!(result, Err(TransactionError::Operation(())));
        assert_eq!(bytes(&e), initial);
        e.route(1, 1000).unwrap();
        assert_eq!(
            e.copper().generated().next().unwrap().id,
            "aaaaaaaa-aaaa-8aaa-8aaa-000000000001"
        );
    }
}
#[test]
fn expansion_work_and_admission_failures_are_distinct() {
    let mut i = fixture(true);
    i.max_expansions = 1;
    let o = run(i.clone());
    assert_eq!(o.status, Status::BudgetExhausted);
    assert_eq!(o.expanded_states, 1);
    assert!(o.work_used > 0);
    i.limits.max_work = 0;
    let o = run(i.clone());
    assert_eq!(o.status, Status::WorkExhausted);
    assert_eq!(o.expanded_states, 0);
    i.limits.max_work = 1000;
    i.limits.max_depth = 0;
    let o = run(i);
    assert_eq!(o.status, Status::NestingExhausted);
    assert_eq!(o.work_used, 1);
    assert_eq!(o.expanded_states, 0);
}

#[test]
fn work_failure_between_nets_retains_expansions_already_performed() {
    let mut i = fixture(true);
    i.nets.push(Net {
        id: 2,
        name: "EMPTY".into(),
    });
    let mut control = Engine::new(i.clone()).unwrap();
    let expanded = control.route(1, 1000).unwrap();
    // Both use one scope. Batch enumerates/sorts two nets and dispatches one
    // (5 units) instead of selected-net membership's two units: difference3.
    i.limits.max_work = control.work_used() + 3;
    let expected_work = i.limits.max_work;
    let o = run(i);
    assert_eq!(o.status, Status::WorkExhausted);
    assert_eq!(o.expanded_states, expanded);
    assert_eq!(o.work_used, expected_work);
    assert!(o.added_tracks.is_empty());
}
#[test]
fn caught_child_panic_does_not_refund_work() {
    let mut i = fixture(false);
    i.limits.max_work = 16;
    let mut e = Engine::new(i).unwrap();
    e.transaction(|outer| {
        let p = catch_unwind(AssertUnwindSafe(|| {
            let _: Result<(), TransactionError<()>> = outer.transaction(|inner| {
                inner.route(1, 1000).unwrap();
                std::panic::panic_any(73u32)
            });
        }))
        .unwrap_err();
        assert_eq!(*p.downcast::<u32>().unwrap(), 73);
        assert_eq!(outer.work_used(), 9);
        outer.route(1, 1000).unwrap();
        assert_eq!(outer.work_used(), 16);
        assert_eq!(
            outer.route(1, 1000).unwrap_err().status,
            Status::WorkExhausted
        );
        Ok::<_, ()>(())
    })
    .unwrap();
}
#[test]
fn oversized_json_has_same_typed_refusal_in_cli_and_library() {
    let bytes = vec![b' '; 16 * 1024 * 1024 + 1];
    let expected = run_json(&bytes);
    assert_eq!(expected.status, Status::AdmissionExceeded);
    let dir = std::env::temp_dir().join(format!("rust-oversize-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let source = dir.join("input.json");
    let output = dir.join("output.json");
    std::fs::write(&source, bytes).unwrap();
    let status = std::process::Command::new(env!("CARGO_BIN_EXE_kicad-route-engine"))
        .args([&source, &output])
        .status()
        .unwrap();
    assert_eq!(status.code(), Some(6));
    let observed: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&output).unwrap()).unwrap();
    assert_eq!(observed, serde_json::to_value(expected).unwrap());
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn standalone_clearance_query_charges_input_and_generated_lookups() {
    // Part A: 2 foreign input tracks at exact AABB tangency & clearance equality.
    let mut input_world = fixture(false);
    input_world.pads.clear();
    input_world.nets = vec![
        Net {
            id: 0,
            name: String::new(),
        },
        Net {
            id: 1,
            name: "N".into(),
        },
    ];
    input_world.tracks = vec![
        Track {
            id: "10000000-0000-4000-8000-000000000010".into(),
            net: 0,
            start: [10, 33],
            end: [20, 33],
            width_nm: 2,
            locked: true,
            layer: "F.Cu".into(),
        },
        Track {
            id: "10000000-0000-4000-8000-000000000011".into(),
            net: 0,
            start: [30, 33],
            end: [40, 33],
            width_nm: 2,
            locked: true,
            layer: "F.Cu".into(),
        },
    ];
    // Bounds(1) + 1 input_spatial leaf(1) + 2 leaf entries(2) + 2 foreign track lookups/geom(2) = 6.
    for limit in 0..=6 {
        input_world.limits.max_work = limit;
        let e = Engine::new(input_world.clone()).unwrap();
        let res = e.legal_edge(1, [10, 30], [50, 30]);
        if limit < 6 {
            assert_eq!(res.unwrap_err().status, Status::WorkExhausted);
        } else {
            assert!(res.unwrap());
            assert_eq!(e.work_used(), 6);
        }
    }

    // Part B: 1 generated track at exact AABB tangency (2 pads share 1 leaf whose
    // envelope overlaps swept at y=52, though each individual pad's copper is disjoint).
    let mut gen1 = fixture(false);
    gen1.bounds = Bounds {
        min: [-50, 3],
        max: [80, 80],
    };
    gen1.rules.grid_nm = 50;
    gen1.pads[0].at = [0, 53];
    gen1.pads.push(Pad {
        id: "10000000-0000-4000-8000-000000000002".into(),
        net: 1,
        at: [50, 53],
        shape: Shape::Circle { radius_nm: 1 },
        drill: None,
        layers: Vec::new(),
        local_clearance_nm: None,
        zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
    });
    gen1.nets.push(Net {
        id: 2,
        name: "Q".into(),
    });
    let mut e1 = Engine::new(gen1).unwrap();
    e1.route(1, 1000).unwrap();
    assert_eq!(e1.copper().generated().count(), 1);
    // Bounds(1) + pad_spatial(1 leaf + 2 entries = 3) + generated_spatial root(1)
    // + generated slot lookup/geom(1) = 6.
    assert!(e1.legal_edge(2, [10, 50], [40, 50]).unwrap());
    assert_eq!(e1.work_used(), 6);
    // Pruned-root check on both pad_spatial leaf and generated_spatial root:
    // Bounds(1) + pruned pad leaf(1) + pruned generated treap root(1) = 3.
    assert!(e1.legal_edge(2, [10, 10], [40, 10]).unwrap());
    assert_eq!(e1.work_used(), 3);

    // Part C: 3 generated tracks at exact AABB tangency across separate nets.
    let mut gen3 = fixture(false);
    gen3.bounds = Bounds {
        min: [-10, 3],
        max: [100, 100],
    };
    gen3.rules.grid_nm = 5;
    gen3.pads.clear();
    gen3.nets.clear();
    for id in 1..=3u32 {
        gen3.nets.push(Net {
            id,
            name: format!("N{id}"),
        });
        let x0 = i64::from(id - 1) * 10;
        gen3.pads.push(Pad {
            id: format!("10000000-0000-4000-8000-{:012x}", id * 2 - 1),
            net: id,
            at: [x0, 33],
            shape: Shape::Circle { radius_nm: 1 },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
        });
        gen3.pads.push(Pad {
            id: format!("10000000-0000-4000-8000-{:012x}", id * 2),
            net: id,
            at: [x0 + 5, 33],
            shape: Shape::Circle { radius_nm: 1 },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
        });
    }
    gen3.nets.push(Net {
        id: 4,
        name: "Q".into(),
    });
    let mut e3 = Engine::new(gen3).unwrap();
    for id in 1..=3u32 {
        e3.route(id, 1000).unwrap();
    }
    assert_eq!(e3.copper().generated().count(), 3);
    // Bounds(1) + pad_spatial(1 leaf + 6 entries + 6 foreign pads = 13)
    // + generated_spatial(3 treap nodes + 3 foreign slot lookups/geom = 6) = 20.
    assert!(e3.legal_edge(4, [0, 30], [25, 30]).unwrap());
    assert_eq!(e3.work_used(), 20);

    // Part D: Pruned non-root treap subtree (seq=3 at [20..25, 33], right child of root seq=2).
    // Foreign net=4: Bounds(1) + pad_spatial(1 leaf + 6 entries + 2 hits = 9)
    // + generated_spatial(seq=2: 1, seq=1: 1 + 1 slot = 2, seq=3 pruned: 1) = 14.
    assert!(e3.legal_edge(4, [0, 30], [5, 30]).unwrap());
    assert_eq!(e3.work_used(), 14);
    // Same-net net=1: pad[0..2] skipped inside LeafEntry (1 + 6 = 7), seq=1 skipped via (slot, net) (1 + 1 + 1 = 3) => 11.
    assert!(e3.legal_edge(1, [0, 30], [5, 30]).unwrap());
    assert_eq!(e3.work_used(), 11);
}

#[test]
fn static_spatial_internal_and_leaf_nodes_charge_before_pruning() {
    let mut input = fixture(false);
    input.bounds = Bounds {
        min: [-10, 0],
        max: [700, 50],
    };
    input.pads.clear();
    input.nets = vec![
        Net {
            id: 0,
            name: String::new(),
        },
        Net {
            id: 1,
            name: "N1".into(),
        },
    ];
    for n in 0..33 {
        let x = n * 20;
        input.tracks.push(Track {
            id: format!("20000000-0000-4000-8000-{n:012x}"),
            net: 0,
            start: [x, 33],
            end: [x + 10, 33],
            width_nm: 2,
            locked: true,
            layer: "F.Cu".into(),
        });
    }
    // 33 items => 1 Internal root + 2 Leaves (16 and 17 entries).
    // Far query [0, 10] -> [500, 10] prunes at the Internal root:
    // Bounds(1) + pruned Internal root(1) = 2.
    for limit in 0..=2 {
        input.limits.max_work = limit;
        let e = Engine::new(input.clone()).unwrap();
        let res = e.legal_edge(1, [0, 10], [500, 10]);
        if limit < 2 {
            assert_eq!(res.unwrap_err().status, Status::WorkExhausted);
        } else {
            assert!(res.unwrap());
            assert_eq!(e.work_used(), 2);
        }
    }
    // Left-leaf-only query [0, 30] -> [10, 30]:
    // Bounds(1) + Internal root(1) + Left Leaf(1) + 16 LeafEntry(16) + 1 hit(1) + pruned Right Leaf(1) = 21.
    for limit in 0..=21 {
        input.limits.max_work = limit;
        let e = Engine::new(input.clone()).unwrap();
        let res = e.legal_edge(1, [0, 30], [10, 30]);
        if limit < 21 {
            assert_eq!(res.unwrap_err().status, Status::WorkExhausted);
        } else {
            assert!(res.unwrap());
            assert_eq!(e.work_used(), 21);
        }
    }
}

#[test]
fn reroute_charges_spatial_index_mutation_work_before_search() {
    let mut i = fixture(true);
    i.nets.push(Net {
        id: 2,
        name: "N2".into(),
    });
    for (idx, x) in [(3usize, 10i64), (4, 50)] {
        let mut p = i.pads[0].clone();
        p.id = format!("10000000-0000-4000-8000-{idx:012x}");
        p.net = 2;
        p.at = [x, 50];
        i.pads.push(p);
    }
    let mut base = Engine::new(i.clone()).unwrap();
    let setup_work = base
        .transaction(|outer| {
            outer.route(1, 1000)?;
            outer.route(2, 1000)?;
            Ok::<_, Failure>(outer.work_used())
        })
        .unwrap();
    assert_eq!(base.copper().generated().count(), 2);

    // Height of 2-node treap is 2, so mutation_work = 8 * (2 + 1) = 24.
    // Pre-charge inside reroute(1, 1): begin(1) + route_selected nets(2) + count(1) = 4.
    // remove_net then atomically charges 1 + 24 = 25 before remove_raw / route_net.
    let pre_charge = 4;
    i.limits.max_work = setup_work + pre_charge + 24;
    let mut e = Engine::new(i).unwrap();
    e.transaction(|outer| {
        outer.route(1, 1000).unwrap();
        outer.route(2, 1000).unwrap();
        assert_eq!(outer.work_used(), setup_work);
        let failure = outer.reroute(1, 1).unwrap_err();
        assert_eq!(failure.status, Status::WorkExhausted);
        assert_eq!(failure.expanded_states, 0);
        assert_eq!(failure.work_used, setup_work + pre_charge);
        assert_eq!(outer.copper().generated().count(), 2);
        Ok::<_, Failure>(())
    })
    .unwrap();
}

#[test]
fn route_insertion_charges_spatial_index_mutation_work() {
    let mut i = fixture(false);
    let mut p = i.pads[0].clone();
    p.id = "10000000-0000-4000-8000-000000000002".into();
    p.at = [20, 30];
    i.pads.push(p);

    // Exact 1-step route(1, 1000) work on this 2-pad fixture (includes 8 units
    // for generated_spatial[0].mutation_work() at empty tree before insert, under
    // PathEdge-based walk_multilayer/insert_path).
    const EXACT_1STEP_WORK: u64 = 131;

    i.limits.max_work = EXACT_1STEP_WORK - 1;
    let mut cutoff = Engine::new(i.clone()).unwrap();
    let err = cutoff.route(1, 1000).unwrap_err();
    assert_eq!(err.status, Status::WorkExhausted);
    assert_eq!(cutoff.copper().generated().count(), 0);

    i.limits.max_work = EXACT_1STEP_WORK;
    let mut ok_engine = Engine::new(i).unwrap();
    ok_engine.route(1, 1000).unwrap();
    assert_eq!(ok_engine.work_used(), EXACT_1STEP_WORK);
    assert_eq!(ok_engine.copper().generated().count(), 1);
}

#[test]
fn static_uniform_net_bulk_charge_exhausts_step_by_step() {
    let base_input = || -> Input {
        let mut i = fixture(false);
        i.bounds = Bounds {
            min: [0, 0],
            max: [100, 100],
        };
        i.rules.grid_nm = 1;
        i.nets = vec![
            Net {
                id: 0,
                name: String::new(),
            },
            Net {
                id: 1,
                name: "N1".into(),
            },
            Net {
                id: 2,
                name: "N2".into(),
            },
        ];
        i.pads.clear();
        i.tracks.clear();
        i
    };

    // Setup 1: 4 same-net pads on net=1, empty tracks.
    let mut s1 = base_input();
    for (idx, x) in [10i64, 20, 30, 40].into_iter().enumerate() {
        s1.pads.push(Pad {
            id: format!("10000000-0000-4000-8000-{:012x}", idx + 1),
            net: 1,
            at: [x, 33],
            shape: Shape::Circle { radius_nm: 1 },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
        });
    }
    for limit in 0..=6u64 {
        s1.limits.max_work = limit;
        let e = Engine::new(s1.clone()).unwrap();
        let res = e.legal_edge(1, [10, 30], [40, 30]);
        if limit < 6 {
            let err = res.unwrap_err();
            assert_eq!(err.status, Status::WorkExhausted);
            assert_eq!(err.work_used, limit);
            assert_eq!(e.work_used(), limit);
        } else {
            assert!(res.unwrap());
            assert_eq!(e.work_used(), 6);
        }
    }
    s1.limits.max_work = Limits::default().max_work;
    let e1 = Engine::new(s1.clone()).unwrap();
    assert!(e1.legal_edge(2, [10, 30], [40, 30]).unwrap());
    assert_eq!(e1.work_used(), 10);
    assert!(!e1.legal_edge(2, [10, 31], [40, 31]).unwrap());
    assert_eq!(e1.work_used(), 4);
    assert!(e1.legal_edge(0, [10, 30], [40, 30]).unwrap());
    assert_eq!(e1.work_used(), 10);

    // Setup 2: 4 same-net input tracks on net=1, empty pads.
    let mut s2 = base_input();
    for (idx, x) in [10i64, 20, 30, 40].into_iter().enumerate() {
        s2.tracks.push(Track {
            id: format!("20000000-0000-4000-8000-{:012x}", idx + 1),
            net: 1,
            start: [x, 33],
            end: [x + 5, 33],
            width_nm: 2,
            locked: true,
            layer: "F.Cu".into(),
        });
    }
    for limit in 0..=6u64 {
        s2.limits.max_work = limit;
        let e = Engine::new(s2.clone()).unwrap();
        let res = e.legal_edge(1, [10, 30], [40, 30]);
        if limit < 6 {
            let err = res.unwrap_err();
            assert_eq!(err.status, Status::WorkExhausted);
            assert_eq!(err.work_used, limit);
            assert_eq!(e.work_used(), limit);
        } else {
            assert!(res.unwrap());
            assert_eq!(e.work_used(), 6);
        }
    }
    s2.limits.max_work = Limits::default().max_work;
    let e2 = Engine::new(s2).unwrap();
    assert!(e2.legal_edge(2, [10, 30], [40, 30]).unwrap());
    assert_eq!(e2.work_used(), 10);
    assert!(e2.legal_edge(0, [10, 30], [40, 30]).unwrap());
    assert_eq!(e2.work_used(), 10);

    // Setup 3: net=0 uniform input tracks & mixed-net pads.
    let mut s3a = base_input();
    for (idx, x) in [10i64, 20, 30, 40].into_iter().enumerate() {
        s3a.tracks.push(Track {
            id: format!("30000000-0000-4000-8000-{:012x}", idx + 1),
            net: 0,
            start: [x, 33],
            end: [x + 5, 33],
            width_nm: 2,
            locked: true,
            layer: "F.Cu".into(),
        });
    }
    let e3a = Engine::new(s3a).unwrap();
    assert!(e3a.legal_edge(0, [10, 30], [40, 30]).unwrap());
    assert_eq!(e3a.work_used(), 10);

    let mut s3b = base_input();
    for (idx, (x, net)) in [(10i64, 1u32), (20, 1), (30, 2), (40, 1)]
        .into_iter()
        .enumerate()
    {
        s3b.pads.push(Pad {
            id: format!("40000000-0000-4000-8000-{:012x}", idx + 1),
            net,
            at: [x, 33],
            shape: Shape::Circle { radius_nm: 1 },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
        });
    }
    let e3b = Engine::new(s3b).unwrap();
    assert!(e3b.legal_edge(1, [10, 30], [40, 30]).unwrap());
    assert_eq!(e3b.work_used(), 7);
}

#[test]
fn generated_uniform_net_and_net0_work_accounting() {
    // 2 pads on net=1 routed -> 1 generated track; net=0 input track blocks legal_edge(0, ...).
    let mut i = fixture(true);
    i.bounds = Bounds {
        min: [0, 0],
        max: [100, 100],
    };
    i.nets = vec![
        Net {
            id: 0,
            name: String::new(),
        },
        Net {
            id: 1,
            name: "N1".into(),
        },
        Net {
            id: 2,
            name: "N2".into(),
        },
    ];
    i.tracks.push(Track {
        id: "50000000-0000-4000-8000-000000000001".into(),
        net: 0,
        start: [30, 50],
        end: [30, 70],
        width_nm: 2,
        locked: true,
        layer: "F.Cu".into(),
    });
    let mut e = Engine::new(i).unwrap();
    e.route(1, 1000).unwrap();
    assert_eq!(e.copper().generated().count(), 1);
    assert!(!e.legal_edge(0, [20, 60], [40, 60]).unwrap());
    // Bounds(1) + pruned pad leaf(1) + input_spatial leaf(1) + 1 entry(1) + 1 geom hit(1) = 5.
    assert_eq!(e.work_used(), 5);
    assert!(!e.legal_edge(2, [30, 20], [30, 40]).unwrap());
    assert_eq!(e.work_used(), 7);
    assert!(e.legal_edge(1, [30, 20], [30, 40]).unwrap());
    assert_eq!(e.work_used(), 6);

    // Multi-segment generated uniform-net=1 route (3 pads on net=1 with a net=0 obstacle):
    // join 1 inserts >= 3 uniform net=1 segments into generated_spatial, then join 2
    // calls legal_edge(1, ...) during A* so every limit cutoff exercises SpatialIndex::visit_matching
    // bulk-charge WorkExhausted fallback through Engine::route.
    let mut multi = fixture(true);
    multi.bounds = Bounds {
        min: [0, 0],
        max: [100, 100],
    };
    multi.nets = vec![
        Net {
            id: 0,
            name: String::new(),
        },
        Net {
            id: 1,
            name: "N1".into(),
        },
    ];
    multi.pads.push(Pad {
        id: "50000000-0000-4000-8000-000000000002".into(),
        net: 0,
        at: [30, 30],
        shape: Shape::Circle { radius_nm: 3 },
        drill: None,
        layers: Vec::new(),
        local_clearance_nm: None,
        zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
    });
    multi.pads.push(Pad {
        id: "50000000-0000-4000-8000-000000000003".into(),
        net: 1,
        at: [50, 60],
        shape: Shape::Circle { radius_nm: 1 },
        drill: None,
        layers: Vec::new(),
        local_clearance_nm: None,
        zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
    });
    const MULTI_GEN_ROUTE_WORK: u64 = 2196;
    let mut probe = Engine::new(multi.clone()).unwrap();
    probe.route(1, 1000).unwrap();
    assert_eq!(probe.work_used(), MULTI_GEN_ROUTE_WORK);
    assert_eq!(probe.copper().generated().count(), 5);
    for limit in [1416u64, 1417, 1429, 1430, 1437, 1438] {
        multi.limits.max_work = limit;
        let mut em = Engine::new(multi.clone()).unwrap();
        let err = em.route(1, 1000).unwrap_err();
        assert_eq!(err.status, Status::WorkExhausted);
        assert_eq!(err.work_used, limit);
        assert_eq!(em.work_used(), limit);
        assert_eq!(em.copper().generated().count(), 0);
    }
}
