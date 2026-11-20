use super::*;
use crate::{
    model::Net,
    search::{Path, PathEdge},
};

#[test]
fn an_existing_legal_path_and_supplemental_probes_preserve_the_blocking_net() {
    let mut input = tests::test_input();
    input.layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        .map(str::to_owned)
        .to_vec();
    input.nets.push(Net {
        id: 2,
        name: "SHIELD".into(),
    });
    input.rules.same_net_hole_clearance_nm = 0;
    for pad in &mut input.pads {
        pad.layers = vec!["F.Cu".into()];
    }
    let mut engine = Engine::new(input).unwrap();
    let mut frontier = BlockedFrontier::new();
    for layer in [1, 2] {
        let (start, end) = ([20, 20], [60, 20]);
        engine
            .insert_path(
                2,
                Path {
                    points: vec![start, end],
                    edges: vec![PathEdge::Track { start, end, layer }],
                    steps: 4,
                },
            )
            .unwrap();
        assert!(!engine
            .legal_edge_on(1, layer as usize, [40, 10], [40, 30])
            .unwrap());
        frontier.push_edge(layer, [40, 10], [40, 30]);
    }
    frontier.push_via_site(0, 3, [40, 20]);
    let groups = engine.components(1).unwrap();
    let path =
        crate::search::join_components(&engine, 1, &groups[0], &groups[1..], 1000, &mut 0).unwrap();
    assert!(!path.edges.is_empty());
    for edge in &path.edges {
        let PathEdge::Track { layer, start, end } = *edge else {
            panic!("expected surface route")
        };
        assert!(engine.legal_edge_on(1, layer as usize, start, end).unwrap());
    }
    let units = SingleEndedAdapter::all().units(&engine).unwrap();
    let specs: BTreeMap<_, _> = units.into_iter().map(|u| (u.id, u)).collect();
    let observations = std::cell::RefCell::new(Vec::new());
    let mut observe = |eng: &Engine, f: &BlockedFrontier, spent, _available| {
        let ranked = analyze_blockers(
            eng,
            &specs[&RouteUnitId::SingleNet(1)],
            f,
            &SingleEndedAdapter::all(),
            &specs,
            &BTreeMap::new(),
        )?;
        assert_eq!(ranked[0].unit, RouteUnitId::SingleNet(2));
        assert_eq!(ranked[0].edge_layers, 0b0110);
        assert!(ranked[0].blocks_via);
        let mut calls = observations.borrow_mut();
        calls.push(spent);
        Ok(calls.len() == 2)
    };
    let mut budget = TrialBudget::fixed(100);
    budget.crossing = Some(CrossingCheck {
        origin: 0,
        next_size: 1,
        next_effort: 4,
        observe: &mut observe,
    });
    budget
        .check_before_expand(&engine, 4, Some(&frontier))
        .unwrap();
    budget
        .check_interlayer_exchange(&engine, 2, Some(&frontier), None, || 0)
        .unwrap();
    budget
        .check_interlayer_exchange(&engine, 4, Some(&frontier), Some(path.steps), || 0)
        .unwrap();
    assert!(observations.borrow().is_empty());
    budget
        .check_interlayer_exchange(&engine, 4, Some(&frontier), None, || 0)
        .unwrap();
    budget
        .check_interlayer_exchange(&engine, 8, Some(&frontier), None, || 0)
        .unwrap();
    assert!(!engine.legal_edge_on(1, 1, [30, 10], [30, 30]).unwrap());
    frontier.push_edge(1, [30, 10], [30, 30]);
    assert!(!budget.aborted);
    let err = budget
        .check_interlayer_exchange(&engine, 8, Some(&frontier), None, || 0)
        .unwrap_err();
    assert_eq!(err.status, Status::BudgetExhausted);
    assert_eq!(*observations.borrow(), [4, 8]);
    assert_eq!(engine.copper().net_generated_tracks(2).len(), 2);
}

#[test]
fn failed_exchange_restores_victim_ids_prices_and_paid_expansions() {
    let mut input = tests::test_input();
    input.nets.push(Net {
        id: 2,
        name: "BUS".into(),
    });
    input.pads[0].at = [50, 2];
    input.pads[1].at = [50, 98];
    for (i, at) in [[2, 50], [98, 50]].into_iter().enumerate() {
        let mut pad = input.pads[i].clone();
        pad.net = 2;
        pad.id = format!("20000000-0000-4000-8000-{:012x}", i + 3);
        pad.at = at;
        input.pads.push(pad);
    }
    let run = |speculate: bool| {
        let mut engine = Engine::new(input.clone()).unwrap();
        let mut adapter = SingleEndedAdapter::all();
        engine
            .transaction(|tx| {
                let (requester, victim) = (RouteUnitId::SingleNet(1), RouteUnitId::SingleNet(2));
                let mut expanded = 0;
                let mut history = CongestionHistory::new();
                adapter
                    .route_unit(tx.engine, victim, &history, 10_000, &mut expanded)
                    .unwrap();
                let original: Vec<_> = tx.engine.copper.generated().cloned().collect();
                assert!(!original.is_empty());
                if speculate {
                    let specs: BTreeMap<_, _> = adapter
                        .units(tx.engine)
                        .unwrap()
                        .into_iter()
                        .map(|u| (u.id, u))
                        .collect();
                    let net_to_unit = BTreeMap::from([(1, requester), (2, victim)]);
                    let mut frontier = BlockedFrontier::new();
                    frontier.push_edge(0, [50, 40], [50, 60]);
                    let mut stats = NegotiationStats::default();
                    let mut counts = BTreeMap::new();
                    let mut no_cap = BTreeSet::new();
                    let charged = (expanded, tx.engine.work.used());
                    let (resolved, blocker) = try_local_ripup_round(
                        tx,
                        &mut adapter,
                        &net_to_unit,
                        &specs,
                        &specs[&requester],
                        &frontier,
                        None,
                        &mut history,
                        &mut counts,
                        &mut stats,
                        10_000,
                        &mut expanded,
                        Some(100),
                        true,
                        Some(1000),
                        &mut no_cap,
                        &mut 0,
                    )
                    .unwrap();
                    assert!(!resolved);
                    assert_eq!(blocker.unwrap().unit, victim);
                    assert_eq!((stats.units_ripped, stats.rounds_rolled_back), (1, 1));
                    assert!(counts.is_empty() && no_cap.is_empty() && history.is_empty());
                    assert!(expanded > charged.0 && tx.engine.work.used() > charged.1);
                    assert_eq!(
                        tx.engine.copper.generated().cloned().collect::<Vec<_>>(),
                        original
                    );
                    let retry_limit = expanded + 2;
                    assert!(adapter
                        .route_unit(tx.engine, requester, &history, retry_limit, &mut expanded)
                        .is_err());
                    assert_eq!(expanded, retry_limit);
                }
                adapter.rip_unit(tx.engine, victim).unwrap();
                adapter
                    .route_unit(tx.engine, requester, &history, 10_000, &mut expanded)
                    .unwrap();
                Ok::<_, Failure>(tx.engine.copper.generated().cloned().collect::<Vec<_>>())
            })
            .unwrap()
    };
    assert_eq!(run(true), run(false));
}
