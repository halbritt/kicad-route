use super::*;
use crate::model::{DrillShape, Input, Shape};
use crate::negotiate::CongestionHistory;
use crate::{search::PlacedViaKey, Engine};

const PLATED: Point = [4_000_000, 3_000_000];
const DRILL: Point = [10_000_000, 17_000_000];
const SMT: Point = [11_250_000, 17_000_000];
const MM: i64 = 1_000_000;

fn board(blocked_drill: bool, blocked_trunk: bool, outward: bool) -> Engine {
    let mut input: Input = serde_json::from_str(include_str!(
        "../../../../tests/fixtures/multiterminal-native-two-nets.json"
    ))
    .unwrap();
    input.layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        .map(String::from)
        .to_vec();
    input.rules.width_nm = 200_000;
    input.rules.clearance_nm = 150_000;
    input.rules.same_net_hole_clearance_nm = 400_000;
    input.tracks.clear();
    input.pads.truncate(2);
    let plated = &mut input.pads[0];
    plated.at = PLATED;
    plated.layers = input.layers.clone();
    plated.drill = Some(DrillShape::Circle {
        diameter_nm: 600_000,
    });
    let smt = &mut input.pads[1];
    smt.at = SMT;
    smt.net = 1;
    smt.layers = vec!["F.Cu".into()];
    smt.shape = Shape::Rect {
        half_size_nm: [250_000, 150_000],
    };
    // The two inner-plane obstructions close the one-elbow paths from
    // opposite ends, leaving an oblique opening between them.
    let mut guards = vec![
        (3_750_000, 7_750_000, 4_250_000, 8_250_000),
        (9_750_000, 9_750_000, 10_250_000, 10_250_000),
    ];
    if outward {
        // This finite obstruction reaches past both pads and leaves the
        // only passage west of the plated terminal at x = 3 mm.
        guards = vec![(3_500_000, 4_750_000, 40 * MM, 6_250_000)];
    }
    if blocked_trunk {
        guards.push((0, 11_750_000, 40 * MM, 12_250_000));
    }
    for (i, (x, y, u, v)) in guards.into_iter().enumerate() {
        input.rule_areas.push(
            serde_json::from_value(serde_json::json!({
                "id":format!("40000000-0000-4000-8000-{i:012}"), "layers":["In1.Cu"],
                "outer":[[x,y],[u,y],[u,v],[x,v]], "no_tracks":true, "no_vias":false
            }))
            .unwrap(),
        );
    }
    if blocked_drill {
        input.rules.hole_clearance_nm = 400_000;
        input.holes.push(crate::model::Hole {
            id: "40000000-0000-4000-8000-000000000004".into(),
            net: 0,
            at: [DRILL[0], DRILL[1] + 500_000],
            shape: DrillShape::Circle {
                diameter_nm: 600_000,
            },
        });
    }
    Engine::new(input).unwrap()
}

fn route(engine: &Engine, reverse: bool, budget: u64, inner_steps: u64) -> (Option<Path>, u64) {
    let mut history = CongestionHistory::default();
    let grid = engine.input.rules.grid_nm;
    // Only the forward surface tail enters SMT's fine cell. A through
    // via also pays for the rear face even though the tracks use F and In1.
    for (l, p, w) in [(0, SMT, 9), (3, DRILL, 7)] {
        history
            .add_weight((l, p[0] / grid, p[1] / grid), w, engine)
            .unwrap();
    }
    // Every inner path must enter this paid row at least once. The
    // cheapest legal spans cross it without travelling along the row.
    for x in 0..=engine.input.bounds.max[0] / grid {
        history
            .add_weight((1, x, 12 * MM / grid), 3, engine)
            .unwrap();
    }
    let mesh = Mesh {
        engine,
        net: 1,
        grid: grid as u64,
        pitch: MM,
        via_step: engine.layer_policy.scale(engine.effective_rules.via_cost),
        history: Some(&history),
        occupied_entries: 0,
        pricing: MeshPricing::Unpenalized,
    };
    let via = PlacedViaKey {
        at: DRILL,
        start_layer: 0,
        end_layer: 3,
        kind: 0,
    };
    let stub = vec![link(SMT, DRILL, 0), PathEdge::Via(via)];
    let stub_cost = mesh.via_step
        + 5 * engine.layer_policy.weight(0)
        + engine.layer_policy.scale(if reverse { 7 } else { 16 });
    let (roots, costs, goals) = if reverse {
        (
            HashMap::from([((DRILL, 1), stub)]),
            HashMap::from([((DRILL, 1), stub_cost)]),
            HashMap::from([((PLATED, 1), (0, vec![]))]),
        )
    } else {
        (
            HashMap::from([((PLATED, 1), vec![])]),
            HashMap::from([((PLATED, 1), 0)]),
            HashMap::from([((DRILL, 1), (stub_cost, stub))]),
        )
    };
    let mut used = 0;
    let path = mesh
        .visible_entries(&roots, &costs, &goals, 0, &mut || {
            if used == budget {
                return Ok(false);
            }
            used += 1;
            Ok(true)
        })
        .unwrap();
    if let Some(p) = &path {
        assert_eq!(
            p.steps,
            inner_steps * engine.layer_policy.weight(1) + stub_cost + engine.layer_policy.scale(3)
        );
        let ends = if reverse {
            (SMT, PLATED)
        } else {
            (PLATED, SMT)
        };
        assert_eq!(p.points.first(), Some(&ends.0));
        assert_eq!(p.points.last(), Some(&ends.1));
        let mut tip = ends.0;
        let mut vias = 0;
        for &edge in &p.edges {
            match edge {
                PathEdge::Track { start, end, layer } => {
                    assert_eq!(start, tip);
                    assert!(engine.legal_edge_on(1, layer as usize, start, end).unwrap());
                    tip = end;
                }
                PathEdge::Via(v) => {
                    assert_eq!(v, via);
                    assert_eq!(v.at, tip);
                    vias += 1;
                }
            }
        }
        assert_eq!((tip, vias), (ends.1, 1));
    }
    (path, used)
}

#[test]
fn a_two_bend_inner_span_joins_registered_terminals_and_pays_in_the_travel_direction() {
    let engine = board(false, false, false);
    for (a, b) in [(PLATED, [4 * MM, 11 * MM]), (DRILL, [10 * MM, 9 * MM])] {
        assert!(!engine.legal_edge_on(1, 1, a, b).unwrap());
    }
    for reverse in [false, true] {
        let (path, used) = route(&engine, reverse, 20, 80);
        let path = path.unwrap();
        let inner = path
            .edges
            .iter()
            .filter(|e| matches!(e, PathEdge::Track { layer: 1, .. }));
        assert_eq!(inner.count(), 3);
        assert!((2..=20).contains(&used));
        let (limited, used) = route(&engine, reverse, 1, 80);
        assert!(limited.is_none());
        assert_eq!(used, 1);
    }
}

#[test]
fn a_clear_inner_span_requires_a_legal_drill_and_an_unobstructed_middle_leg() {
    for (hole, wall) in [(true, false), (false, true)] {
        let engine = board(hole, wall, false);
        assert_eq!(
            engine
                .legal_via(1, DRILL, 0, 3, crate::model::ViaKind::Through, &[])
                .unwrap()
                .is_none(),
            hole
        );
        for reverse in [false, true] {
            assert!(route(&engine, reverse, 25, 80).0.is_none());
        }
    }
}

#[test]
fn a_connector_can_leave_away_from_the_target_before_crossing_the_inner_plane() {
    let engine = board(false, false, true);
    for reverse in [false, true] {
        let (route, _) = route(&engine, reverse, 25, 88);
        let path = route.unwrap();
        assert!(path.points.iter().any(|p| p[0] == 3 * MM));
    }
}

#[test]
fn the_mesh_joins_a_plated_connector_and_surface_land_before_the_grid_can_cross_the_board() {
    use crate::components::Copper;
    use crate::negotiate::TrialBudget;
    use crate::search::TargetBounds;

    let mut input = board(false, false, true).input;
    input.bounds.max[1] = 100 * MM;
    let destination = [11_250_000, 97 * MM];
    input.pads[1].at = destination;
    input.rule_areas.push(
        serde_json::from_value(serde_json::json!({
            "id":"40000000-0000-4000-8000-000000000007", "layers":["F.Cu"],
            "outer":[[0,30*MM],[40*MM,30*MM],[40*MM,31*MM],[0,31*MM]],
            "no_tracks":true,"no_vias":false
        }))
        .unwrap(),
    );
    // Through-hole exclusion strips in the SMT pad field leave the legal
    // drill between lattice columns. The terminal must register that drill.
    for x in 10..=12 {
        let l = x * MM - 50_000;
        let r = x * MM + 50_000;
        input.rule_areas.push(
            serde_json::from_value(serde_json::json!({
                "id":format!("40000000-0000-4000-8000-{x:012}"),"layers":["F.Cu"],
                "outer":[[l,94*MM],[r,94*MM],[r,100*MM],[l,100*MM]],
                "no_tracks":false,"no_vias":true
            }))
            .unwrap(),
        );
    }
    let engine = Engine::new(input).unwrap();
    let target = TargetBounds::new(
        Copper::Pad(&engine.input.pads[1]),
        engine.all_layers_slice(),
        &engine.work,
    )
    .unwrap();
    let mesh = Mesh {
        engine: &engine,
        net: 1,
        grid: engine.input.rules.grid_nm as u64,
        pitch: MM,
        via_step: engine.layer_policy.scale(engine.effective_rules.via_cost),
        history: None,
        occupied_entries: 0,
        pricing: MeshPricing::Unpenalized,
    };
    // With both terminals registered, more than 90 one-pitch moves still
    // separate the two sets of mesh entries. Only a full-width span can
    // join them within 64 expansions.
    let mut expanded = 0;
    let path = mesh
        .search(
            &[(PLATED, 1, false)],
            &[target],
            &[true, true, false, false],
            &mut TrialBudget::fixed(64),
            &mut expanded,
            None,
            64,
        )
        .unwrap()
        .unwrap();
    assert!(expanded <= 64);
    assert_eq!(path.points.first(), Some(&PLATED));
    assert_eq!(path.points.last(), Some(&destination));
    let mut drills = Vec::new();
    for edge in path.edges {
        match edge {
            PathEdge::Track { start, end, layer } => {
                assert!(engine.legal_edge_on(1, layer as usize, start, end).unwrap())
            }
            PathEdge::Via(v) => {
                assert!(engine
                    .legal_via(1, v.at, 0, 3, crate::model::ViaKind::Through, &drills)
                    .unwrap()
                    .is_some());
                drills.push(v);
            }
        }
    }
    assert_eq!(drills.len(), 1);
}

#[test]
fn a_recessed_plane_channel_needs_the_last_clear_point_beyond_the_first_mesh_step() {
    let mut input = board(false, false, true).input;
    // The opening is two steps to the west. A guard on the far side
    // prevents a third step. One mesh step from either pad cannot see it.
    for corner in &mut input.rule_areas[0].outer {
        if corner[0] == 3_500_000 {
            corner[0] = 2_500_000;
        }
    }
    input.rule_areas.push(
        serde_json::from_value(serde_json::json!({
            "id":"40000000-0000-4000-8000-000000000008","layers":["In1.Cu"],
            "outer":[[750_000,2*MM],[1_250_000,2*MM],[1_250_000,4*MM],[750_000,4*MM]],
            "no_tracks":true,"no_vias":false
        }))
        .unwrap(),
    );
    let engine = Engine::new(input).unwrap();
    for reverse in [false, true] {
        let (path, _) = route(&engine, reverse, 25, 96);
        assert!(path.unwrap().points.iter().any(|p| p[0] == 2 * MM));
    }
}
