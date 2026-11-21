//! Exact swept-copper tests. Distances are compared as integer fractions;
//! no raster sampling, floating point tolerances, or square roots.
use crate::model::{
    BoardOutline, CopperGraphic, DrillShape, GraphicShape, MaskAperture, Pad, PadGeometry, Point,
    RuleArea, Shape, Track, Via,
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
pub(crate) fn orientation(a: Point, b: Point, c: Point) -> i128 {
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
pub(crate) fn is_valid_closed_ring(ring: &[Point]) -> bool {
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
    }
    area2 != 0
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

const MAX_ARC_CENTER_OFFSET_NM: i128 = 100_000_000_000_000;

/// Numerators and common signed denominator of the circumcenter, relative to start.
fn arc_center(m: [i128; 2], b: [i128; 2]) -> ([i128; 2], i128) {
    let m2 = dot(m, m);
    let b2 = dot(b, b);
    (
        [m2 * b[1] - b2 * m[1], b2 * m[0] - m2 * b[0]],
        2 * cross(m, b),
    )
}

pub(crate) fn is_ultra_large_major_arc(start: Point, mid: Point, end: Point) -> bool {
    if dot(sub(start, mid), sub(end, mid)) < 0 {
        return false;
    }
    let (start, end) = (start.min(end), start.max(end));
    let ([x, y], d) = arc_center(sub(mid, start), sub(end, start));
    d != 0 && x.abs().max(y.abs()) / d.abs() > MAX_ARC_CENTER_OFFSET_NM
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
    track_near_pad_shape(a, b, width, clearance, p.geometry(), inclusive)
}

fn track_near_pad_shape(
    a: Point,
    b: Point,
    width: i64,
    clearance: i64,
    p: PadGeometry<'_>,
    inclusive: bool,
) -> bool {
    match p.shape {
        Shape::Union { .. } => p
            .parts()
            .any(|part| track_near_pad_shape(a, b, width, clearance, part, inclusive)),
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
    track_connects_pad_shape(t, p.geometry())
}

fn track_connects_pad_shape(t: &Track, p: PadGeometry<'_>) -> bool {
    let w = (t.width_nm / 2) * 2;
    match p.shape {
        Shape::Union { .. } => p.parts().any(|part| track_connects_pad_shape(t, part)),
        Shape::Circle { .. }
        | Shape::Rect { .. }
        | Shape::RoundRect { .. }
        | Shape::ConvexPolygon { .. } => {
            track_near_pad_shape(t.start, t.end, w, -CONTACT_OVERLAP_NM, p, true)
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

/// At a routed junction the narrower trace reaches into the wider copper
/// by its own radius. Two barely overlapping round caps form a narrow neck.
pub(crate) fn tracks_land_together(a: &Track, b: &Track) -> bool {
    let ra = a.width_nm / 2;
    let rb = b.width_nm / 2;
    let distance = ra.max(rb).min(ra + rb - CONTACT_OVERLAP_NM);
    segments_near(
        a.start,
        a.end,
        b.start,
        b.end,
        2 * i128::from(distance),
        true,
    )
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
    fn from_primitive(p: PadGeometry<'_>) -> (Self, i64) {
        match p.shape {
            Shape::Union { .. } => unreachable!("contact cores represent single primitives"),
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
    a.geometry()
        .parts()
        .any(|pa| b.geometry().parts().any(|pb| primitives_touch(pa, pb)))
}

pub(crate) fn union_is_connected(
    p: &Pad,
    work: &crate::budget::WorkBudget,
) -> Result<bool, crate::model::Failure> {
    let parts = p.shape.parts();
    let mut connected = [false; crate::model::MAX_PAD_PRIMITIVES];
    connected[0] = true;
    let mut pending = vec![0];
    while let Some(i) = pending.pop() {
        for (j, next) in parts.iter().enumerate() {
            if connected[j] {
                continue;
            }
            work.charge(1)?;
            if primitives_touch(
                PadGeometry {
                    at: p.at,
                    shape: &parts[i],
                },
                PadGeometry {
                    at: p.at,
                    shape: next,
                },
            ) {
                connected[j] = true;
                pending.push(j);
            }
        }
    }
    Ok(connected[..parts.len()].iter().all(|&c| c))
}

fn primitives_touch(a: PadGeometry<'_>, b: PadGeometry<'_>) -> bool {
    let (ca, ra2) = ConnCore2x::from_primitive(a);
    let (cb, rb2) = ConnCore2x::from_primitive(b);
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
    // Subdivide the same way in both directions, including rounding at exact half nanometres.
    if start > end {
        let (mut points, sag) = arc_to_conservative_chords(end, mid, start);
        points.reverse();
        return (points, sag);
    }
    let m = sub(mid, start);
    let ([ux, uy], d) = arc_center(m, sub(end, start));
    if d == 0 {
        return (vec![start, end], 0);
    }
    let center_offset = ux.abs().max(uy.abs()) / d.abs();
    if center_offset > MAX_ARC_CENTER_OFFSET_NM {
        // Validation rejects major arcs at this extent. For half-chord h and
        // radius R the minor sagitta is h²/(R + sqrt(R²-h²)) <= h²/(2R-h).
        // Use a lower bound on R without squaring an ultra-large radius.
        let em = sub(end, mid);
        let h2 = (dot(m, m).max(dot(em, em)) as u128).div_ceil(4);
        let h_upper = isqrt_u128(h2) + 1;
        let sag = h2.div_ceil(2 * center_offset as u128 - h_upper);
        return (vec![start, mid, end], sag as i64);
    }

    // Retain fractional nanometres while constructing angular bisectors. Within
    // the supported radius, squared lengths and the scaled circumcenter fit i128.
    const S: i128 = 4096;
    let center = [ux * S / d, uy * S / d];
    let radius = isqrt_u128(dot(center, center) as u128) as i128;
    let radial = |p| {
        let v = sub(p, start);
        [v[0] * S - center[0], v[1] * S - center[1]]
    };
    let normal = |v: [i128; 2]| {
        if d > 0 {
            [v[1], -v[0]]
        } else {
            [-v[1], v[0]]
        }
    };
    let on_circle = |v: [i128; 2]| {
        // Normalize *after* scaling, even for a one-nanometre chord. The
        // floored square root then has relative error below 2^-59.
        let bits = v[0].abs().max(v[1].abs()).ilog2();
        let shift = 59u32.saturating_sub(bits);
        let v = [v[0] << shift, v[1] << shift];
        let len = isqrt_u128(dot(v, v) as u128) as i128;
        [radius * v[0] / len, radius * v[1] / len]
    };
    let vertex = |v: [i128; 2]| -> Point {
        std::array::from_fn(|k| {
            let coord = center[k] + v[k];
            start[k] + ((coord + coord.signum() * (S / 2)) / S) as i64
        })
    };

    let mut points = vec![start];
    for (p, q) in [(start, mid), (mid, end)] {
        // The oriented chord normal is the exact radial direction of the
        // arc midpoint, including a major arc with almost coincident ends.
        let mut rays = [[0; 2]; 33];
        rays[0] = radial(p);
        rays[16] = on_circle(normal(sub(q, p)));
        rays[32] = radial(q);
        for half in [8, 4, 2, 1] {
            for j in (half..32).step_by(2 * half) {
                let a = rays[j - half];
                let b = rays[j + half];
                // All remaining subarcs are minor. Choose a well-conditioned
                // bisector: neither the sum near zero sweep nor the rotated
                // difference near a half turn can cancel.
                let dir = if dot(a, b) >= 0 {
                    [a[0] + b[0], a[1] + b[1]]
                } else {
                    normal([b[0] - a[0], b[1] - a[1]])
                };
                rays[j] = on_circle(dir);
            }
        }
        points.extend(rays[1..32].iter().map(|&v| vertex(v)));
        points.push(q);
    }
    points.dedup();

    // Each constructed vertex is within 1 nm of its exact angular position:
    // center/radius truncation and four stable bisectors contribute < 0.1 nm
    // before nearest-nm rounding (at most sqrt(1/2) nm). Hence the ideal
    // chord is at most 2 nm longer. Its sagitta plus the 1 nm vertex error
    // bounds distances in both directions between each chord and its arc.
    let longest2 = points
        .windows(2)
        .map(|w| {
            let v = sub(w[1], w[0]);
            dot(v, v) as u128
        })
        .max()
        .unwrap();
    let l = isqrt_u128(longest2) + 3;
    let q = (l * l).div_ceil(4);
    let r = (radius / S).saturating_sub(1).max(0) as u128;
    let sag = if q >= r * r {
        // Even for a tiny radius each ideal final subarc is minor.
        (radius / S + 2) as u128
    } else {
        r - isqrt_u128(r * r - q)
    };
    (points, (sag + 1) as i64)
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
    if i128::from(width) + i128::from(shape_width) + 2 * i128::from(clearance) < 0 {
        return false;
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

pub fn holes_near(
    a_at: Point,
    a_shape: &DrillShape,
    b_at: Point,
    b_shape: &DrillShape,
    min_hole_to_hole_nm: i64,
    inclusive: bool,
) -> bool {
    match (a_shape, b_shape) {
        (DrillShape::Circle { diameter_nm: da }, DrillShape::Circle { diameter_nm: db }) => {
            let gap2 = i128::from(*da) + i128::from(*db) + 2 * i128::from(min_hole_to_hole_nm);
            point_near_segment(a_at, b_at, b_at, gap2, inclusive)
        }
        (
            DrillShape::Circle { diameter_nm: da },
            DrillShape::Capsule {
                start_nm,
                end_nm,
                width_nm,
            },
        )
        | (
            DrillShape::Capsule {
                start_nm,
                end_nm,
                width_nm,
            },
            DrillShape::Circle { diameter_nm: da },
        ) => {
            let gap2 =
                i128::from(*da) + i128::from(*width_nm) + 2 * i128::from(min_hole_to_hole_nm);
            point_near_segment(a_at, *start_nm, *end_nm, gap2, inclusive)
        }
        (
            DrillShape::Capsule {
                start_nm: a0,
                end_nm: a1,
                width_nm: wa,
            },
            DrillShape::Capsule {
                start_nm: b0,
                end_nm: b1,
                width_nm: wb,
            },
        ) => {
            let gap2 = i128::from(*wa) + i128::from(*wb) + 2 * i128::from(min_hole_to_hole_nm);
            segments_near(*a0, *a1, *b0, *b1, gap2, inclusive)
        }
    }
}

pub fn track_near_polygon_rings(
    a: Point,
    b: Point,
    width_nm: i64,
    clearance_nm: i64,
    outer: &[Point],
    holes: &[Vec<Point>],
    inclusive: bool,
) -> bool {
    let gap2 = i128::from(width_nm) + 2 * i128::from(clearance_nm);
    if gap2 < 0 {
        return false;
    }
    let check_ring = |ring: &[Point]| {
        let n = ring.len();
        (0..n).any(|i| segments_near(a, b, ring[i], ring[(i + 1) % n], gap2, inclusive))
    };
    check_ring(outer) || holes.iter().any(|h| check_ring(h))
}

pub fn track_near_filled_polygon(
    a: Point,
    b: Point,
    width_nm: i64,
    clearance_nm: i64,
    outer: &[Point],
    holes: &[Vec<Point>],
    inclusive: bool,
) -> bool {
    if point_in_polygon_with_holes(a, outer, holes) || point_in_polygon_with_holes(b, outer, holes)
    {
        let gap2 = i128::from(width_nm) + 2 * i128::from(clearance_nm);
        return gap2 > 0 || inclusive;
    }
    track_near_polygon_rings(a, b, width_nm, clearance_nm, outer, holes, inclusive)
}

pub fn track_near_rule_area_poly(
    a: Point,
    b: Point,
    width_nm: i64,
    outer: &[Point],
    holes: &[Vec<Point>],
) -> bool {
    if point_in_polygon_with_holes(a, outer, holes) || point_in_polygon_with_holes(b, outer, holes)
    {
        return true;
    }
    track_near_polygon_rings(a, b, width_nm, 0, outer, holes, false)
}

pub fn via_near_rule_area_poly(
    at: Point,
    diameter_nm: i64,
    outer: &[Point],
    holes: &[Vec<Point>],
) -> bool {
    track_near_rule_area_poly(at, at, diameter_nm, outer, holes)
}

pub fn track_near_rule_area(a: Point, b: Point, width_nm: i64, ra: &RuleArea) -> bool {
    if track_near_rule_area_poly(a, b, width_nm, &ra.outer, &ra.holes) {
        return true;
    }
    ra.extra_polygons
        .iter()
        .any(|p| track_near_rule_area_poly(a, b, width_nm, &p.outer, &p.holes))
}

pub fn via_near_rule_area(at: Point, diameter_nm: i64, ra: &RuleArea) -> bool {
    track_near_rule_area(at, at, diameter_nm, ra)
}

pub fn track_connects_via(t: &Track, v: &Via) -> bool {
    let wt = (t.width_nm / 2) * 2;
    let wv = (v.diameter_nm / 2) * 2;
    let gap2 = i128::from(wt) + i128::from(wv) - 2 * i128::from(CONTACT_OVERLAP_NM);
    point_near_segment(v.at, t.start, t.end, gap2, true)
}

/// A newly routed junction carries at least the radius of the narrower item
/// into the other item's copper. Native connectivity alone also admits two
/// rounded edges that overlap by only a nanometre.
pub(crate) fn track_lands_on_via(t: &Track, v: &Via) -> bool {
    let track_radius = t.width_nm / 2;
    let via_radius = v.diameter_nm / 2;
    let distance = track_radius
        .max(via_radius)
        .min(track_radius + via_radius - CONTACT_OVERLAP_NM);
    point_near_segment(v.at, t.start, t.end, 2 * i128::from(distance), true)
}

/// A finished junction has a track endpoint on the via or the via center
/// on the track. A long segment can graze the via midway even when neither
/// anchor is connected. A search prefix may cross the via before clipping.
pub(crate) fn via_anchored_on_track(v: &Via, t: &Track) -> bool {
    if !track_lands_on_via(t, v) {
        return false;
    }
    let track_width = 2 * i128::from(t.width_nm / 2);
    let via_diameter = 2 * i128::from(v.diameter_nm / 2);
    point_near_segment(v.at, t.start, t.end, track_width, true)
        || point_near_segment(t.start, v.at, v.at, via_diameter, true)
        || point_near_segment(t.end, v.at, v.at, via_diameter, true)
}

pub fn via_connects_pad(v: &Via, p: &Pad) -> bool {
    let synth = Track {
        id: String::new(),
        net: v.net,
        start: v.at,
        end: v.at,
        width_nm: v.diameter_nm,
        locked: false,
        layer: v.start_layer.clone(),
    };
    track_connects_pad(&synth, p)
}

pub fn via_connects_graphic(v: &Via, g: &CopperGraphic) -> bool {
    let synth = Track {
        id: String::new(),
        net: v.net,
        start: v.at,
        end: v.at,
        width_nm: v.diameter_nm,
        locked: false,
        layer: v.start_layer.clone(),
    };
    track_connects_graphic(&synth, g)
}

pub fn vias_connect(a: &Via, b: &Via) -> bool {
    let wa = (a.diameter_nm / 2) * 2;
    let wb = (b.diameter_nm / 2) * 2;
    let gap2 = i128::from(wa) + i128::from(wb) - 2 * i128::from(CONTACT_OVERLAP_NM);
    point_near_segment(a.at, b.at, b.at, gap2, true)
}

fn gcd_u128(mut a: u128, mut b: u128) -> u128 {
    while b != 0 {
        let r = a % b;
        a = b;
        b = r;
    }
    a
}

fn rat_lt(a: (i128, i128), b: (i128, i128)) -> bool {
    a.0 * b.1 < b.0 * a.1
}

fn ring_slab_intervals_2x_rational(
    ring: &[Point],
    py: i128,
    axis: usize,
) -> Vec<((i128, i128), (i128, i128))> {
    let n = ring.len();
    let mut crossings: Vec<(i128, i128)> = Vec::new();
    for i in 0..n {
        let ax = i128::from(ring[i][axis]) * 2;
        let ay = i128::from(ring[i][1 - axis]) * 2;
        let bx = i128::from(ring[(i + 1) % n][axis]) * 2;
        let by = i128::from(ring[(i + 1) % n][1 - axis]) * 2;
        if (ay > py) != (by > py) {
            let mut num = ax * (by - ay) + (py - ay) * (bx - ax);
            let mut den = by - ay;
            if den < 0 {
                num = -num;
                den = -den;
            }
            let g = gcd_u128(num.unsigned_abs(), den as u128) as i128;
            crossings.push((num / g, den / g));
        }
    }
    crossings.sort_unstable_by(|&(n1, d1), &(n2, d2)| (n1 * d2).cmp(&(n2 * d1)));
    let mut out = Vec::with_capacity(crossings.len() / 2);
    for pair in crossings.chunks_exact(2) {
        if rat_lt(pair[0], pair[1]) {
            out.push((pair[0], pair[1]));
        }
    }
    out
}

fn polygon_slab_intervals_2x_rational(
    outer: &[Point],
    holes: &[Vec<Point>],
    py: i128,
    axis: usize,
) -> Vec<((i128, i128), (i128, i128))> {
    let mut intervals = ring_slab_intervals_2x_rational(outer, py, axis);
    if intervals.is_empty() || holes.is_empty() {
        return intervals;
    }
    for hole in holes {
        let hole_ivs = ring_slab_intervals_2x_rational(hole, py, axis);
        if hole_ivs.is_empty() {
            continue;
        }
        let mut next = Vec::with_capacity(intervals.len() + hole_ivs.len());
        let mut hi = 0;
        for (a0, a1) in intervals {
            let mut cur0 = a0;
            while hi < hole_ivs.len() && !rat_lt(cur0, hole_ivs[hi].1) {
                hi += 1;
            }
            while hi < hole_ivs.len() && rat_lt(hole_ivs[hi].0, a1) {
                let (h0, h1) = hole_ivs[hi];
                if rat_lt(cur0, h0) {
                    next.push((cur0, h0));
                }
                if rat_lt(h1, a1) {
                    cur0 = h1;
                    hi += 1;
                } else {
                    cur0 = a1;
                    break;
                }
            }
            if rat_lt(cur0, a1) {
                next.push((cur0, a1));
            }
        }
        intervals = next;
        if intervals.is_empty() {
            break;
        }
    }
    intervals
}

/// Inclusive integer-nm landing ranges along one orthogonal centerline.
pub(crate) fn fill_landing_ranges(
    poly: &crate::model::OutlinePolygon,
    axis: usize,
    line: i64,
) -> Vec<(i64, i64)> {
    let ranges =
        polygon_slab_intervals_2x_rational(&poly.outer, &poly.holes, 2 * i128::from(line), axis);
    // A scanline through a vertex or along a boundary must still land inside
    // copper, including when the coincident edge belongs to a hole.
    let mut boundary = Vec::new();
    for ring in std::iter::once(&poly.outer).chain(&poly.holes) {
        for (p, q) in ring.iter().zip(ring.iter().cycle().skip(1)) {
            if p[1 - axis] == line {
                let end = if q[1 - axis] == line {
                    q[axis]
                } else {
                    p[axis]
                };
                boundary.push((p[axis].min(end), p[axis].max(end)));
            }
        }
    }
    boundary.sort_unstable();
    let mut result = Vec::new();
    let mut j = 0;
    for ((a, da), (b, db)) in ranges {
        let mut lo = a.div_euclid(2 * da) as i64 + 1;
        let hi = -(-b).div_euclid(2 * db) as i64 - 1;
        while j < boundary.len() && boundary[j].0 <= hi {
            let (x, y) = boundary[j];
            if lo < x {
                result.push((lo, x - 1));
            }
            lo = lo.max(y + 1);
            if y > hi {
                break;
            }
            j += 1;
        }
        if lo <= hi {
            result.push((lo, hi));
        }
    }
    result
}

fn intervals_overlap_rational(
    a: &[((i128, i128), (i128, i128))],
    b: &[((i128, i128), (i128, i128))],
) -> bool {
    let (mut ia, mut ib) = (0, 0);
    while ia < a.len() && ib < b.len() {
        let (a0, a1) = a[ia];
        let (b0, b1) = b[ib];
        if rat_lt(a0, b1) && rat_lt(b0, a1) {
            return true;
        }
        if !rat_lt(b0, a1) {
            ia += 1;
        } else {
            ib += 1;
        }
    }
    false
}

pub fn filled_polygons_overlap(
    outer_a: &[Point],
    holes_a: &[Vec<Point>],
    outer_b: &[Point],
    holes_b: &[Vec<Point>],
) -> bool {
    let aabb = |pts: &[Point]| -> (Point, Point) {
        let mut lo = [i64::MAX, i64::MAX];
        let mut hi = [i64::MIN, i64::MIN];
        for p in pts {
            lo[0] = lo[0].min(p[0]);
            lo[1] = lo[1].min(p[1]);
            hi[0] = hi[0].max(p[0]);
            hi[1] = hi[1].max(p[1]);
        }
        (lo, hi)
    };
    let (alo, ahi) = aabb(outer_a);
    let (blo, bhi) = aabb(outer_b);
    if ahi[0] <= blo[0] || bhi[0] <= alo[0] || ahi[1] <= blo[1] || bhi[1] <= alo[1] {
        return false;
    }
    for ra in std::iter::once(outer_a).chain(holes_a.iter().map(Vec::as_slice)) {
        for rb in std::iter::once(outer_b).chain(holes_b.iter().map(Vec::as_slice)) {
            let na = ra.len();
            let nb = rb.len();
            for i in 0..na {
                let a = ra[i];
                let b = ra[(i + 1) % na];
                for j in 0..nb {
                    let c = rb[j];
                    let d = rb[(j + 1) % nb];
                    let o1 = orientation(a, b, c);
                    let o2 = orientation(a, b, d);
                    let o3 = orientation(c, d, a);
                    let o4 = orientation(c, d, b);
                    if o1.signum() * o2.signum() < 0 && o3.signum() * o4.signum() < 0 {
                        return true;
                    }
                }
            }
        }
    }
    let y_lo = i128::from(alo[1].max(blo[1])) * 2;
    let y_hi = i128::from(ahi[1].min(bhi[1])) * 2;
    let mut ys: Vec<i128> = std::iter::once(outer_a)
        .chain(holes_a.iter().map(Vec::as_slice))
        .chain(std::iter::once(outer_b))
        .chain(holes_b.iter().map(Vec::as_slice))
        .flat_map(|ring| ring.iter().map(|p| i128::from(p[1]) * 2))
        .filter(|&y| y >= y_lo && y <= y_hi)
        .collect();
    ys.sort_unstable();
    ys.dedup();
    for w in ys.windows(2) {
        let py = (w[0] + w[1]) / 2;
        let ivs_a = polygon_slab_intervals_2x_rational(outer_a, holes_a, py, 0);
        if ivs_a.is_empty() {
            continue;
        }
        let ivs_b = polygon_slab_intervals_2x_rational(outer_b, holes_b, py, 0);
        if intervals_overlap_rational(&ivs_a, &ivs_b) {
            return true;
        }
    }
    false
}

fn unfilled_circle_connects_pad(center: Point, radius_nm: i64, width_nm: i64, p: &Pad) -> bool {
    unfilled_circle_near_pad(center, radius_nm, width_nm, -CONTACT_OVERLAP_NM, p, true)
}

fn unfilled_circle_near_pad(
    center: Point,
    radius_nm: i64,
    width_nm: i64,
    clearance: i64,
    p: &Pad,
    inclusive: bool,
) -> bool {
    unfilled_circle_near_pad_shape(
        center,
        radius_nm,
        width_nm,
        clearance,
        p.geometry(),
        inclusive,
    )
}

fn unfilled_circle_near_pad_shape(
    center: Point,
    radius_nm: i64,
    width_nm: i64,
    clearance: i64,
    p: PadGeometry<'_>,
    inclusive: bool,
) -> bool {
    let hw = width_nm / 2;
    let r_out = radius_nm + hw;
    let r_in = (radius_nm - hw).max(0);
    if r_in == 0 {
        return track_near_pad_shape(center, center, 2 * r_out, clearance, p, inclusive);
    }
    match p.shape {
        Shape::Union { .. } => p.parts().any(|part| {
            unfilled_circle_near_pad_shape(center, radius_nm, width_nm, clearance, part, inclusive)
        }),
        Shape::Circle { radius_nm: pr } => track_near_circle(
            p.at,
            p.at,
            2 * *pr,
            clearance,
            center,
            radius_nm,
            hw * 2,
            false,
            inclusive,
        ),
        Shape::Capsule {
            start_nm,
            end_nm,
            width_nm: pw,
        } => track_near_circle(
            *start_nm,
            *end_nm,
            (*pw / 2) * 2,
            clearance,
            center,
            radius_nm,
            hw * 2,
            false,
            inclusive,
        ),
        Shape::Rect { .. } | Shape::RoundRect { .. } | Shape::BoxExact { .. } => {
            let (ConnCore2x::Box { lo2, hi2 }, rp2) = ConnCore2x::from_primitive(p) else {
                unreachable!()
            };
            let c2 = [center[0] * 2, center[1] * 2];
            let gap4 = 4 * i128::from(r_out) + 2 * i128::from(rp2) + 4 * i128::from(clearance);
            if !segment_near_rect(c2, c2, lo2, hi2, gap4, inclusive) {
                return false;
            }
            let need2 = 2 * i128::from(r_in) - 2 * i128::from(clearance) - i128::from(rp2);
            if need2 <= 0 {
                return true;
            }
            let corners = [lo2, [hi2[0], lo2[1]], hi2, [lo2[0], hi2[1]]];
            corners.iter().any(|&v| {
                let d2 = dot(sub(v, c2), sub(v, c2));
                if inclusive {
                    d2 >= sq(need2)
                } else {
                    d2 > sq(need2)
                }
            })
        }
        Shape::ConvexPolygon {
            vertices,
            radius_nm: rp,
        } => {
            if !track_near_convex_poly(
                center,
                center,
                2 * r_out,
                clearance,
                vertices,
                *rp,
                inclusive,
            ) {
                return false;
            }
            let need = i128::from(r_in) - i128::from(clearance) - i128::from(*rp);
            need <= 0
                || vertices.iter().any(|&v| {
                    let d2 = dot(sub(v, center), sub(v, center));
                    if inclusive {
                        d2 >= sq(need)
                    } else {
                        d2 > sq(need)
                    }
                })
        }
    }
}

fn ring_bounds(ring: &[Point]) -> (Point, Point) {
    let mut lo = [i64::MAX, i64::MAX];
    let mut hi = [i64::MIN, i64::MIN];
    for p in ring {
        lo[0] = lo[0].min(p[0]);
        lo[1] = lo[1].min(p[1]);
        hi[0] = hi[0].max(p[0]);
        hi[1] = hi[1].max(p[1]);
    }
    (lo, hi)
}

fn bounds_overlap((alo, ahi): (Point, Point), (blo, bhi): (Point, Point)) -> bool {
    alo[0] <= bhi[0] && blo[0] <= ahi[0] && alo[1] <= bhi[1] && blo[1] <= ahi[1]
}

fn point_in_simple_polygon_2x(p2: Point, verts: &[Point]) -> bool {
    let n = verts.len();
    if n < 3 {
        return false;
    }
    for i in 0..n {
        let a = [verts[i][0] * 2, verts[i][1] * 2];
        let b = [verts[(i + 1) % n][0] * 2, verts[(i + 1) % n][1] * 2];
        if orientation(a, b, p2) == 0 && on(a, b, p2) {
            return true;
        }
    }
    let mut inside = false;
    let px = i128::from(p2[0]);
    let py = i128::from(p2[1]);
    for i in 0..n {
        let ax = i128::from(verts[i][0]) * 2;
        let ay = i128::from(verts[i][1]) * 2;
        let bx = i128::from(verts[(i + 1) % n][0]) * 2;
        let by = i128::from(verts[(i + 1) % n][1]) * 2;
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

fn point_in_polygon_with_holes_2x(p2: Point, outer: &[Point], holes: &[Vec<Point>]) -> bool {
    point_in_simple_polygon_2x(p2, outer)
        && !holes.iter().any(|h| point_in_simple_polygon_2x(p2, h))
}

pub fn zone_fill_connects_pad(outer: &[Point], holes: &[Vec<Point>], p: &Pad) -> bool {
    zone_fill_connects_pad_shape(outer, holes, p.geometry())
}

fn zone_fill_connects_pad_shape(outer: &[Point], holes: &[Vec<Point>], p: PadGeometry<'_>) -> bool {
    if outer.len() < 3 || !bounds_overlap(p.copper_bounds(), ring_bounds(outer)) {
        return false;
    }
    let as_zero_track = |a: Point, b: Point| Track {
        id: String::new(),
        net: 0,
        layer: String::new(),
        start: a,
        end: b,
        width_nm: 0,
        locked: false,
    };
    let check_edges_zero = |ring: &[Point]| {
        let n = ring.len();
        (0..n).any(|i| track_connects_pad_shape(&as_zero_track(ring[i], ring[(i + 1) % n]), p))
    };
    match p.shape {
        Shape::Union { .. } => p
            .parts()
            .any(|part| zone_fill_connects_pad_shape(outer, holes, part)),
        Shape::Circle { radius_nm } => {
            if *radius_nm < CONTACT_OVERLAP_NM {
                return false;
            }
            point_in_polygon_with_holes(p.at, outer, holes)
                || check_edges_zero(outer)
                || holes.iter().any(|h| check_edges_zero(h))
        }
        Shape::Capsule {
            start_nm, width_nm, ..
        } => {
            if (*width_nm / 2) * 2 < 2 * CONTACT_OVERLAP_NM {
                return false;
            }
            point_in_polygon_with_holes(*start_nm, outer, holes)
                || check_edges_zero(outer)
                || holes.iter().any(|h| check_edges_zero(h))
        }
        Shape::RoundRect {
            half_size_nm,
            radius_nm,
        } if *radius_nm >= CONTACT_OVERLAP_NM
            && half_size_nm[0] >= *radius_nm
            && half_size_nm[1] >= *radius_nm =>
        {
            point_in_polygon_with_holes(p.at, outer, holes)
                || check_edges_zero(outer)
                || holes.iter().any(|h| check_edges_zero(h))
        }
        Shape::BoxExact {
            center_2x,
            half_size_2x,
            radius_2x,
        } if (*radius_2x / 2) * 2 >= 2 * CONTACT_OVERLAP_NM
            && (half_size_2x[0] / 2) * 2 >= (*radius_2x / 2) * 2
            && (half_size_2x[1] / 2) * 2 >= (*radius_2x / 2) * 2 =>
        {
            point_in_polygon_with_holes_2x(*center_2x, outer, holes)
                || check_edges_zero(outer)
                || holes.iter().any(|h| check_edges_zero(h))
        }
        Shape::Rect { half_size_nm: h }
        | Shape::RoundRect {
            half_size_nm: h, ..
        } => {
            let lo2 = [
                (p.at[0] - h[0]) * 2 + 2 * CONTACT_OVERLAP_NM,
                (p.at[1] - h[1]) * 2 + 2 * CONTACT_OVERLAP_NM,
            ];
            let hi2 = [
                (p.at[0] + h[0]) * 2 - 2 * CONTACT_OVERLAP_NM,
                (p.at[1] + h[1]) * 2 - 2 * CONTACT_OVERLAP_NM,
            ];
            if lo2[0] > hi2[0] || lo2[1] > hi2[1] {
                return false;
            }
            if point_in_polygon_with_holes_2x(lo2, outer, holes) {
                return true;
            }
            let check_shrunk_rect = |ring: &[Point]| {
                let n = ring.len();
                (0..n).any(|i| {
                    let a2 = [ring[i][0] * 2, ring[i][1] * 2];
                    let b2 = [ring[(i + 1) % n][0] * 2, ring[(i + 1) % n][1] * 2];
                    segment_near_rect(a2, b2, lo2, hi2, 0, true)
                })
            };
            check_shrunk_rect(outer) || holes.iter().any(|h| check_shrunk_rect(h))
        }
        Shape::BoxExact {
            center_2x,
            half_size_2x,
            ..
        } => {
            let half2 = [(half_size_2x[0] / 2) * 2, (half_size_2x[1] / 2) * 2];
            let lo2 = [
                center_2x[0] - half2[0] + 2 * CONTACT_OVERLAP_NM,
                center_2x[1] - half2[1] + 2 * CONTACT_OVERLAP_NM,
            ];
            let hi2 = [
                center_2x[0] + half2[0] - 2 * CONTACT_OVERLAP_NM,
                center_2x[1] + half2[1] - 2 * CONTACT_OVERLAP_NM,
            ];
            if lo2[0] > hi2[0] || lo2[1] > hi2[1] {
                return false;
            }
            if point_in_polygon_with_holes_2x(lo2, outer, holes) {
                return true;
            }
            let check_shrunk_rect = |ring: &[Point]| {
                let n = ring.len();
                (0..n).any(|i| {
                    let a2 = [ring[i][0] * 2, ring[i][1] * 2];
                    let b2 = [ring[(i + 1) % n][0] * 2, ring[(i + 1) % n][1] * 2];
                    segment_near_rect(a2, b2, lo2, hi2, 0, true)
                })
            };
            check_shrunk_rect(outer) || holes.iter().any(|h| check_shrunk_rect(h))
        }
        Shape::ConvexPolygon {
            vertices,
            radius_nm,
        } => {
            if *radius_nm >= CONTACT_OVERLAP_NM {
                (!vertices.is_empty() && point_in_polygon_with_holes(vertices[0], outer, holes))
                    || check_edges_zero(outer)
                    || holes.iter().any(|h| check_edges_zero(h))
            } else {
                filled_polygons_overlap(vertices, &[], outer, holes)
            }
        }
    }
}

pub fn track_connects_filled_polygon(t: &Track, outer: &[Point], holes: &[Vec<Point>]) -> bool {
    if outer.len() < 3 || !bounds_overlap(t.copper_bounds(), ring_bounds(outer)) {
        return false;
    }
    let wt = (t.width_nm / 2) * 2;
    if wt < 2 * CONTACT_OVERLAP_NM {
        return false;
    }
    if point_in_polygon_with_holes(t.start, outer, holes)
        || point_in_polygon_with_holes(t.end, outer, holes)
    {
        return true;
    }
    track_near_polygon_rings(t.start, t.end, wt, -CONTACT_OVERLAP_NM, outer, holes, true)
}

pub fn via_connects_filled_polygon(v: &Via, outer: &[Point], holes: &[Vec<Point>]) -> bool {
    let synth = Track {
        id: String::new(),
        net: v.net,
        start: v.at,
        end: v.at,
        width_nm: v.diameter_nm,
        locked: false,
        layer: v.start_layer.clone(),
    };
    track_connects_filled_polygon(&synth, outer, holes)
}

pub fn graphic_connects_filled_polygon(
    g: &CopperGraphic,
    outer: &[Point],
    holes: &[Vec<Point>],
) -> bool {
    if outer.len() < 3 || !bounds_overlap(g.copper_bounds(), ring_bounds(outer)) {
        return false;
    }
    let as_track = |a: Point, b: Point, w: i64| Track {
        id: String::new(),
        net: g.net,
        layer: g.layer.clone(),
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
        } => track_connects_filled_polygon(&as_track(*start, *end, *width_nm), outer, holes),
        GraphicShape::Arc {
            start,
            mid,
            end,
            width_nm,
        } => {
            if [*start, *mid, *end].iter().any(|&pt| {
                track_connects_filled_polygon(&as_track(pt, pt, *width_nm), outer, holes)
            }) {
                return true;
            }
            let (chords, sag) = arc_to_conservative_chords(*start, *mid, *end);
            let w_shrunk = (*width_nm - 2 * sag).max(0);
            w_shrunk >= 2 * CONTACT_OVERLAP_NM
                && chords.windows(2).any(|w| {
                    track_connects_filled_polygon(&as_track(w[0], w[1], w_shrunk), outer, holes)
                })
        }
        GraphicShape::Circle {
            center,
            radius_nm,
            width_nm,
            filled,
        } => {
            if *filled {
                let r_eff = *radius_nm + *width_nm / 2;
                track_connects_filled_polygon(&as_track(*center, *center, 2 * r_eff), outer, holes)
            } else {
                let cw = (*width_nm / 2) * 2;
                if cw < 2 * CONTACT_OVERLAP_NM {
                    return false;
                }
                let check_ring = |ring: &[Point]| {
                    let n = ring.len();
                    (0..n).any(|i| {
                        track_near_circle(
                            ring[i],
                            ring[(i + 1) % n],
                            0,
                            -CONTACT_OVERLAP_NM,
                            *center,
                            *radius_nm,
                            cw,
                            false,
                            true,
                        )
                    })
                };
                check_ring(outer)
                    || holes.iter().any(|h| check_ring(h))
                    || [
                        [center[0] + *radius_nm, center[1]],
                        [center[0] - *radius_nm, center[1]],
                        [center[0], center[1] + *radius_nm],
                        [center[0], center[1] - *radius_nm],
                    ]
                    .into_iter()
                    .any(|pt| point_in_polygon_with_holes(pt, outer, holes))
            }
        }
        GraphicShape::Polygon {
            outer: go,
            holes: gh,
            width_nm,
            filled,
        } => {
            let wg = (*width_nm / 2) * 2;
            let check_g_ring = |ring: &[Point]| {
                let n = ring.len();
                (0..n).any(|i| {
                    track_connects_filled_polygon(
                        &as_track(ring[i], ring[(i + 1) % n], wg),
                        outer,
                        holes,
                    )
                })
            };
            check_g_ring(go)
                || gh.iter().any(|h| check_g_ring(h))
                || (*filled && filled_polygons_overlap(go, gh, outer, holes))
        }
    }
}

pub fn graphic_connects_pad(g: &CopperGraphic, p: &Pad) -> bool {
    let as_track = |a: Point, b: Point, w: i64| Track {
        id: String::new(),
        net: g.net,
        layer: g.layer.clone(),
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
                    layers: vec![g.layer.clone()],
                    local_clearance_nm: None,
                    zone_connection: crate::model::PadZoneConnection::Inherited,
                    thermal_gap_nm: None,
                    thermal_spoke_width_nm: None,
                    thermal_spoke_angle_mdeg: None,
                    orientation_mdeg: 0,
                    footprint_ref: None,
                    pad_number: None,
                    fromto_id: None,
                    shape: Shape::Circle {
                        radius_nm: *radius_nm + *width_nm / 2,
                    },
                    drill: None,
                };
                pads_touch(&synth, p)
            } else {
                unfilled_circle_connects_pad(*center, *radius_nm, *width_nm, p)
            }
        }
        GraphicShape::Polygon {
            outer,
            holes,
            width_nm,
            filled,
        } => {
            if *filled && zone_fill_connects_pad(outer, holes, p) {
                return true;
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

fn unfilled_circles_connect(ca: Point, ra: i64, wa: i64, cb: Point, rb: i64, wb: i64) -> bool {
    unfilled_circles_near(ca, ra, wa, cb, rb, wb, -CONTACT_OVERLAP_NM, true)
}

fn unfilled_circles_near(
    ca: Point,
    ra: i64,
    wa: i64,
    cb: Point,
    rb: i64,
    wb: i64,
    clearance: i64,
    inclusive: bool,
) -> bool {
    let ha = wa / 2;
    let hb = wb / 2;
    if i128::from(ha) + i128::from(hb) + i128::from(clearance) < 0 {
        return false;
    }
    let r_out_a = ra + ha;
    let r_in_a = ra - ha;
    let r_out_b = rb + hb;
    let r_in_b = rb - hb;
    let d2 = dot(sub(ca, cb), sub(ca, cb));
    let outer = i128::from(r_out_a) + i128::from(r_out_b) + i128::from(clearance);
    if outer < 0 || !compare(d2, sq(outer), inclusive) {
        return false;
    }
    let gap_a = i128::from(r_in_b) - i128::from(r_out_a) - i128::from(clearance);
    if gap_a > 0 && !compare(sq(gap_a), d2, inclusive) {
        return false;
    }
    let gap_b = i128::from(r_in_a) - i128::from(r_out_b) - i128::from(clearance);
    if gap_b > 0 && !compare(sq(gap_b), d2, inclusive) {
        return false;
    }
    true
}

pub fn graphics_connect(a: &CopperGraphic, b: &CopperGraphic) -> bool {
    let as_track = |s: Point, e: Point, w: i64, net: u32| Track {
        id: String::new(),
        net,
        layer: a.layer.clone(),
        start: s,
        end: e,
        width_nm: w,
        locked: false,
    };
    match (&a.shape, &b.shape) {
        (
            GraphicShape::Segment {
                start,
                end,
                width_nm,
            },
            _,
        ) => track_connects_graphic(&as_track(*start, *end, *width_nm, a.net), b),
        (_, GraphicShape::Segment { .. }) => graphics_connect(b, a),
        (
            GraphicShape::Arc {
                start: sa,
                mid: ma,
                end: ea,
                width_nm: wa,
            },
            GraphicShape::Arc {
                start: sb,
                mid: mb,
                end: eb,
                width_nm: wb,
            },
        ) => {
            if [*sa, *ma, *ea]
                .iter()
                .any(|&pt| track_connects_graphic(&as_track(pt, pt, *wa, a.net), b))
                || [*sb, *mb, *eb]
                    .iter()
                    .any(|&pt| track_connects_graphic(&as_track(pt, pt, *wb, b.net), a))
            {
                return true;
            }
            let (ca, sag_a) = arc_to_conservative_chords(*sa, *ma, *ea);
            let (cb, sag_b) = arc_to_conservative_chords(*sb, *mb, *eb);
            let gap2 = i128::from((*wa / 2) * 2) + i128::from((*wb / 2) * 2)
                - 2 * i128::from(CONTACT_OVERLAP_NM)
                - 2 * i128::from(sag_a)
                - 2 * i128::from(sag_b);
            gap2 >= 0
                && ca.windows(2).any(|u| {
                    cb.windows(2)
                        .any(|v| segments_near(u[0], u[1], v[0], v[1], gap2, true))
                })
        }
        (
            GraphicShape::Arc {
                start,
                mid,
                end,
                width_nm: aw,
            },
            GraphicShape::Circle {
                center,
                radius_nm,
                width_nm: cw,
                filled,
            },
        ) => {
            if [*start, *mid, *end]
                .iter()
                .any(|&pt| track_connects_graphic(&as_track(pt, pt, *aw, a.net), b))
            {
                return true;
            }
            let (chords, sag) = arc_to_conservative_chords(*start, *mid, *end);
            chords.windows(2).any(|w| {
                track_near_circle(
                    w[0],
                    w[1],
                    (*aw / 2) * 2,
                    -CONTACT_OVERLAP_NM - sag,
                    *center,
                    *radius_nm,
                    (*cw / 2) * 2,
                    *filled,
                    true,
                )
            })
        }
        (GraphicShape::Circle { .. }, GraphicShape::Arc { .. }) => graphics_connect(b, a),
        (
            GraphicShape::Arc {
                start,
                mid,
                end,
                width_nm: aw,
            },
            GraphicShape::Polygon {
                outer,
                holes,
                width_nm: pw,
                ..
            },
        ) => {
            if [*start, *mid, *end]
                .iter()
                .any(|&pt| track_connects_graphic(&as_track(pt, pt, *aw, a.net), b))
            {
                return true;
            }
            let (chords, sag) = arc_to_conservative_chords(*start, *mid, *end);
            let gap2 = i128::from((*aw / 2) * 2) + i128::from((*pw / 2) * 2)
                - 2 * i128::from(CONTACT_OVERLAP_NM)
                - 2 * i128::from(sag);
            if gap2 < 0 {
                return false;
            }
            let check_ring = |ring: &[Point]| {
                let n = ring.len();
                chords.windows(2).any(|w| {
                    (0..n)
                        .any(|i| segments_near(w[0], w[1], ring[i], ring[(i + 1) % n], gap2, true))
                })
            };
            check_ring(outer) || holes.iter().any(|h| check_ring(h))
        }
        (GraphicShape::Polygon { .. }, GraphicShape::Arc { .. }) => graphics_connect(b, a),
        (
            GraphicShape::Circle {
                center: ca,
                radius_nm: ra,
                width_nm: wa,
                filled: fa,
            },
            GraphicShape::Circle {
                center: cb,
                radius_nm: rb,
                width_nm: wb,
                filled: fb,
            },
        ) => match (*fa, *fb) {
            (true, true) | (true, false) => {
                let r_eff = *ra + *wa / 2;
                track_connects_graphic(&as_track(*ca, *ca, 2 * r_eff, a.net), b)
            }
            (false, true) => {
                let r_eff = *rb + *wb / 2;
                track_connects_graphic(&as_track(*cb, *cb, 2 * r_eff, b.net), a)
            }
            (false, false) => unfilled_circles_connect(*ca, *ra, *wa, *cb, *rb, *wb),
        },
        (
            GraphicShape::Circle {
                center,
                radius_nm,
                width_nm: cw,
                filled,
            },
            GraphicShape::Polygon {
                outer,
                holes,
                width_nm: pw,
                filled: poly_filled,
            },
        ) => {
            if *filled {
                let r_eff = *radius_nm + *cw / 2;
                return track_connects_graphic(&as_track(*center, *center, 2 * r_eff, a.net), b);
            }
            let check_ring = |ring: &[Point]| {
                let n = ring.len();
                (0..n).any(|i| {
                    track_connects_graphic(&as_track(ring[i], ring[(i + 1) % n], *pw, b.net), a)
                })
            };
            if check_ring(outer) || holes.iter().any(|h| check_ring(h)) {
                return true;
            }
            *poly_filled
                && (*cw / 2) * 2 >= 2 * CONTACT_OVERLAP_NM
                && [
                    [center[0] + *radius_nm, center[1]],
                    [center[0] - *radius_nm, center[1]],
                    [center[0], center[1] + *radius_nm],
                    [center[0], center[1] - *radius_nm],
                ]
                .into_iter()
                .any(|pt| point_in_polygon_with_holes(pt, outer, holes))
        }
        (GraphicShape::Polygon { .. }, GraphicShape::Circle { .. }) => graphics_connect(b, a),
        (
            GraphicShape::Polygon {
                outer: oa,
                holes: ha,
                width_nm: wa,
                filled: fa,
            },
            GraphicShape::Polygon {
                outer: ob,
                holes: hb,
                width_nm: wb,
                filled: fb,
            },
        ) => {
            let check_rings = |outer: &[Point],
                               holes: &[Vec<Point>],
                               w: i64,
                               net: u32,
                               other: &CopperGraphic| {
                let check_ring = |ring: &[Point]| {
                    let n = ring.len();
                    (0..n).any(|i| {
                        track_connects_graphic(&as_track(ring[i], ring[(i + 1) % n], w, net), other)
                    })
                };
                check_ring(outer) || holes.iter().any(|h| check_ring(h))
            };
            check_rings(oa, ha, *wa, a.net, b)
                || check_rings(ob, hb, *wb, b.net, a)
                || (*fa && *fb && filled_polygons_overlap(oa, ha, ob, hb))
        }
    }
}

pub fn mask_overlaps_pad(m: &MaskAperture, p: &Pad) -> bool {
    let exp = m.expansion_nm.max(0);
    match &m.shape {
        GraphicShape::Segment {
            start,
            end,
            width_nm,
        } => track_near_pad(*start, *end, *width_nm, exp, p, false),
        GraphicShape::Arc {
            start,
            mid,
            end,
            width_nm,
        } => {
            let (chords, sag) = arc_to_conservative_chords(*start, *mid, *end);
            chords
                .windows(2)
                .any(|w| track_near_pad(w[0], w[1], *width_nm, exp + sag, p, false))
        }
        GraphicShape::Circle {
            center,
            radius_nm,
            width_nm,
            filled,
        } => {
            if *filled {
                track_near_pad(*center, *center, 2 * *radius_nm + *width_nm, exp, p, false)
            } else {
                unfilled_circle_near_pad(*center, *radius_nm, *width_nm, exp, p, false)
            }
        }
        GraphicShape::Polygon {
            outer,
            holes,
            width_nm,
            filled,
        } => {
            if *filled && zone_fill_connects_pad(outer, holes, p) {
                return true;
            }
            let check_ring = |ring: &[Point]| {
                let n = ring.len();
                (0..n).any(|i| track_near_pad(ring[i], ring[(i + 1) % n], *width_nm, exp, p, false))
            };
            check_ring(outer) || holes.iter().any(|h| check_ring(h))
        }
    }
}

pub fn mask_overlaps_filled_polygon(
    m: &MaskAperture,
    outer: &[Point],
    holes: &[Vec<Point>],
) -> bool {
    if outer.len() < 3 {
        return false;
    }
    let exp = m.expansion_nm.max(0);
    match &m.shape {
        GraphicShape::Segment {
            start,
            end,
            width_nm,
        } => track_near_filled_polygon(*start, *end, *width_nm, exp, outer, holes, false),
        GraphicShape::Arc {
            start,
            mid,
            end,
            width_nm,
        } => {
            let (chords, sag) = arc_to_conservative_chords(*start, *mid, *end);
            chords.windows(2).any(|w| {
                track_near_filled_polygon(w[0], w[1], *width_nm, exp + sag, outer, holes, false)
            })
        }
        GraphicShape::Circle {
            center,
            radius_nm,
            width_nm,
            filled,
        } => {
            if *filled {
                track_near_filled_polygon(
                    *center,
                    *center,
                    2 * *radius_nm + *width_nm,
                    exp,
                    outer,
                    holes,
                    false,
                )
            } else {
                let check_ring = |ring: &[Point]| {
                    let n = ring.len();
                    (0..n).any(|i| {
                        track_near_circle(
                            ring[i],
                            ring[(i + 1) % n],
                            0,
                            exp,
                            *center,
                            *radius_nm,
                            *width_nm,
                            false,
                            false,
                        )
                    })
                };
                check_ring(outer)
                    || holes.iter().any(|h| check_ring(h))
                    || [
                        [center[0] + *radius_nm, center[1]],
                        [center[0] - *radius_nm, center[1]],
                        [center[0], center[1] + *radius_nm],
                        [center[0], center[1] - *radius_nm],
                    ]
                    .into_iter()
                    .any(|pt| point_in_polygon_with_holes(pt, outer, holes))
            }
        }
        GraphicShape::Polygon {
            outer: mo,
            holes: mh,
            width_nm,
            filled,
        } => {
            let check_m_ring = |ring: &[Point]| {
                let n = ring.len();
                (0..n).any(|i| {
                    track_near_filled_polygon(
                        ring[i],
                        ring[(i + 1) % n],
                        *width_nm,
                        exp,
                        outer,
                        holes,
                        false,
                    )
                })
            };
            if check_m_ring(mo) || mh.iter().any(|h| check_m_ring(h)) {
                return true;
            }
            if *filled {
                if filled_polygons_overlap(mo, mh, outer, holes) {
                    return true;
                }
                let check_poly_ring = |ring: &[Point]| {
                    let n = ring.len();
                    (0..n).any(|i| {
                        track_near_filled_polygon(
                            ring[i],
                            ring[(i + 1) % n],
                            *width_nm,
                            exp,
                            mo,
                            mh,
                            false,
                        )
                    })
                };
                return check_poly_ring(outer) || holes.iter().any(|h| check_poly_ring(h));
            }
            false
        }
    }
}

pub fn mask_overlaps_graphic(m: &MaskAperture, g: &CopperGraphic) -> bool {
    let exp = m.expansion_nm.max(0);
    match &g.shape {
        GraphicShape::Segment {
            start,
            end,
            width_nm,
        } => track_near_mask(*start, *end, *width_nm, 0, m, false),
        GraphicShape::Arc {
            start,
            mid,
            end,
            width_nm,
        } => {
            let (chords, sag) = arc_to_conservative_chords(*start, *mid, *end);
            chords
                .windows(2)
                .any(|w| track_near_mask(w[0], w[1], *width_nm + 2 * sag, 0, m, false))
        }
        GraphicShape::Circle {
            center,
            radius_nm,
            width_nm,
            filled: true,
        } => track_near_mask(*center, *center, 2 * *radius_nm + *width_nm, 0, m, false),
        GraphicShape::Circle {
            center,
            radius_nm,
            width_nm,
            filled: false,
        } => match &m.shape {
            GraphicShape::Segment {
                start,
                end,
                width_nm: mw,
            } => track_near_circle(
                *start, *end, *mw, exp, *center, *radius_nm, *width_nm, false, false,
            ),
            GraphicShape::Arc {
                start,
                mid,
                end,
                width_nm: mw,
            } => {
                let (chords, sag) = arc_to_conservative_chords(*start, *mid, *end);
                chords.windows(2).any(|w| {
                    track_near_circle(
                        w[0],
                        w[1],
                        *mw,
                        exp + sag,
                        *center,
                        *radius_nm,
                        *width_nm,
                        false,
                        false,
                    )
                })
            }
            GraphicShape::Circle {
                center: mc,
                radius_nm: mr,
                width_nm: mw,
                filled: true,
            } => track_near_circle(
                *mc,
                *mc,
                2 * *mr + *mw,
                exp,
                *center,
                *radius_nm,
                *width_nm,
                false,
                false,
            ),
            GraphicShape::Circle {
                center: mc,
                radius_nm: mr,
                width_nm: mw,
                filled: false,
            } => unfilled_circles_near(*mc, *mr, *mw, *center, *radius_nm, *width_nm, exp, false),
            GraphicShape::Polygon {
                outer,
                holes,
                width_nm: mw,
                filled,
            } => {
                let check_ring = |ring: &[Point]| {
                    let n = ring.len();
                    (0..n).any(|i| {
                        track_near_circle(
                            ring[i],
                            ring[(i + 1) % n],
                            *mw,
                            exp,
                            *center,
                            *radius_nm,
                            *width_nm,
                            false,
                            false,
                        )
                    })
                };
                check_ring(outer)
                    || holes.iter().any(|h| check_ring(h))
                    || (*filled
                        && [
                            [center[0] + *radius_nm, center[1]],
                            [center[0] - *radius_nm, center[1]],
                            [center[0], center[1] + *radius_nm],
                            [center[0], center[1] - *radius_nm],
                        ]
                        .into_iter()
                        .any(|pt| point_in_polygon_with_holes(pt, outer, holes)))
            }
        },
        GraphicShape::Polygon {
            outer,
            holes,
            width_nm,
            filled,
        } => {
            let check_ring = |ring: &[Point]| {
                let n = ring.len();
                (0..n).any(|i| track_near_mask(ring[i], ring[(i + 1) % n], *width_nm, 0, m, false))
            };
            check_ring(outer)
                || holes.iter().any(|h| check_ring(h))
                || (*filled && mask_overlaps_filled_polygon(m, outer, holes))
        }
    }
}

pub fn mask_apertures_overlap(a: &MaskAperture, b: &MaskAperture, web_nm: i64) -> bool {
    let mut grown = a.clone();
    grown.expansion_nm = a.expansion_nm.max(0) + b.expansion_nm.max(0) + web_nm.max(0);
    let g = CopperGraphic {
        id: String::new(),
        net: 0,
        layer: b.layer.clone(),
        shape: b.shape.clone(),
    };
    mask_overlaps_graphic(&grown, &g)
}

pub fn point_in_pad(pt: Point, p: &Pad) -> bool {
    point_in_pad_shape(pt, p.geometry())
}

fn point_in_pad_shape(pt: Point, p: PadGeometry<'_>) -> bool {
    match p.shape {
        Shape::Union { .. } => p.parts().any(|part| point_in_pad_shape(pt, part)),
        Shape::Circle { radius_nm } => {
            *radius_nm > 0 && track_near_core(pt, pt, 0, 0, p.at, p.at, *radius_nm, false)
        }
        Shape::Rect { half_size_nm: h } => {
            (i128::from(pt[0]) - i128::from(p.at[0])).abs() < i128::from(h[0])
                && (i128::from(pt[1]) - i128::from(p.at[1])).abs() < i128::from(h[1])
        }
        Shape::RoundRect {
            half_size_nm: h,
            radius_nm: r,
        } => {
            if *r <= 0 {
                (i128::from(pt[0]) - i128::from(p.at[0])).abs() < i128::from(h[0])
                    && (i128::from(pt[1]) - i128::from(p.at[1])).abs() < i128::from(h[1])
            } else {
                track_near_core(
                    pt,
                    pt,
                    0,
                    0,
                    [p.at[0] - (h[0] - r), p.at[1] - (h[1] - r)],
                    [p.at[0] + (h[0] - r), p.at[1] + (h[1] - r)],
                    *r,
                    false,
                )
            }
        }
        Shape::BoxExact {
            center_2x,
            half_size_2x,
            radius_2x,
        } => {
            if *radius_2x <= 0 {
                (2 * i128::from(pt[0]) - i128::from(center_2x[0])).abs()
                    < i128::from(half_size_2x[0])
                    && (2 * i128::from(pt[1]) - i128::from(center_2x[1])).abs()
                        < i128::from(half_size_2x[1])
            } else {
                let core_half = [half_size_2x[0] - radius_2x, half_size_2x[1] - radius_2x];
                track_near_core_2x(
                    pt,
                    pt,
                    0,
                    0,
                    [center_2x[0] - core_half[0], center_2x[1] - core_half[1]],
                    [center_2x[0] + core_half[0], center_2x[1] + core_half[1]],
                    *radius_2x,
                    false,
                )
            }
        }
        Shape::Capsule {
            start_nm,
            end_nm,
            width_nm,
        } => {
            *width_nm > 0 && segments_near(pt, pt, *start_nm, *end_nm, i128::from(*width_nm), false)
        }
        Shape::ConvexPolygon {
            vertices,
            radius_nm,
        } => {
            if *radius_nm <= 0 {
                let n = vertices.len();
                n >= 3
                    && (0..n).all(|i| {
                        cross(
                            sub(vertices[(i + 1) % n], vertices[i]),
                            sub(pt, vertices[i]),
                        ) > 0
                    })
            } else {
                track_near_convex_poly(pt, pt, 0, 0, vertices, *radius_nm, false)
            }
        }
    }
}

fn seg_dist2_rat(pt: Point, a: Point, b: Point) -> (u128, u128) {
    let v = sub(b, a);
    let w = sub(pt, a);
    let len2 = dot(v, v);
    let proj = dot(w, v);
    if len2 <= 0 || proj <= 0 {
        return (dot(w, w) as u128, 1);
    }
    if proj >= len2 {
        let e = sub(pt, b);
        return (dot(e, e) as u128, 1);
    }
    let c = cross(v, w).unsigned_abs();
    (c * c, len2 as u128)
}

fn rat_cmp(a: (u128, u128), b: (u128, u128)) -> std::cmp::Ordering {
    let (q1, r1) = (a.0 / a.1, a.0 % a.1);
    let (q2, r2) = (b.0 / b.1, b.0 % b.1);
    q1.cmp(&q2).then_with(|| (r1 * b.1).cmp(&(r2 * a.1)))
}

fn pad_dist2_rat(pt: Point, p: PadGeometry<'_>) -> (u128, u128) {
    match &p.shape {
        Shape::Capsule {
            start_nm, end_nm, ..
        } => seg_dist2_rat(pt, *start_nm, *end_nm),
        Shape::ConvexPolygon { vertices, .. } => {
            if point_in_convex_poly(pt, vertices) {
                return (0, 1);
            }
            let n = vertices.len();
            (0..n)
                .map(|i| seg_dist2_rat(pt, vertices[i], vertices[(i + 1) % n]))
                .min_by(|&x, &y| rat_cmp(x, y))
                .unwrap_or((u128::MAX, 1))
        }
        _ => (u128::MAX, 1),
    }
}

pub fn segment_pad_interior_candidates(a: Point, b: Point, p: &Pad) -> [Option<Point>; 2] {
    let pad = p.geometry();
    if !matches!(pad.shape, Shape::Union { .. }) {
        return segment_primitive_interior_candidates(a, b, pad);
    }
    // The intersection with a non-convex union may be several intervals.
    // Find interior contacts on each primitive before choosing the nearest and deepest.
    let mut near = None;
    let mut deep = None;
    let axis = usize::from(a[0] == b[0]);
    let distance = |pt: Point| (pt[axis] - a[axis]).abs();
    for part in pad.parts() {
        for pt in segment_primitive_interior_candidates(a, b, part)
            .into_iter()
            .flatten()
        {
            if near.is_none_or(|q| distance(pt) < distance(q)) {
                near = Some(pt);
            }
            if deep.is_none_or(|q| distance(pt) > distance(q)) {
                deep = Some(pt);
            }
        }
    }
    [deep, near.filter(|q| Some(*q) != deep)]
}

fn segment_primitive_interior_candidates(
    a: Point,
    b: Point,
    p: PadGeometry<'_>,
) -> [Option<Point>; 2] {
    if a == b {
        return [point_in_pad_shape(b, p).then_some(b), None];
    }
    let k = usize::from(a[0] == b[0]);
    let len = (b[k] - a[k]).abs();
    let dir = (b[k] - a[k]).signum();
    let pt_at = |d: i64| -> Point {
        let mut pt = a;
        pt[k] += d * dir;
        pt
    };
    let (plo, phi) = p.copper_bounds();
    if a[1 - k] < plo[1 - k] || a[1 - k] > phi[1 - k] {
        return [None, None];
    }
    let (d_min, d_max) = if dir > 0 {
        ((plo[k] - a[k]).max(1), (phi[k] - a[k]).min(len))
    } else {
        ((a[k] - phi[k]).max(1), (a[k] - plo[k]).min(len))
    };
    if d_min > d_max {
        return [None, None];
    }
    let mut best_d: Option<i64> = if point_in_pad_shape(b, p) {
        Some(len)
    } else {
        None
    };
    if best_d.is_none() {
        let d_center = ((p.copper_center()[k] - a[k]) * dir).clamp(d_min, d_max);
        let d_mid = d_min + (d_max - d_min) / 2;
        for cand in [d_center, d_mid, d_min, d_max] {
            if point_in_pad_shape(pt_at(cand), p) {
                best_d = Some(cand);
                break;
            }
        }
        if best_d.is_none()
            && matches!(p.shape, Shape::Capsule { .. } | Shape::ConvexPolygon { .. })
        {
            let mut lo = d_min;
            let mut hi = d_max;
            while hi - lo > 3 {
                let m1 = lo + (hi - lo) / 3;
                let m2 = hi - (hi - lo) / 3;
                if rat_cmp(pad_dist2_rat(pt_at(m1), p), pad_dist2_rat(pt_at(m2), p))
                    != std::cmp::Ordering::Greater
                {
                    hi = m2;
                } else {
                    lo = m1;
                }
            }
            for d in lo..=hi {
                if point_in_pad_shape(pt_at(d), p) {
                    best_d = Some(d);
                    break;
                }
            }
        }
    }
    let Some(d_deep) = best_d else {
        return [None, None];
    };
    let primary = pt_at(d_deep);
    let mut lo = d_min;
    let mut hi = d_deep;
    while lo < hi {
        let mid = lo + (hi - lo) / 2;
        if point_in_pad_shape(pt_at(mid), p) {
            hi = mid;
        } else {
            lo = mid + 1;
        }
    }
    let near = (lo < d_deep).then(|| pt_at(lo));
    [Some(primary), near]
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
