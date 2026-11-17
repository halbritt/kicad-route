//! Conservative broad phase. Static copper uses a flat median-balanced BST;
//! dynamic generated copper uses a history-independent deterministic treap.
//! Exact geometry remains the caller's sole clearance authority.
use crate::{
    budget::WorkBudget,
    model::{Failure, Point},
};
use std::{
    cmp::Ordering, collections::BTreeMap, collections::BTreeSet, num::NonZeroU32, ops::ControlFlow,
};

const NONE_U32: u32 = u32::MAX;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct CopperAabb {
    pub min: [i64; 2],
    pub max: [i64; 2],
}

impl CopperAabb {
    pub fn from_bounds((min, max): (Point, Point)) -> Self {
        Self { min, max }
    }

    pub fn union(self, other: Self) -> Self {
        Self {
            min: [self.min[0].min(other.min[0]), self.min[1].min(other.min[1])],
            max: [self.max[0].max(other.max[0]), self.max[1].max(other.max[1])],
        }
    }

    #[cfg(test)]
    #[inline(always)]
    pub fn overlaps_swept(self, other: SweptAabb) -> bool {
        // Inclusive envelopes preserve exact tangency candidates. Only the
        // exact predicate may decide that equality satisfies clearance.
        (0..2).all(|k| {
            i128::from(self.min[k]) <= other.max[k] && other.min[k] <= i128::from(self.max[k])
        })
    }

    #[inline(always)]
    pub fn overlaps(self, other: Self) -> bool {
        (self.min[0] <= other.max[0])
            & (other.min[0] <= self.max[0])
            & (self.min[1] <= other.max[1])
            & (other.min[1] <= self.max[1])
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct SweptAabb {
    pub min: [i128; 2],
    pub max: [i128; 2],
}

impl SweptAabb {
    pub fn swept(a: Point, b: Point, width: i64, clearance: i64) -> Self {
        let radius = (i128::from(width) + 1) / 2 + i128::from(clearance);
        Self {
            min: std::array::from_fn(|k| i128::from(a[k].min(b[k])) - radius),
            max: std::array::from_fn(|k| i128::from(a[k].max(b[k])) + radius),
        }
    }

    #[inline(always)]
    pub fn clamp_i64(self) -> Option<CopperAabb> {
        let lo = i128::from(i64::MIN);
        let hi = i128::from(i64::MAX);
        if self.max[0] < lo || self.min[0] > hi || self.max[1] < lo || self.min[1] > hi {
            return None;
        }
        Some(CopperAabb {
            min: [self.min[0].max(lo) as i64, self.min[1].max(lo) as i64],
            max: [self.max[0].min(hi) as i64, self.max[1].min(hi) as i64],
        })
    }

    #[cfg(test)]
    pub fn from_copper(copper: CopperAabb) -> Self {
        Self {
            min: copper.min.map(i128::from),
            max: copper.max.map(i128::from),
        }
    }

    #[cfg(test)]
    pub fn overlaps(self, other: Self) -> bool {
        (0..2).all(|k| self.min[k] <= other.max[k] && other.min[k] <= self.max[k])
    }
}

#[derive(Clone, Copy, Debug)]
struct FlatNode {
    copper: CopperAabb,
    envelope: CopperAabb,
    ordinal: u32,
    left: u32,
    right: u32,
}

const _: () = assert!(std::mem::size_of::<FlatNode>() <= 80);

#[derive(Debug)]
pub(crate) struct StaticSpatialIndex {
    nodes: Vec<FlatNode>,
    root: u32,
}

impl StaticSpatialIndex {
    pub fn new(items: impl IntoIterator<Item = (u32, CopperAabb)>) -> Self {
        let mut entries: Vec<(u32, CopperAabb)> = items.into_iter().collect();
        entries.sort_unstable_by(|a, b| (a.1.min[0], a.0).cmp(&(b.1.min[0], b.0)));
        assert!(
            entries
                .windows(2)
                .all(|w| (w[0].1.min[0], w[0].0) < (w[1].1.min[0], w[1].0)),
            "static spatial keys must be unique"
        );
        let mut nodes = Vec::with_capacity(entries.len());
        fn build(slice: &[(u32, CopperAabb)], nodes: &mut Vec<FlatNode>) -> u32 {
            if slice.is_empty() {
                return NONE_U32;
            }
            let mid = slice.len() / 2;
            let (ordinal, copper) = slice[mid];
            let idx = u32::try_from(nodes.len()).expect("bounded spatial node arena");
            nodes.push(FlatNode {
                copper,
                envelope: copper,
                ordinal,
                left: NONE_U32,
                right: NONE_U32,
            });
            let left = build(&slice[..mid], nodes);
            let right = build(&slice[mid + 1..], nodes);
            let mut envelope = copper;
            if left != NONE_U32 {
                envelope = envelope.union(nodes[left as usize].envelope);
            }
            if right != NONE_U32 {
                envelope = envelope.union(nodes[right as usize].envelope);
            }
            nodes[idx as usize].left = left;
            nodes[idx as usize].right = right;
            nodes[idx as usize].envelope = envelope;
            idx
        }
        let root = build(&entries, &mut nodes);
        Self { nodes, root }
    }

    pub fn visit(
        &self,
        bounds: SweptAabb,
        work: &WorkBudget,
        mut visitor: impl FnMut(u32) -> Result<ControlFlow<()>, Failure>,
    ) -> Result<ControlFlow<()>, Failure> {
        if self.root == NONE_U32 {
            return Ok(ControlFlow::Continue(()));
        }
        let Some(q) = bounds.clamp_i64() else {
            work.charge(1)?;
            return Ok(ControlFlow::Continue(()));
        };
        let mut stack = [NONE_U32; 64];
        let mut sp = 0usize;
        let mut idx = self.root;
        loop {
            // One visit covers the subtree and item AABB tests, including pruned
            // nodes. No uncharged linear scan precedes the exact predicates.
            work.charge(1)?;
            let node = &self.nodes[idx as usize];
            if node.envelope.overlaps(q) {
                if node.copper.overlaps(q) && visitor(node.ordinal)?.is_break() {
                    return Ok(ControlFlow::Break(()));
                }
                if node.left != NONE_U32 {
                    if node.right != NONE_U32 {
                        stack[sp] = node.right;
                        sp += 1;
                    }
                    idx = node.left;
                    continue;
                }
                if node.right != NONE_U32 {
                    idx = node.right;
                    continue;
                }
            }
            if sp == 0 {
                break;
            }
            sp -= 1;
            idx = stack[sp];
        }
        Ok(ControlFlow::Continue(()))
    }

    #[cfg(test)]
    pub fn query(&self, bounds: SweptAabb, work: &WorkBudget) -> Result<Vec<u32>, Failure> {
        let mut found = Vec::new();
        let _ = self.visit(bounds, work, |ordinal| {
            found.push(ordinal);
            Ok::<_, Failure>(ControlFlow::Continue(()))
        })?;
        found.sort_unstable();
        Ok(found)
    }

    pub fn consistent(&self, expected: impl IntoIterator<Item = (u32, CopperAabb)>) -> bool {
        let expected: BTreeMap<u32, CopperAabb> = expected.into_iter().collect();
        fn inspect(
            nodes: &[FlatNode],
            idx: u32,
            lo: Option<(i64, u32)>,
            hi: Option<(i64, u32)>,
            visited: &mut BTreeSet<u32>,
            actual: &mut BTreeMap<u32, CopperAabb>,
        ) -> Option<Option<CopperAabb>> {
            if idx == NONE_U32 {
                return Some(None);
            }
            if !visited.insert(idx) {
                return None;
            }
            let n = nodes.get(idx as usize)?;
            let n_ord = (n.copper.min[0], n.ordinal);
            if !lo.is_none_or(|b| b < n_ord) || !hi.is_none_or(|b| n_ord < b) {
                return None;
            }
            let lb = inspect(nodes, n.left, lo, Some(n_ord), visited, actual)?;
            let rb = inspect(nodes, n.right, Some(n_ord), hi, visited, actual)?;
            let mut envelope = n.copper;
            for child in [lb, rb].into_iter().flatten() {
                envelope = envelope.union(child);
            }
            if n.envelope != envelope || actual.insert(n.ordinal, n.copper).is_some() {
                return None;
            }
            Some(Some(envelope))
        }
        let mut visited = BTreeSet::new();
        let mut actual = BTreeMap::new();
        if inspect(
            &self.nodes,
            self.root,
            None,
            None,
            &mut visited,
            &mut actual,
        )
        .is_none()
        {
            return false;
        }
        visited.len() == self.nodes.len() && actual == expected
    }
}

pub(crate) trait SpatialKey: Ord + Clone {
    fn priority(&self) -> u64;
}

fn splitmix64(mut x: u64) -> u64 {
    x = x.wrapping_add(0x9e37_79b9_7f4a_7c15);
    x = (x ^ (x >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    x = (x ^ (x >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    x ^ (x >> 31)
}

impl SpatialKey for u64 {
    fn priority(&self) -> u64 {
        splitmix64(*self)
    }
}

impl SpatialKey for usize {
    fn priority(&self) -> u64 {
        splitmix64(*self as u64)
    }
}

impl SpatialKey for String {
    fn priority(&self) -> u64 {
        let mut h = 0xcbf2_9ce4_8422_2325u64;
        for &b in self.as_bytes() {
            h ^= u64::from(b);
            h = h.wrapping_mul(0x0100_0000_01b3);
        }
        splitmix64(h)
    }
}

#[derive(Clone, Debug)]
struct TreapNode<K, V> {
    copper: CopperAabb,
    envelope: CopperAabb,
    key: K,
    priority: u64,
    value: V,
    height: NonZeroU32,
    left: u32,
    right: u32,
}

const _: () = assert!(std::mem::size_of::<TreapNode<u64, u32>>() <= 96);
const _: () = assert!(std::mem::size_of::<Option<TreapNode<u64, u32>>>() <= 96);

#[derive(Debug)]
pub(crate) struct SpatialIndex<K, V> {
    nodes: Vec<Option<TreapNode<K, V>>>,
    free: Vec<u32>,
    root: u32,
}

impl<K: SpatialKey, V> SpatialIndex<K, V> {
    pub fn new() -> Self {
        Self {
            nodes: Vec::new(),
            free: Vec::new(),
            root: NONE_U32,
        }
    }

    fn node(&self, idx: u32) -> &TreapNode<K, V> {
        self.nodes[idx as usize]
            .as_ref()
            .expect("spatial treap index references live node")
    }

    fn node_mut(&mut self, idx: u32) -> &mut TreapNode<K, V> {
        self.nodes[idx as usize]
            .as_mut()
            .expect("spatial treap index references live node")
    }

    fn alloc_node(&mut self, node: TreapNode<K, V>) -> u32 {
        if let Some(idx) = self.free.pop() {
            assert!(self.nodes[idx as usize].is_none());
            self.nodes[idx as usize] = Some(node);
            idx
        } else {
            let idx = u32::try_from(self.nodes.len()).expect("bounded spatial node arena");
            self.nodes.push(Some(node));
            idx
        }
    }

    fn free_node(&mut self, idx: u32) -> TreapNode<K, V> {
        let node = self.nodes[idx as usize]
            .take()
            .expect("spatial treap free references live node");
        self.free.push(idx);
        node
    }

    fn link_height(&self, idx: u32) -> u32 {
        if idx == NONE_U32 {
            0
        } else {
            self.node(idx).height.get()
        }
    }

    pub fn height(&self) -> u32 {
        self.link_height(self.root)
    }

    // This upper bound pays for descent and rotations along at most one
    // root-to-leaf spine before a forward mutation. Undo uses the same
    // infallible edit without consuming exhausted forward work.
    pub fn mutation_work(&self) -> u64 {
        8 * (u64::from(self.height()) + 1)
    }

    fn refresh(&mut self, idx: u32) {
        let (copper, left, right) = {
            let n = self.node(idx);
            (n.copper, n.left, n.right)
        };
        let lh = self.link_height(left);
        let rh = self.link_height(right);
        let mut envelope = copper;
        if left != NONE_U32 {
            envelope = envelope.union(self.node(left).envelope);
        }
        if right != NONE_U32 {
            envelope = envelope.union(self.node(right).envelope);
        }
        let n = self.node_mut(idx);
        n.height = NonZeroU32::new(1 + lh.max(rh)).expect("treap height >= 1");
        n.envelope = envelope;
    }

    fn heap_gt(&self, a: u32, b: u32) -> bool {
        let na = self.node(a);
        let nb = self.node(b);
        (na.priority, na.copper.min[0], &na.key) > (nb.priority, nb.copper.min[0], &nb.key)
    }

    fn rotate_left(&mut self, cur: u32) -> u32 {
        let parent = self.node(cur).right;
        assert!(parent != NONE_U32, "left rotation requires right child");
        let parent_left = self.node(parent).left;
        self.node_mut(cur).right = parent_left;
        self.refresh(cur);
        self.node_mut(parent).left = cur;
        self.refresh(parent);
        parent
    }

    fn rotate_right(&mut self, cur: u32) -> u32 {
        let parent = self.node(cur).left;
        assert!(parent != NONE_U32, "right rotation requires left child");
        let parent_right = self.node(parent).right;
        self.node_mut(cur).left = parent_right;
        self.refresh(cur);
        self.node_mut(parent).right = cur;
        self.refresh(parent);
        parent
    }

    fn insert_at(&mut self, link: u32, idx: u32) -> u32 {
        if link == NONE_U32 {
            return idx;
        }
        let ord = {
            let cur = self.node(link);
            let new = self.node(idx);
            (new.copper.min[0], &new.key).cmp(&(cur.copper.min[0], &cur.key))
        };
        match ord {
            Ordering::Less => {
                let left = self.node(link).left;
                let new_left = self.insert_at(left, idx);
                self.node_mut(link).left = new_left;
                if self.heap_gt(new_left, link) {
                    self.rotate_right(link)
                } else {
                    self.refresh(link);
                    link
                }
            }
            Ordering::Greater => {
                let right = self.node(link).right;
                let new_right = self.insert_at(right, idx);
                self.node_mut(link).right = new_right;
                if self.heap_gt(new_right, link) {
                    self.rotate_left(link)
                } else {
                    self.refresh(link);
                    link
                }
            }
            Ordering::Equal => panic!("spatial keys are uniquely owned"),
        }
    }

    pub fn insert(&mut self, key: K, value: V, bounds: CopperAabb) {
        let priority = key.priority();
        let idx = self.alloc_node(TreapNode {
            copper: bounds,
            envelope: bounds,
            key,
            priority,
            value,
            height: NonZeroU32::new(1).expect("height >= 1"),
            left: NONE_U32,
            right: NONE_U32,
        });
        self.root = self.insert_at(self.root, idx);
    }

    fn remove_root(&mut self, cur: u32) -> u32 {
        let (left, right) = {
            let n = self.node(cur);
            (n.left, n.right)
        };
        if left == NONE_U32 {
            return right;
        }
        if right == NONE_U32 {
            return left;
        }
        if self.heap_gt(left, right) {
            let new_root = self.rotate_right(cur);
            let new_right = self.remove_root(cur);
            self.node_mut(new_root).right = new_right;
            self.refresh(new_root);
            new_root
        } else {
            let new_root = self.rotate_left(cur);
            let new_left = self.remove_root(cur);
            self.node_mut(new_root).left = new_left;
            self.refresh(new_root);
            new_root
        }
    }

    fn remove_at(&mut self, link: u32, key: &K, bounds: CopperAabb) -> (u32, V) {
        assert!(link != NONE_U32, "spatial removal references owned copper");
        let ord = {
            let cur = self.node(link);
            (bounds.min[0], key).cmp(&(cur.copper.min[0], &cur.key))
        };
        match ord {
            Ordering::Less => {
                let left = self.node(link).left;
                let (new_left, value) = self.remove_at(left, key, bounds);
                self.node_mut(link).left = new_left;
                self.refresh(link);
                (link, value)
            }
            Ordering::Greater => {
                let right = self.node(link).right;
                let (new_right, value) = self.remove_at(right, key, bounds);
                self.node_mut(link).right = new_right;
                self.refresh(link);
                (link, value)
            }
            Ordering::Equal => {
                let new_root = self.remove_root(link);
                let removed = self.free_node(link);
                (new_root, removed.value)
            }
        }
    }

    pub fn remove(&mut self, key: &K, bounds: CopperAabb) -> V {
        let (new_root, value) = self.remove_at(self.root, key, bounds);
        self.root = new_root;
        value
    }

    pub fn visit<'a>(
        &'a self,
        bounds: SweptAabb,
        work: &WorkBudget,
        mut visitor: impl FnMut(&'a K, &'a V) -> Result<ControlFlow<()>, Failure>,
    ) -> Result<ControlFlow<()>, Failure> {
        if self.root == NONE_U32 {
            return Ok(ControlFlow::Continue(()));
        }
        let Some(q) = bounds.clamp_i64() else {
            work.charge(1)?;
            return Ok(ControlFlow::Continue(()));
        };
        fn walk<'a, K, V>(
            nodes: &'a [Option<TreapNode<K, V>>],
            idx: u32,
            bounds: CopperAabb,
            work: &WorkBudget,
            visitor: &mut impl FnMut(&'a K, &'a V) -> Result<ControlFlow<()>, Failure>,
        ) -> Result<ControlFlow<()>, Failure> {
            if idx == NONE_U32 {
                return Ok(ControlFlow::Continue(()));
            }
            // One visit covers the subtree and item AABB tests, including pruned
            // nodes. No uncharged linear scan precedes the exact predicates.
            work.charge(1)?;
            let node = nodes[idx as usize]
                .as_ref()
                .expect("spatial treap index references live node");
            if !node.envelope.overlaps(bounds) {
                return Ok(ControlFlow::Continue(()));
            }
            if node.copper.overlaps(bounds) && visitor(&node.key, &node.value)?.is_break() {
                return Ok(ControlFlow::Break(()));
            }
            if walk(nodes, node.left, bounds, work, visitor)?.is_break() {
                return Ok(ControlFlow::Break(()));
            }
            walk(nodes, node.right, bounds, work, visitor)
        }
        walk(&self.nodes, self.root, q, work, &mut visitor)
    }

    #[cfg(test)]
    pub fn query(&self, bounds: SweptAabb, work: &WorkBudget) -> Result<Vec<&K>, Failure> {
        let mut found = Vec::new();
        let _ = self.visit(bounds, work, |key, _| {
            found.push(key);
            Ok::<_, Failure>(ControlFlow::Continue(()))
        })?;
        found.sort();
        Ok(found)
    }

    pub fn consistent(&self, expected: impl IntoIterator<Item = (K, V, CopperAabb)>) -> bool
    where
        V: Eq + Clone,
    {
        let expected: BTreeMap<K, (V, CopperAabb)> =
            expected.into_iter().map(|(k, v, b)| (k, (v, b))).collect();
        fn inspect<'a, K: SpatialKey, V: Eq + Clone>(
            nodes: &'a [Option<TreapNode<K, V>>],
            idx: u32,
            lo: Option<(i64, &'a K)>,
            hi: Option<(i64, &'a K)>,
            visited: &mut BTreeSet<u32>,
            actual: &mut BTreeMap<K, (V, CopperAabb)>,
        ) -> Option<(u32, Option<CopperAabb>)> {
            if idx == NONE_U32 {
                return Some((0, None));
            }
            if !visited.insert(idx) {
                return None;
            }
            let n = nodes.get(idx as usize)?.as_ref()?;
            let n_ord = (n.copper.min[0], &n.key);
            if !lo.is_none_or(|b| b < n_ord) || !hi.is_none_or(|b| n_ord < b) {
                return None;
            }
            if n.priority != n.key.priority() {
                return None;
            }
            let n_heap = (n.priority, n.copper.min[0], &n.key);
            for child_idx in [n.left, n.right] {
                if child_idx != NONE_U32 {
                    let child = nodes.get(child_idx as usize)?.as_ref()?;
                    if (child.priority, child.copper.min[0], &child.key) >= n_heap {
                        return None;
                    }
                }
            }
            let (lh, lb) = inspect(nodes, n.left, lo, Some(n_ord), visited, actual)?;
            let (rh, rb) = inspect(nodes, n.right, Some(n_ord), hi, visited, actual)?;
            let mut envelope = n.copper;
            for child in [lb, rb].into_iter().flatten() {
                envelope = envelope.union(child);
            }
            if n.height.get() != 1 + lh.max(rh)
                || n.envelope != envelope
                || actual
                    .insert(n.key.clone(), (n.value.clone(), n.copper))
                    .is_some()
            {
                return None;
            }
            Some((n.height.get(), Some(envelope)))
        }
        let mut visited = BTreeSet::new();
        let mut actual = BTreeMap::new();
        if inspect(
            &self.nodes,
            self.root,
            None,
            None,
            &mut visited,
            &mut actual,
        )
        .is_none()
        {
            return false;
        }
        if actual != expected {
            return false;
        }
        let mut free_set = BTreeSet::new();
        for &idx in &self.free {
            if !free_set.insert(idx) || self.nodes.get(idx as usize).is_none_or(Option::is_some) {
                return false;
            }
        }
        visited.len() + free_set.len() == self.nodes.len() && visited.is_disjoint(&free_set)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{geometry, model::*, Engine, TransactionError};
    use std::panic::{catch_unwind, AssertUnwindSafe};

    #[test]
    fn compact_spatial_node_sizes() {
        assert!(std::mem::size_of::<FlatNode>() <= 80);
        assert!(std::mem::size_of::<TreapNode<u64, u32>>() <= 96);
        assert!(std::mem::size_of::<Option<TreapNode<u64, u32>>>() <= 96);
    }

    #[test]
    fn treap_mutations_match_linear_aabb_oracle_and_are_history_independent() {
        let mut index = SpatialIndex::new();
        let mut expected: BTreeMap<usize, CopperAabb> = BTreeMap::new();
        let mut seed = 17u64;
        for step in 0..2000 {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
            let key = (seed >> 32) as usize % 257;
            if let Some(bounds) = expected.remove(&key) {
                let removed = index.remove(&key, bounds);
                assert_eq!(removed, key as u32);
            } else {
                let x = key as i64 * 11 - 1500;
                let bounds =
                    CopperAabb::from_bounds(([x, (step % 17) - 8], [x + 13, (step % 17) + 5]));
                index.insert(key, key as u32, bounds);
                expected.insert(key, bounds);
            }
            assert!(index.consistent(expected.iter().map(|(k, b)| (*k, *k as u32, *b))));
            let work = WorkBudget::new(Limits::default());
            let query = SweptAabb::from_copper(CopperAabb::from_bounds((
                [key as i64 * 7 - 1000, -3],
                [key as i64 * 7 - 900, 3],
            )));
            let found: Vec<_> = index
                .query(query, &work)
                .unwrap()
                .into_iter()
                .copied()
                .collect();
            let linear: Vec<_> = expected
                .iter()
                .filter(|(_, b)| b.overlaps_swept(query))
                .map(|(k, _)| *k)
                .collect();
            assert_eq!(found, linear);
            if step % 200 == 199 {
                let mut rebuilt = SpatialIndex::new();
                for (&k, &b) in expected.iter().rev() {
                    rebuilt.insert(k, k as u32, b);
                }
                let w1 = WorkBudget::new(Limits::default());
                let w2 = WorkBudget::new(Limits::default());
                assert_eq!(
                    index.query(query, &w1).unwrap(),
                    rebuilt.query(query, &w2).unwrap()
                );
                assert_eq!(w1.used(), w2.used());
                assert_eq!(index.height(), rebuilt.height());
            }
        }
        for (key, bounds) in expected.clone() {
            index.remove(&key, bounds);
            expected.remove(&key);
            assert!(index.consistent(expected.iter().map(|(k, b)| (*k, *k as u32, *b))));
        }
        assert_eq!(index.root, NONE_U32);
    }

    #[test]
    fn consistent_rejects_global_bst_order_and_heap_violations() {
        let mut index = SpatialIndex::new();
        let b1 = CopperAabb::from_bounds(([10, 0], [20, 10]));
        let b2 = CopperAabb::from_bounds(([30, 0], [40, 10]));
        let b3 = CopperAabb::from_bounds(([50, 0], [60, 10]));
        index.insert(1u64, 0u32, b1);
        index.insert(2u64, 1u32, b2);
        index.insert(3u64, 2u32, b3);
        let expected = [(1u64, 0u32, b1), (2u64, 1u32, b2), (3u64, 2u32, b3)];
        assert!(index.consistent(expected));

        // Violate BST key order on a node without changing expected map contents.
        let root_idx = index.root;
        let orig_copper = index.node(root_idx).copper;
        index.node_mut(root_idx).copper = CopperAabb::from_bounds(([1000, 0], [1010, 10]));
        index.refresh(root_idx);
        assert!(!index.consistent([(1u64, 0u32, b1), (2u64, 1u32, b2), (3u64, 2u32, b3)]));
        index.node_mut(root_idx).copper = orig_copper;
        index.refresh(root_idx);
        assert!(index.consistent(expected));

        // Violate priority integrity.
        index.node_mut(root_idx).priority ^= 1;
        assert!(!index.consistent(expected));
        index.node_mut(root_idx).priority ^= 1;
        assert!(index.consistent(expected));

        // Exercise String key priority determinism as well.
        let mut s_index = SpatialIndex::new();
        let sa = CopperAabb::from_bounds(([0, 0], [5, 5]));
        let sb = CopperAabb::from_bounds(([10, 0], [15, 5]));
        s_index.insert("alpha".to_string(), 1u32, sa);
        s_index.insert("beta".to_string(), 2u32, sb);
        assert!(s_index.consistent([
            ("alpha".to_string(), 1u32, sa),
            ("beta".to_string(), 2u32, sb)
        ]));
    }

    #[test]
    fn outward_sweep_uses_wide_arithmetic_even_for_extreme_query_coordinates() {
        let swept = SweptAabb::swept(
            [i64::MIN, i64::MAX],
            [i64::MAX, i64::MIN],
            i64::MAX,
            i64::MAX,
        );
        assert!(swept.min.iter().all(|v| *v < i128::from(i64::MIN)));
        assert!(swept.max.iter().all(|v| *v > i128::from(i64::MAX)));
        let odd = SweptAabb::swept([-7, -3], [9, 11], 3, 2);
        assert_eq!(
            odd,
            SweptAabb {
                min: [-11, -7],
                max: [13, 15]
            }
        );
        let corner = CopperAabb::from_bounds(([13, 15], [13, 15]));
        assert!(corner.overlaps_swept(odd));
        assert!(odd.overlaps(SweptAabb::from_copper(corner)));
    }

    fn candidate_fixture(y: i64) -> Input {
        let mut input: Input = serde_json::from_value(serde_json::json!({
            "version":1, "source_id":"a".repeat(64), "layer":"F.Cu",
            "bounds":{"min":[-100,-100],"max":[20000,20000]},
            "rules":{"width_nm":3,"clearance_nm":2,"grid_nm":1,"edge_clearance_nm":0},
            "nets":[{"id":0,"name":""}], "pads":[], "tracks":[], "max_expansions":1000
        }))
        .unwrap();
        for n in 0..1000 {
            input.pads.push(Pad {
                id: format!("10000000-0000-4000-8000-{n:012x}"),
                net: 0,
                at: [10 + n as i64 * 10, y],
                shape: Shape::Circle { radius_nm: 1 },
            });
            input.tracks.push(Track {
                id: format!("20000000-0000-4000-8000-{n:012x}"),
                net: 0,
                start: [10 + n as i64 * 10, y],
                end: [11 + n as i64 * 10, y],
                width_nm: 1,
                locked: true,
            });
        }
        input
    }

    fn linear_counts(input: &Input, a: Point, b: Point) -> (usize, usize, bool) {
        let mut linear_candidates = 0;
        let mut linear_predicates = 0;
        let mut linear_legal = true;
        for pad in &input.pads {
            linear_candidates += 1;
            if pad.net == 0 || pad.net != 1 {
                linear_predicates += 1;
                linear_legal &= !geometry::track_near_pad(
                    a,
                    b,
                    input.rules.width_nm,
                    input.rules.clearance_nm,
                    pad,
                    false,
                );
            }
        }
        for track in &input.tracks {
            linear_candidates += 1;
            if track.net == 0 || track.net != 1 {
                linear_predicates += 1;
                linear_legal &= !geometry::segments_near(
                    a,
                    b,
                    track.start,
                    track.end,
                    i128::from(input.rules.width_nm)
                        + i128::from(track.width_nm)
                        + 2 * i128::from(input.rules.clearance_nm),
                    false,
                );
            }
        }
        (linear_candidates, linear_predicates, linear_legal)
    }

    #[test]
    fn fixed_sparse_and_dense_candidate_counts_match_actual_legality_work() {
        for (label, y, expected_candidates, expected_total) in
            [("sparse", 100, 0, 3u64), ("dense", 5, 2000, 6001u64)]
        {
            let input = candidate_fixture(y);
            let engine = Engine::new(input.clone()).unwrap();
            let a = [-10, 0];
            let b = [11000, 0];
            let swept = SweptAabb::swept(a, b, input.rules.width_nm, input.rules.clearance_nm);
            let query_work = WorkBudget::new(input.limits);
            let pads = engine.pad_spatial.query(swept, &query_work).unwrap();
            let tracks = engine.copper.candidates(swept, &query_work).unwrap();
            let candidates = pads.len() + tracks.len();
            assert_eq!(candidates, expected_candidates);
            let (linear_candidates, linear_predicates, linear_legal) = linear_counts(&input, a, b);
            assert!(linear_legal);
            assert_eq!((linear_candidates, linear_predicates), (2000, 2000));
            assert!(engine.legal_edge(1, a, b).unwrap());
            assert_eq!(engine.work_used(), expected_total);
            if label == "sparse" {
                assert_eq!(query_work.used(), 2);
            }
        }
    }

    #[test]
    fn per_object_non_masking_superset_holds_across_roundrect_static_and_rollback_worlds() {
        let mut seed = 0x5eed_0201u64;
        let mut next_u64 = || -> u64 {
            seed = seed
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            seed
        };
        let shapes = [
            Shape::Circle { radius_nm: 4 },
            Shape::Rect {
                half_size_nm: [6, 3],
            },
            Shape::RoundRect {
                half_size_nm: [7, 4],
                radius_nm: 2,
            },
            Shape::RoundRect {
                half_size_nm: [8, 4],
                radius_nm: 4,
            },
            Shape::RoundRect {
                half_size_nm: [4, 8],
                radius_nm: 4,
            },
        ];
        let mut input = Input {
            version: 1,
            source_id: "c".repeat(64),
            layer: "F.Cu".into(),
            bounds: Bounds {
                min: [-200, -200],
                max: [200, 200],
            },
            rules: Rules {
                width_nm: 3,
                clearance_nm: 2,
                grid_nm: 10,
                edge_clearance_nm: 0,
            },
            nets: (0..=4)
                .map(|id| Net {
                    id,
                    name: if id == 0 {
                        String::new()
                    } else {
                        format!("N{id}")
                    },
                })
                .collect(),
            pads: Vec::new(),
            tracks: Vec::new(),
            max_expansions: 5000,
            limits: Limits::default(),
        };
        for i in 0..20 {
            let x = ((next_u64() % 31) as i64 - 15) * 10;
            let y = ((next_u64() % 31) as i64 - 15) * 10;
            input.pads.push(Pad {
                id: format!("30000000-0000-4000-8000-{:012x}", i + 1),
                net: (i % 5) as u32,
                at: [x, y],
                shape: shapes[i % shapes.len()].clone(),
            });
        }
        let mut engine = Engine::new(input).unwrap();
        let _ = engine.route_all();
        let _ = engine.transaction(|tx| {
            let _ = tx.reroute(1, 1);
            Err::<(), _>(())
        });
        let _ = catch_unwind(AssertUnwindSafe(|| {
            let _: Result<(), TransactionError<()>> = engine.transaction(|tx| {
                let _ = tx.reroute(2, 500);
                std::panic::panic_any(99u32)
            });
        }));
        assert!(engine.copper.index_consistent());
        let r = engine.input.rules;
        for step in 0..200 {
            let a = [
                (next_u64() % 361) as i64 - 180,
                (next_u64() % 361) as i64 - 180,
            ];
            let b = if step % 3 == 0 {
                [
                    (next_u64() % 361) as i64 - 180,
                    (next_u64() % 361) as i64 - 180,
                ]
            } else {
                [a[0] + (step % 19) as i64 - 9, a[1] + (step % 17) as i64 - 8]
            };
            let swept = SweptAabb::swept(a, b, r.width_nm, r.clearance_nm);
            let work = WorkBudget::new(Limits::default());
            let pad_cands: BTreeSet<u32> = engine
                .pad_spatial
                .query(swept, &work)
                .unwrap()
                .into_iter()
                .collect();
            let track_cands: BTreeSet<String> = engine
                .copper
                .candidates(swept, &work)
                .unwrap()
                .into_iter()
                .map(|t| t.id.clone())
                .collect();
            for (ord, pad) in engine.input.pads.iter().enumerate() {
                if geometry::track_near_pad(a, b, r.width_nm, r.clearance_nm, pad, false) {
                    assert!(pad_cands.contains(&(ord as u32)));
                }
            }
            for tr in engine.copper.tracks() {
                let gap = i128::from(r.width_nm)
                    + i128::from(tr.width_nm)
                    + 2 * i128::from(r.clearance_nm);
                if geometry::segments_near(a, b, tr.start, tr.end, gap, false) {
                    assert!(track_cands.contains(&tr.id));
                }
            }
        }
    }
}
