use kicad_route_engine::{model::*, run, Engine};
use std::collections::{BTreeMap, BTreeSet};
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
fn gcd(mut a: i64, mut b: i64) -> i64 {
    while b != 0 {
        let r = a % b;
        a = b;
        b = r;
    }
    a
}
// Independent center-line graph oracle for these deliberately integer-junction
// fixtures only. It does not call production contact/search/component code.
// It is stricter than copper overlap, not a native polygon connectivity model.
fn assert_centerline_connected(i: &Input, added: &[Track]) {
    let mut edges: BTreeMap<Point, Vec<Point>> = BTreeMap::new();
    let mut required = Vec::new();
    for p in &i.pads {
        if p.net == 1 {
            required.push(p.at);
            edges.entry(p.at).or_default();
        }
    }
    for t in i.tracks.iter().chain(added).filter(|t| t.net == 1) {
        required.extend([t.start, t.end]);
        let dx = t.end[0] - t.start[0];
        let dy = t.end[1] - t.start[1];
        let n = gcd(dx.abs(), dy.abs());
        assert!(n > 0);
        for step in 0..n {
            let a = [t.start[0] + dx / n * step, t.start[1] + dy / n * step];
            let b = [a[0] + dx / n, a[1] + dy / n];
            edges.entry(a).or_default().push(b);
            edges.entry(b).or_default().push(a);
        }
    }
    let mut reached = BTreeSet::new();
    let mut todo = vec![required[0]];
    while let Some(p) = todo.pop() {
        if reached.insert(p) {
            todo.extend(edges[&p].iter().copied());
        }
    }
    assert!(
        required.iter().all(|p| reached.contains(p)),
        "unjoined items: {required:?}; added={added:?}"
    );
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
    assert_centerline_connected(&i, &o.added_tracks);
    assert!(o.added_tracks.iter().any(|t| t.end == [35, 30])); // not either incumbent endpoint
    assert!(o.added_tracks.iter().all(|t| t.start != t.end));
}
#[test]
fn diagonal_off_grid_incumbent_attaches_at_its_interior() {
    let mut i = base();
    i.tracks[0].start = [33, 11];
    i.tracks[0].end = [37, 49];
    let o = run(i.clone());
    assert_eq!(o.status, Status::Complete);
    assert!(o.added_tracks.iter().any(|t| t.end == [35, 30]));
    assert_centerline_connected(&i, &o.added_tracks);
}
#[test]
fn connected_pads_do_not_hide_a_stray_copper_component() {
    let mut i = base();
    i.pads.push(pad(3, [20, 30]));
    i.tracks.push(track(4, [10, 30], [20, 30]));
    let o = run(i.clone());
    assert_eq!(o.status, Status::Complete);
    assert!(!o.added_tracks.is_empty());
    assert_centerline_connected(&i, &o.added_tracks);
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
    assert_centerline_connected(&i, &o.added_tracks);
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
    assert_centerline_connected(&i, &generated(&e));
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
    assert_centerline_connected(&i, &generated(&e));
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
