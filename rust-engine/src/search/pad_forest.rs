//! Route the unobstructed edges of a geometric spanning forest before asking
//! the maze search to connect the remaining pad groups.
use super::three_pad_branch::Length;
use super::{allowed_track_layers, Copper, Engine, Failure, PathEdge, Point, TargetBounds};
use crate::diff_pair::octilinear_elbow_paths;
use crate::negotiate::{BlockedFrontier, TrialBudget};

// The distance to a point along an octilinear trace changes slope only on
// its axial and diagonal projections. Emit exact integer centerline contacts.
fn projections(a: Point, b: Point, terminal: Point) -> impl Iterator<Item = Point> {
    let direction: Point = std::array::from_fn(|k| (b[k] - a[k]).signum());
    let length = a[0].abs_diff(b[0]).max(a[1].abs_diff(b[1])) as i64;
    let dx = terminal[0] - a[0];
    let dy = terminal[1] - a[1];
    let [x, y] = direction;
    let equations = [(dx, x), (dy, y), (dx + dy, x + y), (dx - dy, x - y)];
    [a, b]
        .into_iter()
        .chain(equations.into_iter().filter_map(move |(n, d)| {
            if d == 0 || n % d != 0 {
                return None;
            }
            let offset = n / d;
            (0 < offset && offset < length).then(|| [a[0] + x * offset, a[1] + y * offset])
        }))
}

/// Keep the endpoint inside a surface pad, one track radius back along
/// the arm from its exit. A plated connection keeps the barrel center.
fn pad_landing(
    engine: &Engine,
    pad: &crate::model::Pad,
    point: Point,
    toward: Point,
    width: i64,
) -> Result<Point, Failure> {
    if pad.drill.is_some() || pad.shape.parts().len() != 1 {
        return Ok(point);
    }
    let direction: Point = std::array::from_fn(|k| (toward[k] - point[k]).signum());
    let at = |n: i64| std::array::from_fn(|k| point[k] + direction[k] * n);
    let limit = (0..2)
        .map(|k| (toward[k] - point[k]).abs())
        .max()
        .unwrap_or(0);
    let geometry = match &pad.shape.parts()[0] {
        crate::model::Shape::ConvexPolygon { vertices, .. } => 1 + 2 * vertices.len() as u64,
        _ => 1,
    };
    let inside = super::monotone_trunk::clear_span(limit, |n| {
        engine.work.charge(geometry)?;
        Ok(crate::geometry::point_in_pad(at(n), pad))
    })?;
    Ok(at((inside - (width + 1) / 2).max(0)))
}

pub(super) fn between(
    engine: &Engine,
    net: u32,
    groups: &[Vec<Copper<'_>>],
    budget: &mut TrialBudget<'_>,
    expanded: &mut u64,
    frontier: Option<&BlockedFrontier>,
) -> Result<Option<Vec<PathEdge>>, Failure> {
    let Some(share) = engine.surface_search_share else {
        return Ok(None);
    };
    let n = groups.len();
    if n < 4 {
        return Ok(None);
    }
    engine.work.charge(n as u64)?;
    if groups
        .iter()
        .any(|g| !matches!(g.as_slice(), [Copper::Pad(_)]))
    {
        return Ok(None);
    }
    let allowed = allowed_track_layers(engine, net)?;
    let layer_mask = allowed
        .iter()
        .enumerate()
        .fold(0, |mask, (i, ok)| mask | (u32::from(*ok) << i));
    let storage = n.saturating_mul(8);
    let work = &engine.work;
    // This proposal is optional. Leave the normal router its budget if a
    // quadratic scan cannot fit even before checking the actual copper.
    if storage > engine.input.limits.max_search_entries
        || (n as u64).saturating_mul(n as u64).saturating_mul(2) > work.remaining()
    {
        return Ok(None);
    }
    work.search_storage(storage)?;
    let mut pins = Vec::with_capacity(n);
    for group in groups {
        work.charge(1)?;
        let [item @ Copper::Pad(pad)] = group.as_slice() else {
            return Ok(None);
        };
        let target = TargetBounds::new(*item, engine.all_layers_slice(), work)?;
        let contact = match target.surface_contact(&allowed, work)? {
            Some((p, layer)) => Some((p, 1 << layer)),
            None => target.plated_contact(work)?,
        };
        let Some((p, layers)) = contact else {
            return Ok(None);
        };
        if layers & layer_mask == 0 {
            return Ok(None);
        }
        pins.push((p, layers & layer_mask, *pad));
    }
    // Prim's algorithm gives each pad at most one incoming span. A blocked
    // span leaves two independent copper islands for the ordinary search.
    let deadline = expanded.saturating_add(share).min(budget.current_limit);
    let mut in_tree = vec![false; n];
    let mut nearest = vec![None::<(u128, usize)>; n];
    let mut edges: Vec<(usize, PathEdge)> = Vec::new();
    let mut cluster: Vec<_> = (0..n).collect();
    let mut joins = 0;
    'tree: for _ in 0..n {
        work.charge(n as u64)?;
        let next = (0..n)
            .filter(|&i| !in_tree[i])
            .min_by_key(|&i| (nearest[i].is_none(), nearest[i], i));
        let Some(i) = next else {
            break;
        };
        let (a, mask, pad) = pins[i];
        if let Some((_, origin)) = nearest[i] {
            let (b, layers, previous_pad) = pins[origin];
            'span: for layer in 0..allowed.len() {
                work.charge(1)?;
                if mask & layers & (1 << layer) == 0 {
                    continue;
                }
                let mut nearest_contact = b;
                let mut shortest = Length::between(a, b);
                for &(tree, edge) in &edges {
                    work.charge(1)?;
                    let PathEdge::Track {
                        start,
                        end,
                        layer: face,
                    } = edge
                    else {
                        continue;
                    };
                    if tree != cluster[origin] || usize::from(face) != layer {
                        continue;
                    }
                    for q in projections(start, end, a) {
                        work.charge(1)?;
                        let length = Length::between(a, q);
                        if length.compare(shortest).is_lt() {
                            nearest_contact = q;
                            shortest = length;
                        }
                    }
                }
                for dest in [Some(nearest_contact), (nearest_contact != b).then_some(b)]
                    .into_iter()
                    .flatten()
                {
                    for mut line in octilinear_elbow_paths(a, dest) {
                        if *expanded >= deadline {
                            break 'tree;
                        }
                        budget.check_before_expand(engine, *expanded, frontier)?;
                        *expanded += 1;
                        if line.len() < 2 {
                            continue;
                        }
                        let last = line.len() - 1;
                        let width = engine
                            .effective_rules
                            .track_width_for(net, engine.layer_name(layer))?;
                        line[0] = pad_landing(engine, pad, a, line[1], width)?;
                        if dest == b {
                            line[last] =
                                pad_landing(engine, previous_pad, b, line[last - 1], width)?;
                        }
                        let mut legal = true;
                        for seg in line.windows(2) {
                            if !engine.legal_edge_on(net, layer, seg[0], seg[1])? {
                                legal = false;
                                break;
                            }
                        }
                        if legal {
                            cluster[i] = cluster[origin];
                            edges.extend(line.windows(2).map(|s| {
                                (
                                    cluster[i],
                                    PathEdge::Track {
                                        start: s[0],
                                        end: s[1],
                                        layer: layer as u8,
                                    },
                                )
                            }));
                            joins += 1;
                            break 'span;
                        }
                    }
                }
            }
        }
        in_tree[i] = true;
        work.charge(n as u64)?;
        for (k, &(b, layers, _)) in pins.iter().enumerate() {
            if in_tree[k] || mask & layers == 0 {
                continue;
            }
            let distance =
                u128::from(a[0].abs_diff(b[0])).pow(2) + u128::from(a[1].abs_diff(b[1])).pow(2);
            let value = (distance, i);
            if nearest[k].is_none_or(|old| value < old) {
                nearest[k] = Some(value);
            }
        }
    }
    if joins < 2 {
        return Ok(None);
    }
    work.charge(edges.len() as u64)?;
    Ok(Some(edges.into_iter().map(|(_, edge)| edge).collect()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{DrillShape, Input};

    #[test]
    fn projections_use_integral_positions_on_the_actual_segment() {
        let mut p = projections([10, 10], [20, 20], [11, 16]).collect::<Vec<_>>();
        p.sort_unstable();
        p.dedup();
        assert_eq!(p, [[10, 10], [11, 11], [16, 16], [20, 20]]);
        let p = projections([20, 20], [10, 10], [12, 16]).collect::<Vec<_>>();
        assert!(p.contains(&[14, 14]));
        assert!(p.iter().all(|v| v[0] == v[1] && (10..=20).contains(&v[0])));
    }

    #[test]
    fn clear_pad_groups_share_copper_and_a_blocked_span_remains_unjoined() {
        let mut template: Input = serde_json::from_str(include_str!(
            "../../tests/fixtures/multiterminal-native-two-nets.json"
        ))
        .unwrap();
        template.tracks.clear();
        template.layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
            .map(str::to_owned)
            .to_vec();
        template.pads.truncate(4);
        for (i, p) in template.pads.iter_mut().enumerate() {
            p.net = 1;
            p.at = [3_000_000 + i as i64 * 5_000_000, 7_000_000];
            p.layers = vec!["F.Cu".into()];
        }
        template.pads[3].layers = template.layers.clone();
        template.pads[3].drill = Some(DrillShape::Circle {
            diameter_nm: 400_000,
        });
        for (wall, share, expected) in [(false, 100, 3), (true, 100, 2), (false, 1, 0)] {
            let mut input = template.clone();
            if wall {
                input.rule_areas.push(serde_json::from_value(serde_json::json!({
                    "id":"a30ffe92-e9d8-482e-a5de-b14d42193f35","layers":["F.Cu"],
                    "outer":[[10000000,0],[11000000,0],[11000000,20000000],[10000000,20000000]],"no_tracks":true,"no_vias":false
                })).unwrap());
            }
            let mut engine = Engine::new(input).unwrap();
            engine.surface_search_share = Some(share);
            let groups = engine
                .input
                .pads
                .iter()
                .map(|p| vec![Copper::Pad(p)])
                .collect::<Vec<_>>();
            let mut used = 0;
            let result = between(
                &engine,
                1,
                &groups,
                &mut TrialBudget::fixed(100),
                &mut used,
                None,
            )
            .unwrap();
            assert!((1..=share).contains(&used));
            let edges = result.unwrap_or_default();
            assert_eq!(edges.len(), expected);
            let mut barrel = false;
            for e in edges {
                let PathEdge::Track {
                    start,
                    end,
                    layer: 0,
                } = e
                else {
                    panic!("unexpected layer or via")
                };
                assert!(engine.legal_edge_on(1, 0, start, end).unwrap());
                for p in [start, end] {
                    let pad = engine
                        .input
                        .pads
                        .iter()
                        .find(|pad| crate::geometry::point_in_pad(p, pad))
                        .unwrap();
                    if pad.drill.is_some() {
                        assert_eq!(p, pad.at);
                        barrel = true;
                    } else {
                        assert_ne!(p, pad.at);
                    }
                }
            }
            assert_eq!(barrel, expected > 0);
        }
    }
}
