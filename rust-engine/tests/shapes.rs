//! Shape oracles use axis-aligned box gaps, independently of the production
//! segment/rectangle edge intersection and rational segment-distance predicates.
use kicad_route_engine::{geometry::*, model, model::*, run_json};
#[path = "support/contact.rs"]
mod contact;
use contact::Body;

fn pad(shape: Shape, at: Point) -> Pad {
    Pad {
        id: "10000000-0000-4000-8000-000000000001".into(),
        net: 1,
        at,
        shape,
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
fn shapes() -> Vec<Shape> {
    vec![
        Shape::Circle { radius_nm: 3 },
        Shape::Rect {
            half_size_nm: [5, 3],
        },
        Shape::RoundRect {
            half_size_nm: [5, 3],
            radius_nm: 0,
        },
        Shape::RoundRect {
            half_size_nm: [5, 3],
            radius_nm: 2,
        },
        Shape::RoundRect {
            half_size_nm: [5, 3],
            radius_nm: 3,
        },
        Shape::RoundRect {
            half_size_nm: [3, 5],
            radius_nm: 3,
        },
        Shape::RoundRect {
            half_size_nm: [3, 3],
            radius_nm: 3,
        },
    ]
}
#[test]
fn every_pad_pair_matches_independent_box_gap_oracle() {
    for sa in shapes() {
        for sb in shapes() {
            for x in -13..=13 {
                for y in -13..=13 {
                    let a = pad(sa.clone(), [0, 0]);
                    let b = pad(sb.clone(), [x, y]);
                    let expected = Body::pad(&a).contacts(Body::pad(&b));
                    assert_eq!(pads_touch(&a, &b), expected, "{sa:?} {sb:?} [{x},{y}]");
                    assert_eq!(pads_touch(&b, &a), expected);
                }
            }
        }
    }
}
#[test]
fn every_shape_swept_clearance_and_odd_width_contact_match_oracle() {
    for shape in shapes() {
        let p = pad(shape, [0, 0]);
        for x in -9..=9 {
            for y in -9..=9 {
                for width in [1, 2, 3, 6] {
                    for clearance in [0, 1, 3] {
                        for end in [[x + 4, y], [x, y + 4]] {
                            let t = Track {
                                id: p.id.clone(),
                                net: 1,
                                start: [x, y],
                                end,
                                width_nm: width,
                                locked: true,
                                layer: "F.Cu".into(),
                            };
                            assert_eq!(
                                track_near_pad(t.start, t.end, width, clearance, &p, false),
                                Body::pad(&p).violates(Body::track(&t), clearance),
                                "{p:?} {t:?} clearance={clearance}"
                            );
                            assert_eq!(
                                track_connects_pad(&t, &p),
                                Body::pad(&p).contacts(Body::track(&t)),
                                "{p:?} {t:?}"
                            );
                        }
                    }
                }
            }
        }
    }
}
#[test]
fn rounded_corners_are_not_envelope_contacts_or_rectangular_obstacles() {
    let p = pad(
        Shape::RoundRect {
            half_size_nm: [10, 10],
            radius_nm: 5,
        },
        [0, 0],
    );
    // Corner center (5,5); vector (3,4) has length5. A radius1
    // track at (11,13) is tangent to copper (vector6,8, length10)
    // with clearance4; equality is legal, moving one nm inward is illegal.
    assert!(!track_near_pad([11, 13], [11, 14], 2, 4, &p, false));
    assert!(track_near_pad([10, 13], [10, 14], 2, 4, &p, false));
    let t = Track {
        id: p.id.clone(),
        net: 1,
        start: [10, 10],
        end: [11, 11],
        width_nm: 2,
        locked: false,
        layer: "F.Cu".into(),
    };
    assert!(!track_connects_pad(&t, &p)); // envelope has a spurious corner contact
    let mut rect = p.clone();
    rect.shape = Shape::Rect {
        half_size_nm: [10, 10],
    };
    assert!(track_connects_pad(&t, &rect));
    // A diagonal supporting line to the NE corner has normal (3,4).
    // At distance6 from core corner(5,5), radius5 + track radius1
    // is tangent; translating by(-1,-1) produces 1.4nm overlap.
    let diagonal = Track {
        id: p.id.clone(),
        net: 1,
        start: [3, 14],
        end: [11, 8],
        width_nm: 2,
        locked: false,
        layer: "F.Cu".into(),
    };
    assert!(!track_near_pad(
        diagonal.start,
        diagonal.end,
        2,
        0,
        &p,
        false
    ));
    assert!(!track_connects_pad(&diagonal, &p));
    let inward = Track {
        start: [2, 13],
        end: [10, 7],
        ..diagonal
    };
    assert!(track_near_pad(inward.start, inward.end, 2, 0, &p, false));
    assert!(track_connects_pad(&inward, &p));
    let cap = pad(
        Shape::RoundRect {
            half_size_nm: [10, 5],
            radius_nm: 5,
        },
        [0, 0],
    );
    let t = |x| Track {
        id: p.id.clone(),
        net: 1,
        start: [x, 0],
        end: [x + 2, 0],
        width_nm: 3,
        locked: false,
        layer: "F.Cu".into(),
    };
    assert!(!track_connects_pad(&t(11), &cap)); // tangent after odd-width floor
    assert!(track_connects_pad(&t(10), &cap)); // one whole nm overlap
}
#[test]
fn outward_envelopes_and_degenerate_shapes_retain_exact_copper() {
    for shape in shapes() {
        let p = pad(shape, [21, -8]);
        let (lo, hi) = p.copper_bounds();
        let body = Body::pad(&p);
        assert_eq!(
            lo,
            [body.lo[0] - body.radius2 / 2, body.lo[1] - body.radius2 / 2]
        );
        assert_eq!(
            hi,
            [body.hi[0] + body.radius2 / 2, body.hi[1] + body.radius2 / 2]
        );
    }
    let t = Track {
        id: pad(shapes()[0].clone(), [0, 0]).id,
        net: 1,
        start: [-4, 8],
        end: [9, -3],
        width_nm: 3,
        locked: false,
        layer: "F.Cu".into(),
    };
    assert_eq!(t.copper_bounds(), ([-6, -5], [11, 10]));
}
#[test]
fn rounded_shape_wire_validation_is_strict_without_changing_input_version() {
    let base = include_str!("fixtures/multiterminal-native-two-nets.json");
    for (half, radius, valid) in [
        ([5, 3], 0, true),
        ([5, 3], 3, true),
        ([3, 3], 3, true),
        ([5, 3], 4, false),
        ([5, 3], -1, false),
        ([0, 3], 0, false),
        ([-1, 3], 0, false),
        ([100000001, 3], 0, false),
    ] {
        let mut value: serde_json::Value = serde_json::from_str(base).unwrap();
        value["pads"][0]["shape"] =
            serde_json::json!({"kind":"round_rect","half_size_nm":half,"radius_nm":radius});
        let out = run_json(&serde_json::to_vec(&value).unwrap());
        assert_eq!(
            out.status == Status::Invalid,
            !valid,
            "{half:?} radius={radius}: {out:?}"
        );
    }
}

fn f64_dist_seg_seg(a: Point, b: Point, c: Point, d: Point) -> f64 {
    fn dot(u: [f64; 2], v: [f64; 2]) -> f64 {
        u[0] * v[0] + u[1] * v[1]
    }
    fn dist_pt_seg(p: [f64; 2], s0: [f64; 2], s1: [f64; 2]) -> f64 {
        let v = [s1[0] - s0[0], s1[1] - s0[1]];
        let w = [p[0] - s0[0], p[1] - s0[1]];
        let len2 = dot(v, v);
        let t = if len2 == 0.0 {
            0.0
        } else {
            (dot(w, v) / len2).clamp(0.0, 1.0)
        };
        let dx = p[0] - (s0[0] + t * v[0]);
        let dy = p[1] - (s0[1] + t * v[1]);
        (dx * dx + dy * dy).sqrt()
    }
    fn cross(a: [f64; 2], b: [f64; 2], c: [f64; 2]) -> f64 {
        (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    }
    let pa = [a[0] as f64, a[1] as f64];
    let pb = [b[0] as f64, b[1] as f64];
    let pc = [c[0] as f64, c[1] as f64];
    let pd = [d[0] as f64, d[1] as f64];
    let o1 = cross(pa, pb, pc);
    let o2 = cross(pa, pb, pd);
    let o3 = cross(pc, pd, pa);
    let o4 = cross(pc, pd, pb);
    if o1 * o2 < 0.0 && o3 * o4 < 0.0 {
        return 0.0;
    }
    dist_pt_seg(pa, pc, pd)
        .min(dist_pt_seg(pb, pc, pd))
        .min(dist_pt_seg(pc, pa, pb))
        .min(dist_pt_seg(pd, pa, pb))
}

fn f64_dist_seg_poly(a: Point, b: Point, verts: &[Point]) -> f64 {
    let n = verts.len();
    let mut best = f64::INFINITY;
    for p in [a, b] {
        let in_poly = (0..n).all(|i| {
            let v0 = verts[i];
            let v1 = verts[(i + 1) % n];
            (v1[0] - v0[0]) as f64 * (p[1] - v0[1]) as f64
                - (v1[1] - v0[1]) as f64 * (p[0] - v0[0]) as f64
                >= 0.0
        });
        if in_poly {
            return 0.0;
        }
    }
    for i in 0..n {
        best = best.min(f64_dist_seg_seg(a, b, verts[i], verts[(i + 1) % n]));
    }
    best
}

#[test]
fn box_exact_odd_nm_and_offset_match_2x_oracle() {
    // 1x1x1 odd-nm centered BoxExact at (0,0) has half-extent 0.5 nm in X and Y,
    // while connectivity floors to half-extent 0 nm (point at center).
    let odd_rect = pad(
        Shape::BoxExact {
            center_2x: [20, 20],
            half_size_2x: [11, 5],
            radius_2x: 0,
        },
        [10, 10],
    );
    // Physical X spans [4.5, 15.5], Y spans [7.5, 12.5].
    // Track width=2 (radius=1), clearance=1 => required physical X >= 15.5 + 2 = 17.5.
    assert!(track_near_pad([17, 10], [17, 12], 2, 1, &odd_rect, false));
    assert!(!track_near_pad([18, 10], [18, 12], 2, 1, &odd_rect, false));
    // Exact 0.5 nm boundary with odd track width=3 (radius=1.5): 15.5 + 1.5 + 1 = 18.0!
    assert!(!track_near_pad([18, 10], [18, 12], 3, 1, &odd_rect, false));
    assert!(track_near_pad([18, 10], [18, 12], 3, 1, &odd_rect, true));
    assert!(track_near_pad([17, 10], [17, 12], 3, 1, &odd_rect, false));

    // Connectivity floors odd pad half-extent to [5, 2] -> X in [5, 15], Y in [8, 12].
    // Track width=2 (radius=1) at x=16 has 0 nm tangency (not connected), at x=15 has 1 nm overlap (connected).
    let t_tan = Track {
        id: odd_rect.id.clone(),
        net: 1,
        start: [16, 10],
        end: [20, 10],
        width_nm: 2,
        locked: false,
        layer: "F.Cu".into(),
    };
    assert!(!track_connects_pad(&t_tan, &odd_rect));
    let t_ov = Track {
        start: [15, 10],
        ..t_tan
    };
    assert!(track_connects_pad(&t_ov, &odd_rect));

    // Offset BoxExact where pad.at != copper center (pad.at = [0, 0], copper at [20, 0])
    let offset_pad = pad(
        Shape::BoxExact {
            center_2x: [40, 0],
            half_size_2x: [6, 6],
            radius_2x: 2,
        },
        [0, 0],
    );
    assert_eq!(offset_pad.copper_center(), [20, 0]);
    assert_eq!(offset_pad.copper_bounds(), ([17, -3], [23, 3]));
    assert!(!track_near_pad([0, 0], [0, 5], 2, 0, &offset_pad, false));
    assert!(track_near_pad([17, 0], [17, 5], 2, 0, &offset_pad, false));
}

#[test]
fn rotated_convex_polygon_and_capsule_match_independent_f64_sdf_oracle() {
    let diamond = Shape::ConvexPolygon {
        vertices: vec![[10, 0], [0, 10], [-10, 0], [0, -10]],
        radius_nm: 0,
    };
    let rounded_rot = Shape::ConvexPolygon {
        vertices: vec![[6, 0], [0, 6], [-6, 0], [0, -6]],
        radius_nm: 4,
    };
    let trapezoid = Shape::ConvexPolygon {
        vertices: vec![[-10, -4], [10, -4], [6, 4], [-6, 4]],
        radius_nm: 0,
    };
    let capsule = Shape::Capsule {
        start_nm: [-8, -6],
        end_nm: [8, 6],
        width_nm: 6,
    };
    for shape in [diamond, rounded_rot, trapezoid, capsule] {
        let p = pad(shape.clone(), [0, 0]);
        for x in -16..=16 {
            for y in -16..=16 {
                for (dx, dy) in [(5, 0), (0, 5), (4, 3)] {
                    let a = [x, y];
                    let b = [x + dx, y + dy];
                    let width = 3i64;
                    let clr = 2i64;
                    let threshold = width as f64 * 0.5 + clr as f64;
                    let d = match &shape {
                        Shape::ConvexPolygon {
                            vertices,
                            radius_nm,
                        } => f64_dist_seg_poly(a, b, vertices) - *radius_nm as f64,
                        Shape::Capsule {
                            start_nm,
                            end_nm,
                            width_nm,
                        } => f64_dist_seg_seg(a, b, *start_nm, *end_nm) - (*width_nm as f64) * 0.5,
                        _ => unreachable!(),
                    };
                    if (d - threshold).abs() > 1e-6 {
                        assert_eq!(
                            track_near_pad(a, b, width, clr, &p, false),
                            d < threshold,
                            "{shape:?} a={a:?} b={b:?} d={d} thr={threshold}"
                        );
                    }
                }
            }
        }
    }
}

#[test]
fn sat_convex_polys_and_pads_touch_distinguish_tangency_from_1nm_overlap() {
    let a = vec![[0, 0], [10, 0], [10, 10], [0, 10]];
    let b_tan = vec![[10, 0], [20, 0], [20, 10], [10, 10]];
    let b_ov = vec![[9, 0], [19, 0], [19, 10], [9, 10]];
    assert!(!sat_convex_polys_overlap_1nm(&a, &b_tan));
    assert!(sat_convex_polys_overlap_1nm(&a, &b_ov));

    // Rotated 45-deg diamond tangency vs 1 nm overlap
    let pa = pad(
        Shape::ConvexPolygon {
            vertices: vec![[0, -10], [10, 0], [0, 10], [-10, 0]],
            radius_nm: 0,
        },
        [0, 0],
    );
    let pb_tan = pad(
        Shape::ConvexPolygon {
            vertices: vec![[20, -10], [30, 0], [20, 10], [10, 0]],
            radius_nm: 0,
        },
        [20, 0],
    );
    let pb_ov = pad(
        Shape::ConvexPolygon {
            vertices: vec![[18, -10], [28, 0], [18, 10], [8, 0]],
            radius_nm: 0,
        },
        [18, 0],
    );
    assert!(!pads_touch(&pa, &pb_tan));
    assert!(pads_touch(&pa, &pb_ov));

    // 1nm-wide Capsule cannot achieve 1nm overlap with a zero-radius Rect.
    let thin_cap = pad(
        Shape::Capsule {
            start_nm: [0, 0],
            end_nm: [10, 0],
            width_nm: 1,
        },
        [0, 0],
    );
    let rect = pad(
        Shape::Rect {
            half_size_nm: [5, 5],
        },
        [5, 0],
    );
    assert!(!pads_touch(&thin_cap, &rect));

    // Capsule + Capsule 0nm tangency vs 1nm overlap
    let c1 = pad(
        Shape::Capsule {
            start_nm: [0, 0],
            end_nm: [10, 0],
            width_nm: 4,
        },
        [0, 0],
    );
    let c2_tan = pad(
        Shape::Capsule {
            start_nm: [0, 4],
            end_nm: [10, 4],
            width_nm: 4,
        },
        [0, 4],
    );
    let c2_ov = pad(
        Shape::Capsule {
            start_nm: [0, 3],
            end_nm: [10, 3],
            width_nm: 4,
        },
        [0, 3],
    );
    assert!(!pads_touch(&c1, &c2_tan));
    assert!(pads_touch(&c1, &c2_ov));
}

#[test]
fn arc_to_conservative_chords_strictly_encloses_true_circular_arc() {
    // 10 mm span, 2 mm rise arc (like T1-F5-04)
    let start = [1_000_000, 15_000_000];
    let mid = [6_000_000, 13_000_000];
    let end = [11_000_000, 15_000_000];
    let (chords, sagitta) = arc_to_conservative_chords(start, mid, end);
    assert_eq!(chords.first().copied(), Some(start));
    assert_eq!(chords.last().copied(), Some(end));
    assert!(chords.contains(&mid));
    assert!(
        sagitta > 0 && sagitta < 5_000,
        "unexpected sagitta={sagitta}"
    );

    // True circle: center=(6_000_000, 20_250_000), R=7_250_000.
    let cx = 6_000_000.0_f64;
    let cy = 20_250_000.0_f64;
    let r = 7_250_000.0_f64;
    let a0 = (start[1] as f64 - cy).atan2(start[0] as f64 - cx);
    let a1 = (end[1] as f64 - cy).atan2(end[0] as f64 - cx);
    for step in 0..=200 {
        let t = step as f64 / 200.0;
        let ang = a0 + t * (a1 - a0);
        let px = cx + r * ang.cos();
        let py = cy + r * ang.sin();
        let mut min_d = f64::INFINITY;
        for w in chords.windows(2) {
            let x0 = w[0][0] as f64;
            let y0 = w[0][1] as f64;
            let x1 = w[1][0] as f64;
            let y1 = w[1][1] as f64;
            let vx = x1 - x0;
            let vy = y1 - y0;
            let l2 = vx * vx + vy * vy;
            let u = if l2 == 0.0 {
                0.0
            } else {
                (((px - x0) * vx + (py - y0) * vy) / l2).clamp(0.0, 1.0)
            };
            let d = ((px - (x0 + u * vx)).powi(2) + (py - (y0 + u * vy)).powi(2)).sqrt();
            min_d = min_d.min(d);
        }
        assert!(
            min_d <= sagitta as f64,
            "arc sample deviated {min_d} > sagitta {sagitta}"
        );
    }
}

#[test]
fn simple_polygon_drill_and_mask_exact_boundaries() {
    let l_shape = vec![[0, 0], [30, 0], [30, 15], [15, 15], [15, 30], [0, 30]];
    assert!(point_in_simple_polygon([5, 5], &l_shape));
    assert!(point_in_simple_polygon([5, 25], &l_shape));
    assert!(point_in_simple_polygon([15, 15], &l_shape));
    assert!(!point_in_simple_polygon([20, 20], &l_shape));

    let outline = BoardOutline {
        polygons: vec![OutlinePolygon {
            outer: l_shape,
            holes: vec![vec![[4, 4], [8, 4], [8, 8], [4, 8]]],
        }],
    };
    assert!(!point_in_board_outline([6, 6], &outline));
    assert!(point_in_board_outline([2, 2], &outline));

    let drill = DrillShape::Circle { diameter_nm: 10 };
    assert!(!track_near_drill(
        [8, 0],
        [8, 10],
        2,
        2,
        [0, 5],
        &drill,
        false
    ));
    assert!(track_near_drill(
        [8, 0],
        [8, 10],
        2,
        2,
        [0, 5],
        &drill,
        true
    ));
    assert!(track_near_drill(
        [7, 0],
        [7, 10],
        2,
        2,
        [0, 5],
        &drill,
        false
    ));

    // Unfilled ring: track inside hollow center is legal; track hitting annulus is illegal.
    assert!(!track_near_circle(
        [-2, 0],
        [2, 0],
        2,
        1,
        [0, 0],
        10,
        2,
        false,
        false
    ));
    assert!(track_near_circle(
        [7, -2],
        [7, 2],
        2,
        1,
        [0, 0],
        10,
        2,
        false,
        false
    ));

    let mask = MaskAperture {
        id: "10000000-0000-4000-8000-000000000001".into(),
        net: None,
        group_id: None,
        layer: "F.Mask".into(),
        shape: GraphicShape::Segment {
            start: [0, 0],
            end: [0, 20],
            width_nm: 10,
        },
        expansion_nm: 2,
    };
    // Mask half-width=5 + expansion=2 + mask_clr=3 + track_half=1 = 11.
    assert!(!track_near_mask([11, 5], [11, 15], 2, 3, &mask, false));
    assert!(track_near_mask([11, 5], [11, 15], 2, 3, &mask, true));
    assert!(track_near_mask([10, 5], [10, 15], 2, 3, &mask, false));
}

#[test]
fn graphics_connect_and_graphic_connects_pad_symmetry_and_45deg_annulus() {
    let g = |n: u32, shape: GraphicShape| CopperGraphic {
        id: format!("10000000-0000-4000-8000-{n:012}"),
        net: 1,
        layer: "F.Cu".into(),
        shape,
    };
    let ring = g(
        1,
        GraphicShape::Circle {
            center: [0, 0],
            radius_nm: 10_000,
            width_nm: 200,
            filled: false,
        },
    );
    let inner_seg = g(
        2,
        GraphicShape::Segment {
            start: [-500, 0],
            end: [500, 0],
            width_nm: 200,
        },
    );
    let inner_ring = g(
        3,
        GraphicShape::Circle {
            center: [0, 0],
            radius_nm: 5_000,
            width_nm: 200,
            filled: false,
        },
    );
    let overlap_ring = g(
        4,
        GraphicShape::Circle {
            center: [10_000, 0],
            radius_nm: 5_000,
            width_nm: 200,
            filled: false,
        },
    );
    let outer_poly = g(
        5,
        GraphicShape::Polygon {
            outer: vec![
                [-20_000, -20_000],
                [20_000, -20_000],
                [20_000, 20_000],
                [-20_000, 20_000],
            ],
            holes: vec![vec![
                [-2_000, -2_000],
                [2_000, -2_000],
                [2_000, 2_000],
                [-2_000, 2_000],
            ]],
            width_nm: 0,
            filled: true,
        },
    );
    let hole_poly = g(
        6,
        GraphicShape::Polygon {
            outer: vec![
                [-1_000, -1_000],
                [1_000, -1_000],
                [1_000, 1_000],
                [-1_000, 1_000],
            ],
            holes: Vec::new(),
            width_nm: 0,
            filled: true,
        },
    );
    let inner_filled_poly = g(
        7,
        GraphicShape::Polygon {
            outer: vec![
                [4_000, 4_000],
                [6_000, 4_000],
                [6_000, 6_000],
                [4_000, 6_000],
            ],
            holes: Vec::new(),
            width_nm: 0,
            filled: true,
        },
    );
    let tangent_poly = g(
        8,
        GraphicShape::Polygon {
            outer: vec![
                [20_000, -20_000],
                [30_000, -20_000],
                [30_000, 20_000],
                [20_000, 20_000],
            ],
            holes: Vec::new(),
            width_nm: 0,
            filled: true,
        },
    );
    let arc_cross = g(
        9,
        GraphicShape::Arc {
            start: [8_000, -5_000],
            mid: [10_500, 0],
            end: [8_000, 5_000],
            width_nm: 200,
        },
    );
    let all = [
        ring.clone(),
        inner_seg.clone(),
        inner_ring.clone(),
        overlap_ring.clone(),
        outer_poly.clone(),
        hole_poly.clone(),
        inner_filled_poly.clone(),
        tangent_poly.clone(),
        arc_cross.clone(),
    ];
    for a in &all {
        for b in &all {
            assert_eq!(
                graphics_connect(a, b),
                graphics_connect(b, a),
                "asymmetric graphics_connect for {} vs {}",
                a.id,
                b.id
            );
        }
    }
    assert!(!graphics_connect(&ring, &inner_seg));
    assert!(!graphics_connect(&ring, &inner_ring));
    assert!(graphics_connect(&ring, &overlap_ring));
    assert!(!graphics_connect(&outer_poly, &hole_poly));
    assert!(graphics_connect(&outer_poly, &inner_filled_poly));
    assert!(!graphics_connect(&outer_poly, &tangent_poly));
    assert!(graphics_connect(&ring, &arc_cross));
    assert!(graphics_connect(&outer_poly, &ring));

    // Off-cardinal 45-degree pad-to-unfilled-circle connectivity across pad shapes.
    // Ring radius 10_000, half-width 100 -> copper [9_900, 10_100]. At 45 deg, 10_000 / sqrt(2) = 7071.
    for shape in [
        Shape::Circle { radius_nm: 200 },
        Shape::Rect {
            half_size_nm: [200, 200],
        },
        Shape::RoundRect {
            half_size_nm: [200, 200],
            radius_nm: 100,
        },
        Shape::BoxExact {
            center_2x: [14_142, 14_142],
            half_size_2x: [401, 401],
            radius_2x: 401,
        },
        Shape::Capsule {
            start_nm: [7_000, 7_000],
            end_nm: [7_140, 7_140],
            width_nm: 200,
        },
        Shape::ConvexPolygon {
            vertices: vec![
                [7_071, 6_871],
                [7_271, 7_071],
                [7_071, 7_271],
                [6_871, 7_071],
            ],
            radius_nm: 0,
        },
    ] {
        let on_45 = pad(shape, [7_071, 7_071]);
        assert!(
            graphic_connects_pad(&ring, &on_45),
            "45-deg pad should connect to unfilled circle: {:?}",
            on_45.shape
        );
    }
    assert!(!graphic_connects_pad(
        &ring,
        &pad(Shape::Circle { radius_nm: 200 }, [0, 0])
    ));
    assert!(!graphic_connects_pad(
        &ring,
        &pad(Shape::Circle { radius_nm: 200 }, [7_400, 7_400])
    ));
}

#[test]
fn asymmetric_rotated_convex_polygon_catches_rotation_sign_flip() {
    // Asymmetric trapezoid rotated +30 deg around origin (wider base at bottom, narrow top shifted right).
    // If rotation angle sign is flipped (+30 -> -30), vertices reflect across X-axis and this assertion fails.
    let base_verts = [[-200i64, -100], [200, -100], [100, 100], [-50, 100]];
    let rot30 = |(x, y): (i64, i64)| -> Point {
        let ang = std::f64::consts::PI / 6.0;
        let rx = (x as f64) * ang.cos() - (y as f64) * ang.sin();
        let ry = (x as f64) * ang.sin() + (y as f64) * ang.cos();
        [rx.round() as i64, ry.round() as i64]
    };
    let verts: Vec<Point> = base_verts.iter().map(|&[x, y]| rot30((x, y))).collect();
    let p = pad(
        Shape::ConvexPolygon {
            vertices: verts,
            radius_nm: 10,
        },
        [0, 0],
    );
    // Point near rotated vertex (200, -100) rotated +30 deg => (223, 13):
    // Under +30 deg, [223, 13] is inside; under -30 deg, (200, -100) maps to (123, -187), far from [223, 13]!
    assert!(track_near_pad([223, 13], [223, 20], 2, 0, &p, false));
    assert!(!track_near_pad([123, -187], [123, -180], 2, 0, &p, false));
}

#[test]
fn filled_polygons_overlap_non_crossing_u_notch_holes_and_extreme_coords() {
    let g = |n: u32, outer: Vec<Point>, holes: Vec<Vec<Point>>| CopperGraphic {
        id: format!("10000000-0000-4000-8000-{n:012}"),
        net: 1,
        layer: "F.Cu".into(),
        shape: GraphicShape::Polygon {
            outer,
            holes,
            width_nm: 0,
            filled: true,
        },
    };
    let m = 1_000_000i64;
    // Challenger 1 hole-free U-notch counterexample (overlap across [0, 1M] x [0, 3M], zero proper edge crossings).
    let u_a = g(
        1,
        vec![
            [0, 0],
            [4 * m, 0],
            [4 * m, 3 * m],
            [3 * m, 3 * m],
            [3 * m, m],
            [m, m],
            [m, 3 * m],
            [0, 3 * m],
        ],
        Vec::new(),
    );
    let u_b = g(
        2,
        vec![
            [-4 * m, 0],
            [m, 0],
            [m, 3 * m],
            [0, 3 * m],
            [0, m],
            [-3 * m, m],
            [-3 * m, 3 * m],
            [-4 * m, 3 * m],
        ],
        Vec::new(),
    );
    assert!(graphics_connect(&u_a, &u_b));
    assert!(graphics_connect(&u_b, &u_a));

    // Challenger 1 POLY MISMATCH #779 (with hole, overlap in upper/lower slabs without crossing).
    let hole_a = g(
        3,
        vec![[0, -6 * m], [4 * m, -6 * m], [4 * m, m], [0, m]],
        vec![vec![[m, -5 * m], [3 * m, -5 * m], [3 * m, 0], [m, 0]]],
    );
    let hole_b = g(
        4,
        vec![
            [-4 * m, -6 * m],
            [m, -6 * m],
            [m, m],
            [0, m],
            [0, -5 * m],
            [-3 * m, -5 * m],
            [-3 * m, m],
            [-4 * m, m],
        ],
        Vec::new(),
    );
    assert!(graphics_connect(&hole_a, &hole_b));
    assert!(graphics_connect(&hole_b, &hole_a));

    // Upper-slab-only overlap: lowest slab y=[-10, 0] is disjoint, upper slab y=[0, 10] overlaps without crossings.
    let upper_a = g(
        5,
        vec![[0, -10], [10, -10], [10, 10], [0, 10]],
        vec![vec![[1, -9], [9, -9], [9, 0], [1, 0]]],
    );
    let upper_b = g(6, vec![[2, 0], [8, 0], [8, 10], [2, 10]], Vec::new());
    assert!(graphics_connect(&upper_a, &upper_b));
    assert!(graphics_connect(&upper_b, &upper_a));

    // Extreme ±99M coordinates: zero i128 overflow; 1 nm overlap vs 0 nm tangency vs hole exclusion.
    let e = 99_000_000i64;
    let ext_a = g(
        7,
        vec![[-e, -e], [e, -e], [e, e], [-e, e]],
        vec![vec![
            [0, -e + 1],
            [e - 1, -e + 1],
            [e - 1, e - 1],
            [0, e - 1],
        ]],
    );
    let ext_b_tan = g(
        8,
        vec![[0, -e + 1], [e - 1, -e + 1], [e - 1, e - 1], [0, e - 1]],
        Vec::new(),
    );
    let ext_b_ov = g(
        9,
        vec![[-1, -e + 1], [e - 1, -e + 1], [e - 1, e - 1], [-1, e - 1]],
        Vec::new(),
    );
    assert!(!graphics_connect(&ext_a, &ext_b_tan));
    assert!(!graphics_connect(&ext_b_tan, &ext_a));
    assert!(graphics_connect(&ext_a, &ext_b_ov));
    assert!(graphics_connect(&ext_b_ov, &ext_a));

    // Width=0 unfilled Circle is consistent (false both crossing width=0 edge and inside FilledPolygon).
    let zero_ring = g(10, Vec::new(), Vec::new());
    let _ = zero_ring;
    let zero_circle = CopperGraphic {
        id: "10000000-0000-4000-8000-000000000011".into(),
        net: 1,
        layer: "F.Cu".into(),
        shape: GraphicShape::Circle {
            center: [0, 0],
            radius_nm: 5_000,
            width_nm: 0,
            filled: false,
        },
    };
    let square_poly = g(
        12,
        vec![
            [-10_000, -10_000],
            [10_000, -10_000],
            [10_000, 10_000],
            [-10_000, 10_000],
        ],
        Vec::new(),
    );
    let cross_poly = g(
        13,
        vec![
            [0, -10_000],
            [10_000, -10_000],
            [10_000, 10_000],
            [0, 10_000],
        ],
        Vec::new(),
    );
    assert!(!graphics_connect(&zero_circle, &square_poly));
    assert!(!graphics_connect(&square_poly, &zero_circle));
    assert!(!graphics_connect(&zero_circle, &cross_poly));
    assert!(!graphics_connect(&cross_poly, &zero_circle));
}
