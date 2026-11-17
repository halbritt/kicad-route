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
                assert!(failure.work_used <= limit);
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
        },
        Track {
            id: "10000000-0000-4000-8000-000000000011".into(),
            net: 0,
            start: [30, 33],
            end: [40, 33],
            width_nm: 2,
            locked: true,
        },
    ];
    // Bounds(1) + 2 input_spatial visits(2) + 2 input lookups(2) + 2 geometry checks(2) = 7.
    for limit in 0..=7 {
        input_world.limits.max_work = limit;
        let e = Engine::new(input_world.clone()).unwrap();
        let res = e.legal_edge(1, [10, 30], [50, 30]);
        if limit < 7 {
            assert_eq!(res.unwrap_err().status, Status::WorkExhausted);
        } else {
            assert!(res.unwrap());
            assert_eq!(e.work_used(), 7);
        }
    }

    // Part B: 1 generated track at exact AABB tangency (pads disjoint in X from swept).
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
    });
    gen1.nets.push(Net {
        id: 2,
        name: "Q".into(),
    });
    let mut e1 = Engine::new(gen1).unwrap();
    e1.route(1, 1000).unwrap();
    assert_eq!(e1.copper().generated().count(), 1);
    // Bounds(1) + pad_spatial(root + pruned left = 2) + generated_spatial root(1)
    // + generated slot lookup(1) + segments_near(1) = 6.
    assert!(e1.legal_edge(2, [10, 50], [40, 50]).unwrap());
    assert_eq!(e1.work_used(), 6);

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
        });
        gen3.pads.push(Pad {
            id: format!("10000000-0000-4000-8000-{:012x}", id * 2),
            net: id,
            at: [x0 + 5, 33],
            shape: Shape::Circle { radius_nm: 1 },
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
    // Bounds(1) + 6 pads * (visit + lookup + segments = 3) + 3 generated * (visit + lookup + segments = 3) = 28.
    assert!(e3.legal_edge(4, [0, 30], [25, 30]).unwrap());
    assert_eq!(e3.work_used(), 28);
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
    // for generated_spatial.mutation_work() at empty tree before insert).
    const EXACT_1STEP_WORK: u64 = 134;

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
