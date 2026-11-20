use super::*;
use crate::model::{DrillShape, Input, Shape};

const MM: i64 = 1_000_000;
const CONNECTOR: Point = [-71 * MM, 7 * MM];
const LOAD: Point = [69 * MM, 21 * MM];

// Two connected banks occupy one face. A narrow diagonal opening in In1
// is the only conductor passage across the center of the board.
fn backplane(closed: bool) -> Engine {
    let mut input: Input = serde_json::from_str(include_str!(
        "../../../tests/fixtures/multiterminal-native-two-nets.json"
    ))
    .unwrap();
    input.layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        .map(String::from)
        .to_vec();
    input.bounds.min = [-75 * MM, 0];
    input.bounds.max = [79 * MM, 40 * MM];
    input.rules.width_nm = 200_000;
    input.rules.clearance_nm = 150_000;
    let mut pin = input.pads[0].clone();
    let mut segment = input.tracks[0].clone();
    input.pads.clear();
    input.tracks.clear();
    pin.net = 1;
    pin.at = CONNECTOR;
    pin.layers = input.layers.clone();
    pin.drill = Some(DrillShape::Circle {
        diameter_nm: 600_000,
    });
    input.pads.push(pin.clone());
    pin.drill = None;
    pin.layers = vec!["F.Cu".into()];
    pin.shape = Shape::Rect {
        half_size_nm: [300_000, 250_000],
    };
    segment.net = 1;
    segment.layer = "F.Cu".into();
    segment.width_nm = input.rules.width_nm;
    for (row, y) in [LOAD[1], 33 * MM].into_iter().enumerate() {
        for i in 0..3 {
            let at = [LOAD[0] + i * 2 * MM, y];
            pin.id = format!("50000000-0000-4000-8000-{:012}", row * 10 + i as usize);
            pin.at = at;
            input.pads.push(pin.clone());
            if i > 0 {
                segment.id = format!("50000001-0000-4000-8000-{:012}", row * 10 + i as usize);
                segment.start = [at[0] - 2 * MM, y];
                segment.end = at;
                input.tracks.push(segment.clone());
            }
        }
    }
    let x = -65 * MM;
    let u = 61 * MM;
    let center = |xx| CONNECTOR[1] + (xx - CONNECTOR[0]) / 10;
    for (i, (layers, points)) in [
        (
            vec!["F.Cu", "In2.Cu", "B.Cu"],
            vec![[x, 0], [u, 0], [u, 40 * MM], [x, 40 * MM]],
        ),
        (
            vec!["In1.Cu"],
            vec![
                [x, 0],
                [u, 0],
                [u, center(u) - 410_000],
                [x, center(x) - 410_000],
            ],
        ),
        (
            vec!["In1.Cu"],
            vec![
                [x, center(x) + 410_000],
                [u, center(u) + 410_000],
                [u, 40 * MM],
                [x, 40 * MM],
            ],
        ),
    ]
    .into_iter()
    .enumerate()
    {
        input.rule_areas.push(serde_json::from_value(serde_json::json!({
            "id":format!("50000002-0000-4000-8000-{i:012}"),"layers":layers,"outer":points,"no_tracks":true,"no_vias":false
        })).unwrap());
    }
    if closed {
        input.rule_areas.push(
            serde_json::from_value(serde_json::json!({
                "id":"50000002-0000-4000-8000-000000000009","layers":["In1.Cu"],
                "outer":[[-5*MM,0],[-4*MM,0],[-4*MM,40*MM],[-5*MM,40*MM]],
                "no_tracks":true,"no_vias":false
            }))
            .unwrap(),
        );
    }
    let mut engine = Engine::new(input).unwrap();
    engine.set_prefer_higher_g(true);
    engine.surface_search_share = Some(256);
    engine
}

#[test]
fn many_target_copper_objects_can_reach_a_surface_bank_through_a_narrow_oblique_channel() {
    for closed in [false, true] {
        let engine = backplane(closed);
        let groups = engine.components(1).unwrap();
        assert_eq!(groups.len(), 3);
        let source = groups
            .iter()
            .position(|g| {
                g.iter()
                    .any(|c| matches!(c,super::super::Copper::Pad(p) if p.at==CONNECTOR))
            })
            .unwrap();
        let targets = groups
            .iter()
            .enumerate()
            .filter(|(i, _)| *i != source)
            .map(|(_, g)| g.clone())
            .collect::<Vec<_>>();
        assert_eq!(targets.iter().map(Vec::len).sum::<usize>(), 10);
        // The cheapest native lattice walk still needs more than 550 moves.
        // Keep the competing terminal probes and normal frontier in one trial.
        let mut expanded = 0;
        let mut frontier = BlockedFrontier::new();
        let result = super::super::join_components_inner(
            &engine,
            1,
            &groups[source],
            &targets,
            &mut TrialBudget::fixed(480),
            &mut expanded,
            None,
            Some(&mut frontier),
            true,
        );
        assert!(expanded <= 480);
        if closed {
            assert!(result.is_err());
            continue;
        }
        let path = result.unwrap();
        assert_eq!(path.points.first(), Some(&CONNECTOR));
        assert_eq!(path.points.last(), Some(&LOAD));
        let mut drills = Vec::new();
        let mut cost = 0u64;
        let grid = engine.input.rules.grid_nm as u64;
        for e in path.edges {
            match e {
                PathEdge::Track { start, end, layer } => {
                    assert!(engine.legal_edge_on(1, layer as usize, start, end).unwrap());
                    cost += (start[0].abs_diff(end[0]).div_ceil(grid)
                        + start[1].abs_diff(end[1]).div_ceil(grid))
                        * engine.layer_policy.weight(layer as usize);
                }
                PathEdge::Via(v) => {
                    assert!(engine
                        .legal_via(1, v.at, 0, 3, ViaKind::Through, &drills)
                        .unwrap()
                        .is_some());
                    cost += engine.layer_policy.scale(engine.effective_rules.via_cost);
                    drills.push(v);
                }
            }
        }
        assert_eq!(drills.len(), 1);
        assert_eq!(path.steps, cost);
    }
}

#[test]
fn a_limited_straight_probe_shares_the_trial_and_checks_inner_copper_history() {
    let engine = backplane(false);
    let target = TargetBounds::new(
        super::super::Copper::Pad(&engine.input.pads[1]),
        engine.all_layers_slice(),
        &engine.work,
    )
    .unwrap();
    let grid = engine.input.rules.grid_nm;
    let mut history = CongestionHistory::default();
    // Every conductor in the physical channel crosses x=0 on In1.
    for y in 0..=engine.input.bounds.max[1] / grid {
        history
            .add_weight((1, -engine.input.bounds.min[0] / grid, y), 1, &engine)
            .unwrap();
    }
    for (share, paid, expected) in [
        (0, false, false),
        (1, false, false),
        (64, false, true),
        (64, true, false),
    ] {
        let mut expanded = 37;
        let path = from_launch(
            &engine,
            1,
            &[(CONNECTOR, 1, false)],
            std::slice::from_ref(&target),
            &[true; 4],
            0,
            engine.effective_rules.via_cost,
            paid.then_some(&history),
            &mut TrialBudget::fixed(1000),
            &mut expanded,
            None,
            Some(37 + share),
        )
        .unwrap();
        assert_eq!(path.is_some(), expected);
        assert!(expanded <= 37 + share);
    }
}
