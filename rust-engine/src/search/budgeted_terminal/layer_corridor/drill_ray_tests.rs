use super::*;
use crate::{model::Input, negotiate::BlockedFrontier, Engine};

const MM: i64 = 1_000_000;
const LEFT: Point = [3 * MM, 8 * MM];
const RIGHT: Point = [34 * MM, 12 * MM];

fn shielded_board(stop: Option<(bool, bool)>) -> Engine {
    let mut input: Input = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/multiterminal-native-two-nets.json"
    )))
    .unwrap();
    input.rules.width_nm = 200_000;
    input.rules.clearance_nm = 150_000;
    input.layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        .map(String::from)
        .to_vec();
    let mut shield = input.tracks.remove(0);
    input.tracks.clear();
    input.pads.truncate(2);
    for (pad, point) in input.pads.iter_mut().zip([LEFT, RIGHT]) {
        pad.net = 1;
        pad.at = point;
        pad.layers = vec!["F.Cu".into()];
    }
    // The fixture represents a shield carrier. Its reserved layers expose a
    // horizontal In1 passage. A previously routed shield conductor occupies
    // its lower half. The two pad rows face different drill windows.
    let mut areas = vec![
        (
            vec!["F.Cu", "In2.Cu", "B.Cu"],
            [7 * MM, 0, 28 * MM, 20 * MM],
            true,
            false,
        ),
        (vec!["In1.Cu"], [7 * MM, 0, 28 * MM, 9 * MM], true, false),
        (
            vec!["In1.Cu"],
            [7 * MM, 10 * MM, 28 * MM, 20 * MM],
            true,
            false,
        ),
        (vec!["F.Cu"], [0, 0, 6 * MM, 8_450_000], false, true),
        (
            vec!["F.Cu"],
            [32 * MM, 11_550_000, 36 * MM, 18 * MM],
            false,
            true,
        ),
        (vec!["F.Cu"], [32 * MM, 0, 36 * MM, 10_400_000], false, true),
    ];
    if let Some((no_tracks, no_vias)) = stop {
        let y = if no_tracks { 9_400_000 } else { 9_750_000 };
        areas.push((
            vec!["F.Cu"],
            [2_850_000, y - 10_000, 3_150_000, y + 10_000],
            no_tracks,
            no_vias,
        ));
    }
    for (i, (layers, [x, y, u, v], no_tracks, no_vias)) in areas.into_iter().enumerate() {
        input.rule_areas.push(
            serde_json::from_value(serde_json::json!({
                "id":format!("00000000-0000-4000-8000-{i:012}"),"layers":layers,
                "outer":[[x,y],[u,y],[u,v],[x,v]],"no_tracks":no_tracks,"no_vias":no_vias
            }))
            .unwrap(),
        );
    }
    let mut engine = Engine::new(input).unwrap();
    shield.net = 3;
    shield.start = [7 * MM, 9_250_000];
    shield.end = [28 * MM, 9_250_000];
    shield.width_nm = 200_000;
    shield.layer = "In1.Cu".into();
    shield.locked = false;
    engine.copper.insert(shield, &engine.work).unwrap();
    engine
}

#[test]
fn sliding_either_surface_escape_finds_the_inner_passage_beyond_the_first_drill() {
    for block in [None, Some((true, false)), Some((false, true))] {
        let engine = shielded_board(block);
        let lateral = [8 * MM, 9_500_000];
        let next = [9 * MM, 9_500_000];
        assert!(!engine.legal_edge_on(1, 1, lateral, next).unwrap());
        assert!(engine.last_block_generated());
        let mut frontier = BlockedFrontier::new();
        frontier.push_edge_classified(1, lateral, next, true);
        let mut fixed = BlockedFrontier::new();
        fixed.push_edge_classified(1, lateral, next, false);
        for (source_index, dest_index) in [(0, 1), (1, 0)] {
            let from = &engine.input.pads[source_index];
            let to = &engine.input.pads[dest_index];
            let target = crate::search::TargetBounds::new(
                crate::components::Copper::Pad(to),
                engine.all_layers_slice(),
                &engine.work,
            )
            .unwrap();
            for (evidence, limit, reachable) in [
                (&fixed, 3500, false),
                (&frontier, 12, false),
                (&frontier, 3500, block.is_none()),
            ] {
                let mut spent = 0;
                let path = through_launch_corridors(
                    &engine,
                    1,
                    &[crate::components::Copper::Pad(from)],
                    &[(from.at, 0, false)],
                    std::slice::from_ref(&target),
                    &[true; 4],
                    0,
                    engine.effective_rules.via_cost,
                    None,
                    &mut crate::negotiate::TrialBudget::fixed(limit),
                    &mut spent,
                    Some(evidence),
                )
                .unwrap();
                assert!(spent <= limit / (engine.input.nets.len() as u64));
                assert_eq!(
                    path.is_some(),
                    reachable,
                    "source {:?}, work {spent}",
                    from.at
                );
                if let Some(p) = path {
                    assert_eq!(p.points.first(), Some(&from.at));
                    assert_eq!(p.points.last(), Some(&to.at));
                    let mut holes = Vec::new();
                    let mut cost = 0;
                    for edge in p.edges {
                        match edge {
                            PathEdge::Track { start, end, layer } => {
                                assert!(engine
                                    .legal_edge_on(1, usize::from(layer), start, end)
                                    .unwrap());
                                let grid = engine.input.rules.grid_nm as u64;
                                cost += (start[0].abs_diff(end[0]).div_ceil(grid)
                                    + start[1].abs_diff(end[1]).div_ceil(grid))
                                    * engine.layer_policy.weight(usize::from(layer));
                            }
                            PathEdge::Via(v) => {
                                assert!(engine
                                    .legal_via(1, v.at, 0, 3, ViaKind::Through, &holes)
                                    .unwrap()
                                    .is_some());
                                cost += engine.layer_policy.scale(engine.effective_rules.via_cost);
                                holes.push(v);
                            }
                        }
                    }
                    assert_eq!(cost, p.steps);
                    assert_eq!(holes.len(), 2);
                    assert!(holes
                        .iter()
                        .any(|v| v.at[0] == LEFT[0] && v.at[1] == 9_750_000));
                }
            }
        }
    }
}
