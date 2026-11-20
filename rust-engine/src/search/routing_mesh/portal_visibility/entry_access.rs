//! A nearby portal can face a keepout. Compare another landing on the
//! same plane when it avoids an unavoidable first step away from the span.
use super::{Failure, HashMap, Mesh, Node, PathEdge, Point, Tail};

type Pair = (u64, Node, Node);

impl Mesh<'_> {
    fn opens_towards(&self, (at, layer): Node, other: Point) -> Result<bool, Failure> {
        if at == other {
            return Ok(true);
        }
        let [sx, sy] = std::array::from_fn(|i| (other[i] - at[i]).signum());
        let directions = [[sx, 0], [0, sy], [sx, sy]];
        for (i, &dir) in directions.iter().enumerate() {
            self.engine.work.charge(1)?;
            if dir == [0, 0] || directions[..i].contains(&dir) {
                continue;
            }
            let to = std::array::from_fn(|k| at[k] + self.pitch * dir[k]);
            if self
                .engine
                .legal_edge_on(self.net, usize::from(layer), at, to)?
            {
                return Ok(true);
            }
        }
        Ok(false)
    }

    pub(super) fn accessible_neighbor(
        &self,
        (bound, source, goal): Pair,
        roots: &HashMap<Node, Vec<PathEdge>>,
        costs: &HashMap<Node, u64>,
        goals: &HashMap<Node, Tail>,
    ) -> Result<Option<(Pair, u64)>, Failure> {
        if roots.len() == 1 && goals.len() == 1 {
            return Ok(None);
        }
        let root_open = self.opens_towards(source, goal.0)?;
        let goal_open = self.opens_towards(goal, source.0)?;
        if root_open && goal_open {
            return Ok(None);
        }
        // Portal coordinates lie on one lattice. If none of the signed
        // horizontal, vertical or diagonal steps can reduce the route by its
        // own Manhattan length, one coordinate has to travel out and back.
        // Both ends may share that detour, so charge it only once.
        let extra = (self.pitch as u64 / self.grid)
            .saturating_mul(2)
            .saturating_mul(self.engine.layer_policy.weight(usize::from(source.1)));
        let detour = bound.saturating_add(extra);
        let mut best = None;
        let mut consider = |a: Node, b: Node| -> Result<(), Failure> {
            let lower = costs[&a]
                .saturating_add(goals[&b].0)
                .saturating_add(self.steps(a.0, b.0, a.1));
            // This fixed cutoff keeps the same queries in any hash iteration order.
            if lower < detour && self.opens_towards(a, b.0)? && self.opens_towards(b, a.0)? {
                let candidate = (lower, a, b);
                if best.is_none_or(|old| candidate < old) {
                    best = Some(candidate);
                }
            }
            Ok(())
        };
        let drilled = |edges: &[PathEdge]| edges.iter().any(|e| matches!(e, PathEdge::Via(_)));
        if !root_open {
            self.engine.work.charge(roots[&source].len() as u64)?;
            let from_drill = drilled(&roots[&source]);
            for (&a, prefix) in roots {
                self.engine.work.charge(1 + prefix.len() as u64)?;
                if a != source && a.1 == source.1 && drilled(prefix) == from_drill {
                    consider(a, goal)?;
                }
            }
        }
        if !goal_open {
            self.engine.work.charge(goals[&goal].1.len() as u64)?;
            let to_drill = drilled(&goals[&goal].1);
            for (&b, (_, suffix)) in goals {
                self.engine.work.charge(1 + suffix.len() as u64)?;
                if b != goal && b.1 == goal.1 && drilled(suffix) == to_drill {
                    consider(source, b)?;
                }
            }
        }
        Ok(best.map(|pair| (pair, detour)))
    }
}
