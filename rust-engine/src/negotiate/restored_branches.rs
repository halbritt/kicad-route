//! Reuse whole branches of a displaced net after checking the changed copper.
use super::{RouteUnitAdapter, RouteUnitId};
use crate::{
    components::{self, Copper},
    model::{Failure, Status, ViaKind},
    search::{PathEdge, PlacedViaKey},
    Engine,
};
use std::{collections::BTreeMap, ops::Range};

pub(super) fn rip(
    engine: &mut Engine,
    adapter: &mut dyn RouteUnitAdapter,
    unit: RouteUnitId,
    pad_counts: &BTreeMap<u32, usize>,
) -> Result<Option<Range<usize>>, Failure> {
    let first = engine.copper.journal_len();
    adapter.rip_unit(engine, unit)?;
    let eligible = adapter.as_single_ended().is_some()
        && matches!(unit, RouteUnitId::SingleNet(net) if pad_counts.get(&net).is_some_and(|n| *n > 2));
    Ok(eligible.then(|| first..engine.copper.journal_len()))
}

pub(super) fn restore(engine: &mut Engine, net: u32, removed: Range<usize>) -> Result<(), Failure> {
    // The journal already owns the original copper while the requester searches.
    // The temporary partition, edges and pad contacts live only during recovery.
    if removed.len() < 2 || removed.len().saturating_mul(8) > engine.limits().max_search_entries {
        return Ok(());
    }
    engine.charge_work(removed.len() as u64)?;
    let groups = components::partition(
        engine.copper.removed_copper(removed),
        &engine.layer_names,
        &engine.work,
        crate::geometry::track_lands_on_via,
    )?;
    if groups.len() < 2 {
        return Ok(());
    }
    let mut branches = Vec::new();
    for group in groups {
        let mut pads = 0;
        for pad in &engine.input.pads {
            engine.charge_work(1)?;
            if pad.net != net {
                continue;
            }
            for &item in &group {
                engine.charge_work(pad.shape.parts().len() as u64)?;
                if item.touches(
                    Copper::Pad(pad),
                    &engine.layer_names,
                    crate::geometry::track_lands_on_via,
                ) {
                    pads += 1;
                    break;
                }
            }
            if pads == 2 {
                break;
            }
        }
        if pads < 2 {
            continue;
        }
        let mut edges = Vec::with_capacity(group.len());
        for item in group {
            engine.charge_work(1)?;
            edges.push(match item {
                Copper::Track(t) => PathEdge::Track {
                    start: t.start,
                    end: t.end,
                    layer: engine.layer_index(&t.layer).expect("generated track layer") as u8,
                },
                Copper::Via(v) => PathEdge::Via(PlacedViaKey {
                    at: v.at,
                    start_layer: engine
                        .layer_index(&v.start_layer)
                        .expect("generated via layer") as u8,
                    end_layer: engine
                        .layer_index(&v.end_layer)
                        .expect("generated via layer") as u8,
                    kind: match v.kind {
                        ViaKind::Through => 0,
                        ViaKind::BlindBuried => 1,
                        ViaKind::Micro => 2,
                    },
                }),
                _ => unreachable!("only generated tracks and vias can be ripped"),
            });
        }
        branches.push(edges);
    }
    for edges in branches {
        // insert_edges validates the entire proposal before publishing any copper.
        match engine.insert_edges(net, edges) {
            Err(e) if e.status == Status::Unroutable => {}
            result => result?,
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        model::{DrillShape, Net},
        negotiate::{tests::test_input, SingleEndedAdapter},
        TransactionError,
    };

    #[test]
    fn recovers_only_entire_pad_to_pad_branches_on_the_changed_copper() {
        let mut input = test_input();
        input.layers = vec!["F.Cu".into(), "B.Cu".into()];
        input.rules.same_net_hole_clearance_nm = 0;
        input.nets.push(Net {
            id: 2,
            name: "CROSSING".into(),
        });
        input.pads[0].layers = vec!["B.Cu".into()];
        input.pads[1].at = [90, 50];
        input.pads[1].layers = vec!["F.Cu".into()];
        let mut middle = input.pads[0].clone();
        middle.id = "10000000-0000-4000-8000-000000000003".into();
        middle.at = [50, 50];
        middle.layers = input.layers.clone();
        middle.drill = Some(DrillShape::Circle { diameter_nm: 1 });
        input.pads.push(middle);
        let mut engine = Engine::new(input).unwrap();
        let track = |layer, start, end| PathEdge::Track { start, end, layer };
        engine
            .transaction(|tx| -> Result<(), Failure> {
                tx.engine.insert_edges(
                    1,
                    vec![
                        track(1, [10, 50], [50, 50]),
                        track(0, [50, 50], [60, 50]),
                        track(0, [60, 50], [65, 55]),
                        track(0, [65, 55], [85, 55]),
                        track(0, [85, 55], [90, 50]),
                    ],
                )?;
                let original: Vec<_> = tx.engine.copper.generated().cloned().collect();
                assert_eq!(tx.engine.components(1)?.len(), 1);
                let before = tx.work_used();
                let trial = tx.transaction(|trial| -> Result<(), Failure> {
                    let range = rip(
                        trial.engine,
                        &mut SingleEndedAdapter::default(),
                        RouteUnitId::SingleNet(1),
                        &BTreeMap::from([(1, 3)]),
                    )?
                    .unwrap();
                    trial
                        .engine
                        .insert_edges(2, vec![track(0, [70, 40], [70, 70])])?;
                    restore(trial.engine, 1, range)?;
                    let retained = trial.engine.copper.net_generated_tracks(1);
                    assert_eq!(retained.len(), 1);
                    assert_eq!(
                        (&retained[0].layer, retained[0].start, retained[0].end),
                        (&original[0].layer, [10, 50], [50, 50])
                    );
                    assert_eq!(trial.engine.components(1)?.len(), 2);
                    Err(Failure::new(Status::Unroutable, "unresolved displaced net"))
                });
                assert!(matches!(trial, Err(TransactionError::Operation(_))));
                assert!(tx.work_used() > before);
                assert_eq!(
                    tx.engine.copper.generated().cloned().collect::<Vec<_>>(),
                    original
                );
                Ok(())
            })
            .unwrap();
    }
}
