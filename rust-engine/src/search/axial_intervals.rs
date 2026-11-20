//! Bidirectional line search on one layer. A line is the maximal swept-clear
//! axial run containing a seed, sampled to grid-pitch resolution. Intersecting
//! opposite lines give a complete physical route; missing lines prove nothing.
use super::{monotone_trunk, Engine, Failure, Path, PathEdge, Point, TargetBounds};
use crate::negotiate::{BlockedFrontier, CongestionHistory, TrialBudget};
use std::cmp::Reverse;
use std::collections::{BTreeMap, BinaryHeap};

#[derive(Clone, Copy)]
struct Line {
    pivot: Point,
    axis: usize,
    lo: i64,
    hi: i64,
    previous: Option<usize>,
    walked: u64,
    depth: usize,
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct Turns {
    line: usize,
    low: i64,
    high: i64,
}

#[derive(Default)]
struct Wave {
    lines: Vec<Line>,
    on_track: BTreeMap<(usize, i64), Vec<usize>>,
    turns: BinaryHeap<Reverse<(u32, u64, usize, Reverse<u64>, Turns)>>,
}

fn manhattan(a: Point, b: Point) -> u64 {
    a[0].abs_diff(b[0]) + a[1].abs_diff(b[1])
}

struct WaveSearch<'a> {
    engine: &'a Engine,
    net: u32,
    layer: usize,
    grid: i64,
    guides: [Point; 2],
    occupied: usize,
    max_steps: u64,
    wave: [Wave; 2],
}

impl WaveSearch<'_> {
    fn storage(&self, extra: usize) -> Result<(), Failure> {
        self.engine.work.search_storage(
            self.occupied
                + extra
                + self
                    .wave
                    .iter()
                    .map(|w| w.lines.len() * 3 + w.turns.len())
                    .sum::<usize>(),
        )
    }

    fn extent(&self, p: Point, axis: usize, dir: i64) -> Result<i64, Failure> {
        let bound = if dir < 0 {
            self.engine.input.bounds.min[axis]
        } else {
            self.engine.input.bounds.max[axis]
        };
        let max = (bound - p[axis]).abs() / self.grid;
        let free = monotone_trunk::clear_span(max, |n| {
            self.engine.work.charge(1)?;
            let mut q = p;
            q[axis] += dir * n * self.grid;
            self.engine.legal_edge_on(self.net, self.layer, p, q)
        })?;
        Ok(p[axis] + dir * free * self.grid)
    }

    fn push_turns(
        &mut self,
        side: usize,
        index: usize,
        low: i64,
        high: i64,
        split: u32,
    ) -> Result<(), Failure> {
        self.engine.work.charge(1)?;
        if low > high {
            return Ok(());
        }
        let line = self.wave[side].lines[index];
        let mut p = line.pivot;
        p[line.axis] = low + ((high - low) / self.grid / 2) * self.grid;
        let g = line.walked + manhattan(line.pivot, p);
        let goal = self.guides[1 - side];
        let rate = self.engine.layer_policy.weight(self.layer);
        if (g + manhattan(p, goal))
            .div_ceil(self.grid as u64)
            .saturating_mul(rate)
            > self.max_steps
        {
            return Ok(());
        }
        let mut closest = line.pivot;
        closest[line.axis] = goal[line.axis].clamp(line.lo, line.hi);
        let f = g + 2 * manhattan(p, goal) + 2 * manhattan(closest, goal);
        self.storage(1)?;
        self.wave[side].turns.push(Reverse((
            split,
            f,
            line.depth,
            Reverse(low.abs_diff(high)),
            Turns {
                line: index,
                low,
                high,
            },
        )));
        Ok(())
    }

    fn add_line(
        &mut self,
        side: usize,
        p: Point,
        axis: usize,
        parent: Option<usize>,
    ) -> Result<Option<Path>, Failure> {
        self.engine.work.charge(1)?;
        let track = (axis, p[1 - axis]);
        if let Some(indices) = self.wave[side].on_track.get(&track) {
            for &i in indices {
                self.engine.work.charge(1)?;
                let old = &self.wave[side].lines[i];
                if old.lo <= p[axis] && p[axis] <= old.hi {
                    return Ok(None);
                }
            }
        }
        if !self.engine.legal_edge_on(self.net, self.layer, p, p)? {
            return Ok(None);
        }
        let lo = self.extent(p, axis, -1)?;
        let hi = self.extent(p, axis, 1)?;
        let (walked, depth) = parent.map_or((0, 0), |i| {
            let l = self.wave[side].lines[i];
            (l.walked + manhattan(l.pivot, p), l.depth + 1)
        });
        let index = self.wave[side].lines.len();
        self.storage(3)?;
        self.engine.work.charge(3)?;
        self.wave[side].lines.push(Line {
            pivot: p,
            axis,
            lo,
            hi,
            previous: parent,
            walked,
            depth,
        });
        self.wave[side]
            .on_track
            .entry(track)
            .or_default()
            .push(index);
        let other = &self.wave[1 - side];
        let mut best = None;
        for (&(_, fixed), opposite) in other.on_track.range((1 - axis, lo)..=(1 - axis, hi)) {
            for &i in opposite {
                self.engine.work.charge(1)?;
                let line = &other.lines[i];
                if line.lo <= p[1 - axis] && p[1 - axis] <= line.hi {
                    let mut meet = p;
                    meet[axis] = fixed;
                    let length =
                        walked + manhattan(p, meet) + line.walked + manhattan(line.pivot, meet);
                    if best.is_none_or(|old| (length, i, meet) < old) {
                        best = Some((length, i, meet));
                    }
                }
            }
        }
        if let Some((_, i, meet)) = best {
            let ids = if side == 0 { [index, i] } else { [i, index] };
            if let Some(path) = self.connect_at(ids, meet)? {
                return Ok(Some(path));
            }
        }
        if lo >= hi {
            return Ok(None);
        }
        let target = self.guides[1 - side][axis].clamp(lo, hi);
        // Wall ends and the opposite terminal receive individual turns.
        // Subdivide the remaining free line lazily; narrow channels may be
        // reachable only from a coordinate between those three points.
        self.push_turns(side, index, lo, lo, 0)?;
        self.push_turns(side, index, hi, hi, 0)?;
        if lo < target && target < hi {
            self.push_turns(side, index, target, target, 0)?;
        }
        self.push_turns(side, index, lo + self.grid, target - self.grid, 1)?;
        self.push_turns(side, index, target + self.grid, hi - self.grid, 1)?;
        Ok(None)
    }

    // Slide the middle segment of an axial U toward the two open ends.
    // Both legs get shorter. Each accepted new middle segment is checked
    // along its full length, even when an obstruction has several openings.
    fn tighten_crossbar(
        &self,
        a: Point,
        b: Point,
        c: Point,
        d: Point,
    ) -> Result<Option<(Point, Point)>, Failure> {
        let axis = usize::from(a[0] == b[0]);
        let direction = (a[axis] - b[axis]).signum();
        if direction == 0
            || b[axis] != c[axis]
            || c[1 - axis] != d[1 - axis]
            || direction != (d[axis] - c[axis]).signum()
        {
            return Ok(None);
        }
        let mut limit = a[axis].abs_diff(b[axis]).min(d[axis].abs_diff(c[axis])) as i64;
        let mut free = 0;
        let mut shift = limit;
        let mut best = None;
        while shift > free {
            self.engine.work.charge(1)?;
            let mut start = b;
            let mut end = c;
            start[axis] += direction * shift;
            end[axis] += direction * shift;
            if self
                .engine
                .legal_edge_on(self.net, self.layer, start, end)?
            {
                best = Some((start, end));
                free = shift;
            } else {
                limit = shift;
            }
            let cells = (limit - free) / self.grid;
            shift = free + ((cells + 1) / 2) * self.grid;
            if shift >= limit {
                break;
            }
        }
        Ok(best)
    }

    fn tighten(&self, points: &mut Vec<Point>) -> Result<(), Failure> {
        for _ in 0..points.len() {
            let mut changed = false;
            let mut i = 1;
            while i < points.len() {
                self.engine.work.charge(1)?;
                let a = points[i - 1];
                let b = points[i];
                if a == b
                    || points
                        .get(i + 1)
                        .is_some_and(|c| (0..2).any(|k| a[k] == b[k] && b[k] == c[k]))
                {
                    self.engine.work.charge((points.len() - i) as u64)?;
                    points.remove(i);
                    changed = true;
                    continue;
                }
                if let Some(&d) = points.get(i + 2) {
                    if let Some((start, end)) = self.tighten_crossbar(a, b, points[i + 1], d)? {
                        points[i] = start;
                        points[i + 1] = end;
                        changed = true;
                    }
                }
                i += 1;
            }
            if !changed {
                break;
            }
        }
        Ok(())
    }

    fn connect_at(&self, ids: [usize; 2], meet: Point) -> Result<Option<Path>, Failure> {
        let mut points = Vec::new();
        for (side, root) in ids.into_iter().enumerate() {
            let mut endpoint = Some(root);
            if side == 0 {
                points.push(meet);
            }
            while let Some(i) = endpoint {
                self.engine.work.charge(1)?;
                self.storage(points.len() + 1)?;
                let l = self.wave[side].lines[i];
                if points.last() != Some(&l.pivot) {
                    points.push(l.pivot);
                }
                endpoint = l.previous;
            }
            if side == 0 {
                self.engine.work.charge(points.len() as u64)?;
                points.reverse();
            }
        }
        self.tighten(&mut points)?;
        if points.len() < 2 {
            return Ok(None);
        }
        self.storage(points.len().saturating_mul(3))?;
        let mut chamfered = vec![points[0]];
        let clear = |a, b| self.engine.legal_edge_on(self.net, self.layer, a, b);
        for pair in points[1..].windows(2) {
            self.engine.work.charge(1)?;
            monotone_trunk::chamfer_corner(&mut chamfered, pair[0], pair[1], self.grid, &clear)?;
        }
        let end = *points.last().expect("terminal is present");
        if chamfered.last() != Some(&end) {
            chamfered.push(end);
        }
        points = chamfered;
        let mut edges = Vec::new();
        let mut steps = 0u64;
        for w in points.windows(2) {
            self.engine.work.charge(1)?;
            self.storage(points.len() + edges.len() + 1)?;
            if !self
                .engine
                .legal_edge_on(self.net, self.layer, w[0], w[1])?
            {
                return Ok(None);
            }
            steps = steps.saturating_add(
                (w[0][0].abs_diff(w[1][0]).div_ceil(self.grid as u64)
                    + w[0][1].abs_diff(w[1][1]).div_ceil(self.grid as u64))
                .saturating_mul(self.engine.layer_policy.weight(self.layer)),
            );
            edges.push(PathEdge::Track {
                start: w[0],
                end: w[1],
                layer: self.layer as u8,
            });
        }
        Ok(
            (!edges.is_empty() && steps <= self.max_steps).then_some(Path {
                edges,
                points,
                steps,
            }),
        )
    }

    fn advance(&mut self, side: usize) -> Result<Option<Path>, Failure> {
        self.engine.work.charge(1)?;
        let Some(Reverse((split, _, _, _, t))) = self.wave[side].turns.pop() else {
            return Ok(None);
        };
        let l = self.wave[side].lines[t.line];
        let mut p = l.pivot;
        let x = t.low + ((t.high - t.low) / self.grid / 2) * self.grid;
        p[l.axis] = x;
        self.push_turns(side, t.line, t.low, x - self.grid, split + 1)?;
        self.push_turns(side, t.line, x + self.grid, t.high, split + 1)?;
        self.add_line(side, p, 1 - l.axis, Some(t.line))
    }
}

// Round the two sums toward the incumbent so a sub-nanometre rounding
// difference cannot select more planar copper when replacing a via route.
pub(super) fn copper_length(engine: &Engine, path: &Path, round_up: bool) -> Result<u128, Failure> {
    engine.work.charge(path.edges.len() as u64)?;
    Ok(path
        .edges
        .iter()
        .map(|e| match *e {
            PathEdge::Track { start, end, .. } => {
                let squared = u128::from(start[0].abs_diff(end[0])).pow(2)
                    + u128::from(start[1].abs_diff(end[1])).pow(2);
                let length = squared.isqrt();
                length + u128::from(round_up && length * length < squared)
            }
            PathEdge::Via(_) => 0,
        })
        .sum())
}

#[allow(clippy::too_many_arguments)]
pub(super) fn connect(
    engine: &Engine,
    net: u32,
    starts: &[(Point, u8, bool)],
    targets: &[TargetBounds<'_>],
    track_ok: &[bool],
    occupied: usize,
    via_cost: u64,
    incumbent: Option<u64>,
    history: Option<&CongestionHistory>,
    budget: &mut TrialBudget<'_>,
    expanded: &mut u64,
    frontier: Option<&BlockedFrontier>,
) -> Result<Option<Path>, Failure> {
    if history.is_some_and(|h| !h.is_empty()) {
        return Ok(None);
    }
    let mut best = None;
    for t in targets {
        let at_mask = match t.plated_contact(&engine.work)? {
            Some((p, mask)) => Some((p, mask)),
            None => t
                .surface_contact(track_ok, &engine.work)?
                .map(|(p, l)| (p, 1 << l)),
        };
        let Some((goal, mask)) = at_mask else {
            continue;
        };
        for &(p, l, via_only) in starts {
            engine.work.charge(1)?;
            if !via_only && track_ok[l as usize] && mask & (1 << l) != 0 {
                let key = (manhattan(p, goal), p, goal, l);
                if best.is_none_or(|k| key < k) {
                    best = Some(key);
                }
            }
        }
    }
    let Some((distance, a, b, l)) = best else {
        return Ok(None);
    };
    let share = engine.surface_search_share.unwrap_or(0);
    let remaining = budget.current_limit.saturating_sub(*expanded);
    // Limit the optional line probes to the number of ordinary steps in an
    // unobstructed join as well as the remaining share of the board search.
    let direct_steps = distance.div_ceil(engine.input.rules.grid_nm as u64);
    let allowance = share
        .min(remaining / engine.input.nets.len().max(1) as u64)
        .min(direct_steps);
    let stop = expanded.saturating_add(allowance);
    let mut search = WaveSearch {
        engine,
        net,
        layer: l as usize,
        grid: engine.input.rules.grid_nm,
        guides: [a, b],
        // Do not spend the shared trial on a planar route whose detour costs
        // more than the two layer changes of an unobstructed alternative.
        max_steps: distance
            .div_ceil(engine.input.rules.grid_nm as u64)
            .saturating_mul(engine.layer_policy.weight(l as usize))
            .saturating_add(engine.layer_policy.scale(via_cost).saturating_mul(2))
            .min(incumbent.unwrap_or(u64::MAX)),
        occupied,
        wave: Default::default(),
    };
    for axis in 0..2 {
        if *expanded >= stop {
            return Ok(None);
        }
        budget.check_before_expand(engine, *expanded, frontier)?;
        *expanded += 1;
        if let Some(path) = search.add_line(1, b, axis, None)? {
            return Ok(Some(path));
        }
    }
    for &(seed, layer, via_only) in starts {
        engine.work.charge(1)?;
        if layer != l || via_only {
            continue;
        }
        for axis in 0..2 {
            if *expanded >= stop {
                return Ok(None);
            }
            budget.check_before_expand(engine, *expanded, frontier)?;
            *expanded += 1;
            if let Some(path) = search.add_line(0, seed, axis, None)? {
                return Ok(Some(path));
            }
        }
    }
    let mut side = 0;
    while *expanded < stop {
        if search.wave[side].turns.is_empty() {
            side = 1 - side;
        }
        if search.wave[side].turns.is_empty() {
            break;
        }
        budget.check_before_expand(engine, *expanded, frontier)?;
        *expanded += 1;
        if let Some(path) = search.advance(side)? {
            return Ok(Some(path));
        }
        side = 1 - side;
    }
    Ok(None)
}

#[cfg(test)]
mod tests;
