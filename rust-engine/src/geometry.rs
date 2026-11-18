//! Exact swept-copper tests. Distances are compared as integer fractions;
//! no raster sampling, floating point tolerances, or square roots.
use crate::model::{
    BoardOutline, CopperGraphic, DrillShape, GraphicShape, MaskAperture, Pad, Point, Shape, Track,
};
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
#[inline]
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
#[inline]
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
#[inline]
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
#[inline]
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
#[inline]
pub(crate) fn track_near_core(
    a: Point,
    b: Point,
    width: i64,
    clearance: i64,
    lo: Point,
    hi: Point,
    radius: i64,
    inclusive: bool,
) -> bool {
    let gap2 = i128::from(width) + 2 * (i128::from(clearance) + i128::from(radius));
    segment_near_rect(a, b, lo, hi, gap2, inclusive)
}
#[inline]
pub(crate) fn track_near_core_2x(
    a: Point,
    b: Point,
    width: i64,
    clearance: i64,
    lo2: Point,
    hi2: Point,
    radius2: i64,
    inclusive: bool,
) -> bool {
    let a2 = [a[0] * 2, a[1] * 2];
    let b2 = [b[0] * 2, b[1] * 2];
    let gap4 = 2 * i128::from(width) + 4 * i128::from(clearance) + 2 * i128::from(radius2);
    segment_near_rect(a2, b2, lo2, hi2, gap4, inclusive)
}
#[inline]
pub fn point_in_convex_poly(p: Point, verts: &[Point]) -> bool {
    if verts.len() < 3 {
        return false;
    }
    let n = verts.len();
    (0..n).all(|i| cross(sub(verts[(i + 1) % n], verts[i]), sub(p, verts[i])) >= 0)
}
#[inline]
pub fn track_near_convex_poly(
    a: Point,
    b: Point,
    width: i64,
    clearance: i64,
    verts: &[Point],
    radius: i64,
    inclusive: bool,
) -> bool {
    let gap2 = i128::from(width) + 2 * (i128::from(clearance) + i128::from(radius));
    if gap2 < 0 || verts.len() < 3 {
        return false;
    }
    if point_in_convex_poly(a, verts) || point_in_convex_poly(b, verts) {
        return gap2 > 0 || inclusive;
    }
    let n = verts.len();
    (0..n).any(|i| segments_near(a, b, verts[i], verts[(i + 1) % n], gap2, inclusive))
}
pub(crate) fn is_ccw_convex_polygon(verts: &[Point]) -> bool {
    let n = verts.len();
    if !(3..=64).contains(&n) {
        return false;
    }
    for i in 0..n {
        let a = verts[i];
        let b = verts[(i + 1) % n];
        let c = verts[(i + 2) % n];
        if cross(sub(b, a), sub(c, b)) <= 0 {
            return false;
        }
    }
    let k = (0..n)
        .min_by_key(|&i| (verts[i][1], verts[i][0]))
        .expect("nonempty");
    for j in 1..(n - 1) {
        let p = verts[(k + j) % n];
        let q = verts[(k + j + 1) % n];
        if cross(sub(p, verts[k]), sub(q, verts[k])) <= 0 {
            return false;
        }
    }
    true
}
pub(crate) fn is_valid_simple_ring(ring: &[Point]) -> bool {
    let n = ring.len();
    if n < 3 {
        return false;
    }
    let mut area2: i128 = 0;
    for i in 0..n {
        let a = ring[i];
        let b = ring[(i + 1) % n];
        if a == b {
            return false;
        }
        area2 += i128::from(a[0]) * i128::from(b[1]) - i128::from(b[0]) * i128::from(a[1]);
        let prev = ring[(i + n - 1) % n];
        let e1 = sub(a, prev);
        let e2 = sub(b, a);
        if cross(e1, e2) == 0 && dot(e1, e2) < 0 {
            return false;
        }
    }
    if area2 == 0 {
        return false;
    }
    for i in 0..n {
        let a = ring[i];
        let b = ring[(i + 1) % n];
        let amin_x = a[0].min(b[0]);
        let amax_x = a[0].max(b[0]);
        let amin_y = a[1].min(b[1]);
        let amax_y = a[1].max(b[1]);
        for j in (i + 2)..n {
            if i == 0 && j == n - 1 {
                continue;
            }
            let c = ring[j];
            let d = ring[(j + 1) % n];
            if c[0].max(d[0]) < amin_x
                || c[0].min(d[0]) > amax_x
                || c[1].max(d[1]) < amin_y
                || c[1].min(d[1]) > amax_y
            {
                continue;
            }
            if intersects(a, b, c, d) {
                return false;
            }
        }
    }
    true
}
pub(crate) fn rings_cross(r1: &[Point], r2: &[Point]) -> bool {
    let n1 = r1.len();
    let n2 = r2.len();
    for i in 0..n1 {
        let a = r1[i];
        let b = r1[(i + 1) % n1];
        let amin_x = a[0].min(b[0]);
        let amax_x = a[0].max(b[0]);
        let amin_y = a[1].min(b[1]);
        let amax_y = a[1].max(b[1]);
        for j in 0..n2 {
            let c = r2[j];
            let d = r2[(j + 1) % n2];
            if c[0].max(d[0]) < amin_x
                || c[0].min(d[0]) > amax_x
                || c[1].max(d[1]) < amin_y
                || c[1].min(d[1]) > amax_y
            {
                continue;
            }
            if intersects(a, b, c, d) {
                return true;
            }
        }
    }
    false
}
pub(crate) fn is_valid_arc(start: Point, mid: Point, end: Point) -> bool {
    if start == mid || mid == end || start == end {
        return false;
    }
    orientation(start, mid, end) != 0 || dot(sub(mid, start), sub(end, mid)) > 0
}
pub fn point_in_simple_polygon(p: Point, verts: &[Point]) -> bool {
    let n = verts.len();
    if n < 3 {
        return false;
    }
    for i in 0..n {
        let a = verts[i];
        let b = verts[(i + 1) % n];
        if orientation(a, b, p) == 0 && on(a, b, p) {
            return true;
        }
    }
    let mut inside = false;
    let px = i128::from(p[0]);
    let py = i128::from(p[1]);
    for i in 0..n {
        let a = verts[i];
        let b = verts[(i + 1) % n];
        let ax = i128::from(a[0]);
        let ay = i128::from(a[1]);
        let bx = i128::from(b[0]);
        let by = i128::from(b[1]);
        if (ay > py) != (by > py) {
            let lhs = (px - ax) * (by - ay);
            let rhs = (py - ay) * (bx - ax);
            if (by > ay && lhs < rhs) || (by < ay && lhs > rhs) {
                inside = !inside;
            }
        }
    }
    inside
}
pub fn point_in_polygon_with_holes(p: Point, outer: &[Point], holes: &[Vec<Point>]) -> bool {
    point_in_simple_polygon(p, outer) && !holes.iter().any(|h| point_in_simple_polygon(p, h))
}
pub fn point_in_board_outline(p: Point, outline: &BoardOutline) -> bool {
    outline
        .polygons
        .iter()
        .any(|poly| point_in_polygon_with_holes(p, &poly.outer, &poly.holes))
}
#[inline]
pub fn track_near_pad(
    a: Point,
    b: Point,
    width: i64,
    clearance: i64,
    p: &Pad,
    inclusive: bool,
) -> bool {
    match &p.shape {
        Shape::Circle { radius_nm } => {
            track_near_core(a, b, width, clearance, p.at, p.at, *radius_nm, inclusive)
        }
        Shape::Rect { half_size_nm } => track_near_core(
            a,
            b,
            width,
            clearance,
            [p.at[0] - half_size_nm[0], p.at[1] - half_size_nm[1]],
            [p.at[0] + half_size_nm[0], p.at[1] + half_size_nm[1]],
            0,
            inclusive,
        ),
        Shape::RoundRect {
            half_size_nm: h,
            radius_nm: r,
        } => track_near_core(
            a,
            b,
            width,
            clearance,
            [p.at[0] - (h[0] - r), p.at[1] - (h[1] - r)],
            [p.at[0] + (h[0] - r), p.at[1] + (h[1] - r)],
            *r,
            inclusive,
        ),
        Shape::BoxExact {
            center_2x,
            half_size_2x,
            radius_2x,
        } => {
            let core_half = [half_size_2x[0] - radius_2x, half_size_2x[1] - radius_2x];
            track_near_core_2x(
                a,
                b,
                width,
                clearance,
                [center_2x[0] - core_half[0], center_2x[1] - core_half[1]],
                [center_2x[0] + core_half[0], center_2x[1] + core_half[1]],
                *radius_2x,
                inclusive,
            )
        }
        Shape::Capsule {
            start_nm,
            end_nm,
            width_nm,
        } => {
            let gap2 = i128::from(width) + i128::from(*width_nm) + 2 * i128::from(clearance);
            segments_near(a, b, *start_nm, *end_nm, gap2, inclusive)
        }
        Shape::ConvexPolygon {
            vertices,
            radius_nm,
        } => track_near_convex_poly(a, b, width, clearance, vertices, *radius_nm, inclusive),
    }
}
// Native integer-nm contact rounding can leave a sub-nm diagonal overlap open.
// Require a full 1 nm of overlap after flooring each track half-width. This
// changes connectivity only: clearance calls retain full widths and gap rules.
pub(crate) const CONTACT_OVERLAP_NM: i64 = 1;

pub fn track_connects_pad(t: &Track, p: &Pad) -> bool {
    let w = (t.width_nm / 2) * 2;
    match &p.shape {
        Shape::Circle { .. }
        | Shape::Rect { .. }
        | Shape::RoundRect { .. }
        | Shape::ConvexPolygon { .. } => {
            track_near_pad(t.start, t.end, w, -CONTACT_OVERLAP_NM, p, true)
        }
        Shape::BoxExact {
            center_2x,
            half_size_2x,
            radius_2x,
        } => {
            let r2 = (radius_2x / 2) * 2;
            let half2 = [
                (half_size_2x[0] / 2) * 2 - r2,
                (half_size_2x[1] / 2) * 2 - r2,
            ];
            track_near_core_2x(
                t.start,
                t.end,
                w,
                -CONTACT_OVERLAP_NM,
                [center_2x[0] - half2[0], center_2x[1] - half2[1]],
                [center_2x[0] + half2[0], center_2x[1] + half2[1]],
                r2,
                true,
            )
        }
        Shape::Capsule {
            start_nm,
            end_nm,
            width_nm,
        } => {
            let gap2 =
                i128::from(w) + i128::from((width_nm / 2) * 2) - 2 * i128::from(CONTACT_OVERLAP_NM);
            segments_near(t.start, t.end, *start_nm, *end_nm, gap2, true)
        }
    }
}

pub fn tracks_connect(a: &Track, b: &Track) -> bool {
    let gap2 = 2
        * (i128::from(a.width_nm / 2) + i128::from(b.width_nm / 2)
            - i128::from(CONTACT_OVERLAP_NM));
    segments_near(a.start, a.end, b.start, b.end, gap2, true)
}

fn sat_convex_polys_overlap_2x_1nm(a2: &[Point], b2: &[Point]) -> bool {
    let check_edges = |poly: &[Point], other: &[Point]| -> bool {
        let n = poly.len();
        for i in 0..n {
            let e0 = poly[i];
            let e1 = poly[(i + 1) % n];
            let e = sub(e1, e0);
            let out_n = [e[1], -e[0]];
            let n2 = dot(out_n, out_n);
            let max_pen = other
                .iter()
                .map(|&q| dot(sub(e0, q), out_n))
                .max()
                .unwrap_or(0);
            if max_pen <= 0 || sq(max_pen) < 4 * n2 {
                return false;
            }
        }
        true
    };
    check_edges(a2, b2) && check_edges(b2, a2)
}

pub fn sat_convex_polys_overlap_1nm(a: &[Point], b: &[Point]) -> bool {
    if a.len() < 3 || b.len() < 3 {
        return false;
    }
    let a2: Vec<Point> = a.iter().map(|p| [p[0] * 2, p[1] * 2]).collect();
    let b2: Vec<Point> = b.iter().map(|p| [p[0] * 2, p[1] * 2]).collect();
    sat_convex_polys_overlap_2x_1nm(&a2, &b2)
}

enum ConnCore2x {
    Box { lo2: Point, hi2: Point },
    Capsule { a2: Point, b2: Point },
    Poly { verts2: Vec<Point> },
}

impl ConnCore2x {
    fn from_pad(p: &Pad) -> (Self, i64) {
        match &p.shape {
            Shape::Circle { radius_nm } => (
                Self::Box {
                    lo2: [p.at[0] * 2, p.at[1] * 2],
                    hi2: [p.at[0] * 2, p.at[1] * 2],
                },
                radius_nm * 2,
            ),
            Shape::Rect { half_size_nm: h } => (
                Self::Box {
                    lo2: [(p.at[0] - h[0]) * 2, (p.at[1] - h[1]) * 2],
                    hi2: [(p.at[0] + h[0]) * 2, (p.at[1] + h[1]) * 2],
                },
                0,
            ),
            Shape::RoundRect {
                half_size_nm: h,
                radius_nm: r,
            } => (
                Self::Box {
                    lo2: [(p.at[0] - (h[0] - r)) * 2, (p.at[1] - (h[1] - r)) * 2],
                    hi2: [(p.at[0] + (h[0] - r)) * 2, (p.at[1] + (h[1] - r)) * 2],
                },
                r * 2,
            ),
            Shape::BoxExact {
                center_2x,
                half_size_2x,
                radius_2x,
            } => {
                let r2 = (radius_2x / 2) * 2;
                let half2 = [
                    (half_size_2x[0] / 2) * 2 - r2,
                    (half_size_2x[1] / 2) * 2 - r2,
                ];
                (
                    Self::Box {
                        lo2: [center_2x[0] - half2[0], center_2x[1] - half2[1]],
                        hi2: [center_2x[0] + half2[0], center_2x[1] + half2[1]],
                    },
                    r2,
                )
            }
            Shape::Capsule {
                start_nm,
                end_nm,
                width_nm,
            } => (
                Self::Capsule {
                    a2: [start_nm[0] * 2, start_nm[1] * 2],
                    b2: [end_nm[0] * 2, end_nm[1] * 2],
                },
                (width_nm / 2) * 2,
            ),
            Shape::ConvexPolygon {
                vertices,
                radius_nm,
            } => (
                Self::Poly {
                    verts2: vertices.iter().map(|v| [v[0] * 2, v[1] * 2]).collect(),
                },
                radius_nm * 2,
            ),
        }
    }

    fn contains_point(&self, pt: Point) -> bool {
        match self {
            Self::Box { lo2, hi2 } => {
                pt[0] >= lo2[0] && pt[0] <= hi2[0] && pt[1] >= lo2[1] && pt[1] <= hi2[1]
            }
            Self::Capsule { .. } => false,
            Self::Poly { verts2 } => point_in_convex_poly(pt, verts2),
        }
    }

    fn vertices(&self) -> Vec<Point> {
        match *self {
            Self::Box { lo2, hi2 } => {
                if lo2 == hi2 {
                    vec![lo2]
                } else {
                    vec![lo2, [hi2[0], lo2[1]], hi2, [lo2[0], hi2[1]]]
                }
            }
            Self::Capsule { a2, b2 } => vec![a2, b2],
            Self::Poly { ref verts2 } => verts2.clone(),
        }
    }

    fn boundary_segments(&self) -> Vec<(Point, Point)> {
        match *self {
            Self::Box { lo2, hi2 } => {
                if lo2 == hi2 {
                    vec![(lo2, lo2)]
                } else {
                    let c = [lo2, [hi2[0], lo2[1]], hi2, [lo2[0], hi2[1]]];
                    (0..4).map(|i| (c[i], c[(i + 1) % 4])).collect()
                }
            }
            Self::Capsule { a2, b2 } => vec![(a2, b2)],
            Self::Poly { ref verts2 } => {
                let n = verts2.len();
                (0..n).map(|i| (verts2[i], verts2[(i + 1) % n])).collect()
            }
        }
    }
}

/// Conservative contact: at least one whole nm of overlap, including corners.
pub fn pads_touch(a: &Pad, b: &Pad) -> bool {
    let (ca, ra2) = ConnCore2x::from_pad(a);
    let (cb, rb2) = ConnCore2x::from_pad(b);
    if ra2 == 0 && rb2 == 0 {
        return match (ca, cb) {
            (ConnCore2x::Box { lo2: alo, hi2: ahi }, ConnCore2x::Box { lo2: blo, hi2: bhi }) => {
                (0..2).all(|k| {
                    i128::from(ahi[k].min(bhi[k])) - i128::from(alo[k].max(blo[k]))
                        >= 2 * i128::from(CONTACT_OVERLAP_NM)
                })
            }
            (ConnCore2x::Capsule { .. }, _) | (_, ConnCore2x::Capsule { .. }) => false,
            (ca, cb) => {
                let va = ca.vertices();
                let vb = cb.vertices();
                va.len() >= 3 && vb.len() >= 3 && sat_convex_polys_overlap_2x_1nm(&va, &vb)
            }
        };
    }
    let gap_2x = i128::from(ra2) + i128::from(rb2) - 2 * i128::from(CONTACT_OVERLAP_NM);
    if gap_2x < 0 {
        return false;
    }
    if let (ConnCore2x::Box { lo2: alo, hi2: ahi }, ConnCore2x::Box { lo2: blo, hi2: bhi }) =
        (&ca, &cb)
    {
        let distance2: i128 = (0..2)
            .map(|k| {
                let d = (i128::from(alo[k]) - i128::from(bhi[k]))
                    .max(i128::from(blo[k]) - i128::from(ahi[k]))
                    .max(0);
                d * d
            })
            .sum();
        return distance2 <= gap_2x * gap_2x;
    }
    let va = ca.vertices();
    let vb = cb.vertices();
    if va.iter().any(|&p| cb.contains_point(p)) || vb.iter().any(|&p| ca.contains_point(p)) {
        return true;
    }
    let gap4 = 2 * gap_2x;
    let sa = ca.boundary_segments();
    let sb = cb.boundary_segments();
    sa.iter().any(|&(p, q)| {
        sb.iter()
            .any(|&(r, s)| segments_near(p, q, r, s, gap4, true))
    })
}

fn isqrt_u128(n: u128) -> u128 {
    if n < 2 {
        return n;
    }
    let mut x = 1u128 << ((128 - n.leading_zeros()).div_ceil(2));
    loop {
        let next = (x + n / x) >> 1;
        if next >= x {
            return x;
        }
        x = next;
    }
}

/// Subdivide a circular arc `(start, mid, end)` into conservative chords plus
/// a sagitta upper bound (in nm) strictly enclosing the true circular arc.
pub fn arc_to_conservative_chords(start: Point, mid: Point, end: Point) -> (Vec<Point>, i64) {
    if start == end || start == mid || mid == end {
        return (vec![start, end], 0);
    }
    let m = sub(mid, start);
    let b = sub(end, start);
    let cross_mb = cross(m, b);
    if cross_mb == 0 {
        return (vec![start, end], 0);
    }
    let ccw = cross_mb > 0;
    let d = 2 * cross_mb;
    let m2 = dot(m, m);
    let b2 = dot(b, b);
    let ux = m2 * b[1] - b2 * m[1];
    let uy = b2 * m[0] - m2 * b[0];
    let cx_rel = ux / d;
    let cy_rel = uy / d;

    const MAX_ARC_RADIUS_NM: i128 = 100_000_000_000_000; // 100 km
    if cx_rel.abs() > MAX_ARC_RADIUS_NM || cy_rel.abs() > MAX_ARC_RADIUS_NM {
        // Ultra-flat minor arc (`R > 10^14` nm): `[start, mid, end]` has sagitta <= L^2 / (8 R) + 2.
        let l1_2 = dot(m, m);
        let em = sub(end, mid);
        let l2_2 = dot(em, em);
        let max_l2 = l1_2.max(l2_2) as u128;
        let sag = (max_l2 / (8 * MAX_ARC_RADIUS_NM as u128)) as i64 + 3;
        return (vec![start, mid, end], sag);
    }

    let c = [i128::from(start[0]) + cx_rel, i128::from(start[1]) + cy_rel];
    let r2 = (cx_rel * cx_rel + cy_rel * cy_rel) as u128;
    let r = isqrt_u128(r2) as i128;
    if r == 0 {
        return (vec![start, mid, end], 2);
    }

    fn subdivide(
        p: Point,
        q: Point,
        c: [i128; 2],
        r: i128,
        ccw: bool,
        depth: u8,
        out: &mut Vec<Point>,
    ) {
        let dpq = sub(q, p);
        if depth == 0 || dot(dpq, dpq) <= 4 {
            out.push(q);
            return;
        }
        let mid_c = [
            (i128::from(p[0]) + i128::from(q[0])) / 2,
            (i128::from(p[1]) + i128::from(q[1])) / 2,
        ];
        let arc_out = if ccw {
            [dpq[1], -dpq[0]]
        } else {
            [-dpq[1], dpq[0]]
        };
        let mut dir = [mid_c[0] - c[0], mid_c[1] - c[1]];
        let d_out = dir[0] * arc_out[0] + dir[1] * arc_out[1];
        if d_out == 0 {
            dir = arc_out;
        } else if d_out < 0 {
            dir = [-dir[0], -dir[1]];
        }
        let dir2 = (dir[0] * dir[0] + dir[1] * dir[1]) as u128;
        let len = isqrt_u128(dir2) as i128;
        if len == 0 {
            out.push(q);
            return;
        }
        let next = [
            (c[0] + dir[0] * r / len) as i64,
            (c[1] + dir[1] * r / len) as i64,
        ];
        subdivide(p, next, c, r, ccw, depth - 1, out);
        subdivide(next, q, c, r, ccw, depth - 1, out);
    }

    let mut pts = vec![start];
    subdivide(start, mid, c, r, ccw, 5, &mut pts);
    subdivide(mid, end, c, r, ccw, 5, &mut pts);

    let mut max_sag = 2i64;
    let r_sq = (r as u128) * (r as u128);
    for w in pts.windows(2) {
        let d = sub(w[1], w[0]);
        let l2 = dot(d, d) as u128;
        let q = l2.div_ceil(4);
        let sag = if q >= r_sq {
            r as i64
        } else {
            (r as u128 - isqrt_u128(r_sq - q)) as i64
        };
        max_sag = max_sag.max(sag + 2);
    }
    (pts, max_sag)
}

#[inline]
pub fn track_near_drill(
    a: Point,
    b: Point,
    width: i64,
    clearance: i64,
    at: Point,
    drill: &DrillShape,
    inclusive: bool,
) -> bool {
    match *drill {
        DrillShape::Circle { diameter_nm } => {
            let gap2 = i128::from(width) + i128::from(diameter_nm) + 2 * i128::from(clearance);
            point_near_segment(at, a, b, gap2, inclusive)
        }
        DrillShape::Capsule {
            start_nm,
            end_nm,
            width_nm,
        } => {
            let gap2 = i128::from(width) + i128::from(width_nm) + 2 * i128::from(clearance);
            segments_near(a, b, start_nm, end_nm, gap2, inclusive)
        }
    }
}

#[inline]
pub fn track_near_circle(
    a: Point,
    b: Point,
    width: i64,
    clearance: i64,
    center: Point,
    radius_nm: i64,
    shape_width: i64,
    filled: bool,
    inclusive: bool,
) -> bool {
    let outer_gap2 = i128::from(width)
        + 2 * i128::from(clearance)
        + 2 * i128::from(radius_nm)
        + i128::from(shape_width);
    if !point_near_segment(center, a, b, outer_gap2, inclusive) {
        return false;
    }
    if filled {
        return true;
    }
    let inner2 = 2 * i128::from(radius_nm)
        - i128::from(shape_width)
        - i128::from(width)
        - 2 * i128::from(clearance);
    if inner2 <= 0 {
        return true;
    }
    let va = sub(a, center);
    let vb = sub(b, center);
    let inner_sq = sq(inner2);
    let cmp_gt = |d2: i128| {
        if inclusive {
            4 * d2 >= inner_sq
        } else {
            4 * d2 > inner_sq
        }
    };
    cmp_gt(dot(va, va)) || cmp_gt(dot(vb, vb))
}

pub fn track_near_mask(
    a: Point,
    b: Point,
    width: i64,
    mask_clearance: i64,
    mask: &MaskAperture,
    inclusive: bool,
) -> bool {
    let clr = mask.expansion_nm + mask_clearance;
    match &mask.shape {
        GraphicShape::Segment {
            start,
            end,
            width_nm,
        } => {
            let gap2 = i128::from(width) + i128::from(*width_nm) + 2 * i128::from(clr);
            segments_near(a, b, *start, *end, gap2, inclusive)
        }
        GraphicShape::Arc {
            start,
            mid,
            end,
            width_nm,
        } => {
            let (chords, sag) = arc_to_conservative_chords(*start, *mid, *end);
            let gap2 = i128::from(width) + i128::from(*width_nm) + 2 * i128::from(clr + sag);
            chords
                .windows(2)
                .any(|w| segments_near(a, b, w[0], w[1], gap2, inclusive))
        }
        GraphicShape::Circle {
            center,
            radius_nm,
            width_nm,
            filled,
        } => track_near_circle(
            a, b, width, clr, *center, *radius_nm, *width_nm, *filled, inclusive,
        ),
        GraphicShape::Polygon {
            outer,
            holes,
            width_nm,
            filled,
        } => {
            if *filled
                && (point_in_polygon_with_holes(a, outer, holes)
                    || point_in_polygon_with_holes(b, outer, holes))
            {
                return true;
            }
            let gap2 = i128::from(width) + i128::from(*width_nm) + 2 * i128::from(clr);
            let check_ring = |ring: &[Point]| {
                let n = ring.len();
                (0..n).any(|i| segments_near(a, b, ring[i], ring[(i + 1) % n], gap2, inclusive))
            };
            check_ring(outer) || holes.iter().any(|h| check_ring(h))
        }
    }
}

pub fn track_connects_graphic(t: &Track, g: &CopperGraphic) -> bool {
    let wt = (t.width_nm / 2) * 2;
    match &g.shape {
        GraphicShape::Segment {
            start,
            end,
            width_nm,
        } => {
            let gap2 = i128::from(wt) + i128::from((width_nm / 2) * 2)
                - 2 * i128::from(CONTACT_OVERLAP_NM);
            segments_near(t.start, t.end, *start, *end, gap2, true)
        }
        GraphicShape::Arc {
            start,
            mid,
            end,
            width_nm,
        } => {
            let wg = (width_nm / 2) * 2;
            let gap2_exact = i128::from(wt) + i128::from(wg) - 2 * i128::from(CONTACT_OVERLAP_NM);
            if [*start, *mid, *end]
                .iter()
                .any(|&pt| point_near_segment(pt, t.start, t.end, gap2_exact, true))
            {
                return true;
            }
            let (chords, sag) = arc_to_conservative_chords(*start, *mid, *end);
            let gap2_shrunk = gap2_exact - 2 * i128::from(sag);
            chords
                .windows(2)
                .any(|w| segments_near(t.start, t.end, w[0], w[1], gap2_shrunk, true))
        }
        GraphicShape::Circle {
            center,
            radius_nm,
            width_nm,
            filled,
        } => track_near_circle(
            t.start,
            t.end,
            wt,
            -CONTACT_OVERLAP_NM,
            *center,
            *radius_nm,
            (*width_nm / 2) * 2,
            *filled,
            true,
        ),
        GraphicShape::Polygon {
            outer,
            holes,
            width_nm,
            filled,
        } => {
            if *filled
                && wt >= 2 * CONTACT_OVERLAP_NM
                && (point_in_polygon_with_holes(t.start, outer, holes)
                    || point_in_polygon_with_holes(t.end, outer, holes))
            {
                return true;
            }
            let gap2 = i128::from(wt) + i128::from((width_nm / 2) * 2)
                - 2 * i128::from(CONTACT_OVERLAP_NM);
            let check_ring = |ring: &[Point]| {
                let n = ring.len();
                (0..n)
                    .any(|i| segments_near(t.start, t.end, ring[i], ring[(i + 1) % n], gap2, true))
            };
            check_ring(outer) || holes.iter().any(|h| check_ring(h))
        }
    }
}

pub fn graphic_connects_pad(g: &CopperGraphic, p: &Pad) -> bool {
    let as_track = |a: Point, b: Point, w: i64| Track {
        id: String::new(),
        net: g.net,
        start: a,
        end: b,
        width_nm: w,
        locked: false,
    };
    match &g.shape {
        GraphicShape::Segment {
            start,
            end,
            width_nm,
        } => track_connects_pad(&as_track(*start, *end, *width_nm), p),
        GraphicShape::Arc {
            start,
            mid,
            end,
            width_nm,
        } => {
            if [*start, *mid, *end]
                .iter()
                .any(|&pt| track_connects_pad(&as_track(pt, pt, *width_nm), p))
            {
                return true;
            }
            let (chords, sag) = arc_to_conservative_chords(*start, *mid, *end);
            let w_shrunk = (*width_nm - 2 * sag).max(0);
            w_shrunk > 0
                && chords
                    .windows(2)
                    .any(|w| track_connects_pad(&as_track(w[0], w[1], w_shrunk), p))
        }
        GraphicShape::Circle {
            center,
            radius_nm,
            width_nm,
            filled,
        } => {
            if *filled {
                let synth = Pad {
                    id: String::new(),
                    net: g.net,
                    at: *center,
                    shape: Shape::Circle {
                        radius_nm: *radius_nm + *width_nm / 2,
                    },
                    drill: None,
                };
                pads_touch(&synth, p)
            } else {
                let r = *radius_nm;
                let pts = [
                    [center[0] + r, center[1]],
                    [center[0] - r, center[1]],
                    [center[0], center[1] + r],
                    [center[0], center[1] - r],
                ];
                pts.iter()
                    .any(|&pt| track_connects_pad(&as_track(pt, pt, *width_nm), p))
            }
        }
        GraphicShape::Polygon {
            outer,
            holes,
            width_nm,
            filled,
        } => {
            if *filled {
                let c = p.copper_center();
                if point_in_polygon_with_holes(c, outer, holes) {
                    return true;
                }
            }
            let check_ring = |ring: &[Point]| {
                let n = ring.len();
                (0..n).any(|i| {
                    track_connects_pad(&as_track(ring[i], ring[(i + 1) % n], *width_nm), p)
                })
            };
            check_ring(outer) || holes.iter().any(|h| check_ring(h))
        }
    }
}

pub fn graphics_connect(a: &CopperGraphic, b: &CopperGraphic) -> bool {
    let as_track = |s: Point, e: Point, w: i64, net: u32| Track {
        id: String::new(),
        net,
        start: s,
        end: e,
        width_nm: w,
        locked: false,
    };
    match &a.shape {
        GraphicShape::Segment {
            start,
            end,
            width_nm,
        } => track_connects_graphic(&as_track(*start, *end, *width_nm, a.net), b),
        GraphicShape::Arc {
            start,
            mid,
            end,
            width_nm,
        } => {
            if [*start, *mid, *end]
                .iter()
                .any(|&pt| track_connects_graphic(&as_track(pt, pt, *width_nm, a.net), b))
            {
                return true;
            }
            let (chords, sag) = arc_to_conservative_chords(*start, *mid, *end);
            let w_shrunk = (*width_nm - 2 * sag).max(0);
            w_shrunk > 0
                && chords
                    .windows(2)
                    .any(|w| track_connects_graphic(&as_track(w[0], w[1], w_shrunk, a.net), b))
        }
        GraphicShape::Circle {
            center,
            radius_nm,
            width_nm,
            ..
        } => {
            let r_eff = *radius_nm + *width_nm / 2;
            track_connects_graphic(&as_track(*center, *center, 2 * r_eff, a.net), b)
        }
        GraphicShape::Polygon {
            outer,
            holes,
            width_nm,
            ..
        } => {
            let check_ring = |ring: &[Point]| {
                let n = ring.len();
                (0..n).any(|i| {
                    track_connects_graphic(
                        &as_track(ring[i], ring[(i + 1) % n], *width_nm, a.net),
                        b,
                    )
                })
            };
            check_ring(outer) || holes.iter().any(|h| check_ring(h))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn point_near_segment_zero_gap_respects_inclusive_boundary() {
        assert!(point_near_segment([5, 0], [0, 0], [10, 0], 0, true));
        assert!(!point_near_segment([5, 0], [0, 0], [10, 0], 0, false));
        assert!(point_near_segment([0, 0], [0, 0], [10, 0], 0, true));
        assert!(!point_near_segment([0, 0], [0, 0], [10, 0], 0, false));
        assert!(point_near_segment([10, 0], [0, 0], [10, 0], 0, true));
        assert!(!point_near_segment([10, 0], [0, 0], [10, 0], 0, false));
        assert!(!point_near_segment([5, 1], [0, 0], [10, 0], 0, true));
        assert!(!point_near_segment([5, 0], [0, 0], [10, 0], -1, true));
        assert!(track_near_core(
            [0, 4],
            [10, 4],
            2,
            1,
            [2, 0],
            [8, 2],
            0,
            true
        ));
        assert!(!track_near_core(
            [0, 4],
            [10, 4],
            2,
            1,
            [2, 0],
            [8, 2],
            0,
            false
        ));
    }
}
