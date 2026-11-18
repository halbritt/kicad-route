use kicad_route_engine::{model::*, state::CopperStore, Engine, Transaction, TransactionError};
use std::{
    collections::BTreeMap,
    panic::{catch_unwind, AssertUnwindSafe},
};

fn allocated(n: u64) -> String {
    format!("aaaaaaaa-aaaa-8aaa-8aaa-{n:012x}")
}
fn fixture() -> Input {
    let mut pads = Vec::new();
    for net in 1..=3 {
        for (j, x) in [10, 50].into_iter().enumerate() {
            pads.push(Pad {
                id: format!("10000000-0000-4000-8000-{:012x}", net * 2 + j as u32),
                net,
                at: [x, net as i64 * 20 - 10],
                shape: Shape::Circle { radius_nm: 1 },
                drill: None,
                layers: Vec::new(),
                local_clearance_nm: None,
            });
        }
    }
    pads[0].id = allocated(1); // collision at first allocation
    Input {
        version: 1,
        source_id: "a".repeat(64),
        layer: "F.Cu".into(),
        layers: Vec::new(),
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
        pads,
        tracks: vec![Track {
            id: allocated(4),
            net: 0,
            start: [0, 0],
            end: [60, 0],
            width_nm: 2,
            locked: false,
            layer: "F.Cu".into(),
        }],
        max_expansions: 1000,
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
fn bytes(copper: &CopperStore) -> Vec<u8> {
    assert!(copper.index_consistent());
    serde_json::to_vec(&copper.tracks().collect::<Vec<_>>()).unwrap()
}

#[test]
fn inner_commit_outer_error_restores_bytes_and_allocator() {
    let mut e = Engine::new(fixture()).unwrap();
    let before = bytes(e.copper());
    let err = e
        .transaction(|outer| {
            outer.route(1, 1000).unwrap();
            outer
                .transaction(|inner| {
                    inner.route(2, 1000).unwrap();
                    inner.reroute(1, 1000).unwrap();
                    Ok::<_, &str>(())
                })
                .unwrap();
            assert_eq!(outer.copper().generated().count(), 2);
            Err::<(), _>("reject batch")
        })
        .unwrap_err();
    assert_eq!(err, TransactionError::Operation("reject batch"));
    assert_eq!(bytes(e.copper()), before);
    e.route_all().unwrap();
    let mut control = Engine::new(fixture()).unwrap();
    control.route_all().unwrap();
    assert_eq!(bytes(e.copper()), bytes(control.copper()));
}

#[test]
fn inner_rollback_outer_success_keeps_only_outer_changes() {
    let mut e = Engine::new(fixture()).unwrap();
    e.transaction(|outer| {
        outer.route(1, 1000)?;
        let before = bytes(outer.copper());
        let rejected = outer.transaction(|inner| {
            inner.reroute(1, 1000).unwrap();
            inner.route(2, 1000).unwrap();
            Err::<(), _>("undo inner")
        });
        assert_eq!(rejected, Err(TransactionError::Operation("undo inner")));
        assert_eq!(bytes(outer.copper()), before);
        outer.route(3, 1000)?;
        Ok::<_, Failure>(())
    })
    .unwrap();
    let mut control = Engine::new(fixture()).unwrap();
    control.route(1, 1000).unwrap();
    control.route(3, 1000).unwrap();
    assert_eq!(bytes(e.copper()), bytes(control.copper()));
}

#[test]
fn three_levels_remove_reinsert_then_middle_rollback_preserves_outer() {
    let mut e = Engine::new(fixture()).unwrap();
    e.route_all().unwrap();
    e.transaction(|outer| {
        outer.reroute(1, 1000).unwrap();
        let before = bytes(outer.copper());
        assert_eq!(
            outer.transaction(|middle| {
                middle.reroute(2, 1000).unwrap();
                middle
                    .transaction(|inner| {
                        inner.reroute(1, 1000).unwrap();
                        inner.reroute(3, 1000).unwrap();
                        Ok::<_, &str>(())
                    })
                    .unwrap();
                Err::<(), _>("reject middle")
            }),
            Err(TransactionError::Operation("reject middle"))
        );
        assert_eq!(bytes(outer.copper()), before);
        Ok::<_, &str>(())
    })
    .unwrap();
    let mut control = Engine::new(fixture()).unwrap();
    control.route_all().unwrap();
    control.reroute(1, 1000).unwrap();
    assert_eq!(bytes(e.copper()), bytes(control.copper()));
    // The next allocation also agrees, beyond checking visible track bytes.
    e.reroute(2, 1000).unwrap();
    control.reroute(2, 1000).unwrap();
    assert_eq!(bytes(e.copper()), bytes(control.copper()));
}

#[test]
fn failed_reroute_budget_inside_outer_preserves_work_and_can_continue() {
    let mut e = Engine::new(fixture()).unwrap();
    e.transaction(|outer| {
        outer.route(1, 1000).unwrap();
        let before = bytes(outer.copper());
        let failure = outer.reroute(1, 1).unwrap_err();
        assert_eq!(failure.status, Status::BudgetExhausted);
        assert_eq!(failure.expanded_states, 1);
        assert_eq!(bytes(outer.copper()), before);
        for (net, budget) in [(0, 10), (99, 10), (1, 0), (1, MAX_EXPANSIONS + 1)] {
            assert_eq!(
                outer.route(net, budget).unwrap_err().status,
                Status::Invalid
            );
            assert_eq!(
                outer.reroute(net, budget).unwrap_err().status,
                Status::Invalid
            );
            assert_eq!(bytes(outer.copper()), before);
        }
        outer.route(2, 1000).unwrap();
        Ok::<_, ()>(())
    })
    .unwrap();
    let mut control = Engine::new(fixture()).unwrap();
    control.route(1, 1000).unwrap();
    control.route(2, 1000).unwrap();
    assert_eq!(bytes(e.copper()), bytes(control.copper()));
}

#[test]
fn panic_unwinds_all_scopes_without_swallowing_payload() {
    let mut e = Engine::new(fixture()).unwrap();
    e.route(1, 1000).unwrap();
    let before = bytes(e.copper());
    let panic = catch_unwind(AssertUnwindSafe(|| {
        let _: Result<(), TransactionError<()>> = e.transaction(|outer| {
            outer.reroute(1, 1000).unwrap();
            outer
                .transaction(|inner| {
                    inner.route_all().unwrap();
                    Ok::<_, ()>(())
                })
                .unwrap();
            std::panic::panic_any(1484u32);
        });
    }))
    .unwrap_err();
    assert_eq!(*panic.downcast::<u32>().unwrap(), 1484);
    assert_eq!(bytes(e.copper()), before);
    e.route(2, 1000).unwrap();
    let mut control = Engine::new(fixture()).unwrap();
    control.route(1, 1000).unwrap();
    control.route(2, 1000).unwrap();
    assert_eq!(bytes(e.copper()), bytes(control.copper()));
}

#[test]
fn caught_inner_panic_rolls_back_inner_and_outer_can_commit() {
    let mut e = Engine::new(fixture()).unwrap();
    e.transaction(|outer| {
        outer.route(1, 1000).unwrap();
        let before = bytes(outer.copper());
        assert!(catch_unwind(AssertUnwindSafe(|| {
            let _: Result<(), TransactionError<()>> = outer.transaction(|inner| {
                inner.reroute(1, 1000).unwrap();
                inner.route(2, 1000).unwrap();
                panic!("inner panic");
            });
        }))
        .is_err());
        assert_eq!(bytes(outer.copper()), before);
        outer.route(3, 1000).unwrap();
        Ok::<_, ()>(())
    })
    .unwrap();
    let mut control = Engine::new(fixture()).unwrap();
    control.route(1, 1000).unwrap();
    control.route(3, 1000).unwrap();
    assert_eq!(bytes(e.copper()), bytes(control.copper()));
}

// Independent model for these three straight, noninteracting routes. Only test
// code clones a whole expected state at scope entry; production uses undo.
#[derive(Clone, Default)]
struct Oracle {
    tracks: BTreeMap<u32, Track>,
    next: u64,
}
impl Oracle {
    fn route(&mut self, net: u32, replace: bool) {
        if replace {
            self.tracks.remove(&net);
        }
        if self.tracks.contains_key(&net) {
            return;
        }
        while self.next == 0 || self.next == 1 || self.next == 4 {
            self.next += 1;
        }
        let y = net as i64 * 20 - 10;
        self.tracks.insert(
            net,
            Track {
                id: allocated(self.next),
                net,
                start: [10, y],
                end: [49, y],
                width_nm: 2,
                locked: false,
                layer: "F.Cu".into(),
            },
        );
        self.next += 1;
    }
    fn check(&self, c: &CopperStore) {
        assert!(c.index_consistent());
        let actual: BTreeMap<_, _> = c.generated().map(|t| (t.net, t.clone())).collect();
        assert_eq!(actual, self.tracks);
        assert_eq!(c.input(), fixture().tracks);
    }
}
fn step(seed: &mut u64) -> u64 {
    *seed = seed
        .wrapping_mul(6364136223846793005)
        .wrapping_add(1442695040888963407);
    *seed >> 32
}
fn history(tx: &mut Transaction<'_>, model: &mut Oracle, seed: &mut u64, depth: u32) {
    for _ in 0..8 {
        let net = (step(seed) % 3 + 1) as u32;
        match step(seed) % 5 {
            0 => {
                tx.route(net, 1000).unwrap();
                model.route(net, false);
            }
            1 => {
                tx.reroute(net, 1000).unwrap();
                model.route(net, true);
            }
            2 => {
                assert_eq!(
                    tx.reroute(net, 1).unwrap_err().status,
                    Status::BudgetExhausted
                );
            }
            _ if depth < 3 => {
                let snapshot = model.clone();
                let commit = step(seed) % 2 == 0;
                let result = tx.transaction(|inner| {
                    history(inner, model, seed, depth + 1);
                    if commit {
                        Ok(())
                    } else {
                        Err(())
                    }
                });
                if result.is_err() {
                    *model = snapshot;
                }
            }
            _ => {
                tx.route_all().unwrap();
                for n in 1..=3 {
                    model.route(n, false);
                }
            }
        }
        model.check(tx.copper());
    }
}
#[test]
fn deterministic_nested_histories_match_snapshot_oracle() {
    for initial_seed in 0..32 {
        let mut e = Engine::new(fixture()).unwrap();
        let mut model = Oracle::default();
        let mut seed = initial_seed;
        for _ in 0..4 {
            let snapshot = model.clone();
            let commit = step(&mut seed) % 2 == 0;
            let result = e.transaction(|tx| {
                history(tx, &mut model, &mut seed, 0);
                if commit {
                    Ok(())
                } else {
                    Err(())
                }
            });
            if result.is_err() {
                model = snapshot;
            }
            model.check(e.copper());
        }
        // Probe the allocator after the final rollback, even if no later random op did.
        e.reroute(3, 1000).unwrap();
        model.route(3, true);
        model.check(e.copper());
    }
}

#[test]
fn independent_engine_scopes_never_commit_or_rollback_each_other() {
    let mut a = Engine::new(fixture()).unwrap();
    let mut b = Engine::new(fixture()).unwrap();
    let initial_a = bytes(a.copper());
    let outer = a.transaction(|left| {
        left.route(1, 1000).unwrap();
        b.transaction(|right| {
            right.route(2, 1000).unwrap();
            Ok::<_, ()>(())
        })
        .unwrap();
        Err::<(), _>(())
    });
    assert!(outer.is_err());
    assert_eq!(bytes(a.copper()), initial_a);
    let mut control = Engine::new(fixture()).unwrap();
    control.route(2, 1000).unwrap();
    assert_eq!(bytes(b.copper()), bytes(control.copper()));
    a.route(2, 1000).unwrap();
    assert_eq!(bytes(a.copper()), bytes(control.copper()));
}

#[test]
fn failed_route_all_rolls_back_its_partial_batch_not_the_parent() {
    let mut input = fixture();
    input.max_expansions = 5; // enough for one straight route, not two
    let mut e = Engine::new(input).unwrap();
    e.transaction(|outer| {
        outer.route(1, 1000).unwrap();
        let before = bytes(outer.copper());
        // Net 1 already exists; batch inserts net 2 then exhausts its budget
        // on net 3. Its rollback must retain the parent's net 1.
        let failure = outer.route_all().unwrap_err();
        assert_eq!(failure.status, Status::BudgetExhausted);
        assert_eq!(failure.expanded_states, 5);
        assert_eq!(bytes(outer.copper()), before);
        outer.route(3, 1000).unwrap();
        Ok::<_, ()>(())
    })
    .unwrap();
    let mut control = Engine::new(fixture()).unwrap();
    control.route(1, 1000).unwrap();
    control.route(3, 1000).unwrap();
    assert_eq!(bytes(e.copper()), bytes(control.copper()));
}
