use super::{link, Failure, Mesh, MeshPricing, Node, Path, PathEdge, Point};
use crate::search::monotone_trunk::clear_span;
use std::collections::HashMap;

type Tail = (u64, Vec<PathEdge>);

impl Mesh<'_> {
    fn entry_pairs(
        &self,
        roots: &HashMap<Node, Vec<PathEdge>>,
        costs: &HashMap<Node, u64>,
        goals: &HashMap<Node, Tail>,
        occupied: usize,
    ) -> Result<Vec<(u64, Node, Node)>, Failure> {
        let layers = self.engine.layer_count();
        self.storage(occupied + 3 * layers + goals.len())?;
        let mut best = vec![None; layers];
        let mut by_layer: Vec<[Vec<(Node, u64)>; 2]> =
            (0..layers).map(|_| Default::default()).collect();
        let drilled =
            |edges: &[PathEdge]| edges.iter().any(|edge| matches!(edge, PathEdge::Via(_)));
        for (&target, (tail_cost, tail)) in goals {
            self.engine.work.charge(1 + tail.len() as u64)?;
            by_layer[usize::from(target.1)][usize::from(drilled(tail))].push((target, *tail_cost));
        }
        // Only pair terminals which can use existing copper at one end
        // and a registered drill at the other, on this same plane.
        for (&source, prefix) in roots {
            let (at, layer) = source;
            self.engine.work.charge(1 + prefix.len() as u64)?;
            for &(target, tail_cost) in &by_layer[usize::from(layer)][usize::from(!drilled(prefix))]
            {
                self.engine.work.charge(1)?;
                let estimate = costs[&source]
                    .saturating_add(tail_cost)
                    .saturating_add(self.steps(at, target.0, layer));
                let key = (estimate, source, target);
                let slot = &mut best[usize::from(layer)];
                if slot.is_none_or(|other| key < other) {
                    *slot = Some(key);
                }
            }
        }
        let mut pairs = best.into_iter().flatten().collect::<Vec<_>>();
        self.engine
            .work
            .charge(pairs.len() as u64 * pairs.len().max(1).next_power_of_two().ilog2() as u64)?;
        pairs.sort_unstable();
        Ok(pairs)
    }

    // Only the first lattice point and the last clear point on the ray can
    // launch an elbow here. Finding the endpoint takes logarithmic sweeps.
    fn ray_tips(&self, (origin, layer): Node, dir: Point) -> Result<[Option<Point>; 2], Failure> {
        let bounds = self.engine.input.bounds;
        let extent = (0..2)
            .filter(|&k| dir[k] != 0)
            .map(|k| {
                let edge = if dir[k] > 0 {
                    bounds.max[k]
                } else {
                    bounds.min[k]
                };
                (edge - origin[k]).abs() / self.pitch
            })
            .min()
            .unwrap_or(0);
        let at = |t: i64| std::array::from_fn(|k| origin[k] + t * self.pitch * dir[k]);
        let last = clear_span(extent, |t| {
            self.engine
                .legal_edge_on(self.net, usize::from(layer), origin, at(t))
        })?;
        Ok([(last >= 1).then(|| at(1)), (last > 1).then(|| at(last))])
    }

    // The registered destination attachment is stored from the pad outwards.
    // Emit the trunk in that same convention. to_path reverses the complete
    // tail, prices every edge forwards and checks all drills together.
    fn attach_trunk(
        &self,
        source: Node,
        roots: &HashMap<Node, Vec<PathEdge>>,
        suffix: &[PathEdge],
        path: [Point; 4],
        occupied: usize,
    ) -> Result<Option<Path>, Failure> {
        let edges = roots[&source].len() + suffix.len() + 3;
        self.storage(occupied + suffix.len() + 3 + 3 * edges + 1)?;
        self.engine.work.charge(suffix.len() as u64 + 3)?;
        let mut tail = suffix.to_vec();
        for w in path.windows(2).rev() {
            if w[0] != w[1] {
                tail.push(link(w[1], w[0], source.1));
            }
        }
        self.to_path(source, &HashMap::new(), roots, &tail)
    }

    /// A terminal escape on one side and existing copper on the other can
    /// connect across the whole board between their registered mesh entries.
    pub(super) fn visible_entries(
        &self,
        roots: &HashMap<Node, Vec<PathEdge>>,
        costs: &HashMap<Node, u64>,
        goals: &HashMap<Node, Tail>,
        occupied: usize,
        expand: &mut impl FnMut() -> Result<bool, Failure>,
    ) -> Result<Option<Path>, Failure> {
        let pairs = self.entry_pairs(roots, costs, goals, occupied)?;
        let retained = occupied + pairs.len();
        let paid = Mesh {
            pricing: MeshPricing::Forward,
            ..*self
        };
        let mut shortest: Option<Path> = None;
        for (bound, source, target) in pairs {
            if shortest.as_ref().is_some_and(|p| p.steps <= bound) {
                break;
            }
            if !expand()? {
                break;
            }
            let (a, layer) = source;
            let b = target.0;
            // The best through connection need not start towards the other
            // terminal. Near a pad or adjacent barrel the escape can turn at
            // the first free mesh point, or at the last before an obstruction.
            let delta = [b[0] - a[0], b[1] - a[1]];
            let sign = delta.map(i64::signum);
            let major = if delta[0].abs() > delta[1].abs() {
                [sign[0], 0]
            } else {
                [0, sign[1]]
            };
            let directions = [
                major,
                sign,
                [-1, -1],
                [-1, 0],
                [-1, 1],
                [0, -1],
                [0, 1],
                [1, -1],
                [1, 0],
                [1, 1],
            ];
            let legal = |p, q| {
                self.engine
                    .legal_edge_on(self.net, usize::from(layer), p, q)
            };
            for (i, &direction) in directions.iter().enumerate() {
                if direction == [0, 0] || directions[..i].contains(&direction) {
                    continue;
                }
                for reverse in [false, true] {
                    if !expand()? {
                        return Ok(shortest);
                    }
                    let (origin, other, dir) = if reverse {
                        (b, a, direction.map(|d| -d))
                    } else {
                        (a, b, direction)
                    };
                    for tip in self.ray_tips((origin, layer), dir)?.into_iter().flatten() {
                        // History can only increase the weighted copper distance.
                        let base = costs[&source]
                            .saturating_add(goals[&target].0)
                            .saturating_add(self.steps(origin, tip, layer))
                            .saturating_add(self.steps(tip, other, layer));
                        if shortest.as_ref().is_some_and(|p| p.steps <= base) {
                            continue;
                        }
                        let dx = other[0] - tip[0];
                        let dy = other[1] - tip[1];
                        let diag = dx.abs().min(dy.abs());
                        let bends = [
                            [tip[0] + dx.signum() * diag, tip[1] + dy.signum() * diag],
                            [other[0] - dx.signum() * diag, other[1] - dy.signum() * diag],
                            [tip[0], other[1]],
                            [other[0], tip[1]],
                        ];
                        for (k, &bend) in bends.iter().enumerate() {
                            self.engine.work.charge(1)?;
                            if bends[..k].contains(&bend)
                                || !legal(tip, bend)?
                                || !legal(bend, other)?
                            {
                                continue;
                            }
                            let path = if reverse {
                                [a, bend, tip, b]
                            } else {
                                [a, tip, bend, b]
                            };
                            if let Some(candidate) =
                                paid.attach_trunk(source, roots, &goals[&target].1, path, retained)?
                            {
                                if candidate.steps == bound {
                                    return Ok(Some(candidate));
                                }
                                if shortest.as_ref().is_none_or(|p| p.steps > candidate.steps) {
                                    shortest = Some(candidate);
                                }
                            }
                        }
                    }
                }
            }
        }
        Ok(shortest)
    }
}

#[cfg(test)]
mod tests;
