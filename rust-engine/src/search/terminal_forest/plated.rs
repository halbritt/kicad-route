//! Join wired branches at existing barrels. A blocked surface land can wait
//! while several other branches gain access to the same buried copper.
use super::{
    clear_path, octilinear_elbow_paths, try_expand, BlockedFrontier, Engine, Failure, PathEdge,
    Terminal, TrialBudget,
};
use std::collections::BTreeMap;

#[allow(clippy::too_many_arguments)]
pub(super) fn between(
    engine: &Engine,
    net: u32,
    terminals: &[Terminal<'_>],
    groups: usize,
    drilled_groups: usize,
    budget: &mut TrialBudget<'_>,
    expanded: &mut u64,
    stop: u64,
    frontier: Option<&BlockedFrontier>,
) -> Result<Option<Vec<PathEdge>>, Failure> {
    if drilled_groups < 3 || stop.saturating_sub(*expanded) < drilled_groups as u64 {
        return Ok(None);
    }
    let work = &engine.work;
    let inherited = groups + terminals.len();
    let mut barrels = Vec::new();
    for &pin in terminals {
        work.charge(1)?;
        if pin.mask.count_ones() > 1 {
            work.search_storage(inherited + barrels.len() + 1)?;
            barrels.push(pin);
        }
    }
    let possible = barrels
        .len()
        .saturating_mul(barrels.len().saturating_sub(1))
        / 2;
    if possible as u64 > work.remaining() {
        return Ok(None);
    }
    // Each disconnected pair of branches owns one turn. Repeated thermal
    // contacts in one package must not consume every turn on the same pair.
    let mut nearest = BTreeMap::new();
    for (j, b) in barrels.iter().enumerate() {
        work.charge(j as u64)?;
        for (i, a) in barrels[..j].iter().enumerate() {
            if a.group == b.group || a.at == b.at || a.mask & b.mask == 0 {
                continue;
            }
            let d = a.at[0].abs_diff(b.at[0]) + a.at[1].abs_diff(b.at[1]);
            let key = (a.group.min(b.group), a.group.max(b.group));
            work.charge(1 + nearest.len().max(1).ilog2() as u64)?;
            work.search_storage(inherited + barrels.len() + nearest.len() + 1)?;
            let entry = nearest.entry(key).or_insert((d, i, j));
            *entry = (*entry).min((d, i, j));
        }
    }
    let n = nearest.len();
    work.search_storage(inherited + barrels.len() + n * 2 + groups)?;
    work.charge(n as u64 * (2 + n.max(1).ilog2() as u64) + groups as u64)?;
    let mut pairs: Vec<_> = nearest.into_values().collect();
    pairs.sort_unstable();
    let mut cluster: Vec<_> = (0..groups).collect();
    let mut edges = Vec::new();
    let mut joins = 0;
    let occupied = inherited + barrels.len() + n + groups;
    let grid = engine.input.rules.grid_nm as u64;
    for (k, &(d, i, j)) in pairs.iter().enumerate() {
        work.charge(1)?;
        let (a, b) = (barrels[i], barrels[j]);
        if cluster[a.group] == cluster[b.group] {
            continue;
        }
        let mask = a.mask & b.mask;
        // A spanning tree needs one more successful connection per isolated
        // branch. Charging every possible pair an equal slot starves the
        // shortest bent connection as more branches are present.
        let remaining_branches = (drilled_groups - joins - 1).min(n - k);
        let pair_stop = (*expanded).saturating_add(
            stop.saturating_sub(*expanded)
                .div_ceil(remaining_branches as u64),
        );
        let mut joined = None;
        // Check cheap elbows on all common faces before spending a turn on
        // a bent corridor on one face.
        'direct: for l in 0..engine.layer_count() {
            work.charge(1)?;
            if mask & (1 << l) == 0 {
                continue;
            }
            // At most two three-point elbow vectors plus their handles and
            // the two retained edges of a successful candidate.
            work.search_storage(occupied + edges.len() + 2 * (3 + 1) + 2)?;
            for p in octilinear_elbow_paths(a.at, b.at) {
                if !try_expand(engine, budget, expanded, pair_stop, frontier)? {
                    break 'direct;
                }
                if clear_path(engine, net, l, &p)? {
                    joined = Some(
                        p.windows(2)
                            .map(|s| PathEdge::Track {
                                start: s[0],
                                end: s[1],
                                layer: l as u8,
                            })
                            .collect::<Vec<_>>(),
                    );
                    break 'direct;
                }
            }
        }
        if joined.is_none() {
            for l in 0..engine.layer_count() {
                work.charge(1)?;
                if mask & (1 << l) == 0 {
                    continue;
                }
                let faces = (mask >> l).count_ones() as u64;
                let end =
                    (*expanded).saturating_add(pair_stop.saturating_sub(*expanded).div_ceil(faces));
                let limit = d
                    .div_ceil(grid)
                    .saturating_mul(engine.layer_policy.weight(l))
                    .saturating_add(
                        engine
                            .layer_policy
                            .scale(engine.effective_rules.via_cost)
                            .saturating_mul(2),
                    );
                if let Some(path) = super::super::axial_intervals::between(
                    engine,
                    net,
                    l as u8,
                    a.at,
                    b.at,
                    &[(a.at, l as u8, false)],
                    &[b.at],
                    limit,
                    occupied + edges.len(),
                    &mut || try_expand(engine, budget, expanded, end, frontier),
                )? {
                    joined = Some(path.edges);
                    break;
                }
            }
        }
        if let Some(path) = joined {
            if path.is_empty() {
                continue;
            }
            work.search_storage(occupied + edges.len() + path.len() * 2)?;
            work.charge((groups + path.len()) as u64)?;
            let old = cluster[b.group];
            let new = cluster[a.group];
            for v in &mut cluster {
                if *v == old {
                    *v = new;
                }
            }
            edges.extend(path);
            joins += 1;
            if joins + 1 == drilled_groups {
                break;
            }
        }
    }
    Ok((joins >= 2 || joins + 1 == drilled_groups).then_some(edges))
}
