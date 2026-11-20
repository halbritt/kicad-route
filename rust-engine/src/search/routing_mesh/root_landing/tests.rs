use super::*;
use crate::{components::Copper, model::Input};

fn fixture(closed: bool) -> Engine {
    let mut input: Input = serde_json::from_str(include_str!(
        "../../../../tests/fixtures/multiterminal-native-two-nets.json"
    ))
    .unwrap();
    input.layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        .map(str::to_owned)
        .to_vec();
    input.pads.retain(|p| p.net == 1 && p.at[1] == 10_000_000);
    input.pads[0].layers = input.layers.clone();
    input.pads[0].drill = Some(
        serde_json::from_value(serde_json::json!({"kind":"circle","diameter_nm":400000})).unwrap(),
    );
    input.pads[1].layers = vec!["F.Cu".into()];
    input.tracks[0].layer = "In1.Cu".into();
    let mut root = input.tracks[0].clone();
    root.id = "d3f8cc43-5c4e-4f51-be27-567432840f51".into();
    root.net = 1;
    root.layer = "F.Cu".into();
    root.start = input.pads[0].at;
    root.end = [3_000_000, 9_000_000];
    input.tracks.push(root);
    // Only an off-axis aperture admits the new hole. On the buried layer a
    // foreign track between the root and the aperture requires a detour.
    let holes = if closed {
        vec![]
    } else {
        vec![vec![
            [14_000_000, 6_000_000],
            [14_000_000, 8_000_000],
            [16_000_000, 8_000_000],
            [16_000_000, 6_000_000],
        ]]
    };
    input.rule_areas.push(
        serde_json::from_value(serde_json::json!({
            "id":"854bc63a-7fdf-43fb-a486-21372fffcdda","layers":["F.Cu","In1.Cu"],
            "outer":[[0,0],[40000000,0],[40000000,20000000],[0,20000000]],"holes":holes,
            "no_tracks":false,"no_vias":true
        }))
        .unwrap(),
    );
    let mut e = Engine::new(input).unwrap();
    e.surface_search_share = Some(1600);
    e
}

fn run(
    engine: &Engine,
    no_launch: bool,
    history: Option<&CongestionHistory>,
    cap: u64,
) -> (u64, Option<Path>) {
    let source = [
        Copper::Pad(&engine.input.pads[0]),
        Copper::Track(engine.copper.tracks().find(|t| t.net == 1).unwrap()),
    ];
    let goal = TargetBounds::new(
        Copper::Pad(&engine.input.pads[1]),
        engine.all_layers_slice(),
        &engine.work,
    )
    .unwrap();
    let start = engine.input.pads[0].at;
    let mut used = 0;
    let route = connect(
        engine,
        1,
        &source,
        &[(start, 1, no_launch)],
        &[goal],
        &[true, true, false, false],
        0,
        40,
        history,
        &mut TrialBudget::fixed(cap),
        &mut used,
        None,
    )
    .unwrap();
    (used, route)
}

#[test]
fn one_hole_approach_keeps_its_layer_and_pays_the_actual_spur_and_drill() {
    let e = fixture(false);
    let _scope = e.work.scope();
    let (used, path) = run(&e, false, None, 10000);
    let p = path.unwrap();
    assert!(used > 1 && used <= 1600);
    assert_eq!(p.points.first(), Some(&e.input.pads[0].at));
    assert_eq!(p.points.last(), Some(&e.input.pads[1].at));
    let mut layer = 1;
    let mut length = 0;
    let mut drills = 0;
    let mut h = CongestionHistory::default();
    for edge in &p.edges {
        match *edge {
            PathEdge::Track {
                start,
                end,
                layer: actual,
            } => {
                assert_eq!(layer, actual);
                assert!(e.legal_edge_on(1, layer as usize, start, end).unwrap());
                length += start[0].abs_diff(end[0]).div_ceil(250000)
                    + start[1].abs_diff(end[1]).div_ceil(250000);
            }
            PathEdge::Via(v) => {
                assert_eq!(drills, 0);
                assert_eq!((v.start_layer, v.end_layer), (0, 3));
                assert!(e
                    .legal_via(1, v.at, 0, 3, super::super::ViaKind::Through, &[])
                    .unwrap()
                    .is_some());
                assert!((14_000_000..=16_000_000).contains(&v.at[0]));
                assert!((6_000_000..=8_000_000).contains(&v.at[1]));
                let cell = (v.at[0] / 250000, v.at[1] / 250000);
                h.add_weight((2, cell.0, cell.1), 7, &e).unwrap();
                drills += 1;
                layer = 0;
            }
        }
    }
    assert_eq!(drills, 1);
    assert_eq!(p.steps, length + 40);
    let (_, charged) = run(&e, false, Some(&h), 10000);
    assert_eq!(charged.unwrap().steps, p.steps + 7);
}

#[test]
fn closed_drill_window_excluded_layers_and_a_spent_trial_leave_search_available() {
    for (closed, excluded, cap) in [(true, false, 100), (false, true, 10000), (false, false, 4)] {
        let e = fixture(closed);
        let _scope = e.work.scope();
        let (used, p) = run(&e, excluded, None, cap);
        assert!(p.is_none());
        assert!(used <= cap / e.input.nets.len() as u64);
        if excluded {
            assert_eq!(used, 0);
        }
    }
}
