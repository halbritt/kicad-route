use super::*;
use crate::model::Input;

const MM: i64 = 1_000_000;
const A: Point = [6 * MM, 7 * MM];
const B: Point = [16 * MM, 12 * MM];

fn four_layer_board() -> Engine {
    let mut board: Input = serde_json::from_str(include_str!(
        "../../../tests/fixtures/multiterminal-native-two-nets.json"
    ))
    .unwrap();
    board.layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        .map(String::from)
        .to_vec();
    // The fixture's foreign track crosses x=10 mm from y=4 to 16 mm.
    board.tracks[0].layer = "In1.Cu".into();
    Engine::new(board).unwrap()
}

fn probe(
    engine: &Engine,
    rejected: &mut BlockedTrunks,
    source_layer: usize,
    trunk_layer: usize,
    history: Option<&CongestionHistory>,
    limit: usize,
) -> (usize, Option<Path>) {
    let source = Terminal {
        at: [3 * MM, A[1]],
        layer: source_layer,
    };
    let target = Terminal {
        at: B,
        layer: trunk_layer,
    };
    let through = PlacedViaKey {
        at: A,
        start_layer: 0,
        end_layer: 3,
        kind: 0,
    };
    assert!(engine.legal_edge_on(1, source.layer, source.at, A).unwrap());
    assert!(engine
        .legal_via(1, A, 0, 3, ViaKind::Through, &[])
        .unwrap()
        .is_some());
    let join = Join {
        engine,
        net: 1,
        source,
        target,
        via_step: engine.layer_policy.scale(engine.effective_rules.via_cost),
        history,
    };
    let mut expanded = 0;
    let mut best = None;
    join.connect(
        trunk_layer,
        Some(through),
        None,
        &mut best,
        false,
        rejected,
        &mut || {
            if expanded == limit {
                return Ok(false);
            }
            expanded += 1;
            Ok(true)
        },
    )
    .unwrap();
    (expanded, best)
}

#[test]
fn two_surface_launches_share_only_a_fully_obstructed_trunk_on_the_same_layer() {
    let engine = four_layer_board();
    let mut blocked = BlockedTrunks {
        entries: Default::default(),
        capacity: 1,
    };
    // First trial stopped with unchecked shapes. A full sweep is still required.
    let (n, path) = probe(&engine, &mut blocked, 0, 1, None, 1);
    assert_eq!(n, 1);
    assert!(path.is_none());
    let (all_shapes, path) = probe(&engine, &mut blocked, 3, 1, None, usize::MAX);
    assert!(all_shapes > 1 && path.is_none());
    let (retried, path) = probe(&engine, &mut blocked, 0, 1, None, usize::MAX);
    assert_eq!(retried, 0);
    assert!(path.is_none());
    let (retried, path) = probe(&engine, &mut blocked, 0, 2, None, usize::MAX);
    assert!(retried > 0);
    let path = path.unwrap();
    assert_eq!(path.points.last(), Some(&B));
    assert_eq!(
        path.steps,
        72 * engine.layer_policy.weight(2)
            + engine.layer_policy.scale(engine.effective_rules.via_cost)
    );
    assert!(
        path.edges
            .iter()
            .filter(|e| matches!(e, PathEdge::Via(_)))
            .count()
            == 1
    );

    // Under storage pressure both callers can still perform their own sweeps.
    let mut uncached = BlockedTrunks {
        entries: Default::default(),
        capacity: 0,
    };
    for face in [0, 3] {
        let (n, path) = probe(&engine, &mut uncached, face, 1, None, usize::MAX);
        assert_eq!(n, all_shapes);
        assert!(path.is_none());
    }
}

#[test]
fn a_paid_surface_stub_does_not_reject_the_same_clear_trunk_from_the_other_face() {
    let engine = four_layer_board();
    let mut history = CongestionHistory::default();
    // The approach to the through hole crosses (4 mm,7 mm) on F.Cu only.
    let grid = engine.input.rules.grid_nm;
    history
        .add_weight((0, 4 * MM / grid, A[1] / grid), 7, &engine)
        .unwrap();
    let mut blocked = BlockedTrunks {
        entries: Default::default(),
        capacity: 1,
    };
    let (n, path) = probe(&engine, &mut blocked, 0, 2, Some(&history), usize::MAX);
    assert!(n > 0 && path.is_none());
    let (n, path) = probe(&engine, &mut blocked, 3, 2, Some(&history), usize::MAX);
    assert!(n > 0);
    let path = path.unwrap();
    assert_eq!(path.points.first(), Some(&[3 * MM, A[1]]));
    assert_eq!(path.points.last(), Some(&B));
    assert!(unpenalized_terminal(&engine, Some(&history), &path.edges).unwrap());
}

#[test]
fn a_plated_source_shares_blocked_escape_drills_across_its_real_faces() {
    use crate::{components::Copper, model::DrillShape};
    let mut input: Input = serde_json::from_str(include_str!(
        "../../../tests/fixtures/multiterminal-native-two-nets.json"
    ))
    .unwrap();
    input.layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        .map(String::from)
        .to_vec();
    let mut pth = input.pads[0].clone();
    pth.layers = input.layers.clone();
    pth.drill = Some(DrillShape::Circle {
        diameter_nm: 400_000,
    });
    let smd = input.pads[4].clone();
    input.pads = vec![pth, smd];
    let wall = input.tracks.pop().unwrap();
    for (i, layer) in input.layers.iter().enumerate() {
        let mut segment = wall.clone();
        segment.id = format!("36000000-0000-4000-8000-{i:012}");
        segment.layer = layer.clone();
        segment.start = [10 * MM, MM];
        segment.end = [10 * MM, 19 * MM];
        input.tracks.push(segment);
    }
    let mut engine = Engine::new(input).unwrap();
    engine.surface_search_share = Some(0);
    let start = engine.input.pads[0].copper_center();
    let target = TargetBounds::new(
        Copper::Pad(&engine.input.pads[1]),
        engine.all_layers_slice(),
        &engine.work,
    )
    .unwrap();
    let source = TargetBounds::new(
        Copper::Pad(&engine.input.pads[0]),
        engine.all_layers_slice(),
        &engine.work,
    )
    .unwrap();
    assert_eq!(
        source.plated_contact(&engine.work).unwrap(),
        Some((start, 15))
    );
    let visit = |starts: &[(Point, u8, bool)]| {
        let mut used = 0;
        let path = from_launch(
            &engine,
            1,
            starts,
            std::slice::from_ref(&target),
            &[true; 4],
            0,
            engine.effective_rules.via_cost,
            None,
            &mut TrialBudget::fixed(10_000),
            &mut used,
            None,
            None,
        )
        .unwrap();
        assert!(path.is_none());
        used
    };
    let contacts = [0, 1, 2, 3].map(|l| (start, l, false));
    let separate = contacts
        .iter()
        .map(|s| visit(std::slice::from_ref(s)))
        .sum::<u64>();
    let joined = visit(&contacts);
    assert!(joined < separate, "combined {joined}, separate {separate}");
}
