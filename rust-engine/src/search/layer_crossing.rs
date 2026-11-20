//! An axial connection can pass beneath a surface obstruction and return to its
//! original layer. The segment lengths and both through-vias must account for
//! the entire lower bound before this path can end the search.
use super::{Engine, Failure, Path, PathEdge, PlacedViaKey, Point, State, TargetBounds, ViaKind};

pub(super) fn axial(
    engine: &Engine,
    net: u32,
    state: State,
    remaining: u64,
    targets: &[TargetBounds<'_>],
    via_cost: u64,
    attachment: &impl Fn(usize, Point, Point, &[PlacedViaKey]) -> Result<Option<Point>, Failure>,
) -> Result<Option<Path>, Failure> {
    let face = state.layer as usize;
    let rate = engine.layer_policy.weight(face);
    let grid = engine.input.rules.grid_nm;
    let Some(planar) = remaining.checked_sub(engine.layer_policy.scale(via_cost).saturating_mul(2))
    else {
        return Ok(None);
    };
    let steps = planar / rate;
    if planar % rate != 0 || steps < 3 {
        return Ok(None);
    }
    let width = engine
        .effective_rules
        .track_width_for(net, engine.layer_name(face))?;
    let radius = (width + 1) / 2;
    let bounds = engine.input.bounds;
    for target in targets {
        engine.work.charge(1)?;
        if target.mask & (1 << face) == 0 {
            continue;
        }
        for axis in 0..2 {
            let j = 1 - axis;
            if state.at[j] < target.lo[j] - radius || state.at[j] > target.hi[j] + radius {
                continue;
            }
            let distance = state.at[axis].clamp(target.lo[axis], target.hi[axis]) - state.at[axis];
            let step = distance.signum() * grid;
            if step == 0 || steps > bounds.min[axis].abs_diff(bounds.max[axis]) / grid as u64 + 1 {
                continue;
            }
            let along = |i: u64| {
                let mut p = state.at;
                p[axis] += step * i as i64;
                p
            };
            let tip = along(steps);
            if tip[axis] < target.lo[axis] - radius - grid
                || tip[axis] > target.hi[axis] + radius + grid
            {
                continue;
            }
            let Some(end) = attachment(face, along(steps - 1), tip, &[])? else {
                continue;
            };
            if state.at[axis].abs_diff(end[axis]).div_ceil(grid as u64) != steps {
                continue;
            }
            let half = steps.div_ceil(2);
            let Some(first) = terminal_via(engine, net, face, state.at, half, along, &[])? else {
                continue;
            };
            let Some(second) =
                terminal_via(engine, net, face, end, half, |i| along(steps - i), &[first])?
            else {
                continue;
            };
            for inner in 0..engine.layer_count() {
                engine.work.charge(1)?;
                if inner == face
                    || engine.layer_policy.weight(inner) != rate
                    || !engine.legal_edge_on(net, inner, first.at, second.at)?
                {
                    continue;
                }
                return Ok(Some(Path {
                    points: vec![state.at, first.at, first.at, second.at, second.at, end],
                    edges: vec![
                        PathEdge::Track {
                            start: state.at,
                            end: first.at,
                            layer: state.layer,
                        },
                        PathEdge::Via(first),
                        PathEdge::Track {
                            start: first.at,
                            end: second.at,
                            layer: inner as u8,
                        },
                        PathEdge::Via(second),
                        PathEdge::Track {
                            start: second.at,
                            end,
                            layer: state.layer,
                        },
                    ],
                    steps: remaining,
                }));
            }
        }
    }
    Ok(None)
}

// Probe grid-aligned sites outward from a terminal at exponentially increasing
// distances. A blocked surface segment cuts off every longer segment on the ray.
// The ordinary search can still use any legal site between the sampled ones.
fn terminal_via(
    engine: &Engine,
    net: u32,
    face: usize,
    contact: Point,
    half: u64,
    site: impl Fn(u64) -> Point,
    previous: &[PlacedViaKey],
) -> Result<Option<PlacedViaKey>, Failure> {
    let last = engine.layer_count() - 1;
    let mut offset = 1;
    while offset < half {
        engine.work.charge(1)?;
        let at = site(offset);
        if !engine.legal_edge_on(net, face, contact, at)? {
            break;
        }
        if engine
            .legal_via(net, at, 0, last, ViaKind::Through, previous)?
            .is_some()
        {
            return Ok(Some(PlacedViaKey {
                at,
                start_layer: 0,
                end_layer: last as u8,
                kind: 0,
            }));
        }
        offset *= 2;
    }
    Ok(None)
}
