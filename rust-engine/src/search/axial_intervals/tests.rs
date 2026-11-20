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

fn wave(engine: &Engine, max_steps: u64, occupied: usize) -> WaveSearch<'_> {
    WaveSearch {
        engine,
        net: 1,
        layer: 0,
        grid: engine.input.rules.grid_nm,
        guides: [engine.input.pads[0].at, engine.input.pads[1].at],
        occupied,
        max_steps,
        wave: Default::default(),
    }
}

#[test]
fn clipped_line_stops_at_the_first_trace_and_revisits_spend_work_without_more_storage() {
    let engine = engine(false);
    let _scope = engine.work.scope();
    let start = engine.input.pads[0].at;
    let mut search = wave(&engine, 200, 0);
    assert!(search.add_line(0, start, 0, None).unwrap().is_none());
    assert_eq!(search.wave[0].lines[0].hi, 8_750_000);
    assert_eq!(search.wave[0].lines[0].lo, 750_000);
    let used = engine.work.used();
    assert!(search.add_line(0, start, 0, None).unwrap().is_none());
    assert_eq!(search.wave[0].lines.len(), 1);
    assert!(engine.work.used() >= used + 2);

    let mut full = wave(&engine, 200, engine.work.limits.max_search_entries);
    assert_eq!(
        full.add_line(0, start, 0, None).unwrap_err().status,
        crate::model::Status::AdmissionExceeded
    );
}

#[test]
fn reconstructed_path_checks_the_full_segment_and_its_integer_cost() {
    let engine = engine(false);
    let _scope = engine.work.scope();
    for (y, max_steps, expected) in [
        (10_000_000, 56, false),
        (2_000_000, 55, false),
        (2_000_000, 56, true),
    ] {
        let mut search = wave(&engine, max_steps, 0);
        let a = [3_000_000, y];
        let b = [17_000_000, y];
        // Fabricate predecessor entries so the final sweep and price checks
        // get exercised independently of the interval-discovery checks.
        for (side, pivot) in [a, b].into_iter().enumerate() {
            search.wave[side].lines.push(Line {
                pivot,
                axis: side,
                lo: pivot[side],
                hi: pivot[side],
                previous: None,
                walked: 0,
                depth: 0,
            });
        }
        let path = search.connect_at([0, 0], b).unwrap();
        assert_eq!(path.is_some(), expected);
        if let Some(path) = path {
            assert_eq!(path.steps, 56);
        }
    }
    let short = Path {
        edges: vec![PathEdge::Track {
            start: [0, 0],
            end: [1, 1],
            layer: 0,
        }],
        points: vec![[0, 0], [1, 1]],
        steps: 2,
    };
    assert_eq!(copper_length(&engine, &short, false).unwrap(), 1);
    assert_eq!(copper_length(&engine, &short, true).unwrap(), 2);
}

#[test]
fn a_one_probe_board_share_is_consumed_once() {
    let mut engine = engine(false);
    engine.surface_search_share = Some(1);
    let _scope = engine.work.scope();
    let goal = TargetBounds::new(
        Copper::Pad(&engine.input.pads[1]),
        engine.all_layers_slice(),
        &engine.work,
    )
    .unwrap();
    let mut expanded = 7;
    let found = connect(
        &engine,
        1,
        &[(engine.input.pads[0].at, 0, false)],
        &[goal],
        &[true; 4],
        0,
        40,
        None,
        None,
        &mut TrialBudget::fixed(10_000),
        &mut expanded,
        None,
    )
    .unwrap();
    assert!(found.is_none());
    assert_eq!(expanded, 8);
}
