//! Advance along a physical track corridor by changing layers before the
//! next obstruction. The ordinary search remains available for lateral detours.
use super::{
    track, unpenalized_terminal, Failure, Join, Path, PathEdge, PlacedViaKey, Point, ViaKind,
};
use crate::search::monotone_trunk::clear_span;

struct Corridor {
    ends: [Point; 3],
    grid: i64,
    turn: i64,
    end: i64,
}

impl Corridor {
    fn new(a: Point, corner: Point, b: Point, grid: i64) -> Self {
        let count = |p: Point, q: Point| {
            ((p[0].abs_diff(q[0]).max(p[1].abs_diff(q[1]))).div_ceil(grid as u64)) as i64
        };
        let turn = count(a, corner);
        Self {
            ends: [a, corner, b],
            grid,
            turn,
            end: turn + count(corner, b),
        }
    }
    fn point(&self, n: i64) -> Point {
        let (a, b, n) = if n <= self.turn {
            (self.ends[0], self.ends[1], n)
        } else {
            (self.ends[1], self.ends[2], n - self.turn)
        };
        std::array::from_fn(|i| {
            a[i] + (b[i] - a[i]).signum() * (n * self.grid).min((b[i] - a[i]).abs())
        })
    }
    fn pieces(&self, a: i64, b: i64) -> [Point; 3] {
        let p = self.point(a);
        let q = self.point(b);
        [
            p,
            if a.min(b) < self.turn && self.turn < a.max(b) {
                self.ends[1]
            } else {
                p
            },
            q,
        ]
    }
}

impl Join<'_> {
    fn clear_corridor(&self, c: &Corridor, a: i64, b: i64, layer: usize) -> Result<bool, Failure> {
        for pair in c.pieces(a, b).windows(2) {
            if pair[0] != pair[1]
                && !self
                    .engine
                    .legal_edge_on(self.net, layer, pair[0], pair[1])?
            {
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn follow_corridor(
        &self,
        c: &Corridor,
        first: PlacedViaKey,
        last: PlacedViaKey,
        allowed: &[bool],
        occupied: usize,
        expand: &mut impl FnMut() -> Result<bool, Failure>,
    ) -> Result<Option<Path>, Failure> {
        let engine = self.engine;
        let stack_end = engine.layer_count() - 1;
        if engine
            .legal_via(self.net, last.at, 0, stack_end, ViaKind::Through, &[first])?
            .is_none()
        {
            return Ok(None);
        }
        engine.work.search_storage(occupied + 4)?;
        let mut holes = vec![first, last];
        let mut edges = vec![
            track(self.source.at, first.at, self.source.layer),
            PathEdge::Via(first),
        ];
        let mut pos = 0;
        let mut last_layer = self.source.layer;
        while pos < c.end {
            if !expand()? {
                return Ok(None);
            }
            let mut far = pos;
            let mut plane = 0;
            for (l, &ok) in allowed.iter().enumerate() {
                if !ok {
                    continue;
                }
                let extent =
                    pos + clear_span(c.end - pos, |n| self.clear_corridor(c, pos, pos + n, l))?;
                if extent > far {
                    far = extent;
                    plane = l;
                }
            }
            if far == pos {
                return Ok(None);
            }
            if pos == 0 && plane == self.source.layer {
                edges.pop(); // The surface stub continues without a layer transfer.
            }
            last_layer = plane;
            let mut next = far;
            let mut transition = None;
            if far < c.end {
                // The opposite layer must be able to continue across this
                // obstruction. Its backwards extent bounds the overlap in
                // which a through-via could actually transfer the route.
                let mut earliest = far + 1;
                for (l, &ok) in allowed.iter().enumerate() {
                    if l == plane || !ok {
                        continue;
                    }
                    let distance = clear_span(far + 1 - pos, |n| {
                        self.clear_corridor(c, far + 1, far + 1 - n, l)
                    })?;
                    earliest = earliest.min(far + 1 - distance);
                }
                let lo = earliest.max(pos + 1);
                if lo > far {
                    return Ok(None);
                }
                for i in (lo..=far).rev() {
                    if !expand()? {
                        return Ok(None);
                    }
                    let q = c.point(i);
                    let via = PlacedViaKey {
                        at: q,
                        start_layer: 0,
                        end_layer: stack_end as u8,
                        kind: 0,
                    };
                    if engine
                        .legal_via(self.net, q, 0, stack_end, ViaKind::Through, &holes)?
                        .is_some()
                    {
                        next = i;
                        transition = Some(via);
                        break;
                    }
                }
                if transition.is_none() {
                    return Ok(None);
                }
            }
            engine
                .work
                .search_storage(occupied + holes.len() + edges.len() + 4)?;
            for pair in c.pieces(pos, next).windows(2) {
                if pair[0] != pair[1] {
                    edges.push(track(pair[0], pair[1], plane));
                }
            }
            if let Some(v) = transition {
                edges.push(PathEdge::Via(v));
                holes.push(v);
            }
            pos = next;
        }
        if last_layer != self.target.layer {
            edges.push(PathEdge::Via(last));
        }
        edges.push(track(last.at, self.target.at, self.target.layer));
        engine.work.charge(edges.len() as u64)?;
        engine
            .work
            .search_storage(occupied + holes.len() + 2 * edges.len() + 1)?;
        let mut points = vec![self.source.at];
        let mut steps = 0u64;
        for edge in &edges {
            match *edge {
                PathEdge::Track { start, end, layer } => {
                    points.push(end);
                    let span = start[0].abs_diff(end[0]).div_ceil(c.grid as u64)
                        + start[1].abs_diff(end[1]).div_ceil(c.grid as u64);
                    steps = steps.saturating_add(
                        span.saturating_mul(engine.layer_policy.weight(layer as usize)),
                    );
                    if let Some(history) = self.history.filter(|h| !h.is_empty()) {
                        if start[0] != end[0] && start[1] != end[1] {
                            if !unpenalized_terminal(engine, Some(history), &[*edge])? {
                                return Ok(None);
                            }
                        } else {
                            let k = usize::from(start[0] == end[0]);
                            let sign = (end[k] - start[k]).signum();
                            for i in 1..=span {
                                engine.work.charge(1)?;
                                let mut q = start;
                                q[k] += sign * (i as i64 * c.grid).min((end[k] - start[k]).abs());
                                steps = steps.saturating_add(engine.layer_policy.scale(
                                    history.step_penalty(layer, q, engine.input.bounds.min, c.grid),
                                ));
                            }
                        }
                    }
                }
                PathEdge::Via(v) => {
                    points.push(v.at);
                    steps = steps.saturating_add(self.via_step);
                    if let Some(history) = self.history.filter(|h| !h.is_empty()) {
                        engine
                            .work
                            .charge(1 + u64::from(v.end_layer - v.start_layer))?;
                        steps =
                            steps.saturating_add(engine.layer_policy.scale(history.via_penalty(
                                v.at,
                                v.start_layer,
                                v.end_layer,
                                engine.input.bounds.min,
                                c.grid,
                            )));
                    }
                }
            }
        }
        Ok(Some(Path {
            points,
            edges,
            steps,
        }))
    }

    pub(super) fn through_corridors(
        &self,
        a: PlacedViaKey,
        b: PlacedViaKey,
        allowed: &[bool],
        occupied: usize,
        expand: &mut impl FnMut() -> Result<bool, Failure>,
    ) -> Result<Option<Path>, Failure> {
        let p = a.at;
        let q = b.at;
        if p == q {
            return Ok(None);
        }
        let dx = q[0] - p[0];
        let dy = q[1] - p[1];
        let span = dx.abs().min(dy.abs());
        let mut corners = vec![
            [p[0], q[1]],
            [q[0], p[1]],
            [p[0] + dx.signum() * span, p[1] + dy.signum() * span],
            [q[0] - dx.signum() * span, q[1] - dy.signum() * span],
        ];
        corners.dedup();
        for corner in corners {
            let corridor = Corridor::new(p, corner, q, self.engine.input.rules.grid_nm);
            if let Some(path) = self.follow_corridor(&corridor, a, b, allowed, occupied, expand)? {
                return Ok(Some(path));
            }
        }
        Ok(None)
    }
}

#[allow(clippy::too_many_arguments)]
pub(in crate::search) fn through_launch_corridors(
    engine: &crate::Engine,
    net: u32,
    starts: &[(Point, u8, bool)],
    targets: &[crate::search::TargetBounds<'_>],
    allowed: &[bool],
    occupied: usize,
    via_cost: u64,
    history: Option<&crate::negotiate::CongestionHistory>,
    budget: &mut crate::negotiate::TrialBudget<'_>,
    expanded: &mut u64,
    frontier: Option<&crate::negotiate::BlockedFrontier>,
) -> Result<Option<Path>, Failure> {
    let [goal] = targets else {
        return Ok(None);
    };
    let Some((end, dest)) = goal.surface_contact(allowed, &engine.work)? else {
        return Ok(None);
    };
    let right = crate::search::layer_crossing::drill_sites(engine, net, end, dest, None)?;
    if right.is_empty() {
        return Ok(None);
    }
    let mut share =
        budget.current_limit.saturating_sub(*expanded) / engine.input.nets.len().max(1) as u64;
    let mut expand = || {
        if share == 0 {
            return Ok(false);
        }
        budget.check_before_expand(engine, *expanded, frontier)?;
        *expanded += 1;
        share -= 1;
        Ok(true)
    };
    for &(at, face, via_only) in starts {
        let layer = usize::from(face);
        if via_only || !allowed[layer] || (layer != 0 && layer + 1 != engine.layer_count()) {
            continue;
        }
        let left = crate::search::layer_crossing::drill_sites(engine, net, at, layer, None)?;
        let join = Join {
            engine,
            net,
            source: super::Terminal { at, layer },
            target: super::Terminal {
                at: end,
                layer: dest,
            },
            via_step: engine.layer_policy.scale(via_cost),
            history,
        };
        for &a in &left {
            for &b in &right {
                if !expand()? {
                    return Ok(None);
                }
                if let Some(path) = join.through_corridors(
                    a,
                    b,
                    allowed,
                    occupied + left.len() + right.len(),
                    &mut expand,
                )? {
                    return Ok(Some(path));
                }
            }
        }
    }
    Ok(None)
}

#[cfg(test)]
mod tests;
