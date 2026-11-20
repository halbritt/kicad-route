//! Route the unobstructed edges of a geometric spanning forest before asking
//! the maze search to connect the remaining pad groups.
use super::three_pad_branch::Length;
use super::{allowed_track_layers, Copper, Engine, Failure, PathEdge, Point, TargetBounds};
use crate::diff_pair::octilinear_elbow_paths;
use crate::negotiate::{BlockedFrontier, TrialBudget};

// The distance to a point along an octilinear trace changes slope only on
// its axial and diagonal projections. Emit exact integer centerline contacts.
pub(super) fn interior_projections(
    a: Point,
    b: Point,
    terminal: Point,
) -> impl Iterator<Item = Point> {
    let direction: Point = std::array::from_fn(|k| (b[k] - a[k]).signum());
    let length = a[0].abs_diff(b[0]).max(a[1].abs_diff(b[1])) as i64;
    let dx = terminal[0] - a[0];
    let dy = terminal[1] - a[1];
    let [x, y] = direction;
    let equations = [(dx, x), (dy, y), (dx + dy, x + y), (dx - dy, x - y)];
    equations.into_iter().filter_map(move |(n, d)| {
        if d == 0 || n % d != 0 {
            return None;
        }
        let offset = n / d;
        (0 < offset && offset < length).then(|| [a[0] + x * offset, a[1] + y * offset])
    })
}

fn projections(a: Point, b: Point, terminal: Point) -> impl Iterator<Item = Point> {
    [a, b]
        .into_iter()
        .chain(interior_projections(a, b, terminal))
}

/// Keep the endpoint inside a surface pad, one track radius back along
/// the arm from its exit. A plated connection keeps the barrel center.
pub(super) fn pad_landing(
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
    // A forest must join at least two pad pairs. Leave a smaller allowance
    // to the ordinary single-join search.
    if n < 4 || share.min(budget.current_limit.saturating_sub(*expanded)) < 2 {
        return Ok(None);
    }
    engine.work.charge(n as u64)?;
    let allowed = allowed_track_layers(engine, net)?;
    let layer_mask = allowed
        .iter()
        .enumerate()
        .fold(0, |mask, (i, ok)| mask | (u32::from(*ok) << i));
    let work = &engine.work;
    let mut preconnected = false;
    let mut pad_count = 0usize;
    for group in groups {
        preconnected |= group.len() > 1;
        work.charge(group.len() as u64)?;
        for item in group {
            match item {
                Copper::Pad(_) => pad_count += 1,
                Copper::Via(_) => {}
                Copper::Track(t) => {
                    if !super::trace_ends_connected(t, group, engine.all_layers_slice(), work)? {
                        return Ok(None);
                    }
                }
                _ => return Ok(None),
            }
        }
    }
    let storage = n.saturating_mul(8).saturating_add(pad_count);
    if storage > engine.input.limits.max_search_entries
        || (pad_count as u64)
            .saturating_mul(pad_count as u64)
            .saturating_mul(2)
            > work.remaining()
    {
        return Ok(None);
    }
    work.search_storage(storage)?;
    let mut pins = Vec::with_capacity(n);
    let mut shared_layers = layer_mask;
    for group in groups {
        let mut group_layers = 0;
        let mut contacts = Vec::new();
        for item in group {
            work.charge(1)?;
            let Copper::Pad(pad) = *item else {
                continue;
            };
            let target = TargetBounds::new(*item, engine.all_layers_slice(), work)?;
            let contact = match target.surface_contact(&allowed, work)? {
                Some((p, layer)) => Some((p, 1 << layer)),
                None => target.plated_contact(work)?,
            };
            if let Some((p, layers)) = contact {
                if layers & layer_mask != 0 {
                    group_layers |= layers & layer_mask;
                    contacts.push((p, layers & layer_mask, pad));
                }
            }
        }
        if contacts.is_empty() {
            return Ok(None);
        }
        shared_layers &= group_layers;
        pins.push(contacts);
    }
    // A partially wired component can expose pads on opposite faces. When
    // the groups share no routing layer, leave the layer transitions to the
    // multilayer search before committing to more planar branches.
    if shared_layers == 0 && preconnected {
        return Ok(None);
    }
    // Prim's algorithm gives each component at most one incoming span. A blocked
    // span leaves two independent copper islands for the ordinary search.
    let deadline = expanded.saturating_add(share).min(budget.current_limit);
    let mut in_tree = vec![false; n];
    let mut nearest = vec![None::<(u128, usize, usize, usize)>; n];
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
        if let Some((_, origin, source_pin, destination_pin)) = nearest[i] {
            let (a, mask, pad) = pins[i][destination_pin];
            let (b, layers, previous_pad) = pins[origin][source_pin];
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
        for (k, other) in pins.iter().enumerate() {
            if in_tree[k] {
                continue;
            }
            for (source_pin, &(a, mask, _)) in pins[i].iter().enumerate() {
                work.charge(other.len() as u64)?;
                for (destination_pin, &(b, layers, _)) in other.iter().enumerate() {
                    if mask & layers == 0 {
                        continue;
                    }
                    let distance = u128::from(a[0].abs_diff(b[0])).pow(2)
                        + u128::from(a[1].abs_diff(b[1])).pow(2);
                    let value = (distance, i, source_pin, destination_pin);
                    if nearest[k].is_none_or(|old| value < old) {
                        nearest[k] = Some(value);
                    }
                }
            }
        }
    }
    if joins < 2 {
        return Ok(None);
    }
    work.charge(edges.len() as u64)?;
    Ok(Some(edges.into_iter().map(|(_, edge)| edge).collect()))
}

/// Give competing distribution nets a turn at their shared footprints before
/// an unrelated whole-net search can use the pad exits. Only existing legal
/// forest edges are inserted. The regular transaction connects every island.
pub(crate) fn route_shared_footprint_branches(
    engine: &mut Engine,
    units: &[crate::negotiate::UnitSpec],
    pad_counts: &std::collections::BTreeMap<u32, usize>,
    batch_limit: u64,
    expanded: &mut u64,
) -> Result<(), Failure> {
    let mut competing = std::collections::BTreeSet::new();
    {
        let mut first = std::collections::BTreeMap::new();
        engine.work.charge(engine.input.pads.len() as u64)?;
        for pad in &engine.input.pads {
            // A forest needs four separate terminals for two independent joins.
            if pad_counts.get(&pad.net).copied().unwrap_or(0) < 4
                || engine.copper.has_generated_for_net(pad.net)
            {
                continue;
            }
            let Some(reference) = pad.footprint_ref.as_deref().filter(|r| !r.is_empty()) else {
                continue;
            };
            let layers = if pad.layers.is_empty() {
                std::slice::from_ref(&engine.input.layer)
            } else {
                &pad.layers
            };
            engine.work.charge(layers.len() as u64)?;
            for layer in layers {
                // A new footprint and its two nets can add three entries.
                if first.len() + competing.len() + 3 > engine.input.limits.max_search_entries {
                    return Ok(());
                }
                let other = *first.entry((reference, layer.as_str())).or_insert(pad.net);
                if other != pad.net {
                    competing.insert(other);
                    competing.insert(pad.net);
                }
            }
        }
    }
    if competing.is_empty() {
        return Ok(());
    }
    let mut budget = TrialBudget::fixed(batch_limit);
    for unit in units {
        engine.work.charge(1)?;
        let crate::negotiate::RouteUnitId::SingleNet(net) = unit.id else {
            continue;
        };
        if competing.contains(&net) {
            let groups = engine.components(net)?;
            if let Some(edges) = between(engine, net, &groups, &mut budget, expanded, None)? {
                // insert_edges checks continuous clearance before inserting any
                // copper. A geometric miss leaves the ordinary route available.
                if let Err(error) = engine.insert_edges(net, edges) {
                    if error.status != crate::model::Status::Unroutable {
                        return Err(error);
                    }
                }
            }
        }
    }
    Ok(())
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
    fn competing_pad_forests_share_a_footprint_and_face() {
        use crate::negotiate::{RouteUnitAdapter, SingleEndedAdapter};
        let mut template: Input = serde_json::from_str(include_str!(
            "../../tests/fixtures/multiterminal-native-two-nets.json"
        ))
        .unwrap();
        template.tracks.clear();
        template.layers = vec!["F.Cu".into(), "B.Cu".into()];
        let sample = template.pads[0].clone();
        template.pads = (0..8)
            .map(|i| {
                let mut pad = sample.clone();
                pad.id = format!("00000000-0000-4000-8000-{i:012x}");
                pad.net = 1 + i / 4;
                pad.at = [
                    4_000_000 + i64::from(i % 4) * 5_000_000,
                    5_000_000 * i64::from(pad.net),
                ];
                pad.layers = vec!["F.Cu".into()];
                pad.footprint_ref = Some(format!("J{}", i % 4));
                pad
            })
            .collect();
        for (shared, same_face, remaining) in [(true, true, 1), (false, true, 4), (true, false, 4)]
        {
            let mut input = template.clone();
            for pad in input.pads.iter_mut().filter(|p| p.net == 2) {
                if !shared {
                    pad.footprint_ref = Some("J5".into());
                }
                if !same_face {
                    pad.layers = vec!["B.Cu".into()];
                }
            }
            let mut engine = Engine::new(input).unwrap();
            engine
                .transaction(|tx| -> Result<(), Failure> {
                    tx.engine.surface_search_share = Some(100);
                    let units = SingleEndedAdapter::all().units(tx.engine)?;
                    route_shared_footprint_branches(
                        tx.engine,
                        &units,
                        &[(1, 4), (2, 4)].into(),
                        100,
                        &mut 0,
                    )?;
                    for net in [1, 2] {
                        assert_eq!(tx.engine.components(net)?.len(), remaining);
                    }
                    Ok(())
                })
                .unwrap();
        }
    }

    #[test]
    fn a_preconnected_pair_can_branch_from_either_pad() {
        let mut template: Input = serde_json::from_str(include_str!(
            "../../tests/fixtures/multiterminal-native-two-nets.json"
        ))
        .unwrap();
        template.layers = vec!["F.Cu".into(), "B.Cu".into()];
        template.pads.truncate(5);
        for (p, x) in template.pads.iter_mut().zip([12, 24, 7, 29, 34]) {
            p.net = 1;
            p.layers = vec!["F.Cu".into()];
            p.at = [x * 1_000_000, 8_000_000];
        }
        template.tracks.truncate(1);
        template.tracks[0].net = 1;
        template.tracks[0].start = template.pads[0].at;
        template.tracks[0].end = template.pads[1].at;
        for (dangling, opposite_face) in [(false, false), (true, false), (false, true)] {
            let mut input = template.clone();
            if dangling {
                input.tracks[0].end[0] -= 2_000_000;
            }
            if opposite_face {
                input.pads[4].layers = vec!["B.Cu".into()];
            }
            let mut engine = Engine::new(input).unwrap();
            engine.surface_search_share = Some(100);
            let mut components = engine.components(1).unwrap();
            for start_x in [12, 29] {
                let first = components
                    .iter()
                    .position(|group| {
                        group.iter().any(
                            |item| matches!(item, Copper::Pad(p) if p.at[0] == start_x * 1_000_000),
                        )
                    })
                    .unwrap();
                components.swap(0, first);
                let mut expanded = 0;
                let result = between(
                    &engine,
                    1,
                    &components,
                    &mut TrialBudget::fixed(100),
                    &mut expanded,
                    None,
                )
                .unwrap();
                if dangling || opposite_face {
                    assert!(result.is_none());
                    assert_eq!(expanded, 0);
                    continue;
                }
                let edges = result.unwrap();
                assert_eq!(edges.len(), 3);
                assert_eq!(expanded, 3);
                let mut spans = Vec::new();
                for edge in edges {
                    let PathEdge::Track {
                        start,
                        end,
                        layer: 0,
                    } = edge
                    else {
                        panic!("expected front trace")
                    };
                    assert!(engine.legal_edge_on(1, 0, start, end).unwrap());
                    let pad_x = |p| {
                        engine
                            .input
                            .pads
                            .iter()
                            .find(|pad| crate::geometry::point_in_pad(p, pad))
                            .unwrap()
                            .at[0]
                            / 1_000_000
                    };
                    let a = pad_x(start);
                    let b = pad_x(end);
                    spans.push((a.min(b), a.max(b)));
                }
                spans.sort_unstable();
                assert_eq!(spans, [(7, 12), (24, 29), (29, 34)]);
            }
        }
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
            if share < 2 {
                assert_eq!(used, 0);
            } else {
                assert!((1..=share).contains(&used));
            }
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
