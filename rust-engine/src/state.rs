use crate::{
    budget::WorkBudget,
    model::{Failure, Status, Track},
    spatial::{CopperAabb, SpatialIndex, StaticSpatialIndex, SweptAabb, TrackGeom},
};
use std::{
    collections::{BTreeMap, BTreeSet},
    ops::ControlFlow,
};

#[derive(Debug)]
enum Undo {
    Inserted(u64),
    Removed(u64, Track),
}

#[derive(Debug)]
struct Frame {
    journal: usize,
    next: u64,
}

/// Input copper has no mutation/removal API. The net and spatial indexes are
/// derived only from owned copper and are maintained by the same owning store.
#[derive(Debug)]
pub struct CopperStore {
    input: Vec<Track>,
    slots: Vec<Option<Track>>,
    free_slots: Vec<u32>,
    by_seq: BTreeMap<u64, u32>,
    by_net: BTreeMap<u32, BTreeSet<u64>>,
    input_spatial: StaticSpatialIndex<TrackGeom>,
    generated_spatial: SpatialIndex<u64, (u32, u32)>,
    journal: Vec<Undo>,
    frames: Vec<Frame>,
    next: u64,
    reserved: BTreeSet<String>,
    prefix: String,
    base_items: usize,
}

impl CopperStore {
    pub(crate) fn new(
        input: Vec<Track>,
        reserved: BTreeSet<String>,
        source: &str,
        base_items: usize,
    ) -> Self {
        // Deterministic custom UUID (version 8), source prefix plus counter.
        // All input pad/track IDs are reserved; collisions advance the counter.
        let prefix = format!(
            "{}-{}-8{}-8{}-",
            &source[..8],
            &source[8..12],
            &source[12..15],
            &source[15..18]
        );
        let input_spatial = StaticSpatialIndex::new(input.iter().enumerate().map(|(i, track)| {
            (
                u32::try_from(i).expect("bounded input track count"),
                track.net,
                CopperAabb::from_bounds(track.copper_bounds()),
                TrackGeom {
                    start: track.start,
                    end: track.end,
                    width_nm: track.width_nm,
                },
            )
        }));
        Self {
            input,
            slots: Vec::new(),
            free_slots: Vec::new(),
            by_seq: BTreeMap::new(),
            by_net: BTreeMap::new(),
            input_spatial,
            generated_spatial: SpatialIndex::new(),
            journal: Vec::new(),
            frames: Vec::new(),
            next: 1,
            reserved,
            prefix,
            base_items,
        }
    }

    pub fn tracks(&self) -> impl Iterator<Item = &Track> {
        self.input.iter().chain(self.generated())
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
        live_slots.len() + free_set.len() == self.slots.len()
            && live_slots.is_disjoint(&free_set)
            && expected_net == self.by_net
            && self
                .input_spatial
                .consistent(self.input.iter().enumerate().map(|(i, t)| {
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
                }))
            && self
                .generated_spatial
                .consistent(self.by_seq.iter().map(|(&seq, &slot)| {
                    let t = self.slots[slot as usize].as_ref().unwrap();
                    (
                        seq,
                        (slot, t.net),
                        CopperAabb::from_bounds(t.copper_bounds()),
                    )
                }))
    }

    #[inline(always)]
    pub(crate) fn visit_candidates_geoms_filtered(
        &self,
        bounds: SweptAabb,
        net_filter: Option<u32>,
        work: &WorkBudget,
        mut visitor: impl FnMut(TrackGeom) -> Result<ControlFlow<()>, Failure>,
    ) -> Result<ControlFlow<()>, Failure> {
        let flow =
            self.input_spatial
                .visit_matching(bounds, net_filter, work, |_ordinal, geom| {
                    work.charge(1)?;
                    visitor(*geom)
                })?;
        if flow.is_break() {
            return Ok(ControlFlow::Break(()));
        }
        self.generated_spatial
            .visit(bounds, work, |_seq, &(slot, net)| {
                if net_filter.is_some_and(|filter| net != 0 && net == filter) {
                    return Ok(ControlFlow::Continue(()));
                }
                work.charge(1)?;
                let track = self.slots[slot as usize]
                    .as_ref()
                    .expect("spatial slot references owned copper");
                visitor(TrackGeom {
                    start: track.start,
                    end: track.end,
                    width_nm: track.width_nm,
                })
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
            self.input_spatial
                .visit_matching(bounds, net_filter, work, |ordinal, _geom| {
                    work.charge(1)?;
                    visitor(&self.input[ordinal as usize])
                })?;
        if flow.is_break() {
            return Ok(ControlFlow::Break(()));
        }
        self.generated_spatial
            .visit(bounds, work, |_seq, &(slot, net)| {
                if net_filter.is_some_and(|filter| net != 0 && net == filter) {
                    return Ok(ControlFlow::Continue(()));
                }
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
        self.generated_spatial.insert(
            seq,
            (slot, track.net),
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
        self.generated_spatial
            .remove(&seq, CopperAabb::from_bounds(t.copper_bounds()));
        let seqs = self.by_net.get_mut(&t.net).unwrap();
        assert!(seqs.remove(&seq));
        if seqs.is_empty() {
            self.by_net.remove(&t.net);
        }
        t
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

    pub(crate) fn insert(&mut self, mut t: Track, work: &WorkBudget) -> Result<(), Failure> {
        self.admit_journal(1, work)?;
        if self.base_items + self.by_seq.len() >= work.limits.max_items {
            return Err(work.failure(Status::AdmissionExceeded, "copper item limit exceeded"));
        }
        work.charge(1)?;
        let seq = loop {
            work.charge(1)?;
            assert!(
                self.next < (1u64 << 48),
                "bounded search cannot exhaust ID space"
            );
            let seq = self.next;
            let id = format!("{}{:012x}", self.prefix, seq);
            self.next = self.next.checked_add(1).expect("bounded ID counter");
            if !self.reserved.contains(&id) && !self.by_seq.contains_key(&seq) {
                t.id = id;
                break seq;
            }
        };
        work.charge(self.generated_spatial.mutation_work())?;
        self.journal.push(Undo::Inserted(seq));
        self.add_raw(seq, t);
        Ok(())
    }

    pub(crate) fn remove_net(&mut self, net: u32, work: &WorkBudget) -> Result<(), Failure> {
        let count = self.by_net.get(&net).map_or(0, BTreeSet::len);
        self.admit_journal(count, work)?;
        work.charge(count as u64)?;
        let seqs: Vec<u64> = self
            .by_net
            .get(&net)
            .into_iter()
            .flatten()
            .copied()
            .collect();
        for seq in seqs {
            work.charge(1 + self.generated_spatial.mutation_work())?;
            let t = self.remove_raw(seq);
            self.journal.push(Undo::Removed(seq, t));
        }
        Ok(())
    }

    pub(crate) fn rollback(&mut self) {
        let c = self.frames.pop().expect("scope owns an open transaction");
        while self.journal.len() > c.journal {
            match self.journal.pop().unwrap() {
                Undo::Inserted(seq) => {
                    self.remove_raw(seq);
                }
                Undo::Removed(seq, t) => self.add_raw(seq, t),
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
        }
    }

    #[test]
    fn store_insert_and_remove_net_charge_spatial_mutation_work_before_edit() {
        let source = "a".repeat(64);
        let mut store = CopperStore::new(Vec::new(), BTreeSet::new(), &source, 0);
        for step in 1..=12u32 {
            let mut work = WorkBudget::new(Limits::default());
            let mut scope = work.scope();
            let mutation = store.generated_spatial.mutation_work();
            assert!(mutation >= 8);
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
        assert!(store.generated_spatial.height() >= 3);

        let mutation = store.generated_spatial.mutation_work();
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
            },
            Track {
                id: "10000000-0000-4000-8000-000000000002".into(),
                net: 0,
                start: [30, 0],
                end: [40, 0],
                width_nm: 2,
                locked: true,
            },
            Track {
                id: "10000000-0000-4000-8000-000000000003".into(),
                net: 0,
                start: [500, 500],
                end: [510, 500],
                width_nm: 2,
                locked: true,
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
        assert!(store
            .input_spatial
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
        assert!(store
            .generated_spatial
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
                assert_eq!(res.unwrap_err().status, Status::WorkExhausted);
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
        assert!(store
            .generated_spatial
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

        // Same-net filter (net = 1) skips generated slot lookup (4 -> 3) while
        // still charging the pruned seq=4 node and retaining input net=0 hit.
        let same_net_budget = WorkBudget::new(Limits::default());
        let _s4 = same_net_budget.scope();
        let mut same_net_visited = 0u64;
        assert!(store
            .visit_candidates_filtered(pruned_swept, Some(1), &same_net_budget, |_| {
                same_net_visited += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            })
            .unwrap()
            .is_continue());
        assert_eq!(same_net_visited, 1);
        assert_eq!(same_net_budget.used(), 8);

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
}
