use kicad_route_engine::{model::*, run, Engine};
use std::collections::BTreeSet;
fn id(n: u64) -> String {
    format!("10000000-0000-4000-8000-{n:012x}")
}
fn pad(n: u64, at: Point) -> Pad {
    Pad {
        id: id(n),
        net: 1,
        at,
        shape: Shape::Circle { radius_nm: 1 },
        drill: None,
        layers: Vec::new(),
        local_clearance_nm: None,
        zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
        footprint_ref: None,
        pad_number: None,
        fromto_id: None,
    }
}
fn track(n: u64, a: Point, b: Point) -> Track {
    Track {
        id: id(n),
        net: 1,
        start: a,
        end: b,
        width_nm: 2,
        locked: true,
        layer: "F.Cu".into(),
    }
}
fn base() -> Input {
    Input {
        version: 1,
        source_id: "a".repeat(64),
        layer: "F.Cu".into(),
        bounds: Bounds {
            min: [0, 0],
            max: [80, 80],
        },
        rules: Rules {
            width_nm: 2,
            clearance_nm: 1,
            grid_nm: 10,
            edge_clearance_nm: 0,
            hole_clearance_nm: 0,
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
        },
        nets: vec![Net {
            id: 1,
            name: "N".into(),
        }],
        pads: vec![pad(1, [10, 30])],
        tracks: vec![track(2, [35, 11], [35, 49])],
        max_expansions: 10000,
        limits: Limits::default(),
        holes: Vec::new(),
        board_outline: None,
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
        layers: Vec::new(),
        vias: Vec::new(),
        rule_areas: Vec::new(),
        board_design_settings: None,
        project_rules: None,
        custom_rules: None,
        effective_rules: None,
        zones: Vec::new(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: None,
    }
}
mod model {
    pub use kicad_route_engine::model::*;
}
#[path = "support/contact.rs"]
mod contact;
// Independent axis-aligned copper-contact graph. Endpoints need overlap,
// not identical center-line vertices. Diagonal control is checked separately.
fn assert_copper_connected(i: &Input, added: &[Track]) {
    let bodies: Vec<_> = i
        .pads
        .iter()
        .filter(|p| p.net == 1)
        .map(contact::Body::pad)
        .chain(
            i.tracks
                .iter()
                .chain(added)
                .filter(|t| t.net == 1)
                .map(contact::Body::track),
        )
        .collect();
    let mut reached = BTreeSet::new();
    let mut todo = vec![0];
    while let Some(n) = todo.pop() {
        if reached.insert(n) {
            for j in 0..bodies.len() {
                if !reached.contains(&j) && bodies[n].contacts(bodies[j]) {
                    todo.push(j);
                }
            }
        }
    }
    assert_eq!(reached.len(), bodies.len(), "copper not joined: {added:?}");
}
fn generated(e: &Engine) -> Vec<Track> {
    e.copper().generated().cloned().collect()
}
#[test]
fn singleton_pad_joins_incumbent_body_between_grid_nodes() {
    let i = base();
    let o = run(i.clone());
    assert_eq!(o.status, Status::Complete);
    assert!(!o.added_tracks.is_empty());
    assert_copper_connected(&i, &o.added_tracks);
    // radius1+radius1-1: earliest integer cap contact is x34.
    assert!(o.added_tracks.iter().any(|t| t.end == [34, 30]));
    assert!(o.added_tracks.iter().all(|t| t.start != t.end));
}
#[test]
fn diagonal_off_grid_incumbent_attaches_at_its_interior() {
    let mut i = base();
    i.tracks[0].start = [33, 11];
    i.tracks[0].end = [37, 49];
    let o = run(i.clone());
    assert_eq!(o.status, Status::Complete);
    assert_eq!(o.added_tracks.len(), 1);
    let t = &o.added_tracks[0];
    assert_eq!(t.start, [10, 30]);
    // Line vector(4,38); at(34,30), distance squared is38^2/1460<1.
    // At preceding integer(33,30),76^2/1460>1. No production predicate.
    assert_eq!(t.end, [34, 30]);
    assert!(38 * 38 < 4 * 4 + 38 * 38 && 76 * 76 > 4 * 4 + 38 * 38);
}
#[test]
fn internal_jumper_groups_connect_across_layers_without_creating_copper() {
    let mut i = base();
    i.tracks.clear();
    i.layers = vec!["F.Cu".into(), "B.Cu".into()];
    i.nets.push(Net {
        id: 2,
        name: "OTHER".into(),
    });
    i.pads.extend([pad(2, [40, 30]), pad(3, [70, 30])]);
    for p in &mut i.pads {
        p.footprint_ref = Some("J".into());
    }
    i.pads[1].layers = vec!["B.Cu".into()];
    i.components = vec![serde_json::from_value(serde_json::json!({
        "ref_des": "J", "jumper_pad_groups": [[id(1), id(2)], [id(2), id(3)]]
    }))
    .unwrap()];
    let mut e = Engine::new(i.clone()).unwrap();
    assert_eq!(e.route(1, 1).unwrap(), 0);
    assert_eq!(e.copper().tracks().count(), 0);
    assert_eq!(e.copper().vias().count(), 0);
    // The foreign net can cross a jumper's gap, but must clear its actual land.
    assert!(e.legal_edge(2, [20, 20], [20, 40]).unwrap());
    assert!(!e.legal_edge(2, [10, 20], [10, 40]).unwrap());
    assert!(e
        .legal_edge_on_layer(2, "F.Cu", [40, 20], [40, 40])
        .unwrap());
    assert!(!e
        .legal_edge_on_layer(2, "B.Cu", [40, 20], [40, 40])
        .unwrap());

    let mut unknown = i.clone();
    unknown.components[0].jumper_pad_groups[0][0] = id(42);
    let mut other_net = i.clone();
    other_net.pads[0].net = 2;
    let mut other_footprint = i;
    other_footprint.pads[0].footprint_ref = Some("J2".into());
    for (bad, reason) in [
        (unknown, "unknown pad"),
        (other_net, "same footprint and net"),
        (other_footprint, "same footprint and net"),
    ] {
        let failure = bad.validate().unwrap_err();
        assert_eq!(Engine::new(bad).err(), Some(failure.clone()));
        assert_eq!(failure.status, Status::Invalid);
        assert!(failure.reason.contains(reason));
    }
}

#[test]
fn connected_pads_do_not_hide_a_stray_copper_component() {
    let mut i = base();
    i.pads.push(pad(3, [20, 30]));
    i.tracks.push(track(4, [10, 30], [20, 30]));
    let o = run(i.clone());
    assert_eq!(o.status, Status::Complete);
    assert!(!o.added_tracks.is_empty());
    assert_copper_connected(&i, &o.added_tracks);
}
#[test]
fn three_track_only_components_with_off_grid_seeds_are_joined() {
    let mut i = base();
    i.pads.clear();
    i.tracks = vec![
        track(2, [11, 11], [21, 11]),
        track(3, [41, 1], [41, 41]),
        track(4, [61, 21], [61, 41]),
    ];
    let o = run(i.clone());
    assert_eq!(o.status, Status::Complete);
    assert_copper_connected(&i, &o.added_tracks);
    assert!(o
        .added_tracks
        .iter()
        .any(|t| t.start[0] % 10 != 0 || t.start[1] % 10 != 0));
    assert_eq!(
        serde_json::to_vec(&o).unwrap(),
        serde_json::to_vec(&run(i)).unwrap()
    );
}
#[test]
fn net_zero_stays_disconnected_and_blocks_other_net_joins() {
    let mut i = base();
    i.nets.push(Net {
        id: 0,
        name: String::new(),
    });
    let mut wall = track(9, [30, 0], [30, 80]);
    wall.net = 0;
    i.tracks.push(wall);
    let o = run(i.clone());
    assert_eq!(o.status, Status::Unroutable);
    assert!(o.added_tracks.is_empty());
    i.pads[0].net = 0;
    i.tracks[0].net = 0;
    let o = run(i);
    assert_eq!(o.status, Status::Complete);
    assert!(o.added_tracks.is_empty());
}
#[test]
fn budget_failure_after_first_component_join_restores_net_and_ids() {
    let first = base();
    let first_result = run(first.clone());
    assert_eq!(first_result.status, Status::Complete);
    let mut i = first;
    i.tracks.push(track(3, [65, 11], [65, 49]));
    let mut e = Engine::new(i.clone()).unwrap();
    let original = e.copper().input().to_vec();
    let err = e.route(1, first_result.expanded_states).unwrap_err();
    assert_eq!(err.status, Status::BudgetExhausted);
    assert_eq!(err.expanded_states, first_result.expanded_states);
    assert!(generated(&e).is_empty());
    assert_eq!(e.copper().input(), original);
    assert!(e.copper().index_consistent());
    e.route(1, 10000).unwrap();
    let expected = run(i.clone());
    assert_eq!(generated(&e), expected.added_tracks);
    assert_copper_connected(&i, &generated(&e));
    i.max_expansions = first_result.expanded_states;
    let o = run(i);
    assert_eq!(o.status, Status::BudgetExhausted);
    assert!(o.added_tracks.is_empty());
}
#[test]
fn outer_rollback_undoes_completed_and_rerouted_component_joins() {
    let mut i = base();
    i.tracks.push(track(3, [65, 11], [65, 49]));
    let mut e = Engine::new(i.clone()).unwrap();
    let outcome = e.transaction(|outer| {
        outer.route(1, 10000).unwrap();
        assert!(!outer.copper().generated().collect::<Vec<_>>().is_empty());
        outer
            .transaction(|inner| {
                inner.reroute(1, 10000).unwrap();
                Ok::<_, ()>(())
            })
            .unwrap();
        Err::<(), _>(())
    });
    assert!(outcome.is_err());
    assert!(generated(&e).is_empty());
    assert_eq!(e.copper().input(), i.tracks);
    assert!(e.copper().index_consistent());
    e.route_all().unwrap();
    assert_eq!(generated(&e), run(i.clone()).added_tracks);
    assert_copper_connected(&i, &generated(&e));
}
#[test]
fn conservative_tiny_width_contact_failure_is_not_complete() {
    let mut i = base();
    i.pads.clear();
    i.rules.width_nm = 1;
    i.tracks = vec![track(2, [11, 11], [21, 11]), track(3, [41, 11], [51, 11])];
    for t in &mut i.tracks {
        t.width_nm = 1;
    }
    let o = run(i);
    assert_eq!(o.status, Status::Unroutable);
    assert!(o.added_tracks.is_empty());
    assert!(o.reason.unwrap().contains("seed"));
}

#[test]
fn shortened_attachment_is_legal_even_when_full_grid_step_hits_wall() {
    let mut i = base();
    i.bounds = Bounds {
        min: [0, 0],
        max: [20, 20],
    };
    i.pads = vec![pad(1, [10, 10])];
    i.tracks = vec![track(2, [15, 5], [15, 15])];
    i.nets.push(Net {
        id: 0,
        name: String::new(),
    });
    let mut wall = track(3, [18, 1], [18, 19]);
    wall.net = 0;
    i.tracks.push(wall);
    let e = Engine::new(i.clone()).unwrap();
    assert!(e.legal_edge(1, [10, 10], [15, 10]).unwrap());
    assert!(!e.legal_edge(1, [10, 10], [20, 10]).unwrap());
    let o = run(i.clone());
    assert_eq!(o.status, Status::Complete);
    assert_eq!(o.added_tracks.len(), 1);
    assert_eq!(o.added_tracks[0].start, [10, 10]);
    assert_eq!(o.added_tracks[0].end, [14, 10]); // first 1nm contact, not centerline
    assert_eq!(o.expanded_states, 2); // seed, then one unit-cost shortened goal edge
    assert_copper_connected(&i, &o.added_tracks);
    let mut limited = i;
    limited.max_expansions = 1;
    let failed = run(limited);
    assert_eq!(failed.status, Status::BudgetExhausted);
    assert_eq!(failed.expanded_states, 1);
    assert!(failed.added_tracks.is_empty());
}

#[test]
fn clearance_shadowed_target_joins_with_a_full_radius_of_copper() {
    // Fable F1: preserve the original dimensions and rules. The terminal cap
    // must overlap y6000000 by 1 nm, but cannot approach the foreign y6300000
    // centerline closer than 400000 nm. Thus y5800001..=5900000 is legal.
    let mut i = base();
    i.bounds.max = [10000000, 10000000];
    i.rules = Rules {
        width_nm: 300000,
        clearance_nm: 200000,
        grid_nm: 500000,
        edge_clearance_nm: 200000,
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    i.max_expansions = 200000;
    i.nets.push(Net {
        id: 2,
        name: "FOREIGN".into(),
    });
    i.pads = vec![Pad {
        id: id(1),
        net: 1,
        at: [5000000, 2000000],
        shape: Shape::Circle { radius_nm: 300000 },
        drill: None,
        layers: Vec::new(),
        local_clearance_nm: None,
        zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
        footprint_ref: None,
        pad_number: None,
        fromto_id: None,
    }];
    i.tracks = vec![
        Track {
            id: id(2),
            net: 1,
            start: [2000000, 6000000],
            end: [8000000, 6000000],
            width_nm: 100000,
            locked: false,
            layer: "F.Cu".into(),
        },
        Track {
            id: id(3),
            net: 2,
            start: [2000000, 6300000],
            end: [8000000, 6300000],
            width_nm: 100000,
            locked: true,
            layer: "F.Cu".into(),
        },
    ];
    let o = run(i.clone());
    assert_eq!(o.status, Status::Complete);
    assert_eq!(o.added_tracks.len(), 1);
    let t = &o.added_tracks[0];
    assert_eq!(t.start, [5000000, 2000000]);
    // The old endpoint left 1 nm of copper overlap. Here the narrower
    // track's centerline reaches the wider cap with clearance left over.
    assert_eq!(t.end, [5000000, 5850000]);
    assert!(contact::Body::track(&i.tracks[0]).accepts_landing(contact::Body::track(t)));
    let mut old = t.clone();
    old.end = [5000000, 5800001];
    assert!(contact::Body::track(&old).contacts(contact::Body::track(&i.tracks[0])));
    assert!(!contact::Body::track(&i.tracks[0]).accepts_landing(contact::Body::track(&old)));
    assert_copper_connected(&i, &o.added_tracks);
    assert!(!contact::Body::track(t).violates(contact::Body::track(&i.tracks[1]), 200000));
}

#[test]
fn rect_rect_contact_helper_distinguishes_overlap_from_tangency() {
    let mut a = pad(1, [20, 20]);
    a.shape = Shape::Rect {
        half_size_nm: [5, 5],
    };
    let mut b = a.clone();
    b.id = id(2);
    for at in [[30, 20], [30, 30], [29, 30]] {
        b.at = at;
        assert!(!contact::Body::pad(&a).contacts(contact::Body::pad(&b)));
        assert!(!kicad_route_engine::geometry::pads_touch(&a, &b));
    }
    for at in [[29, 20], [29, 29]] {
        b.at = at;
        assert!(contact::Body::pad(&a).contacts(contact::Body::pad(&b)));
        assert!(kicad_route_engine::geometry::pads_touch(&a, &b));
    }
}

#[test]
fn two_pad_shadowed_center_rect_routes_via_edge_attachment() {
    let mut i = base();
    i.bounds.max = [10000000, 10000000];
    i.rules = Rules {
        width_nm: 300000,
        clearance_nm: 200000,
        grid_nm: 500000,
        edge_clearance_nm: 200000,
        hole_clearance_nm: 0,
        same_net_hole_clearance_nm: 0,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
    };
    i.max_expansions = 200000;
    i.nets.push(Net {
        id: 2,
        name: "FOREIGN".into(),
    });
    i.pads = vec![
        Pad {
            id: id(1),
            net: 1,
            at: [5000000, 2000000],
            shape: Shape::Circle { radius_nm: 300000 },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
        },
        Pad {
            id: id(2),
            net: 1,
            at: [5000000, 6000000],
            shape: Shape::Rect {
                half_size_nm: [200000, 50000],
            },
            drill: None,
            layers: Vec::new(),
            local_clearance_nm: None,
            zone_connection: kicad_route_engine::model::PadZoneConnection::Inherited,
            thermal_gap_nm: None,
            thermal_spoke_width_nm: None,
            thermal_spoke_angle_mdeg: None,
            orientation_mdeg: 0,
            footprint_ref: None,
            pad_number: None,
            fromto_id: None,
        },
    ];
    i.tracks = vec![Track {
        id: id(3),
        net: 2,
        start: [2000000, 6300000],
        end: [8000000, 6300000],
        width_nm: 100000,
        locked: true,
        layer: "F.Cu".into(),
    }];
    let o = run(i.clone());
    assert_eq!(o.status, Status::Complete, "{:?}", o.reason);
    assert_eq!(o.added_tracks.len(), 1);
    let t = &o.added_tracks[0];
    assert_eq!(t.start, [5000000, 2000000]);
    // The 150 µm trace cap extends 50 µm into the 100 µm tall pad.
    assert_eq!(t.end, [5000000, 5850000]);
    assert!(contact::Body::pad(&i.pads[1]).accepts_landing(contact::Body::track(t)));
    assert_copper_connected(&i, &o.added_tracks);
    assert!(!contact::Body::track(t).violates(contact::Body::track(&i.tracks[0]), 200000));
}

#[test]
fn small_zone_island_can_join_the_side_of_a_large_filled_region() {
    let mut input = base();
    input.bounds.max = [1000, 1000];
    input.max_expansions = 12;
    input.tracks.clear();
    input.pads = vec![pad(1, [100, 50]), pad(2, [325, 480])];
    let large = [[10, 10], [300, 10], [300, 950], [10, 950]];
    let small = [[310, 470], [330, 470], [330, 490], [310, 490]];
    input.zones = vec![serde_json::from_value(serde_json::json!({
        "id":id(3), "net":1, "layers":["F.Cu"], "connection":"solid", "filled":true,
        "outer":[[1,1],[999,1],[999,999],[1,999]], "min_thickness_nm":1,
        "layer_fills":[{"layer":"F.Cu","polygons":[{"outer":large},{"outer":small}]}]
    }))
    .unwrap()];
    let plan = run(input.clone());
    assert_eq!(plan.status, Status::Complete, "{:?}", plan.reason);
    assert_eq!(plan.added_tracks.len(), 1);
    let edge = &plan.added_tracks[0];
    assert_eq!(edge.start[1], edge.end[1]);
    assert!(edge.start[1] >= 469 && edge.start[1] <= 491);
    assert!(edge.start[0].min(edge.end[0]) <= 300);
    assert!(edge.start[0].max(edge.end[0]) >= 310);
    input.pads.reverse();
    input.zones[0].layer_fills[0].polygons.reverse();
    let swapped = run(input);
    assert_eq!(swapped.status, Status::Complete);
    assert_eq!(swapped.added_tracks, plan.added_tracks);
}

#[test]
fn plane_with_track_and_via_keepout_can_use_its_plated_pads_on_another_layer() {
    let mut input = base();
    input.bounds.max = [100, 100];
    input.layers = vec!["F.Cu".into(), "B.Cu".into()];
    input.tracks.clear();
    input.pads = vec![pad(1, [20, 50]), pad(2, [80, 50])];
    for p in &mut input.pads {
        p.layers = input.layers.clone();
        p.drill = Some(DrillShape::Circle { diameter_nm: 2 });
        p.shape = Shape::Circle { radius_nm: 3 };
    }
    input.zones = vec![serde_json::from_value(serde_json::json!({
        "id":id(3),"net":1,"layers":["B.Cu"],"connection":"solid","filled":true,
        "outer":[[0,0],[100,0],[100,100],[0,100]],"min_thickness_nm":1,
        "layer_fills":[{"layer":"B.Cu","polygons":[
            {"outer":[[10,40],[30,40],[30,60],[10,60]]},
            {"outer":[[70,40],[90,40],[90,60],[70,60]]}
        ]}]
    }))
    .unwrap()];
    input.rule_areas = vec![serde_json::from_value(serde_json::json!({
        "id":id(4),"layers":["B.Cu"],"outer":[[1,1],[99,1],[99,99],[1,99]],
        "no_tracks":true,"no_vias":true
    }))
    .unwrap()];
    let plan = run(input.clone());
    assert_eq!(plan.status, Status::Complete, "{:?}", plan.reason);
    assert_eq!(plan.added_tracks.len(), 1);
    assert_eq!(plan.added_tracks[0].layer, "F.Cu");
    assert!(plan.added_vias.is_empty());
    let mut limited = input.clone();
    limited.limits.max_work = plan.work_used - 1;
    let out = run(limited);
    assert_eq!(out.status, Status::WorkExhausted);
    assert!(out.added_tracks.is_empty());
    input.max_expansions = plan.expanded_states - 1;
    let out = run(input);
    assert_eq!(out.status, Status::BudgetExhausted);
    assert!(out.added_tracks.is_empty());
}

#[test]
fn pad_without_layers_joins_copper_on_the_requested_face() {
    let mut i = base();
    i.layer = "B.Cu".into();
    i.layers = vec!["F.Cu".into(), "B.Cu".into()];
    i.pads = vec![pad(101, [20, 30]), pad(102, [60, 30])];
    let mut connection = track(103, [20, 30], [60, 30]);
    connection.layer = "B.Cu".into();
    i.tracks = vec![connection];
    let mut explicit = i.clone();
    for p in &mut explicit.pads {
        p.layers.push("B.Cu".into());
    }
    let native = run(explicit);
    let implicit = run(i.clone());
    assert_eq!(native.status, Status::Complete);
    assert_eq!(implicit.status, native.status);
    assert_eq!(implicit.expanded_states, 0);
    assert!(implicit.added_tracks.is_empty() && implicit.added_vias.is_empty());
    i.tracks[0].layer = "F.Cu".into();
    let other_face = run(i);
    assert!(
        other_face.status != Status::Complete
            || !other_face.added_tracks.is_empty()
            || !other_face.added_vias.is_empty()
    );
}
