//! Additional search phases for off-grid via escapes in orthogonal surface pad fields.
//! The four adjacent pads suggest a site. The normal via predicate validates the full
//! stack, and the ordinary router must actually reach the site from source copper.
use crate::components::Copper;
use crate::geometry;
use crate::model::{Failure, Point, Status, ViaKind};
use crate::model::{Pad, Shape};
use crate::rules::DruItemKind;
use crate::Engine;
use std::collections::{BTreeMap, BTreeSet};
use std::ops::Bound::{Excluded, Included};

fn symmetric_surface(engine: &Engine, pad: &Pad) -> Option<usize> {
    if pad.drill.is_some()
        || !matches!(
            pad.shape,
            Shape::Rect { .. }
                | Shape::Circle { .. }
                | Shape::RoundRect { .. }
                | Shape::BoxExact { .. }
        )
    {
        return None;
    }
    let layer = match pad.layers.as_slice() {
        [] => &engine.input.layer,
        [layer] => layer,
        _ => return None,
    };
    engine
        .layer_index(layer)
        .filter(|&i| i == 0 || i + 1 == engine.layer_count())
}

type Row<'a> = BTreeMap<(usize, i64, i64), Option<&'a Pad>>;

/// Nearest collinear neighbors on each side. The key's first two coordinates keep
/// both range queries on the pad's physical copper layer and row.
fn neighbors<'a>(row: &Row<'a>, layer: usize, a: Point, axis: usize) -> [Option<&'a Pad>; 2] {
    let key = |value| (layer, a[1 - axis], value);
    [
        row.range(key(i64::MIN)..key(a[axis]))
            .next_back()
            .and_then(|(_, p)| *p),
        row.range((Excluded(key(a[axis])), Included(key(i64::MAX))))
            .next()
            .and_then(|(_, p)| *p),
    ]
}

/// First allowable integer coordinate while moving from the pad's centerline towards
/// the other row. For all admitted shapes here, each such cross-section is an interval.
#[allow(clippy::too_many_arguments)]
fn edge_of_clearance(
    engine: &Engine,
    net: u32,
    pad: &Pad,
    fixed_axis: usize,
    midline: i64,
    near: i64,
    far: i64,
    layer: usize,
    via: (i64, i64),
) -> Result<Option<i64>, Failure> {
    let layers = engine.all_layers_slice();
    let same_net = pad.net == net;
    if same_net
        && engine
            .input
            .board_design_settings
            .as_ref()
            .is_none_or(|board| board.fill_vias && board.cap_vias)
    {
        return Ok(Some(near));
    }
    // Read the same netclass, imported pad override, hole and mask clearances
    // used by the through-via predicate. This interval only proposes a site.
    let (diameter, drill) = via;
    let exclusion_diameter = if same_net {
        diameter.max(drill + 2 * engine.input.rules.same_net_hole_clearance_nm)
    } else {
        engine
            .work
            .charge(2 * engine.effective_rules.dru_rules.len() as u64)?;
        let pad_layers = std::slice::from_ref(&layers[layer]);
        let rules = engine.input.rules;
        let copper_gap = engine
            .effective_rules
            .pair_clearance_on_layer(
                (net, DruItemKind::Via, layers),
                (pad.net, DruItemKind::Pad, pad_layers),
                &layers[layer],
                pad.local_clearance_nm,
            )
            .max(
                rules.solder_mask_expansion_nm.max(0)
                    + rules.solder_mask_to_copper_clearance_nm.max(0),
            );
        let hole_gap = engine.effective_rules.pair_hole_clearance(
            net,
            DruItemKind::Via,
            layers,
            pad.net,
            DruItemKind::Pad,
            pad_layers,
        );
        (diameter + 2 * copper_gap).max(drill + 2 * hole_gap)
    };
    let blocked = |v: i64| -> Result<bool, Failure> {
        engine.work.charge(1)?;
        let mut at = [v, v];
        at[fixed_axis] = midline;
        Ok(geometry::track_near_pad(
            at,
            at,
            exclusion_diameter,
            0,
            pad,
            false,
        ))
    };
    if !blocked(near)? {
        return Ok(Some(near));
    }
    if blocked(far)? {
        return Ok(None);
    }
    let (mut closed, mut open) = (near, far);
    while open.abs_diff(closed) > 1 {
        let mid = closed + (open - closed) / 2;
        if blocked(mid)? {
            closed = mid;
        } else {
            open = mid;
        }
    }
    Ok(Some(open))
}

fn cell_phase(
    engine: &Engine,
    net: u32,
    pads: [&Pad; 4],
    layer: usize,
    axis: usize,
    via: (i64, i64),
) -> Result<Option<Point>, Failure> {
    let a = pads[0].copper_center();
    let b = pads[1].copper_center();
    let z = pads[2].copper_center();
    let cross = 1 - axis;
    let midline = a[axis] + (b[axis] - a[axis]) / 2;
    let (low, high) = (a[cross].min(z[cross]), a[cross].max(z[cross]));
    let mut bounds = (low, high);
    for pad in pads {
        let near = pad.copper_center()[cross];
        let far = if near == low { high } else { low };
        let Some(edge) = edge_of_clearance(engine, net, pad, axis, midline, near, far, layer, via)?
        else {
            return Ok(None);
        };
        if near == low {
            bounds.0 = bounds.0.max(edge);
        } else {
            bounds.1 = bounds.1.min(edge);
        }
    }
    if bounds.0 > bounds.1 {
        return Ok(None);
    }
    let mut site = [midline; 2];
    site[cross] = bounds.0 + (bounds.1 - bounds.0) / 2;
    let end = engine.layer_count() - 1;
    Ok(engine
        .legal_via(net, site, 0, end, ViaKind::Through, &[])?
        .map(|_| site))
}

pub(super) fn starts(
    engine: &Engine,
    net: u32,
    source: &[Copper<'_>],
    targets: &[Vec<Copper<'_>>],
) -> Result<Vec<(Point, u8, bool)>, Failure> {
    if engine.layer_count() < 2 || engine.input.pads.len() < 4 {
        return Ok(Vec::new());
    }
    let layers = engine.all_layers_slice();
    if !engine.layer_policy.via_allowed(net, 0, layers.len() - 1)
        || engine.effective_rules.disallow_via_on(net, layers)
        || engine
            .effective_rules
            .net(net)
            .max_via_count
            .is_some_and(|n| engine.copper.via_count_for_net(net) >= n as usize)
    {
        return Ok(Vec::new());
    }
    let via = match engine.effective_rules.via_dims_for(net, false, layers) {
        Ok(dims) => dims,
        Err(e) if e.status == Status::Unroutable => return Ok(Vec::new()),
        Err(e) => return Err(e),
    };
    let mut terminals = Vec::new();
    let mut collect_pads = |group: &[Copper<'_>]| -> Result<bool, Failure> {
        let before = terminals.len();
        for c in group {
            engine.work.charge(1)?;
            if let Copper::Pad(pad) = c {
                if let Some(layer) = symmetric_surface(engine, pad) {
                    engine.work.search_storage(terminals.len() + 1)?;
                    terminals.push((layer, pad.copper_center()));
                }
            }
        }
        Ok(terminals.len() > before)
    };
    if !collect_pads(source)? {
        return Ok(Vec::new());
    }
    for target in targets {
        collect_pads(target)?;
    }
    let mut rows: [Row<'_>; 2] = Default::default();
    for pad in &engine.input.pads {
        engine.work.charge(1)?;
        let Some(layer) = symmetric_surface(engine, pad) else {
            continue;
        };
        let p = pad.copper_center();
        engine
            .work
            .search_storage(terminals.len() + rows[0].len() + rows[1].len() + 2)?;
        for i in 0..2 {
            rows[i]
                .entry((layer, p[1 - i], p[i]))
                .and_modify(|entry| *entry = None)
                .or_insert(Some(pad));
        }
    }
    let grid = engine.input.rules.grid_nm;
    let mut phases = BTreeSet::new();
    let base_storage = terminals.len() + rows[0].len() + rows[1].len();
    for (layer, center) in terminals {
        engine.work.charge(1)?;
        let Some(&Some(pad)) = rows[0].get(&(layer, center[1], center[0])) else {
            continue;
        };
        let [xs, ys] = std::array::from_fn(|axis| neighbors(&rows[axis], layer, center, axis));
        for x in xs.into_iter().flatten() {
            for y in ys.into_iter().flatten() {
                engine.work.charge(1)?;
                let key = (layer, y.copper_center()[1], x.copper_center()[0]);
                let Some(&Some(opposite)) = rows[0].get(&key) else {
                    continue;
                };
                for (axis, cell) in [(0, [pad, x, y, opposite]), (1, [pad, y, x, opposite])] {
                    if let Some(site) = cell_phase(engine, net, cell, layer, axis, via)? {
                        engine
                            .work
                            .search_storage(base_storage + phases.len() + 1)?;
                        phases.insert(site.map(|v| v.rem_euclid(grid)));
                    }
                }
            }
        }
    }
    drop(rows);
    let mut starts = BTreeSet::new();
    for c in source {
        engine.work.charge(1)?;
        let Copper::Pad(pad) = c else { continue };
        let Some(layer) = symmetric_surface(engine, pad) else {
            continue;
        };
        let existing = c.anchors(engine.input.bounds.min, grid);
        let (lo, hi) = pad.copper_bounds();
        let center = pad.copper_center();
        for phase in &phases {
            engine.work.charge(1)?;
            let low: Point = std::array::from_fn(|k| lo[k] + (phase[k] - lo[k]).rem_euclid(grid));
            if (0..2).any(|k| low[k] > hi[k]) {
                continue;
            }
            let anchor: Point = std::array::from_fn(|k| {
                let n = ((center[k] - low[k]) + grid / 2)
                    .div_euclid(grid)
                    .clamp(0, (hi[k] - low[k]) / grid);
                low[k] + n * grid
            });
            if !existing.contains(&anchor)
                && geometry::point_in_pad(anchor, pad)
                && engine.legal_edge_on(net, layer, anchor, anchor)?
            {
                engine
                    .work
                    .search_storage(phases.len() + starts.len() + 1)?;
                starts.insert((anchor, layer as u8, false));
            }
        }
    }
    Ok(starts.into_iter().collect())
}
