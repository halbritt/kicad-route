//! Once competing routing units are waiting, try to connect this join with
//! short, fully checked pad escapes before expanding more unit grid edges.
use super::{
    layer_crossing, unpenalized_terminal, Engine, Failure, Path, PathEdge, PlacedViaKey, Point,
    TargetBounds, ViaKind,
};
mod layer_corridor;
pub(super) use layer_corridor::through_launch_corridors;

use crate::diff_pair::octilinear_elbow_paths;
use crate::negotiate::{BlockedFrontier, CongestionHistory, TrialBudget};

#[derive(Clone, Copy)]
struct Terminal {
    at: Point,
    layer: usize,
}

fn track(a: Point, b: Point, layer: usize) -> PathEdge {
    PathEdge::Track {
        start: a,
        end: b,
        layer: layer as u8,
    }
}

struct Join<'a> {
    engine: &'a Engine,
    net: u32,
    source: Terminal,
    target: Terminal,
    via_step: u64,
    history: Option<&'a CongestionHistory>,
}

impl Join<'_> {
    /// Sweeps on the two terminal faces are already checked by terminal_via.
    /// Every intermediate segment and the second drill are checked in context.
    fn connect(
        &self,
        layer: usize,
        first: Option<PlacedViaKey>,
        second: Option<PlacedViaKey>,
        best: &mut Option<Path>,
        expand: &mut impl FnMut() -> Result<(), Failure>,
    ) -> Result<(), Failure> {
        let engine = self.engine;
        let a = first.map_or(self.source.at, |v| v.at);
        let b = second.map_or(self.target.at, |v| v.at);
        let grid = engine.input.rules.grid_nm as u64;
        let distance = |a: Point, b: Point| {
            (0..2)
                .map(|k| a[k].abs_diff(b[k]).div_ceil(grid))
                .sum::<u64>()
        };
        let stubs = distance(a, self.source.at)
            .saturating_mul(engine.layer_policy.weight(self.source.layer))
            .saturating_add(
                distance(b, self.target.at)
                    .saturating_mul(engine.layer_policy.weight(self.target.layer)),
            );
        let via_cost = self
            .via_step
            .saturating_mul(u64::from(first.is_some()) + u64::from(second.is_some()));
        let lower = stubs
            .saturating_add(via_cost)
            .saturating_add(distance(a, b).saturating_mul(engine.layer_policy.weight(layer)));
        engine.work.charge(1)?;
        if a == b || best.as_ref().is_some_and(|p| p.steps <= lower) {
            return Ok(());
        }
        if let (Some(prev), Some(next)) = (first, second) {
            if engine
                .legal_via(
                    self.net,
                    next.at,
                    0,
                    engine.layer_count() - 1,
                    ViaKind::Through,
                    &[prev],
                )?
                .is_none()
            {
                return Ok(());
            }
        }
        let mut options = octilinear_elbow_paths(a, b);
        if a[0] != b[0] && a[1] != b[1] {
            options.extend([vec![a, [a[0], b[1]], b], vec![a, [b[0], a[1]], b]]);
            if a[0].abs_diff(b[0]) != a[1].abs_diff(b[1]) {
                options.push(vec![a, b]);
            }
        }
        'path: for bends in options {
            expand()?;
            let mut edges = Vec::with_capacity(6);
            let mut points = vec![self.source.at];
            let mut steps = stubs.saturating_add(via_cost);
            if let Some(v) = first {
                edges.push(track(self.source.at, v.at, self.source.layer));
                edges.push(PathEdge::Via(v));
                points.extend([v.at, v.at]);
            }
            for w in bends.windows(2) {
                engine.work.charge(1)?;
                if !engine.legal_edge_on(self.net, layer, w[0], w[1])? {
                    continue 'path;
                }
                steps = steps.saturating_add(
                    distance(w[0], w[1]).saturating_mul(engine.layer_policy.weight(layer)),
                );
                edges.push(track(w[0], w[1], layer));
                points.push(w[1]);
            }
            if let Some(v) = second {
                edges.push(PathEdge::Via(v));
                edges.push(track(v.at, self.target.at, self.target.layer));
                points.extend([v.at, self.target.at]);
            }
            if best.as_ref().is_none_or(|p| p.steps > steps)
                && unpenalized_terminal(engine, self.history, &edges)?
            {
                *best = Some(Path {
                    points,
                    edges,
                    steps,
                });
            }
        }
        Ok(())
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) fn from_launch(
    engine: &Engine,
    net: u32,
    starts: &[(Point, u8, bool)],
    targets: &[TargetBounds<'_>],
    track_ok: &[bool],
    frontier_entries: usize,
    via_cost: u64,
    history: Option<&CongestionHistory>,
    budget: &mut TrialBudget<'_>,
    expanded: &mut u64,
    frontier: Option<&BlockedFrontier>,
) -> Result<Option<Path>, Failure> {
    let mut ends = Vec::new();
    for target in targets {
        if let Some((at, mask)) = target.plated_contact(&engine.work)? {
            // A plated barrel can receive an inner-layer trunk directly.
            // Surface landings already participate in the ordinary frontier.
            for layer in 1..track_ok.len().saturating_sub(1) {
                let allowed = track_ok[layer];
                engine.work.charge(1)?;
                if allowed
                    && mask & (1 << layer) != 0
                    && engine.legal_edge_on(net, layer, at, at)?
                {
                    engine
                        .work
                        .search_storage(frontier_entries + ends.len() + 1)?;
                    ends.push((at, layer, true));
                }
            }
        } else if let Some((at, layer)) = target.surface_contact(track_ok, &engine.work)? {
            engine
                .work
                .search_storage(frontier_entries + ends.len() + 1)?;
            ends.push((at, layer, false));
        }
    }
    let frontier_entries = frontier_entries + ends.len();
    from_contacts(
        engine,
        net,
        starts,
        ends,
        track_ok,
        frontier_entries,
        via_cost,
        history,
        budget,
        expanded,
        frontier,
    )
}

#[allow(clippy::too_many_arguments)]
fn from_contacts(
    engine: &Engine,
    net: u32,
    starts: &[(Point, u8, bool)],
    ends: impl IntoIterator<Item = (Point, usize, bool)>,
    track_ok: &[bool],
    frontier_entries: usize,
    via_cost: u64,
    history: Option<&CongestionHistory>,
    budget: &mut TrialBudget<'_>,
    expanded: &mut u64,
    frontier: Option<&BlockedFrontier>,
) -> Result<Option<Path>, Failure> {
    let mut choices = Vec::new();
    let via_step = engine.layer_policy.scale(via_cost);
    for (end, dest, plated) in ends {
        for &(at, face, via_only) in starts {
            engine.work.charge(1)?;
            if via_only || !track_ok[face as usize] {
                continue;
            }
            let nm = at[0].abs_diff(end[0]) + at[1].abs_diff(end[1]);
            let lower = nm
                .div_ceil(engine.input.rules.grid_nm as u64)
                .saturating_mul(engine.layer_policy.minimum_step(net))
                .saturating_add(if usize::from(face) == dest {
                    0
                } else {
                    via_step
                });
            engine
                .work
                .search_storage(frontier_entries + choices.len() + 1)?;
            choices.push((lower, at, face, end, dest, plated));
        }
    }
    engine
        .work
        .charge(choices.len() as u64 * choices.len().max(1).next_power_of_two().ilog2() as u64)?;
    choices.sort_unstable();
    // All supplemental via probes for these contact pairs share one allowance.
    // Sparse probes and the ordinary search remain available when it is spent.
    let remaining = budget.current_limit.saturating_sub(*expanded);
    let mut extra_sites = engine
        .surface_search_share
        .unwrap_or(remaining / engine.input.nets.len().max(1) as u64)
        .min(remaining);
    let mut expand = || {
        budget.check_before_expand(engine, *expanded, frontier)?;
        *expanded += 1;
        Ok(())
    };
    let active_choices = frontier_entries + choices.len();
    let mut sites_by_contact =
        std::collections::BTreeMap::<(Point, usize), Vec<PlacedViaKey>>::new();
    let mut cached_entries = 0usize;
    let mut best: Option<Path> = None;
    for (lower, at, face, end, dest, plated) in choices {
        if best.as_ref().is_some_and(|p| p.steps <= lower) {
            break;
        }
        expand()?;
        let source = Terminal {
            at,
            layer: face as usize,
        };
        let target = Terminal {
            at: end,
            layer: dest,
        };
        let join = Join {
            engine,
            net,
            source,
            target,
            via_step,
            history,
        };
        if source.layer == dest {
            join.connect(dest, None, None, &mut best, &mut expand)?;
        }
        if best.as_ref().is_some_and(|p| p.steps <= lower) {
            continue;
        }
        let mut sample = || {
            if extra_sites == 0 || best.is_some() {
                return Ok(false);
            }
            expand()?;
            extra_sites -= 1;
            Ok(true)
        };
        let mut sites = |t: Terminal| -> Result<Vec<PlacedViaKey>, Failure> {
            let key = (t.at, t.layer);
            engine
                .work
                .charge(1 + sites_by_contact.len().max(1).ilog2() as u64)?;
            if let Some(old) = sites_by_contact.get(&key) {
                engine.work.charge(old.len() as u64)?;
                return Ok(old.clone());
            }
            let found = layer_crossing::drill_sites(engine, net, t.at, t.layer, Some(&mut sample))?;
            let needed = 1 + found.len();
            // Caching a contact is optional when the ordinary frontier owns
            // the remaining search storage.
            if active_choices + cached_entries + needed <= engine.input.limits.max_search_entries {
                engine.work.charge(needed as u64)?;
                cached_entries += needed;
                sites_by_contact.insert(key, found.clone());
            }
            Ok(found)
        };
        let left = sites(source)?;
        // Each allowed plated face is already a destination. The existing
        // barrel needs neither a new drill nor a surface stub at this end.
        let right = if plated { Vec::new() } else { sites(target)? };
        if source.layer != dest {
            for &v in &left {
                join.connect(dest, Some(v), None, &mut best, &mut expand)?;
            }
            for &v in &right {
                join.connect(source.layer, None, Some(v), &mut best, &mut expand)?;
            }
        }
        for (layer, &allowed) in track_ok.iter().enumerate() {
            if !allowed || layer == source.layer || layer == dest {
                continue;
            }
            for &v in &left {
                for &w in &right {
                    join.connect(layer, Some(v), Some(w), &mut best, &mut expand)?;
                }
            }
        }
    }
    Ok(best)
}

/// Surface tracks can leave a land-grid pad diagonally and continue between
/// its neighboring rows until drilling is allowed beyond the package.
#[allow(clippy::too_many_arguments)]
pub(super) fn through_pad_channels(
    engine: &Engine,
    net: u32,
    source: &[super::Copper<'_>],
    targets: &[Vec<super::Copper<'_>>],
    history: Option<&CongestionHistory>,
    budget: &mut TrialBudget<'_>,
    expanded: &mut u64,
    frontier: Option<&BlockedFrontier>,
) -> Result<Option<Path>, Failure> {
    let ([super::Copper::Pad(a)], [group]) = (source, targets) else {
        return Ok(None);
    };
    let [super::Copper::Pad(b)] = group.as_slice() else {
        return Ok(None);
    };
    if engine.layer_count() < 2
        || a.drill.is_some()
        || b.drill.is_some()
        || engine.input.rules.same_net_hole_clearance_nm != 0
        || engine.effective_rules.net(net).max_via_count.is_some()
        || engine.effective_rules.allow_blind_buried_vias
        || engine.effective_rules.allow_microvias
        || engine
            .input
            .routing_options
            .as_ref()
            .is_some_and(|o| o.bus.is_some())
    {
        return Ok(None);
    }
    let allowed = super::allowed_track_layers(engine, net)?;
    let contact = |pad| {
        let target = TargetBounds::new(
            super::Copper::Pad(pad),
            engine.all_layers_slice(),
            &engine.work,
        )?;
        target.surface_contact(&allowed, &engine.work)
    };
    let (Some((a, face)), Some((b, dest))) = (contact(a)?, contact(b)?) else {
        return Ok(None);
    };
    let mut exits = super::pad_field::row_channels(engine, [(a, face), (b, dest)])?;
    for (points, (center, layer)) in exits.iter_mut().zip([(a, face), (b, dest)]) {
        let mut allowed = Vec::new();
        for p in points.drain(..) {
            if engine.legal_edge_on(net, layer, center, p)?
                && unpenalized_terminal(engine, history, &[track(center, p, layer)])?
            {
                allowed.push(p);
            }
        }
        *points = allowed;
    }
    let [mut left, mut right] = exits;
    if left.is_empty() && right.is_empty() {
        return Ok(None);
    }
    left.push(a);
    right.push(b);
    let grid = engine.input.rules.grid_nm as u64;
    let cost = |a: Point, b: Point, layer: usize| {
        (a[0].abs_diff(b[0]).div_ceil(grid) + a[1].abs_diff(b[1]).div_ceil(grid))
            .saturating_mul(engine.layer_policy.weight(layer))
    };
    let mut best: Option<Path> = None;
    for &p in &left {
        for &q in &right {
            engine.work.charge(1)?;
            if p == a && q == b {
                continue;
            }
            let stubs = cost(a, p, face).saturating_add(cost(q, b, dest));
            let lower = (p[0].abs_diff(q[0]) + p[1].abs_diff(q[1]))
                .div_ceil(grid)
                .saturating_mul(engine.layer_policy.minimum_step(net))
                .saturating_add(stubs);
            if best.as_ref().is_some_and(|path| path.steps <= lower) {
                continue;
            }
            if let Some(mut path) = from_contacts(
                engine,
                net,
                &[(p, face as u8, false)],
                [(q, dest, false)],
                &allowed,
                left.len() + right.len(),
                engine.effective_rules.via_cost.max(1),
                history,
                budget,
                expanded,
                frontier,
            )? {
                path.steps = path.steps.saturating_add(stubs);
                if best.as_ref().is_some_and(|old| old.steps <= path.steps) {
                    continue;
                }
                if a != p {
                    path.edges.insert(0, track(a, p, face));
                    path.points.insert(0, a);
                }
                if b != q {
                    path.edges.push(track(q, b, dest));
                    path.points.push(b);
                }
                best = Some(path);
            }
        }
    }
    Ok(best)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::components::Copper;
    use crate::model::{DrillShape, Input};

    #[test]
    fn inner_trace_terminates_on_the_actual_plated_stack_without_redrilling() {
        let mut board: Input = serde_json::from_str(include_str!(
            "../../tests/fixtures/multiterminal-native-two-nets.json"
        ))
        .unwrap();
        board.layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
            .map(str::to_owned)
            .to_vec();
        board.tracks.clear();
        board.pads.truncate(1);
        let pad = &mut board.pads[0];
        pad.layers = vec!["F.Cu".into(), "In2.Cu".into(), "B.Cu".into()];
        pad.drill = Some(DrillShape::Circle {
            diameter_nm: 400_000,
        });
        let engine = Engine::new(board).unwrap();
        let target = TargetBounds::new(
            Copper::Pad(&engine.input.pads[0]),
            engine.all_layers_slice(),
            &engine.work,
        )
        .unwrap();
        let contact = engine.input.pads[0].copper_center();
        let start = [15_000_000, 10_000_000];
        for (source, allowed, expected) in [
            (1, [false, true, false, false], None), // the pad omits In1
            (2, [false, false, true, false], Some(0)),
            (0, [true, false, true, false], Some(1)),
        ] {
            let mut used = 0;
            let path = from_launch(
                &engine,
                1,
                &[(start, source, false)],
                std::slice::from_ref(&target),
                &allowed,
                0,
                engine.effective_rules.via_cost.max(1),
                None,
                &mut TrialBudget::fixed(1_000),
                &mut used,
                None,
            )
            .unwrap();
            assert_eq!(path.is_some(), expected.is_some());
            if let (Some(path), Some(vias)) = (path, expected) {
                assert!(used > 0 && used <= 1_000);
                assert_eq!(path.points.first(), Some(&start));
                assert_eq!(path.points.last(), Some(&contact));
                assert_eq!(
                    path.edges
                        .iter()
                        .filter(|e| matches!(e, PathEdge::Via(_)))
                        .count(),
                    vias
                );
                assert!(
                    matches!(path.edges.last(),Some(PathEdge::Track{end,layer:2,..}) if *end==contact)
                );
                for e in path.edges {
                    match e {
                        PathEdge::Track { start, end, layer } => assert!(engine
                            .legal_edge_on(1, usize::from(layer), start, end)
                            .unwrap()),
                        PathEdge::Via(v) => {
                            assert_ne!(v.at, contact);
                            assert!(engine
                                .legal_via(1, v.at, 0, 3, ViaKind::Through, &[])
                                .unwrap()
                                .is_some());
                        }
                    }
                }
            }
        }
    }
}
