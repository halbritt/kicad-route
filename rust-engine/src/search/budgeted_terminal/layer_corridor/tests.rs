use super::super::Terminal;
use super::*;
use crate::{model::Input, negotiate::CongestionHistory, Engine};

const GRID: i64 = 250_000;

fn board(guards: &[(&str, i64, i64)]) -> Engine {
    let mut input: Input = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/multiterminal-native-two-nets.json"
    )))
    .unwrap();
    input.pads.clear();
    input.tracks.clear();
    input.layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        .map(String::from)
        .to_vec();
    input.rules.width_nm = 200_000;
    input.rules.clearance_nm = 150_000;
    input.routing_options = Some(
        serde_json::from_value(serde_json::json!({
            "layer_policy": {"signal_layers":input.layers,"layer_costs":[1.0,2.0,3.0,1.0]}
        }))
        .unwrap(),
    );
    for (i, &(layer, x, max_x)) in guards.iter().enumerate() {
        input.rule_areas.push(
            serde_json::from_value(serde_json::json!({
                "id":format!("00000000-0000-4000-8000-{i:012}"), "layers":[layer],
                "outer":[[x,1_000_000],[max_x,1_000_000],[max_x,19_000_000],[x,19_000_000]],
                "no_tracks":true, "no_vias":false
            }))
            .unwrap(),
        );
    }
    Engine::new(input).unwrap()
}

fn hole(x: i64) -> PlacedViaKey {
    PlacedViaKey {
        at: [x, 8_000_000],
        start_layer: 0,
        end_layer: 3,
        kind: 0,
    }
}

fn join(engine: &Engine) -> Join<'_> {
    Join {
        engine,
        net: 1,
        source: Terminal {
            at: hole(2_250_000).at,
            layer: 0,
        },
        target: Terminal {
            at: hole(33_250_000).at,
            layer: 3,
        },
        via_step: engine.layer_policy.scale(engine.effective_rules.via_cost),
        history: None,
    }
}

#[test]
fn staggered_corridor_charges_drills_swept_history_and_only_permitted_layers() {
    let engine = board(&[
        ("In1.Cu", 9_000_000, 10_000_000),
        ("In2.Cu", 15_000_000, 16_000_000),
        ("In1.Cu", 23_000_000, 24_000_000),
        ("F.Cu", 7_000_000, 27_000_000),
        ("B.Cu", 7_000_000, 27_000_000),
    ]);
    let mut join = join(&engine);
    let a = hole(3_250_000);
    let b = hole(32_250_000);
    let corridor = Corridor::new(a.at, a.at, b.at, GRID);
    let solve = |j: &Join, flags: [bool; 4], expand: &mut dyn FnMut() -> Result<bool, Failure>| {
        j.follow_corridor(&corridor, a.into(), b.into(), &flags, 0, &mut || expand())
    };
    let allowed = [true; 4];
    let mut available = || Ok(true);
    let mut paid = 0;
    let clean = solve(&join, allowed, &mut || {
        paid += 1;
        Ok(true)
    })
    .unwrap()
    .unwrap();
    assert!(paid >= 5);
    let mut layer = join.source.layer;
    let mut free_transfer = false;
    let mut cursor = join.source.at;
    let mut prior = Vec::new();
    let mut cost = 0;
    for edge in &clean.edges {
        match *edge {
            PathEdge::Track {
                start,
                end,
                layer: track_layer,
            } => {
                let next = track_layer as usize;
                assert_eq!(start, cursor);
                assert!(free_transfer || layer == next);
                assert!(engine.legal_edge_on(1, next, start, end).unwrap());
                assert_eq!(start[1], end[1]);
                let steps = start[0].abs_diff(end[0]) / GRID as u64;
                cost += steps * engine.layer_policy.weight(next);
                cursor = end;
                layer = next;
                free_transfer = false;
            }
            PathEdge::Via(v) => {
                assert_eq!(cursor, v.at);
                assert!(engine
                    .legal_via(1, v.at, 0, 3, ViaKind::Through, &prior)
                    .unwrap()
                    .is_some());
                prior.push(v);
                cost += join.via_step;
                free_transfer = true;
            }
        }
    }
    assert_eq!((cursor, layer, prior.len()), (join.target.at, 3, 4));
    assert_eq!(clean.steps, cost);

    let mut history = CongestionHistory::default();
    history.add_weight((0, 11, 32), 7, &engine).unwrap(); // middle of the launch stub
    history.add_weight((3, 13, 32), 11, &engine).unwrap(); // rear face pierced by first drill
    history.add_weight((1, 13, 35), 19, &engine).unwrap(); // adjacent, unswept cell
    join.history = Some(&history);
    assert_eq!(
        solve(&join, allowed, &mut available)
            .unwrap()
            .unwrap()
            .steps,
        cost + engine.layer_policy.scale(18)
    );

    for k in [1, 2] {
        let mut restricted = allowed;
        restricted[k] = false;
        assert!(solve(&join, restricted, &mut available).unwrap().is_none());
    }
    let mut remaining = paid - 1;
    assert!(solve(&join, allowed, &mut || {
        let ok = remaining > 0;
        remaining -= i32::from(ok);
        Ok(ok)
    })
    .unwrap()
    .is_none());
    assert_eq!(remaining, 0);
    assert!(join
        .follow_corridor(
            &corridor,
            a.into(),
            b.into(),
            &allowed,
            engine.input.limits.max_search_entries,
            &mut available
        )
        .is_err());
}

#[test]
fn staggered_corridor_rejects_individually_legal_coincident_drill_apertures() {
    // In1 is the only incoming plane and In2 the only outgoing one.
    // One 250 um advance is available between the two keepouts. That site
    // cannot contain another hole next to the source drill.
    let engine = board(&[
        ("In1.Cu", 8_970_000, 9_500_000),
        ("In2.Cu", 8_000_000, 8_540_000),
    ]);
    let join = join(&engine);
    let b = hole(32_250_000);
    let flags = [false, true, true, false];
    for (x, accepted) in [(8_500_000, false), (7_000_000, true)] {
        let a = hole(x);
        let corridor = Corridor::new(a.at, a.at, b.at, GRID);
        assert!(engine
            .legal_via(1, a.at, 0, 3, ViaKind::Through, &[])
            .unwrap()
            .is_some());
        let middle = hole(8_750_000);
        assert!(engine
            .legal_via(1, middle.at, 0, 3, ViaKind::Through, &[])
            .unwrap()
            .is_some());
        assert_eq!(
            engine
                .legal_via(1, middle.at, 0, 3, ViaKind::Through, &[a])
                .unwrap()
                .is_some(),
            accepted
        );
        assert_eq!(
            join.follow_corridor(&corridor, a.into(), b.into(), &flags, 0, &mut || Ok(true))
                .unwrap()
                .is_some(),
            accepted
        );
    }
}

#[test]
fn existing_barrel_uses_only_layers_present_on_its_ends() {
    let engine = board(&[
        ("F.Cu", 5_000_000, 30_000_000),
        ("B.Cu", 5_000_000, 30_000_000),
    ]);
    let j = join(&engine);
    let a = j.source.at;
    let b = j.target.at;
    let c = Corridor::new(a, a, b, GRID);
    for (start, end, connected) in [(1 << 2, 1 << 2, true), (1 << 2, 1 << 1, false)] {
        let path = j
            .follow_corridor(
                &c,
                Access::Copper {
                    at: a,
                    layers: start,
                },
                Access::Copper { at: b, layers: end },
                &[true; 4],
                0,
                &mut || Ok(true),
            )
            .unwrap();
        assert_eq!(path.is_some(), connected);
        if let Some(path) = path {
            assert_eq!(path.points, [a, b]);
            assert!(matches!(
                path.edges.as_slice(),
                [PathEdge::Track { layer: 2, .. }]
            ));
            assert_eq!(
                path.steps,
                a[0].abs_diff(b[0]) / GRID as u64 * engine.layer_policy.weight(2)
            );
        }
    }
}

#[test]
fn unused_drill_sites_do_not_extend_or_price_a_surface_join_past_its_turn() {
    let engine = board(&[]);
    let source = [3_000_000, 6_000_000];
    let a = [7_000_000, 6_000_000];
    let corner = [7_000_000, 9_000_000];
    let b = [7_090_000, 9_000_000];
    let target = [3_090_000, 9_000_000];
    let access = |at| Access::NewVia(PlacedViaKey { at, ..hole(0) });
    for reverse in [false, true] {
        let (s, t, first, last) = if reverse {
            (target, source, b, a)
        } else {
            (source, target, a, b)
        };
        let mut join = join(&engine);
        join.source = Terminal { at: s, layer: 0 };
        join.target = Terminal { at: t, layer: 0 };
        let path = join
            .follow_corridor(
                &Corridor::new(first, corner, last, GRID),
                access(first),
                access(last),
                &[true, false, false, false],
                0,
                &mut || Ok(true),
            )
            .unwrap()
            .unwrap();
        let mut expected = vec![source, a, corner, target];
        if reverse {
            expected.reverse();
        }
        assert_eq!(path.points, expected);
        let expected_edges: Vec<_> = expected.windows(2).map(|p| track(p[0], p[1], 0)).collect();
        assert_eq!(path.edges, expected_edges);
        let steps: u64 = expected
            .windows(2)
            .map(|p| (p[0][0].abs_diff(p[1][0]) + p[0][1].abs_diff(p[1][1])).div_ceil(GRID as u64))
            .sum();
        assert_eq!(path.steps, steps * engine.layer_policy.weight(0));
    }

    let mut join = join(&engine);
    join.source = Terminal {
        at: source,
        layer: 0,
    };
    join.target = Terminal {
        at: target,
        layer: 3,
    };
    let crossing = join
        .follow_corridor(
            &Corridor::new(a, corner, b, GRID),
            access(a),
            access(b),
            &[true, false, false, true],
            0,
            &mut || Ok(true),
        )
        .unwrap()
        .unwrap();
    assert_eq!(
        crossing.edges,
        vec![
            track(source, a, 0),
            track(a, corner, 0),
            track(corner, b, 0),
            PathEdge::Via(access(b).via().unwrap()),
            track(b, target, 3),
        ]
    );
}

#[test]
fn a_shortened_landing_requires_a_clear_continuous_track() {
    let engine = board(&[("F.Cu", 6_000_000, 7_000_000)]);
    let guarded = join(&engine);
    let (start, tip, target) = (
        [4_000_000, 5_000_000],
        [10_000_000, 11_000_000],
        [8_000_000, 9_000_000],
    );
    let mut edges = vec![track(start, tip, 0)];
    assert!(!guarded
        .continue_without_drilling(&mut edges, tip, target, 0)
        .unwrap());
    assert_eq!(edges, vec![track(start, tip, 0)]);

    let clear = board(&[]);
    let join = join(&clear);
    assert!(join
        .continue_without_drilling(&mut edges, tip, target, 0)
        .unwrap());
    assert_eq!(edges, vec![track(start, target, 0)]);
    assert!(join
        .continue_without_drilling(&mut edges, target, start, 0)
        .unwrap());
    assert!(edges.is_empty());
}
