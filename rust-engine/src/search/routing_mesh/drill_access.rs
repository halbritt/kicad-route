//! Surface attachments when a small drill window has no coarse lattice point.
use super::{link, Failure, Mesh, Node, PathEdge, PlacedViaKey, Point, ViaKind};
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
        if !self.accepts(&[drill])? {
            return Ok(());
        }
        let drill_price = self.drill_price(via)?;
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
                    prefix_cost.saturating_add(drill_price).saturating_add(cost),
                    path,
                ));
            }
        }
        Ok(())
    }

    /// Last visible grid center before a nearby obstruction along one axis.
    fn turn_at_obstruction(
        &self,
        p: Point,
        layer: u8,
        axis: usize,
        sign: i64,
        radius: i64,
    ) -> Result<Option<Point>, Failure> {
        let grid = self.grid as i64;
        let at = |steps| {
            let mut q = p;
            q[axis] += sign * grid * steps;
            q
        };
        let mut blocked = radius / grid;
        if self.clear(p, at(blocked), layer)? {
            return Ok(None);
        }
        let mut clear = 0;
        while blocked - clear > 1 {
            let mid = clear.midpoint(blocked);
            if self.clear(p, at(mid), layer)? {
                clear = mid;
            } else {
                blocked = mid;
            }
        }
        Ok((clear > 0).then(|| at(clear)))
    }

    fn drill_after_turn(
        &self,
        bend: Point,
        layer: u8,
        axis: usize,
        sign: i64,
    ) -> Result<Option<PlacedViaKey>, Failure> {
        let grid = self.grid as i64;
        let bound = if sign < 0 {
            self.engine.input.bounds.min
        } else {
            self.engine.input.bounds.max
        };
        let extent = bound[axis].abs_diff(bend[axis]) / self.grid + 1;
        let at = |steps: u64| {
            let mut q = bend;
            q[axis] += sign * grid * steps as i64;
            q
        };
        let Some(mut via) = super::super::layer_crossing::terminal_via(
            self.engine,
            self.net,
            layer as usize,
            bend,
            extent,
            at,
            &[],
        )?
        else {
            return Ok(None);
        };
        // Keep a known legal outer end while shortening the sampled stub.
        // Other legal apertures can exist between the earlier samples.
        let mut outer = via.at[axis].abs_diff(bend[axis]) / self.grid;
        let mut inner = outer / 2;
        while outer - inner > 1 {
            let mid = inner.midpoint(outer);
            if self
                .engine
                .legal_via(
                    self.net,
                    at(mid),
                    0,
                    self.engine.layer_count() - 1,
                    ViaKind::Through,
                    &[],
                )?
                .is_some()
            {
                outer = mid;
            } else {
                inner = mid;
            }
        }
        via.at = at(outer);
        Ok(self.clear(bend, via.at, layer)?.then_some(via))
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
                && self.accepts(&[PathEdge::Via(via_key(q))])?
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
            if self.accepts(&[stub])? {
                self.via_portals(
                    via,
                    layer,
                    &[stub],
                    self.price(p, via.at, layer)?,
                    allowed,
                    occupied,
                    &mut result,
                )?;
            }
        }
        if result.len() > surface {
            return Ok(result);
        }
        let grid = self.grid as i64;
        let radius = self.pitch * 2;
        // When a nearby mesh junction cannot admit a barrel, or drilling is
        // excluded throughout the local window, try turning along a pad row
        // at the first obstruction. The strip beyond the turn may reach
        // drillable copper outside the local window.
        let mut extra_used = 0;
        let window_covered =
            self.same_no_via_region(p.map(|v| v - radius), p.map(|v| v + radius))?;
        if surface != 0 || window_covered {
            let mut bends = Vec::new();
            for (axis, sign) in [(0, -1), (0, 1), (1, -1), (1, 1)] {
                if extra_used >= optional_expansions {
                    break;
                }
                let Some(bend) = self.turn_at_obstruction(p, layer, axis, sign, radius)? else {
                    continue;
                };
                if !expand()? {
                    break;
                }
                extra_used += 1;
                for direction in [-1, 1] {
                    let Some(via) = self.drill_after_turn(bend, layer, 1 - axis, direction)? else {
                        continue;
                    };
                    let cost = self
                        .price(p, bend, layer)?
                        .saturating_add(self.price(bend, via.at, layer)?);
                    self.storage(occupied + result.len() + bends.len() + 1)?;
                    bends.push((cost, bend, via));
                }
            }
            self.engine.work.charge(
                bends.len() as u64 * bends.len().max(1).next_power_of_two().ilog2() as u64,
            )?;
            bends.sort_by_key(|(cost, bend, via)| (*cost, *bend, via.at));
            for (cost, bend, via) in bends.iter().copied() {
                let prefix = [link(p, bend, layer), link(bend, via.at, layer)];
                self.via_portals(
                    via,
                    layer,
                    &prefix,
                    cost,
                    allowed,
                    occupied + bends.len(),
                    &mut result,
                )?;
                if result.len() > surface {
                    return Ok(result);
                }
            }
        }
        // Every grid site considered by the local flood lies in this square.
        if window_covered {
            return Ok(result);
        }
        self.storage(occupied + result.len() + 2)?;
        let mut parents = HashMap::from([(p, p)]);
        let mut queue = VecDeque::from([(p, 0usize)]);
        let mut found_level = None;
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
                let price = if self.pricing.paid() {
                    let mut total = 0u64;
                    for edge in &prefix {
                        if let PathEdge::Track { start, end, layer } = *edge {
                            total = total.saturating_add(self.price(start, end, layer)?);
                        }
                    }
                    total
                } else {
                    (level as u64).saturating_mul(self.engine.layer_policy.weight(layer as usize))
                };
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
            pricing: super::super::MeshPricing::Unpenalized,
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
    fn surface_trace_can_turn_between_lands_and_reach_a_barrel_beyond_the_package() {
        let mut input: Input = serde_json::from_str(include_str!(
            "../../../tests/fixtures/multiterminal-native-two-nets.json"
        ))
        .unwrap();
        input.layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
            .map(str::to_owned)
            .to_vec();
        input.rules.grid_nm = 100_000;
        input.rules.width_nm = 200_000;
        input.rules.clearance_nm = 200_000;
        input.pads.clear();
        input.tracks.clear();
        let center = [7_170_000i64, 10_170_000i64];
        for row in -1..=1 {
            for col in -1..=1 {
                let signal = row == 0 && col == 0;
                let id = format!("9e9a5669-0215-4d8d-8232-{:012}", 5 + 3 * row + col);
                input.pads.push(serde_json::from_value(serde_json::json!({
                    "id": id, "net":if signal {1} else {0}, "layers":["F.Cu"],
                    "at":[center[0]+col*1_620_000,center[1]+row*1_200_000],
                    "shape":{"kind":"rect","half_size_nm":[if col == 0 {220_000} else {950_000},210_000]}
                })).unwrap());
            }
        }
        let edge = |x, y| [center[0] + x, center[1] + y];
        input.rule_areas.push(serde_json::from_value(serde_json::json!({
            "id":"5496846d-27f7-4f55-bcdd-2473db731e64","layers":["In1.Cu"],
            "outer":[edge(-2_550_000,-1_750_000),edge(2_550_000,-1_750_000),edge(2_550_000,1_750_000),edge(-2_550_000,1_750_000)],
            "no_tracks": false, "no_vias":true
        })).unwrap());
        let engine = Engine::new(input).unwrap();
        let mesh = Mesh {
            engine: &engine,
            net: 1,
            grid: 100_000,
            pitch: 800_000,
            via_step: 5,
            history: None,
            occupied_entries: 0,
            pricing: super::super::MeshPricing::Unpenalized,
        };
        assert!(
            super::super::super::layer_crossing::drill_sites(&engine, 1, center, 0, None)
                .unwrap()
                .is_empty()
        );
        let run = |available| {
            let mut charged = 0;
            let portals = mesh
                .contact_portals(center, 0, &[true; 4], 0, available, &mut || {
                    charged += 1;
                    Ok(true)
                })
                .unwrap();
            (charged, portals)
        };
        assert!(run(0).1.iter().all(|((_, layer), _, _)| *layer == 0));
        let (charged, portals) = run(4);
        assert!((1..=4).contains(&charged));
        let ((_, dest), cost, edges) = portals
            .iter()
            .find(|((_, face), _, _)| *face == 1)
            .expect("a physical inner-layer attachment");
        let mut at = center;
        let mut layer = 0;
        let mut price = 0;
        let mut drilled = false;
        for &edge in edges {
            match edge {
                PathEdge::Track {
                    start,
                    end,
                    layer: face,
                } => {
                    assert_eq!((start, face), (at, layer));
                    assert!(mesh.clear(start, end, layer).unwrap());
                    price += mesh.steps(start, end, layer);
                    at = end;
                }
                PathEdge::Via(via) => {
                    assert!(!drilled);
                    assert_eq!(via.at, at);
                    assert!(at[0].abs_diff(center[0]) > 2_550_000 && at[1] != center[1]);
                    assert!(engine
                        .legal_via(1, at, 0, 3, ViaKind::Through, &[])
                        .unwrap()
                        .is_some());
                    price += mesh.via_step;
                    layer = *dest;
                    drilled = true;
                }
            }
        }
        assert!(drilled);
        assert_eq!(price, *cost);
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
            pricing: super::super::MeshPricing::Unpenalized,
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
