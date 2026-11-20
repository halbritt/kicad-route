//! Once competing routing units are waiting, try to connect this join with
//! short, fully checked pad escapes before expanding more unit grid edges.
use super::{
    layer_crossing, unpenalized_terminal, Engine, Failure, Path, PathEdge, PlacedViaKey, Point,
    TargetBounds, ViaKind,
};
use crate::diff_pair::octilinear_elbow_paths;
use crate::negotiate::{BlockedFrontier, CongestionHistory, TrialBudget};

#[derive(Clone, Copy)]
struct Terminal {
    at: Point,
    layer: usize,
}

fn track(a: Point, b: Point, layer: usize) -> PathEdge {
    PathEdge::Track {
        start: a,
        end: b,
        layer: layer as u8,
    }
}

struct Join<'a> {
    engine: &'a Engine,
    net: u32,
    source: Terminal,
    target: Terminal,
    via_step: u64,
    history: Option<&'a CongestionHistory>,
}

impl Join<'_> {
    /// Sweeps on the two terminal faces are already checked by terminal_via.
    /// Every intermediate segment and the second drill are checked in context.
    fn connect(
        &self,
        layer: usize,
        first: Option<PlacedViaKey>,
        second: Option<PlacedViaKey>,
        best: &mut Option<Path>,
        expand: &mut impl FnMut() -> Result<(), Failure>,
    ) -> Result<(), Failure> {
        let engine = self.engine;
        let a = first.map_or(self.source.at, |v| v.at);
        let b = second.map_or(self.target.at, |v| v.at);
        let grid = engine.input.rules.grid_nm as u64;
        let distance = |a: Point, b: Point| {
            (0..2)
                .map(|k| a[k].abs_diff(b[k]).div_ceil(grid))
                .sum::<u64>()
        };
        let stubs = distance(a, self.source.at)
            .saturating_mul(engine.layer_policy.weight(self.source.layer))
            .saturating_add(
                distance(b, self.target.at)
                    .saturating_mul(engine.layer_policy.weight(self.target.layer)),
            );
        let via_cost = self
            .via_step
            .saturating_mul(u64::from(first.is_some()) + u64::from(second.is_some()));
        let lower = stubs
            .saturating_add(via_cost)
            .saturating_add(distance(a, b).saturating_mul(engine.layer_policy.weight(layer)));
        engine.work.charge(1)?;
        if a == b || best.as_ref().is_some_and(|p| p.steps <= lower) {
            return Ok(());
        }
        if let (Some(prev), Some(next)) = (first, second) {
            if engine
                .legal_via(
                    self.net,
                    next.at,
                    0,
                    engine.layer_count() - 1,
                    ViaKind::Through,
                    &[prev],
                )?
                .is_none()
            {
                return Ok(());
            }
        }
        let mut options = octilinear_elbow_paths(a, b);
        if a[0] != b[0] && a[1] != b[1] {
            options.extend([vec![a, [a[0], b[1]], b], vec![a, [b[0], a[1]], b]]);
        }
        'path: for bends in options {
            expand()?;
            let mut edges = Vec::with_capacity(6);
            let mut points = vec![self.source.at];
            let mut steps = stubs.saturating_add(via_cost);
            if let Some(v) = first {
                edges.push(track(self.source.at, v.at, self.source.layer));
                edges.push(PathEdge::Via(v));
                points.extend([v.at, v.at]);
            }
            for w in bends.windows(2) {
                engine.work.charge(1)?;
                if !engine.legal_edge_on(self.net, layer, w[0], w[1])? {
                    continue 'path;
                }
                steps = steps.saturating_add(
                    distance(w[0], w[1]).saturating_mul(engine.layer_policy.weight(layer)),
                );
                edges.push(track(w[0], w[1], layer));
                points.push(w[1]);
            }
            if let Some(v) = second {
                edges.push(PathEdge::Via(v));
                edges.push(track(v.at, self.target.at, self.target.layer));
                points.extend([v.at, self.target.at]);
            }
            if best.as_ref().is_none_or(|p| p.steps > steps)
                && unpenalized_terminal(engine, self.history, &edges)?
            {
                *best = Some(Path {
                    points,
                    edges,
                    steps,
                });
            }
        }
        Ok(())
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) fn from_launch(
    engine: &Engine,
    net: u32,
    starts: &[(Point, u8, bool)],
    targets: &[TargetBounds<'_>],
    track_ok: &[bool],
    frontier_entries: usize,
    via_cost: u64,
    history: Option<&CongestionHistory>,
    budget: &mut TrialBudget<'_>,
    expanded: &mut u64,
    frontier: Option<&BlockedFrontier>,
) -> Result<Option<Path>, Failure> {
    let mut choices = Vec::new();
    let via_step = engine.layer_policy.scale(via_cost);
    for target in targets {
        let Some((end, dest)) = target.surface_contact(track_ok, &engine.work)? else {
            continue;
        };
        for &(at, face, via_only) in starts {
            engine.work.charge(1)?;
            if via_only || !track_ok[face as usize] {
                continue;
            }
            let nm = at[0].abs_diff(end[0]) + at[1].abs_diff(end[1]);
            let lower = nm
                .div_ceil(engine.input.rules.grid_nm as u64)
                .saturating_mul(engine.layer_policy.minimum_step(net))
                .saturating_add(if usize::from(face) == dest {
                    0
                } else {
                    via_step
                });
            engine
                .work
                .search_storage(frontier_entries + choices.len() + 1)?;
            choices.push((lower, at, face, end, dest));
        }
    }
    engine
        .work
        .charge(choices.len() as u64 * choices.len().max(1).next_power_of_two().ilog2() as u64)?;
    choices.sort_unstable();
    let mut expand = || {
        budget.check_before_expand(engine, *expanded, frontier)?;
        *expanded += 1;
        Ok(())
    };
    let mut best: Option<Path> = None;
    for (lower, at, face, end, dest) in choices {
        if best.as_ref().is_some_and(|p| p.steps <= lower) {
            break;
        }
        expand()?;
        let source = Terminal {
            at,
            layer: face as usize,
        };
        let target = Terminal {
            at: end,
            layer: dest,
        };
        let join = Join {
            engine,
            net,
            source,
            target,
            via_step,
            history,
        };
        if source.layer == dest {
            join.connect(dest, None, None, &mut best, &mut expand)?;
        }
        if best.as_ref().is_some_and(|p| p.steps <= lower) {
            continue;
        }
        let left = layer_crossing::drill_sites(engine, net, source.at, source.layer)?;
        let right = layer_crossing::drill_sites(engine, net, target.at, target.layer)?;
        if source.layer != dest {
            for &v in &left {
                join.connect(dest, Some(v), None, &mut best, &mut expand)?;
            }
            for &v in &right {
                join.connect(source.layer, None, Some(v), &mut best, &mut expand)?;
            }
        }
        for (layer, &allowed) in track_ok.iter().enumerate() {
            if !allowed || layer == source.layer || layer == dest {
                continue;
            }
            for &v in &left {
                for &w in &right {
                    join.connect(layer, Some(v), Some(w), &mut best, &mut expand)?;
                }
            }
        }
    }
    Ok(best)
}
