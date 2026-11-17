use crate::model::Track;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug)]
enum Undo {
    Inserted(String),
    Removed(Track),
}
#[derive(Debug)]
struct Frame {
    journal: usize,
    next: u64,
}
/// Input copper has no mutation/removal API. The net index is derived only
/// from generated copper and is maintained by the same owning store.
#[derive(Debug)]
pub struct CopperStore {
    input: Vec<Track>,
    generated: BTreeMap<String, Track>,
    by_net: BTreeMap<u32, BTreeSet<String>>,
    journal: Vec<Undo>,
    frames: Vec<Frame>,
    next: u64,
    reserved: BTreeSet<String>,
    prefix: String,
}
impl CopperStore {
    pub(crate) fn new(input: Vec<Track>, reserved: BTreeSet<String>, source: &str) -> Self {
        // Deterministic custom UUID (version 8), source prefix plus counter.
        // All input pad/track IDs are reserved; collisions advance the counter.
        let prefix = format!(
            "{}-{}-8{}-8{}-",
            &source[..8],
            &source[8..12],
            &source[12..15],
            &source[15..18]
        );
        Self {
            input,
            generated: BTreeMap::new(),
            by_net: BTreeMap::new(),
            journal: Vec::new(),
            frames: Vec::new(),
            next: 1,
            reserved,
            prefix,
        }
    }
    pub fn tracks(&self) -> impl Iterator<Item = &Track> {
        self.input.iter().chain(self.generated.values())
    }
    pub fn generated(&self) -> impl Iterator<Item = &Track> {
        self.generated.values()
    }
    pub fn input(&self) -> &[Track] {
        &self.input
    }
    pub fn index_consistent(&self) -> bool {
        let mut expected: BTreeMap<u32, BTreeSet<String>> = BTreeMap::new();
        for t in self.generated.values() {
            expected.entry(t.net).or_default().insert(t.id.clone());
        }
        expected == self.by_net
    }
    // Only the private Engine scope guard opens/closes frames. No checkpoint
    // token can escape, be copied, or be applied to another store.
    pub(crate) fn begin(&mut self) {
        self.frames.push(Frame {
            journal: self.journal.len(),
            next: self.next,
        });
    }
    fn add_raw(&mut self, t: Track) {
        self.by_net.entry(t.net).or_default().insert(t.id.clone());
        assert!(self.generated.insert(t.id.clone(), t).is_none());
    }
    fn remove_raw(&mut self, id: &str) -> Track {
        let t = self
            .generated
            .remove(id)
            .expect("journal/index references owned generated copper");
        let ids = self.by_net.get_mut(&t.net).unwrap();
        ids.remove(id);
        if ids.is_empty() {
            self.by_net.remove(&t.net);
        }
        t
    }
    pub(crate) fn insert(&mut self, mut t: Track) {
        loop {
            assert!(
                self.next < (1u64 << 48),
                "bounded search cannot exhaust ID space"
            );
            let id = format!("{}{:012x}", self.prefix, self.next);
            self.next = self.next.checked_add(1).expect("bounded ID counter");
            if !self.reserved.contains(&id) && !self.generated.contains_key(&id) {
                t.id = id;
                break;
            }
        }
        self.journal.push(Undo::Inserted(t.id.clone()));
        self.add_raw(t);
    }
    pub(crate) fn remove_net(&mut self, net: u32) {
        let ids: Vec<_> = self
            .by_net
            .get(&net)
            .into_iter()
            .flatten()
            .cloned()
            .collect();
        for id in ids {
            let t = self.remove_raw(&id);
            self.journal.push(Undo::Removed(t));
        }
    }
    pub(crate) fn rollback(&mut self) {
        let c = self.frames.pop().expect("scope owns an open transaction");
        while self.journal.len() > c.journal {
            match self.journal.pop().unwrap() {
                Undo::Inserted(id) => {
                    self.remove_raw(&id);
                }
                Undo::Removed(t) => self.add_raw(t),
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
