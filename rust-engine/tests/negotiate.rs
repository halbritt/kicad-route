use kicad_route_engine::{
    geometry::*,
    model::*,
    negotiate::{
        self, BlockedFrontier, CongestionHistory, RouteUnitAdapter, RouteUnitId, UnitRouteError,
        UnitSpec,
    },
    run, Engine,
};
use std::collections::{BTreeMap, BTreeSet};

fn id(n: u64) -> String {
    format!("10000000-0000-4000-8000-{n:012x}")
}

fn pad(n: u64, net: u32, at: Point) -> Pad {
    Pad {
        id: id(n),
        net,
        at,
        shape: Shape::Circle { radius_nm: 2 },
        drill: None,
        layers: Vec::new(),
        local_clearance_nm: None,
        zone_connection: PadZoneConnection::Inherited,
        thermal_gap_nm: None,
        thermal_spoke_width_nm: None,
        thermal_spoke_angle_mdeg: None,
        orientation_mdeg: 0,
        footprint_ref: None,
        pad_number: None,
        fromto_id: None,
    }
}

fn wire(n: u64, net: u32, a: Point, b: Point) -> Track {
    Track {
        id: id(n),
        net,
        start: a,
        end: b,
        width_nm: 2,
        locked: true,
        layer: "F.Cu".into(),
    }
}

fn base_input(max: Point, layers: Vec<String>) -> Input {
    Input {
        version: 1,
        source_id: "a".repeat(64),
        layer: "F.Cu".into(),
        layers,
        bounds: Bounds { min: [0, 0], max },
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
        nets: Vec::new(),
        pads: Vec::new(),
        tracks: Vec::new(),
        vias: Vec::new(),
        rule_areas: Vec::new(),
        max_expansions: 50_000,
        limits: Limits::default(),
        holes: Vec::new(),
        board_outline: None,
        graphics: Vec::new(),
        mask_apertures: Vec::new(),
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

fn verify_disjoint_and_legal(input: &Input, tracks: &[Track], vias: &[Via]) {
    let e = Engine::new(input.clone()).unwrap();
    for t in tracks {
        assert!(t.start != t.end, "degenerate zero-length track: {t:?}");
        assert!(
            e.legal_edge_on_layer(t.net, &t.layer, t.start, t.end)
                .expect("legal_edge_on_layer query succeeded"),
            "generated track violates legality on {}: {t:?}",
            t.layer
        );
    }
    for i in 0..tracks.len() {
        for j in (i + 1)..tracks.len() {
            if tracks[i].net != tracks[j].net && tracks[i].layer == tracks[j].layer {
                let clr = input.rules.clearance_nm;
                let gap = i128::from(tracks[i].width_nm)
                    + i128::from(tracks[j].width_nm)
                    + 2 * i128::from(clr);
                assert!(
                    !segments_near(
                        tracks[i].start,
                        tracks[i].end,
                        tracks[j].start,
                        tracks[j].end,
                        gap,
                        false
                    ),
                    "clearance violation between {:?} and {:?}",
                    tracks[i],
                    tracks[j]
                );
            }
        }
    }
    for v in vias {
        assert!(v.diameter_nm >= v.drill_nm);
    }
}

fn bottleneck_inversion_input() -> Input {
    // 1-layer board: Net 1 wants horizontal corridor y=50, sealing the mouth of a
    // U-shaped static obstacle wall around Net 2's pad at [50, 30].
    // Net 1 can detour above the U-wall at y=10 or below [50, 80] at y=90.
    let mut i = base_input([100, 100], Vec::new());
    i.nets = vec![
        Net {
            id: 0,
            name: String::new(),
        },
        Net {
            id: 1,
            name: "A".into(),
        },
        Net {
            id: 2,
            name: "B".into(),
        },
    ];
    i.pads = vec![
        pad(1, 1, [10, 50]),
        pad(2, 1, [90, 50]),
        pad(3, 2, [50, 30]),
        pad(4, 2, [50, 80]),
    ];
    // U-box around [50, 30]: top y=20, left x=40, right x=60, open at y=50.
    i.tracks = vec![
        wire(10, 0, [40, 20], [60, 20]),
        wire(11, 0, [40, 20], [40, 45]),
        wire(12, 0, [60, 20], [60, 45]),
    ];
    i
}

#[test]
fn bottleneck_order_inversion_resolves_via_ripup_and_preserves_input() {
    let i = bottleneck_inversion_input();
    let mut e = Engine::new(i.clone()).unwrap();
    let original_input = e.copper().input().to_vec();
    e.route_all().unwrap();
    assert!(e.copper().index_consistent());
    assert_eq!(e.copper().input(), original_input);
    let stats = e.last_negotiation_stats().expect("stats recorded");
    assert!(stats.rounds_committed >= 1);
    assert!(stats.units_ripped >= 1);
    assert!(stats.victim_recoveries >= 1);

    let out = run(i.clone());
    assert_eq!(out.status, Status::Complete);
    assert!(out.removed_track_ids.is_empty());
    verify_disjoint_and_legal(&i, &out.added_tracks, &out.added_vias);
    // Net 2 must exit straight through [50, 50], forcing Net 1 to detour via y <= 10 or y >= 90.
    let net1: Vec<_> = out.added_tracks.iter().filter(|t| t.net == 1).collect();
    assert!(net1
        .iter()
        .any(|t| t.start[1] <= 10 || t.end[1] <= 10 || t.start[1] >= 90 || t.end[1] >= 90));
}

#[test]
fn domino_chain_and_congested_crossing_are_deterministic() {
    // 1-layer 3-net domino chain + 2-layer 6-net crossing board.
    // On 1-layer domino: Net 1 blocks Net 2's U-mouth, and Net 1's detour above Net 2
    // interacts with Net 3; rip-up + PathFinder history routes all cleanly and deterministically.
    let mut domino = base_input([140, 140], Vec::new());
    domino.nets = vec![
        Net {
            id: 0,
            name: String::new(),
        },
        Net {
            id: 1,
            name: "N1".into(),
        },
        Net {
            id: 2,
            name: "N2".into(),
        },
        Net {
            id: 3,
            name: "N3".into(),
        },
    ];
    domino.pads = vec![
        pad(1, 1, [20, 70]),
        pad(2, 1, [120, 70]),
        pad(3, 2, [70, 50]),
        pad(4, 2, [70, 100]),
        pad(5, 3, [20, 30]),
        pad(6, 3, [120, 30]),
    ];
    // U-wall around Net 2 pad [70, 50]: top y=40, left x=60, right x=80.
    domino.tracks = vec![
        wire(10, 0, [60, 40], [80, 40]),
        wire(11, 0, [60, 40], [60, 65]),
        wire(12, 0, [80, 40], [80, 65]),
    ];
    let d_a = run(domino.clone());
    let d_b = run(domino.clone());
    assert_eq!(d_a.status, Status::Complete);
    assert_eq!(
        serde_json::to_vec(&d_a).unwrap(),
        serde_json::to_vec(&d_b).unwrap()
    );
    verify_disjoint_and_legal(&domino, &d_a.added_tracks, &d_a.added_vias);

    // 6 crossing nets on 2 layers with an inner B.Cu keepout obstacle forcing F.Cu contention.
    // Use realistic nm dimensions so default 600_000 nm vias fit cleanly on 2 layers.
    let mut i = base_input([30_000_000, 30_000_000], vec!["F.Cu".into(), "B.Cu".into()]);
    i.rules.width_nm = 250_000;
    i.rules.clearance_nm = 200_000;
    i.rules.grid_nm = 500_000;
    i.rules.edge_clearance_nm = 500_000;
    for n in 1..=6u32 {
        i.nets.push(Net {
            id: n,
            name: format!("SIG_{n}"),
        });
        let y0 = 4_000_000 + i64::from(n - 1) * 3_500_000;
        let y1 = 4_000_000 + i64::from(6 - n) * 3_500_000;
        for (p_idx, pos) in [
            (u64::from(n) * 2 - 1, [4_000_000, y0]),
            (u64::from(n) * 2, [26_000_000, y1]),
        ] {
            let mut p = pad(p_idx, n, pos);
            p.shape = Shape::Circle { radius_nm: 500_000 };
            i.pads.push(p);
        }
    }
    i.rule_areas.push(RuleArea {
        id: id(900),
        name: None,
        layers: vec!["B.Cu".into()],
        outer: vec![
            [11_000_000, 0],
            [19_000_000, 0],
            [19_000_000, 15_000_000],
            [11_000_000, 15_000_000],
        ],
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        no_tracks: true,
        no_vias: true,
        no_pads: false,
        no_footprints: false,
        no_zone_fills: false,
    });
    let out_a = run(i.clone());
    let out_b = run(i.clone());
    assert_eq!(out_a.status, Status::Complete);
    assert_eq!(
        serde_json::to_vec(&out_a).unwrap(),
        serde_json::to_vec(&out_b).unwrap()
    );
    assert!(out_a.removed_track_ids.is_empty());
    verify_disjoint_and_legal(&i, &out_a.added_tracks, &out_a.added_vias);
}

#[test]
fn unrecoverable_victim_rolls_back_outer_transaction_and_preserves_allocator() {
    let mut i = bottleneck_inversion_input();
    // Add Net 3 sealed inside an immutable input copper wall.
    i.nets.push(Net {
        id: 3,
        name: "TRAPPED".into(),
    });
    i.pads.extend([pad(5, 3, [20, 80]), pad(6, 3, [80, 80])]);
    i.tracks.extend([
        wire(20, 0, [15, 75], [25, 75]),
        wire(21, 0, [25, 75], [25, 85]),
        wire(22, 0, [25, 85], [15, 85]),
        wire(23, 0, [15, 85], [15, 75]),
    ]);
    let mut e = Engine::new(i.clone()).unwrap();
    let orig_input = e.copper().input().to_vec();
    let err = e.route_all().unwrap_err();
    assert_eq!(err.status, Status::Unroutable);
    assert_eq!(e.copper().generated().count(), 0);
    assert!(e.copper().index_consistent());
    assert_eq!(e.copper().input(), orig_input);

    // Subsequent route(1, ...) must match a fresh Engine byte-for-byte (allocator restored).
    e.route(1, 10_000).unwrap();
    let mut fresh = Engine::new(i.clone()).unwrap();
    fresh.route(1, 10_000).unwrap();
    assert_eq!(
        serde_json::to_vec(&e.copper().generated().collect::<Vec<_>>()).unwrap(),
        serde_json::to_vec(&fresh.copper().generated().collect::<Vec<_>>()).unwrap()
    );

    let out = run(i);
    assert_eq!(out.status, Status::Unroutable);
    assert!(out.added_tracks.is_empty());
    assert!(out.added_vias.is_empty());
    assert!(out.removed_track_ids.is_empty());
}

#[test]
fn work_and_journal_exhaustion_mid_ripup_abort_outer_transaction_cleanly() {
    let base = bottleneck_inversion_input();
    let full = run(base.clone());
    assert_eq!(full.status, Status::Complete);
    let needed_work = full.work_used;

    // Sample cutoffs across initial routing and rip-up negotiation.
    for limit in [0, 1, 10, 50, needed_work / 2, needed_work - 1] {
        let mut i = base.clone();
        i.limits.max_work = limit;
        let out = run(i);
        assert_eq!(out.status, Status::WorkExhausted, "limit={limit}");
        assert!(out.work_used <= limit);
        assert!(out.added_tracks.is_empty());
        assert!(out.added_vias.is_empty());
        assert!(out.removed_track_ids.is_empty());
    }

    // Journal exhaustion during rip-up/reroute.
    let mut j_input = base.clone();
    j_input.limits.max_journal_entries = 1;
    let out_j = run(j_input);
    assert_eq!(out_j.status, Status::JournalExhausted);
    assert!(out_j.added_tracks.is_empty());
    assert!(out_j.removed_track_ids.is_empty());
}

#[test]
fn pinned_and_active_nets_protect_existing_copper() {
    let mut i = bottleneck_inversion_input();
    i.nets.push(Net {
        id: 3,
        name: "SKIPPED".into(),
    });
    i.pads.extend([pad(7, 3, [10, 10]), pad(8, 3, [20, 10])]);
    let mut e = Engine::new(i).unwrap();
    // Pre-route net 1 cleanly in an outer transaction, then run route_all_negotiated
    // with net 1 pinned and active_nets = {2} (net 3 excluded).
    e.route(1, 10_000).unwrap();
    let net1_before: Vec<Track> = e
        .copper()
        .generated()
        .filter(|t| t.net == 1)
        .cloned()
        .collect();
    assert!(!net1_before.is_empty());

    // Pinning net 1 prevents ripping it, so net 2 cannot exit its U-wall.
    let pinned: BTreeSet<u32> = [1].into_iter().collect();
    let active: BTreeSet<u32> = [2].into_iter().collect();
    let err = negotiate::route_all_negotiated(&mut e, &pinned, Some(&active)).unwrap_err();
    assert_eq!(err.status, Status::Unroutable);
    let net1_after: Vec<Track> = e
        .copper()
        .generated()
        .filter(|t| t.net == 1)
        .cloned()
        .collect();
    assert_eq!(
        serde_json::to_vec(&net1_before).unwrap(),
        serde_json::to_vec(&net1_after).unwrap()
    );
    // Net 3 was excluded by active_nets.
    assert_eq!(e.copper().generated().filter(|t| t.net == 3).count(), 0);
}

struct TieredMockAdapter {
    specs: Vec<UnitSpec>,
    net_map: BTreeMap<u32, RouteUnitId>,
    ripped: Vec<RouteUnitId>,
}

impl RouteUnitAdapter for TieredMockAdapter {
    fn units(&self, _engine: &Engine) -> Result<Vec<UnitSpec>, Failure> {
        Ok(self.specs.clone())
    }

    fn unit_for_net(&self, net: u32) -> Option<RouteUnitId> {
        self.net_map.get(&net).copied()
    }

    fn route_unit(
        &mut self,
        engine: &mut Engine,
        unit: RouteUnitId,
        _history: &CongestionHistory,
        max_expansions: u64,
        expanded: &mut u64,
    ) -> Result<(), UnitRouteError> {
        let spec = self.specs.iter().find(|s| s.id == unit).unwrap();
        for &net in &spec.nets {
            match engine.route(net, max_expansions) {
                Ok(exp) => *expanded = expanded.saturating_add(exp),
                Err(e) if e.status == Status::Unroutable => {
                    let mut frontier = BlockedFrontier::new();
                    frontier.push_edge(0, [50, 40], [50, 50]);
                    return Err(UnitRouteError::Blocked(frontier));
                }
                Err(e) => return Err(UnitRouteError::Fatal(e)),
            }
        }
        Ok(())
    }

    fn rip_unit(&mut self, _engine: &mut Engine, unit: RouteUnitId) -> Result<(), Failure> {
        self.ripped.push(unit);
        Ok(())
    }

    fn is_unit_complete(&self, _engine: &Engine, _unit: RouteUnitId) -> Result<bool, Failure> {
        Ok(true)
    }
}

#[test]
fn route_unit_adapter_never_rips_non_rippable_diff_pair_for_single_net() {
    let i = bottleneck_inversion_input();
    let mut e = Engine::new(i).unwrap();
    let specs = vec![
        UnitSpec {
            id: RouteUnitId::DiffPair(0),
            nets: vec![1],
            priority_tier: 0,
            rippable_by_single: false,
        },
        UnitSpec {
            id: RouteUnitId::SingleNet(2),
            nets: vec![2],
            priority_tier: 1,
            rippable_by_single: true,
        },
    ];
    let net_map = [
        (1, RouteUnitId::DiffPair(0)),
        (2, RouteUnitId::SingleNet(2)),
    ]
    .into_iter()
    .collect();
    let mut adapter = TieredMockAdapter {
        specs,
        net_map,
        ripped: Vec::new(),
    };
    let mut expanded = 0;
    let err = e
        .transaction(|tx| negotiate::solve_with_adapter(tx, &mut adapter, &mut expanded))
        .unwrap_err();
    assert!(
        adapter.ripped.is_empty(),
        "DiffPair(0) must never be ripped by SingleNet"
    );
    let failure: Failure = err.into();
    assert_eq!(failure.status, Status::Unroutable);
}

#[test]
fn multiterminal_victim_whole_net_repair_and_zone_corridor_preservation() {
    // 3-terminal Net 1 blocks Net 2; ripping Net 1 must repair all 3 terminals of Net 1
    // while respecting a B.Cu GND thermal spoke corridor and Power netclass width.
    let mut i = base_input([120, 120], vec!["F.Cu".into(), "B.Cu".into()]);
    i.nets = vec![
        Net {
            id: 0,
            name: String::new(),
        },
        Net {
            id: 1,
            name: "SIG1".into(),
        },
        Net {
            id: 2,
            name: "PWR".into(),
        },
        Net {
            id: 3,
            name: "GND".into(),
        },
    ];
    i.pads = vec![
        // 3-terminal Net 1 (straight path passes [60, 60], sealing PWR's U-mouth)
        pad(1, 1, [10, 60]),
        pad(2, 1, [30, 60]),
        pad(3, 1, [110, 60]),
        // Net 2 inside U-wall on F.Cu, B.Cu blocked by keepout
        pad(4, 2, [60, 40]),
        pad(5, 2, [60, 100]),
        // GND pad inside B.Cu zone
        pad(6, 3, [20, 20]),
        pad(7, 3, [100, 20]),
    ];
    i.tracks = vec![
        wire(10, 0, [50, 30], [70, 30]),
        wire(11, 0, [50, 30], [50, 55]),
        wire(12, 0, [70, 30], [70, 55]),
    ];
    i.rule_areas.push(RuleArea {
        id: id(901),
        name: None,
        layers: vec!["B.Cu".into()],
        outer: vec![[0, 0], [120, 0], [120, 120], [0, 120]],
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        no_tracks: true,
        no_vias: true,
        no_pads: false,
        no_footprints: false,
        no_zone_fills: false,
    });
    i.zones.push(Zone {
        id: id(902),
        name: Some("GND_B".into()),
        net: 3,
        layers: vec!["B.Cu".into()],
        priority: 0,
        connection: ZoneConnection::Thermal,
        thermal_gap_nm: 2,
        thermal_spoke_width_nm: 2,
        min_thickness_nm: 1,
        local_clearance_nm: 1,
        island_removal: ZoneIslandRemoval::Always,
        island_area_min_nm2: 0,
        fill_mode: ZoneFillMode::Polygon,
        refillable: true,
        filled: true,
        locked: false,
        outer: vec![[10, 10], [110, 10], [110, 30], [10, 30]],
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        layer_fills: Vec::new(),
    });
    i.project_rules = Some(
        serde_json::json!({
            "meta": {"version": 1},
            "net_settings": {
                "classes": [
                    {"name": "Default", "priority": 0, "clearance": 0.000001, "track_width": 0.000002, "via_diameter": 0.000004, "via_drill": 0.000002},
                    {"name": "Power", "priority": 1, "clearance": 0.000001, "track_width": 0.000004, "via_diameter": 0.000004, "via_drill": 0.000002}
                ],
                "netclass_patterns": [{"netclass": "Power", "pattern": "PWR"}]
            }
        })
        .to_string(),
    );
    let out = run(i);
    assert_eq!(out.status, Status::Complete);
    let net2: Vec<_> = out.added_tracks.iter().filter(|t| t.net == 2).collect();
    assert!(!net2.is_empty());
    assert!(net2.iter().all(|t| t.width_nm >= 4));
    let net1: Vec<_> = out.added_tracks.iter().filter(|t| t.net == 1).collect();
    assert!(!net1.is_empty());
}

#[test]
fn eight_crossing_nets_converge_within_compact_work_budget() {
    let mut i = base_input([30_000_000, 30_000_000], vec!["F.Cu".into(), "B.Cu".into()]);
    i.rules.width_nm = 200_000;
    i.rules.clearance_nm = 200_000;
    i.rules.grid_nm = 200_000;
    i.rules.edge_clearance_nm = 500_000;
    i.rules.hole_clearance_nm = 250_000;
    i.rules.same_net_hole_clearance_nm = 0;
    i.max_expansions = 200_000;
    i.board_design_settings = Some(kicad_route_engine::rules::BoardDesignSettings {
        min_track_width_nm: 200_000,
        min_clearance_nm: 200_000,
        min_edge_clearance_nm: 500_000,
        min_via_diameter_nm: 500_000,
        min_via_annular_width_nm: 100_000,
        min_through_hole_diameter_nm: 300_000,
        min_hole_clearance_nm: 250_000,
        min_hole_to_hole_nm: 250_000,
        min_microvia_diameter_nm: 200_000,
        min_microvia_drill_nm: 100_000,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
        default_clearance_nm: 200_000,
        default_track_width_nm: 200_000,
        default_via_diameter_nm: 600_000,
        default_via_drill_nm: 300_000,
        default_microvia_diameter_nm: 300_000,
        default_microvia_drill_nm: 150_000,
        min_resolved_spokes: None,
        max_error_nm: None,
        ..Default::default()
    });
    let pairs = [
        ([4_000_000, 25_000_000], [26_000_000, 4_000_000]),
        ([4_000_000, 10_000_000], [26_000_000, 19_000_000]),
        ([4_000_000, 22_000_000], [26_000_000, 7_000_000]),
        ([4_000_000, 7_000_000], [26_000_000, 22_000_000]),
        ([4_000_000, 13_000_000], [26_000_000, 16_000_000]),
        ([4_000_000, 4_000_000], [26_000_000, 25_000_000]),
        ([4_000_000, 16_000_000], [26_000_000, 13_000_000]),
        ([4_000_000, 19_000_000], [26_000_000, 10_000_000]),
    ];
    i.nets = (1..=8)
        .map(|n| Net {
            id: n,
            name: format!("SIG_{n}"),
        })
        .collect();
    let mut pid = 10u64;
    for (idx, (a, b)) in pairs.iter().enumerate() {
        let net = (idx + 1) as u32;
        let mut p1 = pad(pid, net, *a);
        p1.shape = Shape::Circle { radius_nm: 500_000 };
        pid += 1;
        let mut p2 = pad(pid, net, *b);
        p2.shape = Shape::Circle { radius_nm: 500_000 };
        pid += 1;
        i.pads.extend([p1, p2]);
    }
    let out_a = run(i.clone());
    assert_eq!(out_a.status, Status::Complete);
    verify_disjoint_and_legal(&i, &out_a.added_tracks, &out_a.added_vias);
    let out_b = run(i.clone());
    assert_eq!(out_b.status, Status::Complete);
    assert_eq!(out_a.work_used, out_b.work_used);
    assert_eq!(out_a.expanded_states, out_b.expanded_states);
    assert_eq!(
        serde_json::to_vec(&out_a.added_tracks).unwrap(),
        serde_json::to_vec(&out_b.added_tracks).unwrap()
    );
    let mut engine = Engine::new(i).unwrap();
    let exp = engine.route_all().unwrap();
    assert_eq!(exp, out_a.expanded_states);
    assert!(
        out_a.work_used <= 5_000_000,
        "expected eight_crossing_nets work_used <= 5_000_000, got {}",
        out_a.work_used
    );
    let stats = engine.last_negotiation_stats().unwrap();
    assert_eq!(stats.initial_routed + stats.initial_failed, 8);
}

#[test]
fn tier1_single_net_never_rips_same_tier_non_rippable_unit() {
    let i = bottleneck_inversion_input();
    let mut e = Engine::new(i.clone()).unwrap();
    let specs = vec![
        UnitSpec {
            id: RouteUnitId::SingleNet(1),
            nets: vec![1],
            priority_tier: 1,
            rippable_by_single: false,
        },
        UnitSpec {
            id: RouteUnitId::SingleNet(2),
            nets: vec![2],
            priority_tier: 1,
            rippable_by_single: true,
        },
    ];
    let net_map = [
        (1, RouteUnitId::SingleNet(1)),
        (2, RouteUnitId::SingleNet(2)),
    ]
    .into_iter()
    .collect();
    let mut adapter = TieredMockAdapter {
        specs,
        net_map,
        ripped: Vec::new(),
    };
    let mut expanded = 0;
    let err = e
        .transaction(|tx| negotiate::solve_with_adapter(tx, &mut adapter, &mut expanded))
        .unwrap_err();
    assert!(
        adapter.ripped.is_empty(),
        "same-tier non-rippable SingleNet(1) must never be ripped: {:?}",
        adapter.ripped
    );
    let failure: Failure = err.into();
    assert_eq!(failure.status, Status::Unroutable);
    let stats = e.last_negotiation_stats().expect("stats recorded");
    assert!(
        stats.global_passes <= 1,
        "expected early exit <= 1 global pass, got {}",
        stats.global_passes
    );

    // Companion: at priority_tier 0, DiffPair(1) CAN rip DiffPair(0) if DiffPair(0) is blocking it
    // (though global_passes keeps !rippable_by_single pinned). Let's verify local rip succeeds.
    let mut e2 = Engine::new(i).unwrap();
    let dp_specs = vec![
        UnitSpec {
            id: RouteUnitId::DiffPair(0),
            nets: vec![1],
            priority_tier: 0,
            rippable_by_single: false,
        },
        UnitSpec {
            id: RouteUnitId::DiffPair(1),
            nets: vec![2],
            priority_tier: 0,
            rippable_by_single: true,
        },
    ];
    let dp_map = [(1, RouteUnitId::DiffPair(0)), (2, RouteUnitId::DiffPair(1))]
        .into_iter()
        .collect();
    let mut dp_adapter = TieredMockAdapter {
        specs: dp_specs,
        net_map: dp_map,
        ripped: Vec::new(),
    };
    let mut expanded2 = 0;
    let _ = e2.transaction(|tx| negotiate::solve_with_adapter(tx, &mut dp_adapter, &mut expanded2));
    assert!(
        dp_adapter.ripped.contains(&RouteUnitId::DiffPair(0)),
        "tier-0 DiffPair(1) should be allowed to rip tier-0 DiffPair(0)"
    );
}

#[test]
fn unblocked_net_exceeding_trial_expansion_cap_completes_without_thrash() {
    // Net 1 routes cleanly; Net 2 sits above a wide static wall requiring > 40_000 expansions
    // whose frontier grazes Net 1's generated track before finding the open detour.
    // With max_expansions = 200_000, trial_cap = 40_000 triggers and must fall back cleanly.
    let mut i = base_input([2600, 2000], Vec::new());
    i.max_expansions = 200_000;
    i.limits.max_work = 50_000_000;
    i.nets = vec![
        Net {
            id: 0,
            name: String::new(),
        },
        Net {
            id: 1,
            name: "N1".into(),
        },
        Net {
            id: 2,
            name: "N2".into(),
        },
    ];
    i.pads = vec![
        pad(1, 1, [1250, 1130]),
        pad(2, 1, [1350, 1130]),
        pad(3, 2, [1300, 900]),
        pad(4, 2, [1300, 1400]),
    ];
    // Wide static wall y = 1150 from x = 200 to x = 2400: forces > 40k expansions before detouring
    // around x < 200 or x > 2400.
    i.tracks = vec![wire(10, 0, [200, 1150], [2400, 1150])];

    let out = run(i.clone());
    assert_eq!(out.status, Status::Complete);
    assert!(
        out.expanded_states > 40_000,
        "expected Net 2 to exceed trial_cap (40_000), got {}",
        out.expanded_states
    );
    verify_disjoint_and_legal(&i, &out.added_tracks, &out.added_vias);
}

#[test]
fn static_unrecoverable_exits_early_without_wasting_global_passes() {
    // Net 1 routes cleanly (routed_rippable_count == 1); Net 2 is sealed inside a closed static box.
    let mut i = base_input([100, 100], Vec::new());
    i.nets = vec![
        Net {
            id: 0,
            name: String::new(),
        },
        Net {
            id: 1,
            name: "N1".into(),
        },
        Net {
            id: 2,
            name: "SEALED".into(),
        },
    ];
    i.pads = vec![
        pad(1, 1, [10, 20]),
        pad(2, 1, [90, 20]),
        pad(3, 2, [50, 50]),
        pad(4, 2, [50, 90]),
    ];
    i.tracks = vec![
        wire(10, 0, [40, 40], [60, 40]),
        wire(11, 0, [60, 40], [60, 60]),
        wire(12, 0, [60, 60], [40, 60]),
        wire(13, 0, [40, 60], [40, 40]),
    ];
    let mut e = Engine::new(i).unwrap();
    let err = e.route_all().unwrap_err();
    assert_eq!(err.status, Status::Unroutable);
    let stats = e.last_negotiation_stats().expect("stats recorded");
    assert_eq!(stats.initial_routed, 1);
    assert_eq!(stats.global_passes, 0);
}

#[test]
fn blocked_frontier_finds_generated_victim_past_4096_static_keepout_edges() {
    // Net 2 starts inside a comb of static keepout bars generating > 4,100 distinct blocked edges
    // before reaching the single narrow exit at [10, 30] sealed by Net 1.
    let mut i = base_input([1000, 700], Vec::new());
    i.max_expansions = 100_000;
    i.limits.max_work = 50_000_000;
    i.nets = vec![
        Net {
            id: 0,
            name: String::new(),
        },
        Net {
            id: 1,
            name: "VICTIM".into(),
        },
        Net {
            id: 2,
            name: "ESCAPER".into(),
        },
    ];
    // Net 1 connects [10, 10] -> [10, 50] along x=10, sealing ESCAPER's exit at [10, 30],
    // while Net 1 can detour around [20..900] or y > 580? Wait: give Net 1 pads at [10, 10] and [40, 10]
    // with a wall forcing? Even simpler: Net 1 pads at [10, 10] and [10, 60] inside [0..1000, 0..700]:
    // Wait - Net 1 shortest path is straight down x=10 (from y=10 to y=60), sealing [10, 30]!
    // When ripped, Net 1 can detour via x=950? No, detouring via x=0..20 is blocked by keepout?
    // Wait: make the comb chamber x in [20..940], y in [50..600], closed at bottom y=610, right x=950,
    // left x=20 EXCEPT an opening at y=30? Wait: comb bars are at y=80, 100, ..., 560 from x=30..930.
    // Put a static wall around the chamber: top y=40 (x=20..950), right x=950 (y=40..610),
    // bottom y=610 (x=20..950), left x=20 (y=60..610) — leaving ONLY [20, 50] as the exit!
    // Wait: Net 1 connects [10, 20] -> [10, 80] along x=10? If Net 1 goes along x=10, it doesn't seal [20, 50] unless clearance reaches x=20,
    // OR put Net 1 pads at [10, 50] and [30, 50]? No, if Net 1 is [20, 20] -> [20, 80] (pads at [20, 20] and [20, 80] - no, keepout is at y=40),
    // put Net 1 pads at [10, 20] and [10, 100] with x=10 as the exit shaft (wall at x=0, wall at x=20 y=60..610, wall at x=20 y=0..40)!
    // Wait: if Net 1 routes along x=10 from y=20 to y=100, and ESCAPER exits at [10, 50] -> [500, 20],
    // then Net 1 can detour via x=30? No, x=20 has a wall y=0..40 and y=60..610! So Net 1 can detour via [20, 50] if ESCAPER isn't there, wait:
    // ESCAPER needs to go from [500, 580] -> [500, 20] through [20, 50]!
    // If Net 1 goes [10, 10] -> [50, 10]? No, Net 1 pads [10, 20] and [50, 20]?
    // Let's keep it super simple:
    // Left wall x=0? No, board min x=0, track width=2, grid=10, so x=10 is legal!
    // Chamber: top wall y=40 (x=20..960), right wall x=960 (y=40..640), bottom wall y=640 (x=10..960),
    // left wall x=10 (y=60..640) and x=20 (y=0..40? No, x=20 y=40..640 has gap at y=50? Wait: x=20 y=60..640).
    // Exit is the gap at y=50, x=20! Net 2 (ESCAPER) pad A is at [500, 580] inside the chamber, pad B is at [500, 20] outside!
    // Net 1 (VICTIM) has pad A at [20, 30] and pad B at [20, 90]? Wait: [20, 90] is inside the chamber if left wall is x=10!
    // Put Net 1 pads at [20, 30] and [10, 90]? No, then Net 1 ALSO uses the exit!
    // Make the exit corridor horizontal at y=50 from x=20 to x=10, and outside x=10 is open!
    // Put Net 1 pads at [10, 10] and [10, 90], with wall on x=20 (y=0..40 and y=60..640) -> no, Net 1 can detour via x=5? No, grid=10.
    // Even simpler: put Net 1 pads at [10, 50] and [980, 50]? No, that goes through x=20..960.
    // Put Net 1 pads at [10, 30] and [10, 70] -> straight path is x=10, y=30..70, which crosses [10, 50] (ESCAPER's ONLY exit!).
    // Wait: how can Net 1 detour around [10, 50]? On the LEFT at x=0? Board min is [-20, 0]..[1000, 700]!
    // YES! Board min [-30, 0]..[1000, 700]!
    // Put a static wall along x=20 from y=60..640 and y=0..40 (gap ONLY at y=50).
    // Net 1 pads at [10, 30] and [10, 70]: straight path crosses [10, 50] (sealing ESCAPER's exit at [10, 50]!),
    // while Net 1 can easily detour via x = -10 (around y=30..70) once ripped!
    // Wait: if x=10 is open at y=50, ESCAPER goes [500, 580] -> comb -> [20, 50] -> [10, 50] -> [10, 10] -> [500, 20]!
    // And inside the chamber x in [30..950], y in [60..620], place 25 horizontal `no_tracks` RuleArea bars
    // at y = 70, 90, ..., 550 (each spanning x = 40..940, height 6 nm -> leaves y=60, 80, 100... open,
    // while blocking vertical edges between every even-10 row!).
    // Every cell (x, y) in x=40..940 (91 cols) x 25 rows = 2,275 cells * 2 vertical blocked edges = 4,550 distinct static blocked edges!
    i.bounds = Bounds {
        min: [-50, 0],
        max: [1000, 700],
    };
    i.pads = vec![
        pad(1, 1, [10, 30]),
        pad(2, 1, [10, 70]),
        pad(3, 2, [500, 580]),
        pad(4, 2, [-20, 50]),
    ];
    i.tracks = vec![
        // Chamber walls around x=20..960, y=40..640 with single exit at [20, 50] -> [10, 50]
        wire(10, 0, [20, 0], [20, 40]),
        wire(11, 0, [20, 60], [20, 640]),
        wire(12, 0, [20, 40], [960, 40]),
        wire(13, 0, [960, 40], [960, 640]),
        wire(14, 0, [20, 640], [960, 640]),
    ];
    for row in 0..25i64 {
        let y_mid = 70 + row * 20;
        i.rule_areas.push(RuleArea {
            id: id(100 + row as u64),
            name: None,
            layers: vec!["F.Cu".into()],
            outer: vec![
                [40, y_mid - 3],
                [940, y_mid - 3],
                [940, y_mid + 3],
                [40, y_mid + 3],
            ],
            holes: Vec::new(),
            extra_polygons: Vec::new(),
            no_tracks: true,
            no_vias: false,
            no_pads: false,
            no_footprints: false,
            no_zone_fills: false,
        });
    }

    let mut e = Engine::new(i.clone()).unwrap();
    e.route_all().unwrap();
    let stats = e.last_negotiation_stats().expect("stats recorded");
    assert!(
        stats.rounds_committed >= 1 || stats.global_passes >= 1,
        "expected rip-up attribution past 4096 static keepout edges, got {stats:?}"
    );
    let out = run(i.clone());
    assert_eq!(out.status, Status::Complete);
    verify_disjoint_and_legal(&i, &out.added_tracks, &out.added_vias);
}

#[test]
fn multi_zone_thermal_spoke_corridors_preserved_under_multinet_ripup() {
    // 2-layer board with two distinct refillable thermal zones (GND on F.Cu, PWR on B.Cu)
    // and no full-layer keepout masking B.Cu, plus 3 crossing signal nets forcing rip-up.
    let mut i = base_input([160, 160], vec!["F.Cu".into(), "B.Cu".into()]);
    i.nets = vec![
        Net {
            id: 0,
            name: String::new(),
        },
        Net {
            id: 1,
            name: "SIG1".into(),
        },
        Net {
            id: 2,
            name: "SIG2".into(),
        },
        Net {
            id: 3,
            name: "GND".into(),
        },
        Net {
            id: 4,
            name: "PWR".into(),
        },
        Net {
            id: 5,
            name: "SIG3".into(),
        },
    ];
    let mut gnd_pad = pad(6, 3, [30, 30]);
    gnd_pad.layers = vec!["F.Cu".into()];
    let mut gnd_pad2 = pad(7, 3, [130, 30]);
    gnd_pad2.layers = vec!["F.Cu".into()];
    let mut pwr_pad = pad(8, 4, [130, 130]);
    pwr_pad.layers = vec!["B.Cu".into()];
    let mut pwr_pad2 = pad(9, 4, [30, 130]);
    pwr_pad2.layers = vec!["B.Cu".into()];
    i.pads = vec![
        pad(1, 1, [10, 80]),
        pad(2, 1, [150, 80]),
        pad(3, 2, [80, 60]),
        pad(4, 2, [80, 140]),
        pad(5, 5, [10, 40]),
        pad(10, 5, [150, 120]),
        gnd_pad,
        gnd_pad2,
        pwr_pad,
        pwr_pad2,
    ];
    // U-wall on F.Cu around SIG2 [80, 60]; no B.Cu full-layer keepout so B.Cu is routable too.
    i.tracks = vec![
        wire(20, 0, [70, 50], [90, 50]),
        wire(21, 0, [70, 50], [70, 75]),
        wire(22, 0, [90, 50], [90, 75]),
    ];
    i.project_rules = Some(
        serde_json::json!({
            "meta": {"version": 1},
            "net_settings": {
                "classes": [
                    {"name": "Default", "priority": 0, "clearance": 0.000001, "track_width": 0.000002, "via_diameter": 0.000004, "via_drill": 0.000002}
                ]
            }
        })
        .to_string(),
    );
    i.zones = vec![
        Zone {
            id: id(901),
            name: Some("GND_F".into()),
            net: 3,
            layers: vec!["F.Cu".into()],
            priority: 0,
            connection: ZoneConnection::Thermal,
            thermal_gap_nm: 2,
            thermal_spoke_width_nm: 2,
            min_thickness_nm: 1,
            local_clearance_nm: 1,
            island_removal: ZoneIslandRemoval::Always,
            island_area_min_nm2: 0,
            fill_mode: ZoneFillMode::Polygon,
            refillable: true,
            filled: true,
            locked: false,
            outer: vec![[15, 15], [145, 15], [145, 45], [15, 45]],
            holes: Vec::new(),
            extra_polygons: Vec::new(),
            layer_fills: Vec::new(),
        },
        Zone {
            id: id(902),
            name: Some("PWR_B".into()),
            net: 4,
            layers: vec!["B.Cu".into()],
            priority: 0,
            connection: ZoneConnection::Thermal,
            thermal_gap_nm: 2,
            thermal_spoke_width_nm: 2,
            min_thickness_nm: 1,
            local_clearance_nm: 1,
            island_removal: ZoneIslandRemoval::Always,
            island_area_min_nm2: 0,
            fill_mode: ZoneFillMode::Polygon,
            refillable: true,
            filled: true,
            locked: false,
            outer: vec![[15, 115], [145, 115], [145, 145], [15, 145]],
            holes: Vec::new(),
            extra_polygons: Vec::new(),
            layer_fills: Vec::new(),
        },
    ];
    let out = run(i.clone());
    assert_eq!(out.status, Status::Complete);
    verify_disjoint_and_legal(&i, &out.added_tracks, &out.added_vias);
    // Verify no foreign track crosses the GND thermal pad spoke corridor on F.Cu or PWR on B.Cu.
    for t in &out.added_tracks {
        if t.net != 3 && t.layer == "F.Cu" {
            assert!(
                !segments_near(t.start, t.end, [30, 30], [30, 30], 6, false),
                "foreign F.Cu track intruded GND thermal spoke corridor: {t:?}"
            );
        }
        if t.net != 4 && t.layer == "B.Cu" {
            assert!(
                !segments_near(t.start, t.end, [130, 130], [130, 130], 6, false),
                "foreign B.Cu track intruded PWR thermal spoke corridor: {t:?}"
            );
        }
    }
}

#[test]
fn seed_via_blocker_under_no_tracks_keepout_attributes_other_layer_victim() {
    // 2-layer board: Net 1 routes on B.Cu across [50, 50];
    // Net 2 has an F.Cu SMD pad at [50, 50] covered by an F.Cu no_tracks=true, no_vias=false keepout.
    // Net 2 can ONLY escape via a through-via at [50, 50], which Net 1's B.Cu track blocks.
    let mut i = base_input([100, 100], vec!["F.Cu".into(), "B.Cu".into()]);
    i.nets = vec![
        Net {
            id: 0,
            name: String::new(),
        },
        Net {
            id: 1,
            name: "VICTIM_B".into(),
        },
        Net {
            id: 2,
            name: "ESCAPER_VIA".into(),
        },
    ];
    let mut p1 = pad(1, 1, [10, 50]);
    p1.layers = vec!["B.Cu".into()];
    let mut p2 = pad(2, 1, [90, 50]);
    p2.layers = vec!["B.Cu".into()];
    let mut p3 = pad(3, 2, [50, 50]);
    p3.layers = vec!["F.Cu".into()];
    let mut p4 = pad(4, 2, [50, 90]);
    p4.layers = vec!["B.Cu".into()];
    i.pads = vec![p1, p2, p3, p4];
    i.project_rules = Some(
        serde_json::json!({
            "meta": {"version": 1},
            "net_settings": {
                "classes": [
                    {"name": "Default", "priority": 0, "clearance": 0.000001, "track_width": 0.000002, "via_diameter": 0.000004, "via_drill": 0.000002}
                ]
            }
        })
        .to_string(),
    );
    i.rule_areas.push(RuleArea {
        id: id(905),
        name: None,
        layers: vec!["F.Cu".into()],
        outer: vec![[45, 45], [55, 45], [55, 55], [45, 55]],
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        no_tracks: true,
        no_vias: false,
        no_pads: false,
        no_footprints: false,
        no_zone_fills: false,
    });

    let mut e = Engine::new(i.clone()).unwrap();
    e.route_all().unwrap();
    let stats = e.last_negotiation_stats().expect("stats recorded");
    assert!(
        stats.rounds_committed >= 1,
        "seed !track_ok && !via_ok must attribute B.Cu victim via ViaSite probe: {stats:?}"
    );
    let out = run(i.clone());
    assert_eq!(out.status, Status::Complete);
    verify_disjoint_and_legal(&i, &out.added_tracks, &out.added_vias);
}

#[test]
fn multilayer_microvia_enabled_still_attributes_blocked_through_via_victim() {
    // 4-layer board with allow_microvias=true: Net 2 is boxed on F.Cu and In1.Cu (microvia 0..=1
    // is legal so via_buf is non-empty), In2.Cu has a full-layer no_tracks keepout, and Net 1
    // routes on B.Cu across [5M, 5M] blocking the 0..=3 through-via Net 2 needs to reach B.Cu.
    let mut i = base_input(
        [10_000_000, 10_000_000],
        vec![
            "F.Cu".into(),
            "In1.Cu".into(),
            "In2.Cu".into(),
            "B.Cu".into(),
        ],
    );
    i.rules.width_nm = 200_000;
    i.rules.clearance_nm = 200_000;
    i.rules.grid_nm = 1_000_000;
    i.rules.edge_clearance_nm = 500_000;
    i.rules.hole_clearance_nm = 250_000;
    i.board_design_settings = Some(kicad_route_engine::rules::BoardDesignSettings {
        min_track_width_nm: 200_000,
        min_clearance_nm: 200_000,
        min_edge_clearance_nm: 500_000,
        min_via_diameter_nm: 500_000,
        min_via_annular_width_nm: 100_000,
        min_through_hole_diameter_nm: 300_000,
        min_hole_clearance_nm: 250_000,
        min_hole_to_hole_nm: 250_000,
        min_microvia_diameter_nm: 200_000,
        min_microvia_drill_nm: 100_000,
        solder_mask_expansion_nm: 0,
        solder_mask_min_width_nm: 0,
        solder_mask_to_copper_clearance_nm: 0,
        default_clearance_nm: 200_000,
        default_track_width_nm: 200_000,
        default_via_diameter_nm: 600_000,
        default_via_drill_nm: 300_000,
        default_microvia_diameter_nm: 300_000,
        default_microvia_drill_nm: 150_000,
        min_resolved_spokes: None,
        max_error_nm: None,
        ..Default::default()
    });
    i.project_rules = Some(
        serde_json::json!({
            "meta": {"version": 1},
            "board": {
                "design_settings": {
                    "rules": {
                        "allow_microvias": true,
                        "allow_blind_buried_vias": false,
                        "min_microvia_diameter": 0.2,
                        "min_microvia_drill": 0.1
                    }
                }
            }
        })
        .to_string(),
    );
    i.nets = vec![
        Net {
            id: 0,
            name: String::new(),
        },
        Net {
            id: 1,
            name: "VICTIM_B".into(),
        },
        Net {
            id: 2,
            name: "NEED_THROUGH".into(),
        },
    ];
    let mk_pad = |pid: u64, net: u32, at: Point, layer: &str| {
        let mut p = pad(pid, net, at);
        p.shape = Shape::Circle { radius_nm: 500_000 };
        p.layers = vec![layer.into()];
        p
    };
    i.pads = vec![
        mk_pad(1, 1, [1_000_000, 5_000_000], "B.Cu"),
        mk_pad(2, 1, [9_000_000, 5_000_000], "B.Cu"),
        mk_pad(3, 2, [5_000_000, 5_000_000], "F.Cu"),
        mk_pad(4, 2, [5_000_000, 9_000_000], "B.Cu"),
    ];
    // Box Net 2 inside [4M, 4M]..[6M, 6M] on BOTH F.Cu and In1.Cu, and block tracks on In2.Cu.
    let wall = |wid: u64, layer: &str, a: Point, b: Point| {
        let mut t = wire(wid, 0, a, b);
        t.width_nm = 200_000;
        t.layer = layer.into();
        t
    };
    i.tracks = vec![
        wall(10, "F.Cu", [4_000_000, 4_000_000], [6_000_000, 4_000_000]),
        wall(11, "F.Cu", [6_000_000, 4_000_000], [6_000_000, 6_000_000]),
        wall(12, "F.Cu", [6_000_000, 6_000_000], [4_000_000, 6_000_000]),
        wall(13, "F.Cu", [4_000_000, 6_000_000], [4_000_000, 4_000_000]),
        wall(14, "In1.Cu", [4_000_000, 4_000_000], [6_000_000, 4_000_000]),
        wall(15, "In1.Cu", [6_000_000, 4_000_000], [6_000_000, 6_000_000]),
        wall(16, "In1.Cu", [6_000_000, 6_000_000], [4_000_000, 6_000_000]),
        wall(17, "In1.Cu", [4_000_000, 6_000_000], [4_000_000, 4_000_000]),
    ];
    i.rule_areas.push(RuleArea {
        id: id(910),
        name: None,
        layers: vec!["In2.Cu".into()],
        outer: vec![
            [0, 0],
            [10_000_000, 0],
            [10_000_000, 10_000_000],
            [0, 10_000_000],
        ],
        holes: Vec::new(),
        extra_polygons: Vec::new(),
        no_tracks: true,
        no_vias: false,
        no_pads: false,
        no_footprints: false,
        no_zone_fills: false,
    });

    let mut e = Engine::new(i.clone()).unwrap();
    e.route_all().unwrap();
    let stats = e.last_negotiation_stats().expect("stats recorded");
    assert!(
        stats.rounds_committed >= 1,
        "4-layer allow_microvias=true must still attribute blocked 0..=3 through-via victim: {stats:?}"
    );
    let out = run(i.clone());
    assert_eq!(out.status, Status::Complete);
    verify_disjoint_and_legal(&i, &out.added_tracks, &out.added_vias);
}
