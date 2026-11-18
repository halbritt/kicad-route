use kicad_route_engine::diff_pair::{
    apply_length_matching_in_tx, differential_microstrip_z0, differential_stripline_z0,
    discover_and_validate, microstrip_z0, prepare_and_route_pairs_in_tx,
    prepare_and_route_pairs_with_penalty_in_tx, unit_members, RouteUnitId,
};
use kicad_route_engine::model::*;
use kicad_route_engine::rules::{
    resolve_effective_rules, BoardDesignSettings, EffectiveRules, NetClassDef, TuningProfile,
    TuningProfileLayerEntry,
};
use kicad_route_engine::Engine;
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

fn uuid(n: u64) -> String {
    format!("20000000-0000-4000-8000-{n:012x}")
}

fn pad(id: u64, net: u32, at: Point, layers: &[&str]) -> Pad {
    Pad {
        id: format!("10000000-0000-4000-8000-{id:012x}"),
        net,
        at,
        shape: Shape::Circle { radius_nm: 150_000 },
        drill: None,
        layers: layers.iter().map(|s| (*s).to_owned()).collect(),
        local_clearance_nm: None,
        zone_connection: PadZoneConnection::Inherited,
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
        footprint_ref: Some(format!("J{}", (id % 10) + 1)),
        pad_number: Some(format!("{}", (id % 4) + 1)),
        fromto_id: None,
    }
}

fn base_diff_input(net_names: &[&str]) -> Input {
    let mut nets = vec![Net {
        id: 0,
        name: String::new(),
    }];
    let mut pads = Vec::new();
    let mut assignments = BTreeMap::new();
    for (idx, &name) in net_names.iter().enumerate() {
        let nid = (idx + 1) as u32;
        nets.push(Net {
            id: nid,
            name: name.to_owned(),
        });
        let y = 10_000_000 + (idx as i64) * 1_500_000;
        pads.push(pad(nid as u64 * 2, nid, [10_000_000, y], &["F.Cu"]));
        pads.push(pad(nid as u64 * 2 + 1, nid, [40_000_000, y], &["F.Cu"]));
        assignments.insert(name.to_owned(), vec!["DIFF".to_owned()]);
    }
    let netclasses = vec![
        NetClassDef {
            name: "Default".to_owned(),
            priority: 0,
            clearance_nm: 200_000,
            track_width_nm: 250_000,
            via_diameter_nm: 600_000,
            via_drill_nm: 300_000,
            microvia_diameter_nm: 200_000,
            microvia_drill_nm: 100_000,
            diff_pair_width_nm: 250_000,
            diff_pair_gap_nm: 350_000,
            diff_pair_via_gap_nm: 250_000,
            tuning_profile: None,
        },
        NetClassDef {
            name: "DIFF".to_owned(),
            priority: 1,
            clearance_nm: 200_000,
            track_width_nm: 250_000,
            via_diameter_nm: 600_000,
            via_drill_nm: 300_000,
            microvia_diameter_nm: 200_000,
            microvia_drill_nm: 100_000,
            diff_pair_width_nm: 250_000,
            diff_pair_gap_nm: 350_000,
            diff_pair_via_gap_nm: 250_000,
            tuning_profile: None,
        },
    ];
    Input {
        version: INPUT_VERSION,
        source_id: "a".repeat(64),
        layer: "F.Cu".to_owned(),
        layers: vec!["F.Cu".to_owned(), "B.Cu".to_owned()],
        bounds: Bounds {
            min: [0, 0],
            max: [60_000_000, 40_000_000],
        },
        rules: Rules {
            width_nm: 250_000,
            clearance_nm: 200_000,
            grid_nm: 250_000,
            edge_clearance_nm: 200_000,
            hole_clearance_nm: 200_000,
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
        },
        nets,
        pads,
        tracks: Vec::new(),
        vias: Vec::new(),
        holes: Vec::new(),
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
        zones: Vec::new(),
        rule_areas: Vec::new(),
        board_outline: None,
        board_design_settings: Some(BoardDesignSettings {
            min_track_width_nm: 100_000,
            min_clearance_nm: 100_000,
            min_edge_clearance_nm: 100_000,
            min_via_diameter_nm: 400_000,
            min_via_annular_width_nm: 50_000,
            min_through_hole_diameter_nm: 200_000,
            min_hole_clearance_nm: 150_000,
            min_hole_to_hole_nm: 150_000,
            min_microvia_diameter_nm: 200_000,
            min_microvia_drill_nm: 100_000,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
            default_clearance_nm: 200_000,
            default_track_width_nm: 250_000,
            default_via_diameter_nm: 600_000,
            default_via_drill_nm: 300_000,
            default_microvia_diameter_nm: 200_000,
            default_microvia_drill_nm: 100_000,
            min_resolved_spokes: None,
            max_error_nm: None,
            netclasses,
            netclass_assignments: assignments,
            use_height_for_length_calcs: Some(false),
        }),
        project_rules: None,
        custom_rules: None,
        effective_rules: None,
        max_expansions: 1_000_000,
        limits: Limits::default(),
        components: Vec::new(),
        stackup: Vec::new(),
        routing_options: Some(RoutingOptions {
            diff_pairs_mode: Some(DiffPairsMode::Auto),
            ..Default::default()
        }),
    }
}

#[test]
fn pair_discovery_supports_conventions_sheet_paths_and_refuses_invalid_preflight() {
    // 1. Bare P/N alone (VIN) stays ordinary; TXP+TXN, _P/_N, +/- with sheet prefix are discovered.
    let mut input = base_diff_input(&["VIN", "TXP", "TXN", "/CAN+", "/CAN-", "/S/D_P", "/S/D_N"]);
    let rules = resolve_effective_rules(&mut input).unwrap();
    let plan = discover_and_validate(&input, &rules).unwrap();
    assert_eq!(plan.pairs.len(), 3);
    assert_eq!(unit_members(&plan, RouteUnitId::DiffPair(0)), vec![4, 5]);
    assert_eq!(unit_members(&plan, RouteUnitId::DiffPair(1)), vec![6, 7]);
    assert_eq!(unit_members(&plan, RouteUnitId::DiffPair(2)), vec![2, 3]);

    // 2. Unpaired _P refuses with Invalid
    let mut unpaired = base_diff_input(&["USB_P", "CLK"]);
    let r2 = resolve_effective_rules(&mut unpaired).unwrap();
    assert_eq!(
        discover_and_validate(&unpaired, &r2).unwrap_err().status,
        Status::Invalid
    );

    // 3. --nets splitting a pair refuses with Invalid
    let mut split = base_diff_input(&["A_P", "A_N"]);
    split.routing_options.as_mut().unwrap().net_filters = vec!["A_P".to_owned()];
    let r3 = resolve_effective_rules(&mut split).unwrap();
    assert_eq!(
        discover_and_validate(&split, &r3).unwrap_err().status,
        Status::Invalid
    );

    // 4. <2 pads on either leg refuses with Invalid
    let mut onepad = base_diff_input(&["A_P", "A_N"]);
    onepad.pads.retain(|p| p.net != 1 || p.at[0] < 20_000_000);
    let r4 = resolve_effective_rules(&mut onepad).unwrap();
    assert_eq!(
        discover_and_validate(&onepad, &r4).unwrap_err().status,
        Status::Invalid
    );

    // 5. Mismatched netclasses refuse with Invalid
    let mut misclass = base_diff_input(&["A_P", "A_N"]);
    misclass
        .board_design_settings
        .as_mut()
        .unwrap()
        .netclass_assignments
        .insert("A_N".to_owned(), vec!["Default".to_owned()]);
    let r5 = resolve_effective_rules(&mut misclass).unwrap();
    assert_eq!(
        discover_and_validate(&misclass, &r5).unwrap_err().status,
        Status::Invalid
    );

    // 6. gap < clearance refuses with Invalid
    let mut tight_gap = base_diff_input(&["A_P", "A_N"]);
    tight_gap.rules.clearance_nm = 400_000;
    let r6 = resolve_effective_rules(&mut tight_gap).unwrap();
    assert_eq!(
        discover_and_validate(&tight_gap, &r6).unwrap_err().status,
        Status::Invalid
    );

    // 7. Duplicate or unknown --pair-layers, or --pair-layers without --diff-pairs, refuses Invalid
    let mut dup_layers = base_diff_input(&["A_P", "A_N"]);
    dup_layers.routing_options.as_mut().unwrap().pair_layers =
        vec!["F.Cu".to_owned(), "F.Cu".to_owned()];
    let r7 = resolve_effective_rules(&mut dup_layers).unwrap();
    assert_eq!(
        discover_and_validate(&dup_layers, &r7).unwrap_err().status,
        Status::Invalid
    );

    let mut no_dp = base_diff_input(&["A_P", "A_N"]);
    no_dp.routing_options.as_mut().unwrap().diff_pairs_mode = None;
    no_dp.routing_options.as_mut().unwrap().pair_layers = vec!["F.Cu".to_owned()];
    let r8 = resolve_effective_rules(&mut no_dp).unwrap();
    assert_eq!(
        discover_and_validate(&no_dp, &r8).unwrap_err().status,
        Status::Invalid
    );

    // 8. Conflicting group tolerances refuse Invalid
    let mut conf = base_diff_input(&["DQ0", "DQ1"]);
    conf.routing_options = Some(RoutingOptions {
        length_match_groups: vec![LengthMatchGroupSpec {
            members: vec!["DQ0".to_owned(), "DQ1".to_owned()],
            ..Default::default()
        }],
        group_match_tolerance_nm: Some(100_000),
        group_delay_tolerance_as: Some(1_000_000),
        ..Default::default()
    });
    let r9 = resolve_effective_rules(&mut conf).unwrap();
    assert_eq!(
        discover_and_validate(&conf, &r9).unwrap_err().status,
        Status::Invalid
    );
}

#[test]
fn impedance_equations_and_stripline_asymmetry_preflight_refusal() {
    // Check microstrip & stripline closed-form Zdiff against Python impedance.py values
    let z_ms = differential_microstrip_z0(0.325537, 0.35, 0.2, 0.035, 4.2);
    assert!((z_ms - 99.7409).abs() < 0.05, "got {z_ms}");
    let z_sl = differential_stripline_z0(0.315869, 0.35, 0.4866665, 0.035, 4.2);
    assert!((z_sl - 100.1537).abs() < 0.05, "got {z_sl}");
    let _ = microstrip_z0(0.325537, 0.2, 0.035, 4.2);

    // Asymmetric stripline (> 1nm) & missing epsilon_r refuse with Invalid
    let mut input = base_diff_input(&["CAN+", "CAN-", "GND"]);
    input.layers = vec![
        "F.Cu".to_owned(),
        "In1.Cu".to_owned(),
        "In2.Cu".to_owned(),
        "B.Cu".to_owned(),
    ];
    input.zones.push(Zone {
        id: uuid(100),
        name: Some("GND".to_owned()),
        net: 3,
        layers: vec!["F.Cu".to_owned(), "In2.Cu".to_owned()],
        outer: vec![
            [0, 0],
            [60_000_000, 0],
            [60_000_000, 40_000_000],
            [0, 40_000_000],
        ],
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        priority: 0,
        local_clearance_nm: 0,
        min_thickness_nm: 200_000,
        connection: ZoneConnection::Thermal,
        thermal_gap_nm: 200_000,
        thermal_spoke_width_nm: 200_000,
        island_removal: ZoneIslandRemoval::Always,
        island_area_min_nm2: 0,
        fill_mode: ZoneFillMode::Polygon,
        filled: true,
        refillable: false,
        locked: false,
        layer_fills: vec![
            ZoneLayerFill {
                layer: "F.Cu".to_owned(),
                polygons: vec![OutlinePolygon {
                    outer: vec![
                        [0, 0],
                        [60_000_000, 0],
                        [60_000_000, 40_000_000],
                        [0, 40_000_000],
                    ],
                    holes: Vec::new(),
                }],
            },
            ZoneLayerFill {
                layer: "In2.Cu".to_owned(),
                polygons: vec![OutlinePolygon {
                    outer: vec![
                        [0, 0],
                        [60_000_000, 0],
                        [60_000_000, 40_000_000],
                        [0, 40_000_000],
                    ],
                    holes: Vec::new(),
                }],
            },
        ],
    });
    input.stackup = vec![
        StackupLayer {
            name: "F.Cu".into(),
            kind: StackupKind::Copper,
            thickness_nm: 35_000,
            epsilon_r: None,
            material: None,
        },
        StackupLayer {
            name: "di1".into(),
            kind: StackupKind::Prepreg,
            thickness_nm: 520_000,
            epsilon_r: Some(4.2),
            material: None,
        },
        StackupLayer {
            name: "In1.Cu".into(),
            kind: StackupKind::Copper,
            thickness_nm: 35_000,
            epsilon_r: None,
            material: None,
        },
        StackupLayer {
            name: "di2".into(),
            kind: StackupKind::Core,
            thickness_nm: 486_666,
            epsilon_r: Some(4.2),
            material: None,
        },
        StackupLayer {
            name: "In2.Cu".into(),
            kind: StackupKind::Copper,
            thickness_nm: 35_000,
            epsilon_r: None,
            material: None,
        },
    ];
    input.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        pair_layers: vec!["In1.Cu".into()],
        pair_impedance_ohm: Some(100.0),
        impedance_tolerance_pct: Some(10.0),
        impedance_reference: Some("GND".into()),
        impedance_uncoupled_nm: Some(4_000_000),
        ..Default::default()
    });
    let r = resolve_effective_rules(&mut input).unwrap();
    let err = discover_and_validate(&input, &r).unwrap_err();
    assert_eq!(err.status, Status::Invalid);
    assert!(
        err.reason.contains("asymmetric stripline"),
        "got: {}",
        err.reason
    );

    // Fix thickness symmetry -> succeeds and solves ~315_869 nm
    input.stackup[1].thickness_nm = 486_667;
    let r_ok = resolve_effective_rules(&mut input).unwrap();
    let plan = discover_and_validate(&input, &r_ok).unwrap();
    let imp = plan.pairs[0].impedance.as_ref().unwrap();
    assert!(
        (imp.solved_width_nm - 315_869).abs() < 2_000,
        "got {}",
        imp.solved_width_nm
    );
}

#[test]
fn coupled_routing_invokes_penalty_hooks_and_rolls_back_atomically_on_keepout_block() {
    // 1. Clean route invokes step_penalty hook
    let calls = Arc::new(AtomicUsize::new(0));
    let calls_cl = Arc::clone(&calls);
    let step_pen = move |_li: u8, _pt: Point| -> u64 {
        calls_cl.fetch_add(1, Ordering::Relaxed);
        0
    };
    let via_pen = |_pt: Point, _a: u8, _b: u8| 0u64;
    // Block direct path with a small keepout obstacle so A* runs and exercises step_penalty
    let mut obs_input = base_diff_input(&["CAN+", "CAN-"]);
    obs_input.rule_areas.push(RuleArea {
        id: uuid(200),
        name: None,
        layers: vec!["F.Cu".into()],
        outer: vec![
            [24_000_000, 8_000_000],
            [26_000_000, 8_000_000],
            [26_000_000, 14_000_000],
            [24_000_000, 14_000_000],
        ],
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        no_tracks: true,
        no_vias: false,
        no_pads: false,
        no_footprints: false,
        no_zone_fills: false,
    });
    let mut obs_eng = Engine::new(obs_input).unwrap();
    let state = prepare_and_route_pairs_with_penalty_in_tx(&mut obs_eng, &step_pen, &via_pen)
        .expect("should detour around partial keepout");
    assert!(
        calls.load(Ordering::Relaxed) > 0,
        "step_penalty must be invoked"
    );
    assert_eq!(state.routed_pair_nets.len(), 2);
    assert!(!obs_eng.copper().generated().collect::<Vec<_>>().is_empty());

    // 2. Full impassable keepout wall across F.Cu & B.Cu -> Unroutable, 0 copper emitted
    let mut wall_input = base_diff_input(&["CAN+", "CAN-"]);
    wall_input.rule_areas.push(RuleArea {
        id: uuid(201),
        name: None,
        layers: vec!["F.Cu".into(), "B.Cu".into()],
        outer: vec![
            [24_000_000, 0],
            [26_000_000, 0],
            [26_000_000, 40_000_000],
            [24_000_000, 40_000_000],
        ],
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        no_tracks: true,
        no_vias: true,
        no_pads: false,
        no_footprints: false,
        no_zone_fills: false,
    });
    let mut wall_eng = Engine::new(wall_input).unwrap();
    let err = prepare_and_route_pairs_in_tx(&mut wall_eng).unwrap_err();
    assert_eq!(err.status, Status::Unroutable);
    assert_eq!(wall_eng.copper().generated().count(), 0);
    assert_eq!(wall_eng.copper().generated_vias().count(), 0);
}

#[test]
fn dead_end_stub_pruning_and_length_group_matching_with_protect() {
    // Two single-ended nets DQ0 (30mm) & DQ1 (40mm) + protected pair A_P/A_N
    let mut input = base_diff_input(&["A_P", "A_N"]);
    input.nets.push(Net {
        id: 3,
        name: "DQ0".into(),
    });
    input.nets.push(Net {
        id: 4,
        name: "DQ1".into(),
    });
    input
        .pads
        .push(pad(10, 3, [10_000_000, 20_000_000], &["F.Cu"]));
    input
        .pads
        .push(pad(11, 3, [40_000_000, 20_000_000], &["F.Cu"]));
    input
        .pads
        .push(pad(12, 4, [10_000_000, 25_000_000], &["F.Cu"]));
    input
        .pads
        .push(pad(13, 4, [50_000_000, 25_000_000], &["F.Cu"]));
    // Add existing tracks on A_P/A_N (protected!) + dead-end stub on DQ1 (must be pruned!)
    input.tracks.push(Track {
        id: uuid(300),
        net: 1,
        start: [10_000_000, 10_000_000],
        end: [40_000_000, 10_000_000],
        width_nm: 250_000,
        locked: false,
        layer: "F.Cu".into(),
    });
    input.tracks.push(Track {
        id: uuid(301),
        net: 2,
        start: [10_000_000, 11_500_000],
        end: [40_000_000, 11_500_000],
        width_nm: 250_000,
        locked: false,
        layer: "F.Cu".into(),
    });
    input.tracks.push(Track {
        id: uuid(302),
        net: 4,
        start: [10_000_000, 25_000_000],
        end: [50_000_000, 25_000_000],
        width_nm: 250_000,
        locked: false,
        layer: "F.Cu".into(),
    });
    // 10 mm dead-end stub branching off DQ1 midpoint: useful path MUST remain 40mm, NOT 50mm!
    input.tracks.push(Track {
        id: uuid(303),
        net: 4,
        start: [30_000_000, 25_000_000],
        end: [30_000_000, 35_000_000],
        width_nm: 250_000,
        locked: false,
        layer: "F.Cu".into(),
    });

    input.routing_options = Some(RoutingOptions {
        protect_mode: Some(ProtectMode::Existing),
        net_filters: vec!["DQ0".into(), "DQ1".into()],
        length_match_groups: vec![LengthMatchGroupSpec {
            members: vec!["DQ0".into(), "DQ1".into()],
            ..Default::default()
        }],
        group_match_tolerance_nm: Some(100_000),
        group_meander_amplitude_nm: Some(3_000_000),
        ..Default::default()
    });
    let mut eng = Engine::new(input).unwrap();
    let state = prepare_and_route_pairs_in_tx(&mut eng).unwrap();
    assert!(state.protected_nets.contains(&1));
    assert!(state.protected_nets.contains(&2));
    assert_eq!(
        state
            .active_single_ended_nets
            .as_ref()
            .unwrap()
            .iter()
            .copied()
            .collect::<Vec<_>>(),
        vec![3, 4]
    );
    eng.route(3, 50_000).unwrap();
    apply_length_matching_in_tx(&mut eng, &state).unwrap();

    let dq0_len: f64 = eng
        .copper()
        .tracks()
        .filter(|t| t.net == 3)
        .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
        .sum();
    // Should match DQ1 useful length (~40mm, minus up to pad-edge attachment 0.275mm), NOT 50mm!
    assert!(
        (39_600_000.0..=40_150_000.0).contains(&dq0_len),
        "expected ~40mm (stub pruned), got {dq0_len}"
    );
}

#[test]
fn delay_matching_preflight_refusals_and_tandem_inter_pair_matching() {
    // 1. Delay preflight refuses use_height_for_length_calcs == false
    let mut input = base_diff_input(&["DQ0", "DQ1"]);
    input.routing_options = Some(RoutingOptions {
        length_match_groups: vec![LengthMatchGroupSpec {
            members: vec!["DQ0".into(), "DQ1".into()],
            ..Default::default()
        }],
        group_delay_tolerance_as: Some(1_000_000),
        ..Default::default()
    });
    let r = resolve_effective_rules(&mut input).unwrap();
    assert_eq!(
        discover_and_validate(&input, &r).unwrap_err().status,
        Status::Invalid
    );

    // 2. Coupled inter-pair tandem matching (A: 30mm, B: 40mm, uncoupled max 2mm!)
    let mut ip = base_diff_input(&["A_P", "A_N", "B_P", "B_N"]);
    ip.pads.clear();
    // Pair A: 30mm span (x=10..40, y=10 & 10.6)
    ip.pads.push(pad(1, 1, [10_000_000, 10_000_000], &["F.Cu"]));
    ip.pads.push(pad(2, 1, [40_000_000, 10_000_000], &["F.Cu"]));
    ip.pads.push(pad(3, 2, [10_000_000, 10_600_000], &["F.Cu"]));
    ip.pads.push(pad(4, 2, [40_000_000, 10_600_000], &["F.Cu"]));
    // Pair B: 40mm span (x=10..50, y=25 & 25.6)
    ip.pads.push(pad(5, 3, [10_000_000, 25_000_000], &["F.Cu"]));
    ip.pads.push(pad(6, 3, [50_000_000, 25_000_000], &["F.Cu"]));
    ip.pads.push(pad(7, 4, [10_000_000, 25_600_000], &["F.Cu"]));
    ip.pads.push(pad(8, 4, [50_000_000, 25_600_000], &["F.Cu"]));
    ip.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        length_match_groups: vec![LengthMatchGroupSpec {
            members: vec!["A_P".into(), "A_N".into(), "B_P".into(), "B_N".into()],
            ..Default::default()
        }],
        group_match_tolerance_nm: Some(100_000),
        group_meander_amplitude_nm: Some(4_000_000),
        impedance_uncoupled_nm: None,
        ..Default::default()
    });
    let mut ip_eng = Engine::new(ip).unwrap();
    let state = prepare_and_route_pairs_in_tx(&mut ip_eng).unwrap();
    apply_length_matching_in_tx(&mut ip_eng, &state).unwrap();
    for nid in 1..=4 {
        let len: f64 = ip_eng
            .copper()
            .tracks()
            .filter(|t| t.net == nid)
            .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
            .sum();
        assert!(
            (len - 40_000_000.0).abs() <= 100_000.0,
            "net {nid} expected ~40mm, got {len}"
        );
    }

    // Silence unused TuningProfile import check when building tests
    let _ = std::marker::PhantomData::<(EffectiveRules, TuningProfile, TuningProfileLayerEntry)>;
}

#[test]
fn optional_external_fixture_roundtrip_hook() {
    let Ok(in_path) = std::env::var("AE_F9_FIXTURE_IN") else {
        return;
    };
    let out_path = std::env::var("AE_F9_FIXTURE_OUT").expect("AE_F9_FIXTURE_OUT required");
    let bytes = std::fs::read(&in_path).unwrap();
    let req_sha = kicad_route_engine::request_sha256(&bytes);
    let input: Input = serde_json::from_slice(&bytes).unwrap();
    let source_id = input.source_id.clone();
    let mut pad_counts: BTreeMap<u32, usize> = BTreeMap::new();
    for p in &input.pads {
        if p.net != 0 {
            *pad_counts.entry(p.net).or_insert(0) += 1;
        }
    }
    let all_nets: Vec<u32> = input
        .nets
        .iter()
        .filter(|n| n.id != 0)
        .map(|n| n.id)
        .collect();
    let mut eng = Engine::new(input).unwrap();
    let state = prepare_and_route_pairs_in_tx(&mut eng).unwrap();
    let mut single_nets: Vec<u32> = match &state.active_single_ended_nets {
        Some(s) => s.iter().copied().collect(),
        None => all_nets
            .into_iter()
            .filter(|nid| !state.routed_pair_nets.contains(nid))
            .collect(),
    };
    single_nets.sort_unstable();
    for nid in single_nets {
        if pad_counts.get(&nid).copied().unwrap_or(0) >= 2
            && !eng.copper().tracks().any(|t| t.net == nid)
            && !state.protected_nets.contains(&nid)
        {
            eng.route(nid, 200_000).unwrap();
        }
    }
    apply_length_matching_in_tx(&mut eng, &state).unwrap();
    let tracks: Vec<Track> = eng.copper().generated().cloned().collect();
    let vias: Vec<Via> = eng.copper().generated_vias().cloned().collect();
    let out = Output {
        version: RESULT_VERSION,
        request_sha256: req_sha,
        source_id,
        status: Status::Complete,
        added_tracks: tracks,
        added_vias: vias,
        removed_track_ids: Vec::new(),
        expanded_states: 0,
        work_used: eng.work_used(),
        reason: None,
    };
    std::fs::write(&out_path, serde_json::to_vec_pretty(&out).unwrap()).unwrap();
}
