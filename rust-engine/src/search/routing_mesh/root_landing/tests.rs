use super::*;
use crate::{components::Copper, model::Input};

fn fixture_input(closed: bool) -> Input {
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
    input
}

fn fixture(closed: bool) -> Engine {
    let mut e = Engine::new(fixture_input(closed)).unwrap();
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

#[test]
fn a_segmented_tree_can_use_its_far_side_barrel_to_reach_one_surface_pad() {
    const MM: i64 = 1_000_000;
    for (second_barrel, share, should_join) in
        [(true, 24, true), (false, 24, false), (true, 200, false)]
    {
        let mut input: Input = serde_json::from_str(include_str!(
            "../../../../tests/fixtures/multiterminal-native-two-nets.json"
        ))
        .unwrap();
        input.layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
            .map(str::to_owned)
            .to_vec();
        input.pads.retain(|p| p.net == 1 && p.at[1] == 10 * MM);
        input.pads[0].layers = input.layers.clone();
        input.pads[0].drill = Some(crate::model::DrillShape::Circle {
            diameter_nm: 400_000,
        });
        input.pads[1].layers = vec!["F.Cu".into()];
        if second_barrel {
            let mut far = input.pads[0].clone();
            far.id = "15000000-0000-4000-8000-000000000002".into();
            far.at = [12 * MM, 10 * MM];
            input.pads.push(far);
        }
        let sample = input.tracks.pop().unwrap();
        for i in 0..36 {
            let mut t = sample.clone();
            t.id = format!("16000000-0000-4000-8000-{i:012x}");
            t.layer = "F.Cu".into();
            t.net = 1;
            t.start = [3 * MM + i * 250_000, 10 * MM];
            t.end = [t.start[0] + 250_000, 10 * MM];
            input.tracks.push(t);
        }
        for (i, layer, x) in [(1, "In1.Cu", 10 * MM), (2, "F.Cu", 14 * MM)] {
            input.rule_areas.push(
                serde_json::from_value(serde_json::json!({
                    "id":format!("17000000-0000-4000-8000-{i:012x}"),"layers":[layer],
                    "outer":[[x,0],[x+250000,0],[x+250000,20000000],[x,20000000]],
                    "no_tracks":true,"no_vias":false
                }))
                .unwrap(),
            );
        }
        let mut engine = Engine::new(input).unwrap();
        engine.surface_search_share = Some(share);
        let _scope = engine.work.scope();
        let groups = engine.components(1).unwrap();
        assert_eq!(groups.len(), 2);
        let tree = groups.iter().max_by_key(|g| g.len()).unwrap();
        let mut starts = Vec::new();
        for &item in tree {
            let mask = item.layer_mask(engine.all_layers_slice());
            for (layer, _) in engine.all_layers_slice().iter().enumerate() {
                if mask & (1 << layer) != 0 {
                    starts.extend(
                        item.anchors(engine.input.bounds.min, engine.input.rules.grid_nm)
                            .into_iter()
                            .map(|at| (at, layer as u8, false)),
                    );
                }
            }
        }
        starts.sort_unstable();
        starts.dedup();
        let pad = &engine.input.pads[1];
        assert!(
            super::super::super::layer_crossing::has_axial_drill(&engine, 1, 0, pad.at).unwrap()
        );
        let target =
            TargetBounds::new(Copper::Pad(pad), engine.all_layers_slice(), &engine.work).unwrap();
        let mut used = 0;
        let found = connect(
            &engine,
            1,
            tree,
            &starts,
            &[target],
            &[true, true, false, false],
            0,
            40,
            None,
            &mut TrialBudget::fixed(2_000),
            &mut used,
            None,
        )
        .unwrap();
        assert_eq!(found.is_some(), should_join);
        if let Some(path) = found {
            assert!(used <= share);
            assert_eq!(
                path.edges
                    .iter()
                    .filter(|e| matches!(e, PathEdge::Via(_)))
                    .count(),
                1
            );
            assert!(path
                .edges
                .iter()
                .any(|e| matches!(e,PathEdge::Track{layer:1,start,..} if *start==[12*MM,10*MM])));
        }
    }
}

#[test]
fn mixed_islands_escape_only_the_unattached_surface_land() {
    let mut input = fixture_input(false);
    let mut wired_pad = input.pads[1].clone();
    wired_pad.id = "87000000-0000-4000-8000-000000000001".into();
    wired_pad.at = [3_000_000, 4_000_000];
    let mut stub = input.tracks[1].clone();
    stub.id = "87000000-0000-4000-8000-000000000002".into();
    stub.start = wired_pad.at;
    stub.end = [4_000_000, 4_000_000];
    input.pads.push(wired_pad);
    input.tracks.push(stub);

    let mut engine = Engine::new(input).unwrap();
    engine.surface_search_share = Some(1600);
    let _scope = engine.work.scope();
    let groups = engine.components(1).unwrap();
    assert_eq!(groups.len(), 3);
    let mut source = Vec::new();
    let mut islands = Vec::new();
    for group in groups {
        if group
            .iter()
            .any(|c| matches!(c, Copper::Pad(p) if p.drill.is_some()))
        {
            source = group;
        } else {
            islands.push(group);
        }
    }

    let allowed = [true, true, false, false];
    for order in 0..2 {
        if order == 1 {
            islands.reverse();
        }
        let bounds: Vec<_> = islands
            .iter()
            .flatten()
            .map(|&c| TargetBounds::new(c, engine.all_layers_slice(), &engine.work).unwrap())
            .collect();
        let bare = isolated_targets(&engine, &islands, &bounds, &allowed, 0).unwrap();
        assert_eq!(bare.len(), 1);
        assert!(matches!(bare[0].copper, Copper::Pad(p) if p.id == engine.input.pads[1].id));
        assert!(
            isolated_targets(&engine, &islands, &bounds, &[false, true, true, true], 0)
                .unwrap()
                .is_empty()
        );

        let mut expanded = 0;
        let candidate = connect(
            &engine,
            1,
            &source,
            &[(engine.input.pads[0].at, 1, false)],
            bare.iter().copied(),
            &allowed,
            bare.len(),
            40,
            None,
            &mut TrialBudget::fixed(10_000),
            &mut expanded,
            None,
        )
        .unwrap()
        .unwrap();
        assert!(expanded > 0 && expanded <= 1_600);
        assert_eq!(candidate.points.first(), Some(&engine.input.pads[0].at));
        assert_eq!(candidate.points.last(), Some(&engine.input.pads[1].at));
        let holes: Vec<_> = candidate
            .edges
            .iter()
            .filter_map(|e| match e {
                PathEdge::Via(v) => Some(v.at),
                _ => None,
            })
            .collect();
        assert_eq!(holes.len(), 1);
        assert!((14_000_000..=16_000_000).contains(&holes[0][0]));
        assert!((6_000_000..=8_000_000).contains(&holes[0][1]));
    }
    // Two isolated surface lands with no wired competitor use the regular mesh.
    let pads: Vec<_> = islands
        .into_iter()
        .map(|g| {
            g.into_iter()
                .filter(|c| matches!(c, Copper::Pad(_)))
                .collect()
        })
        .collect();
    let bounds: Vec<_> = pads
        .iter()
        .flatten()
        .map(|&c| TargetBounds::new(c, engine.all_layers_slice(), &engine.work).unwrap())
        .collect();
    assert!(isolated_targets(&engine, &pads, &bounds, &allowed, 0)
        .unwrap()
        .is_empty());
}
