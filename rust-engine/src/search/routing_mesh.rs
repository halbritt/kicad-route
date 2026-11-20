//! A second search scale for a congested multilayer join. A mesh edge spans
//! several ordinary grid steps. Its full copper sweep must be legal. Missing
//! a path at this scale leaves the normal search frontier intact.
use super::{
    unpenalized_terminal, Engine, Failure, Path, PathEdge, PlacedViaKey, Point, TargetBounds,
    ViaKind,
};
use crate::diff_pair::octilinear_elbow_paths;
use crate::model::Status;
use crate::negotiate::{BlockedFrontier, CongestionHistory, TrialBudget};
use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap};

mod drill_access;

type Node = (Point, u8);

fn link(a: Point, b: Point, layer: u8) -> PathEdge {
    PathEdge::Track {
        start: a,
        end: b,
        layer,
    }
}

struct Mesh<'a> {
    engine: &'a Engine,
    net: u32,
    grid: u64,
    pitch: i64,
    via_step: u64,
    history: Option<&'a CongestionHistory>,
    occupied_entries: usize,
}

impl Mesh<'_> {
    fn storage(&self, entries: usize) -> Result<(), Failure> {
        self.engine
            .work
            .search_storage(self.occupied_entries.saturating_add(entries))
    }

    fn steps(&self, a: Point, b: Point, layer: u8) -> u64 {
        (a[0].abs_diff(b[0]).div_ceil(self.grid) + a[1].abs_diff(b[1]).div_ceil(self.grid))
            .saturating_mul(self.engine.layer_policy.weight(layer as usize))
    }

    fn clear(&self, a: Point, b: Point, layer: u8) -> Result<bool, Failure> {
        Ok(self.engine.legal_edge_on(self.net, layer as usize, a, b)?
            && unpenalized_terminal(self.engine, self.history, &[link(a, b, layer)])?)
    }

    // Attach off-grid native copper to neighboring mesh intersections with
    // continuous segments. The board origin defines one common mesh.
    fn portals(&self, p: Point, layer: u8) -> Result<Vec<(Node, u64, Vec<PathEdge>)>, Failure> {
        let origin = self.engine.input.bounds.min;
        let nearest: Point = std::array::from_fn(|k| {
            origin[k] + (p[k] - origin[k] + self.pitch / 2).div_euclid(self.pitch) * self.pitch
        });
        let mut result = Vec::new();
        for dx in [-self.pitch, 0, self.pitch] {
            for dy in [-self.pitch, 0, self.pitch] {
                self.engine.work.charge(1)?;
                let q = [nearest[0] + dx, nearest[1] + dy];
                let mut found: Option<(u64, Vec<PathEdge>)> = None;
                for route in octilinear_elbow_paths(p, q) {
                    let mut cost = 0u64;
                    let mut edges = Vec::new();
                    for w in route.windows(2) {
                        if !self.clear(w[0], w[1], layer)? {
                            break;
                        }
                        cost = cost.saturating_add(self.steps(w[0], w[1], layer));
                        edges.push(link(w[0], w[1], layer));
                    }
                    if edges.len() + 1 == route.len()
                        && found.as_ref().is_none_or(|(n, _)| cost < *n)
                    {
                        found = Some((cost, edges));
                    }
                }
                if let Some((cost, edges)) = found {
                    result.push(((q, layer), cost, edges));
                }
            }
        }
        Ok(result)
    }

    fn to_path(
        &self,
        start: Node,
        parents: &HashMap<Node, (Node, PathEdge)>,
        roots: &HashMap<Node, Vec<PathEdge>>,
        tail: &[PathEdge],
    ) -> Result<Option<Path>, Failure> {
        let mut node = start;
        let mut backwards = Vec::new();
        while let Some(&(parent, edge)) = parents.get(&node) {
            self.engine.work.charge(1)?;
            self.storage(backwards.len() + 1)?;
            backwards.push(edge);
            node = parent;
        }
        let Some(prefix) = roots.get(&node) else {
            return Err(Failure::new(
                Status::InternalError,
                "mesh predecessor lost its source attachment",
            ));
        };
        self.engine
            .work
            .charge((backwards.len() + prefix.len() + tail.len()) as u64)?;
        let mut edges = prefix.clone();
        edges.extend(backwards.into_iter().rev());
        for edge in tail.iter().rev() {
            edges.push(match *edge {
                PathEdge::Track { start, end, layer } => link(end, start, layer),
                PathEdge::Via(via) => PathEdge::Via(via),
            });
        }
        let mut points = Vec::new();
        let mut vias = Vec::new();
        let mut steps = 0u64;
        for &edge in &edges {
            self.engine.work.charge(1)?;
            match edge {
                PathEdge::Track { start, end, layer } => {
                    if points.is_empty() {
                        points.push(start);
                    }
                    points.push(end);
                    steps = steps.saturating_add(self.steps(start, end, layer));
                }
                PathEdge::Via(v) => {
                    if self
                        .engine
                        .legal_via(
                            self.net,
                            v.at,
                            0,
                            self.engine.layer_count() - 1,
                            ViaKind::Through,
                            &vias,
                        )?
                        .is_none()
                    {
                        return Ok(None);
                    }
                    vias.push(v);
                    if points.is_empty() {
                        points.push(v.at);
                    }
                    points.push(v.at);
                    steps = steps.saturating_add(self.via_step);
                }
            }
        }
        Ok((!edges.is_empty()).then_some(Path {
            points,
            edges,
            steps,
        }))
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) fn connect(
    engine: &Engine,
    net: u32,
    starts: &[(Point, u8, bool)],
    targets: &[TargetBounds<'_>],
    track_ok: &[bool],
    occupied_entries: usize,
    via_cost: u64,
    history: Option<&CongestionHistory>,
    budget: &mut TrialBudget<'_>,
    expanded: &mut u64,
    frontier: Option<&BlockedFrontier>,
) -> Result<Option<Path>, Failure> {
    let grid = engine.input.rules.grid_nm;
    let diameter = match engine
        .effective_rules
        .via_dims_for(net, false, engine.all_layers_slice())
    {
        Ok((d, _)) => d,
        Err(e) if e.status == Status::Unroutable => return Ok(None),
        Err(e) => return Err(e),
    };
    let pitch = ((diameter + engine.input.rules.clearance_nm + grid - 1) / grid) * grid;
    if pitch <= grid {
        return Ok(None);
    }
    let mut mesh = Mesh {
        engine,
        net,
        grid: grid as u64,
        pitch,
        via_step: engine.layer_policy.scale(via_cost),
        history,
        occupied_entries,
    };
    // A narrow channel can lie entirely between adjacent rows of the mesh.
    // Refine only when the coarser search has finished without a connection.
    // All pitches together get the same allowance as the original mesh search.
    let share =
        budget.current_limit.saturating_sub(*expanded) / engine.input.nets.len().max(1) as u64;
    let stop = expanded.saturating_add(share);
    loop {
        let path = mesh.search(starts, targets, track_ok, budget, expanded, frontier, stop)?;
        if path.is_some() || *expanded >= stop || mesh.pitch == grid {
            return Ok(path);
        }
        mesh.pitch = ((mesh.pitch / grid + 1) / 2) * grid;
    }
}

impl Mesh<'_> {
    #[allow(clippy::too_many_arguments)]
    fn search(
        &self,
        starts: &[(Point, u8, bool)],
        targets: &[TargetBounds<'_>],
        track_ok: &[bool],
        budget: &mut TrialBudget<'_>,
        expanded: &mut u64,
        frontier: Option<&BlockedFrontier>,
        stop: u64,
    ) -> Result<Option<Path>, Failure> {
        let engine = self.engine;
        let net = self.net;
        let mut expand = || {
            if *expanded >= stop {
                return Ok(false);
            }
            budget.check_before_expand(engine, *expanded, frontier)?;
            *expanded += 1;
            Ok(true)
        };
        let mut goals = HashMap::<Node, (u64, Vec<PathEdge>)>::new();
        let mut ends = Vec::new();
        for target in targets {
            let mut attach = |center, layer, plated| -> Result<(), Failure> {
                let layer = layer as u8;
                ends.push((center, layer));
                let portals = if plated {
                    // The barrel already reaches this layer. Connect with a trace
                    // and keep its existing hole out of the list of new drills.
                    self.portals(center, layer)?
                } else {
                    self.contact_portals(center, layer, track_ok, goals.len(), &mut expand)?
                };
                for (node, cost, edges) in portals {
                    if goals.get(&node).is_none_or(|(n, _)| cost < *n) {
                        self.storage(goals.len() + ends.len() + 1)?;
                        goals.insert(node, (cost, edges));
                    }
                }
                Ok(())
            };
            if let Some((center, mask)) = target.plated_contact(&engine.work)? {
                for (layer, &allowed) in track_ok.iter().enumerate() {
                    engine.work.charge(1)?;
                    if allowed
                        && mask & (1 << layer) != 0
                        && self.clear(center, center, layer as u8)?
                    {
                        attach(center, layer, true)?;
                    }
                }
            } else if let Some((center, layer)) = target.surface_contact(track_ok, &engine.work)? {
                attach(center, layer, false)?;
            }
        }
        if goals.is_empty() {
            return Ok(None);
        }
        let min_step = engine.layer_policy.minimum_step(net);
        let estimate = |(p, layer): Node| -> Result<u64, Failure> {
            engine.work.charge(ends.len() as u64)?;
            Ok(ends
                .iter()
                .map(|&(q, face)| {
                    ((p[0].abs_diff(q[0]) + p[1].abs_diff(q[1])).div_ceil(self.grid))
                        .saturating_mul(min_step)
                        .saturating_add(if layer == face { 0 } else { self.via_step })
                })
                .min()
                .unwrap_or(0))
        };
        let mut open = BinaryHeap::new();
        let mut costs = HashMap::new();
        let mut roots = HashMap::new();
        let mut parents = HashMap::new();
        for &(p, layer, via_only) in starts {
            engine.work.charge(1)?;
            if via_only || !track_ok[layer as usize] {
                continue;
            }
            for (node, cost, edges) in self.contact_portals(
                p,
                layer,
                track_ok,
                costs.len() + open.len() + roots.len() + goals.len(),
                &mut expand,
            )? {
                if costs.get(&node).is_none_or(|&c| cost < c) {
                    self.storage(costs.len() + open.len() + roots.len() + goals.len() + 3)?;
                    open.push(Reverse((
                        cost.saturating_add(estimate(node)?),
                        Reverse(cost),
                        node,
                    )));
                    costs.insert(node, cost);
                    roots.insert(node, edges);
                }
            }
        }
        let mut best_path: Option<Path> = None;
        while let Some(Reverse((priority, Reverse(cost), node))) = open.pop() {
            engine.work.charge(1)?;
            if best_path.as_ref().is_some_and(|p| p.steps <= priority) {
                break;
            }
            if costs.get(&node) != Some(&cost) {
                continue;
            }
            if !expand()? {
                break;
            }
            if let Some((tail_cost, tail)) = goals.get(&node) {
                // Two neighboring mesh attachments can have different terminal
                // lengths. Retain the complete cost before comparing open nodes.
                if best_path
                    .as_ref()
                    .is_none_or(|p| cost.saturating_add(*tail_cost) < p.steps)
                {
                    if let Some(path) = self.to_path(node, &parents, &roots, tail)? {
                        best_path = Some(path);
                    }
                }
            }
            let (at, layer) = node;
            let mut push = |dest, edge, delta| -> Result<(), Failure> {
                let g = cost.saturating_add(delta);
                if costs.get(&dest).is_none_or(|&old| g < old) {
                    self.storage(
                        costs.len() + open.len() + roots.len() + goals.len() + parents.len() + 3,
                    )?;
                    costs.insert(dest, g);
                    parents.insert(dest, (node, edge));
                    open.push(Reverse((
                        g.saturating_add(estimate(dest)?),
                        Reverse(g),
                        dest,
                    )));
                }
                Ok(())
            };
            for (dx, dy) in [
                (1, 0),
                (0, 1),
                (-1, 0),
                (0, -1),
                (1, 1),
                (-1, 1),
                (1, -1),
                (-1, -1),
            ] {
                let q = [at[0] + dx * self.pitch, at[1] + dy * self.pitch];
                if self.clear(at, q, layer)? {
                    push((q, layer), link(at, q, layer), self.steps(at, q, layer))?;
                }
            }
            let last = engine.layer_count() - 1;
            let via = PlacedViaKey {
                at,
                start_layer: 0,
                end_layer: last as u8,
                kind: 0,
            };
            if engine
                .legal_via(net, at, 0, last, ViaKind::Through, &[])?
                .is_some()
                && unpenalized_terminal(engine, self.history, &[PathEdge::Via(via)])?
            {
                for (other, &allowed) in track_ok.iter().enumerate() {
                    if allowed && usize::from(layer) != other {
                        push((at, other as u8), PathEdge::Via(via), self.via_step)?;
                    }
                }
            }
        }
        Ok(best_path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Input;

    #[test]
    fn plated_terminal_connects_on_its_listed_inner_layer_without_a_new_hole() {
        use crate::components::Copper;
        use crate::model::DrillShape;

        let mut input: Input = serde_json::from_str(include_str!(
            "../../tests/fixtures/multiterminal-native-two-nets.json"
        ))
        .unwrap();
        input.layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
            .map(str::to_owned)
            .to_vec();
        input.pads.truncate(1);
        input.pads[0].layers = vec!["F.Cu".into(), "In2.Cu".into(), "B.Cu".into()];
        input.pads[0].drill = Some(DrillShape::Circle {
            diameter_nm: 400_000,
        });
        input.tracks.clear();
        let engine = Engine::new(input).unwrap();
        let goal = TargetBounds::new(
            Copper::Pad(&engine.input.pads[0]),
            engine.all_layers_slice(),
            &engine.work,
        )
        .unwrap();
        let mesh = Mesh {
            engine: &engine,
            net: 1,
            grid: 250_000,
            pitch: 1_000_000,
            via_step: 5,
            history: None,
            occupied_entries: 0,
        };
        for (layer, expected) in [(1, false), (2, true)] {
            let mut allowed = [false; 4];
            allowed[layer] = true;
            let mut expanded = 0;
            let path = mesh
                .search(
                    &[([12_000_000, 10_000_000], layer as u8, false)],
                    std::slice::from_ref(&goal),
                    &allowed,
                    &mut TrialBudget::fixed(5_000),
                    &mut expanded,
                    None,
                    5_000,
                )
                .unwrap();
            assert_eq!(path.is_some(), expected);
            if let Some(path) = path {
                assert_eq!(
                    path.points.last(),
                    Some(&engine.input.pads[0].copper_center())
                );
                assert!(!path.edges.is_empty());
                assert!(path
                    .edges
                    .iter()
                    .all(|e| matches!(*e, PathEdge::Track { layer: 2, .. })));
            }
        }
    }

    #[test]
    fn mesh_checks_hole_spacing_between_new_drills_before_returning_a_route() {
        let mut input: Input = serde_json::from_str(include_str!(
            "../../tests/fixtures/multiterminal-native-two-nets.json"
        ))
        .unwrap();
        input.layers = vec![
            "F.Cu".into(),
            "In1.Cu".into(),
            "In2.Cu".into(),
            "B.Cu".into(),
        ];
        input.pads.clear();
        input.tracks.clear();
        let engine = Engine::new(input).unwrap();
        let mesh = Mesh {
            engine: &engine,
            net: 1,
            grid: 250_000,
            pitch: 1_000_000,
            via_step: 5,
            history: None,
            occupied_entries: 0,
        };
        for (separation, accepted) in [(1, false), (2_000_000, true)] {
            let a = [10_000_000, 8_000_000];
            let b = [a[0] + separation, a[1]];
            let via = |at| PlacedViaKey {
                at,
                start_layer: 0,
                end_layer: 3,
                kind: 0,
            };
            assert!(engine
                .legal_via(1, a, 0, 3, ViaKind::Through, &[])
                .unwrap()
                .is_some());
            assert!(engine
                .legal_via(1, b, 0, 3, ViaKind::Through, &[])
                .unwrap()
                .is_some());
            let start = (a, 0);
            let end = (b, 0);
            let parents = HashMap::from([
                ((a, 1), (start, PathEdge::Via(via(a)))),
                ((b, 1), ((a, 1), link(a, b, 1))),
                (end, ((b, 1), PathEdge::Via(via(b)))),
            ]);
            let roots = HashMap::from([(start, Vec::new())]);
            let route = mesh.to_path(end, &parents, &roots, &[]).unwrap();
            assert_eq!(route.is_some(), accepted);
            if let Some(path) = route {
                assert_eq!(path.points, [a, a, b, b]);
            }
            // A drill in the target attachment must survive reversal and be
            // checked against the drill in the source attachment.
            let roots = HashMap::from([((b, 1), vec![PathEdge::Via(via(a)), link(a, b, 1)])]);
            let contact = [b[0], b[1] + 200_000];
            let tail = [link(contact, b, 0), PathEdge::Via(via(b))];
            let route = mesh
                .to_path((b, 1), &HashMap::new(), &roots, &tail)
                .unwrap();
            assert_eq!(route.is_some(), accepted);
            if let Some(path) = route {
                assert_eq!(path.points, [a, a, b, b, contact]);
                assert_eq!(
                    path.steps,
                    mesh.steps(a, b, 1) + mesh.steps(b, contact, 0) + 10
                );
            }
        }
    }
}
