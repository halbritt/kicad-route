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
    generated_spatial: SpatialIndex<u64, u32>,
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
                    (seq, slot, t.net, CopperAabb::from_bounds(t.copper_bounds()))
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
            .visit_matching(bounds, net_filter, work, |_seq, &slot| {
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
            .visit_matching(bounds, net_filter, work, |_seq, &slot| {
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
            assert_eq!(
                mutation,
                8 * (u64::from(store.generated_spatial.height()) + 1)
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
        assert_eq!(store.generated_spatial.height(), 8);

        let mutation = store.generated_spatial.mutation_work();
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
