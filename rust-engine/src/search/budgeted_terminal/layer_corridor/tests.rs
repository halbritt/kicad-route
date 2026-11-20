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
        j.follow_corridor(&corridor, a, b, &flags, 0, &mut || expand())
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
            a,
            b,
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
            join.follow_corridor(&corridor, a, b, &flags, 0, &mut || Ok(true))
                .unwrap()
                .is_some(),
            accepted
        );
    }
}
