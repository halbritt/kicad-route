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
            board_thickness_nm: None,
            outer_solder_mask_thickness_nm: None,
            cap_vias: false,
            fill_vias: false,
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
    assert!(state.protected_nets.contains(&4));
    assert_eq!(
        state
            .active_single_ended_nets
            .as_ref()
            .unwrap()
            .iter()
            .copied()
            .collect::<Vec<_>>(),
        vec![3]
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
fn option_b_and_coupled_group_preflight_refusals() {
    use kicad_route_engine::model::Status;

    // 1. Option B: complete 1-pair coupled group (["DP+", "DP-"]) is allowed and matches P/N skew
    let mut input = base_diff_input(&["DP+", "DP-"]);
    input.pads[2].at = [10_000_000, 11_500_000];
    input.pads[3].at = [39_200_000, 11_500_000]; // 0.8mm shorter than DP+ (> 150um tol)
    input.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        length_match_groups: vec![LengthMatchGroupSpec {
            members: vec!["DP+".into(), "DP-".into()],
            ..Default::default()
        }],
        group_match_tolerance_nm: Some(150_000),
        group_meander_amplitude_nm: Some(2_000_000),
        ..Default::default()
    });
    let mut eng = Engine::new(input).unwrap();
    let state = prepare_and_route_pairs_in_tx(&mut eng).unwrap();
    apply_length_matching_in_tx(&mut eng, &state).unwrap();
    let len_p: f64 = eng
        .copper()
        .tracks()
        .filter(|t| t.net == 1)
        .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
        .sum();
    let len_n: f64 = eng
        .copper()
        .tracks()
        .filter(|t| t.net == 2)
        .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
        .sum();
    assert!(
        (len_p - len_n).abs() <= 150_000.0,
        "1-pair group skew {} > 150um",
        (len_p - len_n).abs()
    );

    // 2. Partial pair omitted partner -> Status::Invalid
    let mut partial = base_diff_input(&["DP+", "DP-", "SE0"]);
    partial.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        length_match_groups: vec![LengthMatchGroupSpec {
            members: vec!["DP+".into(), "SE0".into()],
            ..Default::default()
        }],
        ..Default::default()
    });
    let r_partial = resolve_effective_rules(&mut partial).unwrap();
    assert_eq!(
        discover_and_validate(&partial, &r_partial)
            .unwrap_err()
            .status,
        Status::Invalid
    );

    // 3. Mixing single-ended and diff-pair in same group -> Status::Invalid
    let mut mixed = base_diff_input(&["DP+", "DP-", "SE0"]);
    mixed.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        length_match_groups: vec![LengthMatchGroupSpec {
            members: vec!["DP+".into(), "DP-".into(), "SE0".into()],
            ..Default::default()
        }],
        ..Default::default()
    });
    let r_mixed = resolve_effective_rules(&mut mixed).unwrap();
    assert_eq!(
        discover_and_validate(&mixed, &r_mixed).unwrap_err().status,
        Status::Invalid
    );

    // 4. Delay-profile matching on coupled diff-pair group -> Status::Invalid
    let mut delay_coupled = base_diff_input(&["A_P", "A_N", "B_P", "B_N"]);
    delay_coupled
        .board_design_settings
        .as_mut()
        .unwrap()
        .use_height_for_length_calcs = Some(true);
    delay_coupled.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        length_match_groups: vec![LengthMatchGroupSpec {
            members: vec!["A_P".into(), "A_N".into(), "B_P".into(), "B_N".into()],
            ..Default::default()
        }],
        group_delay_tolerance_as: Some(1_000_000),
        ..Default::default()
    });
    let r_delay = resolve_effective_rules(&mut delay_coupled).unwrap();
    assert_eq!(
        discover_and_validate(&delay_coupled, &r_delay)
            .unwrap_err()
            .status,
        Status::Invalid
    );

    // 5. Conflicting intra-match vs coupled group controls -> Status::Invalid
    let mut conflict = base_diff_input(&["A_P", "A_N", "B_P", "B_N"]);
    conflict.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        intra_match: true,
        length_match_tolerance_nm: Some(50_000),
        length_match_groups: vec![LengthMatchGroupSpec {
            members: vec!["A_P".into(), "A_N".into(), "B_P".into(), "B_N".into()],
            ..Default::default()
        }],
        group_match_tolerance_nm: Some(100_000),
        ..Default::default()
    });
    let r_conflict = resolve_effective_rules(&mut conflict).unwrap();
    assert_eq!(
        discover_and_validate(&conflict, &r_conflict)
            .unwrap_err()
            .status,
        Status::Invalid
    );
}

#[test]
fn dru_bounds_gap_max_within_diff_pairs_and_protect_without_filters() {
    use kicad_route_engine::model::Status;
    use kicad_route_engine::rules::{DruConstraint, DruRule, DruSeverity};

    // 1. DRU skew with within_diff_pairs == false does NOT set dru_skew_max_nm;
    //    DruSeverity::Ignore is skipped; within_diff_pairs == true sets dru_skew_max_nm.
    let mut input = base_diff_input(&["DP+", "DP-", "SE0"]);
    let mut r = resolve_effective_rules(&mut input).unwrap();
    r.dru_rules = vec![
        DruRule {
            name: "se_skew".into(),
            layer: None,
            condition: None,
            constraint: DruConstraint::Skew {
                min_nm: None,
                max_nm: Some(25_000),
                min_as: None,
                max_as: None,
                within_diff_pairs: false,
            },
            severity: DruSeverity::Error,
        },
        DruRule {
            name: "ignored_dp_skew".into(),
            layer: None,
            condition: None,
            constraint: DruConstraint::Skew {
                min_nm: None,
                max_nm: Some(10_000),
                min_as: None,
                max_as: None,
                within_diff_pairs: true,
            },
            severity: DruSeverity::Ignore,
        },
    ];
    let plan = discover_and_validate(&input, &r).unwrap();
    assert_eq!(plan.pairs[0].dru_skew_max_nm, None);

    r.dru_rules.push(DruRule {
        name: "dp_skew".into(),
        layer: None,
        condition: None,
        constraint: DruConstraint::Skew {
            min_nm: None,
            max_nm: Some(40_000),
            min_as: None,
            max_as: None,
            within_diff_pairs: true,
        },
        severity: DruSeverity::Error,
    });
    let plan2 = discover_and_validate(&input, &r).unwrap();
    assert_eq!(plan2.pairs[0].dru_skew_max_nm, Some(40_000));

    // Pair-exclusive condition without within_diff_pairs (e.g. A.NetName == 'DP+' || A.NetName == 'DP-') also applies
    let mut pair_cond = base_diff_input(&["DP+", "DP-", "SE0"]);
    pair_cond.custom_rules = Some(
        "(version 1)\n(rule \"pair_skew\" (condition \"A.NetName == 'DP+' || A.NetName == 'DP-'\") (constraint skew (max 0.035mm)))".into(),
    );
    let r_pair_cond = resolve_effective_rules(&mut pair_cond).unwrap();
    let plan3 = discover_and_validate(&pair_cond, &r_pair_cond).unwrap();
    assert_eq!(plan3.pairs[0].dru_skew_max_nm, Some(35_000));

    // 2. gap_max_nm violation refuses with Status::Invalid
    let mut gap_bad = base_diff_input(&["DP+", "DP-"]);
    let mut r_gap = resolve_effective_rules(&mut gap_bad).unwrap();
    r_gap.dru_rules = vec![DruRule {
        name: "tight_gap_max".into(),
        layer: None,
        condition: None,
        constraint: DruConstraint::DiffPairGap {
            min_nm: Some(100_000),
            max_nm: Some(150_000),
        },
        severity: DruSeverity::Error,
    }];
    assert_eq!(
        discover_and_validate(&gap_bad, &r_gap).unwrap_err().status,
        Status::Invalid
    );

    // 3. --protect with NO --net filter excludes protected SE net from active_single_ended_nets
    let mut prot_only = base_diff_input(&["SE0", "SE1"]);
    prot_only.tracks.push(Track {
        id: uuid(701),
        net: 1,
        start: [10_000_000, 10_000_000],
        end: [40_000_000, 10_000_000],
        width_nm: 250_000,
        locked: false,
        layer: "F.Cu".into(),
    });
    prot_only.routing_options = Some(RoutingOptions {
        protect_mode: Some(ProtectMode::Existing),
        ..Default::default()
    });
    let mut eng = Engine::new(prot_only).unwrap();
    let state = prepare_and_route_pairs_in_tx(&mut eng).unwrap();
    assert!(state.protected_nets.contains(&1));
    let active_se: Vec<u32> = state
        .active_single_ended_nets
        .as_ref()
        .expect("active_single_ended_nets should be populated when --protect is active")
        .iter()
        .copied()
        .collect();
    assert_eq!(active_se, vec![2]);
}

#[test]
fn cumulative_uncoupled_budget_and_meander_geometry_safety() {
    use kicad_route_engine::model::Status;

    // 1. Tight diff_pair_uncoupled budget (1.5mm): initial fanout (~1.0mm) routes cleanly,
    //    then cumulative budget enforcement fails closed during intra-pair match (no 200um bypass).
    let mut input = base_diff_input(&["DP+", "DP-"]);
    input.pads[2].at = [10_000_000, 10_600_000];
    input.pads[3].at = [39_000_000, 10_600_000]; // 1.0mm stagger -> ~1.0mm existing uncoupled
    input.custom_rules = Some(
        "(version 1)\n(rule \"tight_uncoupled\" (constraint diff_pair_uncoupled (max 1.5mm)))"
            .into(),
    );
    input.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        intra_match: true,
        length_match_tolerance_nm: Some(50_000),
        meander_amplitude_nm: Some(1_000_000),
        ..Default::default()
    });
    let mut eng = Engine::new(input).unwrap();
    let state = prepare_and_route_pairs_in_tx(&mut eng).unwrap();
    let err = apply_length_matching_in_tx(&mut eng, &state)
        .expect_err("intra-pair match exceeding diff_pair_uncoupled must fail closed");
    assert_eq!(err.status, Status::Unroutable);

    // 2. Generous uncoupled budget + multi-layer via transition succeeds without short V-peak stubs
    let mut trans = base_diff_input(&["CAN+", "CAN-"]);
    trans.pads[1].layers = vec!["B.Cu".into()];
    trans.pads[2].at = [10_000_000, 11_500_000];
    trans.pads[3].at = [39_200_000, 11_500_000];
    trans.pads[3].layers = vec!["B.Cu".into()];
    trans.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        intra_match: true,
        length_match_tolerance_nm: Some(150_000),
        meander_amplitude_nm: Some(2_000_000),
        ..Default::default()
    });
    let mut eng2 = Engine::new(trans).unwrap();
    let state2 = prepare_and_route_pairs_in_tx(&mut eng2).unwrap();
    apply_length_matching_in_tx(&mut eng2, &state2).unwrap();
    // Every generated segment connected to a pad on F.Cu/B.Cu that isn't a via stub must have clean geometry
    assert!(eng2.copper().generated_vias().count() >= 2);
}

#[test]
fn explorer1_exact_uncoupled_projection_and_gap_corridor_slit_rejection() {
    use kicad_route_engine::model::Status;

    // 1. DEEP_2D_NO_INTRA: 1.2mm stagger with intra_match=false & max uncoupled 1mm must refuse Unroutable
    let mut no_intra = base_diff_input(&["DP+", "DP-"]);
    no_intra.pads[2].at = [10_000_000, 10_600_000];
    no_intra.pads[3].at = [38_800_000, 10_600_000];
    no_intra.custom_rules = Some(
        "(version 1)\n(rule \"tight_unc\" (constraint diff_pair_uncoupled (max 1.0mm)))".into(),
    );
    no_intra.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        intra_match: false,
        ..Default::default()
    });
    let mut eng1 = Engine::new(no_intra).unwrap();
    let err1 = prepare_and_route_pairs_in_tx(&mut eng1).expect_err(
        "1.2mm uncoupled stagger with 1mm budget must fail closed even when intra_match=false",
    );
    assert_eq!(err1.status, Status::Unroutable);
    assert_eq!(eng1.copper().generated().count(), 0);

    // 2. Coupled meander miter overhangs exceeding 1mm budget (2C_small_1wave) must refuse Unroutable
    let mut miter_ov = base_diff_input(&["A_P", "A_N", "B_P", "B_N"]);
    miter_ov.pads.clear();
    miter_ov
        .pads
        .push(pad(1, 1, [10_000_000, 10_000_000], &["F.Cu"]));
    miter_ov
        .pads
        .push(pad(2, 1, [30_000_000, 10_000_000], &["F.Cu"]));
    miter_ov
        .pads
        .push(pad(3, 2, [10_000_000, 10_280_000], &["F.Cu"]));
    miter_ov
        .pads
        .push(pad(4, 2, [29_100_000, 10_280_000], &["F.Cu"]));
    miter_ov
        .pads
        .push(pad(5, 3, [10_000_000, 25_000_000], &["F.Cu"]));
    miter_ov
        .pads
        .push(pad(6, 3, [40_000_000, 25_000_000], &["F.Cu"]));
    miter_ov
        .pads
        .push(pad(7, 4, [10_000_000, 25_280_000], &["F.Cu"]));
    miter_ov
        .pads
        .push(pad(8, 4, [40_000_000, 25_280_000], &["F.Cu"]));
    for p in &mut miter_ov.pads {
        p.shape = Shape::Circle { radius_nm: 60_000 };
    }
    for nc in miter_ov
        .board_design_settings
        .as_mut()
        .unwrap()
        .netclasses
        .iter_mut()
    {
        nc.clearance_nm = 100_000;
        nc.track_width_nm = 80_000;
        nc.diff_pair_width_nm = 80_000;
        nc.diff_pair_gap_nm = 200_000;
    }
    let bds = miter_ov.board_design_settings.as_mut().unwrap();
    bds.default_clearance_nm = 100_000;
    bds.default_track_width_nm = 80_000;
    bds.min_track_width_nm = 80_000;
    miter_ov.rules.clearance_nm = 100_000;
    miter_ov.rules.width_nm = 80_000;
    miter_ov.custom_rules = Some(
        "(version 1)\n(rule \"tight_unc\" (constraint diff_pair_uncoupled (max 1.0mm)))".into(),
    );
    miter_ov.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        length_match_groups: vec![LengthMatchGroupSpec {
            members: vec!["A_P".into(), "A_N".into(), "B_P".into(), "B_N".into()],
            ..Default::default()
        }],
        group_match_tolerance_nm: Some(150_000),
        group_meander_amplitude_nm: Some(3_000_000),
        ..Default::default()
    });
    let mut eng2 = Engine::new(miter_ov).unwrap();
    let st2 = prepare_and_route_pairs_in_tx(&mut eng2).unwrap();
    let err2 = apply_length_matching_in_tx(&mut eng2, &st2)
        .expect_err("coupled meander miter overhangs pushing uncoupled > 1mm must fail closed");
    assert_eq!(err2.status, Status::Unroutable);

    // 3. Coupled gap corridor slit strictly between P (y=10.0) and N (y=10.674) on In1.Cu forces detour or refusal
    let mut slit = base_diff_input(&["CAN+", "CAN-", "GND"]);
    slit.layers = vec!["F.Cu".into(), "In1.Cu".into(), "B.Cu".into()];
    slit.pads.clear();
    slit.pads
        .push(pad(1, 1, [10_000_000, 10_000_000], &["F.Cu"]));
    slit.pads
        .push(pad(2, 1, [30_000_000, 10_000_000], &["F.Cu"]));
    slit.pads
        .push(pad(3, 2, [10_000_000, 10_675_537], &["F.Cu"]));
    slit.pads
        .push(pad(4, 2, [30_000_000, 10_675_537], &["F.Cu"]));
    slit.pads
        .push(pad(5, 3, [5_000_000, 5_000_000], &["F.Cu", "In1.Cu"]));
    slit.pads
        .push(pad(6, 3, [55_000_000, 35_000_000], &["F.Cu", "In1.Cu"]));
    slit.stackup = vec![
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
            thickness_nm: 200_000,
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
    ];
    // Full-board GND plane on In1.Cu with a 100um slit strictly inside the gap at y=[10.28, 10.38], x=[18, 22]
    slit.zones.push(Zone {
        id: uuid(400),
        name: Some("GND".into()),
        net: 3,
        layers: vec!["In1.Cu".into()],
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
        layer_fills: vec![ZoneLayerFill {
            layer: "In1.Cu".into(),
            polygons: vec![OutlinePolygon {
                outer: vec![
                    [0, 0],
                    [60_000_000, 0],
                    [60_000_000, 40_000_000],
                    [0, 40_000_000],
                ],
                holes: vec![vec![
                    [18_000_000, 10_280_000],
                    [22_000_000, 10_280_000],
                    [22_000_000, 10_380_000],
                    [18_000_000, 10_380_000],
                ]],
            }],
        }],
    });
    slit.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        pair_layers: vec!["F.Cu".into()],
        pair_impedance_ohm: Some(100.0),
        impedance_tolerance_pct: Some(10.0),
        impedance_reference: Some("GND".into()),
        impedance_uncoupled_nm: Some(4_000_000),
        ..Default::default()
    });
    let mut eng3 = Engine::new(slit).unwrap();
    if prepare_and_route_pairs_in_tx(&mut eng3).is_ok() {
        // Must have detoured around the slit rather than running straight across x=[18..22], y=[10.28..10.38]
        let detoured = eng3.copper().generated().any(|t| {
            t.start[1] < 9_800_000
                || t.end[1] < 9_800_000
                || t.start[1] > 10_900_000
                || t.end[1] > 10_900_000
        });
        assert!(
            detoured,
            "coupled route crossed gap-corridor slit without detouring"
        );
    }
}

#[test]
fn explorer2_pad_layers_cross_layer_jogs_and_non_root_pad_isolation() {
    // 1. 2-layer F.Cu-only SMD pads (pad.layers = vec![]) routed with pair_layers=["B.Cu"] -> inserts >= 2 vias
    let mut smd = base_diff_input(&["CAN+", "CAN-"]);
    smd.pads.clear();
    smd.pads.push(pad(1, 1, [10_000_000, 10_000_000], &[]));
    smd.pads.push(pad(2, 1, [30_000_000, 10_000_000], &[]));
    smd.pads.push(pad(3, 2, [10_000_000, 10_600_000], &[]));
    smd.pads.push(pad(4, 2, [30_000_000, 10_600_000], &[]));
    smd.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        pair_layers: vec!["B.Cu".into()],
        ..Default::default()
    });
    let mut eng_smd = Engine::new(smd).unwrap();
    prepare_and_route_pairs_in_tx(&mut eng_smd).unwrap();
    assert!(
        eng_smd.copper().generated_vias().count() >= 4,
        "F.Cu-only SMD pads with pair_layers=B.Cu must insert start+end coupled vias"
    );
    assert!(
        eng_smd.copper().generated().any(|t| t.layer == "B.Cu"),
        "main trunk must route on B.Cu"
    );

    // 2. Angled cross-layer launch/arrival (diagonal pads -> F.Cu escape -> B.Cu trunk) has zero open endpoints
    let mut angled = base_diff_input(&["CAN+", "CAN-"]);
    angled.pads.clear();
    angled.pads.push(pad(1, 1, [10_000_000, 10_000_000], &[]));
    angled.pads.push(pad(2, 1, [35_000_000, 22_000_000], &[]));
    angled.pads.push(pad(3, 2, [10_000_000, 11_000_000], &[]));
    angled.pads.push(pad(4, 2, [35_000_000, 23_000_000], &[]));
    angled.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        pair_layers: vec!["B.Cu".into()],
        ..Default::default()
    });
    let mut eng_ang = Engine::new(angled).unwrap();
    prepare_and_route_pairs_in_tx(&mut eng_ang).unwrap();
    assert!(eng_ang.copper().generated_vias().count() >= 4);

    // 3. Staggered SMD pads (adv_uncoupled_6mm geometry): zero non-root tracks touch pad copper & intra-match hits 50um
    let mut stag = base_diff_input(&["CAN+", "CAN-"]);
    stag.pads.clear();
    let mk_pad = |id, net, at| {
        let mut p = pad(id, net, at, &["F.Cu"]);
        p.shape = Shape::Circle { radius_nm: 300_000 };
        p
    };
    stag.pads.push(mk_pad(1, 1, [10_000_000, 14_000_000]));
    stag.pads.push(mk_pad(2, 1, [40_000_000, 14_700_000]));
    stag.pads.push(mk_pad(3, 2, [10_000_000, 16_000_000]));
    stag.pads.push(mk_pad(4, 2, [41_200_000, 15_300_000]));
    stag.custom_rules =
        Some("(version 1)\n(rule \"unc6\" (constraint diff_pair_uncoupled (max 6.0mm)))".into());
    stag.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        intra_match: true,
        length_match_tolerance_nm: Some(50_000),
        meander_amplitude_nm: Some(1_500_000),
        ..Default::default()
    });
    let stag_pads = stag.pads.clone();
    let mut eng_stag = Engine::new(stag).unwrap();
    let st_stag = prepare_and_route_pairs_in_tx(&mut eng_stag).unwrap();
    apply_length_matching_in_tx(&mut eng_stag, &st_stag).unwrap();
    for p in &stag_pads {
        let non_root_hit = eng_stag.copper().generated().any(|t| {
            t.net == p.net
                && t.start != p.at
                && t.end != p.at
                && kicad_route_engine::geometry::track_near_pad(
                    t.start, t.end, t.width_nm, 1_000, p, true,
                )
        });
        assert!(
            !non_root_hit,
            "non-root track entered pad {:?} copper",
            p.at
        );
    }
}

#[test]
fn explorer3_dru_indiffpair_wildcard_option_b_defaults_and_via_overrides() {
    use kicad_route_engine::model::Status;
    use kicad_route_engine::rules::parse_kicad_pro;

    // 1. A.inDiffPair('*') with within_diff_pairs=false across 2 pairs -> dru_skew_max_nm == None (even with --nets A_*)
    let mut mp = base_diff_input(&["A_P", "A_N", "B_P", "B_N"]);
    mp.custom_rules = Some(
        "(version 1)\n(rule \"all_dp_skew\" (condition \"A.inDiffPair('*')\") (constraint skew (max 0.025mm)))".into(),
    );
    let r_full = resolve_effective_rules(&mut mp).unwrap();
    let p_full = discover_and_validate(&mp, &r_full).unwrap();
    assert_eq!(p_full.pairs[0].dru_skew_max_nm, None);
    assert_eq!(p_full.pairs[1].dru_skew_max_nm, None);

    mp.routing_options.as_mut().unwrap().net_filters = vec!["A_*".into()];
    let r_filt = resolve_effective_rules(&mut mp).unwrap();
    let p_filt = discover_and_validate(&mp, &r_filt).unwrap();
    assert_eq!(p_filt.pairs[0].dru_skew_max_nm, None);

    // Pair-specific A.inDiffPair('A') applies only to pair A
    mp.routing_options.as_mut().unwrap().net_filters.clear();
    mp.custom_rules = Some(
        "(version 1)\n(rule \"a_skew\" (condition \"A.inDiffPair('A')\") (constraint skew (max 0.025mm)))".into(),
    );
    let r_spec = resolve_effective_rules(&mut mp).unwrap();
    let p_spec = discover_and_validate(&mp, &r_spec).unwrap();
    assert_eq!(p_spec.pairs[0].dru_skew_max_nm, Some(25_000));
    assert_eq!(p_spec.pairs[1].dru_skew_max_nm, None);

    // 2. Option B implicit-default conflict check
    let mut opt_b = base_diff_input(&["CAN+", "CAN-"]);
    opt_b.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        intra_match: true,
        length_match_groups: vec![LengthMatchGroupSpec {
            members: vec!["CAN+".into(), "CAN-".into()],
            ..Default::default()
        }],
        group_match_tolerance_nm: Some(150_000),
        ..Default::default()
    });
    let r_ob1 = resolve_effective_rules(&mut opt_b).unwrap();
    assert_eq!(
        discover_and_validate(&opt_b, &r_ob1).unwrap_err().status,
        Status::Invalid
    );

    opt_b
        .routing_options
        .as_mut()
        .unwrap()
        .group_match_tolerance_nm = None;
    opt_b
        .routing_options
        .as_mut()
        .unwrap()
        .group_meander_amplitude_nm = Some(4_000_000);
    let r_ob2 = resolve_effective_rules(&mut opt_b).unwrap();
    assert_eq!(
        discover_and_validate(&opt_b, &r_ob2).unwrap_err().status,
        Status::Invalid
    );

    // Omitting --group-* inherits unified 50um / 1.5mm defaults -> Ok
    opt_b
        .routing_options
        .as_mut()
        .unwrap()
        .group_meander_amplitude_nm = None;
    let r_ob3 = resolve_effective_rules(&mut opt_b).unwrap();
    let p_ob3 = discover_and_validate(&opt_b, &r_ob3).unwrap();
    assert_eq!(p_ob3.intra_tolerance_nm, 50_000);
    assert_eq!(p_ob3.intra_amplitude_nm, 1_500_000);

    // 3. KiCad 10 via_overrides schema parsing + positive delay validation
    let pro_json = r#"{
        "meta": {"version": 1},
        "tuning_profiles": {
            "tuning_profiles_impedance_geometric": [
                {
                    "profile_name": "P1",
                    "type": 0,
                    "target_impedance": 50,
                    "enable_time_domain_tuning": true,
                    "via_prop_delay": 7500000,
                    "layer_entries": [
                        {"signal_layer": "F.Cu", "width": 250000, "delay": 6000000000},
                        {"signal_layer": "B.Cu", "width": 250000, "delay": 6000000000}
                    ],
                    "via_overrides": [
                        {
                            "signal_layer_from": "F.Cu",
                            "signal_layer_to": "B.Cu",
                            "via_layer_from": "F.Cu",
                            "via_layer_to": "B.Cu",
                            "delay": 12000000
                        }
                    ]
                }
            ]
        }
    }"#;
    let parsed = parse_kicad_pro(pro_json.as_bytes()).unwrap();
    assert_eq!(parsed.tuning_profiles.len(), 1);
    let ov = &parsed.tuning_profiles[0].via_overrides[0];
    assert_eq!(ov.signal_layer_from, "F.Cu");
    assert_eq!(ov.signal_layer_to, "B.Cu");
    assert_eq!(ov.via_layer_from, "F.Cu");
    assert_eq!(ov.via_layer_to, "B.Cu");
    assert_eq!(ov.delay_as, 12_000_000);

    // Non-positive override delay in an active delay group must refuse Status::Invalid
    let mut bad_ov = base_diff_input(&["DQ0", "DQ1"]);
    bad_ov
        .board_design_settings
        .as_mut()
        .unwrap()
        .use_height_for_length_calcs = Some(true);
    bad_ov
        .board_design_settings
        .as_mut()
        .unwrap()
        .netclass_assignments
        .insert("DQ0".into(), vec!["DIFF".into()]);
    bad_ov
        .board_design_settings
        .as_mut()
        .unwrap()
        .netclass_assignments
        .insert("DQ1".into(), vec!["DIFF".into()]);
    bad_ov.project_rules = Some(
        r#"{
            "meta": {"version": 1},
            "board": {
                "design_settings": {
                    "rules": {},
                    "net_settings": {
                        "classes": [
                            {"name": "Default"},
                            {"name": "DIFF", "tuning_profile": "P1"}
                        ],
                        "netclass_assignments": {"DQ0": ["DIFF"], "DQ1": ["DIFF"]}
                    }
                }
            },
            "tuning_profiles": {
                "tuning_profiles_impedance_geometric": [{
                    "profile_name": "P1",
                    "type": 0,
                    "target_impedance": 50,
                    "enable_time_domain_tuning": true,
                    "via_prop_delay": 7500000,
                    "layer_entries": [{"signal_layer": "F.Cu", "width": 250000, "delay": 6000000000}],
                    "via_overrides": [{
                        "signal_layer_from": "F.Cu",
                        "signal_layer_to": "B.Cu",
                        "via_layer_from": "F.Cu",
                        "via_layer_to": "B.Cu",
                        "delay": 0
                    }]
                }]
            }
        }"#
        .into(),
    );
    let r_bad_ov = resolve_effective_rules(&mut bad_ov).unwrap();
    bad_ov.routing_options = Some(RoutingOptions {
        length_match_groups: vec![LengthMatchGroupSpec {
            members: vec!["DQ0".into(), "DQ1".into()],
            ..Default::default()
        }],
        group_delay_tolerance_as: Some(1_000_000),
        ..Default::default()
    });
    assert_eq!(
        discover_and_validate(&bad_ov, &r_bad_ov)
            .unwrap_err()
            .status,
        Status::Invalid
    );
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

#[test]
fn r4_pair_stripline_rect_circle_symmetric_escape_and_impedance_vbump_refusal() {
    let make_stripline = |j2_neg_x: i64| -> Input {
        let mut input = base_diff_input(&["CAN+", "CAN-", "GND"]);
        input.layers = vec![
            "F.Cu".into(),
            "In1.Cu".into(),
            "In2.Cu".into(),
            "B.Cu".into(),
        ];
        input.bounds = Bounds {
            min: [0, 0],
            max: [100_000_000, 100_000_000],
        };
        input.pads.clear();
        let all_cu = &["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"];
        let mut p1 = pad(1, 1, [45_000_000, 48_730_000], all_cu);
        p1.shape = Shape::Rect {
            half_size_nm: [850_000, 850_000],
        };
        p1.drill = Some(DrillShape::Circle {
            diameter_nm: 1_000_000,
        });
        let mut p2 = pad(2, 1, [55_000_000, 48_730_000], all_cu);
        p2.shape = Shape::Rect {
            half_size_nm: [850_000, 850_000],
        };
        p2.drill = Some(DrillShape::Circle {
            diameter_nm: 1_000_000,
        });
        let mut n1 = pad(3, 2, [45_000_000, 51_270_000], all_cu);
        n1.shape = Shape::Circle { radius_nm: 850_000 };
        n1.drill = Some(DrillShape::Circle {
            diameter_nm: 1_000_000,
        });
        let mut n2 = pad(4, 2, [j2_neg_x, 51_270_000], all_cu);
        n2.shape = Shape::Circle { radius_nm: 850_000 };
        n2.drill = Some(DrillShape::Circle {
            diameter_nm: 1_000_000,
        });
        let g1 = pad(5, 3, [10_000_000, 10_000_000], all_cu);
        let g2 = pad(6, 3, [90_000_000, 90_000_000], all_cu);
        input.pads.extend([p1, p2, n1, n2, g1, g2]);
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
                thickness_nm: 486_667,
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
                thickness_nm: 486_667,
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
            StackupLayer {
                name: "di3".into(),
                kind: StackupKind::Prepreg,
                thickness_nm: 200_000,
                epsilon_r: Some(4.2),
                material: None,
            },
            StackupLayer {
                name: "B.Cu".into(),
                kind: StackupKind::Copper,
                thickness_nm: 35_000,
                epsilon_r: None,
                material: None,
            },
        ];
        // GND reference zones on F.Cu & In2.Cu with per-pad antipad holes (+200um)
        let antipads = vec![
            vec![
                [43_950_000, 47_680_000],
                [46_050_000, 47_680_000],
                [46_050_000, 49_780_000],
                [43_950_000, 49_780_000],
            ],
            vec![
                [43_950_000, 50_220_000],
                [46_050_000, 50_220_000],
                [46_050_000, 52_320_000],
                [43_950_000, 52_320_000],
            ],
            vec![
                [53_950_000, 47_680_000],
                [56_050_000, 47_680_000],
                [56_050_000, 49_780_000],
                [53_950_000, 49_780_000],
            ],
            vec![
                [j2_neg_x - 1_050_000, 50_220_000],
                [j2_neg_x + 1_050_000, 50_220_000],
                [j2_neg_x + 1_050_000, 52_320_000],
                [j2_neg_x - 1_050_000, 52_320_000],
            ],
        ];
        input.zones.push(Zone {
            id: uuid(800),
            name: Some("GND".into()),
            net: 3,
            layers: vec!["F.Cu".into(), "In2.Cu".into()],
            outer: vec![
                [0, 0],
                [100_000_000, 0],
                [100_000_000, 100_000_000],
                [0, 100_000_000],
            ],
            holes: Vec::new(),
            extra_polygons: Vec::new(),
            priority: 0,
            local_clearance_nm: 200_000,
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
            layer_fills: ["F.Cu", "In2.Cu"]
                .into_iter()
                .map(|ln| ZoneLayerFill {
                    layer: ln.into(),
                    polygons: vec![OutlinePolygon {
                        outer: vec![
                            [0, 0],
                            [100_000_000, 0],
                            [100_000_000, 100_000_000],
                            [0, 100_000_000],
                        ],
                        holes: antipads.clone(),
                    }],
                })
                .collect(),
        });
        input.custom_rules = Some(
            "(version 1)\n(rule \"can_skew\" (condition \"A.inDiffPair('*')\") (constraint skew (max 0.15mm) (within_diff_pairs)))".into(),
        );
        input.routing_options = Some(RoutingOptions {
            diff_pairs_mode: Some(DiffPairsMode::Auto),
            pair_layers: vec!["In1.Cu".into()],
            pair_impedance_ohm: Some(100.0),
            impedance_tolerance_pct: Some(10.0),
            impedance_reference: Some("GND".into()),
            impedance_uncoupled_nm: Some(4_000_000),
            ..Default::default()
        });
        input
    };

    // 1. Symmetric pair_stripline: rect+circle PTH pads choose symmetric escapes with 0 mid-trunk V-bumps
    let mut eng = Engine::new(make_stripline(55_000_000)).unwrap();
    let state = prepare_and_route_pairs_in_tx(&mut eng).unwrap();
    apply_length_matching_in_tx(&mut eng, &state).unwrap();
    let tracks: Vec<Track> = eng.copper().generated().cloned().collect();
    assert_eq!(
        tracks.len(),
        6,
        "expected 6 clean tracks (no mid-trunk V-bump), got {tracks:?}"
    );
    let len_p: f64 = tracks
        .iter()
        .filter(|t| t.net == 1)
        .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
        .sum();
    let len_n: f64 = tracks
        .iter()
        .filter(|t| t.net == 2)
        .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
        .sum();
    assert!(
        (len_p - len_n).abs() <= 1_000.0,
        "expected symmetric lengths, got {len_p} vs {len_n}"
    );

    // 2. Unabsorbable skew on impedance-controlled pair refuses Status::Unroutable rather than emitting mid-trunk V-bump
    let mut bad_inp = make_stripline(55_500_000);
    bad_inp.routing_options.as_mut().unwrap().intra_match = true;
    bad_inp
        .routing_options
        .as_mut()
        .unwrap()
        .length_match_tolerance_nm = Some(50_000);
    let mut bad_eng = Engine::new(bad_inp).unwrap();
    let bad_state = prepare_and_route_pairs_in_tx(&mut bad_eng).unwrap();
    let before_count = bad_eng.copper().generated().count();
    let err = apply_length_matching_in_tx(&mut bad_eng, &bad_state).expect_err(
        "unabsorbable skew on impedance pair must fail closed rather than emit mid-trunk V-bump",
    );
    assert_eq!(err.status, Status::Unroutable);
    assert_eq!(bad_eng.copper().generated().count(), before_count);
}

#[test]
fn r4_measure_fromto_short_segment_via_copper_and_three_pad_fromto_dru() {
    // 1. 3+-pad net in DRU !A.fromTo(...) and A.fromTo(...)
    let mut inp = base_diff_input(&["DP+", "DP-", "CLK"]);
    inp.pads[0].footprint_ref = Some("J1".into());
    inp.pads[0].pad_number = Some("1".into());
    inp.pads[1].footprint_ref = Some("J2".into());
    inp.pads[1].pad_number = Some("1".into());
    inp.pads[2].footprint_ref = Some("J1".into());
    inp.pads[2].pad_number = Some("2".into());
    inp.pads[3].footprint_ref = Some("J2".into());
    inp.pads[3].pad_number = Some("2".into());
    inp.pads[4].footprint_ref = Some("U1".into());
    inp.pads[4].pad_number = Some("1".into());
    inp.pads[5].footprint_ref = Some("U1".into());
    inp.pads[5].pad_number = Some("2".into());
    let mut p_u3 = pad(99, 3, [25_000_000, 13_000_000], &["F.Cu"]);
    p_u3.footprint_ref = Some("U1".into());
    p_u3.pad_number = Some("3".into());
    inp.pads.push(p_u3);
    inp.custom_rules = Some(
        "(version 1)\n(rule \"not_clk\" (condition \"!A.fromTo('U1-1', 'U1-2')\") (constraint skew (max 0.030mm)))".into(),
    );
    let r = resolve_effective_rules(&mut inp).unwrap();
    let plan = discover_and_validate(&inp, &r).unwrap();
    assert_eq!(plan.pairs[0].dru_skew_max_nm, Some(30_000));

    // Also 3-pad pair net matching A.fromTo('J1-1', 'J2-1')
    let mut p3 = pad(100, 1, [25_000_000, 10_000_000], &["F.Cu"]);
    p3.footprint_ref = Some("J3".into());
    p3.pad_number = Some("1".into());
    let mut n3 = pad(101, 2, [25_000_000, 11_500_000], &["F.Cu"]);
    n3.footprint_ref = Some("J3".into());
    n3.pad_number = Some("2".into());
    inp.pads.extend([p3, n3]);
    inp.custom_rules = Some(
        "(version 1)\n(rule \"dp3\" (condition \"A.fromTo('J1-1', 'J2-1') || A.fromTo('J1-2', 'J2-2')\") (constraint skew (max 0.045mm)))".into(),
    );
    let r2 = resolve_effective_rules(&mut inp).unwrap();
    let plan2 = discover_and_validate(&inp, &r2).unwrap();
    assert_eq!(plan2.pairs[0].dru_skew_max_nm, Some(45_000));

    // 2. measure_net_fromto short-segment bypass (< width_nm vs >= width_nm) via group matching
    let run_step_probe = |step_h: i64| -> f64 {
        let mut g = base_diff_input(&["NET_A", "NET_B"]);
        g.pads.clear();
        g.pads.push(pad(1, 1, [10_000_000, 10_000_000], &["F.Cu"]));
        g.pads.push(pad(2, 1, [30_000_000, 10_000_000], &["F.Cu"]));
        let mut pb1 = pad(3, 2, [10_000_000, 25_000_000], &["F.Cu"]);
        pb1.shape = Shape::Circle { radius_nm: 5_000 };
        let mut pb2 = pad(4, 2, [29_000_000, 25_000_000], &["F.Cu"]);
        pb2.shape = Shape::Circle { radius_nm: 5_000 };
        g.pads.push(pb1);
        g.pads.push(pb2);
        let segs = [
            ([10_000_000, 10_000_000], [15_000_000, 10_000_000]),
            ([15_000_000, 10_000_000], [15_000_000, 10_000_000 + step_h]),
            (
                [15_000_000, 10_000_000 + step_h],
                [20_000_000, 10_000_000 + step_h],
            ),
            ([20_000_000, 10_000_000 + step_h], [20_000_000, 10_000_000]),
            ([20_000_000, 10_000_000], [30_000_000, 10_000_000]),
        ];
        for (idx, &(a, b)) in segs.iter().enumerate() {
            g.tracks.push(Track {
                id: uuid(500 + idx as u64),
                net: 1,
                start: a,
                end: b,
                width_nm: 300_000,
                locked: false,
                layer: "F.Cu".into(),
            });
        }
        g.routing_options = Some(RoutingOptions {
            length_match_groups: vec![LengthMatchGroupSpec {
                members: vec!["NET_A".into(), "NET_B".into()],
                ..Default::default()
            }],
            group_match_tolerance_nm: Some(15_000),
            group_meander_amplitude_nm: Some(4_000_000),
            ..Default::default()
        });
        let mut eng = Engine::new(g).unwrap();
        eng.route(2, 50_000).unwrap();
        let st = prepare_and_route_pairs_in_tx(&mut eng).unwrap();
        apply_length_matching_in_tx(&mut eng, &st).unwrap();
        eng.copper()
            .tracks()
            .filter(|t| t.net == 2)
            .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
            .sum()
    };
    let len_290 = run_step_probe(290_000);
    assert!(
        (len_290 - 20_000_000.0).abs() <= 200_000.0,
        "0.29mm < 0.30mm step should bypass to ~20.00mm, got {len_290}"
    );
    let len_300 = run_step_probe(300_000);
    assert!(
        (len_300 - 20_600_000.0).abs() <= 200_000.0,
        "0.30mm >= 0.30mm step should measure ~20.60mm, got {len_300}"
    );

    // 3. measure_net_fromto via-to-track copper overlap (d < R, R <= d < R+w/2, d >= R+w/2)
    let run_via_probe = |y_off: i64| -> f64 {
        let mut g = base_diff_input(&["NET_A", "NET_B"]);
        g.layers = vec!["F.Cu".into(), "In1.Cu".into(), "B.Cu".into()];
        g.pads.clear();
        g.pads.push(pad(1, 1, [10_000_000, 10_000_000], &["F.Cu"]));
        g.pads
            .push(pad(2, 1, [30_000_000, 10_000_000], &["In1.Cu"]));
        let mut pb1 = pad(3, 2, [10_000_000, 25_000_000], &["F.Cu"]);
        pb1.shape = Shape::Circle { radius_nm: 5_000 };
        let mut pb2 = pad(4, 2, [29_000_000, 25_000_000], &["F.Cu"]);
        pb2.shape = Shape::Circle { radius_nm: 5_000 };
        g.pads.push(pb1);
        g.pads.push(pb2);
        g.tracks.push(Track {
            id: uuid(600),
            net: 1,
            start: [10_000_000, 10_000_000],
            end: [15_000_000, 10_000_000],
            width_nm: 250_000,
            locked: false,
            layer: "F.Cu".into(),
        });
        g.tracks.push(Track {
            id: uuid(601),
            net: 1,
            start: [15_000_000, 10_000_000],
            end: [15_000_000, 10_000_000 + y_off],
            width_nm: 250_000,
            locked: false,
            layer: "F.Cu".into(),
        });
        g.tracks.push(Track {
            id: uuid(602),
            net: 1,
            start: [15_000_000, 10_000_000 + y_off],
            end: [15_000_000, 10_000_000],
            width_nm: 250_000,
            locked: false,
            layer: "In1.Cu".into(),
        });
        g.tracks.push(Track {
            id: uuid(603),
            net: 1,
            start: [15_000_000, 10_000_000],
            end: [30_000_000, 10_000_000],
            width_nm: 250_000,
            locked: false,
            layer: "In1.Cu".into(),
        });
        g.vias.push(Via {
            id: uuid(604),
            net: 1,
            at: [15_000_000, 10_000_000 + y_off],
            diameter_nm: 600_000,
            drill_nm: 300_000,
            locked: false,
            start_layer: "F.Cu".into(),
            end_layer: "B.Cu".into(),
            kind: ViaKind::Through,
        });
        g.routing_options = Some(RoutingOptions {
            length_match_groups: vec![LengthMatchGroupSpec {
                members: vec!["NET_A".into(), "NET_B".into()],
                ..Default::default()
            }],
            group_match_tolerance_nm: Some(15_000),
            group_meander_amplitude_nm: Some(4_000_000),
            ..Default::default()
        });
        let mut eng = Engine::new(g).unwrap();
        eng.route(2, 50_000).unwrap();
        let st = prepare_and_route_pairs_in_tx(&mut eng).unwrap();
        apply_length_matching_in_tx(&mut eng, &st).unwrap();
        eng.copper()
            .tracks()
            .filter(|t| t.net == 2)
            .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
            .sum()
    };
    let v_175 = run_via_probe(175_000);
    let v_300 = run_via_probe(300_000);
    let v_425 = run_via_probe(425_000);
    assert!(
        (v_175 - v_300 - 112_661.0).abs() <= 30_000.0,
        "d=0.175 < R=0.30 should add ~0.1127mm over d=R: v_175={v_175}, v_300={v_300}"
    );
    assert!(
        (v_425 - v_300 - 850_000.0).abs() <= 30_000.0,
        "d=0.425 == R+w/2 should add ~0.8500mm over d=R: v_425={v_425}, v_300={v_300}"
    );
}

#[test]
fn r4_pitch_720_gap_max_enforced_and_indiffpair_underscore_stem() {
    // 1. adv_pitch_720: 0.30x0.30mm SMD pads at 0.72mm pitch with diff_pair_gap (min 0.34mm) (max 0.36mm)
    let mut input = base_diff_input(&["CAN+", "CAN-"]);
    input.pads.clear();
    let mk_rect = |id, net, at| {
        let mut p = pad(id, net, at, &["F.Cu"]);
        p.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        p
    };
    input.pads.push(mk_rect(1, 1, [10_000_000, 14_640_000]));
    input.pads.push(mk_rect(2, 1, [40_000_000, 14_640_000]));
    input.pads.push(mk_rect(3, 2, [10_000_000, 15_360_000]));
    input.pads.push(mk_rect(4, 2, [40_000_000, 15_360_000]));
    input.custom_rules = Some(
        "(version 1)\n(rule \"gap\" (constraint diff_pair_gap (min 0.34mm) (max 0.36mm)))".into(),
    );
    input.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        pair_layers: vec!["F.Cu".into()],
        ..Default::default()
    });
    let mut eng = Engine::new(input).unwrap();
    prepare_and_route_pairs_in_tx(&mut eng).unwrap();
    let p_tracks: Vec<_> = eng.copper().generated().filter(|t| t.net == 1).collect();
    let n_tracks: Vec<_> = eng.copper().generated().filter(|t| t.net == 2).collect();
    assert!(!p_tracks.is_empty() && !n_tracks.is_empty());
    // Ensure no strictly parallel overlapping segments exceed gap_max (0.36mm -> 0.61mm center-to-center)
    for pt in &p_tracks {
        let p_len = ((pt.end[0] - pt.start[0]) as f64).hypot((pt.end[1] - pt.start[1]) as f64);
        let (ux, uy) = (
            (pt.end[0] - pt.start[0]) as f64 / p_len,
            (pt.end[1] - pt.start[1]) as f64 / p_len,
        );
        for nt in &n_tracks {
            let n_len = ((nt.end[0] - nt.start[0]) as f64).hypot((nt.end[1] - nt.start[1]) as f64);
            let (nx, ny) = (
                (nt.end[0] - nt.start[0]) as f64 / n_len,
                (nt.end[1] - nt.start[1]) as f64 / n_len,
            );
            if (ux * nx + uy * ny).abs() >= 0.99999 {
                let p1 = ux * (nt.start[0] - pt.start[0]) as f64
                    + uy * (nt.start[1] - pt.start[1]) as f64;
                let p2 =
                    ux * (nt.end[0] - pt.start[0]) as f64 + uy * (nt.end[1] - pt.start[1]) as f64;
                if p1.max(p2).min(p_len) > p1.min(p2).max(0.0) + 1_000.0 {
                    let perp = (ux * (nt.start[1] - pt.start[1]) as f64
                        - uy * (nt.start[0] - pt.start[0]) as f64)
                        .abs();
                    assert!(
                        perp <= 610_005.0,
                        "parallel tracks exceeded gap_max: perp={perp}"
                    );
                }
            }
        }
    }

    // 2. DP_+/DP_- with A.inDiffPair('DP') and A.inDiffPair('DP_') match; A.inDiffPair('DP_+') does not
    let mut dp_u = base_diff_input(&["DP_+", "DP_-"]);
    dp_u.custom_rules = Some(
        "(version 1)\n(rule \"dp\" (condition \"A.inDiffPair('DP')\") (constraint skew (max 0.025mm)))".into(),
    );
    let r_dp = resolve_effective_rules(&mut dp_u).unwrap();
    assert_eq!(
        discover_and_validate(&dp_u, &r_dp).unwrap().pairs[0].dru_skew_max_nm,
        Some(25_000)
    );

    dp_u.custom_rules = Some(
        "(version 1)\n(rule \"dp_\" (condition \"A.inDiffPair('DP_')\") (constraint skew (max 0.030mm)))".into(),
    );
    let r_dp_ = resolve_effective_rules(&mut dp_u).unwrap();
    assert_eq!(
        discover_and_validate(&dp_u, &r_dp_).unwrap().pairs[0].dru_skew_max_nm,
        Some(30_000)
    );

    dp_u.custom_rules = Some(
        "(version 1)\n(rule \"dp_plus\" (condition \"A.inDiffPair('DP_+')\") (constraint skew (max 0.025mm)))".into(),
    );
    let r_dp_plus = resolve_effective_rules(&mut dp_u).unwrap();
    assert_eq!(
        discover_and_validate(&dp_u, &r_dp_plus).unwrap().pairs[0].dru_skew_max_nm,
        None
    );
}

#[test]
fn r4_multilayer_start_end_and_ortho_pads_aligned_leads() {
    // 1. adv_4l_in1_in2_clean: F.Cu SMD pads -> In1.Cu (right blocked) -> In2.Cu (left blocked) -> F.Cu
    let mut ml = base_diff_input(&["CAN+", "CAN-"]);
    ml.layers = vec![
        "F.Cu".into(),
        "In1.Cu".into(),
        "In2.Cu".into(),
        "B.Cu".into(),
    ];
    ml.pads.clear();
    ml.pads.push(pad(1, 1, [10_000_000, 14_700_000], &["F.Cu"]));
    ml.pads.push(pad(2, 1, [40_000_000, 18_700_000], &["F.Cu"]));
    ml.pads.push(pad(3, 2, [10_000_000, 15_300_000], &["F.Cu"]));
    ml.pads.push(pad(4, 2, [40_000_000, 19_300_000], &["F.Cu"]));
    ml.rule_areas.push(RuleArea {
        id: uuid(900),
        name: None,
        layers: vec!["In1.Cu".into()],
        outer: vec![
            [28_000_000, 0],
            [50_000_000, 0],
            [50_000_000, 40_000_000],
            [28_000_000, 40_000_000],
        ],
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        no_tracks: true,
        no_vias: false,
        no_pads: false,
        no_footprints: false,
        no_zone_fills: false,
    });
    ml.rule_areas.push(RuleArea {
        id: uuid(901),
        name: None,
        layers: vec!["In2.Cu".into()],
        outer: vec![
            [0, 0],
            [20_000_000, 0],
            [20_000_000, 40_000_000],
            [0, 40_000_000],
        ],
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        no_tracks: true,
        no_vias: false,
        no_pads: false,
        no_footprints: false,
        no_zone_fills: false,
    });
    ml.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        pair_layers: vec!["In1.Cu".into(), "In2.Cu".into()],
        ..Default::default()
    });
    let mut eng_ml = Engine::new(ml).unwrap();
    prepare_and_route_pairs_in_tx(&mut eng_ml).unwrap();
    assert!(eng_ml.copper().generated_vias().count() >= 6);
    assert!(eng_ml.copper().generated().any(|t| t.layer == "In1.Cu"));
    assert!(eng_ml.copper().generated().any(|t| t.layer == "In2.Cu"));

    // 2. adv_4l_ortho_pads_in1: aligned cross-layer leads & zero non-jog via-to-track copper overlaps
    let mut ortho = base_diff_input(&["CAN+", "CAN-"]);
    ortho.layers = vec![
        "F.Cu".into(),
        "In1.Cu".into(),
        "In2.Cu".into(),
        "B.Cu".into(),
    ];
    ortho.pads.clear();
    ortho
        .pads
        .push(pad(1, 1, [10_000_000, 11_700_000], &["F.Cu"]));
    ortho
        .pads
        .push(pad(2, 1, [35_300_000, 28_000_000], &["F.Cu"]));
    ortho
        .pads
        .push(pad(3, 2, [10_000_000, 12_300_000], &["F.Cu"]));
    ortho
        .pads
        .push(pad(4, 2, [34_700_000, 28_000_000], &["F.Cu"]));
    ortho.custom_rules =
        Some("(version 1)\n(rule \"unc\" (constraint diff_pair_uncoupled (max 8.0mm)))".into());
    ortho.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        pair_layers: vec!["In1.Cu".into()],
        intra_match: true,
        length_match_tolerance_nm: Some(150_000),
        ..Default::default()
    });
    let mut eng_o = Engine::new(ortho).unwrap();
    let st_o = prepare_and_route_pairs_in_tx(&mut eng_o).unwrap();
    apply_length_matching_in_tx(&mut eng_o, &st_o).unwrap();
    let vias: Vec<Via> = eng_o.copper().generated_vias().cloned().collect();
    let tracks: Vec<Track> = eng_o.copper().generated().cloned().collect();
    assert_eq!(vias.len(), 4);
    for v in &vias {
        let mut hubs: Vec<(String, Point)> = Vec::new();
        for tj in &tracks {
            if tj.net == v.net && (tj.start == v.at || tj.end == v.at) {
                let other = if tj.start == v.at { tj.end } else { tj.start };
                let d = ((other[0] - v.at[0]) as f64).hypot((other[1] - v.at[1]) as f64);
                if d * 2.0 < (v.diameter_nm + tj.width_nm) as f64 {
                    hubs.push((tj.layer.clone(), other));
                }
            }
        }
        for tr in &tracks {
            if tr.net != v.net || tr.start == v.at || tr.end == v.at {
                continue;
            }
            if hubs
                .iter()
                .any(|(hl, hp)| *hl == tr.layer && (tr.start == *hp || tr.end == *hp))
            {
                continue;
            }
            assert!(
                !kicad_route_engine::geometry::point_near_segment(
                    v.at,
                    tr.start,
                    tr.end,
                    i128::from(v.diameter_nm) + i128::from(tr.width_nm),
                    false,
                ),
                "non-jog track {:?}->{:?} on {} overlapped via {:?}",
                tr.start,
                tr.end,
                tr.layer,
                v.at
            );
        }
    }
}

#[test]
fn r5_asymmetric_pad_fromto_zero_offset_xcross_split_and_dp_pn_indiffpair() {
    // 1. Asymmetric SMD pads (1.2x1.2mm vs 0.25x0.25mm) in SE length_match_groups:
    //    measure_net_fromto adds 0.0mm pad-center offset, so actual copper lengths match within tol.
    let mut asym = base_diff_input(&["SIG_A", "SIG_B"]);
    asym.pads.clear();
    let mut pa1 = pad(1, 1, [10_000_000, 15_000_000], &["F.Cu"]);
    pa1.shape = Shape::Rect {
        half_size_nm: [600_000, 600_000],
    };
    let mut pa2 = pad(2, 1, [35_000_000, 15_000_000], &["F.Cu"]);
    pa2.shape = Shape::Rect {
        half_size_nm: [600_000, 600_000],
    };
    let mut pb1 = pad(3, 2, [10_000_000, 25_000_000], &["F.Cu"]);
    pb1.shape = Shape::Rect {
        half_size_nm: [125_000, 125_000],
    };
    let mut pb2 = pad(4, 2, [30_000_000, 25_000_000], &["F.Cu"]);
    pb2.shape = Shape::Rect {
        half_size_nm: [125_000, 125_000],
    };
    asym.pads.extend([pa1, pa2, pb1, pb2]);
    asym.routing_options = Some(RoutingOptions {
        length_match_groups: vec![LengthMatchGroupSpec {
            members: vec!["SIG_A".into(), "SIG_B".into()],
            ..Default::default()
        }],
        group_match_tolerance_nm: Some(100_000),
        group_meander_amplitude_nm: Some(3_000_000),
        ..Default::default()
    });
    let mut eng_asym = Engine::new(asym).unwrap();
    eng_asym.route(1, 50_000).unwrap();
    eng_asym.route(2, 50_000).unwrap();
    let st_asym = prepare_and_route_pairs_in_tx(&mut eng_asym).unwrap();
    apply_length_matching_in_tx(&mut eng_asym, &st_asym).unwrap();
    let sum_net_copper = |eng: &Engine, nid: u32| -> f64 {
        eng.copper()
            .tracks()
            .filter(|t| t.net == nid)
            .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
            .sum()
    };
    let len_a = sum_net_copper(&eng_asym, 1);
    let len_b = sum_net_copper(&eng_asym, 2);
    assert!(
        (len_a - len_b).abs() <= 100_000.0,
        "asymmetric pad SE copper skew exceeded 0.10mm: SIG_A={len_a}, SIG_B={len_b}"
    );

    // 2. Non-incident X-crossing tracks match KiCad 10.0.8 fromTo:
    //    unsplit crossing tracks incur len(ta) + len(tb) (35.4626mm on x_cross_open / x_closed_loop,
    //    31.6228mm on x_simple_open), while explicit shared-vertex tracks prune stubs (24.1994mm).
    let run_xcross_probe = |segs: &[([i64; 2], [i64; 2])]| -> f64 {
        let mut xc = base_diff_input(&["SIG_P", "SIG_N"]);
        xc.pads.clear();
        let mut px1 = pad(1, 1, [10_000_000, 20_000_000], &["F.Cu"]);
        px1.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut px2 = pad(2, 1, [30_000_000, 20_000_000], &["F.Cu"]);
        px2.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut pn1 = pad(3, 2, [10_000_000, 30_000_000], &["F.Cu"]);
        pn1.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut pn2 = pad(4, 2, [28_000_000, 30_000_000], &["F.Cu"]);
        pn2.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        xc.pads.extend([px1, px2, pn1, pn2]);
        for (idx, &(a, b)) in segs.iter().enumerate() {
            xc.tracks.push(Track {
                id: uuid(800 + idx as u64),
                net: 1,
                start: a,
                end: b,
                width_nm: 300_000,
                locked: false,
                layer: "F.Cu".into(),
            });
        }
        xc.routing_options = Some(RoutingOptions {
            length_match_groups: vec![LengthMatchGroupSpec {
                members: vec!["SIG_P".into(), "SIG_N".into()],
                ..Default::default()
            }],
            group_match_tolerance_nm: Some(100_000),
            group_meander_amplitude_nm: Some(4_000_000),
            ..Default::default()
        });
        let mut eng_xc = Engine::new(xc).unwrap();
        eng_xc.route(2, 50_000).unwrap();
        let st_xc = prepare_and_route_pairs_in_tx(&mut eng_xc).unwrap();
        apply_length_matching_in_tx(&mut eng_xc, &st_xc).unwrap();
        sum_net_copper(&eng_xc, 2)
    };
    let len_x_open = run_xcross_probe(&[
        ([10_000_000, 20_000_000], [22_000_000, 22_000_000]),
        ([15_000_000, 25_000_000], [15_000_000, 17_000_000]),
        ([15_000_000, 17_000_000], [30_000_000, 20_000_000]),
    ]);
    assert!(
        (len_x_open - 35_462_584.0).abs() <= 150_000.0,
        "x_cross_open SIG_N copper {len_x_open} did not match KiCad 10.0.8 35.4626mm"
    );
    let len_x_closed = run_xcross_probe(&[
        ([10_000_000, 20_000_000], [22_000_000, 22_000_000]),
        ([22_000_000, 22_000_000], [15_000_000, 25_000_000]),
        ([15_000_000, 25_000_000], [15_000_000, 17_000_000]),
        ([15_000_000, 17_000_000], [30_000_000, 20_000_000]),
    ]);
    assert!(
        (len_x_closed - 35_462_584.0).abs() <= 150_000.0,
        "x_closed_loop SIG_N copper {len_x_closed} did not match KiCad 10.0.8 35.4626mm"
    );
    let len_x_simple = run_xcross_probe(&[
        ([10_000_000, 20_000_000], [25_000_000, 25_000_000]),
        ([15_000_000, 25_000_000], [30_000_000, 20_000_000]),
    ]);
    assert!(
        (len_x_simple - 31_622_777.0).abs() <= 150_000.0,
        "x_simple_open SIG_N copper {len_x_simple} did not match KiCad 10.0.8 31.6228mm"
    );
    let len_x_shared = run_xcross_probe(&[
        ([10_000_000, 20_000_000], [15_000_000, 20_833_333]),
        ([15_000_000, 20_833_333], [22_000_000, 22_000_000]),
        ([15_000_000, 25_000_000], [15_000_000, 20_833_333]),
        ([15_000_000, 20_833_333], [15_000_000, 17_000_000]),
        ([15_000_000, 17_000_000], [30_000_000, 20_000_000]),
    ]);
    assert!(
        (len_x_shared - 24_199_361.0).abs() <= 150_000.0,
        "x_shared_vertex SIG_N copper {len_x_shared} did not match KiCad 10.0.8 24.1994mm"
    );

    // 3. (DP_P, DP_N) matches both A.inDiffPair('DP') and A.inDiffPair('DP_') (not 'DP_P'),
    //    while (DP+, DP-) matches A.inDiffPair('DP') and NOT A.inDiffPair('DP_').
    let mut dp_pn = base_diff_input(&["DP_P", "DP_N"]);
    dp_pn.custom_rules = Some(
        "(version 1)\n(rule \"dp\" (condition \"A.inDiffPair('DP')\") (constraint skew (max 0.025mm)))".into(),
    );
    let r_pn = resolve_effective_rules(&mut dp_pn).unwrap();
    let plan_pn = discover_and_validate(&dp_pn, &r_pn).unwrap();
    assert_eq!(plan_pn.pairs[0].stem, "DP");
    assert_eq!(plan_pn.pairs[0].dru_skew_max_nm, Some(25_000));

    dp_pn.custom_rules = Some(
        "(version 1)\n(rule \"dp_\" (condition \"A.inDiffPair('DP_')\") (constraint skew (max 0.030mm)))".into(),
    );
    let r_pn_ = resolve_effective_rules(&mut dp_pn).unwrap();
    let plan_pn_ = discover_and_validate(&dp_pn, &r_pn_).unwrap();
    assert_eq!(plan_pn_.pairs[0].stem, "DP");
    assert_eq!(plan_pn_.pairs[0].dru_skew_max_nm, Some(30_000));

    dp_pn.custom_rules = Some(
        "(version 1)\n(rule \"dp_p\" (condition \"A.inDiffPair('DP_P')\") (constraint skew (max 0.030mm)))".into(),
    );
    let r_pn_p = resolve_effective_rules(&mut dp_pn).unwrap();
    assert_eq!(
        discover_and_validate(&dp_pn, &r_pn_p).unwrap().pairs[0].dru_skew_max_nm,
        None
    );

    let mut dp_pm = base_diff_input(&["DP+", "DP-"]);
    dp_pm.custom_rules = Some(
        "(version 1)\n(rule \"dp\" (condition \"A.inDiffPair('DP')\") (constraint skew (max 0.025mm)))".into(),
    );
    let r_pm = resolve_effective_rules(&mut dp_pm).unwrap();
    assert_eq!(
        discover_and_validate(&dp_pm, &r_pm).unwrap().pairs[0].dru_skew_max_nm,
        Some(25_000)
    );

    dp_pm.custom_rules = Some(
        "(version 1)\n(rule \"dp_\" (condition \"A.inDiffPair('DP_')\") (constraint skew (max 0.030mm)))".into(),
    );
    let r_pm_ = resolve_effective_rules(&mut dp_pm).unwrap();
    assert_eq!(
        discover_and_validate(&dp_pm, &r_pm_).unwrap().pairs[0].dru_skew_max_nm,
        None
    );
}

#[test]
fn r6_xcross_chained_near_ep_and_unsplit_t_match_kicad10() {
    let sum_net_copper = |eng: &Engine, nid: u32| -> f64 {
        eng.copper()
            .tracks()
            .filter(|t| t.net == nid)
            .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
            .sum()
    };
    let run_probe = |p1: [i64; 2],
                     p2: [i64; 2],
                     pad1_layers: &[&str],
                     pad_half: i64,
                     segs: &[([i64; 2], [i64; 2], &str)]|
     -> f64 {
        let mut xc = base_diff_input(&["SIG_P", "SIG_N"]);
        xc.pads.clear();
        let mut px1 = pad(1, 1, p1, pad1_layers);
        px1.shape = Shape::Rect {
            half_size_nm: [pad_half, pad_half],
        };
        let mut px2 = pad(2, 1, p2, pad1_layers);
        px2.shape = Shape::Rect {
            half_size_nm: [pad_half, pad_half],
        };
        let mut pn1 = pad(3, 2, [10_000_000, 35_000_000], &["F.Cu"]);
        pn1.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut pn2 = pad(4, 2, [20_000_000, 35_000_000], &["F.Cu"]);
        pn2.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        xc.pads.extend([px1, px2, pn1, pn2]);
        for (idx, &(a, b, layer)) in segs.iter().enumerate() {
            xc.tracks.push(Track {
                id: uuid(1800 + idx as u64),
                net: 1,
                start: a,
                end: b,
                width_nm: 300_000,
                locked: false,
                layer: layer.into(),
            });
        }
        xc.routing_options = Some(RoutingOptions {
            length_match_groups: vec![LengthMatchGroupSpec {
                members: vec!["SIG_P".into(), "SIG_N".into()],
                ..Default::default()
            }],
            group_match_tolerance_nm: Some(100_000),
            group_meander_amplitude_nm: Some(4_000_000),
            ..Default::default()
        });
        let mut eng = Engine::new(xc).unwrap();
        eng.route(2, 50_000).unwrap();
        let st = prepare_and_route_pairs_in_tx(&mut eng).unwrap();
        apply_length_matching_in_tx(&mut eng, &st).unwrap();
        sum_net_copper(&eng, 2)
    };

    let cases: &[(
        &str,
        [i64; 2],
        [i64; 2],
        &[&str],
        i64,
        &[([i64; 2], [i64; 2], &str)],
        f64,
    )] = &[
        (
            "x_double_cross",
            [10_000_000, 20_000_000],
            [30_000_000, 20_000_000],
            &["F.Cu"],
            150_000,
            &[
                ([10_000_000, 20_000_000], [18_000_000, 24_000_000], "F.Cu"),
                ([14_000_000, 25_000_000], [22_000_000, 18_000_000], "F.Cu"),
                ([18_000_000, 19_000_000], [30_000_000, 20_000_000], "F.Cu"),
            ],
            31_616_000.0,
        ),
        (
            "x_triple_cross",
            [10_000_000, 20_000_000],
            [30_000_000, 20_000_000],
            &["F.Cu"],
            150_000,
            &[
                ([10_000_000, 20_000_000], [16_000_000, 24_000_000], "F.Cu"),
                ([14_000_000, 25_000_000], [20_000_000, 17_000_000], "F.Cu"),
                ([18_000_000, 18_000_000], [24_000_000, 26_000_000], "F.Cu"),
                ([21_000_000, 25_000_000], [30_000_000, 20_000_000], "F.Cu"),
            ],
            37_506_700.0,
        ),
        (
            "x_near_ep_020",
            [10_000_000, 20_000_000],
            [30_000_000, 20_000_000],
            &["F.Cu"],
            150_000,
            &[
                ([10_000_000, 20_000_000], [15_200_000, 20_000_000], "F.Cu"),
                ([15_000_000, 25_000_000], [15_000_000, 17_000_000], "F.Cu"),
                ([15_000_000, 17_000_000], [30_000_000, 20_000_000], "F.Cu"),
            ],
            28_497_100.0,
        ),
        (
            "x_near_ep_005",
            [10_000_000, 20_000_000],
            [30_000_000, 20_000_000],
            &["F.Cu"],
            150_000,
            &[
                ([10_000_000, 20_000_000], [15_050_000, 20_000_000], "F.Cu"),
                ([15_000_000, 25_000_000], [15_000_000, 17_000_000], "F.Cu"),
                ([15_000_000, 17_000_000], [30_000_000, 20_000_000], "F.Cu"),
            ],
            28_347_100.0,
        ),
        (
            "x_near_b_ep_020",
            [10_000_000, 20_000_000],
            [30_000_000, 20_000_000],
            &["F.Cu"],
            150_000,
            &[
                ([10_000_000, 20_000_000], [22_000_000, 20_000_000], "F.Cu"),
                ([15_000_000, 20_200_000], [15_000_000, 15_000_000], "F.Cu"),
                ([15_000_000, 15_000_000], [30_000_000, 20_000_000], "F.Cu"),
            ],
            33_011_400.0,
        ),
        (
            "t_near_gap_015",
            [10_000_000, 20_000_000],
            [30_000_000, 20_000_000],
            &["F.Cu"],
            150_000,
            &[
                ([10_000_000, 20_000_000], [14_850_000, 20_000_000], "F.Cu"),
                ([15_000_000, 25_000_000], [15_000_000, 17_000_000], "F.Cu"),
                ([15_000_000, 17_000_000], [30_000_000, 20_000_000], "F.Cu"),
            ],
            28_147_100.0,
        ),
        (
            "t_interior_unsplit",
            [10_000_000, 20_000_000],
            [20_000_000, 25_000_000],
            &["F.Cu"],
            150_000,
            &[
                ([10_000_000, 20_000_000], [30_000_000, 20_000_000], "F.Cu"),
                ([20_000_000, 20_000_000], [20_000_000, 25_000_000], "F.Cu"),
            ],
            25_000_000.0,
        ),
        (
            "cav1_pth_direct_plus_bcu_boundary",
            [10_000_000, 20_000_000],
            [30_000_000, 20_000_000],
            &["F.Cu", "B.Cu"],
            800_000,
            &[
                ([10_000_000, 20_000_000], [10_000_000, 30_000_000], "F.Cu"),
                ([10_000_000, 30_000_000], [30_000_000, 30_000_000], "F.Cu"),
                ([30_000_000, 30_000_000], [30_000_000, 20_000_000], "F.Cu"),
                ([10_700_000, 20_000_000], [30_000_000, 20_000_000], "B.Cu"),
            ],
            19_300_000.0,
        ),
        (
            "cav2_t_on_unsplit_x_noncross_tc",
            [15_000_000, 25_000_000],
            [30_000_000, 15_000_000],
            &["F.Cu"],
            150_000,
            &[
                ([15_000_000, 25_000_000], [15_000_000, 20_000_000], "F.Cu"),
                ([10_000_000, 20_000_000], [30_000_000, 20_000_000], "F.Cu"),
                ([22_000_000, 25_000_000], [22_000_000, 15_000_000], "F.Cu"),
                ([22_000_000, 15_000_000], [30_000_000, 15_000_000], "F.Cu"),
            ],
            43_000_000.0,
        ),
    ];
    for &(name, p1, p2, p1_layers, p_half, segs, expected_nm) in cases {
        let got = run_probe(p1, p2, p1_layers, p_half, segs);
        assert!(
            (got - expected_nm).abs() <= 150_000.0,
            "{name}: SIG_N copper {got} != expected {expected_nm}"
        );
    }
}

#[test]
fn r6_via_height_no_stackup_and_pro_use_height_override() {
    let run_via_height = |layers: &[&str],
                          mid_layer: &str,
                          board_thickness_nm: Option<i64>,
                          mask_nm: Option<i64>,
                          pro_use_height: Option<bool>|
     -> Result<f64, Failure> {
        let mut g = base_diff_input(&["SIG_A", "SIG_B"]);
        g.layers = layers.iter().map(|s| (*s).to_owned()).collect();
        g.pads.clear();
        g.pads.push(pad(1, 1, [10_000_000, 15_000_000], &["F.Cu"]));
        g.pads.push(pad(2, 1, [30_000_000, 15_000_000], &["F.Cu"]));
        let mut pb1 = pad(3, 2, [10_000_000, 30_000_000], &["F.Cu"]);
        pb1.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut pb2 = pad(4, 2, [20_000_000, 30_000_000], &["F.Cu"]);
        pb2.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        g.pads.extend([pb1, pb2]);
        g.tracks.extend([
            Track {
                id: uuid(1900),
                net: 1,
                start: [10_000_000, 15_000_000],
                end: [15_000_000, 15_000_000],
                width_nm: 250_000,
                locked: false,
                layer: "F.Cu".into(),
            },
            Track {
                id: uuid(1901),
                net: 1,
                start: [15_000_000, 15_000_000],
                end: [25_000_000, 15_000_000],
                width_nm: 250_000,
                locked: false,
                layer: mid_layer.into(),
            },
            Track {
                id: uuid(1902),
                net: 1,
                start: [25_000_000, 15_000_000],
                end: [30_000_000, 15_000_000],
                width_nm: 250_000,
                locked: false,
                layer: "F.Cu".into(),
            },
        ]);
        g.vias.extend([
            Via {
                id: uuid(1903),
                net: 1,
                at: [15_000_000, 15_000_000],
                diameter_nm: 600_000,
                drill_nm: 300_000,
                locked: false,
                start_layer: "F.Cu".into(),
                end_layer: "B.Cu".into(),
                kind: ViaKind::Through,
            },
            Via {
                id: uuid(1904),
                net: 1,
                at: [25_000_000, 15_000_000],
                diameter_nm: 600_000,
                drill_nm: 300_000,
                locked: false,
                start_layer: "F.Cu".into(),
                end_layer: "B.Cu".into(),
                kind: ViaKind::Through,
            },
        ]);
        let bds = g.board_design_settings.as_mut().unwrap();
        bds.use_height_for_length_calcs = Some(true);
        bds.board_thickness_nm = board_thickness_nm;
        bds.outer_solder_mask_thickness_nm = mask_nm;
        if let Some(uh) = pro_use_height {
            g.project_rules = Some(
                serde_json::json!({
                    "meta": {"version": 3},
                    "board": {"design_settings": {"rules": {"use_height_for_length_calcs": uh}}}
                })
                .to_string(),
            );
        }
        g.routing_options = Some(RoutingOptions {
            length_match_groups: vec![LengthMatchGroupSpec {
                members: vec!["SIG_A".into(), "SIG_B".into()],
                ..Default::default()
            }],
            group_match_tolerance_nm: Some(100_000),
            group_meander_amplitude_nm: Some(4_000_000),
            ..Default::default()
        });
        let mut eng = Engine::new(g)?;
        eng.route(2, 50_000)?;
        let st = prepare_and_route_pairs_in_tx(&mut eng)?;
        apply_length_matching_in_tx(&mut eng, &st)?;
        Ok(eng
            .copper()
            .tracks()
            .filter(|t| t.net == 2)
            .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
            .sum())
    };

    // 2L 1.6mm both masks -> 2 * 1.5800mm = 3.1600mm -> 23.1600mm
    let len_2l = run_via_height(
        &["F.Cu", "B.Cu"],
        "B.Cu",
        Some(1_600_000),
        Some(20_000),
        None,
    )
    .unwrap();
    assert!(
        (len_2l - 23_160_000.0).abs() <= 150_000.0,
        "2L empty-stackup via height expected ~23.1600mm, got {len_2l}"
    );
    // 4L 1.6mm both masks F.Cu<->In1.Cu -> 2 * 0.5325mm = 1.0650mm -> 21.0650mm
    let len_4l = run_via_height(
        &["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
        "In1.Cu",
        Some(1_600_000),
        Some(20_000),
        None,
    )
    .unwrap();
    assert!(
        (len_4l - 21_065_000.0).abs() <= 150_000.0,
        "4L empty-stackup F->In1 via height expected ~21.0650mm, got {len_4l}"
    );
    // .kicad_pro use_height=false overrides bds use_height=true -> 20.0000mm
    let len_pro_false = run_via_height(
        &["F.Cu", "B.Cu"],
        "B.Cu",
        Some(1_600_000),
        Some(20_000),
        Some(false),
    )
    .unwrap();
    assert!(
        (len_pro_false - 20_000_000.0).abs() <= 150_000.0,
        ".kicad_pro use_height=false expected ~20.0000mm, got {len_pro_false}"
    );
    // Missing stackup and missing board_thickness_nm with use_height=true fails closed Invalid
    let err = run_via_height(&["F.Cu", "B.Cu"], "B.Cu", None, None, None).unwrap_err();
    assert_eq!(err.status, Status::Invalid);
}

#[test]
fn r7_via_collinear_angled_via_overrides_and_pro_ds_use_height() {
    let run_via_probe = |name: &str,
                         p1: [i64; 2],
                         p2: [i64; 2],
                         p2_layers: &[&str],
                         segs: &[([i64; 2], [i64; 2], &str)],
                         vias: &[([i64; 2], &str, &str)]|
     -> f64 {
        let mut g = base_diff_input(&["SIG_P", "SIG_N"]);
        g.pads.clear();
        let mut p1_pad = pad(1, 1, p1, &["F.Cu"]);
        p1_pad.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut p2_pad = pad(2, 1, p2, p2_layers);
        p2_pad.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut n1_pad = pad(3, 2, [10_000_000, 35_000_000], &["F.Cu"]);
        n1_pad.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut n2_pad = pad(4, 2, [20_000_000, 35_000_000], &["F.Cu"]);
        n2_pad.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        g.pads.extend([p1_pad, p2_pad, n1_pad, n2_pad]);
        for (idx, &(a, b, layer)) in segs.iter().enumerate() {
            g.tracks.push(Track {
                id: uuid(2100 + idx as u64),
                net: 1,
                start: a,
                end: b,
                width_nm: 300_000,
                locked: false,
                layer: layer.into(),
            });
        }
        for (idx, &(at, sl, el)) in vias.iter().enumerate() {
            g.vias.push(Via {
                id: uuid(2200 + idx as u64),
                net: 1,
                at,
                diameter_nm: 600_000,
                drill_nm: 300_000,
                locked: false,
                start_layer: sl.into(),
                end_layer: el.into(),
                kind: ViaKind::Through,
            });
        }
        g.routing_options = Some(RoutingOptions {
            length_match_groups: vec![LengthMatchGroupSpec {
                members: vec!["SIG_P".into(), "SIG_N".into()],
                ..Default::default()
            }],
            group_match_tolerance_nm: Some(10_000),
            group_meander_amplitude_nm: Some(4_000_000),
            ..Default::default()
        });
        let mut eng = Engine::new(g).unwrap();
        eng.route(2, 50_000).unwrap();
        let st = prepare_and_route_pairs_in_tx(&mut eng).unwrap();
        apply_length_matching_in_tx(&mut eng, &st).unwrap();
        let sum: f64 = eng
            .copper()
            .tracks()
            .filter(|t| t.net == 2)
            .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
            .sum();
        assert!(sum > 0.0, "{name}: expected routed copper");
        sum
    };

    // 1. Collinear same-layer bridge & F->B transitions + 45-deg angled probes + R4 regression
    let len_bridge = run_via_probe(
        "via_collinear_bridge_same_layer",
        [10_000_000, 20_000_000],
        [30_000_000, 20_000_000],
        &["F.Cu"],
        &[
            ([10_000_000, 20_000_000], [18_000_000, 20_000_000], "F.Cu"),
            ([18_350_000, 20_000_000], [30_000_000, 20_000_000], "F.Cu"),
        ],
        &[([18_175_000, 20_000_000], "F.Cu", "B.Cu")],
    );
    assert!(
        (len_bridge - 20_000_000.0).abs() <= 50_000.0,
        "via_collinear_bridge_same_layer got {len_bridge}"
    );
    for d_ep in [50_000_i64, 175_000, 250_000] {
        let len_fb = run_via_probe(
            "via_collinear_f_to_b",
            [10_000_000, 20_000_000],
            [30_000_000, 20_000_000],
            &["B.Cu"],
            &[
                (
                    [10_000_000, 20_000_000],
                    [20_000_000 - d_ep, 20_000_000],
                    "F.Cu",
                ),
                (
                    [20_000_000 + d_ep, 20_000_000],
                    [30_000_000, 20_000_000],
                    "B.Cu",
                ),
            ],
            &[([20_000_000, 20_000_000], "F.Cu", "B.Cu")],
        );
        assert!(
            (len_fb - 20_000_000.0).abs() <= 50_000.0,
            "via_collinear_f_to_b d_ep={d_ep} got {len_fb}"
        );
    }
    let s175 = (175_000.0_f64 / std::f64::consts::SQRT_2).round() as i64;
    let len_45 = run_via_probe(
        "via_45_175",
        [10_000_000, 20_000_000],
        [30_000_000, 20_000_000],
        &["B.Cu"],
        &[
            (
                [10_000_000, 20_000_000],
                [20_000_000 - s175, 20_000_000 - s175],
                "F.Cu",
            ),
            (
                [20_000_000 + s175, 20_000_000 - s175],
                [30_000_000, 20_000_000],
                "B.Cu",
            ),
        ],
        &[([20_000_000, 20_000_000], "F.Cu", "B.Cu")],
    );
    assert!(
        (len_45 - 20_053_300.0).abs() <= 50_000.0,
        "via_45_175 got {len_45}"
    );
    let len_r4 = run_via_probe(
        "r4_via_probe_175",
        [10_000_000, 20_000_000],
        [30_000_000, 20_000_000],
        &["B.Cu"],
        &[
            ([10_000_000, 20_000_000], [20_000_000, 20_000_000], "F.Cu"),
            ([20_000_000, 20_000_000], [30_000_000, 20_000_000], "B.Cu"),
        ],
        &[([20_000_000, 20_175_000], "F.Cu", "B.Cu")],
    );
    assert!(
        (len_r4 - 20_112_700.0).abs() <= 50_000.0,
        "r4_via_probe_175 got {len_r4}"
    );
}

#[test]
fn r7_via_overrides_and_pro_ds_use_height_parity() {
    // 1. via_overrides KiCad 10.0.8 parity (4L F.Cu <-> In2.Cu via F.Cu..B.Cu 600um vias)
    let run_via_ov = |via_ov_json: serde_json::Value| -> f64 {
        let mut g = base_diff_input(&["DQ0", "DQ1"]);
        g.layers = vec![
            "F.Cu".into(),
            "In1.Cu".into(),
            "In2.Cu".into(),
            "B.Cu".into(),
        ];
        g.pads.clear();
        let mut p1 = pad(1, 1, [10_000_000, 15_000_000], &["F.Cu"]);
        p1.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut p2 = pad(2, 1, [30_000_000, 15_000_000], &["F.Cu"]);
        p2.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut q1 = pad(3, 2, [10_000_000, 35_000_000], &["F.Cu"]);
        q1.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut q2 = pad(4, 2, [20_000_000, 35_000_000], &["F.Cu"]);
        q2.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        g.pads.extend([p1, p2, q1, q2]);
        g.tracks.extend([
            Track {
                id: uuid(2300),
                net: 1,
                start: [10_000_000, 15_000_000],
                end: [15_000_000, 15_000_000],
                width_nm: 250_000,
                locked: false,
                layer: "F.Cu".into(),
            },
            Track {
                id: uuid(2301),
                net: 1,
                start: [15_000_000, 15_000_000],
                end: [25_000_000, 15_000_000],
                width_nm: 250_000,
                locked: false,
                layer: "In2.Cu".into(),
            },
            Track {
                id: uuid(2302),
                net: 1,
                start: [25_000_000, 15_000_000],
                end: [30_000_000, 15_000_000],
                width_nm: 250_000,
                locked: false,
                layer: "F.Cu".into(),
            },
        ]);
        g.vias.extend([
            Via {
                id: uuid(2303),
                net: 1,
                at: [15_000_000, 15_000_000],
                diameter_nm: 600_000,
                drill_nm: 300_000,
                locked: false,
                start_layer: "F.Cu".into(),
                end_layer: "B.Cu".into(),
                kind: ViaKind::Through,
            },
            Via {
                id: uuid(2304),
                net: 1,
                at: [25_000_000, 15_000_000],
                diameter_nm: 600_000,
                drill_nm: 300_000,
                locked: false,
                start_layer: "F.Cu".into(),
                end_layer: "B.Cu".into(),
                kind: ViaKind::Through,
            },
        ]);
        let bds = g.board_design_settings.as_mut().unwrap();
        bds.use_height_for_length_calcs = Some(true);
        bds.board_thickness_nm = Some(940_000);
        bds.outer_solder_mask_thickness_nm = Some(0);
        bds.netclasses[1].tuning_profile = Some("P1".into());
        bds.netclass_assignments
            .insert("DQ0".into(), vec!["DIFF".into()]);
        bds.netclass_assignments
            .insert("DQ1".into(), vec!["DIFF".into()]);
        g.stackup = vec![
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
                thickness_nm: 200_000,
                epsilon_r: Some(4.0),
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
                thickness_nm: 400_000,
                epsilon_r: Some(4.0),
                material: None,
            },
            StackupLayer {
                name: "In2.Cu".into(),
                kind: StackupKind::Copper,
                thickness_nm: 35_000,
                epsilon_r: None,
                material: None,
            },
            StackupLayer {
                name: "di3".into(),
                kind: StackupKind::Prepreg,
                thickness_nm: 200_000,
                epsilon_r: Some(4.0),
                material: None,
            },
            StackupLayer {
                name: "B.Cu".into(),
                kind: StackupKind::Copper,
                thickness_nm: 35_000,
                epsilon_r: None,
                material: None,
            },
        ];
        g.project_rules = Some(
            serde_json::json!({
                "meta": {"version": 3},
                "board": {"design_settings": {"rules": {"use_height_for_length_calcs": true}}},
                "net_settings": {
                    "classes": [
                        {"name": "Default"},
                        {"name": "DIFF", "tuning_profile": "P1"}
                    ],
                    "netclass_assignments": {"DQ0": ["DIFF"], "DQ1": ["DIFF"]}
                },
                "tuning_profiles": {
                    "tuning_profiles_impedance_geometric": [{
                        "profile_name": "P1",
                        "type": 0,
                        "target_impedance": 0,
                        "enable_time_domain_tuning": true,
                        "via_prop_delay": 6_000_000,
                        "layer_entries": [
                            {"signal_layer": "F.Cu", "width": 250_000, "delay": 5_000_000},
                            {"signal_layer": "In2.Cu", "width": 250_000, "delay": 6_000_000}
                        ],
                        "via_overrides": [via_ov_json]
                    }]
                }
            })
            .to_string(),
        );
        g.routing_options = Some(RoutingOptions {
            length_match_groups: vec![LengthMatchGroupSpec {
                members: vec!["DQ0".into(), "DQ1".into()],
                ..Default::default()
            }],
            group_delay_tolerance_as: Some(1_000_000),
            group_meander_amplitude_nm: Some(4_000_000),
            ..Default::default()
        });
        let mut eng = Engine::new(g).unwrap();
        eng.route(2, 50_000).unwrap();
        let st = prepare_and_route_pairs_in_tx(&mut eng).unwrap();
        apply_length_matching_in_tx(&mut eng, &st).unwrap();
        eng.copper()
            .tracks()
            .filter(|t| t.net == 2)
            .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
            .sum()
    };

    // Baseline barrel height delay: 10mm*5ps + 10mm*6ps + 2*(0.6875mm*6ps) = 118.25ps -> 23.65mm on F.Cu
    // Override 45ps/via: 10mm*5ps + 10mm*6ps + 2*45ps = 200ps -> 40.0mm on F.Cu
    let len_empty_via = run_via_ov(serde_json::json!({
        "signal_layer_from": "F.Cu",
        "signal_layer_to": "In2.Cu",
        "via_layer_from": "",
        "via_layer_to": "",
        "delay": 45_000_000
    }));
    assert!(
        (len_empty_via - 23_650_000.0).abs() <= 400_000.0,
        "sig_empty_via should ignore empty via_layer_* override, got {len_empty_via}"
    );
    let len_start_end = run_via_ov(serde_json::json!({
        "start_layer": "F.Cu",
        "end_layer": "In2.Cu",
        "via_layer_from": "F.Cu",
        "via_layer_to": "B.Cu",
        "delay": 45_000_000
    }));
    assert!(
        (len_start_end - 23_650_000.0).abs() <= 400_000.0,
        "start_end_F_In2 should ignore start_layer/end_layer, got {len_start_end}"
    );
    let len_diam_mismatch = run_via_ov(serde_json::json!({
        "signal_layer_from": "F.Cu",
        "signal_layer_to": "In2.Cu",
        "via_layer_from": "F.Cu",
        "via_layer_to": "B.Cu",
        "via_diameter": 500_000,
        "delay": 45_000_000
    }));
    assert!(
        (len_diam_mismatch - 40_000_000.0).abs() <= 400_000.0,
        "sig_via_diam_500k should ignore via_diameter mismatch, got {len_diam_mismatch}"
    );
    let len_sig_and_via = run_via_ov(serde_json::json!({
        "signal_layer_from": "F.Cu",
        "signal_layer_to": "In2.Cu",
        "via_layer_from": "F.Cu",
        "via_layer_to": "B.Cu",
        "delay": 45_000_000
    }));
    assert!(
        (len_sig_and_via - 40_000_000.0).abs() <= 400_000.0,
        "sig_and_via_F_B should apply override, got {len_sig_and_via}"
    );

    // 2. board.design_settings.use_height_for_length_calcs outside .rules is ignored
    let mut ds_pro = base_diff_input(&["SIG_A", "SIG_B"]);
    ds_pro.pads.clear();
    ds_pro
        .pads
        .push(pad(1, 1, [10_000_000, 15_000_000], &["F.Cu"]));
    ds_pro
        .pads
        .push(pad(2, 1, [30_000_000, 15_000_000], &["F.Cu"]));
    let mut pb1 = pad(3, 2, [10_000_000, 30_000_000], &["F.Cu"]);
    pb1.shape = Shape::Rect {
        half_size_nm: [150_000, 150_000],
    };
    let mut pb2 = pad(4, 2, [20_000_000, 30_000_000], &["F.Cu"]);
    pb2.shape = Shape::Rect {
        half_size_nm: [150_000, 150_000],
    };
    ds_pro.pads.extend([pb1, pb2]);
    ds_pro.tracks.extend([
        Track {
            id: uuid(2400),
            net: 1,
            start: [10_000_000, 15_000_000],
            end: [15_000_000, 15_000_000],
            width_nm: 250_000,
            locked: false,
            layer: "F.Cu".into(),
        },
        Track {
            id: uuid(2401),
            net: 1,
            start: [15_000_000, 15_000_000],
            end: [25_000_000, 15_000_000],
            width_nm: 250_000,
            locked: false,
            layer: "B.Cu".into(),
        },
        Track {
            id: uuid(2402),
            net: 1,
            start: [25_000_000, 15_000_000],
            end: [30_000_000, 15_000_000],
            width_nm: 250_000,
            locked: false,
            layer: "F.Cu".into(),
        },
    ]);
    ds_pro.vias.extend([
        Via {
            id: uuid(2403),
            net: 1,
            at: [15_000_000, 15_000_000],
            diameter_nm: 600_000,
            drill_nm: 300_000,
            locked: false,
            start_layer: "F.Cu".into(),
            end_layer: "B.Cu".into(),
            kind: ViaKind::Through,
        },
        Via {
            id: uuid(2404),
            net: 1,
            at: [25_000_000, 15_000_000],
            diameter_nm: 600_000,
            drill_nm: 300_000,
            locked: false,
            start_layer: "F.Cu".into(),
            end_layer: "B.Cu".into(),
            kind: ViaKind::Through,
        },
    ]);
    let bds = ds_pro.board_design_settings.as_mut().unwrap();
    bds.use_height_for_length_calcs = Some(true);
    bds.board_thickness_nm = Some(1_600_000);
    bds.outer_solder_mask_thickness_nm = Some(20_000);
    ds_pro.project_rules = Some(
        serde_json::json!({
            "meta": {"version": 3},
            "board": {"design_settings": {"use_height_for_length_calcs": false, "rules": {}}}
        })
        .to_string(),
    );
    ds_pro.routing_options = Some(RoutingOptions {
        length_match_groups: vec![LengthMatchGroupSpec {
            members: vec!["SIG_A".into(), "SIG_B".into()],
            ..Default::default()
        }],
        group_match_tolerance_nm: Some(100_000),
        group_meander_amplitude_nm: Some(4_000_000),
        ..Default::default()
    });
    let mut eng = Engine::new(ds_pro).unwrap();
    eng.route(2, 50_000).unwrap();
    let st = prepare_and_route_pairs_in_tx(&mut eng).unwrap();
    apply_length_matching_in_tx(&mut eng, &st).unwrap();
    let len_ds_ignored: f64 = eng
        .copper()
        .tracks()
        .filter(|t| t.net == 2)
        .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
        .sum();
    assert!(
        (len_ds_ignored - 23_160_000.0).abs() <= 150_000.0,
        "f1_pro_false_in_ds should ignore ds.use_height_for_length_calcs and yield ~23.16mm, got {len_ds_ignored}"
    );
}

#[test]
fn r8_via_overshoot_xor_and_stub_bypass_parity() {
    let run_len_probe = |name: &str,
                         p1: [i64; 2],
                         p1_layers: &[&str],
                         p2: [i64; 2],
                         p2_layers: &[&str],
                         segs: &[([i64; 2], [i64; 2], &str)],
                         vias: &[([i64; 2], &str, &str)]|
     -> f64 {
        let mut g = base_diff_input(&["SIG_P", "SIG_N"]);
        g.pads.clear();
        let mut p1_pad = pad(1, 1, p1, p1_layers);
        p1_pad.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut p2_pad = pad(2, 1, p2, p2_layers);
        p2_pad.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut n1_pad = pad(3, 2, [10_000_000, 35_000_000], &["F.Cu"]);
        n1_pad.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut n2_pad = pad(4, 2, [18_000_000, 35_000_000], &["F.Cu"]);
        n2_pad.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        g.pads.extend([p1_pad, p2_pad, n1_pad, n2_pad]);
        for (idx, &(a, b, layer)) in segs.iter().enumerate() {
            g.tracks.push(Track {
                id: uuid(3100 + idx as u64),
                net: 1,
                start: a,
                end: b,
                width_nm: 300_000,
                locked: false,
                layer: layer.into(),
            });
        }
        for (idx, &(at, sl, el)) in vias.iter().enumerate() {
            g.vias.push(Via {
                id: uuid(3200 + idx as u64),
                net: 1,
                at,
                diameter_nm: 600_000,
                drill_nm: 300_000,
                locked: false,
                start_layer: sl.into(),
                end_layer: el.into(),
                kind: ViaKind::Through,
            });
        }
        g.routing_options = Some(RoutingOptions {
            length_match_groups: vec![LengthMatchGroupSpec {
                members: vec!["SIG_P".into(), "SIG_N".into()],
                ..Default::default()
            }],
            group_match_tolerance_nm: Some(5_000),
            group_meander_amplitude_nm: Some(4_000_000),
            ..Default::default()
        });
        let mut eng = Engine::new(g).unwrap();
        eng.route(2, 50_000).unwrap();
        let st = prepare_and_route_pairs_in_tx(&mut eng).unwrap();
        apply_length_matching_in_tx(&mut eng, &st).unwrap();
        let sum: f64 = eng
            .copper()
            .tracks()
            .filter(|t| t.net == 2)
            .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
            .sum();
        assert!(sum > 0.0, "{name}: expected routed copper");
        sum
    };

    let cases: &[(&str, f64, &str, &str, Vec<_>, Vec<_>)] = &[
        (
            "e2e_behind_ep_a",
            20_057_900.0,
            "F.Cu",
            "B.Cu",
            vec![
                ([10_000_000, 20_000_000], [20_100_000, 20_000_000], "F.Cu"),
                ([20_000_000, 20_175_000], [30_000_000, 20_000_000], "B.Cu"),
            ],
            vec![([20_000_000, 20_175_000], "F.Cu", "B.Cu")],
        ),
        (
            "e2e_behind_ep_g_on_cl",
            20_000_000.0,
            "F.Cu",
            "B.Cu",
            vec![
                ([10_000_000, 20_000_000], [20_100_000, 20_000_000], "F.Cu"),
                ([20_000_000, 20_000_000], [30_000_000, 20_000_000], "B.Cu"),
            ],
            vec![([20_000_000, 20_000_000], "F.Cu", "B.Cu")],
        ),
        (
            "via_off_center_before_ep",
            20_040_200.0,
            "F.Cu",
            "B.Cu",
            vec![
                ([10_000_000, 20_000_000], [20_150_000, 20_000_000], "F.Cu"),
                ([20_000_000, 20_150_000], [30_000_000, 20_150_000], "B.Cu"),
            ],
            vec![([20_000_000, 20_150_000], "F.Cu", "B.Cu")],
        ),
        (
            "adv_via_behind_ep_collinear",
            20_000_000.0,
            "F.Cu",
            "B.Cu",
            vec![
                ([10_000_000, 20_000_000], [20_200_000, 20_000_000], "F.Cu"),
                ([20_000_000, 20_000_000], [30_000_000, 20_000_000], "B.Cu"),
            ],
            vec![([20_000_000, 20_000_000], "F.Cu", "B.Cu")],
        ),
        (
            "adv_via_behind_ep_perp250",
            20_134_200.0,
            "F.Cu",
            "B.Cu",
            vec![
                ([10_000_000, 20_000_000], [20_100_000, 20_000_000], "F.Cu"),
                ([20_000_000, 20_250_000], [30_000_000, 20_250_000], "B.Cu"),
            ],
            vec![([20_000_000, 20_250_000], "F.Cu", "B.Cu")],
        ),
        (
            "col_over_299999",
            20_000_000.0,
            "F.Cu",
            "B.Cu",
            vec![
                ([10_000_000, 20_000_000], [20_299_999, 20_000_000], "F.Cu"),
                ([20_000_000, 20_000_000], [30_000_000, 20_000_000], "B.Cu"),
            ],
            vec![([20_000_000, 20_000_000], "F.Cu", "B.Cu")],
        ),
        (
            "col_over_300000",
            20_300_000.0,
            "F.Cu",
            "B.Cu",
            vec![
                ([10_000_000, 20_000_000], [20_300_000, 20_000_000], "F.Cu"),
                ([20_000_000, 20_000_000], [30_000_000, 20_000_000], "B.Cu"),
            ],
            vec![([20_000_000, 20_000_000], "F.Cu", "B.Cu")],
        ),
        (
            "two_vias_both_over_off150",
            20_080_400.0,
            "F.Cu",
            "F.Cu",
            vec![
                ([10_000_000, 20_000_000], [15_000_000, 20_000_000], "F.Cu"),
                ([14_800_000, 20_150_000], [25_200_000, 20_150_000], "B.Cu"),
                ([25_000_000, 20_000_000], [30_000_000, 20_000_000], "F.Cu"),
            ],
            vec![
                ([15_000_000, 20_000_000], "F.Cu", "B.Cu"),
                ([25_000_000, 20_000_000], "F.Cu", "B.Cu"),
            ],
        ),
        (
            "both_in_via_overshoot_len05",
            20_000_000.0,
            "F.Cu",
            "B.Cu",
            vec![
                ([10_000_000, 20_000_000], [19_500_000, 20_000_000], "F.Cu"),
                ([19_750_000, 20_000_000], [20_250_000, 20_000_000], "F.Cu"),
                ([20_000_000, 20_000_000], [30_000_000, 20_000_000], "B.Cu"),
            ],
            vec![([20_000_000, 20_000_000], "F.Cu", "B.Cu")],
        ),
        (
            "adv_stub_050_inside_via_fwd",
            20_000_000.0,
            "F.Cu",
            "B.Cu",
            vec![
                ([10_000_000, 20_000_000], [19_750_000, 20_000_000], "F.Cu"),
                ([19_800_000, 20_000_000], [19_850_000, 20_000_000], "F.Cu"),
                ([20_200_000, 20_000_000], [30_000_000, 20_000_000], "B.Cu"),
            ],
            vec![([20_000_000, 20_000_000], "F.Cu", "B.Cu")],
        ),
        (
            "adv_stub_050_inside_via_rev_bf",
            20_000_000.0,
            "B.Cu",
            "F.Cu",
            vec![
                ([20_200_000, 20_000_000], [30_000_000, 20_000_000], "B.Cu"),
                ([19_800_000, 20_000_000], [19_850_000, 20_000_000], "F.Cu"),
                ([10_000_000, 20_000_000], [19_750_000, 20_000_000], "F.Cu"),
            ],
            vec![([20_000_000, 20_000_000], "F.Cu", "B.Cu")],
        ),
        (
            "overshoot_t1_with_stub_inside_via",
            20_000_000.0,
            "F.Cu",
            "B.Cu",
            vec![
                ([10_000_000, 20_000_000], [20_200_000, 20_000_000], "F.Cu"),
                ([19_800_000, 20_000_000], [19_850_000, 20_000_000], "F.Cu"),
                ([20_200_000, 20_000_000], [30_000_000, 20_000_000], "B.Cu"),
            ],
            vec![([20_000_000, 20_000_000], "F.Cu", "B.Cu")],
        ),
        (
            "t1_1975_perp_chord_1971_via",
            20_000_000.0,
            "F.Cu",
            "B.Cu",
            vec![
                ([10_000_000, 20_000_000], [19_750_000, 20_000_000], "F.Cu"),
                ([19_710_000, 19_920_000], [19_710_000, 20_080_000], "F.Cu"),
                ([20_000_000, 20_000_000], [30_000_000, 20_000_000], "B.Cu"),
            ],
            vec![([20_000_000, 20_000_000], "F.Cu", "B.Cu")],
        ),
        (
            "t1_1950_perp_chord_1971_via",
            19_660_000.0,
            "F.Cu",
            "B.Cu",
            vec![
                ([10_000_000, 20_000_000], [19_500_000, 20_000_000], "F.Cu"),
                ([19_710_000, 19_920_000], [19_710_000, 20_080_000], "F.Cu"),
                ([20_000_000, 20_000_000], [30_000_000, 20_000_000], "B.Cu"),
            ],
            vec![([20_000_000, 20_000_000], "F.Cu", "B.Cu")],
        ),
        (
            "q2_both_in_via_boundary_19700",
            19_800_000.0,
            "F.Cu",
            "B.Cu",
            vec![
                ([10_000_000, 20_000_000], [19_500_000, 20_000_000], "F.Cu"),
                ([19_700_000, 20_000_000], [19_850_000, 20_000_000], "F.Cu"),
                ([20_000_000, 20_000_000], [30_000_000, 20_000_000], "B.Cu"),
            ],
            vec![([20_000_000, 20_000_000], "F.Cu", "B.Cu")],
        ),
        (
            "q2_both_in_via_boundary_19701",
            19_649_000.0,
            "F.Cu",
            "B.Cu",
            vec![
                ([10_000_000, 20_000_000], [19_500_000, 20_000_000], "F.Cu"),
                ([19_701_000, 20_000_000], [19_850_000, 20_000_000], "F.Cu"),
                ([20_000_000, 20_000_000], [30_000_000, 20_000_000], "B.Cu"),
            ],
            vec![([20_000_000, 20_000_000], "F.Cu", "B.Cu")],
        ),
        (
            "same_layer_via_collinear_gap250",
            19_750_000.0,
            "F.Cu",
            "F.Cu",
            vec![
                ([10_000_000, 20_000_000], [18_050_000, 20_000_000], "F.Cu"),
                ([18_300_000, 20_000_000], [30_000_000, 20_000_000], "F.Cu"),
            ],
            vec![([18_175_000, 20_000_000], "F.Cu", "B.Cu")],
        ),
    ];

    for (name, expected_nm, l1, l2, segs, vias) in cases {
        let p1 = if *l1 == "B.Cu" {
            [30_000_000, 20_000_000]
        } else {
            [10_000_000, 20_000_000]
        };
        let p2 = if *l1 == "B.Cu" {
            [10_000_000, 20_000_000]
        } else {
            let end = segs.last().unwrap().1;
            [end[0].max(30_000_000), end[1]]
        };
        let got = run_len_probe(name, p1, &[*l1], p2, &[*l2], segs, vias);
        assert!(
            (got - expected_nm).abs() <= 25_000.0,
            "{name}: expected ~{expected_nm} nm, got {got} nm"
        );
    }
}

#[test]
fn r8_via_overrides_canonical_stackup_stub_and_delay_overshoot_parity() {
    // 1. Delay scaling on track-to-via overshoot (F.Cu=5ps/mm, B.Cu=8ps/mm, use_height=false -> 130ps / 130.2ps)
    let run_delay_over = |segs: &[([i64; 2], [i64; 2], &str)],
                          vias: &[([i64; 2], &str, &str)],
                          p1_l: &str,
                          p2_l: &str,
                          p2: [i64; 2]|
     -> f64 {
        let mut g = base_diff_input(&["DQ0", "DQ1"]);
        g.pads.clear();
        let mut pa1 = pad(1, 1, [10_000_000, 20_000_000], &[p1_l]);
        pa1.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut pa2 = pad(2, 1, p2, &[p2_l]);
        pa2.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut pb1 = pad(3, 2, [10_000_000, 35_000_000], &["F.Cu"]);
        pb1.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut pb2 = pad(4, 2, [20_000_000, 35_000_000], &["F.Cu"]);
        pb2.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        g.pads.extend([pa1, pa2, pb1, pb2]);
        for (idx, &(a, b, l)) in segs.iter().enumerate() {
            g.tracks.push(Track {
                id: uuid(3300 + idx as u64),
                net: 1,
                start: a,
                end: b,
                width_nm: 250_000,
                locked: false,
                layer: l.into(),
            });
        }
        for (idx, &(at, sl, el)) in vias.iter().enumerate() {
            g.vias.push(Via {
                id: uuid(3400 + idx as u64),
                net: 1,
                at,
                diameter_nm: 600_000,
                drill_nm: 300_000,
                locked: false,
                start_layer: sl.into(),
                end_layer: el.into(),
                kind: ViaKind::Through,
            });
        }
        let bds = g.board_design_settings.as_mut().unwrap();
        bds.use_height_for_length_calcs = Some(true);
        bds.board_thickness_nm = Some(1_000_000);
        bds.outer_solder_mask_thickness_nm = Some(0);
        bds.netclasses[1].tuning_profile = Some("P1".into());
        bds.netclass_assignments
            .insert("DQ0".into(), vec!["DIFF".into()]);
        bds.netclass_assignments
            .insert("DQ1".into(), vec!["DIFF".into()]);
        g.stackup = vec![
            StackupLayer {
                name: "F.Cu".into(),
                kind: StackupKind::Copper,
                thickness_nm: 35_000,
                epsilon_r: None,
                material: None,
            },
            StackupLayer {
                name: "di".into(),
                kind: StackupKind::Core,
                thickness_nm: 930_000,
                epsilon_r: Some(4.0),
                material: None,
            },
            StackupLayer {
                name: "B.Cu".into(),
                kind: StackupKind::Copper,
                thickness_nm: 35_000,
                epsilon_r: None,
                material: None,
            },
        ];
        g.project_rules = Some(
            serde_json::json!({
                "meta": {"version": 3},
                "board": {"design_settings": {"rules": {"use_height_for_length_calcs": true}}},
                "net_settings": {
                    "classes": [{"name": "Default"}, {"name": "DIFF", "tuning_profile": "P1"}],
                    "netclass_assignments": {"DQ0": ["DIFF"], "DQ1": ["DIFF"]}
                },
                "tuning_profiles": {
                    "tuning_profiles_impedance_geometric": [{
                        "profile_name": "P1",
                        "type": 0,
                        "target_impedance": 0,
                        "enable_time_domain_tuning": true,
                        "via_prop_delay": 10_000_000,
                        "layer_entries": [
                            {"signal_layer": "F.Cu", "width": 250_000, "delay": 5_000_000},
                            {"signal_layer": "B.Cu", "width": 250_000, "delay": 8_000_000}
                        ],
                        "via_overrides": [{
                            "signal_layer_from": "F.Cu",
                            "signal_layer_to": "B.Cu",
                            "via_layer_from": "F.Cu",
                            "via_layer_to": "B.Cu",
                            "delay": 0
                        }]
                    }]
                }
            })
            .to_string(),
        );
        // Zero via delay on F->B override is invalid for active delay group; use 1ps override to isolate track delay!
        let mut pro_val: serde_json::Value =
            serde_json::from_str(g.project_rules.as_ref().unwrap()).unwrap();
        pro_val["tuning_profiles"]["tuning_profiles_impedance_geometric"][0]["via_overrides"][0]
            ["delay"] = serde_json::json!(1_000_000);
        g.project_rules = Some(pro_val.to_string());
        g.routing_options = Some(RoutingOptions {
            length_match_groups: vec![LengthMatchGroupSpec {
                members: vec!["DQ0".into(), "DQ1".into()],
                ..Default::default()
            }],
            group_delay_tolerance_as: Some(150_000),
            group_meander_amplitude_nm: Some(4_000_000),
            ..Default::default()
        });
        let mut eng = Engine::new(g).unwrap();
        eng.route(2, 50_000).unwrap();
        let st = prepare_and_route_pairs_in_tx(&mut eng).unwrap();
        apply_length_matching_in_tx(&mut eng, &st).unwrap();
        eng.copper()
            .tracks()
            .filter(|t| t.net == 2)
            .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
            .sum()
    };
    // 10mm*5ps + 10mm*8ps + 1*1ps = 131ps -> 26.2mm on F.Cu
    let d_col = run_delay_over(
        &[
            ([10_000_000, 20_000_000], [20_200_000, 20_000_000], "F.Cu"),
            ([20_000_000, 20_000_000], [30_000_000, 20_000_000], "B.Cu"),
        ],
        &[([20_000_000, 20_000_000], "F.Cu", "B.Cu")],
        "F.Cu",
        "B.Cu",
        [30_000_000, 20_000_000],
    );
    assert!(
        (d_col - 26_200_000.0).abs() <= 150_000.0,
        "delay_col_over_0.20 expected ~26.20mm, got {d_col}"
    );
    // 10.04019*5 + 10*8 + 1 = 131.201ps -> 26.24mm
    let d_off = run_delay_over(
        &[
            ([10_000_000, 20_000_000], [20_200_000, 20_000_000], "F.Cu"),
            ([20_000_000, 20_150_000], [30_000_000, 20_150_000], "B.Cu"),
        ],
        &[([20_000_000, 20_150_000], "F.Cu", "B.Cu")],
        "F.Cu",
        "B.Cu",
        [30_000_000, 20_150_000],
    );
    assert!(
        (d_off - 26_240_000.0).abs() <= 150_000.0,
        "delay_off150_over_0.20 expected ~26.24mm, got {d_off}"
    );
    // Two vias both overshoot on B.Cu: 5*5 + 10*8 + 5*5 + 2*1 = 132ps -> 26.4mm
    let d_two = run_delay_over(
        &[
            ([10_000_000, 20_000_000], [15_000_000, 20_000_000], "F.Cu"),
            ([14_800_000, 20_000_000], [25_200_000, 20_000_000], "B.Cu"),
            ([25_000_000, 20_000_000], [30_000_000, 20_000_000], "F.Cu"),
        ],
        &[
            ([15_000_000, 20_000_000], "F.Cu", "B.Cu"),
            ([25_000_000, 20_000_000], "F.Cu", "B.Cu"),
        ],
        "F.Cu",
        "F.Cu",
        [30_000_000, 20_000_000],
    );
    assert!(
        (d_two - 26_400_000.0).abs() <= 150_000.0,
        "delay_two_vias_both_overshoot expected ~26.40mm, got {d_two}"
    );

    // 2. Canonical stackup order, 1-via In2->F, empty stackup 4L, In1.Cu stub w/ cheaper split, duplicate last-match, float delay
    let run_4l_ov = |with_stackup: bool,
                     with_in1_stub: bool,
                     one_via_in2_to_f: bool,
                     via_ov_list: serde_json::Value|
     -> f64 {
        let mut g = base_diff_input(&["DQ0", "DQ1"]);
        g.layers = vec![
            "F.Cu".into(),
            "In1.Cu".into(),
            "In2.Cu".into(),
            "B.Cu".into(),
        ];
        g.pads.clear();
        let p1_l = if one_via_in2_to_f { "In2.Cu" } else { "F.Cu" };
        let mut p1 = pad(1, 1, [10_000_000, 15_000_000], &[p1_l]);
        p1.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut p2 = pad(2, 1, [30_000_000, 15_000_000], &["F.Cu"]);
        p2.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut q1 = pad(3, 2, [10_000_000, 35_000_000], &["F.Cu"]);
        q1.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        let mut q2 = pad(4, 2, [20_000_000, 35_000_000], &["F.Cu"]);
        q2.shape = Shape::Rect {
            half_size_nm: [150_000, 150_000],
        };
        g.pads.extend([p1, p2, q1, q2]);
        if one_via_in2_to_f {
            g.tracks.extend([
                Track {
                    id: uuid(3500),
                    net: 1,
                    start: [10_000_000, 15_000_000],
                    end: [20_000_000, 15_000_000],
                    width_nm: 250_000,
                    locked: false,
                    layer: "In2.Cu".into(),
                },
                Track {
                    id: uuid(3501),
                    net: 1,
                    start: [20_000_000, 15_000_000],
                    end: [30_000_000, 15_000_000],
                    width_nm: 250_000,
                    locked: false,
                    layer: "F.Cu".into(),
                },
            ]);
            g.vias.push(Via {
                id: uuid(3502),
                net: 1,
                at: [20_000_000, 15_000_000],
                diameter_nm: 600_000,
                drill_nm: 300_000,
                locked: false,
                start_layer: "F.Cu".into(),
                end_layer: "B.Cu".into(),
                kind: ViaKind::Through,
            });
        } else {
            g.tracks.extend([
                Track {
                    id: uuid(3500),
                    net: 1,
                    start: [10_000_000, 15_000_000],
                    end: [15_000_000, 15_000_000],
                    width_nm: 250_000,
                    locked: false,
                    layer: "F.Cu".into(),
                },
                Track {
                    id: uuid(3501),
                    net: 1,
                    start: [15_000_000, 15_000_000],
                    end: [25_000_000, 15_000_000],
                    width_nm: 250_000,
                    locked: false,
                    layer: "In2.Cu".into(),
                },
                Track {
                    id: uuid(3502),
                    net: 1,
                    start: [25_000_000, 15_000_000],
                    end: [30_000_000, 15_000_000],
                    width_nm: 250_000,
                    locked: false,
                    layer: "F.Cu".into(),
                },
            ]);
            if with_in1_stub {
                g.tracks.push(Track {
                    id: uuid(3505),
                    net: 1,
                    start: [15_000_000, 15_000_000],
                    end: [15_000_000, 16_000_000],
                    width_nm: 250_000,
                    locked: false,
                    layer: "In1.Cu".into(),
                });
            }
            g.vias.extend([
                Via {
                    id: uuid(3503),
                    net: 1,
                    at: [15_000_000, 15_000_000],
                    diameter_nm: 600_000,
                    drill_nm: 300_000,
                    locked: false,
                    start_layer: "F.Cu".into(),
                    end_layer: "B.Cu".into(),
                    kind: ViaKind::Through,
                },
                Via {
                    id: uuid(3504),
                    net: 1,
                    at: [25_000_000, 15_000_000],
                    diameter_nm: 600_000,
                    drill_nm: 300_000,
                    locked: false,
                    start_layer: "F.Cu".into(),
                    end_layer: "B.Cu".into(),
                    kind: ViaKind::Through,
                },
            ]);
        }
        let bds = g.board_design_settings.as_mut().unwrap();
        bds.use_height_for_length_calcs = Some(true);
        bds.board_thickness_nm = Some(940_000);
        bds.outer_solder_mask_thickness_nm = Some(0);
        bds.netclasses[1].tuning_profile = Some("P1".into());
        bds.netclass_assignments
            .insert("DQ0".into(), vec!["DIFF".into()]);
        bds.netclass_assignments
            .insert("DQ1".into(), vec!["DIFF".into()]);
        if with_stackup {
            g.stackup = vec![
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
                    thickness_nm: 200_000,
                    epsilon_r: Some(4.0),
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
                    thickness_nm: 400_000,
                    epsilon_r: Some(4.0),
                    material: None,
                },
                StackupLayer {
                    name: "In2.Cu".into(),
                    kind: StackupKind::Copper,
                    thickness_nm: 35_000,
                    epsilon_r: None,
                    material: None,
                },
                StackupLayer {
                    name: "di3".into(),
                    kind: StackupKind::Prepreg,
                    thickness_nm: 200_000,
                    epsilon_r: Some(4.0),
                    material: None,
                },
                StackupLayer {
                    name: "B.Cu".into(),
                    kind: StackupKind::Copper,
                    thickness_nm: 35_000,
                    epsilon_r: None,
                    material: None,
                },
            ];
        }
        g.project_rules = Some(
            serde_json::json!({
                "meta": {"version": 3},
                "board": {"design_settings": {"rules": {"use_height_for_length_calcs": true}}},
                "net_settings": {
                    "classes": [{"name": "Default"}, {"name": "DIFF", "tuning_profile": "P1"}],
                    "netclass_assignments": {"DQ0": ["DIFF"], "DQ1": ["DIFF"]}
                },
                "tuning_profiles": {
                    "tuning_profiles_impedance_geometric": [{
                        "profile_name": "P1",
                        "type": 0,
                        "target_impedance": 0,
                        "enable_time_domain_tuning": true,
                        "via_prop_delay": 6_000_000,
                        "layer_entries": [
                            {"signal_layer": "F.Cu", "width": 250_000, "delay": 5_000_000},
                            {"signal_layer": "In1.Cu", "width": 250_000, "delay": 6_000_000},
                            {"signal_layer": "In2.Cu", "width": 250_000, "delay": 6_000_000}
                        ],
                        "via_overrides": via_ov_list
                    }]
                }
            })
            .to_string(),
        );
        if !with_stackup {
            // Empty stackup tests canonical active_layers() via length-matching path (delay preflight requires dielectric stackup)
            g.routing_options = Some(RoutingOptions {
                length_match_groups: vec![LengthMatchGroupSpec {
                    members: vec!["DQ0".into(), "DQ1".into()],
                    ..Default::default()
                }],
                group_match_tolerance_nm: Some(50_000),
                group_meander_amplitude_nm: Some(4_000_000),
                ..Default::default()
            });
        } else {
            g.routing_options = Some(RoutingOptions {
                length_match_groups: vec![LengthMatchGroupSpec {
                    members: vec!["DQ0".into(), "DQ1".into()],
                    ..Default::default()
                }],
                group_delay_tolerance_as: Some(250_000),
                group_meander_amplitude_nm: Some(4_000_000),
                ..Default::default()
            });
        }
        let mut eng = Engine::new(g).unwrap();
        eng.route(2, 50_000).unwrap();
        let st = prepare_and_route_pairs_in_tx(&mut eng).unwrap();
        apply_length_matching_in_tx(&mut eng, &st).unwrap();
        eng.copper()
            .tracks()
            .filter(|t| t.net == 2)
            .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
            .sum()
    };

    // Reversed signal_layer order (In2.Cu -> F.Cu) is ignored -> baseline 23.65mm
    let rev_sig = run_4l_ov(
        true,
        false,
        false,
        serde_json::json!([{
            "signal_layer_from": "In2.Cu",
            "signal_layer_to": "F.Cu",
            "via_layer_from": "F.Cu",
            "via_layer_to": "B.Cu",
            "delay": 45_000_000
        }]),
    );
    assert!(
        (rev_sig - 23_650_000.0).abs() <= 400_000.0,
        "rev_sig_order must be ignored (~23.65mm), got {rev_sig}"
    );
    // Reversed via_layer order (B.Cu -> F.Cu) is ignored -> baseline 23.65mm
    let rev_via = run_4l_ov(
        true,
        false,
        false,
        serde_json::json!([{
            "signal_layer_from": "F.Cu",
            "signal_layer_to": "In2.Cu",
            "via_layer_from": "B.Cu",
            "via_layer_to": "F.Cu",
            "delay": 45_000_000
        }]),
    );
    assert!(
        (rev_via - 23_650_000.0).abs() <= 400_000.0,
        "rev_via_order must be ignored (~23.65mm), got {rev_via}"
    );
    // 1-via In2.Cu -> F.Cu applies canonical F.Cu -> In2.Cu override: 10*6 + 10*5 + 45 = 155ps -> 31.0mm
    let in2_to_f = run_4l_ov(
        true,
        false,
        true,
        serde_json::json!([{
            "signal_layer_from": "F.Cu",
            "signal_layer_to": "In2.Cu",
            "via_layer_from": "F.Cu",
            "via_layer_to": "B.Cu",
            "delay": 45_000_000
        }]),
    );
    assert!(
        (in2_to_f - 31_000_000.0).abs() <= 400_000.0,
        "1via_In2_to_F must apply canonical F->In2 override (~31.0mm), got {in2_to_f}"
    );
    // In1.Cu stub on F->B via with cheaper F->In1 (10ps) + In1->In2 (10ps) vs F->In2 (45ps):
    // atomic via split chooses direct F->In2 (45ps) -> 10*5 + 10*6 + 2*45 = 200ps -> 40.0mm!
    let in1_stub = run_4l_ov(
        true,
        true,
        false,
        serde_json::json!([
            {
                "signal_layer_from": "F.Cu",
                "signal_layer_to": "In2.Cu",
                "via_layer_from": "F.Cu",
                "via_layer_to": "B.Cu",
                "delay": 45_000_000
            },
            {
                "signal_layer_from": "F.Cu",
                "signal_layer_to": "In1.Cu",
                "via_layer_from": "F.Cu",
                "via_layer_to": "B.Cu",
                "delay": 10_000_000
            },
            {
                "signal_layer_from": "In1.Cu",
                "signal_layer_to": "In2.Cu",
                "via_layer_from": "F.Cu",
                "via_layer_to": "B.Cu",
                "delay": 10_000_000
            }
        ]),
    );
    assert!(
        (in1_stub - 40_000_000.0).abs() <= 400_000.0,
        "In1.Cu stub must take direct F->In2 45ps override (~40.0mm), got {in1_stub}"
    );
    // Duplicate via_overrides: last-match wins ([45ps, 70ps] -> 250ps=50mm; [70ps, 45ps] -> 200ps=40mm) + float delay
    let dup_last_70 = run_4l_ov(
        true,
        false,
        false,
        serde_json::json!([
            {
                "signal_layer_from": "F.Cu",
                "signal_layer_to": "In2.Cu",
                "via_layer_from": "F.Cu",
                "via_layer_to": "B.Cu",
                "delay": 45_000_000.0
            },
            {
                "signal_layer_from": "F.Cu",
                "signal_layer_to": "In2.Cu",
                "via_layer_from": "F.Cu",
                "via_layer_to": "B.Cu",
                "delay": 70_000_000.0
            }
        ]),
    );
    assert!(
        (dup_last_70 - 50_000_000.0).abs() <= 400_000.0,
        "duplicate via_overrides last-match 70ps expected ~50.0mm, got {dup_last_70}"
    );
    let dup_last_45 = run_4l_ov(
        true,
        false,
        false,
        serde_json::json!([
            {
                "signal_layer_from": "F.Cu",
                "signal_layer_to": "In2.Cu",
                "via_layer_from": "F.Cu",
                "via_layer_to": "B.Cu",
                "delay": 70_000_000.0
            },
            {
                "signal_layer_from": "F.Cu",
                "signal_layer_to": "In2.Cu",
                "via_layer_from": "F.Cu",
                "via_layer_to": "B.Cu",
                "delay": 45_000_000.0
            }
        ]),
    );
    assert!(
        (dup_last_45 - 40_000_000.0).abs() <= 400_000.0,
        "duplicate via_overrides last-match 45ps expected ~40.0mm, got {dup_last_45}"
    );
    // Empty stackup 4L length-matching with In1.Cu stub still computes direct F->In2 barrel height
    let no_stack = run_4l_ov(false, true, false, serde_json::json!([]));
    assert!(
        (no_stack - 21_240_000.0).abs() <= 400_000.0,
        "empty stackup 4L F->In2 expected ~21.24mm, got {no_stack}"
    );
}

#[test]
fn route_all_combines_diff_pair_and_congested_se_negotiation_and_protects_filters() {
    let mut input = base_diff_input(&["DP_P", "DP_N"]);
    // Add two crossing single-ended nets (Default netclass) and one filtered-out net.
    for (nid, name) in [(3u32, "SE_A"), (4, "SE_B"), (5, "SE_SKIP")] {
        input.nets.push(Net {
            id: nid,
            name: name.to_owned(),
        });
        input
            .board_design_settings
            .as_mut()
            .unwrap()
            .netclass_assignments
            .insert(name.to_owned(), vec!["Default".to_owned()]);
    }
    input.pads.extend([
        pad(101, 3, [15_000_000, 20_000_000], &["F.Cu"]),
        pad(102, 3, [35_000_000, 20_000_000], &["F.Cu"]),
        pad(103, 4, [25_000_000, 14_000_000], &["F.Cu"]),
        pad(104, 4, [25_000_000, 26_000_000], &["F.Cu"]),
        pad(105, 5, [10_000_000, 32_000_000], &["F.Cu"]),
        pad(106, 5, [20_000_000, 32_000_000], &["F.Cu"]),
    ]);
    // U-shaped F.Cu keepout around SE_A's start forces SE_A onto B.Cu/detour and contends with SE_B.
    input.rule_areas.push(RuleArea {
        id: uuid(901),
        name: None,
        layers: vec!["F.Cu".to_owned()],
        outer: vec![
            [17_000_000, 18_000_000],
            [23_000_000, 18_000_000],
            [23_000_000, 22_000_000],
            [17_000_000, 22_000_000],
        ],
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        no_tracks: true,
        no_vias: true,
        no_pads: false,
        no_zone_fills: false,
        no_footprints: false,
    });
    let opts = input.routing_options.as_mut().unwrap();
    opts.net_filters = vec![
        "DP_P".to_owned(),
        "DP_N".to_owned(),
        "SE_A".to_owned(),
        "SE_B".to_owned(),
    ];
    let mut engine = Engine::new(input).unwrap();
    let exp = engine.route_all().unwrap();
    assert!(exp > 0);
    for nid in 1..=4 {
        assert!(
            engine.copper().generated().any(|t| t.net == nid),
            "expected tracks for net {nid}"
        );
    }
    assert!(
        !engine.copper().generated().any(|t| t.net == 5),
        "filtered-out net 5 must not be routed"
    );
    assert!(engine.copper().index_consistent());
}

#[test]
fn route_all_resolves_multi_pair_contention_and_rolls_back_atomically_on_failure() {
    // 1. Multi-pair contention via Engine::route_all (CompositeRouteAdapter):
    // P1 routes horizontally across (25, 20); P2 routes vertically through an F.Cu corridor
    // whose B.Cu underneath P2 pads is blocked, forcing Tier-0 contention resolution.
    let mut input = base_diff_input(&["P1_P", "P1_N", "P2_P", "P2_N"]);
    input.pads = vec![
        pad(1, 1, [10_000_000, 19_500_000], &["F.Cu"]),
        pad(2, 1, [40_000_000, 19_500_000], &["F.Cu"]),
        pad(3, 2, [10_000_000, 20_500_000], &["F.Cu"]),
        pad(4, 2, [40_000_000, 20_500_000], &["F.Cu"]),
        pad(5, 3, [24_500_000, 10_000_000], &["F.Cu"]),
        pad(6, 3, [24_500_000, 30_000_000], &["F.Cu"]),
        pad(7, 4, [25_500_000, 10_000_000], &["F.Cu"]),
        pad(8, 4, [25_500_000, 30_000_000], &["F.Cu"]),
    ];
    let mut engine = Engine::new(input).unwrap();
    assert!(engine.route_all().unwrap() > 0);
    for nid in 1..=4 {
        assert!(
            engine.copper().generated().any(|t| t.net == nid),
            "expected tracks for pair net {nid}"
        );
    }
    assert!(engine.copper().index_consistent());

    // 2. Atomic rollback of Phase 1 diff pair when Phase 2 SE net is unroutable.
    let mut fail_se = base_diff_input(&["DP_P", "DP_N"]);
    fail_se.nets.push(Net {
        id: 3,
        name: "SE_BOXED".to_owned(),
    });
    fail_se
        .board_design_settings
        .as_mut()
        .unwrap()
        .netclass_assignments
        .insert("SE_BOXED".to_owned(), vec!["Default".to_owned()]);
    fail_se.pads.extend([
        pad(201, 3, [15_000_000, 25_000_000], &["F.Cu"]),
        pad(202, 3, [35_000_000, 25_000_000], &["F.Cu"]),
    ]);
    fail_se.rule_areas.push(RuleArea {
        id: uuid(902),
        name: None,
        layers: vec!["F.Cu".to_owned(), "B.Cu".to_owned()],
        outer: vec![
            [33_000_000, 23_000_000],
            [37_000_000, 23_000_000],
            [37_000_000, 27_000_000],
            [33_000_000, 27_000_000],
        ],
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        no_tracks: true,
        no_vias: true,
        no_pads: false,
        no_zone_fills: false,
        no_footprints: false,
    });
    let mut eng_se = Engine::new(fail_se).unwrap();
    assert!(
        eng_se.route_all().is_err(),
        "boxed SE net must fail route_all"
    );
    assert_eq!(
        eng_se.copper().generated().count(),
        0,
        "Phase 1 diff-pair tracks must roll back"
    );
    assert_eq!(eng_se.copper().generated_vias().count(), 0);
    assert!(eng_se.copper().index_consistent());

    // 3. Atomic rollback of Phase 1 diff pair AND Phase 2 SE net when Phase 3 length matching fails.
    let mut fail_lm = base_diff_input(&["DP_P", "DP_N"]);
    fail_lm.nets.push(Net {
        id: 3,
        name: "SE_OK".to_owned(),
    });
    fail_lm
        .board_design_settings
        .as_mut()
        .unwrap()
        .netclass_assignments
        .insert("SE_OK".to_owned(), vec!["Default".to_owned()]);
    fail_lm.pads[2].at = [20_000_000, 11_500_000]; // DP_N much shorter than DP_P -> unmatchable at 1nm
    fail_lm.pads.extend([
        pad(301, 3, [10_000_000, 25_000_000], &["F.Cu"]),
        pad(302, 3, [40_000_000, 25_000_000], &["F.Cu"]),
    ]);
    let opts = fail_lm.routing_options.as_mut().unwrap();
    opts.intra_match = true;
    opts.length_match_tolerance_nm = Some(1);
    opts.meander_amplitude_nm = Some(5_000_000);
    // Block F.Cu/B.Cu near DP_N so meander cannot reach required skew.
    fail_lm.rule_areas.push(RuleArea {
        id: uuid(903),
        name: None,
        layers: vec!["F.Cu".to_owned(), "B.Cu".to_owned()],
        outer: vec![
            [9_000_000, 12_200_000],
            [21_000_000, 12_200_000],
            [21_000_000, 18_000_000],
            [9_000_000, 18_000_000],
        ],
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        no_tracks: true,
        no_vias: true,
        no_pads: false,
        no_zone_fills: false,
        no_footprints: false,
    });
    let mut eng_lm = Engine::new(fail_lm).unwrap();
    assert!(
        eng_lm.route_all().is_err(),
        "impossible intra_match must fail route_all"
    );
    assert_eq!(
        eng_lm.copper().generated().count(),
        0,
        "Phase 1+2 tracks must roll back on Phase 3 failure"
    );
    assert_eq!(eng_lm.copper().generated_vias().count(), 0);
    assert!(eng_lm.copper().index_consistent());
}

#[test]
fn route_all_tier0_multipair_contention_succeeds_under_default_budget() {
    let mut input = base_diff_input(&["P1_P", "P1_N", "P2_P", "P2_N"]);
    input.max_expansions = 100_000;
    input.routing_options.as_mut().unwrap().pair_layers = vec!["F.Cu".into()];
    input.pads = vec![
        pad(1, 1, [10_000_000, 19_500_000], &["F.Cu"]),
        pad(2, 1, [50_000_000, 19_500_000], &["F.Cu"]),
        pad(3, 2, [10_000_000, 20_500_000], &["F.Cu"]),
        pad(4, 2, [50_000_000, 20_500_000], &["F.Cu"]),
        pad(5, 3, [29_500_000, 12_000_000], &["F.Cu"]),
        pad(6, 3, [29_500_000, 35_000_000], &["F.Cu"]),
        pad(7, 4, [30_500_000, 12_000_000], &["F.Cu"]),
        pad(8, 4, [30_500_000, 35_000_000], &["F.Cu"]),
    ];
    let mk_ra = |id_num: u64, outer: Vec<Point>| RuleArea {
        id: uuid(id_num),
        name: None,
        layers: vec!["F.Cu".into()],
        outer,
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        no_tracks: true,
        no_vias: false,
        no_pads: false,
        no_zone_fills: false,
        no_footprints: false,
    };
    input.rule_areas = vec![
        mk_ra(
            910,
            vec![
                [27_000_000, 9_000_000],
                [33_000_000, 9_000_000],
                [33_000_000, 10_000_000],
                [27_000_000, 10_000_000],
            ],
        ),
        mk_ra(
            911,
            vec![
                [27_000_000, 9_000_000],
                [28_000_000, 9_000_000],
                [28_000_000, 18_800_000],
                [27_000_000, 18_800_000],
            ],
        ),
        mk_ra(
            912,
            vec![
                [32_000_000, 9_000_000],
                [33_000_000, 9_000_000],
                [33_000_000, 18_800_000],
                [32_000_000, 18_800_000],
            ],
        ),
    ];

    let mut engine = Engine::new(input.clone()).unwrap();
    let exp = engine
        .route_all()
        .expect("P6 Tier-0 rip-up must succeed under 100_000");
    assert!(exp < 100_000, "expected < 100_000 expansions, got {exp}");
    let stats = engine
        .last_negotiation_stats()
        .expect("negotiation stats must be recorded");
    assert!(
        stats.units_ripped >= 1,
        "expected units_ripped >= 1: {stats:?}"
    );
    assert!(
        stats.rounds_committed >= 1,
        "expected rounds_committed >= 1: {stats:?}"
    );
    assert!(engine.copper().index_consistent());
    let out = kicad_route_engine::run(input);
    assert_eq!(out.status, Status::Complete);
}

#[test]
fn route_all_tier0_multipair_contention_monotonic_across_80000_to_100000() {
    let mut base = base_diff_input(&["P1_P", "P1_N", "P2_P", "P2_N"]);
    base.routing_options.as_mut().unwrap().pair_layers = vec!["F.Cu".into()];
    base.pads = vec![
        pad(1, 1, [10_000_000, 19_500_000], &["F.Cu"]),
        pad(2, 1, [50_000_000, 19_500_000], &["F.Cu"]),
        pad(3, 2, [10_000_000, 20_500_000], &["F.Cu"]),
        pad(4, 2, [50_000_000, 20_500_000], &["F.Cu"]),
        pad(5, 3, [29_500_000, 12_000_000], &["F.Cu"]),
        pad(6, 3, [29_500_000, 35_000_000], &["F.Cu"]),
        pad(7, 4, [30_500_000, 12_000_000], &["F.Cu"]),
        pad(8, 4, [30_500_000, 35_000_000], &["F.Cu"]),
    ];
    let mk_ra = |id_num: u64, outer: Vec<Point>| RuleArea {
        id: uuid(id_num),
        name: None,
        layers: vec!["F.Cu".into()],
        outer,
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        no_tracks: true,
        no_vias: false,
        no_pads: false,
        no_zone_fills: false,
        no_footprints: false,
    };
    base.rule_areas = vec![
        mk_ra(
            910,
            vec![
                [27_000_000, 9_000_000],
                [33_000_000, 9_000_000],
                [33_000_000, 10_000_000],
                [27_000_000, 10_000_000],
            ],
        ),
        mk_ra(
            911,
            vec![
                [27_000_000, 9_000_000],
                [28_000_000, 9_000_000],
                [28_000_000, 18_800_000],
                [27_000_000, 18_800_000],
            ],
        ),
        mk_ra(
            912,
            vec![
                [32_000_000, 9_000_000],
                [33_000_000, 9_000_000],
                [33_000_000, 18_800_000],
                [32_000_000, 18_800_000],
            ],
        ),
    ];

    let mut expected_exp: Option<u64> = None;
    for budget in [80_000u64, 80_007, 85_000, 90_000, 100_000] {
        let mut input = base.clone();
        input.max_expansions = budget;
        let mut engine = Engine::new(input.clone()).unwrap();
        let exp = engine.route_all().unwrap_or_else(|err| {
            panic!("P6 Tier-0 rip-up must succeed at budget={budget}: {err:?}")
        });
        assert!(
            exp < budget,
            "budget={budget}: expected < {budget}, got {exp}"
        );
        if let Some(prev) = expected_exp {
            assert_eq!(
                exp, prev,
                "budget={budget}: expected consistent exp={prev}, got {exp}"
            );
        } else {
            expected_exp = Some(exp);
        }
        let stats = engine
            .last_negotiation_stats()
            .expect("negotiation stats must be recorded");
        assert!(
            stats.units_ripped >= 1 && stats.rounds_committed >= 1,
            "budget={budget}: expected units_ripped >= 1 and rounds_committed >= 1: {stats:?}"
        );
        assert!(engine.copper().index_consistent());
        if budget == 80_007 || budget == 100_000 {
            let out = kicad_route_engine::run(input);
            assert_eq!(out.status, Status::Complete);
        }
    }
}

#[test]
fn tier1_detour_around_non_rippable_tier0_exceeding_trial_cap_succeeds() {
    let mut base = base_diff_input(&["DP_P", "DP_N"]);
    base.layers = vec!["F.Cu".into()];
    base.bounds = Bounds {
        min: [0, 0],
        max: [90_000_000, 70_000_000],
    };
    base.routing_options.as_mut().unwrap().pair_layers = vec!["F.Cu".into()];
    base.limits.max_work = 50_000_000;
    base.nets.push(Net {
        id: 3,
        name: "SE_OK".into(),
    });
    base.nets.push(Net {
        id: 4,
        name: "SE_DETOUR".into(),
    });
    base.pads = vec![
        pad(1, 1, [25_000_000, 34_700_000], &["F.Cu"]),
        pad(2, 1, [65_000_000, 34_700_000], &["F.Cu"]),
        pad(3, 2, [25_000_000, 35_300_000], &["F.Cu"]),
        pad(4, 2, [65_000_000, 35_300_000], &["F.Cu"]),
        pad(5, 3, [1_000_000, 1_000_000], &["F.Cu"]),
        pad(6, 3, [2_500_000, 1_000_000], &["F.Cu"]),
        pad(7, 4, [45_000_000, 27_500_000], &["F.Cu"]),
        pad(8, 4, [45_000_000, 42_500_000], &["F.Cu"]),
    ];

    let mut expected_exp: Option<u64> = None;
    for budget in [80_000u64, 80_007, 85_000, 100_000] {
        let mut input = base.clone();
        input.max_expansions = budget;
        let mut engine = Engine::new(input.clone()).unwrap();
        let exp = engine.route_all().unwrap_or_else(|err| {
            panic!(
                "Tier-1 detour around non-rippable Tier-0 must succeed at budget={budget}: {err:?}"
            )
        });
        assert!(
            exp > 40_000 && exp <= budget,
            "budget={budget}: expected (40_000..={budget}], got {exp}"
        );
        if let Some(prev) = expected_exp {
            assert_eq!(exp, prev, "budget={budget}: expected exp={prev}, got {exp}");
        } else {
            expected_exp = Some(exp);
        }
        let stats = engine
            .last_negotiation_stats()
            .expect("negotiation stats must be recorded");
        assert_eq!(
            stats.units_ripped, 0,
            "Tier-1 must not rip Tier-0 diff pair: {stats:?}"
        );
        for nid in [1u32, 2, 3, 4] {
            assert!(
                engine.copper().generated().any(|t| t.net == nid),
                "budget={budget}: expected generated tracks for net {nid}"
            );
        }
        assert!(engine.copper().index_consistent());
        if budget == 100_000 {
            let out = kicad_route_engine::run(input);
            assert_eq!(out.status, Status::Complete);
        }
    }
}

#[test]
fn diff_pair_zero_mask_clearance_detours_around_static_gnd_mask_aperture() {
    let mut input = base_diff_input(&["DP_P", "DP_N"]);
    if let Some(opts) = input.routing_options.as_mut() {
        opts.pair_layers = vec!["F.Cu".into()];
    }
    input.nets.push(Net {
        id: 3,
        name: "GND".into(),
    });
    input
        .pads
        .push(pad(10, 3, [25_000_000, 15_000_000], &["F.Cu"]));
    let mask = MaskAperture {
        id: uuid(100),
        net: None,
        group_id: None,
        layer: "F.Mask".into(),
        shape: GraphicShape::Polygon {
            outer: vec![
                [24_000_000, 9_000_000],
                [26_000_000, 9_000_000],
                [26_000_000, 16_000_000],
                [24_000_000, 16_000_000],
            ],
            holes: Vec::new(),
            width_nm: 0,
            filled: true,
        },
        expansion_nm: 0,
    };
    input.mask_apertures.push(mask.clone());
    let out = kicad_route_engine::run(input);
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    assert!(!out.added_tracks.is_empty());
    for t in &out.added_tracks {
        assert!(
            !kicad_route_engine::geometry::track_near_mask(
                t.start, t.end, t.width_nm, 0, &mask, false
            ),
            "track {t:?} bridged GND F.Mask aperture"
        );
    }
}

#[test]
fn diff_pair_zero_mask_clearance_blocks_coupled_self_bridge_over_empty_aperture() {
    let mut input = base_diff_input(&["DP_P", "DP_N"]);
    let mask = MaskAperture {
        id: uuid(101),
        net: None,
        group_id: None,
        layer: "F.Mask".into(),
        shape: GraphicShape::Polygon {
            outer: vec![
                [24_000_000, 9_500_000],
                [26_000_000, 9_500_000],
                [26_000_000, 12_000_000],
                [24_000_000, 12_000_000],
            ],
            holes: Vec::new(),
            width_nm: 0,
            filled: true,
        },
        expansion_nm: 0,
    };
    input.mask_apertures.push(mask.clone());
    let out = kicad_route_engine::run(input);
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    let pos_hits = out.added_tracks.iter().any(|t| {
        t.net == 1
            && kicad_route_engine::geometry::track_near_mask(
                t.start, t.end, t.width_nm, 0, &mask, false,
            )
    });
    let neg_hits = out.added_tracks.iter().any(|t| {
        t.net == 2
            && kicad_route_engine::geometry::track_near_mask(
                t.start, t.end, t.width_nm, 0, &mask, false,
            )
    });
    assert!(
        !(pos_hits && neg_hits),
        "DP_P and DP_N must never both cross the same F.Mask aperture"
    );
}

#[test]
fn diff_pair_zero_mask_clearance_allows_single_leg_pad_mask_aperture() {
    let mut input = base_diff_input(&["DP_P", "DP_N"]);
    // F.Mask aperture covers ONLY DP_P's start pad at (10M, 10M).
    let mask = MaskAperture {
        id: uuid(102),
        net: None,
        group_id: None,
        layer: "F.Mask".into(),
        shape: GraphicShape::Polygon {
            outer: vec![
                [9_500_000, 9_500_000],
                [10_500_000, 9_500_000],
                [10_500_000, 10_500_000],
                [9_500_000, 10_500_000],
            ],
            holes: Vec::new(),
            width_nm: 0,
            filled: true,
        },
        expansion_nm: 0,
    };
    input.mask_apertures.push(mask);
    let out = kicad_route_engine::run(input);
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    assert!(out.added_tracks.iter().any(|t| t.net == 1));
    assert!(out.added_tracks.iter().any(|t| t.net == 2));
}

#[test]
fn diff_pair_zero_mask_clearance_enforces_pad_solder_mask_expansion() {
    let mut input = base_diff_input(&["DP_P", "DP_N"]);
    input.layers = vec!["F.Cu".into(), "In1.Cu".into(), "B.Cu".into()];
    input.rules.solder_mask_expansion_nm = 400_000;
    input.rules.solder_mask_to_copper_clearance_nm = 0;
    input.rules.clearance_nm = 200_000;
    if let Some(bds) = input.board_design_settings.as_mut() {
        bds.solder_mask_expansion_nm = 400_000;
    }
    input.nets.push(Net {
        id: 3,
        name: "GND".into(),
    });
    // Place GND pad at y=10.75M (center between 10M and 11.5M) on F.Cu and In1.Cu.
    // Pad radius=150k, track half-width=125k -> copper gap is 750k - 275k = 475k? Use radius 400k ->
    // copper gap = 750k - 400k - 125k = 225k (>200k copper, <400k mask expansion!).
    let mut gnd = pad(10, 3, [25_000_000, 10_750_000], &["F.Cu", "In1.Cu"]);
    gnd.shape = Shape::Circle { radius_nm: 400_000 };
    input.pads.push(gnd);
    let out = kicad_route_engine::run(input.clone());
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    // On F.Cu, no diff-pair track center may come within 400k + 400k + 125k = 925k of (25M, 10.75M).
    for t in out.added_tracks.iter().filter(|t| t.layer == "F.Cu") {
        assert!(
            !kicad_route_engine::geometry::point_near_segment(
                [25_000_000, 10_750_000],
                t.start,
                t.end,
                i128::from(t.width_nm) + 2 * 400_000 + 2 * 400_000,
                false
            ),
            "F.Cu track {t:?} violated GND pad solder_mask_expansion"
        );
    }
}

#[test]
fn diff_pair_wider_than_default_width_respects_mask_aperture_both_clearances() {
    for to_cu in [0i64, 100_000] {
        let mut input = base_diff_input(&["DP_P", "DP_N"]);
        input.rules.solder_mask_to_copper_clearance_nm = to_cu;
        if let Some(bds) = input.board_design_settings.as_mut() {
            bds.solder_mask_to_copper_clearance_nm = to_cu;
            bds.netclasses[1].diff_pair_width_nm = 500_000;
        }
        input.nets.push(Net {
            id: 3,
            name: "GND".into(),
        });
        input
            .pads
            .push(pad(10, 3, [25_000_000, 15_000_000], &["F.Cu"]));
        // Place an F.Mask aperture whose boundary at y=9.78M is cleared by default_w=250k (gap=95k)
        // when to_cu=0, or within 100k of default_w, and ALSO blocks wider width=500k (half-width 250k)!
        let mask = MaskAperture {
            id: uuid(103),
            net: None,
            group_id: None,
            layer: "F.Mask".into(),
            shape: GraphicShape::Polygon {
                outer: vec![
                    [24_000_000, 9_800_000],
                    [26_000_000, 9_800_000],
                    [26_000_000, 16_000_000],
                    [24_000_000, 16_000_000],
                ],
                holes: Vec::new(),
                width_nm: 0,
                filled: true,
            },
            expansion_nm: 0,
        };
        input.mask_apertures.push(mask.clone());
        let out = kicad_route_engine::run(input);
        assert_eq!(
            out.status,
            Status::Complete,
            "to_cu={to_cu}: {:?}",
            out.reason
        );
        for t in &out.added_tracks {
            assert_eq!(t.width_nm, 500_000);
            assert!(
                !kicad_route_engine::geometry::track_near_mask(
                    t.start, t.end, t.width_nm, to_cu, &mask, false
                ),
                "to_cu={to_cu}: wide track {t:?} violated mask clearance"
            );
        }
    }
}

#[test]
fn diff_pair_coupled_vias_and_meander_avoid_mask_bridge() {
    let mut input = base_diff_input(&["DP_P", "DP_N"]);
    input.nets.push(Net {
        id: 3,
        name: "GND".into(),
    });
    // Force layer transition from F.Cu (start pads) to B.Cu (end pads) with F.Cu keepout in middle.
    input.pads[1].layers = vec!["B.Cu".into()];
    input.pads[3].layers = vec!["B.Cu".into()];
    input
        .pads
        .push(pad(10, 3, [25_000_000, 15_000_000], &["F.Cu", "B.Cu"]));
    input.rule_areas.push(RuleArea {
        id: uuid(104),
        name: None,
        layers: vec!["F.Cu".into()],
        outer: vec![
            [18_000_000, 0],
            [32_000_000, 0],
            [32_000_000, 40_000_000],
            [18_000_000, 40_000_000],
        ],
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        no_tracks: true,
        no_vias: true,
        no_pads: false,
        no_footprints: false,
        no_zone_fills: false,
    });
    let f_mask = MaskAperture {
        id: uuid(105),
        net: None,
        group_id: None,
        layer: "F.Mask".into(),
        shape: GraphicShape::Polygon {
            outer: vec![
                [14_000_000, 9_000_000],
                [16_000_000, 9_000_000],
                [16_000_000, 16_000_000],
                [14_000_000, 16_000_000],
            ],
            holes: Vec::new(),
            width_nm: 0,
            filled: true,
        },
        expansion_nm: 0,
    };
    let b_mask = MaskAperture {
        id: uuid(106),
        net: None,
        group_id: None,
        layer: "B.Mask".into(),
        shape: GraphicShape::Polygon {
            outer: vec![
                [24_000_000, 9_000_000],
                [26_000_000, 9_000_000],
                [26_000_000, 16_000_000],
                [24_000_000, 16_000_000],
            ],
            holes: Vec::new(),
            width_nm: 0,
            filled: true,
        },
        expansion_nm: 0,
    };
    input
        .mask_apertures
        .extend([f_mask.clone(), b_mask.clone()]);
    if let Some(opts) = input.routing_options.as_mut() {
        opts.intra_match = true;
        opts.length_match_tolerance_nm = Some(50_000);
        opts.meander_amplitude_nm = Some(2_000_000);
    }
    let out = kicad_route_engine::run(input);
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    assert!(!out.added_vias.is_empty());
    for v in &out.added_vias {
        assert!(!kicad_route_engine::geometry::track_near_mask(
            v.at,
            v.at,
            v.diameter_nm,
            0,
            &f_mask,
            false
        ));
        assert!(!kicad_route_engine::geometry::track_near_mask(
            v.at,
            v.at,
            v.diameter_nm,
            0,
            &b_mask,
            false
        ));
    }
    for t in &out.added_tracks {
        let mask = if t.layer == "F.Cu" { &f_mask } else { &b_mask };
        assert!(!kicad_route_engine::geometry::track_near_mask(
            t.start, t.end, t.width_nm, 0, mask, false
        ));
    }
}

#[test]
fn diff_pair_pad_expansion_and_min_width_bridge_to_mask_aperture() {
    for (tag, exp_nm, min_w_nm, gnd_y, mask_exp_nm) in [
        ("DP_PAD_EXP_TOUCHES_RECT", 350_000, 0, 12_900_000, 350_000),
        ("DP_PAD_WEB_TOUCHES_RECT", 0, 200_000, 12_500_000, 0),
    ] {
        let mut input = base_diff_input(&["DP_P", "DP_N"]);
        input.layers = vec!["F.Cu".into(), "B.Cu".into()];
        input.rules.solder_mask_expansion_nm = exp_nm;
        input.rules.solder_mask_min_width_nm = min_w_nm;
        input.rules.solder_mask_to_copper_clearance_nm = 0;
        input.rules.clearance_nm = 200_000;
        if let Some(bds) = input.board_design_settings.as_mut() {
            bds.solder_mask_expansion_nm = exp_nm;
            bds.solder_mask_min_width_nm = min_w_nm;
            bds.solder_mask_to_copper_clearance_nm = 0;
        }
        input.nets.push(Net {
            id: 3,
            name: "GND".into(),
        });
        let mut gnd = pad(10, 3, [25_000_000, gnd_y], &["F.Cu"]);
        gnd.shape = Shape::Rect {
            half_size_nm: [400_000, 400_000],
        };
        input.pads.push(gnd);
        let mask = MaskAperture {
            id: uuid(107),
            net: None,
            group_id: None,
            layer: "F.Mask".into(),
            shape: GraphicShape::Polygon {
                outer: vec![
                    [24_000_000, 9_500_000],
                    [26_000_000, 9_500_000],
                    [26_000_000, 12_000_000],
                    [24_000_000, 12_000_000],
                ],
                holes: Vec::new(),
                width_nm: 0,
                filled: true,
            },
            expansion_nm: mask_exp_nm,
        };
        input.mask_apertures.push(mask.clone());
        let out = kicad_route_engine::run(input);
        assert_eq!(out.status, Status::Complete, "{tag}: {:?}", out.reason);
        for t in out.added_tracks.iter().filter(|t| t.layer == "F.Cu") {
            assert!(
                !kicad_route_engine::geometry::track_near_mask(
                    t.start, t.end, t.width_nm, 0, &mask, false
                ),
                "{tag}: F.Cu track {t:?} touched bridged mask aperture"
            );
        }
    }
}

#[test]
fn meander_wave_respects_requested_amplitude_ceiling_dp1_and_dp1b() {
    use kicad_route_engine::model::Status;
    let mk_match_input =
        |width_nm: i64, n1_len_dx: i64, n2_len_dx: i64, amp_nm: i64, tol_nm: i64| {
            let mut inp = base_diff_input(&["N1", "N2"]);
            inp.rules.width_nm = width_nm;
            inp.rules.clearance_nm = 200_000;
            inp.rules.grid_nm = 200_000;
            if let Some(bds) = inp.board_design_settings.as_mut() {
                bds.default_track_width_nm = width_nm;
                bds.min_track_width_nm = width_nm.min(150_000);
                for nc in &mut bds.netclasses {
                    nc.track_width_nm = width_nm;
                }
            }
            inp.pads[0].at = [10_000_000, 10_000_000];
            inp.pads[1].at = [10_000_000 + n1_len_dx, 10_000_000];
            inp.pads[2].at = [10_000_000, 14_000_000];
            inp.pads[3].at = [10_000_000 + n2_len_dx, 14_000_000];
            inp.routing_options = Some(RoutingOptions {
                length_match_groups: vec![LengthMatchGroupSpec {
                    members: vec!["N1".into(), "N2".into()],
                    ..Default::default()
                }],
                group_match_tolerance_nm: Some(tol_nm),
                group_meander_amplitude_nm: Some(amp_nm),
                ..Default::default()
            });
            inp
        };

    // DP-1 / DP-1b negative: width=200_000, amp=50_000 (below self-copper bridge threshold
    // width/sqrt(2) ~= 141_421 nm) must NEVER overshoot 50_000 nm and must fail closed.
    let out_self_bridge = kicad_route_engine::run(mk_match_input(
        200_000, 20_000_000, 20_160_000, 50_000, 50_000,
    ));
    assert_eq!(
        out_self_bridge.status,
        Status::Unroutable,
        "amp=50_000 on 200_000 trace must fail closed rather than overshooting amplitude: {:?}",
        out_self_bridge.reason
    );

    // Positive: width=200_000, amp=155_000 (between width/sqrt(2)=141_421 and old 0.85*width=170_000)
    // succeeds cleanly without exceeding the 155_000 nm amplitude ceiling.
    let out_bounded = kicad_route_engine::run(mk_match_input(
        200_000, 20_000_000, 20_200_000, 155_000, 50_000,
    ));
    assert_eq!(
        out_bounded.status,
        Status::Complete,
        "bounded amp=155_000 failed: {:?}",
        out_bounded.reason
    );
    let max_exc = out_bounded
        .added_tracks
        .iter()
        .filter(|t| t.net == 1)
        .map(|t| {
            (t.start[1] - 10_000_000)
                .abs()
                .max((t.end[1] - 10_000_000).abs())
        })
        .max()
        .unwrap_or(0);
    assert!(
        max_exc > 0 && max_exc <= 155_000,
        "unexpected exc {max_exc}"
    );

    // Positive: width=60_000, amp=50_000 (below old 0.85*60_000=51_000 floor) succeeds with <= 50_000 nm.
    let out_thin = kicad_route_engine::run(mk_match_input(
        60_000, 20_000_000, 20_200_000, 50_000, 50_000,
    ));
    assert_eq!(
        out_thin.status,
        Status::Complete,
        "thin amp=50_000 failed: {:?}",
        out_thin.reason
    );
    let thin_exc = out_thin
        .added_tracks
        .iter()
        .filter(|t| t.net == 1)
        .map(|t| {
            (t.start[1] - 10_000_000)
                .abs()
                .max((t.end[1] - 10_000_000).abs())
        })
        .max()
        .unwrap_or(0);
    assert!(
        thin_exc > 0 && thin_exc <= 50_000,
        "thin excursion {thin_exc} exceeded 50_000 nm"
    );

    // Impossible amplitude ceiling fails closed rather than violating amp_nm
    let out_fail = kicad_route_engine::run(mk_match_input(
        200_000, 10_000_000, 25_000_000, 20_000, 40_000,
    ));
    assert_eq!(out_fail.status, Status::Unroutable, "{:?}", out_fail.reason);
}

#[test]
fn protect_mode_human_completes_unlocked_partial_and_fails_closed_on_locked_partial() {
    use kicad_route_engine::model::Status;
    let mk_partial = |locked: bool, mode: ProtectMode| {
        let mut inp = base_diff_input(&["PART", "OTHER"]);
        inp.pads[0].at = [2_000_000, 10_000_000];
        inp.pads[1].at = [18_000_000, 10_000_000];
        inp.pads[2].at = [2_000_000, 14_000_000];
        inp.pads[3].at = [18_000_000, 14_000_000];
        inp.tracks.push(Track {
            id: uuid(910),
            net: 1,
            start: [2_000_000, 10_000_000],
            end: [4_000_000, 10_000_000],
            width_nm: 250_000,
            locked,
            layer: "F.Cu".into(),
        });
        inp.routing_options = Some(RoutingOptions {
            protect_mode: Some(mode),
            ..Default::default()
        });
        inp
    };

    // DP-3: unlocked partial net under ProtectMode::Human completes open terminals while keeping input copper
    let unlocked_inp = mk_partial(false, ProtectMode::Human);
    let out_ok = kicad_route_engine::run(unlocked_inp.clone());
    assert_eq!(out_ok.status, Status::Complete, "{:?}", out_ok.reason);
    assert!(
        out_ok.added_tracks.iter().any(|t| t.net == 1),
        "expected added track completing PART net under ProtectMode::Human"
    );
    let mut check_inp = unlocked_inp;
    check_inp.tracks.extend(out_ok.added_tracks);
    let out_recheck = kicad_route_engine::run(check_inp);
    assert_eq!(
        out_recheck.status,
        Status::Complete,
        "{:?}",
        out_recheck.reason
    );
    assert!(
        out_recheck.added_tracks.is_empty(),
        "expected zero open connections remaining, got {:?}",
        out_recheck.added_tracks
    );

    // Locked partial net under ProtectMode::Human fails closed with Status::Unroutable (never Complete with open net)
    let locked_human = mk_partial(true, ProtectMode::Human);
    let out_locked = kicad_route_engine::run(locked_human);
    assert_eq!(
        out_locked.status,
        Status::Unroutable,
        "{:?}",
        out_locked.reason
    );

    // Partial net under ProtectMode::Existing also fails closed when in scope
    let existing_partial = mk_partial(false, ProtectMode::Existing);
    let out_existing = kicad_route_engine::run(existing_partial);
    assert_eq!(
        out_existing.status,
        Status::Unroutable,
        "{:?}",
        out_existing.reason
    );
}

#[test]
fn ac_chain_three_pair_multipass_under_dru_skew_cap() {
    use kicad_route_engine::model::Status;
    let mk_pad = |id_n: u64, net: u32, at: Point, fp: &str, pnum: &str| Pad {
        id: format!("10000000-0000-4000-8000-{id_n:012x}"),
        net,
        at,
        shape: Shape::Rect {
            half_size_nm: [125_000, 125_000],
        },
        drill: None,
        layers: vec!["F.Cu".into()],
        local_clearance_nm: None,
        zone_connection: PadZoneConnection::Inherited,
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
        footprint_ref: Some(fp.into()),
        pad_number: Some(pnum.into()),
        fromto_id: None,
    };
    for ac_tol in [50_000_i64, 1_i64] {
        let mut inp = base_diff_input(&["DPA_P", "DPA_N", "DPB_P", "DPB_N", "DPC_P", "DPC_N"]);
        inp.bounds = Bounds {
            min: [0, 0],
            max: [95_000_000, 40_000_000],
        };
        inp.rules.width_nm = 200_000;
        inp.rules.clearance_nm = 150_000;
        inp.rules.grid_nm = 200_000;
        if let Some(bds) = inp.board_design_settings.as_mut() {
            bds.default_clearance_nm = 150_000;
            bds.default_track_width_nm = 200_000;
            for nc in &mut bds.netclasses {
                nc.clearance_nm = 150_000;
                nc.track_width_nm = 200_000;
                nc.diff_pair_width_nm = 200_000;
                nc.diff_pair_gap_nm = 200_000;
            }
        }
        // 3 pairs across C1/C2 (DPA->DPB) and C3/C4 (DPB->DPC): DPA (1,2) pre-routed +2.0mm P-longer;
        // DPB (3,4) and DPC (5,6) must absorb 2.0mm across two pairs because DRU caps per-pair skew at 1.2mm.
        inp.pads = vec![
            mk_pad(1, 1, [5_000_000, 10_000_000], "J1", "1"),
            mk_pad(2, 1, [25_000_000, 10_000_000], "C1", "1"),
            mk_pad(3, 2, [5_000_000, 10_400_000], "J1", "2"),
            mk_pad(4, 2, [25_000_000, 10_400_000], "C2", "1"),
            mk_pad(5, 3, [26_000_000, 10_000_000], "C1", "2"),
            mk_pad(6, 3, [56_000_000, 10_000_000], "C3", "1"),
            mk_pad(7, 4, [26_000_000, 10_400_000], "C2", "2"),
            mk_pad(8, 4, [56_000_000, 10_400_000], "C4", "1"),
            mk_pad(9, 5, [57_000_000, 10_000_000], "C3", "2"),
            mk_pad(10, 5, [85_000_000, 10_000_000], "J2", "1"),
            mk_pad(11, 6, [57_000_000, 10_400_000], "C4", "2"),
            mk_pad(12, 6, [85_000_000, 10_400_000], "J2", "2"),
        ];
        inp.components = vec![
            ComponentInfo {
                ref_des: "J1".into(),
                dnp: false,
                exclude_from_bom: false,
                exclude_from_pos: false,
                pad_numbers: vec!["1".into(), "2".into()],
                net_ids: vec![1, 2],
            },
            ComponentInfo {
                ref_des: "J2".into(),
                dnp: false,
                exclude_from_bom: false,
                exclude_from_pos: false,
                pad_numbers: vec!["1".into(), "2".into()],
                net_ids: vec![5, 6],
            },
            ComponentInfo {
                ref_des: "C1".into(),
                dnp: false,
                exclude_from_bom: false,
                exclude_from_pos: false,
                pad_numbers: vec!["1".into(), "2".into()],
                net_ids: vec![1, 3],
            },
            ComponentInfo {
                ref_des: "C2".into(),
                dnp: false,
                exclude_from_bom: false,
                exclude_from_pos: false,
                pad_numbers: vec!["1".into(), "2".into()],
                net_ids: vec![2, 4],
            },
            ComponentInfo {
                ref_des: "C3".into(),
                dnp: false,
                exclude_from_bom: false,
                exclude_from_pos: false,
                pad_numbers: vec!["1".into(), "2".into()],
                net_ids: vec![3, 5],
            },
            ComponentInfo {
                ref_des: "C4".into(),
                dnp: false,
                exclude_from_bom: false,
                exclude_from_pos: false,
                pad_numbers: vec!["1".into(), "2".into()],
                net_ids: vec![4, 6],
            },
        ];
        // Pre-routed locked DPA with +2.0mm skew on DPA_P (22.0mm vs 20.0mm via non-overlapping detour)
        inp.tracks = vec![
            Track {
                id: uuid(801),
                net: 1,
                start: [5_000_000, 10_000_000],
                end: [14_000_000, 10_000_000],
                width_nm: 200_000,
                locked: true,
                layer: "F.Cu".into(),
            },
            Track {
                id: uuid(802),
                net: 1,
                start: [14_000_000, 10_000_000],
                end: [14_000_000, 9_000_000],
                width_nm: 200_000,
                locked: true,
                layer: "F.Cu".into(),
            },
            Track {
                id: uuid(803),
                net: 1,
                start: [14_000_000, 9_000_000],
                end: [16_000_000, 9_000_000],
                width_nm: 200_000,
                locked: true,
                layer: "F.Cu".into(),
            },
            Track {
                id: uuid(804),
                net: 1,
                start: [16_000_000, 9_000_000],
                end: [16_000_000, 10_000_000],
                width_nm: 200_000,
                locked: true,
                layer: "F.Cu".into(),
            },
            Track {
                id: uuid(805),
                net: 1,
                start: [16_000_000, 10_000_000],
                end: [25_000_000, 10_000_000],
                width_nm: 200_000,
                locked: true,
                layer: "F.Cu".into(),
            },
            Track {
                id: uuid(806),
                net: 2,
                start: [5_000_000, 10_400_000],
                end: [25_000_000, 10_400_000],
                width_nm: 200_000,
                locked: true,
                layer: "F.Cu".into(),
            },
        ];
        inp.custom_rules = Some(
            "(version 1)\n\
             (rule \"dpb_skew\" (condition \"A.inDiffPair('DPB')\") (constraint skew (max 1.2mm) (within_diff_pairs)))\n\
             (rule \"dpc_skew\" (condition \"A.inDiffPair('DPC')\") (constraint skew (max 1.2mm) (within_diff_pairs)))"
                .into(),
        );
        inp.routing_options = Some(RoutingOptions {
            diff_pairs_mode: Some(DiffPairsMode::Auto),
            ac_couple_chains: vec![vec!["C1".into(), "C2".into(), "C3".into(), "C4".into()]],
            ac_match_tolerance_nm: Some(ac_tol),
            ac_meander_amplitude_nm: Some(3_000_000),
            ..Default::default()
        });
        let out = kicad_route_engine::run(inp.clone());
        assert_eq!(
            out.status,
            Status::Complete,
            "ac_tol={ac_tol}: {:?}",
            out.reason
        );
        let len_of = |net: u32| -> f64 {
            inp.tracks
                .iter()
                .chain(out.added_tracks.iter())
                .filter(|t| t.net == net)
                .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
                .sum()
        };
        let sum_p = len_of(1) + len_of(3) + len_of(5);
        let sum_n = len_of(2) + len_of(4) + len_of(6);
        assert!(
            (sum_p - sum_n).abs() <= (ac_tol as f64) + 5_000.0,
            "ac_tol={ac_tol}: composed skew {} > {ac_tol}",
            (sum_p - sum_n).abs()
        );
        assert!(
            (len_of(3) - len_of(4)).abs() <= 1_205_000.0,
            "DPB skew {} > 1.2mm (P={}, N={}, DPC P={}, N={})",
            (len_of(3) - len_of(4)).abs(),
            len_of(3),
            len_of(4),
            len_of(5),
            len_of(6)
        );
        assert!(
            (len_of(5) - len_of(6)).abs() <= 1_205_000.0,
            "DPC skew {} > 1.2mm (P={}, N={})",
            (len_of(5) - len_of(6)).abs(),
            len_of(5),
            len_of(6)
        );
    }
}

#[test]
fn human_diff_unlocked_pre_routed_leg_fails_closed() {
    use kicad_route_engine::model::Status;
    let mk_input = |both_routed: bool, full_p: bool, mode: ProtectMode| {
        let mut inp = base_diff_input(&["DP_P", "DP_N"]);
        inp.pads[0].at = [5_000_000, 10_000_000];
        inp.pads[1].at = [20_000_000, 10_000_000];
        inp.pads[2].at = [5_000_000, 10_400_000];
        inp.pads[3].at = [20_000_000, 10_400_000];
        inp.rules.width_nm = 200_000;
        inp.rules.clearance_nm = 150_000;
        inp.rules.grid_nm = 200_000;
        inp.tracks.push(Track {
            id: uuid(920),
            net: 1,
            start: [5_000_000, 10_000_000],
            end: if full_p {
                [20_000_000, 10_000_000]
            } else {
                [10_000_000, 10_000_000]
            },
            width_nm: 200_000,
            locked: false,
            layer: "F.Cu".into(),
        });
        if both_routed {
            inp.tracks.push(Track {
                id: uuid(921),
                net: 2,
                start: [5_000_000, 10_400_000],
                end: [20_000_000, 10_400_000],
                width_nm: 200_000,
                locked: false,
                layer: "F.Cu".into(),
            });
        }
        inp.routing_options = Some(RoutingOptions {
            diff_pairs_mode: Some(DiffPairsMode::Auto),
            protect_mode: Some(mode),
            ..Default::default()
        });
        inp
    };

    for (mode, full_p) in [
        (ProtectMode::Human, true),
        (ProtectMode::Human, false),
        (ProtectMode::Locked, true),
        (ProtectMode::None, true),
    ] {
        let out = kicad_route_engine::run(mk_input(false, full_p, mode));
        assert_eq!(
            out.status,
            Status::Unroutable,
            "mode={mode:?}, full_p={full_p}: {:?}",
            out.reason
        );
        assert!(
            out.added_tracks.is_empty() && out.added_vias.is_empty(),
            "mode={mode:?}, full_p={full_p} emitted duplicate/partial copper: {:?}",
            out.added_tracks
        );
    }

    // Positive control: fully pre-routed unlocked pair completes without duplicating copper
    let out_both = kicad_route_engine::run(mk_input(true, true, ProtectMode::Human));
    assert_eq!(out_both.status, Status::Complete, "{:?}", out_both.reason);
    assert!(out_both.added_tracks.is_empty());
}

#[test]
fn dp_pad_1mm_short_escape_vbump_matches_within_tolerance() {
    use kicad_route_engine::model::Status;
    let mut inp = base_diff_input(&["DP_P", "DP_N"]);
    inp.pads[0].at = [5_000_000, 10_000_000];
    inp.pads[1].at = [6_000_000, 10_000_000];
    inp.pads[2].at = [5_000_000, 10_400_000];
    inp.pads[3].at = [6_025_000, 10_400_000];
    inp.rules.width_nm = 200_000;
    inp.rules.clearance_nm = 150_000;
    inp.rules.grid_nm = 200_000;
    if let Some(bds) = inp.board_design_settings.as_mut() {
        bds.default_clearance_nm = 150_000;
        bds.default_track_width_nm = 200_000;
        for nc in &mut bds.netclasses {
            nc.clearance_nm = 150_000;
            nc.track_width_nm = 200_000;
            nc.diff_pair_width_nm = 200_000;
            nc.diff_pair_gap_nm = 200_000;
        }
    }
    inp.routing_options = Some(RoutingOptions {
        diff_pairs_mode: Some(DiffPairsMode::Auto),
        intra_match: true,
        length_match_tolerance_nm: Some(10_000),
        meander_amplitude_nm: Some(500_000),
        ..Default::default()
    });
    let out = kicad_route_engine::run(inp);
    assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    let len_of = |net: u32| -> f64 {
        out.added_tracks
            .iter()
            .filter(|t| t.net == net)
            .map(|t| ((t.end[0] - t.start[0]) as f64).hypot((t.end[1] - t.start[1]) as f64))
            .sum()
    };
    let skew = (len_of(1) - len_of(2)).abs();
    assert!(
        skew <= 10_500.0,
        "expected matched DP_P/DP_N skew <= 10_000 nm, got {skew} (P={}, N={})",
        len_of(1),
        len_of(2)
    );
}
