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
    }
}

fn verify_disjoint_and_legal(input: &Input, tracks: &[Track], vias: &[Via]) {
    let e = Engine::new(input.clone()).unwrap();
    for t in tracks {
        assert!(t.start != t.end, "degenerate zero-length track: {t:?}");
        assert!(
            e.legal_edge(t.net, t.start, t.end).is_ok() || input.layers.len() > 1,
            "unexpected error checking edge"
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
    let stats = engine.last_negotiation_stats().unwrap();
    assert!(stats.rounds_committed >= 1 || stats.global_passes >= 1);
}
