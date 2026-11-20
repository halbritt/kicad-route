//! A through-drill at either terminal can put both terminals on a common
//! plane. Identify the plane with the longest clear approaches from the ends.
use super::{Failure, Mesh, Node, PathEdge, PlacedViaKey, Point, ViaKind};
use crate::search::monotone_trunk::clear_span;
use std::collections::HashMap;

impl Mesh<'_> {
    /// A surface mesh goal may itself admit a through-drill. Register that
    /// same connection on its other faces, including the actual drill price.
    pub(super) fn drilled_goals(
        &self,
        goals: &mut HashMap<Node, (u64, Vec<PathEdge>)>,
        allowed: &[bool],
        occupied: usize,
    ) -> Result<(), Failure> {
        self.storage(occupied + 2 * goals.len())?;
        let mut surface: Vec<_> = goals.keys().copied().collect();
        self.engine.work.charge(
            surface.len() as u64 * surface.len().max(1).next_power_of_two().ilog2() as u64,
        )?;
        surface.sort_unstable();
        let last = self.engine.layer_count() - 1;
        for node in surface {
            let (pt, layer) = node;
            if usize::from(layer) != 0 && usize::from(layer) != last {
                continue;
            }
            let (cost, tail) = &goals[&node];
            self.engine.work.charge(tail.len() as u64 + 1)?;
            if tail.iter().any(|e| matches!(e, PathEdge::Via(_)))
                || self
                    .engine
                    .legal_via(self.net, pt, 0, last, ViaKind::Through, &[])?
                    .is_none()
            {
                continue;
            }
            let v = PlacedViaKey {
                at: pt,
                start_layer: 0,
                end_layer: last as u8,
                kind: 0,
            };
            let total = cost.saturating_add(self.drill_price(v)?);
            for (k, &ok) in allowed.iter().enumerate() {
                if !ok || k == usize::from(layer) {
                    continue;
                }
                let dest = (pt, k as u8);
                if goals.get(&dest).is_none_or(|(old, _)| *old > total) {
                    let tail = &goals[&node].1;
                    self.storage(occupied + goals.len() + tail.len() + 2)?;
                    self.engine.work.charge(tail.len() as u64 + 1)?;
                    let mut path = tail.clone();
                    path.push(PathEdge::Via(v));
                    goals.insert(dest, (total, path));
                }
            }
        }
        Ok(())
    }

    pub(super) fn common_approach(
        &self,
        src: Point,
        dst: Point,
        launches: &HashMap<Node, Vec<PathEdge>>,
        goals: &HashMap<Node, (u64, Vec<PathEdge>)>,
    ) -> Result<Option<u8>, Failure> {
        let axis = usize::from(src[0].abs_diff(dst[0]) < src[1].abs_diff(dst[1]));
        let limit = (src[axis].abs_diff(dst[axis]) / self.pitch as u64) as i64;
        if limit == 0 {
            return Ok(None);
        }
        let sign = (dst[axis] - src[axis]).signum();
        let mut reach = vec![[None; 2]; self.engine.layer_count()];
        let mut probe = |&(at, l): &Node, side: usize| -> Result<(), Failure> {
            let direction = if side == 0 { sign } else { -sign };
            let bound = if direction > 0 {
                self.engine.input.bounds.max
            } else {
                self.engine.input.bounds.min
            };
            let n = limit.min((bound[axis].abs_diff(at[axis]) / self.pitch as u64) as i64);
            let extent = clear_span(n, |steps| {
                let mut q = at;
                q[axis] += direction * steps * self.pitch;
                self.clear(at, q, l)
            })?;
            let record = &mut reach[usize::from(l)][side];
            *record = Some(record.unwrap_or(0).max(extent));
            Ok(())
        };
        for n in launches.keys() {
            probe(n, 0)?;
        }
        for n in goals.keys() {
            probe(n, 1)?;
        }
        // If the clearance sweeps cannot distinguish the planes, keep the
        // normal cost ordering across every layer.
        let mut score = (0, 0);
        let mut preferred = None;
        for (layer, approaches) in reach.into_iter().enumerate() {
            if let [Some(a), Some(b)] = approaches {
                let candidate = (a + b, a.max(b));
                if candidate > score {
                    score = candidate;
                    preferred = Some(layer as u8);
                } else if candidate == score {
                    preferred = None;
                }
            }
        }
        Ok(preferred)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{model::Input, negotiate::CongestionHistory, Engine};

    fn blank_board() -> Engine {
        let mut input: Input = serde_json::from_str(include_str!(
            "../../../tests/fixtures/multiterminal-native-two-nets.json"
        ))
        .unwrap();
        input.pads.clear();
        input.tracks.clear();
        Engine::new(input).unwrap()
    }

    #[test]
    fn clipped_tracks_charge_each_directed_fine_move_and_diagonal_crossings() {
        let engine = blank_board();
        let grid = engine.input.rules.grid_nm;
        let point = |x, y| [x * grid / 10, y * grid / 10];
        let mut h = CongestionHistory::default();
        for (x, y, w) in [(0, 0, 2), (1, 0, 7), (0, 1, 3), (1, 1, 5)] {
            h.add_weight((0, x, y), w, &engine).unwrap();
        }
        for (a, b, expected) in [
            (point(4, 0), point(19, 0), 14),
            (point(19, 0), point(4, 0), 4),
            (point(1, 4), point(16, 19), 13),
            (point(16, 19), point(1, 4), 7),
            (point(2, 2), point(12, 12), 5),
            (point(12, 12), point(2, 2), 2),
            (point(3, 3), point(3, 3), 0),
        ] {
            assert_eq!(
                h.octilinear_penalty(&engine, 0, a, b).unwrap(),
                expected,
                "{a:?} -> {b:?}"
            );
        }
    }

    #[test]
    fn a_paid_terminal_portal_compares_both_clear_elbows_in_the_requested_direction() {
        let engine = blank_board();
        let at = [8_800_000, 7_200_000];
        let q = [12_000_000, 12_000_000];
        // Only the diagonal-first elbow crosses this cell.
        let mut h = CongestionHistory::default();
        h.add_weight((0, 40, 33), 23, &engine).unwrap();
        h.add_weight((3, 40, 33), 11, &engine).unwrap();
        let mesh = Mesh {
            engine: &engine,
            net: 1,
            grid: 250_000,
            pitch: 4_000_000,
            via_step: 5,
            history: Some(&h),
            occupied_entries: 0,
            pricing: super::super::MeshPricing::Forward,
        };
        let choose = |m: &Mesh| {
            let (_, c, edges) = m
                .portals(at, 0)
                .unwrap()
                .into_iter()
                .find(|(node, _, _)| *node == (q, 0))
                .unwrap();
            let PathEdge::Track { end, .. } = edges[0] else {
                panic!("expected terminal track")
            };
            (c, end)
        };
        let v = PlacedViaKey {
            at: [10_000_000, 8_250_000],
            start_layer: 0,
            end_layer: 3,
            kind: 0,
        };
        assert_eq!(
            mesh.drill_price(v).unwrap(),
            mesh.via_step + engine.layer_policy.scale(34)
        );
        let cheaper = [8_800_000, 8_800_000];
        assert_eq!(
            choose(&mesh),
            (
                mesh.steps(at, cheaper, 0) + mesh.steps(cheaper, q, 0),
                cheaper
            )
        );
        let incoming = Mesh {
            pricing: super::super::MeshPricing::Reverse,
            ..mesh
        };
        assert_eq!(choose(&incoming), choose(&mesh));
        assert_eq!(
            choose(&Mesh {
                history: None,
                ..mesh
            })
            .1,
            [12_000_000, 10_400_000]
        );
    }

    #[test]
    fn a_stamped_surface_launch_keeps_its_real_drill_and_the_other_end_of_the_plane() {
        use crate::components::Copper;
        use crate::negotiate::TrialBudget;
        use crate::search::TargetBounds;
        let mut input: Input = serde_json::from_str(include_str!(
            "../../../tests/fixtures/multiterminal-native-two-nets.json"
        ))
        .unwrap();
        input.layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
            .map(String::from)
            .to_vec();
        input.tracks.clear();
        input.pads.truncate(2);
        input.rules.width_nm = 200_000;
        input.rules.clearance_nm = 150_000;
        let source = [4_130_000, 7_270_000];
        let destination = [34_850_000, 9_530_000];
        for (p, at) in input.pads.iter_mut().zip([source, destination]) {
            p.at = at;
            p.net = 1;
            p.layers = vec!["F.Cu".into()];
        }
        for (k, l) in ["F.Cu", "In1.Cu", "B.Cu"].into_iter().enumerate() {
            input.rule_areas.push(serde_json::from_value(serde_json::json!({
                "id":format!("20000000-0000-4000-8000-{k:012}"),"layers":[l],
                "outer":[[12_000_000,0],[30_000_000,0],[30_000_000,20_000_000],[12_000_000,20_000_000]],
                "no_tracks":true,"no_vias":false
            })).unwrap());
        }
        let engine = Engine::new(input).unwrap();
        let grid = engine.input.rules.grid_nm;
        let mut history = CongestionHistory::default();
        // Previously routed surface copper has been removed from this
        // entrance. Its cells still cost one extra step during the repair.
        for x in 5..=25 {
            for y in 18..=41 {
                history.add_weight((0, x, y), 1, &engine).unwrap();
            }
        }
        let target = TargetBounds::new(
            Copper::Pad(&engine.input.pads[1]),
            engine.all_layers_slice(),
            &engine.work,
        )
        .unwrap();
        let mesh = Mesh {
            engine: &engine,
            net: 1,
            grid: grid as u64,
            pitch: 1_000_000,
            via_step: engine.layer_policy.scale(engine.effective_rules.via_cost),
            history: Some(&history),
            occupied_entries: 0,
            pricing: super::super::MeshPricing::Unpenalized,
        };
        let mut registrations = 0;
        assert!(mesh
            .contact_portals(source, 0, &[true; 4], 0, 25, &mut || {
                registrations += 1;
                Ok(true)
            })
            .unwrap()
            .is_empty());
        let mut limited = 0;
        let partial = mesh
            .search(
                &[(source, 0, false)],
                std::slice::from_ref(&target),
                &[true; 4],
                &mut TrialBudget::fixed(1),
                &mut limited,
                None,
                1,
            )
            .unwrap();
        assert!(partial.is_none());
        assert_eq!(limited, 1);
        for allowed in [[true; 4], [true, true, false, true]] {
            let mut expanded = 0;
            let route = mesh
                .search(
                    &[(source, 0, false)],
                    std::slice::from_ref(&target),
                    &allowed,
                    &mut TrialBudget::fixed(2048),
                    &mut expanded,
                    None,
                    2048,
                )
                .unwrap();
            assert!(0 < expanded && expanded <= 2048);
            assert_eq!(route.is_some(), allowed[2]);
            if let Some(path) = route {
                assert_eq!(path.points.first(), Some(&source));
                assert_eq!(path.points.last(), Some(&destination));
                let mut base = 0;
                let mut penalty = 0;
                let mut drills = Vec::new();
                for e in path.edges {
                    match e {
                        PathEdge::Track { start, end, layer } => {
                            assert!(engine.legal_edge_on(1, layer as usize, start, end).unwrap());
                            base += mesh.steps(start, end, layer);
                            penalty += history
                                .octilinear_penalty(&engine, layer, start, end)
                                .unwrap();
                        }
                        PathEdge::Via(v) => {
                            assert!(engine
                                .legal_via(1, v.at, 0, 3, ViaKind::Through, &drills)
                                .unwrap()
                                .is_some());
                            drills.push(v);
                            base += mesh.via_step;
                            penalty +=
                                history.via_penalty(v.at, 0, 3, engine.input.bounds.min, grid);
                        }
                    }
                }
                assert_eq!(drills.len(), 2);
                assert!(penalty > 0);
                assert_eq!(path.steps, base + engine.layer_policy.scale(penalty));
            }
        }
    }
}
