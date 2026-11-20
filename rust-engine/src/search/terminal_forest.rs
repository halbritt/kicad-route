//! Attach separated surface lands to a partially stitched multilayer net.
//! Try short same-layer branches first. The remaining surface components
//! can drill to a plated contact or tap a buried branch just constructed.
mod plated;

use super::{allowed_track_layers, Copper, Engine, Failure, PathEdge, Point, TargetBounds};
use crate::diff_pair::octilinear_elbow_paths;
use crate::negotiate::{BlockedFrontier, TrialBudget};

fn try_expand(
    engine: &Engine,
    budget: &mut TrialBudget<'_>,
    expanded: &mut u64,
    stop: u64,
    frontier: Option<&BlockedFrontier>,
) -> Result<bool, Failure> {
    if *expanded >= stop {
        return Ok(false);
    }
    budget.check_before_expand(engine, *expanded, frontier)?;
    *expanded += 1;
    Ok(true)
}

fn clear_path(engine: &Engine, net: u32, layer: usize, points: &[Point]) -> Result<bool, Failure> {
    for ends in points.windows(2) {
        if !engine.legal_edge_on(net, layer, ends[0], ends[1])? {
            return Ok(false);
        }
    }
    Ok(true)
}

#[derive(Clone, Copy)]
struct Terminal<'a> {
    pad: Option<&'a crate::model::Pad>,
    group: usize,
    at: Point,
    mask: u32,
}

// The ordinary tree tends to use less copper when its surface pads have
// usable pitch-aligned fanouts. Select this alternative only when the
// physical copper exposes a reachable off-pitch drill at the pad boundary.
#[allow(clippy::too_many_arguments)]
fn constrained_pad_access(
    engine: &Engine,
    net: u32,
    terminals: &[Terminal<'_>],
    groups: &[Vec<Copper<'_>>],
    budget: &mut TrialBudget<'_>,
    expanded: &mut u64,
    stop: u64,
    frontier: Option<&BlockedFrontier>,
) -> Result<bool, Failure> {
    let Some(diameter) = super::layer_crossing::uncapped_drill_diameter(engine, net)? else {
        return Ok(false);
    };
    let pitch = engine.input.rules.grid_nm as u64;
    let last = engine.layer_count() - 1;
    for pin in terminals {
        engine.work.charge(1)?;
        let Some(pad) = pin.pad else {
            continue;
        };
        if pin.mask.count_ones() != 1 {
            continue;
        }
        let Some(mut positions) = super::layer_crossing::pad_edge_positions(pad, pin.at, diameter)
        else {
            continue;
        };
        let off_pitch = |p: &Point| {
            p[0].abs_diff(pin.at[0]) % pitch != 0 || p[1].abs_diff(pin.at[1]) % pitch != 0
        };
        if !positions.iter().any(off_pitch) {
            continue;
        }
        let layer = pin.mask.trailing_zeros() as usize;
        if super::layer_crossing::has_axial_drill(engine, net, layer, pin.at)? {
            continue;
        }
        positions.sort_unstable();
        for point in positions.into_iter().filter(off_pitch) {
            if !try_expand(engine, budget, expanded, stop, frontier)? {
                return Ok(false);
            }
            if !engine.legal_edge_on(net, layer, pin.at, point)? {
                continue;
            }
            // An earlier accepted branch may have already used this escape.
            // Only a barrel in the pad's own electrical component qualifies.
            engine.work.charge(groups[pin.group].len() as u64)?;
            let attached = groups[pin.group].iter().any(|item| {
                matches!(item, Copper::Via(via) if via.at == point
                    && via.diameter_nm == diameter && via.kind == super::ViaKind::Through)
            });
            if attached
                || engine
                    .legal_via(net, point, 0, last, super::ViaKind::Through, &[])?
                    .is_some()
            {
                return Ok(true);
            }
        }
    }
    Ok(false)
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
    if groups.len() < 3
        || engine.layer_count() < 3
        || share.min(budget.current_limit.saturating_sub(*expanded)) < 2
    {
        return Ok(None);
    }
    let work = &engine.work;
    work.charge(groups.len() as u64)?;
    if groups.len() < 4 && groups.iter().filter(|group| group.len() > 1).count() < 2 {
        return Ok(None);
    }
    if !groups.iter().any(|g| g.len() > 1) {
        return Ok(None);
    }
    let allowed = allowed_track_layers(engine, net)?;
    let enabled = allowed
        .iter()
        .enumerate()
        .fold(0, |mask, (i, ok)| mask | (u32::from(*ok) << i));
    let stackup = engine.all_layers_slice();
    let mut terminals = Vec::new();
    let mut stitched = false;
    let mut drilled_groups = 0;
    let mut has_track = false;
    let mut root_is_wired_and_plated = false;
    let mut wired_plated_groups = 0;
    let mut isolated_surface_pad = false;
    for (group, members) in groups.iter().enumerate() {
        let mut first_barrel = None;
        let mut wired = false;
        for &item in members {
            work.charge(1)?;
            let entry = match item {
                Copper::Via(via) => Some((via.at, item.layer_mask(stackup))),
                Copper::Pad(_) => {
                    let bounds = TargetBounds::new(item, stackup, work)?;
                    match bounds.plated_contact(work)? {
                        Some(barrel) => Some(barrel),
                        None => bounds
                            .surface_contact(&allowed, work)?
                            .map(|(p, l)| (p, 1 << l)),
                    }
                }
                Copper::Track(_) => {
                    has_track = true;
                    wired = true;
                    continue;
                }
                _ => return Ok(None),
            };
            if let Some((at, mask)) = entry {
                let mask = mask & enabled;
                if mask != 0 {
                    if mask.count_ones() > 1 {
                        match first_barrel {
                            None => {
                                first_barrel = Some(at);
                                drilled_groups += 1;
                            }
                            Some(first) => stitched |= first != at,
                        }
                    }
                    work.search_storage(groups.len() + terminals.len() + 1)?;
                    let pad = match item {
                        Copper::Pad(p) => Some(p),
                        _ => None,
                    };
                    terminals.push(Terminal {
                        group,
                        at,
                        mask,
                        pad,
                    });
                }
            }
        }
        if wired && first_barrel.is_some() {
            wired_plated_groups += 1;
            root_is_wired_and_plated |= group == 0;
        }
        isolated_surface_pad |=
            first_barrel.is_none() && matches!(members.as_slice(), [Copper::Pad(_)]);
    }
    // A stitched component supplies distinct places to enter buried copper.
    // An unjoined barrel and several surface components can then share its
    // branches. Smaller single-join problems keep their ordinary search.
    if !has_track || !stitched || drilled_groups < 2 {
        return Ok(None);
    }
    let deadline = expanded.saturating_add(share).min(budget.current_limit);
    // With two independently wired plated branches, the common layers let us
    // join their local copper before searching from every track on the root.
    if root_is_wired_and_plated && wired_plated_groups >= 2 && !isolated_surface_pad {
        if let Some(edges) = plated::between(
            engine,
            net,
            &terminals,
            groups.len(),
            drilled_groups,
            budget,
            expanded,
            deadline,
            frontier,
        )? {
            return Ok(Some(edges));
        }
    }
    if groups.len() < 4 || groups.len() - drilled_groups < 2 {
        return Ok(None);
    }
    if !constrained_pad_access(
        engine, net, &terminals, groups, budget, expanded, deadline, frontier,
    )? {
        return Ok(None);
    }
    let possible = terminals
        .len()
        .saturating_mul(terminals.len().saturating_sub(1))
        / 2;
    let n = groups.len();
    let entries = terminals
        .len()
        .saturating_add(2 * n)
        .saturating_add(possible);
    if entries > engine.input.limits.max_search_entries
        || (possible as u64).saturating_mul(2 + possible.max(1).ilog2() as u64) > work.remaining()
    {
        return Ok(None);
    }
    work.search_storage(entries)?;
    let mut pairs = Vec::new();
    for (i, a) in terminals.iter().enumerate() {
        work.charge(i as u64)?;
        for (j, b) in terminals[..i].iter().enumerate() {
            if a.group == b.group || a.at == b.at || a.mask & b.mask == 0 {
                continue;
            }
            let distance = u128::from(a.at[0].abs_diff(b.at[0])).pow(2)
                + u128::from(a.at[1].abs_diff(b.at[1])).pow(2);
            pairs.push((distance, j, i));
        }
    }
    work.charge(pairs.len() as u64 * (1 + pairs.len().max(1).ilog2() as u64))?;
    pairs.sort_unstable();
    let mut cluster = (0..n).collect::<Vec<_>>();
    let mut edges = Vec::new();
    let mut joins = 0;
    let mut spans: Vec<Rail> = Vec::new();
    let stop = expanded.saturating_add(share / 2).min(deadline);
    'pair: for (_, i, j) in pairs {
        work.charge(1)?;
        let (a, b) = (terminals[i], terminals[j]);
        if cluster[a.group] == cluster[b.group] {
            continue;
        }
        for layer in 0..stackup.len() {
            work.charge(1)?;
            if a.mask & b.mask & (1 << layer) == 0 {
                continue;
            }
            let width = engine
                .effective_rules
                .track_width_for(net, engine.layer_name(layer))?;
            let mut shortest = super::three_pad_branch::Length::between(a.at, b.at);
            let mut endpoints = (a.at, b.at);
            for &span in &spans {
                work.charge(1)?;
                if span.layer != layer {
                    continue;
                }
                for (pin, peer, reverse) in [(a, b, false), (b, a, true)] {
                    if cluster[span.group] != cluster[peer.group] {
                        continue;
                    }
                    for point in targets(span, pin.at) {
                        work.charge(1)?;
                        let length = super::three_pad_branch::Length::between(pin.at, point);
                        if length.compare(shortest).is_lt() {
                            shortest = length;
                            endpoints = if reverse {
                                (point, pin.at)
                            } else {
                                (pin.at, point)
                            };
                        }
                    }
                }
            }
            for (head, tail) in [
                Some(endpoints),
                (endpoints != (a.at, b.at)).then_some((a.at, b.at)),
            ]
            .into_iter()
            .flatten()
            {
                for mut points in octilinear_elbow_paths(head, tail) {
                    if points.len() < 2 {
                        continue;
                    }
                    let last = points.len() - 1;
                    if head == a.at {
                        if let Some(pad) = a.pad {
                            points[0] = super::pad_forest::pad_landing(
                                engine, pad, head, points[1], width,
                            )?;
                        }
                    }
                    if tail == b.at {
                        if let Some(pad) = b.pad {
                            points[last] = super::pad_forest::pad_landing(
                                engine,
                                pad,
                                tail,
                                points[last - 1],
                                width,
                            )?;
                        }
                    }
                    if !try_expand(engine, budget, expanded, stop, frontier)? {
                        break 'pair;
                    }
                    if !clear_path(engine, net, layer, &points)? {
                        continue;
                    }
                    let from = cluster[b.group];
                    let to = cluster[a.group];
                    work.charge(n as u64)?;
                    for c in &mut cluster {
                        if *c == from {
                            *c = to;
                        }
                    }
                    work.search_storage(
                        entries + spans.len() + edges.len() + 2 * (points.len() - 1),
                    )?;
                    spans.extend(points.windows(2).map(|s| Rail {
                        group: a.group,
                        layer,
                        a: s[0],
                        b: s[1],
                    }));
                    edges.extend(points.windows(2).map(|s| PathEdge::Track {
                        start: s[0],
                        end: s[1],
                        layer: layer as u8,
                    }));
                    joins += 1;
                    if joins == n - 1 {
                        break 'pair;
                    }
                    continue 'pair;
                }
            }
        }
    }
    drop(spans);
    if joins < n - 1 && *expanded < deadline {
        joins += extend_buried_tree(
            engine,
            net,
            &terminals,
            &mut cluster,
            &mut edges,
            budget,
            expanded,
            deadline,
            frontier,
        )?;
    }
    if joins < 2 || !clears_proposed_drills(engine, net, &edges)? {
        return Ok(None);
    }
    Ok(Some(edges))
}

// Independent branches may approach a drill proposed by an earlier branch.
// Leave a conflicting tree to the ordinary search before it reaches insertion.
fn clears_proposed_drills(engine: &Engine, net: u32, edges: &[PathEdge]) -> Result<bool, Failure> {
    if engine.input.rules.same_net_hole_clearance_nm <= 0 {
        return Ok(true);
    }
    let work = &engine.work;
    let mut holes = Vec::new();
    for edge in edges {
        work.charge(1)?;
        if let PathEdge::Via(via) = edge {
            work.search_storage(edges.len() + holes.len() + 1)?;
            holes.push(*via);
        }
    }
    for edge in edges {
        work.charge(1)?;
        if let PathEdge::Track { start, end, layer } = *edge {
            if !engine.edge_clears_same_net_vias(net, usize::from(layer), start, end, &holes)? {
                return Ok(false);
            }
        }
    }
    Ok(true)
}

#[derive(Clone, Copy)]
struct Rail {
    group: usize,
    a: Point,
    b: Point,
    layer: usize,
}

fn targets(rail: Rail, point: Point) -> impl Iterator<Item = Point> {
    super::pad_forest::interior_projections(rail.a, rail.b, point)
        .chain(std::iter::once(rail.a))
        .chain((rail.a != rail.b).then_some(rail.b))
}

#[allow(clippy::too_many_arguments)]
fn extend_buried_tree(
    engine: &Engine,
    net: u32,
    pins: &[Terminal<'_>],
    cluster: &mut [usize],
    edges: &mut Vec<PathEdge>,
    budget: &mut TrialBudget<'_>,
    expanded: &mut u64,
    stop: u64,
    frontier: Option<&BlockedFrontier>,
) -> Result<usize, Failure> {
    let work = &engine.work;
    let last = engine.layer_count() - 1;
    let retained = 2 * (pins.len() + cluster.len());
    work.search_storage(retained + edges.len())?;
    let mut rails = Vec::new();
    let mut rooted = vec![false; cluster.len()];
    for pin in pins {
        work.charge(engine.layer_count() as u64)?;
        if pin.mask.count_ones() < 2 {
            continue;
        }
        for layer in 1..last {
            if pin.mask & (1 << layer) != 0 {
                work.search_storage(retained + edges.len() + rails.len() + 1)?;
                rails.push(Rail {
                    group: pin.group,
                    layer,
                    a: pin.at,
                    b: pin.at,
                });
                rooted[cluster[pin.group]] = true;
            }
        }
    }
    let mut used = vec![false; pins.len()];
    let mut holes = Vec::new();
    let mut joins = 0;
    loop {
        if *expanded >= stop {
            break;
        }
        let mut next = None;
        for (i, pin) in pins.iter().enumerate() {
            work.charge(1)?;
            if used[i] || rooted[cluster[pin.group]] || pin.mask.count_ones() != 1 {
                continue;
            }
            for (j, rail) in rails.iter().enumerate() {
                work.charge(1)?;
                for q in targets(*rail, pin.at) {
                    work.charge(1)?;
                    let square = u128::from(q[0].abs_diff(pin.at[0])).pow(2)
                        + u128::from(q[1].abs_diff(pin.at[1])).pow(2);
                    let rank = (square, i, q, j);
                    if next.is_none_or(|v| rank < v) {
                        next = Some(rank);
                    }
                }
            }
        }
        let Some((_, i, q, j)) = next else {
            break;
        };
        used[i] = true;
        let pin = pins[i];
        let rail = rails[j];
        let face = pin.mask.trailing_zeros() as usize;
        budget.check_before_expand(engine, *expanded, frontier)?;
        *expanded += 1;
        let mut sites = super::layer_crossing::drill_sites(engine, net, pin.at, face, None)?;
        if sites.is_empty() {
            sites = super::layer_crossing::pad_edge_drills(engine, net, pin.at, face, &mut || {
                try_expand(engine, budget, expanded, stop, frontier)
            })?;
        }
        let candidate_sites = sites.len();
        work.search_storage(retained + rails.len() + holes.len() + edges.len() + candidate_sites)?;
        work.charge(sites.len() as u64 * (1 + sites.len().max(1).ilog2() as u64))?;
        sites.sort_unstable_by_key(|v| v.at[0].abs_diff(q[0]) + v.at[1].abs_diff(q[1]));
        'site: for via in sites {
            if engine
                .legal_via(net, via.at, 0, last, super::ViaKind::Through, &holes)?
                .is_none()
            {
                continue;
            }
            // A plated anchor can receive copper on several faces. A blocked
            // inner segment on the first face cannot close the others.
            let mut faces = 1u32 << rail.layer;
            for other in &rails {
                work.charge(1)?;
                if other.a == q && other.b == q && cluster[other.group] == cluster[rail.group] {
                    faces |= 1u32 << other.layer;
                }
            }
            for layer in 1..last {
                work.charge(1)?;
                if faces & (1 << layer) == 0 {
                    continue;
                }
                for points in octilinear_elbow_paths(via.at, q) {
                    if !try_expand(engine, budget, expanded, stop, frontier)? {
                        break 'site;
                    }
                    if !clear_path(engine, net, layer, &points)? {
                        continue;
                    }
                    work.search_storage(
                        retained
                            + rails.len()
                            + holes.len()
                            + edges.len()
                            + candidate_sites
                            + 2 * (points.len() - 1)
                            + 2
                            + usize::from(pin.at != via.at),
                    )?;
                    if pin.at != via.at {
                        edges.push(PathEdge::Track {
                            start: pin.at,
                            end: via.at,
                            layer: face as u8,
                        });
                    }
                    edges.push(PathEdge::Via(via));
                    holes.push(via);
                    for ends in points.windows(2) {
                        edges.push(PathEdge::Track {
                            start: ends[0],
                            end: ends[1],
                            layer: layer as u8,
                        });
                        rails.push(Rail {
                            group: rail.group,
                            layer,
                            a: ends[0],
                            b: ends[1],
                        });
                    }
                    let old = cluster[pin.group];
                    let new = cluster[rail.group];
                    work.charge(cluster.len() as u64)?;
                    for c in cluster.iter_mut() {
                        if *c == old {
                            *c = new;
                        }
                    }
                    rooted[new] = true;
                    joins += 1;
                    break 'site;
                }
            }
        }
    }
    Ok(joins)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{DrillShape, Input};

    fn stitched_layout() -> Input {
        let mut input: Input = serde_json::from_str(include_str!(
            "../../tests/fixtures/multiterminal-native-two-nets.json"
        ))
        .unwrap();
        input.layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
            .map(str::to_owned)
            .to_vec();
        let template = input.pads[0].clone();
        input.pads = [(5, 8), (9, 8), (14, 8), (22, 6), (23, 12)]
            .into_iter()
            .enumerate()
            .map(|(i, (x, y))| {
                let mut p = template.clone();
                p.id = format!("93531385-fa29-4877-8e98-{i:012}");
                p.net = 1;
                p.at = [x * 1_000_000, y * 1_000_000];
                p.layers = vec![input.layers[0].clone()];
                if i < 3 {
                    p.layers = input.layers.clone();
                    p.drill = Some(DrillShape::Circle {
                        diameter_nm: 300_000,
                    });
                }
                p
            })
            .collect();
        input.tracks.truncate(1);
        input.tracks[0].net = 1;
        input.tracks[0].start = input.pads[0].at;
        input.tracks[0].end = input.pads[1].at;
        input
    }

    fn enable_through_vias(input: &mut Input) {
        input.board_design_settings = Some(crate::rules::BoardDesignSettings {
            default_track_width_nm: 250_000,
            default_clearance_nm: 1_000_000,
            default_via_diameter_nm: 600_000,
            default_via_drill_nm: 300_000,
            min_through_hole_diameter_nm: 300_000,
            min_via_annular_width_nm: 100_000,
            min_via_diameter_nm: 500_000,
            ..Default::default()
        });
    }

    // A 20um-wide through-drill window lies between pitch samples.
    fn open_pad_edge_window(input: &mut Input) {
        enable_through_vias(input);
        input.pads[3].shape = crate::model::Shape::Rect {
            half_size_nm: [380_000, 220_000],
        };
        for (i, (x, y, r, t)) in [
            (0, 0, 22_379_999, 20_000_000),
            (23_000_000, 0, 40_000_000, 20_000_000),
            (22_379_999, 0, 23_000_000, 5_650_000),
            (22_379_999, 6_350_000, 23_000_000, 20_000_000),
        ]
        .into_iter()
        .enumerate()
        {
            input.rule_areas.push(
                serde_json::from_value(serde_json::json!({
                    "id":format!("aa7a03ee-4324-406c-9761-{i:012}"),"layers":input.layers,
                    "outer":[[x,y],[r,y],[r,t],[x,t]],"no_tracks":false,"no_vias":true
                }))
                .unwrap(),
            );
        }
    }

    #[test]
    fn wired_branches_share_existing_holes_across_separate_layer_keepouts() {
        for inner_closed in [false, true] {
            let mut input = stitched_layout();
            enable_through_vias(&mut input);
            let mut extra = input.pads[2].clone();
            extra.id = "148a082a-af02-404d-92ef-9a328a4d6417".into();
            extra.at = [19_000_000, 8_000_000];
            input.pads.push(extra);
            let mut side = input.pads[3].clone();
            side.at = [14_000_000, 5_000_000];
            side.id = "11ab32a1-c9d2-45ef-aad7-8b813b068c55".into();
            let mut trace = input.tracks[0].clone();
            trace.id = "954d85a2-71b0-4661-b922-8af8f97968ac".into();
            trace.start = side.at;
            trace.end = input.pads[2].at;
            input.tracks.push(trace);
            input.pads.push(side);
            let mut surface_trace = input.tracks[0].clone();
            surface_trace.id = "f9d6f345-2bc7-4b8a-bec4-48eb496638bf".into();
            surface_trace.start = input.pads[3].at;
            surface_trace.end = input.pads[4].at;
            input.tracks.push(surface_trace);
            // Split the two existing connector traces into the short
            // straight segments produced by a prior routing pass.
            for track in input.tracks.drain(..2).collect::<Vec<_>>() {
                let count = 48i64;
                let at = |i: i64| {
                    [0, 1].map(|axis| {
                        track.start[axis] + i * (track.end[axis] - track.start[axis]) / count
                    })
                };
                for i in 0..count {
                    let mut part = track.clone();
                    part.id = format!("a76863c3-85b0-47a4-a1ae-{:012}", input.tracks.len());
                    part.start = at(i);
                    part.end = at(i + 1);
                    input.tracks.push(part);
                }
            }
            for (index, (layers, low_x, high_x, low_y, high_y)) in [
                (vec!["F.Cu", "In1.Cu", "B.Cu"], 10, 11, -1, 22),
                (vec!["F.Cu", "In1.Cu", "B.Cu"], 16, 17, -1, 22),
                (
                    vec!["In2.Cu"],
                    16,
                    17,
                    if inner_closed { -1 } else { 6 },
                    if inner_closed { 22 } else { 10 },
                ),
            ]
            .into_iter()
            .enumerate()
            {
                let (x, r, y, t) = (
                    low_x * 1_000_000,
                    high_x * 1_000_000,
                    low_y * 1_000_000,
                    high_y * 1_000_000,
                );
                input.rule_areas.push(
                    serde_json::from_value(serde_json::json!({
                        "id":format!("5f530e96-bda9-4f89-a482-{index:012}"),
                        "layers":layers,"outer":[[x,y],[r,y],[r,t],[x,t]],
                        "no_tracks":true,"no_vias":false
                    }))
                    .unwrap(),
                );
            }
            let mut engine = Engine::new(input).unwrap();
            engine.surface_search_share = Some(300);
            let groups = engine.components(1).unwrap();
            assert_eq!(groups.len(), 4);
            let mut expanded = 0;
            let edges = between(
                &engine,
                1,
                &groups,
                &mut TrialBudget::fixed(300),
                &mut expanded,
                None,
            )
            .unwrap();
            assert!(expanded > 0 && expanded <= 300);
            if inner_closed {
                assert!(edges.is_none());
            } else {
                let edges = edges.unwrap();
                assert!(edges
                    .iter()
                    .all(|e| matches!(e, PathEdge::Track { layer: 2, .. })));
                assert!(edges
                    .iter()
                    .any(|e| matches!(e,PathEdge::Track{start,end,..} if start[1] != end[1])));
                engine.insert_edges(1, edges).unwrap();
                assert_eq!(engine.components(1).unwrap().len(), 2);
            }
        }
    }

    #[test]
    fn a_surface_branch_can_reach_the_back_inner_face_of_a_plated_anchor() {
        for block_both_faces in [false, true] {
            let mut input = stitched_layout();
            enable_through_vias(&mut input);
            let mut masks = vec!["F.Cu", "In1.Cu"];
            if block_both_faces {
                masks.push("In2.Cu");
            }
            input.rule_areas.push(
                serde_json::from_value(serde_json::json!({
                    "id":"932a7c4c-ed36-4f84-b0d5-c5cb75dd7346",
                    "layers":masks,"outer":[[17_000_000,0],[18_000_000,0],
                                            [18_000_000,20_000_000],[17_000_000,20_000_000]],
                    "no_tracks":true,"no_vias":false
                }))
                .unwrap(),
            );
            let mut engine = Engine::new(input).unwrap();
            let anchors = [
                Terminal {
                    pad: None,
                    group: 0,
                    at: engine.input.pads[2].at,
                    mask: 15,
                },
                Terminal {
                    pad: Some(&engine.input.pads[3]),
                    group: 1,
                    at: engine.input.pads[3].at,
                    mask: 1,
                },
            ];
            let mut cluster = [0, 1];
            let mut edges = Vec::new();
            let mut expanded = 0;
            let count = extend_buried_tree(
                &engine,
                1,
                &anchors,
                &mut cluster,
                &mut edges,
                &mut TrialBudget::fixed(150),
                &mut expanded,
                150,
                None,
            )
            .unwrap();
            assert!(expanded > 0 && expanded <= 150);
            if block_both_faces {
                assert_eq!(count, 0);
                assert!(edges.is_empty());
            } else {
                assert_eq!(count, 1);
                assert!(edges
                    .iter()
                    .any(|e| matches!(e, PathEdge::Track { layer: 2, .. })));
                assert!(edges.iter().any(|e| matches!(e, PathEdge::Via(_))));
                let prior = engine.components(1).unwrap().len();
                engine.insert_edges(1, edges).unwrap();
                assert_eq!(engine.components(1).unwrap().len(), prior - 1);
            }
        }
    }

    #[test]
    fn only_a_stitched_root_with_open_plated_and_surface_components_branches() {
        for (disconnect, collapse, single_surface, share, expected) in [
            (false, false, false, 150, true),
            (true, false, false, 150, false),
            (false, true, false, 150, false),
            (false, false, true, 150, false),
            (false, false, false, 1, false),
        ] {
            let mut input = stitched_layout();
            open_pad_edge_window(&mut input);
            if disconnect {
                input.tracks[0].end[0] -= 2_000_000;
            }
            if collapse {
                input.pads[1].at = input.pads[0].at;
            }
            if single_surface {
                input.pads.pop();
            }
            let mut engine = Engine::new(input).unwrap();
            engine.surface_search_share = Some(share);
            let groups = engine.components(1).unwrap();
            let mut expanded = 0;
            let proposal = between(
                &engine,
                1,
                &groups,
                &mut TrialBudget::fixed(150),
                &mut expanded,
                None,
            )
            .unwrap();
            assert_eq!(proposal.is_some(), expected);
            assert!(expanded <= share);
            if let Some(edges) = proposal {
                engine.insert_edges(1, edges).unwrap();
                assert_eq!(engine.components(1).unwrap().len(), 1);
            }
        }
    }

    #[test]
    fn both_a_closed_pitch_launch_and_an_open_off_pitch_drill_are_required() {
        for state in ["narrow", "ordinary", "sealed", "capped"] {
            let mut input = stitched_layout();
            open_pad_edge_window(&mut input);
            match state {
                "ordinary" => input.rule_areas.clear(),
                "sealed" => {
                    // Close the last window by translating its right wall.
                    for p in &mut input.rule_areas[1].outer {
                        if p[0] == 23_000_000 {
                            p[0] = 22_600_000;
                        }
                    }
                }
                "capped" => {
                    let b = input.board_design_settings.as_mut().unwrap();
                    b.fill_vias = true;
                    b.cap_vias = true;
                }
                _ => {}
            }
            let engine = Engine::new(input).unwrap();
            let pad = &engine.input.pads[3];
            let terminals = [Terminal {
                pad: Some(pad),
                at: pad.at,
                group: 0,
                mask: 1,
            }];
            let groups = vec![vec![Copper::Pad(pad)]];
            let mut used = 0;
            let selected = constrained_pad_access(
                &engine,
                1,
                &terminals,
                &groups,
                &mut TrialBudget::fixed(20),
                &mut used,
                20,
                None,
            )
            .unwrap();
            assert_eq!(selected, state == "narrow", "{state}");
            assert!(used <= 4);
        }
    }
    #[test]
    fn a_branch_must_clear_every_new_drill_or_connect_to_its_annulus() {
        let mut input = stitched_layout();
        enable_through_vias(&mut input);
        input.rules.same_net_hole_clearance_nm = 800_000;
        let engine = Engine::new(input).unwrap();
        let via = super::super::PlacedViaKey {
            at: [20_000_000, 10_000_000],
            start_layer: 0,
            end_layer: 3,
            kind: 0,
        };
        for (offset, expected) in [(450_000, false), (1_200_000, true), (0, true)] {
            let edges = [
                PathEdge::Track {
                    start: [18_000_000, 10_000_000 + offset],
                    end: [22_000_000, 10_000_000 + offset],
                    layer: 1,
                },
                PathEdge::Via(via),
            ];
            assert_eq!(
                clears_proposed_drills(&engine, 1, &edges).unwrap(),
                expected
            );
        }
    }
}
