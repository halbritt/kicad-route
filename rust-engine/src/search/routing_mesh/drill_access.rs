//! Surface attachments when a small drill window has no coarse lattice point.
use super::{
    link, unpenalized_terminal, Failure, Mesh, Node, PathEdge, PlacedViaKey, Point, ViaKind,
};
use std::collections::{HashMap, VecDeque};

pub(super) type Portal = (Node, u64, Vec<PathEdge>);

impl Mesh<'_> {
    fn via_portals(
        &self,
        via: PlacedViaKey,
        face: u8,
        prefix: &[PathEdge],
        prefix_cost: u64,
        allowed: &[bool],
        occupied: usize,
        result: &mut Vec<Portal>,
    ) -> Result<(), Failure> {
        let drill = PathEdge::Via(via);
        if !unpenalized_terminal(self.engine, self.history, &[drill])? {
            return Ok(());
        }
        for (other, &ok) in allowed.iter().enumerate() {
            self.engine.work.charge(1)?;
            if !ok
                || other == usize::from(face)
                || !self.engine.layer_policy.track_allowed(self.net, other)
            {
                continue;
            }
            for (node, cost, edges) in self.portals(via.at, other as u8)? {
                self.engine
                    .work
                    .charge((prefix.len() + edges.len() + 1) as u64)?;
                self.storage(occupied + result.len() + 1)?;
                let mut path = prefix.to_vec();
                path.push(drill);
                path.extend(edges);
                result.push((
                    node,
                    prefix_cost
                        .saturating_add(self.via_step)
                        .saturating_add(cost),
                    path,
                ));
            }
        }
        Ok(())
    }

    pub(super) fn contact_portals(
        &self,
        p: Point,
        layer: u8,
        allowed: &[bool],
        occupied: usize,
        optional_expansions: u64,
        expand: &mut impl FnMut() -> Result<bool, Failure>,
    ) -> Result<Vec<Portal>, Failure> {
        let mut result = self.portals(p, layer)?;
        let last = self.engine.layer_count() - 1;
        let via_key = |at| PlacedViaKey {
            at,
            start_layer: 0,
            end_layer: last as u8,
            kind: 0,
        };
        for &((q, _), _, _) in &result {
            self.engine.work.charge(1)?;
            if self
                .engine
                .legal_via(self.net, q, 0, last, ViaKind::Through, &[])?
                .is_some()
                && unpenalized_terminal(self.engine, self.history, &[PathEdge::Via(via_key(q))])?
            {
                return Ok(result);
            }
        }
        let surface = result.len();
        for via in super::super::layer_crossing::drill_sites(
            self.engine,
            self.net,
            p,
            usize::from(layer),
            None,
        )? {
            let stub = link(p, via.at, layer);
            if unpenalized_terminal(self.engine, self.history, &[stub])? {
                self.via_portals(
                    via,
                    layer,
                    &[stub],
                    self.steps(p, via.at, layer),
                    allowed,
                    occupied,
                    &mut result,
                )?;
            }
        }
        if result.len() > surface {
            return Ok(result);
        }
        // Work outward from the contact on its own ordinary grid phase. Each
        // legal axial run may turn around the adjacent pad or keepout. Stop at
        // the nearest level with a usable drill, or at the far side of the
        // neighboring coarse cell. The ordinary search can go farther.
        let grid = self.grid as i64;
        let radius = self.pitch * 2;
        // Every site considered by this local drill search is inside the
        // square. A through-via exclusion on any crossed layer covers it.
        if self.same_no_via_region(p.map(|v| v - radius), p.map(|v| v + radius))? {
            return Ok(result);
        }
        self.storage(occupied + result.len() + 2)?;
        let mut parents = HashMap::from([(p, p)]);
        let mut queue = VecDeque::from([(p, 0usize)]);
        let mut found_level = None;
        let mut extra_used = 0;
        while let Some((at, level)) = queue.pop_front() {
            if (surface != 0 && extra_used >= optional_expansions)
                || found_level.is_some_and(|found| level > found)
                || !expand()?
            {
                break;
            }
            extra_used += 1;
            self.engine.work.charge(1)?;
            if self
                .engine
                .legal_via(self.net, at, 0, last, ViaKind::Through, &[])?
                .is_some()
            {
                let mut prefix = Vec::new();
                let mut q = at;
                while q != p {
                    self.engine.work.charge(1)?;
                    self.storage(
                        occupied + result.len() + parents.len() + queue.len() + prefix.len() + 1,
                    )?;
                    let prev = parents[&q];
                    prefix.push(link(prev, q, layer));
                    q = prev;
                }
                prefix.reverse();
                let price =
                    (level as u64).saturating_mul(self.engine.layer_policy.weight(layer as usize));
                self.via_portals(
                    via_key(at),
                    layer,
                    &prefix,
                    price,
                    allowed,
                    occupied + parents.len() + queue.len() + prefix.len(),
                    &mut result,
                )?;
                if result.len() > surface {
                    found_level = Some(level);
                }
            }
            if found_level.is_some() {
                continue;
            }
            for (dx, dy) in [(grid, 0), (0, grid), (-grid, 0), (0, -grid)] {
                self.engine.work.charge(1)?;
                let q = [at[0] + dx, at[1] + dy];
                if q[0].abs_diff(p[0]).max(q[1].abs_diff(p[1])) > radius as u64
                    || parents.contains_key(&q)
                    || !self.clear(at, q, layer)?
                {
                    continue;
                }
                self.storage(occupied + result.len() + parents.len() + queue.len() + 2)?;
                parents.insert(q, at);
                queue.push_back((q, level + 1));
            }
        }
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Input;
    use crate::Engine;

    #[test]
    fn a_drill_exclusion_covering_the_search_window_needs_no_surface_expansion() {
        let mut input: Input = serde_json::from_str(include_str!(
            "../../../tests/fixtures/multiterminal-native-two-nets.json"
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
        input.rule_areas.push(
            serde_json::from_value(serde_json::json!({
                "id":"350dcdff-86b7-4e9d-8a69-2833fbc18ddd","layers":["In1.Cu"],
                "outer":[[0,0],[40000000,0],[40000000,20000000],[0,20000000]],
                "no_tracks":false,"no_vias":true
            }))
            .unwrap(),
        );
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
        let p = [13_150_000, 8_350_000];
        let expected = mesh.portals(p, 0).unwrap();
        let mut used = 0;
        let got = mesh
            .contact_portals(p, 0, &[true; 4], 0, 1000, &mut || {
                used += 1;
                Ok(true)
            })
            .unwrap();
        assert!(!expected.is_empty());
        assert_eq!(used, 0);
        assert_eq!(got.len(), expected.len());
        assert!(got.iter().all(|((_, layer), _, edges)| *layer == 0
            && edges
                .iter()
                .all(|e| matches!(e, PathEdge::Track { layer: 0, .. }))));
    }

    #[test]
    fn bent_surface_drill_access_consumes_the_available_local_expansions() {
        let mut input: Input = serde_json::from_str(include_str!(
            "../../../tests/fixtures/multiterminal-native-two-nets.json"
        ))
        .unwrap();
        input.layers = vec!["F.Cu".into(), "B.Cu".into()];
        input.pads.clear();
        input.tracks.clear();
        input.rule_areas.push(serde_json::from_value(serde_json::json!({
            "id": "854bc63a-7fdf-43fb-a486-21372ff7cdda", "layers": ["F.Cu", "B.Cu"],
            "outer": [[0,0],[40000000,0],[40000000,20000000],[0,20000000]],
            "holes": [[[9850000,8850000],[9850000,9850000],[10850000,9850000],[10850000,8850000]]],
            "no_tracks": false, "no_vias": true
        })).unwrap());
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
        let pad = [9_350_000, 8_350_000];
        let hole = [10_350_000, 9_350_000];
        assert!(engine
            .legal_via(1, hole, 0, 1, ViaKind::Through, &[])
            .unwrap()
            .is_some());
        let measure = |limit, optional| {
            let mut used = 0;
            let portals = mesh
                .contact_portals(pad, 0, &[true, true], 0, optional, &mut || {
                    if used == limit {
                        return Ok(false);
                    }
                    used += 1;
                    Ok(true)
                })
                .unwrap();
            (used, portals)
        };
        let (paid, surface_only) = measure(1000, 1);
        assert_eq!(paid, 1);
        assert!(surface_only.iter().all(|((_, layer), _, _)| *layer == 0));
        let (used, short) = measure(1, u64::MAX);
        assert_eq!(used, 1);
        assert!(short.iter().all(|((_, layer), _, _)| *layer == 0));
        let (used, complete) = measure(1000, u64::MAX);
        assert!(used > 1 && used < 1000);
        let (_, _, edges) = complete
            .iter()
            .find(|((_, layer), _, _)| *layer == 1)
            .expect("reachable drill window");
        let mut at = pad;
        let mut drilled = false;
        for edge in edges {
            match *edge {
                PathEdge::Track { start, end, layer } => {
                    assert_eq!(start, at);
                    assert_eq!(layer, u8::from(drilled));
                    assert!(mesh.clear(start, end, layer).unwrap());
                    at = end;
                }
                PathEdge::Via(v) => {
                    assert_eq!(v.at, at);
                    assert_ne!(v.at[0], pad[0]);
                    assert_ne!(v.at[1], pad[1]);
                    assert!(!drilled);
                    drilled = true;
                }
            }
        }
        assert!(drilled);
    }
}
