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
fn standalone_clearance_query_charges_scan_and_geometry_before_work() {
    let mut i = fixture(false);
    i.pads[0].net = 0;
    i.nets = vec![Net {
        id: 0,
        name: String::new(),
    }];
    // Bounds check1, foreign pad visit1, geometry1. It is far from the edge.
    for limit in 0..=3 {
        i.limits.max_work = limit;
        let e = Engine::new(i.clone()).unwrap();
        let r = e.legal_edge(1, [10, 10], [50, 10]);
        if limit < 3 {
            let f = r.unwrap_err();
            assert_eq!(f.status, Status::WorkExhausted);
            assert_eq!(f.work_used, limit);
        } else {
            assert!(r.unwrap());
        }
        assert_eq!(e.work_used(), limit);
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
