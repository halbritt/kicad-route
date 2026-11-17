use kicad_route_engine::{model::*, run, Engine};
use std::collections::BTreeSet;
fn id(n: u64) -> String {
    format!("10000000-0000-4000-8000-{n:012x}")
}
fn pad(n: u64, at: Point) -> Pad {
    Pad {
        id: id(n),
        net: 1,
        at,
        shape: Shape::Circle { radius_nm: 1 },
    }
}
fn track(n: u64, a: Point, b: Point) -> Track {
    Track {
        id: id(n),
        net: 1,
        start: a,
        end: b,
        width_nm: 2,
        locked: true,
    }
}
fn base() -> Input {
    Input {
        version: 1,
        source_id: "a".repeat(64),
        layer: "F.Cu".into(),
        bounds: Bounds {
            min: [0, 0],
            max: [80, 80],
        },
        rules: Rules {
            width_nm: 2,
            clearance_nm: 1,
            grid_nm: 10,
            edge_clearance_nm: 0,
        },
        nets: vec![Net {
            id: 1,
            name: "N".into(),
        }],
        pads: vec![pad(1, [10, 30])],
        tracks: vec![track(2, [35, 11], [35, 49])],
        max_expansions: 10000,
    }
}
mod model {
    pub use kicad_route_engine::model::*;
}
#[path = "support/contact.rs"]
mod contact;
// Independent axis-aligned copper-contact graph. Endpoints need overlap,
// not identical center-line vertices. Diagonal control is checked separately.
fn assert_copper_connected(i: &Input, added: &[Track]) {
    let bodies: Vec<_> = i
        .pads
        .iter()
        .filter(|p| p.net == 1)
        .map(contact::Body::pad)
        .chain(
            i.tracks
                .iter()
                .chain(added)
                .filter(|t| t.net == 1)
                .map(contact::Body::track),
        )
        .collect();
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
    assert_eq!(reached.len(), bodies.len(), "copper not joined: {added:?}");
}
fn generated(e: &Engine) -> Vec<Track> {
    e.copper().generated().cloned().collect()
}
#[test]
fn singleton_pad_joins_incumbent_body_between_grid_nodes() {
    let i = base();
    let o = run(i.clone());
    assert_eq!(o.status, Status::Complete);
    assert!(!o.added_tracks.is_empty());
    assert_copper_connected(&i, &o.added_tracks);
    // radius1+radius1-1: earliest integer cap contact is x34.
    assert!(o.added_tracks.iter().any(|t| t.end == [34, 30]));
    assert!(o.added_tracks.iter().all(|t| t.start != t.end));
}
#[test]
fn diagonal_off_grid_incumbent_attaches_at_its_interior() {
    let mut i = base();
    i.tracks[0].start = [33, 11];
    i.tracks[0].end = [37, 49];
    let o = run(i.clone());
    assert_eq!(o.status, Status::Complete);
    assert_eq!(o.added_tracks.len(), 1);
    let t = &o.added_tracks[0];
    assert_eq!(t.start, [10, 30]);
    // Line vector(4,38); at(34,30), distance squared is38^2/1460<1.
    // At preceding integer(33,30),76^2/1460>1. No production predicate.
    assert_eq!(t.end, [34, 30]);
    assert!(38 * 38 < 4 * 4 + 38 * 38 && 76 * 76 > 4 * 4 + 38 * 38);
}
#[test]
fn connected_pads_do_not_hide_a_stray_copper_component() {
    let mut i = base();
    i.pads.push(pad(3, [20, 30]));
    i.tracks.push(track(4, [10, 30], [20, 30]));
    let o = run(i.clone());
    assert_eq!(o.status, Status::Complete);
    assert!(!o.added_tracks.is_empty());
    assert_copper_connected(&i, &o.added_tracks);
}
#[test]
fn three_track_only_components_with_off_grid_seeds_are_joined() {
    let mut i = base();
    i.pads.clear();
    i.tracks = vec![
        track(2, [11, 11], [21, 11]),
        track(3, [41, 1], [41, 41]),
        track(4, [61, 21], [61, 41]),
    ];
    let o = run(i.clone());
    assert_eq!(o.status, Status::Complete);
    assert_copper_connected(&i, &o.added_tracks);
    assert!(o
        .added_tracks
        .iter()
        .any(|t| t.start[0] % 10 != 0 || t.start[1] % 10 != 0));
    assert_eq!(
        serde_json::to_vec(&o).unwrap(),
        serde_json::to_vec(&run(i)).unwrap()
    );
}
#[test]
fn net_zero_stays_disconnected_and_blocks_other_net_joins() {
    let mut i = base();
    i.nets.push(Net {
        id: 0,
        name: String::new(),
    });
    let mut wall = track(9, [30, 0], [30, 80]);
    wall.net = 0;
    i.tracks.push(wall);
    let o = run(i.clone());
    assert_eq!(o.status, Status::Unroutable);
    assert!(o.added_tracks.is_empty());
    i.pads[0].net = 0;
    i.tracks[0].net = 0;
    let o = run(i);
    assert_eq!(o.status, Status::Complete);
    assert!(o.added_tracks.is_empty());
}
#[test]
fn budget_failure_after_first_component_join_restores_net_and_ids() {
    let first = base();
    let first_result = run(first.clone());
    assert_eq!(first_result.status, Status::Complete);
    let mut i = first;
    i.tracks.push(track(3, [65, 11], [65, 49]));
    let mut e = Engine::new(i.clone()).unwrap();
    let original = e.copper().input().to_vec();
    let err = e.route(1, first_result.expanded_states).unwrap_err();
    assert_eq!(err.status, Status::BudgetExhausted);
    assert_eq!(err.expanded_states, first_result.expanded_states);
    assert!(generated(&e).is_empty());
    assert_eq!(e.copper().input(), original);
    assert!(e.copper().index_consistent());
    e.route(1, 10000).unwrap();
    let expected = run(i.clone());
    assert_eq!(generated(&e), expected.added_tracks);
    assert_copper_connected(&i, &generated(&e));
    i.max_expansions = first_result.expanded_states;
    let o = run(i);
    assert_eq!(o.status, Status::BudgetExhausted);
    assert!(o.added_tracks.is_empty());
}
#[test]
fn outer_rollback_undoes_completed_and_rerouted_component_joins() {
    let mut i = base();
    i.tracks.push(track(3, [65, 11], [65, 49]));
    let mut e = Engine::new(i.clone()).unwrap();
    let outcome = e.transaction(|outer| {
        outer.route(1, 10000).unwrap();
        assert!(!outer.copper().generated().collect::<Vec<_>>().is_empty());
        outer
            .transaction(|inner| {
                inner.reroute(1, 10000).unwrap();
                Ok::<_, ()>(())
            })
            .unwrap();
        Err::<(), _>(())
    });
    assert!(outcome.is_err());
    assert!(generated(&e).is_empty());
    assert_eq!(e.copper().input(), i.tracks);
    assert!(e.copper().index_consistent());
    e.route_all().unwrap();
    assert_eq!(generated(&e), run(i.clone()).added_tracks);
    assert_copper_connected(&i, &generated(&e));
}
#[test]
fn conservative_tiny_width_contact_failure_is_not_complete() {
    let mut i = base();
    i.pads.clear();
    i.rules.width_nm = 1;
    i.tracks = vec![track(2, [11, 11], [21, 11]), track(3, [41, 11], [51, 11])];
    for t in &mut i.tracks {
        t.width_nm = 1;
    }
    let o = run(i);
    assert_eq!(o.status, Status::Unroutable);
    assert!(o.added_tracks.is_empty());
    assert!(o.reason.unwrap().contains("seed"));
}

#[test]
fn shortened_attachment_is_legal_even_when_full_grid_step_hits_wall() {
    let mut i = base();
    i.bounds = Bounds {
        min: [0, 0],
        max: [20, 20],
    };
    i.pads = vec![pad(1, [10, 10])];
    i.tracks = vec![track(2, [15, 5], [15, 15])];
    i.nets.push(Net {
        id: 0,
        name: String::new(),
    });
    let mut wall = track(3, [18, 1], [18, 19]);
    wall.net = 0;
    i.tracks.push(wall);
    let e = Engine::new(i.clone()).unwrap();
    assert!(e.legal_edge(1, [10, 10], [15, 10]));
    assert!(!e.legal_edge(1, [10, 10], [20, 10]));
    let o = run(i.clone());
    assert_eq!(o.status, Status::Complete);
    assert_eq!(o.added_tracks.len(), 1);
    assert_eq!(o.added_tracks[0].start, [10, 10]);
    assert_eq!(o.added_tracks[0].end, [14, 10]); // first 1nm contact, not centerline
    assert_eq!(o.expanded_states, 2); // seed, then one unit-cost shortened goal edge
    assert_copper_connected(&i, &o.added_tracks);
    let mut limited = i;
    limited.max_expansions = 1;
    let failed = run(limited);
    assert_eq!(failed.status, Status::BudgetExhausted);
    assert_eq!(failed.expanded_states, 1);
    assert!(failed.added_tracks.is_empty());
}

#[test]
fn clearance_shadowed_target_joins_at_first_contact() {
    // Fable F1: preserve the original dimensions and rules. The terminal cap
    // must overlap y6000000 by 1 nm, but cannot approach the foreign y6300000
    // centerline closer than 400000 nm. Thus y5800001..=5900000 is legal.
    let mut i = base();
    i.bounds.max = [10000000, 10000000];
    i.rules = Rules {
        width_nm: 300000,
        clearance_nm: 200000,
        grid_nm: 500000,
        edge_clearance_nm: 200000,
    };
    i.max_expansions = 200000;
    i.nets.push(Net {
        id: 2,
        name: "FOREIGN".into(),
    });
    i.pads = vec![Pad {
        id: id(1),
        net: 1,
        at: [5000000, 2000000],
        shape: Shape::Circle { radius_nm: 300000 },
    }];
    i.tracks = vec![
        Track {
            id: id(2),
            net: 1,
            start: [2000000, 6000000],
            end: [8000000, 6000000],
            width_nm: 100000,
            locked: false,
        },
        Track {
            id: id(3),
            net: 2,
            start: [2000000, 6300000],
            end: [8000000, 6300000],
            width_nm: 100000,
            locked: true,
        },
    ];
    let o = run(i.clone());
    assert_eq!(o.status, Status::Complete);
    assert_eq!(o.added_tracks.len(), 1);
    let t = &o.added_tracks[0];
    assert_eq!(t.start, [5000000, 2000000]);
    assert_eq!(t.end, [5000000, 5800001]);
    assert_copper_connected(&i, &o.added_tracks);
    assert!(!contact::Body::track(t).violates(contact::Body::track(&i.tracks[1]), 200000));
}
