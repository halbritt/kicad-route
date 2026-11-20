//! A connection can pass beneath a surface obstruction and return to its
//! original layer. The segment lengths and both through-vias must account for
//! the entire lower bound before this path can end the search.
use super::{Engine, Failure, Path, PathEdge, PlacedViaKey, Point, State, TargetBounds, ViaKind};

pub(super) fn between_terminals(
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
            if let Some(path) = cross(engine, net, face, state.at, end, first, second, remaining)? {
                return Ok(Some(path));
            }
        }
        if let Some(path) = offset(
            engine, net, face, state.at, target, radius, steps, remaining, attachment,
        )? {
            return Ok(Some(path));
        }
    }
    Ok(None)
}

// Split the planar cost between the two axes. All bends and both drill sites
// stay on the source lattice. Only the last axial contact may be clipped.
fn offset(
    engine: &Engine,
    net: u32,
    face: usize,
    source: Point,
    target: &TargetBounds<'_>,
    radius: i64,
    steps: u64,
    cost: u64,
    attachment: &impl Fn(usize, Point, Point, &[PlacedViaKey]) -> Result<Option<Point>, Failure>,
) -> Result<Option<Path>, Failure> {
    let grid = engine.input.rules.grid_nm;
    let mut signs = [0; 2];
    let mut min = [0; 2];
    let mut max = [0; 2];
    for k in 0..2 {
        let low = target.lo[k] - radius;
        let high = target.hi[k] + radius;
        let near = source[k].clamp(low, high);
        signs[k] = (near - source[k]).signum();
        if signs[k] == 0 {
            return Ok(None);
        }
        let far = if signs[k] > 0 { high } else { low };
        min[k] = source[k].abs_diff(near).div_ceil(grid as u64);
        max[k] = source[k].abs_diff(far).div_ceil(grid as u64);
    }
    let lo = min[0].max(steps.saturating_sub(max[1]));
    let hi = max[0].min(steps.saturating_sub(min[1]));
    if hi < lo || steps < min[1] {
        return Ok(None);
    }
    let splits = [lo.midpoint(hi), lo, hi];
    for (i, &dx) in splits.iter().enumerate() {
        if splits[..i].contains(&dx) {
            continue;
        }
        let count = [dx, steps - dx];
        let tip = std::array::from_fn(|k| source[k] + signs[k] * grid * count[k] as i64);
        let shift = |mut p: Point, axis: usize, n: i64| {
            p[axis] += n * grid * signs[axis];
            p
        };
        for finish_axis in 0..2 {
            engine.work.charge(1)?;
            let Some(end) = attachment(face, shift(tip, finish_axis, -1), tip, &[])? else {
                continue;
            };
            if (0..2)
                .map(|k| source[k].abs_diff(end[k]).div_ceil(grid as u64))
                .sum::<u64>()
                != steps
            {
                continue;
            }
            for start_axis in [finish_axis, 1 - finish_axis] {
                let limit = |k: usize| {
                    if start_axis == finish_axis {
                        count[k].div_ceil(2)
                    } else {
                        count[k]
                    }
                };
                let Some(first) = terminal_via(
                    engine,
                    net,
                    face,
                    source,
                    limit(start_axis),
                    |i| shift(source, start_axis, i as i64),
                    &[],
                )?
                else {
                    continue;
                };
                let Some(second) = terminal_via(
                    engine,
                    net,
                    face,
                    end,
                    limit(finish_axis),
                    |i| shift(tip, finish_axis, -(i as i64)),
                    &[first],
                )?
                else {
                    continue;
                };
                if let Some(path) = cross(engine, net, face, source, end, first, second, cost)? {
                    return Ok(Some(path));
                }
            }
        }
    }
    Ok(None)
}

fn cross(
    engine: &Engine,
    net: u32,
    face: usize,
    source: Point,
    end: Point,
    first: PlacedViaKey,
    second: PlacedViaKey,
    cost: u64,
) -> Result<Option<Path>, Failure> {
    let rate = engine.layer_policy.weight(face);
    for inner in 0..engine.layer_count() {
        engine.work.charge(1)?;
        if inner == face || engine.layer_policy.weight(inner) != rate {
            continue;
        }
        for bends in crate::diff_pair::octilinear_elbow_paths(first.at, second.at) {
            if bends.len() < 2 {
                continue;
            }
            let mut legal = true;
            for t in bends.windows(2) {
                engine.work.charge(1)?;
                if !engine.legal_edge_on(net, inner, t[0], t[1])? {
                    legal = false;
                    break;
                }
            }
            if !legal {
                continue;
            }
            let mut points = vec![source, first.at];
            points.extend_from_slice(&bends);
            points.extend([second.at, end]);
            let mut edges = vec![
                PathEdge::Track {
                    start: source,
                    end: first.at,
                    layer: face as u8,
                },
                PathEdge::Via(first),
            ];
            edges.extend(bends.windows(2).map(|t| PathEdge::Track {
                start: t[0],
                end: t[1],
                layer: inner as u8,
            }));
            edges.extend([
                PathEdge::Via(second),
                PathEdge::Track {
                    start: second.at,
                    end,
                    layer: face as u8,
                },
            ]);
            return Ok(Some(Path {
                points,
                edges,
                steps: cost,
            }));
        }
    }
    Ok(None)
}

/// Prefer the existing axial samples. If every ray misses, a narrow drill
/// window can still lie between them. Charge each additional grid site to
/// the calling search and retain the nearest reachable window.
pub(super) fn drill_sites(
    engine: &Engine,
    net: u32,
    at: Point,
    layer: usize,
    extra_sites: Option<&mut dyn FnMut() -> Result<bool, Failure>>,
) -> Result<Vec<PlacedViaKey>, Failure> {
    let grid = engine.input.rules.grid_nm;
    let last = engine.layer_count() - 1;
    let rays = [(0, -1), (0, 1), (1, -1), (1, 1)];
    let mut sites = Vec::with_capacity(rays.len());
    let mut ends = [0; 4];
    let along = |axis: usize, sign: i64, n: u64| {
        let mut p = at;
        p[axis] += sign * grid * n as i64;
        p
    };
    for (i, (axis, sign)) in rays.into_iter().enumerate() {
        let edge = if sign < 0 {
            engine.input.bounds.min
        } else {
            engine.input.bounds.max
        };
        let extent = edge[axis].abs_diff(at[axis]) / grid as u64;
        let sample = sample_via(
            engine,
            net,
            layer,
            at,
            extent + 1,
            |n| along(axis, sign, n),
            &[],
        )?;
        if let Some(via) = sample.via {
            sites.push(via);
        }
        ends[i] = sample.end;
    }
    let Some(expand) = extra_sites else {
        return Ok(sites);
    };
    if !sites.is_empty() {
        return Ok(sites);
    }
    let mut next = [3; 4];
    loop {
        for (i, (axis, sign)) in rays.into_iter().enumerate() {
            next[i] = next_drill_offset(engine, at, axis, sign, next[i], ends[i])?;
        }
        let Some(offset) = next
            .iter()
            .zip(ends)
            .filter_map(|(&n, end)| (n < end).then_some(n))
            .min()
        else {
            break;
        };
        for (i, (axis, sign)) in rays.into_iter().enumerate() {
            if next[i] != offset || offset >= ends[i] {
                continue;
            }
            if !expand()? {
                return Ok(sites);
            }
            engine.work.charge(1)?;
            let p = along(axis, sign, offset);
            if !engine.legal_edge_on(net, layer, at, p)? {
                ends[i] = offset;
            } else if engine
                .legal_via(net, p, 0, last, ViaKind::Through, &[])?
                .is_some()
            {
                sites.push(PlacedViaKey {
                    at: p,
                    start_layer: 0,
                    end_layer: last as u8,
                    kind: 0,
                });
            }
            next[i] += 1;
        }
        if !sites.is_empty() {
            break;
        }
    }
    Ok(sites)
}

/// A through drill spans every active copper layer. All admitted rule areas
/// name at least one such layer. Skip centers contained in a solid no-via
/// rectangle, leaving holes, slanted boundaries and clearances to legal_via.
fn next_drill_offset(
    engine: &Engine,
    at: Point,
    axis: usize,
    sign: i64,
    mut n: u64,
    end: u64,
) -> Result<u64, Failure> {
    let grid = engine.input.rules.grid_nm;
    while n < end {
        engine.work.charge(1)?;
        if n.is_power_of_two() {
            n += 1;
            continue;
        }
        let pos = at[axis] + sign * grid * n as i64;
        let mut next = n;
        engine.work.charge(engine.input.rule_areas.len() as u64)?;
        for area in &engine.input.rule_areas {
            if !area.no_vias {
                continue;
            }
            let Some((lo, hi)) = area.rectangular_bounds() else {
                continue;
            };
            if (lo[axis]..=hi[axis]).contains(&pos)
                && (lo[1 - axis]..=hi[1 - axis]).contains(&at[1 - axis])
            {
                let far = if sign < 0 { lo[axis] } else { hi[axis] };
                next = next.max(at[axis].abs_diff(far) / grid as u64 + 1);
            }
        }
        if next == n {
            break;
        }
        n = next;
    }
    Ok(n)
}

struct RaySample {
    via: Option<PlacedViaKey>,
    // First blocked sample or one past the supplied extent.
    end: u64,
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
    Ok(sample_via(engine, net, face, contact, half, site, previous)?.via)
}

fn sample_via(
    engine: &Engine,
    net: u32,
    face: usize,
    contact: Point,
    half: u64,
    site: impl Fn(u64) -> Point,
    previous: &[PlacedViaKey],
) -> Result<RaySample, Failure> {
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
            return Ok(RaySample {
                via: Some(PlacedViaKey {
                    at,
                    start_layer: 0,
                    end_layer: last as u8,
                    kind: 0,
                }),
                end: half,
            });
        }
        offset *= 2;
    }
    Ok(RaySample {
        via: None,
        end: offset.min(half),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Input;

    #[test]
    fn a_solid_via_keepout_on_one_layer_skips_the_whole_ray() {
        let mut input: Input = serde_json::from_str(include_str!(
            "../../tests/fixtures/multiterminal-native-two-nets.json"
        ))
        .unwrap();
        input.layers = vec!["F.Cu".into(), "B.Cu".into()];
        input.rule_areas.push(
            serde_json::from_value(serde_json::json!({
                "id":"53b118ae-365c-4b7a-9bbf-d6fed14cd3e1","layers":["B.Cu"],
                "outer":[[0,0],[40000000,0],[40000000,20000000],[0,20000000]],
                "no_tracks":false,"no_vias":true
            }))
            .unwrap(),
        );
        let engine = Engine::new(input).unwrap();
        let mut probes = 0;
        let found = drill_sites(
            &engine,
            1,
            [10_000_000, 10_000_000],
            0,
            Some(&mut || {
                probes += 1;
                Ok(true)
            }),
        )
        .unwrap();
        assert!(found.is_empty());
        assert_eq!(probes, 0);
    }

    #[test]
    fn drill_window_between_samples_requires_a_paid_reachable_landing() {
        let mut input: Input = serde_json::from_str(include_str!(
            "../../tests/fixtures/multiterminal-native-two-nets.json"
        ))
        .unwrap();
        input.layers = vec!["F.Cu".into(), "B.Cu".into()];
        input.pads.clear();
        input.tracks.clear();
        // The 250 µm grid puts the only accessible drill at six steps from
        // the contact, between the four- and eight-step geometric samples.
        input.rule_areas.push(serde_json::from_value(serde_json::json!({
            "id":"854bc63a-7fdf-43fb-a486-21372fffcdda","layers":["F.Cu","B.Cu"],
            "outer":[[0,0],[40000000,0],[40000000,20000000],[0,20000000]],
            "holes":[[[11000000,9500000],[11000000,10500000],[12000000,10500000],[12000000,9500000]]],
            "no_tracks":false,"no_vias":true
        })).unwrap());
        let engine = Engine::new(input).unwrap();
        let source = [10_000_000, 10_000_000];
        let sample = |limit| {
            let mut charged = 0;
            let sites = drill_sites(
                &engine,
                1,
                source,
                0,
                Some(&mut || {
                    if charged == limit {
                        return Ok(false);
                    }
                    charged += 1;
                    Ok(true)
                }),
            )
            .unwrap();
            (charged, sites)
        };
        let (used, none) = sample(1);
        assert_eq!(used, 1);
        assert!(none.is_empty());
        let (used, sites) = sample(100);
        assert!(used > 1 && used < 100);
        assert_eq!(sites.len(), 1);
        let via = sites[0];
        assert_eq!(via.at, [11_500_000, 10_000_000]);
        assert!(engine.legal_edge_on(1, 0, source, via.at).unwrap());
        assert!(engine
            .legal_via(1, via.at, 0, 1, ViaKind::Through, &[])
            .unwrap()
            .is_some());
    }
}
