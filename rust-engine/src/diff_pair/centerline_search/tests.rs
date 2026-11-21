use super::*;

fn pose(gx: i32, gy: i32, li: u8, dir: u8) -> Pose {
    Pose {
        gx,
        gy,
        li,
        dir,
        arrival: Arrival::Straight,
    }
}

#[test]
fn prior_transition_changes_next_cost() {
    let east = pose(1, 0, 0, 0);
    let after_via = pose(1, 0, 1, 0).via(0, 250_000).0;
    let after_turn = pose(0, 0, 0, 1).step(1, 0, 0).0;
    // The straight arrival trails the other two by one cost unit.
    // It overtakes the via arrival on the turn and the turn arrival on the via.
    assert_eq!(east.step(2, 1, 1).1, 25_000);
    assert_eq!(after_via.step(2, 1, 1).1, 300_000);
    assert!(1 + east.step(2, 1, 1).1 < after_via.step(2, 1, 1).1);
    assert_eq!(east.via(1, 250_000).1, 0);
    assert_eq!(after_turn.via(1, 250_000).1, 300_000);
    assert!(1 + east.via(1, 250_000).1 < after_turn.via(1, 250_000).1);
    assert_eq!(after_turn.via(1, 800_000).1, 0);
    assert_eq!(after_via.step(2, 0, 0).1, 0);
    assert!(!after_via.can_start_via());
    assert!(east.can_start_via() && after_turn.can_start_via());
}

#[test]
fn snap_bound_matches_independent_shortest_grid_walk() {
    // Multi-source Dijkstra on a finite obstacle-free grid, starting at
    // every point inside the actual off-grid endpoint's closed snap box.
    let end: Point = [27, 36];
    let step = 10;
    let radius = 12;
    for (axial, diagonal) in [(10, 14), (11, 15), (13, 18), (20, 28)] {
        let mut costs = BTreeMap::new();
        let mut queue = BinaryHeap::new();
        for x in 0..=6i64 {
            for y in 0..=7i64 {
                if (x * step - end[0]).abs() <= radius && (y * step - end[1]).abs() <= radius {
                    queue.push((Reverse(0u64), x, y));
                }
            }
        }
        while let Some((Reverse(cost), x, y)) = queue.pop() {
            if costs.contains_key(&(x, y)) {
                continue;
            }
            costs.insert((x, y), cost);
            for dx in -1..=1 {
                for dy in -1..=1 {
                    if (dx, dy) == (0, 0) {
                        continue;
                    }
                    let (nx, ny) = (x + dx, y + dy);
                    if (0..=6).contains(&nx)
                        && (0..=7).contains(&ny)
                        && !costs.contains_key(&(nx, ny))
                    {
                        let edge = if dx == 0 || dy == 0 { axial } else { diagonal };
                        queue.push((Reverse(cost + edge), nx, ny));
                    }
                }
            }
        }
        assert_eq!(costs.len(), 56);
        for ((x, y), cost) in costs {
            let pt = [x * step, y * step];
            assert_eq!(
                snap_bound(pt, end, radius as u64, step as u64, axial, diagonal),
                cost,
                "point {pt:?}, costs {axial}/{diagonal}"
            );
        }
    }
}

#[test]
fn deferred_prefix_can_run_while_new_poses_are_pending() {
    let work = WorkBudget::new(Default::default());
    let mut open = Frontier::default();
    let junction = pose(0, 0, 0, 0);
    open.push(&work, junction, None, 0, 0, 0).unwrap();
    let root = open.pop(&work, false).unwrap().unwrap().0;
    open.push(&work, junction, Some(root), 4, 0, 0).unwrap();
    let new_pose = pose(1, 0, 0, 0);
    open.push(&work, new_pose, Some(root), 1, 0, 0).unwrap();
    let (deferred, label) = open.pop(&work, true).unwrap().unwrap();
    assert_eq!(label.g, 4);
    assert_eq!(open.path(&work, deferred).unwrap(), [junction, junction]);
    assert_eq!(open.pop(&work, false).unwrap().unwrap().1.pose, new_pose);
}

#[test]
fn search_limits_cover_labels_queues_and_reconstruction() {
    use crate::model::{Limits, Status};
    let state = pose(0, 0, 0, 0);
    let work = WorkBudget::new(Limits {
        max_search_entries: 4,
        ..Default::default()
    });
    let mut open = Frontier::default();
    open.push(&work, state, None, 0, 0, 0).unwrap();
    let root = open.pop(&work, false).unwrap().unwrap().0;
    let next = pose(1, 0, 0, 0);
    open.push(&work, next, Some(root), 1, 0, 0).unwrap();
    let tail = open.pop(&work, false).unwrap().unwrap().0;
    // Two immutable labels and two expanded poses now fill the allowance.
    assert_eq!(
        open.path(&work, tail).unwrap_err().status,
        Status::AdmissionExceeded
    );
    assert_eq!(
        open.push(&work, next, Some(tail), 2, 0, 0)
            .unwrap_err()
            .status,
        Status::AdmissionExceeded
    );
    assert_eq!(open.entries(), 4);

    let work = WorkBudget::new(Limits {
        max_work: 4,
        ..Default::default()
    });
    let mut open = Frontier::default();
    open.push(&work, state, None, 0, 0, 0).unwrap();
    open.push(&work, state, None, 1, 0, 0).unwrap();
    open.pop(&work, false).unwrap().unwrap();
    // The last unit transfers the stale preferred entry. Popping it for
    // alternative expansion would need another unit.
    assert_eq!(
        open.pop(&work, false).err().unwrap().status,
        Status::WorkExhausted
    );
    assert_eq!(work.used(), 4);

    let work = WorkBudget::new(Limits {
        max_work: 5,
        ..Default::default()
    });
    let mut open = Frontier::default();
    open.push(&work, state, None, 0, 0, 0).unwrap();
    let root = open.pop(&work, false).unwrap().unwrap().0;
    open.push(&work, next, Some(root), 1, 0, 0).unwrap();
    let tail = open.pop(&work, false).unwrap().unwrap().0;
    assert_eq!(
        open.path(&work, tail).unwrap_err().status,
        Status::WorkExhausted
    );
    assert_eq!(work.used(), 4);
}

use super::super::{
    astar_coupled_centerline, simplify_collinear_points, try_commit_coupled_solution,
    validate_and_build_coupled_solution, EndFanout, ResolvedDiffPair, TrialBudget,
};
use crate::{model::Input, Engine};
use serde_json::json;
const MM: i64 = 1_000_000;
fn point(x: i32, y: i32) -> Point {
    [i64::from(x) * MM, i64::from(y) * MM]
}

fn pair_fixture(
    end_point: Point,
    end_left: Point,
    end_dir: (f64, f64),
) -> (Input, ResolvedDiffPair, EndFanout, EndFanout) {
    let fanout = |gather: Point, left: Point, dir| EndFanout {
        gather,
        gp: [gather[0] + left[0], gather[1] + left[1]],
        gn: [gather[0] - left[0], gather[1] - left[1]],
        dir,
        pos_on_left: true,
        layer_idx: 0,
        pos_segs: Vec::new(),
        neg_segs: Vec::new(),
        uncoupled_nm: 0,
        est_uncoupled_nm: 0,
        skew_nm: 0,
        short_seg_count: 0,
    };
    let start = fanout(point(5, 10), [0, 300_000], (1.0, 0.0));
    let end = fanout(end_point, end_left, end_dir);
    let pads: Vec<_> = [(1, start.gp), (2, start.gn), (1, end.gp), (2, end.gn)]
        .into_iter()
        .enumerate()
        .map(|(idx, (net, at))| {
            json!({
                "id":format!("00000000-0000-4000-8000-{idx:012}"), "net":net, "at":at,
                "shape":{"kind":"rect","half_size_nm":[110_000,110_000]}, "layers":["F.Cu"]
            })
        })
        .collect();
    let input: Input = serde_json::from_value(json!({
        "version":1, "source_id":"b".repeat(64), "layer":"F.Cu",
        "bounds":{"min":[0,0],"max":[30*MM,20*MM]},
        "rules":{"grid_nm":MM,"width_nm":200_000,"clearance_nm":150_000,"edge_clearance_nm":150_000},
        "nets":[{"id":0,"name":""},{"id":1,"name":"DP+"},{"id":2,"name":"DP-"}],
        "pads":pads,"tracks":[],"max_expansions":100
    })).unwrap();
    let pair = ResolvedDiffPair {
        index: 0,
        stem: "DP".into(),
        pos_net: 1,
        neg_net: 2,
        pos_name: "DP+".into(),
        neg_name: "DP-".into(),
        class_name: "Default".into(),
        width_nm: 200_000,
        class_width_nm: 200_000,
        gap_nm: 400_000,
        gap_min_nm: 150_000,
        gap_max_nm: 400_000,
        via_gap_nm: 400_000,
        clearance_nm: 150_000,
        uncoupled_max_nm: 10 * MM,
        has_dru_gap_max: false,
        dru_skew_max_nm: None,
        impedance: None,
    };
    (input, pair, start, end)
}

#[test]
fn disconnected_terminal_rolls_back_then_resumes_the_same_search() {
    let (input, pair, start, end) = pair_fixture(point(9, 10), [0, 300_000], (1.0, 0.0));
    let mut outcomes = Vec::new();
    for reject_first in [false, true] {
        let mut engine = Engine::new(input.clone()).unwrap();
        let mut attempts = 0;
        let mut checked_work = 0;
        let result = engine
            .transaction(|tx| {
                astar_coupled_centerline(
                    tx.engine,
                    &pair,
                    &start,
                    &end,
                    &[0],
                    MM,
                    &|_, _| 0,
                    &|_, _, _| 0,
                    &mut TrialBudget::fixed(100),
                    None,
                    None,
                    |e, pair, mut sol| {
                        attempts += 1;
                        if reject_first && attempts == 1 {
                            sol.tracks.retain(|t| t.net == pair.pos_net);
                            let before = e.work_used();
                            assert!(!try_commit_coupled_solution(e, pair, sol)?);
                            checked_work = e.work_used();
                            assert!(checked_work > before);
                            assert_eq!(e.copper().generated().count(), 0);
                            assert_eq!(e.copper().generated_vias().count(), 0);
                            return Ok(false);
                        }
                        assert!(e.work_used() >= checked_work);
                        try_commit_coupled_solution(e, pair, sol)
                    },
                )
            })
            .unwrap();
        assert!(result);
        assert_eq!(attempts, if reject_first { 2 } else { 1 });
        outcomes.push((
            engine.copper().generated().cloned().collect::<Vec<_>>(),
            engine.work_used(),
        ));
    }
    assert!(!outcomes[0].0.is_empty());
    // Includes generated IDs, so a rejected candidate cannot silently
    // advance the sequence. Rolling back the candidate keeps its work spent.
    assert_eq!(outcomes[0].0, outcomes[1].0);
    assert!(outcomes[1].1 > outcomes[0].1);
}

#[test]
fn consecutive_pair_transitions_respect_the_hole_spacing_boundary() {
    let (mut input, pair, start, end) = pair_fixture(point(12, 10), [0, 300_000], (1.0, 0.0));
    input.layers = vec!["F.Cu".into(), "B.Cu".into()];
    input.custom_rules = Some(
        r#"(version 1)
        (rule "minimum drill gap" (condition "A.Type == 'Via' && B.Type == 'Via'")
          (constraint hole_to_hole (min 0.65mm)))"#
            .into(),
    );
    for delta in [-1, 0] {
        let mut engine = Engine::new(input.clone()).unwrap();
        let drill = engine.effective_rules.net(pair.pos_net).via_drill_nm;
        let first = point(8, 10);
        let second = [first[0] + drill + 650_000 + delta, first[1]];
        let runs = [
            (vec![start.gather, first], 0),
            (vec![first, second], 1),
            (vec![second, end.gather], 0),
        ];
        let sol = validate_and_build_coupled_solution(&engine, &pair, &start, &end, &runs)
            .unwrap()
            .expect("individual transition sites and connecting tracks clear");
        assert_eq!(sol.vias.len(), 4);
        assert_eq!(sol.vias[0].net, sol.vias[2].net);
        assert_eq!(
            sol.vias[2].at[0] - sol.vias[0].at[0] - drill,
            650_000 + delta
        );
        let accepted = try_commit_coupled_solution(&mut engine, &pair, sol).unwrap();
        assert_eq!(accepted, delta == 0);
        assert_eq!(
            engine.copper().generated_vias().count(),
            if accepted { 4 } else { 0 }
        );
        if !accepted {
            assert_eq!(engine.copper().generated().count(), 0);
        }
    }
}

#[test]
fn only_longer_prefix_clears_the_common_tail_and_connects_both_nets() {
    let (input, pair, start, end) = pair_fixture(point(14, 9), [300_000, 0], (0.0, -1.0));
    let mut engine = Engine::new(input).unwrap();
    // Enumerate the two paths of a small directed lattice. Both share the
    // last three eastbound steps. The vertical tail crosses y=10 at x=14.
    // At x=14 the cheaper branch is at y=10, the detour is at y=7.
    let mut edges = BTreeMap::<(i32, i32), std::collections::BTreeSet<_>>::new();
    for branch in [
        &[(5, 10), (20, 10)][..],
        &[(5, 10), (7, 10), (11, 6), (13, 6), (17, 10), (20, 10)][..],
    ] {
        for w in branch.windows(2) {
            let (mut x, mut y): (i32, i32) = w[0];
            let (dx, dy) = ((w[1].0 - x).signum(), (w[1].1 - y).signum());
            let dir = match dy {
                0 => 0,
                1 => 1,
                -1 => 7,
                _ => unreachable!(),
            };
            while (x, y) != w[1] {
                edges.entry((x, y)).or_default().insert((dx, dy, dir));
                x += dx;
                y += dy;
            }
        }
    }
    let mut open = Frontier::default();
    open.push(&engine.work, pose(5, 10, 0, 8), None, 0, 0, 0)
        .unwrap();
    let mut attempts = Vec::new();
    let mut solution = None;
    while let Some((id, label)) = open.pop(&engine.work, false).unwrap() {
        let p = label.pose;
        if let Some(moves) = edges.get(&(p.gx, p.gy)) {
            for &(dx, dy, dir) in moves {
                let (next, turn) = p.step(p.gx + dx, p.gy + dy, dir);
                let step = if dy == 0 { 1_000_000 } else { 1_414_214 };
                open.push(&engine.work, next, Some(id), label.g + step + turn, 0, 0)
                    .unwrap();
            }
            continue;
        }
        assert_eq!(p, pose(20, 10, 0, 0));
        let mut chain: Vec<_> = open
            .path(&engine.work, id)
            .unwrap()
            .into_iter()
            .map(|p| point(p.gx, p.gy))
            .collect();
        chain.extend(
            [
                (23, 10),
                (25, 12),
                (25, 14),
                (23, 16),
                (16, 16),
                (14, 14),
                (14, 9),
            ]
            .map(|(x, y)| point(x, y)),
        );
        let runs = [(simplify_collinear_points(chain), 0)];
        let candidate =
            validate_and_build_coupled_solution(&engine, &pair, &start, &end, &runs).unwrap();
        attempts.push((label.g, candidate.is_some()));
        if candidate.is_some() {
            solution = candidate;
        }
    }
    // Independent lengths and bends of the two prefix polylines.
    assert_eq!(
        attempts,
        vec![
            (15_000_000, false),
            (7_000_000 + 8 * 1_414_214 + 4 * 25_000, true)
        ]
    );
    assert!(try_commit_coupled_solution(&mut engine, &pair, solution.unwrap()).unwrap());
    for net in [1, 2] {
        assert_eq!(engine.components(net).unwrap().len(), 1);
    }
}
