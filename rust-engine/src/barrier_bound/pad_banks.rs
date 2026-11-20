//! A cut through the common centerline of a bank of surface lands has an
//! open forbidden interval for each foreign pad. Overlapping intervals form
//! a planar barrier. A single elongated land can also force a detour past
//! either end. The ends stay passable in the relaxed geometry.
use super::{Barrier, BarrierBound};
use crate::model::{Failure, Input, Status};
use crate::rules::{DruItemKind, EffectiveRules};
use crate::Engine;
use std::collections::BTreeMap;

#[derive(Clone, Copy)]
struct PadInterval {
    index: usize,
    lo2: i64,
    hi2: i64,
    elongated: bool,
}

struct PadRow {
    pads: std::ops::Range<usize>,
    // Widest possible forbidden interval over all admitted nets.
    lo2: i64,
    hi2: i64,
}

#[derive(Default)]
pub(crate) struct PadBanks {
    // Cut coordinate, then layer. At most two entries per single-layer pad.
    rows: [BTreeMap<(i64, usize), PadRow>; 2],
    // A row with one elongated flank does not need a separate allocation.
    intervals: [Vec<PadInterval>; 2],
}

impl PadBanks {
    pub(crate) fn new(
        input: &Input,
        layers: &[String],
        rules: &EffectiveRules,
        max_local: i64,
    ) -> Self {
        let mut result = Self::default();
        if layers.len() < 2 || input.rules.same_net_hole_clearance_nm > 0 || input.pads.len() < 2 {
            return result;
        }
        // Like the static pad spatial index, this initialization is bounded by
        // the admitted input item count. Retain possible connected banks and
        // the transverse cuts of elongated leads.
        let mut entries = [Vec::new(), Vec::new()];
        let max_margin2 = rules.max_track_width_nm + 2 * rules.max_pair_clearance_nm.max(max_local);
        for (index, pad) in input.pads.iter().enumerate() {
            if pad.drill.is_some() {
                continue;
            }
            let layer = match pad.layers.as_slice() {
                [] => &input.layer,
                [l] => l,
                _ => continue,
            };
            let Some(k) = layers.iter().position(|s| s == layer) else {
                continue;
            };
            if k != 0 && k + 1 != layers.len() {
                continue;
            }
            let Some((center, half)) = pad.axial_extents_2x() else {
                continue;
            };
            for (axis, row) in entries.iter_mut().enumerate() {
                // A half-nanometre cut is outside this integer-coordinate relaxation.
                if center[axis] % 2 != 0 {
                    continue;
                }
                let j = 1 - axis;
                row.push((
                    (center[axis] / 2, k),
                    PadInterval {
                        index,
                        lo2: center[j] - half[j],
                        hi2: center[j] + half[j],
                        elongated: half[j] > half[axis],
                    },
                ));
            }
        }
        for (axis, entries) in entries.iter_mut().enumerate() {
            let rows = &mut result.rows[axis];
            let intervals = &mut result.intervals[axis];
            entries.sort_unstable_by_key(|(key, p)| (*key, p.lo2, p.index));
            for group in entries.chunk_by(|a, b| a.0 == b.0) {
                let mut hi = group[0].1.hi2;
                let mut overlap = false;
                for (_, p) in &group[1..] {
                    overlap |= p.lo2 < hi + 2 * max_margin2;
                    hi = hi.max(p.hi2);
                }
                if overlap || group.iter().any(|(_, p)| p.elongated) {
                    let start = intervals.len();
                    intervals.extend(group.iter().map(|(_, p)| *p));
                    rows.insert(
                        group[0].0,
                        PadRow {
                            pads: start..intervals.len(),
                            lo2: group[0].1.lo2 - max_margin2,
                            hi2: hi + max_margin2,
                        },
                    );
                }
            }
        }
        result
    }

    pub(crate) fn is_empty(&self) -> bool {
        self.rows.iter().all(BTreeMap::is_empty)
    }

    pub(crate) fn include(
        &self,
        engine: &Engine,
        net: u32,
        bound: &mut BarrierBound,
    ) -> Result<(), Failure> {
        for (axis, rows) in self.rows.iter().enumerate() {
            if rows.is_empty() {
                continue;
            }
            let (left, right) = (
                bound.source.1[axis].min(bound.target.1[axis]),
                bound.source.0[axis].max(bound.target.0[axis]),
            );
            if left >= right {
                continue;
            }
            let j = 1 - axis;
            engine.work.charge(1)?;
            for (&(cut, layer), row) in rows.range((left, usize::MAX)..(right, 0)) {
                engine.work.charge(1)?;
                // Both terminal envelopes already reach the same free half-line.
                if 2 * bound.source.0[j].max(bound.target.0[j]) <= row.lo2
                    || 2 * bound.source.1[j].min(bound.target.1[j]) >= row.hi2
                {
                    continue;
                }
                let pads = &self.intervals[axis][row.pads.clone()];
                let name = engine.layer_name(layer);
                engine
                    .work
                    .charge(engine.effective_rules.dru_rules.len() as u64)?;
                let width = match engine.effective_rules.track_width_for(net, name) {
                    Ok(v) => v,
                    Err(e) if e.status == Status::Unroutable => continue,
                    Err(e) => return Err(e),
                };
                engine.work.search_storage(pads.len())?;
                engine.work.charge(pads.len() as u64)?;
                let mut ranges = Vec::with_capacity(pads.len());
                for p in pads {
                    let pad = &engine.input.pads[p.index];
                    if pad.net == net {
                        continue;
                    }
                    engine
                        .work
                        .charge(engine.effective_rules.dru_rules.len() as u64)?;
                    let clr = engine.effective_rules.pair_clearance(
                        net,
                        DruItemKind::Track,
                        pad.net,
                        DruItemKind::Pad,
                        name,
                        pad.local_clearance_nm,
                    );
                    let margin = width + 2 * clr;
                    ranges.push((p.lo2 - margin, p.hi2 + margin, p.elongated));
                }
                // The actual width and pair clearance may change the lower-edge order.
                engine.work.charge(
                    ranges.len() as u64 * ranges.len().max(1).next_power_of_two().ilog2() as u64,
                )?;
                ranges.sort_unstable();
                engine.work.charge(ranges.len() as u64)?;
                let mut entries = ranges.into_iter().peekable();
                while let Some((lo, mut hi, mut obstructs)) = entries.next() {
                    while let Some((_, end, _)) = entries.next_if(|&(start, _, _)| start < hi) {
                        hi = hi.max(end);
                        obstructs = true;
                    }
                    if !obstructs {
                        continue;
                    }
                    // Round inward only after merging the strict open half-nm
                    // intervals. An equal pair of ends admits a tangent track.
                    let b = Barrier {
                        axis,
                        cut,
                        lo: -(-lo).div_euclid(2),
                        hi: hi.div_euclid(2),
                    };
                    bound.retain_layer_wall(layer, b);
                }
            }
        }
        Ok(())
    }
}
