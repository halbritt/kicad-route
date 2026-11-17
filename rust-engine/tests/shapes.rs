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
