use super::*;
use crate::components::Copper;
use crate::model::Input;

// One finite foreign trace blocks the straight line between the two pads.
// Extending it to both board margins seals the surface.
fn engine(seal: bool) -> Engine {
    let mut input: Input = serde_json::from_str(include_str!(
        "../../../tests/fixtures/multiterminal-native-two-nets.json"
    ))
    .unwrap();
    input.layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        .map(str::to_owned)
        .to_vec();
    input.pads.retain(|p| p.net == 1 && p.at[1] == 10_000_000);
    for pad in &mut input.pads {
        pad.layers = vec!["F.Cu".into()];
    }
    if seal {
        input.tracks[0].start[1] = 625_000;
        input.tracks[0].end[1] = 19_375_000;
    }
    let mut engine = Engine::new(input).unwrap();
    engine.surface_search_share = Some(1_000);
    engine
}

#[test]
fn finite_trace_detour_respects_swept_width_and_tightens_to_the_trace_end() {
    let engine = engine(false);
    let a = engine.input.pads[0].at;
    let b = engine.input.pads[1].at;
    let target = TargetBounds::new(
        Copper::Pad(&engine.input.pads[1]),
        engine.all_layers_slice(),
        &engine.work,
    )
    .unwrap();
    let mut expanded = 0;
    let path = connect(
        &engine,
        1,
        &[(a, 0, false)],
        &[target],
        &[true; 4],
        0,
        40,
        None,
        None,
        &mut TrialBudget::fixed(10_000),
        &mut expanded,
        None,
    )
    .unwrap()
    .unwrap();
    assert_eq!(path.points.first(), Some(&a));
    assert_eq!(path.points.last(), Some(&b));
    assert!(expanded <= 56);
    // The centers need a 1.25 mm gap for two 0.25 mm tracks with 1 mm clearance.
    // With a 14 mm horizontal separation and 7.25 mm detour at each end,
    // the shortest cardinal route costs 114 quarter-millimeter steps.
    assert!(
        (114..=116).contains(&path.steps),
        "{} {:?}",
        path.steps,
        path.points
    );
    let target = engine.copper.tracks().next().unwrap();
    let point_gap = |p: Point, a: Point, b: Point| {
        let v = [(b[0] - a[0]) as f64, (b[1] - a[1]) as f64];
        let w = [(p[0] - a[0]) as f64, (p[1] - a[1]) as f64];
        let t = ((w[0] * v[0] + w[1] * v[1]) / (v[0] * v[0] + v[1] * v[1])).clamp(0.0, 1.0);
        (w[0] - t * v[0]).hypot(w[1] - t * v[1])
    };
    for edge in path.edges {
        let PathEdge::Track {
            start,
            end,
            layer: 0,
        } = edge
        else {
            panic!("in-layer detour expected")
        };
        let dx = end[0] - start[0];
        let dy = end[1] - start[1];
        assert!(dx == 0 || dy == 0 || dx.abs() == dy.abs());
        if dx != 0 {
            let cross = (target.start[0] - start[0]) as f64 / dx as f64;
            if (0.0..=1.0).contains(&cross) {
                let y = start[1] as f64 + cross * dy as f64;
                assert!(y < (target.start[1] as f64) || y > (target.end[1] as f64));
            }
        }
        for gap in [
            point_gap(start, target.start, target.end),
            point_gap(end, target.start, target.end),
            point_gap(target.start, start, end),
            point_gap(target.end, start, end),
        ] {
            assert!(gap + 1e-6 >= 1_250_000.0);
        }
    }
}

#[test]
fn sealed_surface_via_only_source_and_shorter_incumbent_leave_the_ordinary_frontier() {
    for (seal, via_only, max_cost, cap) in [
        (true, false, None, 10_000),
        (false, true, None, 10_000),
        (false, false, Some(56), 10_000),
        (false, false, None, 3),
    ] {
        let engine = engine(seal);
        let goal = TargetBounds::new(
            Copper::Pad(&engine.input.pads[1]),
            engine.all_layers_slice(),
            &engine.work,
        )
        .unwrap();
        let mut expanded = 0;
        let found = connect(
            &engine,
            1,
            &[(engine.input.pads[0].at, 0, via_only)],
            &[goal],
            &[true; 4],
            0,
            40,
            max_cost,
            None,
            &mut TrialBudget::fixed(cap),
            &mut expanded,
            None,
        )
        .unwrap();
        assert!(found.is_none());
        assert!(expanded <= cap);
        if via_only {
            assert_eq!(expanded, 0);
        }
    }
}
