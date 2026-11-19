//! Opt-in single-ended bus preferences. A group shares a direction while each
//! member keeps its own transverse position between the two terminal banks.
use crate::budget::WorkBudget;
use crate::model::{BusOptions, Failure, Pad, Point};
use crate::negotiate::{RouteUnitId, UnitSpec};
use std::collections::BTreeMap;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Span {
    a: Point,
    b: Point,
}

fn vector(a: Point, b: Point) -> [i128; 2] {
    [
        i128::from(b[0]) - i128::from(a[0]),
        i128::from(b[1]) - i128::from(a[1]),
    ]
}
fn dot(a: [i128; 2], b: [i128; 2]) -> i128 {
    a[0] * b[0] + a[1] * b[1]
}
fn cross(a: [i128; 2], b: [i128; 2]) -> i128 {
    a[0] * b[1] - a[1] * b[0]
}
fn distance2(a: Point, b: Point) -> i128 {
    let d = vector(a, b);
    dot(d, d)
}
impl Span {
    fn delta(self) -> [i128; 2] {
        vector(self.a, self.b)
    }
    fn align(self, direction: [i128; 2]) -> Self {
        if dot(direction, self.delta()) < 0 {
            Self {
                a: self.b,
                b: self.a,
            }
        } else {
            self
        }
    }
    fn neighbors(self, other: Self, radius2: i128) -> bool {
        let d = self.delta();
        let other = other.align(d);
        let e = other.delta();
        // Banks must be distinct, and lanes approximately parallel (within 30 degrees).
        if !parallel(d, e) {
            return false;
        }
        let a2 = distance2(self.a, other.a);
        let b2 = distance2(self.b, other.b);
        let bank_separation2 = dot(d, d).min(dot(e, e));
        if a2.max(b2) > radius2 || 4 * a2.max(b2) >= bank_separation2 {
            return false;
        }
        // Side-by-side lanes preserve their physical ordering at both banks.
        // Traces in series and crossed endpoint assignments have no shared lane.
        let dir = [d[0] + e[0], d[1] + e[1]];
        let first = cross(dir, vector(self.a, other.a));
        let last = cross(dir, vector(self.b, other.b));
        first != 0 && first.signum() == last.signum()
    }
}

// Use the longer of the two axis-extreme spans. Two-pad nets are exact;
// a branch pad need not disqualify the net or be assigned to a different net.
struct Extrema {
    x_min: Point,
    x_max: Point,
    y_min: Point,
    y_max: Point,
}
impl Extrema {
    fn new(p: Point) -> Self {
        Self {
            x_min: p,
            x_max: p,
            y_min: p,
            y_max: p,
        }
    }
    fn add(&mut self, p: Point) {
        self.x_min = self.x_min.min(p);
        self.x_max = self.x_max.max(p);
        if (p[1], p[0]) < (self.y_min[1], self.y_min[0]) {
            self.y_min = p;
        }
        if (p[1], p[0]) > (self.y_max[1], self.y_max[0]) {
            self.y_max = p;
        }
    }
    fn span(&self) -> Option<Span> {
        let (a, b) = if distance2(self.x_min, self.x_max) >= distance2(self.y_min, self.y_max) {
            (self.x_min, self.x_max)
        } else {
            (self.y_min, self.y_max)
        };
        (a != b).then_some(Span { a, b })
    }
}

#[derive(Clone, Copy)]
struct Lane {
    span: Span,
    length_squared: i128,
}
impl Lane {
    // Midpoint distance to the finite lane. Integer half-nanometres keep
    // queue preferences identical for the same edge in either search.
    fn rank(self, a: Point, b: Point, options: BusOptions) -> u32 {
        let d = self.span.delta();
        let mid = [
            i128::from(a[0]) + i128::from(b[0]) - 2 * i128::from(self.span.a[0]),
            i128::from(a[1]) + i128::from(b[1]) - 2 * i128::from(self.span.a[1]),
        ];
        let projection = dot(mid, d);
        let distance2x = if projection <= 0 {
            dot(mid, mid).isqrt()
        } else if projection >= 2 * self.length_squared {
            let tail = [mid[0] - 2 * d[0], mid[1] - 2 * d[1]];
            dot(tail, tail).isqrt()
        } else if d[1] == 0 {
            mid[1].abs()
        } else if d[0] == 0 {
            mid[0].abs()
        } else {
            // Validated board coordinates bound the squared cross product.
            (cross(mid, d).pow(2) / self.length_squared).isqrt()
        };
        // The attraction bonus sets the number of distance bins in thousandths.
        // This rank breaks equal route-cost ties, including in reverse search.
        let radius2x = 2 * i128::from(options.attraction_radius_nm);
        let scaled_distance = i128::from(options.attraction_bonus) * distance2x.min(radius2x);
        ((scaled_distance + 1000 * radius2x - 1) / (1000 * radius2x)) as u32
    }
}
#[derive(Default)]
pub(crate) struct BusPlan {
    pub(crate) groups: Vec<Vec<u32>>,
    lanes: BTreeMap<u32, Lane>,
}
fn parallel(d: [i128; 2], e: [i128; 2]) -> bool {
    let p = dot(d, e);
    4 * p * p >= 3 * dot(d, d) * dot(e, e)
}

// A string of locally neighbouring lanes can drift around a corner, extend
// indefinitely along the board, or connect two crossed assignments through a
// third net. Grow groups with an actual common run and consistent bank order.
fn coherent_groups(
    spans: &[(u32, Span)],
    radius: i64,
    work: &WorkBudget,
) -> Result<Vec<Vec<(u32, Span)>>, Failure> {
    let cell_width = 2 * i128::from(radius);
    let radius2 = i128::from(radius).pow(2);
    // If both ends are within r of a neighbour's ends, their midpoints must
    // also be within r, independently of span orientation.
    let cell = |span: Span| {
        [0, 1].map(|i| i128::from(span.a[i] + span.b[i]).div_euclid(cell_width) as i64)
    };
    let mut cells = BTreeMap::<[i64; 2], Vec<usize>>::new();
    work.charge(spans.len() as u64)?;
    for (i, &(_, span)) in spans.iter().enumerate() {
        cells.entry(cell(span)).or_default().push(i);
    }
    let mut groups = Vec::new();
    let mut claimed = vec![false; spans.len()];
    for (seed, &(_, reference)) in spans.iter().enumerate() {
        if claimed[seed] {
            continue;
        }
        let axis = reference.delta();
        let mut common_start = 0;
        let mut common_end = dot(axis, axis);
        let mut longest = common_end;
        let mut bank_order = BTreeMap::from([(0, 0)]);
        let mut members = vec![seed];
        claimed[seed] = true;
        let mut next = 0;
        while let Some(&i) = members.get(next) {
            next += 1;
            let [x, y] = cell(spans[i].1);
            work.charge(9)?;
            let neighbours = (x - 1..=x + 1).flat_map(|cx| (y - 1..=y + 1).map(move |cy| [cx, cy]));
            for bucket in neighbours.filter_map(|key| cells.get(&key)) {
                work.charge(bucket.len() as u64)?;
                for &j in bucket {
                    let candidate = spans[j].1;
                    if claimed[j] || !spans[i].1.neighbors(candidate, radius2) {
                        continue;
                    }
                    let span = candidate.align(axis);
                    if !parallel(axis, span.delta()) {
                        continue;
                    }
                    let a = vector(reference.a, span.a);
                    let b = vector(reference.a, span.b);
                    let lo = dot(axis, a);
                    let hi = dot(axis, b);
                    let start = common_start.max(lo);
                    let end = common_end.min(hi);
                    let length = longest.max(hi - lo);
                    if 2 * (end - start) < length {
                        continue;
                    }
                    let left = cross(axis, a);
                    let right = cross(axis, b);
                    if bank_order.contains_key(&left)
                        || bank_order
                            .range(..left)
                            .next_back()
                            .is_some_and(|(_, v)| *v >= right)
                        || bank_order
                            .range(left..)
                            .next()
                            .is_some_and(|(_, v)| *v <= right)
                    {
                        continue;
                    }
                    bank_order.insert(left, right);
                    common_start = start;
                    common_end = end;
                    longest = length;
                    claimed[j] = true;
                    members.push(j);
                }
            }
        }
        groups.push(members.into_iter().map(|i| spans[i]).collect());
    }
    Ok(groups)
}
impl BusPlan {
    pub(crate) fn build(
        units: &mut [UnitSpec],
        pads: &[Pad],
        options: BusOptions,
        work: &WorkBudget,
    ) -> Result<Self, Failure> {
        let mut banks: BTreeMap<u32, Option<Extrema>> = BTreeMap::new();
        work.charge(units.len() as u64)?;
        for unit in units.iter() {
            if let RouteUnitId::SingleNet(net) = unit.id {
                banks.insert(net, None);
            }
        }
        let mut plan = Self::default();
        if banks.len() < options.min_nets {
            return Ok(plan);
        }
        work.charge(pads.len() as u64)?;
        for pad in pads {
            if let Some(bank) = banks.get_mut(&pad.net) {
                bank.get_or_insert_with(|| Extrema::new(pad.at)).add(pad.at);
            }
        }
        let spans: Vec<_> = banks
            .into_iter()
            .filter_map(|(net, e)| Some((net, e?.span()?)))
            .collect();
        let count = spans.len();
        if count < options.min_nets {
            return Ok(plan);
        }
        let clusters = coherent_groups(&spans, options.detection_radius_nm, work)?;
        work.charge(units.len() as u64)?;
        let mut slots = BTreeMap::new();
        for (i, unit) in units.iter().enumerate() {
            if let RouteUnitId::SingleNet(net) = unit.id {
                slots.insert(net, i);
            }
        }
        for mut group in clusters.into_iter().filter(|g| g.len() >= options.min_nets) {
            let n = group.len();
            work.charge(n as u64 * (6 + u64::from(n.ilog2()) * 3))?;
            let seed = group[0].1.delta();
            let mut sum = [0i128; 2];
            for (_, span) in &mut group {
                *span = span.align(seed);
                let d = span.delta();
                sum[0] += d[0];
                sum[1] += d[1];
            }
            // Canonical major-axis sign makes physical order independent of pin numbering.
            let major = usize::from(sum[1].abs() > sum[0].abs());
            if sum[major] < 0 {
                sum = [-sum[0], -sum[1]];
            }
            let mean = [sum[0] / n as i128, sum[1] / n as i128];
            if mean == [0, 0] {
                continue;
            }
            group.sort_unstable_by_key(|&(net, span)| {
                let mid2 = [
                    i128::from(span.a[0] + span.b[0]),
                    i128::from(span.a[1] + span.b[1]),
                ];
                (cross(mean, mid2), net)
            });
            let mut nets = Vec::with_capacity(n);
            for &(net, span) in &group {
                let start = [
                    (i128::from(span.a[0] + span.b[0]) - mean[0]) / 2,
                    (i128::from(span.a[1] + span.b[1]) - mean[1]) / 2,
                ];
                let lane = Span {
                    a: [start[0] as i64, start[1] as i64],
                    b: [(start[0] + mean[0]) as i64, (start[1] + mean[1]) as i64],
                };
                plan.lanes.insert(
                    net,
                    Lane {
                        span: lane,
                        length_squared: dot(mean, mean),
                    },
                );
                nets.push(net);
            }
            // Permute only the original slots of this group. Other units,
            // including coupled pairs, retain their positions and priorities.
            let mut positions: Vec<_> = nets.iter().map(|n| slots[n]).collect();
            positions.sort_unstable();
            let mut middle_out: Vec<_> = (0..n).collect();
            middle_out.sort_unstable_by_key(|&i| ((2 * i).abs_diff(n - 1), i));
            for (slot, idx) in positions.into_iter().zip(middle_out) {
                let desired = nets[idx];
                let current = slots[&desired];
                let RouteUnitId::SingleNet(displaced) = units[slot].id else {
                    unreachable!()
                };
                units.swap(slot, current);
                slots.insert(desired, slot);
                slots.insert(displaced, current);
            }
            plan.groups.push(nets);
        }
        Ok(plan)
    }
    pub(crate) fn edge_rank(
        &self,
        net: u32,
        a: Point,
        b: Point,
        options: BusOptions,
        work: &WorkBudget,
    ) -> Result<u32, Failure> {
        if let Some(&lane) = self.lanes.get(&net) {
            work.charge(1)?;
            return Ok(lane.rank(a, b, options));
        }
        Ok(0)
    }
    pub(crate) fn finish(&mut self) {
        self.lanes.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{Limits, Status};

    fn unit(id: RouteUnitId) -> UnitSpec {
        UnitSpec {
            id,
            nets: match id {
                RouteUnitId::DiffPair(_) => vec![20, 21],
                RouteUnitId::SingleNet(net) => vec![net],
            },
            priority_tier: 1,
            rippable_by_single: true,
        }
    }
    fn pad(net: u32, at: Point) -> Pad {
        serde_json::from_value(serde_json::json!({"id":"00000000-0000-4000-8000-000000000001","at":at,"net":net,"shape":{"kind":"circle","radius_nm":1}})).unwrap()
    }
    #[test]
    fn bus_group_uses_both_banks_and_only_scheduled_single_ended_nets() {
        use RouteUnitId::{DiffPair, SingleNet};
        // IDs differ from the order of the lanes. One lane has a test point.
        let ids = [7, 3, 9, 4, 2, 5, 1, 8];
        let mut pads = Vec::new();
        for (i, net) in ids.iter().enumerate() {
            let y = 10 * i as i64;
            pads.extend([pad(*net, [0, y]), pad(*net, [200, y])]);
        }
        pads.push(pad(4, [100, 80]));
        for (net, a, b) in [
            (10, [0, 140], [200, 140]),
            (11, [0, 75], [15, 250]),
            (20, [0, 25], [200, 25]),
            (21, [0, 35], [200, 35]),
            (33, [0, 55], [200, 55]),
        ] {
            pads.extend([pad(net, a), pad(net, b)]);
        }
        pads.reverse();
        let mut units = vec![unit(DiffPair(0)), unit(SingleNet(10)), unit(SingleNet(11))];
        units.extend(ids.into_iter().rev().map(|n| unit(SingleNet(n))));
        let options = BusOptions {
            detection_radius_nm: 15,
            attraction_radius_nm: 25,
            min_nets: 3,
            ..Default::default()
        };
        let work = WorkBudget::new(Limits::default());
        let plan = BusPlan::build(&mut units, &pads, options, &work).unwrap();
        assert_eq!(plan.groups, [ids]);
        assert_eq!(
            &units.iter().map(|u| u.id).collect::<Vec<_>>(),
            &[
                DiffPair(0),
                SingleNet(10),
                SingleNet(11),
                SingleNet(4),
                SingleNet(2),
                SingleNet(9),
                SingleNet(5),
                SingleNet(3),
                SingleNet(1),
                SingleNet(7),
                SingleNet(8)
            ]
        );
        for excluded in [10, 11, 20, 21, 33] {
            assert_eq!(
                plan.edge_rank(excluded, [5, 100], [6, 100], options, &work)
                    .unwrap(),
                0
            );
        }
        let forward = plan
            .edge_rank(4, [20, 60], [30, 60], options, &work)
            .unwrap();
        assert!(forward > 0);
        assert_eq!(
            forward,
            plan.edge_rank(4, [30, 60], [20, 60], options, &work)
                .unwrap()
        );
        assert_eq!(
            plan.edge_rank(4, [20, 30], [30, 30], options, &work)
                .unwrap(),
            0
        );
    }
    #[test]
    fn bus_local_neighbors_must_share_a_longitudinal_run() {
        // Each successive lane is in range of its immediate neighbor. The
        // terminals drift along X until the end lanes no longer overlap.
        let spans: Vec<_> = (0..7)
            .map(|i| {
                (
                    i as u32,
                    Span {
                        a: [30 * i, 5 * i],
                        b: [30 * i + 100, 5 * i],
                    },
                )
            })
            .collect();
        let groups = coherent_groups(&spans, 40, &WorkBudget::new(Limits::default())).unwrap();
        assert!(groups.len() > 1);
        assert!(groups.iter().any(|group| group.len() >= 2));
        for group in groups {
            let start = group.iter().map(|(_, s)| s.a[0]).max().unwrap();
            let end = group.iter().map(|(_, s)| s.b[0]).min().unwrap();
            assert!(end - start >= 50);
        }
    }
    #[test]
    fn bus_edge_rank_is_symmetric_bounded_and_zero_on_the_lane() {
        let span = Span {
            a: [0, 0],
            b: [100, 0],
        };
        let lane = Lane {
            span,
            length_squared: 10000,
        };
        let points = ([20, -20], [30, -20]);
        for radius in [1, 20, i64::MAX] {
            for bonus in [0, 1, 5000, u32::MAX] {
                let options = BusOptions {
                    attraction_radius_nm: radius,
                    attraction_bonus: bonus,
                    ..Default::default()
                };
                let cost = lane.rank(points.0, points.1, options);
                assert_eq!(cost, lane.rank(points.1, points.0, options));
                assert_eq!(cost > 0, bonus > 0);
                assert_eq!(lane.rank([10, 0], [20, 0], options), 0);
                assert!(cost <= bonus.div_ceil(1000));
            }
        }
    }
    #[test]
    fn bus_neighbor_discovery_charges_work_before_comparing_all_lanes() {
        let mut units: Vec<_> = (1..=8).map(|n| unit(RouteUnitId::SingleNet(n))).collect();
        let pads: Vec<_> = (1..=8)
            .flat_map(|n| {
                [
                    pad(n, [0, 10 * i64::from(n)]),
                    pad(n, [200, 10 * i64::from(n)]),
                ]
            })
            .collect();
        let work = WorkBudget::new(Limits {
            max_work: 35,
            ..Default::default()
        });
        let err = BusPlan::build(
            &mut units,
            &pads,
            BusOptions {
                detection_radius_nm: i64::MAX,
                ..Default::default()
            },
            &work,
        )
        .err()
        .unwrap();
        assert_eq!(err.status, Status::WorkExhausted);
        assert!(work.used() > units.len() as u64 + pads.len() as u64);
        assert!(work.used() <= 35);
    }
}
