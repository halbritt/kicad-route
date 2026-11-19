//! Conservative broad phase. Static copper uses a leaf-bucketed flat
//! median-balanced BST; dynamic generated copper uses a history-independent
//! deterministic treap. Exact geometry remains the caller's sole clearance authority.
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

pub(crate) const STATIC_LEAF_CAP: usize = 32;

type StaticKey = (i64, i64, u32);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct LeafEntry {
    copper: CopperAabb,
    net: u32,
    ordinal: u32,
}

const _: () = assert!(std::mem::size_of::<LeafEntry>() == 40);

type AxisKey = (i64, u32);

#[inline(always)]
fn entry_key(entry: &LeafEntry) -> StaticKey {
    (entry.copper.min[0], entry.copper.min[1], entry.ordinal)
}

#[inline(always)]
fn axis_key(entry: &LeafEntry, axis: u8) -> AxisKey {
    (entry.copper.min[axis as usize], entry.ordinal)
}

#[derive(Clone, Copy, Debug)]
enum StaticKind {
    Internal {
        left: u32,
        right: u32,
        split_axis: u8,
        split_ordinal: u32,
        split_coord: i64,
    },
    Leaf {
        start: u32,
        end: u32,
        uniform_net: u32,
    },
}

#[derive(Clone, Copy, Debug)]
struct StaticNode {
    envelope: CopperAabb,
    kind: StaticKind,
}

const _: () = assert!(std::mem::size_of::<StaticNode>() <= 64);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum PadCore {
    Box {
        lo: [i32; 2],
        hi: [i32; 2],
        radius: i32,
    },
    Box2x {
        lo2: [i32; 2],
        hi2: [i32; 2],
        radius2: i32,
    },
    Capsule {
        a: [i32; 2],
        b: [i32; 2],
        width: i32,
    },
    General,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum DrillGeom {
    Circle {
        center: [i32; 2],
        diameter: i32,
    },
    Capsule {
        a: [i32; 2],
        b: [i32; 2],
        width: i32,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct EdgeGeom {
    pub a: [i32; 2],
    pub b: [i32; 2],
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ArtworkGeom {
    Segment {
        a: [i32; 2],
        b: [i32; 2],
        width: i32,
    },
    Circle {
        center: [i32; 2],
        radius: i32,
        width: i32,
        filled: bool,
    },
    FilledPolygon {
        graphic_idx: u32,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum MaskGeom {
    Segment {
        a: [i32; 2],
        b: [i32; 2],
        width: i32,
        expansion: i32,
    },
    Circle {
        center: [i32; 2],
        radius: i32,
        width: i32,
        expansion: i32,
        filled: bool,
    },
    FilledPolygon {
        mask_idx: u32,
        expansion: i32,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct TrackGeom {
    pub start: Point,
    pub end: Point,
    pub width_nm: i64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct ViaGeom {
    pub at: [i32; 2],
    pub diameter: i32,
    pub drill: i32,
    pub layer_mask: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct KeepoutGeom {
    pub rule_area_idx: u32,
    pub poly_idx: u32,
    pub no_tracks: bool,
    pub no_vias: bool,
    pub no_zone_fills: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct ZoneFillGeom {
    pub zone_idx: u32,
    pub poly_idx: u32,
    pub refillable: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct SpokeCorridorGeom {
    pub corridor_idx: u32,
}

const _: () = assert!(std::mem::size_of::<PadCore>() <= 40);
const _: () = assert!(std::mem::size_of::<DrillGeom>() <= 40);
const _: () = assert!(std::mem::size_of::<EdgeGeom>() <= 40);
const _: () = assert!(std::mem::size_of::<ArtworkGeom>() <= 40);
const _: () = assert!(std::mem::size_of::<MaskGeom>() <= 40);
const _: () = assert!(std::mem::size_of::<TrackGeom>() <= 40);
const _: () = assert!(std::mem::size_of::<ViaGeom>() <= 40);
const _: () = assert!(std::mem::size_of::<KeepoutGeom>() <= 40);
const _: () = assert!(std::mem::size_of::<ZoneFillGeom>() <= 40);
const _: () = assert!(std::mem::size_of::<SpokeCorridorGeom>() <= 40);

#[derive(Debug)]
pub(crate) struct StaticSpatialIndex<P> {
    entries: Vec<LeafEntry>,
    payloads: Vec<P>,
    nodes: Vec<StaticNode>,
    root: u32,
}

impl<P: Copy + Eq> StaticSpatialIndex<P> {
    pub fn new(items: impl IntoIterator<Item = (u32, u32, CopperAabb, P)>) -> Self {
        let (mut entries, mut payloads): (Vec<LeafEntry>, Vec<P>) = items
            .into_iter()
            .map(|(ordinal, net, copper, payload)| {
                (
                    LeafEntry {
                        copper,
                        net,
                        ordinal,
                    },
                    payload,
                )
            })
            .unzip();
        if entries.is_empty() {
            return Self {
                entries,
                payloads,
                nodes: Vec::new(),
                root: NONE_U32,
            };
        }
        let mut order: Vec<u32> =
            (0..u32::try_from(entries.len()).expect("bounded static entries")).collect();
        order.sort_unstable_by_key(|&i| entries[i as usize].ordinal);
        assert!(
            order
                .windows(2)
                .all(|w| entries[w[0] as usize].ordinal < entries[w[1] as usize].ordinal),
            "static spatial ordinals must be unique"
        );
        let approx_leaves = entries.len().div_ceil(STATIC_LEAF_CAP / 2).max(1);
        let mut nodes = Vec::with_capacity(approx_leaves * 2);
        fn build(
            entries: &[LeafEntry],
            order: &mut [u32],
            start: usize,
            end: usize,
            nodes: &mut Vec<StaticNode>,
        ) -> u32 {
            let len = end - start;
            let idx = u32::try_from(nodes.len()).expect("bounded spatial node arena");
            if len <= STATIC_LEAF_CAP {
                order[start..end].sort_unstable_by(|&a, &b| {
                    entry_key(&entries[a as usize]).cmp(&entry_key(&entries[b as usize]))
                });
                let first = &entries[order[start] as usize];
                let mut envelope = first.copper;
                let mut uniform_net = first.net;
                for &ord_idx in &order[start + 1..end] {
                    let entry = &entries[ord_idx as usize];
                    envelope = envelope.union(entry.copper);
                    if entry.net != uniform_net {
                        uniform_net = 0;
                    }
                }
                nodes.push(StaticNode {
                    envelope,
                    kind: StaticKind::Leaf {
                        start: u32::try_from(start).expect("bounded leaf start"),
                        end: u32::try_from(end).expect("bounded leaf end"),
                        uniform_net,
                    },
                });
                return idx;
            }
            let first_c = entries[order[start] as usize].copper;
            let mut min0 = first_c.min[0];
            let mut max0 = first_c.min[0];
            let mut min1 = first_c.min[1];
            let mut max1 = first_c.min[1];
            for &ord_idx in &order[start + 1..end] {
                let c = entries[ord_idx as usize].copper;
                min0 = min0.min(c.min[0]);
                max0 = max0.max(c.min[0]);
                min1 = min1.min(c.min[1]);
                max1 = max1.max(c.min[1]);
            }
            let axis = if max1.abs_diff(min1) > max0.abs_diff(min0) {
                1u8
            } else {
                0u8
            };
            let mid_rel = len / 2;
            order[start..end].select_nth_unstable_by(mid_rel, |&a, &b| {
                axis_key(&entries[a as usize], axis).cmp(&axis_key(&entries[b as usize], axis))
            });
            let mid = start + mid_rel;
            let split = entries[order[mid] as usize];
            nodes.push(StaticNode {
                envelope: split.copper,
                kind: StaticKind::Leaf {
                    start: 0,
                    end: 0,
                    uniform_net: 0,
                },
            });
            let left = build(entries, order, start, mid, nodes);
            let right = build(entries, order, mid, end, nodes);
            let envelope = nodes[left as usize]
                .envelope
                .union(nodes[right as usize].envelope);
            nodes[idx as usize] = StaticNode {
                envelope,
                kind: StaticKind::Internal {
                    left,
                    right,
                    split_axis: axis,
                    split_ordinal: split.ordinal,
                    split_coord: split.copper.min[axis as usize],
                },
            };
            idx
        }
        let root = build(&entries, &mut order, 0, entries.len(), &mut nodes);
        for i in 0..order.len() {
            let mut dst = i;
            if order[dst] as usize == dst {
                continue;
            }
            let tmp_entry = entries[dst];
            let tmp_payload = payloads[dst];
            loop {
                let src = order[dst] as usize;
                order[dst] = dst as u32;
                if src == i {
                    entries[dst] = tmp_entry;
                    payloads[dst] = tmp_payload;
                    break;
                }
                entries[dst] = entries[src];
                payloads[dst] = payloads[src];
                dst = src;
            }
        }
        drop(order);
        Self {
            entries,
            payloads,
            nodes,
            root,
        }
    }

    #[inline(always)]
    pub fn visit_matching(
        &self,
        bounds: SweptAabb,
        net_filter: Option<u32>,
        work: &WorkBudget,
        mut visitor: impl FnMut(u32, &P) -> Result<ControlFlow<()>, Failure>,
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
            // One visit covers the node (internal or leaf), including pruned
            // nodes. No uncharged linear scan precedes the exact predicates.
            work.charge(1)?;
            let node = &self.nodes[idx as usize];
            if node.envelope.overlaps(q) {
                match node.kind {
                    StaticKind::Leaf {
                        start,
                        end,
                        uniform_net,
                    } => {
                        let start = start as usize;
                        let end = end as usize;
                        if uniform_net != 0 && net_filter == Some(uniform_net) {
                            let len = (end - start) as u64;
                            if work.charge(len).is_err() {
                                for _ in start..end {
                                    work.charge(1)?;
                                }
                            }
                        } else {
                            for (entry, payload) in self.entries[start..end]
                                .iter()
                                .zip(&self.payloads[start..end])
                            {
                                // Charge before inspecting entry.net or entry.copper.
                                work.charge(1)?;
                                if net_filter.is_none_or(|net| entry.net == 0 || entry.net != net)
                                    && entry.copper.overlaps(q)
                                    && visitor(entry.ordinal, payload)?.is_break()
                                {
                                    return Ok(ControlFlow::Break(()));
                                }
                            }
                        }
                    }
                    StaticKind::Internal { left, right, .. } => {
                        stack[sp] = right;
                        sp += 1;
                        idx = left;
                        continue;
                    }
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
    #[inline]
    pub fn visit(
        &self,
        bounds: SweptAabb,
        work: &WorkBudget,
        mut visitor: impl FnMut(u32) -> Result<ControlFlow<()>, Failure>,
    ) -> Result<ControlFlow<()>, Failure> {
        self.visit_matching(bounds, None, work, |ordinal, _| visitor(ordinal))
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

    pub fn consistent(
        &self,
        expected: impl IntoIterator<Item = (u32, u32, CopperAabb, P)>,
    ) -> bool {
        let expected: BTreeMap<u32, (u32, CopperAabb, P)> = expected
            .into_iter()
            .map(|(ordinal, net, copper, payload)| (ordinal, (net, copper, payload)))
            .collect();
        if self.payloads.len() != self.entries.len() {
            return false;
        }
        if self.root == NONE_U32 {
            return self.nodes.is_empty() && self.entries.is_empty() && expected.is_empty();
        }
        if self.entries.len() != expected.len() {
            return false;
        }
        fn inspect<P: Copy + Eq>(
            nodes: &[StaticNode],
            entries: &[LeafEntry],
            payloads: &[P],
            idx: u32,
            bounds: [(Option<AxisKey>, Option<AxisKey>); 2],
            visited: &mut BTreeSet<u32>,
            leaf_spans: &mut Vec<(u32, u32)>,
            actual: &mut BTreeMap<u32, (u32, CopperAabb, P)>,
        ) -> Option<CopperAabb> {
            if !visited.insert(idx) {
                return None;
            }
            let n = nodes.get(idx as usize)?;
            match n.kind {
                StaticKind::Internal {
                    left,
                    right,
                    split_axis,
                    split_ordinal,
                    split_coord,
                } => {
                    if split_axis > 1 {
                        return None;
                    }
                    let axis = split_axis as usize;
                    let split = (split_coord, split_ordinal);
                    if !bounds[axis].0.is_none_or(|b| b < split)
                        || !bounds[axis].1.is_none_or(|b| split < b)
                    {
                        return None;
                    }
                    let mut left_bounds = bounds;
                    left_bounds[axis].1 = Some(split);
                    let mut right_bounds = bounds;
                    right_bounds[axis].0 = Some(split);
                    let lb = inspect(
                        nodes,
                        entries,
                        payloads,
                        left,
                        left_bounds,
                        visited,
                        leaf_spans,
                        actual,
                    )?;
                    let rb = inspect(
                        nodes,
                        entries,
                        payloads,
                        right,
                        right_bounds,
                        visited,
                        leaf_spans,
                        actual,
                    )?;
                    let envelope = lb.union(rb);
                    if n.envelope != envelope {
                        return None;
                    }
                    Some(envelope)
                }
                StaticKind::Leaf {
                    start,
                    end,
                    uniform_net,
                } => {
                    if start >= end
                        || (end as usize) > entries.len()
                        || (end - start) as usize > STATIC_LEAF_CAP
                    {
                        return None;
                    }
                    let mut envelope = entries[start as usize].copper;
                    let mut expected_uniform_net = entries[start as usize].net;
                    let mut prev: Option<StaticKey> = None;
                    for entry_idx in start as usize..end as usize {
                        let entry = &entries[entry_idx];
                        if entry.net != expected_uniform_net {
                            expected_uniform_net = 0;
                        }
                        for axis in 0..2 {
                            let ak = axis_key(entry, axis as u8);
                            if !bounds[axis].0.is_none_or(|b| b <= ak)
                                || !bounds[axis].1.is_none_or(|b| ak < b)
                            {
                                return None;
                            }
                        }
                        let k = entry_key(entry);
                        if !prev.is_none_or(|p| p < k) {
                            return None;
                        }
                        prev = Some(k);
                        envelope = envelope.union(entry.copper);
                        if actual
                            .insert(
                                entry.ordinal,
                                (entry.net, entry.copper, payloads[entry_idx]),
                            )
                            .is_some()
                        {
                            return None;
                        }
                    }
                    if uniform_net != expected_uniform_net {
                        return None;
                    }
                    if n.envelope != envelope {
                        return None;
                    }
                    leaf_spans.push((start, end));
                    Some(envelope)
                }
            }
        }
        let mut visited = BTreeSet::new();
        let mut leaf_spans = Vec::new();
        let mut actual = BTreeMap::new();
        if inspect(
            &self.nodes,
            &self.entries,
            &self.payloads,
            self.root,
            [(None, None), (None, None)],
            &mut visited,
            &mut leaf_spans,
            &mut actual,
        )
        .is_none()
        {
            return false;
        }
        if visited.len() != self.nodes.len() || actual != expected {
            return false;
        }
        leaf_spans.sort_unstable();
        let mut cursor = 0u32;
        for (start, end) in leaf_spans {
            if start != cursor {
                return false;
            }
            cursor = end;
        }
        cursor as usize == self.entries.len()
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

pub(crate) const TREAP_UNIFORM_SKIP_CAP: u32 = 32;

#[derive(Clone, Debug)]
struct TreapNode<K, V> {
    copper: CopperAabb,
    envelope: CopperAabb,
    key: K,
    priority: u64,
    value: V,
    net: u32,
    subtree_uniform_net: u32,
    subtree_len: u32,
    height: NonZeroU32,
    left: u32,
    right: u32,
}

const _: () = assert!(std::mem::size_of::<TreapNode<u64, u32>>() <= 112);
const _: () = assert!(std::mem::size_of::<Option<TreapNode<u64, u32>>>() <= 112);

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
        let (copper, net, left, right) = {
            let n = self.node(idx);
            (n.copper, n.net, n.left, n.right)
        };
        let mut lh = 0;
        let mut rh = 0;
        let mut envelope = copper;
        let mut subtree_len = 1u32;
        let mut subtree_uniform_net = net;
        if left != NONE_U32 {
            let l = self.node(left);
            lh = l.height.get();
            envelope = envelope.union(l.envelope);
            subtree_len += l.subtree_len;
            if l.subtree_uniform_net != subtree_uniform_net {
                subtree_uniform_net = 0;
            }
        }
        if right != NONE_U32 {
            let r = self.node(right);
            rh = r.height.get();
            envelope = envelope.union(r.envelope);
            subtree_len += r.subtree_len;
            if r.subtree_uniform_net != subtree_uniform_net {
                subtree_uniform_net = 0;
            }
        }
        let n = self.node_mut(idx);
        n.height = NonZeroU32::new(1 + lh.max(rh)).expect("treap height >= 1");
        n.envelope = envelope;
        n.subtree_uniform_net = subtree_uniform_net;
        n.subtree_len = subtree_len;
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

    pub fn insert(&mut self, key: K, value: V, net: u32, bounds: CopperAabb) {
        let priority = key.priority();
        let idx = self.alloc_node(TreapNode {
            copper: bounds,
            envelope: bounds,
            key,
            priority,
            value,
            net,
            subtree_uniform_net: net,
            subtree_len: 1,
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

    #[inline(always)]
    pub fn visit_matching<'a>(
        &'a self,
        bounds: SweptAabb,
        net_filter: Option<u32>,
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
        let mut stack = [NONE_U32; 64];
        let mut sp = 0usize;
        let mut overflow: Vec<u32> = Vec::new();
        let mut idx = self.root;
        loop {
            // One visit covers the subtree and item AABB tests, including pruned
            // nodes. No uncharged linear scan precedes the exact predicates.
            work.charge(1)?;
            let node = self.nodes[idx as usize]
                .as_ref()
                .expect("spatial treap index references live node");
            if node.envelope.overlaps(q) {
                if node.subtree_uniform_net != 0
                    && net_filter == Some(node.subtree_uniform_net)
                    && node.subtree_len <= TREAP_UNIFORM_SKIP_CAP
                {
                    let rest = u64::from(node.subtree_len - 1);
                    if rest > 0 && work.charge(rest).is_err() {
                        for _ in 0..rest {
                            work.charge(1)?;
                        }
                    }
                } else {
                    if net_filter.is_none_or(|filter| node.net == 0 || node.net != filter)
                        && node.copper.overlaps(q)
                        && visitor(&node.key, &node.value)?.is_break()
                    {
                        return Ok(ControlFlow::Break(()));
                    }
                    let left = node.left;
                    let right = node.right;
                    if left != NONE_U32 {
                        if right != NONE_U32 {
                            if sp < stack.len() {
                                stack[sp] = right;
                                sp += 1;
                            } else {
                                overflow.push(right);
                            }
                        }
                        idx = left;
                        continue;
                    }
                    if right != NONE_U32 {
                        idx = right;
                        continue;
                    }
                }
            }
            if sp == stack.len() {
                if let Some(next) = overflow.pop() {
                    idx = next;
                    continue;
                }
            }
            if sp > 0 {
                sp -= 1;
                idx = stack[sp];
            } else {
                break;
            }
        }
        Ok(ControlFlow::Continue(()))
    }

    #[cfg(test)]
    #[inline]
    pub fn visit<'a>(
        &'a self,
        bounds: SweptAabb,
        work: &WorkBudget,
        visitor: impl FnMut(&'a K, &'a V) -> Result<ControlFlow<()>, Failure>,
    ) -> Result<ControlFlow<()>, Failure> {
        self.visit_matching(bounds, None, work, visitor)
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

    pub fn consistent(&self, expected: impl IntoIterator<Item = (K, V, u32, CopperAabb)>) -> bool
    where
        V: Eq + Clone,
    {
        let expected: BTreeMap<K, (V, u32, CopperAabb)> = expected
            .into_iter()
            .map(|(k, v, net, b)| (k, (v, net, b)))
            .collect();
        fn inspect<'a, K: SpatialKey, V: Eq + Clone>(
            nodes: &'a [Option<TreapNode<K, V>>],
            idx: u32,
            lo: Option<(i64, &'a K)>,
            hi: Option<(i64, &'a K)>,
            visited: &mut BTreeSet<u32>,
            actual: &mut BTreeMap<K, (V, u32, CopperAabb)>,
        ) -> Option<(u32, u32, u32, Option<CopperAabb>)> {
            if idx == NONE_U32 {
                return Some((0, 0, 0, None));
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
            let (lh, l_len, l_unif, lb) = inspect(nodes, n.left, lo, Some(n_ord), visited, actual)?;
            let (rh, r_len, r_unif, rb) =
                inspect(nodes, n.right, Some(n_ord), hi, visited, actual)?;
            let mut envelope = n.copper;
            for child in [lb, rb].into_iter().flatten() {
                envelope = envelope.union(child);
            }
            let expected_len = 1u32.checked_add(l_len)?.checked_add(r_len)?;
            let mut expected_unif = n.net;
            if n.left != NONE_U32 && l_unif != expected_unif {
                expected_unif = 0;
            }
            if n.right != NONE_U32 && r_unif != expected_unif {
                expected_unif = 0;
            }
            if n.height.get() != 1 + lh.max(rh)
                || n.subtree_len != expected_len
                || n.subtree_uniform_net != expected_unif
                || n.envelope != envelope
                || actual
                    .insert(n.key.clone(), (n.value.clone(), n.net, n.copper))
                    .is_some()
            {
                return None;
            }
            Some((n.height.get(), expected_len, expected_unif, Some(envelope)))
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
        assert!(std::mem::size_of::<LeafEntry>() <= 40);
        assert!(std::mem::size_of::<PadCore>() <= 40);
        assert!(std::mem::size_of::<DrillGeom>() <= 40);
        assert!(std::mem::size_of::<EdgeGeom>() <= 40);
        assert!(std::mem::size_of::<ArtworkGeom>() <= 40);
        assert!(std::mem::size_of::<MaskGeom>() <= 40);
        assert!(std::mem::size_of::<TrackGeom>() <= 40);
        assert!(std::mem::size_of::<StaticNode>() <= 64);
        assert!(std::mem::size_of::<TreapNode<u64, u32>>() <= 112);
        assert!(std::mem::size_of::<Option<TreapNode<u64, u32>>>() <= 112);
    }

    #[test]
    fn static_spatial_consistent_rejects_broken_invariants() {
        let items: Vec<(u32, u32, CopperAabb, ())> = (0..33u32)
            .map(|i| {
                let x = i64::from(i) * 10;
                (i, i % 3, CopperAabb::from_bounds(([x, 0], [x + 4, 4])), ())
            })
            .collect();
        let make = || StaticSpatialIndex::new(items.iter().copied());
        assert!(make().consistent(items.iter().copied()));

        // Empty invariant & non-empty mismatch.
        let empty: StaticSpatialIndex<()> = StaticSpatialIndex::new(std::iter::empty());
        assert!(empty.consistent(std::iter::empty()));
        assert!(!empty.consistent(items.iter().take(1).copied()));

        // Wrong expected item or net.
        let mut wrong_net = items.clone();
        wrong_net[0].1 += 1;
        assert!(!make().consistent(wrong_net));

        // Broken internal split key violating strict (lo, hi) BST bounds:
        // test both root (exercising leaf [lo, hi) check) and non-root internal (66 items).
        let mut idx = make();
        if let StaticKind::Internal { split_coord, .. } = &mut idx.nodes[0].kind {
            *split_coord = 10_000;
        }
        assert!(!idx.consistent(items.iter().copied()));

        let items66: Vec<(u32, u32, CopperAabb, ())> = (0..66u32)
            .map(|i| {
                let x = i64::from(i) * 10;
                (i, i % 3, CopperAabb::from_bounds(([x, 0], [x + 4, 4])), ())
            })
            .collect();
        let mut idx66 = StaticSpatialIndex::new(items66.iter().copied());
        assert!(idx66.consistent(items66.iter().copied()));
        if let StaticKind::Internal { left, .. } = idx66.nodes[0].kind {
            if let StaticKind::Internal { split_coord, .. } = &mut idx66.nodes[left as usize].kind {
                *split_coord = 10_000;
            } else {
                panic!("expected non-root internal node for 66 items");
            }
        }
        assert!(!idx66.consistent(items66.iter().copied()));

        // Stale leaf envelope.
        let mut idx = make();
        idx.nodes[1].envelope.max[0] += 1;
        assert!(!idx.consistent(items.iter().copied()));

        // Stale internal envelope.
        let mut idx = make();
        assert!(matches!(idx.nodes[0].kind, StaticKind::Internal { .. }));
        idx.nodes[0].envelope.max[0] += 1;
        assert!(!idx.consistent(items.iter().copied()));

        // Out-of-order intra-leaf entries (even if expected matches).
        let mut idx = make();
        idx.entries.swap(0, 1);
        assert!(!idx.consistent(items.iter().copied()));

        // Leaf exceeding STATIC_LEAF_CAP (matching items' exact net/copper/payload).
        let mut idx = make();
        idx.nodes = vec![StaticNode {
            envelope: CopperAabb::from_bounds(([0, 0], [324, 4])),
            kind: StaticKind::Leaf {
                start: 0,
                end: 33,
                uniform_net: 0,
            },
        }];
        idx.root = 0;
        assert!(!idx.consistent(items.iter().copied()));

        // Non-contiguous leaf spans (gap) & orphan node.
        let mut idx = make();
        let orphan = idx.nodes[1];
        idx.nodes.push(orphan);
        assert!(!idx.consistent(items.iter().copied()));

        let mut idx = make();
        if let StaticKind::Leaf { start, .. } = &mut idx.nodes[2].kind {
            *start += 1;
        }
        assert!(!idx.consistent(items.iter().take(32).copied()));

        // Cycle in internal node.
        let mut idx = make();
        if let StaticKind::Internal { left, .. } = &mut idx.nodes[0].kind {
            *left = 0;
        }
        assert!(!idx.consistent(items.iter().copied()));

        // Payload length mismatch.
        let mut idx = make();
        idx.payloads.pop();
        assert!(!idx.consistent(items.iter().copied()));

        // Wrong leaf uniform_net summary.
        let same_net_items: Vec<(u32, u32, CopperAabb, ())> = (0..4u32)
            .map(|i| {
                let x = i64::from(i) * 10;
                (i, 7, CopperAabb::from_bounds(([x, 0], [x + 4, 4])), ())
            })
            .collect();
        let mut idx = StaticSpatialIndex::new(same_net_items.iter().copied());
        assert!(idx.consistent(same_net_items.iter().copied()));
        if let StaticKind::Leaf { uniform_net, .. } = &mut idx.nodes[0].kind {
            *uniform_net = 0;
        }
        assert!(!idx.consistent(same_net_items.iter().copied()));
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
                index.insert(key, key as u32, (key % 5) as u32, bounds);
                expected.insert(key, bounds);
            }
            assert!(index.consistent(expected.iter().map(|(k, b)| (
                *k,
                *k as u32,
                (*k % 5) as u32,
                *b
            ))));
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
                    rebuilt.insert(k, k as u32, (k % 5) as u32, b);
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
            assert!(index.consistent(expected.iter().map(|(k, b)| (
                *k,
                *k as u32,
                (*k % 5) as u32,
                *b
            ))));
        }
        assert_eq!(index.root, NONE_U32);
    }

    #[test]
    fn consistent_rejects_global_bst_order_and_heap_violations() {
        let mut index = SpatialIndex::new();
        let b1 = CopperAabb::from_bounds(([10, 0], [20, 10]));
        let b2 = CopperAabb::from_bounds(([30, 0], [40, 10]));
        let b3 = CopperAabb::from_bounds(([50, 0], [60, 10]));
        index.insert(1u64, 0u32, 1, b1);
        index.insert(2u64, 1u32, 2, b2);
        index.insert(3u64, 2u32, 3, b3);
        let expected = [
            (1u64, 0u32, 1u32, b1),
            (2u64, 1u32, 2u32, b2),
            (3u64, 2u32, 3u32, b3),
        ];
        assert!(index.consistent(expected));

        // Violate BST key order on a node without changing expected map contents.
        let root_idx = index.root;
        let orig_copper = index.node(root_idx).copper;
        index.node_mut(root_idx).copper = CopperAabb::from_bounds(([1000, 0], [1010, 10]));
        index.refresh(root_idx);
        assert!(!index.consistent([
            (1u64, 0u32, 1u32, b1),
            (2u64, 1u32, 2u32, b2),
            (3u64, 2u32, 3u32, b3)
        ]));
        index.node_mut(root_idx).copper = orig_copper;
        index.refresh(root_idx);
        assert!(index.consistent(expected));

        // Violate priority integrity.
        index.node_mut(root_idx).priority ^= 1;
        assert!(!index.consistent(expected));
        index.node_mut(root_idx).priority ^= 1;
        assert!(index.consistent(expected));

        // Violate BST order with unchanged expected map, heap, and envelopes by swapping left/right.
        let orig_left = index.node(root_idx).left;
        let orig_right = index.node(root_idx).right;
        assert!(orig_left != NONE_U32 && orig_right != NONE_U32);
        index.node_mut(root_idx).left = orig_right;
        index.node_mut(root_idx).right = orig_left;
        assert!(!index.consistent(expected));
        index.node_mut(root_idx).left = orig_left;
        index.node_mut(root_idx).right = orig_right;
        assert!(index.consistent(expected));

        // Corrupt TreapNode.height (to another NonZeroU32).
        let orig_height = index.node(root_idx).height;
        index.node_mut(root_idx).height = NonZeroU32::new(orig_height.get() + 1).unwrap();
        assert!(!index.consistent(expected));
        index.node_mut(root_idx).height = orig_height;
        assert!(index.consistent(expected));

        // Corrupt TreapNode.envelope without changing copper or expected.
        index.node_mut(root_idx).envelope.max[0] += 1;
        assert!(!index.consistent(expected));
        index.refresh(root_idx);
        assert!(index.consistent(expected));

        // Corrupt free-slot / arena disjointness (untracked None slot & in-use index in free).
        index.nodes.push(None);
        assert!(!index.consistent(expected));
        index.nodes.pop();
        assert!(index.consistent(expected));
        index.free.push(root_idx);
        assert!(!index.consistent(expected));
        index.free.pop();
        assert!(index.consistent(expected));

        // Valid-priority, valid-BST, heap-violating 2-node treap (child priority > parent priority).
        let mut heap_viol = SpatialIndex::new();
        heap_viol.insert(1u64, 0u32, 1, b1);
        heap_viol.insert(2u64, 1u32, 2, b2);
        let hv_expected = [(1u64, 0u32, 1u32, b1), (2u64, 1u32, 2u32, b2)];
        assert!(heap_viol.consistent(hv_expected));
        let hv_parent = heap_viol.root;
        let hv_child = if heap_viol.node(hv_parent).left != NONE_U32 {
            heap_viol.node(hv_parent).left
        } else {
            heap_viol.node(hv_parent).right
        };
        assert!(hv_child != NONE_U32);
        heap_viol.node_mut(hv_parent).left = NONE_U32;
        heap_viol.node_mut(hv_parent).right = NONE_U32;
        if heap_viol.node(hv_parent).key < heap_viol.node(hv_child).key {
            heap_viol.node_mut(hv_child).left = hv_parent;
            heap_viol.node_mut(hv_child).right = NONE_U32;
        } else {
            heap_viol.node_mut(hv_child).left = NONE_U32;
            heap_viol.node_mut(hv_child).right = hv_parent;
        }
        heap_viol.refresh(hv_parent);
        heap_viol.refresh(hv_child);
        heap_viol.root = hv_child;
        assert!(!heap_viol.consistent(hv_expected));

        // Wrong expected map contents.
        assert!(!index.consistent([(1u64, 0u32, 1u32, b1)]));

        // Exercise String key priority determinism and non-triviality as well.
        assert_ne!("alpha".to_string().priority(), 0);
        assert_ne!(
            "alpha".to_string().priority(),
            "beta".to_string().priority()
        );
        let mut s_index = SpatialIndex::new();
        let sa = CopperAabb::from_bounds(([0, 0], [5, 5]));
        let sb = CopperAabb::from_bounds(([10, 0], [15, 5]));
        s_index.insert("alpha".to_string(), 1u32, 1, sa);
        s_index.insert("beta".to_string(), 2u32, 2, sb);
        assert!(s_index.consistent([
            ("alpha".to_string(), 1u32, 1u32, sa),
            ("beta".to_string(), 2u32, 2u32, sb)
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

        // Out-of-i64 SweptAabb (clamp_i64() == None) must still charge 1 unit
        // on non-empty StaticSpatialIndex and SpatialIndex.
        let off_range = SweptAabb {
            min: [i128::from(i64::MAX) + 1, 0],
            max: [i128::from(i64::MAX) + 10, 10],
        };
        assert!(off_range.clamp_i64().is_none());
        let static_idx =
            StaticSpatialIndex::new([(0u32, 0u32, CopperAabb::from_bounds(([0, 0], [5, 5])), ())]);
        let mut treap_idx = SpatialIndex::new();
        treap_idx.insert(1u64, 0u32, 0, CopperAabb::from_bounds(([0, 0], [5, 5])));

        let w_static = WorkBudget::new(Limits::default());
        let _s1 = w_static.scope();
        assert!(static_idx.query(off_range, &w_static).unwrap().is_empty());
        assert_eq!(w_static.used(), 1);
        drop(_s1);

        let w_treap = WorkBudget::new(Limits::default());
        let _s2 = w_treap.scope();
        assert!(treap_idx.query(off_range, &w_treap).unwrap().is_empty());
        assert_eq!(w_treap.used(), 1);
        drop(_s2);

        let zero_budget = WorkBudget::new(Limits {
            max_work: 0,
            ..Limits::default()
        });
        let _s3 = zero_budget.scope();
        assert_eq!(
            static_idx
                .query(off_range, &zero_budget)
                .unwrap_err()
                .status,
            Status::WorkExhausted
        );
        assert_eq!(
            treap_idx.query(off_range, &zero_budget).unwrap_err().status,
            Status::WorkExhausted
        );
        drop(_s3);

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

    #[test]
    fn treap_visit_handles_depth_above_64_overflow() {
        // Construct a left-skewed spine of 80 nodes (bypassing heap balance)
        // with right leaf children so the iterative stack overflows past 64 frames.
        let mut index: SpatialIndex<u64, u32> = SpatialIndex::new();
        let mut expected = Vec::new();
        let mut prev = NONE_U32;
        let mut prev_len = 0u32;
        for i in (0..80u64).rev() {
            let x = i as i64 * 10;
            let copper = CopperAabb::from_bounds(([x, 0], [x + 4, 4]));
            let leaf_key = 1000 + i;
            let leaf_copper = CopperAabb::from_bounds(([x, 10], [x + 4, 14]));
            let right_idx = index.alloc_node(TreapNode {
                copper: leaf_copper,
                envelope: leaf_copper,
                key: leaf_key,
                priority: leaf_key.priority(),
                value: leaf_key as u32,
                net: 0,
                subtree_uniform_net: 0,
                subtree_len: 1,
                height: NonZeroU32::new(1).unwrap(),
                left: NONE_U32,
                right: NONE_U32,
            });
            let mut env = copper.union(leaf_copper);
            if prev != NONE_U32 {
                env = env.union(index.node(prev).envelope);
            }
            prev_len += 2;
            let cur_idx = index.alloc_node(TreapNode {
                copper,
                envelope: env,
                key: i,
                priority: i.priority(),
                value: i as u32,
                net: 0,
                subtree_uniform_net: 0,
                subtree_len: prev_len,
                height: NonZeroU32::new(1 + index.link_height(prev).max(1)).unwrap(),
                left: prev,
                right: right_idx,
            });
            prev = cur_idx;
            expected.push(i);
            expected.push(leaf_key);
        }
        index.root = prev;
        expected.sort();
        let work = WorkBudget::new(Limits::default());
        let _s = work.scope();
        let q = SweptAabb::from_copper(CopperAabb::from_bounds(([-10, -10], [900, 20])));
        let found: Vec<u64> = index
            .query(q, &work)
            .unwrap()
            .into_iter()
            .copied()
            .collect();
        assert_eq!(found, expected);
        assert_eq!(work.used(), 160);
        for limit in [65u64, 159u64] {
            let short = WorkBudget::new(Limits {
                max_work: limit,
                ..Limits::default()
            });
            let _short_s = short.scope();
            let err = index.query(q, &short).unwrap_err();
            assert_eq!(err.status, Status::WorkExhausted);
            assert_eq!(err.work_used, limit);
            assert_eq!(short.used(), limit);
        }
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
                layers: Vec::new(),
                local_clearance_nm: None,
                shape: Shape::Circle { radius_nm: 1 },
                drill: None,
                zone_connection: crate::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
                footprint_ref: None,
                pad_number: None,
                fromto_id: None,
            });
            input.tracks.push(Track {
                id: format!("20000000-0000-4000-8000-{n:012x}"),
                net: 0,
                layer: "F.Cu".into(),
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
            [("sparse", 100, 0, 3u64), ("dense", 5, 2000, 4127u64)]
        {
            let input = candidate_fixture(y);
            let engine = Engine::new(input.clone()).unwrap();
            let a = [-10, 0];
            let b = [11000, 0];
            let swept = SweptAabb::swept(a, b, input.rules.width_nm, input.rules.clearance_nm);
            let query_work = WorkBudget::new(input.limits);
            let pads = engine.pad_spatial[0].query(swept, &query_work).unwrap();
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
            layers: Vec::new(),
            bounds: Bounds {
                min: [-200, -200],
                max: [200, 200],
            },
            rules: Rules {
                width_nm: 3,
                clearance_nm: 2,
                grid_nm: 10,
                edge_clearance_nm: 0,
                hole_clearance_nm: 0,
                same_net_hole_clearance_nm: 0,
                solder_mask_expansion_nm: 0,
                solder_mask_min_width_nm: 0,
                solder_mask_to_copper_clearance_nm: 0,
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
            vias: Vec::new(),
            rule_areas: Vec::new(),
            max_expansions: 5000,
            limits: Limits::default(),
            holes: Vec::new(),
            board_outline: None,
            graphics: Vec::new(),
            mask_apertures: Vec::new(),
            board_design_settings: None,
            project_rules: None,
            custom_rules: None,
            effective_rules: None,
            zones: Vec::new(),
            components: Vec::new(),
            stackup: Vec::new(),
            routing_options: None,
        };
        for i in 0..20 {
            let x = ((next_u64() % 31) as i64 - 15) * 10;
            let y = ((next_u64() % 31) as i64 - 15) * 10;
            input.pads.push(Pad {
                id: format!("30000000-0000-4000-8000-{:012x}", i + 1),
                net: (i % 5) as u32,
                at: [x, y],
                layers: Vec::new(),
                local_clearance_nm: None,
                shape: shapes[i % shapes.len()].clone(),
                drill: None,
                zone_connection: crate::model::PadZoneConnection::Inherited,
                thermal_gap_nm: None,
                thermal_spoke_width_nm: None,
                thermal_spoke_angle_mdeg: None,
                orientation_mdeg: 0,
                footprint_ref: None,
                pad_number: None,
                fromto_id: None,
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
            let pad_cands: BTreeSet<u32> = engine.pad_spatial[0]
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

    #[test]
    fn static_uniform_net_partial_budgets_and_edge_cases() {
        let items: Vec<(u32, u32, CopperAabb, u32)> = (0..4u32)
            .map(|i| {
                let x = i64::from(i) * 10;
                (i, 1, CopperAabb::from_bounds(([x, 0], [x + 4, 4])), i)
            })
            .collect();
        let idx = StaticSpatialIndex::new(items);
        let q_all = SweptAabb::from_copper(CopperAabb::from_bounds(([-1, -1], [35, 5])));
        for limit in 0..=5u64 {
            let work = WorkBudget::new(Limits {
                max_work: limit,
                ..Limits::default()
            });
            let _s = work.scope();
            let mut calls = 0u64;
            let res = idx.visit_matching(q_all, Some(1), &work, |_, _| {
                calls += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            });
            if limit < 5 {
                let err = res.unwrap_err();
                assert_eq!(err.status, Status::WorkExhausted);
                assert_eq!(err.work_used, limit);
                assert_eq!(work.used(), limit);
                assert_eq!(calls, 0);
            } else {
                assert!(res.unwrap().is_continue());
                assert_eq!(calls, 0);
                assert_eq!(work.used(), 5);
            }
        }
        for limit in 0..=5u64 {
            let work = WorkBudget::new(Limits {
                max_work: limit,
                ..Limits::default()
            });
            let _s = work.scope();
            let mut calls = 0u64;
            let res = idx.visit(q_all, &work, |_| {
                calls += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            });
            if limit < 5 {
                let err = res.unwrap_err();
                assert_eq!(err.status, Status::WorkExhausted);
                assert_eq!(err.work_used, limit);
                assert_eq!(calls, limit.saturating_sub(1));
            } else {
                assert!(res.unwrap().is_continue());
                assert_eq!(calls, 4);
                assert_eq!(work.used(), 5);
            }
        }
        for net_filter in [Some(2), Some(0)] {
            let work = WorkBudget::new(Limits::default());
            let _s = work.scope();
            let mut calls = 0u64;
            assert!(idx
                .visit_matching(q_all, net_filter, &work, |_, _| {
                    calls += 1;
                    Ok::<_, Failure>(ControlFlow::Continue(()))
                })
                .unwrap()
                .is_continue());
            assert_eq!(calls, 4);
            assert_eq!(work.used(), 5);
        }

        let q_disjoint = SweptAabb::from_copper(CopperAabb::from_bounds(([100, 100], [110, 110])));
        let w0 = WorkBudget::new(Limits {
            max_work: 0,
            ..Limits::default()
        });
        let _s0 = w0.scope();
        assert_eq!(
            idx.visit_matching(q_disjoint, Some(1), &w0, |_, _| Ok(ControlFlow::Continue(
                ()
            )))
            .unwrap_err()
            .work_used,
            0
        );
        drop(_s0);
        let w1 = WorkBudget::new(Limits {
            max_work: 1,
            ..Limits::default()
        });
        let _s1 = w1.scope();
        let mut calls = 0u64;
        assert!(idx
            .visit_matching(q_disjoint, Some(1), &w1, |_, _| {
                calls += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            })
            .unwrap()
            .is_continue());
        assert_eq!(calls, 0);
        assert_eq!(w1.used(), 1);
        drop(_s1);

        let net0_idx = StaticSpatialIndex::new((0..4u32).map(|i| {
            let x = i64::from(i) * 10;
            (i, 0, CopperAabb::from_bounds(([x, 0], [x + 4, 4])), i)
        }));
        let w_net0 = WorkBudget::new(Limits::default());
        let _sn0 = w_net0.scope();
        let mut net0_calls = 0u64;
        assert!(net0_idx
            .visit_matching(q_all, Some(0), &w_net0, |_, _| {
                net0_calls += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            })
            .unwrap()
            .is_continue());
        assert_eq!(net0_calls, 4);
        assert_eq!(w_net0.used(), 5);
        drop(_sn0);

        let mixed_idx =
            StaticSpatialIndex::new([1u32, 1, 2, 1].into_iter().enumerate().map(|(i, net)| {
                let x = i64::try_from(i).unwrap() * 10;
                (
                    i as u32,
                    net,
                    CopperAabb::from_bounds(([x, 0], [x + 4, 4])),
                    i as u32,
                )
            }));
        let w_mix = WorkBudget::new(Limits::default());
        let _smix = w_mix.scope();
        let mut mix_calls = 0u64;
        assert!(mixed_idx
            .visit_matching(q_all, Some(1), &w_mix, |_, _| {
                mix_calls += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            })
            .unwrap()
            .is_continue());
        assert_eq!(mix_calls, 1);
        assert_eq!(w_mix.used(), 5);
        drop(_smix);

        // 2-leaf StaticSpatialIndex (34 items): left 17 uniform net=1, right 16 net=1 + 1 net=2.
        let two_leaf_items: Vec<(u32, u32, CopperAabb, u32)> = (0..34u32)
            .map(|i| {
                let x = if i < 17 {
                    i64::from(i) * 10
                } else {
                    200 + i64::from(i - 17) * 10
                };
                let net = if i == 33 { 2 } else { 1 };
                (i, net, CopperAabb::from_bounds(([x, 0], [x + 4, 4])), i)
            })
            .collect();
        let two_leaf = StaticSpatialIndex::new(two_leaf_items);
        let q_two_all = SweptAabb::from_copper(CopperAabb::from_bounds(([-5, -5], [400, 10])));
        for limit in 0..=37u64 {
            let work = WorkBudget::new(Limits {
                max_work: limit,
                ..Limits::default()
            });
            let _s = work.scope();
            let mut calls = 0u64;
            let res = two_leaf.visit_matching(q_two_all, Some(1), &work, |_, _| {
                calls += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            });
            if limit < 37 {
                let err = res.unwrap_err();
                assert_eq!(err.status, Status::WorkExhausted);
                assert_eq!(err.work_used, limit);
            } else {
                assert!(res.unwrap().is_continue());
                assert_eq!(calls, 1);
                assert_eq!(work.used(), 37);
            }
        }
        let q_left_only = SweptAabb::from_copper(CopperAabb::from_bounds(([-5, -5], [170, 10])));
        for limit in 0..=20u64 {
            let work = WorkBudget::new(Limits {
                max_work: limit,
                ..Limits::default()
            });
            let _s = work.scope();
            let mut calls = 0u64;
            let res = two_leaf.visit_matching(q_left_only, Some(1), &work, |_, _| {
                calls += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            });
            if limit < 20 {
                let err = res.unwrap_err();
                assert_eq!(err.status, Status::WorkExhausted);
                assert_eq!(err.work_used, limit);
            } else {
                assert!(res.unwrap().is_continue());
                assert_eq!(calls, 0);
                assert_eq!(work.used(), 20);
            }
        }

        // 4-node treap partial WorkExhausted check across 0..=4.
        let mut treap4 = SpatialIndex::new();
        for i in 1..=4u64 {
            let x = i as i64 * 10;
            treap4.insert(
                i,
                i as u32,
                0,
                CopperAabb::from_bounds(([x, 0], [x + 4, 4])),
            );
        }
        let q_t4 = SweptAabb::from_copper(CopperAabb::from_bounds(([0, -2], [60, 10])));
        for limit in 0..=4u64 {
            let work = WorkBudget::new(Limits {
                max_work: limit,
                ..Limits::default()
            });
            let _s = work.scope();
            let res = treap4.visit(q_t4, &work, |_, _| Ok(ControlFlow::Continue(())));
            if limit < 4 {
                let err = res.unwrap_err();
                assert_eq!(err.status, Status::WorkExhausted);
                assert_eq!(err.work_used, limit);
            } else {
                assert!(res.unwrap().is_continue());
                assert_eq!(work.used(), 4);
            }
        }
    }

    #[test]
    fn treap_uniform_net_bulk_skip_and_exact_work_cutoff() {
        // 1. n=16 (< cap), n=32 (== TREAP_UNIFORM_SKIP_CAP), & n=64 (> cap) uniform net=1.
        for n in [16u64, 32u64, 64u64] {
            let mut idx = SpatialIndex::new();
            let mut expected = Vec::new();
            for i in 1..=n {
                let x = i as i64 * 10;
                let b = CopperAabb::from_bounds(([x, 0], [x + 4, 4]));
                idx.insert(i, i as u32, 1, b);
                expected.push((i, i as u32, 1u32, b));
            }
            assert!(idx.consistent(expected));
            let q_all =
                SweptAabb::from_copper(CopperAabb::from_bounds(([0, -2], [n as i64 * 10 + 10, 8])));
            for limit in 0..=n {
                let work = WorkBudget::new(Limits {
                    max_work: limit,
                    ..Limits::default()
                });
                let _s = work.scope();
                let mut calls = 0u64;
                let res = idx.visit_matching(q_all, Some(1), &work, |_, _| {
                    calls += 1;
                    Ok::<_, Failure>(ControlFlow::Continue(()))
                });
                if limit < n {
                    let err = res.unwrap_err();
                    assert_eq!(err.status, Status::WorkExhausted);
                    assert_eq!(err.work_used, limit);
                    assert_eq!(work.used(), limit);
                    assert_eq!(calls, 0);
                } else {
                    assert!(res.unwrap().is_continue());
                    assert_eq!(calls, 0);
                    assert_eq!(work.used(), n);
                }
            }
            for filter in [Some(2), None] {
                let work = WorkBudget::new(Limits::default());
                let _s = work.scope();
                let mut calls = 0u64;
                assert!(idx
                    .visit_matching(q_all, filter, &work, |_, _| {
                        calls += 1;
                        Ok::<_, Failure>(ControlFlow::Continue(()))
                    })
                    .unwrap()
                    .is_continue());
                assert_eq!(calls, n);
                assert_eq!(work.used(), n);
            }
            if n == 32 {
                // At exact TREAP_UNIFORM_SKIP_CAP (32), a partial-overlap query overlapping root's
                // envelope (key 32 at [320, 0]..=[324, 4]) while missing disjoint children (1..=31)
                // still bulk-skips all 32 units.
                assert_eq!(idx.node(idx.root).subtree_len, 32);
                let q_partial =
                    SweptAabb::from_copper(CopperAabb::from_bounds(([320, 0], [324, 4])));
                for limit in 0..=32u64 {
                    let work = WorkBudget::new(Limits {
                        max_work: limit,
                        ..Limits::default()
                    });
                    let _s = work.scope();
                    let mut calls = 0u64;
                    let res = idx.visit_matching(q_partial, Some(1), &work, |_, _| {
                        calls += 1;
                        Ok::<_, Failure>(ControlFlow::Continue(()))
                    });
                    if limit < 32 {
                        let err = res.unwrap_err();
                        assert_eq!(err.status, Status::WorkExhausted);
                        assert_eq!(err.work_used, limit);
                        assert_eq!(work.used(), limit);
                        assert_eq!(calls, 0);
                    } else {
                        assert!(res.unwrap().is_continue());
                        assert_eq!(calls, 0);
                        assert_eq!(work.used(), 32);
                    }
                }
            }
            if n == 64 {
                // Capped skip prevents root bulk-charge explosion on a small local query.
                let q_local = SweptAabb::from_copper(CopperAabb::from_bounds(([10, 0], [14, 4])));
                let work = WorkBudget::new(Limits::default());
                let _s = work.scope();
                assert!(idx
                    .visit_matching(q_local, Some(1), &work, |_, _| {
                        Ok(ControlFlow::Continue(()))
                    })
                    .unwrap()
                    .is_continue());
                assert_eq!(work.used(), 37);
            }
        }

        // 2. Non-root uniform subtree: root key=6 (net=2), left 1..=4 (net=1), right 7..=8 (net=3).
        let mut non_root = SpatialIndex::new();
        for (k, net) in [(1u64, 1u32), (2, 1), (3, 1), (4, 1), (6, 2), (7, 3), (8, 3)] {
            let x = k as i64 * 10;
            non_root.insert(
                k,
                k as u32,
                net,
                CopperAabb::from_bounds(([x, 0], [x + 4, 4])),
            );
        }
        assert_eq!(non_root.node(non_root.root).net, 2);
        assert_eq!(non_root.node(non_root.root).subtree_uniform_net, 0);
        let q_sub = SweptAabb::from_copper(CopperAabb::from_bounds(([0, -2], [100, 8])));
        for limit in 0..=7u64 {
            let work = WorkBudget::new(Limits {
                max_work: limit,
                ..Limits::default()
            });
            let _s = work.scope();
            let mut calls = 0u64;
            let res = non_root.visit_matching(q_sub, Some(1), &work, |_, _| {
                calls += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            });
            if limit < 7 {
                let err = res.unwrap_err();
                assert_eq!(err.status, Status::WorkExhausted);
                assert_eq!(err.work_used, limit);
            } else {
                assert!(res.unwrap().is_continue());
                assert_eq!(calls, 3); // root(net=2) + right(7,8 net=3)
                assert_eq!(work.used(), 7);
            }
        }

        // 3. net=0 with Some(0), disjoint q on uniform net=1, and sibling right subtree continuation.
        let mut net0_idx = SpatialIndex::new();
        for k in 1..=4u64 {
            let x = k as i64 * 10;
            net0_idx.insert(
                k,
                k as u32,
                0,
                CopperAabb::from_bounds(([x, 0], [x + 4, 4])),
            );
        }
        let w_n0 = WorkBudget::new(Limits::default());
        let _sn0 = w_n0.scope();
        let mut n0_calls = 0u64;
        assert!(net0_idx
            .visit_matching(q_sub, Some(0), &w_n0, |_, _| {
                n0_calls += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            })
            .unwrap()
            .is_continue());
        assert_eq!(n0_calls, 4);
        assert_eq!(w_n0.used(), 4);
        drop(_sn0);

        let mut dis_idx = SpatialIndex::new();
        for k in 1..=6u64 {
            let x = 100 + k as i64 * 10;
            dis_idx.insert(
                k,
                k as u32,
                1,
                CopperAabb::from_bounds(([x, 0], [x + 4, 4])),
            );
        }
        let q_dis = SweptAabb::from_copper(CopperAabb::from_bounds(([0, 0], [10, 10])));
        for limit in 0..=1u64 {
            let work = WorkBudget::new(Limits {
                max_work: limit,
                ..Limits::default()
            });
            let _s = work.scope();
            let mut calls = 0u64;
            let res = dis_idx.visit_matching(q_dis, Some(1), &work, |_, _| {
                calls += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            });
            if limit == 0 {
                assert_eq!(res.unwrap_err().work_used, 0);
            } else {
                assert!(res.unwrap().is_continue());
                assert_eq!(calls, 0);
                assert_eq!(work.used(), 1);
            }
        }

        // 4. Sibling right subtree continuation after left uniform subtree skip.
        let mut sib = SpatialIndex::new();
        for (k, net) in [(1u64, 1u32), (2, 1), (3, 1), (6, 1), (7, 2)] {
            let x = k as i64 * 10;
            sib.insert(
                k,
                k as u32,
                net,
                CopperAabb::from_bounds(([x, 0], [x + 4, 4])),
            );
        }
        let w_sib = WorkBudget::new(Limits::default());
        let _ssib = w_sib.scope();
        let mut sib_calls = 0u64;
        assert!(sib
            .visit_matching(q_sub, Some(1), &w_sib, |_, _| {
                sib_calls += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            })
            .unwrap()
            .is_continue());
        assert_eq!(sib_calls, 1);
        assert_eq!(w_sib.used(), 5);
        drop(_ssib);

        // 5. Rotations (insert + remove), left-only/right-only child refresh, and consistent() corruptions.
        let mut rot = SpatialIndex::new();
        let mut exp_rot: BTreeMap<u64, (u32, CopperAabb)> = BTreeMap::new();
        for k in 1..=16u64 {
            let net = if matches!(k, 1 | 8 | 16) { 2 } else { 1 };
            let b = CopperAabb::from_bounds(([k as i64 * 10, 0], [k as i64 * 10 + 4, 4]));
            rot.insert(k, k as u32, net, b);
            exp_rot.insert(k, (net, b));
        }
        assert!(rot.consistent(exp_rot.iter().map(|(&k, &(net, b))| (k, k as u32, net, b))));
        let q_rot = SweptAabb::from_copper(CopperAabb::from_bounds(([0, -2], [200, 8])));
        let w_rot1 = WorkBudget::new(Limits::default());
        let _sr1 = w_rot1.scope();
        let mut rot_calls = 0u64;
        assert!(rot
            .visit_matching(q_rot, Some(1), &w_rot1, |_, _| {
                rot_calls += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            })
            .unwrap()
            .is_continue());
        assert_eq!(rot_calls, 3);
        drop(_sr1);

        for k in [1u64, 8, 16] {
            let (_, b) = exp_rot.remove(&k).unwrap();
            rot.remove(&k, b);
        }
        assert!(rot.consistent(exp_rot.iter().map(|(&k, &(net, b))| (k, k as u32, net, b))));
        let w_rot2 = WorkBudget::new(Limits::default());
        let _sr2 = w_rot2.scope();
        let mut rot_calls2 = 0u64;
        assert!(rot
            .visit_matching(q_rot, Some(1), &w_rot2, |_, _| {
                rot_calls2 += 1;
                Ok::<_, Failure>(ControlFlow::Continue(()))
            })
            .unwrap()
            .is_continue());
        assert_eq!(rot_calls2, 0);
        assert_eq!(w_rot2.used(), 13);
        drop(_sr2);

        // Left-only and right-only 2-node child refresh (root net=1, child net=2).
        for (root_k, child_k) in [(6u64, 1u64), (6u64, 7u64)] {
            let mut lr = SpatialIndex::new();
            let br =
                CopperAabb::from_bounds(([root_k as i64 * 10, 0], [root_k as i64 * 10 + 4, 4]));
            let bc =
                CopperAabb::from_bounds(([child_k as i64 * 10, 0], [child_k as i64 * 10 + 4, 4]));
            lr.insert(root_k, root_k as u32, 1, br);
            lr.insert(child_k, child_k as u32, 2, bc);
            assert!(lr.consistent([
                (root_k, root_k as u32, 1, br),
                (child_k, child_k as u32, 2, bc)
            ]));
            let w_lr = WorkBudget::new(Limits::default());
            let _slr = w_lr.scope();
            let mut lr_calls = 0u64;
            assert!(lr
                .visit_matching(q_sub, Some(1), &w_lr, |_, _| {
                    lr_calls += 1;
                    Ok::<_, Failure>(ControlFlow::Continue(()))
                })
                .unwrap()
                .is_continue());
            assert_eq!(lr_calls, 1);
        }

        // Direct consistent() corruptions on subtree_uniform_net, subtree_len, and node.net.
        let mut cor = SpatialIndex::new();
        let b1 = CopperAabb::from_bounds(([10, 0], [14, 4]));
        let b6 = CopperAabb::from_bounds(([60, 0], [64, 4]));
        cor.insert(1u64, 1, 1, b1);
        cor.insert(6u64, 6, 1, b6);
        let exp_cor = [(1u64, 1u32, 1u32, b1), (6u64, 6u32, 1u32, b6)];
        assert!(cor.consistent(exp_cor));
        let r = cor.root;
        let l = cor.node(r).left;
        cor.node_mut(r).subtree_uniform_net = 0;
        assert!(!cor.consistent(exp_cor));
        cor.node_mut(r).subtree_uniform_net = 2;
        assert!(!cor.consistent(exp_cor));
        cor.node_mut(r).subtree_uniform_net = 1;
        cor.node_mut(l).subtree_uniform_net = 0;
        assert!(!cor.consistent(exp_cor));
        cor.node_mut(l).subtree_uniform_net = 1;
        cor.node_mut(r).subtree_len += 1;
        assert!(!cor.consistent(exp_cor));
        cor.node_mut(r).subtree_len -= 2;
        assert!(!cor.consistent(exp_cor));
        cor.node_mut(r).subtree_len = 0;
        assert!(!cor.consistent(exp_cor));
        cor.node_mut(r).subtree_len = 2;
        cor.node_mut(l).net = 2;
        assert!(!cor.consistent(exp_cor));
    }

    #[test]
    fn query_four_direction_boundary_tangency_and_negative_controls() {
        let target = CopperAabb::from_bounds(([10, 20], [30, 40]));
        let cases = [
            (
                "left_x_min",
                CopperAabb::from_bounds(([0, 25], [10, 35])),
                CopperAabb::from_bounds(([0, 25], [9, 35])),
            ),
            (
                "right_x_max",
                CopperAabb::from_bounds(([30, 25], [40, 35])),
                CopperAabb::from_bounds(([31, 25], [40, 35])),
            ),
            (
                "bottom_y_min",
                CopperAabb::from_bounds(([15, 10], [25, 20])),
                CopperAabb::from_bounds(([15, 10], [25, 19])),
            ),
            (
                "top_y_max",
                CopperAabb::from_bounds(([15, 40], [25, 50])),
                CopperAabb::from_bounds(([15, 41], [25, 50])),
            ),
        ];

        // Single-item indexes.
        let s1 = StaticSpatialIndex::new([(0u32, 1u32, target, 0u32)]);
        let mut t1 = SpatialIndex::new();
        t1.insert(1u64, 0u32, 1u32, target);

        // Multi-item indexes (34 items; others lie inside y=[25, 35] and x>=1000 so
        // internal/root envelopes preserve [20, 40] y-bounds and x_min=10).
        let mut s_items = vec![(0u32, 1u32, target, 0u32)];
        let mut t_multi = SpatialIndex::new();
        t_multi.insert(1u64, 0u32, 1u32, target);
        for i in 1..34u32 {
            let x = 1000 + i64::from(i) * 10;
            let b = CopperAabb::from_bounds(([x, 25], [x + 4, 35]));
            s_items.push((i, (i % 3) + 1, b, i));
            t_multi.insert(u64::from(i) + 1, i, (i % 3) + 1, b);
        }
        let s_multi = StaticSpatialIndex::new(s_items);

        for (label, hit_box, miss_box) in cases {
            let hit = SweptAabb::from_copper(hit_box);
            let miss = SweptAabb::from_copper(miss_box);
            let w = WorkBudget::new(Limits::default());
            assert_eq!(s1.query(hit, &w).unwrap(), vec![0], "static1 {label}");
            assert_eq!(
                s1.query(miss, &w).unwrap(),
                Vec::<u32>::new(),
                "static1 miss {label}"
            );
            assert_eq!(t1.query(hit, &w).unwrap(), vec![&1u64], "treap1 {label}");
            assert_eq!(
                t1.query(miss, &w).unwrap(),
                Vec::<&u64>::new(),
                "treap1 miss {label}"
            );
            assert_eq!(s_multi.query(hit, &w).unwrap(), vec![0], "static34 {label}");
            assert_eq!(
                s_multi.query(miss, &w).unwrap(),
                Vec::<u32>::new(),
                "static34 miss {label}"
            );
            assert_eq!(
                t_multi.query(hit, &w).unwrap(),
                vec![&1u64],
                "treap34 {label}"
            );
            assert_eq!(
                t_multi.query(miss, &w).unwrap(),
                Vec::<&u64>::new(),
                "treap34 miss {label}"
            );
        }
    }
}
