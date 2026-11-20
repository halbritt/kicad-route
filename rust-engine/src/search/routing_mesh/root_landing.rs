//! Offer a surface terminal an escape onto the layer of an existing conductor.
use crate::components::Copper;
use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};

use super::{
    link, BlockedFrontier, CongestionHistory, Engine, Failure, Mesh, MeshPricing, Path, PathEdge,
    Point, TargetBounds, TrialBudget,
};

/// All ordinary goal bounds are in island order. A pad in an already wired
/// island has additional ways to attach, so reserve the reverse drill search
/// for islands whose only copper is one native surface land.
pub(in crate::search) fn isolated_targets<'a, 'c>(
    engine: &Engine,
    islands: &[Vec<Copper<'c>>],
    mut bounds: &'a [TargetBounds<'c>],
    allowed: &[bool],
    occupied: usize,
) -> Result<Vec<&'a TargetBounds<'c>>, Failure> {
    let mut bare = Vec::new();
    let mut wired = false;
    for group in islands {
        engine.work.charge(1 + group.len() as u64)?;
        let (members, rest) = bounds.split_at(group.len());
        bounds = rest;
        if let [target] = members {
            if target.surface_contact(allowed, &engine.work)?.is_some() {
                engine.work.search_storage(occupied + bare.len() + 1)?;
                bare.push(target);
                continue;
            }
        }
        wired |= group
            .iter()
            .any(|c| matches!(c, Copper::Track(_) | Copper::Via(_)));
    }
    if !wired {
        bare.clear();
    }
    Ok(bare)
}

#[allow(clippy::too_many_arguments)]
pub(in crate::search) fn connect<'t, 'c: 't>(
    engine: &Engine,
    net: u32,
    source: &[Copper],
    starts: &[(Point, u8, bool)],
    targets: impl IntoIterator<Item = &'t TargetBounds<'c>>,
    allowed: &[bool],
    occupied: usize,
    via_cost: u64,
    history: Option<&CongestionHistory>,
    budget: &mut TrialBudget<'_>,
    expanded: &mut u64,
    frontier: Option<&BlockedFrontier>,
) -> Result<Option<Path>, Failure> {
    let Some(share) = engine.surface_search_share else {
        return Ok(None);
    };
    let mut goals = Vec::new();
    let mut face_mask = 0u32;
    for target in targets {
        engine.work.charge(1)?;
        let Some((p, face)) = target.surface_contact(allowed, &engine.work)? else {
            return Ok(None);
        };
        face_mask |= 1 << face;
        engine.work.search_storage(occupied + goals.len() + 1)?;
        goals.push((p, face));
    }
    if face_mask.count_ones() != 1 {
        return Ok(None);
    }
    let mut plated = BTreeSet::new();
    let mut has_track = false;
    for &item in source {
        engine.work.charge(1)?;
        let center = match item {
            Copper::Pad(p) if p.drill.is_some() => {
                engine
                    .work
                    .charge((p.layers.len() * engine.layer_count()) as u64)?;
                (item.layer_mask(engine.all_layers_slice()).count_ones() > 1)
                    .then(|| p.copper_center())
            }
            Copper::Via(v) => Some(v.at),
            Copper::Track(_) => {
                has_track = true;
                None
            }
            _ => None,
        };
        if let Some(at) = center {
            engine.work.charge(1 + plated.len().max(1).ilog2() as u64)?;
            engine
                .work
                .search_storage(occupied + goals.len() + plated.len() + 1)?;
            plated.insert(at);
        }
    }
    if !has_track || plated.is_empty() {
        return Ok(None);
    }
    let occupied = occupied + goals.len() + plated.len();
    let mut anchors = Vec::new();
    for &(p, l, via_only) in starts {
        engine.work.charge(1)?;
        if !via_only && face_mask & (1u32 << l) == 0 && allowed[usize::from(l)] {
            engine.work.search_storage(occupied + anchors.len() + 1)?;
            anchors.push((p, l));
        }
    }
    if anchors.is_empty() {
        return Ok(None);
    }
    let mesh = Mesh {
        engine,
        net,
        grid: engine.input.rules.grid_nm as u64,
        pitch: engine.input.rules.grid_nm,
        via_step: engine.layer_policy.scale(via_cost),
        history,
        occupied_entries: occupied + anchors.len(),
        pricing: MeshPricing::Forward,
    };
    let allowance = share.min(
        budget.current_limit.saturating_sub(*expanded) / engine.input.nets.len().max(1) as u64,
    );
    let stop = expanded.saturating_add(allowance);
    let spend =
        |expanded: &mut u64, budget: &mut TrialBudget, stop: u64| -> Result<bool, Failure> {
            if *expanded >= stop {
                return Ok(false);
            }
            budget.check_before_expand(engine, *expanded, frontier)?;
            *expanded += 1;
            Ok(true)
        };
    let mut tips = BTreeMap::new();
    let mut blocked = Vec::new();
    // A final surface land may have an axial hole facing a dead end, while a
    // closer bent escape can see a different barrel of the same routed tree.
    // The ordinary terminal search can inspect at least one proposal per
    // origin in a small tree. Keep that search first when its launch set fits.
    let distributed = goals.len() == 1 && plated.len() > 1 && starts.len() as u64 > allowance;
    for &(p, face) in &goals {
        if distributed || !super::super::layer_crossing::has_axial_drill(engine, net, face, p)? {
            mesh.storage(blocked.len() + 1)?;
            blocked.push((p, face));
        }
    }
    // A pad surrounded by same-face tracks can require several turns before a
    // hole fits. Keep at least a share of the trial for approaching each hole.
    if blocked.is_empty() {
        return Ok(None);
    }
    let discovery = allowance.saturating_sub(allowance / (allowed.len() as u64 + 1));
    let per_pad = discovery / blocked.len() as u64;
    let mut tip_entries = 0;
    for &(p, face) in &blocked {
        let substop = stop.min(expanded.saturating_add(per_pad));
        if let Some((via, edges)) =
            mesh.reachable_drill(p, face as u8, blocked.len() + tip_entries, &mut || {
                spend(expanded, budget, substop)
            })?
        {
            engine.work.charge(1 + tips.len().max(1).ilog2() as u64)?;
            let added = edges.len() + 2;
            mesh.storage(blocked.len() + tip_entries + added)?;
            let old = tips.insert(via.at, (via, edges));
            tip_entries += added;
            if let Some((_, prior)) = old {
                tip_entries -= 2 + prior.len();
            }
        }
    }
    if tips.is_empty() {
        return Ok(None);
    }
    let memory = blocked.len() + tip_entries + tips.len();
    mesh.storage(memory)?;
    engine.work.charge(tips.len() as u64)?;
    let holes: Vec<Point> = tips.keys().copied().collect();
    let mut best: Option<Path> = None;
    for (l, &ok) in allowed.iter().enumerate() {
        if !ok || face_mask & (1 << l) != 0 {
            continue;
        }
        let retained = best.as_ref().map_or(0, |p| p.edges.len() + p.points.len());
        let memory = memory + retained;
        let mut launches = Vec::new();
        let mut closest = None;
        let mut far = 0;
        for &via in &holes {
            let mut nearest = None;
            for &(root, layer) in &anchors {
                engine.work.charge(1)?;
                if layer as usize != l {
                    continue;
                }
                let candidate = (mesh.steps(root, via, layer), root, via);
                engine.work.charge(1 + plated.len().ilog2() as u64)?;
                if plated.contains(&root) {
                    mesh.storage(memory + launches.len() + 1)?;
                    launches.push((root, layer, false));
                    far = far.max(candidate.0);
                }
                if nearest.is_none_or(|old| candidate < old) {
                    nearest = Some(candidate);
                }
            }
            if let Some(pair) = nearest {
                if closest.is_none_or(|old| pair < old) {
                    closest = Some(pair);
                }
                far = far.max(pair.0);
                mesh.storage(memory + launches.len() + 1)?;
                launches.push((pair.1, l as u8, false));
            }
        }
        let Some((_, a, b)) = closest else {
            continue;
        };
        engine
            .work
            .charge(launches.len() as u64 * (1 + launches.len().max(1).ilog2() as u64))?;
        launches.sort_unstable();
        launches.dedup();
        engine.work.charge((allowed.len() - l) as u64)?;
        let remaining = allowed[l..]
            .iter()
            .enumerate()
            .filter(|(i, ok)| **ok && face_mask & (1 << (l + i)) == 0)
            .count();
        let layer_stop =
            expanded.saturating_add(stop.saturating_sub(*expanded).div_ceil(remaining as u64));
        let price = far.saturating_add(mesh.via_step.saturating_mul(2));
        if let Some(path) = super::super::axial_intervals::between(
            engine,
            net,
            l as u8,
            a,
            b,
            &launches,
            &holes,
            price,
            mesh.occupied_entries + memory + launches.len(),
            &mut || spend(expanded, budget, layer_stop),
        )? {
            engine.work.charge(1 + tips.len().ilog2() as u64)?;
            let (via, spur) = &tips[path
                .points
                .last()
                .expect("joined line ends at a seeded drill")];
            let mut edges = path.edges;
            engine.work.charge(spur.len() as u64)?;
            // Repricing holds the edge list, its contact points, and the hole.
            mesh.storage(memory + launches.len() + 2 * (edges.len() + spur.len() + 1) + 2)?;
            edges.push(PathEdge::Via(*via));
            edges.extend_from_slice(spur);
            if let Some(proposal) = mesh.validated_edges(edges)? {
                if best.as_ref().is_none_or(|old| proposal.steps < old.steps) {
                    best = Some(proposal);
                }
            }
        }
    }
    Ok(best)
}

impl Mesh<'_> {
    fn reachable_drill(
        &self,
        p: Point,
        face: u8,
        occupied: usize,
        expand: &mut impl FnMut() -> Result<bool, Failure>,
    ) -> Result<Option<(super::PlacedViaKey, Vec<PathEdge>)>, Failure> {
        self.storage(occupied + 2)?;
        let mut parents = HashMap::from([(p, p)]);
        let mut queue = VecDeque::from([p]);
        let grid = self.grid as i64;
        let last = self.engine.layer_count() - 1;
        while let Some(at) = queue.pop_front() {
            if !expand()? {
                break;
            }
            let via = if self
                .engine
                .legal_via(self.net, at, 0, last, super::ViaKind::Through, &[])?
                .is_some()
            {
                Some(super::PlacedViaKey {
                    at,
                    start_layer: 0,
                    end_layer: last as u8,
                    kind: 0,
                })
            } else {
                super::super::layer_crossing::drill_sites(
                    self.engine,
                    self.net,
                    at,
                    usize::from(face),
                    None,
                )?
                .into_iter()
                .min_by_key(|v| at[0].abs_diff(v.at[0]) + at[1].abs_diff(v.at[1]))
            };
            if let Some(via) = via {
                let mut q = at;
                let mut edges = Vec::new();
                if q != via.at {
                    self.storage(occupied + parents.len() + queue.len() + 1)?;
                    edges.push(link(via.at, q, face));
                }
                while q != p {
                    self.engine.work.charge(1)?;
                    self.storage(occupied + parents.len() + queue.len() + edges.len() + 1)?;
                    let prev = parents[&q];
                    edges.push(link(q, prev, face));
                    q = prev;
                }
                return Ok(Some((via, edges)));
            }
            for dx in -1..=1 {
                for dy in -1..=1 {
                    self.engine.work.charge(1)?;
                    let next = [at[0] + dx * grid, at[1] + dy * grid];
                    if parents.contains_key(&next) || !self.clear(at, next, face)? {
                        continue;
                    }
                    self.storage(occupied + parents.len() + queue.len() + 2)?;
                    parents.insert(next, at);
                    queue.push_back(next);
                }
            }
        }
        Ok(None)
    }
}

#[cfg(test)]
mod tests;
