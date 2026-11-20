//! Trace around nearby copper on a terminal face. Every accepted segment is
//! swept against the board. Bounds only suggest where to try the next bend.
use super::{link, Failure, Mesh, Node, Path, Point, TargetBounds};
use crate::rules::DruItemKind;
use crate::spatial::SweptAabb;
use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, BinaryHeap};
use std::ops::ControlFlow;

impl Mesh<'_> {
    /// If both ends lie inside one convex no-via area, a through via cannot
    /// leave the direct channel. Seek a continuous in-layer detour there.
    fn same_no_via_region(&self, a: Point, b: Point) -> Result<bool, Failure> {
        for area in &self.engine.input.rule_areas {
            self.engine.work.charge(1)?;
            if area.no_vias && !area.layers.is_empty() {
                if let Some((lo, hi)) = area.rectangular_bounds() {
                    if (0..2).all(|k| lo[k] <= a[k].min(b[k]) && a[k].max(b[k]) <= hi[k]) {
                        return Ok(true);
                    }
                }
            }
        }
        Ok(false)
    }

    fn bends_at_obstruction(
        &self,
        start: Point,
        end: Point,
        layer: u8,
        occupied: usize,
    ) -> Result<BTreeSet<Point>, Failure> {
        let face = usize::from(layer);
        let engine = self.engine;
        let name = engine.layer_name(face);
        let width = engine.effective_rules.track_width_for(self.net, name)?;
        // Find the short interval where this segment first loses clearance.
        // Rounding an intermediate point can only affect the suggested bends.
        let distance = start[0].abs_diff(end[0]).max(start[1].abs_diff(end[1]));
        if distance == 0 {
            return Ok(BTreeSet::new());
        }
        let point = |n: u64| -> Point {
            std::array::from_fn(|k| {
                start[k]
                    + (i128::from(end[k] - start[k]) * i128::from(n) / i128::from(distance)) as i64
            })
        };
        let mut lo = 0;
        let mut hi = distance;
        while hi - lo > self.grid {
            let mid = lo.midpoint(hi);
            if engine.legal_edge_on(self.net, face, start, point(mid))? {
                lo = mid;
            } else {
                hi = mid;
            }
        }
        let max_clr = engine
            .effective_rules
            .max_pair_clearance_nm
            .max(engine.max_pad_local_clearance_nm);
        let scan = SweptAabb::swept(point(lo), point(hi), width, max_clr);
        let mut bends = BTreeSet::new();
        let mut rectangle = |lower: Point, upper: Point, margin: i64| -> Result<(), Failure> {
            let low = lower.map(|x| x.saturating_sub(margin));
            let high = upper.map(|x| x.saturating_add(margin));
            for x in [low[0], high[0]] {
                for y in [low[1], high[1]] {
                    engine.work.charge(1 + bends.len().max(1).ilog2() as u64)?;
                    self.storage(occupied + bends.len() + 1)?;
                    bends.insert([x, y]);
                }
            }
            Ok(())
        };
        let radius = (width + 1) / 2 + 1;
        let _ = engine.pad_spatial[face].visit_matching(
            scan,
            Some(self.net),
            &engine.work,
            |id, _| {
                let p = &engine.input.pads[id as usize];
                engine.work.charge(p.shape.parts().len() as u64)?;
                for part in p.shape.parts() {
                    if let crate::model::Shape::ConvexPolygon { vertices, .. } = part {
                        engine.work.charge(vertices.len() as u64)?;
                    }
                }
                let clr = engine.effective_rules.pair_clearance(
                    self.net,
                    DruItemKind::Track,
                    p.net,
                    DruItemKind::Pad,
                    name,
                    p.local_clearance_nm,
                );
                let (a, b) = p.copper_bounds();
                rectangle(a, b, radius + clr)?;
                Ok(ControlFlow::Continue(()))
            },
        )?;
        let _ = engine.keepout_spatial[face].visit_matching(scan, None, &engine.work, |_, g| {
            engine.work.charge(1)?;
            if g.no_tracks {
                if let Some((a, b)) =
                    engine.input.rule_areas[g.rule_area_idx as usize].rectangular_bounds()
                {
                    rectangle(a, b, radius)?;
                }
            }
            Ok(ControlFlow::Continue(()))
        })?;
        let _ = engine.copper.visit_layer_candidates_with_net_filtered(
            face,
            scan,
            Some(self.net),
            &engine.work,
            |net, t| {
                let clr = engine.effective_rules.pair_clearance(
                    self.net,
                    DruItemKind::Track,
                    net,
                    DruItemKind::Track,
                    name,
                    None,
                );
                let a = std::array::from_fn(|k| t.start[k].min(t.end[k]));
                let b = std::array::from_fn(|k| t.start[k].max(t.end[k]));
                rectangle(a, b, radius + clr + (t.width_nm + 1) / 2)?;
                Ok(ControlFlow::Continue(()))
            },
        )?;
        let _ = engine.copper.visit_via_candidates_filtered(
            scan,
            Some(self.net),
            &engine.work,
            |via, mask| {
                if mask & (1 << face) != 0 {
                    let clr = engine.effective_rules.pair_clearance(
                        self.net,
                        DruItemKind::Track,
                        via.net,
                        DruItemKind::Via,
                        name,
                        None,
                    );
                    rectangle(via.at, via.at, radius + clr + (via.diameter_nm + 1) / 2)?;
                }
                Ok(ControlFlow::Continue(()))
            },
        )?;
        Ok(bends)
    }

    pub(super) fn around_corners(
        &self,
        starts: &[(Point, u8, bool)],
        targets: &[TargetBounds<'_>],
        allowed: &[bool],
        expand: &mut impl FnMut() -> Result<bool, Failure>,
    ) -> Result<Option<Path>, Failure> {
        let work = &self.engine.work;
        if self.engine.input.rule_areas.is_empty() {
            return Ok(None);
        }
        let mut ends = Vec::new();
        for t in targets {
            work.charge(1)?;
            if let Some((at, mask)) = t.plated_contact(work)? {
                for (i, &ok) in allowed.iter().enumerate() {
                    work.charge(1)?;
                    if ok && mask & (1 << i) != 0 {
                        self.storage(ends.len() + 1)?;
                        ends.push((at, i as u8));
                    }
                }
            } else if let Some((at, i)) = t.surface_contact(allowed, work)? {
                self.storage(ends.len() + 1)?;
                ends.push((at, i as u8));
            }
        }
        let nearest = |at, layer| -> Result<Option<(u64, Point)>, Failure> {
            work.charge(ends.len() as u64)?;
            Ok(ends
                .iter()
                .filter(|(_, l)| *l == layer)
                .map(|&(q, _)| (self.steps(at, q, layer), q))
                .min())
        };
        let mut queue = BinaryHeap::new();
        let mut parents = BTreeMap::<Node, (u64, Option<Point>)>::new();
        for &(p, layer, only_via) in starts {
            work.charge(1)?;
            if only_via || !allowed[layer as usize] || parents.contains_key(&(p, layer)) {
                continue;
            }
            if let Some((h, q)) = nearest(p, layer)? {
                if !self.same_no_via_region(p, q)? {
                    continue;
                }
                self.storage(ends.len() + parents.len() + queue.len() + 2)?;
                parents.insert((p, layer), (0, None));
                queue.push(Reverse((h, Reverse(0u64), p, layer, q)));
            }
        }
        while let Some(Reverse((_, Reverse(cost), at, layer, goal))) = queue.pop() {
            work.charge(1)?;
            if parents[&(at, layer)].0 != cost {
                continue;
            }
            if !expand()? {
                break;
            }
            if at != goal && self.clear(at, goal, layer)? {
                let mut points = vec![goal, at];
                let mut curr = at;
                while let Some(prev) = parents[&(curr, layer)].1 {
                    self.storage(ends.len() + parents.len() + queue.len() + points.len() + 1)?;
                    work.charge(1)?;
                    points.push(prev);
                    curr = prev;
                }
                points.reverse();
                work.charge(points.len() as u64)?;
                let edges = points.windows(2).map(|s| link(s[0], s[1], layer)).collect();
                let steps = points.windows(2).fold(0u64, |total, s| {
                    total.saturating_add(self.steps(s[0], s[1], layer))
                });
                return Ok(Some(Path {
                    points,
                    edges,
                    steps,
                }));
            }
            let occupied = ends.len() + parents.len() + queue.len();
            let bends = self.bends_at_obstruction(at, goal, layer, occupied)?;
            let bend_count = bends.len();
            for p in bends {
                work.charge(1)?;
                let next = cost.saturating_add(self.steps(at, p, layer));
                if at == p
                    || parents.get(&(p, layer)).is_some_and(|old| old.0 <= next)
                    || !self.clear(at, p, layer)?
                {
                    continue;
                }
                if let Some((h, q)) = nearest(p, layer)? {
                    self.storage(ends.len() + parents.len() + queue.len() + bend_count + 2)?;
                    queue.push(Reverse((
                        next.saturating_add(h),
                        Reverse(next),
                        p,
                        layer,
                        q,
                    )));
                    parents.insert((p, layer), (next, Some(at)));
                }
            }
        }
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::components::Copper;
    use crate::model::Input;
    use crate::Engine;

    #[test]
    fn a_local_drill_exclusion_can_detour_around_a_foreign_trace() {
        let mut input: Input = serde_json::from_str(include_str!(
            "../../../tests/fixtures/multiterminal-native-two-nets.json"
        ))
        .unwrap();
        input.layers = vec!["F.Cu".into(), "B.Cu".into()];
        input.pads = vec![input.pads[0].clone()];
        input.pads[0].layers = vec!["F.Cu".into()];
        input.tracks[0].layer = "F.Cu".into();
        input.rule_areas.push(serde_json::from_value(serde_json::json!({
            "id":"d6ca586a-d7c7-4173-9bff-13e4fbd1d988",
            "layers":["B.Cu"],
            "outer":[[2000000,2000000],[19000000,2000000],[19000000,18000000],[2000000,18000000]],
            "no_tracks":false, "no_vias":true
        })).unwrap());
        let engine = Engine::new(input).unwrap();
        let target = TargetBounds::new(
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
        let start = [17_000_000, 10_000_000];
        let end = engine.input.pads[0].copper_center();
        assert!(mesh.same_no_via_region(start, end).unwrap());
        assert!(!mesh
            .same_no_via_region([20_000_000, 10_000_000], end)
            .unwrap());
        assert!(!mesh.clear(start, end, 0).unwrap());
        let attempt = |room: &mut dyn FnMut() -> bool| {
            mesh.around_corners(
                &[(start, 0, false)],
                std::slice::from_ref(&target),
                &[true, true],
                &mut || Ok(room()),
            )
            .unwrap()
        };
        assert!(attempt(&mut || false).is_none());
        let mut expansions = 0;
        let path = attempt(&mut || {
            expansions += 1;
            expansions <= 64
        })
        .unwrap();
        assert!(expansions > 1 && expansions <= 64);
        assert_eq!(path.points.first(), Some(&start));
        assert_eq!(path.points.last(), Some(&end));
        assert!(path.points.len() >= 4);
        let mut cost = 0;
        for piece in path.points.windows(2) {
            assert!(mesh.clear(piece[0], piece[1], 0).unwrap());
            cost += mesh.steps(piece[0], piece[1], 0);
        }
        assert_eq!(path.steps, cost);
    }
}
