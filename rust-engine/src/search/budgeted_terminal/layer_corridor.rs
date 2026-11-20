//! Advance along a physical track corridor by changing layers before the
//! next obstruction. The ordinary search remains available for lateral detours.
use super::{
    track, unpenalized_terminal, Failure, Join, Path, PathEdge, PlacedViaKey, Point, ViaKind,
};
use crate::search::monotone_trunk::clear_span;

#[derive(Clone, Copy)]
enum Access {
    NewVia(PlacedViaKey),
    Copper { at: Point, layers: u32 },
}
impl From<PlacedViaKey> for Access {
    fn from(v: PlacedViaKey) -> Self {
        Self::NewVia(v)
    }
}
impl Access {
    fn at(self) -> Point {
        match self {
            Self::NewVia(v) => v.at,
            Self::Copper { at, .. } => at,
        }
    }
    fn via(self) -> Option<PlacedViaKey> {
        match self {
            Self::NewVia(v) => Some(v),
            Self::Copper { .. } => None,
        }
    }
    fn reaches(self, layer: usize) -> bool {
        match self {
            Self::NewVia(_) => true,
            Self::Copper { layers, .. } => layers & (1 << layer) != 0,
        }
    }
}

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
        first: Access,
        last: Access,
        allowed: &[bool],
        occupied: usize,
        expand: &mut impl FnMut() -> Result<bool, Failure>,
    ) -> Result<Option<Path>, Failure> {
        let engine = self.engine;
        let stack_end = engine.layer_count() - 1;
        engine.work.search_storage(occupied + 4)?;
        let mut holes = Vec::new();
        let mut edges = Vec::new();
        if let Some(v) = first.via() {
            holes.push(v);
            edges.push(track(self.source.at, v.at, self.source.layer));
            edges.push(PathEdge::Via(v));
        }
        if let Some(v) = last.via() {
            if engine
                .legal_via(self.net, v.at, 0, stack_end, ViaKind::Through, &holes)?
                .is_none()
            {
                return Ok(None);
            }
            holes.push(v);
        }
        let mut pos = 0;
        let mut last_layer = self.source.layer;
        while pos < c.end {
            if !expand()? {
                return Ok(None);
            }
            let mut far = pos;
            let mut plane = 0;
            for (l, &ok) in allowed.iter().enumerate() {
                if !ok || (pos == 0 && !first.reaches(l)) {
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
            if pos == 0 && plane == self.source.layer && first.via().is_some() {
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
        if let Some(v) = last.via() {
            if last_layer != self.target.layer {
                edges.push(PathEdge::Via(v));
            }
            edges.push(track(v.at, self.target.at, self.target.layer));
        } else if !last.reaches(last_layer) {
            return Ok(None);
        }
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
                            let penalty = history.octilinear_penalty(engine, layer, start, end)?;
                            steps = steps.saturating_add(engine.layer_policy.scale(penalty));
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

    fn through_corridors(
        &self,
        a: Access,
        b: Access,
        allowed: &[bool],
        occupied: usize,
        expand: &mut impl FnMut() -> Result<bool, Failure>,
    ) -> Result<Option<Path>, Failure> {
        let p = a.at();
        let q = b.at();
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

    /// Try the neighboring drill centers of a legal surface escape, while its
    /// entire source track remains clear. The opposite endpoint stays fixed.
    fn along_drill_rays(
        &self,
        left: &[Access],
        right: &[Access],
        allowed: &[bool],
        occupied: usize,
        expand: &mut impl FnMut() -> Result<bool, Failure>,
    ) -> Result<Option<Path>, Failure> {
        #[derive(Clone, Copy)]
        struct DrillRay {
            terminal: super::Terminal,
            from_left: bool,
            hole: PlacedViaKey,
            step: Point,
        }
        let engine = self.engine;
        let storage = occupied + left.len() + right.len();
        if storage.saturating_add(4) > engine.input.limits.max_search_entries {
            return Ok(None);
        }
        engine.work.search_storage(storage)?;
        let grid = engine.input.rules.grid_nm;
        let mut rays = Vec::new();
        for (start, from_left, ends) in [(self.source, true, left), (self.target, false, right)] {
            for &end in ends {
                if let Some(via) = end.via() {
                    let step: Point =
                        std::array::from_fn(|k| (via.at[k] - start.at[k]).signum() * grid);
                    if step != [0, 0] {
                        rays.push(DrillRay {
                            terminal: start,
                            from_left,
                            hole: via,
                            step,
                        });
                    }
                }
            }
        }
        while !rays.is_empty() {
            let mut i = 0;
            while i < rays.len() {
                if !expand()? {
                    return Ok(None);
                }
                let ray = rays[i];
                let q = std::array::from_fn(|k| ray.hole.at[k] + ray.step[k]);
                if !engine.legal_edge_on(self.net, ray.terminal.layer, ray.terminal.at, q)? {
                    rays.remove(i);
                    continue;
                }
                let via = PlacedViaKey { at: q, ..ray.hole };
                rays[i].hole = via;
                i += 1;
                if engine
                    .legal_via(
                        self.net,
                        q,
                        0,
                        engine.layer_count() - 1,
                        ViaKind::Through,
                        &[],
                    )?
                    .is_none()
                {
                    continue;
                }
                for &fixed in if ray.from_left { right } else { left } {
                    if !expand()? {
                        return Ok(None);
                    }
                    let shifted = Access::NewVia(via);
                    let (a, b) = if ray.from_left {
                        (shifted, fixed)
                    } else {
                        (fixed, shifted)
                    };
                    if let Some(p) = self.through_corridors(a, b, allowed, storage, expand)? {
                        return Ok(Some(p));
                    }
                }
            }
        }
        Ok(None)
    }
}

#[allow(clippy::too_many_arguments)]
pub(in crate::search) fn through_launch_corridors(
    engine: &crate::Engine,
    net: u32,
    source: &[crate::components::Copper<'_>],
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
    let (end, dest, right) =
        if let Some((end, dest)) = goal.surface_contact(allowed, &engine.work)? {
            let holes = crate::search::layer_crossing::drill_sites(engine, net, end, dest, None)?;
            (
                end,
                dest,
                holes.into_iter().map(Access::NewVia).collect::<Vec<_>>(),
            )
        } else if let Some((at, layers)) = goal.plated_contact(&engine.work)? {
            (
                at,
                layers.trailing_zeros() as usize,
                vec![Access::Copper { at, layers }],
            )
        } else {
            return Ok(None);
        };
    if right.is_empty() {
        return Ok(None);
    }
    let mut share =
        budget.current_limit.saturating_sub(*expanded) / engine.input.nets.len().max(1) as u64;
    let mut expand = |share: &mut u64, keep: u64| {
        if *share <= keep {
            return Ok(false);
        }
        budget.check_before_expand(engine, *expanded, frontier)?;
        *expanded += 1;
        *share -= 1;
        Ok(true)
    };
    // Plated pads provide a permanent contact on several layers. A seed
    // on a routed track is only an attachment on its own face. Check
    // the original pad geometry before using its barrel as a free transfer.
    let inner = |layer: u8| layer > 0 && usize::from(layer) + 1 < allowed.len();
    let mut faces = std::collections::BTreeMap::<Point, u32>::new();
    engine.work.charge(source.len() as u64)?;
    for &c in source {
        if matches!(c, crate::components::Copper::Pad(p) if p.drill.is_some()) {
            let pad = crate::search::TargetBounds::new(c, engine.all_layers_slice(), &engine.work)?;
            if let Some((at, mask)) = pad.plated_contact(&engine.work)? {
                engine
                    .work
                    .search_storage(occupied + right.len() + faces.len() + 1)?;
                *faces.entry(at).or_default() |= mask;
            }
        }
    }
    for (index, &(at, face, via_only)) in starts
        .iter()
        .filter(|s| inner(s.1))
        .chain(starts.iter().filter(|s| !inner(s.1)))
        .enumerate()
    {
        engine.work.charge(1)?;
        let layer = usize::from(face);
        if via_only || !allowed[layer] {
            continue;
        }
        let left = if inner(face) {
            let Some(layers) = faces.remove(&at) else {
                continue;
            };
            vec![Access::Copper { at, layers }]
        } else {
            crate::search::layer_crossing::drill_sites(engine, net, at, layer, None)?
                .into_iter()
                .map(Access::NewVia)
                .collect()
        };
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
        let storage = occupied + faces.len() + left.len() + right.len();
        for &a in &left {
            for &b in &right {
                if !expand(&mut share, 0)? {
                    return Ok(None);
                }
                if let Some(path) =
                    join.through_corridors(a, b, allowed, storage, &mut || expand(&mut share, 0))?
                {
                    return Ok(Some(path));
                }
            }
        }
        // Keep the remaining starting phases eligible. Only expand a drill
        // window when ordinary search has encountered previously laid copper.
        if share > 0 && !left.is_empty() && frontier.is_some_and(|f| !f.probes().is_empty()) {
            let keep = share - share.div_ceil((starts.len() - index) as u64);
            if let Some(path) =
                join.along_drill_rays(&left, &right, allowed, storage, &mut || {
                    expand(&mut share, keep)
                })?
            {
                return Ok(Some(path));
            }
        }
    }
    Ok(None)
}

#[cfg(test)]
mod tests;

#[cfg(test)]
mod drill_ray_tests;
