//! Long axial neighbors inside a through-via keepout. A search can reach
//! the next open part of a connector breakout without expanding each cell.
use super::{monotone_trunk, Engine, Failure, PathEdge, Point, TargetBounds};

/// A no-via rectangle on any copper layer blocks through drills. The trace
/// can still run to another grid point on a legal, swept planar segment.
fn in_drill_exclusion(engine: &Engine, point: Point) -> Result<bool, Failure> {
    for area in &engine.input.rule_areas {
        engine.work.charge(1)?;
        if area.no_vias {
            if let Some((lo, hi)) = area.rectangular_bounds() {
                if (0..2).all(|k| lo[k] <= point[k] && point[k] <= hi[k]) {
                    return Ok(true);
                }
            }
        }
    }
    Ok(false)
}

pub(super) fn steps_taken(a: Point, b: Point, grid: u64) -> u64 {
    (a[0].abs_diff(b[0]) + a[1].abs_diff(b[1]))
        .div_ceil(grid)
        .max(1)
}

pub(super) fn toward_pad(
    engine: &Engine,
    net: u32,
    layer: usize,
    start: Point,
    previous: Option<PathEdge>,
    targets: &[TargetBounds<'_>],
) -> Result<[Option<Point>; 2], Failure> {
    let mut runs = [None; 2];
    if !in_drill_exclusion(engine, start)? {
        return Ok(runs);
    }
    engine.work.charge(targets.len() as u64)?;
    let target = targets
        .iter()
        .map(|t| [t.lo[0].midpoint(t.hi[0]), t.lo[1].midpoint(t.hi[1])])
        .min_by_key(|p| (p[0].abs_diff(start[0]) + p[1].abs_diff(start[1]), *p));
    let Some(target) = target else {
        return Ok(runs);
    };
    // An axial approach already has the ordinary endpoint-cap and via
    // landings. Reserve long neighbors for positioning an offset turn.
    if start[0] == target[0] || start[1] == target[1] {
        return Ok(runs);
    }
    let grid = engine.input.rules.grid_nm;
    for axis in 0..2 {
        let sign = (target[axis] - start[axis]).signum();
        let count = target[axis].abs_diff(start[axis]) / grid as u64;
        let continuing = match previous {
            Some(PathEdge::Track {
                start: a, end: b, ..
            }) => a[1 - axis] == b[1 - axis] && (b[axis] - a[axis]).signum() == sign,
            _ => false,
        };
        if count < 2 || continuing {
            continue;
        }
        let distance = monotone_trunk::clear_span(count as i64, |n| {
            let mut end = start;
            end[axis] += n * sign * grid;
            engine.legal_edge_on(net, layer, start, end)
        })?;
        if distance >= 2 {
            let mut delta = [0; 2];
            delta[axis] = distance * sign * grid;
            runs[axis] = Some(delta);
        }
    }
    Ok(runs)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::components::Copper;
    use crate::model::Input;

    #[test]
    fn long_neighbor_stops_before_foreign_copper_and_pays_the_cells_traveled() {
        let mut input: Input = serde_json::from_str(include_str!(
            "../../tests/fixtures/multiterminal-native-two-nets.json"
        ))
        .unwrap();
        input.layers = vec!["F.Cu".into(), "B.Cu".into()];
        input.pads[0].layers = vec!["F.Cu".into()];
        input.pads[0].at[1] += 100_000;
        input.rule_areas.push(serde_json::from_value(serde_json::json!({
            "id":"e18c8974-ea8c-4ad6-ba46-5d8ed014a7eb","layers":["B.Cu"],
            "outer":[[12000000,8000000],[18000000,8000000],[18000000,12000000],[12000000,12000000]],
            "no_tracks":false,"no_vias":true
        })).unwrap());
        let engine = Engine::new(input).unwrap();
        let goal = TargetBounds::new(
            Copper::Pad(&engine.input.pads[0]),
            engine.all_layers_slice(),
            &engine.work,
        )
        .unwrap();
        let goals = std::slice::from_ref(&goal);
        let start = [17_000_000, 10_000_000];
        let grid = engine.input.rules.grid_nm;
        let dx = toward_pad(&engine, 1, 0, start, None, goals).unwrap()[0].unwrap()[0];
        assert!(dx < -grid);
        let end = [start[0] + dx, start[1]];
        assert!(engine.legal_edge_on(1, 0, start, end).unwrap());
        assert!(!engine
            .legal_edge_on(1, 0, start, [end[0] - grid, end[1]])
            .unwrap());
        assert_eq!(
            steps_taken(start, end, grid as u64),
            dx.unsigned_abs() / grid as u64
        );
        assert_eq!(
            steps_taken(start, [start[0] - grid - 1, start[1]], grid as u64),
            2
        );
        let aligned = [start[0], engine.input.pads[0].copper_center()[1]];
        assert_eq!(
            toward_pad(&engine, 1, 0, aligned, None, goals).unwrap(),
            [None, None]
        );
        let previous = PathEdge::Track {
            start: [start[0] + grid, start[1]],
            end: start,
            layer: 0,
        };
        assert_eq!(
            toward_pad(&engine, 1, 0, start, Some(previous), goals).unwrap(),
            [None, None]
        );
        assert_eq!(
            toward_pad(&engine, 1, 0, [19_000_000, start[1]], None, goals).unwrap(),
            [None, None]
        );
    }
}
