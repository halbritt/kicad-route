//! Continuous zero-via escape between two isolated surface pads after the
//! four-direction grid, alternate grid phases and pad-field escapes are exhausted.
use super::{Copper, Engine, Failure, Path, PathEdge};
use crate::diff_pair::octilinear_elbow_paths;
use crate::geometry;
use crate::negotiate::{BlockedFrontier, CongestionHistory, TrialBudget};

#[allow(clippy::too_many_arguments)]
pub(super) fn between_pads(
    engine: &Engine,
    net: u32,
    source: &[Copper<'_>],
    targets: &[Vec<Copper<'_>>],
    history: Option<&CongestionHistory>,
    budget: &mut TrialBudget<'_>,
    expanded: &mut u64,
    frontier: Option<&BlockedFrontier>,
) -> Result<Option<Path>, Failure> {
    let ([Copper::Pad(source)], [group]) = (source, targets) else {
        return Ok(None);
    };
    let [Copper::Pad(target)] = group.as_slice() else {
        return Ok(None);
    };
    let stack = engine.all_layers_slice();
    let layer_mask = Copper::Pad(source).layer_mask(stack);
    if source.drill.is_some()
        || target.drill.is_some()
        || layer_mask.count_ones() != 1
        || Copper::Pad(target).layer_mask(stack) != layer_mask
    {
        return Ok(None);
    }
    let layer = layer_mask.trailing_zeros() as usize;
    let a = source.copper_center();
    let b = target.copper_center();
    // Axis-only routes belong to the existing grid search. Some custom pads
    // have a center outside copper; a geometric center is never a landing by itself.
    if a[0] == b[0] || a[1] == b[1] {
        return Ok(None);
    }
    engine.work.charge(2)?;
    if !geometry::point_in_pad(a, source)
        || !geometry::point_in_pad(b, target)
        || !engine.legal_edge_on(net, layer, a, a)?
        || !engine.legal_edge_on(net, layer, b, b)?
    {
        return Ok(None);
    }
    let grid = engine.input.rules.grid_nm;
    let mut best: Option<Path> = None;
    for points in octilinear_elbow_paths(a, b) {
        budget.check_before_expand(engine, *expanded, frontier)?;
        *expanded += 1;
        engine.work.charge(1)?;
        let mut edges = Vec::new();
        let mut cost = 0u64;
        for w in points.windows(2) {
            let start = w[0];
            let end = w[1];
            if !engine.legal_edge_on(net, layer, start, end)? {
                break;
            }
            // Path cost uses the existing grid axis-step units, with two axes
            // charged on a diagonal. The swept copper is a single native segment.
            let dx = start[0].abs_diff(end[0]);
            let dy = start[1].abs_diff(end[1]);
            let axis_steps = dx.div_ceil(grid as u64) + dy.div_ceil(grid as u64);
            cost =
                cost.saturating_add(axis_steps.saturating_mul(engine.layer_policy.weight(layer)));
            if let Some(h) = history {
                let len = dx.max(dy);
                for i in 1..=len.div_ceil(grid as u64) {
                    engine.work.charge(1)?;
                    let d = (i * (grid as u64)).min(len) as i64;
                    let p = std::array::from_fn(|j| start[j] + (end[j] - start[j]).signum() * d);
                    cost = cost.saturating_add(engine.layer_policy.scale(h.step_penalty(
                        layer as u8,
                        p,
                        engine.input.bounds.min,
                        grid,
                    )));
                }
            }
            edges.push(PathEdge::Track {
                start,
                end,
                layer: layer as u8,
            });
        }
        if edges.len() + 1 == points.len() && best.as_ref().is_none_or(|old| cost < old.steps) {
            best = Some(Path {
                points,
                edges,
                steps: cost,
            });
            // Both elbows sweep the same axis distance and layer. Their costs
            // can differ only in the presence of congestion history.
            if history.is_none() {
                break;
            }
        }
    }
    Ok(best)
}
