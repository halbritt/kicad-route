//! Exact swept-copper tests. Distances are compared as integer fractions;
//! no raster sampling, floating point tolerances, or square roots.
use crate::model::{Pad, Point, Shape, Track};
fn sub(a: Point, b: Point) -> [i128; 2] {
    [
        i128::from(a[0]) - i128::from(b[0]),
        i128::from(a[1]) - i128::from(b[1]),
    ]
}
fn dot(a: [i128; 2], b: [i128; 2]) -> i128 {
    a[0] * b[0] + a[1] * b[1]
}
fn cross(a: [i128; 2], b: [i128; 2]) -> i128 {
    a[0] * b[1] - a[1] * b[0]
}
fn sq(v: i128) -> i128 {
    v.checked_mul(v)
        .expect("validated coordinate range bounds squares")
}
fn compare(a: i128, b: i128, inclusive: bool) -> bool {
    if inclusive {
        a <= b
    } else {
        a < b
    }
}
// gap2 is twice the physical distance, retaining odd-nm half widths exactly.
pub fn point_near_segment(p: Point, a: Point, b: Point, gap2: i128, inclusive: bool) -> bool {
    if gap2 < 0 {
        return false;
    }
    let v = sub(b, a);
    let w = sub(p, a);
    let len2 = dot(v, v);
    let projection = dot(w, v);
    if len2 == 0 || projection <= 0 {
        return compare(4 * dot(w, w), sq(gap2), inclusive);
    }
    if projection >= len2 {
        let e = sub(p, b);
        return compare(4 * dot(e, e), sq(gap2), inclusive);
    }
    compare(
        4 * sq(cross(v, w)),
        sq(gap2)
            .checked_mul(len2)
            .expect("validated geometry product"),
        inclusive,
    )
}
fn orientation(a: Point, b: Point, c: Point) -> i128 {
    cross(sub(b, a), sub(c, a))
}
fn on(a: Point, b: Point, p: Point) -> bool {
    (0..2).all(|i| p[i] >= a[i].min(b[i]) && p[i] <= a[i].max(b[i]))
}
pub fn intersects(a: Point, b: Point, c: Point, d: Point) -> bool {
    let (o1, o2, o3, o4) = (
        orientation(a, b, c),
        orientation(a, b, d),
        orientation(c, d, a),
        orientation(c, d, b),
    );
    (o1 == 0 && on(a, b, c))
        || (o2 == 0 && on(a, b, d))
        || (o3 == 0 && on(c, d, a))
        || (o4 == 0 && on(c, d, b))
        || (o1.signum() * o2.signum() < 0 && o3.signum() * o4.signum() < 0)
}
pub fn segments_near(a: Point, b: Point, c: Point, d: Point, gap2: i128, inclusive: bool) -> bool {
    if gap2 < 0 {
        return false;
    }
    if intersects(a, b, c, d) {
        return gap2 > 0 || inclusive;
    }
    point_near_segment(a, c, d, gap2, inclusive)
        || point_near_segment(b, c, d, gap2, inclusive)
        || point_near_segment(c, a, b, gap2, inclusive)
        || point_near_segment(d, a, b, gap2, inclusive)
}
pub fn segment_near_rect(
    a: Point,
    b: Point,
    lo: Point,
    hi: Point,
    gap2: i128,
    inclusive: bool,
) -> bool {
    if gap2 < 0 {
        return false;
    }
    let inside = |p: Point| (0..2).all(|i| p[i] >= lo[i] && p[i] <= hi[i]);
    if inside(a) || inside(b) {
        return gap2 > 0 || inclusive;
    }
    let corners = [lo, [hi[0], lo[1]], hi, [lo[0], hi[1]]];
    (0..4).any(|i| segments_near(a, b, corners[i], corners[(i + 1) % 4], gap2, inclusive))
}
pub fn track_near_pad(
    a: Point,
    b: Point,
    width: i64,
    clearance: i64,
    p: &Pad,
    inclusive: bool,
) -> bool {
    let gap2 = i128::from(width) + 2 * i128::from(clearance);
    match p.shape {
        Shape::Circle { radius_nm } => {
            point_near_segment(p.at, a, b, gap2 + 2 * i128::from(radius_nm), inclusive)
        }
        Shape::Rect { half_size_nm: h } => segment_near_rect(
            a,
            b,
            [p.at[0] - h[0], p.at[1] - h[1]],
            [p.at[0] + h[0], p.at[1] + h[1]],
            gap2,
            inclusive,
        ),
    }
}
// Native integer-nm contact rounding can leave a sub-nm diagonal overlap open.
// Require a full 1 nm of overlap after flooring each track half-width. This
// changes connectivity only: clearance calls retain full widths and gap rules.
const CONTACT_OVERLAP_NM: i64 = 1;

pub fn track_connects_pad(t: &Track, p: &Pad) -> bool {
    track_near_pad(
        t.start,
        t.end,
        (t.width_nm / 2) * 2,
        -CONTACT_OVERLAP_NM,
        p,
        true,
    )
}

pub fn tracks_connect(a: &Track, b: &Track) -> bool {
    let gap2 = 2
        * (i128::from(a.width_nm / 2) + i128::from(b.width_nm / 2)
            - i128::from(CONTACT_OVERLAP_NM));
    segments_near(a.start, a.end, b.start, b.end, gap2, true)
}

/// Conservative contact: at least one whole nm of overlap, including corners.
pub fn pads_touch(a: &Pad, b: &Pad) -> bool {
    match (&a.shape, &b.shape) {
        (Shape::Circle { radius_nm: r }, _) => {
            track_near_pad(a.at, a.at, 2 * r, -CONTACT_OVERLAP_NM, b, true)
        }
        (_, Shape::Circle { .. }) => pads_touch(b, a),
        (Shape::Rect { half_size_nm: x }, Shape::Rect { half_size_nm: y }) => (0..2).all(|i| {
            (i128::from(a.at[i]) - i128::from(b.at[i])).abs()
                <= i128::from(x[i]) + i128::from(y[i]) - i128::from(CONTACT_OVERLAP_NM)
        }),
    }
}
