use crate::{
    budget::WorkBudget,
    components::span_mask,
    model::{Failure, Point, Status, Track, Via},
    spatial::{CopperAabb, SpatialIndex, StaticSpatialIndex, SweptAabb, TrackGeom, ViaGeom},
};
use std::{
    cell::Cell,
    collections::{BTreeMap, BTreeSet},
    ops::ControlFlow,
};

#[derive(Debug)]
enum Undo {
    InsertedTrack(u64),
    RemovedTrack(u64, Track),
    SplitTrack(u64, Track),
    InsertedVia(u64),
    RemovedVia(u64, Via),
}

#[derive(Debug)]
struct Frame {
    journal: usize,
    next: u64,
}

fn via_bounds(v: &Via) -> (Point, Point) {
    let r = (v.diameter_nm + 1) / 2;
    ([v.at[0] - r, v.at[1] - r], [v.at[0] + r, v.at[1] + r])
}

/// Input copper has no mutation/removal API. The net and spatial indexes are
/// derived only from owned copper and are maintained by the same owning store.
#[derive(Debug)]
pub struct CopperStore {
    layer_names: Vec<String>,
    input: Vec<Track>,
    slots: Vec<Option<Track>>,
    free_slots: Vec<u32>,
    by_seq: BTreeMap<u64, u32>,
    by_net: BTreeMap<u32, BTreeSet<u64>>,
    input_spatial: Vec<StaticSpatialIndex<TrackGeom>>,
    generated_spatial: Vec<SpatialIndex<u64, u32>>,
    input_vias: Vec<Via>,
    via_slots: Vec<Option<Via>>,
    free_via_slots: Vec<u32>,
    via_by_seq: BTreeMap<u64, u32>,
    via_by_net: BTreeMap<u32, BTreeSet<u64>>,
    input_via_spatial: StaticSpatialIndex<ViaGeom>,
    generated_via_spatial: SpatialIndex<u64, u32>,
    journal: Vec<Undo>,
    frames: Vec<Frame>,
    next: u64,
    reserved: BTreeSet<String>,
    prefix: String,
    base_items: usize,
    last_hit_generated: Cell<bool>,
}

// gap2 retains odd-nanometre half widths. The far cap has to overlap the
// finite host; a tangency, a collinear overlap, or a near-centerline touch
// does not need a new junction.
fn short_tap(arm: &Track, host: &Track) -> Option<crate::model::Point> {
    let lo = host.start.min(host.end);
    let hi = host.start.max(host.end);
    let gap2 = i128::from(host.width_nm) + i128::from(arm.width_nm);
    [(arm.start, arm.end), (arm.end, arm.start)]
        .into_iter()
        .find_map(|(tap, far)| {
            (lo < tap
                && tap < hi
                && crate::geometry::orientation(host.start, host.end, tap) == 0
                && crate::geometry::orientation(host.start, host.end, far) != 0
                && crate::geometry::point_near_segment(far, host.start, host.end, gap2, false))
            .then_some(tap)
        })
}

impl CopperStore {
    #[allow(dead_code)]
    pub(crate) fn new(
        input: Vec<Track>,
        reserved: BTreeSet<String>,
        source: &str,
        base_items: usize,
    ) -> Self {
        Self::new_multilayer(
            input,
            Vec::new(),
            vec!["F.Cu".to_owned()],
            reserved,
            source,
            base_items,
        )
    }

    pub(crate) fn new_multilayer(
        input: Vec<Track>,
        input_vias: Vec<Via>,
        layer_names: Vec<String>,
        reserved: BTreeSet<String>,
        source: &str,
        base_items: usize,
    ) -> Self {
        // Deterministic custom UUID (version 8), source prefix plus counter.
        // All input pad/track/via IDs are reserved; collisions advance the counter.
        let prefix = format!(
            "{}-{}-8{}-8{}-",
            &source[..8],
            &source[8..12],
            &source[12..15],
            &source[15..18]
        );
        let layer_count = layer_names.len().max(1);
        let mut per_layer_items: Vec<Vec<(u32, u32, CopperAabb, TrackGeom)>> =
            vec![Vec::new(); layer_count];
        for (i, track) in input.iter().enumerate() {
            let l_idx = layer_names
                .iter()
                .position(|l| l == &track.layer)
                .unwrap_or(0);
            per_layer_items[l_idx].push((
                u32::try_from(i).expect("bounded input track count"),
                track.net,
                CopperAabb::from_bounds(track.copper_bounds()),
                TrackGeom {
                    start: track.start,
                    end: track.end,
                    width_nm: track.width_nm,
                },
            ));
        }
        let input_spatial = per_layer_items
            .into_iter()
            .map(StaticSpatialIndex::new)
            .collect();
        let generated_spatial = (0..layer_count).map(|_| SpatialIndex::new()).collect();
        let input_via_spatial =
            StaticSpatialIndex::new(input_vias.iter().enumerate().map(|(i, v)| {
                let s = layer_names
                    .iter()
                    .position(|l| l == &v.start_layer)
                    .unwrap_or(0);
                let e = layer_names
                    .iter()
                    .position(|l| l == &v.end_layer)
                    .unwrap_or(s);
                (
                    u32::try_from(i).expect("bounded input via count"),
                    v.net,
                    CopperAabb::from_bounds(via_bounds(v)),
                    ViaGeom {
                        at: [v.at[0] as i32, v.at[1] as i32],
                        diameter: v.diameter_nm as i32,
                        drill: v.drill_nm as i32,
                        layer_mask: span_mask(s, e),
                    },
                )
            }));
        Self {
            layer_names,
            input,
            slots: Vec::new(),
            free_slots: Vec::new(),
            by_seq: BTreeMap::new(),
            by_net: BTreeMap::new(),
            input_spatial,
            generated_spatial,
            input_vias,
            via_slots: Vec::new(),
            free_via_slots: Vec::new(),
            via_by_seq: BTreeMap::new(),
            via_by_net: BTreeMap::new(),
            input_via_spatial,
            generated_via_spatial: SpatialIndex::new(),
            journal: Vec::new(),
            frames: Vec::new(),
            next: 1,
            reserved,
            prefix,
            base_items,
            last_hit_generated: Cell::new(false),
        }
    }

    fn layer_idx(&self, layer: &str) -> usize {
        self.layer_names
            .iter()
            .position(|l| l == layer)
            .expect("validated track layer exists in stackup")
    }

    fn via_mask(&self, v: &Via) -> u32 {
        let s = self.layer_idx(&v.start_layer);
        let e = self.layer_idx(&v.end_layer);
        span_mask(s, e)
    }

    pub fn tracks(&self) -> impl Iterator<Item = &Track> {
        self.input.iter().chain(self.generated())
    }

    pub(crate) fn generated_item_count(&self) -> usize {
        self.by_seq.len() + self.via_by_seq.len()
    }

    pub(crate) fn has_generated_tracks(&self) -> bool {
        !self.by_seq.is_empty()
    }

    pub fn generated(&self) -> impl Iterator<Item = &Track> {
        self.by_seq.values().map(|&slot| {
            self.slots[slot as usize]
                .as_ref()
                .expect("by_seq references live track slot")
        })
    }

    pub fn input(&self) -> &[Track] {
        &self.input
    }

    pub fn vias(&self) -> impl Iterator<Item = &Via> {
        self.input_vias.iter().chain(self.generated_vias())
    }

    pub fn generated_vias(&self) -> impl Iterator<Item = &Via> {
        self.via_by_seq.values().map(|&slot| {
            self.via_slots[slot as usize]
                .as_ref()
                .expect("via_by_seq references live via slot")
        })
    }

    pub fn input_vias(&self) -> &[Via] {
        &self.input_vias
    }

    pub fn via_count_for_net(&self, net: u32) -> usize {
        self.input_vias.iter().filter(|v| v.net == net).count()
            + self.via_by_net.get(&net).map_or(0, BTreeSet::len)
    }

    pub(crate) fn has_generated_for_net(&self, net: u32) -> bool {
        self.by_net.contains_key(&net) || self.via_by_net.contains_key(&net)
    }

    pub(crate) fn net_generated_tracks(&self, net: u32) -> Vec<&Track> {
        self.by_net
            .get(&net)
            .into_iter()
            .flatten()
            .map(|seq| {
                let slot = self.by_seq[seq];
                self.slots[slot as usize]
                    .as_ref()
                    .expect("by_seq references live track slot")
            })
            .collect()
    }

    pub(crate) fn net_generated_vias(&self, net: u32) -> Vec<&Via> {
        self.via_by_net
            .get(&net)
            .into_iter()
            .flatten()
            .map(|seq| {
                let slot = self.via_by_seq[seq];
                self.via_slots[slot as usize]
                    .as_ref()
                    .expect("via_by_seq references live via slot")
            })
            .collect()
    }

    pub fn index_consistent(&self) -> bool {
        let mut expected_net: BTreeMap<u32, BTreeSet<u64>> = BTreeMap::new();
        let mut live_slots = BTreeSet::new();
        for (&seq, &slot) in &self.by_seq {
            let Some(Some(t)) = self.slots.get(slot as usize) else {
                return false;
            };
            if !live_slots.insert(slot) || t.id != format!("{}{:012x}", self.prefix, seq) {
                return false;
            }
            expected_net.entry(t.net).or_default().insert(seq);
        }
        let mut free_set = BTreeSet::new();
        for &slot in &self.free_slots {
            if !free_set.insert(slot) || self.slots.get(slot as usize).is_none_or(Option::is_some) {
                return false;
            }
        }
        if live_slots.len() + free_set.len() != self.slots.len()
            || !live_slots.is_disjoint(&free_set)
            || expected_net != self.by_net
        {
            return false;
        }
        for (l_idx, l_name) in self.layer_names.iter().enumerate() {
            let input_ok = self.input_spatial[l_idx].consistent(
                self.input
                    .iter()
                    .enumerate()
                    .filter(|(_, t)| &t.layer == l_name)
                    .map(|(i, t)| {
                        (
                            u32::try_from(i).expect("bounded input track count"),
                            t.net,
                            CopperAabb::from_bounds(t.copper_bounds()),
                            TrackGeom {
                                start: t.start,
                                end: t.end,
                                width_nm: t.width_nm,
                            },
                        )
                    }),
            );
            if !input_ok {
                return false;
            }
            let gen_ok = self.generated_spatial[l_idx].consistent(self.by_seq.iter().filter_map(
                |(&seq, &slot)| {
                    let t = self.slots[slot as usize].as_ref().unwrap();
                    (&t.layer == l_name)
                        .then(|| (seq, slot, t.net, CopperAabb::from_bounds(t.copper_bounds())))
                },
            ));
            if !gen_ok {
                return false;
            }
        }
        let mut expected_via_net: BTreeMap<u32, BTreeSet<u64>> = BTreeMap::new();
        let mut live_via_slots = BTreeSet::new();
        for (&seq, &slot) in &self.via_by_seq {
            let Some(Some(v)) = self.via_slots.get(slot as usize) else {
                return false;
            };
            if !live_via_slots.insert(slot) || v.id != format!("{}{:012x}", self.prefix, seq) {
                return false;
            }
            expected_via_net.entry(v.net).or_default().insert(seq);
        }
        let mut free_via_set = BTreeSet::new();
        for &slot in &self.free_via_slots {
            if !free_via_set.insert(slot)
                || self
                    .via_slots
                    .get(slot as usize)
                    .is_none_or(Option::is_some)
            {
                return false;
            }
        }
        live_via_slots.len() + free_via_set.len() == self.via_slots.len()
            && live_via_slots.is_disjoint(&free_via_set)
            && expected_via_net == self.via_by_net
            && self
                .input_via_spatial
                .consistent(self.input_vias.iter().enumerate().map(|(i, v)| {
                    (
                        u32::try_from(i).expect("bounded input via count"),
                        v.net,
                        CopperAabb::from_bounds(via_bounds(v)),
                        ViaGeom {
                            at: [v.at[0] as i32, v.at[1] as i32],
                            diameter: v.diameter_nm as i32,
                            drill: v.drill_nm as i32,
                            layer_mask: self.via_mask(v),
                        },
                    )
                }))
            && self
                .generated_via_spatial
                .consistent(self.via_by_seq.iter().map(|(&seq, &slot)| {
                    let v = self.via_slots[slot as usize].as_ref().unwrap();
                    (seq, slot, v.net, CopperAabb::from_bounds(via_bounds(v)))
                }))
    }

    #[cfg(test)]
    #[inline(always)]
    pub(crate) fn visit_candidates_geoms_filtered(
        &self,
        bounds: SweptAabb,
        net_filter: Option<u32>,
        work: &WorkBudget,
        mut visitor: impl FnMut(TrackGeom) -> Result<ControlFlow<()>, Failure>,
    ) -> Result<ControlFlow<()>, Failure> {
        self.visit_layer_candidates_with_net_filtered(0, bounds, net_filter, work, |_, g| {
            visitor(g)
        })
    }

    #[inline(always)]
    pub(crate) fn last_hit_generated(&self) -> bool {
        self.last_hit_generated.get()
    }

    #[inline(always)]
    pub(crate) fn visit_layer_candidates_with_net_filtered(
        &self,
        layer_idx: usize,
        bounds: SweptAabb,
        net_filter: Option<u32>,
        work: &WorkBudget,
        mut visitor: impl FnMut(u32, TrackGeom) -> Result<ControlFlow<()>, Failure>,
    ) -> Result<ControlFlow<()>, Failure> {
        self.last_hit_generated.set(false);
        let flow = self.input_spatial[layer_idx].visit_matching(
            bounds,
            net_filter,
            work,
            |ordinal, geom| {
                work.charge(1)?;
                visitor(self.input[ordinal as usize].net, *geom)
            },
        )?;
        if flow.is_break() {
            return Ok(ControlFlow::Break(()));
        }
        let gen_flow = self.generated_spatial[layer_idx].visit_matching(
            bounds,
            net_filter,
            work,
            |_seq, &slot| {
                work.charge(1)?;
                let track = self.slots[slot as usize]
                    .as_ref()
                    .expect("spatial slot references owned copper");
                visitor(
                    track.net,
                    TrackGeom {
                        start: track.start,
                        end: track.end,
                        width_nm: track.width_nm,
                    },
                )
            },
        )?;
        if gen_flow.is_break() {
            self.last_hit_generated.set(true);
        }
        Ok(gen_flow)
    }

    #[inline(always)]
    pub(crate) fn visit_via_candidates_filtered(
        &self,
        bounds: SweptAabb,
        net_filter: Option<u32>,
        work: &WorkBudget,
        mut visitor: impl FnMut(&Via, u32) -> Result<ControlFlow<()>, Failure>,
    ) -> Result<ControlFlow<()>, Failure> {
        self.last_hit_generated.set(false);
        let flow =
            self.input_via_spatial
                .visit_matching(bounds, net_filter, work, |ordinal, geom| {
                    work.charge(1)?;
                    visitor(&self.input_vias[ordinal as usize], geom.layer_mask)
                })?;
        if flow.is_break() {
            return Ok(ControlFlow::Break(()));
        }
        let gen_flow = self.generated_via_spatial.visit_matching(
            bounds,
            net_filter,
            work,
            |_seq, &slot| {
                work.charge(1)?;
                let via = self.via_slots[slot as usize]
                    .as_ref()
                    .expect("via spatial slot references owned via");
                visitor(via, self.via_mask(via))
            },
        )?;
        if gen_flow.is_break() {
            self.last_hit_generated.set(true);
        }
        Ok(gen_flow)
    }

    #[inline(always)]
    pub(crate) fn visit_generated_layer_candidates(
        &self,
        layer_idx: usize,
        bounds: SweptAabb,
        net_filter: Option<u32>,
        work: &WorkBudget,
        mut visitor: impl FnMut(u32, TrackGeom) -> Result<ControlFlow<()>, Failure>,
    ) -> Result<ControlFlow<()>, Failure> {
        self.generated_spatial[layer_idx].visit_matching(bounds, net_filter, work, |_seq, &slot| {
            work.charge(1)?;
            let track = self.slots[slot as usize]
                .as_ref()
                .expect("spatial slot references owned copper");
            visitor(
                track.net,
                TrackGeom {
                    start: track.start,
                    end: track.end,
                    width_nm: track.width_nm,
                },
            )
        })
    }

    #[inline(always)]
    pub(crate) fn visit_generated_via_candidates(
        &self,
        bounds: SweptAabb,
        net_filter: Option<u32>,
        work: &WorkBudget,
        mut visitor: impl FnMut(&Via, u32) -> Result<ControlFlow<()>, Failure>,
    ) -> Result<ControlFlow<()>, Failure> {
        self.generated_via_spatial
            .visit_matching(bounds, net_filter, work, |_seq, &slot| {
                work.charge(1)?;
                let via = self.via_slots[slot as usize]
                    .as_ref()
                    .expect("via spatial slot references owned via");
                visitor(via, self.via_mask(via))
            })
    }

    #[cfg(test)]
    #[inline]
    pub(crate) fn visit_candidates_filtered<'a>(
        &'a self,
        bounds: SweptAabb,
        net_filter: Option<u32>,
        work: &WorkBudget,
        mut visitor: impl FnMut(&'a Track) -> Result<ControlFlow<()>, Failure>,
    ) -> Result<ControlFlow<()>, Failure> {
        let flow =
            self.input_spatial[0].visit_matching(bounds, net_filter, work, |ordinal, _geom| {
                work.charge(1)?;
                visitor(&self.input[ordinal as usize])
            })?;
        if flow.is_break() {
            return Ok(ControlFlow::Break(()));
        }
        self.generated_spatial[0].visit_matching(bounds, net_filter, work, |_seq, &slot| {
            work.charge(1)?;
            let track = self.slots[slot as usize]
                .as_ref()
                .expect("spatial slot references owned copper");
            visitor(track)
        })
    }

    #[cfg(test)]
    pub(crate) fn visit_candidates<'a>(
        &'a self,
        bounds: SweptAabb,
        work: &WorkBudget,
        visitor: impl FnMut(&'a Track) -> Result<ControlFlow<()>, Failure>,
    ) -> Result<ControlFlow<()>, Failure> {
        self.visit_candidates_filtered(bounds, None, work, visitor)
    }

    #[cfg(test)]
    pub(crate) fn candidates(
        &self,
        bounds: SweptAabb,
        work: &WorkBudget,
    ) -> Result<Vec<&Track>, Failure> {
        let mut tracks = Vec::new();
        let _ = self.visit_candidates(bounds, work, |track| {
            tracks.push(track);
            Ok::<_, Failure>(ControlFlow::Continue(()))
        })?;
        tracks.sort_by(|a, b| a.id.cmp(&b.id));
        Ok(tracks)
    }

    // Only the private Engine scope guard opens/closes frames. No checkpoint
    // token can escape, be copied, or be applied to another store.
    pub(crate) fn begin(&mut self, work: &WorkBudget) -> Result<(), Failure> {
        work.charge(1)?;
        if self.frames.len() >= work.limits.max_depth {
            return Err(work.failure(
                Status::NestingExhausted,
                "transaction nesting limit exceeded",
            ));
        }
        self.frames.push(Frame {
            journal: self.journal.len(),
            next: self.next,
        });
        Ok(())
    }

    fn add_raw(&mut self, seq: u64, t: Track) {
        let l_idx = self.layer_idx(&t.layer);
        let slot = if let Some(slot) = self.free_slots.pop() {
            assert!(self.slots[slot as usize].is_none());
            self.slots[slot as usize] = Some(t);
            slot
        } else {
            let slot = u32::try_from(self.slots.len()).expect("bounded copper slots");
            self.slots.push(Some(t));
            slot
        };
        let track = self.slots[slot as usize].as_ref().unwrap();
        self.generated_spatial[l_idx].insert(
            seq,
            slot,
            track.net,
            CopperAabb::from_bounds(track.copper_bounds()),
        );
        self.by_net.entry(track.net).or_default().insert(seq);
        assert!(self.by_seq.insert(seq, slot).is_none());
    }

    fn remove_raw(&mut self, seq: u64) -> Track {
        let slot = self
            .by_seq
            .remove(&seq)
            .expect("journal/index references owned generated copper");
        let t = self.slots[slot as usize]
            .take()
            .expect("slot references owned generated copper");
        self.free_slots.push(slot);
        let l_idx = self.layer_idx(&t.layer);
        self.generated_spatial[l_idx].remove(&seq, CopperAabb::from_bounds(t.copper_bounds()));
        let seqs = self.by_net.get_mut(&t.net).unwrap();
        assert!(seqs.remove(&seq));
        if seqs.is_empty() {
            self.by_net.remove(&t.net);
        }
        t
    }

    fn add_via_raw(&mut self, seq: u64, v: Via) {
        let slot = if let Some(slot) = self.free_via_slots.pop() {
            assert!(self.via_slots[slot as usize].is_none());
            self.via_slots[slot as usize] = Some(v);
            slot
        } else {
            let slot = u32::try_from(self.via_slots.len()).expect("bounded via slots");
            self.via_slots.push(Some(v));
            slot
        };
        let via = self.via_slots[slot as usize].as_ref().unwrap();
        self.generated_via_spatial.insert(
            seq,
            slot,
            via.net,
            CopperAabb::from_bounds(via_bounds(via)),
        );
        self.via_by_net.entry(via.net).or_default().insert(seq);
        assert!(self.via_by_seq.insert(seq, slot).is_none());
    }

    fn remove_via_raw(&mut self, seq: u64) -> Via {
        let slot = self
            .via_by_seq
            .remove(&seq)
            .expect("journal/index references owned generated via");
        let v = self.via_slots[slot as usize]
            .take()
            .expect("slot references owned generated via");
        self.free_via_slots.push(slot);
        self.generated_via_spatial
            .remove(&seq, CopperAabb::from_bounds(via_bounds(&v)));
        let seqs = self.via_by_net.get_mut(&v.net).unwrap();
        assert!(seqs.remove(&seq));
        if seqs.is_empty() {
            self.via_by_net.remove(&v.net);
        }
        v
    }

    fn admit_journal(&self, count: usize, work: &WorkBudget) -> Result<(), Failure> {
        if count
            > work
                .limits
                .max_journal_entries
                .saturating_sub(self.journal.len())
        {
            return Err(work.failure(
                Status::JournalExhausted,
                "undo journal entry limit exceeded",
            ));
        }
        Ok(())
    }

    fn alloc_seq_id(&mut self, work: &WorkBudget) -> Result<(u64, String), Failure> {
        loop {
            work.charge(1)?;
            assert!(
                self.next < (1u64 << 48),
                "bounded search cannot exhaust ID space"
            );
            let seq = self.next;
            let id = format!("{}{:012x}", self.prefix, seq);
            self.next = self.next.checked_add(1).expect("bounded ID counter");
            if !self.reserved.contains(&id)
                && !self.by_seq.contains_key(&seq)
                && !self.via_by_seq.contains_key(&seq)
            {
                return Ok((seq, id));
            }
        }
    }

    pub(crate) fn insert(&mut self, mut t: Track, work: &WorkBudget) -> Result<(), Failure> {
        self.admit_journal(1, work)?;
        if self.base_items + self.by_seq.len() + self.via_by_seq.len() >= work.limits.max_items {
            return Err(work.failure(Status::AdmissionExceeded, "copper item limit exceeded"));
        }
        let l_idx = self.layer_idx(&t.layer);
        work.charge(1)?;
        let (seq, id) = self.alloc_seq_id(work)?;
        t.id = id;
        work.charge(self.generated_spatial[l_idx].mutation_work())?;
        self.journal.push(Undo::InsertedTrack(seq));
        self.add_raw(seq, t);
        Ok(())
    }

    /// A branch end exactly on a trunk centerline may still be reported
    /// dangling when its first short bend also touches the trunk's copper.
    /// Make the tap an endpoint on the trunk, whichever piece arrived first.
    pub(crate) fn insert_joined_track(
        &mut self,
        mut track: Track,
        work: &WorkBudget,
    ) -> Result<(), Failure> {
        let taps = self.split_short_branch_taps(&track, work)?;
        for tap in taps {
            work.charge(1)?;
            self.insert(
                Track {
                    end: tap,
                    ..track.clone()
                },
                work,
            )?;
            track.start = tap;
        }
        self.insert(track, work)
    }

    fn split_short_branch_taps(
        &mut self,
        incoming: &Track,
        work: &WorkBudget,
    ) -> Result<Vec<crate::model::Point>, Failure> {
        let mut taps = Vec::new();
        if !self.has_generated_for_net(incoming.net) {
            return Ok(taps);
        }
        let layer = self.layer_idx(&incoming.layer);
        let mut hosts = Vec::new();
        // A single query over the arriving centerline's bounds limits visits
        // to the generated spatial index. Its extent never expands into a
        // sequence of grid points, even for a board-length diagonal.
        let _ = self.generated_spatial[layer].visit_matching(
            SweptAabb::swept(incoming.start, incoming.end, 0, 0),
            None,
            work,
            |&seq, &slot| {
                let existing = self.slots[slot as usize].as_ref().unwrap();
                work.charge(1)?;
                if existing.net != incoming.net {
                    return Ok(ControlFlow::Continue(()));
                }
                if let Some(tap) = short_tap(incoming, existing) {
                    work.search_storage(hosts.len() + taps.len() + 1)?;
                    hosts.push((seq, tap));
                }
                if let Some(tap) = short_tap(existing, incoming) {
                    work.search_storage(hosts.len() + taps.len() + 1)?;
                    taps.push(tap);
                }
                Ok(ControlFlow::Continue(()))
            },
        )?;
        // Stable along-track order preserves the source end of the original
        // trunk and allocates reproducible identities for the other pieces.
        work.charge(hosts.len() as u64 * (1 + hosts.len().max(1).ilog2() as u64))?;
        hosts.sort_unstable();
        for (seq, tap) in hosts {
            self.admit_journal(1, work)?;
            work.charge(2 * self.generated_spatial[layer].mutation_work())?;
            let original = self.remove_raw(seq);
            let first = Track {
                end: tap,
                ..original.clone()
            };
            let second = Track {
                start: tap,
                ..original.clone()
            };
            self.journal.push(Undo::SplitTrack(seq, original));
            self.add_raw(seq, first);
            self.insert(second, work)?;
        }
        work.charge(taps.len() as u64 * (2 + taps.len().max(1).ilog2() as u64))?;
        taps.sort_unstable();
        taps.dedup();
        if incoming.start > incoming.end {
            taps.reverse();
        }
        Ok(taps)
    }

    pub(crate) fn insert_via(&mut self, mut v: Via, work: &WorkBudget) -> Result<(), Failure> {
        self.admit_journal(1, work)?;
        if self.base_items + self.by_seq.len() + self.via_by_seq.len() >= work.limits.max_items {
            return Err(work.failure(Status::AdmissionExceeded, "copper item limit exceeded"));
        }
        work.charge(1)?;
        let (seq, id) = self.alloc_seq_id(work)?;
        v.id = id;
        work.charge(self.generated_via_spatial.mutation_work())?;
        self.journal.push(Undo::InsertedVia(seq));
        self.add_via_raw(seq, v);
        Ok(())
    }

    pub(crate) fn journal_len(&self) -> usize {
        self.journal.len()
    }

    /// A rip within an open round leaves the original geometry in its undo log.
    pub(crate) fn removed_copper(
        &self,
        range: std::ops::Range<usize>,
    ) -> Vec<crate::components::Copper<'_>> {
        self.journal[range]
            .iter()
            .filter_map(|entry| match entry {
                Undo::RemovedTrack(_, t) => Some(crate::components::Copper::Track(t)),
                Undo::RemovedVia(_, v) => Some(crate::components::Copper::Via(v)),
                _ => None,
            })
            .collect()
    }

    pub(crate) fn remove_net(&mut self, net: u32, work: &WorkBudget) -> Result<(), Failure> {
        let track_count = self.by_net.get(&net).map_or(0, BTreeSet::len);
        let via_count = self.via_by_net.get(&net).map_or(0, BTreeSet::len);
        let total = track_count + via_count;
        self.admit_journal(total, work)?;
        work.charge(total as u64)?;
        let track_seqs: Vec<u64> = self
            .by_net
            .get(&net)
            .into_iter()
            .flatten()
            .copied()
            .collect();
        for seq in track_seqs {
            let slot = self.by_seq[&seq];
            let l_idx = self.layer_idx(&self.slots[slot as usize].as_ref().unwrap().layer);
            work.charge(1 + self.generated_spatial[l_idx].mutation_work())?;
            let t = self.remove_raw(seq);
            self.journal.push(Undo::RemovedTrack(seq, t));
        }
        let via_seqs: Vec<u64> = self
            .via_by_net
            .get(&net)
            .into_iter()
            .flatten()
            .copied()
            .collect();
        for seq in via_seqs {
            work.charge(1 + self.generated_via_spatial.mutation_work())?;
            let v = self.remove_via_raw(seq);
            self.journal.push(Undo::RemovedVia(seq, v));
        }
        Ok(())
    }

    pub(crate) fn rollback(&mut self) {
        let c = self.frames.pop().expect("scope owns an open transaction");
        while self.journal.len() > c.journal {
            match self.journal.pop().unwrap() {
                Undo::InsertedTrack(seq) => {
                    self.remove_raw(seq);
                }
                Undo::RemovedTrack(seq, t) => self.add_raw(seq, t),
                Undo::SplitTrack(seq, original) => {
                    self.remove_raw(seq);
                    self.add_raw(seq, original);
                }
                Undo::InsertedVia(seq) => {
                    self.remove_via_raw(seq);
                }
                Undo::RemovedVia(seq, v) => self.add_via_raw(seq, v),
            }
        }
        self.next = c.next;
    }

    pub(crate) fn commit(&mut self) {
        self.frames.pop().expect("scope owns an open transaction");
        // Inner commits remain undoable by the parent. Only an outermost
        // commit can release the journal (including removed track objects).
        if self.frames.is_empty() {
            self.journal.clear();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Limits;

    fn dummy_track(net: u32, x: i64) -> Track {
        Track {
            id: String::new(),
            net,
            start: [x, 0],
            end: [x + 5, 0],
            width_nm: 2,
            locked: false,
            layer: "F.Cu".into(),
        }
    }

    #[test]
    fn short_branch_splits_only_a_matching_generated_trunk_and_rolls_back_its_id() {
        let mut trunk = dummy_track(1, 0);
        trunk.start = [40, 100];
        trunk.end = [40, 0];
        trunk.width_nm = 10;
        let mut authored = trunk.clone();
        authored.id = "protected".into();
        authored.locked = true;
        authored.start[0] = 10;
        authored.end[0] = 10;
        let mut store = CopperStore::new_multilayer(
            vec![authored.clone()],
            Vec::new(),
            vec!["F.Cu".into(), "B.Cu".into()],
            BTreeSet::from([authored.id.clone()]),
            &"d".repeat(64),
            1,
        );
        let work = WorkBudget::new(Limits::default());
        store.begin(&work).unwrap();
        store.insert(trunk.clone(), &work).unwrap();
        store.commit();
        let original = store.generated().next().unwrap().clone();
        let arm = Track {
            start: [46, 56],
            end: [40, 50],
            ..trunk.clone()
        };
        for (start, end, net, layer, width_nm) in [
            ([16, 56], [10, 50], 1, "F.Cu", 10), // authored trunk
            ([48, 56], [42, 50], 1, "F.Cu", 10), // off centerline
            ([50, 60], [40, 50], 1, "F.Cu", 10), // tangent end caps
            ([40, 56], [40, 50], 1, "F.Cu", 10), // collinear
            (arm.start, arm.end, 1, "B.Cu", 10),
            (arm.start, arm.end, 2, "F.Cu", 10),
        ] {
            let other = Track {
                start,
                end,
                net,
                layer: layer.into(),
                width_nm,
                ..arm.clone()
            };
            store.begin(&work).unwrap();
            store.split_short_branch_taps(&other, &work).unwrap();
            assert_eq!(store.generated().collect::<Vec<_>>(), [&original]);
            assert_eq!(store.input(), [authored.clone()]);
            store.commit();
        }

        // A rejected extra segment must leave no lasting shortening or ID gap.
        let full = WorkBudget::new(Limits {
            max_items: 2,
            ..Limits::default()
        });
        store.begin(&full).unwrap();
        assert_eq!(
            store
                .split_short_branch_taps(&arm, &full)
                .unwrap_err()
                .status,
            Status::AdmissionExceeded
        );
        store.rollback();
        assert_eq!(store.generated().collect::<Vec<_>>(), [&original]);
        let mut restored_ids = Vec::new();
        for _ in 0..2 {
            store.begin(&work).unwrap();
            store.split_short_branch_taps(&arm, &work).unwrap();
            let pieces: Vec<_> = store.generated().collect();
            assert_eq!(pieces.len(), 2);
            assert_eq!(
                pieces[0],
                &Track {
                    end: arm.end,
                    ..original.clone()
                }
            );
            assert_eq!(
                pieces[1],
                &Track {
                    id: pieces[1].id.clone(),
                    start: arm.end,
                    ..original.clone()
                }
            );
            assert_ne!(pieces[0].id, pieces[1].id);
            restored_ids.push(pieces[1].id.clone());
            assert!(store.removed_copper(0..store.journal_len()).is_empty());
            store.rollback();
            assert!(store.index_consistent());
            assert_eq!(store.generated().collect::<Vec<_>>(), [&original]);
        }
        assert_eq!(restored_ids[0], restored_ids[1]);
    }

    #[test]
    fn generated_junction_uses_exact_centerline_for_both_orders_and_widths() {
        let tap = [15_000_000, 15_000_000];
        for (start, end, far, host_width) in [
            (
                [15_000_000, 25_000_000],
                [15_000_000, 5_000_000],
                [15_150_000, 15_150_000],
                200_000,
            ),
            (
                [8_000_000, 8_000_000],
                [22_000_000, 22_000_000],
                [15_212_132, 15_000_000],
                240_000,
            ),
            (
                [21_000_000, 23_000_000],
                [9_000_000, 7_000_000],
                [15_212_132, 15_000_000],
                160_001,
            ),
        ] {
            let host = Track {
                start,
                end,
                width_nm: host_width,
                ..dummy_track(1, 0)
            };
            let arm = Track {
                start: tap,
                end: far,
                width_nm: 200_000,
                ..dummy_track(1, 0)
            };
            for arm_first in [false, true] {
                let mut store = CopperStore::new(Vec::new(), BTreeSet::new(), &"e".repeat(64), 0);
                let work = WorkBudget::new(Limits::default());
                let (first, second) = if arm_first {
                    (&arm, &host)
                } else {
                    (&host, &arm)
                };
                store.begin(&work).unwrap();
                store.insert_joined_track(first.clone(), &work).unwrap();
                store.commit();
                let prior = store.generated().next().unwrap().clone();
                // Exhaust the live-item budget after the first host half.
                let limited = WorkBudget::new(Limits {
                    max_items: 2,
                    ..Limits::default()
                });
                store.begin(&limited).unwrap();
                assert_eq!(
                    store
                        .insert_joined_track(second.clone(), &limited)
                        .unwrap_err()
                        .status,
                    Status::AdmissionExceeded
                );
                store.rollback();
                assert_eq!(store.generated().collect::<Vec<_>>(), [&prior]);
                assert!(store.index_consistent());
                let mut attempts = Vec::new();
                for _ in 0..2 {
                    store.begin(&work).unwrap();
                    store.insert_joined_track(second.clone(), &work).unwrap();
                    let pieces = store.generated().cloned().collect::<Vec<_>>();
                    assert_eq!(pieces.len(), 3);
                    for (p, q, width) in [
                        (start, tap, host_width),
                        (tap, end, host_width),
                        (tap, far, 200_000),
                    ] {
                        assert!(pieces
                            .iter()
                            .any(|t| t.start == p && t.end == q && t.width_nm == width));
                    }
                    assert_eq!(pieces[0].id, prior.id);
                    assert_eq!(pieces[0].start, first.start);
                    assert!(store.index_consistent());
                    attempts.push(pieces);
                    store.rollback();
                    assert_eq!(store.generated().collect::<Vec<_>>(), [&prior]);
                    assert!(store.index_consistent());
                }
                assert_eq!(attempts[0], attempts[1]);
            }
            let off = Track {
                start: [tap[0] + 1, tap[1]],
                ..arm.clone()
            };
            assert_eq!(short_tap(&off, &host), None);
            let long = Track {
                end: [tap[0] + 2_000_000, tap[1]],
                ..arm.clone()
            };
            assert_eq!(short_tap(&long, &host), None);
        }
    }

    #[test]
    fn an_arriving_trunk_keeps_multiple_taps_in_its_own_direction() {
        let mut store = CopperStore::new(Vec::new(), BTreeSet::new(), &"c".repeat(64), 0);
        let work = WorkBudget::new(Limits::default());
        store.begin(&work).unwrap();
        for x in [30, 70, 30] {
            let arm = Track {
                start: [x, 100],
                end: [x + 3, 106],
                width_nm: 10,
                ..dummy_track(1, 0)
            };
            store.insert_joined_track(arm, &work).unwrap();
        }
        let trunk = Track {
            start: [100, 100],
            end: [0, 100],
            width_nm: 10,
            ..dummy_track(1, 0)
        };
        store.insert_joined_track(trunk, &work).unwrap();
        let pieces = store
            .generated()
            .filter(|t| t.start[1] == 100 && t.end[1] == 100)
            .map(|t| (t.start[0], t.end[0]))
            .collect::<Vec<_>>();
        assert_eq!(pieces, [(100, 70), (70, 30), (30, 0)]);
        store.rollback();
        assert!(store.generated().next().is_none());
        assert!(store.index_consistent());
    }

    #[test]
    fn store_insert_and_remove_net_charge_spatial_mutation_work_before_edit() {
        let source = "a".repeat(64);
        let mut store = CopperStore::new(Vec::new(), BTreeSet::new(), &source, 0);
        for step in 1..=12u32 {
            let mut work = WorkBudget::new(Limits::default());
            let mut scope = work.scope();
            let mutation = store.generated_spatial[0].mutation_work();
            assert_eq!(
                mutation,
                8 * (u64::from(store.generated_spatial[0].height()) + 1)
            );
            let short_work = WorkBudget::new(Limits {
                max_work: 2 + mutation - 1,
                ..Limits::default()
            });
            let _short_scope = short_work.scope();
            let err = store
                .insert(dummy_track(step, i64::from(step) * 20), &short_work)
                .unwrap_err();
            assert_eq!(err.status, Status::WorkExhausted);
            assert_eq!(short_work.used(), 2);
            assert_eq!(store.generated().count(), (step - 1) as usize);
            assert!(store.index_consistent());
            drop(_short_scope);
            drop(scope);

            work = WorkBudget::new(Limits::default());
            scope = work.scope();
            store
                .insert(dummy_track(step, i64::from(step) * 20), &work)
                .unwrap();
            assert_eq!(work.used(), 2 + mutation);
            assert!(store.index_consistent());
            drop(scope);
        }
        assert_eq!(store.generated_spatial[0].height(), 8);

        let mutation = store.generated_spatial[0].mutation_work();
        assert_eq!(mutation, 72);
        let short_budget = WorkBudget::new(Limits {
            max_work: 1 + 1 + mutation - 1,
            ..Limits::default()
        });
        let _short_scope = short_budget.scope();
        let err = store.remove_net(5, &short_budget).unwrap_err();
        assert_eq!(err.status, Status::WorkExhausted);
        assert_eq!(short_budget.used(), 1);
        assert_eq!(store.generated().count(), 12);
        assert!(store.index_consistent());
        drop(_short_scope);

        let full_budget = WorkBudget::new(Limits::default());
        let _full_scope = full_budget.scope();
        store.remove_net(5, &full_budget).unwrap();
        assert_eq!(full_budget.used(), 1 + 1 + mutation);
        assert_eq!(store.generated().count(), 11);
        assert!(store.index_consistent());
    }

    #[test]
    fn store_visit_candidates_charges_input_and_generated_lookups_before_access() {
        let source = "b".repeat(64);
        let input = vec![
            Track {
                id: "10000000-0000-4000-8000-000000000001".into(),
                net: 0,
                start: [10, 0],
                end: [20, 0],
                width_nm: 2,
                locked: true,
                layer: "F.Cu".into(),
            },
            Track {
                id: "10000000-0000-4000-8000-000000000002".into(),
                net: 0,
                start: [30, 0],
                end: [40, 0],
                width_nm: 2,
                locked: true,
                layer: "F.Cu".into(),
            },
            Track {
                id: "10000000-0000-4000-8000-000000000003".into(),
                net: 0,
                start: [500, 500],
                end: [510, 500],
                width_nm: 2,
                locked: true,
                layer: "F.Cu".into(),
            },
        ];
        let mut store = CopperStore::new(input, BTreeSet::new(), &source, 3);
        let gen_work = WorkBudget::new(Limits::default());
        let _gen_scope = gen_work.scope();
        for x in [12, 24, 36, 600] {
            store.insert(dummy_track(1, x), &gen_work).unwrap();
        }
        drop(_gen_scope);

        let swept = SweptAabb::swept([0, 0], [50, 0], 2, 1);
        let input_only = WorkBudget::new(Limits::default());
        let _s1 = input_only.scope();
        let mut input_hits = 0u64;
        assert!(store.input_spatial[0]
            .visit(swept, &input_only, |_| {
                input_hits += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            })
            .unwrap()
            .is_continue());
        assert_eq!(input_hits, 2);
        assert_eq!(input_only.used(), 4);

        let gen_only = WorkBudget::new(Limits::default());
        let _s2 = gen_only.scope();
        let mut gen_hits = 0u64;
        assert!(store.generated_spatial[0]
            .visit(swept, &gen_only, |_, _| {
                gen_hits += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            })
            .unwrap()
            .is_continue());
        assert_eq!(gen_hits, 3);
        assert_eq!(gen_only.used(), 4);

        // Hardcoded total: input_spatial (1 leaf + 3 entries + 2 hits = 6) +
        // generated_spatial (4 treap nodes + 3 hits = 7) = 13.
        let total_needed: u64 = 13;
        for limit in 0..=total_needed {
            let budget = WorkBudget::new(Limits {
                max_work: limit,
                ..Limits::default()
            });
            let _s = budget.scope();
            let mut visited = 0u64;
            let res = store.visit_candidates(swept, &budget, |_| {
                visited += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            });
            if limit < total_needed {
                let err = res.unwrap_err();
                assert_eq!(err.status, Status::WorkExhausted);
                assert_eq!(err.work_used, limit);
                assert_eq!(budget.used(), limit);
                assert!(visited < 5);
            } else {
                assert!(res.unwrap().is_continue());
                assert_eq!(visited, 5);
                assert_eq!(budget.used(), total_needed);
            }
        }

        // Pruned treap subtree check on [0, 0] -> [20, 0] (swept [-2, -2] .. [22, 2]):
        // seq=4 (right child of root seq=2, envelope [35..606]) is pruned before visiting seq=3.
        let pruned_swept = SweptAabb::swept([0, 0], [20, 0], 2, 1);
        let gen_pruned_only = WorkBudget::new(Limits::default());
        let _s3 = gen_pruned_only.scope();
        let mut gen_pruned_hits = 0u64;
        assert!(store.generated_spatial[0]
            .visit(pruned_swept, &gen_pruned_only, |_, _| {
                gen_pruned_hits += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            })
            .unwrap()
            .is_continue());
        assert_eq!(gen_pruned_hits, 1);
        assert_eq!(gen_pruned_only.used(), 3);

        // input_spatial (1 leaf + 3 entries + 1 hit = 5) + generated_spatial (3 nodes + 1 hit = 4) = 9.
        let pruned_total: u64 = 9;
        for limit in 0..=pruned_total {
            let budget = WorkBudget::new(Limits {
                max_work: limit,
                ..Limits::default()
            });
            let _s = budget.scope();
            let mut visited = 0u64;
            let res = store.visit_candidates_filtered(pruned_swept, None, &budget, |_| {
                visited += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            });
            if limit < pruned_total {
                assert_eq!(res.unwrap_err().status, Status::WorkExhausted);
                assert_eq!(budget.used(), limit);
                if limit < 8 {
                    assert!(visited < 2);
                }
            } else {
                assert!(res.unwrap().is_continue());
                assert_eq!(visited, 2);
                assert_eq!(budget.used(), pruned_total);
            }
        }

        // Uniform net=1 generated treap (4 <= TREAP_UNIFORM_SKIP_CAP): root seq=2
        // overlaps pruned_swept and bulk-charges all 4 nodes (5 input + 4 treap = 9)
        // with step-by-step WorkExhausted fallback across 0..=9.
        let uniform_total: u64 = 9;
        for limit in 0..=uniform_total {
            let same_net_budget = WorkBudget::new(Limits {
                max_work: limit,
                ..Limits::default()
            });
            let _s4 = same_net_budget.scope();
            let mut same_net_visited = 0u64;
            let res =
                store.visit_candidates_filtered(pruned_swept, Some(1), &same_net_budget, |_| {
                    same_net_visited += 1;
                    Ok::<_, Failure>(ControlFlow::Continue(()))
                });
            if limit < uniform_total {
                let err = res.unwrap_err();
                assert_eq!(err.status, Status::WorkExhausted);
                assert_eq!(err.work_used, limit);
                assert_eq!(same_net_budget.used(), limit);
            } else {
                assert!(res.unwrap().is_continue());
                assert_eq!(same_net_visited, 1);
                assert_eq!(same_net_budget.used(), uniform_total);
            }
        }

        // Mixed-net treap: replacing seq=4 (x=600) with net=2 clears subtree_uniform_net
        // at root seq=2, so seq=4 is still pruned before visiting seq=3 (5 input + 3 treap = 8).
        let mut mixed_store = CopperStore::new(store.input().to_vec(), BTreeSet::new(), &source, 3);
        let mix_w = WorkBudget::new(Limits::default());
        let _smix = mix_w.scope();
        for (x, net) in [(12, 1), (24, 1), (36, 1), (600, 2)] {
            mixed_store.insert(dummy_track(net, x), &mix_w).unwrap();
        }
        drop(_smix);
        let mixed_total: u64 = 8;
        for limit in 0..=mixed_total {
            let mixed_budget = WorkBudget::new(Limits {
                max_work: limit,
                ..Limits::default()
            });
            let _sm = mixed_budget.scope();
            let mut mixed_visited = 0u64;
            let res =
                mixed_store.visit_candidates_filtered(pruned_swept, Some(1), &mixed_budget, |_| {
                    mixed_visited += 1;
                    Ok::<_, Failure>(ControlFlow::Continue(()))
                });
            if limit < mixed_total {
                let err = res.unwrap_err();
                assert_eq!(err.status, Status::WorkExhausted);
                assert_eq!(err.work_used, limit);
                assert_eq!(mixed_budget.used(), limit);
            } else {
                assert!(res.unwrap().is_continue());
                assert_eq!(mixed_visited, 1);
                assert_eq!(mixed_budget.used(), mixed_total);
            }
        }

        // Unconnected net=0 generated copper is never treated as same-net even
        // when net_filter is Some(0).
        let mut net0_store = CopperStore::new(Vec::new(), BTreeSet::new(), &source, 0);
        let setup_w = WorkBudget::new(Limits::default());
        let _s5 = setup_w.scope();
        net0_store.insert(dummy_track(0, 10), &setup_w).unwrap();
        drop(_s5);
        let net0_q = WorkBudget::new(Limits::default());
        let _s6 = net0_q.scope();
        let mut net0_hits = 0u64;
        assert!(net0_store
            .visit_candidates_geoms_filtered(pruned_swept, Some(0), &net0_q, |_| {
                net0_hits += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            })
            .unwrap()
            .is_continue());
        assert_eq!(net0_hits, 1);
        assert_eq!(net0_q.used(), 2);
    }

    #[test]
    fn store_input_uniform_net_partial_budgets() {
        let source = "c".repeat(64);
        let input: Vec<Track> = [10i64, 20, 30, 40]
            .into_iter()
            .enumerate()
            .map(|(i, x)| Track {
                id: format!("10000000-0000-4000-8000-{:012x}", i + 1),
                net: 1,
                start: [x, 33],
                end: [x + 5, 33],
                width_nm: 2,
                locked: true,
                layer: "F.Cu".into(),
            })
            .collect();
        let store = CopperStore::new(input, BTreeSet::new(), &source, 4);
        let swept = SweptAabb::swept([10, 30], [45, 30], 2, 1);

        // 1 input_spatial leaf + 4 uniform net=1 entries = 5.
        for limit in 0..=5u64 {
            let budget = WorkBudget::new(Limits {
                max_work: limit,
                ..Limits::default()
            });
            let _s = budget.scope();
            let mut hits = 0u64;
            let res = store.visit_candidates_geoms_filtered(swept, Some(1), &budget, |_| {
                hits += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            });
            if limit < 5 {
                let err = res.unwrap_err();
                assert_eq!(err.status, Status::WorkExhausted);
                assert_eq!(err.work_used, limit);
                assert_eq!(budget.used(), limit);
                assert_eq!(hits, 0);
            } else {
                assert!(res.unwrap().is_continue());
                assert_eq!(hits, 0);
                assert_eq!(budget.used(), 5);
            }
        }

        // Some(0) and None do not take uniform_net skip: 1 leaf + 4 entries + 4 hits = 9.
        let b0 = WorkBudget::new(Limits::default());
        let _s0 = b0.scope();
        let mut hits0 = 0u64;
        assert!(store
            .visit_candidates_geoms_filtered(swept, Some(0), &b0, |_| {
                hits0 += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            })
            .unwrap()
            .is_continue());
        assert_eq!(hits0, 4);
        assert_eq!(b0.used(), 9);
        drop(_s0);

        let b_none = WorkBudget::new(Limits::default());
        let _sn = b_none.scope();
        let tracks = store.candidates(swept, &b_none).unwrap();
        assert_eq!(tracks.len(), 4);
        assert_eq!(b_none.used(), 9);
    }

    #[test]
    fn store_generated_uniform_net_partial_budgets_and_rollback() {
        let source = "d".repeat(64);
        let mut store = CopperStore::new(Vec::new(), BTreeSet::new(), &source, 0);
        let setup = WorkBudget::new(Limits::default());
        let _ss = setup.scope();
        for x in [10, 20, 30, 40] {
            store.insert(dummy_track(1, x), &setup).unwrap();
        }
        drop(_ss);

        let swept = SweptAabb::swept([0, 0], [50, 0], 2, 1);
        // 4 generated treap nodes all net=1 (<= TREAP_UNIFORM_SKIP_CAP): total = 4.
        for limit in 0..=4u64 {
            let budget = WorkBudget::new(Limits {
                max_work: limit,
                ..Limits::default()
            });
            let _s = budget.scope();
            let mut hits = 0u64;
            let res = store.visit_candidates_geoms_filtered(swept, Some(1), &budget, |_| {
                hits += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            });
            if limit < 4 {
                let err = res.unwrap_err();
                assert_eq!(err.status, Status::WorkExhausted);
                assert_eq!(err.work_used, limit);
                assert_eq!(budget.used(), limit);
                assert_eq!(hits, 0);
            } else {
                assert!(res.unwrap().is_continue());
                assert_eq!(hits, 0);
                assert_eq!(budget.used(), 4);
            }
        }

        // net_filter = None visits all 4 generated tracks: 4 nodes + 4 slot lookups = 8.
        let b_all = WorkBudget::new(Limits::default());
        let _sa = b_all.scope();
        let tracks = store.candidates(swept, &b_all).unwrap();
        assert_eq!(tracks.len(), 4);
        assert_eq!(b_all.used(), 8);
        drop(_sa);

        // 4 generated net=0 tracks queried with Some(0): never skipped (4 + 4 = 8).
        let mut store0 = CopperStore::new(Vec::new(), BTreeSet::new(), &source, 0);
        let s0_w = WorkBudget::new(Limits::default());
        let _ss0 = s0_w.scope();
        for x in [10, 20, 30, 40] {
            store0.insert(dummy_track(0, x), &s0_w).unwrap();
        }
        drop(_ss0);
        let q0 = WorkBudget::new(Limits::default());
        let _sq0 = q0.scope();
        let mut hits0 = 0u64;
        assert!(store0
            .visit_candidates_geoms_filtered(swept, Some(0), &q0, |_| {
                hits0 += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            })
            .unwrap()
            .is_continue());
        assert_eq!(hits0, 4);
        assert_eq!(q0.used(), 8);
        drop(_sq0);

        // Rollback metadata restoration checks on generated_spatial.
        let mut rb_store = CopperStore::new(Vec::new(), BTreeSet::new(), &source, 0);
        let rb_w = WorkBudget::new(Limits::default());
        let _srb = rb_w.scope();
        for x in [10, 20, 30] {
            rb_store.insert(dummy_track(1, x), &rb_w).unwrap();
        }
        // begin -> insert(net=2) -> rollback() -> index_consistent(), Some(1) gives 0 hits & 3 work.
        rb_store.begin(&rb_w).unwrap();
        rb_store.insert(dummy_track(2, 40), &rb_w).unwrap();
        rb_store.rollback();
        assert!(rb_store.index_consistent());
        drop(_srb);

        let check1 = WorkBudget::new(Limits::default());
        let _sc1 = check1.scope();
        let mut h1 = 0u64;
        assert!(rb_store
            .visit_candidates_geoms_filtered(swept, Some(1), &check1, |_| {
                h1 += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            })
            .unwrap()
            .is_continue());
        assert_eq!(h1, 0);
        assert_eq!(check1.used(), 3);
        drop(_sc1);

        // begin -> remove_net(1) -> rollback() -> index_consistent(), Some(1) gives 0 hits & 3 work.
        let rb_w2 = WorkBudget::new(Limits::default());
        let _srb2 = rb_w2.scope();
        rb_store.begin(&rb_w2).unwrap();
        rb_store.remove_net(1, &rb_w2).unwrap();
        rb_store.rollback();
        assert!(rb_store.index_consistent());
        drop(_srb2);

        let check2 = WorkBudget::new(Limits::default());
        let _sc2 = check2.scope();
        let mut h2 = 0u64;
        assert!(rb_store
            .visit_candidates_geoms_filtered(swept, Some(1), &check2, |_| {
                h2 += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            })
            .unwrap()
            .is_continue());
        assert_eq!(h2, 0);
        assert_eq!(check2.used(), 3);
        drop(_sc2);

        // Mixed (net=1, net=2) -> outer begin -> inner begin -> remove_net(2) -> commit inner -> rollback outer.
        let mut mix_rb = CopperStore::new(Vec::new(), BTreeSet::new(), &source, 0);
        let mw = WorkBudget::new(Limits::default());
        let _smw = mw.scope();
        mix_rb.insert(dummy_track(1, 10), &mw).unwrap();
        mix_rb.insert(dummy_track(2, 20), &mw).unwrap();
        mix_rb.begin(&mw).unwrap();
        mix_rb.begin(&mw).unwrap();
        mix_rb.remove_net(2, &mw).unwrap();
        mix_rb.commit();
        mix_rb.rollback();
        assert!(mix_rb.index_consistent());
        drop(_smw);

        let check3 = WorkBudget::new(Limits::default());
        let _sc3 = check3.scope();
        let mut h3 = 0u64;
        assert!(mix_rb
            .visit_candidates_geoms_filtered(swept, Some(1), &check3, |_| {
                h3 += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            })
            .unwrap()
            .is_continue());
        assert_eq!(h3, 1);
        assert_eq!(check3.used(), 3);
    }
}
